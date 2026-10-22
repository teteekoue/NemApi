"""Extension DOM extraction in a real Chromium (optional: needs playwright).

Content scripts run in an ISOLATED world: they cannot see the React fiber of
the page, so before the page bridge the extension rebuilt DeepSeek answers from
the rendered HTML, where KaTeX had eaten every `$…$` of the shell commands of
tool calls. This test loads base.js in a real isolated world (CDP), like Chrome
does for a content script, and page_bridge.js in the page world.
"""
import json
from pathlib import Path

import pytest

playwright = pytest.importorskip("playwright.sync_api")

EXT = Path(__file__).resolve().parents[2] / "extension" / "providers"

RAW_MD = (
    "Je vérifie les outils.\n"
    '<tool>{"name": "run_shell_command", "arguments": {"command": "for c in gcc make; do printf \'%-12s\' '
    '\\"$c\\"; command -v \\"$c\\" >/dev/null 2>&1 && \\"$c\\" --version; done; echo \\"DISPLAY=$DISPLAY\\""}}</tool>'
)

# what DeepSeek renders: markdown consumed the \" escapes, KaTeX ate $c" … "$
KATEX = (
    '<span class="katex"><span class="katex-mathml"><math><semantics><mrow><mi>c</mi></mrow>'
    '<annotation encoding="application/x-tex">{tex}</annotation></semantics></math></span>'
    '<span class="katex-html" aria-hidden="true">c  command −;¨ v</span></span>'
)
PAGE = """<!doctype html><html><body>
<div class="ds-markdown" id="answer"><p>Je vérifie les outils.</p><p>&lt;tool&gt;{"name": "run_shell_command"…}&lt;/tool&gt;</p></div>
<div class="ds-markdown" id="norreact"><p>printf '%-12s' "KATEX1c" --version</p>
<span class="katex-display">KATEX2</span></div>
<script>
  document.getElementById("answer")["__reactFiber$test"] = {
    return: { return: { return: { memoizedProps: { markdown: RAW } } } }
  };
</script></body></html>"""


def test_bridge_reads_raw_markdown_and_katex_is_restored():
    page_html = (
        PAGE.replace("RAW", json.dumps(RAW_MD))
        .replace("KATEX1", KATEX.format(tex='c" &gt;/dev/null 2&gt;&amp;1 &amp;&amp; "'))
        .replace("KATEX2", KATEX.format(tex=r"\sum_{i=1}^n i"))
    )
    with playwright.sync_playwright() as p:
        try:
            browser = p.chromium.launch()
        except Exception as exc:  # pragma: no cover - browser not installed
            pytest.skip(f"chromium unavailable: {exc}")
        page = browser.new_page()
        page.set_content(page_html)
        page.add_script_tag(content=(EXT / "page_bridge.js").read_text())

        cdp = page.context.new_cdp_session(page)
        frame_id = cdp.send("Page.getFrameTree")["frameTree"]["frame"]["id"]
        ctx_id = cdp.send("Page.createIsolatedWorld", {"frameId": frame_id, "worldName": "nemapi-content"})[
            "executionContextId"]

        def iso(expr):
            r = cdp.send("Runtime.evaluate", {"expression": expr, "contextId": ctx_id, "returnByValue": True})
            assert "exceptionDetails" not in r, r
            return r["result"].get("value")

        iso((EXT / "base.js").read_text())
        # isolated world: the fiber expando of the page is invisible…
        assert iso('NemApiBase.getReactFiber(document.getElementById("answer"))') is None
        # …but the bridge returns the ORIGINAL markdown, byte for byte
        md = iso('NemApiBase.extractReactMarkdown(document.getElementById("answer"), ["markdown","content","text"])')
        assert md == RAW_MD.strip()
        assert iso('document.getElementById("answer").hasAttribute("data-nemapi-md")') is False

        # no React data → HTML→MD fallback restores the TeX source with its $ delimiters
        out = iso('NemApiBase.htmlToMarkdown(document.getElementById("norreact"))')
        assert 'printf \'%-12s\' "$c" >/dev/null 2>&1 && "$c" --version' in out
        assert r"$$\sum_{i=1}^n i$$" in out and "command −" not in out
        browser.close()

    from nemapi.utils.tools_format import parse_tool_calls

    _, calls = parse_tool_calls(md, ["run_shell_command"])
    cmd = json.loads(calls[0]["function"]["arguments"])["command"]
    assert cmd == 'for c in gcc make; do printf \'%-12s\' "$c"; command -v "$c" >/dev/null 2>&1 && "$c" --version; done; echo "DISPLAY=$DISPLAY"'


