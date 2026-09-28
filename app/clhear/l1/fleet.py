# Copyright (C) 2026 Reg42 AI
# This file is part of CLHEAR. See LICENSE (AGPL-3.0-only).
"""Fleet plan: every source-registry row has an adapter on the same verbatim path.

`wave1_adapters()` only instantiated entries with a `fetch` dict. `fleet_plan()`
yields every `S` row (plus the NIST/FATCA starters that live outside `S`) so
the midnight worker never returns `ran: 0` and never leaves a source as
watchlist-only.
"""
from app.clhear.l1.adapters.base import Adapter, SourceMeta
from app.clhear.l1.source_registry import S, source_meta

# Starters ingested via get_adapter (not every one is a row in S).
STARTERS = {
    "uk_legislation": ["uk_legislation"],
    "eur_lex": ["eur_lex"],
    "govinfo_us": ["govinfo_us_usc", "govinfo_us_ecfr", "nist_sp800_53", "nist_csf"],
}

PDF_ADAPTERS = frozenset(
    {
        "cysec",
        "wolfsberg",
        "israel",
        "seychelles",
        "gibraltar",
        "overlay",
    }
)

HTML_ADAPTERS = frozenset(
    {
        "au_legislation",
        "sg_legislation",
        "adgm",
        "nydfs",
        "nasdaq",
        "malta",
        "uae",
        "official_html",
    }
)


def _url(entry: dict) -> str:
    fetch = entry.get("fetch") or {}
    return fetch.get("url") or entry.get("canonical_url") or ""


class DeclarationGapAdapter:
    """A visible expected discovery dependency; never acquire substitute text."""
    def __init__(self, entry):
        self.key = entry["adapter"]
        self._meta = source_meta(entry)
        self.declaration_gap = {"code": entry["fetch"]["blocked"],
                                "source_role": entry.get("source_role", "reference"),
                                "reason": "This declaration requires an identified original document before ingestion."}

    def meta(self):
        return self._meta

    def fetch(self, since_version=None):
        raise ValueError(self.declaration_gap["code"])


def adapter_for(entry: dict) -> Adapter:
    """Instantiate the adapter that owns this registry row."""
    meta = source_meta(entry)
    fetch = entry.get("fetch") or {}
    key = entry["adapter"]
    if fetch.get("blocked"):
        return DeclarationGapAdapter(entry)
    from app.clhear.l1.adapters.document import KEYS as DOCUMENT_KEYS, DocumentAdapter
    if key in DOCUMENT_KEYS:
        return DocumentAdapter(entry, meta)
    if fetch.get("document_type") == "publisher_publication":
        from app.clhear.l1.adapters.publisher import GenericPublisherDocumentAdapter
        return GenericPublisherDocumentAdapter(source_key=entry["key"], title=entry["name"], url=_url(entry),
                                               meta=meta, adapter=key, expected_format=fetch.get("kind", "auto"))
    if key == "eur_lex":
        from app.clhear.l1.adapters.eur_lex import EurLexAdapter

        celex = fetch.get("celex") or entry["key"].split("/", 1)[-1]
        version = fetch.get("celex_version", celex)
        if fetch.get("language"):
            from app.clhear.l1.adapters.eur_lex_languages import EurLexLanguageAdapter
            return EurLexLanguageAdapter(language=fetch["language"], celex=celex, celex_version=version, meta=meta)
        return EurLexAdapter(celex=celex, celex_version=version, meta=meta)
    if key == "uk_legislation":
        from app.clhear.l1.adapters.uk_legislation import UkLegislationAdapter

        if fetch.get("kind") == "html":
            from app.clhear.l1.adapters.official_html import OfficialHtmlAdapter

            return OfficialHtmlAdapter(
                source_key=entry["key"],
                title=entry["name"],
                url=_url(entry),
                adapter=key,
                meta=meta,
            )
        doc = fetch.get("doc") or entry["key"]
        return UkLegislationAdapter(doc=doc, name=entry["name"], meta=meta)
    if key == "govinfo_us":
        return _govinfo_for(entry, meta, fetch)
    if key in PUBLISHER_ADAPTERS:
        return _publisher_for(entry, meta, fetch)
    if key == "lists":
        from app.clhear.l1.adapters.lists import ListsAdapter

        return ListsAdapter(source_key=entry["key"], title=entry["name"], url=_url(entry), meta=meta)
    if key == "restricted_file":
        from app.clhear.l1.adapters.restricted_file import RestrictedFileAdapter

        return RestrictedFileAdapter(source_key=entry["key"], title=entry["name"], url=_url(entry), meta=meta)
    if key in PDF_ADAPTERS or fetch.get("kind") == "pdf":
        from app.clhear.l1.adapters.pdf_docling import PdfOfficialAdapter

        return PdfOfficialAdapter(
            source_key=entry["key"],
            title=entry["name"],
            url=_url(entry),
            adapter=key,
            meta=meta,
        )
    from app.clhear.l1.adapters.official_html import OfficialHtmlAdapter

    return OfficialHtmlAdapter(
        source_key=entry["key"],
        title=entry["name"],
        url=_url(entry),
        adapter=key,
        meta=meta,
        jurisdiction=entry.get("jurisdiction", ""),
        issuer=entry.get("issuer", ""),
        kind=entry.get("kind", "regulation"),
        license=entry.get("license", "open"),
    )


