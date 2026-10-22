"""Real browser check of the admin web UI (optional: needs `pip install playwright`
and `python -m playwright install chromium`, skipped otherwise).

* every page loads without JavaScript error and shows the shared menu, with the
  « Intégration agents IA » tab;
* the configuration switches work and the incremental context is no longer there;
* « Intégration agents IA »: Qwen Code is off by default (classic NemApi), its
  features are locked while it is off, it can be switched on and off from the
  page, persists after a reload, and the session status / reset work with a
  virtual Qwen Code agent running.
"""
import asyncio
import sys
from pathlib import Path

import httpx
import pytest

playwright_api = pytest.importorskip("playwright.async_api")

from nemapi import __version__  # noqa: E402

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "tools"))
from virtual_agent import TASK_1, VirtualCodingAgent, WebUIExtension  # noqa: E402

PAGES = ["/", "/config", "/integrations", "/chat", "/analytics", "/api-keys-page"]
FEATURES = ("persistent_chat", "incremental_context", "tool_calls",
            "side_queries_local", "continuation", "keepalive")


async def _get(url, path):
    async with httpx.AsyncClient(base_url=url) as c:
        return (await c.get(path)).json()


async def _qwen(url):
    return next(a for a in (await _get(url, "/agent-integrations"))["agents"] if a["id"] == "qwen-code")


async def _wait_for(predicate, timeout=8.0):
    deadline = asyncio.get_running_loop().time() + timeout
    while True:
        value = await predicate()
        if value:
            return value
        if asyncio.get_running_loop().time() > deadline:
            raise AssertionError("condition not reached")
        await asyncio.sleep(0.1)


async def _text_has(locator, needle):
    return needle in (await locator.inner_text())


@pytest.fixture
async def browser_page(anyio_backend):
    try:
        pw = await playwright_api.async_playwright().start()
        browser = await pw.chromium.launch()
    except Exception as exc:  # browser binaries not installed
        pytest.skip(f"chromium unavailable: {exc}")
    errors, dialogs = [], []
    page = await browser.new_page()
    page.on("pageerror", lambda e: errors.append(f"{page.url}: {e}"))
    page.on("console", lambda m: m.type == "error" and errors.append(f"{page.url}: {m.text}"))
    page.on("dialog", lambda d: (dialogs.append(d.message), asyncio.ensure_future(d.dismiss())))
    try:
        yield page
    finally:
        await browser.close()
        await pw.stop()
    assert not errors, errors
    assert not dialogs, dialogs  # no native alert/confirm anymore (toasts + modal)


@pytest.mark.anyio
async def test_every_page_has_the_shared_menu(server, browser_page):
    page = browser_page
    async with WebUIExtension(server.url):
        for path in PAGES:
            r = await page.goto(server.url + path)
            assert r.status == 200, path
            nav = page.locator(".sidebar .nav a")
            await nav.first.wait_for()
            labels = [t.strip() for t in await nav.all_inner_texts()]
            assert any("Intégration agents IA" in t for t in labels), (path, labels)
            assert await page.locator(".sidebar .nav a.active").count() == 1, path
            await _wait_for(lambda: _text_has(page.locator("#hconn"), "Extension connectée"))
        await _wait_for(lambda: _text_has(page.locator("#side-version"), f"v{__version__}"))
        # theme toggle persists
        await page.click("#theme-btn")
        assert await page.evaluate("document.documentElement.dataset.theme") == "light"
        await page.reload()
        assert await page.evaluate("document.documentElement.dataset.theme") == "light"
        # mobile: the hamburger opens the menu
        await page.set_viewport_size({"width": 390, "height": 800})
        await page.click(".menu-btn")
        await page.wait_for_timeout(300)
        assert await page.locator(".sidebar.open").count() == 1
        await page.locator('.sidebar a[data-nav="integrations"]').click()
        await page.wait_for_url("**/integrations")


@pytest.mark.anyio
async def test_config_switches(server, browser_page):
    page = browser_page
    async with WebUIExtension(server.url):
        await page.goto(server.url + "/config")
        assert await page.locator("#cfg-incremental").count() == 0  # moved to the integrations page
        await _wait_for(lambda: _text_has(page.locator("#cfg-providers"), "Qwen"))
        await page.wait_for_timeout(600)
        for box_id, key in (("cfg-stream", "stream_enabled"), ("cfg-auto-config", "auto_config"),
                            ("cfg-fresh-chat", "fresh_chat"), ("cfg-premium-md", "premium_md")):
            before = (await _get(server.url, "/settings"))[key]
            assert await page.locator(f"#{box_id}").is_checked() == before, box_id
            for target in (not before, before):
                await page.locator(f"label.switch:has(#{box_id})").click()
                await _wait_for(lambda: _is(server.url, key, target))
        # stop jobs: confirmation modal
        await page.click("#cfg-stop")
        await page.locator('.modal [data-a="1"]').click()
        await _wait_for(lambda: _text_has(page.locator("#toasts"), "annulé"))


