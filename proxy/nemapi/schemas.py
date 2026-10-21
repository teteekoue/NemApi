"""Request bodies (lenient: unknown fields are accepted and ignored)."""
from __future__ import annotations

from typing import Any, Dict, List, Optional

from pydantic import BaseModel, ConfigDict, Field


class _Lenient(BaseModel):
    model_config = ConfigDict(extra="allow")


class ChatCompletionRequest(_Lenient):
    model: Optional[str] = None
    provider: Optional[str] = None
    messages: Optional[List[Any]] = None
    tools: Optional[Any] = None
    functions: Optional[Any] = None
    stream: Optional[bool] = None
    # NemApi extension: per-request override of the global fresh-chat setting.
    fresh_chat: Optional[bool] = None


class JobResult(_Lenient):
    jobId: Optional[str] = None
    action: str = "error"
    result: Optional[str] = ""
    error: Optional[str] = ""
    typed: Optional[bool] = None  # extension: prompt submitted before the error?
    # diagnostics: how the extension read the answer ("react" = original
    # markdown; "html"/"dom" = rebuilt from the rendered page, lossy) and
    # whether the MAIN-world page bridge was present in the tab
    source: Optional[str] = None
    bridge: Optional[bool] = None
    partial: Optional[bool] = None  # the extension stopped waiting before the answer ended


class ExtensionLog(_Lenient):
    message: Any = ""
    level: Optional[str] = "info"


class TabsReport(_Lenient):
    tabs: List[Dict[str, Any]] = Field(default_factory=list)
    busy: bool = False


class TargetTab(_Lenient):
    provider: Optional[str] = None
    targetTabId: Optional[int] = None


class SettingsUpdate(_Lenient):
    stream_enabled: Optional[bool] = None
    auto_config: Optional[bool] = None
    fresh_chat: Optional[bool] = None
    premium_md: Optional[bool] = None


class IntegrationUpdate(_Lenient):
    enabled: Optional[bool] = None
    features: Optional[Dict[str, Optional[bool]]] = None


class NewApiKey(_Lenient):
    name: Optional[str] = ""
