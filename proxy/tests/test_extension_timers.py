"""Timers, cheap polling and the freeze opt-outs of the content script.

v5.4 turned two user-visible bugs into invariants, and this module locks them:

* a request that hangs while the browser is in the background and finishes
  "instantly" when the tab is brought back. Chrome throttles a hidden page's
  chained timers to one wake per second, then to one per MINUTE after five
  minutes hidden ("intensive wake-up throttling"); it also FREEZES a silent,
  hidden tab (Energy Saver) where nothing runs at all. The content script now
  waits on a dedicated Worker's timers (not throttled), falls back to a
  MessageChannel bounce (the timer armed from a message task restarts the
  nesting level, so the 5+ chain condition is never met), and holds a Web Lock,
  which Chrome documents as an opt-out of freezing.

* a provider tab that became slow until it was closed and reopened. The wait
  loops polls the CHEAP text of the last answer and rebuilt the whole markdown
  on every 400 ms tick: the cost grew with the answer, on the page's own main
  thread. The markdown is now built once, when the answer is accepted
  (`readCheap` / `getFinal`).

The behavioural tests run in a real Chromium through CDP (isolated world), like
`test_extension_dom.py`; they are skipped when playwright is not installed.
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


def test_every_provider_exposes_a_cheap_polling_reader():
    for name in PROVIDER_FILES:
        source = (PROVIDERS / f"{name}.js").read_text()
        assert "function readCheapText" in source, name
        assert "function getActivitySignature" in source, name
        exported = source[source.index(f"NemApiProviders.{name} = {{"):]
        assert "readCheapText," in exported, name
        assert "getActivitySignature," in exported, name


def test_wait_loops_do_not_rebuild_the_markdown_on_every_tick():
    """A provider must either pass `readCheap` or let `getFinal` do the build."""
    for name in PROVIDER_FILES:
        source = (PROVIDERS / f"{name}.js").read_text()
        start = source.index("async function waitForResponse")
        body = source[start:source.index("\n  }\n", start)]
        assert "readCheap:" in body or "getFinal:" in body, name
        # the fixed sleeps paid by every request are gone
        assert "await B.sleep(900)" not in body, name
        assert "await B.sleep(1200)" not in body, name
        assert "await B.sleep(1000)" not in body, name


def test_base_exports_the_timer_and_cheap_read_helpers():
    source = (PROVIDERS / "base.js").read_text()
    exports = source[source.index("global.NemApiBase = {"):]
    for name in ["sleep", "sleepOrChange", "timerMode", "cheapText", "sameAnswer", "keepTabAwake"]:
        assert name in exports, name
    # the hidden-tab workaround itself
    assert "providers/ticker.js" in source
    assert 'new MessageChannel()' in source
    assert 'navigator.locks' in source


def test_content_script_drops_raw_timers():
    """Every wait of the router must go through the throttling-proof timer."""
    source = (EXT / "content.js").read_text()
    assert "B.sleep" in source or "base().sleep" in source
    assert "keepTabAwake" in source
    # the only raw timer left is the fallback used when base.js is missing
    assert "setTimeout(" not in source.replace("new Promise((r) => setTimeout(r, ms))", "")


def test_background_protects_the_provider_tabs():
    source = (EXT / "background.js").read_text()
    assert "autoDiscardable: false" in source
    assert "routerAlive" in source  # no blind re-injection before every job
    assert "JOB_LONG_POLL" in source


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
PAGE = """<!doctype html><html><body>
<div id="answers">
  <div class="msg" id="old">ancienne reponse</div>
