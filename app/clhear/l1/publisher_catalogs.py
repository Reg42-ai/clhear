# Copyright (C) 2026 Reg42 AI
# This file is part of CLHEAR. See LICENSE (AGPL-3.0-only).
"""Reviewed official publication-library routes (metadata, never replacement text).

Every seed below is a publisher catalog observed on 2026-09-16 or an existing
registered collection. Only links actually returned by those catalogs become
new document URLs. GET pagination, exposed archive links, language alternates
and attachments remain in the persisted worker frontier. Forms/JS APIs that
cannot be enumerated and unreviewed categories are explicit coverage gaps.
"""
import hashlib
import re
from urllib.parse import parse_qsl, unquote, urlencode, urljoin, urlparse, urlunparse
from bs4 import BeautifulSoup

VERSION = "2026-09-16.2"
EXTERNAL_DEPENDENCIES = {"fca", "mas", "it-consob", "iso", "aicpa", "pci", "ifrs"}


def contract(adapter, jurisdiction, roots, *, collections=(), documents=(), files=(), query=(), native=(), gaps=(), licensed=False):
    return dict(adapter=adapter, jurisdiction=jurisdiction, roots=roots,
                collections=list(collections), documents=list(documents), files=list(files),
                query=list(query), native_languages=list(native), gaps=list(gaps), licensed=licensed,
                references=list(roots.values()), version=VERSION)


