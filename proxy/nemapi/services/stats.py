"""Usage analytics (requests / tokens / estimated cost), persisted to data/stats.json.

Counters live in memory; disk writes are batched by a background task instead of
rewriting the file synchronously on every request (major latency fix vs v4.0).

The daily history (one bucket per day, 14 days kept) is what makes the
analytics page a real dashboard instead of a single total.
"""
from __future__ import annotations

import asyncio
import threading
import time
from datetime import date, datetime, timedelta
from pathlib import Path
from typing import Any, Dict, List

from ..core.providers import PRICING_CHECKED_AT, PRICING_SOURCES, PROVIDERS, TOKEN_PRICING, calculate_cost
from ..utils.jsonfile import read_json, write_json_atomic
from .logs import LogBuffer

# How many days of history are kept (the chart shows them all).
HISTORY_DAYS = 14


def _empty_provider() -> Dict[str, float]:
    return {"requests": 0, "prompt_tokens": 0, "completion_tokens": 0, "cost": 0.0}


def _empty_day() -> Dict[str, float]:
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
            "daily": {},
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
            daily = data.get("daily")
            if isinstance(daily, dict):
                for day, ddata in daily.items():
                    if not isinstance(day, str) or not isinstance(ddata, dict):
                        continue
                    try:
                        base["daily"][day] = {
                            "requests": int(ddata.get("requests") or 0),
                            "prompt_tokens": int(ddata.get("prompt_tokens") or 0),
                            "completion_tokens": int(ddata.get("completion_tokens") or 0),
                            "cost": float(ddata.get("cost") or 0.0),
                        }
                    except (TypeError, ValueError):
                        continue
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
            day = d["daily"].setdefault(date.today().isoformat(), _empty_day())
            day["requests"] += 1
            day["prompt_tokens"] += prompt_tokens
            day["completion_tokens"] += completion_tokens
            day["cost"] += cost
            self._prune(d["daily"])
            d["updated_at"] = datetime.now().isoformat(timespec="seconds")
            self._dirty = True
        return cost

    @staticmethod
    def _prune(daily: Dict[str, Any]) -> None:
        """Keep the last HISTORY_DAYS days only.

        The cutoff is a DATE, not a count: a history written every day for a
        month holds 30 buckets, but a history that skipped a week still holds
        stale days that must go.
        """
        cutoff = (date.today() - timedelta(days=HISTORY_DAYS - 1)).isoformat()
        for day in [k for k in daily if k < cutoff]:
            daily.pop(day, None)

    def clear(self) -> None:
        with self._lock:
            self._data = self._empty()
            self._data["updated_at"] = datetime.now().isoformat(timespec="seconds")
            self._dirty = True
        self.flush()
        self.logs.info("Analytics counters reset")

    # ------------------------------------------------------------- reports
    @staticmethod
    def pricing_view(pid: str) -> Dict[str, Any]:
        """The reference prices of a provider, as shown in the analytics page."""
        p = TOKEN_PRICING.get(pid, {})
        return {
            "prompt_per_1k": p.get("prompt"),
            "completion_per_1k": p.get("completion"),
            "cached_per_1k": p.get("cached"),
            "label": p.get("label", pid),
            "note": p.get("note", ""),
            "source": PRICING_SOURCES.get(pid, ""),
        }

    def report(self, started_at: float, version: str) -> Dict[str, Any]:
        with self._lock:
            d = self._data
            providers_out = {}
            for name, pdata in d["providers"].items():
                req = pdata.get("requests") or 0
                providers_out[name] = {
                    **pdata,
                    "avg_prompt_tokens": round(pdata["prompt_tokens"] / req, 1) if req else 0,
                    "avg_completion_tokens": round(pdata["completion_tokens"] / req, 1) if req else 0,
                    "cost": round(pdata.get("cost") or 0.0, 6),
                    "pricing": self.pricing_view(name),
                }
            uptime = max(1, int(time.time() - started_at))
            total = d["requests"]
            # The last HISTORY_DAYS days, oldest first, with the empty days
            # filled in: a gap in the chart must read as "nothing that day",
            # not as a missing point.
            today = date.today()
            days: List[Dict[str, Any]] = []
            for offset in range(HISTORY_DAYS - 1, -1, -1):
                key = (today - timedelta(days=offset)).isoformat()
                bucket = d["daily"].get(key) or _empty_day()
                days.append({
                    "date": key,
                    "requests": bucket["requests"],
                    "prompt_tokens": bucket["prompt_tokens"],
                    "completion_tokens": bucket["completion_tokens"],
                    "cost": round(bucket["cost"], 6),
                })
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
                "daily": days,
                "history_days": HISTORY_DAYS,
                "pricing": {pid: self.pricing_view(pid) for pid in TOKEN_PRICING},
                "pricing_checked_at": PRICING_CHECKED_AT,
                "updated_at": d.get("updated_at"),
                "persisted": True,
            }
