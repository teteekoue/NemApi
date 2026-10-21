"""Runtime settings (overridable via environment variables NEMAPI_*)."""
from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path

PROXY_ROOT = Path(__file__).resolve().parents[2]  # .../proxy

DEFAULT_HOST = "0.0.0.0"
DEFAULT_PORT = 8090  # port officiel NemApi


def _env_float(name: str, default: float) -> float:
    try:
        return float(os.environ.get(name, default))
    except (TypeError, ValueError):
        return default


def _env_int(name: str, default: int) -> int:
    try:
        return int(os.environ.get(name, default))
    except (TypeError, ValueError):
        return default


@dataclass(frozen=True)
class Settings:
    host: str = DEFAULT_HOST
    port: int = DEFAULT_PORT
    data_dir: Path = PROXY_ROOT / "data"
    web_dir: Path = PROXY_ROOT / "web"
    # Max time a completion waits for the browser extension to answer.
    # Long: writing a big file can take several minutes on a chat web UI
    # (the extension, its watchdog and the reaper all follow this value).
    job_timeout: float = 900.0
    # Answer cut off inside a tool call → up to N continuations in the same discussion.
    max_continuations: int = 4
    # When the extension is offline, wait this long for it before 503.
    extension_wait: float = 30.0
    # Extension considered online if seen within this window...
    heartbeat_ttl: float = 15.0
    # ...or within this (longer) window while it is processing a job
    # (the extension's poll loop is blocked during fresh-chat navigation).
    busy_heartbeat_ttl: float = 120.0
    # Identical in-flight requests within this window share the same job
    # (protects against SDK retries double-firing the web UI).
    dedup_window: float = 2.5
    # Max duration of a long-poll on GET /job?wait=N
    max_long_poll: float = 25.0
    # Stats are flushed to disk at most every N seconds.
    stats_flush_interval: float = 2.0
    log_level: str = "info"

    @property
    def config_file(self) -> Path:
        return self.data_dir / "config.json"

    @property
    def stats_file(self) -> Path:
        return self.data_dir / "stats.json"

    @classmethod
    def from_env(cls, **overrides) -> "Settings":
        values = dict(
            host=os.environ.get("NEMAPI_HOST", DEFAULT_HOST),
            port=_env_int("NEMAPI_PORT", DEFAULT_PORT),
            data_dir=Path(os.environ.get("NEMAPI_DATA_DIR", PROXY_ROOT / "data")),
            web_dir=Path(os.environ.get("NEMAPI_WEB_DIR", PROXY_ROOT / "web")),
            job_timeout=_env_float("NEMAPI_JOB_TIMEOUT", 900.0),
            max_continuations=_env_int("NEMAPI_MAX_CONTINUATIONS", 4),
            extension_wait=_env_float("NEMAPI_EXTENSION_WAIT", 30.0),
            heartbeat_ttl=_env_float("NEMAPI_HEARTBEAT_TTL", 15.0),
            log_level=os.environ.get("NEMAPI_LOG_LEVEL", "info"),
        )
        values.update(overrides)
        return cls(**values)
