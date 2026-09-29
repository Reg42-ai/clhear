# Copyright (C) 2026 Reg42 AI
# This file is part of CLHEAR. See LICENSE (AGPL-3.0-only).
"""The evidence contract every derived record follows.

L1 holds the official texts. Every record in L2 to L8 either quotes them or
points at a lower-layer record that does. Nothing is filled in from general
knowledge: when the texts do not support a record, the layer writes an
*evidence gap* that says what is missing and which kind of source would
supply it.

A quote is ``{"layer": "L1", "clause_id", "source_key", "clause_ref",
"start", "end", "quote"}``; ``start``/``end`` are offsets into
``clauses.text`` and ``quote`` equals that slice. A reference to a lower
record is ``{"layer", "table", "id"}``.

Some words are the engine's own data model, not facts about an industry:
the measure kinds and their fields, the duty grammar (modal verbs, "where" /
"if" / "unless"), and the blueprint states. They describe how CLHEAR reads
any text.
"""
from __future__ import annotations

import hashlib
import re
from datetime import datetime, timezone

import sqlalchemy as sa
from sqlalchemy.engine import Connection

_WORD = re.compile(r"[A-Za-z0-9]+")
# Function words: a name made of clause words may add these to join them.
STOP = frozenset({
    "a", "an", "the", "of", "to", "and", "or", "in", "on", "for", "by", "with", "at", "as", "from", "into", "that",
    "which", "its", "their", "his", "her", "any", "all", "each", "every", "such", "this", "these", "those", "be",
    "is", "are", "was", "were", "it", "not", "no", "per", "than", "so", "if", "where", "when", "must", "shall",
})


# ----------------------------------------------------------------- quotes


def locate(text: str, needle: str, start: int = 0) -> tuple[int, int] | None:
    """Offsets of ``needle`` in ``text``: exact, then ignoring case, then
    ignoring differences in whitespace. None when the words are not there."""
    if not text or not needle or not needle.strip():
        return None
    needle = needle.strip()
    if re.fullmatch(r"\w+", needle):
        # A single word is the word, not part of another ("person" is not in "personal").
        m = re.search(rf"\b{re.escape(needle)}\b", text[start:]) or re.search(rf"(?i)\b{re.escape(needle)}\b", text[start:])
        return (start + m.start(), start + m.end()) if m else None
    at = text.find(needle, start)
    if at >= 0:
        return at, at + len(needle)
    at = text.lower().find(needle.lower(), start)
    if at >= 0:
        return at, at + len(needle)
    words = needle.split()
    pattern = re.compile(r"\s+".join(re.escape(w) for w in words), re.I)
    m = pattern.search(text, start)
    return (m.start(), m.end()) if m else None


def quote(clause: dict, needle: str, *, start: int = 0) -> dict | None:
    """A quote of ``needle`` inside one clause row (``id``, ``source_key``,
    ``ref``/``clause_ref``, ``text``), or None."""
    text = clause.get("text") or ""
    span = locate(text, needle, start)
    if span is None:
        return None
    return {"layer": "L1", "clause_id": clause.get("id"), "source_key": clause.get("source_key", ""),
            "clause_ref": clause.get("clause_ref") or clause.get("ref") or "", "start": span[0], "end": span[1],
            "quote": text[span[0]:span[1]]}


def quote_first(clauses: list[dict], needle: str) -> dict | None:
    """The first clause (in order) that contains ``needle``."""
    for clause in clauses:
        found = quote(clause, needle)
        if found is not None:
            return found
    return None


def whole(clause: dict) -> dict:
    """The clause itself, trimmed of surrounding whitespace."""
    text = clause.get("text") or ""
    start = len(text) - len(text.lstrip())
    end = len(text.rstrip())
    return {"layer": "L1", "clause_id": clause.get("id"), "source_key": clause.get("source_key", ""),
            "clause_ref": clause.get("clause_ref") or clause.get("ref") or "", "start": start, "end": end,
            "quote": text[start:end]}


def record_ref(layer: str, table: str, row_id) -> dict:
    return {"layer": layer, "table": table, "id": row_id}


