# Copyright (C) 2026 Reg42 AI
# This file is part of CLHEAR. See LICENSE (AGPL-3.0-only).
"""HLD v2 §4.1 starter corpus — the instruments CLHEAR ships with on day one.

    MiFIR / MiFID II (UK + EU), MAR (UK + EU), MLRs 2017 family, FCA Handbook
    (selected sourcebooks), SEC / FINRA via EDGAR, FATCA statute + 26 CFR +
    Rev. Proc. 2022-43, GDPR (+ corrigenda), NIST spine (SP 800-53, CSF).

Each entry names the registry rows (``app.clhear.l1.registry_etoro.S``) or
starter adapters that carry the instrument, plus the tier used by the
currency gate (tier A publishers expose a dated feed and must be ≤ 24 h
behind; tier B are editions checked daily). ``starter_plan()`` resolves the
whole corpus to (entry, adapter) pairs on the same verbatim pipeline as the
rest of the fleet, so `python -m app.clhear.l1.starter_corpus` is a complete
first ingest.
"""
import json
from dataclasses import dataclass, field

from app.clhear.l1.adapters.base import Adapter

__all__ = ["STARTER_CORPUS", "StarterInstrument", "TIER_A_ADAPTERS", "starter_keys", "starter_plan", "coverage"]

# Publishers with an official dated feed (citator / consolidation date) — the
# currency gate measures lag against these.
TIER_A_ADAPTERS = frozenset({"uk_legislation", "eur_lex", "govinfo_us", "govinfo_us_usc", "govinfo_us_ecfr", "fca_handbook"})


@dataclass(frozen=True)
class StarterInstrument:
    instrument: str
    jurisdictions: tuple[str, ...]
    source_keys: tuple[str, ...]
    starters: tuple[str, ...] = field(default_factory=tuple)  # get_adapter keys
    tier: str = "A"


STARTER_CORPUS: tuple[StarterInstrument, ...] = (
    StarterInstrument("MiFID II", ("EU",), ("celex/32014L0065", "celex/32017R0565", "celex/32017L0593")),
    StarterInstrument("MiFIR", ("EU", "UK"), ("celex/32014R0600", "eur/2014/600/uk")),
    StarterInstrument("MiFID II RTS", ("EU",), ("celex/32017R0587", "celex/32017R0583", "celex/32017R0590", "celex/32017R0585", "celex/32017R0574", "celex/32017R0589")),
    StarterInstrument("MAR", ("EU", "UK"), ("celex/32014R0596", "eur/2014/596/uk", "esma/guidelines/mar-delay"), tier="A"),
    StarterInstrument("MLRs 2017 family", ("UK",), ("uksi/2017/692", "ukpga/2002/29", "ukpga/2000/11", "ukpga/2017/22", "ukpga/2018/13")),
    StarterInstrument("FSMA 2000 / RAO / FPO", ("UK",), ("ukpga/2000/8", "ukpga/2023/29", "uksi/2001/544", "uksi/2005/1529")),
    StarterInstrument(
        "FCA Handbook (selected)",
        ("UK",),
        ("fca/handbook", "fca/handbook/SYSC", "fca/handbook/COBS", "fca/handbook/CASS", "fca/handbook/PROD",
         "fca/handbook/SUP", "fca/handbook/DISP", "fca/handbook/MIFIDPRU"),
    ),
    StarterInstrument("SEC rules via EDGAR", ("US",), ("cfr/17/240-bd", "cfr/17/reg-bi-sp", "sec/release/34-86031", "sec/release/34-100155"), tier="A"),
    StarterInstrument("FINRA rules via EDGAR (derived-only)", ("US",), ("finra/rulebook", "finra/rule/3110", "finra/rule/2111", "finra/rule/3310", "finra/rule/2210", "finra/rule/4511"), tier="B"),
    StarterInstrument("FATCA statute + 26 CFR + Rev. Proc.", ("US",), ("irs/qi-agreement", "cfr/26/871m"), starters=("govinfo_us_usc", "govinfo_us_ecfr"), tier="A"),
    StarterInstrument("GDPR", ("EU",), ("celex/32016R0679", "celex/32016R0679R(01)", "celex/32016R0679R(02)", "celex/32016R0679R(03)")),
    StarterInstrument("ESMA guidelines (MiFID II)", ("EU",), ("esma/guidelines/suitability", "esma/guidelines/product-governance"), tier="B"),
    StarterInstrument("NIST spine", ("US", "INTL"), (), starters=("nist_sp800_53", "nist_csf"), tier="B"),
    StarterInstrument("FATF / BIS / IOSCO standards", ("INTL",), ("fatf/40-recommendations", "bis/basel/CRE20", "bis/basel/OPE25", "iosco/objectives-principles"), tier="B"),
    StarterInstrument("MAS / ASIC / ISA", ("SG", "AU", "IL"), ("sg/mas-aml-sfa04-n02", "sg/mas-psn02", "au/asic-rg227", "au/asic-rg271", "il/securities-law-5728"), tier="B"),
)


