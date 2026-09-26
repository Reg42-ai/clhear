# Copyright (C) 2026 Reg42 AI
# This file is part of CLHEAR. See LICENSE (AGPL-3.0-only).
"""Hybrid retrieval over the unified search-unit store (Cerebras lessons).

Build side (called from the pipeline's `index` stage): every PUBLIC clause of
the newly in-force version lands in `search_units` in its DISTILLED form
(short name + path + category/topics + text), plus paragraph-grain "burst"
units (one paragraph with its clause heading prepended) above a signal
threshold. An FTS5 mirror gives BM25 (full-text + rarity) when available.

Query side: no single scorer is trusted —
  1. ref-lookup retriever (a pasted citation beats any semantic match)
  2. FTS5/BM25 retriever (exact tokens + term rarity)
  3. LIKE retriever (substring fallback; also the FTS5-absent fallback)
  4. vector retriever over `clauses.embedding` (pgvector on Aurora, packed
     float32 scan on SQLite — platform.embeddings), mapped back to clause-grain
     units so it fuses with the others. Only a learned embedder (Titan/Cohere
     through Infer) fuses; the offline hash embedder is too noisy on long
     clauses and stays out of the fused ranking (it still answers
     `/graph/search` on its own).
Result lists are fused with Reciprocal Rank Fusion — score += weight/(60+rank)
— so consensus beats a single strong vote. Winners get context restored
(clause path + neighboring sibling preview) before returning.
"""
import logging
import re

import sqlalchemy as sa
from sqlalchemy.engine import Connection, Engine

from app.clhear.platform import record
from app.clhear.l1.adapters.base import CLAUSE_TYPES, DocNode, SourceMeta
from app.clhear.l1.models import (
    clause_annotations,
    clauses,
    doc_nodes,
    search_units,
    source_versions,
    sources,
)

log = logging.getLogger("clhear.l1.retrieval")

PARAGRAPH_TYPES = {"paragraph", "point", "subparagraph", "statement", "recital"}
PARAGRAPH_MIN_CHARS = 120
RRF_K = 60  # smoothing constant per the reference design
RETRIEVER_WEIGHTS = {"ref": 2.0, "fts": 1.0, "vec": 1.0, "vec:hash": 0.0, "like": 0.6}
PER_SOURCE_CAP = 8


def ws(text: str) -> str:
    return " ".join((text or "").split())


# ------------------------------------------------------------------ build side
def _fts_ok(conn: Connection) -> bool:
    """SQLite with the FTS5 projection present. PostgreSQL always answers False
    without touching the database (record.fts_available): a trial query there
    would abort the surrounding import transaction."""
    return record.fts_available(conn, "search_units_fts")


