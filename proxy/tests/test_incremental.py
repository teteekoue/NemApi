"""Incremental context: ONE web discussion, never a new chat, duplicates filtered.

A virtual coding agent (official openai SDK, tools on a virtual FS) talks to the
proxy; a simulated provider web UI keeps real discussions so we can check what
the web model actually sees.
"""
import asyncio
import sys
from pathlib import Path

import httpx
import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "tools"))

from virtual_agent import (  # noqa: E402
    SYSTEM_PROMPT,
    TASK_1,
    TASK_2,
    TOOLS,
    ScriptedWebModel,
    VirtualCodingAgent,
    WebUIExtension,
    QWEN_CODE_UA,
    configure,
)

from nemapi.utils.history import NUDGE, History, format_delta_prompt, message_fingerprint, select_new  # noqa: E402


@pytest.fixture(autouse=True)
async def _close_agents(anyio_backend):
    yield
    await VirtualCodingAgent.close_all()


async def set_incremental(url: str, on: bool, **extra):
    """on: Qwen Code integration with incremental context; off: classic NemApi."""
    agent = await configure(url, incremental=on, **extra)
    assert agent["enabled"] is on


async def integration(url: str) -> dict:
    async with httpx.AsyncClient(base_url=url) as c:
        return next(a for a in (await c.get("/agent-integrations")).json()["agents"] if a["id"] == "qwen-code")


async def sessions(url: str):
    async with httpx.AsyncClient(base_url=url) as c:
        return (await c.get("/sessions")).json()


async def run_two_tasks(agent: VirtualCodingAgent):
    a1 = await agent.ask(TASK_1)
    a2 = await agent.ask(TASK_2)
    return a1, a2


def assert_single_clean_discussion(ext: WebUIExtension, provider: str = "qwen"):
    """Never a new chat, system prompt / tools / tasks / tool results present once."""
    assert all(j["freshChat"] is False for j in ext.jobs if j["provider"] == provider)
    tab = ext.tabs[provider]
    assert len(tab.discussions) == 1
    text = "\n\n".join(tab.current)
    assert text.count(SYSTEM_PROMPT) == 1
    assert text.count("Available tools:") == 1
    assert text.count(TASK_1) <= 1 and text.count(TASK_2) <= 1
    assert text.count("Tool result (read_file): print('hello')") <= 1
    assert "ERROR_" not in text
    return text


# ============================================================================ unit
def _msgs():
    return [
        {"role": "system", "content": "SYS"},
        {"role": "user", "content": "do it"},
        {"role": "assistant", "content": None, "tool_calls": [{"id": "c1", "function": {"name": "run", "arguments": "{}"}}]},
        {"role": "tool", "tool_call_id": "c1", "content": "1 passed"},
    ]


def test_fingerprint_tolerates_client_rewrites():
    ours = {"role": "assistant", "content": None, "tool_calls": [
        {"id": "call_abc", "type": "function", "function": {"name": "read_file", "arguments": '{"path": "a.py", "n": 1}'}}]}
    echoed = {"role": "assistant", "content": "Je lis le fichier.", "tool_calls": [
        {"id": "call_zzz", "type": "function", "function": {"name": "read_file", "arguments": '{"n":1,"path":"a.py"}'}}]}
    assert message_fingerprint(ours) == message_fingerprint(echoed)
    assert message_fingerprint({"role": "user", "content": "hi  there\n"}) == message_fingerprint(
        {"role": "user", "content": [{"type": "text", "text": "hi there"}]})
    assert message_fingerprint({"role": "user", "content": "a"}) != message_fingerprint({"role": "user", "content": "b"})
    assert message_fingerprint({"role": "assistant", "content": "x"}) != message_fingerprint({"role": "user", "content": "x"})


