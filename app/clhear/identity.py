# Copyright (C) 2026 Reg42 AI
# This file is part of CLHEAR. See LICENSE (AGPL-3.0-only).
"""The web tier's writable identity store.

The corpus is served from a read-only snapshot, so accounts, API keys, usage
and rate windows cannot live there: a copy on local disk is lost when a task
is replaced and is not shared between tasks. When
``CLHEAR_IDENTITY_DATABASE_URL`` is set, these writes go to Aurora through a
role granted only the identity tables. Without it (workers, tests, local dev)
the ordinary engine is used.
"""
from __future__ import annotations

import os
import threading

from sqlalchemy.engine import Engine

IDENTITY_URL_ENV = "CLHEAR_IDENTITY_DATABASE_URL"
ROLE = "clhear_web"
PASSWORD_PARAMETER = "/clhear/web/IDENTITY_DB_PASSWORD"

_engine: Engine | None = None
_lock = threading.Lock()


def configured() -> bool:
    return bool(os.environ.get(IDENTITY_URL_ENV, "").strip())


def engine() -> Engine:
    global _engine
    if not configured():
        from app.clhear.db import get_engine

        return get_engine()
    with _lock:
        if _engine is None:
            url = os.environ[IDENTITY_URL_ENV]
            if url.startswith("postgresql"):
                import sqlalchemy as sa

                _engine = sa.create_engine(url, future=True, pool_pre_ping=True, pool_size=5, max_overflow=5,
                                           pool_recycle=900)
            else:
                from app.clhear.db import make_engine

                _engine = make_engine(url)
        return _engine


def writable() -> bool:
    """True when account and key writes land somewhere durable."""
    from app.clhear.community_writes import snapshot_readonly

    return configured() or not snapshot_readonly()


def reset() -> None:
    """Test hook."""
    global _engine
    with _lock:
        if _engine is not None:
            _engine.dispose()
        _engine = None