# Route patterns are intentionally publisher-specific. A hostname is not a
# blanket authorization to traverse navigation, filings, news or arbitrary APIs.
LIBRARIES = {
    "fca": contract("fca_handbook", "UK", {
        "publications": "https://www.fca.org.uk/publications"},
        collections=[r"/publications/?$", r"/publications/(?:search-results|policy-and-guidance|notices-and-decisions)/?$"],
        documents=[r"/publications/(?:policy-statements|guidance-consultations|finalised-guidance|consultation-papers|final-notices|decision-notices|warning-notices|dear-ceo-letters)/[^/]+/?$"],
        files=["www.fca.org.uk"], query=["page", "year", "category", "p_search_term", "sort_by"], native=["en"],
        gaps=["The authenticated Handbook API and its separately exposed historical editions require a reviewed API credential/contract."]),
    "sec": contract("sec_edgar", "US", {
        "rules": "https://www.sec.gov/rules-regulations/rulemaking-activity",
        "sro": "https://www.sec.gov/rules-regulations/self-regulatory-organization-rulemaking",
        "enforcement": "https://www.sec.gov/enforcement-litigation"},
        collections=[r"/rules-regulations/(?:rulemaking-activity|self-regulatory-organization-rulemaking)(?:/[^/]+)?/?$", r"/enforcement-litigation(?:/[a-z-]+)?/?$", r"/(?:rules|litigation)/[a-z-]+(?:/[0-9]{4})?\.s?html$"],
        documents=[r"/rules-regulations/[0-9]{4}/[0-9]{2}/[^/]+/?$", r"/enforcement-litigation/(?:litigation-releases|administrative-proceedings)/[^/]+/?$", r"/(?:rules|litigation)/.+\.(?:htm|html|pdf)$"],
        files=["www.sec.gov"], query=["page", "year", "field_display_title_value"], native=["en"],
        gaps=["Staff interpretive guidance, examination risk-alert catalogs and pre-modern rulemaking archive category reconciliation remain required."]),
    "fincen": contract("sec_edgar", "US", {
        "guidance": "https://www.fincen.gov/resources/statutes-regulations/guidance",
        "rulings": "https://www.fincen.gov/resources/statutes-regulations/administrative-rulings"},
        collections=[r"/resources/statutes-regulations/(?:guidance|administrative-rulings)/?$"],
        documents=[r"/resources/(?:statutes-regulations/(?:guidance|administrative-rulings)|advisories)/[^/]+/?$"],
        files=["www.fincen.gov"], query=["page", "year"], native=["en"],
        gaps=["Alerts, enforcement actions, special measures and Federal Register cross-catalog reconciliation remain required."]),
    "irs": contract("irs_gov", "US", {"bulletins": "https://www.irs.gov/irb"},
        collections=[r"/irb/?$", r"/irb/[0-9]{4}/?$", r"/irb/(?:previous|archive)[^/]*$"],
        documents=[r"/irb/[0-9]{4}-[0-9]{1,2}_IRB(?:/.*)?$"],
        files=["www.irs.gov"], query=["page", "year", "field_pup_historical_1"], native=["en"],
        gaps=["Separate tax forms/instructions and official guidance outside Internal Revenue Bulletins need category reconciliation."]),
    "ofac": contract("sec_edgar", "US", {
        "programs": "https://ofac.treasury.gov/sanctions-programs-and-country-information",
        "faqs": "https://ofac.treasury.gov/faqs/all-faqs",
        "archives": "https://ofac.treasury.gov/archive-inactive-sanctions-programs"},
        collections=[r"/sanctions-programs-and-country-information(?:/[^/]+)?/?$", r"/faqs/(?:all-faqs|topic/[0-9]+)/?$", r"/archive-inactive-sanctions-programs/?$"],
        documents=[r"/faqs/[0-9]+/?$", r"/recent-actions/[0-9]{8}[^/]*$", r"/media/[0-9]+/download$"],
        files=["ofac.treasury.gov"], query=["page", "year", "inline"], native=["en"],
        gaps=["Civil penalties, licenses and non-SDN list datasets require explicit category/data-schema reconciliation."]),
    "nydfs": contract("nydfs", "US", {"regulations": "https://www.dfs.ny.gov/industry_guidance/regulations"},
        collections=[r"/industry_guidance/(?:regulations|circular_letters|industry_letters)(?:/[^/]+)?/?$"],
        documents=[r"/industry_guidance/(?:regulations|circular_letters|industry_letters)/[^/]+/[^/]+/?$"],
        files=["www.dfs.ny.gov"], query=["page", "year"], native=["en"],
        gaps=["Enforcement orders and historical predecessor agency archive reconciliation remain required."]),
    "nasdaq": contract("nasdaq", "US", {"rules": "https://listingcenter.nasdaq.com/rulebook/nasdaq/rules"},
        collections=[r"/rulebook/[^/]+/(?:rules|filings)/?$"],
        documents=[r"/rulebook/[^/]+/rules/[^/]+/?$", r"/assets/.*\.pdf$"],
        files=["listingcenter.nasdaq.com"], query=["page", "year"], native=["en"],
        gaps=["POST/search-driven rule-filing pagination and disciplinary catalogs need a documented endpoint contract."]),
    "nist": contract("sec_edgar", "US", {"publications": "https://csrc.nist.gov/Publications"},
        collections=[r"/[Pp]ublications/?$", r"/[Pp]ublications/(?:search|sp800|sp1800|fips|sp|nistir|cswp|final-pubs|drafts-open-for-comment|drafts|withdrawn)/?$"],
        documents=[r"/[Pp]ublications/(?:detail|sp|fips|nistir|cswp)/.+/(?:final|draft|withdrawn)$", r"/pubs/(?:sp|fips|ir|cswp)/.+/(?:final(?:-\(\d+\))?|ipd|fpd|draft|withdrawn)$"],
        files=["csrc.nist.gov", "nvlpubs.nist.gov"], query=["page", "pageNumber", "status", "series"], native=["en"],
        gaps=["Search-only withdrawn editions and mixed research publication types need catalog row reconciliation; unrelated research is excluded."]),
    "cysec": contract("cysec", "CY", {"legislation": "https://www.cysec.gov.cy/en-GB/legislation/investment-services/"},
        collections=[r"/(?:en-GB|el-GR)/legislation/.*", r"/(?:en-GB|el-GR)/(?:circulars|decisions|consultations)/.*"],
        files=["www.cysec.gov.cy"], query=["page", "year"], native=["el"],
        gaps=["Non-investment-services categories and enforcement archives require category reconciliation; English translation authority remains document-specific."]),
    "asic": contract("asic", "AU", {"guides": "https://asic.gov.au/regulatory-resources/find-a-document/regulatory-guides/"},
        collections=[r"/regulatory-resources/find-a-document/(?:regulatory-guides|information-sheets|reports|consultation-papers|legislative-instruments|regulatory-document-updates)/?$", r"/regulatory-resources/find-a-document/regulatory-document-updates/[0-9]{4}/?$"],
        documents=[r"/regulatory-resources/find-a-document/(?:regulatory-guides|information-sheets|reports|consultation-papers|legislative-instruments)/[^/]+/?$"],
        files=["asic.gov.au", "download.asic.gov.au"], query=["page", "year"], native=["en"],
        gaps=["Enforcement and examination catalogs require separate publication-category reconciliation."]),
    "austrac": contract("asic", "AU", {
        "guidance": "https://www.austrac.gov.au/industry-and-business/obligations-and-guidance",
        "archive": "https://www.austrac.gov.au/business/how-comply-and-report-guidance-and-resources/guidance-resources/all-resources"},
        collections=[r"/business/how-comply-and-report-guidance-and-resources/guidance-resources/all-resources/?$", r"/industry-and-business/obligations-and-guidance/?$"],
        documents=[r"/industry-and-business/obligations-and-guidance/.+", r"/business/how-comply-and-report-guidance-and-resources/.+"],
        files=["www.austrac.gov.au"], query=["page", "field_guidance_topics_target_id", "field_industries_target_id", "field_resource_type_target_id"], native=["en"],
        gaps=["Enforcement notices and historical AML/CTF instruments need category reconciliation."]),
    "sg-law": contract("sg_legislation", "SG", {"acts": "https://sso.agc.gov.sg/Browse/Act/Current", "subsidiary": "https://sso.agc.gov.sg/Browse/SL/Current", "gazette": "https://sso.agc.gov.sg/Browse/SL-Supp"},
        collections=[r"/Browse/(?:Act|SL|Act-Supp|SL-Supp|Act-Reved|SL-Reved)(?:/(?:Current|Repealed|Revoked|Spent|Uncommenced))?/?$"],
        documents=[r"/(?:Act|SL|Acts-Supp|SL-Supp)/[^/]+/?$"],
        files=["sso.agc.gov.sg"], query=["PageIndex", "PageSize", "SortBy", "SortOrder", "ViewType", "DocDate", "ValidDate", "TransactionDate"], native=["en"],
        gaps=["POST-only browse filters and point-in-time history not exposed as links remain unresolved."]),
    "mas": contract("mas", "SG", {"regulation": "https://www.mas.gov.sg/regulation"},
        collections=[r"/regulation/?$", r"/regulation/(?:notices|guidelines|circulars|consultations|acts)/?$"],
        documents=[r"/regulation/(?:notices|guidelines|circulars|consultations|acts)/[^/]+/?$"],
        files=["www.mas.gov.sg"], query=["page", "year"], native=["en"],
        gaps=["Publisher currently presents a maintenance/access page to research requests; no bypass or complete inventory is claimed."]),
    "sg-privacy": contract("sg_legislation", "SG", {"guidance": "https://www.pdpc.gov.sg/guidelines-and-consultation"},
        collections=[r"/(?:guidelines-and-consultation|all-commissions-decisions)/?$"],
        documents=[r"/(?:guidelines-and-consultation|all-commissions-decisions)/[0-9]{4}/[0-9]{2}/[^/]+/?$", r"/-/media/.+\.ashx$"],
        files=["www.pdpc.gov.sg"], query=["page", "year", "la"], native=["en"],
        gaps=["Commission decision pagination and archived consultation closure documents require reconciliation."]),
    "adgm": contract("adgm", "AE-ADGM", {"rules": "https://www.adgm.com/legal-framework/rules-and-regulations", "guidance": "https://www.adgm.com/legal-framework/guidance-and-policy-statements"},
        collections=[r"/legal-framework/(?:rules-and-regulations|guidance-and-policy-statements|public-consultations|notices-of-publication)/?$", r"/rulebook/?$"],
        documents=[r"/rulebook/[^/]+/?$", r"/download/.+"],
        files=["www.adgm.com", "assets.adgm.com", "en.adgm.thomsonreuters.com"], query=["page", "year"], native=["en"],
        gaps=["ADGM rulebook historical effective-date controls need an exact provider contract; UAE English translations are not authoritative originals."]),
    "uae-law": contract("uae", "AE", {"arabic": "https://uaelegislation.gov.ae/ar", "english": "https://uaelegislation.gov.ae/en"},
        collections=[r"/(?:ar|en)/?$", r"/(?:ar|en)/legislations/?$", r"/(?:ar|en)/legislations/categories/[^/]+/?$"],
        documents=[r"/(?:ar|en)/legislations/[0-9]+/?$"],
        files=["uaelegislation.gov.ae", "www.uaelegislation.gov.ae"], query=["page", "keyword", "category"], native=["ar"],
        gaps=["Dynamic category filters and repealed historical editions are not an exhausted archive; English counterpart authority requires explicit evidence."]),
    "seychelles": contract("seychelles", "SC", {"legislation": "https://fsaseychelles.sc/legal-framework/legislation"},
        collections=[r"/legal-framework/(?:legislation|guidelines|circulars)(?:/[^/]+)?/?$"],
        files=["fsaseychelles.sc", "www.fsaseychelles.sc"], query=["page", "start", "limitstart", "limit"], native=["en"],
        gaps=["Enforcement and superseded-document archive categories require reconciliation."]),
    "malta-law": contract("malta", "MT", {"legislation": "https://legislation.mt/"},
        collections=[r"/?$", r"/(?:Legislation|Publications|Browse|Search)(?:/.*)?$"],
        documents=[r"/eli/(?:cap|sl|act|ln)/[A-Za-z0-9./_-]+$"],
        files=["legislation.mt"], query=["page", "lang", "year"], native=["mt", "en"],
        gaps=["Dynamic official catalog API and full historical edition traversal require a verified response schema."]),
    "mfsa": contract("malta", "MT", {"circulars": "https://www.mfsa.mt/publications/circulars/"},
        collections=[r"/publications/circulars/(?:[^/]+/)?$", r"/our-work/(?:[^/]+/)*$"],
        files=["www.mfsa.mt"], query=["page", "paged", "year"], native=["en"],
        gaps=["Rulebooks, decisions and JS-only circular date filters need category and pagination reconciliation."]),
    "fiau": contract("malta", "MT", {"procedures": "https://fiaumalta.org/procedures-guidance-2/"},
        collections=[r"/(?:procedures-guidance-2|implementing-procedures|guidance|enforcement)/?$"],
        files=["fiaumalta.org", "www.fiaumalta.org"], query=["page", "paged", "year"], native=["en"],
        gaps=["Administrative enforcement and superseded implementing-procedure archives require reconciliation."]),
    "gibraltar": contract("gibraltar", "GI", {"library": "https://www.fsc.gi/downloads?section=9&type=0", "aml": "https://www.fsc.gi/AMLCFTCPF_guidance_notes"},
        collections=[r"/downloads/?$", r"/AMLCFTCPF_guidance_notes/?$"],
        documents=[r"/legislations/[^/]+/?$"],
        files=["www.fsc.gi", "fsc.gi", "www.gibraltarlaws.gov.gi"], query=["section", "type", "page", "year"], native=["en"],
        gaps=["GFSC decisions and legislation-site archive enumeration need category reconciliation; unrelated speeches are excluded."]),
    "isa": contract("isa", "IL", {"legacy-library": "https://www.isa.gov.il/sites/ISAEng/1489/1511/Pages/default.aspx"},
        collections=[r"/sites/ISA(?:Eng)?/.+/Pages/default\.aspx$"],
        files=["www.isa.gov.il", "www.new.isa.gov.il", "new.isa.gov.il"], query=["page", "year"], native=["he"],
        gaps=["The legacy English collection does not enumerate the current Hebrew ISA publication catalog; new portal schema/archives remain unresolved."]),
    "il-privacy": contract("israel", "IL", {"authority": "https://www.gov.il/he/departments/the_privacy_protection_authority"},
        collections=[r"/(?:he|en)/departments/the_privacy_protection_authority(?:/.*)?$"],
        documents=[r"/(?:he|en)/departments/(?:legalInfo|legalinfo|reports|policies)/[^/]+/?$"],
        files=["www.gov.il"], query=["page", "skip", "limit"], native=["he"],
        gaps=["Gov.il dynamic collectors need a reviewed authority-specific API contract; links to other government departments are not assumed in scope."]),
    "un": contract("sec_edgar", "INT", {"sanctions": "https://main.un.org/securitycouncil/en/sanctions/information", "archives": "https://main.un.org/securitycouncil/en/content/repertoire/sanctions-and-other-committees"},
        collections=[r"/securitycouncil/(?:en|fr|ar|zh|ru|es)/(?:sanctions|content/repertoire)/.*"],
        documents=[r"/securitycouncil/(?:en|fr|ar|zh|ru|es)/content/(?:resolutions[^/]*|list-updates-unsc-consolidated-list)/?$"],
        files=["main.un.org", "docs.un.org"], query=["page", "year"], native=[],
        gaps=["Resolution symbol/language identity and terminated committee archives require structured UN document metadata; all linked language versions are retained."]),
    "eu-sanctions": contract("esma", "EU", {"sanctions": "https://finance.ec.europa.eu/eu-and-world/sanctions-restrictive-measures_en"},
        collections=[r"/eu-and-world/sanctions-restrictive-measures.*"],
        documents=[r"/document/download/[A-Za-z0-9_-]+$"],
        files=["finance.ec.europa.eu"], query=["page", "filename", "prefLang"], native=[],
        gaps=["Sanctions-map legal-act relationships and historical official consolidated-list datasets need structured reconciliation."]),
    "ofsi": contract("fca_handbook", "UK", {"authority": "https://www.gov.uk/government/organisations/office-of-financial-sanctions-implementation"},
        collections=[r"/government/organisations/office-of-financial-sanctions-implementation(?:/.*)?$", r"/government/collections/[^/]*(?:sanctions|ofsi)[^/]*$"],
        documents=[r"/government/publications/[^/]*(?:sanctions|ofsi)[^/]*(?:/[^/]+)?$"],
        files=["www.gov.uk", "assets.publishing.service.gov.uk"], query=["page", "year"], native=["en"],
        gaps=["Complete OFSI publication ownership and former list replacement/history need GOV.UK metadata API reconciliation."]),
    "fatf": contract("fatf", "INT", {"publications": "https://www.fatf-gafi.org/en/publications.html", "french": "https://www.fatf-gafi.org/fr/publications.html", "guidance": "https://www.fatf-gafi.org/en/guidance.html"},
        collections=[r"/(?:en|fr)/(?:publications|guidance|recommendations)\.html$", r"/publications\.html$"],
        documents=[r"/(?:en|fr)/publications/[^/]+/[^/]+\.html$"],
        files=["www.fatf-gafi.org"], query=["b", "hf", "page", "year"], native=["en", "fr"],
        gaps=["Search-only pagination and separation of compliance guidance from unrelated research require typed-result reconciliation."]),
    "basel": contract("bis_basel", "INT", {"publications": "https://www.bis.org/bcbs/publications.htm", "framework": "https://www.bis.org/basel_framework/"},
        collections=[r"/bcbs/(?:publications|publ)\.htm$", r"/basel_framework/?$"],
        documents=[r"/bcbs/publ/[a-z0-9_]+\.htm$", r"/basel_framework/chapter/[A-Z]+/[0-9]+\.htm$"],
        files=["www.bis.org"], query=["page", "year", "m", "inforce", "published"], native=["en"],
        gaps=["Framework effective-date selections and publication status/category totals require explicit historical reconciliation."]),
    "iosco": contract("iosco", "INT", {"reports": "https://www.iosco.org/publications/?subsection=public_reports"},
        collections=[r"/publications/?$", r"/v2/publications/?$"],
        files=["www.iosco.org", "api.iosco.org"], query=["page", "year", "subsection", "subSection1", "pdcid", "pdrtid", "publicDocID", "showAll", "keywords", "keywordsAuthor", "keywordsContent", "keywordsTitle"], native=["en"],
        gaps=["IOSCO v2 dynamic response schema and excluded research-type rows require typed catalog validation."]),
    "wolfsberg": contract("wolfsberg", "INT", {"standards": "https://wolfsberg-group.org/resources/general"},
        collections=[r"/resources/?$", r"/resources/(?:general|standards|guidance|faqs|archive)/?$"],
        documents=[r"/resources/[0-9]+/[^/]+/?$"],
        files=["wolfsberg-group.org", "www.wolfsberg-group.org", "db.wolfsberg-group.org"], query=["page", "year"], native=["en"],
        gaps=["Historical withdrawn versions not linked from the resources index need publisher catalog reconciliation."]),
    "iso": contract("restricted_file", "INT", {"security": "https://www.iso.org/committee/45306/x/catalogue/", "financial-services": "https://www.iso.org/committee/49650/x/catalogue/"},
        collections=[r"/committee/(?:45306|49650)/x/catalogue/?$"], documents=[r"/standard/[A-Za-z0-9-]+(?:\.html)?$"],
        files=["www.iso.org"], query=["page", "ics", "status"], licensed=True,
        gaps=["Governance, reporting and resilience committee boundaries need explicit financial-domain review; paid originals/amendments require authorized artifacts and processing rights."]),
    "aicpa": contract("restricted_file", "US", {"soc": "https://www.aicpa-cima.com/topic/audit-assurance/audit-and-assurance-greater-than-soc-2"},
        collections=[r"/topic/audit-assurance/audit-and-assurance-greater-than-soc-[123]$"],
        documents=[r"/resources/(?:download|article)/[^/]+$"], files=["www.aicpa-cima.com"], query=["page"], licensed=True,
        gaps=["Broader assurance standards and archived editions require authenticated publisher catalog access; free account access does not establish processing rights."]),
    "pci": contract("restricted_file", "INT", {"documents": "https://www.pcisecuritystandards.org/document_library/", "standards": "https://www.pcisecuritystandards.org/standards/"},
        collections=[r"/(?:document_library|standards)/?$"], documents=[r"/standards/[^/]+/?$"],
        files=["www.pcisecuritystandards.org", "docs-prv.pcisecuritystandards.org"], query=["page", "category", "document"], licensed=True,
        gaps=["Document-library acceptance/download tokens and retired-edition filters require a reviewed publisher contract; standards landing pages never count as originals."]),
    "ifrs": contract("restricted_file", "INT", {"standards": "https://www.ifrs.org/issued-standards/list-of-standards/"},
        collections=[r"/issued-standards/list-of-standards/?$"], documents=[r"/issued-standards/list-of-standards/[^/]+/?$"],
        files=["www.ifrs.org"], query=["page", "year", "language"], licensed=True,
        gaps=["Standards Navigator authentication, translations and historical digital editions require licensed catalog/artifact access; public summaries are not standards text."]),
    "be-fsma": contract("overlay", "BE", {"french": "https://www.fsma.be/fr/intermediaire", "english": "https://www.fsma.be/en/intermediary", "dutch": "https://www.fsma.be/nl/publiciteitsverplichtingen"},
        collections=[r"/(?:fr|nl|en)/(?:intermediaire|intermediary|publiciteitsverplichtingen|intermediair|intermediaire-en-.*|intermediary-in-.*)$"],
        files=["www.fsma.be"], query=["page", "year"], native=["fr", "nl"],
        gaps=["Non-intermediary financial-sector categories, sanction decisions and complete multilingual circular catalogs require reconciliation."]),
    "fr-amf": contract("overlay", "FR", {"regulation": "https://www.amf-france.org/fr/reglementation/reglementation-accueil"},
        collections=[r"/(?:fr|en)/(?:reglementation|regulation)/.*"],
        documents=[r"/(?:fr|en)/sanctions-transactions/.+"],
        files=["www.amf-france.org"], query=["page", "year", "from", "to"], native=["fr"],
        gaps=["General Regulation point-in-time versions, policy version links and sanctions catalogs require typed reconciliation."]),
    "es-cnmv": contract("overlay", "ES", {"regulation": "https://www.cnmv.es/Portal/Menu/Legislacion?lang=es", "circulars": "https://www.cnmv.es/Portal/Legislacion/Circulares?lang=es"},
        collections=[r"/[Pp]ortal/(?:[Mm]enu|[Ll]egislacion)/[^/]+/?$"],
        files=["www.cnmv.es", "internet.cnmv.es", "api.cnmv.es"], query=["lang", "page", "year", "anio", "desde", "hasta"], native=["es"],
        gaps=["Sanction decisions, archive year filter parameters and translation legal status require reconciliation."]),
    "de-bafin": contract("overlay", "DE", {"rules": "https://www.bafin.de/DE/RechtRegelungen/Rundschreiben/rundschreiben_node.html"},
        collections=[r"/(?:DE|EN)/RechtRegelungen/.+_node\.html$"],
        documents=[r"/SharedDocs/(?:Veroeffentlichungen|Downloads)/(?:DE|EN)/(?:Rundschreiben|Merkblatt|Auslegungsentscheidung|Konsultation)/.+\.html$"],
        files=["www.bafin.de", "bafin.de"], query=["page", "year", "__blob", "v", "nn"], native=["de"],
        gaps=["Current portal route migration, revoked circular catalogs and enforcement measures require primary endpoint confirmation."]),
    "it-consob": contract("overlay", "IT", {"mifid": "https://www.consob.it/web/area-pubblica/mifid-2", "intermediaries": "https://www.consob.it/web/area-pubblica/intermediari-normativa-nazionale-secondaria"},
        collections=[r"/web/area-pubblica/[^/]*(?:normativa|orientamenti|mifid|micar|regolamenti|consultazioni)[^/]*$"],
        documents=[r"/documents/.+", r"/web/area-pubblica/consultazioni/.+\.pdf$"],
        files=["www.consob.it"], query=["page", "year", "p_p_id", "p_p_lifecycle", "p_p_state", "p_p_mode"], native=["it"],
        gaps=["Publisher currently presents CAPTCHA/bot protection; no bypass. Wider regulation and sanctions archive categories remain unresolved."]),
}


