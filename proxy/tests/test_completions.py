"""End-to-end chat completions through a simulated browser extension."""
import asyncio
import json
import time

import httpx
import pytest

from conftest import FakeExtension

pytestmark = pytest.mark.anyio

MSG = [{"role": "user", "content": "Bonjour"}]


def parse_sse(text: str):
    events = [line[6:] for line in text.split("\n") if line.startswith("data: ")]
    assert events[-1] == "[DONE]"
    return [json.loads(e) for e in events[:-1]]


async def test_validation_errors(server):
    async with httpx.AsyncClient(base_url=server.url) as c:
        r = await c.post("/v1/chat/completions", content=b"{bad json", headers={"content-type": "application/json"})
        assert r.status_code == 400 and r.json()["error"]["message"] == "Invalid JSON body"
        r = await c.post("/v1/chat/completions", json={"messages": MSG})
        assert r.status_code == 400 and "'model' field is required" in r.json()["error"]["message"]
        r = await c.post("/v1/chat/completions", json={"model": "qwen-chat"})
        assert r.status_code == 400 and "message" in r.json()["error"]["message"]
        r = await c.post("/v1/chat/completions", json={"model": "qwen-chat", "messages": ["hi"]})
        assert r.status_code == 400
        r = await c.post("/v1/chat/completions", json={"model": "qwen-chat", "messages": [{"role": "user", "content": "  "}]})
        assert r.status_code == 400 and "empty prompt" in r.json()["error"]["message"]
        r = await c.post("/v1/chat/completions", json={"model": "x", "provider": "nope", "messages": MSG})
        assert r.status_code == 400 and "Unknown provider" in r.json()["error"]["message"]


async def test_extension_offline_503(server):
    async with httpx.AsyncClient(base_url=server.url) as c:
        t0 = time.perf_counter()
        r = await c.post("/v1/chat/completions", json={"model": "qwen-chat", "messages": MSG})
        assert r.status_code == 503 and r.json()["error"]["type"] == "extension_unavailable"
        assert time.perf_counter() - t0 < 2  # extension_wait=0.5s in tests


async def test_no_tab_selected_409(server):
    async with FakeExtension(server.url, providers=["qwen"]):
        async with httpx.AsyncClient(base_url=server.url) as c:
            r = await c.post("/v1/chat/completions", json={"model": "claude-sonnet", "messages": MSG})
            assert r.status_code == 409 and r.json()["error"]["type"] == "provider_not_selected"


async def test_non_stream_completion(server):
    async with FakeExtension(server.url) as ext:
        async with httpx.AsyncClient(base_url=server.url, timeout=10) as c:
            r = await c.post(
                "/v1/chat/completions",
                json={"model": "qwen-plus", "messages": [{"role": "system", "content": "Sois bref"}, *MSG], "stream": False},
            )
        assert r.status_code == 200, r.text
        data = r.json()
        assert data["object"] == "chat.completion"
        assert data["model"] == "qwen-plus"  # client model echoed back
        assert data["choices"][0]["finish_reason"] == "stop"
        assert data["choices"][0]["message"]["content"].startswith("ECHO:")
        assert data["usage"]["total_tokens"] > 0
        job = ext.received[0]
        assert job["provider"] == "qwen" and job["model"] == "qwen-chat"
        assert "Sois bref" in job["question"] and "User: Bonjour" in job["question"]
        assert job["freshChat"] is True and job["premiumMd"] is True
    st = server.ctx.stats.report(server.ctx.started_at, "t")
    assert st["total_requests"] == 1 and st["providers"]["qwen"]["requests"] == 1


async def test_stream_completion_and_accept_header(server):
    long_text = "Lorem ipsum dolor sit amet. " * 20

    async def responder(job):
        return "result", long_text

    async with FakeExtension(server.url, responder=responder):
        async with httpx.AsyncClient(base_url=server.url, timeout=10) as c:
            r = await c.post("/v1/chat/completions", json={"model": "deepseek-chat", "messages": MSG, "stream": True})
            assert r.status_code == 200
            assert r.headers["content-type"].startswith("text/event-stream")
            chunks = parse_sse(r.text)
            assert chunks[0]["choices"][0]["delta"] == {"role": "assistant"}
            content = "".join(ch["choices"][0]["delta"].get("content", "") for ch in chunks)
            assert content == long_text.strip()
            assert chunks[-1]["choices"][0]["finish_reason"] == "stop"
            assert chunks[-1]["usage"]["completion_tokens"] > 0
            assert all(ch["object"] == "chat.completion.chunk" for ch in chunks)

            # stream omitted + SSE Accept header => streaming
            r = await c.post(
                "/v1/chat/completions",
                json={"model": "deepseek-chat", "messages": [{"role": "user", "content": "autre"}]},
                headers={"Accept": "text/event-stream"},
            )
            assert r.headers["content-type"].startswith("text/event-stream")


