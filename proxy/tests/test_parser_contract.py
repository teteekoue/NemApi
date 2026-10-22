"""Contract tests of the tool-call parser and of the incremental context.

These tests encode the CONTRACT the agent relies on, not the current
implementation:

* the ````tool` contract is the primary format: one fenced block, one
  ``<tool>{json}</tool>`` line, nothing else;
* the arguments must reach the agent byte for byte (backslashes, newlines,
  ``$`` and quotes are what a shell command or a file content is made of);
* the leftover text must be prose only — no tool block, no DSML tag, no
  empty fence;
* a call that was cut off is NEVER executed;
* in incremental mode, only what the discussion does not hold yet is typed.
"""
import json

import pytest

from nemapi.services.sessions import postprocess
from nemapi.utils.continuation import NOTE_NOT_EXECUTED
from nemapi.utils.history import History, format_delta_prompt, select_new
from nemapi.utils.tools_format import (
    TOOL_FENCE,
    clean_assistant_text,
    format_messages_prompt,
    parse_tool_calls,
)

NAMES = ["read_file", "run_shell_command", "write_file"]
TOOLS = [
    {"type": "function", "function": {"name": "read_file", "parameters": {
        "type": "object", "properties": {"file_path": {"type": "string"}}}}},
    {"type": "function", "function": {"name": "run_shell_command", "parameters": {
        "type": "object", "properties": {
            "command": {"type": "string"},
            "timeout": {"type": "number"},
            "is_background": {"type": "boolean"},
            "env": {"type": "object"},
        }}}},
    {"type": "function", "function": {"name": "write_file", "parameters": {
        "type": "object", "properties": {
            "file_path": {"type": "string"},
            "content": {"type": "string"},
            "lines": {"type": "array", "items": {"type": "string"}},
        }}}},
]


def calls_of(text, names=NAMES, tools=TOOLS):
    rest, calls = parse_tool_calls(text, names, tools)
    return rest, [(c["function"]["name"], json.loads(c["function"]["arguments"])) for c in calls]


def fenced(payload):
    """The canonical shape the prompt asks the model for."""
    return f"{TOOL_FENCE}tool\n{payload}\n{TOOL_FENCE}"


# ------------------------------------------------------------------ contract
def test_fenced_tool_block_is_parsed_and_leaves_no_residue():
    text = (
        "Je lis le fichier.\n\n"
        + fenced('<tool>{"name": "read_file", "arguments": {"file_path": "/tmp/a.txt"}}</tool>')
        + "\n\nJe continue."
    )
    rest, calls = calls_of(text)
    assert calls == [("read_file", {"file_path": "/tmp/a.txt"})]
    # the leftover is prose only
    assert "```" not in rest
    assert "<tool" not in rest
    assert "Je lis le fichier." in rest
    assert "Je continue." in rest


def test_several_tool_blocks_are_all_parsed_in_order():
    text = (
        fenced('<tool>{"name": "read_file", "arguments": {"file_path": "/a"}}</tool>')
        + "\n"
        + fenced('<tool>{"name": "read_file", "arguments": {"file_path": "/b"}}</tool>')
    )
    rest, calls = calls_of(text)
    assert [c[1]["file_path"] for c in calls] == ["/a", "/b"]
    assert "```" not in rest


def test_arguments_are_preserved_byte_for_byte():
    # a shell command with backslashes, quotes, $ and a real newline
    raw = 'echo "a\\tb" | grep $HOME && printf \'x\\ny\' > f.txt'
    payload = json.dumps({"name": "run_shell_command", "arguments": {"command": raw}})
    text = fenced(f"<tool>{payload}</tool>")
    rest, calls = calls_of(text)
    assert calls == [("run_shell_command", {"command": raw})]
    assert "```" not in rest


def test_multiline_file_content_survives():
    content = "line1\nline2\n\ttabbed\n"
    payload = json.dumps({"name": "write_file", "arguments": {"file_path": "/f", "content": content}})
    rest, calls = calls_of(fenced(f"<tool>{payload}</tool>"))
    assert calls == [("write_file", {"file_path": "/f", "content": content})]


def test_schema_coercion_of_string_arguments():
    payload = json.dumps({
        "name": "run_shell_command",
        "arguments": {"command": "ls", "timeout": "30", "is_background": "true"},
    })
    rest, calls = calls_of(fenced(f"<tool>{payload}</tool>"))
    assert calls == [("run_shell_command", {"command": "ls", "timeout": 30, "is_background": True})]


def test_object_argument_given_as_a_json_string_is_expanded():
    payload = json.dumps({
        "name": "run_shell_command",
        "arguments": {"command": "ls", "env": "{\"A\": \"1\"}"},
    })
    rest, calls = calls_of(fenced(f"<tool>{payload}</tool>"))
    assert calls[0][1]["env"] == {"A": "1"}


def test_tool_block_outside_a_fence_is_still_parsed():
    rest, calls = calls_of('<tool>{"name": "read_file", "arguments": {"file_path": "/a"}}</tool>')
    assert calls == [("read_file", {"file_path": "/a"})]
    assert "<tool" not in rest


def test_a_call_whose_name_is_unknown_keeps_its_block_in_the_text():
    # a fenced JSON naming a tool the agent does not have: it must NOT be
    # executed, the text stays visible
    payload = json.dumps({"name": "delete_everything", "arguments": {}})
    text = f"```json\n{payload}\n```"
    rest, calls = calls_of(text)
    assert calls == []
    assert "delete_everything" in rest


