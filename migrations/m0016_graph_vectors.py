# Copyright (C) 2026 Reg42 AI
# This file is part of CLHEAR. See LICENSE (AGPL-3.0-only).
"""0016 — HLD v2 I7 projections: query graph log + pgvector clause index.

* ``l0_platform.graph_projections`` — append-only log of every graph / vector
  index rebuild (backend, release, counts, checksum)
* ``clauses.embedding_hash`` / ``clauses.embedded_at`` — so a rebuild only
  re-embeds clauses whose text changed since
* Aurora: ``clauses.embedding`` becomes ``vector(1024)`` with an HNSW cosine
  index when the pgvector extension is available; SQLite keeps packed float32.
"""
import sqlalchemy as sa
from sqlalchemy import text
from sqlalchemy.engine import Connection

from app.clhear.l1.models import L1_SCHEMA, clauses
from app.clhear.models import L0_SCHEMA, graph_projections


def _columns(conn: Connection, table: str, schema: str | None) -> dict[str, str]:
    insp = sa.inspect(conn)
    return {c["name"]: str(c["type"]).lower() for c in insp.get_columns(table, schema=schema)}


def upgrade(conn: Connection) -> None:
    pg = conn.engine.dialect.name == "postgresql"
    if pg:
        conn.execute(text(f"CREATE SCHEMA IF NOT EXISTS {L0_SCHEMA}"))
    graph_projections.create(conn, checkfirst=True)

    schema = L1_SCHEMA if pg else None
    table = f"{L1_SCHEMA}.clauses" if pg else "clauses"
    cols = _columns(conn, "clauses", schema)
    if "embedding_hash" not in cols:
        conn.execute(text(f"ALTER TABLE {table} ADD COLUMN embedding_hash TEXT"))
    if "embedded_at" not in cols:
        conn.execute(text(f"ALTER TABLE {table} ADD COLUMN embedded_at TIMESTAMP" + (" WITH TIME ZONE" if pg else "")))
    if "embedding" not in cols:
        conn.execute(text(f"ALTER TABLE {table} ADD COLUMN embedding " + ("TEXT" if pg else "BLOB")))
        cols["embedding"] = "text"

    if pg:
        has_vector = conn.execute(text("SELECT 1 FROM pg_extension WHERE extname = 'vector'")).first() is not None
        if not has_vector:
            try:
                # Optional extension failure must not abort the surrounding
                # migration transaction; JSON/text embeddings remain valid.
                with conn.begin_nested():
                    conn.execute(text("CREATE EXTENSION IF NOT EXISTS vector"))
                has_vector = True
            except Exception:  # pragma: no cover - host without pgvector
                has_vector = False
        if has_vector and not cols["embedding"].startswith("vector"):
            # existing values (if any) are JSON/text lists, which is also pgvector's text form
            conn.execute(text(f"ALTER TABLE {table} ALTER COLUMN embedding TYPE vector(1024) "
                              "USING CASE WHEN embedding IS NULL THEN NULL ELSE embedding::text::vector END"))
        if has_vector:
            conn.execute(text(f"CREATE INDEX IF NOT EXISTS clauses_embedding_hnsw ON {table} "
                              "USING hnsw (embedding vector_cosine_ops)"))
        conn.execute(text(f"CREATE INDEX IF NOT EXISTS clauses_embedding_model_idx ON {table} (embedding_model)"))
    else:
        conn.execute(text("CREATE INDEX IF NOT EXISTS clauses_embedding_model_idx ON clauses (embedding_model)"))
    _ = clauses  # the model carries the new columns for fresh databases