def safe_url(raw, spec):
    """Canonicalize only documented GET parameters and exact publisher hosts."""
    try:
        p = urlparse(raw)
        hosts = {urlparse(url).hostname for url in spec["roots"].values()} | set(spec["files"])
        if (p.scheme != "https" or p.hostname not in hosts or p.port not in (None, 443)
                or p.username or p.password or ".." in unquote(p.path).split("/") or "\\" in unquote(p.path)
                or any(ord(c) < 32 for c in raw)):
            return None
        pairs = parse_qsl(p.query, keep_blank_values=True)
        if len(pairs) > 20 or len(p.query) > 4000 or any(k not in spec["query"] or len(v) > (2500 if k == "category" else 500) for k, v in pairs):
            return None
        if len({k for k, _ in pairs}) != len(pairs):
            return None
        return urlunparse(("https", p.hostname, p.path or "/", "", urlencode(sorted(pairs)), ""))
    except ValueError:
        return None


def _matches(patterns, path):
    return any(re.fullmatch(pattern, path) for pattern in patterns)


def is_attachment(url):
    p = urlparse(url)
    path = p.path.lower()
    return bool(re.search(r"\.(?:pdf|pdf\.coredownload\.(?:pdf|inline\.pdf)|ashx)$", path)
                or re.search(r"/(?:media/[0-9]+/download|document/download/[a-z0-9_-]+|download/[a-z0-9/_-]+)$", path)
                or (dict(parse_qsl(p.query)).get("ViewType", "").lower() == "pdf")
                or (".pdf/" in path and p.hostname == "www.consob.it"))


