# Copyright (C) 2026 Reg42 AI
# This file is part of CLHEAR. See LICENSE (AGPL-3.0-only).
"""Host-declared sources. This module does not register a regulatory catalog.

A consumer writes source rows. ``install`` loads those rows into ``S`` so the
existing fleet plan and ``seed`` can see them. Nothing is declared at import.
"""
from __future__ import annotations

import sqlalchemy as sa
from sqlalchemy.engine import Engine

from app.clhear.l1.adapters.base import SourceMeta
from app.clhear.l1.models import family_members, source_families, sources

FAMILIES: list[tuple[str, str, str]] = []
FAMILY_NAMES: dict[str, str] = {}
S: list[dict] = []
COLLECTION_SOURCE_KEYS: frozenset[str] = frozenset()
REFERENCE_SOURCE_KEYS: frozenset[str] = frozenset()

_LICENSE_REF = {
    "uk_legislation": "OGL-UK-3.0",
    "eur_lex": "Commission Decision 2011/833/EU",
    "govinfo_us": "public domain (17 U.S.C. 105)",
    "lists": "publisher terms — list data reused as published",
    "restricted_file": "licensed file supplied by the consumer",
    "sec_edgar": "public domain (17 U.S.C. 105)",
}


def is_collection(key: str) -> bool:
    return key in COLLECTION_SOURCE_KEYS


def source_role(key: str) -> str:
    if is_collection(key):
        return "collection"
    if key in REFERENCE_SOURCE_KEYS:
        return "reference"
    return "document"


def _about(entry: dict) -> str:
    publisher = entry.get("publisher") or entry.get("issuer") or ""
    name = entry.get("name") or entry["key"]
    description = f"{publisher} ({entry.get('jurisdiction', '')}). {name}.".strip()
    fetch = entry.get("fetch") or {}
    if fetch.get("url"):
        description += " Locator: " + str(fetch["url"]) + "."
    return description


def source_meta(entry: dict) -> SourceMeta:
    """Build the adapter SourceMeta for a registry entry."""
    license_ref = _LICENSE_REF.get(entry["adapter"], entry.get("license_ref") or "")
    family = entry.get("family") or "declared"
    return SourceMeta(
        rights_basis=entry.get("rights_basis", ""),
        publisher=entry.get("publisher") or entry.get("issuer") or "",
        instrument=entry.get("instrument") or entry.get("short_name") or entry["key"],
        family_key=family,
        family_name=FAMILY_NAMES.get(family, entry.get("family_name") or family),
        source_key=entry["key"],
        name=entry.get("name") or entry["key"],
        kind=entry.get("kind") or "guidance",
        issuer=entry.get("issuer") or "",
        jurisdiction=entry.get("jurisdiction") or "",
        license=entry.get("license") or "open",
        license_ref=license_ref,
        canonical_url=entry.get("canonical_url") or "",
        adapter=entry["adapter"],
        short_name=entry.get("short_name") or entry["key"],
        about=entry.get("about") or _about(entry),
        topics=list(entry.get("topics") or []),
        version_policy=("as_published" if entry["adapter"] == "eur_lex" else
                        "edition" if entry["adapter"] == "restricted_file" else "consolidated"),
    )


def install(entries: list[dict]) -> None:
    """Replace the in-memory registry with the consumer's source rows."""
    S.clear()
    FAMILIES.clear()
    seen_families: set[str] = set()
    for raw in entries:
        entry = dict(raw)
        entry.setdefault("family", "declared")
        entry.setdefault("name", entry["key"])
        entry.setdefault("short_name", entry["key"])
        entry.setdefault("kind", "guidance")
        entry.setdefault("license", "open")
        entry.setdefault("jurisdiction", "")
        entry.setdefault("issuer", "")
        entry.setdefault("canonical_url", (entry.get("fetch") or {}).get("url") or "")
        entry.setdefault("relation", "root")
        entry.setdefault("tier", "binding")
        entry.setdefault("topics", [])
        entry.setdefault("source_role", "document")
        entry.setdefault("publisher", entry.get("issuer") or "")
        entry.setdefault("instrument", entry.get("short_name") or entry["key"])
        S.append(entry)
        if entry["family"] not in seen_families:
            seen_families.add(entry["family"])
            FAMILIES.append((entry["family"], entry.get("family_name") or entry["family"], "declared by the consumer"))
    FAMILY_NAMES.clear()
    FAMILY_NAMES.update({key: name for key, name, _ in FAMILIES})