def starter_keys() -> list[str]:
    keys: list[str] = []
    for item in STARTER_CORPUS:
        for key in item.source_keys:
            if key not in keys:
                keys.append(key)
    return keys


def starter_plan() -> list[tuple[dict | None, Adapter]]:
    """(registry entry | None, adapter) for every starter-corpus source."""
    from app.clhear.l1.adapters import get_adapter
    from app.clhear.l1.fleet import adapter_for
    from app.clhear.l1.registry_etoro import S

    by_key = {entry["key"]: entry for entry in S}
    plan: list[tuple[dict | None, Adapter]] = []
    seen: set[str] = set()
    for item in STARTER_CORPUS:
        for starter in item.starters:
            adapter = get_adapter(starter)
            key = adapter.meta().source_key
            if key not in seen:
                plan.append((None, adapter))
                seen.add(key)
        for key in item.source_keys:
            if key in seen:
                continue
            entry = by_key.get(key)
            if entry is None:
                raise KeyError(f"starter corpus references unknown registry key {key}")
            plan.append((entry, adapter_for(entry)))
            seen.add(key)
    return plan


def coverage(engine) -> dict:
    """Which starter instruments have an in-force version (published scorecard input)."""
    import sqlalchemy as sa

    from app.clhear.l1.models import source_versions, sources

    with engine.connect() as conn:
        versioned = {
            row.key
            for row in conn.execute(
                sa.select(sources.c.key)
                .join(source_versions, source_versions.c.source_id == sources.c.id)
                .where(source_versions.c.status == "in_force")
            )
        }
    out = []
    for item in STARTER_CORPUS:
        keys = list(item.source_keys)
        have = [k for k in keys if k in versioned]
        out.append(
            {
                "instrument": item.instrument,
                "jurisdictions": list(item.jurisdictions),
                "tier": item.tier,
                "sources": len(keys),
                "ingested": len(have),
                "missing": [k for k in keys if k not in versioned],
            }
        )
    total = sum(o["sources"] for o in out)
    done = sum(o["ingested"] for o in out)
    return {"instruments": out, "sources": total, "ingested": done, "share": round(done / total, 4) if total else 0.0}


def main(argv: list[str] | None = None) -> int:
    import argparse

    from app.clhear.db import get_engine, run_migrations
    from app.clhear.l1 import pipeline
    from app.clhear.settings import get_settings

    parser = argparse.ArgumentParser(description="Ingest the HLD v2 starter corpus")
    parser.add_argument("--dry-run", action="store_true", help="list the plan without fetching")
    args = parser.parse_args(argv)
    plan = starter_plan()
    if args.dry_run:
        for entry, adapter in plan:
            meta = adapter.meta()
            print(f"{meta.adapter:16} {meta.source_key:40} {meta.canonical_url}")
        print(f"{len(plan)} sources")
        return 0
    engine = get_engine()
    run_migrations(engine)
    settings = get_settings()
    store = pipeline.LocalStore(settings.clhear_artifacts_dir)
    results = []
    for _entry, adapter in plan:
        results.append(pipeline.ingest(engine, adapter, store, trigger="starter-corpus"))
    print(json.dumps({"ran": len(results), "statuses": sorted({r.get("status", "") for r in results}), "coverage": coverage(engine)}, indent=2, default=str))
    return 0


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