def test_select_new_normal_turn_and_repeated_messages():
    m = _msgs()
    h = History.from_messages(m)
    view = h.chain[:2]  # [user, assistant] already in the discussion
    assert select_new(h, view, view, [h.chain[1]]) == [2]
    # an identical tool output in a LATER turn is new content and must be sent
    m2 = m + [
        {"role": "assistant", "content": None, "tool_calls": [{"id": "c2", "function": {"name": "run", "arguments": '{"x":1}'}}]},
        {"role": "tool", "tool_call_id": "c2", "content": "1 passed"},
    ]
    h2 = History.from_messages(m2)
    assert select_new(h2, h2.chain[:4], h2.chain[:4], [h2.chain[1], h2.chain[3]]) == [4]
    # same for a user repeating "continue"
    m3 = m + [{"role": "assistant", "content": "ok"}, {"role": "user", "content": "do it"}]
    h3 = History.from_messages(m3)
    assert select_new(h3, h3.chain[:4], h3.chain[:4], [h3.chain[1], h3.chain[3]]) == [4]


def test_select_new_filters_duplicates_when_history_is_rewritten():
    m = _msgs() + [{"role": "assistant", "content": "done"}, {"role": "user", "content": "next"}]
    h = History.from_messages(m)
    sent = list(h.chain)
    answers = [h.chain[1], h.chain[3]]
    # exact retry of an answered turn: nothing new
    retry = History.from_messages(m[:-2] + [{"role": "user", "content": "ZZZ"}])
    assert select_new(History.from_messages(m), sent, sent, answers) == []
    # agent dropped our last answer and retries with a new message → only the new message
    assert select_new(retry, sent, sent, answers) == [3]
    # our answer echoed back modified (truncated text) + a new tool result
    m_mod = m[:3] + [{"role": "tool", "tool_call_id": "c1", "content": "1 passed"},
                     {"role": "assistant", "content": "do"}, {"role": "user", "content": "next"},
                     {"role": "user", "content": "and more"}]
    hm = History.from_messages(m_mod)
    assert select_new(hm, sent, sent, answers) == [5]
    # compaction: summary + kept recent messages → only what was never typed
    comp = [m[0], {"role": "user", "content": "SUMMARY"}, {"role": "assistant", "content": "Understood."}, m[5]]
    hc = History.from_messages(comp)
    assert select_new(hc, sent, sent, answers) == [0]


def test_delta_format():
    m = _msgs()
    h = History.from_messages(m)
    delta = format_delta_prompt(h, [2], tools=TOOLS)
    assert delta.startswith("Tool result (run): 1 passed")
    assert "SYS" not in delta and "do it" not in delta
    assert "Do NOT repeat successful tool calls" in delta and "Reminder:" in delta
    h2 = History.from_messages(m + [{"role": "assistant", "content": "ok"}, {"role": "user", "content": "next"}])
    assert format_delta_prompt(h2, [4]) == "next"  # a lone user message is typed verbatim
    upd = format_delta_prompt(h2, [4], new_system_lines=["Date: today"], new_tools=[TOOLS[0]])
    assert upd.startswith("System instructions (update):\nDate: today") and "list_files" in upd and "read_file" not in upd


# ============================================================================= e2e
@pytest.mark.anyio
@pytest.mark.parametrize("stream", [True, False])
async def test_agent_one_discussion_only_new_messages(server, stream):
    await set_incremental(server.url, True)
    async with WebUIExtension(server.url) as ext:
        agent = VirtualCodingAgent(server.url, stream=stream)
        a1, a2 = await run_two_tasks(agent)

    assert a1.startswith("TASK-1 DONE") and "hello done" in a1, a1
    assert a2.startswith("TASK-2 DONE") and "1 passed" in a2, a2
    assert agent.fs.files["main.py"] == "print('hello')\nprint('done')"

    jobs = ext.jobs
    assert len(jobs) == agent.requests == 8
    assert SYSTEM_PROMPT in jobs[0]["question"] and "Available tools:" in jobs[0]["question"]
    for j in jobs[1:]:
        assert SYSTEM_PROMPT not in j["question"] and "Available tools:" not in j["question"]
        assert TASK_1 not in j["question"]
    assert jobs[5]["question"].startswith(TASK_2)
    text = assert_single_clean_discussion(ext)
    assert text.count(TASK_1) == 1 and text.count(TASK_2) == 1

    s = await sessions(server.url)
    assert s["requests"] == {"full": 1, "delta": 7, "cached": 0, "nudge": 0, "side": 0, "local": 0}
    assert s["sessions"]["qwen"]["turns"] == 8 and s["chars_saved"] > 0