# ------------------------------------------------------------ v4.4: DeepSeek final read

DS_PAGE = """<!doctype html><html><body>
<div class="ds-message" id="m1"><div class="ds-markdown" id="md1"><p>rendu</p></div></div>
<div class="ds-message" id="m2"><div class="ds-markdown" id="md2">HTML2</div></div>
<script>
  document.getElementById("md1")["__reactFiber$x"] = {
    return: { return: { return: { memoizedProps: { markdown: RAW } } } }
  };
</script></body></html>"""


def _ds_world(page):
    cdp = page.context.new_cdp_session(page)
    frame_id = cdp.send("Page.getFrameTree")["frameTree"]["frame"]["id"]
    ctx_id = cdp.send("Page.createIsolatedWorld", {"frameId": frame_id, "worldName": "nemapi-ds"})[
        "executionContextId"]

    def iso(expr, wait=False):
        r = cdp.send("Runtime.evaluate", {"expression": expr, "contextId": ctx_id, "returnByValue": True,
                                          "awaitPromise": wait})
        assert "exceptionDetails" not in r, r
        return r["result"].get("value")

    iso((EXT / "base.js").read_text())
    iso((EXT / "deepseek.js").read_text())
    return iso


def test_deepseek_final_read_keeps_code_words_and_reports_source():
    call = {"name": "write_file", "arguments": {
        "file_path": "a.js", "content": "for (;;) { if (x) continue; JSON.parse(s); } // Edit Stop Copy"}}
    raw = "Je l'écris.\n\n````tool\n<tool>" + json.dumps(call) + "</tool>\n````"
    # what the page shows for the same answer (code block: verbatim text)
    html2 = ('<p>Je l\'écris.</p><div class="md-code-block"><div class="md-code-block-banner">tool'
             '<button>Copier</button></div><pre><code class="language-tool">'
             + ("<tool>" + json.dumps(call) + "</tool>").replace("&", "&amp;").replace("<", "&lt;")
             + "</code></pre></div>")
    with playwright.sync_playwright() as p:
        try:
            browser = p.chromium.launch()
        except Exception as exc:  # pragma: no cover
            pytest.skip(f"chromium unavailable: {exc}")
        page = browser.new_page()
        page.set_content(DS_PAGE.replace("RAW", json.dumps(raw)).replace("HTML2", html2))
        iso = _ds_world(page)
        assert iso("NemApiBase.bridgeReady()") is False       # bridge not injected yet
        page.add_script_tag(content=(EXT / "page_bridge.js").read_text())
        assert iso("NemApiBase.bridgeReady()") is True

        iso("NemApiBase.resetExtractSource()")
        via_react = iso('NemApiProviders.deepseek.extractPremium(document.getElementById("m1"))', wait=True)
        assert via_react == raw
        assert iso("NemApiBase.getExtractSource()") == "react"

        iso("NemApiBase.resetExtractSource()")
        via_html = iso('NemApiProviders.deepseek.extractPremium(document.getElementById("m2"))', wait=True)
        assert iso("NemApiBase.getExtractSource()") == "html"
        browser.close()

    from nemapi.services.sessions import postprocess

    for text in (via_react, via_html):
        parsed = postprocess(text, ["write_file"])
        assert json.loads(parsed.tool_calls[0]["function"]["arguments"]) == call["arguments"], text
        assert parsed.text == "Je l'écris.", repr(text)