def document_entry(profile, spec, url, title, category, *, catalog_page=None):
    publisher_id = profile["publisher_id"]
    key = publisher_id + "/document/" + hashlib.sha256(url.encode()).hexdigest()[:24]
    from app.clhear.l1.source_registry import S, source_role
    from app.clhear.l1.publishers import publisher_ids
    known = next((row for row in S if row.get("canonical_url") == url and publisher_id in publisher_ids(row)
                  and source_role(row["key"]) == "document"), None)
    if known:
        # Rediscovery of the exact declared original keeps its stable identity
        # and reviewed permissions. A nearby collection or title is not enough.
        key = known["key"]
    # Only reuse exact existing publisher policy, never the overlay lane's ESMA
    # licence for other national publishers or one host's licence for another.
    basis, ref, evidence = "derived_only", "Publisher processing permissions require review", ""
    safe_policy = {"fca": "fca_handbook", "sec": "sec_edgar", "irs": "irs_gov", "nydfs": "nydfs", "nasdaq": "nasdaq", "nist": "nist",
                   "cysec": "cysec", "asic": "asic", "sg-law": "sg_legislation", "mas": "mas", "adgm": "adgm", "uae-law": "uae", "seychelles": "seychelles",
                   "malta-law": "malta", "isa": "isa", "il-privacy": "israel", "fatf": "fatf", "basel": "bis_basel", "iosco": "iosco", "wolfsberg": "wolfsberg"}
    if publisher_id in safe_policy:
        from app.clhear.l1.rights import RIGHTS
        policy = RIGHTS[safe_policy[publisher_id]]
        basis, ref, evidence = policy.basis, policy.ref, policy.evidence_url
    if spec["licensed"]:
        basis = "byol_only"
    entry = dict(key=key, name=title or profile["name"] + " publication", short_name=title or "Publisher publication",
                 instrument=title or "Publisher publication", canonical_url=url, publisher=profile["name"], issuer=profile["name"],
                 publisher_ids=[publisher_id], family="publisher-" + publisher_id, family_name=profile["name"] + " regulatory library",
                 jurisdiction=spec["jurisdiction"], adapter=spec["adapter"], kind="standard" if spec["licensed"] else "guidance",
                 license="open" if basis in {"public_domain", "open_licence"} else "restricted", rights_basis=basis, rights_ref=ref,
                 rights_evidence_url=evidence, source_role="document", relation="supplements", tier="informative", topics=[], registry_ids=[], wave=1,
                 discovered_category=category, catalog_evidence={"contract_version": VERSION, "url": catalog_page},
                 fetch={"url": url, "kind": "pdf" if is_attachment(url) else "html", "document_type": "publisher_publication"})
    if known:
        for name in ("family", "family_name", "kind", "relation", "tier", "topics", "registry_ids"):
            if name in known:
                entry[name] = known[name]
    if spec["licensed"] and not is_attachment(url):
        # A product/summary record is an expected real document with an explicit
        # artifact gap. Its body must never masquerade as the standard.
        if known and known.get("adapter") == "restricted_file":
            # Existing authorized-artifact inputs remain executable through
            # their owning worker; never replace them with catalog HTML.
            entry["fetch"] = dict(known.get("fetch") or {})
        else:
            entry["fetch"]["blocked"] = "authorized_standard_artifact_required"
        entry["catalog_record_url"] = url
    return entry