@pytest.mark.anyio
async def test_compare_typed_chars_legacy_vs_incremental(server_factory, tmp_path):
    report = {}
    for mode in ("legacy_fresh", "legacy_same_chat", "incremental"):
        srv = server_factory(data_dir=tmp_path / mode)
        if mode == "incremental":
            await set_incremental(srv.url, True)
        else:
            await set_incremental(srv.url, False, fresh_chat=(mode == "legacy_fresh"))
        async with WebUIExtension(srv.url) as ext:
            agent = VirtualCodingAgent(srv.url)
            a1, a2 = await run_two_tasks(agent)
        assert a1.startswith("TASK-1 DONE") and a2.startswith("TASK-2 DONE"), (mode, a1, a2)
        tab = ext.tabs["qwen"]
        report[mode] = {
            "requests": len(ext.jobs), "typed": ext.chars_typed(), "discussions": len(tab.discussions),
            "context": sum(len(x) for x in tab.current), "sys": "\n".join(tab.current).count(SYSTEM_PROMPT),
        }
        srv.stop()
    print("\nmode                 req  typed_chars  discussions  web_context  system_prompt_copies")
    for mode, r in report.items():
        print(f"{mode:<20} {r['requests']:>3}  {r['typed']:>11}  {r['discussions']:>11}  {r['context']:>11}  {r['sys']:>20}")
    inc, same, fresh = report["incremental"], report["legacy_same_chat"], report["legacy_fresh"]
    assert same["sys"] == same["requests"]  # the original problem: context doubled every turn
    assert inc["discussions"] == 1 and inc["sys"] == 1
    assert inc["typed"] < fresh["typed"] / 3 and inc["context"] < same["context"] / 3


@pytest.mark.anyio
async def test_disabled_by_default_keeps_legacy_behaviour(server):
    async with httpx.AsyncClient(base_url=server.url) as c:
        assert "incremental_context" not in (await c.get("/settings")).json()
    assert (await integration(server.url))["enabled"] is False  # every integration is off by default
    async with WebUIExtension(server.url) as ext:
        agent = VirtualCodingAgent(server.url)
        await agent.ask(TASK_1)
    assert all(j["freshChat"] is True for j in ext.jobs)
    assert all(SYSTEM_PROMPT in j["question"] for j in ext.jobs)


@pytest.mark.anyio
async def test_never_new_chat_even_if_fresh_chat_requested(server):
    """Global fresh_chat=true and an explicit fresh_chat=true are ignored in incremental mode."""
    await set_incremental(server.url, True, fresh_chat=True)
    async with WebUIExtension(server.url) as ext:
        async with httpx.AsyncClient(base_url=server.url, headers={"User-Agent": QWEN_CODE_UA}, timeout=10) as c:
            msgs = [{"role": "system", "content": SYSTEM_PROMPT}, {"role": "user", "content": "TASK-1 a"}]
            r = (await c.post("/v1/chat/completions", json={"model": "qwen", "messages": msgs, "stream": False,
                                                            "fresh_chat": True})).json()
            msgs += [r["choices"][0]["message"], {"role": "user", "content": "TASK-1 b"}]
            await c.post("/v1/chat/completions", json={"model": "qwen", "messages": msgs, "stream": False, "fresh_chat": True})
    assert [j["freshChat"] for j in ext.jobs] == [False, False]
    assert ext.jobs[1]["question"] == "TASK-1 b"
    assert len(ext.tabs["qwen"].discussions) == 1


@pytest.mark.anyio
async def test_compaction_stays_in_same_discussion(server):
    await set_incremental(server.url, True)
    async with WebUIExtension(server.url) as ext:
        agent = VirtualCodingAgent(server.url)
        await agent.ask(TASK_1)
        agent.compact()  # history rewritten: summary + fake assistant ack
        a2 = await agent.ask(TASK_2)
    assert a2.startswith("TASK-2 DONE")
    after = ext.jobs[5]["question"]
    assert "Summary of previous work" in after and TASK_2 in after
    assert SYSTEM_PROMPT not in after and "Understood." not in after  # nothing re-typed
    assert_single_clean_discussion(ext)