def test_wait_until_stable_respects_busy_and_unfinished_answers():
    """A long answer is not captured while the page still generates or while
    its tool call is open (DeepSeek pauses between chunks of a big file)."""
    with playwright.sync_playwright() as p:
        try:
            browser = p.chromium.launch()
        except Exception as exc:  # pragma: no cover - browser not installed
            pytest.skip(f"chromium unavailable: {exc}")
        page = browser.new_page()
        page.set_content("<!doctype html><html><body><div id='a'></div></body></html>")
        page.add_script_tag(content=(EXT / "base.js").read_text())
        run = """async ([script, opts]) => {
            const t0 = Date.now(); const B = window.NemApiBase; const el = document.getElementById('a');
            window.__NEMAPI_JOB_BUDGET_MS__ = 0;
            const steps = script.map(s => [s[0], s[1], s[2]]);
            let busy = true; window.__NEMAPI_IS_BUSY__ = () => busy;
            for (const [at, text, b] of steps) setTimeout(() => { el.textContent = text; busy = b; }, at);
            const out = await B.waitUntilStable(() => el.textContent, opts);
            return {out, ms: Date.now() - t0, partial: !!window.__NEMAPI_LAST_PARTIAL__};
        }"""
        fast = {"stableMs": 200, "pollMs": 50, "incompleteStableMs": 1500, "busyGraceMs": 2500, "timeout": 8000}

        # 1) text paused for 1 s mid-answer while the page is still generating
        r = page.evaluate(run, [[[0, "Je commence", True], [1000, "Je commence la suite.", True],
                                 [1600, "Je commence la suite.", False]], fast])
        assert r["out"] == "Je commence la suite." and r["ms"] >= 1600 and not r["partial"]

        # 2) busy detection broken (never clears): the answer is still taken,
        #    after the BOUNDED grace period (it used to cost busyStuckMs = 60 s)
        r = page.evaluate(run, [[[0, "Fini.", True]], fast])
        assert r["out"] == "Fini." and 2400 <= r["ms"] < 5000

        # 3) an open tool call is only protected WHILE the model still writes:
        #    the page says it is generating → the answer is not finished.
        open_call = '````tool\n<tool>{"name": "write_file", "arguments": {"content": "<html>'
        done = open_call + '</html>"}}</tool>\n````'
        r = page.evaluate(run, [[[0, open_call, True], [1000, done, True], [2000, done, False]], fast])
        assert r["out"] == done and not r["partial"]
        assert page.evaluate("t => NemApiBase.looksIncomplete(t)", open_call) is True
        assert page.evaluate("t => NemApiBase.looksIncomplete(t)", done) is False

        # 3b) the page is IDLE and the answer looks unfinished: the answer is
        #     taken after `stableMs` (waiting 25 s for it was the main source
        #     of latency). The proxy detects a call that was cut off and
        #     continues it, so there is nothing to wait for here.
        r = page.evaluate(run, [[[0, open_call, False], [1000, done, False]], fast])
        assert r["out"] == open_call and not r["partial"]
        assert r["ms"] < 900

        # 4) still growing when the budget ends: returned but flagged partial
        steps = [[i * 100, "x" * (i + 1), False] for i in range(40)]
        r = page.evaluate(run, [steps, {**fast, "timeout": 1200}])
        assert r["partial"] and r["out"].startswith("x")

        # the job budget (set from the proxy timeout) extends the provider's own timeout
        assert page.evaluate("() => { window.__NEMAPI_JOB_BUDGET_MS__ = 870000; return NemApiBase.jobBudgetMs(180000); }") == 870000
        browser.close()


# ---------------------------------------------- v5.1: the image + its prompt
# A minimal React-like CONTROLLED textarea:
#   - the state is the single source of truth and lives in the DOM (so the
#     isolated world can read it too);
#   - every re-render writes the state back into the field: this is what
#     wipes a text that React never accepted (the end of an image upload);
#   - an "input" event updates the state, but NOT while the field is
#     disabled — React ignores the events of a disabled field.
REACT_TA = """<!doctype html><html><body>
<textarea id="ta" data-state=""></textarea>
<script>
  const ta = document.getElementById("ta");
  const state = () => ta.getAttribute("data-state") || "";
  const render = () => { ta.value = state(); };
  ta.addEventListener("input", function () {
    if (ta.disabled) return;                              // React ignores a disabled field
    if (ta.getAttribute("data-ignore") === "1") return;   // this page ignores the event
    ta.setAttribute("data-state", ta.value);
  });
  // the fiber React exposes on the node: the page bridge reads props.value
  Object.defineProperty(ta, "__reactFiber$test", {
    enumerable: true,
    get: function () { return { memoizedProps: { value: state() }, return: null }; }
  });
  document.addEventListener("nemapi:rerender", render);
</script></body></html>"""


def _react_world(page):
    """base.js in an isolated world + page_bridge.js in the page world."""
    page.add_script_tag(content=(EXT / "page_bridge.js").read_text())
    cdp = page.context.new_cdp_session(page)
    frame_id = cdp.send("Page.getFrameTree")["frameTree"]["frame"]["id"]
    ctx_id = cdp.send("Page.createIsolatedWorld", {"frameId": frame_id, "worldName": "nemapi-react"})[
        "executionContextId"]

    def iso(expr, wait=False):
        r = cdp.send("Runtime.evaluate", {"expression": expr, "contextId": ctx_id, "returnByValue": True,
                                          "awaitPromise": wait})
        assert "exceptionDetails" not in r, r
        return r["result"].get("value")

    iso((EXT / "base.js").read_text())
    return iso