async def _is(url, key, value):
    return (await _get(url, "/settings"))[key] is value


@pytest.mark.anyio
async def test_qwen_code_integration_page(server, browser_page, tmp_path):
    page = browser_page
    async with WebUIExtension(server.url) as ext:
        await page.goto(server.url + "/integrations")
        main = page.locator("#int-qwen-code-enabled")
        await main.wait_for(state="attached")
        await page.wait_for_timeout(500)
        assert not await main.is_checked()  # off by default
        assert (await _qwen(server.url))["enabled"] is False
        for f in FEATURES:  # features locked while the integration is off
            assert await page.locator(f"#int-qwen-code-{f}").is_disabled(), f
        await _wait_for(lambda: _text_has(page.locator("#int-sessions"), "Inactif"))

        # classic mode: the agent is detected but its requests stay classic
        # (the discussion still persists by default — only the integration is off)
        agent = VirtualCodingAgent(server.url)
        assert (await agent.ask(TASK_1)).startswith("TASK-1 DONE")
        assert all(j["freshChat"] is False for j in ext.jobs)
        await _wait_for(lambda: _text_has(page.locator("#int-qwen-code-detect"), "mode classique"))
        await _wait_for(lambda: _text_has(page.locator("#clients"), "Qwen Code · intégration désactivée"))

        # switch ON from the page
        await page.locator("label.switch:has(#int-qwen-code-enabled)").click()
        await _wait_for(lambda: _qwen_on(server.url, True))
        await _wait_for(lambda: _text_has(page.locator("#int-qwen-code-state"), "Activée"))
        for f in FEATURES:
            await _wait_for(lambda: _enabled(page, f"#int-qwen-code-{f}"))
            assert await page.locator(f"#int-qwen-code-{f}").is_checked(), f  # all on by default
        status = page.locator("#int-sessions")
        await _wait_for(lambda: _text_has(status, "Aucune session active"))

        # a feature can be switched off and on again
        await page.locator("label.switch:has(#int-qwen-code-keepalive)").click()
        await _wait_for(lambda: _feature(server.url, "keepalive", False))
        await page.locator("label.switch:has(#int-qwen-code-keepalive)").click()
        await _wait_for(lambda: _feature(server.url, "keepalive", True))

        # the virtual Qwen Code agent now uses the incremental context
        n = len(ext.jobs)
        agent.messages = agent.messages[:1]
        assert (await agent.ask(TASK_1)).startswith("TASK-1 DONE")
        await agent.aclose()
        assert all(j["freshChat"] is False for j in ext.jobs[n:])  # one discussion, never a new chat
        await _wait_for(lambda: _text_has(status, "5 tour(s)"))
        assert "économisés" in await status.inner_text()
        await _wait_for(lambda: _text_has(page.locator("#clients"), "intégration Qwen Code"))
        await _wait_for(lambda: _text_has(page.locator("#nav-badge-integrations"), "1"))
        await page.screenshot(path=str(tmp_path / "integrations.png"), full_page=True)

        # reset (confirmation modal)
        await page.click("#int-reset")
        await page.locator('.modal [data-a="1"]').click()
        await _wait_for(lambda: _text_has(status, "Aucune session active"))

        # persists after a reload
        await page.reload()
        await page.wait_for_timeout(800)
        assert await page.locator("#int-qwen-code-enabled").is_checked()

        # switch OFF → classic NemApi again
        await page.locator("label.switch:has(#int-qwen-code-enabled)").click()
        await _wait_for(lambda: _qwen_on(server.url, False))
        await _wait_for(lambda: _text_has(status, "Inactif"))
        assert await page.locator("#int-qwen-code-incremental_context").is_disabled()


async def _qwen_on(url, value):
    return (await _qwen(url))["enabled"] is value


async def _feature(url, fid, value):
    return {f["id"]: f["enabled"] for f in (await _qwen(url))["features"]}[fid] is value


async def _enabled(page, selector):
    return not await page.locator(selector).is_disabled()


