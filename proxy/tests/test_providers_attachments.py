"""Per-provider image upload: the file goes through the page's own file input.

The bug this file guards against is the one that cost the v5.0 image feature:
the image is uploaded, but the text that accompanies it is never typed, and
the send button is never pressed.

The fake app below behaves like the real ones:

* it keeps its OWN state and rebuilds the composer from it — a text the app
  ignored is wiped by the next render;
* the file input is filled by the BROWSER (the app listens to `change` and
  keeps the file in its own state);
* the send control stays DISABLED until the composer really holds the text;
* when the message is sent, the app records BOTH the text and the files it
  holds — a prompt sent without its image is a failure.

The DOM of each scenario is the one of the real provider (see AGENTS.md §4.1
for the sources), so a selector that drifts is caught here.
"""
import json
from pathlib import Path

import pytest

playwright = pytest.importorskip("playwright.sync_api")

EXT = Path(__file__).resolve().parents[2] / "extension" / "providers"

# 1x1 transparent PNG
PNG_B64 = (
    "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAYAAAAfFcSJAAAADUlEQVR42mP8z8BQDwAEhQGAhKmMIQAAAABJRU5ErkJggg=="
)


def _page(file_input_html, composer_html, kind="contenteditable", transport="input"):
    """A provider page with a file input and a composer.

    `kind`:
      textarea     — a plain <textarea> (Qwen, Z.ai)
      contenteditable — a rich-text editor (Claude, ChatGPT, Kimi, DeepSeek)
      quill        — a Quill editor (Gemini): the instance owns the model and
                     the app only reacts to USER-initiated changes

    `transport` — how the app accepts the file (the real sites do not agree):
      input  — a hidden <input type=file> in the DOM (DeepSeek, ChatGPT,
               Gemini: `input.hidden-file-input`)
      paste  — NO file input in the DOM: the app reads the files from a paste
               event (Qwen, Claude, Kimi, Z.ai — the documented route)
      drop   — NO file input: the app reads them from a drop event
    """
    if kind == "textarea":
        read = "editor.value"
    elif kind == "quill":
        read = "quill.getText()"
    else:
        read = "editor.textContent"

    # how the fake app writes its own state back into the editor
    write_back = (
        "if (quill.getText() !== state) quill.setText(state, 'user');"
        if kind == "quill"
        else "if (editor.textContent !== state) editor.textContent = state;"
    )

    # the app's own listener for the files
    if transport == "input":
        # Every file input of the page listens — as the real apps do — and
        # the app RECORDS which one delivered the file: a page can hold
        # several (ChatGPT's composer form has four), and the wrong one is
        # silently ignored. The test asserts the right one was used.
        take_js = """
  document.querySelectorAll("input[type=file]").forEach(function (fi, i) {
    fi.addEventListener("change", function (e) { onFiles(e, fi, i); });
  });
"""
    elif transport == "input-ignored":
        # The app IGNORES the dispatched events (ChatGLM's Element-Plus
        # upload does): the file never reaches it. The request must fail
        # instead of pretending the image was sent.
        take_js = """
  document.querySelectorAll("input[type=file]").forEach(function (fi) {
    fi.addEventListener("click", function () { onFiles(e, fi); });
  });
"""
    elif transport == "input-slow":
        # The app takes the file ASYNCHRONOUSLY and RE-RENDERS its composer
        # at the end of the transfer: a prompt typed in the meantime is WIPED
        # (the image stays, the text disappears) and the send control stays
        # disabled. This is the historical bug — the prompt must wait for the
        # upload, not be typed during it.
        take_js = """
  document.querySelectorAll("input[type=file]").forEach(function (fi, i) {
    fi.addEventListener("change", function (e) {
      busy = true;
      render();
      setTimeout(function () {
        var had = state.trim().length > 0;
        onFiles(e, fi, i);
        // the composer was re-rendered: the text it held is gone
        if (had) document.body.setAttribute("data-wiped", "1");
        busy = false;
        state = "";
        render();
      }, 2500);
    });
  });
"""
    elif transport == "paste":
        take_js = """
  editor.addEventListener("paste", function (e) {
    var list = e.clipboardData ? e.clipboardData.files : null;
    if (!list || !list.length) return;
    onFiles({ target: { files: list } });
  });
"""
    else:  # drop
        take_js = """
  document.getElementById("dropzone").addEventListener("drop", function (e) {
    var list = e.dataTransfer ? e.dataTransfer.files : null;
    if (!list || !list.length) return;
    onFiles({ target: { files: list } });
  });
"""

    drop_zone = '<div id="dropzone" class="drop-zone">drop</div>' if transport == "drop" else ""

    quill_stub = ""
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

    quill_take = (
        """
  document.addEventListener("quill:text-change", function (e) {
    if (e.detail && e.detail.source !== "user") return;  // a strict app
    state = quill.getText();
    render();
  });
"""
        if kind == "quill"
        else ""
    )

    return f"""<!doctype html><html><body>
<div class="composer">
  {file_input_html}
  {composer_html}
  {drop_zone}
  <button id="attach" type="button" aria-label="Attach">+</button>
  <button id="send" aria-label="Send message" disabled>Send</button>
</div>
<script>
  const editor = document.querySelector("[data-ed]");
  const send = document.getElementById("send");
  let state = "";
  let files = [];
  let clicks = 0;
  let busy = false;   // an upload is running: the app disables the send control
{quill_stub}
  function onFiles(e, src, idx) {{
    files = Array.from((e && e.target && e.target.files) || []).map((f) => f.name);
    document.body.setAttribute("data-files", String(files.length));
    // which input delivered the file (index among the page's file inputs)
    document.body.setAttribute("data-files-from", idx === undefined ? "?" : String(idx));
    // the app renders a preview chip (a blob: image, like the real ones)
    var chip = document.querySelector(".chip");
    if (!chip) {{
      chip = document.createElement("div");
      chip.className = "chip";
      document.querySelector(".composer").appendChild(chip);
    }}
    chip.textContent = files.join(",");
    if (!chip.querySelector("img")) {{
      var img = document.createElement("img");
      img.src = "blob:" + files.length;
      chip.appendChild(img);
    }}
  }}

  function render() {{
    if (editor.tagName === "TEXTAREA") editor.value = state;
    else {write_back}
    send.disabled = !state.trim() || busy;
    document.body.setAttribute("data-clicks", String(clicks));
  }}
{take_js}{quill_take}  editor.addEventListener("input", function () {{
    state = {read};
    render();
  }});
  send.addEventListener("click", function () {{
    clicks++;
    document.body.setAttribute("data-sent", state);
    document.body.setAttribute("data-sent-files", files.join(","));
    state = "";
    render();
  }});
  render();
</script></body></html>"""


