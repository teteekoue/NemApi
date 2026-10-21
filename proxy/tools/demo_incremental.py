#!/usr/bin/env python3
"""Démo / benchmark du « contexte incrémental » avec un agent de coding virtuel.

Simulation complète (aucun navigateur requis) :
    pip install openai
    python tools/demo_incremental.py --rounds 3

Contre ton proxy réel + l'extension (l'agent virtuel parle au vrai provider web) :
    python tools/demo_incremental.py --live http://127.0.0.1:8090 --model qwen
"""
from __future__ import annotations

import argparse
import asyncio
import sys
import tempfile
import threading
import time
from pathlib import Path

import httpx

ROOT = Path(__file__).resolve().parents[1]
sys.path[:0] = [str(ROOT), str(ROOT / "tools")]

from virtual_agent import SYSTEM_PROMPT, TASK_1, TASK_2, VirtualCodingAgent, WebUIExtension, configure  # noqa: E402

# classic NemApi (integration off) vs the Qwen Code integration
MODES = {
    "classique, nouveau chat": {"incremental": False, "fresh_chat": True},
    "classique, même chat": {"incremental": False, "fresh_chat": False},
    "intégration Qwen Code": {"incremental": True},
}


class LocalProxy:
    def __init__(self):
        import socket

        import uvicorn

        from nemapi.core.settings import Settings
        from nemapi.main import create_app

        with socket.socket() as s:
            s.bind(("127.0.0.1", 0))
            self.port = s.getsockname()[1]
        settings = Settings(host="127.0.0.1", port=self.port, data_dir=Path(tempfile.mkdtemp()), job_timeout=30)
        self.server = uvicorn.Server(uvicorn.Config(create_app(settings), host="127.0.0.1", port=self.port, log_level="warning"))
        self.thread = threading.Thread(target=self.server.run, daemon=True)

    @property
    def url(self):
        return f"http://127.0.0.1:{self.port}"

    def __enter__(self):
        self.thread.start()
        while not self.server.started:
            time.sleep(0.02)
        return self

    def __exit__(self, *exc):
        self.server.should_exit = True
        self.thread.join(timeout=10)


async def simulate(rounds: int):
    rows = []
    for label, settings in MODES.items():
        with LocalProxy() as proxy:
            await configure(proxy.url, **settings)
            async with WebUIExtension(proxy.url, latency=0.005) as ext:
                agent = VirtualCodingAgent(proxy.url)
                t0 = time.perf_counter()
                for _ in range(rounds):
                    assert (await agent.ask(TASK_1)).startswith("TASK-1 DONE")
                    assert (await agent.ask(TASK_2)).startswith("TASK-2 DONE")
                elapsed = time.perf_counter() - t0
                await agent.aclose()
            tab = ext.tabs["qwen"]
            sizes = [len(j["question"]) for j in ext.jobs]
            rows.append((label, len(ext.jobs), sum(sizes), max(sizes), sizes[-1], len(tab.discussions),
                         sum(len(x) for x in tab.current), "\n".join(tab.current).count(SYSTEM_PROMPT), elapsed))

    print(f"\nAgent virtuel : {rounds} x (TASK-1 + TASK-2), outils read/write/run sur un FS virtuel\n")
    head = ("mode", "requêtes", "car. tapés", "plus gros", "dernier", "discussions", "contexte web", "copies sys.")
    print(f"{head[0]:<22}" + "".join(f"{h:>13}" for h in head[1:]))
    for r in rows:
        print(f"{r[0]:<22}" + "".join(f"{v:>13}" for v in r[1:8]))
    base, inc = rows[0][2], rows[2][2]
    print(f"\n→ contexte incrémental : {base / max(inc, 1):.1f}x moins de caractères tapés que « nouveau chat », "
          f"une seule discussion, prompt système présent une seule fois.")


async def live(url: str, model: str):
    async with httpx.AsyncClient(base_url=url, timeout=10) as c:
        snap = (await c.get("/agent-integrations")).json()
        qwen = next((a for a in snap.get("agents", []) if a.get("id") == "qwen-code"), {})
        if not qwen.get("enabled"):
            print("⚠️  L'intégration « Qwen Code » est désactivée (page Intégration agents IA) : "
                  "la démo tourne en mode classique.")
    agent = VirtualCodingAgent(url, model=model, sdk_retries=0)
    agent.client = agent.client.with_options(timeout=300)
    for task in (TASK_1, TASK_2):
        print(f"\n> {task}")
        print(await agent.ask(task))
    print("\nFichiers virtuels :", agent.fs.files)
    async with httpx.AsyncClient(base_url=url, timeout=10) as c:
        print("\nSessions :", (await c.get("/sessions")).json())
    await agent.aclose()


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--rounds", type=int, default=3)
    ap.add_argument("--live", metavar="URL", help="proxy réel (ex. http://127.0.0.1:8090)")
    ap.add_argument("--model", default="qwen")
    a = ap.parse_args()
    asyncio.run(live(a.live, a.model) if a.live else simulate(a.rounds))


if __name__ == "__main__":
    main()
