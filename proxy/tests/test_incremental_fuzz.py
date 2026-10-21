"""Randomised agent sessions: whatever happens (identical answers, identical tool
outputs, repeated user messages, errors before/after typing, retries, compaction,
system prompt changes) every new message must reach the web discussion EXACTLY
once, in a single discussion."""
import random
import sys
from collections import Counter
from pathlib import Path

import httpx
import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "tools"))
from virtual_agent import QWEN_CODE_UA, ScriptedWebModel, WebUIExtension, configure  # noqa: E402

TOOLS = [{"type": "function", "function": {"name": "run", "description": "Run.",
                                           "parameters": {"type": "object", "properties": {"n": {"type": "integer"}}}}}]


class FuzzModel(ScriptedWebModel):
    def __init__(self, rng: random.Random):
        super().__init__()
        self.rng = rng

    def answer(self, discussion):
        r = self.rng.random()
        if r < 0.08:
            return "error", "composer not found"
        if r < 0.16:
            return "error_typed", "response not captured"
        if r < 0.55:
            return "result", f'<tool>{{"name": "run", "arguments": {{"n": {self.rng.randint(1, 2)}}}}}</tool>'
        return "result", self.rng.choice(["Done.", "OK", "Done."])


@pytest.mark.anyio
@pytest.mark.parametrize("seed", range(6))
async def test_fuzz_every_message_typed_exactly_once(server, seed):
    rng = random.Random(seed)
    async with httpx.AsyncClient(base_url=server.url, headers={"User-Agent": QWEN_CODE_UA}, timeout=20) as c:
        await configure(server.url, incremental=True)
        async with WebUIExtension(server.url, model=FuzzModel(rng), latency=0.0) as ext:
            system = "SYSTEM-BASE line"
            messages = [{"role": "user", "content": "<<u0>>"}]
            expected = Counter({"<<u0>>": 1, "SYSTEM-BASE line": 1})
            n = 0
            for step in range(30):
                body = {"model": "qwen", "messages": [{"role": "system", "content": system}] + messages,
                        "tools": TOOLS, "stream": False}
                for _attempt in range(6):  # SDK-like retries with the identical history
                    r = await c.post("/v1/chat/completions", json=body)
                    if r.status_code == 200:
                        break
                    if rng.random() < 0.3:  # sometimes retry twice the same request after success too
                        continue
                assert r.status_code == 200, r.text
                msg = r.json()["choices"][0]["message"]
                if rng.random() < 0.15:  # duplicate retry of an already answered request
                    r2 = await c.post("/v1/chat/completions", json=body)
                    assert r2.status_code == 200
                messages.append({k: v for k, v in msg.items() if v is not None})
                n += 1
                if msg.get("tool_calls"):
                    for tc in msg["tool_calls"]:
                        out = rng.choice(["OUTPUT-SAME", f"<<t{n}>>"])
                        messages.append({"role": "tool", "tool_call_id": tc["id"], "content": out})
                        expected[out] += 1
                else:
                    u = rng.choice(["CONTINUE-NOW", f"<<u{n}>>"])
                    messages.append({"role": "user", "content": u})
                    expected[u] += 1
                roll = rng.random()
                if roll < 0.07:  # compaction keeping the last 2 messages
                    s = f"<<summary{n}>>"
                    last_a = max(i for i, m in enumerate(messages) if m.get("role") == "assistant")
                    messages = [{"role": "user", "content": s}] + messages[last_a:]  # keep calls + results
                    expected[s] += 1
                elif roll < 0.12:  # dynamic system prompt line
                    line = f"<<sys{n}>>"
                    system += "\n" + line
                    expected[line] += 1
            # final request so the last appended messages are typed too
            body = {"model": "qwen", "messages": [{"role": "system", "content": system}] + messages,
                    "tools": TOOLS, "stream": False}
            for _ in range(6):
                if (await c.post("/v1/chat/completions", json=body)).status_code == 200:
                    break

    tab = ext.tabs["qwen"]
    assert len(tab.discussions) == 1 and all(j["freshChat"] is False for j in ext.jobs)
    text = "\n".join(tab.current)
    problems = {}
    for token, want in expected.items():
        got = text.count(token)
        # a compaction summary placed BEFORE our last answer only restates what the
        # discussion already contains: typing it is optional, typing it twice is not
        ok = got in (0, 1) if token.startswith("<<summary") else got == want
        if not ok:
            problems[token] = (want, got)
    assert not problems, f"seed {seed}: (attendu, présent) {problems}"
