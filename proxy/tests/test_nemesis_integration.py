"""Intégration NEMESIS: discussion unique + appels d'outils natifs.

Nemesis (github.com/teteekoue/NEMESIS-AGENT) n'envoie que le message courant :
il gère son contexte de son côté et n'ouvre jamais de nouvelle discussion web.
L'intégration active donc deux choses :

* « Discussion unique » : la requête ne déclenche jamais l'ouverture d'une
  nouvelle URL de chat, même si le client ou la configuration globale le
  demandent ;
* « Appels d'outils natifs » : les appels d'outils écrits par l'agent (JSON
  dans un bloc ```json) sont renvoyés au format OpenAI tool_calls, et le
  contrat d'outils de NemApi n'est pas injecté (l'agent a le sien).

Et surtout PAS de contexte incrémental : chaque requête est tapée en entier.
"""
import json

import httpx
import pytest

from conftest import FakeExtension

# The exact shape Nemesis writes (NEMESIS-AGENT/TOOL_CALL_FORMATS.md):
# a JSON object in a ```json block, {"tool": name, "parameters": {...}}.
NEMESIS_CALL = {
    "tool": "read_file",
    "parameters": {"file_path": "src/main.py"},
}
# The canonical runtime prompt (NEMESIS-AGENT/src/core/system_prompt.py).
NEMESIS_PROMPT = (
    "# NEMESIS — Autonomous Engineering & Linux Operations Agent\n\n"
    "You are **NEMESIS**, a professional autonomous agent specialised in software\n"
    "engineering and Linux system administration."
)
# The older fallback wording (NEMESIS-AGENT/prompt_system.txt).
NEMESIS_FALLBACK_PROMPT = (
    "# NEMESIS Runtime Prompt\n\nNEMESIS is a reliable engineering agent. "
    "Use canonical JSON tool calls."
)
NEMESIS_HEADERS = {"X-NemApi-Agent": "nemesis"}
# Nemesis merges its system prompt into the FIRST USER message.
NEMESIS_FIRST_TURN = {
    "role": "user",
    "content": "[SYSTEM INSTRUCTIONS]\n" + NEMESIS_PROMPT + "\n\n[USER MESSAGE]\nlist the files",
}


def _body(*messages, **extra):
    return {"model": "qwen-chat", "stream": False, "messages": list(messages), **extra}


async def _configure(c, *, enabled=True, **features):
    body = {"enabled": enabled}
    if features:
        body["features"] = features
    r = await c.post("/agent-integrations/nemesis", json=body)
    r.raise_for_status()
    return r.json()


def _nemesis(snapshot):
    return next(a for a in snapshot["agents"] if a["id"] == "nemesis")


@pytest.mark.anyio
async def test_nemesis_is_listed_and_off_by_default(server):
    async with httpx.AsyncClient(base_url=server.url) as c:
        snap = (await c.get("/agent-integrations")).json()
        n = _nemesis(snap)
        assert n["name"] == "Nemesis" and n["enabled"] is False
        assert {f["id"] for f in n["features"]} == {"persistent_chat", "native_tool_calls"}
        assert all(f["enabled"] and f["default"] for f in n["features"])
        assert "incremental_context" not in {f["id"] for f in n["features"]}


@pytest.mark.anyio
@pytest.mark.parametrize("how", ["header", "prompt", "prompt_fallback", "user_agent"])
async def test_nemesis_is_detected(server, how):
    async with FakeExtension(server.url) as ext, httpx.AsyncClient(base_url=server.url, timeout=20) as c:
        await _configure(c)
        headers = {"User-Agent": "python-requests/2.32.3"}
        messages = [{"role": "user", "content": "hello"}]
        if how == "header":
            headers["X-NemApi-Agent"] = "nemesis"
        elif how == "prompt":
            messages = [{"role": "system", "content": NEMESIS_PROMPT}, {"role": "user", "content": "hello"}]
        elif how == "prompt_fallback":
            messages = [{"role": "system", "content": NEMESIS_FALLBACK_PROMPT},
                        {"role": "user", "content": "hello"}]
        else:
            headers["User-Agent"] = "Nemesis-Agent"
            messages = [NEMESIS_FIRST_TURN]
        r = await c.post("/v1/chat/completions", json=_body(*messages), headers=headers)
        assert r.status_code == 200, r.text
        reason = _nemesis((await c.get("/agent-integrations")).json())["detected"]["reason"]
        assert reason == {
            "header": "en-tête X-NemApi-Agent",
            "prompt": "prompt système",
            "prompt_fallback": "prompt système",
            "user_agent": "User-Agent",
        }[how]


@pytest.mark.anyio
async def test_another_client_is_not_taken_for_nemesis(server):
    async with FakeExtension(server.url) as ext, httpx.AsyncClient(base_url=server.url, timeout=20) as c:
        await _configure(c)
        await c.post("/v1/chat/completions", json=_body({"role": "user", "content": "hello"}),
                     headers={"User-Agent": "python-requests/2.32.3"})
        snap = (await c.get("/agent-integrations")).json()
        assert _nemesis(snap)["detected"]["requests"] == 0
        assert snap["clients"][0]["mode"] == "classique"


