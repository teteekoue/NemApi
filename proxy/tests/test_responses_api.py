"""The Responses API and the paths MonkeyCode actually calls.

MonkeyCode posts the path to the base URL the user typed:

    base URL https://x/v1  →  https://x/v1/chat/completions   (NemApi: OK)
    base URL https://x     →  https://x/chat/completions      (before: 404)
    « OpenAI Responses »   →  …/responses                     (before: 404)

Both cases were a bare 404 with no explanation, which the cloud agent reported
as an unsupported format. These tests pin the surfaces that fix it.
"""
from __future__ import annotations

import json

import httpx
import pytest

from nemapi.utils import responses_api

from conftest import FakeExtension

pytestmark = pytest.mark.anyio


async def _read_sse(response) -> list:
    """All SSE events of a streaming answer, as (event name, payload)."""
    events = []
    name = None
    async for line in response.aiter_lines():
        if line.startswith("event: "):
            name = line[7:].strip()
        elif line.startswith("data: "):
            payload = json.loads(line[6:])
            events.append((name or payload.get("type"), payload))
            name = None
    return events


# --------------------------------------------------------------- conversions
def test_a_responses_body_becomes_the_chat_body_the_pipeline_takes():
    chat = responses_api.to_chat_body(
        {
            "model": "deepseek-chat",
            "instructions": "You are OhMyAgent, an AI coding agent.",
            "input": [
                {"type": "message", "role": "developer", "content": [{"type": "input_text", "text": "runtime"}]},
                {"type": "message", "role": "user", "content": [{"type": "input_text", "text": "fix the bug"}]},
            ],
            "tools": [
                {
                    "type": "function",
                    "name": "read_file",
                    "description": "read a file",
                    "parameters": {"type": "object", "properties": {"path": {"type": "string"}}},
                }
            ],
            "stream": True,
        }
    )
    assert chat["model"] == "deepseek-chat"
    assert chat["stream"] is True
    # instructions first, then the input items, the developer role folded into system
    assert [m["role"] for m in chat["messages"]] == ["system", "system", "user"]
    assert chat["messages"][0]["content"].startswith("You are OhMyAgent")
    assert chat["messages"][-1]["content"] == "fix the bug"
    assert chat["tools"][0]["function"]["name"] == "read_file"


def test_a_plain_string_input_and_a_tool_result_are_understood():
    chat = responses_api.to_chat_body({"model": "deepseek-chat", "input": "just a string"})
    assert chat["messages"] == [{"role": "user", "content": "just a string"}]

    chat = responses_api.to_chat_body(
        {
            "model": "deepseek-chat",
            "input": [
                {"role": "user", "content": "go"},
                {"type": "function_call", "call_id": "c1", "name": "ls", "arguments": "{\"path\":\".\"}"},
                {"type": "function_call_output", "call_id": "c1", "output": "a.txt"},
            ],
        }
    )
    assistant, tool = chat["messages"][1], chat["messages"][2]
    assert assistant["tool_calls"][0]["function"]["name"] == "ls"
    assert tool == {"role": "tool", "tool_call_id": "c1", "content": "a.txt"}


def test_non_function_tools_are_dropped_not_forwarded():
    chat = responses_api.to_chat_body(
        {"model": "deepseek-chat", "input": "hi", "tools": [{"type": "web_search"}]}
    )
    assert "tools" not in chat


def test_the_stream_is_a_well_formed_responses_sequence():
    events = list(
        responses_api.response_events("resp_1", "deepseek-chat", "hello world", None, 7, 2)
    )
    names = [line.split("\n", 1)[0][len("event: "):] for line in events]
    assert names[0] == "response.created"
    assert "response.output_text.delta" in names
    assert names[-1] == "response.completed"
    final = json.loads(events[-1].split("data: ", 1)[1])
    assert final["response"]["status"] == "completed"
    assert final["response"]["output"][0]["content"][0]["text"] == "hello world"
    assert final["response"]["usage"]["input_tokens"] == 7
    # each data line carries its own type, like the real API
    for line in events:
        payload = json.loads(line.split("data: ", 1)[1])
        assert payload["type"] in names


def test_a_tool_call_is_an_output_item_of_its_own():
    call = {"id": "call_1", "type": "function", "function": {"name": "ls", "arguments": "{\"p\":\".\"}"}}
    events = list(responses_api.response_events("resp_2", "m", "", [call], 1, 1))
    done = [json.loads(e.split("data: ", 1)[1]) for e in events if e.startswith("event: response.output_item.done")]
    item = done[-1]["item"]
    assert item["type"] == "function_call"
    assert item["name"] == "ls"
    assert item["call_id"] == "call_1"
    assert json.loads(item["arguments"]) == {"p": "."}


