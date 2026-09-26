# Copyright (C) 2026 Reg42 AI
# This file is part of CLHEAR. See LICENSE (AGPL-3.0-only).
"""L4 grounded license registry — RAG, closed-world, no general knowledge.

1. Retrieve authorization-creating provisions from the ingested corpus.
2. The LLM sees ONLY retrieved clause text and must quote an anchor per type.
3. Any output whose anchor does not resolve to a retrieved clause is discarded.
4. authorisations on /v1/blueprint is a closed enum of these grounded types.
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

# Retrieval hints for authorization-creating provisions. They only find
# candidate clauses: a licence type takes the jurisdiction of the source its
# quoted anchor comes from, never a hint's.
LICENSE_QUERIES = (
    "registration required registered with the Commission",
    "authorisation authorization required to carry on",
    "licence license required",
    "permission to carry on regulated activity",
    "Part 4A permission authorisation FSMA",
    "electronic money authorisation e-money",
    "payment institution authorisation",
    "CASP authorisation crypto-asset service provider",
    "investment firm authorisation",
    "obliged entities anti-money laundering",
    "financial entities digital operational resilience",
    "broker-dealer registration",
)
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


# Licences are created by law and regulation; guidance and standards describe them.
BINDING_KINDS = frozenset({"law", "regulation"})


def _jurisdictions(engine: Engine) -> dict[str, str]:
    with engine.connect() as conn:
        return {r.key: (r.jurisdiction or "").strip().upper() for r in conn.execute(sa.select(sources.c.key, sources.c.jurisdiction))}


def _binding_keys(engine: Engine) -> set[str]:
    with engine.connect() as conn:
        return {r.key for r in conn.execute(sa.select(sources.c.key).where(sources.c.kind.in_(BINDING_KINDS)))}


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
            anchored = {jurisdiction_of.get(a.get("source_key"), "") for a in anchors}
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
    written = discarded = 0
    ids: list[str] = []
    coverage_gaps: list[str] = []
    jurisdiction_of = _jurisdictions(engine)
    binding = _binding_keys(engine)
    for query in LICENSE_QUERIES:
        from app.clhear.l1.scopes import in_scope

        retrieved = [h for h in _retrieve(engine, query)
                     if h["source_key"] in binding and in_scope(h["source_key"])
                     and anchor_is_live(engine, h["source_key"], h["ref"])]
        if not retrieved:
            coverage_gaps.append(query)
            continue
        allowed = {(h["source_key"], h["ref"]) for h in retrieved}
        corpus = "\n\n".join(
            f"[{h['source_key']}#{h['ref']}]\n{h['text'][:800]}" for h in retrieved
        )
        prompt = (
            "Extract authorization / license TYPES that this text creates. A licence type is a registration, "
            "licence, authorisation or permission that a person must hold, or may obtain, to lawfully carry on an "
            "activity, named by what it authorises (for example 'Investment Adviser Registration'). The authority's "
            "actions on a licence (censure, suspension, revocation, denial, withdrawal, orders) and exemptions are not "
            "licence types. You may ONLY use the clauses below. Every type MUST quote source_key and ref "
            "from the brackets. Do not use general knowledge.\n"
            'JSON: {"license_types": [{"name": "", "issuing_regime": "", '
            '"source_key": "", "ref": ""}]}\n\n'
            + corpus
        )
        try:
            result = complete(
                llm, "l4.license_extract",
                prompt=prompt,
                system="Extractive only. If a type is not in the text, omit it. JSON only.",
                required_keys=["license_types"],
                max_tokens=800,
            )
            parsed = parse_json_object(result.text)
        except Exception:
            log.exception("license extract failed for %s", query)
            discarded += 1
            continue
        for item in parsed.get("license_types") or []:
            if not isinstance(item, dict):
                discarded += 1
                continue
            key, ref = item.get("source_key"), item.get("ref")
            if (key, ref) not in allowed:
                discarded += 1
                continue
            hit = next(h for h in retrieved if h["source_key"] == key and h["ref"] == ref)
            name = str(item.get("name") or "").strip()
            jur = jurisdiction_of.get(key, "")
            if not names_a_licence(name) or not jur:
                discarded += 1
                continue
            same = _same_licence(engine, jur, name)
            if same is not None:
                ids.append(same)
                continue
            lid = f"LIC:{jur}:{_slug(name)}"
            anchors = [{"source_key": key, "ref": ref, "text_hash": hit["text_hash"]}]
            with engine.begin() as conn:
                exists = conn.execute(sa.select(license_types.c.id).where(license_types.c.id == lid)).first()
                values = dict(
                    jurisdiction=jur, name=name,
                    issuing_regime=str(item.get("issuing_regime") or "")[:200],
                    clause_anchors=anchors, status="ai_generated",
                    generated_by=result.model,
                )
                if exists:
                    conn.execute(license_types.update().where(license_types.c.id == lid).values(**values))
                else:
                    conn.execute(license_types.insert().values(id=lid, **values))
            from app.clhear.governance import mark_generated

            mark_generated(
                engine, layer="L4", subject_ref=lid, generated_by=result.model,
                routing_reason="closed-world license RAG", detail={"anchors": anchors},
            )
            written += 1
            ids.append(lid)
    try:
        from app.clhear import ai_ops

        ai_ops.record(
            engine, kind="fleet_generation", layer="L4", fleet="l4.licenses",
            reasoning=f"Surveyor: {written} grounded license types; {discarded} ungrounded discarded; "
            f"{len(coverage_gaps)} coverage gaps (incomplete, never invented)",
            detail={"written": written, "discarded": discarded, "coverage_gaps": coverage_gaps, "ids": ids},
        )
    except Exception:
        log.exception("L4 ai_ops failed")
    return {"written": written, "discarded": discarded, "coverage_gaps": coverage_gaps, "ids": ids,
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


def validate_authorisations(engine: Engine, attributes: dict) -> None:
    """Raise ValueError if any authorisation is not in the grounded registry.

    Incomplete registry is allowed (no types for a jurisdiction → that
    jurisdiction cannot claim invented licenses). Empty authorisations always OK.
    """
    wanted = attributes.get("authorisations") or []
    if not wanted:
        return
    if not isinstance(wanted, list):
        raise ValueError("attributes.authorisations must be a list of grounded license types")
    known = {row["name"] for row in list_license_types(engine)}
    known |= {row["id"] for row in list_license_types(engine)}
    if not known:
        # Incomplete registry: do not invent a block and do not invent types.
        # Free-text authorisations stay unchecked until the fleet grounds some.
        return
    unknown = [a for a in wanted if a not in known]
    if unknown:
        raise ValueError(f"ungrounded authorisations (not in L4 license registry): {unknown}")