@pytest.mark.anyio
async def test_nemesis_never_opens_a_new_discussion(server):
    """« Discussion unique »: fresh_chat is forced off, whatever the client asks."""
    async with FakeExtension(server.url) as ext, httpx.AsyncClient(base_url=server.url, timeout=20) as c:
        await c.post("/settings", json={"fresh_chat": True})
        await _configure(c)
        r = await c.post("/v1/chat/completions", json=_body(NEMESIS_FIRST_TURN, fresh_chat=True),
                         headers=NEMESIS_HEADERS)
        assert r.status_code == 200, r.text
        assert ext.received[-1]["freshChat"] is False


@pytest.mark.anyio
async def test_nemesis_stays_classic_while_the_integration_is_off(server):
    async with FakeExtension(server.url) as ext, httpx.AsyncClient(base_url=server.url, timeout=20) as c:
        await c.post("/settings", json={"fresh_chat": True})
        await _configure(c, enabled=False)
        r = await c.post("/v1/chat/completions", json=_body(NEMESIS_FIRST_TURN),
                         headers=NEMESIS_HEADERS)
        assert r.status_code == 200, r.text
        assert ext.received[-1]["freshChat"] is True  # classic behaviour


@pytest.mark.anyio
async def test_nemesis_has_no_incremental_context(server):
    """Every request is typed in full: Nemesis keeps its own context."""
    async with FakeExtension(server.url) as ext, httpx.AsyncClient(base_url=server.url, timeout=20) as c:
        await _configure(c)
        for word in ("first", "second"):
            r = await c.post("/v1/chat/completions", json=_body({"role": "user", "content": word}),
                             headers=NEMESIS_HEADERS)
            assert r.status_code == 200, r.text
        assert len(ext.received) == 2
        assert ext.received[0]["question"] != ext.received[1]["question"]
        assert "first" in ext.received[0]["question"] and "second" in ext.received[1]["question"]
        assert (await c.get("/sessions")).json()["enabled"] is False


@pytest.mark.anyio
async def test_nemesis_tool_calls_are_returned_in_the_openai_format(server):
    """The agent's own JSON dialect becomes OpenAI tool_calls."""
    answer = "Je lis le fichier.\n\n```json\n" + json.dumps(NEMESIS_CALL) + "\n```"

    async def responder(job):
        return "result", answer

    async with FakeExtension(server.url, responder=responder) as ext, \
            httpx.AsyncClient(base_url=server.url, timeout=20) as c:
        await _configure(c)
        r = await c.post("/v1/chat/completions", json=_body(NEMESIS_FIRST_TURN), headers=NEMESIS_HEADERS)
        assert r.status_code == 200, r.text
        msg = r.json()["choices"][0]["message"]
        assert len(msg["tool_calls"]) == 1
        call = msg["tool_calls"][0]
        assert call["type"] == "function"
        assert call["function"]["name"] == "read_file"
        assert json.loads(call["function"]["arguments"]) == {"file_path": "src/main.py"}
        # the call is not left in the text
        assert "read_file" not in (msg["content"] or "")
        assert "Je lis le fichier." in (msg["content"] or "")


@pytest.mark.anyio
async def test_an_ordinary_json_snippet_is_not_a_tool_call(server):
    """A JSON block that is not a call must stay in the answer."""
    snippet = "Voici la configuration :\n\n```json\n{\"name\": \"prod\", \"timeout\": 30}\n```"

    async def responder(job):
        return "result", snippet

    async with FakeExtension(server.url, responder=responder) as ext, \
            httpx.AsyncClient(base_url=server.url, timeout=20) as c:
        await _configure(c)
        r = await c.post("/v1/chat/completions", json=_body(NEMESIS_FIRST_TURN), headers=NEMESIS_HEADERS)
        assert r.status_code == 200, r.text
        msg = r.json()["choices"][0]["message"]
        assert not msg.get("tool_calls")
        assert "prod" in (msg["content"] or "")


@pytest.mark.anyio
async def test_the_tool_contract_of_nemapi_is_not_injected(server):
    """Nemesis brings its own tool instructions: a second contract would conflict."""
    tools = [{"type": "function", "function": {"name": "read_file", "description": "Read a file.",
                                               "parameters": {"type": "object", "properties": {}}}}]
    async with FakeExtension(server.url) as ext, httpx.AsyncClient(base_url=server.url, timeout=20) as c:
        await _configure(c)
        await c.post("/v1/chat/completions", json=_body(NEMESIS_FIRST_TURN, tools=tools), headers=NEMESIS_HEADERS)
        assert "You can call tools" not in ext.received[-1]["question"]
        # a classic client still gets the contract
        await c.post("/v1/chat/completions", json=_body({"role": "user", "content": "hi"}, tools=tools),
                     headers={"User-Agent": "python-requests/2.32.3"})
        assert "You can call tools" in ext.received[-1]["question"]


@pytest.mark.anyio
async def test_the_features_can_be_switched_off_independently(server):
    async def responder(job):
        return "result", "```json\n" + json.dumps(NEMESIS_CALL) + "\n```"

    async with FakeExtension(server.url, responder=responder) as ext, \
            httpx.AsyncClient(base_url=server.url, timeout=20) as c:
        await c.post("/settings", json={"fresh_chat": True})
        await _configure(c, persistent_chat=False, native_tool_calls=False)
        r = await c.post("/v1/chat/completions", json=_body(NEMESIS_FIRST_TURN), headers=NEMESIS_HEADERS)
        assert r.status_code == 200, r.text
        assert ext.received[-1]["freshChat"] is True  # the switch is off
        msg = r.json()["choices"][0]["message"]
        assert not msg.get("tool_calls")  # the call stays in the text
        assert "read_file" in (msg["content"] or "")
