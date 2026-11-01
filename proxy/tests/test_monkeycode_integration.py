"""MonkeyCode (chaitin/OhMyAgent) integration, over the Responses API.

The cloud agent always comes back with its WHOLE history and the content of the
files it touched. Typed as-is into the web chat, that is the same context
again on every turn; the integration keeps ONE discussion and types only what
is new — the same contract as Qwen Code, checked here on the exact wire format
MonkeyCode uses (``instructions`` + ``input`` + flat ``tools``).
"""
from __future__ import annotations

import json

import httpx
import pytest

from conftest import FakeExtension

pytestmark = pytest.mark.anyio

OHMYAGENT_PROMPT = "You are OhMyAgent, an AI coding agent for software engineering tasks."
TOOLS = [
    {
        "type": "function",
        "name": "read_file",
        "description": "Read a file",
        "parameters": {"type": "object", "properties": {"path": {"type": "string"}}, "required": ["path"]},
    }
]
TURN_1 = "TASK-1: read main.py and tell me what it prints."
TURN_2 = "TASK-2: now add a test for it."


async def set_integration(url: str, enabled: bool = True, **features):
    async with httpx.AsyncClient(base_url=url) as c:
        body = {"enabled": enabled}
        if features:
            body["features"] = features
        r = await c.post("/agent-integrations/monkey-code", json=body)
        r.raise_for_status()
        return r.json()


def responses_body(turn: str, history=None, stream: bool = False):
    items = history if history is not None else []
    return {
        "model": "deepseek-chat",
        "instructions": OHMYAGENT_PROMPT,
        "input": items + [{"type": "message", "role": "user", "content": [{"type": "input_text", "text": turn}]}],
        "tools": TOOLS,
        "stream": stream,
    }


def tool_dialect_responder(answer: str, tool: str = "read_file", args: str = '{"path": "main.py"}'):
    async def responder(job: dict):
        # the web model answers in the contract NemApi gave it
        return "result", answer + f'\n<tool>{{"name": "{tool}", "arguments": {args}}}</tool>'

    return responder


async def test_the_cloud_agent_is_recognised_by_its_system_prompt(server):
    async with FakeExtension(server.url):
        await set_integration(server.url)
        async with httpx.AsyncClient(base_url=server.url, timeout=30) as c:
            snap = (await c.get("/agent-integrations")).json()
            monkey = next(a for a in snap["agents"] if a["id"] == "monkey-code")
            assert monkey["enabled"] is True
            assert monkey["header"] == "monkey-code"
            r = await c.post("/v1/responses", json=responses_body(TURN_1))
            assert r.status_code == 200, r.text
            state = (await c.get("/agent-integrations")).json()
            detected = next(a for a in state["agents"] if a["id"] == "monkey-code")["detected"]
            assert detected["requests"] == 1
            # the marker of the system prompt is what recognised it here
            assert detected["reason"] == "prompt système"


async def test_the_context_of_the_cloud_agent_is_incremental(server):
    async with FakeExtension(server.url) as ext:
        await set_integration(server.url)
        async with httpx.AsyncClient(base_url=server.url, timeout=30) as c:
            first = await c.post("/v1/responses", json=responses_body(TURN_1))
            assert first.status_code == 200, first.text
            answer_1 = first.json()["output"][0]["content"][0]["text"]

            # turn 2: the agent resends everything, as it always does
            history = [
                {"type": "message", "role": "user", "content": [{"type": "input_text", "text": TURN_1}]},
                {"type": "message", "role": "assistant", "content": [{"type": "output_text", "text": answer_1}]},
            ]
            second = await c.post("/v1/responses", json=responses_body(TURN_2, history))
            assert second.status_code == 200, second.text

        jobs = [j for j in ext.received if j["provider"] == "deepseek"]
        assert len(jobs) == 2
        # never a new discussion, never the whole history twice
        assert all(job["freshChat"] is False for job in jobs)
        assert TURN_1 in jobs[0]["question"] and OHMYAGENT_PROMPT in jobs[0]["question"]
        assert len(jobs[0]["question"]) > 500  # system prompt + tools contract + task
        assert TURN_1 not in jobs[1]["question"]  # already in the discussion
        assert TURN_2 in jobs[1]["question"]
        assert len(jobs[1]["question"]) < len(jobs[0]["question"])


async def test_a_tool_call_returns_as_a_responses_function_call(server):
    responder = tool_dialect_responder("Je lis le fichier.")
    async with FakeExtension(server.url, responder=responder):
        await set_integration(server.url)
        async with httpx.AsyncClient(base_url=server.url, timeout=30) as c:
            r = await c.post("/v1/responses", json=responses_body(TURN_1))
            assert r.status_code == 200, r.text
            output = r.json()["output"]
        call = next(item for item in output if item["type"] == "function_call")
        assert call["name"] == "read_file"
        assert json.loads(call["arguments"]) == {"path": "main.py"}
        assert call["call_id"].startswith("call")
        # the call is not left in the text handed to the agent
        text = next(
            (item["content"][0]["text"] for item in output if item["type"] == "message"), ""
        )
        assert "<tool>" not in text


