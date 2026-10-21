"""Test fixtures: a real Uvicorn server in a thread + a simulated browser extension."""
from __future__ import annotations

import asyncio
import socket
import sys
import threading
import time
from pathlib import Path
from typing import Awaitable, Callable, Dict, List, Optional

import httpx
import pytest
import uvicorn

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from nemapi.core.settings import Settings  # noqa: E402
from nemapi.main import create_app  # noqa: E402


def _free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


class LiveServer:
    def __init__(self, data_dir: Path, **overrides):
        self.port = _free_port()
        self.data_dir = data_dir
        self.settings = Settings(
            host="127.0.0.1",
            port=self.port,
            data_dir=data_dir,
            job_timeout=overrides.pop("job_timeout", 5.0),
            extension_wait=overrides.pop("extension_wait", 0.5),
            heartbeat_ttl=overrides.pop("heartbeat_ttl", 3.0),
            busy_heartbeat_ttl=overrides.pop("busy_heartbeat_ttl", 10.0),
            stats_flush_interval=0.2,
            **overrides,
        )
        self.app = create_app(self.settings)
        self.server = uvicorn.Server(
            uvicorn.Config(self.app, host="127.0.0.1", port=self.port, log_level="warning", lifespan="on")
        )
        self.thread = threading.Thread(target=self.server.run, daemon=True)

    @property
    def url(self) -> str:
        return f"http://127.0.0.1:{self.port}"

    @property
    def ctx(self):
        return self.app.state.ctx

    def start(self) -> "LiveServer":
        self.thread.start()
        deadline = time.time() + 10
        while not self.server.started:
            if time.time() > deadline:
                raise RuntimeError("server did not start")
            time.sleep(0.02)
        return self

    def stop(self) -> None:
        self.server.should_exit = True
        self.thread.join(timeout=10)


@pytest.fixture
def server_factory(tmp_path):
    servers: List[LiveServer] = []

    def make(data_dir: Optional[Path] = None, **overrides) -> LiveServer:
        srv = LiveServer(data_dir or (tmp_path / "data"), **overrides).start()
        servers.append(srv)
        return srv

    yield make
    for srv in servers:
        srv.stop()


@pytest.fixture
def server(server_factory) -> LiveServer:
    return server_factory()


@pytest.fixture
def anyio_backend():
    return "asyncio"


Responder = Callable[[dict], Awaitable[tuple]]


async def echo_responder(job: dict):
    return "result", f"ECHO: {job['question'][-40:]}"


class FakeExtension:
    """Mimics extension/background.js: reports tabs, polls /job, posts results."""

    PROVIDERS = ["deepseek", "qwen", "claude", "gemini", "chatgpt", "kimi", "zai"]

    def __init__(self, base_url: str, responder: Responder = echo_responder, providers=None, long_poll: float = 1.0):
        self.base_url = base_url
        self.responder = responder
        self.providers = providers or self.PROVIDERS
        self.long_poll = long_poll
        self.received: List[dict] = []
        self.active: Dict[str, int] = {}
        self.max_parallel = 0
        self._tasks: List[asyncio.Task] = []
        self._stop = asyncio.Event()

    def tabs(self):
        return [
            {"id": 100 + i, "provider": p, "url": f"https://{p}.example/", "title": p}
            for i, p in enumerate(self.providers)
        ]

    async def report_tabs(self, client: httpx.AsyncClient, busy: bool = False):
        await client.post("/extension/tabs", json={"tabs": self.tabs(), "busy": busy})

    async def _run_job(self, client: httpx.AsyncClient, job: dict):
        provider = job["provider"]
        self.active[provider] = self.active.get(provider, 0) + 1
        self.max_parallel = max(self.max_parallel, sum(1 for v in self.active.values() if v))
        try:
            action, value = await self.responder(job)
            payload = {"jobId": job["jobId"], "action": action}
            payload["result" if action == "result" else "error"] = value
            await client.post("/job", json=payload)
        finally:
            self.active[provider] -= 1

    async def _loop(self):
        async with httpx.AsyncClient(base_url=self.base_url, timeout=30) as client:
            await self.report_tabs(client)
            while not self._stop.is_set():
                try:
                    r = await client.get("/job", params={"wait": self.long_poll})
                    job = r.json()
                except (httpx.HTTPError, ValueError):
                    await asyncio.sleep(0.05)
                    continue
                await self.report_tabs(client)
                if job.get("action") == "ask":
                    self.received.append(job)
                    self._tasks.append(asyncio.create_task(self._run_job(client, job)))

    async def __aenter__(self):
        self._tasks.append(asyncio.create_task(self._loop()))
        # wait until the proxy has registered the tabs
        async with httpx.AsyncClient(base_url=self.base_url) as c:
            for _ in range(100):
                st = (await c.get("/status")).json()
                if st["extension"] and all(st["providers_ready"].get(p) for p in self.providers):
                    break
                await asyncio.sleep(0.02)
        return self

    async def __aexit__(self, *exc):
        self._stop.set()
        for t in self._tasks:
            t.cancel()
        await asyncio.gather(*self._tasks, return_exceptions=True)
