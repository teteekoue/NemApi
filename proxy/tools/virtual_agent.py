"""Virtual coding agent + simulated provider web UI, used to test incremental context.

* ``VirtualCodingAgent`` behaves like Qwen Code / Cline: it talks to NemApi with
  the official ``openai`` SDK, sends the system prompt + tools + the FULL history
  on every request, executes tool calls on a virtual file system and loops.

* ``WebUIExtension`` replaces the browser extension: each provider tab holds a
  real *discussion* (what a web chat would display). ``freshChat`` opens a new
  discussion, otherwise the prompt is appended to the current one, exactly like
  the real extension does.

* ``ScriptedWebModel`` is the "LLM" of that web UI. Like a real model it only
  knows what is in the CURRENT discussion: if the tools contract or a tool result
  is missing it cannot continue, if something is duplicated we can see it.
"""
from __future__ import annotations

import asyncio
import json
import re
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional

import httpx

try:  # the agent uses the official SDK when available (pip install openai)
    from openai import AsyncOpenAI
except ImportError:  # pragma: no cover
    AsyncOpenAI = None

SYSTEM_PROMPT = (
    "You are CodeAgent, an autonomous coding assistant working in the user's repository. "
    "Always inspect files before editing them, keep changes minimal, and run the code to verify."
)

