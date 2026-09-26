# Copyright (C) 2026 Reg42 AI
# This file is part of CLHEAR. See LICENSE (AGPL-3.0-only).
"""Company-independent publisher scope contracts, not assertions of coverage.

Known document URLs come exclusively from the reviewed source registry. A leaf
URL is never promoted to an exhaustive catalog. Missing catalog implementations
remain visible until a reviewed publisher-specific discovery adapter exists.
"""
from copy import deepcopy

PROFILE_VERSION = "2026-09-16.2"
BOUNDARIES = {
    "include": ["rules and legislation", "financially relevant standards", "official compliance guidance",
                "rule filings and amendments", "enforcement and examination publications", "linked official attachments"],
    "history": "Current publications and officially exposed historical archives; no reconstruction of unavailable history.",
    "exclude": ["unrelated news", "unrelated research", "speeches", "company-filing databases", "unrelated industry standards"],
    "standards_domains": ["financial services", "compliance", "governance", "reporting", "security", "privacy", "resilience"],
    "organization_filter": None,
    "denominator_policy": "Unknown until every included catalog is enumerated and its exact inventory independently reviewed.",
}
# A publisher may distribute through another institution's portal. Identity is
# assigned from the declared instrument, never inferred solely from its host.
_PUBLISHERS = {
    "eu-law": ("EU Publications Office / legislative institutions", "legislation"),
    "esma": ("European Securities and Markets Authority", "regulator"),
    "uk-law": ("UK Parliament / HM Government / National Archives", "legislation"),
    "fca": ("Financial Conduct Authority", "regulator"),
    "us-law": ("US Congress / Government Publishing Office", "legislation"),
    "sec": ("US Securities and Exchange Commission", "regulator"),
    "ftc": ("US Federal Trade Commission", "regulator"),
    "finra": ("FINRA", "regulator"), "fincen": ("FinCEN", "regulator"),
    "irs": ("US Treasury / Internal Revenue Service", "regulator"),
    "ofac": ("US Treasury OFAC", "regulator"), "nydfs": ("New York DFS", "regulator"),
    "nasdaq": ("Nasdaq", "regulator"), "nist": ("NIST", "standards"),
    "cysec": ("Cyprus / CySEC", "regulator"), "au-law": ("Australian Federal Register of Legislation", "legislation"),
    "asic": ("Australian Securities and Investments Commission", "regulator"),
    "austrac": ("AUSTRAC", "regulator"), "sg-law": ("Singapore / AGC", "legislation"),
    "mas": ("Monetary Authority of Singapore", "regulator"), "sg-privacy": ("Singapore PDPC", "regulator"),
    "adgm": ("ADGM / FSRA", "regulator"), "uae-law": ("UAE Federal Government", "legislation"),
    "seychelles": ("Seychelles / FSA", "regulator"), "malta-law": ("Malta legislation", "legislation"),
    "mfsa": ("Malta Financial Services Authority", "regulator"), "fiau": ("Malta FIAU", "regulator"),
    "gibraltar": ("Gibraltar / GFSC", "regulator"), "isa": ("Israel Securities Authority", "regulator"),
    "il-privacy": ("Israel privacy authority / official legislation", "regulator"),
    "un": ("United Nations Security Council", "regulator"),
    "eu-sanctions": ("European Commission sanctions", "regulator"), "ofsi": ("UK Treasury / OFSI", "regulator"),
    "fatf": ("Financial Action Task Force", "standards"), "basel": ("Basel Committee / BIS", "standards"),
    "iosco": ("IOSCO", "standards"), "wolfsberg": ("Wolfsberg Group", "standards"),
    "iso": ("ISO/IEC", "licensed_standards"), "aicpa": ("AICPA", "licensed_standards"),
    "pci": ("PCI Security Standards Council", "licensed_standards"), "ifrs": ("IFRS Foundation", "licensed_standards"),
    "be-fsma": ("Belgian FSMA", "regulator"), "fr-amf": ("France / AMF", "regulator"),
    "es-cnmv": ("Spain / CNMV", "regulator"), "de-bafin": ("Germany / BaFin", "regulator"),
    "it-consob": ("Italy / Consob", "regulator"),
}
_PREFIXES = {
    "celex/": ("eu-law",), "esma/": ("esma",), "ukpga/": ("uk-law",), "uksi/": ("uk-law",), "eur/": ("uk-law",),
    "fca/": ("fca",), "finra/": ("finra",),     "sec/": ("sec",), "usc/15/": ("us-law",),
    "usc/26/": ("us-law", "irs"), "cfr/17/": ("sec",), "cfr/26/": ("irs",), "cfr/31/": ("fincen",),
    "cfr/16/": ("ftc",), "ftc/": ("ftc",),
    "nasdaq/": ("nasdaq",), "nydfs/": ("nydfs",), "nist/": ("nist",), "cy/": ("cysec",),
    "au/asic": ("asic", "au-law"), "au/aml": ("austrac", "au-law"), "au/": ("au-law",),
    "sg/mas": ("mas",), "sg/pdpa": ("sg-law", "sg-privacy"), "sg/": ("sg-law", "mas"),
    "adgm/": ("adgm",), "ae/": ("uae-law",), "sc/": ("seychelles",),
    "mt/pmlftr": ("malta-law", "fiau"), "mt/": ("malta-law", "mfsa"), "gi/": ("gibraltar",),
    "il/securities": ("isa",), "il/privacy": ("il-privacy",), "lists/un": ("un",),
    "lists/ofac": ("ofac",), "lists/eu": ("eu-sanctions",), "lists/uk": ("ofsi",), "irs/": ("irs",),
    "fatf/": ("fatf",), "bis/": ("basel",), "iosco/": ("iosco",), "wolfsberg/": ("wolfsberg",),
    "iso/": ("iso",), "aicpa/": ("aicpa",), "pci/": ("pci",), "ifrs/": ("ifrs",),
    "ovl/be": ("be-fsma",), "ovl/fr": ("fr-amf",), "ovl/es": ("es-cnmv",),
    "ovl/de": ("de-bafin",), "ovl/it": ("it-consob",),
}