def build_units_for_version(
    conn: Connection, meta: SourceMeta, source_id: int, version_id: int, tree: list[DocNode]
) -> int:
    """Replace this source's search units with units for the new in-force
    version. Restricted sources are never indexed (public_ok discipline)."""
    fts = _fts_ok(conn)
    old_ids = [r[0] for r in conn.execute(
        sa.select(search_units.c.id).where(search_units.c.source_id == source_id)
    )]
    if old_ids:
        if fts:
            record.drop_fts_rows(conn, "search_units_fts", old_ids)
        record.rebuild_projection(conn, search_units, search_units.c.source_id == source_id)
    if meta.license != "open":
        return 0

    topics = " ".join(meta.topics)
    inserted = 0

    def _insert(grain: str, ref: str, text: str, clause_id=None, doc_node_id=None) -> None:
        nonlocal inserted
        unit_id = conn.execute(
            search_units.insert()
            .values(
                source_id=source_id,
                source_version_id=version_id,
                clause_id=clause_id,
                doc_node_id=doc_node_id,
                grain=grain,
                ref=ref,
                text=text,
            )
            .returning(search_units.c.id)
        ).scalar_one()
        if fts:
            conn.exec_driver_sql(
                "INSERT INTO search_units_fts(rowid, text) VALUES (?, ?)", (unit_id, text)
            )
        inserted += 1

    # Clause grain: the distilled form.
    annotation_map = {
        row.clause_id: row
        for row in conn.execute(
            sa.select(clause_annotations.c.clause_id, clause_annotations.c.category, clause_annotations.c.topics)
            .join(clauses, clauses.c.id == clause_annotations.c.clause_id)
            .where(clauses.c.source_version_id == version_id)
        )
    }
    for row in conn.execute(
        sa.select(clauses.c.id, clauses.c.ref, clauses.c.path, clauses.c.text, clauses.c.doc_node_id).where(
            clauses.c.source_version_id == version_id
        )
    ):
        annotation = annotation_map.get(row.id)
        category = annotation.category if annotation else ""
        distilled = f"{meta.short_name} · {row.path} · {category} · {topics}\n{row.text}"
        _insert("clause", row.ref, distilled, clause_id=row.id, doc_node_id=row.doc_node_id)

    # Paragraph grain (bursting): one unit per substantial paragraph, clause
    # heading prepended so a tangent paragraph is findable on its own.
    def walk(node: DocNode, clause_context: str) -> None:
        if node.node_type in CLAUSE_TYPES:
            head = node.heading or ws(node.raw_text)[:100] or node.label or node.ref
            clause_context = ws(f"{node.label} {head}")[:160]
        paragraph_text = node.raw_text
        if not paragraph_text and node.node_type == "point" and node.source_locator.get("structure") == "legal-html-element":
            # A lossless table point keeps its text in source-addressed cells.
            # Index that existing text with the checked article context; never
            # duplicate it into the verbatim record merely to serve search.
            paragraph_text = node.subtree_text()
            if node.label and paragraph_text.startswith(node.label + "\n"):
                paragraph_text = paragraph_text[len(node.label) + 1:]
        if (
            node.node_type in PARAGRAPH_TYPES
            and len(ws(paragraph_text)) >= PARAGRAPH_MIN_CHARS
            and getattr(node, "db_id", None) is not None
        ):
            _insert(
                "paragraph",
                node.ref,
                f"{meta.short_name} · {clause_context}\n{ws(node.label + ' ' + paragraph_text)}",
                doc_node_id=node.db_id,
            )
        for child in node.children:
            walk(child, clause_context)

    for root in tree:
        walk(root, meta.short_name)
    return inserted


def append_summary_to_unit(conn: Connection, clause_id: int, summary: str) -> None:
    """When the LLM explainer lands, fold the summary into the distilled unit."""
    row = conn.execute(
        sa.select(search_units.c.id, search_units.c.text)
        .where(search_units.c.clause_id == clause_id)
        .where(search_units.c.grain == "clause")
    ).first()
    if row is None:
        return
    new_text = f"{row.text}\n{summary}"
    conn.execute(search_units.update().where(search_units.c.id == row.id).values(text=new_text))
    if _fts_ok(conn):
        record.drop_fts_rows(conn, "search_units_fts", [row.id])
        conn.exec_driver_sql("INSERT INTO search_units_fts(rowid, text) VALUES (?, ?)", (row.id, new_text))