</div>
</body></html>"""

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
            return None

        page.route(ORIGIN + "/**", handle)
        page.goto(ORIGIN + "/chat.html")
        yield page, served
        browser.close()


def _base() -> str:
    return (PROVIDERS / "base.js").read_text()


def test_sleep_runs_on_the_ticker_worker(browser_page):
    """The Worker is spawned and answers; without it a hidden tab is throttled."""
    page, served = browser_page
    world = World(page, [_base()], worker=True)
    took = world.eval(
        "(async () => { const t = Date.now(); await NemApiBase.sleep(120);"
        "  return {ms: Date.now() - t, mode: NemApiBase.timerMode()}; })()",
        await_promise=True,
    )
    # a hidden tab's plain timer is clamped to 1 s, and the Worker is the only
    # way to keep the pace: the sleep must come back far below that
    assert 100 <= took["ms"] < 900, took
    assert took["mode"] in ("worker", "message"), took  # "message" = Worker refused by the page
    if took["mode"] == "worker":
        assert served["ticker"] == 1, served


def test_sleep_still_resolves_without_the_extension(browser_page):
    page, _served = browser_page
    world = World(page, [_base()])
    took = world.eval(
        "(async () => { const t = Date.now(); await NemApiBase.sleep(80); return Date.now() - t; })()",
        await_promise=True,
    )
    assert 60 <= took < 1500, took


def test_polling_never_rebuilds_the_markdown(browser_page):
    """`readText` (the markdown build) runs once; the poll uses the cheap read."""
    page, _served = browser_page
    world = World(page, [_base()])
    result = world.eval(
        """
        (async () => {
          const answers = document.getElementById('answers');
          let heavy = 0, cheap = 0;
          const heavyRead = (el) => { heavy += 1; return '<p>' + (el ? el.textContent : '') + '</p>'; };
          const cheapRead = (el) => { cheap += 1; return el ? el.textContent : ''; };
          const p = NemApiBase.waitForNewResponse(
            () => Array.from(answers.querySelectorAll('.msg')),
            heavyRead,
            { timeout: 8000, stableMs: 150, previous: 'ancienne reponse', newElTimeout: 2000, readCheap: cheapRead }
          );
          await NemApiBase.sleep(150);
          const div = document.createElement('div');
          div.className = 'msg';
          answers.appendChild(div);
          for (const t of ['Bon', 'Bonjour', 'Bonjour le monde']) {
            div.textContent = t;
            await NemApiBase.sleep(120);
          }
          const out = await p;
          return {out: String(out), heavy: heavy, cheap: cheap};
        })()
        """,
        await_promise=True,
    )
    assert "Bonjour le monde" in result["out"], result
    assert result["cheap"] > 2, result
    assert result["heavy"] <= 2, result


def test_a_finished_answer_is_accepted_without_waiting_the_full_window(browser_page):
    """`idleStableMs`: a page that says it finished is believed after ~900 ms."""
    page, _served = browser_page
    world = World(page, [_base()])
    result = world.eval(
        """
        (async () => {
          const el = document.getElementById('old');
          window.__NEMAPI_IS_BUSY__ = () => false;
          const t = Date.now();
          el.textContent = 'la reponse complete';
          const out = await NemApiBase.waitUntilStable(
            () => el.textContent,
            { timeout: 8000, stableMs: 2500, pollMs: 100 }
          );
          return {out: out, ms: Date.now() - t};
        })()
        """,
        await_promise=True,
    )
    assert result["out"] == "la reponse complete", result
    assert result["ms"] < 2000, result


def test_a_page_that_still_says_busy_is_believed_after_a_bounded_grace(browser_page):
    page, _served = browser_page
    world = World(page, [_base()])
    result = world.eval(
        """
        (async () => {
          const el = document.getElementById('old');
          window.__NEMAPI_IS_BUSY__ = () => true;
          el.textContent = 'reponse figee';
          const t = Date.now();
          const out = await NemApiBase.waitUntilStable(
            () => el.textContent,
            { timeout: 20000, stableMs: 2500, pollMs: 100, busyGraceMs: 1200 }
          );
          return {out: out, ms: Date.now() - t};
        })()
        """,
        await_promise=True,
    )
    assert result["out"] == "reponse figee", result
    # the old code waited `incompleteStableMs` (25 s) for a stuck indicator
    assert result["ms"] < 6000, result


def test_the_previous_answer_is_never_returned(browser_page):
    page, _served = browser_page
    world = World(page, [_base()])
    result = world.eval(
        """
        (async () => {
          const el = document.getElementById('old');
          try {
            await NemApiBase.waitUntilStable(
              () => el.textContent,
              { timeout: 1200, stableMs: 200, pollMs: 100, previous: 'ancienne **reponse**' }
            );
            return 'returned';
          } catch (e) {
            return 'timeout';
          }
        })()
        """,
        await_promise=True,
    )
    assert result == "timeout", result
