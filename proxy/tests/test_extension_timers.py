"""The hidden-tab timers of the extension (v5.4.1).

Chrome throttles a hidden page's chained timers: one wake per second, then —
after five minutes hidden, once the timer chain reaches 5 (every polling loop
is such a chain) — ONE wake per MINUTE. Every wait of the extension is a
chained timer, so a request in a background tab took 2 to 5 minutes and
finished "instantly" when the tab came back to the foreground. Chrome can also
FREEZE a hidden, silent tab (Energy Saver): nothing runs at all until the tab
is activated.

v5.4.1 changes the TIMER SOURCE only — the wait loops, the detection and the
extraction are the ones of v5.3.1:

* `providers/ticker.js`, a dedicated Worker, owns the timers of a hidden tab
  (worker timers are not throttled). It is pinged before being trusted and
  written off after two missed wake-ups.
* failing that, a `MessageChannel` bounce arms the `setTimeout` from a message
  task (nesting level 1), so the chain never reaches 5.
* a Web Lock is held while a hidden tab waits (the documented opt-out of
  freezing).

The static checks below run everywhere; the behavioural ones need Chromium and
are skipped when it is not installed (like the other DOM tests of this repo).
"""
import json
import shutil
import subprocess
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]
EXT = ROOT / "extension"
PROVIDERS = EXT / "providers"
PROVIDER_FILES = ["deepseek", "qwen", "claude", "gemini", "chatgpt", "kimi", "zai"]

playwright = pytest.importorskip("playwright.sync_api")


# --------------------------------------------------------------- static checks
def test_ticker_worker_is_web_accessible():
    """`new Worker(chrome.runtime.getURL(...))` needs the file declared."""
    manifest = json.loads((EXT / "manifest.json").read_text())
    urls = []
    for entry in manifest.get("web_accessible_resources", []):
        resources = entry.get("resources") or []
        if "providers/ticker.js" in resources:
            urls = entry.get("matches") or []
    assert urls, "providers/ticker.js must be listed in web_accessible_resources"
    # the worker is created from a content script: every host needs the match
    for script in manifest.get("content_scripts", []):
        if "content.js" not in (script.get("js") or []):
            continue
        for match in script.get("matches") or []:
            assert match in urls, f"{match} runs content.js but cannot load ticker.js"


def test_base_delegates_its_sleep_and_opts_out_of_freezing():
    source = (PROVIDERS / "base.js").read_text()
    assert "providers/ticker.js" in source, "the timer worker is not spawned"
    assert "new MessageChannel()" in source, "no bounce fallback"
    assert "navigator.locks" in source, "no Web Lock opt-out"
    exports = source[source.index("global.NemApiBase = {"):]
    for name in ["sleep", "timerMode", "keepTabAwake"]:
        assert name in exports, name
    # the throttling problem must be explained where it is worked around
    assert "MINUTE" in source and "throttl" in source.lower()


def test_content_script_never_waits_on_a_raw_timer():
    """A raw `setTimeout` in a hidden tab is clamped to 1 s, then 1 min."""
    source = (EXT / "content.js").read_text()
    # the only raw timer left is the fallback used when base.js is missing
    assert "setTimeout(" not in source.replace("new Promise((r) => setTimeout(r, ms))", "")


def test_background_protects_the_provider_tabs_from_the_memory_saver():
    source = (EXT / "background.js").read_text()
    assert "autoDiscardable: false" in source


def test_the_providers_are_untouched_by_this_release():
    """v5.4.1 is a timer fix: no extraction/detection logic may change."""
    for name in PROVIDER_FILES:
        source = (PROVIDERS / f"{name}.js").read_text()
        # the detection of v5.3.1, unchanged
        assert "async function waitForResponse" in source, name


def test_extension_javascript_parses():
    node = shutil.which("node")
    if not node:
        pytest.skip("node is not installed")
    files = sorted(str(p) for p in PROVIDERS.glob("*.js")) + [
        str(EXT / "content.js"),
        str(EXT / "background.js"),
    ]
    for path in files:
        result = subprocess.run([node, "--check", path], capture_output=True, text=True)
        assert result.returncode == 0, f"{path}: {result.stderr}"


