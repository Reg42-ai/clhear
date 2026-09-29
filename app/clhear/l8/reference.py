# Copyright (C) 2026 Reg42 AI
# This file is part of CLHEAR. See LICENSE (AGPL-3.0-only).
"""L8 reference benchmark: what regulators found, advise and order, read from L1.

Not peer data. Every row is one in-force clause of a public guidance
publication or examination report, or the remediation an enforcement action
orders, quoted exactly:

* guidance sources (kind ``guidance``): each clause is a practice (a finding
  when the source is an examination report);
* enforcement sources (kind ``enforcement``): each clause that states an
  ordered or undertaken remediation ("is ordered to …", "agreed to …",
  "shall, within 30 days, …", corrective action) is a practice for the
  component it concerns.

A clause that is itself the basis of an obligation is the obligation, not a
practice. Its L3 block is the one whose name, purpose and required obligations
share the most words with the quote; a row that shares too little names no
block. Aggregates over member data keep the k-anonymity gate
(``l8.cohorts.K``); nothing here reads member data.
"""
from __future__ import annotations

import re

import sqlalchemy as sa
from sqlalchemy.engine import Connection, Engine

LABEL = "reference benchmark: regulator examination findings, guidance and ordered remediation; not peer data"
PRACTICE_KINDS = ("guidance", "enforcement")
# The remediation an enforcement action orders or the respondent undertakes. Generic drafting
# grammar: an order or undertaking, corrective action, or an obligation with a deadline.
REMEDIATION = re.compile(
    r"\b(?:ordered|required|directed|instructed|agreed|undert(?:ook|akes|aken)|consented|committed)\s+to\b"
    r"|\bcorrective action|\bremedia(?:l|tion|te)\b"
    r"|\b(?:shall|must|will)\b[^.;]{0,80}\b(?:within \w+ (?:days?|weeks?|months?)|by \d)", re.I)
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

    scope = active()
    if scope:
        declared = list(role("reference"))
        if declared:
            return declared
        rows = conn.execute(sa.select(sources.c.key).where(sources.c.key.in_(scope["sources"]),
                                                           sources.c.kind.in_(PRACTICE_KINDS))).all()
        return sorted(r.key for r in rows)
    rows = conn.execute(sa.select(sources.c.key, sources.c.topics).where(sources.c.kind == "guidance")).all()
    return sorted(key for key, topics in rows if "examinations" in (topics or []))


def _blocks(conn: Connection, source_keys: list[str]) -> list[dict]:
    from app.clhear.l1.models import clauses, source_versions, sources

    from app.clhear.derived_models import asserts, obligations

    rows = [dict(r) for r in conn.execute(
        sa.select(sources.c.key, sources.c.kind, sources.c.canonical_url, sources.c.topics,
                  source_versions.c.version_label, clauses.c.id, clauses.c.ref, clauses.c.text, clauses.c.span_start,
                  clauses.c.span_end)
        .join(source_versions, source_versions.c.source_id == sources.c.id)
        .join(clauses, clauses.c.source_version_id == source_versions.c.id)
        .where(sources.c.key.in_(source_keys), source_versions.c.status == "in_force", clauses.c.valid_to.is_(None))
        .order_by(sources.c.key, clauses.c.ordering)
    ).mappings()]
    # The clauses live obligations are read from are those obligations, not practices.
    basis = {r[0] for r in conn.execute(
        sa.select(asserts.c.clause_id).join(obligations, obligations.c.id == asserts.c.obligation_id)
        .where(asserts.c.valid_to.is_(None), obligations.c.status.in_(("derived", "validated"))))}
    leaves = _leaves(rows)
    kept = []
    for r in rows:
        text = r["text"] or ""
        if r["id"] not in leaves or r["id"] in basis or len(" ".join(text.split())) < MIN_QUOTE_CHARS:
            continue
        if r["kind"] == "enforcement" and not REMEDIATION.search(text):
            continue
        kept.append(r)
    return kept


def _leaves(rows: list[dict]) -> set:
    """Ids of the clauses that contain no other clause: a parent repeats its children's text."""
    leaves = set()
    by_source: dict[str, list[dict]] = {}
    for r in rows:
        by_source.setdefault(r["key"], []).append(r)
    for group in by_source.values():
        spanned = sorted((r for r in group if r["span_start"] is not None and r["span_end"] is not None),
                         key=lambda r: (r["span_start"], -r["span_end"]))
        for i, r in enumerate(spanned):
            following = spanned[i + 1] if i + 1 < len(spanned) else None
            if following is None or following["span_start"] >= r["span_end"]:
                leaves.add(r["id"])
        rest = [r for r in group if r["span_start"] is None or r["span_end"] is None]
        leaves |= {r["id"] for r in rest
                   if not any(o is not r and o["text"] and o["text"] != r["text"] and o["text"] in (r["text"] or "")
                              for o in rest)}
    return leaves


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
    from app.clhear.evidence import whole

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
        remediation = row["kind"] == "enforcement"
        out.append({
            "id": f"REF:{row['key']}#{row['ref']}",
            "label": LABEL,
            "kind": "remediation" if remediation else "finding" if examination else "practice",
            "finding": quote if examination and not remediation else "",
            "practice": "" if examination and not remediation else quote,
            "quote": quote,
            "evidence": whole({"id": row["id"], "source_key": row["key"], "ref": row["ref"], "text": row["text"] or ""}),
            **({"ordered_in": {"source_key": row["key"], "clause_ref": row["ref"]}} if remediation else {}),
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
