"""Tool-call parsing of web answers → real OpenAI tool_calls.

Formats taken from a real Qwen Code + DeepSeek (chat.deepseek.com) session:
DeepSeek mixes our <tool>{json}</tool> contract with its native DSML syntax
(full-width or ASCII bars, single or doubled, spaced, missing closing tags) and
leaves stray DSML closing tags after valid <tool> blocks.
"""
import json

import pytest

from nemapi.utils.tools_format import loads_lenient, parse_tool_calls

NAMES = ["read_file", "run_shell_command", "write_file", "glob", "respond_in_schema"]
TOOLS = [
    {"type": "function", "function": {"name": "read_file", "parameters": {
        "type": "object", "properties": {"file_path": {"type": "string"}, "limit": {"type": "integer"}}}}},
    {"type": "function", "function": {"name": "run_shell_command", "parameters": {
        "type": "object", "properties": {"command": {"type": "string"}, "description": {"type": "string"},
                                         "timeout": {"type": "number"}, "is_background": {"type": "boolean"}}}}},
    {"type": "function", "function": {"name": "write_file", "parameters": {
        "type": "object", "properties": {"file_path": {"type": "string"}, "content": {"type": "string"}}}}},
]


def calls_of(text, names=NAMES, tools=TOOLS):
    rest, calls = parse_tool_calls(text, names, tools)
    return rest, [(c["function"]["name"], json.loads(c["function"]["arguments"])) for c in calls]


# ------------------------------------------------------------------ DSML
MEM = "/home/nemesis/.qwen/projects/x/memory/MEMORY.md"


@pytest.mark.parametrize("open_tag,close_tag,block", [
    ("<｜DSML｜", "</｜DSML｜", "tool_calls"),       # documented V4 form
    ("<｜DSML｜", "</｜DSML｜", "function_calls"),
    ("<｜DSML｜ ", "</｜DSML｜ ", "calls"),            # V4.1: leading space before tag names
    ("<｜｜DSML｜｜ ", "</｜｜DSML｜｜ ", "calls"),      # DeepSeek web (the PDF): doubled bars
    ("< ｜｜ DSML ｜｜  ", "</ ｜｜ DSML ｜｜  ", "calls"),  # same, as printed in the PDF
    ("<||DSML||", "</||DSML||", "tool_calls"),       # degraded ASCII bars
    ("<|DSML|", "</|DSML|", "tool_calls"),
])
def test_dsml_variants(open_tag, close_tag, block):
    text = (
        "Je lis l'index de la mémoire.\n"
        f"{open_tag}{block}>\n"
        f'{open_tag}invoke name="read_file">\n'
        f'{open_tag}parameter name="file_path" string="true">{MEM}{close_tag}parameter>\n'
        f'{open_tag}parameter name="limit" string="false">50{close_tag}parameter>\n'
        f"{close_tag}invoke>\n"
        f"{close_tag}{block}>"
    )
    rest, calls = calls_of(text)
    assert calls == [("read_file", {"file_path": MEM, "limit": 50})]
    assert rest == "Je lis l'index de la mémoire."
    assert "DSML" not in rest


def test_dsml_several_invokes_json_values_and_missing_closers():
    text = (
        "<｜DSML｜tool_calls>"
        '<｜DSML｜invoke name="run_shell_command">'
        '<｜DSML｜parameter name="command" string="true">echo "$HOME" && ls -la</｜DSML｜parameter>'
        '<｜DSML｜parameter name="timeout" string="false">60000</｜DSML｜parameter>'
        '<｜DSML｜parameter name="is_background" string="false">false</｜DSML｜parameter>'
        "</｜DSML｜invoke>"
        '<｜DSML｜invoke name="write_file">'
        '<｜DSML｜parameter name="file_path" string="true">a.json</｜DSML｜parameter>'
        '<｜DSML｜parameter name="content" string="true">\n{"a": [1, 2]}\n</｜DSML｜parameter>'
        # closing invoke + block missing (answer cut)
    )
    rest, calls = calls_of(text)
    assert calls == [
        ("run_shell_command", {"command": 'echo "$HOME" && ls -la', "timeout": 60000, "is_background": False}),
        ("write_file", {"file_path": "a.json", "content": '{"a": [1, 2]}'}),
    ]
    assert rest == ""


