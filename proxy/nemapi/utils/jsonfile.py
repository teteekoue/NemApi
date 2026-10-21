"""Small helpers for safe JSON persistence."""
from __future__ import annotations

import json
import os
import tempfile
from pathlib import Path
from typing import Any


def read_json(path: Path) -> Any:
    with open(path, "r", encoding="utf-8") as f:
        return json.load(f)


def write_json_atomic(path: Path, data: Any) -> None:
    """Write JSON to a temp file then atomically replace the target.

    A crash mid-write can never leave a truncated/corrupted config file.
    """
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(prefix=f".{path.name}.", suffix=".tmp", dir=str(path.parent))
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            json.dump(data, f, indent=2, ensure_ascii=False)
        os.replace(tmp, path)
    except BaseException:
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise
