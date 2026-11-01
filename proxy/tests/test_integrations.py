"""v5 « Intégration agents IA »: every agent integration is off by default and,
while it is off, NemApi behaves like the classic v4.0.0 proxy (fresh chat setting,
full prompt typed every time, side queries sent to the browser, no keep-alive).
"""
import json
import sys
from pathlib import Path

import httpx
import pytest

from conftest import FakeExtension

from nemapi import __version__

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "tools"))
from virtual_agent import QWEN_CODE_UA, configure  # noqa: E402

FEATURES = {
    "persistent_chat", "incremental_context", "tool_calls",
    "side_queries_local", "continuation", "keepalive",
}
SYSTEM = {"role": "system", "content": "You are Qwen Code, an interactive CLI agent developed by Alibaba Group."}
QWEN = {"User-Agent": QWEN_CODE_UA}


def _body(*messages, **extra):
    return {"model": "qwen-chat", "stream": False, "messages": list(messages), **extra}


async def _integrations(c):
    return (await c.get("/agent-integrations")).json()


def _qwen(snapshot):
    return next(a for a in snapshot["agents"] if a["id"] == "qwen-code")


@pytest.mark.anyio
async def test_defaults_off_and_shape(server):
    async with httpx.AsyncClient(base_url=server.url) as c:
        snap = await _integrations(c)
        assert set(snap) >= {"agents", "clients", "sessions"}
        q = _qwen(snap)
        assert q["name"] == "Qwen Code" and q["enabled"] is False
        assert {f["id"] for f in q["features"]} == FEATURES
        assert all(f["enabled"] is True and f["default"] is True for f in q["features"])  # ready once switched on
        assert snap["sessions"]["enabled"] is False
        # every agent is listed, and every one of them is off by default
        assert (await c.get("/status")).json()["integrations"] == {
            a["id"]: False for a in snap["agents"]
        }
        assert {a["id"] for a in snap["agents"]} == {"qwen-code", "monkey-code", "nemesis"}
        health = (await c.get("/health")).json()
        assert health["app_version"] == __version__
        settings = (await c.post("/settings", json={})).json()
        assert set(settings) == {"stream_enabled", "auto_config", "fresh_chat", "premium_md"}


@pytest.mark.anyio
async def test_unknown_agent_and_partial_update(server):
    async with httpx.AsyncClient(base_url=server.url) as c:
        r = await c.post("/agent-integrations/cline", json={"enabled": True})
        assert r.status_code == 404 and r.json()["error"]["type"] == "not_found"
        r = await c.post("/agent-integrations/qwen-code", json={"features": {"keepalive": False, "nope": True}})
        assert r.status_code == 200
        q = _qwen(await _integrations(c))
        assert q["enabled"] is False  # a feature change alone does not switch the integration on
        assert {f["id"]: f["enabled"] for f in q["features"]}["keepalive"] is False
        await c.post("/agent-integrations/qwen-code", json={"enabled": True})
        assert (await c.get("/sessions")).json()["enabled"] is True
        await c.post("/agent-integrations/qwen-code", json={"enabled": False})
        assert (await c.get("/sessions")).json()["enabled"] is False


@pytest.mark.anyio
async def test_qwen_code_stays_classic_while_the_integration_is_off(server):
    async with FakeExtension(server.url) as ext, httpx.AsyncClient(base_url=server.url, timeout=20) as c:
        await configure(server.url, fresh_chat=True)
        for turn in range(3):
            msgs = [SYSTEM, {"role": "user", "content": f"step {turn}"}]
            r = await c.post("/v1/chat/completions", json=_body(*msgs), headers=QWEN)
            assert r.status_code == 200, r.text
        assert len(ext.received) == 3
        assert all(j["freshChat"] is True for j in ext.received)  # config setting, as in v4.0
        assert all("Qwen Code" in j["question"] for j in ext.received)  # full prompt each time
        assert all(j.get("continuation") in (None, False) for j in ext.received)
        q = _qwen(await _integrations(c))
        assert q["detected"]["requests"] == 3 and q["detected"]["reason"] == "User-Agent"
        clients = (await _integrations(c))["clients"]
        assert clients[0]["name"] == "QwenCode/0.9.0" and clients[0]["mode"] == "Qwen Code · intégration désactivée"

        # side query: classic NemApi sends it to the browser
        n = len(ext.received)
        r = await c.post("/v1/chat/completions", headers=QWEN, json=_body(
            SYSTEM, {"role": "user", "content": "hi"}, {"role": "assistant", "content": "hello"},
            {"role": "user", "content": "[SUGGESTION MODE: suggest what the user may type next]"}))
        assert r.status_code == 200 and len(ext.received) == n + 1