def test_dsml_invoke_without_block_and_end_token():
    text = ('<｜DSML｜invoke name="glob"><｜DSML｜parameter name="pattern" string="true">**/*.py'
            "</｜DSML｜parameter></｜DSML｜invoke><｜end▁of▁sentence｜>")
    rest, calls = calls_of(text)
    assert calls == [("glob", {"pattern": "**/*.py"})] and rest == ""


def test_dsml_schema_coercion_when_string_attribute_is_wrong():
    text = ('<｜DSML｜tool_calls><｜DSML｜invoke name="read_file">'
            '<｜DSML｜parameter name="file_path" string="false">42</｜DSML｜parameter>'
            '<｜DSML｜parameter name="limit" string="true">10</｜DSML｜parameter>'
            "</｜DSML｜invoke></｜DSML｜tool_calls>")
    _, calls = calls_of(text)
    assert calls == [("read_file", {"file_path": "42", "limit": 10})]


# ------------------------------------------------------------------ the PDF answer
PDF_ANSWER = (
    "Je vais explorer les capacités matérielles et logicielles de la machine (CPU, RAM, disque,\n"
    "GPU, OS, réseau, outils installés). Ces commandes sont en lecture seule.\n"
    '<tool>{"name": "run_shell_command", "arguments": {"command": "echo \'=== OS ===\';\n'
    "uname -a; cat /etc/os-release 2>/dev/null | head -8; echo; echo '=== CPU ==='; lscpu\n"
    '2>/dev/null | head -25", "description": "Collecte OS, CPU, RAM, disques\n'
    'et GPU en lecture seule", "timeout": 60000}}</tool>\n'
    '<tool>{"name": "run_shell_command", "arguments": {"command": "for c in gcc make; do printf \'%-12s\' '
    '\\"$c\\"; command -v \\"$c\\" >/dev/null 2>&1 && \\"$c\\" --version 2>&1 | head -1 || echo \'(absent)\'; done",'
    ' "description": "Vérifie versions des runtimes et outils installés", "timeout": 90000}}</tool> '
    '<tool>{"name":\n"run_shell_command", "arguments": {"command": "echo \\"DISPLAY=$DISPLAY\\"; '
    'echo \\"MEM_KB=$(grep MemTotal /proc/meminfo)\\"", "description": "Vérifie réseau, session\n'
    'graphique et processus d\'affichage", "timeout": 60000}}</tool>\n'
    "</ ｜｜ DSML ｜｜  parameter>\n</ ｜｜ DSML ｜｜  invoke>\n</ ｜｜ DSML ｜｜  calls>"
)


def test_pdf_answer_three_tool_blocks_and_stray_dsml_closers():
    rest, calls = calls_of(PDF_ANSWER)
    assert [n for n, _ in calls] == ["run_shell_command"] * 3
    # raw newlines inside JSON strings (as typed by the model) are kept
    assert calls[0][1]["command"].startswith("echo '=== OS ===';\nuname -a")
    assert calls[0][1]["timeout"] == 60000
    assert '"$c" --version' in calls[1][1]["command"]
    assert calls[2][1]["command"] == 'echo "DISPLAY=$DISPLAY"; echo "MEM_KB=$(grep MemTotal /proc/meminfo)"'
    assert rest.startswith("Je vais explorer") and rest.endswith("en lecture seule.")
    assert "DSML" not in rest and "<tool>" not in rest


# ------------------------------------------------------------------ lenient JSON
def test_unescaped_inner_quotes_trailing_comma_missing_brace():
    text = '<tool>{"name": "run_shell_command", "arguments": {"command": "echo "hello world" && ls", "timeout": 5,}</tool>'
    _, calls = calls_of(text)
    assert calls == [("run_shell_command", {"command": 'echo "hello world" && ls', "timeout": 5})]


def test_invalid_escapes_are_kept_literally():
    text = r'<tool>{"name": "run_shell_command", "arguments": {"command": "grep -E \d+ file \$HOME"}}</tool>'
    _, calls = calls_of(text)
    assert calls == [("run_shell_command", {"command": r"grep -E \d+ file \$HOME"})]