SCENARIOS = {
    # Qwen: a plain textarea. The file input #filesUpload is PERSISTENT in
    # the DOM (live capture, tamga 2026-10-18) — the older belief that it was
    # dead was wrong.
    "qwen": {
        "file": "qwen.js",
        "kind": "textarea",
        "transport": "input",
        "html": '<textarea data-ed="1" id="chat-input" class="message-input-textarea"></textarea>',
        "file_input": '<input id="filesUpload" type="file" multiple aria-label="Upload files" tabindex="-1" hidden>',
        "expect_index": 0,
    },
    # Qwen without its file input: the documented fallback is a paste.
    "qwen-paste": {
        "file": "qwen.js",
        "provider": "qwen",
        "kind": "textarea",
        "transport": "paste",
        "html": '<textarea data-ed="1" id="chat-input" class="message-input-textarea"></textarea>',
        "file_input": "",
    },
    # Claude: a ProseMirror editor, file input data-testid="file-upload"
    # (live capture, tamga 2026-10-11).
    "claude": {
        "file": "claude.js",
        "kind": "contenteditable",
        "transport": "input",
        "html": '<div data-ed="1" class="ProseMirror" contenteditable="true" role="textbox" data-testid="chat-input"></div>',
        "file_input": '<input type="file" multiple hidden data-testid="file-upload" aria-label="Upload files">',
        "expect_index": 0,
    },
    # ChatGPT: div#prompt-textarea (ProseMirror). The composer form holds
    # FOUR file inputs (camera / image+video / images / any file) and the
    # camera one comes FIRST in the DOM: a bare "input[type=file]" lands on
    # it and the upload is lost.
    "chatgpt": {
        "file": "chatgpt.js",
        "kind": "contenteditable",
        "transport": "input",
        "html": '<div data-ed="1" id="prompt-textarea" class="ProseMirror" contenteditable="true" role="textbox"></div>',
        "file_input": (
            '<form id="composer">'
            '<input id="capture-camera" type="file" accept="image/*" capture hidden>'
            '<input id="upload-photos" type="file" accept="image/*" multiple hidden>'
            '<input id="upload-any" type="file" accept="*/*" multiple hidden>'
            "</form>"
        ),
        "expect_index": 1,
    },
    # Kimi: a Lexical editor, file input .hidden-input in .chat-editor-action
    # (live capture, tamga 2026-10-18).
    "kimi": {
        "file": "kimi.js",
        "kind": "contenteditable",
        "transport": "input",
        "html": '<div data-ed="1" class="chat-input-editor" contenteditable="true" role="textbox" data-lexical-editor="true"></div>',
        "file_input": '<div class="chat-editor-action"><input class="hidden-input" type="file" multiple hidden></div>',
        "expect_index": 0,
    },
    # Gemini: a Quill editor, the input carries the class hidden-file-input
    # and lives inside the opened menu (images-files-uploader).
    "gemini": {
        "file": "gemini.js",
        "kind": "quill",
        "transport": "input",
        "html": (
            '<rich-textarea><div id="qcontainer" class="ql-container">'
            '<div data-ed="1" class="ql-editor" contenteditable="true"></div></div></rich-textarea>'
        ),
        "file_input": '<images-files-uploader><input type="file" class="hidden-file-input" accept="image/*" multiple hidden></images-files-uploader>',
        "expect_index": 0,
    },
    # Z.ai (chat.z.ai): a plain textarea, hidden file input in
    # div.messageInputContainer (live capture, tamga 2026-10-18).
    "zai": {
        "file": "zai.js",
        "kind": "textarea",
        "transport": "input",
        "html": '<textarea data-ed="1" id="chat-input"></textarea>',
        "file_input": '<div class="messageInputContainer"><input type="file" hidden multiple accept=".pdf,.png,.jpg,.jpeg"></div>',
        "expect_index": 0,
    },
    # DeepSeek: the reference implementation (Lexical composer + file input)
    "deepseek": {
        "file": "deepseek.js",
        "kind": "contenteditable",
        "transport": "input",
        "html": '<div data-ed="1" contenteditable="true" role="textbox"></div>',
        "file_input": '<input type="file" multiple hidden>',
        "expect_index": 0,
    },
}