async def test_stream_default_follows_admin_setting(server):
    async with FakeExtension(server.url):
        async with httpx.AsyncClient(base_url=server.url, timeout=10) as c:
            await c.post("/settings", json={"stream_enabled": False})
            r = await c.post("/v1/chat/completions", json={"model": "kimi", "messages": MSG})
            assert r.headers["content-type"].startswith("application/json")
            await c.post("/settings", json={"stream_enabled": True})
            r = await c.post("/v1/chat/completions", json={"model": "kimi", "messages": [{"role": "user", "content": "b"}]})
            assert r.headers["content-type"].startswith("text/event-stream")


async def test_tool_calls(server):
    async def responder(job):
        return "result", 'Je lis le fichier.\n<tool>{"name": "read_file", "arguments": {"path": "a.py"}}</tool>'

    tools = [{"type": "function", "function": {"name": "read_file", "parameters": {"type": "object"}}}]
    async with FakeExtension(server.url, responder=responder) as ext:
        async with httpx.AsyncClient(base_url=server.url, timeout=10) as c:
            r = await c.post("/v1/chat/completions", json={"model": "qwen3-coder-plus", "messages": MSG, "tools": tools, "stream": False})
            choice = r.json()["choices"][0]
            assert choice["finish_reason"] == "tool_calls"
            tc = choice["message"]["tool_calls"][0]
            assert tc["function"]["name"] == "read_file"
            assert json.loads(tc["function"]["arguments"]) == {"path": "a.py"}
            assert "read_file" in ext.received[0]["question"]

            r = await c.post("/v1/chat/completions", json={"model": "qwen3-coder-plus", "messages": [{"role": "user", "content": "x"}], "tools": tools, "stream": True})
            chunks = parse_sse(r.text)
            names = [
                d["tool_calls"][0]["function"].get("name")
                for d in (ch["choices"][0]["delta"] for ch in chunks)
                if "tool_calls" in d
            ]
            assert "read_file" in names
            assert chunks[-1]["choices"][0]["finish_reason"] == "tool_calls"


async def test_parallel_providers_and_serialized_same_provider(server):
    async def slow(job):
        await asyncio.sleep(0.5)
        return "result", job["provider"]

    async with FakeExtension(server.url, responder=slow, long_poll=0.2) as ext:
        async with httpx.AsyncClient(base_url=server.url, timeout=15) as c:
            def req(model, i):
                return c.post("/v1/chat/completions", json={"model": model, "messages": [{"role": "user", "content": f"q{i}"}], "stream": False})

            t0 = time.perf_counter()
            rs = await asyncio.gather(req("qwen-chat", 1), req("claude-chat", 2), req("gemini-chat", 3), req("deepseek-chat", 4))
            elapsed = time.perf_counter() - t0
            assert all(r.status_code == 200 for r in rs)
            assert [r.json()["choices"][0]["message"]["content"] for r in rs] == ["qwen", "claude", "gemini", "deepseek"]
            assert elapsed < 1.5, elapsed  # 4 x 0.5s ran in parallel
            assert ext.max_parallel >= 3

            t0 = time.perf_counter()
            rs = await asyncio.gather(req("qwen-chat", 10), req("qwen-chat", 11), req("qwen-chat", 12))
            elapsed = time.perf_counter() - t0
            assert all(r.status_code == 200 for r in rs)
            assert elapsed >= 1.4, elapsed  # same provider => one at a time


async def test_dedup_identical_inflight(server):
    async def slow(job):
        await asyncio.sleep(0.3)
        return "result", "same"

    async with FakeExtension(server.url, responder=slow) as ext:
        async with httpx.AsyncClient(base_url=server.url, timeout=10) as c:
            body = {"model": "zai", "messages": MSG, "stream": False}
            r1, r2 = await asyncio.gather(c.post("/v1/chat/completions", json=body), c.post("/v1/chat/completions", json=body))
            assert r1.status_code == r2.status_code == 200
            assert len(ext.received) == 1