def test_python_style_dict_and_string_arguments():
    _, calls = calls_of("<tool>{'name': 'read_file', 'arguments': {'file_path': 'a.py'}}</tool>")
    assert calls == [("read_file", {"file_path": "a.py"})]
    _, calls = calls_of('<tool>{"name": "read_file", "arguments": "{\\"file_path\\": \\"b.py\\"}"}</tool>')
    assert calls == [("read_file", {"file_path": "b.py"})]


def test_unclosed_tool_block_at_end():
    rest, calls = calls_of('Je lis.\n<tool>{"name": "read_file", "arguments": {"file_path": "main.py"}}')
    assert calls == [("read_file", {"file_path": "main.py"})] and rest == "Je lis."


def test_loads_lenient_basics():
    assert loads_lenient('```json\n{"a": 1}\n```') == {"a": 1}
    assert loads_lenient('{"a": "x\ny"}') == {"a": "x\ny"}
    assert loads_lenient('{"a": [1, 2,], "b": {"c": true') == {"a": [1, 2], "b": {"c": True}}
    with pytest.raises(ValueError):
        loads_lenient("pas du json")


# ------------------------------------------------------------------ other syntaxes
def test_qwen3_coder_xml():
    text = ("<tool_call>\n<function=write_file>\n<parameter=file_path>\nsrc/a.py\n</parameter>\n"
            "<parameter=content>\ndef f():\n    return 1\n</parameter>\n</function>\n</tool_call>")
    rest, calls = calls_of(text)
    assert calls == [("write_file", {"file_path": "src/a.py", "content": "def f():\n    return 1"})] and rest == ""


def test_deepseek_v3_special_tokens():
    text = ("<｜tool▁calls▁begin｜><｜tool▁call▁begin｜>function<｜tool▁sep｜>read_file\n"
            '```json\n{"file_path": "x.md"}\n```<｜tool▁call▁end｜><｜tool▁calls▁end｜>')
    rest, calls = calls_of(text)
    assert calls == [("read_file", {"file_path": "x.md"})] and rest == ""


def test_anthropic_invoke_and_function_shapes():
    _, calls = calls_of('<invoke name="read_file"><parameter name="file_path">a</parameter></invoke>')
    assert calls == [("read_file", {"file_path": "a"})]
    _, calls = calls_of('<tool>{"function": {"name": "glob", "arguments": {"pattern": "*"}}}</tool>')
    assert calls == [("glob", {"pattern": "*"})]
    _, calls = calls_of('<tool>[{"name": "glob", "arguments": {}}, {"name": "read_file", "arguments": {"file_path": "b"}}]</tool>')
    assert calls == [("glob", {}), ("read_file", {"file_path": "b"})]


def test_fenced_json_only_for_known_tools():
    rest, calls = calls_of('Voici :\n```json\n{"name": "read_file", "arguments": {"file_path": "a"}}\n```')
    assert calls == [("read_file", {"file_path": "a"})] and rest == "Voici :"
    text = 'Exemple de config :\n```json\n{"name": "mon-projet", "arguments": {"x": 1}}\n```'
    rest, calls = calls_of(text)
    assert calls == [] and rest == text


def test_tool_name_canonicalisation():
    _, calls = calls_of('<tool>{"name": "Read_File", "arguments": {"file_path": "a"}}</tool>')
    assert calls[0][0] == "read_file"
    _, calls = calls_of('<tool>{"name": "functions.run_shell_command", "arguments": {"command": "ls"}}</tool>')
    assert calls[0][0] == "run_shell_command"


def test_plain_answers_are_untouched_except_dsml_noise():
    text = "Utilise la balise <tool> pour appeler un outil.\n\n```python\nx = {'a': 1}\n```"
    assert calls_of(text) == (text, [])
    rest, calls = calls_of("Terminé.\n</｜DSML｜parameter></｜DSML｜invoke></｜DSML｜calls>")
    assert calls == [] and rest == "Terminé."


