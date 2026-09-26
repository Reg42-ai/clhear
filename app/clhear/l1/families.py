# Copyright (C) 2026 Reg42 AI
# This file is part of CLHEAR. See LICENSE (AGPL-3.0-only).
"""Family machinery (HLD §7.2, HLD v2 §4.1 family completeness).

* Citator sync reads the OFFICIAL effects/relations feed of a family's root
  source and auto-contains the affecting instruments as binding-tier family
  members (added_via='citator').
* Citation mining reads every clause of a freshly persisted version for
  references to other instruments (EU CELEX-able citations, UK S.I./chapter
  citations, US CFR/USC citations). Each becomes a ``citations`` row:
  resolved (member of the same family), out_of_scope (known source in another
  family), or open with a filed ``discovery_candidates`` row for a human.
* ``family_scorecard`` publishes completeness per family: members present over
  members present + open discovery candidates (gate ≥ 99 %).
"""
import logging
import re
import time

import sqlalchemy as sa
from sqlalchemy.engine import Connection, Engine

from app.clhear.l1.adapters.base import CitatorAdapter
from app.clhear.l1.models import (
    citations,
    clauses,
    discovery_candidates,
    family_members,
    source_families,
    source_versions,
    sources,
)
from app.clhear.models import runs
from app.clhear.platform import events as l0_events

log = logging.getLogger("clhear.l1.families")

# --- citation mining ----------------------------------------------------------

_EU = re.compile(
    r"\b(?P<kind>Regulation|Directive|Decision)\s+(?:\((?:EU|EC|EEC|Euratom)\)\s+)?(?:No\s+)?"
    r"(?P<a>\d{2,4})/(?P<b>\d{1,4})(?:/(?:EU|EC|EEC))?",
    re.I,
)
_UK_SI = re.compile(r"\bS\.?\s?I\.?\s+(?P<year>\d{4})/(?P<num>\d{1,4})\b")
_UK_ACT = re.compile(r"\b(?P<year>\d{4})\s+\(?c\.\s?(?P<chapter>\d{1,3})\)?")
_US_CFR = re.compile(r"\b(?P<title>\d{1,2})\s+C\.?F\.?R\.?\s+(?:§+\s*)?(?:part\s+)?(?P<part>\d{1,4})(?:\.(?P<sec>[0-9a-z\-]+))?", re.I)
_US_USC = re.compile(r"\b(?P<title>\d{1,2})\s+U\.?S\.?C\.?\s+(?:§+\s*)?(?P<sec>\d{1,5}[a-z\-]*)", re.I)

_KIND_LETTER = {"regulation": "R", "directive": "L", "decision": "D"}


def _celex(kind: str, a: str, b: str) -> str:
    # "2014/65" (year/number) or "600/2014" (number/year): the four-digit part is the year.
    if len(a) == 4 and (len(b) < 4 or int(a) >= 1950 and int(b) < 1950):
        year, num = a, b
    elif len(b) == 4:
        year, num = b, a
    else:
        year, num = a, b
    return f"celex/3{year}{_KIND_LETTER[kind.lower()]}{int(num):04d}"


def extract_citations(text: str) -> list[tuple[str, str, str]]:
    """(raw citation, candidate source key, publisher family hint) in text order."""
    out: list[tuple[str, str, str]] = []
    seen: set[str] = set()
    for m in _EU.finditer(text or ""):
        key = _celex(m.group("kind"), m.group("a"), m.group("b"))
        if key not in seen:
            seen.add(key)
            out.append((m.group(0), key, "eur_lex"))
    for m in _UK_SI.finditer(text or ""):
        key = f"uksi/{m.group('year')}/{int(m.group('num'))}"
        if key not in seen:
            seen.add(key)
            out.append((m.group(0), key, "uk_legislation"))
    for m in _UK_ACT.finditer(text or ""):
        key = f"ukpga/{m.group('year')}/{int(m.group('chapter'))}"
        if key not in seen:
            seen.add(key)
            out.append((m.group(0), key, "uk_legislation"))
    for m in _US_CFR.finditer(text or ""):
        key = f"cfr/{int(m.group('title'))}/{int(m.group('part'))}"
        if key not in seen:
            seen.add(key)
            out.append((m.group(0), key, "govinfo_us"))
    for m in _US_USC.finditer(text or ""):
        key = f"usc/{int(m.group('title'))}/{m.group('sec')}"
        if key not in seen:
            seen.add(key)
            out.append((m.group(0), key, "govinfo_us"))
    return out


