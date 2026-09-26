# Copyright (C) 2026 Reg42 AI
# This file is part of CLHEAR. See LICENSE (AGPL-3.0-only).
"""Official collection adapters using already-declared publisher references.

These adapters enumerate linked documents and pages in one known library.
Unconfigured sibling categories remain gaps: exhausting one library is not a
claim to cover the entire publisher. No catalog URL is invented from a name.
"""
import hashlib
from urllib.parse import parse_qsl, unquote, urlencode, urlparse, urlunparse

from app.clhear.l1.discovery import run_batch

CATALOG_SOURCES = {
    "cysec": "cy/l87i-2017", "nydfs": "nydfs/part200-500", "seychelles": "sc/securities-act-2007",
    "wolfsberg": "wolfsberg/standards", "isa": "il/securities-law-5728",
    "ftc": "cfr/16/255",
}


def discover_catalog(engine, store, profile, *, job_id, fetcher):
    from app.clhear.l1.source_registry import S
    from app.clhear.l1.structured_catalogs import CONTRACTS, discover_structured
    if profile["publisher_id"] in CONTRACTS:
        return discover_structured(engine, store, profile, job_id=job_id, fetcher=fetcher)
    from app.clhear.l1.legislation_catalogs import REFERENCES, discover_legislation
    from app.clhear.l1.publisher_catalogs import LIBRARIES, discover_library
    if profile["publisher_id"] in REFERENCES:
        return discover_legislation(engine, store, profile, job_id=job_id, fetcher=fetcher)
    if profile["publisher_id"] in LIBRARIES:
        return discover_library(engine, store, profile, job_id=job_id, fetcher=fetcher)
    key = CATALOG_SOURCES.get(profile["publisher_id"])
    source = next((e for e in S if e["key"] == key), None)
    if source is None:
        return {}, {"publisher_id": profile["publisher_id"], "complete": False, "checked_at": None,
                    "expected_documents": None, "denominator_known": False, "categories": [], "pages": [],
                    "findings": [{"publisher_id": profile["publisher_id"], "code": "publisher_catalog_unconfigured",
                                  "detail": "Publisher-wide catalog adapter and reviewed collection endpoints are required."}]}
    seed_url = source["canonical_url"]
    seed = urlparse(seed_url)
    catalog_key = f"{profile['publisher_id']}/catalog/declared-library"
    def classify(raw, parent):
        try:
            parsed = urlparse(raw)
            if parsed.scheme != "https" or parsed.hostname != seed.hostname or parsed.port not in (None, 443):
                return None
        except ValueError:
            return None
        if parsed.username or parsed.password or ".." in unquote(parsed.path).split("/"):
            return None
        query = parse_qsl(parsed.query, keep_blank_values=True)
        if any(k not in {"page", "year"} or not v.isdigit() for k, v in query):
            return None
        url = urlunparse(("https", seed.netloc, parsed.path, "", urlencode(sorted(query)), ""))
        if parsed.path.rstrip("/") == seed.path.rstrip("/"):
            return {"url": url, "source_key": catalog_key, "category": "declared-library", "role": "collection"}
        # The generic reviewed collection contract supports linked original PDF
        # artifacts only. HTML/dynamic/regulator-specific library shapes need
        # their own classifier; the unresolved-category finding prevents a pass.
        if query or not parsed.path.lower().endswith(".pdf"):
            return None
        digest = hashlib.sha256(url.encode()).hexdigest()[:24]
        document_key = f"{profile['publisher_id']}/document/{digest}"
        entry = {**source, "key": document_key, "canonical_url": url, "name": profile["name"] + " publication " + parsed.path.rsplit("/", 1)[-1],
                 "short_name": "Publisher document", "instrument": parsed.path.rsplit("/", 1)[-1],
                 "source_role": "document", "discovered_category": "declared-library", "publisher_ids": [profile["publisher_id"]],
                 "fetch": {"url": url, "kind": "pdf"}, "license": "restricted", "rights_basis": "derived_only"}
        # Generic original-PDF parser, still subject to independent certification.
        entry["adapter"] = source["adapter"]
        return {"url": url, "source_key": document_key, "category": "declared-library", "role": "document", "entry": entry}
    entries, result = run_batch(engine, store, publisher_id=profile["publisher_id"], profile=profile,
        seeds=[{"url": seed_url, "source_key": catalog_key, "category": "declared-library"}], job_id=job_id,
        fetcher=fetcher, classify=classify)
    result["findings"].append({"publisher_id": profile["publisher_id"], "code": "publisher_categories_unconfigured",
        "detail": "Known collection PDFs are enumerated; remaining publication categories, HTML documents, alternate official hosts and archives require publisher-specific contracts."})
    result["complete"] = False
    return entries, result
