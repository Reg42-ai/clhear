# Copyright (C) 2026 Reg42 AI
# This file is part of CLHEAR. See LICENSE (AGPL-3.0-only).
"""Official documented Atom/OData metadata enumeration, within L1 workers only."""
import hashlib
import json
import re
from urllib.parse import parse_qs, urlencode, urljoin, urlparse
import xml.etree.ElementTree as ET

REFERENCES = {
    "uk-law": ["https://legislation.github.io/data-documentation/api/search.html", "https://legislation.github.io/data-documentation/formats/atom.html"],
    "au-law": ["https://www.legislation.gov.au/help-and-resources/using-the-legislation-register/data-share-and-reuse", "https://api.prod.legislation.gov.au/swagger/v1/swagger.json"],
}
ATOM = "{http://www.w3.org/2005/Atom}"
AU = "https://api.prod.legislation.gov.au/v1/Documents"
AU_FIELDS = "titleId,start,retrospectiveStart,rectificationVersionNumber,type,uniqueTypeNumber,volumeNumber,format,registerId,name,isAuthorised"
AU_ORDER = "titleId,start,retrospectiveStart,rectificationVersionNumber,type,uniqueTypeNumber,volumeNumber,format"
AU_TYPES = {"Primary", "ES", "SupportingMaterial", "IncorporatedByReference", "SupplementaryES"}
AU_ID = re.compile(r"[A-Z][0-9]{4}[A-Z][0-9]{5}")


def _entry(profile, key, title, url, adapter, fetch, jurisdiction):
    from app.clhear.l1.rights import RIGHTS
    basis = RIGHTS[adapter]
    return dict(key=key, name=title, short_name=title, instrument=title, canonical_url=url, publisher=profile["name"], issuer=profile["name"],
                publisher_ids=[profile["publisher_id"]], family="publisher-" + profile["publisher_id"], family_name=profile["name"] + " legislation",
                adapter=adapter, kind="law", jurisdiction=jurisdiction, license="open", rights_basis=basis.basis, rights_ref=basis.ref,
                rights_evidence_url=basis.evidence_url, source_role="document", relation="supplements", tier="binding", topics=[], registry_ids=[],
                wave=1, fetch=fetch, discovered_category="legislation")


def uk_atom(profile):
    seed = "https://www.legislation.gov.uk/all/data.feed"
    key = "uk-law/catalog/legislation"
    def decode(body, page):
        if b"<!DOCTYPE" in body.upper() or b"<!ENTITY" in body.upper():
            raise ValueError("External XML entities are forbidden")
        root = ET.fromstring(body)
        if root.tag != ATOM + "feed":
            raise ValueError("Expected publisher Atom feed")
        entries, links, findings = [], [], []
        for row in root.findall(ATOM + "entry"):
            ident = row.findtext(ATOM + "id", "")
            p = urlparse(ident)
            path = p.path.removeprefix("/id/").strip("/")
            if p.hostname not in {"www.legislation.gov.uk", "legislation.gov.uk"} or not re.fullmatch(r"[a-z]+/[0-9]{4}/[0-9]+", path):
                findings.append({"code": "publisher_identifier_unverified", "detail": "Legislation Atom identifier does not match the documented instrument identity."})
                continue
            url = "https://www.legislation.gov.uk/" + path
            entry = _entry(profile, path, row.findtext(ATOM + "title") or path, url, "uk_legislation", {"doc": path}, "UK")
            entry["publisher_updated_at"] = row.findtext(ATOM + "updated")
            entry["catalog_evidence"] = {"url": page["url"], "entry_id": ident, "sha256": hashlib.sha256(body).hexdigest()}
            # xml:lang on the actual publisher entry is metadata evidence;
            # original byte binding must still be supplied by the importer.
            lang = row.get("{http://www.w3.org/XML/1998/namespace}lang")
            if lang:
                entry["language_candidates"] = [{"language": lang, "method": "atom_entry_xml_lang", "evidence_ref": page["url"], "authority": "unknown"}]
            for relation in row.findall(ATOM + "link"):
                href = urljoin(page["url"], relation.get("href", ""))
                if relation.get("hreflang"):
                    entry.setdefault("language_candidates", []).append({"language": relation.get("hreflang"), "url": href, "method": "atom_alternate", "authority": "unknown"})
            entries.append(entry)
        next_links = root.findall(ATOM + "link[@rel='next']")
        if len(next_links) > 1:
            raise ValueError("Multiple next-page Atom cursors")
        if next_links:
            nxt = urljoin(page["url"], next_links[0].get("href", ""))
            p = urlparse(nxt)
            if p.scheme not in {"http", "https"} or p.hostname != "www.legislation.gov.uk" or p.path != "/all/data.feed" or p.username or p.password:
                raise ValueError("Untrusted Atom continuation")
            q = parse_qs(p.query)
            if set(q) - {"page", "sort"} or not q.get("page", [""])[0].isdigit() or len(q["page"]) != 1:
                raise ValueError("Unsupported Atom continuation")
            current = int(parse_qs(urlparse(page["url"]).query).get("page", ["1"])[0])
            if int(q["page"][0]) <= current:
                raise ValueError("Atom continuation did not advance")
            links.append(dict(url="https://www.legislation.gov.uk/all/data.feed?" + urlencode(q, doseq=True), source_key=key, category="legislation", role="collection"))
        if not entries:
            findings.append({"code": "empty_discovery_index", "detail": "Legislation feed contains no supported instrument records."})
        return dict(entries=entries, links=links, findings=findings, catalog_metadata={"feed_updated": root.findtext(ATOM + "updated"), "observed_identifiers": len(entries)})
    return [{"url": seed, "source_key": key, "category": "legislation"}], decode