def is_quote(item) -> bool:
    return isinstance(item, dict) and item.get("layer") == "L1" and "quote" in item


# ----------------------------------------------------------------- words


def words(text: str) -> list[str]:
    return [w.lower() for w in _WORD.findall(text or "")]


def _stem(word: str) -> str:
    """Singular and plural fold to one form: procedure(s), service(s), process(es), polic(y|ies)."""
    if word.endswith("ies") and len(word) > 4:
        word = word[:-3] + "y"
    elif word.endswith("sses"):
        word = word[:-2]
    elif word.endswith("s") and not word.endswith("ss") and len(word) > 3:
        word = word[:-1]
    if word.endswith("e") and len(word) > 4:
        word = word[:-1]
    return word


def content_words(text: str) -> list[str]:
    return [w for w in words(text) if w not in STOP]


def missing_words(phrase: str, texts: list[str]) -> list[str]:
    """Content words of ``phrase`` that none of ``texts`` contains."""
    corpus = {_stem(w) for t in texts for w in words(t)}
    return [w for w in content_words(phrase) if _stem(w) not in corpus]


def grounded(phrase: str, texts: list[str]) -> bool:
    """True when ``phrase`` has content words and every one occurs in ``texts``."""
    return bool(content_words(phrase)) and not missing_words(phrase, texts)


# ----------------------------------------------------------------- checking


def clause_rows(conn: Connection, clause_ids) -> dict[int, dict]:
    """id -> {text, text_hash, source_key, ref, in_force} for the given clauses."""
    from app.clhear.l1.models import clauses, source_versions, sources

    ids = sorted({int(i) for i in clause_ids if i is not None})
    if not ids:
        return {}
    rows = conn.execute(
        sa.select(clauses.c.id, clauses.c.text, clauses.c.text_hash, clauses.c.ref, sources.c.key,
                  source_versions.c.status)
        .join(source_versions, clauses.c.source_version_id == source_versions.c.id)
        .join(sources, source_versions.c.source_id == sources.c.id)
        .where(clauses.c.id.in_(ids))).all()
    return {r.id: {"id": r.id, "text": r.text or "", "text_hash": r.text_hash, "ref": r.ref, "source_key": r.key,
                   "in_force": r.status == "in_force"} for r in rows}


def check(item: dict, clauses_by_id: dict[int, dict]) -> str | None:
    """None when the quote holds; otherwise the reason it does not."""
    if not is_quote(item):
        return "not a quote"
    clause = clauses_by_id.get(item.get("clause_id"))
    if clause is None:
        return "clause not found"
    if not clause["in_force"]:
        return "clause no longer in force"
    start, end = item.get("start"), item.get("end")
    if not isinstance(start, int) or not isinstance(end, int) or not 0 <= start < end <= len(clause["text"]):
        return "offsets outside the clause"
    if clause["text"][start:end] != item.get("quote"):
        return "quote differs from the clause text"
    return None