def _world(page, files):
    """The page bridge in the PAGE world, base.js + the provider in an
    isolated world — exactly what the manifest does."""
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
def test_the_image_reaches_the_app_and_the_text_is_sent_with_it(browser, name):
    """The image is uploaded, the text is typed, and the message carries both."""
    sc = SCENARIOS[name]
    provider = sc.get("provider", name)
    page = browser.new_page()
    page.set_content(_page(sc["file_input"], sc["html"], sc.get("kind", "contenteditable"),
                           sc.get("transport", "input")))
    iso = _world(page, ["base.js", sc["file"]])

    r = iso(
        """(async () => {
            const b64 = "%s";
            const bin = atob(b64);
            const bytes = new Uint8Array(bin.length);
            for (let i = 0; i < bin.length; i++) bytes[i] = bin.charCodeAt(i);
            const file = new File([bytes], "shot.png", { type: "image/png" });
            let error = null;
            try {
                await NemApiProviders.%s.attachFiles([file]);
                await NemApiProviders.%s.sendPrompt("Regarde cette image");
            } catch (e) {
                error = String(e && e.message || e);
            }
            return {
                error: error,
                files: document.body.getAttribute("data-files") || "0",
                filesFrom: document.body.getAttribute("data-files-from") || "",
                clicks: Number(document.body.getAttribute("data-clicks") || 0),
                sent: document.body.getAttribute("data-sent") || "",
                sentFiles: document.body.getAttribute("data-sent-files") || "",
            };
        })()"""
        % (PNG_B64, provider, provider),
        wait=True,
    )
    assert not r["error"], f"{name}: {r['error']}"
    assert r["files"] == "1", f"{name}: the app never received the file"
    assert r["clicks"] == 1, f"{name}: the send button was never pressed"
    assert r["sent"].strip() == "Regarde cette image", f"{name}: the text was not sent ({r['sent']!r})"
    assert r["sentFiles"] == "shot.png", f"{name}: the message was sent without its image"
    if sc.get("transport") == "input":
        # the RIGHT input was used: a page can hold several (ChatGPT's
        # composer form has four) and the wrong one is silently ignored
        assert r["filesFrom"] == str(sc["expect_index"]), (
            f"{name}: the file went to the wrong input "
            f"(#{r['filesFrom']} instead of #{sc['expect_index']})"
        )


