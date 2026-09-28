# Copyright (C) 2026 Reg42 AI
# This file is part of CLHEAR. See LICENSE (AGPL-3.0-only).
"""Adapter registry. Adapters self-describe via meta(); the pipeline owns
hashing, storage, diffing, and events (HLD §7.2)."""
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from app.clhear.l1.adapters.base import Adapter


def get_adapter(key: str) -> "Adapter":
    """Instantiate a registered starter adapter by key (import-on-demand).

    Parameterized registry rows go through `fleet.adapter_for`, not this map.
    """
    from app.clhear.l1.adapters import eur_lex, govinfo_us, nist, uk_legislation

    registry = {
        "uk_legislation": uk_legislation.UkLegislationAdapter,
        "eur_lex": eur_lex.EurLexAdapter,
        "govinfo_us_usc": govinfo_us.GovInfoUscAdapter,
        "govinfo_us_ecfr": govinfo_us.GovInfoEcfrAdapter,
        "nist_sp800_53": nist.NistSp80053Adapter,
        "nist_csf": nist.NistCsfAdapter,
    }
    return registry[key]()


ADAPTER_KEYS = (
    "uk_legislation",
    "eur_lex",
    "govinfo_us_usc",
    "govinfo_us_ecfr",
    "nist_sp800_53",
    "nist_csf",
)

# HLD v2 §4.1 publisher adapters (parameterised; instantiated through
# `fleet.adapter_for`). Listed here so tooling can enumerate
# every adapter class the fleet ships.
PUBLISHER_ADAPTER_CLASSES = {
    "fca_handbook": "app.clhear.l1.adapters.fca_handbook:FcaHandbookAdapter",
    "sec_edgar": "app.clhear.l1.adapters.sec_edgar:SecEdgarAdapter",
    "esma": "app.clhear.l1.adapters.standards_bodies:EsmaAdapter",
    "fatf": "app.clhear.l1.adapters.standards_bodies:FatfAdapter",
    "bis_basel": "app.clhear.l1.adapters.standards_bodies:BisBaselAdapter",
    "iosco": "app.clhear.l1.adapters.standards_bodies:IoscoAdapter",
    "mas": "app.clhear.l1.adapters.standards_bodies:MasAdapter",
    "asic": "app.clhear.l1.adapters.standards_bodies:AsicAdapter",
    "isa": "app.clhear.l1.adapters.standards_bodies:IsaAdapter",
    "irs_gov": "app.clhear.l1.adapters.standards_bodies:IrsRevProcAdapter",
    # HLD v2 §4.7 enforcement sources (read by L7; informative family members)
    "fca_enforcement": "app.clhear.l1.adapters.enforcement:FcaFinalNoticesAdapter",
    "sec_enforcement": "app.clhear.l1.adapters.enforcement:SecEnforcementAdapter",
}


def publisher_adapter_class(key: str):
    import importlib

    module_name, class_name = PUBLISHER_ADAPTER_CLASSES[key].split(":")
    return getattr(importlib.import_module(module_name), class_name)

# What a host can register as a source: adapter key, what it reads, and the
# locator fields it needs. ``GET /v1/adapters`` serves this list.
SOURCE_ADAPTERS = (
    {"key": "local_text", "reads": "Pasted text, or a text, HTML or PDF file under CLHEAR_LOCAL_SOURCES_DIR",
     "locator": {"text": "the text itself", "path": "a file path (relative to CLHEAR_LOCAL_SOURCES_DIR)",
                 "format": "optional: text (default) or html, for pasted text"}},
    {"key": "url", "reads": "A public https page or PDF, fetched live",
     "locator": {"url": "https://..."}},
    {"key": "eur_lex", "reads": "An EU act from EUR-Lex / Cellar (XHTML)",
     "locator": {"celex": "CELEX number, e.g. 32016R0679", "celex_version": "optional consolidated id"}},
    {"key": "uk_legislation", "reads": "A UK act or SI from legislation.gov.uk (CLML XML)",
     "locator": {"doc": "e.g. uksi/2017/692"}},
    {"key": "govinfo_us", "reads": "US Code sections or eCFR sections (GovInfo / eCFR)",
     "locator": {"usc_title": "e.g. 15", "usc_sections": ["45"], "ecfr_title": "e.g. 16", "ecfr_sections": ["314.4"]}},
)

# Adapters whose source has an official citator/relations feed (HLD §7.2).
CITATOR_KEYS = ("uk_legislation", "eur_lex")