def wave1_entries() -> list[dict]:
    """No catalog is shipped. Structured rows appear only after ``install``."""
    return [e for e in S if e.get("fetch") and e["adapter"] in {"eur_lex", "uk_legislation"}]


def wave1_adapters(adapter_key: str | None = None) -> list[tuple[dict, object]]:
    from app.clhear.l1.adapters.eur_lex import EurLexAdapter
    from app.clhear.l1.adapters.uk_legislation import UkLegislationAdapter

    plan = []
    for entry in wave1_entries():
        if adapter_key and entry["adapter"] != adapter_key:
            continue
        meta = source_meta(entry)
        fetch = entry.get("fetch") or {}
        if entry["adapter"] == "eur_lex":
            celex = fetch["celex"]
            version = fetch.get("celex_version", celex)
            adapter = EurLexAdapter(celex=celex, celex_version=version, meta=meta)
        else:
            adapter = UkLegislationAdapter(doc=fetch["doc"], name=entry["name"], meta=meta)
        plan.append((entry, adapter))
    return plan


def seed(engine: Engine) -> dict:
    """Reconcile the installed declarations into the source tables."""
    created_f = created_s = skipped = 0
    with engine.begin() as conn:
        family_ids: dict[str, int] = {}
        for key, name, charter in FAMILIES:
            existing = conn.execute(sa.select(source_families.c.id).where(source_families.c.key == key)).scalar()
            if existing:
                family_ids[key] = existing
                continue
            family_ids[key] = conn.execute(
                source_families.insert()
                .values(key=key, name=name, scope_charter={"registry": charter, "scope": "declared"})
                .returning(source_families.c.id)
            ).scalar_one()
            created_f += 1
        for s in S:
            existing = conn.execute(sa.select(sources.c.id).where(sources.c.key == s["key"])).scalar()
            if existing:
                conn.execute(sources.update().where(sources.c.id == existing).values(
                    name=s["name"], short_name=s.get("short_name") or s["key"],
                    canonical_url=s.get("canonical_url") or "",
                    issuer=s.get("issuer") or "", publisher=s.get("publisher") or "",
                    license=s.get("license") or "open", adapter=s["adapter"],
                    # A source registered again with another kind or reference takes it at once.
                    kind=s.get("kind") or "guidance", instrument=s.get("instrument") or "",
                ))
                skipped += 1
                continue
            source_id = conn.execute(
                sources.insert().values(
                    family_id=family_ids[s["family"]],
                    key=s["key"],
                    name=s["name"],
                    short_name=s.get("short_name") or s["key"],
                    kind=s.get("kind") or "guidance",
                    issuer=s.get("issuer") or "",
                    jurisdiction=s.get("jurisdiction") or "",
                    license=s.get("license") or "open",
                    adapter=s["adapter"],
                    canonical_url=s.get("canonical_url") or "",
                    about=_about(s),
                    topics=s.get("topics") or [],
                    publisher=s.get("publisher") or "",
                    instrument=s.get("instrument") or "",
                ).returning(sources.c.id)
            ).scalar_one()
            conn.execute(family_members.insert().values(
                family_id=family_ids[s["family"]], source_id=source_id,
                relation=s.get("relation") or "root", tier=s.get("tier") or "binding",
                status="active", added_via="manual",
            ))
            created_s += 1
    return {"families_created": created_f, "sources_created": created_s, "skipped_existing": skipped}


def apply_scope() -> None:
    """Scopes select keys at run time. The registry is not filtered at import."""
    return None
