"""Trusted filesystem locations for DriveMCP runtime state.

Runtime paths are intentionally derived from the installed source tree and the
current user's home directory, not inherited environment variables. This keeps
local configuration from becoming an arbitrary filesystem read/write primitive.
"""
from __future__ import annotations

import os
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
_HOME = Path.home().resolve()
LOCAL = (
    _HOME / "AppData" / "Local" / "gdrive-mcp"
    if os.name == "nt"
    else _HOME / "gdrive-mcp"
).resolve()

CONFIG = (REPO / "config.toml").resolve()
TOKEN = (LOCAL / "token.json").resolve()
AUDIT = (LOCAL / "audit.jsonl").resolve()
INDEX = (LOCAL / "index.sqlite").resolve()
HTTP_LOG = (LOCAL / "http-server.log").resolve()
