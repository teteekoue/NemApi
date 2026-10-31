"""The composer of a provider, with a VERY large prompt (agent integration).

The report from the field: an agent task reaching a 147 006-character prompt
stopped sending. The text appeared in the composer, disappeared, appeared
again — a loop — and the job failed with "the prompt could not be written into
the composer (the page keeps clearing it)".

Two causes, both in the shared composer code:

1. `writeAndConfirm` compared the composer with what it had written
   **character for character**. No rich editor round-trips a 150 KB text
   faithfully (newlines, non-breaking spaces, blank lines), so the comparison
   failed on text that was in fact there;
2. it judged the composer 320 ms after the insert. A rich editor commits a
   150 KB paste over several frames: the reading is a PREFIX, which the caller
   took for "the app wiped my text" — it cleared the field and typed the whole
   prompt again, which is exactly the loop the user saw.

The static checks below always run. The behavioural ones run the real
`base.js` + `deepseek.js` in a DOM, and are skipped when `node` is not
available with `jsdom` installed (the same rule as the other DOM tests of this
repo, which need Chromium).
"""
import json
import shutil
import subprocess
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]
EXT = ROOT / "extension"
HARNESS = Path(__file__).resolve().parent / "js" / "long_prompt.mjs"


# --------------------------------------------------------------- static checks
def test_the_composer_check_is_tolerant_for_long_prompts():
    base = (EXT / "providers" / "base.js").read_text()
    assert "function composerMatches(" in base
    assert "function composerReport(" in base
    # layout is dropped, content is not
    assert "compactComposer" in base
    # a long text that comes back word for word is accepted, and a short one
    # must still match exactly (a leftover draft is never sent)
    body = base[base.index("function composerMatches("):]
    body = body[: body.index("function composerReport(")]
    assert "b.length < 2000" in body
    assert "j === wordsB.length" in body


def test_a_long_insert_is_not_judged_before_it_is_committed():
    base = (EXT / "providers" / "base.js").read_text()
    assert "waitForComposerToSettle" in base
    # the settle wait only applies to big prompts: a small one is instant
    assert "if (want.length > 4000) await waitForComposerToSettle(el);" in base
    body = base[base.index("async function waitForComposerToSettle"):]
    body = body[: body.index("async function writeAndConfirm")]
    assert "timeout = 12000" in body, "the wait must stay bounded"


def test_a_failed_write_does_not_clear_the_composer_or_loop():
    deepseek = (EXT / "providers" / "deepseek.js").read_text()
    # the prompt is written ONCE, whatever its size: a second write only
    # happens when the composer is empty (a real wipe) — v5.5.0 wrote it three
    # times and then wiped it, which is the "it is written and disappears"
    typing = deepseek[deepseek.index("async function typePrompt"):]
    typing = typing[: typing.index("async function sendPrompt")]
    assert typing.count("writeAndConfirm") == 1
    assert "want.length > 40000 ? 1 : 3" not in typing
    # the failure keeps what the composer really held, to say why
    assert "lastComposerReport" in deepseek
    body = deepseek[deepseek.index("const res = await typePrompt(text, el);"):]
    body = body[: body.index("if (!input)")]
    assert 'B.setTextareaValue(el, "")' not in body, (
        "clearing a failed attempt erases the evidence and makes the flicker"
    )


def test_a_prompt_already_in_the_composer_is_sent_anyway():
    deepseek = (EXT / "providers" / "deepseek.js").read_text()
    assert "tolerated comparison" in deepseek
    assert "too large for this composer" in deepseek


def test_extension_javascript_parses():
    node = shutil.which("node")
    if not node:
        pytest.skip("node is not installed")
    for path in [EXT / "providers" / "base.js", EXT / "providers" / "deepseek.js"]:
        result = subprocess.run([node, "--check", str(path)], capture_output=True, text=True)
        assert result.returncode == 0, f"{path}: {result.stderr}"


# ------------------------------------------------------------- real behaviour
def _node_with_jsdom() -> str:
    node = shutil.which("node")
    if not node:
        pytest.skip("node is not installed")
    probe = subprocess.run(
        [node, "-e", "require.resolve('jsdom')"], capture_output=True, text=True, cwd=ROOT
    )
    if probe.returncode != 0:
        pytest.skip("jsdom is not installed (npm install jsdom)")
    return node


