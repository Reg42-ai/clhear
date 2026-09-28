# Copyright (C) 2026 Reg42 AI
# This file is part of CLHEAR. See LICENSE (AGPL-3.0-only).
"""L4 licence types, read only from the licensing clauses of the texts in scope.

1. Candidate clauses are the in-scope clauses that use the words of licensing
   in any sector: licence, permit, registration, authorisation, certificate,
   accreditation. No query names a sector's licences.
2. The model sees only those clauses and must cite one per type.
3. A type is kept only when its citation is one of those clauses and every word
   of its name occurs in that clause. Anything else is discarded.
4. No licensing clause in scope: no model call, and an evidence gap says so.
"""
from __future__ import annotations

import logging
import re

import sqlalchemy as sa
from sqlalchemy.engine import Engine

from app.clhear.derived_models import license_types
from app.clhear.l1.models import clauses, source_versions, sources
from app.clhear.platform.gateway import parse_json_object
from app.clhear.platform.router import complete

log = logging.getLogger("clhear.l4.licenses")

# The words of licensing, in any sector. They find candidate clauses; they name no licence.
LICENSING_WORDS = re.compile(
    r"\b(?:licen[cs]e[ds]?|licen[cs]ing|licensee|permits?|permitted to|registration|registered|authori[sz]ation|"
    r"authori[sz]ed|certificates?|certification|certified|accreditation|accredited)\b", re.I)
MAX_CLAUSES = 24
BATCH = 12

RETIRED = "retired"

# A licence type names what it authorises. Bare words ("Registration") and the
# authority's actions on a licence (censure, suspension, revocation, orders,
# exemptions) are not licence types.
_GENERIC = frozenset({"registration", "licence", "license", "authorisation", "authorization", "permission",
                      "approval", "certificate", "certification"})
_NOT_A_LICENCE = re.compile(r"\b(?:censure|suspensions?|suspend|revocations?|revoke|withdrawals?|denials?|deny|"
                            r"cancellations?|disgorgement|penalt(?:y|ies)|orders?|exemptions?|exempt|bars?)\b", re.I)
_STOP = frozenset({"of", "the", "a", "an", "for", "to", "and", "or", "as", "in", "under", "with", "by", "on"})


def _content_words(name: str) -> list[str]:
    return [w for w in re.findall(r"[a-z]+", name.lower()) if w not in _STOP]


def names_a_licence(name: str) -> bool:
    words = _content_words(name)
    return bool(words) and not _NOT_A_LICENCE.search(name) and not set(words) <= _GENERIC


def licence_key(name: str) -> str:
    """Word-order and plural insensitive: "Registration of investment advisers" is
    "Investment Adviser Registration"."""
    return " ".join(sorted(w[:-1] if w.endswith("s") and len(w) > 3 else w for w in _content_words(name)))


def _retrieve(engine: Engine, query: str, limit: int = 8) -> list[dict]:
    try:
        from app.clhear.l1.retrieval import search

        hits = search(engine, query, limit=limit)
    except Exception:
        log.exception("license retrieval failed for %s", query)
        hits = []
    out = []
    for h in hits:
        out.append({
            "source_key": h.get("source_key") or h.get("key"),
            "ref": h.get("ref") or h.get("clause_ref"),
            "text": h.get("text") or h.get("snippet") or "",
            "text_hash": h.get("text_hash") or "",
        })
    # Fallback: LIKE over public clauses when the search index is empty.
    if not out:
        like = f"%{query.split()[0]}%"
        with engine.connect() as conn:
            rows = conn.execute(
                sa.select(sources.c.key, clauses.c.ref, clauses.c.text, clauses.c.text_hash)
                .join(source_versions, source_versions.c.source_id == sources.c.id)
                .join(clauses, clauses.c.source_version_id == source_versions.c.id)
                .where(source_versions.c.status == "in_force")
                .where(sources.c.license == "open")
                .where(clauses.c.public_ok.is_(True))
                .where(clauses.c.text.ilike(like))
                .limit(limit)
            ).all()
        out = [
            {"source_key": r.key, "ref": r.ref, "text": r.text or "", "text_hash": r.text_hash}
            for r in rows
        ]
    return [h for h in out if h.get("source_key") and h.get("ref")]


def _slug(text: str) -> str:
    return re.sub(r"[^a-z0-9]+", "-", text.lower()).strip("-")[:50]