def au_url(skip=0):
    if type(skip) is not int or not 0 <= skip <= 100000000:
        raise ValueError("Invalid OData continuation")
    return AU + "?" + urlencode({"$select": AU_FIELDS, "$filter": "format eq 'Pdf'", "$orderby": AU_ORDER, "$top": "100", "$skip": str(skip), "$count": "true"})


def au_odata(profile):
    key = "au-law/catalog/legislation"
    def decode(body, page):
        result = json.loads(body)
        rows = result.get("value")
        if not isinstance(rows, list) or len(rows) > 100:
            raise ValueError("Invalid official OData page")
        skip = int(parse_qs(urlparse(page["url"]).query).get("$skip", ["0"])[0])
        total = result.get("@odata.count")
        if type(total) is not int or total < 0 or skip + len(rows) > total:
            raise ValueError("Official OData total unavailable or inconsistent")
        entries, findings = [], []
        seen = set()
        for row in rows:
            if not AU_ID.fullmatch(str(row.get("titleId", ""))) or row.get("type") not in AU_TYPES or row.get("format") != "Pdf":
                raise ValueError("OData document identity outside reviewed schema")
            if type(row.get("isAuthorised")) is not bool:
                raise ValueError("OData original authority marker is not a Boolean")
            if row.get("bytes"):
                raise ValueError("Metadata response unexpectedly contains document bytes")
            if not row.get("isAuthorised") and row["type"] == "Primary":
                findings.append({"code": "original_authority_unverified", "detail": "Primary document lacks the publisher's authorised-original marker."})
            dates = []
            for name in ("start", "retrospectiveStart"):
                value = row.get(name)
                if not isinstance(value, str) or not re.fullmatch(r"[0-9]{4}-[0-9]{2}-[0-9]{2}T[0-9]{2}:[0-9]{2}:[0-9]{2}(?:\.[0-9]+)?(?:Z|[+-][0-9]{2}:[0-9]{2})?", value):
                    raise ValueError("Invalid official document version date")
                dates.append(value)
            numbers = [row.get(n) for n in ("rectificationVersionNumber", "uniqueTypeNumber", "volumeNumber")]
            if any(type(n) is not int or n < 0 for n in numbers):
                raise ValueError("Invalid official document volume/version identity")
            # Exact composite-key URL from the publisher's OpenAPI, not a
            # guessed filename or substitution for a missing artifact.
            path = ("/v1/documents(titleid='" + row["titleId"] + "',start=" + dates[0] + ",retrospectivestart=" + dates[1]
                    + ",rectificationversionnumber=" + str(numbers[0]) + ",type='" + row["type"] + "',uniqueTypeNumber=" + str(numbers[1])
                    + ",volumeNumber=" + str(numbers[2]) + ",format='Pdf')")
            url = "https://api.prod.legislation.gov.au" + path
            if url in seen:
                raise ValueError("Duplicate OData composite document identity")
            seen.add(url)
            source_key = "au/document/" + hashlib.sha256(url.encode()).hexdigest()[:24]
            entry = _entry(profile, source_key, row.get("name") or row["titleId"] + " " + row["type"], url, "au_legislation",
                           {"url": url, "kind": "pdf", "document_type": "publisher_publication"}, "AU")
            entry.update(publisher_document_id=row["titleId"], publisher_edition=dates[0], publisher_register_id=row.get("registerId"),
                         publisher_document_type=row["type"], publisher_volume=numbers[2], publisher_rectification=numbers[0],
                         catalog_evidence={"url": page["url"], "sha256": hashlib.sha256(body).hexdigest(), "method": "official_openapi_composite_identity"})
            entries.append(entry)
        links = []
        if skip + len(rows) < total:
            if not rows:
                raise ValueError("OData ended before its declared total")
            links.append(dict(url=au_url(skip + len(rows)), source_key=key, category="legislation", role="collection"))
        return dict(entries=entries, links=links, findings=findings, catalog_metadata={"declared_documents": total, "offset": skip, "observed_documents": len(rows), "ordered_by": AU_ORDER})
    return [{"url": au_url(), "source_key": key, "category": "legislation"}], decode


def discover_legislation(engine, store, profile, *, job_id, fetcher):
    from app.clhear.l1.discovery import run_batch
    key = profile["publisher_id"]
    seeds, decode = {"uk-law": uk_atom, "au-law": au_odata}[key](profile)
    entries, report = run_batch(engine, store, publisher_id=key, profile={**profile, "protocol_references": REFERENCES[key]}, seeds=seeds,
                               job_id=job_id, fetcher=fetcher, classify=lambda raw, parent: None, decoder=decode)
    report["findings"].append({"publisher_id": key, "code": "publisher_history_reconciliation_required", "detail":
        "Atom lists instrument identities; exposed point-in-time versions, Welsh expressions and effects/attachments require reconciliation." if key == "uk-law" else
        "OData enumerates current and historical PDF document identities, volumes and rectifications; offset paging needs stable inventory/count reconciliation and non-PDF-only attachments remain gaps."})
    report["complete"] = False
    return entries, report
