"""Application container holding every service (one instance per app)."""
from __future__ import annotations

import time
from dataclasses import dataclass, field

from fastapi import Request

from ..services.config_store import ConfigStore
from ..services.extension import ExtensionState
from ..services.integrations import IntegrationManager
from ..services.jobs import JobManager
from ..services.logs import LogBuffer
from ..services.sessions import SessionManager
from ..services.stats import StatsStore
from .settings import Settings


@dataclass
class AppContext:
    settings: Settings
    logs: LogBuffer
    config: ConfigStore
    stats: StatsStore
    extension: ExtensionState
    jobs: JobManager
    sessions: SessionManager
    integrations: IntegrationManager
    started_at: float = field(default_factory=time.time)

    def extension_connected(self) -> bool:
        return self.extension.is_connected(self.jobs.has_active())

    @classmethod
    def build(cls, settings: Settings) -> "AppContext":
        settings.data_dir.mkdir(parents=True, exist_ok=True)
        logs = LogBuffer()
        config = ConfigStore(settings.config_file, logs)
        stats = StatsStore(settings.stats_file, logs, settings.stats_flush_interval)
        extension = ExtensionState(
            logs,
            heartbeat_ttl=settings.heartbeat_ttl,
            busy_heartbeat_ttl=settings.busy_heartbeat_ttl,
            auto_config=config.get("auto_config"),
        )
        jobs = JobManager(logs, dedup_window=settings.dedup_window, max_continuations=settings.max_continuations)
        sessions = SessionManager(extension, logs, path=settings.data_dir / "sessions.json")
        integrations = IntegrationManager(config, logs)
        return cls(
            settings=settings, logs=logs, config=config, stats=stats,
            extension=extension, jobs=jobs, sessions=sessions, integrations=integrations,
        )


def get_ctx(request: Request) -> AppContext:
    return request.app.state.ctx