# ------------------------------------------------------------------ query side
# (pattern, template, transform-for-captured-group)
_REF_PATTERNS: list[tuple[re.Pattern, str, str]] = [
    (re.compile(r"\breg(?:ulation)?\.?\s*(\d+[A-Za-z]{0,2})\b", re.I), "regulation-{0}", "upper_suffix"),
    (re.compile(r"\bart(?:icle)?\.?\s*(\d+)\b", re.I), "art_{0}", "as_is"),
    (re.compile(r"\brecital\s*(\d+)\b", re.I), "rct_{0}", "as_is"),
    (re.compile(r"\bsch(?:edule)?\.?\s*(\d+[A-Za-z]{0,3})\b", re.I), "schedule-{0}", "upper_suffix"),
    (re.compile(r"(?:§+|\bsec(?:tion)?\.?)\s*(\d{4})\b", re.I), "sec{0}", "as_is"),
    (re.compile(r"\b(\d\.\d{4}-\d+(?:\([a-z0-9]+\))?)", re.I), "{0}", "lower"),
    (re.compile(r"\b([A-Za-z]{2}-\d{1,2}(?:\.\d+)?)\b"), "{0}", "lower"),   # NIST 800-53: ac-2, ac-2.1
    (re.compile(r"\b([A-Z]{2}\.[A-Z]{2}(?:-\d{2})?)\b"), "{0}", "as_is"),   # CSF: GV.OC-01
]


def detect_refs(query: str) -> list[str]:
    """Deterministic router: citation shapes a user might paste, per source."""
    refs: list[str] = []
    for pattern, template, transform in _REF_PATTERNS:
        for match in pattern.finditer(query):
            group = match.group(1)
            if transform == "lower":
                group = group.lower()
            elif transform == "upper_suffix":  # UK ids: digits + UPPER letters (18A)
                group = re.sub(r"[a-z]+$", lambda m: m.group(0).upper(), group)
            candidate = template.format(group)
            if candidate not in refs:
                refs.append(candidate)
    return refs


def _ref_list(conn: Connection, refs: list[str], limit: int) -> list[int]:
    if not refs:
        return []
    variants = set()
    for ref in refs:
        variants.update({ref, ref.upper(), ref.lower(), f"art_{ref}" if ref.isdigit() else ref})
    rows = conn.execute(
        sa.select(search_units.c.id)
        .where(sa.or_(search_units.c.ref.in_(variants), *[search_units.c.ref.like(f"{v}(%") for v in variants]))
        .order_by(search_units.c.grain, search_units.c.id)  # 'clause' < 'paragraph': citation targets first
        .limit(limit)
    ).all()
    return [r.id for r in rows]


def _fts_list(conn: Connection, query: str, limit: int) -> list[int]:
    """The FTS leg of hybrid search. Empty on PostgreSQL, which keeps its existing
    ref / vector / LIKE legs (the restricted-content exclusion lives in the
    search_units projection itself, so no leg can surface withheld text)."""
    if not _fts_ok(conn):
        return []
    tokens = [t for t in re.findall(r"[A-Za-z0-9§\.\-]+", query) if t]
    if not tokens:
        return []
    for joiner in (" ", " OR "):  # AND first; relax to OR if nothing matches
        match_expr = joiner.join(f'"{t}"' for t in tokens)
        try:
            rows = conn.exec_driver_sql(
                "SELECT rowid FROM search_units_fts WHERE search_units_fts MATCH ? "
                "ORDER BY bm25(search_units_fts) LIMIT ?",
                (match_expr, limit),
            ).fetchall()
        except sa.exc.OperationalError:
            # A malformed MATCH expression only; SQLite does not abort the
            # transaction on a statement error, so continuing is sound here.
            return []
        if rows:
            return [r[0] for r in rows]
    return []


def _like_list(conn: Connection, query: str, limit: int) -> list[int]:
    pattern = f"%{query.lower()}%"
    rows = conn.execute(
        sa.select(search_units.c.id)
        .where(sa.func.lower(search_units.c.text).like(pattern))
        .order_by(search_units.c.id)
        .limit(limit)
    ).all()
    return [r.id for r in rows]


