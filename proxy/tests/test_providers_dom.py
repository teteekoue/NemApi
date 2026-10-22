"""Per-provider DOM harness: the prompt must be written AND actually sent.

Each scenario reproduces the composer of a real provider, with the two
behaviours that made the old code fail silently:

* the app keeps its OWN state and rebuilds the composer from it — a text the
  app ignored is wiped by the next render (the "wipe");
* the send control stays DISABLED until the composer really holds the text.

The send control is therefore the PROOF that the app took the prompt: the
test asserts it was clicked, and that the composer was emptied by the app.

The composer types covered here are the ones the providers really use:
a plain ``<textarea>`` (Qwen), a ProseMirror editor (Claude, ChatGPT), a
Lexical editor (Kimi) and a Quill editor (Gemini).
"""
import json
from pathlib import Path

import pytest

playwright = pytest.importorskip("playwright.sync_api")

EXT = Path(__file__).resolve().parents[2] / "extension" / "providers"


# --------------------------------------------------------------- the fake app
def _page(composer, kind, draft=""):
    """A composer page whose app behaves like the real ones.

    `kind`:
      textarea     — a plain <textarea> (Qwen)
      contenteditable — a rich-text editor (ProseMirror / Lexical)
      quill        — a Quill editor (Gemini): the instance owns the model,
                     and the app only reacts to USER-initiated changes

    `draft` — text the composer already holds when the job starts (a
    restored draft, a previous prompt). A rich editor INSERTS at the caret,
    so a composer that is not cleared would send the draft AND the prompt.
    """
    if kind == "textarea":
        read = "editor.value"
        write = "editor.value = state;"
    elif kind == "contenteditable":
        read = "editor.textContent"
        write = "if (editor.textContent !== state) editor.textContent = state;"
    else:  # quill
        read = "quill.getText()"
        write = "if (quill.getText() !== state) quill.setText(state, 'user');"

    quill_stub = ""
    app_take = ""
    if kind == "quill":
        quill_stub = """
  const quill = {
    _text: "",
    setText: function (t, source) {
      this._text = String(t);
      editor.textContent = this._text;
      document.dispatchEvent(new CustomEvent("quill:text-change", {
        detail: { text: this._text, source: source || "user" }
      }));
    },
    getText: function () { return this._text; },
    clipboard: { dangerouslyPasteHTML: function (i, h) { this.setText(h, "user"); } }
  };
  document.getElementById("qcontainer").__quill = quill;
"""
        app_take = """
  // a strict app only reacts to USER-initiated changes: a write made with
  // the "api" source is ignored (Quill emits the change either way)
  document.addEventListener("quill:text-change", function (e) {
    if (ignoreAll) return;
    if (e.detail && e.detail.source !== "user") return;
    state = e.detail.text;
    render();
  });
"""
    else:
        app_take = f"""
  editor.addEventListener("input", function () {{
    if (ignoreAll) return;
    state = {read};
    render();
  }});
"""

    return f"""<!doctype html><html><body>
<div class="composer">
  {composer}
  <button id="send" aria-label="Send message" disabled>Send</button>
</div>
<script>
  const editor = document.querySelector("[data-ed]");
  const send = document.getElementById("send");
  let state = {draft!r};
  let clicks = 0;
  let ignoreAll = false;
{quill_stub}
  function render() {{
    {write}
    send.disabled = !state.trim();
    document.body.setAttribute("data-state", state);
    document.body.setAttribute("data-clicks", String(clicks));
  }}
{app_take}  send.addEventListener("click", function () {{
    clicks++;
    document.body.setAttribute("data-sent", state);
    state = "";
    render();
  }});
  setInterval(render, 60);
  // an app that ignores the text (simulates a write the app did not take)
  document.addEventListener("nemapi:ignore", function () {{ ignoreAll = true; }});
  render();
</script></body></html>"""


SCENARIOS = {
    # Qwen: a plain textarea (#chat-input)
    "qwen": {
        "file": "qwen.js",
        "kind": "textarea",
        "html": '<textarea data-ed="1" id="chat-input" class="message-input-textarea"></textarea>',
    },
    # Claude: a ProseMirror editor
    "claude": {
        "file": "claude.js",
        "kind": "contenteditable",
        "html": '<div data-ed="1" class="ProseMirror" contenteditable="true" role="textbox" data-testid="chat-input"></div>',
    },
    # ChatGPT: div#prompt-textarea wrapping a ProseMirror editor
    "chatgpt": {
        "file": "chatgpt.js",
        "kind": "contenteditable",
        "html": '<div data-ed="1" id="prompt-textarea" class="ProseMirror" contenteditable="true" role="textbox" aria-label="Chat"></div>',
    },
    # Kimi: a Lexical editor
    "kimi": {
        "file": "kimi.js",
        "kind": "contenteditable",
        "html": '<div data-ed="1" class="chat-input-editor" contenteditable="true" role="textbox" data-lexical-editor="true"></div>',
    },
    # Gemini: a Quill editor (the instance owns the model)
    "gemini": {
        "file": "gemini.js",
        "kind": "quill",
        "html": (
            '<rich-textarea><div id="qcontainer" class="ql-container">'
            '<div data-ed="1" class="ql-editor" contenteditable="true"></div></div></rich-textarea>'
        ),
    },
}