def test_empty_answer_yields_nothing():
    assert parse_tool_calls("", NAMES, TOOLS) == ("", [])
    assert parse_tool_calls("   \n ", NAMES, TOOLS) == ("", [])


def test_answer_without_any_call_is_untouched():
    text = "Voici la réponse.\n\n```python\nprint(1)\n```"
    rest, calls = calls_of(text)
    assert calls == []
    assert "print(1)" in rest


def test_remaining_text_never_contains_dsml_tags():
    text = (
        '<｜DSML｜tool_calls>\n<｜DSML｜invoke name="read_file">\n'
        '<｜DSML｜parameter name="file_path" string="true">/a</｜DSML｜parameter>\n'
        "<｜DSML｜invoke>\n<｜DSML｜tool_calls>"
    )
    rest, calls = calls_of(text)
    assert calls == [("read_file", {"file_path": "/a"})]
    assert "DSML" not in rest
    assert rest.strip() == ""


# ------------------------------------------------------- never execute a cut
def test_a_truncated_call_is_never_executed():
    text = (
        "J'écris le fichier.\n\n"
        + fenced('{"name": "write_file", "arguments": {"file_path": "/f", "content": "part1')
    )
    parsed = postprocess(text, NAMES, TOOLS)
    assert parsed.tool_calls == []
    assert NOTE_NOT_EXECUTED in parsed.text


def test_a_complete_call_is_executed_even_with_trailing_prose():
    text = (
        "J'écris le fichier.\n\n"
        + fenced('<tool>{"name": "write_file", "arguments": {"file_path": "/f", "content": "ok"}}</tool>')
        + "\n\nC'est fait."
    )
    parsed = postprocess(text, NAMES, TOOLS)
    assert len(parsed.tool_calls) == 1
    assert json.loads(parsed.tool_calls[0]["function"]["arguments"])["content"] == "ok"
    assert "C'est fait." in parsed.text


# --------------------------------------------------------------- the prompt
def test_the_prompt_asks_for_the_fenced_contract():
    prompt = format_messages_prompt(
        [{"role": "user", "content": "hello"}],
        tools=TOOLS,
    )
    assert TOOL_FENCE + "tool" in prompt
    assert "<tool>" in prompt
    for name in NAMES:
        assert name in prompt


def test_images_are_never_typed_as_text():
    prompt = format_messages_prompt([
        {"role": "user", "content": [
            {"type": "text", "text": "regarde"},
            {"type": "image_url", "image_url": {"url": "data:image/png;base64,AAAA"}},
        ]},
    ])
    assert "regarde" in prompt
    assert "base64" not in prompt
    assert "data:image" not in prompt


def test_roles_are_labelled():
    prompt = format_messages_prompt([
        {"role": "system", "content": "sys"},
        {"role": "user", "content": "u1"},
        {"role": "assistant", "content": "a1"},
        {"role": "user", "content": "u2"},
    ])
    assert "User: u1" in prompt
    assert "Assistant: a1" in prompt
    assert "User: u2" in prompt


# ------------------------------------------------------- incremental context
def _hist(messages):
    return History.from_messages(messages)


def test_only_the_new_messages_are_typed():
    h = _hist([
        {"role": "user", "content": "q1"},
        {"role": "assistant", "content": "a1"},
        {"role": "user", "content": "q2"},
    ])
    view = [h.chain[0], h.chain[1]]
    assert select_new(h, view, [], []) == [2]


def test_a_repeated_user_message_is_typed_again():
    # "continue" typed twice must reach the web model twice
    h = _hist([
        {"role": "user", "content": "q1"},
        {"role": "assistant", "content": "a1"},
        {"role": "user", "content": "continue"},
        {"role": "user", "content": "continue"},
    ])
    view = [h.chain[0], h.chain[1], h.chain[2]]
    assert select_new(h, view, [], []) == [3]


def test_assistant_messages_are_never_re_typed():
    h = _hist([
        {"role": "user", "content": "q1"},
        {"role": "assistant", "content": "a1"},
        {"role": "user", "content": "q2"},
    ])
    view = [h.chain[0], h.chain[1]]
    assert select_new(h, view, [], []) == [2]


def test_an_identical_retry_types_nothing():
    h = _hist([{"role": "user", "content": "q1"}])
    assert select_new(h, list(h.chain), [], []) == []


def test_delta_prompt_labels_the_roles():
    h = _hist([
        {"role": "user", "content": "q1"},
        {"role": "assistant", "content": "a1"},
        {"role": "user", "content": "q2"},
    ])
    body = format_delta_prompt(h, [2])
    assert "q2" in body
    assert "q1" not in body


def test_delta_prompt_names_the_tool_of_a_result():
    h = _hist([
        {"role": "user", "content": "q1"},
        {"role": "assistant", "content": "", "tool_calls": [
            {"id": "call_1", "type": "function",
             "function": {"name": "run_shell_command", "arguments": "{}"}}]},
        {"role": "tool", "tool_call_id": "call_1", "content": "ok"},
    ])
    body = format_delta_prompt(h, [2])
    assert "run_shell_command" in body
    assert "ok" in body


# ------------------------------------------------------------- the cleanup
def test_the_cleanup_never_touches_a_code_block():
    text = "Voici:\n\n```bash\necho \"copy\" # stop\n```\n\nFin."
    out = clean_assistant_text(text)
    assert "echo \"copy\" # stop" in out
    assert "Fin." in out


def test_the_cleanup_does_not_decode_entities_inside_a_tool_block():
    payload = '<tool>{"name": "write_file", "arguments": {"content": "a &amp; b"}}</tool>'
    out = clean_assistant_text(fenced(payload))
    assert "a &amp; b" in out