@pytest.mark.anyio
async def test_integration_on_applies_only_to_the_agent(server):
    async with FakeExtension(server.url) as ext, httpx.AsyncClient(base_url=server.url, timeout=20) as c:
        await configure(server.url, incremental=True, fresh_chat=True)
        # an ordinary client stays classic
        r = await c.post("/v1/chat/completions", json=_body({"role": "user", "content": "plain"}),
                         headers={"User-Agent": "python-requests/2.32"})
        assert r.status_code == 200
        assert ext.received[-1]["freshChat"] is True
        clients = {x["name"]: x["mode"] for x in (await _integrations(c))["clients"]}
        assert clients["python-requests/2.32"] == "classique"
        # Qwen Code uses the integration: one discussion, no new chat
        msgs = [SYSTEM, {"role": "user", "content": "first"}]
        await c.post("/v1/chat/completions", json=_body(*msgs), headers=QWEN)
        first = ext.received[-1]
        msgs += [{"role": "assistant", "content": f"ECHO: {first['question'][-40:]}"}, {"role": "user", "content": "second"}]
        await c.post("/v1/chat/completions", json=_body(*msgs), headers=QWEN)
        second = ext.received[-1]
        assert first["freshChat"] is False and second["freshChat"] is False
        assert "Qwen Code" not in second["question"] and "second" in second["question"]
        clients = {x["name"]: x["mode"] for x in (await _integrations(c))["clients"]}
        assert clients["QwenCode/0.9.0"] == "intégration Qwen Code"

        # side query answered locally (feature on) → nothing typed
        n = len(ext.received)
        r = await c.post("/v1/chat/completions", headers=QWEN, json=_body(
            *msgs, {"role": "assistant", "content": "ok"},
            {"role": "user", "content": "[SUGGESTION MODE: suggest what the user may type next]"}))
        assert r.status_code == 200 and len(ext.received) == n
        # …and sent to the browser once the feature is switched off
        await configure(server.url, side_queries_local=False)
        r = await c.post("/v1/chat/completions", headers=QWEN, json=_body(
            SYSTEM, {"role": "user", "content": "[SUGGESTION MODE: suggest what the user may type next]"}))
        assert r.status_code == 200 and len(ext.received) == n + 1


@pytest.mark.anyio
@pytest.mark.parametrize("how", ["header", "prompt"])
async def test_detection_without_the_user_agent(server, how):
    async with FakeExtension(server.url) as ext, httpx.AsyncClient(base_url=server.url, timeout=20) as c:
        await configure(server.url, incremental=True, fresh_chat=True)
        headers = {"User-Agent": "OpenAI/Python 1.0"}
        system = {"role": "system", "content": "generic agent"}
        if how == "header":
            headers["X-NemApi-Agent"] = "qwen-code"
        else:
            system = SYSTEM
        r = await c.post("/v1/chat/completions", json=_body(system, {"role": "user", "content": "go"}), headers=headers)
        assert r.status_code == 200
        assert ext.received[-1]["freshChat"] is False  # integration (incremental) applied
        reason = _qwen(await _integrations(c))["detected"]["reason"]
        assert reason == ("en-tête X-NemApi-Agent" if how == "header" else "prompt système")