async def test_automation_error_500(server):
    async def fail(job):
        return "error", "composer not found"

    async with FakeExtension(server.url, responder=fail):
        async with httpx.AsyncClient(base_url=server.url, timeout=10) as c:
            r = await c.post("/v1/chat/completions", json={"model": "gemini-chat", "messages": MSG})
            assert r.status_code == 500
            assert r.json()["error"] == {"message": "composer not found", "type": "automation_error"}
            # provider slot was released: next request works
            st = (await c.get("/status")).json()
            assert st["jobs"]["active"] == {}


async def test_timeout_504_releases_slot(server_factory):
    srv = server_factory(job_timeout=0.6)

    async def never(job):
        await asyncio.sleep(30)
        return "result", "late"

    async with FakeExtension(srv.url, responder=never):
        async with httpx.AsyncClient(base_url=srv.url, timeout=10) as c:
            r = await c.post("/v1/chat/completions", json={"model": "claude", "messages": MSG})
            assert r.status_code == 504 and r.json()["error"]["type"] == "timeout"
            assert (await c.get("/status")).json()["jobs"]["active"] == {}


async def test_client_disconnect_cancels_queued_job(server):
    gate = asyncio.Event()

    async def blocked(job):
        await gate.wait()
        return "result", "ok"

    async with FakeExtension(server.url, responder=blocked) as ext:
        async with httpx.AsyncClient(base_url=server.url, timeout=10) as c:
            first = asyncio.create_task(c.post("/v1/chat/completions", json={"model": "qwen", "messages": MSG}))
            await asyncio.sleep(0.3)  # first job dispatched, provider busy
            second = asyncio.create_task(
                c.post("/v1/chat/completions", json={"model": "qwen", "messages": [{"role": "user", "content": "abandon"}]})
            )
            await asyncio.sleep(0.3)
            assert (await c.get("/status")).json()["jobs"]["queues"] == {"qwen": 1}
            second.cancel()  # client gives up while its job is still queued
            await asyncio.gather(second, return_exceptions=True)
            for _ in range(30):
                if (await c.get("/status")).json()["jobs"]["queues"] == {}:
                    break
                await asyncio.sleep(0.1)
            assert (await c.get("/status")).json()["jobs"]["queues"] == {}
            gate.set()
            assert (await first).status_code == 200
            await asyncio.sleep(0.3)
            assert len(ext.received) == 1  # the abandoned prompt never reached the browser


async def test_stop_cancels_everything(server):
    gate = asyncio.Event()

    async def blocked(job):
        await gate.wait()
        return "result", "ok"

    async with FakeExtension(server.url, responder=blocked):
        async with httpx.AsyncClient(base_url=server.url, timeout=10) as c:
            tasks = [
                asyncio.create_task(c.post("/v1/chat/completions", json={"model": "qwen", "messages": [{"role": "user", "content": f"m{i}"}]}))
                for i in range(3)
            ]
            await asyncio.sleep(0.4)
            r = await c.post("/stop")
            assert r.json()["cancelled"] == 3
            results = await asyncio.gather(*tasks)
            assert all(x.status_code == 500 and x.json()["error"]["message"] == "stopped" for x in results)
            gate.set()


async def test_fresh_chat_per_request_override(server):
    async with FakeExtension(server.url) as ext:
        async with httpx.AsyncClient(base_url=server.url, timeout=10) as c:
            await c.post("/v1/chat/completions", json={"model": "qwen", "messages": MSG, "fresh_chat": False})
            await c.post("/settings", json={"fresh_chat": False})
            await c.post("/v1/chat/completions", json={"model": "qwen", "messages": [{"role": "user", "content": "2"}]})
            await c.post("/v1/chat/completions", json={"model": "qwen", "messages": [{"role": "user", "content": "3"}], "fresh_chat": True})
        assert [j["freshChat"] for j in ext.received] == [False, False, True]