@pytest.mark.parametrize("name", ["chatgpt", "claude", "gemini", "kimi", "qwen", "zai"])
def test_the_prompt_is_typed_after_the_upload_not_during_it(browser, name):
    """The prompt must NOT be typed while the upload is running.

    The app takes the file asynchronously and re-renders its composer at the
    end of the transfer: a prompt typed in the meantime is WIPED (the image
    stays, the text disappears) and the send control stays disabled — the
    request then hangs until the watchdog.

    The witness is the app itself: it records that the composer held text
    when the upload ended. The prompt must not be there yet.
    """
    sc = SCENARIOS[name]
    provider = sc.get("provider", name)
    page = browser.new_page()
    page.set_content(_page(sc["file_input"], sc["html"], sc.get("kind", "contenteditable"),
                           "input-slow"))
    iso = _world(page, ["base.js", sc["file"]])

    r = iso(
        """(async () => {
            const b64 = "%s";
            const bin = atob(b64);
            const bytes = new Uint8Array(bin.length);
            for (let i = 0; i < bin.length; i++) bytes[i] = bin.charCodeAt(i);
            const file = new File([bytes], "shot.png", { type: "image/png" });
            let error = null;
            try {
                await NemApiProviders.%s.attachFiles([file]);
                await NemApiProviders.%s.sendPrompt("Regarde cette image");
            } catch (e) {
                error = String(e && e.message || e);
            }
            return {
                error: error,
                wiped: document.body.getAttribute("data-wiped") || "",
                clicks: Number(document.body.getAttribute("data-clicks") || 0),
                sent: document.body.getAttribute("data-sent") || "",
                sentFiles: document.body.getAttribute("data-sent-files") || "",
            };
        })()"""
        % (PNG_B64, provider, provider),
        wait=True,
    )
    assert not r["error"], f"{name}: {r['error']}"
    assert r["wiped"] != "1", (
        f"{name}: the prompt was typed DURING the upload — the app wiped it "
        f"at the end of the transfer"
    )
    assert r["clicks"] == 1, f"{name}: the send button was never pressed"
    assert r["sent"].strip() == "Regarde cette image", f"{name}: the text was not sent ({r['sent']!r})"
    assert r["sentFiles"] == "shot.png", f"{name}: the message was sent without its image"


@pytest.mark.parametrize("name", sorted(SCENARIOS))
def test_a_site_without_a_file_input_reports_a_clear_error(browser, name):
    """No file input and no paste/drop listener: the error says so.

    The page accepts nothing: the request must fail with an explicit message
    instead of pretending the image was sent.
    """
    sc = SCENARIOS[name]
    provider = sc.get("provider", name)
    page = browser.new_page()
    # transport "none": no <input type=file>, no paste listener, no drop zone
    page.set_content(_page("", sc["html"], sc.get("kind", "contenteditable"), "none"))
    iso = _world(page, ["base.js", sc["file"]])

    r = iso(
        """(async () => {
            const b64 = "%s";
            const bin = atob(b64);
            const bytes = new Uint8Array(bin.length);
            for (let i = 0; i < bin.length; i++) bytes[i] = bin.charCodeAt(i);
            const file = new File([bytes], "shot.png", { type: "image/png" });
            let error = null;
            try {
                await NemApiProviders.%s.attachFiles([file]);
            } catch (e) {
                error = String(e && e.message || e);
            }
            return { error: error };
        })()"""
        % (PNG_B64, provider),
        wait=True,
    )
    assert r["error"], f"{name}: the missing file input was not reported"
    assert "file input" in r["error"], f"{name}: unclear error ({r['error']!r})"


@pytest.mark.parametrize("name", sorted(SCENARIOS))
def test_a_page_that_ignores_the_file_events_does_not_report_a_success(browser, name):
    """The app ignores the dispatched events: the request must FAIL.

    Setting `input.files` from JS is not a proof that the app took the file.
    An app that ignores the events (ChatGLM's Element-Plus upload is the
    known case) leaves the image nowhere — sending the prompt anyway would
    deliver a message whose image is silently missing.
    """
    sc = SCENARIOS[name]
    provider = sc.get("provider", name)
    page = browser.new_page()
    page.set_content(_page(sc["file_input"], sc["html"], sc.get("kind", "contenteditable"),
                           "input-ignored"))
    iso = _world(page, ["base.js", sc["file"]])

    r = iso(
        """(async () => {
            const b64 = "%s";
            const bin = atob(b64);
            const bytes = new Uint8Array(bin.length);
            for (let i = 0; i < bin.length; i++) bytes[i] = bin.charCodeAt(i);
            const file = new File([bytes], "shot.png", { type: "image/png" });
            let error = null;
            try {
                await NemApiProviders.%s.attachFiles([file]);
            } catch (e) {
                error = String(e && e.message || e);
            }
            return {
                error: error,
                files: document.body.getAttribute("data-files") || "0",
            };
        })()"""
        % (PNG_B64, provider),
        wait=True,
    )
    assert r["files"] == "0", f"{name}: the app was supposed to ignore the file"
    assert r["error"], f"{name}: the ignored upload was reported as a success"
