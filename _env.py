"""Load .env.local into the process environment, without a dependency and without overriding.

Anything already exported wins, so a one-off on the command line beats the file:

    LOGIN_PASSWORD=other python3 login.py

Looked for in this order, first hit wins:
    $TOOLKIT_ENV_FILE
    <repo root>/.env.local
    <script folder>/.env.local
"""

from __future__ import annotations

import os
import pathlib


def load_env_file(verbose: bool = True) -> pathlib.Path | None:
    here = pathlib.Path(__file__).resolve().parent
    candidates = []
    explicit = os.environ.get("TOOLKIT_ENV_FILE")
    if explicit:
        candidates.append(pathlib.Path(explicit).expanduser())
    candidates.append(here.parent / ".env.local")
    candidates.append(here / ".env.local")

    for path in candidates:
        if not path.is_file():
            continue
        loaded = 0
        for raw in path.read_text(encoding="utf-8").splitlines():
            line = raw.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            key, value = line.split("=", 1)
            key = key.strip()
            value = value.strip().strip('"').strip("'")
            if not key or not value:
                continue
            if key not in os.environ:  # exported values win
                os.environ[key] = value
                loaded += 1
        if verbose:
            print(f"env: read {loaded} values from {path}", flush=True)
        return path
    return None
