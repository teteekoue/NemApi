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
    # below this size the exact comparison stays: a leftover draft must be refused
    assert "length <= 4000" in base
    # layout is dropped, content is not
    assert "compactComposer" in base


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
    # a huge prompt gets ONE attempt per editor, not three
    assert "want.length > 40000 ? 1 : 3" in deepseek
    # the failure keeps what the composer really held, to say why
    assert "lastComposerReport" in deepseek
    body = deepseek[deepseek.index("const res = await typePrompt(text, el);"):]
    body = body[: body.index("if (!input || !btn)")]
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