def _world(page, files):
    """The page bridge in the PAGE world, base.js + the provider in an
    isolated world — exactly what the manifest does.

    The bridge is not optional: a Quill instance (`container.__quill`) is an
    expando of the PAGE realm and is invisible from the isolated world.
    """
    page.add_script_tag(content=(EXT / "page_bridge.js").read_text())
    cdp = page.context.new_cdp_session(page)
    frame_id = cdp.send("Page.getFrameTree")["frameTree"]["frame"]["id"]
    ctx_id = cdp.send(
        "Page.createIsolatedWorld", {"frameId": frame_id, "worldName": "nemapi-prov"}
    )["executionContextId"]

    def iso(expr, wait=False):
        r = cdp.send(
            "Runtime.evaluate",
            {
                "expression": expr,
                "contextId": ctx_id,
                "returnByValue": True,
                "awaitPromise": wait,
            },
        )
        assert "exceptionDetails" not in r, r
        return r["result"].get("value")

    for f in files:
        iso((EXT / f).read_text())
    return iso


@pytest.fixture
def browser():
    with playwright.sync_playwright() as p:
        try:
            b = p.chromium.launch()
        except Exception as exc:  # pragma: no cover
            pytest.skip(f"chromium unavailable: {exc}")
        yield b
        b.close()


@pytest.mark.parametrize("name", sorted(SCENARIOS))
def test_the_prompt_is_written_and_the_send_button_is_pressed(browser, name):
    """The prompt is typed, the app takes it, and the message is sent."""
    sc = SCENARIOS[name]
    page = browser.new_page()
    page.set_content(_page(sc["html"], sc["kind"]))
    iso = _world(page, ["base.js", sc["file"]])

    r = iso(
        """(async () => {
            await NemApiProviders.%s.sendPrompt("Regarde cette image");
            return {
                clicks: Number(document.body.getAttribute("data-clicks") || 0),
                state: document.body.getAttribute("data-state") || "",
            };
        })()"""
        % name,
        wait=True,
    )
    assert r["clicks"] == 1, f"{name}: the send button was never pressed"
    assert r["state"] == "", f"{name}: the composer still holds the prompt"


@pytest.mark.parametrize("name", sorted(SCENARIOS))
def test_a_prompt_the_app_ignores_is_never_sent(browser, name):
    """A write the app ignored must NOT be reported as sent.

    The DOM value can look right while the app keeps its old state. The
    proof is the send control: if it stays disabled, the message must not be
    sent (the agent would think it asked something it never asked).
    """
    sc = SCENARIOS[name]
    page = browser.new_page()
    page.set_content(_page(sc["html"], sc["kind"]))
    iso = _world(page, ["base.js", sc["file"]])

    r = iso(
        """(async () => {
            document.dispatchEvent(new CustomEvent("nemapi:ignore"));
            let error = null;
            try {
                await NemApiProviders.%s.sendPrompt("texte ignore");
            } catch (e) {
                error = String(e && e.message || e);
            }
            return {
                clicks: Number(document.body.getAttribute("data-clicks") || 0),
                state: document.body.getAttribute("data-state") || "",
                error: error,
            };
        })()"""
        % name,
        wait=True,
    )
    assert r["clicks"] == 0, f"{name}: a prompt the app ignored was sent anyway"
    assert r["error"], f"{name}: the failure was not reported"


@pytest.mark.parametrize("name", sorted(SCENARIOS))
def test_a_leftover_draft_is_replaced_not_appended(browser, name):
    """A composer that already holds a draft must send ONLY the prompt.

    A rich editor INSERTS at the caret: without clearing the field first,
    the message that leaves is the draft AND the prompt. The check on the
    composer is therefore an exact comparison, not a "contains" one.
    """
    sc = SCENARIOS[name]
    page = browser.new_page()
    page.set_content(_page(sc["html"], sc["kind"], draft="brouillon précédent"))
    iso = _world(page, ["base.js", sc["file"]])

    r = iso(
        """(async () => {
            let error = null;
            try {
                await NemApiProviders.%s.sendPrompt("le vrai prompt");
            } catch (e) {
                error = String(e && e.message || e);
            }
            return {
                clicks: Number(document.body.getAttribute("data-clicks") || 0),
                sent: document.body.getAttribute("data-sent") || "",
                error: error,
            };
        })()"""
        % name,
        wait=True,
    )
    assert r["clicks"] == 1, f"{name}: the send button was never pressed"
    assert r["sent"] == "le vrai prompt", (
        f"{name}: the message sent was {r['sent']!r} — the leftover draft was kept"
    )
