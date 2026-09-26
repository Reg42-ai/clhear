# Copyright (C) 2026 Reg42 AI
# This file is part of CLHEAR. See LICENSE (AGPL-3.0-only).
"""Engine factory + startup migration runner.

# ARCH: the host service applies numbered migrations at startup; this runner follows
# that convention (numbered modules in /migrations, recorded in
# l0_platform.schema_migrations) and should be swapped for the existing
# runner when this package moves into the host service.
"""
import importlib
import logging
import pkgutil

import sqlalchemy as sa
from sqlalchemy.engine import Engine

from app.clhear.models import L0_SCHEMA, metadata, schema_migrations
from app.clhear.settings import get_settings

log = logging.getLogger("clhear.db")

_engine: Engine | None = None


def all_schemas() -> tuple[str, ...]:
    """Every Postgres schema the record uses (one per layer + platform + community)."""
    from app.clhear.community_models import COMMUNITY_SCHEMA
    from app.clhear.conformance import CONFORMANCE_SCHEMA
    from app.clhear.derived_models import L2_SCHEMA, L3_SCHEMA, L4_SCHEMA, L5_SCHEMA, L6_SCHEMA
    from app.clhear.identity_models import IDENTITY_SCHEMA
    from app.clhear.l1.models import L1_SCHEMA
    from app.clhear.l7.models import L7_SCHEMA
    from app.clhear.l8.models import L8_SCHEMA

    return (
        L0_SCHEMA, L1_SCHEMA, L2_SCHEMA, L3_SCHEMA, L4_SCHEMA, L5_SCHEMA, L6_SCHEMA, L7_SCHEMA, L8_SCHEMA,
        COMMUNITY_SCHEMA, CONFORMANCE_SCHEMA, IDENTITY_SCHEMA,
    )


def make_engine(database_url: str) -> Engine:
    kwargs: dict = {"future": True}
    if not database_url.startswith("postgresql"):
        # SQLite has no schemas: translate `l0_platform.events` -> `events`.
        kwargs["execution_options"] = {
            "schema_translate_map": {schema: None for schema in all_schemas()}
        }
    return sa.create_engine(database_url, **kwargs)


def get_engine() -> Engine:
    global _engine
    if get_settings().clhear_preview_mode and _engine is not None and not _engine.get_execution_options().get("clhear_preview_readonly"):
        dispose_engine()
    if _engine is None:
        if get_settings().clhear_preview_mode:
            from app.clhear.preview import make_preview_engine

            _engine = make_preview_engine(get_settings())
        else:
            _engine = make_engine(get_settings().database_url)
    return _engine


def set_engine(engine: Engine) -> None:
    """Test hook."""
    global _engine
    _engine = engine


def dispose_engine() -> None:
    """Drop pooled connections and rebuild on next use (snapshot refresh)."""
    from app.clhear import snapshot_cache

    global _engine
    if _engine is not None:
        snapshot_cache.forget(_engine)
        _engine.dispose()
    _engine = None


def run_migrations(engine: Engine) -> list[int]:
    """Apply numbered migrations under one transaction and PostgreSQL lock.

    The transaction-scoped lock serializes the ledger read and every pending
    migration across fleet boots. PostgreSQL releases it on commit, rollback
    or connection loss; no session lock can leak into the connection pool.
    """
    with engine.begin() as conn:
        if engine.dialect.name == "postgresql":
            conn.execute(sa.text("SELECT pg_advisory_xact_lock(:key)"), {"key": 0x434C48454152})
            for schema in all_schemas():
                conn.execute(sa.text(f"CREATE SCHEMA IF NOT EXISTS {schema}"))
            # HLD v2 I7: pgvector lives beside the record (Aurora Postgres).
            try:
                # A failed optional extension statement aborts PostgreSQL's
                # transaction unless isolated behind a savepoint.
                with conn.begin_nested():
                    conn.execute(sa.text("CREATE EXTENSION IF NOT EXISTS vector"))
            except Exception:  # pragma: no cover - extension not available on this host
                log.warning("pgvector extension unavailable; embeddings stay JSON")
        schema_migrations.create(conn, checkfirst=True)
        # The audit ledger is L0 rails: data migrations that seed through record.write
        # (m0012 …) must already be able to account for themselves (HLD v2 §7.1).
        from app.clhear.platform.audit import audit_log

        audit_log.create(conn, checkfirst=True)
        applied = {row.version for row in conn.execute(sa.select(schema_migrations.c.version))}

        import migrations as migrations_pkg

        available = []
        for mod_info in pkgutil.iter_modules(migrations_pkg.__path__):
            if mod_info.name.startswith("m"):
                version = int(mod_info.name.split("_")[0][1:])
                available.append((version, mod_info.name))
        available.sort()

        newly_applied = []
        for version, name in available:
            if version in applied:
                continue
            module = importlib.import_module(f"migrations.{name}")
            from app.clhear.platform import audit

            token = audit.bind_actor(audit.system_actor(f"migration:{name}"))
            try:
                module.upgrade(conn)
                conn.execute(schema_migrations.insert().values(version=version, name=name))
            finally:
                audit.reset_actor(token)
            log.info("applied migration %s", name)
            newly_applied.append(version)
    return newly_applied


__all__ = ["get_engine", "set_engine", "make_engine", "run_migrations", "metadata"]
