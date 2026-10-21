"""Incremental context with a Qwen Code-like agent and a DeepSeek-like web model.

Reproduces the real session that broke the single discussion: besides its main
conversation, Qwen Code sends memory-recall / permission-classifier structured
queries (forced respond_in_schema), input suggestions, a memory-extraction
sub-agent and real side tasks through the same endpoint, while DeepSeek answers
tool calls in <tool> or native DSML syntax.
"""
import json
import sys
from pathlib import Path

import httpx
import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "tools"))

from virtual_agent import (  # noqa: E402
    EXTRACTION_SYSTEM,
    SYSTEM_PROMPT,
    TASK_1,
    TASK_2,
    QwenCodeLikeAgent,
    ScriptedWebModel,
    VirtualCodingAgent,
    WebUIExtension,
    configure,
)

from nemapi.utils.history import BACK_TO_MAIN, SIDE_HEADER  # noqa: E402
from nemapi.utils.side_queries import classify_local, schema_default  # noqa: E402

DSML_STYLES = ScriptedWebModel.STYLES


@pytest.fixture(autouse=True)
async def _close_agents(anyio_backend):
    yield
    await VirtualCodingAgent.close_all()


async def settings(url: str, incremental_context: bool = True, side_queries_local=None):
    return await configure(url, incremental=incremental_context, side_queries_local=side_queries_local)


async def sessions(url: str):
    async with httpx.AsyncClient(base_url=url) as c:
        return (await c.get("/sessions")).json()


def discussion_text(ext: WebUIExtension, provider: str = "qwen") -> str:
    tab = ext.tabs[provider]
    assert len(tab.discussions) == 1, "a new discussion was opened"
    assert all(j["freshChat"] is False for j in ext.jobs)
    return "\n\n".join(tab.current)


def assert_main_conversation_clean(text: str, tool_contracts: int = 1, quoted_tasks: int = 0):
    assert text.count(SYSTEM_PROMPT) == 1  # never re-typed (no reset / full replay)
    assert text.count("Available tools:") == tool_contracts
    # each task typed once (+ quoted by the memory-recall side query when it is typed too)
    assert text.count(TASK_1) == 1 + quoted_tasks and text.count(TASK_2) == 1 + quoted_tasks
    assert text.count("Tool result (read_file): print('hello')") == 1  # tool results never re-typed
    assert "System instructions (update)" not in text
    assert "ERROR_" not in text


# ============================================================================ unit
def test_classify_local_known_side_queries():
    recall_tool = [{"type": "function", "function": {"name": "respond_in_schema", "parameters": {
        "type": "object", "properties": {"selected_memories": {"type": "array"}}, "required": ["selected_memories"]}}}]
    a = classify_local([{"role": "user", "content": "pick"}], recall_tool, {"tool_choice": "required"})
    assert a.kind == "schema" and json.loads(a.tool_calls[0]["function"]["arguments"]) == {"selected_memories": []}

    guard = [{"type": "function", "function": {"name": "respond_in_schema", "parameters": {
        "type": "object", "properties": {"shouldBlock": {"type": "boolean"}}}}}]
    a = classify_local([{"role": "user", "content": "x"}], guard)
    assert a.kind == "safety" and a.error  # fail-closed, never "allowed"

    a = classify_local([{"role": "system", "content": "S"}, {"role": "user", "content": "[SUGGESTION MODE: Suggest…]"}])
    assert a.kind == "suggestion" and a.text == ""
    a = classify_local([{"role": "system", "content": EXTRACTION_SYSTEM}, {"role": "user", "content": "go"}])
    assert a.kind == "memory" and a.text and not a.tool_calls

    # the main conversation is never answered locally
    tools = [{"type": "function", "function": {"name": "read_file", "parameters": {}}}]
    assert classify_local([{"role": "system", "content": SYSTEM_PROMPT}, {"role": "user", "content": TASK_1}], tools) is None
    assert classify_local([{"role": "user", "content": "hello"}]) is None


def test_schema_default_is_neutral():
    schema = {"type": "object", "required": ["a", "b", "c", "d", "e"], "properties": {
        "a": {"type": "string"}, "b": {"type": "integer"}, "c": {"type": "array"},
        "d": {"enum": ["user", "model"]}, "e": {"type": "object", "properties": {"x": {"type": "boolean"}}}}}
    assert schema_default(schema) == {"a": "", "b": 0, "c": [], "d": "user", "e": {"x": False}}


