# Copyright (C) 2026 Reg42 AI
# This file is part of CLHEAR. See LICENSE (AGPL-3.0-only).
"""Fixed-window rate limits shared by every web task (``identity.rate_windows``).

A bucket names who is limited ("key:<app id>", "account:<user id>",
"ip:<hash>", "email:<hash>"). Counters live in the identity store so two
tasks cannot each grant the full allowance. When no identity store is
configured, an in-process counter keeps local and test runs honest.
"""
from __future__ import annotations

import threading
import time
from datetime import datetime, timezone

import sqlalchemy as sa

from app.clhear import identity
from app.clhear.identity_models import rate_windows


class RateLimited(Exception):
    def __init__(self, retry_after: int):
        super().__init__(f"rate limited; retry after {retry_after}s")
        self.retry_after = retry_after


_local: dict[tuple[str, int], int] = {}
_local_lock = threading.Lock()


def _window(now: float, seconds: int) -> int:
    return int(now // seconds) * seconds


def hit(bucket: str, *, limit: int, window_s: int, now: float | None = None) -> int:
    """Count one request; raise ``RateLimited`` once the window's allowance is spent."""
    now = time.time() if now is None else now
    start = _window(now, window_s)
    retry_after = max(1, int(start + window_s - now))
    if not identity.configured():
        with _local_lock:
            count = _local.get((bucket, start), 0) + 1
            _local[(bucket, start)] = count
            for key in [k for k in _local if k[1] < start - window_s]:
                _local.pop(key, None)
    else:
        count = _shared_hit(bucket, start)
    if count > limit:
        raise RateLimited(retry_after)
    return count


def _shared_hit(bucket: str, start: int) -> int:
    at = datetime.fromtimestamp(start, timezone.utc)
    engine = identity.engine()
    with engine.begin() as conn:
        if engine.dialect.name == "postgresql":
            from sqlalchemy.dialects.postgresql import insert

            stmt = insert(rate_windows).values(bucket=bucket, window_start=at, count=1)
            stmt = stmt.on_conflict_do_update(index_elements=[rate_windows.c.bucket, rate_windows.c.window_start],
                                              set_={"count": rate_windows.c.count + 1}).returning(rate_windows.c.count)
            return int(conn.execute(stmt).scalar_one())
        updated = conn.execute(rate_windows.update().where(rate_windows.c.bucket == bucket, rate_windows.c.window_start == at)
                               .values(count=rate_windows.c.count + 1))
        if not updated.rowcount:
            conn.execute(rate_windows.insert().values(bucket=bucket, window_start=at, count=1))
        return int(conn.execute(sa.select(rate_windows.c.count).where(
            rate_windows.c.bucket == bucket, rate_windows.c.window_start == at)).scalar_one())


def prune(engine=None, *, older_than_s: int = 3600) -> int:
    engine = engine or identity.engine()
    cutoff = datetime.fromtimestamp(time.time() - older_than_s, timezone.utc)
    with engine.begin() as conn:
        return conn.execute(rate_windows.delete().where(rate_windows.c.window_start < cutoff)).rowcount or 0


def reset_local() -> None:
    with _local_lock:
        _local.clear()
