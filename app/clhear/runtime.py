# Copyright (C) 2026 Reg42 AI
# This file is part of CLHEAR. See LICENSE (AGPL-3.0-only).
"""Process-wide database handle."""
from __future__ import annotations

from sqlalchemy.engine import Engine

from app.clhear.db import dispose_engine, get_engine, run_migrations
from app.clhear.settings import get_settings

_ready = False


def reset() -> None:
    """Drop the cached engine so the next call reads the current environment."""
    global _ready
    _ready = False
    dispose_engine()
    get_settings.cache_clear()


def engine() -> Engine:
    global _ready
    handle = get_engine()
    if not _ready:
        run_migrations(handle)
        _ready = True
    return handle
