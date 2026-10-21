"""Tool calls through a REAL markdown renderer + the extension's DOM extraction.

Chat pages (DeepSeek…) render the model's answer as markdown. When the
extension cannot read the original markdown (page bridge missing, React
structure changed) it rebuilds the text from the rendered HTML, and a tool
call written as plain text loses what markdown interprets:
  * backslash escapes:  \\"  -> "      \\\\n -> \\n   (sed/python code broken)
  * bare "m.to"        -> link -> [m.to](http://m.to)
  * $…$                -> math (KaTeX)
This is exactly the "\\n problem" of the DeepSeek session (Présentation.pdf).

Since v4.4 the model is asked to put each <tool> block in a ````tool code
block: code blocks are shown verbatim, so even the lossy HTML path returns the
exact JSON. These tests run the whole chain:
  model text -> markdown-it (+linkify, +KaTeX-like math) -> Chromium DOM
  -> base.js htmlToMarkdown (isolated world) -> proxy postprocess -> tool_calls
"""
import json
from pathlib import Path

import pytest

markdown_it = pytest.importorskip("markdown_it")
playwright = pytest.importorskip("playwright.sync_api")

from nemapi.services.sessions import postprocess  # noqa: E402

EXT = Path(__file__).resolve().parents[2] / "extension" / "providers"

# What the agent must receive (the command of the DeepSeek session that failed)
COMMAND = (
    "f='/tmp/jeu.html'; cp \"$f\" \"$f.bak\"; "
    "sed -E -i 's/\\[([A-Za-z_]+)\\.to\\]\\(https:\\/\\/[a-z]+\\.to\\/\\)/\\1.to/g' \"$f\"; "
    "python3 - <<'PYEOF'\nimport io\nlines = io.open('/tmp/jeu.html').read().split('\\n')\n"
    "print(len(lines), 'm.to')\nPYEOF"
)
CONTENT = '<!DOCTYPE html>\n<html lang="fr">\n<script>\nfor (const m of moves) { if (!m.to) continue; nb[m.to] = `${p}`; }\n</script>\n</html>\n'

CALLS = [
    {"name": "run_shell_command", "arguments": {"command": COMMAND}},
    {"name": "write_file", "arguments": {"file_path": "/tmp/jeu.html", "content": CONTENT}},
]
TOOLS = [
    {"type": "function", "function": {"name": "run_shell_command", "parameters": {
        "type": "object", "properties": {"command": {"type": "string"}}}}},
    {"type": "function", "function": {"name": "write_file", "parameters": {
        "type": "object", "properties": {"file_path": {"type": "string"}, "content": {"type": "string"}}}}},
]
NAMES = ["run_shell_command", "write_file"]


def model_answer(fenced: bool) -> str:
    parts = ["Je corrige le fichier puis je le réécris."]
    for c in CALLS:
        block = "<tool>" + json.dumps(c, ensure_ascii=False) + "</tool>"
        parts.append("````tool\n" + block + "\n````" if fenced else block)
    return "\n\n".join(parts)


def render(md_text: str) -> str:
    """markdown-it like a chat page: raw HTML escaped, linkify on, $…$ as KaTeX."""
    from markdown_it import MarkdownIt

    md = MarkdownIt("commonmark", {"html": False, "linkify": True}).enable("linkify")
    try:
        from mdit_py_plugins.dollarmath import dollarmath_plugin

        md.use(dollarmath_plugin)

        def katex(self, tokens, idx, options, env):
            tex = tokens[idx].content
            esc = tex.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")
            return ('<span class="katex"><span class="katex-mathml"><math><semantics><mrow></mrow>'
                    f'<annotation encoding="application/x-tex">{esc}</annotation></semantics></math></span>'
                    '<span class="katex-html" aria-hidden="true">∫garbage</span></span>')

        md.add_render_rule("math_inline", katex)
    except ImportError:  # pragma: no cover
        pass
    return md.render(md_text)


@pytest.fixture(scope="module")
def dom_extract():
    with playwright.sync_playwright() as p:
        try:
            browser = p.chromium.launch()
        except Exception as exc:  # pragma: no cover - browser not installed
            pytest.skip(f"chromium unavailable: {exc}")
        page = browser.new_page()

        def run(html: str) -> str:
            page.set_content('<!doctype html><html><body><div class="ds-markdown" id="a">'
                             + html + "</div></body></html>")
            cdp = page.context.new_cdp_session(page)
            frame_id = cdp.send("Page.getFrameTree")["frameTree"]["frame"]["id"]
            ctx_id = cdp.send("Page.createIsolatedWorld", {"frameId": frame_id, "worldName": "nemapi"})[
                "executionContextId"]

            def iso(expr):
                r = cdp.send("Runtime.evaluate", {"expression": expr, "contextId": ctx_id, "returnByValue": True})
                assert "exceptionDetails" not in r, r
                return r["result"].get("value")

            iso((EXT / "base.js").read_text())
            out = iso('NemApiBase.htmlToMarkdown(document.getElementById("a"))')
            assert iso("NemApiBase.getExtractSource()") == "html"
            return out

        yield run
        browser.close()


def _args(parsed):
    return [(c["function"]["name"], json.loads(c["function"]["arguments"])) for c in parsed.tool_calls]


def test_unfenced_tool_calls_are_damaged_by_the_renderer(dom_extract):
    """Documents the bug: plain-text tool calls do not survive the HTML path."""
    text = dom_extract(render(model_answer(fenced=False)))
    got = _args(postprocess(text, NAMES, TOOLS))
    assert len(got) == 2
    cmd = got[0][1]["command"]
    assert cmd != COMMAND
    # the python heredoc got a REAL newline instead of the two characters \n
    assert ".split('\n')" in cmd or "\\1" not in cmd


def test_fenced_tool_calls_survive_the_html_path_byte_for_byte(dom_extract):
    text = dom_extract(render(model_answer(fenced=True)))
    parsed = postprocess(text, NAMES, TOOLS)
    assert _args(parsed) == [(c["name"], c["arguments"]) for c in CALLS]
    # neither the fence nor the code-block label leak to the agent
    assert parsed.text == "Je corrige le fichier puis je le réécris."


def test_fenced_tool_calls_from_original_markdown_are_exact():
    """React/page-bridge path: the raw markdown itself."""
    parsed = postprocess(model_answer(fenced=True), NAMES, TOOLS)
    assert _args(parsed) == [(c["name"], c["arguments"]) for c in CALLS]
    assert parsed.text == "Je corrige le fichier puis je le réécris."


def test_unfenced_tool_calls_from_original_markdown_are_exact():
    parsed = postprocess(model_answer(fenced=False), NAMES, TOOLS)
    assert _args(parsed) == [(c["name"], c["arguments"]) for c in CALLS]


def test_html_path_keeps_markdown_files_with_inner_fences(dom_extract):
    """A README written by write_file contains ``` fences: the 4-backtick fence holds."""
    readme = "# Demo\n\n```bash\nnpm i\necho \"$HOME\" \\\\\n```\n"
    call = {"name": "write_file", "arguments": {"file_path": "README.md", "content": readme}}
    md_text = "````tool\n<tool>" + json.dumps(call) + "</tool>\n````"
    text = dom_extract(render(md_text))
    assert _args(postprocess(text, NAMES, TOOLS)) == [("write_file", call["arguments"])]