@pytest.mark.anyio
async def test_error_before_typing_resends_the_delta_once(server):
    """Composer not found → nothing typed → the SDK retry types the same delta, once."""
    await set_incremental(server.url, True)
    async with WebUIExtension(server.url, model=ScriptedWebModel(fail_on_call=3)) as ext:
        agent = VirtualCodingAgent(server.url, sdk_retries=2)
        a1 = await agent.ask(TASK_1)
    assert a1.startswith("TASK-1 DONE")
    failed, retry = ext.jobs[2], ext.jobs[3]
    assert retry["question"] == failed["question"] and retry["freshChat"] is False
    text = assert_single_clean_discussion(ext)
    assert text.count("Tool result (read_file): print('hello')") == 1


@pytest.mark.anyio
async def test_error_after_typing_is_not_typed_twice(server):
    """Prompt submitted but answer lost → the retry only asks for the answer."""
    await set_incremental(server.url, True)
    async with WebUIExtension(server.url, model=ScriptedWebModel(fail_on_call=3, fail_after_typing=True)) as ext:
        agent = VirtualCodingAgent(server.url, sdk_retries=2)
        a1 = await agent.ask(TASK_1)
    assert a1.startswith("TASK-1 DONE")
    assert ext.jobs[3]["question"] == NUDGE
    text = assert_single_clean_discussion(ext)
    assert text.count("Tool result (read_file): print('hello')") == 1
    s = await sessions(server.url)
    assert s["requests"]["nudge"] == 1


@pytest.mark.anyio
async def test_retry_while_generating_and_after_answer(server):
    """Agent HTTP timeout shorter than the web generation: the retry waits for the
    same job; a later duplicate retry is answered without typing anything."""
    await set_incremental(server.url, True)
    async with WebUIExtension(server.url, latency=0.05) as ext:
        async with httpx.AsyncClient(base_url=server.url, headers={"User-Agent": QWEN_CODE_UA}, timeout=10) as c:
            msgs = [{"role": "system", "content": SYSTEM_PROMPT}, {"role": "user", "content": "TASK-1 premier tour"}]
            body = {"model": "qwen", "messages": msgs, "stream": False}
            r1 = (await c.post("/v1/chat/completions", json=body)).json()
            msgs += [r1["choices"][0]["message"], {"role": "user", "content": "TASK-2 second tour"}]
            ext.latency = 1.5
            with pytest.raises(httpx.ReadTimeout):
                await c.post("/v1/chat/completions", json={**body, "messages": msgs}, timeout=0.4)
            await asyncio.sleep(0.5)
            r2 = await c.post("/v1/chat/completions", json={**body, "messages": msgs})
            r3 = await c.post("/v1/chat/completions", json={**body, "messages": msgs})  # duplicate
    assert r2.status_code == r3.status_code == 200
    assert r3.json()["choices"][0]["message"]["content"] == r2.json()["choices"][0]["message"]["content"]
    assert len(ext.jobs) == 2  # turn 2 typed exactly once, the duplicate typed nothing
    assert ext.tabs["qwen"].current.count("TASK-2 second tour") == 1
    s = await sessions(server.url)
    assert s["requests"]["cached"] == 1 and len(ext.tabs["qwen"].discussions) == 1


@pytest.mark.anyio
async def test_proxy_timeout_then_late_answer_is_reused(server_factory):
    srv = server_factory(job_timeout=0.8)
    await set_incremental(srv.url, True)
    async with WebUIExtension(srv.url, latency=0.01) as ext:
        async with httpx.AsyncClient(base_url=srv.url, headers={"User-Agent": QWEN_CODE_UA}, timeout=10) as c:
            msgs = [{"role": "system", "content": SYSTEM_PROMPT}, {"role": "user", "content": "TASK-1 x"}]
            body = {"model": "qwen", "messages": msgs, "stream": False}
            await c.post("/v1/chat/completions", json=body)
            msgs += [{"role": "assistant", "content": "ERROR_NO_TOOLS_IN_CONTEXT"}, {"role": "user", "content": "TASK-1 y"}]
            ext.latency = 1.5
            r = await c.post("/v1/chat/completions", json={**body, "messages": msgs})
            assert r.status_code == 504
            await asyncio.sleep(1.2)  # the web UI finishes, the late answer reaches the proxy
            r2 = await c.post("/v1/chat/completions", json={**body, "messages": msgs})
    assert r2.status_code == 200 and r2.json()["choices"][0]["message"]["content"]
    assert len(ext.jobs) == 2  # retry answered from the late result, nothing typed again
    assert ext.tabs["qwen"].current.count("TASK-1 y") == 1