# ------------------------------------------------------------- over the wire
async def test_the_responses_endpoint_answers_with_or_without_v1(server):
    async with FakeExtension(server.url):
        async with httpx.AsyncClient(base_url=server.url, timeout=30) as client:
            for path in ("/v1/responses", "/responses"):
                r = await client.post(
                    path,
                    json={"model": "deepseek-chat", "input": [{"role": "user", "content": "ping"}]},
                )
                assert r.status_code == 200, (path, r.text)
                body = r.json()
                assert body["object"] == "response"
                assert body["status"] == "completed"
                assert body["output"][0]["content"][0]["text"].startswith("ECHO:")
                assert body["usage"]["total_tokens"] > 0


async def test_the_responses_stream_ends_on_response_completed(server):
    async with FakeExtension(server.url):
        async with httpx.AsyncClient(base_url=server.url, timeout=30) as client:
            async with client.stream(
                "POST",
                "/v1/responses",
                json={"model": "deepseek-chat", "input": "ping", "stream": True},
            ) as r:
                assert r.status_code == 200
                assert "text/event-stream" in r.headers["content-type"]
                events = await _read_sse(r)
    names = [n for n, _ in events]
    assert names[0] == "response.created"
    assert names[-1] == "response.completed"
    text = "".join(
        p["delta"] for n, p in events if n == "response.output_text.delta"
    )
    assert text.startswith("ECHO:")


async def test_the_responses_endpoint_rejects_an_empty_body(server):
    async with FakeExtension(server.url):
        async with httpx.AsyncClient(base_url=server.url, timeout=30) as client:
            r = await client.post("/v1/responses", json={"model": "deepseek-chat"})
            assert r.status_code == 400
            assert r.json()["error"]["type"] == "invalid_request_error"


async def test_the_chat_and_models_paths_work_without_the_v1_prefix(server):
    """What MonkeyCode calls when the base URL has no /v1."""
    async with FakeExtension(server.url):
        async with httpx.AsyncClient(base_url=server.url, timeout=30) as client:
            r = await client.get("/models")
            assert r.status_code == 200, (r.status_code, r.text[:200])
            assert {m["id"] for m in r.json()["data"]} >= {"deepseek-chat", "qwen-chat"}
            r = await client.post(
                "/chat/completions",
                json={
                    "model": "deepseek-chat",
                    "messages": [{"role": "user", "content": "ping"}],
                    "stream": False,
                },
            )
            assert r.status_code == 200
            assert r.json()["choices"][0]["message"]["content"].startswith("ECHO:")


async def test_a_big_five_client_asking_for_responses_receives_one(server):
    """MonkeyCode's health check: model + input, no stream (pkg/llm/check.go)."""
    async with FakeExtension(server.url):
        async with httpx.AsyncClient(base_url=server.url, timeout=30) as client:
            r = await client.post(
                "/responses",
                json={"model": "deepseek-chat", "input": [{"role": "user", "content": "hi"}]},
                headers={"Authorization": "Bearer whatever", "X-Api-Key": "whatever"},
            )
            assert r.status_code == 200
            assert r.json()["output"][0]["type"] == "message"


async def test_a_slow_answer_streams_heartbeats_until_the_end(server, monkeypatch):
    """Cloudflare cuts a silent origin after 100 s (524): data must flow.

    The answer takes longer than the keep-alive threshold: the client must get
    the SSE headers at once, a comment while it waits, then the real events.
    """
    import asyncio

    from nemapi.routers import openai as openai_router

    async def slow(job: dict):
        await asyncio.sleep(1.2)
        return "result", "lent mais complet"

    monkeypatch.setattr(openai_router, "KEEPALIVE_AFTER", 0.2)
    monkeypatch.setattr(openai_router, "KEEPALIVE_EVERY", 0.3)

    async with FakeExtension(server.url, responder=slow):
        async with httpx.AsyncClient(base_url=server.url, timeout=30) as client:
            async with client.stream(
                "POST", "/v1/responses", json={"model": "deepseek-chat", "input": "ping", "stream": True}
            ) as r:
                assert r.status_code == 200
                raw = [line async for line in r.aiter_lines()]
    heartbeats = [line for line in raw if line.startswith(": keep-alive")]
    assert heartbeats, "no heartbeat: a 100 s tunnel would cut the connection"
    text = "".join(
        json.loads(line[6:])["delta"]
        for line in raw
        if line.startswith("data: ") and json.loads(line[6:]).get("type") == "response.output_text.delta"
    )
    assert text == "lent mais complet"
    assert any(line.startswith("event: response.completed") for line in raw)