def language_metadata(body, *, publisher_id, document_key, url):
    """Exact-byte HTML language evidence; URL/ASCII never establish language.

    A native-language rule describes this publisher's own original publication,
    not every hosted third-party attachment. No metadata => no assertion.
    """
    spec = LIBRARIES.get(publisher_id)
    if publisher_id == "finra":
        spec = {"native_languages": ["en"]}
    if publisher_id == "eu-law":
        from app.clhear.l1.structured_catalogs import EU_LANGUAGES
        spec = {"native_languages": list(EU_LANGUAGES.values())}
    if not spec or not spec["native_languages"] or body.startswith(b"%PDF-"):
        return None
    soup = BeautifulSoup(body, "html.parser")
    html = soup.find("html")
    raw = (html.get("xml:lang") or html.get("lang") or "") if html else ""
    language = raw.strip().lower().split("-")[0]
    if language not in spec["native_languages"]:
        return None
    # Publisher language switchers describe UI; an actual publication body is
    # mandatory, and an explicitly labelled translation cannot become original.
    title = soup.find("h1")
    area = soup.find("article") or soup.find("main")
    if publisher_id == "eu-law" and soup.select_one('[id^="art_"], [id^="anx_"], .oj-normal'):
        area = soup.body
        title = soup.select_one(".title-doc-first, .oj-doc-ti, #tit_1")
    if title is None or area is None or not area.find(["p", "section"]):
        return None
    if re.search(r"unofficial translation|translation for (?:information|convenience)|translated (?:version|text)", area.get_text(" ", strip=True), re.I):
        return None
    return {"language": language, "authority": "authoritative", "document_key": document_key,
            "evidence_ref": url, "method": "publisher_metadata", "contract_version": VERSION,
            "artifact_sha256": hashlib.sha256(body).hexdigest(), "publisher_id": publisher_id}


