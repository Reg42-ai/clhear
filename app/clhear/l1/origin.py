# Copyright (C) 2026 Reg42 AI
# This file is part of CLHEAR. See LICENSE (AGPL-3.0-only).
"""Worker-owned test-origin classification; never rewrite publisher originals.

Only explicit fixture identities are classified automatically. A missing review
does not certify publisher provenance: artifact and inventory checks own that.
"""
from __future__ import annotations

import os
from datetime import datetime, timezone

import sqlalchemy as sa

from app.clhear.l1.models import BigId, Json, L1_SCHEMA, sources

metadata = sa.MetaData(schema=L1_SCHEMA)
origin_reviews = sa.Table(
    "source_origin_reviews", metadata,
    sa.Column("id", BigId, sa.Identity(), primary_key=True),
    sa.Column("source_key", sa.Text, nullable=False, unique=True),
    sa.Column("origin", sa.Text, nullable=False),
    sa.Column("evidence", Json, nullable=False),
    sa.Column("reviewed_at", sa.DateTime(timezone=True), nullable=False),
)
TEST_PREFIXES = ("synthetic/", "dummy/", "test/", "fixture/")


def is_test_source(source) -> bool:
    data = source._mapping if hasattr(source, "_mapping") else source
    def value(name):
        return data.get(name, "") if isinstance(data, dict) or hasattr(data, "get") else getattr(data, name, "")
    key = value("key") or value("source_key")
    return (str(key).lower().startswith(TEST_PREFIXES)
            or str(value("issuer")).lower() in {"synthetic", "test fixture", "clhear test fixture"})


def production_worker() -> bool:
    return (os.environ.get("CLHEAR_HTTP_MODE") == "live"
            or os.environ.get("CLHEAR_ARTIFACT_STORE") == "s3"
            or bool(os.environ.get("AWS_EXECUTION_ENV")))


def corpus_sources_predicate():
    """SQL equivalent for projections; test bodies never leave the record."""
    return sa.and_(
        *(~sa.func.lower(sources.c.key).startswith(prefix) for prefix in TEST_PREFIXES),
        sa.func.lower(sources.c.issuer).not_in(["synthetic", "test fixture", "clhear test fixture"]),
    )


def reconcile_origins(engine) -> dict:
    """Idempotently record known test identities, retaining every existing row."""
    from app.clhear.platform import audit
    with engine.begin() as conn:
        inspector = sa.inspect(conn)
        if not inspector.has_table(origin_reviews.name, schema=L1_SCHEMA if conn.dialect.name == "postgresql" else None):
            return {"status": "migration_required", "classified": 0}
        found = list(conn.execute(sa.select(sources).where(~corpus_sources_predicate())).mappings())
        reviewed = set(conn.execute(sa.select(origin_reviews.c.source_key)).scalars())
        count = 0
        for source in found:
            if source["key"] in reviewed:
                continue
            values = {"source_key": source["key"], "origin": "test",
                      "evidence": {"method": "explicit_fixture_identity_v1", "source_id": source["id"]},
                      "reviewed_at": datetime.now(timezone.utc)}
            if conn.dialect.name == "postgresql":
                from sqlalchemy.dialects.postgresql import insert
            else:
                from sqlalchemy.dialects.sqlite import insert
            result = conn.execute(insert(origin_reviews).values(**values).on_conflict_do_nothing(index_elements=["source_key"]))
            if result.rowcount:
                audit.log(conn, "l1.origin.classified", resource=source["key"],
                          detail={"origin": "test", "method": "explicit_fixture_identity_v1"})
                count += 1
    return {"status": "completed", "classified": count, "excluded_test_sources": len(found)}