def test_the_prompt_is_only_accepted_when_react_really_holds_it():
    """The image arrives but its text never did: the text was written while the
    page re-rendered (end of the upload), and React silently dropped it.

    The composer must therefore check the value REACT holds (through the page
    bridge), not only the DOM value: a DOM value that React ignores is wiped
    by the next render, and the prompt would be sent empty.
    """
    with playwright.sync_playwright() as p:
        try:
            browser = p.chromium.launch()
        except Exception as exc:  # pragma: no cover
            pytest.skip(f"chromium unavailable: {exc}")
        page = browser.new_page()
        page.set_content(REACT_TA)
        iso = _react_world(page)

        # 1) a normal write: React holds the text and keeps it
        r = iso("""(async () => {
            const el = document.getElementById("ta");
            const out = await NemApiBase.writeAndConfirm(() => el, "Regarde cette image",
                                                          {attempts: 3, holdMs: 150});
            return {ok: !!out, dom: el.value, react: el.getAttribute("data-state")};
        })()""", wait=True)
        assert r["ok"] is True
        assert r["dom"] == "Regarde cette image"
        assert r["react"] == "Regarde cette image"

        # 2) the page ignores the event (React never took the text) and a
        #    re-render wipes the field: the write must NOT be reported as done
        r = iso("""(async () => {
            const el = document.getElementById("ta");
            el.setAttribute("data-ignore", "1");
            el.setAttribute("data-state", "");
            el.value = "";
            const iv = setInterval(() => document.dispatchEvent(new CustomEvent("nemapi:rerender")), 100);
            const out = await NemApiBase.writeAndConfirm(() => el, "texte perdu",
                                                          {attempts: 4, holdMs: 150});
            clearInterval(iv);
            return {ok: !!out, dom: el.value, react: el.getAttribute("data-state")};
        })()""", wait=True)
        assert r["ok"] is False          # never confirmed
        assert r["react"] == ""          # React really holds nothing

        # 3) the field is disabled (the upload is still running): the write
        #    must wait for it instead of writing into a dead field
        r = iso("""(async () => {
            const el = document.getElementById("ta");
            el.removeAttribute("data-ignore");
            el.setAttribute("data-state", "");
            el.value = "";
            el.disabled = true;
            const out = await NemApiBase.writeAndConfirm(() => el, "trop tot",
                                                          {attempts: 2, holdMs: 150});
            const whileDisabled = el.value;
            el.disabled = false;
            const out2 = await NemApiBase.writeAndConfirm(() => el, "trop tot",
                                                           {attempts: 4, holdMs: 150});
            return {whileDisabled: whileDisabled, ok: !!out2, react: el.getAttribute("data-state")};
        })()""", wait=True)
        assert r["whileDisabled"] == ""   # nothing written in a disabled field
        assert r["ok"] is True            # written once the field is editable
        assert r["react"] == "trop tot"

        # 4) the bridge reports the value React holds (not the DOM value)
        r = iso("""(() => {
            const el = document.getElementById("ta");
            el.value = "DOM seulement";
            return NemApiBase.readReactValue(el);
        })()""")
        assert r == "trop tot"            # React still holds the accepted text
        browser.close()


def test_the_bridge_reads_the_value_react_holds():
    """readReactValue() answers with the CONTROLLED value, not the DOM one."""
    with playwright.sync_playwright() as p:
        try:
            browser = p.chromium.launch()
        except Exception as exc:  # pragma: no cover
            pytest.skip(f"chromium unavailable: {exc}")
        page = browser.new_page()
        page.set_content(REACT_TA)
        iso = _react_world(page)
        r = iso("""(() => {
            const el = document.getElementById("ta");
            el.setAttribute("data-state", "ce que react hold");
            el.value = "ce que le dom affiche";
            return {react: NemApiBase.readReactValue(el), dom: el.value};
        })()""")
        assert r["react"] == "ce que react hold"
        assert r["dom"] == "ce que le dom affiche"
        browser.close()