def _resolve_key(conn: Connection, key: str) -> tuple[int | None, int | None]:
    """(source_id, family_id) for a candidate key, matching key prefixes for
    CFR/USC rows whose registry key carries a suffix (cfr/17/240-bd)."""
    row = conn.execute(sa.select(sources.c.id, sources.c.family_id).where(sources.c.key == key)).first()
    if row is None and (key.startswith("cfr/") or key.startswith("usc/")):
        row = conn.execute(
            sa.select(sources.c.id, sources.c.family_id).where(sources.c.key.like(key + "%")).limit(1)
        ).first()
    return (row.id, row.family_id) if row else (None, None)


def mine_citations(conn: Connection, source_id: int, version_id: int) -> dict:
    """Populate ``citations`` for a version; file discovery candidates for unknowns."""
    source = conn.execute(sa.select(sources.c.key, sources.c.family_id).where(sources.c.id == source_id)).one()
    rows = conn.execute(
        sa.select(clauses.c.id, clauses.c.text).where(clauses.c.source_version_id == version_id)
    ).all()
    counts = {"resolved": 0, "out_of_scope": 0, "open": 0, "candidates": 0}
    proposed: dict[str, int] = {}
    for row in rows:
        for raw, key, hint in extract_citations(row.text):
            if key == source.key:
                continue  # self-citation
            resolved_id, family_id = _resolve_key(conn, key)
            if resolved_id is not None and family_id == source.family_id:
                disposition, reason = "resolved", ""
            elif resolved_id is not None:
                other = conn.execute(sa.select(source_families.c.key).where(source_families.c.id == family_id)).scalar()
                disposition, reason = "out_of_scope", f"cross-family:{other}"
            else:
                candidate_id = proposed.get(key)
                if candidate_id is None:
                    existing = conn.execute(
                        sa.select(discovery_candidates.c.id)
                        .where(discovery_candidates.c.family_id == source.family_id)
                        .where(discovery_candidates.c.url == key)
                    ).scalar()
                    if existing is None:
                        existing = conn.execute(
                            discovery_candidates.insert()
                            .values(
                                family_id=source.family_id,
                                url=key,
                                title=raw,
                                found_via=f"citation:{source.key}",
                                classification={"adapter_hint": hint, "cited_as": raw},
                                status="proposed",
                            )
                            .returning(discovery_candidates.c.id)
                        ).scalar_one()
                        counts["candidates"] += 1
                    proposed[key] = candidate_id = existing
                disposition, reason = "open", f"discovery_candidate:{candidate_id}"
            conn.execute(
                citations.insert().values(
                    from_clause_id=row.id,
                    raw_text=raw[:300],
                    resolved_source_id=resolved_id,
                    disposition=disposition,
                    reason=reason,
                )
            )
            counts[disposition] += 1
    return counts


def family_tree(conn: Connection, family_key: str) -> dict | None:
    fam = conn.execute(sa.select(source_families).where(source_families.c.key == family_key)).first()
    if fam is None:
        return None
    rows = conn.execute(
        sa.select(
            sources.c.key, sources.c.name, sources.c.short_name, sources.c.kind, sources.c.jurisdiction,
            sources.c.rights_basis, sources.c.instrument, sources.c.publisher,
            family_members.c.relation, family_members.c.tier, family_members.c.status, family_members.c.added_via,
        )
        .join(family_members, family_members.c.source_id == sources.c.id)
        .where(family_members.c.family_id == fam.id)
        .order_by(family_members.c.relation, sources.c.key)
    ).mappings().all()
    versioned = {
        r[0]
        for r in conn.execute(
            sa.select(sources.c.key)
            .join(source_versions, source_versions.c.source_id == sources.c.id)
            .where(source_versions.c.status == "in_force")
            .where(sources.c.family_id == fam.id)
        )
    }
    members = [{**dict(r), "ingested": r["key"] in versioned} for r in rows]
    roots = [m for m in members if m["relation"] == "root"]
    return {
        "family": fam.key,
        "name": fam.name,
        "scope_charter": fam.scope_charter,
        "roots": [m["key"] for m in roots],
        "members": members,
    }