@pytest.mark.anyio
async def test_system_prompt_and_tools_updates_send_only_the_difference(server):
    await set_incremental(server.url, True)
    async with WebUIExtension(server.url) as ext:
        agent = VirtualCodingAgent(server.url)
        await agent.ask(TASK_1)
        agent.messages[0]["content"] += "\nCurrent date: 2026-10-18"
        agent.tools.append({"type": "function", "function": {
            "name": "search", "description": "Search the code.",
            "parameters": {"type": "object", "properties": {"q": {"type": "string"}}}}})
        a2 = await agent.ask(TASK_2)
    assert a2.startswith("TASK-2 DONE")
    q = ext.jobs[5]["question"]
    assert q.startswith("System instructions (update):\nCurrent date: 2026-10-18")
    assert "- search:" in q and "- read_file:" not in q and SYSTEM_PROMPT not in q
    assert all("System instructions" not in j["question"] for j in ext.jobs[6:])  # sent once
    assert len(ext.tabs["qwen"].discussions) == 1


@pytest.mark.anyio
async def test_session_survives_proxy_restart(server_factory, tmp_path):
    data = tmp_path / "persist"
    srv = server_factory(data_dir=data)
    await set_incremental(srv.url, True)
    async with WebUIExtension(srv.url) as ext:
        agent = VirtualCodingAgent(srv.url)
        await agent.ask(TASK_1)
    srv.stop()
    srv2 = server_factory(data_dir=data)
    ext.base_url = srv2.url
    async with ext:
        agent.client = agent.client.with_options(base_url=srv2.url + "/v1")
        a2 = await agent.ask(TASK_2)
    assert a2.startswith("TASK-2 DONE")
    assert ext.jobs[5]["question"].startswith(TASK_2)  # not the whole history again
    assert_single_clean_discussion(ext)


@pytest.mark.anyio
async def test_stop_and_tab_change_never_open_a_new_chat(server):
    await set_incremental(server.url, True)
    async with WebUIExtension(server.url) as ext:
        agent = VirtualCodingAgent(server.url)
        await agent.ask(TASK_1)
        ext.tabs["qwen"].tab_id = 999  # the user re-selects another tab of the same discussion
        async with httpx.AsyncClient(base_url=server.url) as c:
            await c.post("/stop", json={})
            await ext._report(c)
            r = await c.post("/extension/config", json={"provider": "qwen", "targetTabId": 999})
            assert r.status_code == 200, r.text
        a2 = await agent.ask(TASK_2)
    assert a2.startswith("TASK-2 DONE")
    assert ext.jobs[5]["question"].startswith(TASK_2)
    assert_single_clean_discussion(ext)


@pytest.mark.anyio
async def test_two_providers_in_parallel(server):
    await set_incremental(server.url, True)
    async with WebUIExtension(server.url) as ext:
        a = VirtualCodingAgent(server.url, model="qwen-plus")
        b = VirtualCodingAgent(server.url, model="deepseek-chat")
        (_, a2), (_, b2) = await asyncio.gather(run_two_tasks(a), run_two_tasks(b))
    assert a2.startswith("TASK-2 DONE") and b2.startswith("TASK-2 DONE")
    for p in ("qwen", "deepseek"):
        assert_single_clean_discussion(ext, p)


@pytest.mark.anyio
async def test_reset_and_toggle_persist(server_factory, tmp_path):
    data = tmp_path / "toggle"
    srv = server_factory(data_dir=data)
    await set_incremental(srv.url, True)
    async with httpx.AsyncClient(base_url=srv.url) as c:
        assert (await c.post("/sessions/reset")).json()["ok"] is True
    srv.stop()
    srv2 = server_factory(data_dir=data)
    async with httpx.AsyncClient(base_url=srv2.url) as c:
        assert 'id="cfg-incremental"' not in (await c.get("/config")).text  # moved to the integrations page
        assert (await c.get("/integrations")).status_code == 200
    agent = await integration(srv2.url)
    assert agent["enabled"] is True
    assert {f["id"]: f["enabled"] for f in agent["features"]}["incremental_context"] is True


