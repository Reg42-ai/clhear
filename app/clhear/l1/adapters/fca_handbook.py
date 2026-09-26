# Copyright (C) 2026 Reg42 AI
# This file is part of CLHEAR. See LICENSE (AGPL-3.0-only).
"""FCA Handbook — first-class adapter (HLD v2 §4.1 starter corpus).

One source per sourcebook (PRIN, SYSC, COBS, CASS, PROD, SUP, DISP,
MIFIDPRU …). The adapter fetches every chapter of the sourcebook in the
chapter view (``/handbook/{SB}/{n}/?view=chapter``) — one artifact per
chapter — and projects each numbered rule (``PRIN 2.1.1 R``) into a
``provision`` node whose ref is the Handbook citation. The publisher's rule
status (R rule, G guidance, E evidential, D direction, UK/EU onshored text)
is kept on the node and drives the normative flag.

Chapter discovery reads the sourcebook landing page for ``/handbook/{SB}/{n}/``
links; callers may pin ``chapters`` explicitly (starter corpus does, so the
daily run is deterministic and polite).
"""
import re
from datetime import date

from bs4 import BeautifulSoup

from app.clhear.l1.adapters.publisher import NumberedHtmlAdapter, render_html

BASE = "https://www.handbook.fca.org.uk/handbook"

SOURCEBOOKS = {
    "PRIN": "Principles for Businesses",
    "SYSC": "Senior Management Arrangements, Systems and Controls",
    "COBS": "Conduct of Business Sourcebook",
    "CASS": "Client Assets",
    "PROD": "Product Intervention and Product Governance Sourcebook",
    "SUP": "Supervision",
    "DISP": "Dispute Resolution: Complaints",
    "MIFIDPRU": "Prudential sourcebook for MiFID Investment Firms",
    "COCON": "Code of Conduct",
    "GEN": "General Provisions",
    "MAR": "Market Conduct",
    "CONC": "Consumer Credit sourcebook",
}

_SB = "|".join(sorted(SOURCEBOOKS, key=len, reverse=True))
_RELEASE = re.compile(r"Release\s+(\d+)\s*[●•·-]?\s*([A-Za-z]+\s+\d{4})")


class FcaHandbookAdapter(NumberedHtmlAdapter):
    key = "fca_handbook"
    publisher = "Financial Conduct Authority"
    issuer = "Financial Conduct Authority"
    jurisdiction = "UK"
    kind = "regulation"
    # Rules have ≥ 3 levels ("PRIN 2.1.1"); two-level numbers are section
    # headings ("PRIN 2.1 The Principles"). A trailing letter is part of the
    # number ("SYSC 4.1.1A") unless it is the rule status ("PRIN 2.1.1R").
    PROVISION = re.compile(
        rf"^(?P<ref>(?:{_SB})\s+\d+[A-Z]?(?:\.\d+(?:(?!(?:R|G|E|D|C|UK|EU)(?=\s|$))[A-Z]){{0,2}}){{2,3}})"
        r"\s*(?P<status>R|G|E|D|UK|EU|C)?(?=\s|$)"
    )

    def __init__(self, sourcebook: str, *, chapters: tuple[str, ...] | list[str] | None = None, **kwargs):
        self.sourcebook = sourcebook.upper()
        self.chapters = tuple(str(c) for c in chapters) if chapters else ()
        kwargs.setdefault("source_key", f"fca/handbook/{self.sourcebook}")
        kwargs.setdefault("title", f"FCA Handbook — {self.sourcebook} ({SOURCEBOOKS.get(self.sourcebook, self.sourcebook)})")
        kwargs.setdefault("url", f"{BASE}/{self.sourcebook}/")
        kwargs.setdefault("family_key", "uk-fca")
        kwargs.setdefault("family_name", "UK conduct & prudential (FCA)")
        kwargs.setdefault("short_name", f"FCA {self.sourcebook}")
        kwargs.setdefault("instrument", f"FCA Handbook {self.sourcebook}")
        kwargs.setdefault("topics", ["conduct", "uk", self.sourcebook.lower()])
        super().__init__(**kwargs)

    def chapter_urls(self) -> list[str]:
        if self.chapters:
            return [f"{BASE}/{self.sourcebook}/{c}/?view=chapter" for c in self.chapters]
        landing = render_html(self._url, renderer=self.renderer)
        soup = BeautifulSoup(landing, "html.parser")
        found: list[str] = []
        pattern = re.compile(rf"/handbook/{self.sourcebook}/(\d+[A-Z]?)/")
        for a in soup.find_all("a", href=True):
            m = pattern.search(a["href"])
            if m and m.group(1) not in found:
                found.append(m.group(1))
        if not found:
            return [self._url]
        return [f"{BASE}/{self.sourcebook}/{c}/?view=chapter" for c in found]

    def fetch_bytes(self) -> list[tuple[str, bytes]]:
        out = []
        for url in self.chapter_urls():
            m = re.search(rf"/{self.sourcebook}/(\d+[A-Z]?)/", url)
            name = f"chapter-{m.group(1)}.html" if m else "page.html"
            out.append((name, render_html(url, renderer=self.renderer)))
        return out

    def version_of(self, content: bytes) -> tuple[str, date | None]:
        text = BeautifulSoup(content, "html.parser").get_text(" ", strip=True)
        m = _RELEASE.search(text)
        if m:
            return f"consolidated:release-{m.group(1)}", None
        return super().version_of(content)

    def make_ref(self, match, context) -> str:
        return " ".join(match.group("ref").split())