def reanchor(conn: Connection) -> dict:
    """Point quotes at the in-force copy of their clause.

    When L1 stores a new version of a source, every clause gets a new row, and a
    clause whose text did not change is the same words under a new id. Each
    quote that names a clause no longer in force is moved to the in-force
    clause of the same source, reference and text hash; its offsets and words
    stay valid because the text is identical. Quotes whose clause changed or
    disappeared are left alone: lineage reports them and the layer re-derives."""
    from app.clhear.derived_models import (
        activities,
        applies_to,
        blocks,
        characteristics,
        license_types,
        obligations,
        operates,
        requires,
    )
    from app.clhear.l1.models import clauses, source_versions, sources

    tables = (obligations, requires, blocks, characteristics, applies_to, activities, operates, license_types)
    rows = []
    referenced: set[int] = set()
    for table in tables:
        for r in conn.execute(sa.select(table.c[_pk(table)], table.c.evidence).where(table.c.evidence.isnot(None))):
            found = r.evidence if not isinstance(r.evidence, str) else _loads(r.evidence)
            ids = {q.get("clause_id") for q in _all_quotes(found)}
            if ids:
                rows.append((table, r[0], found, ids))
                referenced |= {i for i in ids if isinstance(i, int)}
    stale = {cid: c for cid, c in clause_rows(conn, referenced).items() if not c["in_force"]}
    if not stale:
        return {"moved": 0, "rows": 0}
    live = {}
    for r in conn.execute(
            sa.select(clauses.c.id, clauses.c.ref, clauses.c.text_hash, sources.c.key)
            .join(source_versions, clauses.c.source_version_id == source_versions.c.id)
            .join(sources, source_versions.c.source_id == sources.c.id)
            .where(source_versions.c.status == "in_force")
            .where(sources.c.key.in_(sorted({c["source_key"] for c in stale.values()})))):
        live[(r.key, r.ref, r.text_hash)] = r.id
    moved = {cid: live[(c["source_key"], c["ref"], c["text_hash"])] for cid, c in stale.items()
             if (c["source_key"], c["ref"], c["text_hash"]) in live}
    changed = 0
    for table, key, found, ids in rows:
        if not ids & set(moved):
            continue
        conn.execute(table.update().where(table.c[_pk(table)] == key).values(evidence=_moved(found, moved)))
        changed += 1
    return {"moved": len(moved), "rows": changed}


def _pk(table) -> str:
    return next(c.name for c in table.primary_key.columns)


def _loads(value):
    import json

    try:
        return json.loads(value)
    except ValueError:
        return {}


def _all_quotes(found) -> list[dict]:
    if is_quote(found):
        return [found]
    if isinstance(found, dict):
        return [q for v in found.values() for q in _all_quotes(v)]
    if isinstance(found, list):
        return [q for v in found for q in _all_quotes(v)]
    return []


def _moved(found, moved: dict):
    if is_quote(found):
        return {**found, "clause_id": moved.get(found.get("clause_id"), found.get("clause_id"))}
    if isinstance(found, dict):
        return {k: _moved(v, moved) for k, v in found.items()}
    if isinstance(found, list):
        return [_moved(v, moved) for v in found]
    return found


# ----------------------------------------------------------------- evidence gaps

def recommendation(kind: str, **detail) -> str:
    """What to add to L1 so the layer can derive this record (see ``app.clhear.advisor``)."""
    from app.clhear.advisor import recommendation as advise

    return advise(kind, **detail)


def _gaps_table():
    from app.clhear.derived_models import evidence_gaps

    return evidence_gaps


def gap_id(scope: str, layer: str, kind: str, subject: str) -> str:
    digest = hashlib.sha1(f"{scope}|{layer}|{kind}|{subject}".encode()).hexdigest()[:16]
    return f"GAP-{digest}"


def clear_gaps(conn: Connection, *, scope: str, layer: str) -> None:
    """A layer rebuilds its gaps on every build: they report the current state."""
    table = _gaps_table()
    conn.execute(table.delete().where(table.c.scope == scope, table.c.layer == layer))


def record_gap(conn: Connection, *, scope: str, layer: str, kind: str, subject: str, missing: str,
               source_key: str = "", clause_ref: str = "", detail: dict | None = None, **fields) -> str:
    table = _gaps_table()
    gid = gap_id(scope, layer, kind, subject)
    if conn.execute(sa.select(table.c.id).where(table.c.id == gid)).first() is not None:
        return gid
    conn.execute(table.insert().values(
        id=gid, scope=scope, layer=layer, kind=kind, subject=subject, source_key=source_key, clause_ref=clause_ref,
        missing=missing, recommendation=recommendation(kind, **fields), detail=detail or {},
        created_at=datetime.now(timezone.utc)))
    return gid


def gaps_for(conn: Connection, scope: str) -> list[dict]:
    table = _gaps_table()
    rows = conn.execute(sa.select(table).where(table.c.scope == scope).order_by(table.c.layer, table.c.kind,
                                                                               table.c.subject)).mappings()
    return [{k: r[k] for k in ("id", "layer", "kind", "subject", "source_key", "clause_ref", "missing",
                               "recommendation", "detail")} for r in rows]
