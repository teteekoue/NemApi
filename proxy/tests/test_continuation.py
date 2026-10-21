"""Answers cut off in the middle of a tool call (long files) — v4.5.

Real DeepSeek session (« Bonsoir - DeepSeek.pdf »): rewriting an 840-line
HTML game, the answer stopped in the middle of the write_file JSON. The
lenient parser closed the string, the agent wrote a TRUNCATED file and was
told « Successfully overwrote file ». Now the proxy detects the cut, asks the
model to continue in the same discussion, joins the parts, and never executes
a call that is still incomplete.
"""
import asyncio
import json
import sys
from pathlib import Path

import httpx
import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "tools"))

from virtual_agent import TASK_1, TASK_2, ScriptedWebModel, VirtualCodingAgent, WebUIExtension, QWEN_CODE_UA, configure  # noqa: E402

from nemapi.services.jobs import DISPATCHED, DONE, JobManager  # noqa: E402
from nemapi.services.logs import LogBuffer  # noqa: E402
from nemapi.services.sessions import postprocess  # noqa: E402
from nemapi.utils.continuation import (  # noqa: E402
    NOTE_NOT_EXECUTED,
    continuation_prompt,
    find_truncated_call,
    stitch,
)

CONTENT = "".join(f"function f{i}(r){{ return r>=0 && r<8 ? \"ok\\\\n\" : '{i}'; }}\n" for i in range(40))
CALL = {"name": "write_file", "arguments": {"file_path": "/tmp/jeu.html", "content": CONTENT}}
FULL = "Je réécris le fichier.\n\n````tool\n<tool>" + json.dumps(CALL, ensure_ascii=False) + "</tool>\n````"


def _calls(parsed):
    return [(c["function"]["name"], json.loads(c["function"]["arguments"])) for c in parsed.tool_calls]


# ------------------------------------------------------------------ detection
def test_detects_a_cut_anywhere_inside_the_call():
    start = FULL.index("<tool>")
    for cut in range(start + 8, len(FULL) - len("}</tool>\n````"), 97):
        assert find_truncated_call(FULL[:cut]) == start, cut


def test_complete_calls_are_not_truncated():
    assert find_truncated_call(FULL) is None
    assert find_truncated_call("Bonjour, rien à faire.") is None
    # </tool> forgotten but the JSON is complete
    assert find_truncated_call('<tool>{"name": "x", "arguments": {"a": 1}}') is None
    # unescaped inner quotes (mangled), but the value ends: not a cut
    assert find_truncated_call('<tool>{"name": "x", "arguments": {"a": "say "hi" now"}}') is None


def test_detects_cut_dsml_and_bare_json_fence():
    dsml = ('Je l\'écris.\n<｜｜DSML｜｜ calls>\n<｜｜DSML｜｜ invoke name="write_file">\n'
            '<｜｜DSML｜｜ parameter name="content" string="true"><html><body>…')
    assert find_truncated_call(dsml) == dsml.index("<｜｜DSML｜｜ calls>")
    closed = dsml + "</｜｜DSML｜｜ parameter></｜｜DSML｜｜ invoke></｜｜DSML｜｜ calls>"
    assert find_truncated_call(closed) is None
    bare = '````tool\n{"name": "write_file", "arguments": {"content": "abc'
    assert find_truncated_call(bare) == 0


