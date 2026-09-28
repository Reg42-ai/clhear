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
    for suffix in ("ies", "es", "s"):
        if word.endswith(suffix) and len(word) - len(suffix) >= 3:
            return word[: -len(suffix)] + ("y" if suffix == "ies" else "")
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


# ----------------------------------------------------------------- evidence gaps

# What to add when a record cannot be derived. Guidance about sources, never content.
RECOMMEND = {
    "no_measure": "The text states this duty without naming anything concrete to put in place. Add the "
                  "implementing guidance, standard or code of practice that says how it is met.",
    "measure_name_rejected": "A proposed measure name used words that are not in the text, so it was not kept. "
                             "Add guidance that names the measure, or review the duty by hand.",
    "operator_not_stated": "The text does not say who carries out this duty. Add the provision, guidance or "
                           "internal allocation that assigns it.",
    "characteristic_unspecified": "The text does not specify {field}. Add the guidance or standard that "
                                  "specifies it for this duty.",
    "role_undefined": "The texts use '{role}' but no definition of it is in scope. Add the definitions section "
                      "or the act that defines who counts as '{role}'.",
    "no_licence_types": "No licensing, registration or authorisation regime is in scope. If your activity needs "
                        "one, add the text that establishes it.",
    "no_enforcement_sources": "No enforcement source is in scope, so no duty carries an enforcement record. Add "
                              "the regulator's published enforcement actions or decisions.",
    "no_reference_sources": "No guidance or reference source is in scope. Add supervisory guidance, examination "
                            "findings or a recognised standard to support how each measure is operated.",
}


def recommendation(kind: str, **detail) -> str:
    template = RECOMMEND.get(kind, "")
    try:
        return template.format(**detail)
    except (KeyError, IndexError):
        return template


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
