# Copyright (C) 2026 Reg42 AI
# This file is part of CLHEAR. See LICENSE (AGPL-3.0-only).
"""Documented official metadata catalogs, called only by the L1 discovery worker.

Primary protocol references are recorded in CONTRACTS. These implementations
make useful finite progress; explicit residual gaps still block full-publisher
acceptance (languages, archive/attachment contracts and category review).
"""
import hashlib
import json
import re
from urllib.parse import parse_qs, quote, unquote, urlencode, urljoin, urlparse
import xml.etree.ElementTree as ET

from bs4 import BeautifulSoup

from app.clhear.l1.discovery import run_batch

CONTRACTS = {
    "eu-law": {"adapter": "cellar_celex_catalog", "references": [
        "https://op.europa.eu/en/web/webtools/linked-data-and-sparql-test-linda",
        "https://leos.pages.code.europa.eu/ai4drpm/_modules/ai4drpm/utils/sparql_utils.html"],
        "coverage": "CELEX secondary legislation and consolidated texts, exposed amendment/corrigendum identifiers, and publisher-declared language expressions"},
    "esma": {"adapter": "esma_typed_library", "references": ["https://www.esma.europa.eu/databases-library/esma-library?page=0"],
        "coverage": "Typed compliance documents in the paginated ESMA library, including linked original PDF attachments"},
    "us-law": {"adapter": "govinfo_collection_sitemaps", "references": ["https://www.govinfo.gov/sitemaps", "https://github.com/usgpo/sitemap"],
        "coverage": "USCODE, annual CFR and Federal Register exposed collection/year indexes and linked package originals"},
}
CELLAR = "https://publications.europa.eu/webapi/rdf/sparql"
ESMA = "https://www.esma.europa.eu/databases-library/esma-library"
ESMA_PAGE_SIZE = 20
GOVINFO = {k: f"https://www.govinfo.gov/sitemap/{k}_sitemap_index.xml" for k in ("USCODE", "CFR", "FR")}
EU_LANGUAGES = dict(zip("BUL CES DAN DEU ELL ENG EST FIN FRA GLE HRV HUN ITA LAV LIT MLT NLD POL POR RON SLK SLV SPA SWE".split(),
                        "bg cs da de el en et fi fr ga hr hu it lv lt mt nl pl pt ro sk sl es sv".split()))


def _entry(publisher, key, title, url, adapter, family, *, kind="guidance", fetch=None):
    # Reuse the established exact official-publisher policy. Enumeration must
    # not relabel existing public-domain/open-licence works as closed material.
    # Catalog permissions remain separately reviewed; no grants are created.
    from app.clhear.l1.rights import RIGHTS
    basis = RIGHTS[adapter]
    return {"key": key, "name": title, "short_name": title, "instrument": title, "canonical_url": url,
            "publisher": publisher["name"], "issuer": publisher["name"], "publisher_ids": [publisher["publisher_id"]],
            "family": "publisher-" + publisher["publisher_id"], "family_name": publisher["name"] + " regulatory library", "kind": kind, "jurisdiction": "EU" if publisher["publisher_id"] in {"eu-law", "esma"} else "US",
            "adapter": adapter, "license": "open", "rights_basis": basis.basis, "rights_ref": basis.ref,
            "rights_evidence_url": basis.evidence_url, "source_role": "document",
            "relation": "supplements", "tier": "binding" if kind in {"law", "regulation"} else "informative",
            "topics": [], "registry_ids": [], "wave": 1, "fetch": fetch or {"url": url, "kind": "pdf"},
            "discovered_category": publisher["publisher_id"] + "-library"}


def _link(url, key, category, *, entry=None, terminal=False):
    return {"url": url, "source_key": key, "category": category, "role": "document" if entry else "collection",
            "terminal": terminal, **({"entry": entry} if entry else {})}


def _safe(url, host):
    parsed = urlparse(url)
    try:
        return (parsed.scheme == "https" and parsed.hostname == host and parsed.port in (None, 443)
                and not parsed.username and not parsed.password and ".." not in unquote(parsed.path).split("/"))
    except ValueError:
        return False