async def test_long_poll_wakes_immediately(server):
    async with httpx.AsyncClient(base_url=server.url, timeout=10) as c:
        await c.post("/extension/tabs", json={"tabs": [{"id": 7, "provider": "qwen"}]})
        poll = asyncio.create_task(c.get("/job", params={"wait": 5}))
        await asyncio.sleep(0.2)
        assert not poll.done()
        t0 = time.perf_counter()
        comp = asyncio.create_task(c.post("/v1/chat/completions", json={"model": "qwen", "messages": MSG, "stream": False}))
        job = (await poll).json()
        assert job["action"] == "ask" and time.perf_counter() - t0 < 0.5
        await c.post("/job", json={"jobId": job["jobId"], "action": "result", "result": "pong"})
        r = await comp
        assert r.json()["choices"][0]["message"]["content"] == "pong"
        # without wait => immediate idle
        assert (await c.get("/job")).json() == {"action": "idle"}


async def test_tabs_autoconfig_prune_and_select(server):
    async with httpx.AsyncClient(base_url=server.url) as c:
        await c.post("/extension/tabs", json={"tabs": [{"id": 1, "provider": "qwen"}, {"id": 2, "provider": "qwen"}]})
        assert (await c.get("/extension/config")).json()["targetTabs"] == {"qwen": 1}
        r = await c.post("/extension/config", json={"provider": "qwen", "targetTabId": 2})
        assert r.json()["targetTabs"] == {"qwen": 2}
        r = await c.post("/extension/config", json={"provider": "qwen", "targetTabId": 99})
        assert r.status_code == 400
        r = await c.post("/extension/config", json={"provider": "nope", "targetTabId": 1})
        assert r.status_code == 400
        # tab 2 closed -> selection dropped and auto-config picks tab 1 again
        await c.post("/extension/tabs", json={"tabs": [{"id": 1, "provider": "qwen"}]})
        assert (await c.get("/extension/state")).json()["targetTabs"] == {"qwen": 1}
        # all closed -> nothing selected
        await c.post("/extension/tabs", json={"tabs": []})
        assert (await c.get("/extension/state")).json()["targetTabs"] == {}


async def test_heartbeat_busy_tolerance(server_factory):
    srv = server_factory(heartbeat_ttl=0.3, busy_heartbeat_ttl=5)
    async with httpx.AsyncClient(base_url=srv.url) as c:
        await c.post("/extension/tabs", json={"tabs": [], "busy": False})
        assert (await c.get("/health")).json()["extension"] is True
        await asyncio.sleep(0.5)
        assert (await c.get("/health")).json()["extension"] is False
        await c.post("/extension/tabs", json={"tabs": [], "busy": True})
        await asyncio.sleep(0.5)
        assert (await c.get("/health")).json()["extension"] is True  # busy: tolerated


async def test_late_result_is_ignored(server):
    async with httpx.AsyncClient(base_url=server.url) as c:
        r = await c.post("/job", json={"jobId": "unknown", "action": "result", "result": "x"})
        assert r.json() == {"ok": True}
        logs = (await c.get("/extension/state")).json()["logs"]
        assert "ignored" in logs[-1]["msg"]


async def test_abandoned_long_poll_does_not_steal_jobs(server):
    """An extension reload leaves an orphan GET /job?wait=… : it must not take jobs."""
    async with httpx.AsyncClient(base_url=server.url, timeout=10) as c:
        await c.post("/extension/tabs", json={"tabs": [{"id": 7, "provider": "qwen"}]})
        async with httpx.AsyncClient(base_url=server.url, timeout=10) as dead:
            orphan = asyncio.create_task(dead.get("/job", params={"wait": 10}))
            await asyncio.sleep(0.3)
            orphan.cancel()
            await asyncio.gather(orphan, return_exceptions=True)
        await asyncio.sleep(0.8)  # server notices the disconnect on its next check
        comp = asyncio.create_task(c.post("/v1/chat/completions", json={"model": "qwen", "messages": MSG, "stream": False}))
        await asyncio.sleep(0.2)
        job = (await c.get("/job", params={"wait": 2})).json()
        assert job["action"] == "ask", "job was stolen by the orphan poller"
        await c.post("/job", json={"jobId": job["jobId"], "action": "result", "result": "ok"})
        assert (await comp).status_code == 200


async def test_long_poll_counts_as_heartbeat(server_factory):
    srv = server_factory(heartbeat_ttl=0.3)
    async with httpx.AsyncClient(base_url=srv.url, timeout=10) as c:
        poll = asyncio.create_task(c.get("/job", params={"wait": 1.5}))
        await asyncio.sleep(0.8)
        assert (await c.get("/health")).json()["extension"] is True
        await poll