def test_remaining_text_has_no_empty_fence():
    rest, calls = calls_of('Je lance :\n```xml\n<tool>{"name": "glob", "arguments": {"pattern": "*"}}</tool>\n```\nOK')
    assert calls == [("glob", {"pattern": "*"})]
    assert "```" not in rest and rest.startswith("Je lance :") and rest.endswith("OK")


# ---------------------------------------------------------------- v4.4: verbatim tool content

def test_prompt_asks_for_fenced_tool_blocks():
    from nemapi.utils.tools_format import serialize_tools_prompt
    from nemapi.utils.history import TOOL_REMINDER, side_request_prompt

    p = serialize_tools_prompt([{"type": "function", "function": {"name": "x", "parameters": {}}}])
    assert "````tool\n<tool>" in p and "no code fence" not in p
    assert "````tool" in TOOL_REMINDER
    assert "````tool" in side_request_prompt("b", ["x"])


def test_cleaning_never_touches_tool_blocks_or_fences():
    from nemapi.utils.tools_format import clean_assistant_text

    content = ("<details><summary>Copy</summary>&amp; &lt;b&gt;</details>\\nCopy\\n"
               "for(;;){ continue; } // Download JSON Regenerate")
    call = {"name": "write_file", "arguments": {"file_path": "a.html", "content": content}}
    raw = "Texte &amp; Download\nCopy\n<tool>" + json.dumps(call) + "</tool>\n" \
          "````tool\n<tool>" + json.dumps(call) + "</tool>\n````\n```html\n<details>&amp;\nCopy\n</details>\n```"
    out = clean_assistant_text(raw)
    assert out.startswith("Texte &")             # prose: entity decoded, UI word gone
    assert "\nCopy\n<tool>" not in out           # prose UI line removed
    assert out.count(json.dumps(call)) == 2      # both tool blocks byte-for-byte
    assert "<details>&amp;\nCopy\n</details>" in out  # code fence verbatim
    _, calls = parse_tool_calls(out, ["write_file"])
    assert [json.loads(c["function"]["arguments"])["content"] for c in calls] == [content, content]


def test_dsml_parameter_values_are_not_cleaned():
    from nemapi.utils.tools_format import clean_assistant_text

    raw = ('<｜DSML｜tool_calls><｜DSML｜invoke name="write_file">'
           '<｜DSML｜parameter name="content" string="true">a &amp; b\nCopy\n<details>x</details></｜DSML｜parameter>'
           "</｜DSML｜invoke></｜DSML｜tool_calls>")
    _, calls = parse_tool_calls(clean_assistant_text(raw), ["write_file"])
    assert json.loads(calls[0]["function"]["arguments"])["content"] == "a &amp; b\nCopy\n<details>x</details>"


def test_renderer_autolinks_are_undone():
    from nemapi.utils.tools_format import clean_assistant_text

    call = '<tool>{"name": "w", "arguments": {"c": "nb[[m.to](https://m.to/)]=rowOf([m.to](http://m.to))"}}</tool>'
    _, calls = parse_tool_calls(clean_assistant_text(call), ["w"])
    assert json.loads(calls[0]["function"]["arguments"])["c"] == "nb[m.to]=rowOf(m.to)"
    # a real markdown link is kept
    assert clean_assistant_text("voir [la doc](https://example.com/doc)") == "voir [la doc](https://example.com/doc)"


def test_four_backtick_fence_parsing_variants():
    call = {"name": "run_shell_command", "arguments": {"command": "printf 'a\\nb' | sed 's/\\//x/'"}}
    j = json.dumps(call)
    for text in (
        "Je lance.\n\n````tool\n<tool>" + j + "</tool>\n````",
        "Je lance.\ntool\n```tool\n<tool>" + j + "</tool>\n```",   # DOM path: label line + 3 ticks
        "Je lance.\n\n````tool\n" + j + "\n````",                   # fence without <tool> tags
        "Je lance.\n\n~~~~tool\n<tool>" + j + "</tool>\n~~~~",
    ):
        remaining, calls = parse_tool_calls(text, ["run_shell_command"])
        assert remaining == "Je lance.", (text, remaining)
        assert json.loads(calls[0]["function"]["arguments"]) == call["arguments"]