def cellar_url(after=""):
    if after and not re.fullmatch(r"[03][0-9]{4}[A-Z][A-Za-z0-9()/_-]{1,50}", after):
        raise ValueError("Invalid CELEX continuation")
    # Keyset pagination remains bounded and does not rely on unstable offsets.
    query = '''PREFIX cdm: <http://publications.europa.eu/ontology/cdm#>
SELECT ?celex (GROUP_CONCAT(DISTINCT STR(?language);separator="|") AS ?languages) WHERE {
 GRAPH ?g { ?resource cdm:resource_legal_id_celex ?value . }
 BIND(STR(?value) AS ?celex)
 FILTER(REGEX(?celex, "^[03][0-9]{4}[A-Z]"))
 FILTER(?celex > "''' + after + '''")
 OPTIONAL { ?expression cdm:expression_belongs_to_work ?resource ;
                         cdm:expression_uses_language ?language . }
} GROUP BY ?celex ORDER BY ?celex LIMIT 100'''
    return CELLAR + "?" + urlencode({"query": query, "format": "application/sparql-results+json"})


def _cellar(profile):
    catalog_key = "eu-law/catalog/celex"
    seeds = [{"url": cellar_url(), "source_key": catalog_key, "category": "eu-law-library"}]
    def decode(body, page):
        result = json.loads(body)
        bindings = result["results"]["bindings"]
        if not isinstance(bindings, list) or len(bindings) > 100:
            raise ValueError("Invalid Cellar catalog response")
        ids = [row["celex"]["value"] for row in bindings]
        if ids != sorted(set(ids)) or any(not re.fullmatch(r"[03][0-9]{4}[A-Z][A-Za-z0-9()/_-]{1,50}", c) for c in ids):
            raise ValueError("Invalid or unordered CELEX catalog identifiers")
        request_query = parse_qs(urlparse(page.get("url", "")).query).get("query", [""])[0]
        cursor = re.search(r'FILTER\(\?celex > "([^"]*)"\)', request_query)
        if cursor and ids and ids[0] <= cursor.group(1):
            raise ValueError("Cellar continuation did not advance")
        entries, findings = [], []
        for celex, row in zip(ids, bindings):
            raw_languages = row.get("languages", {}).get("value", "")
            language_codes = set()
            for language in raw_languages.split("|") if raw_languages else []:
                match = re.fullmatch(r"https?://publications.europa.eu/resource/authority/language/([A-Z]{3})(?:/LNG[0-9]+)?", language)
                if not match or match.group(1) not in EU_LANGUAGES:
                    findings.append({"code": "cellar_language_expression_unverified", "detail": "Publisher expression language is not in the reviewed language authority contract.", "publisher_reference": celex})
                    continue
                language_codes.add(match.group(1))
            if not language_codes:
                findings.append({"code": "cellar_language_inventory_missing", "detail": "A CELEX work has no usable publisher expression metadata; the declared English dependency remains unverified.", "publisher_reference": celex})
            # EU legal language expressions are equally authoritative. English
            # satisfies original+English when available; metadata for every
            # other expression is retained without 24 duplicate import tasks.
            # If English is absent, preserve one identified original language
            # and let the separate English-view workflow handle translation.
            selected = {"ENG"} if "ENG" in language_codes or not language_codes else {sorted(language_codes)[0]}
            for language in sorted(selected):
                iso = EU_LANGUAGES[language]
                url = "https://eur-lex.europa.eu/legal-content/" + iso.upper() + "/TXT/?uri=CELEX:" + quote(celex, safe="()")
                entry = _entry(profile, "celex/" + celex + ("" if language == "ENG" else "/" + iso), "EU legal publication " + celex + " · " + iso, url, "eur_lex", "eu-data",
                                  kind="regulation" if celex[5] == "R" else "law",
                                  fetch={"celex": celex, "celex_version": celex, "language": language})
                entry["document_group_key"] = "celex/" + celex
                entry["publisher_language_expressions"] = [EU_LANGUAGES[code] for code in sorted(language_codes)]
                entry["language_candidates"] = [{"language": iso, "authority": "unknown", "method": "cellar_expression_uses_language", "evidence_ref": page.get("url", CELLAR)}] if language in language_codes else []
                entry["catalog_evidence"] = {"url": page.get("url", CELLAR), "sha256": hashlib.sha256(body).hexdigest(), "expression_language": language if language in language_codes else None}
                entries.append(entry)
        # Document acquisition belongs to the source task, not this metadata
        # enumeration. Missing originals remain in the audit's denominator.
        links = [_link(cellar_url(ids[-1]), catalog_key, "eu-law-library")] if len(ids) == 100 else []
        return {"entries": entries, "links": links, "findings": findings, "catalog_metadata": {"observed_identifiers": len(ids), "observed_language_documents": len(entries), "last_identifier": ids[-1] if ids else None}}
    return seeds, decode


