# Copyright (C) 2026 Reg42 AI
# This file is part of CLHEAR. See LICENSE (AGPL-3.0-only).
"""Compile a private L1 release from explicitly allowed corpus tables.

Called by L0 publication only. Never copy the operational database (sessions,
model prompts, credentials and unrelated layers are not release artifacts).
"""
import os
from pathlib import Path

import sqlalchemy as sa

from app.clhear.db import make_engine
from app.clhear.l1 import inventory, models, permissions, translation
from app.clhear.l1.translation_models import TABLES as ENGLISH_TABLES
from app.clhear.l1.origin import corpus_sources_predicate, production_worker

TABLE_NAMES = ("source_families", "sources", "family_members", "source_versions",
               "doc_nodes", "clauses", "clause_annotations", "citations", "rights_records")


def current_bindings(conn, *, corpus_only=False) -> list[dict]:
    query = (sa.select(
        models.sources.c.key.label("source_key"), models.source_versions.c.id.label("source_version_id"),
        models.source_versions.c.content_hash,
    ).join(models.source_versions, models.source_versions.c.source_id == models.sources.c.id)
      .where(models.source_versions.c.status == "in_force"))
    if corpus_only or production_worker():
        query = query.where(corpus_sources_predicate())
    return [dict(r) for r in conn.execute(query.order_by(models.sources.c.key, models.source_versions.c.id)).mappings()]


def compile_snapshot(engine, destination: Path, *, source_keys=None) -> dict:
    """Read one repeatable database view and publish no unapproved text.

    ``source_keys`` limits the corpus to one scope. Unset copies every corpus
    source, which is what an accepted L1 release does.
    """
    destination.parent.mkdir(parents=True, exist_ok=True)
    descriptor = os.open(destination, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    os.close(descriptor)
    target = make_engine(f"sqlite:///{destination}")
    try:
        with engine.connect() as connection:
            if engine.dialect.name == "postgresql":
                connection = connection.execution_options(isolation_level="REPEATABLE READ")
            with connection.begin():
                if engine.dialect.name == "postgresql":
                    connection.exec_driver_sql("SET TRANSACTION READ ONLY")
                elif engine.dialect.name == "sqlite":
                    connection.exec_driver_sql("BEGIN")
                bindings = current_bindings(connection, corpus_only=True)
                source_rows = list(connection.execute(sa.select(models.sources).where(corpus_sources_predicate())).mappings())
                if source_keys is not None:
                    chosen = set(source_keys)
                    bindings = [row for row in bindings if row["source_key"] in chosen]
                    source_rows = [row for row in source_rows if row["key"] in chosen]
                version_ids = [b["source_version_id"] for b in bindings] or ([-1] if source_keys is not None else [])
                source_ids = [s["id"] for s in source_rows] or ([-1] if source_keys is not None else [])
                family_ids = [s["family_id"] for s in source_rows] or ([-1] if source_keys is not None else [])
                for source in source_rows:
                    if permissions.required_for(source):
                        decisions = [permissions.decision(connection, source["key"], op)
                                     for op in ("display_internal", "display_public")]
                        has_version = any(b["source_key"] == source["key"] for b in bindings)
                        if has_version and not permissions.decision(connection, source["key"], "store")["allowed"]:
                            raise PermissionError(f"No permitted release storage for {source['key']}")
                        if has_version and any(not permissions.decision(connection, source["key"], op)["allowed"]
                                               for op in ("acquire", "parse")):
                            raise PermissionError(f"Publisher permission is unresolved for release of {source['key']}")
                        if has_version and not any(d["allowed"] for d in decisions):
                            raise PermissionError(f"No permitted release audience for {source['key']}")
                with target.begin() as output:
                    models.metadata.create_all(output)
                    counts = {}
                    for name in TABLE_NAMES:
                        table = getattr(models, name)
                        query = sa.select(*[
                            sa.cast(sa.null(), column.type).label(column.name)
                            if column.name.startswith("embedding") or column.name == "embedded_at" else column
                            for column in table.c
                        ])
                        if name == "sources":
                            query = query.where(table.c.id.in_(source_ids))
                        elif name == "source_families":
                            query = query.where(sa.or_(
                                table.c.id.in_(family_ids),
                                table.c.id.in_(sa.select(models.family_members.c.family_id).where(models.family_members.c.source_id.in_(source_ids))),
                            ))
                        elif name == "source_versions":
                            query = query.where(table.c.id.in_(version_ids))
                        elif "source_version_id" in table.c:
                            query = query.where(table.c.source_version_id.in_(version_ids))
                        elif "clause_id" in table.c:
                            clause_ids = sa.select(models.clauses.c.id).where(models.clauses.c.source_version_id.in_(version_ids))
                            query = query.where(table.c.clause_id.in_(clause_ids))
                        elif "from_clause_id" in table.c:
                            clause_ids = sa.select(models.clauses.c.id).where(models.clauses.c.source_version_id.in_(version_ids))
                            query = query.where(table.c.from_clause_id.in_(clause_ids))
                        elif "source_id" in table.c:
                            query = query.where(table.c.source_id.in_(source_ids))
                        rows = [dict(r) for r in connection.execute(query).mappings()]
                        if rows:
                            output.execute(table.insert(), rows)
                        counts[name] = len(rows)
                    english_queries = translation.snapshot_queries(connection, version_ids)
                    for table in ENGLISH_TABLES:
                        rows = [dict(row) for row in connection.execute(english_queries[table.name]).mappings()]
                        if rows:
                            output.execute(table.insert(), rows)
                        counts[table.name] = len(rows)
                    # English views need the exact original-proof and rights
                    # evidence for readback. Reuse the private viewer scrubber.
                    from app.clhear.l1.viewer_snapshot import _clean_row
                    for table in (permissions.source_permissions, inventory.inventory_snapshots, inventory.inventory_audits):
                        table.create(output, checkfirst=True)
                        query = sa.select(table)
                        if table is permissions.source_permissions:
                            query = query.where(table.c.source_key.in_([s["key"] for s in source_rows]))
                        rows = [_clean_row(table, row) for row in connection.execute(query).mappings()]
                        if rows:
                            output.execute(table.insert(), rows)
                        counts[table.name] = len(rows)
        destination.chmod(0o600)
        return {"bindings": bindings, "counts": counts, "audience": "restricted-reviewers",
                "table_allowlist": list(counts)}
    except Exception:
        target.dispose()
        destination.unlink(missing_ok=True)
        raise
    finally:
        target.dispose()


def verify_snapshot_bindings(path: Path, expected: list[dict]) -> bool:
    path = Path(path)
    if not path.is_file():
        return False
    target = make_engine(f"sqlite:///{path.resolve().as_uri()}?mode=ro&uri=true")
    try:
        with target.connect() as conn:
            return current_bindings(conn) == expected
    finally:
        target.dispose()