# ============================================================================ end to end
@pytest.mark.anyio
@pytest.mark.parametrize("stream", [True, False])
async def test_qwen_code_side_queries_never_touch_the_discussion(server, stream):
    await settings(server.url, incremental_context=True)
    model = ScriptedWebModel(styles=DSML_STYLES)  # DeepSeek: <tool> + every DSML flavour + stray tags
    async with WebUIExtension(server.url, model=model) as ext:
        agent = QwenCodeLikeAgent(server.url, stream=stream)
        a1 = await agent.ask(TASK_1)
        a2 = await agent.ask(TASK_2)

    # the agent received REAL tool calls for every syntax and finished both tasks
    assert a1.startswith("TASK-1 DONE") and "hello done" in a1, a1
    assert a2.startswith("TASK-2 DONE") and "1 passed" in a2, a2
    assert agent.fs.files["main.py"] == "print('hello')\nprint('done')"
    assert model.tool_calls_made == 6
    for m in agent.messages:
        assert "DSML" not in (m.get("content") or "") and "<tool>" not in (m.get("content") or "")

    # side queries: answered locally, instantly
    kinds = {}
    for kind, outcome in agent.side_results:
        kinds.setdefault(kind, []).append(outcome)
    assert all(json.loads(o["tool_calls"][0]["function"]["arguments"]) == {"selected_memories": []}
               for o in kinds["recall"])
    assert kinds["classifier"] == ["HTTP 400"] * 6  # fail-closed → the agent asks the user
    assert all(not o.get("content") and not o.get("tool_calls") for o in kinds["suggestion"])
    assert all(o.get("content") and not o.get("tool_calls") for o in kinds["memory"])

    # the web discussion only holds the main conversation, typed once
    text = discussion_text(ext)
    assert_main_conversation_clean(text)
    for marker in ("SUGGESTION MODE", "memory extraction", "respond_in_schema", "security classifier", SIDE_HEADER):
        assert marker not in text
    assert len(ext.jobs) == agent.requests == 8  # nothing but the 8 main requests reached the browser
    s = await sessions(server.url)
    assert s["requests"]["full"] == 1 and s["requests"]["delta"] == 7
    assert s["requests"]["local"] == len(agent.side_results) == 2 + 6 + 2 + 2


@pytest.mark.anyio
async def test_real_side_task_is_framed_and_main_conversation_resumes(server):
    """A side task that needs a real answer (web page summary) goes into the same
    discussion, framed, with its own state; the main conversation is untouched."""
    await settings(server.url, incremental_context=True)
    async with WebUIExtension(server.url) as ext:
        agent = QwenCodeLikeAgent(server.url, web_fetch_on="TASK-2")
        await agent.ask(TASK_1)
        a2 = await agent.ask(TASK_2)
    assert a2.startswith("TASK-2 DONE"), a2
    summary = [o for k, o in agent.side_results if k == "web_summary"][0]
    assert summary["content"].startswith("SIDE-SUMMARY of https://example.com/nemapi")

    text = discussion_text(ext)
    assert_main_conversation_clean(text)
    side = [j["question"] for j in ext.jobs if j["question"].startswith(SIDE_HEADER)]
    assert len(side) == 1 and "No tool is available for this side request" in side[0]
    assert "Instructions for this side request:" in side[0] and "summarize fetched web pages" in side[0]
    # the next main message tells the model that the main task resumes
    i = next(i for i, j in enumerate(ext.jobs) if j["question"].startswith(SIDE_HEADER))
    assert ext.jobs[i + 1]["question"].startswith(BACK_TO_MAIN) and TASK_2 in ext.jobs[i + 1]["question"]
    assert sum(BACK_TO_MAIN in j["question"] for j in ext.jobs) == 1
    s = await sessions(server.url)
    assert s["requests"]["side"] == 1 and s["requests"]["full"] == 1


@pytest.mark.anyio
async def test_side_queries_typed_in_discussion_when_local_mode_is_off(server):
    """Option off: every side query is typed (framed) — still ONE discussion, no
    reset, no full replay, and the main conversation keeps working."""
    await settings(server.url, incremental_context=True, side_queries_local=False)
    model = ScriptedWebModel(styles=("dsml_double", "tool_then_stray_dsml"))
    async with WebUIExtension(server.url, model=model) as ext:
        agent = QwenCodeLikeAgent(server.url)
        a1 = await agent.ask(TASK_1)
        a2 = await agent.ask(TASK_2)
    assert a1.startswith("TASK-1 DONE") and a2.startswith("TASK-2 DONE"), (a1, a2)
    text = discussion_text(ext)
    # + the 2 respond_in_schema definitions (recall / classifier), each typed once
    assert_main_conversation_clean(text, tool_contracts=3, quoted_tasks=1)
    assert text.count("Select the relevant memories for: " + TASK_1) == 1
    assert text.count('"selected_memories"') >= 1 and text.count("- respond_in_schema:") == 2
    assert text.count(EXTRACTION_SYSTEM.splitlines()[0]) == 1  # sub-agent prompt typed once, framed
    # structured side queries came back as real respond_in_schema tool calls
    recall = [o for k, o in agent.side_results if k == "recall"]
    assert all(o["tool_calls"][0]["function"]["name"] == "respond_in_schema" for o in recall)
    guard = [o for k, o in agent.side_results if k == "classifier"]
    assert all(json.loads(o["tool_calls"][0]["function"]["arguments"]) == {"shouldBlock": False} for o in guard)
    s = await sessions(server.url)
    assert s["requests"]["full"] == 1 and s["requests"]["local"] == 0
    assert s["requests"]["side"] == len(agent.side_results)
    lanes = s["sessions"]["qwen"]["side_lanes"]
    assert len(lanes) == 4  # recall, classifier, suggestion, memory sub-agent


@pytest.mark.anyio
async def test_new_coding_session_still_detected_with_side_queries(server):
    """A structured side query before the main request must not be taken for a
    new coding session, a real new session still is."""
    await settings(server.url, incremental_context=True)
    async with WebUIExtension(server.url) as ext:
        agent = QwenCodeLikeAgent(server.url)
        await agent.ask(TASK_1)
        agent2 = QwenCodeLikeAgent(server.url)  # user restarts Qwen Code
        a = await agent2.ask(TASK_2)
    assert a.startswith("TASK-2 DONE")
    full = [j for j in ext.jobs if SYSTEM_PROMPT in j["question"]]
    assert len(full) == 2 and all("Available tools:" in j["question"] for j in full)
    assert len(ext.tabs["qwen"].discussions) == 1  # still never a new chat from the proxy