def test_a_truncated_call_is_never_executed():
    parsed = postprocess(FULL[: len(FULL) // 2], ["write_file"])
    assert parsed.tool_calls == []
    assert parsed.text.startswith("Je réécris le fichier.") and NOTE_NOT_EXECUTED in parsed.text
    assert "````" not in parsed.text and "<tool>" not in parsed.text


# ------------------------------------------------------------------ stitching
def test_stitch_with_repeated_words():
    cut = len(FULL) // 2
    partial = FULL[:cut]
    cont = "````tool\n" + FULL[cut - 15:].replace("\n````", "") + "\n````"
    joined = stitch(partial, cont)
    assert _calls(postprocess(joined, ["write_file"])) == [("write_file", CALL["arguments"])]
    assert postprocess(joined, ["write_file"]).text == "Je réécris le fichier."


def test_stitch_without_overlap_and_restart():
    cut = len(FULL) // 2
    partial = FULL[:cut]
    exact = "````tool\n" + FULL[cut:].replace("\n````", "") + "\n````"
    assert _calls(postprocess(stitch(partial, exact), ["write_file"])) == [("write_file", CALL["arguments"])]
    # the model started the call again from scratch: the new call replaces the cut one
    restart = "Je reprends.\n````tool\n<tool>" + json.dumps(CALL) + "</tool>\n````"
    joined = stitch(partial, restart)
    assert _calls(postprocess(joined, ["write_file"])) == [("write_file", CALL["arguments"])]


@pytest.mark.parametrize("repeated", [0, 8, 15, 60])
def test_stitch_on_repetitive_code_never_drops_or_duplicates(repeated):
    import random

    rnd = random.Random(repeated)
    html = "".join(f'<div class="cell" data-r="{i % 8}" data-c="{i // 8}"></div>\n' for i in range(80))
    call = {"name": "write_file", "arguments": {"path": "a.html", "content": html}}
    full = "Ok.\n````tool\n<tool>" + json.dumps(call) + "</tool>\n````"
    start = full.index("<tool>") + 10
    for _ in range(60):
        cut = rnd.randint(start + repeated, len(full) - 20)
        cont = "````tool\n" + full[cut - repeated:].replace("\n````", "") + "\n````"
        assert _calls(postprocess(stitch(full[:cut], cont), ["write_file"])) == [("write_file", call["arguments"])], cut


def test_continuation_prompt_quotes_the_end():
    p = continuation_prompt(FULL[:500])
    assert p.startswith("[CONTINUE]") and FULL[440:500].strip() in p


# ------------------------------------------------------------------ job layer
@pytest.mark.anyio
async def test_job_manager_continues_in_the_same_discussion():
    jm = JobManager(LogBuffer(), max_continuations=3)
    job = jm.create("write the file", "deepseek", "m", fresh_chat=True, continuation=True)
    finished = []
    job.on_complete = lambda j: finished.append(j.result)
    assert jm.take() is job
    cut = len(FULL) // 3
    assert jm.complete(job.id, "result", result=FULL[:cut])
    assert job.status == DISPATCHED and not finished  # still waiting for the rest

    other = jm.create("another request", "deepseek", "m")
    part2 = jm.take()
    assert part2 is not other and part2.question.startswith("[CONTINUE]")
    assert part2.fresh_chat is False  # same discussion, even when fresh_chat is on
    cut2 = 2 * len(FULL) // 3
    jm.complete(part2.id, "result", result="````tool\n" + FULL[cut - 10:cut2])
    part3 = jm.take()
    assert part3.question.startswith("[CONTINUE]") and job.continuations == 2
    jm.complete(part3.id, "result", result="````tool\n" + FULL[cut2 - 10:].replace("\n````", "") + "\n````")
    assert job.status == DONE and finished
    assert _calls(postprocess(job.result, ["write_file"])) == [("write_file", CALL["arguments"])]
    assert jm.take() is other  # the queue goes on


@pytest.mark.anyio
async def test_failed_continuation_keeps_the_partial_answer_unexecuted():
    jm = JobManager(LogBuffer(), max_continuations=2)
    job = jm.create("q", "deepseek", "m", continuation=True)
    jm.take()
    jm.complete(job.id, "result", result=FULL[:300])
    part2 = jm.take()
    jm.complete(part2.id, "error", error="Provider did not start responding")
    assert job.status == DONE and job.result == FULL[:300]
    parsed = postprocess(job.result, ["write_file"])
    assert parsed.tool_calls == [] and NOTE_NOT_EXECUTED in parsed.text


@pytest.mark.anyio
async def test_classic_mode_never_continues_but_never_executes_a_cut_call():
    jm = JobManager(LogBuffer())
    job = jm.create("q", "deepseek", "m")  # no agent integration
    jm.take()
    jm.complete(job.id, "result", result=FULL[:300])
    assert job.status == DONE and jm.take() is None
    assert postprocess(job.result, ["write_file"]).tool_calls == []


@pytest.mark.anyio
async def test_cancelling_the_parent_cancels_the_continuation():
    jm = JobManager(LogBuffer())
    job = jm.create("q", "deepseek", "m", continuation=True)
    jm.take()
    jm.complete(job.id, "result", result=FULL[:300])
    child = job.child
    jm.complete(job.id, "error", error="timeout waiting for extension")
    assert child.status == "cancelled" and jm.take() is None


# ------------------------------------------------------------------ end to end
@pytest.fixture(autouse=True)
async def _close_agents(anyio_backend):
    yield
    await VirtualCodingAgent.close_all()


@pytest.mark.anyio
@pytest.mark.parametrize("incremental", [True, False])
async def test_agent_writes_a_long_file_through_an_output_limit(server, incremental):
    async with httpx.AsyncClient(base_url=server.url) as c:
        await c.post("/settings", json={"fresh_chat": True})
    await configure(server.url, integration=True, incremental=incremental)
    model = ScriptedWebModel(styles=("fenced",), output_limit=700, big_test_lines=25)
    async with WebUIExtension(server.url, model=model) as ext:
        agent = VirtualCodingAgent(server.url, stream=True)
        a1 = await agent.ask(TASK_1)
        a2 = await agent.ask(TASK_2)
    assert a1.startswith("TASK-1 DONE") and a2.startswith("TASK-2 DONE"), (a1, a2)
    # the long file arrived complete and byte-exact (backslashes, quotes, \n)
    assert agent.fs.files["test_main.py"] == model.test_file_content()
    assert model.continuations >= 3
    cont_jobs = [j for j in ext.jobs if j["question"].startswith("[CONTINUE]")]
    assert len(cont_jobs) == model.continuations and all(j["freshChat"] is False for j in cont_jobs)
    if incremental:
        assert len(ext.tabs["qwen"].discussions) == 1  # still one discussion
    for m in agent.messages:
        assert "[NemApi]" not in (m.get("content") or "")


# ------------------------------------------------------------------ keep-alive
@pytest.mark.anyio
async def test_slow_stream_gets_headers_and_keepalive_chunks(server, monkeypatch):
    from nemapi.routers import openai as openai_router

    monkeypatch.setattr(openai_router, "KEEPALIVE_AFTER", 0.2)
    monkeypatch.setattr(openai_router, "KEEPALIVE_EVERY", 0.25)
    from conftest import FakeExtension

    async def slow(job):
        await asyncio.sleep(1.3)
        if "fail" in job["question"]:
            return "error", "Provider did not start responding"
        return "result", "Voici la réponse."

    body = {"model": "deepseek-chat", "stream": True, "messages": [{"role": "user", "content": "salut"}]}
    await configure(server.url, integration=True)
    async with FakeExtension(server.url, responder=slow, providers=["deepseek"]):
        async with httpx.AsyncClient(base_url=server.url, headers={"User-Agent": QWEN_CODE_UA}, timeout=10) as c:
            async with c.stream("POST", "/v1/chat/completions", json=body) as r:
                assert r.status_code == 200 and r.headers["content-type"].startswith("text/event-stream")
                events = [line async for line in r.aiter_lines() if line.startswith("data: ")]
            chunks = [json.loads(e[6:]) for e in events if e != "data: [DONE]"]
            keepalive = [ch for ch in chunks if ch["choices"][0]["delta"] == {"content": ""}]
            assert len(keepalive) >= 2
            assert "".join(ch["choices"][0]["delta"].get("content") or "" for ch in chunks) == "Voici la réponse."
            assert sum(1 for ch in chunks if ch["choices"][0]["delta"].get("role")) == 1
            assert events[-1] == "data: [DONE]"

            # an error after the headers is sent in the stream (the SDK raises it)
            body["messages"][0]["content"] = "please fail"
            async with c.stream("POST", "/v1/chat/completions", json=body) as r:
                lines = [line async for line in r.aiter_lines() if line.startswith("data: ")]
            err = json.loads(lines[-2][6:])
            assert err["error"]["type"] == "automation_error" and "did not start" in err["error"]["message"]
