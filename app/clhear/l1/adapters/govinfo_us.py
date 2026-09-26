# Copyright (C) 2026 Reg42 AI
# This file is part of CLHEAR. See LICENSE (AGPL-3.0-only).
"""govinfo + eCFR adapter (HLD §7.2 adapter 3): FATCA — 26 USC ch. 4 (statute,
GPO govinfo HTML) and 26 CFR ch. 4 regulations (eCFR versioner API).

Emits a typed DocNode tree (section / subsection / paragraph) with raw text
and the exact HTML/XML fragment per node. IGAs stay reference-level per the
v1 charter (HLD §7.3).
"""
import re
import xml.etree.ElementTree as ET
from datetime import date
from urllib.parse import quote

from bs4 import BeautifulSoup, Tag

from app.clhear.l1 import http
from app.clhear.l1.adapters.base import Artifact, DocNode, FetchResult, SourceMeta

USC_EDITION = "2023"
USC_SECTIONS = ("1471", "1472", "1473", "1474")
USC_URL = (
    "https://www.govinfo.gov/content/pkg/USCODE-{ed}-title26/html/"
    "USCODE-{ed}-title26-subtitleA-chap4-sec{sec}.htm"
)

# 26 CFR ch. 4 regulation series (T.D. 9610 et seq.). The eCFR structure API
# returns the whole 60MB+ title; the charter pins the section list instead.
# ARCH: switch to structure-API enumeration when the P2 reconciliation job lands.
ECFR_DATE = "2025-12-31"
ECFR_SECTIONS = (
    "1.1471-0", "1.1471-1", "1.1471-2", "1.1471-3", "1.1471-4", "1.1471-5", "1.1471-6",
    "1.1472-1",
    "1.1473-1",
    "1.1474-1", "1.1474-2", "1.1474-3", "1.1474-4", "1.1474-5", "1.1474-6", "1.1474-7",
)
ECFR_URL = (
    "https://www.ecfr.gov/api/versioner/v1/full/{date}/title-26.xml"
    "?chapter=I&subchapter=A&part=1&section={section}"
)

FAMILY = dict(
    family_key="us-fatca",
    family_name="US FATCA (26 USC ch. 4 + regulations)",
    issuer="US Congress / Treasury-IRS (published by GPO)",
    jurisdiction="US",
    license="open",
    license_ref="public domain (17 U.S.C. 105)",
)


def _field(html: str, name: str) -> str:
    """Slice one GPO field (the page's own section delimiters)."""
    start = html.find(f"<!-- field-start:{name} -->")
    end = html.find(f"<!-- field-end:{name} -->")
    if start == -1 or end == -1:
        return ""
    return html[start + len(f"<!-- field-start:{name} -->") : end]


class GovInfoUscAdapter:
    key = "govinfo_us_usc"

    def __init__(
        self,
        title: str = "26",
        sections: tuple[str, ...] = USC_SECTIONS,
        edition: str = USC_EDITION,
        meta: SourceMeta | None = None,
        url_template: str | None = None,
    ):
        self.title = title
        self.sections = sections
        self.edition = edition
        self._meta = meta
        self._url_template = url_template or (
            "https://www.govinfo.gov/content/pkg/USCODE-{ed}-title{title}/html/"
            "USCODE-{ed}-title{title}-subtitleA-chap4-sec{sec}.htm"
            if title == "26"
            else (
                "https://www.govinfo.gov/content/pkg/USCODE-{ed}-title{title}/html/"
                "USCODE-{ed}-title{title}-sec{sec}.htm"
            )
        )

    def meta(self) -> SourceMeta:
        if self._meta is not None:
            return self._meta
        return SourceMeta(
            source_key="usc/26/ch4",
            name="26 USC §§1471–1474 — Taxes to enforce reporting on certain foreign accounts",
            kind="law",
            canonical_url="https://uscode.house.gov/view.xhtml?path=/prelim@title26/subtitleA/chapter4",
            adapter="govinfo_us",
            scope_charter={
                "binding": "statute + 26 CFR ch.4 + current FFI-agreement Rev. Proc. + form instructions",
                "out": ["IGAs (reference-level stubs in v1)"],
            },
            short_name="FATCA statute",
            about=(
                "The FATCA statute (Internal Revenue Code chapter 4): US federal law requiring "
                "foreign financial institutions to identify and report US account holders, "
                "enforced through a 30% withholding tax on withholdable payments to "
                "non-compliant institutions and recalcitrant account holders."
            ),
            topics=["tax", "fatca", "reporting", "us"],
            version_policy="edition",
            **FAMILY,
        )

    def fetch(self, since_version: str | None = None) -> FetchResult | None:
        version_label = f"edition:{self.edition}"
        if since_version == version_label:
            return None
        tree: list[DocNode] = []
        artifacts: list[Artifact] = []
        for sec in self.sections:
            content = http.get(self._url_template.format(ed=self.edition, sec=sec, title=self.title))
            artifacts.append(Artifact(name=f"sec{sec}.htm", content=content, content_type="text/html"))
            from app.clhear.l1.adapters.dom_document import parse
            tree.extend(parse(content, self.meta().source_key, len(artifacts)))
        return FetchResult(
            version_label=version_label, artifacts=artifacts, tree=tree, version_kind="edition"
        )

    def expected_text(self, artifacts: list[Artifact]) -> list[str]:
        from app.clhear.l1.adapters.dom_document import original_records
        return [row[7] for part, artifact in enumerate(artifacts, 1)
                for row in original_records(artifact.content, self.meta().source_key, part) if row[7].strip()]