@pytest.mark.anyio
async def test_chat_analytics_and_api_keys_pages(server, browser_page):
    page = browser_page
    async with WebUIExtension(server.url) as ext:
        # chat test: streams through the proxy (classic mode, the browser is not an agent)
        await page.goto(server.url + "/chat")
        await _wait_for(lambda: _count(page, "#chat-model option"))
        await page.select_option("#chat-model", "qwen-chat")
        await page.fill("#chat-input", "Bonjour")
        await page.keyboard.press("Enter")
        await _wait_for(lambda: _text_has(page.locator(".bubble.assistant"), "ERROR_NO_TOOLS_IN_CONTEXT"))
        # the chat never reopens the discussion: persistent chat is the default
        assert ext.jobs[-1]["freshChat"] is False and ext.jobs[-1]["provider"] == "qwen"
        await page.click("#chat-clear")
        assert await page.locator(".bubble").count() == 0

        # analytics: the request is counted, then cleared through the modal
        await page.goto(server.url + "/analytics")
        await _wait_for(lambda: _text_has(page.locator("#total-requests"), "1"))
        assert "Qwen" in await page.locator("#provider-stats").inner_text()
        await page.click("#btn-clear-stats")
        await page.locator('.modal [data-a="1"]').click()
        await _wait_for(lambda: _text_has(page.locator("#total-requests"), "0"))

        # API keys: create, then delete through the modal
        await page.goto(server.url + "/api-keys-page")
        await page.locator("#new-key-name").wait_for()
        await page.fill("#new-key-name", "ui-test")
        await page.click("#new-key-btn")
        await _wait_for(lambda: _text_has(page.locator("#new-key-value"), "nemapi-token"))
        await _wait_for(lambda: _text_has(page.locator("#api-keys-list"), "ui-test"))
        await page.locator("#api-keys-list [data-del]").first.click()
        await page.locator('.modal [data-a="1"]').click()
        await _wait_for(lambda: _no_text(page.locator("#api-keys-list"), "ui-test"))


async def _count(page, selector):
    return await page.locator(selector).count()


async def _no_text(locator, needle):
    return needle not in (await locator.inner_text())


@pytest.mark.anyio
async def test_every_icon_is_sized(server, browser_page):
    """An SVG with no width fills its container: every icon must carry the
    "ico" class, which carries the default size."""
    page = browser_page
    async with WebUIExtension(server.url):
        for path in PAGES:
            await page.goto(server.url + path)
            await page.locator(".sidebar .nav a").first.wait_for()
            await page.wait_for_timeout(400)
            sizes = await page.evaluate("""() => {
              const out = [];
              for (const svg of document.querySelectorAll("svg")) {
                const r = svg.getBoundingClientRect();
                if (r.width > 40 || r.height > 40) {
                  out.push({cls: svg.getAttribute("class"), w: Math.round(r.width), h: Math.round(r.height)});
                }
              }
              return out;
            }""")
            assert not sizes, (path, sizes)


@pytest.mark.anyio
async def test_the_analytics_page_shows_the_reference_prices(server, browser_page):
    """The pricing table carries the cache price, the source and the date the
    prices were checked on."""
    page = browser_page
    async with WebUIExtension(server.url):
        await page.goto(server.url + "/analytics")
        await page.locator("#pricing-body tr").first.wait_for()
        rows = await page.locator("#pricing-body tr").count()
        assert rows == 7, rows
        text = await page.locator("#pricing-body").inner_text()
        assert "DeepSeek V4-Pro" in text and "GLM-5.3" in text
        assert "$0.00066" in text  # DeepSeek input / 1K
        assert "$0.00002" in text  # its cached input / 1K
        checked = await page.locator("#price-date").inner_text()
        assert checked and checked != "—"
        # the daily chart is drawn from the history
        await page.locator("#trend-chart").wait_for()
        assert await page.locator("#trend-chart svg.chart, #trend-chart .chart-empty").count() == 1


@pytest.mark.anyio
async def test_the_integrations_page_explains_how_the_agent_is_recognised(server, browser_page):
    page = browser_page
    async with WebUIExtension(server.url):
        await page.goto(server.url + "/integrations")
        await page.locator("#int-qwen-code-enabled").wait_for(state="attached")
        await page.wait_for_timeout(400)
        for agent in ("qwen-code", "nemesis"):
            card = page.locator(f'[data-agent="{agent}"]')
            text = (await card.inner_text()).lower()
            assert "x-nemapi-agent" in text, agent
            # the section title is uppercased by CSS, hence the lower-case comparison
            assert "comment nemapi le reconnaît" in text, agent
        # the three things Qwen Code must get right are named as features
        qwen = await page.locator('[data-agent="qwen-code"] .features').inner_text()
        assert "Plus de nouvelle URL" in qwen
        assert "Contexte incrémental" in qwen
        assert "Appels d'outils" in qwen