def test_select_new_with_identical_answers():
    """Two identical answers in a row must not confuse the duplicate filter."""
    m = [{"role": "user", "content": "a"}, {"role": "assistant", "content": "Done."},
         {"role": "user", "content": "b"}]
    h = History.from_messages(m)
    done = h.chain[1]
    sent = list(h.chain) + [done]  # the web model answered "Done." again
    assert select_new(h, sent, sent, [done]) == []  # retry of the answered turn: nothing new
    h2 = History.from_messages(m + [{"role": "assistant", "content": "Done."}, {"role": "user", "content": "c"}])
    assert select_new(h2, sent, sent, [done]) == [4]


@pytest.mark.anyio
async def test_user_workflow_new_coding_session_in_new_discussion(server):
    """Exactly the intended workflow:
    1. the user opens a new discussion himself and connects his agent;
    2. request 1 = system prompt + tools + user message, typed in that discussion;
    3. NemApi answers with a tool call; the agent runs it and resends the WHOLE history;
    4. request 2 = only the difference (new history − (request 1 + NemApi's answer)),
       i.e. the tool result, in the same discussion.
    Then a new coding session: the user opens another discussion, a new agent starts →
    everything is sent again (system prompt, tools, message), never a chat opened by NemApi."""
    await set_incremental(server.url, True)
    async with WebUIExtension(server.url) as ext:
        tab = ext.tabs["qwen"]
        tab.discussions.append([])  # user clicks "new chat" in the browser
        agent = VirtualCodingAgent(server.url, stream=False)
        msg1 = await agent._complete_for(TASK_1)
        assert msg1["tool_calls"][0]["function"]["name"] == "list_files"
        q1 = ext.jobs[0]["question"]
        assert SYSTEM_PROMPT in q1 and "Available tools:" in q1 and TASK_1 in q1
        # the agent runs the tool and resends the whole history
        agent.messages.append({"role": "tool", "tool_call_id": msg1["tool_calls"][0]["id"],
                               "content": agent.fs.run("list_files", {})})
        msg2 = await agent._complete()
        q2 = ext.jobs[1]["question"]
        assert q2.startswith("Tool result (list_files): README.md, main.py")
        assert SYSTEM_PROMPT not in q2 and TASK_1 not in q2 and "list_files\", \"arguments\"" not in q2
        assert msg2["tool_calls"][0]["function"]["name"] == "read_file"
        assert len(tab.discussions) == 1 and all(j["freshChat"] is False for j in ext.jobs)

        # --- new coding session, same task text, much later -------------------
        server.ctx.sessions.retry_window = 0
        tab.discussions.append([])  # the user opens a new discussion himself
        agent2 = VirtualCodingAgent(server.url, stream=False)
        await agent2.ask(TASK_1)
    first_new = ext.jobs[2]["question"]
    assert SYSTEM_PROMPT in first_new and "Available tools:" in first_new and TASK_1 in first_new
    assert all(SYSTEM_PROMPT not in j["question"] for j in ext.jobs[3:])
    assert len(tab.discussions) == 2 and all(j["freshChat"] is False for j in ext.jobs)
    last = "\n\n".join(tab.current)
    assert last.count(SYSTEM_PROMPT) == 1 and "ERROR_" not in last


@pytest.mark.parametrize("content", [
    "{garbage", "[]", '{"sessions": 3}', '{"sessions": {"qwen": {"view": "abc"}}}',
    '{"sessions": {"qwen": {"provider": "qwen", "sent": [1, 2]}}}', '{"sessions": {"qwen": 5}}',
])
def test_corrupted_sessions_file_never_crashes(tmp_path, content):
    from nemapi.services.logs import LogBuffer
    from nemapi.services.sessions import SessionManager

    path = tmp_path / "sessions.json"
    path.write_text(content)
    mgr = SessionManager(extension=None, logs=LogBuffer(), path=path)
    assert mgr.active == {}
    mgr.save()  # rewritten clean
    assert SessionManager(extension=None, logs=LogBuffer(), path=path).active == {}