def family_scorecard(engine: Engine, family_key: str | None = None) -> dict:
    """Completeness per family (HLD v2 gate: ≥ 99 % of expected members present).

    expected = members present + open discovery candidates (cited instruments
    a human has not yet accepted or rejected). Registry rows without a
    version count as present-but-not-ingested and are reported separately.
    """
    out = []
    with engine.connect() as conn:
        fams = conn.execute(sa.select(source_families).order_by(source_families.c.key)).all()
        for fam in fams:
            if family_key and fam.key != family_key:
                continue
            present = conn.execute(
                sa.select(sa.func.count()).select_from(family_members).where(family_members.c.family_id == fam.id)
            ).scalar_one()
            open_candidates = conn.execute(
                sa.select(sa.func.count())
                .select_from(discovery_candidates)
                .where(discovery_candidates.c.family_id == fam.id)
                .where(discovery_candidates.c.status == "proposed")
            ).scalar_one()
            ingested = conn.execute(
                sa.select(sa.func.count(sa.distinct(sources.c.id)))
                .select_from(sources)
                .join(source_versions, source_versions.c.source_id == sources.c.id)
                .join(family_members, family_members.c.source_id == sources.c.id)
                .where(family_members.c.family_id == fam.id)
                .where(source_versions.c.status == "in_force")
            ).scalar_one()
            expected = present + open_candidates
            score = present / expected if expected else 1.0
            out.append(
                {
                    "family": fam.key,
                    "name": fam.name,
                    "members": present,
                    "ingested": ingested,
                    "open_candidates": open_candidates,
                    "completeness": round(score, 4),
                    "passed": score >= 0.99,
                }
            )
    passed = all(f["passed"] for f in out) if out else False
    return {"families": out, "passed": passed}


def sync_citator(
    engine: Engine, adapter: CitatorAdapter, *, trigger: str = "manual", job_id: str | None = None
) -> dict:
    """Upsert one family-member row per official effect record."""
    started = time.monotonic()
    meta = adapter.meta()
    effects = adapter.family_effects()

    added: list[str] = []
    with engine.begin() as conn:
        family_id = conn.execute(
            sa.select(source_families.c.id).where(source_families.c.key == meta.family_key)
        ).scalar()
        if family_id is None:
            raise RuntimeError(f"family {meta.family_key} not ingested yet (run the pipeline first)")
        for effect in effects:
            source_id = conn.execute(
                sa.select(sources.c.id).where(sources.c.key == effect.affecting_key)
            ).scalar()
            if source_id is None:
                source_id = conn.execute(
                    sources.insert()
                    .values(
                        family_id=family_id,
                        key=effect.affecting_key,
                        name=effect.affecting_name,
                        kind=effect.kind,
                        issuer=meta.issuer,
                        jurisdiction=meta.jurisdiction,
                        license="open",
                        adapter=meta.adapter,
                        canonical_url=effect.affecting_url,
                    )
                    .returning(sources.c.id)
                ).scalar_one()
            member = conn.execute(
                sa.select(family_members.c.source_id)
                .where(family_members.c.family_id == family_id)
                .where(family_members.c.source_id == source_id)
            ).first()
            if member is None:
                conn.execute(
                    family_members.insert().values(
                        family_id=family_id,
                        source_id=source_id,
                        relation=effect.relation,
                        tier="binding",
                        status="active",
                        added_via="citator",
                    )
                )
                added.append(effect.affecting_key)
        if added:
            l0_events.emit(
                conn,
                layer="l1",
                kind="FamilyMembersAdded",
                subject_ref=meta.family_key,
                payload={"family": meta.family_key, "members": added, "via": "citator"},
                producer=f"l1.families.{meta.adapter}",
            )

    summary = {"family": meta.family_key, "effects": len(effects), "new_members": added, "status": "succeeded"}
    inputs = {"family": meta.family_key, "source": meta.source_key}
    if job_id:
        inputs["job_id"] = job_id
    with engine.begin() as conn:
        conn.execute(
            runs.insert().values(
                fleet="l1.citator",
                trigger=trigger,
                inputs=inputs,
                outputs=summary,
                duration_ms=int((time.monotonic() - started) * 1000),
            )
        )
    log.info("citator sync %s: %d effects, %d new members", meta.family_key, len(effects), len(added))
    return summary