def _esma(profile):
    catalog_key = "esma/catalog/library"
    seeds = [{"url": ESMA + "?page=0", "source_key": catalog_key, "category": "esma-library"}]
    allowed = {"guidelines & recommendations", "technical standards", "q&a", "compliance table", "decision", "opinion", "statement", "final report", "cesr document", "investor warning"}
    excluded = {"press release", "speech", "vacancy", "annual report"}
    def decode(body, page):
        soup = BeautifulSoup(body, "html.parser")
        table = next((t for t in soup.find_all("table") if "Main document" in t.get_text(" ", strip=True) and "Reference" in t.get_text(" ", strip=True)), None)
        if table is None:
            raise ValueError("ESMA publication table unavailable")
        headers = [h.get_text(" ", strip=True).lower() for h in table.find_all("th")]
        names = ["reference", "title", "sections", "type", "main document"]
        if not all(name in headers for name in names):
            raise ValueError("ESMA library column contract changed")
        entries, findings = [], []
        rows = [r.find_all("td", recursive=False) for r in table.find_all("tr")]
        rows = [r for r in rows if len(r) >= len(headers)]
        for cells in rows:
            record = {name: cells[headers.index(name)] for name in names}
            doctype = record["type"].get_text(" ", strip=True).lower()
            if doctype in excluded:
                continue
            ref = record["reference"].get_text(" ", strip=True)
            if doctype not in allowed:
                findings.append({"code": "publication_category_review_required", "detail": "A library row type requires compliance-scope classification.", "publisher_reference": ref, "document_type": doctype})
                continue
            links = record["main document"].find_all("a", href=True)
            accepted = 0
            for a in links:
                url = urljoin(page["url"], a["href"])
                if not _safe(url, "www.esma.europa.eu") or not urlparse(url).path.lower().endswith(".pdf"):
                    continue
                key = "esma/document/" + hashlib.sha256(url.encode()).hexdigest()[:24]
                entry = _entry(profile, key, record["title"].get_text(" ", strip=True), url, "esma", "eu-mifid")
                entry["publisher_reference"] = ref
                entries.append(entry)
                accepted += 1
            if not accepted:
                findings.append({"code": "original_link_unresolved", "detail": "An included library row has no supported official original link.", "publisher_reference": ref})
        links = []
        current = int(parse_qs(urlparse(page["url"]).query).get("page", ["0"])[0])
        totals = re.findall(r"\b([0-9][0-9, ]*)\s+documents\b", soup.get_text(" ", strip=True), re.I)
        total = int(re.sub(r"[, ]", "", totals[-1])) if totals else None
        if total is None:
            findings.append({"code": "catalog_total_unverified", "detail": "The ESMA catalog row total is unavailable."})
        elif rows and current * ESMA_PAGE_SIZE + len(rows) < total:
            links.append(_link(ESMA + "?page=" + str(current + 1), catalog_key, "esma-library"))
            if len(rows) != ESMA_PAGE_SIZE:
                findings.append({"code": "catalog_page_size_changed", "detail": "Observed ESMA page size differs from its catalog contract; enumeration cannot certify the row total."})
        if len(rows) > ESMA_PAGE_SIZE:
            findings.append({"code": "catalog_page_size_changed", "detail": "Observed ESMA page size exceeds its catalog contract; enumeration requires review."})
        elif not rows and total:
            findings.append({"code": "catalog_page_empty", "detail": "Publisher reports documents but the catalog page is empty."})
        return {"entries": entries, "links": links, "findings": findings,
                "catalog_metadata": {"declared_rows": total, "observed_rows": len(rows), "page": current}}
    return seeds, decode


