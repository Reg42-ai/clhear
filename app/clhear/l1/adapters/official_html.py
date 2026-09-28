# Copyright (C) 2026 Reg42 AI
# This file is part of CLHEAR. See LICENSE (AGPL-3.0-only).
"""Official HTML page adapter: fetch a publisher page, emit a DocNode tree.

Used for Class B publishers (FCA, AU, SG, ADGM, NYDFS, Nasdaq, Malta,
UAE) and any other registry row whose official artifact is HTML. The oracle
is a dumb visible-text walk of the same page after chrome (script/style/nav/
header/footer) is stripped — it does not share the structural grouping logic.
"""
from datetime import date
import hashlib

from bs4 import BeautifulSoup, Tag

from app.clhear.l1 import http
from app.clhear.l1.adapters.base import Artifact, DocNode, FetchResult, SourceMeta

_CHROME = {
    "script",
    "style",
    "noscript",
    "nav",
    "header",
    "footer",
    "aside",
    "form",
    "button",
    "svg",
    "iframe",
    "template",
}

_HEADING = {"h1", "h2", "h3", "h4", "h5", "h6"}
_BLOCK = {"p", "li", "td", "th", "dt", "dd", "blockquote", "pre"}


def _strip_chrome(soup: BeautifulSoup) -> BeautifulSoup:
    for el in list(soup.find_all(_CHROME)):
        el.decompose()
    return soup


def _visible_strings(soup: BeautifulSoup) -> list[str]:
    return [str(s).strip() for s in soup.stripped_strings if str(s).strip()]


class OfficialHtmlAdapter:
    """Fetch `url` and project it into title / section / paragraph nodes."""

    key = "official_html"

    def __init__(
        self,
        source_key: str,
        title: str,
        url: str,
        *,
        adapter: str = "official_html",
        meta: SourceMeta | None = None,
        jurisdiction: str = "",
        issuer: str = "",
        kind: str = "regulation",
        license: str = "open",
        license_ref: str = "",
        family_key: str = "",
        family_name: str = "",
        short_name: str = "",
        about: str = "",
        topics: list[str] | None = None,
    ):
        self._source_key = source_key
        self._title = title
        self._url = url
        self.key = adapter
        self._meta = meta
        self._jurisdiction = jurisdiction
        self._issuer = issuer
        self._kind = kind
        self._license = license
        self._license_ref = license_ref
        self._family_key = family_key or adapter
        self._family_name = family_name or title
        self._short_name = short_name or title
        self._about = about
        self._topics = topics or []

    def meta(self) -> SourceMeta:
        if self._meta is not None:
            return self._meta
        return SourceMeta(
            family_key=self._family_key,
            family_name=self._family_name,
            source_key=self._source_key,
            name=self._title,
            kind=self._kind,
            issuer=self._issuer,
            jurisdiction=self._jurisdiction,
            license=self._license,
            license_ref=self._license_ref,
            canonical_url=self._url,
            adapter=self.key,
            short_name=self._short_name,
            about=self._about,
            topics=list(self._topics),
            version_policy="consolidated",
        )

    def fetch(self, since_version: str | None = None) -> FetchResult | None:
        content = http.get(self._url)
        artifact = Artifact(name="page.html", content=content, content_type="text/html")
        tree = self._parse(content)
        version_label = f"consolidated:acquired-sha256-{hashlib.sha256(content).hexdigest()}"
        return FetchResult(
            version_label=version_label,
            artifacts=[artifact],
            tree=tree,
            version_kind="consolidated",
            as_of_date=None,
        )

    def expected_text(self, artifacts: list[Artifact]) -> list[str]:
        from app.clhear.l1.originals import html_text
        return [html_text(artifact.content) for artifact in artifacts]

    def _parse(self, content: bytes) -> list[DocNode]:
        from app.clhear.l1.adapters.html_document import parse
        return parse(content, self._source_key)