def _vec_list(engine: Engine, conn: Connection, query: str, limit: int) -> tuple[str, list[int]]:
    """Nearest clauses by embedding, expressed as clause-grain unit ids (ranked).

    Returns the leg name with the ids: a learned model (Titan/Cohere via Infer)
    fuses at full weight as ``vec``; the offline hash embedder is lexical and
    noisy on long clauses, so its leg is ``vec:hash`` and carries whatever
    weight RETRIEVER_WEIGHTS gives it (zero by default — skipped entirely).
    """
    from app.clhear.settings import get_settings
    if get_settings().clhear_preview_mode:
        return "vec", []  # Read-only preview uses stored lexical indexes, never a model call.
    from app.clhear.platform import embeddings

    try:
        emb = embeddings.embedder()
    except Exception:
        log.debug("embedder unavailable", exc_info=True)
        return "vec", []
    name = "vec:hash" if emb.name == embeddings.HashEmbedder.name else "vec"
    if RETRIEVER_WEIGHTS.get(name, 1.0) <= 0:
        return name, []
    try:
        hits = embeddings.semantic_search(engine, query, limit=limit, emb=emb)
    except Exception:  # the index is optional; the other legs still answer
        log.debug("vector leg unavailable", exc_info=True)
        return name, []
    if not hits:
        return name, []
    rank = {cid: i for i, (cid, _) in enumerate(hits)}
    rows = conn.execute(
        sa.select(search_units.c.id, search_units.c.clause_id)
        .where(search_units.c.clause_id.in_(list(rank)), search_units.c.grain == "clause")
    ).all()
    return name, [r.id for r in sorted(rows, key=lambda r: rank[r.clause_id])]


def rrf(ranked_lists: dict[str, list[int]], k: int = RRF_K) -> list[tuple[int, float]]:
    """Reciprocal Rank Fusion: score += weight / (k + rank)."""
    scores: dict[int, float] = {}
    for name, ids in ranked_lists.items():
        weight = RETRIEVER_WEIGHTS.get(name, 1.0)
        for rank, unit_id in enumerate(ids, start=1):
            scores[unit_id] = scores.get(unit_id, 0.0) + weight / (k + rank)
    return sorted(scores.items(), key=lambda kv: kv[1], reverse=True)


def search(
    engine: Engine,
    query: str,
    *,
    scope: str | None = None,
    category: str | None = None,
    topic: str | None = None,
    limit: int = 30,
) -> list[dict]:
    """Hybrid search: fan out retrievers, fuse with RRF, dedup per clause,
    cap per source, restore context."""
    fetch = max(limit * 3, 60)
    with engine.connect() as conn:
        vec_name, vec_ids = _vec_list(engine, conn, query, fetch)
        ranked = {
            "ref": _ref_list(conn, detect_refs(query), fetch),
            "fts": _fts_list(conn, query, fetch),
            vec_name: vec_ids,
            "like": _like_list(conn, query, fetch),
        }
        fused = rrf(ranked)
        if not fused:
            return []
        unit_ids = [uid for uid, _ in fused[: fetch * 2]]
        unit_rows = {
            row.id: row
            for row in conn.execute(
                sa.select(
                    search_units,
                    sources.c.key.label("source_key"),
                    sources.c.name.label("source_name"),
                    sources.c.short_name,
                    sources.c.topics.label("source_topics"),
                    sources.c.family_id,
                    sources.c.license,
                    sources.c.adapter,
                    sources.c.canonical_url,
                    source_versions.c.version_label,
                )
                .join(sources, sources.c.id == search_units.c.source_id)
                .join(source_versions, source_versions.c.id == search_units.c.source_version_id)
                .where(search_units.c.id.in_(unit_ids))
            )
        }
        # optional scope/category filters
        annotation_by_clause = {}
        if category or topic:
            clause_ids = [unit_rows[uid].clause_id for uid in unit_ids if uid in unit_rows and unit_rows[uid].clause_id]
            if clause_ids:
                for row in conn.execute(
                    sa.select(clause_annotations.c.clause_id, clause_annotations.c.category, clause_annotations.c.topics)
                    .where(clause_annotations.c.clause_id.in_(clause_ids))
                ):
                    annotation_by_clause.setdefault(row.clause_id, []).append(row)

        results: list[dict] = []
        seen_clauses: set = set()
        per_source: dict[str, int] = {}
        display_decisions: dict[str, bool] = {}
        for unit_id, score in fused:
            row = unit_rows.get(unit_id)
            if row is None:
                continue
            from app.clhear.l1 import permissions
            if row.source_key not in display_decisions:
                display_decisions[row.source_key] = (not permissions.required_for(row) or
                    permissions.decision(conn, row.source_key, "display_public")["allowed"])
            if not display_decisions[row.source_key]:
                continue  # a materialized index never overrides expiry/revocation
            if scope and scope not in ((row.source_topics or []) if isinstance(row.source_topics, list) else []) and scope != _family_key(conn, row.family_id):
                continue
            if category or topic:
                clause_key = row.clause_id
                anns = annotation_by_clause.get(clause_key, [])
                if category and not any(a.category == category for a in anns):
                    continue
                if topic and not any(topic in (a.topics if isinstance(a.topics, list) else []) for a in anns):
                    continue
            dedup_key = row.clause_id or f"node-{row.doc_node_id}"
            if dedup_key in seen_clauses:
                continue
            seen_clauses.add(dedup_key)
            if per_source.get(row.source_key, 0) >= PER_SOURCE_CAP:
                continue
            per_source[row.source_key] = per_source.get(row.source_key, 0) + 1
            results.append(_result_dict(conn, row, query, score))
            if len(results) >= limit:
                break
        return results


