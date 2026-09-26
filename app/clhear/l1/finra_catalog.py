# Copyright (C) 2026 Reg42 AI
# This file is part of CLHEAR. See LICENSE (AGPL-3.0-only).
"""FINRA publication and exposed archive metadata decoder, called by workers."""
import re
from urllib.parse import urljoin, urlparse
from bs4 import BeautifulSoup
from app.clhear.l1.publisher_catalogs import form_continuations, language_metadata

# The incorporated NYSE index HTML only links series headings plus 409/435.
# Individual articles exist at /incorporated-nyse-rules/rule-N (verified
# 20 Sep 2026: rule-1 "The Exchange", rule-312, rule-409). Series titles
# on the official pages name the published ranges.
_NYSE_RANGE = re.compile(r"Rules?\s+(\d+)([A-Za-z])?\s*[–—-]\s*(\d+)([A-Za-z])?", re.I)
_NYSE_SINGLE = re.compile(r"\bRule\s+(\d+)([A-Za-z])?\b", re.I)
_NYSE_INDEX_PATH = "/rules-guidance/rulebooks/incorporated-nyse-rules"
MAX_NYSE_OFFICIAL_LEAVES = 400
# Official series page omitted from the collapsed book nav; its h1 is
# "Operation of Member Organizations (Rules 325–465)".
NYSE_EXTRA_SERIES = (
    "https://www.finra.org/rules-guidance/rulebooks/incorporated-nyse-rules-5",
)


def official_nyse_rule_numbers(text):
    """Rule numbers named on an official incorporated NYSE page."""
    numbers, seen = [], set()

    def add(num, letter=None):
        token = str(int(num)) + (letter.upper() if letter else "")
        if token not in seen:
            seen.add(token)
            numbers.append(token)

    for match in _NYSE_RANGE.finditer(text or ""):
        lo, hi = int(match.group(1)), int(match.group(3))
        if hi < lo or hi - lo > MAX_NYSE_OFFICIAL_LEAVES:
            continue
        for n in range(lo, hi + 1):
            add(n)
        if match.group(2):
            add(match.group(1), match.group(2))
        if match.group(4):
            add(match.group(3), match.group(4))
    for match in _NYSE_SINGLE.finditer(text or ""):
        add(match.group(1), match.group(2))
    return numbers


def official_nyse_leaf_urls(soup):
    """Official /rule-N URLs named by series titles and linked rule headings."""
    texts = []
    title = soup.find("h1")
    if title:
        texts.append(title.get_text(" ", strip=True))
    area = soup.find("main") or soup
    for anchor in area.find_all("a"):
        texts.append(anchor.get_text(" ", strip=True))
    urls = []
    for token in official_nyse_rule_numbers(" \n ".join(texts))[:MAX_NYSE_OFFICIAL_LEAVES]:
        slug = token[:-1] + token[-1].lower() if token[-1:].isalpha() else token
        urls.append("https://www.finra.org" + _NYSE_INDEX_PATH + "/rule-" + slug)
    return urls


def decoder(classify):
    def decode(body, page):
        if body.startswith(b"%PDF-"):
            return {"entries": [], "links": [], "findings": []}
        soup = BeautifulSoup(body, "html.parser")
        area = soup.find("main") or soup.find("article") or soup.body or soup
        if re.search(r"access denied|solve this CAPTCHA", area.get_text(" ", strip=True), re.I):
            raise ValueError("FINRA catalog access unavailable")
        links, entries, findings = {}, {}, []
        if page["role"] == "document":
            own = classify(page["url"], page)
            if own and own.get("entry"):
                entry = own["entry"]
                title = soup.find("h1")
                if title:
                    entry["name"] = entry["short_name"] = title.get_text(" ", strip=True)
                evidence = language_metadata(body, publisher_id="finra", document_key=entry["key"], url=page["url"])
                if evidence:
                    entry["language_evidence"] = evidence
                entries[entry["key"]] = entry
        for a in area.find_all("a", href=True):
            if a.find_parent(["header", "footer", "aside"]):
                continue
            raw = urljoin(page["url"], a["href"])
            found = classify(raw, page)
            if not found:
                if "next" in (a.get("rel") or []):
                    findings.append({"code": "unsupported_pagination", "detail": "An exposed next-page link requires an expanded FINRA catalog contract.", "link_url": raw})
                elif re.search(r"\.pdf(?:\?|$)", raw, re.I):
                    findings.append({"code": "finra_attachment_host_unverified", "detail": "A linked original is outside reviewed FINRA distribution hosts.", "link_url": raw})
                continue
            if found["url"] == page["url"]:
                continue
            links[found["url"]] = {k: found[k] for k in ("url", "source_key", "category", "role", "terminal") if k in found}
            if found.get("entry"):
                entry = found["entry"]
                if a.get_text(strip=True):
                    entry["name"] = a.get_text(" ", strip=True)
                entry["catalog_evidence"] = {"url": page["url"], "method": "official_link"}
                if page["role"] == "document":
                    entry["related_document_key"] = page["source_key"]
                entries[entry["key"]] = entry
        if "incorporated-nyse-rules" in (page.get("url") or ""):
            # Series titles name published ranges; the index HTML does not
            # link each /rule-N article. Enumerate those official paths.
            if urlparse(page["url"]).path.rstrip("/") == _NYSE_INDEX_PATH:
                for extra in NYSE_EXTRA_SERIES:
                    found = classify(extra, page)
                    if found and found["url"] != page["url"]:
                        links[found["url"]] = {k: found[k] for k in ("url", "source_key", "category", "role", "terminal") if k in found}
            for url in official_nyse_leaf_urls(soup):
                found = classify(url, page)
                if not found or not found.get("entry") or found["url"] == page["url"]:
                    continue
                links[found["url"]] = {k: found[k] for k in ("url", "source_key", "category", "role", "terminal") if k in found}
                entry = found["entry"]
                entry.setdefault("catalog_evidence", {"url": page["url"], "method": "official_rulebook_path"})
                entries[entry["key"]] = entry
        form_links, form_findings = form_continuations(area, page, classify)
        links.update({link["url"]: link for link in form_links})
        findings.extend(form_findings)
        if area.select("[data-drupal-views-infinite-scroll-content-wrapper], [data-total-pages]"):
            findings.append({"code": "dynamic_enumeration_unverified", "detail": "FINRA dynamic continuation requires a verified endpoint contract."})
        if page["role"] == "collection" and not links:
            findings.append({"code": "empty_discovery_index", "detail": "Collection returned no supported records or continuation."})
        return {"entries": list(entries.values()), "links": list(links.values()), "findings": findings,
                "catalog_metadata": {"method": "official_links_and_observed_get_facets", "observed_links": len(links)}}
    return decode
