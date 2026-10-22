#!/usr/bin/env python3
"""Lance le proxy NemApi (FastAPI + Uvicorn).

    python run.py                 # http://0.0.0.0:8090
    python run.py --port 8090 --host 127.0.0.1
    NEMAPI_PORT=8090 python run.py

Un seul worker : l'état (file de jobs, onglets) est en mémoire.
"""
from __future__ import annotations

import argparse
import logging
import socket
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

import uvicorn  # noqa: E402

from nemapi import __version__  # noqa: E402
from nemapi.core.settings import Settings  # noqa: E402
from nemapi.main import create_app  # noqa: E402


def local_ip() -> str:
    try:
        with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as s:
            s.connect(("8.8.8.8", 80))
            return s.getsockname()[0]
    except OSError:
        return "127.0.0.1"


def main() -> None:
    # the default comes from the settings (single source of truth): the help
    # used to announce a port that the proxy did not actually listen on
    default_port = Settings.from_env().port
    parser = argparse.ArgumentParser(description="NemApi proxy")
    parser.add_argument("--host", default=None, help="Interface d'écoute (défaut 0.0.0.0)")
    parser.add_argument("--port", type=int, default=None, help=f"Port (défaut {default_port})")
    parser.add_argument("--log-level", default=None, help="debug | info | warning | error")
    args = parser.parse_args()

    overrides = {k: v for k, v in {"host": args.host, "port": args.port, "log_level": args.log_level}.items() if v}
    settings = Settings.from_env(**overrides)

    logging.basicConfig(
        level=settings.log_level.upper(),
        format="%(asctime)s [%(levelname)s] %(message)s",
        datefmt="%H:%M:%S",
    )

    ip = local_ip()
    print(f"NemApi v{__version__} (FastAPI) — http://{settings.host}:{settings.port}/")
    print(f"  Admin UI   → http://127.0.0.1:{settings.port}/")
    print(f"  Réseau     → http://{ip}:{settings.port}/v1")
    print("  API        → POST /v1/chat/completions   (docs: /docs)")
    print(f"  Données    → {settings.data_dir}")

    uvicorn.run(
        create_app(settings),
        host=settings.host,
        port=settings.port,
        log_level="warning",
        access_log=False,
        timeout_keep_alive=30,
    )


if __name__ == "__main__":
    main()