def anchor_is_live(engine: Engine, source_key: str, ref: str) -> bool:
    """Same resolution the l4_grounding eval uses — in-force clause or discard."""
    if not source_key or not ref:
        return False
    with engine.connect() as conn:
        hit = conn.execute(
            sa.select(clauses.c.id)
            .join(source_versions, source_versions.c.id == clauses.c.source_version_id)
            .join(sources, sources.c.id == source_versions.c.source_id)
            .where(sources.c.key == source_key)
            .where(source_versions.c.status == "in_force")
            .where(clauses.c.ref == ref)
            .limit(1)
        ).first()
    return hit is not None


def _jurisdictions(engine: Engine) -> dict[str, str]:
    with engine.connect() as conn:
        return {r.key: (r.jurisdiction or "").strip().upper() for r in conn.execute(sa.select(sources.c.key, sources.c.jurisdiction))}


def _candidates(engine: Engine) -> list[dict]:
    """In-scope, in-force, open clauses that use the words of licensing."""
    from app.clhear.l1.scopes import in_scope

    with engine.connect() as conn:
        rows = conn.execute(
            sa.select(clauses.c.id, sources.c.key, clauses.c.ref, clauses.c.text, clauses.c.text_hash)
            .join(source_versions, source_versions.c.id == clauses.c.source_version_id)
            .join(sources, sources.c.id == source_versions.c.source_id)
            .where(source_versions.c.status == "in_force").where(sources.c.license == "open")
            .where(clauses.c.public_ok.is_(True)).order_by(sources.c.key, clauses.c.ordering)).all()
    out = [{"id": r.id, "source_key": r.key, "ref": r.ref, "text": r.text or "", "text_hash": r.text_hash}
           for r in rows if in_scope(r.key) and LICENSING_WORDS.search(r.text or "")]
    # A parent clause repeats its children's text: keep the most specific clauses.
    leaves = [c for c in out if not any(o is not c and o["text"] and o["text"] in c["text"] for o in out)]
    return leaves[:MAX_CLAUSES]


def retire_unsound(engine: Engine, jurisdiction_of: dict[str, str] | None = None) -> list[str]:
    """Retire licence types that do not name a licence, or whose jurisdiction is not
    that of the source their anchor quotes, and close their live licence row;
    nothing is deleted."""
    from app.clhear.derived_models import licences
    from app.clhear.platform import record

    from app.clhear.l1.scopes import keys as scope_keys

    jurisdiction_of = jurisdiction_of if jurisdiction_of is not None else _jurisdictions(engine)
    chosen = scope_keys()
    retired = []
    with engine.begin() as conn:
        for row in conn.execute(sa.select(license_types).where(license_types.c.status != RETIRED)).mappings():
            anchors = row["clause_anchors"] or []
            anchor_keys = {a.get("source_key") for a in anchors if isinstance(a, dict) and a.get("source_key")}
            # A licence quoted from outside the active scope is not this build's to retire.
            if chosen is not None and (not anchor_keys or not anchor_keys <= chosen):
                continue
            # A source with no declared jurisdiction gives its licence types the jurisdiction "*".
            anchored = {jurisdiction_of.get(a.get("source_key"), "") or "*" for a in anchors}
            if not names_a_licence(row["name"]):
                reason = f"'{row['name']}' does not name a licence: retired"
            elif anchored and row["jurisdiction"].upper() not in anchored:
                reason = (f"{row['id']} is labelled {row['jurisdiction']} but its anchor quotes a "
                          f"{'/'.join(sorted(anchored))} source: retired")
            else:
                continue
            conn.execute(license_types.update().where(license_types.c.id == row["id"]).values(status=RETIRED))
            why = record.WhyTrail(layer="L4", subject_ref=row["id"], agent_id="l4.licenses", skill_version="l4.licenses",
                                  reasoning_summary=reason,
                                  evidence_refs=list(row["clause_anchors"] or []), inputs=(row["id"],), input_layers=("L1",))
            trail = why.write(conn)
            record.invalidate(conn, licences, sa.and_(licences.c.id == row["id"], licences.c.valid_to.is_(None)),
                              why=trail, reason=reason)
            retired.append(row["id"])
    return retired


def _same_licence(engine: Engine, jurisdiction: str, name: str) -> str | None:
    """The live licence type of this jurisdiction that differs from ``name`` only
    in word order or plural."""
    key = licence_key(name)
    with engine.connect() as conn:
        for row in conn.execute(sa.select(license_types.c.id, license_types.c.name).where(
                license_types.c.jurisdiction == jurisdiction, license_types.c.status != RETIRED)):
            if licence_key(row.name) == key and row.id != f"LIC:{jurisdiction}:{_slug(name)}":
                return row.id
    return None