@pytest.mark.anyio
async def test_explicit_header_for_another_agent_wins_over_the_prompt(server):
    async with FakeExtension(server.url) as ext, httpx.AsyncClient(base_url=server.url, timeout=20) as c:
        await configure(server.url, incremental=True, fresh_chat=True)
        r = await c.post("/v1/chat/completions", json=_body(SYSTEM, {"role": "user", "content": "go"}),
                         headers={"X-NemApi-Agent": "my-script", "User-Agent": QWEN_CODE_UA})
        assert r.status_code == 200 and ext.received[-1]["freshChat"] is True  # classic


@pytest.mark.anyio
async def test_incremental_feature_off_keeps_the_rest(server):
    """Without the incremental context the prompt is still typed in full, but
    the discussion is the same one (no new URL) and the side queries are
    still answered locally."""
    async with FakeExtension(server.url) as ext, httpx.AsyncClient(base_url=server.url, timeout=20) as c:
        await configure(server.url, integration=True, incremental=False, fresh_chat=True)
        assert (await c.get("/sessions")).json()["enabled"] is False
        for word in ("a", "b"):
            await c.post("/v1/chat/completions", json=_body(SYSTEM, {"role": "user", "content": word}), headers=QWEN)
        assert all(j["freshChat"] is False for j in ext.received)  # « Plus de nouvelle URL »
        assert all("Qwen Code" in j["question"] for j in ext.received)  # full prompt each time
        n = len(ext.received)  # side queries are still answered locally
        r = await c.post("/v1/chat/completions", headers=QWEN, json=_body(
            SYSTEM, {"role": "user", "content": "[SUGGESTION MODE: suggest what the user may type next]"}))
        assert r.status_code == 200 and len(ext.received) == n


@pytest.mark.anyio
async def test_the_tool_calls_feature_decides_the_tool_contract(server):
    """« Appels d'outils » on: the agent's tools array is given to the model and
    the answer comes back as OpenAI tool_calls. Off: the tools are ignored."""
    tools = [{"type": "function", "function": {"name": "read_file", "description": "Read a file.",
                                               "parameters": {"type": "object", "properties": {}}}}]
    answer = 'Je lis.\n\n```tool\n<tool>{"name": "read_file", "arguments": {"path": "a.py"}}</tool>\n```'
    async with FakeExtension(server.url) as ext, httpx.AsyncClient(base_url=server.url, timeout=20) as c:
        await configure(server.url, integration=True)
        await c.post("/v1/chat/completions", json=_body(SYSTEM, {"role": "user", "content": "go"}, tools=tools),
                     headers=QWEN)
        assert "You can call tools" in ext.received[-1]["question"]
        assert "read_file" in ext.received[-1]["question"]

        await configure(server.url, tool_calls=False)
        await c.post("/v1/chat/completions", json=_body(SYSTEM, {"role": "user", "content": "go2"}, tools=tools),
                     headers=QWEN)
        assert "You can call tools" not in ext.received[-1]["question"]
        assert "read_file" not in ext.received[-1]["question"]


@pytest.mark.anyio
async def test_legacy_settings_are_dropped_and_persisted_state_reloads(server_factory, tmp_path):
    data = tmp_path / "data"
    data.mkdir()
    (data / "config.json").write_text(json.dumps({
        "settings": {"incremental_context": True, "side_queries_local": True, "fresh_chat": False}}))
    srv = server_factory(data)
    async with httpx.AsyncClient(base_url=srv.url) as c:
        assert _qwen(await _integrations(c))["enabled"] is False  # not migrated: off by default
        assert (await c.get("/sessions")).json()["enabled"] is False
        settings = (await c.post("/settings", json={"incremental_context": True})).json()
        assert "incremental_context" not in settings and settings["fresh_chat"] is False
        await c.post("/agent-integrations/qwen-code", json={"enabled": True, "features": {"continuation": False}})
    saved = json.loads((data / "config.json").read_text())
    assert "incremental_context" not in saved.get("settings", {})
    assert saved["integrations"]["qwen-code"]["enabled"] is True
    srv.stop()
    srv2 = server_factory(data)
    async with httpx.AsyncClient(base_url=srv2.url) as c:
        q = _qwen(await _integrations(c))
        assert q["enabled"] is True
        assert {f["id"]: f["enabled"] for f in q["features"]}["continuation"] is False