def form_continuations(area, page, target):
    """Follow finite GET facets actually published in the catalog HTML.

    No guessed years, form actions, JavaScript execution or POST submission.
    Every combination must classify as a reviewed collection. Unknown fields,
    dependencies and large products remain an explicit gap.
    """
    from itertools import product
    links, findings = {}, []
    for form in area.find_all("form"):
        selects = form.find_all("select")
        if not selects:
            continue
        action = urljoin(page["url"], form.get("action") or page["url"])
        fields = []
        unsupported = str(form.get("method", "get")).lower() != "get"
        for select in selects:
            name = select.get("name")
            values = list(dict.fromkeys(str(option.get("value", option.get_text(strip=True)))
                                        for option in select.find_all("option") if not option.has_attr("disabled")))
            if not name or select.has_attr("multiple") or not values:
                unsupported = True
                continue
            fields.append((str(name), values))
        for item in form.find_all("input"):
            if not item.get("name") or item.get("type", "text").lower() in {"submit", "button", "reset"}:
                continue
            if item.get("type", "text").lower() not in {"hidden", "text", "search"}:
                unsupported = True
            fields.append((str(item["name"]), [str(item.get("value", ""))]))
        count = 1
        for _, values in fields:
            count *= len(values)
        if unsupported or not fields or count > 256 or len({name for name, _ in fields}) != len(fields):
            findings.append({"code": "dynamic_enumeration_unverified", "detail": "Publisher facets require POST, unsupported fields or more than 256 observed combinations."})
            continue
        parsed = urlparse(action)
        fixed = dict(parse_qsl(parsed.query, keep_blank_values=True))
        rejected = False
        for values in product(*(options for _, options in fields)):
            query = {**fixed, **dict(zip((name for name, _ in fields), values))}
            candidate = urlunparse((parsed.scheme, parsed.netloc, parsed.path, "", urlencode(query), ""))
            found = target(candidate, page)
            if not found or found["role"] != "collection":
                rejected = True
                continue
            if found["url"] != page["url"]:
                links[found["url"]] = {k: found[k] for k in ("url", "source_key", "category", "role")}
        if rejected:
            findings.append({"code": "catalog_facet_contract_required", "detail": "An observed form option falls outside the reviewed URL/parameter contract."})
    return list(links.values()), findings