def _govinfo(profile):
    seeds = [{"url": url, "source_key": "us-law/catalog/" + code, "category": code} for code, url in GOVINFO.items()]
    def decode(body, page):
        if b"<!DOCTYPE" in body.upper() or b"<!ENTITY" in body.upper():
            raise ValueError("External XML entities are forbidden")
        if urlparse(page["url"]).path.startswith("/sitemap/"):
            root = ET.fromstring(body)
            kind = root.tag.rsplit("}", 1)[-1]
            if kind not in {"sitemapindex", "urlset"}:
                raise ValueError("Invalid GovInfo sitemap")
            links, findings = [], []
            for item in root:
                loc = next((e.text.strip() for e in item if e.tag.rsplit("}", 1)[-1] == "loc" and e.text), None)
                if not loc or not _safe(loc, "www.govinfo.gov"):
                    findings.append({"code": "catalog_link_unverified", "detail": "Sitemap link is absent or outside the official distribution host."})
                    continue
                path = urlparse(loc).path
                if kind == "sitemapindex" and re.fullmatch(r"/sitemap/" + re.escape(page["category"]) + r"_[A-Za-z0-9_-]+\.xml", path):
                    links.append(_link(loc, page["source_key"], page["category"]))
                elif kind == "urlset" and path.startswith("/app/details/" + page["category"] + "-"):
                    links.append(_link(loc, page["source_key"], page["category"]))
                else:
                    findings.append({"code": "catalog_link_unverified", "detail": "Sitemap entry does not match its reviewed collection contract."})
            if not links:
                findings.append({"code": "empty_discovery_index", "detail": "GovInfo collection yielded no supported entries."})
            return {"links": links, "entries": [], "findings": findings}
        package = urlparse(page["url"]).path.rsplit("/", 1)[-1]
        soup = BeautifulSoup(body, "html.parser")
        originals = {urljoin(page["url"], a["href"]) for a in soup.find_all("a", href=True)
                     if "/content/pkg/" + package + "/pdf/" in a["href"] and urlparse(a["href"]).path.endswith(".pdf")}
        originals = {u for u in originals if _safe(u, "www.govinfo.gov")}
        if len(originals) != 1:
            return {"entries": [], "links": [], "findings": [{"code": "original_link_unresolved", "detail": "An exposed GovInfo package needs one exact official PDF original.", "package_id": package}]}
        url = originals.pop()
        title = soup.find("h1")
        entry = _entry(profile, "govinfo/package/" + package, title.get_text(" ", strip=True) if title else package,
                       url, "govinfo_us", "us-broker-dealer", kind="law" if page["category"] == "USCODE" else "regulation")
        entry["discovered_category"] = page["category"]
        return {"entries": [entry], "links": [], "findings": []}
    return seeds, decode


def discover_structured(engine, store, profile, *, job_id, fetcher):
    publisher_id = profile["publisher_id"]
    seeds, decoder = {"eu-law": _cellar, "esma": _esma, "us-law": _govinfo}[publisher_id](profile)
    entries, result = run_batch(engine, store, publisher_id=publisher_id, profile={**profile, "contract": CONTRACTS[publisher_id]},
        seeds=seeds, job_id=job_id, fetcher=fetcher, classify=lambda raw, parent: None, decoder=decoder)
    result["findings"].append({"publisher_id": publisher_id, "code": "publisher_categories_unconfigured",
        "detail": "This catalog contract requires full category, exposed archive, language and attachment review before it can certify the whole publisher."})
    result["complete"] = False
    return entries, result