def extract_licenses(engine: Engine, llm) -> dict:
    from app.clhear import evidence
    from app.clhear.l1.scopes import active_name

    written = discarded = 0
    ids: list[str] = []
    jurisdiction_of = _jurisdictions(engine)
    candidates = _candidates(engine)
    for start in range(0, len(candidates), BATCH):
        retrieved = candidates[start:start + BATCH]
        allowed = {(h["source_key"], h["ref"]): h for h in retrieved}
        corpus = "\n\n".join(f"[{h['source_key']}#{h['ref']}]\n{h['text'][:800]}" for h in retrieved)
        prompt = (
            "Extract the licence TYPES these clauses establish: a licence, permit, registration, authorisation, "
            "certificate or accreditation that a person must hold, or may obtain, to lawfully carry on an activity. "
            "Name each by the words of the clause. The authority's actions on a licence (suspension, revocation, "
            "denial, orders) and exemptions are not licence types. You may ONLY use the clauses below. Every type "
            "MUST cite source_key and ref from the brackets. Do not use general knowledge.\n"
            'JSON: {"license_types": [{"name": "", "issuing_regime": "", "source_key": "", "ref": ""}]}\n\n'
            + corpus
        )
        try:
            result = complete(llm, "l4.license_extract", prompt=prompt,
                              system="Extractive only. If a type is not in the text, omit it. JSON only.",
                              required_keys=["license_types"], max_tokens=800)
            parsed = parse_json_object(result.text)
        except Exception:
            log.exception("licence extraction failed")
            discarded += 1
            continue
        for item in parsed.get("license_types") or []:
            hit = allowed.get((item.get("source_key"), item.get("ref"))) if isinstance(item, dict) else None
            name = " ".join(str((item or {}).get("name") or "").split()) if isinstance(item, dict) else ""
            if hit is None or not names_a_licence(name) or not evidence.grounded(name, [hit["text"]]):
                discarded += 1
                continue
            jur = jurisdiction_of.get(hit["source_key"], "") or "*"
            same = _same_licence(engine, jur, name)
            if same is not None:
                ids.append(same)
                continue
            lid = f"LIC:{jur}:{_slug(name)}"
            anchors = [{"source_key": hit["source_key"], "ref": hit["ref"], "text_hash": hit["text_hash"]}]
            regime = str(item.get("issuing_regime") or "")
            clause = {"id": hit["id"], "source_key": hit["source_key"], "ref": hit["ref"], "text": hit["text"]}
            with engine.begin() as conn:
                exists = conn.execute(sa.select(license_types.c.id).where(license_types.c.id == lid)).first()
                values = dict(jurisdiction=jur, name=name,
                              issuing_regime=regime[:200] if evidence.grounded(regime, [hit["text"]]) else "",
                              clause_anchors=anchors, status="ai_generated", generated_by=result.model,
                              evidence={"name_words_in": [evidence.whole(clause)]})
                if exists:
                    conn.execute(license_types.update().where(license_types.c.id == lid).values(**values))
                else:
                    conn.execute(license_types.insert().values(id=lid, **values))
            from app.clhear.governance import mark_generated

            mark_generated(engine, layer="L4", subject_ref=lid, generated_by=result.model,
                           routing_reason="licence types quoted from licensing clauses in scope",
                           detail={"anchors": anchors})
            written += 1
            ids.append(lid)
    if not ids:
        with engine.begin() as conn:
            evidence.record_gap(conn, scope=active_name() or "", layer="L4", kind="no_licence_types",
                                subject="licences", missing="a licensing, registration or authorisation regime",
                                detail={"licensing_clauses_read": len(candidates)})
    try:
        from app.clhear import ai_ops

        ai_ops.record(
            engine, kind="fleet_generation", layer="L4", fleet="l4.licenses",
            reasoning=f"{written} licence types quoted from {len(candidates)} licensing clause(s); {discarded} discarded",
            detail={"written": written, "discarded": discarded, "ids": ids, "candidates": len(candidates)},
        )
    except Exception:
        log.exception("L4 ai_ops failed")
    return {"written": written, "discarded": discarded, "candidates": len(candidates), "ids": ids,
            "retired": retire_unsound(engine, jurisdiction_of)}


def list_license_types(engine: Engine, jurisdiction: str | None = None) -> list[dict]:
    stmt = sa.select(license_types).where(license_types.c.status != RETIRED)
    if jurisdiction:
        stmt = stmt.where(license_types.c.jurisdiction == jurisdiction)
    with engine.connect() as conn:
        return [dict(r) for r in conn.execute(stmt).mappings()]


def grounded_enum(engine: Engine) -> dict[str, list[str]]:
    """jurisdiction → list of grounded license type names."""
    by: dict[str, list[str]] = {}
    for row in list_license_types(engine):
        by.setdefault(row["jurisdiction"], []).append(row["name"])
    return by
