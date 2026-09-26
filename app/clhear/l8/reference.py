# Copyright (C) 2026 Reg42 AI
# This file is part of CLHEAR. See LICENSE (AGPL-3.0-only).
"""L8 reference benchmark: what regulators found and what they advise, read from L1.

Not peer data. Every row is one in-force block of a public examination report
or guidance publication (the scope's ``reference`` sources), quoted exactly.
Its L3 block is the one whose name, purpose and required obligations share the
most words with the quote; a row that shares too little names no block.
Aggregates over member data keep the k-anonymity gate (``l8.cohorts.K``);
nothing here reads member data.
"""
from __future__ import annotations

import re

import sqlalchemy as sa
from sqlalchemy.engine import Connection, Engine

LABEL = "reference benchmark: regulator examination findings and guidance; not peer data"
MIN_QUOTE_CHARS = 20
MIN_BLOCK_SIMILARITY = 0.08
_STOP = frozenset("""the and for with that this from are was were been has have had not but any all its their which
such other than into when where who whom what will shall may must can could should would about also each
those these them they there here more most some only very over under upon between within without via per
our your you his her him she he it is be as of to in on at by or an a if so do does did""".split())


def _words(text: str) -> set[str]:
    return {w for w in re.findall(r"[a-z]{3,}", (text or "").lower()) if w not in _STOP}


def reference_source_keys(conn: Connection) -> list[str]:
    from app.clhear.l1.models import sources
    from app.clhear.l1.scopes import active, role

    if active():
        return list(role("reference"))
    rows = conn.execute(sa.select(sources.c.key, sources.c.topics).where(sources.c.kind == "guidance")).all()
    return sorted(key for key, topics in rows if "examinations" in (topics or []))


def _blocks(conn: Connection, source_keys: list[str]) -> list[dict]:
    from app.clhear.l1.models import clauses, source_versions, sources

    rows = conn.execute(
        sa.select(sources.c.key, sources.c.canonical_url, sources.c.topics, source_versions.c.version_label,
                  clauses.c.id, clauses.c.ref, clauses.c.text)
        .join(source_versions, source_versions.c.source_id == sources.c.id)
        .join(clauses, clauses.c.source_version_id == source_versions.c.id)
        .where(sources.c.key.in_(source_keys), source_versions.c.status == "in_force", clauses.c.valid_to.is_(None))
        .order_by(sources.c.key, clauses.c.ordering)
    ).mappings()
    return [dict(r) for r in rows if len(" ".join((r["text"] or "").split())) >= MIN_QUOTE_CHARS]


def _block_vocabulary(conn: Connection) -> list[tuple[str, str, set[str]]]:
    from app.clhear.derived_models import blocks, obligations, requires

    statements: dict[str, list[str]] = {}
    for r in conn.execute(sa.select(requires.c.block_id, obligations.c.statement)
                          .join(obligations, obligations.c.id == requires.c.obligation_id)
                          .where(requires.c.valid_to.is_(None), obligations.c.status.in_(("derived", "validated")))):
        statements.setdefault(r.block_id, []).append(r.statement or "")
    out = []
    for r in conn.execute(sa.select(blocks.c.id, blocks.c.name, blocks.c.purpose).where(blocks.c.valid_to.is_(None))):
        vocabulary = _words(" ".join([r.name or "", r.purpose or "", *statements.get(r.id, [])]))
        if vocabulary:
            out.append((r.id, r.name or "", vocabulary))
    return out


def derived_reference_rows(conn: Connection, source_keys: list[str] | None = None) -> list[dict]:
    blocks = _block_vocabulary(conn)
    out = []
    for row in _blocks(conn, reference_source_keys(conn) if source_keys is None else source_keys):
        quote = " ".join((row["text"] or "").split())
        words = _words(quote)
        best, score = None, 0.0
        for block_id, name, vocabulary in blocks:
            overlap = len(words & vocabulary) / len(words | vocabulary) if words else 0.0
            if overlap > score or (overlap == score and best is not None and block_id < best[0]):
                best, score = (block_id, name), overlap
        matched = best if best is not None and score >= MIN_BLOCK_SIMILARITY else None
        examination = "examinations" in (row["topics"] or [])
        out.append({
            "id": f"REF:{row['key']}#{row['ref']}",
            "label": LABEL,
            "kind": "finding" if examination else "practice",
            "finding": quote if examination else "",
            "practice": "" if examination else quote,
            "quote": quote,
            "source": {"source_key": row["key"], "clause_ref": row["ref"], "clause_id": row["id"],
                       "url": row["canonical_url"], "version_label": row["version_label"]},
            "block_id": matched[0] if matched else None,
            "block_name": matched[1] if matched else "",
            "similarity": round(score, 4),
            "peer_data": False,
        })
    return out


def reference_rows(engine: Engine, *, blueprint: dict | None = None, source_keys: list[str] | None = None) -> list[dict]:
    on_blueprint = {item.get("block_id") for item in (blueprint or {}).get("items") or []}
    with engine.connect() as conn:
        rows = derived_reference_rows(conn, source_keys)
    for row in rows:
        row["on_blueprint"] = (row["block_id"] in on_blueprint) if blueprint is not None and row["block_id"] else (
            False if blueprint is not None else None)
    return rows