TOOLS = [
    {
        "type": "function",
        "function": {
            "name": "list_files",
            "description": "List the files of the workspace.",
            "parameters": {"type": "object", "properties": {}},
        },
    },
    {
        "type": "function",
        "function": {
            "name": "read_file",
            "description": "Read a file.",
            "parameters": {"type": "object", "properties": {"path": {"type": "string"}}, "required": ["path"]},
        },
    },
    {
        "type": "function",
        "function": {
            "name": "write_file",
            "description": "Create or overwrite a file.",
            "parameters": {
                "type": "object",
                "properties": {"path": {"type": "string"}, "content": {"type": "string"}},
                "required": ["path", "content"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "run_command",
            "description": "Run a shell command and return its output.",
            "parameters": {"type": "object", "properties": {"command": {"type": "string"}}, "required": ["command"]},
        },
    },
]

# Qwen Code's User-Agent (provider/default.ts: `QwenCode/${version} (${platform}; ${arch})`):
# NemApi recognises the agent with it and applies the « Qwen Code » integration.
QWEN_CODE_UA = "QwenCode/0.9.0 (linux; x64)"


async def configure(base_url: str, *, integration: Optional[bool] = None, incremental: Optional[bool] = None,
                    side_queries_local: Optional[bool] = None, continuation: Optional[bool] = None,
                    keepalive: Optional[bool] = None, **settings) -> dict:
    """Set the proxy settings and the Qwen Code integration.

    ``incremental=True`` enables the integration with incremental context,
    ``incremental=False`` alone means classic NemApi (integration off)."""
    async with httpx.AsyncClient(base_url=base_url) as c:
        if settings:
            (await c.post("/settings", json=settings)).raise_for_status()
        features = {k: v for k, v in {"incremental_context": incremental, "side_queries_local": side_queries_local,
                                      "continuation": continuation, "keepalive": keepalive}.items() if v is not None}
        enabled = integration if integration is not None else incremental
        body: Dict[str, Any] = {}
        if enabled is not None:
            body["enabled"] = enabled
        if features:
            body["features"] = features
        if not body:
            return {}
        r = await c.post("/agent-integrations/qwen-code", json=body)
        r.raise_for_status()
        return r.json()


TASK_1 = "TASK-1: add a line that prints 'done' at the end of main.py, then run it."
TASK_2 = "TASK-2: now add a test file test_main.py and run the tests."


# --------------------------------------------------------------------------- web UI side
class ScriptedWebModel:
    """Deterministic 'LLM' that decides its next move from the discussion only."""

    RESULT_RE = re.compile(r"Tool result \((\w+)\): (.*)")

    # tool-call syntaxes a real web model may use (DeepSeek mixes them)
    STYLES = ("fenced", "tool", "dsml_fullwidth", "dsml_double", "dsml_ascii", "tool_then_stray_dsml", "fenced_json")

    def __init__(self, fail_on_call: Optional[int] = None, fail_after_typing: bool = False, styles=("tool",),
                 output_limit: int = 0, big_test_lines: int = 0):
        # output_limit: like a web chat that stops an answer after N characters
        # (DeepSeek's output cap); the model goes on when asked to [CONTINUE]
        self.output_limit = output_limit
        self.big_test_lines = big_test_lines
        self._pending = ""
        self._emitted = ""
        self.continuations = 0
        self.calls = 0
        self.styles = tuple(styles)
        self.tool_calls_made = 0
        self.side_requests: List[str] = []
        self.fail_on_call = fail_on_call
        # False: the composer was not found (nothing typed)
        # True : prompt submitted but the answer could not be captured
        self.fail_after_typing = fail_after_typing

    def answer(self, discussion: List[str]) -> tuple:
        action, value = self._answer(discussion)
        if action == "result" and self.output_limit and len(value) > self.output_limit:
            self._pending = value[self.output_limit:]
            value = value[: self.output_limit]
            self._emitted = value
        return action, value

    def test_file_content(self) -> str:
        lines = ["def test_ok():", "    assert True"]
        for i in range(self.big_test_lines):
            lines.append(f"def test_case_{i}():\n    data = {{\"id\": {i}, \"path\": \"C:\\\\tmp\\\\{i}\", \"re\": r\"\\d+\\n\"}}\n    assert data[\"id\"] == {i}")
        return "\n".join(lines)

    def _continue(self) -> tuple:
        self.continuations += 1
        overlap = self._emitted[-12:]  # "repeat the last few words"
        rest = self._pending
        self._pending = ""
        text = "````tool\n" + overlap + rest + "\n````"
        if self.output_limit and len(text) > self.output_limit:
            self._pending = text[self.output_limit:]
            text = text[: self.output_limit]
            # what remains goes on after the overlap of the next continuation
            self._pending = self._pending.removesuffix("\n````")
        self._emitted = text
        return "result", text

    def _answer(self, discussion: List[str]) -> tuple:
        self.calls += 1
        if discussion and discussion[-1].startswith("[CONTINUE]") and self._pending:
            return self._continue()
        if self.fail_on_call and self.calls == self.fail_on_call:
            if self.fail_after_typing:
                return "error_typed", "simulated failure: response not captured (timeout)"
            return "error", "simulated DOM failure (composer not found)"
        text = "\n\n".join(discussion)
        last = discussion[-1] if discussion else ""
        if last.startswith("[SIDE REQUEST"):
            return self._side_answer(last)
        if "Available tools:" not in text:
            return "result", "ERROR_NO_TOOLS_IN_CONTEXT"
        if "CodeAgent" not in text:
            return "result", "ERROR_NO_SYSTEM_PROMPT_IN_CONTEXT"

        last_task = max(text.rfind("TASK-1"), text.rfind("TASK-2"))
        if last_task < 0:
            return "result", "ERROR_NO_TASK_IN_CONTEXT"
        task = text[last_task : last_task + 6]
        segment = text[last_task:]
        results: Dict[str, str] = {}
        for m in self.RESULT_RE.finditer(segment):
            results[m.group(1)] = m.group(2).strip()

        def call(name: str, args: dict, say: str) -> tuple:
            return "result", f"{say}\n{self.render_call(name, args)}"

        if task == "TASK-1":
            if "list_files" not in results:
                return call("list_files", {}, "Je commence par lister les fichiers.")
            if "read_file" not in results:
                return call("read_file", {"path": "main.py"}, "Je lis main.py.")
            if "write_file" not in results:
                original = results["read_file"]  # proves the tool result is in the context
                return call("write_file", {"path": "main.py", "content": f"{original}\nprint('done')"}, "J'ajoute la ligne.")
            if "run_command" not in results:
                return call("run_command", {"command": "python main.py"}, "J'exécute le script.")
            return "result", f"TASK-1 DONE. Sortie : {results['run_command']}"

        if "write_file" not in results:
            return call("write_file", {"path": "test_main.py", "content": self.test_file_content()}, "Je crée le test.")
        if "run_command" not in results:
            return call("run_command", {"command": "pytest -q"}, "Je lance les tests.")
        return "result", f"TASK-2 DONE. Sortie : {results['run_command']}"


    def render_call(self, name: str, args: dict) -> str:
        style = self.styles[self.tool_calls_made % len(self.styles)]
        self.tool_calls_made += 1
        if style == "tool":
            return f'<tool>{json.dumps({"name": name, "arguments": args})}</tool>'
        if style == "fenced":  # the v4.4 contract: <tool> inside a ````tool code block
            return f'````tool\n<tool>{json.dumps({"name": name, "arguments": args})}</tool>\n````'
        if style == "fenced_json":  # model drops the <tool> tags but keeps the fence
            return f'````tool\n{json.dumps({"name": name, "arguments": args})}\n````'
        if style == "tool_then_stray_dsml":
            return (f'<tool>{json.dumps({"name": name, "arguments": args})}</tool>\n'
                    "</｜DSML｜parameter>\n</｜DSML｜invoke>\n</｜DSML｜calls>")
        bar, sp = {"dsml_fullwidth": ("｜", ""), "dsml_double": ("｜｜", " "), "dsml_ascii": ("||", "")}[style]
        tag = f"{bar}{sp}DSML{sp}{bar}{sp}"
        block = "calls" if style == "dsml_double" else "tool_calls"
        params = "".join(
            f'<{tag}parameter name="{k}" string="{"true" if isinstance(v, str) else "false"}">'
            f'{v if isinstance(v, str) else json.dumps(v)}</{tag}parameter>\n'
            for k, v in args.items()
        )
        return f'<{tag}{block}>\n<{tag}invoke name="{name}">\n{params}</{tag}invoke>\n</{tag}{block}>'

    def _side_answer(self, prompt: str) -> tuple:
        """A framed side request of the agent: answered on its own."""
        self.side_requests.append(prompt)
        if "respond_in_schema" in prompt:
            args = {"shouldBlock": False} if "blocked" in prompt else {"selected_memories": []}
            return "result", f'<tool>{json.dumps({"name": "respond_in_schema", "arguments": args})}</tool>'
        m = re.search(r"SUMMARIZE-PAGE: (\S+)", prompt)
        if m:
            return "result", f"SIDE-SUMMARY of {m.group(1)}: a short page about NemApi."
        return "result", "SIDE-DONE"


@dataclass
class Tab:
    provider: str
    tab_id: int
    discussions: List[List[str]] = field(default_factory=list)  # each: [prompt, answer, prompt, answer…]

    @property
    def current(self) -> List[str]:
        if not self.discussions:
            self.discussions.append([])
        return self.discussions[-1]


class WebUIExtension:
    """Simulated browser extension driving simulated provider web chats."""

    def __init__(self, base_url: str, providers=("qwen", "deepseek", "claude"), model: Optional[ScriptedWebModel] = None,
                 latency: float = 0.02):
        self.base_url = base_url
        self.model = model or ScriptedWebModel()
        self.latency = latency
        self.tabs = {p: Tab(p, 200 + i) for i, p in enumerate(providers)}
        self.jobs: List[dict] = []  # every job received (question, freshChat, provider)
        self.fresh_flags: List[Any] = []
        self._tasks: List[asyncio.Task] = []
        self._stop = asyncio.Event()

    async def _report(self, c: httpx.AsyncClient):
        tabs = [{"id": t.tab_id, "provider": p, "url": f"https://{p}.example/", "title": p} for p, t in self.tabs.items()]
        await c.post("/extension/tabs", json={"tabs": tabs, "busy": False})

    async def _run(self, c: httpx.AsyncClient, job: dict):
        tab = self.tabs[job["provider"]]
        if job.get("freshChat") is not False or not tab.discussions:
            tab.discussions.append([])  # navigate to "new chat"
        self.fresh_flags.append(job.get("freshChat"))
        discussion = tab.current
        discussion.append(job["question"])  # typed into the composer
        await asyncio.sleep(self.latency)
        action, value = self.model.answer(discussion)
        if action == "result":
            discussion.append(value)  # the answer is displayed in the discussion
            payload = {"jobId": job["jobId"], "action": "result", "result": value}
        elif action == "error_typed":
            payload = {"jobId": job["jobId"], "action": "error", "error": value, "typed": True}
        else:
            discussion.pop()  # the prompt never reached the web UI
            payload = {"jobId": job["jobId"], "action": "error", "error": value, "typed": False}
        await c.post("/job", json=payload)

    async def _loop(self):
        async with httpx.AsyncClient(base_url=self.base_url, timeout=30) as c:
            await self._report(c)
            while not self._stop.is_set():
                try:
                    job = (await c.get("/job", params={"wait": 1})).json()
                except (httpx.HTTPError, ValueError):
                    await asyncio.sleep(0.05)
                    continue
                await self._report(c)
                if job.get("action") == "ask":
                    self.jobs.append(job)
                    t = asyncio.create_task(self._run(c, job))
                    self._tasks.append(t)

    async def __aenter__(self):
        self._stop = asyncio.Event()  # the simulator can be re-entered (proxy restart)
        self._tasks = []
        self._tasks.append(asyncio.create_task(self._loop()))
        async with httpx.AsyncClient(base_url=self.base_url) as c:
            for _ in range(200):
                st = (await c.get("/status")).json()
                if st["extension"] and all(st["providers_ready"].get(p) for p in self.tabs):
                    break
                await asyncio.sleep(0.02)
        return self

    async def __aexit__(self, *exc):
        self._stop.set()
        for t in self._tasks:
            t.cancel()
        await asyncio.gather(*self._tasks, return_exceptions=True)

    # helpers for assertions / reports
    def chars_typed(self, provider: Optional[str] = None) -> int:
        return sum(len(j["question"]) for j in self.jobs if provider in (None, j["provider"]))


# --------------------------------------------------------------------------- agent side
class VirtualFS:
    def __init__(self):
        self.files = {"main.py": "print('hello')", "README.md": "# demo"}

    def run(self, name: str, args: dict) -> str:
        if name == "list_files":
            return ", ".join(sorted(self.files))
        if name == "read_file":
            return self.files.get(args.get("path", ""), "ERROR: file not found")
        if name == "write_file":
            self.files[args["path"]] = args["content"]
            return f"ok ({len(args['content'])} bytes written to {args['path']})"
        if name == "run_command":
            cmd = args.get("command", "")
            if cmd.startswith("python"):
                src = self.files.get(cmd.split()[-1], "")
                return " ".join(re.findall(r"print\('([^']*)'\)", src)) or "(no output)"
            if "pytest" in cmd:
                n = sum(1 for f in self.files if f.startswith("test_"))
                return f"{n} passed" if n else "no tests ran"
            return "(no output)"
        return f"ERROR: unknown tool {name}"


class VirtualCodingAgent:
    """Agent loop à la Qwen Code: full history + tools on every request."""

    instances: List["VirtualCodingAgent"] = []

    def __init__(self, base_url: str, model: str = "qwen3-coder-plus", stream: bool = True, api_key: str = "nemapi",
                 max_steps: int = 12, sdk_retries: int = 2, name: str = "agent", user_agent: str = QWEN_CODE_UA):
        if AsyncOpenAI is None:
            raise RuntimeError("pip install openai")
        VirtualCodingAgent.instances.append(self)
        self.client = AsyncOpenAI(base_url=base_url + "/v1", api_key=api_key, max_retries=sdk_retries, timeout=30,
                                  default_headers={"User-Agent": user_agent} if user_agent else None)
        self.model = model
        self.stream = stream
        self.max_steps = max_steps
        self.name = name
        self.fs = VirtualFS()
        self.tools = list(TOOLS)
        system = SYSTEM_PROMPT if name == "agent" else f"{SYSTEM_PROMPT} (agent {name})"
        self.messages: List[Dict[str, Any]] = [{"role": "system", "content": system}]
        self.requests = 0
        self.final_answers: List[str] = []

    async def _complete(self) -> Dict[str, Any]:
        self.requests += 1
        if not self.stream:
            resp = await self.client.chat.completions.create(model=self.model, messages=self.messages, tools=self.tools, stream=False)
            return resp.choices[0].message.model_dump(exclude_none=True)
        content, calls = "", {}
        stream = await self.client.chat.completions.create(model=self.model, messages=self.messages, tools=self.tools, stream=True)
        async for chunk in stream:
            if not chunk.choices:
                continue
            delta = chunk.choices[0].delta
            content += delta.content or ""
            for tc in delta.tool_calls or []:
                slot = calls.setdefault(tc.index, {"id": None, "type": "function", "function": {"name": "", "arguments": ""}})
                if tc.id:
                    slot["id"] = tc.id
                if tc.function and tc.function.name:
                    slot["function"]["name"] = tc.function.name
                if tc.function and tc.function.arguments:
                    slot["function"]["arguments"] += tc.function.arguments
        msg: Dict[str, Any] = {"role": "assistant", "content": content or None}
        if calls:
            msg["tool_calls"] = [calls[i] for i in sorted(calls)]
        return msg

    async def _complete_for(self, user_text: str) -> Dict[str, Any]:
        """Single request (no tool loop): append the user message, return the reply."""
        self.messages.append({"role": "user", "content": user_text})
        msg = await self._complete()
        self.messages.append(msg)
        return msg

    async def ask(self, user_text: str) -> str:
        self.messages.append({"role": "user", "content": user_text})
        for _ in range(self.max_steps):
            msg = await self._complete()
            self.messages.append(msg)
            if not msg.get("tool_calls"):
                answer = msg.get("content") or ""
                self.final_answers.append(answer)
                return answer
            for tc in msg["tool_calls"]:
                args = json.loads(tc["function"]["arguments"] or "{}")
                out = self.fs.run(tc["function"]["name"], args)
                self.messages.append({"role": "tool", "tool_call_id": tc["id"], "content": out})
        raise RuntimeError(f"{self.name}: too many steps")

    async def aclose(self) -> None:
        await self.client.close()

    @classmethod
    async def close_all(cls) -> None:
        while cls.instances:
            await cls.instances.pop().aclose()

    def compact(self) -> None:
        """Simulate an agent compressing its history (Qwen Code /compress, Cline truncation)."""
        summary = "Summary of previous work: main.py now prints hello and done; tests may be needed."
        self.messages = [self.messages[0], {"role": "user", "content": summary}, {"role": "assistant", "content": "Understood."}]


# --------------------------------------------------------------------------- Qwen Code-like agent
# Side queries Qwen Code sends through the same model (seen in a real DeepSeek
# discussion): memory recall selection and the AUTO-mode permission classifier
# (forced respond_in_schema tool), input suggestions ([SUGGESTION MODE: …]
# appended to the history), the managed memory extraction sub-agent (own
# system prompt + tools, conversation history in context) — plus a real side
# task that needs an answer (web page summary).
RECALL_SYSTEM = "You select which saved memories are relevant to the user's request. Answer with the schema tool."
CLASSIFIER_SYSTEM = "You are a security classifier for an autonomous coding agent running in AUTO mode."
EXTRACTION_SYSTEM = (
    "You are now acting as the managed memory extraction subagent for an AI coding assistant.\n\n"
    "The recent conversation history is already in your context. Analyze only that recent conversation "
    "and use it to update persistent managed memory.\n\nRules:\n"
    "- Read existing memory files first to avoid creating duplicates."
)
SUGGESTION_PROMPT = (
    "[SUGGESTION MODE: Suggest what the user might naturally type next.]\n\n"
    "FIRST: Read the LAST FEW LINES of the assistant's most recent message."
)
WEB_SUMMARY_SYSTEM = "You summarize fetched web pages for a coding agent. Be concise."


def schema_tool(properties: dict, required: list) -> list:
    return [{"type": "function", "function": {
        "name": "respond_in_schema", "description": "Respond with a JSON object matching the schema.",
        "parameters": {"type": "object", "properties": properties, "required": required}}}]


class QwenCodeLikeAgent(VirtualCodingAgent):
    """VirtualCodingAgent + the side queries Qwen Code sends to the same endpoint."""

    def __init__(self, *args, web_fetch_on: Optional[str] = None, **kw):
        super().__init__(*args, **kw)
        self.side_results: List[tuple] = []  # (kind, outcome)
        self.web_fetch_on = web_fetch_on  # TASK prefix that triggers a web summary side task
        self.raw = httpx.AsyncClient(base_url=str(self.client.base_url).rstrip("/"), timeout=30,
                                     headers={"User-Agent": kw.get("user_agent", QWEN_CODE_UA)})

    async def _side(self, kind: str, messages: list, tools: Optional[list] = None, tool_choice: Any = None):
        body: Dict[str, Any] = {"model": self.model, "messages": messages, "stream": False}
        if tools:
            body["tools"] = tools
        if tool_choice:
            body["tool_choice"] = tool_choice
        r = await self.raw.post("/chat/completions", json=body)
        if r.status_code != 200:
            self.side_results.append((kind, f"HTTP {r.status_code}"))
            return None
        msg = r.json()["choices"][0]["message"]
        self.side_results.append((kind, msg))
        return msg

    async def recall(self, user_text: str):
        return await self._side(
            "recall",
            [{"role": "system", "content": RECALL_SYSTEM},
             {"role": "user", "content": f"Select the relevant memories for: {user_text}\nCatalog: (empty)"}],
            schema_tool({"selected_memories": {"type": "array", "items": {"type": "string"}}}, ["selected_memories"]),
            "required",
        )

    async def classify(self, call: dict):
        return await self._side(
            "classifier",
            [{"role": "system", "content": CLASSIFIER_SYSTEM},
             {"role": "user", "content": f"Should this action be blocked? {json.dumps(call['function'])}"}],
            schema_tool({"shouldBlock": {"type": "boolean"}}, ["shouldBlock"]),
            "required",
        )

    async def suggest(self):
        return await self._side("suggestion", self.messages + [{"role": "user", "content": SUGGESTION_PROMPT}],
                                self.tools)

    async def extract_memories(self):
        history = [m for m in self.messages if m.get("role") != "system"]
        tools = [t for t in TOOLS if t["function"]["name"] in ("read_file", "write_file")]
        return await self._side(
            "memory",
            [{"role": "system", "content": EXTRACTION_SYSTEM}] + history
            + [{"role": "user", "content": "Extract durable memories from the conversation above."}],
            tools,
        )

    async def web_summary(self, url: str):
        return await self._side(
            "web_summary",
            [{"role": "system", "content": WEB_SUMMARY_SYSTEM},
             {"role": "user", "content": f"SUMMARIZE-PAGE: {url}\n<html>NemApi is a local proxy…</html>"}],
        )

    async def ask(self, user_text: str) -> str:
        await self.recall(user_text)  # Qwen Code: memory recall on every user prompt
        if self.web_fetch_on and user_text.startswith(self.web_fetch_on):
            await self.web_summary("https://example.com/nemapi")
        self.messages.append({"role": "user", "content": user_text})
        for _ in range(self.max_steps):
            msg = await self._complete()
            self.messages.append(msg)
            if not msg.get("tool_calls"):
                answer = msg.get("content") or ""
                self.final_answers.append(answer)
                await self.suggest()
                await self.extract_memories()
                return answer
            for tc in msg["tool_calls"]:
                await self.classify(tc)  # AUTO-mode permission check (fail-closed → manual approval)
                args = json.loads(tc["function"]["arguments"] or "{}")
                out = self.fs.run(tc["function"]["name"], args)
                self.messages.append({"role": "tool", "tool_call_id": tc["id"], "content": out})
        raise RuntimeError(f"{self.name}: too many steps")

    async def aclose(self) -> None:
        await self.raw.aclose()
        await super().aclose()