async def test_the_tool_call_arrives_in_the_stream_too(server):
    responder = tool_dialect_responder("Je lis.", tool="read_file")
    async with FakeExtension(server.url, responder=responder):
        await set_integration(server.url)
        async with httpx.AsyncClient(base_url=server.url, timeout=30) as c:
            async with c.stream("POST", "/v1/responses", json=responses_body(TURN_1, stream=True)) as r:
                assert r.status_code == 200
                names, items = [], []
                current = None
                async for line in r.aiter_lines():
                    if line.startswith("event: "):
                        current = line[7:].strip()
                        names.append(current)
                    elif line.startswith("data: "):
                        payload = json.loads(line[6:])
                        if current == "response.output_item.done":
                            items.append(payload["item"])
        assert names[0] == "response.created" and names[-1] == "response.completed"
        call = next(item for item in items if item["type"] == "function_call")
        assert call["name"] == "read_file"
        assert json.loads(call["arguments"])["path"] == "main.py"


async def test_a_disabled_integration_keeps_the_classic_behaviour(server):
    async with FakeExtension(server.url) as ext:
        # integration off: no incremental context, the whole prompt is typed
        async with httpx.AsyncClient(base_url=server.url, timeout=30) as c:
            r = await c.post("/v1/responses", json=responses_body(TURN_1))
            assert r.status_code == 200
        job = ext.received[0]
        assert OHMYAGENT_PROMPT in job["question"] and TURN_1 in job["question"]


async def test_the_official_openai_sdk_reads_our_responses_stream(server):
    """The real client parser, not ours: if it accepts the stream, MonkeyCode will."""
    from openai import AsyncOpenAI

    async with FakeExtension(server.url):
        await set_integration(server.url)
        client = AsyncOpenAI(base_url=server.url + "/v1", api_key="nemapi", max_retries=0, timeout=30)
        try:
            # non-stream: the health check of MonkeyCode
            resp = await client.responses.create(model="deepseek-chat", input="ping")
            assert resp.object == "response"
            assert resp.output[0].content[0].text.startswith("ECHO:")

            # stream: what the agent does during a task
            chunks, completed = [], None
            stream = await client.responses.create(model="deepseek-chat", input="ping", stream=True)
            async for event in stream:
                if event.type == "response.output_text.delta":
                    chunks.append(event.delta)
                elif event.type == "response.completed":
                    completed = event.response
            assert completed is not None
            assert "".join(chunks).startswith("ECHO:")
            assert completed.usage.total_tokens > 0
        finally:
            await client.close()


async def test_the_monkeycode_health_check_can_save_the_configuration(server):
    """The exact request of `backend/pkg/llm/check.go`, on a stock proxy.

    Go: `{"model", "messages":[…"hi"], "max_tokens": 1}`, headers Content-Type
    and Authorization only — no `stream`, no `Accept`. The body is then parsed
    with `json.Unmarshal`, so an SSE answer fails on the `d` of `data: `:
    "Invalid character 'd' looking for beginning of value", and the model can
    never be saved. The admin « Streaming » switch is ON by default, so this is
    the shipped-configuration case.
    """
    async with FakeExtension(server.url):
        async with httpx.AsyncClient(base_url=server.url, timeout=30) as c:
            # default settings: stream_enabled is true
            settings = (await c.get("/status")).json()
            assert settings["settings"]["stream_enabled"] is True

            for path in ("/v1/chat/completions", "/chat/completions"):
                r = await c.post(
                    path,
                    json={
                        "model": "deepseek-chat",
                        "messages": [{"role": "user", "content": "hi"}],
                        "max_tokens": 1,
                    },
                    headers={"Authorization": "Bearer nemapi", "Content-Type": "application/json"},
                )
                assert r.status_code == 200, (path, r.status_code, r.text[:200])
                # what Go's json.Unmarshal needs: a JSON document, no `data: `
                assert r.headers["content-type"].startswith("application/json"), (
                    path,
                    r.headers["content-type"],
                )
                assert r.content[:1] == b"{", r.content[:40]
                body = r.json()
                assert "error" not in body
                assert body["object"] == "chat.completion"

            # the Responses flavour of the same check
            r = await c.post(
                "/v1/responses",
                json={"model": "deepseek-chat", "input": [{"role": "user", "content": "hi"}]},
                headers={"Authorization": "Bearer nemapi", "Content-Type": "application/json"},
            )
            assert r.status_code == 200 and r.content[:1] == b"{"
            assert r.json()["object"] == "response"