# ------------------------------------------------- the reported failure, as-is
# A page shaped like the current DeepSeek composer:
#   - a rich-text editor (contenteditable) that keeps the text in its OWN
#     state and rebuilds the DOM from it on every render — a text written
#     straight into the DOM is dropped at the next render;
#   - a send control that stays DISABLED until the editor really holds text
#     (this is the proof that the app accepted the prompt);
#   - a file input the browser fills, and an upload that churns the DOM
#     before its FINAL re-render — the re-render that used to wipe the
#     prompt typed while the image was still being uploaded.
DEEPSEEK_LIKE = """<!doctype html><html><body>
<div class="ds-chat">
  <div class="composer">
    <input type="file" id="file" multiple>
    <div id="preview" data-tick="0"></div>
    <div id="editor" contenteditable="true" role="textbox" aria-multiline="true"></div>
    <button id="send" class="ds-icon-button" aria-label="Send" disabled>Send</button>
  </div>
</div>
<script>
  // NOTE: a Chrome isolated world has its OWN `window`: the page state must
  // therefore travel through the DOM (attributes), which both worlds share.
  const editor = document.getElementById("editor");
  const send = document.getElementById("send");
  let state = "";
  let clicks = 0;

  function render() {
    if (editor.textContent !== state) editor.textContent = state;
    send.disabled = !state.trim();
    document.body.setAttribute("data-editor-state", state);
    document.body.setAttribute("data-send-clicks", String(clicks));
  }
  editor.addEventListener("input", function () { state = editor.textContent; render(); });
  send.addEventListener("click", function () { clicks++; state = ""; render(); });
  setInterval(render, 80);

  // the upload: the page churns, then re-renders at the end (the wipe)
  function upload(ms) {
    return new Promise(function (resolve) {
      const t0 = Date.now();
      const preview = document.getElementById("preview");
      const iv = setInterval(function () {
        preview.setAttribute("data-tick", String(Date.now()));
        if (Date.now() - t0 > ms) { clearInterval(iv); render(); resolve(); }
      }, 80);
    });
  }
  // started from the isolated world (a CustomEvent crosses the worlds)
  document.addEventListener("nemapi:start-upload", function (e) {
    upload((e.detail && e.detail.ms) || 2500);
  });
</script></body></html>"""


def test_the_prompt_survives_the_upload_and_the_send_button_is_pressed():
    """The reported failure, reproduced: the image is uploaded, the prompt
    that accompanies it is never typed, and the send button is never pressed.

    The page re-renders when the upload ends, which wipes a prompt written
    too early. The extension must wait for the upload, write the prompt, and
    PROVE the app took it (the send control becomes usable) before clicking.
    """
    with playwright.sync_playwright() as p:
        try:
            browser = p.chromium.launch()
        except Exception as exc:  # pragma: no cover
            pytest.skip(f"chromium unavailable: {exc}")
        page = browser.new_page()
        page.set_content(DEEPSEEK_LIKE)
        iso = _ds_world(page)

        r = iso("""(async () => {
            // the extension uploads the image (the browser fills the input)
            const input = document.getElementById("file");
            const dt = new DataTransfer();
            dt.items.add(new File([new Uint8Array([137, 80, 78, 71])], "shot.png", {type: "image/png"}));
            input.files = dt.files;
            // the upload finishes (and re-renders) a bit later
            document.dispatchEvent(new CustomEvent("nemapi:start-upload", {detail: {ms: 2500}}));
            await NemApiProviders.deepseek.sendPrompt("Regarde cette image");
            return {
                attached: input.files.length,
                state: document.body.getAttribute("data-editor-state"),
                clicks: Number(document.body.getAttribute("data-send-clicks") || 0),
            };
        })()""", wait=True)

        assert r["attached"] == 1
        assert r["clicks"] == 1, "the send button was never pressed"
        assert r["state"] == "", "the composer still holds the prompt (it was never sent)"
        browser.close()


def test_the_prompt_is_typed_in_the_rich_text_editor():
    """The composer of the current build is a contenteditable: the text must
    land in the editor's own state, not only in the DOM."""
    with playwright.sync_playwright() as p:
        try:
            browser = p.chromium.launch()
        except Exception as exc:  # pragma: no cover
            pytest.skip(f"chromium unavailable: {exc}")
        page = browser.new_page()
        page.set_content(DEEPSEEK_LIKE)
        iso = _ds_world(page)

        r = iso("""(async () => {
            const el = document.getElementById("editor");
            const written = await NemApiBase.writeAndConfirm(() => el, "bonjour",
                                                              {attempts: 3, holdMs: 150});
            return {
                ok: !!written,
                state: document.body.getAttribute("data-editor-state"),
                dom: el.innerText,
                sendEnabled: !document.getElementById("send").disabled,
            };
        })()""", wait=True)

        assert r["ok"] is True
        assert r["state"] == "bonjour"      # the editor really took the text
        assert r["dom"] == "bonjour"
        assert r["sendEnabled"] is True     # and the app enabled the send control
        browser.close()