def test_a_150kb_prompt_is_sent_once_and_never_loops():
    node = _node_with_jsdom()
    env_cwd = ROOT  # so that `jsdom` resolves from the repository
    result = subprocess.run(
        [node, str(HARNESS)], capture_output=True, text=True, cwd=env_cwd, timeout=180
    )
    print(result.stdout)
    if result.returncode == 77:  # the harness could not load jsdom
        pytest.skip("jsdom is not installed (npm install jsdom)")
    assert result.returncode == 0, result.stdout + result.stderr
    assert "all checks passed" in result.stdout
    # the scenario really went through the 150 KB send
    assert "a 150 KB prompt committed over 1.5 s is sent" in result.stdout
    assert json.dumps("a re-flowed long prompt is sent (tolerant comparison)").strip('"') in result.stdout


# ------------------------------------------------- the send control of a site
def test_the_send_prompt_is_written_once_and_the_control_is_searched_for():
    base = (EXT / "providers" / "base.js").read_text()
    body = base[base.index("async function sendWithProof("):]
    body = body[: body.index("/**\n   * Click the send control")]
    # one write per surface: the re-paste loop came from rewriting on every try
    assert "attempts: 1, log" in body
    assert "if (!holds(el))" in body
    # an unknown send control falls back to Enter, then to the next surface
    assert "pressEnter(el)" in body
    assert "no usable send control" in body


def test_kimi_and_zai_can_find_a_composer_they_no_longer_know():
    for name in ("kimi", "zai"):
        source = (EXT / "providers" / f"{name}.js").read_text()
        assert "B.discoverEditable" in source, f"{name}: no composer fallback"
        assert "B.discoverSend" in source, f"{name}: no send-control fallback"


def test_deepseek_composer_code_is_left_alone():
    """The user's constraint: DeepSeek works, do not touch its selectors."""
    deepseek = (EXT / "providers" / "deepseek.js").read_text()
    assert "discoverEditable" not in deepseek
    assert "discoverSend" not in deepseek
    # and it keeps its own send path (not the shared sendWithProof)
    assert "B.sendWithProof" not in deepseek


def test_the_send_control_harness_runs_the_real_extension():
    """The Kimi re-paste loop, replayed on the real base.js in a DOM."""
    node = shutil.which("node")
    if not node:
        pytest.skip("node is not installed")
    harness = Path(__file__).resolve().parent / "js" / "send_control.mjs"
    result = subprocess.run(
        [node, str(harness)], capture_output=True, text=True, cwd=ROOT, timeout=240
    )
    print(result.stdout)
    if result.returncode == 77:
        pytest.skip("jsdom is not installed (npm install jsdom)")
    assert result.returncode == 0, result.stdout + result.stderr
    assert "all checks passed" in result.stdout
    assert "written exactly once (no re-paste loop)" in result.stdout


def test_the_same_prompt_is_not_retyped_forever_deepseek():
    """The 17 562-character prompt of the report, replayed on the real adapter.

    v5.5.0 log: `Job error: DeepSeek: the prompt could not be written into the
    composer (the page keeps clearing it)` — while a 127-character prompt went
    through on the same page. The adapter re-read the composer with an exact
    comparison (an editor that reflows text never matches it), concluded "the
    app wiped it", wiped it for real and typed it again.
    """
    node = shutil.which("node")
    if not node:
        pytest.skip("node is not installed")
    harness = Path(__file__).resolve().parent / "js" / "deepseek_composer.mjs"
    result = subprocess.run([node, str(harness)], capture_output=True, text=True, cwd=ROOT, timeout=600)
    print(result.stdout[-4000:])
    if result.returncode == 77:
        pytest.skip("jsdom is not installed (npm install jsdom)")
    assert result.returncode == 0, result.stdout[-4000:] + result.stderr[-2000:]
    assert "all checks passed" in result.stdout
    for size in ("127", "1122", "17562"):
        assert f"{size} chars: the prompt is written once, not re-typed" in result.stdout


def test_the_deepseek_adapter_reads_its_composer_tolerantly():
    deepseek = (EXT / "providers" / "deepseek.js").read_text()
    holds = deepseek[deepseek.index("function composerHolds"):]
    holds = holds[: holds.index("\n  }")]
    # every size, not only the huge one: a 17 KB prompt is not "big" by the old
    # 4 000-character threshold, yet it is exactly what reflow broke
    assert "composerMatches" in holds
    assert "text.length > 4000" not in holds
    # writing twice is allowed only when the composer is EMPTY (a real wipe)
    typing = deepseek[deepseek.index("async function typePrompt"):]
    typing = typing[: typing.index("async function sendPrompt")]
    assert "if (holds())" in typing
    # a disabled send control keeps the text (Enter fallback), it never rewrites
    assert "Enter fallback" in typing
    assert "the app wiped the text)" not in typing