def _family_key(conn: Connection, family_id: int) -> str:
    # No module-level cache: numeric family ids restart per database, so a
    # process-wide id->key map goes stale across DBs (it broke the scope
    # filter whenever the process had seen another corpus first, e.g. the
    # test suite or a worker switching snapshots). One indexed-PK lookup per
    # search result is cheap; correctness wins.
    from app.clhear.l1.models import source_families

    return conn.execute(
        sa.select(source_families.c.key).where(source_families.c.id == family_id)
    ).scalar() or ""


def _result_dict(conn: Connection, row, query: str, score: float) -> dict:
    # snippet from the unit text around the first query-term hit
    text = row.text
    body = text.split("\n", 1)[1] if "\n" in text else text
    lowered = body.lower()
    pos = -1
    for token in [query.lower()] + query.lower().split():
        pos = lowered.find(token)
        if pos != -1:
            break
    if pos == -1:
        pos = 0
    start = max(0, pos - 120)
    snippet = ("…" if start > 0 else "") + body[start : pos + 260] + "…"

    # context restoration: clause path + adjacent sibling preview for bursts
    context = ""
    clause_ref = row.ref
    doc_node_id = row.doc_node_id
    if row.clause_id is not None:
        clause = conn.execute(
            sa.select(clauses.c.path, clauses.c.ref).where(clauses.c.id == row.clause_id)
        ).first()
        if clause is not None:
            context = clause.path
            clause_ref = clause.ref
    elif row.doc_node_id is not None:
        node = conn.execute(sa.select(doc_nodes).where(doc_nodes.c.id == row.doc_node_id)).first()
        cursor = node
        while cursor is not None:
            if cursor.node_type in CLAUSE_TYPES:
                label = (cursor.label or "").strip()
                heading = (cursor.heading or "").strip()
                if label and heading and heading != label:
                    context = f"{label} — {heading}"
                else:
                    context = ws(f"{label} {heading}") or cursor.ref
                if cursor.ref:
                    clause_ref = cursor.ref
                break
            if cursor.parent_id is None:
                break
            cursor = conn.execute(sa.select(doc_nodes).where(doc_nodes.c.id == cursor.parent_id)).first()
    return {
        "ref": clause_ref,
        "grain": row.grain,
        "doc_node_id": doc_node_id,
        "source_key": row.source_key,
        "source_name": row.source_name,
        "short_name": row.short_name,
        "version": row.version_label,
        "snippet": snippet,
        "context": context,
        "score": round(score, 5),
    }