class GovInfoEcfrAdapter:
    key = "govinfo_us_ecfr"

    def __init__(
        self,
        as_of: str = ECFR_DATE,
        title: str = "26",
        sections: tuple[str, ...] = ECFR_SECTIONS,
        chapter: str = "I",
        part: str = "1",
        subchapter: str = "A",
        meta: SourceMeta | None = None,
        url_template: str | None = None,
    ):
        self.as_of = as_of
        self.title = title
        self.sections = sections
        self.chapter = chapter
        self.part = part
        self.subchapter = subchapter
        self._meta = meta
        self._url_template = url_template or (
            "https://www.ecfr.gov/api/versioner/v1/full/{date}/title-{title}.xml"
            "?chapter={chapter}&subchapter={subchapter}&part={part}&section={section}"
        )

    def meta(self) -> SourceMeta:
        if self._meta is not None:
            return self._meta
        return SourceMeta(
            source_key="cfr/26/ch4",
            name="26 CFR §§1.1471–1.1474 — FATCA regulations",
            kind="regulation",
            issuer=FAMILY["issuer"],
            jurisdiction=FAMILY["jurisdiction"],
            license=FAMILY["license"],
            license_ref=FAMILY["license_ref"],
            family_key=FAMILY["family_key"],
            family_name=FAMILY["family_name"],
            canonical_url="https://www.ecfr.gov/current/title-26/chapter-I/subchapter-A/part-1",
            adapter="govinfo_us",
            scope_charter={"binding": "26 CFR ch.4 regulation series (T.D. 9610 et seq.)"},
            short_name="FATCA regulations (26 CFR)",
            about=(
                "The Treasury/IRS implementing regulations for FATCA (26 CFR §§1.1471–1.1474): "
                "the operational rulebook for withholding agents and foreign financial "
                "institutions — definitions, due-diligence procedures for identifying US "
                "accounts, registration, reporting and withholding mechanics."
            ),
            topics=["tax", "fatca", "withholding", "us"],
            version_policy="consolidated",
        )

    def section_url(self, section: str) -> str:
        """Versioner URL for one section. Parentheses are encoded. An empty
        subchapter is omitted so title 17 part 275 is not queried as subchapter A."""
        template = self._url_template
        if not self.subchapter:
            template = template.replace("&subchapter={subchapter}", "").replace(
                "subchapter={subchapter}&", "").replace("?subchapter={subchapter}", "?")
        return template.format(
            date=self.as_of,
            section=quote(str(section), safe=""),
            title=self.title,
            chapter=self.chapter,
            part=self.part,
            subchapter=self.subchapter,
        )

    def fetch(self, since_version: str | None = None) -> FetchResult | None:
        version_label = f"consolidated:{self.as_of}"
        if since_version == version_label:
            return None
        tree: list[DocNode] = []
        artifacts: list[Artifact] = []
        for section in self.sections:
            content = http.get(self.section_url(section))
            artifacts.append(Artifact(name=f"{section}.xml", content=content, content_type="application/xml"))
            from app.clhear.l1.adapters.xml_document import parse
            tree.extend(parse(content, self.meta().source_key, "govinfo_us", len(artifacts)))
        try:
            as_of_date = date.fromisoformat(self.as_of)
        except ValueError:
            as_of_date = None
        return FetchResult(
            version_label=version_label,
            artifacts=artifacts,
            tree=tree,
            version_kind="consolidated",
            as_of_date=as_of_date,
        )

    def expected_text(self, artifacts: list[Artifact]) -> list[str]:
        from app.clhear.l1.adapters.xml_document import original_records
        return [row[7] for part, artifact in enumerate(artifacts, 1)
                for row in original_records(artifact.content, self.meta().source_key, "govinfo_us", part)
                if row[7].strip()]