# HLD v2 §4.1 first-class publisher adapters (replace the generic HTML/PDF
# fallbacks for these keys; keys stay the fleet schedule names).
PUBLISHER_ADAPTERS = frozenset(
    {"fca_handbook", "sec_edgar", "esma", "fatf", "bis_basel", "iosco", "mas", "asic", "isa", "irs_gov",
     "fca_enforcement", "sec_enforcement"}
)


ENFORCEMENT_ADAPTERS = frozenset({"fca_enforcement", "sec_enforcement"})


def _publisher_for(entry: dict, meta: SourceMeta, fetch: dict):
    from app.clhear.l1.adapters import standards_bodies as sb
    from app.clhear.l1.adapters.fca_handbook import FcaHandbookAdapter
    from app.clhear.l1.adapters.sec_edgar import SecEdgarAdapter

    key = entry["adapter"]
    common = dict(source_key=entry["key"], title=entry["name"], url=_url(entry), meta=meta)
    if key == "fca_handbook":
        return FcaHandbookAdapter(
            fetch.get("sourcebook", "PRIN"), chapters=fetch.get("chapters"), **common
        )
    if key in {"sec_edgar", "sec_enforcement"} and fetch.get("document_type") == "sec_page":
        from app.clhear.l1.adapters.sec_pages import SecPageAdapter
        return SecPageAdapter(**common, adapter=key)
    if key == "sec_edgar":
        return SecEdgarAdapter(**common)
    if key in ENFORCEMENT_ADAPTERS:
        from app.clhear.l1.adapters import enforcement as enf

        return {"fca_enforcement": enf.FcaFinalNoticesAdapter, "sec_enforcement": enf.SecEnforcementAdapter}[key](**common)
    cls = {
        "esma": sb.EsmaAdapter,
        "fatf": sb.FatfAdapter,
        "bis_basel": sb.BisBaselAdapter,
        "iosco": sb.IoscoAdapter,
        "mas": sb.MasAdapter,
        "asic": sb.AsicAdapter,
        "isa": sb.IsaAdapter,
        "irs_gov": sb.IrsRevProcAdapter,
    }[key]
    return cls(**common)


def _govinfo_for(entry: dict, meta: SourceMeta, fetch: dict):
    from app.clhear.l1.adapters.govinfo_us import GovInfoEcfrAdapter, GovInfoUscAdapter
    from app.clhear.l1.adapters.official_html import OfficialHtmlAdapter

    if fetch.get("document_type") == "ftc_page":
        from app.clhear.l1.adapters.ftc_pages import FtcPageAdapter
        return FtcPageAdapter(source_key=entry["key"], title=entry["name"], url=_url(entry), meta=meta)
    if fetch.get("kind") == "pdf":
        from app.clhear.l1.adapters.pdf_docling import PdfOfficialAdapter
        return PdfOfficialAdapter(source_key=entry["key"], title=entry["name"], url=_url(entry), adapter="govinfo_us", meta=meta)
    if fetch.get("usc_sections"):
        return GovInfoUscAdapter(
            title=str(fetch.get("usc_title", "15")),
            sections=tuple(fetch["usc_sections"]),
            edition=str(fetch.get("edition", "2023")),
            meta=meta,
            url_template=fetch.get("url_template") or None,
        )
    if fetch.get("ecfr_sections"):
        # Absent subchapter keeps the FATCA default (A). An explicit empty
        # subchapter (17 CFR part 275) must not be rewritten to A.
        pinned = dict(
            title=str(fetch.get("ecfr_title", "17")),
            sections=tuple(fetch["ecfr_sections"]),
            as_of=str(fetch.get("as_of", "2025-12-31")),
            chapter=fetch.get("chapter", ""),
            part=fetch.get("part", ""),
            meta=meta,
        )
        if "subchapter" in fetch:
            pinned["subchapter"] = str(fetch.get("subchapter") or "")
        return GovInfoEcfrAdapter(**pinned)
    return OfficialHtmlAdapter(
        source_key=entry["key"],
        title=entry["name"],
        url=_url(entry),
        adapter="govinfo_us",
        meta=meta,
    )


def fleet_plan(adapter_key: str | None = None) -> list[tuple[dict | None, Adapter]]:
    """Every registry row (optionally filtered to one adapter key), plus starters.

    Starters that already appear in `S` (MLRs, GDPR once added) are not
    duplicated. NIST / FATCA statute+regs stay on the `govinfo_us` lane.
    """
    from app.clhear.l1.adapters import ADAPTER_KEYS, get_adapter

    plan: list[tuple[dict | None, Adapter]] = []
    seen: set[str] = set()
    starter_keys = STARTERS.get(adapter_key, []) if adapter_key else [
        k for keys in STARTERS.values() for k in keys
    ]
    # Deduplicate while preserving order when adapter_key is None.
    ordered: list[str] = []
    for key in starter_keys:
        if key not in ordered:
            ordered.append(key)
    from app.clhear.l1.scopes import in_scope

    for key in ordered:
        if key in ADAPTER_KEYS:
            adapter = get_adapter(key)
            source_key = adapter.meta().source_key
            if source_key not in seen and in_scope(source_key):
                plan.append((None, adapter))
                seen.add(source_key)
    for entry in S:
        if adapter_key and entry["adapter"] != adapter_key:
            continue
        if entry["key"] in seen:
            continue
        plan.append((entry, adapter_for(entry)))
        seen.add(entry["key"])
    return plan


def fleet_adapter_keys() -> list[str]:
    """Sorted unique adapter keys that must have a daily EventBridge rule."""
    keys = {e["adapter"] for e in S}
    keys.update(STARTERS)
    return sorted(keys)