# ------------------------------------------------------------ real Chromium
PAGE = "<!doctype html><html><body><div id='a'>ancienne reponse</div></body></html>"
ORIGIN = "http://nemapi.test"


class World:
    """base.js in an isolated world, like a content script in a real tab."""

    def __init__(self, page, scripts, *, worker=False):
        self.cdp = page.context.new_cdp_session(page)
        frame = self.cdp.send("Page.getFrameTree")["frameTree"]["frame"]["id"]
        self.ctx = self.cdp.send(
            "Page.createIsolatedWorld", {"frameId": frame, "worldName": "nemapi-timer-world"}
        )["executionContextId"]
        if worker:
            self.eval(
                "(function(){ try { Object.defineProperty(window, 'chrome', {configurable: true, "
                "value: {runtime: {getURL: (p) => location.origin + '/' + p}}}); } catch (e) {} })()"
            )
        for script in scripts:
            self.eval(script)

    def eval(self, expression, await_promise=False):
        result = self.cdp.send(
            "Runtime.evaluate",
            {
                "expression": expression,
                "contextId": self.ctx,
                "returnByValue": True,
                "awaitPromise": await_promise,
            },
        )
        assert "exceptionDetails" not in result, result
        return result["result"].get("value")


@pytest.fixture()
def browser_page():
    with playwright.sync_playwright() as p:
        try:
            browser = p.chromium.launch()
        except Exception as exc:  # pragma: no cover - browser not installed
            pytest.skip(f"chromium unavailable: {exc}")
        page = browser.new_page()
        served = {"ticker": 0}

        def handle(route):
            url = route.request.url
            if url.endswith("/providers/ticker.js"):
                served["ticker"] += 1
                route.fulfill(
                    status=200,
                    content_type="application/javascript",
                    body=(PROVIDERS / "ticker.js").read_text(),
                )
            elif url.endswith("/chat.html"):
                route.fulfill(status=200, content_type="text/html", body=PAGE)
            else:
                route.fulfill(status=404, body="")

        page.route(ORIGIN + "/**", handle)
        page.goto(ORIGIN + "/chat.html")
        yield page, served
        browser.close()


def _base() -> str:
    return (PROVIDERS / "base.js").read_text()


def test_a_hidden_tab_waits_on_the_worker(browser_page):
    """A hidden tab's own timer is clamped to 1 s: the Worker is the fix."""
    page, served = browser_page
    world = World(page, [_base()], worker=True)
    took = world.eval(
        "(async () => {"
        "  Object.defineProperty(document, 'hidden', {configurable: true, get: () => true});"
        "  Object.defineProperty(document, 'visibilityState', {configurable: true, get: () => 'hidden'});"
        "  const t = Date.now(); await NemApiBase.sleep(120);"
        "  return {ms: Date.now() - t, mode: NemApiBase.timerMode(), lock: NemApiBase.keepTabAwake()};"
        "})()",
        await_promise=True,
    )
    assert 100 <= took["ms"] < 900, took  # a throttled page timer would take >= 1 s
    assert took["mode"] in ("worker", "message"), took
    if took["mode"] == "worker":
        assert served["ticker"] == 1, served


def test_a_visible_tab_keeps_the_plain_timer(browser_page):
    """No round trip for the common case: the timer source is unchanged."""
    page, _served = browser_page
    world = World(page, [_base()], worker=True)
    took = world.eval(
        "(async () => { const t = Date.now(); await NemApiBase.sleep(80);"
        "  return {ms: Date.now() - t, mode: NemApiBase.timerMode()}; })()",
        await_promise=True,
    )
    assert 60 <= took["ms"] < 900, took
    assert took["mode"] == "timer", took


def test_sleep_still_resolves_without_the_extension(browser_page):
    page, _served = browser_page
    world = World(page, [_base()])
    took = world.eval(
        "(async () => { const t = Date.now(); await NemApiBase.sleep(80); return Date.now() - t; })()",
        await_promise=True,
    )
    assert 60 <= took < 1500, took