def publisher_ids(entry):
    if entry.get("publisher_ids"):
        return [key for key in entry["publisher_ids"] if key in _PUBLISHERS]
    key = entry.get("key", "")
    for prefix in sorted(_PREFIXES, key=len, reverse=True):
        if key.startswith(prefix):
            return list(_PREFIXES[prefix])
    return []


def publisher_profiles(entries=None):
    if entries is None:
        from app.clhear.l1.source_registry import S
        entries = S
    members = {key: [] for key in _PUBLISHERS}
    for entry in entries:
        for key in publisher_ids(entry):
            members[key].append(entry)
    from app.clhear.l1.catalogs import CATALOG_SOURCES
    from app.clhear.l1.structured_catalogs import CONTRACTS
    from app.clhear.l1.publisher_catalogs import LIBRARIES
    from app.clhear.l1.legislation_catalogs import REFERENCES
    result = []
    for key, (name, kind) in _PUBLISHERS.items():
        categories = (["standards", "amendments and corrigenda", "official implementation guidance", "exposed edition archives", "attachments"]
                      if "standards" in kind else
                      ["rules and legislation", "official guidance", "rule filings", "enforcement", "examination reports", "exposed archives", "attachments"])
        result.append({"publisher_id": key, "name": name, "kind": kind, "profile_version": PROFILE_VERSION,
                       "boundaries": deepcopy(BOUNDARIES), "categories": categories,
                       "declared_source_keys": sorted(e["key"] for e in members[key]),
                       "known_reference_urls": sorted({e["canonical_url"] for e in members[key] if e.get("canonical_url")}),
                       "discovery_adapter": "finra_catalog" if key == "finra" else CONTRACTS[key]["adapter"] if key in CONTRACTS else "legislation_metadata_catalog" if key in REFERENCES else "publisher_publication_library" if key in LIBRARIES else "official_pdf_link_catalog" if key in CATALOG_SOURCES else None,
                       "catalog_status": "configured" if key == "finra" else "partial_category_support" if key in CATALOG_SOURCES or key in CONTRACTS or key in LIBRARIES or key in REFERENCES else "catalog_adapter_required",
                       "artifact_acquisition": "reviewed_authorized_artifact" if kind == "licensed_standards" else "reviewed_official_source",
                       "contract_references": CONTRACTS.get(key, {}).get("references", REFERENCES.get(key, LIBRARIES.get(key, {}).get("references", []))),
                       "catalog_routes": LIBRARIES.get(key, {}).get("roots", {}),
                       "remaining_contract_gaps": LIBRARIES.get(key, {}).get("gaps", []),
                       "language_policy": "Preserve every observed original and linked language expression; certify language/authority only from publisher metadata bound to exact artifact bytes",
                       "access_prerequisite": "Registered FCA Handbook API account and reviewed API terms; API excludes historical versions" if key == "fca" else None,
                       "expected_documents": None, "denominator_known": False})
    return result


def profiles_for_scope(scope, entries=None):
    profiles = publisher_profiles(entries)
    return [p for p in profiles if p["publisher_id"] == "finra"] if scope == "finra" else profiles


def coverage_findings(profiles):
    return [{"code": "publisher_catalog_unconfigured", "detail": "Publisher-wide catalog enumeration is not implemented; document denominator remains unknown.",
             "publisher_id": p["publisher_id"]} for p in profiles if not p["discovery_adapter"]]