def library_decoder(profile):
    """Pure decoder used by the existing checkpointed discovery worker."""
    publisher_id = profile["publisher_id"]
    spec = LIBRARIES[publisher_id]
    canonical_roots = {safe_url(url, spec): category for category, url in spec["roots"].items()}

    def target(raw, parent, title=""):
        url = safe_url(raw, spec)
        if not url:
            return None
        path = urlparse(url).path
        if publisher_id == "sec" and (path.startswith(("/Archives/edgar/", "/comments/", "/files/comments/", "/newsroom/speeches-statements/"))):
            return None
        category = canonical_roots.get(url, parent["category"])
        is_file = is_attachment(url)
        if publisher_id == "nist" and not is_file and _matches(spec["documents"], path):
            # CSRC detail/status pages contain abstracts and file links. They
            # are metadata collections, never the FIPS/SP/IR original text.
            return dict(url=url, source_key=f"nist/catalog/{category}", category=category, role="collection")
        # Specific document routes win over broad section routes, except exact
        # root URLs. e.g a standards index is not a standards original.
        if url not in canonical_roots and (is_file or _matches(spec["documents"], path)):
            entry = document_entry(profile, spec, url, title, category, catalog_page=parent["url"])
            return dict(url=url, source_key=entry["key"], category=category, role="document", entry=entry,
                        terminal=is_file or spec["licensed"])
        if url in canonical_roots or _matches(spec["collections"], path):
            return dict(url=url, source_key=f"{publisher_id}/catalog/{category}", category=category, role="collection")
        return None

    def decode(body, page):
        if body.startswith(b"%PDF-"):
            raise ValueError("Catalog endpoint returned an original, not an enumerable collection")
        soup = BeautifulSoup(body, "html.parser")
        area = soup.find("main") or soup.find("article") or soup.body or soup
        text = area.get_text(" ", strip=True)
        if re.search(r"(?:solve this CAPTCHA|activity and behavior.*bot|service is currently unavailable|access denied|enable javascript.*continue)", text, re.I):
            raise ValueError("Publisher catalog access unavailable")
        entries, links, findings = {}, {}, []
        language_alternates = []
        if page.get("role") == "document":
            h1 = soup.find("h1")
            entry = document_entry(profile, spec, page["url"], h1.get_text(" ", strip=True) if h1 else "", page["category"], catalog_page=page["url"])
            language = language_metadata(body, publisher_id=publisher_id, document_key=entry["key"], url=page["url"])
            if language:
                entry["language_evidence"] = language
            entries[entry["key"]] = entry
        # Navigation chrome is omitted, but rel=alternate language expressions
        # in <head> are considered when they point to an exact document route.
        for a in list(area.find_all("a", href=True)) + list(soup.select('link[rel="alternate"][href][hreflang]')):
            nav = a.find_parent("nav")
            pager = nav and re.search(r"pag(?:er|in)|page", " ".join(nav.get("class", [])) + " " + str(nav.get("aria-label", "")), re.I)
            if a.find_parent(["footer", "header", "aside"]) or (nav and not pager and "next" not in (a.get("rel") or [])):
                continue
            href = str(a["href"])
            if not href or href.startswith(("#", "mailto:", "javascript:")):
                continue
            raw = urljoin(page["url"], href)
            title = a.get_text(" ", strip=True) or a.get("title", "")
            found = target(raw, page, title)
            if found is None:
                if "next" in (a.get("rel") or []) or re.fullmatch(r"next(?: page)?|suivant|volgende|weiter|successiv[ao]|siguiente", title, re.I):
                    findings.append({"code": "unsupported_pagination", "detail": "An actual next-page link lies outside the reviewed publisher contract.", "link_url": raw})
                elif is_attachment(raw) and (a.get("download") is not None or re.search(r"pdf|download|attachment|annex", title, re.I)):
                    findings.append({"code": "attachment_host_or_query_unverified", "detail": "A linked attachment requires review of its host or download parameters.", "link_url": raw})
                continue
            if found["url"] == page["url"]:
                continue
            if a.get("hreflang") and page.get("role") == "document":
                alternate = str(a["hreflang"]).lower().split("-")[0]
                language_alternates.append({"language": alternate, "url": found["url"], "method": "linked_hreflang", "authority": "unknown"})
                # An established original plus English is the deliverable.
                # Preserve every other alternate as catalog metadata, without
                # scheduling all language duplicates as document imports.
                current = entries.get(page["source_key"], {}).get("language_evidence")
                preferred_original = (spec["native_languages"] or [None])[0]
                if alternate != "en" and (current or alternate != preferred_original):
                    continue
            links[found["url"]] = {k: found[k] for k in ("url", "source_key", "category", "role", "terminal") if k in found}
            if found.get("entry"):
                entry = found["entry"]
                if a.get("hreflang"):
                    entry["language_candidates"] = [{"language": a["hreflang"], "evidence_ref": page["url"], "method": "linked_hreflang", "authority": "unknown"}]
                if page.get("role") == "document":
                    entry["related_document_key"] = page["source_key"]
                    entry["relation_evidence"] = {"relation": "language_alternate" if a.get("hreflang") else "linked_attachment_or_reference", "catalog_url": page["url"]}
                entries[entry["key"]] = entry
        form_links, form_findings = form_continuations(area, page, target)
        for found in form_links:
            links[found["url"]] = found
        findings.extend(form_findings)
        if area.select("[data-drupal-views-infinite-scroll-content-wrapper], [data-total-pages], .dataTables_wrapper"):
            findings.append({"code": "dynamic_enumeration_unverified", "detail": "Publisher exposes dynamic facets or table pagination; static links do not prove exhaustion."})
        if not entries and not links:
            findings.append({"code": "empty_discovery_index", "detail": "No supported publication or continuation was present; empty catalogs are not certified."})
        return {"entries": list(entries.values()), "links": list(links.values()), "findings": findings,
                "catalog_metadata": {"contract_version": VERSION, "observed_documents": len(entries), "observed_links": len(links),
                                     "language_alternates": language_alternates,
                                     "language_policy": "one identified original plus English; other linked language expressions remain metadata"}}
    return target, decode


def discover_library(engine, store, profile, *, job_id, fetcher):
    from app.clhear.l1.discovery import run_batch
    spec = LIBRARIES[profile["publisher_id"]]
    seeds = [{"url": safe_url(url, spec), "source_key": f"{profile['publisher_id']}/catalog/{category}", "category": category}
             for category, url in spec["roots"].items()]
    if any(not seed["url"] for seed in seeds):
        raise ValueError("Invalid reviewed publisher seed")
    target, decode = library_decoder(profile)
    entries, report = run_batch(engine, store, publisher_id=profile["publisher_id"], profile={**profile, "contract": spec},
                               seeds=seeds, job_id=job_id, fetcher=fetcher, classify=target, decoder=decode, decode_documents=True)
    report["contract_version"] = VERSION
    for gap in spec["gaps"]:
        report["findings"].append({"publisher_id": profile["publisher_id"], "code": profile["publisher_id"].replace("-", "_") + "_catalog_reconciliation_required", "detail": gap,
                                  "dependency_class": "access_and_contract" if profile["publisher_id"] in EXTERNAL_DEPENDENCIES else "implementation_and_scope_review"})
    if spec["gaps"]:
        report["complete"] = False
    return entries, report
