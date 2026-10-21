"""Usage analytics (requests / tokens / estimated cost), persisted to data/stats.json.

Counters live in memory; disk writes are batched by a background task instead of
rewriting the file synchronously on every request (major latency fix vs v4.0).
"""
from __future__ import annotations

import asyncio
import threading
import time
from datetime import datetime
from pathlib import Path
from typing import Any, Dict

from ..core.providers import PROVIDERS, TOKEN_PRICING, calculate_cost
from ..utils.jsonfile import read_json, write_json_atomic
from .logs import LogBuffer


def _empty_provider() -> Dict[str, float]:
    return {"requests": 0, "prompt_tokens": 0, "completion_tokens": 0, "cost": 0.0}


class StatsStore:
    def __init__(self, path: Path, logs: LogBuffer, flush_interval: float = 2.0):
        self.path = path
        self.logs = logs
        self.flush_interval = flush_interval
        self._lock = threading.Lock()
        self._dirty = False
        self._task: asyncio.Task | None = None
        self._data = self._load()

    # ------------------------------------------------------------------ io
    def _empty(self) -> Dict[str, Any]:
        return {
            "requests": 0,
            "prompt_tokens": 0,
            "completion_tokens": 0,
            "estimated_cost": 0.0,
            "providers": {pid: _empty_provider() for pid in PROVIDERS},
            "updated_at": None,
        }

    def _load(self) -> Dict[str, Any]:
        base = self._empty()
        if not self.path.exists():
            return base
        try:
            data = read_json(self.path)
            if not isinstance(data, dict):
                return base
            for pid, pdata in (data.get("providers") or {}).items():
                if pid in base["providers"] and isinstance(pdata, dict):
                    base["providers"][pid] = {
                        "requests": int(pdata.get("requests") or 0),
                        "prompt_tokens": int(pdata.get("prompt_tokens") or 0),
                        "completion_tokens": int(pdata.get("completion_tokens") or 0),
                        "cost": float(pdata.get("cost") or 0.0),
                    }
            base.update(
                requests=int(data.get("requests") or 0),
                prompt_tokens=int(data.get("prompt_tokens") or 0),
                completion_tokens=int(data.get("completion_tokens") or 0),
                estimated_cost=float(data.get("estimated_cost") or 0.0),
                updated_at=data.get("updated_at"),
            )
        except (OSError, ValueError, TypeError) as exc:
            self.logs.warn(f"stats.json illisible, compteurs remis à zéro: {exc}")
        return base

    def flush(self) -> None:
        with self._lock:
            if not self._dirty:
                return
            payload = {**self._data, "providers": {k: dict(v) for k, v in self._data["providers"].items()}}
            self._dirty = False
        try:
            write_json_atomic(self.path, payload)
        except OSError as exc:
            self.logs.error(f"Erreur sauvegarde stats: {exc}")

    async def _flush_loop(self) -> None:
        while True:
            await asyncio.sleep(self.flush_interval)
            if self._dirty:
                await asyncio.to_thread(self.flush)

    def start(self) -> None:
        if self._task is None:
            self._task = asyncio.create_task(self._flush_loop())

    async def stop(self) -> None:
        if self._task:
            self._task.cancel()
            try:
                await self._task
            except asyncio.CancelledError:
                pass
            self._task = None
        self.flush()

    # ------------------------------------------------------------- updates
    def record(self, provider: str, prompt_tokens: int, completion_tokens: int) -> float:
        cost = calculate_cost(provider, prompt_tokens, completion_tokens)
        with self._lock:
            d = self._data
            d["requests"] += 1
            d["prompt_tokens"] += prompt_tokens
            d["completion_tokens"] += completion_tokens
            d["estimated_cost"] += cost
            p = d["providers"].setdefault(provider, _empty_provider())
            p["requests"] += 1
            p["prompt_tokens"] += prompt_tokens
            p["completion_tokens"] += completion_tokens
            p["cost"] += cost
            d["updated_at"] = datetime.now().isoformat(timespec="seconds")
            self._dirty = True
        return cost

    def clear(self) -> None:
        with self._lock:
            self._data = self._empty()
            self._data["updated_at"] = datetime.now().isoformat(timespec="seconds")
            self._dirty = True
        self.flush()
        self.logs.info("Analytics counters reset")

    # ------------------------------------------------------------- reports
    def report(self, started_at: float, version: str) -> Dict[str, Any]:
        with self._lock:
            d = self._data
            providers_out = {}
            for name, pdata in d["providers"].items():
                req = pdata.get("requests") or 0
                pricing = TOKEN_PRICING.get(name, {})
                providers_out[name] = {
                    **pdata,
                    "avg_prompt_tokens": round(pdata["prompt_tokens"] / req, 1) if req else 0,
                    "avg_completion_tokens": round(pdata["completion_tokens"] / req, 1) if req else 0,
                    "cost": round(pdata.get("cost") or 0.0, 6),
                    "pricing": {
                        "prompt_per_1k": pricing.get("prompt"),
                        "completion_per_1k": pricing.get("completion"),
                        "label": pricing.get("label", name),
                    },
                }
            uptime = max(1, int(time.time() - started_at))
            total = d["requests"]
            return {
                "version": version,
                "uptime_seconds": uptime,
                "uptime_human": f"{uptime // 3600}h {(uptime % 3600) // 60}m {uptime % 60}s",
                "total_requests": total,
                "requests_per_minute": round(total / (uptime / 60), 2) if uptime >= 60 else total,
                "prompt_tokens": d["prompt_tokens"],
                "completion_tokens": d["completion_tokens"],
                "total_tokens": d["prompt_tokens"] + d["completion_tokens"],
                "estimated_cost": round(d["estimated_cost"], 6),
                "providers": providers_out,
                "pricing": {
                    pid: {
                        "prompt_per_1k": p.get("prompt"),
                        "completion_per_1k": p.get("completion"),
                        "label": p.get("label", pid),
                    }
                    for pid, p in TOKEN_PRICING.items()
                },
                "updated_at": d.get("updated_at"),
                "persisted": True,
            }
