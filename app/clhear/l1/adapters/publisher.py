# Copyright (C) 2026 Reg42 AI
# This file is part of CLHEAR. See LICENSE (AGPL-3.0-only).
"""Publisher adapter bases (HLD v2 §4.1 starter corpus).

Two reusable structural parsers on top of the verbatim-text contract:

``NumberedHtmlAdapter``
    Walks an official HTML page in document order. Headings become
    ``section`` nodes; blocks that *start* with the publisher's provision
    numbering (``PRIN 2.1.1 R``, ``CRE20.1``, ``3110(a)``) become
    ``provision`` nodes with a stable ref; other blocks attach to the current
    provision (or section) as paragraphs. Visible text the walk did not
    capture is appended as a flagged note so the fidelity oracle stays whole.

``NumberedPdfAdapter``
    Same idea over PDF page text (Docling when installed, pypdf otherwise):
    line-based scanning with a heading pattern and a provision pattern.

Subclasses only declare ``key``, the patterns, and how to build a ref. Both
expose ``parse(content)`` so the clause-boundary golden set can score the
parser without network access, and both accept an optional
``renderer="crawl4ai"`` (used only when the package is installed) for pages
that need a browser to render their text.
"""
import re
from datetime import date
import hashlib

from bs4 import BeautifulSoup, Tag

from app.clhear.l1 import http
from app.clhear.l1.adapters.base import Artifact, DocNode, FetchResult, SourceMeta
from app.clhear.l1.adapters.official_html import _BLOCK, _HEADING, _strip_chrome, _visible_strings
from app.clhear.l1.adapters.pdf_docling import extract_pdf_pages

__all__ = ["NumberedHtmlAdapter", "NumberedPdfAdapter", "render_html", "unique_ref"]


def render_html(url: str, *, renderer: str = "", headers: dict | None = None) -> bytes:
    """Fetch a page; with ``renderer='crawl4ai'`` and the package present, use a
    headless browser so JS-rendered publisher pages (FCA Handbook tabs, Basel
    Framework) yield their full text. Falls back to the polite HTTP client."""
    if renderer == "crawl4ai":
        try:  # pragma: no cover - optional heavy dependency
            import asyncio

            from crawl4ai import AsyncWebCrawler

            async def _run() -> bytes:
                async with AsyncWebCrawler() as crawler:
                    result = await crawler.arun(url=url)
                    return (result.html or "").encode()

            html = asyncio.run(_run())
            if html.strip():
                return html
        except Exception:
            pass
    return http.get(url, headers=headers)


def _promote_groups_without_provisions(nodes: list[DocNode]) -> None:
    """Headings are containers (``group``) when numbered provisions exist under
    them — provisions are the clause grain and must not nest inside another
    clause. A page with headings but no numbered provisions keeps its text
    clause-addressable by promoting the headings to ``section`` clauses."""
    if any(n.node_type == "provision" for node in nodes for n in node.walk()):
        return
    for node in nodes:
        if node.node_type == "group":
            node.node_type = "section"


def unique_ref(ref: str, seen: set[str]) -> str:
    """Refs must be unique per version (fidelity lint); suffix repeats."""
    if ref not in seen:
        seen.add(ref)
        return ref
    n = 2
    while f"{ref}#{n}" in seen:
        n += 1
    out = f"{ref}#{n}"
    seen.add(out)
    return out


class _PublisherBase:
    key = "publisher"
    publisher = ""
    instrument = ""
    kind = "regulation"
    jurisdiction = ""
    issuer = ""
    license = "open"
    version_kind = "consolidated"
    version_policy = "consolidated"
    # Subclasses override: provision numbering at block/line start. Must define
    # a named group ``ref`` and may define ``status``.
    PROVISION: re.Pattern | None = None
    # Optional heading pattern for PDFs / non-<h*> headings.
    HEADING: re.Pattern | None = None
    renderer = ""
    headers: dict | None = None

    def __init__(
        self,
        source_key: str,
        title: str,
        url: str,
        *,
        meta: SourceMeta | None = None,
        family_key: str = "",
        family_name: str = "",
        short_name: str = "",
        about: str = "",
        topics: list[str] | None = None,
        instrument: str = "",
        renderer: str | None = None,
    ):
        self._source_key = source_key
        self._title = title
        self._url = url
        self._meta = meta
        self._family_key = family_key or self.key
        self._family_name = family_name or title
        self._short_name = short_name or title
        self._about = about
        self._topics = topics or []
        self._instrument = instrument or self.instrument or short_name or title
        if renderer is not None:
            self.renderer = renderer

    # -- meta -------------------------------------------------------------
    def meta(self) -> SourceMeta:
        if self._meta is not None:
            return self._meta
        from app.clhear.l1 import rights as l1_rights

        basis = l1_rights.rights_for(self.key, self.license)
        return SourceMeta(
            family_key=self._family_key,
            family_name=self._family_name,
            source_key=self._source_key,
            name=self._title,
            kind=self.kind,
            issuer=self.issuer or self.publisher,
            jurisdiction=self.jurisdiction,
            license=self.license,
            canonical_url=self._url,
            adapter=self.key,
            short_name=self._short_name,
            about=self._about,
            topics=list(self._topics),
            version_policy=self.version_policy,
            rights_basis=basis.basis,
            rights_ref=basis.ref,
            publisher=self.publisher,
            instrument=self._instrument,
        )

    # -- ref building (subclasses may override) ---------------------------
    def make_ref(self, match: re.Match, context: dict) -> str:
        return " ".join(match.group("ref").split())

    def status_of(self, match: re.Match) -> str:
        try:
            return (match.group("status") or "").strip()
        except IndexError:
            return ""

    def version_of(self, content: bytes) -> tuple[str, date | None]:
        return f"{self.version_kind}:acquired-sha256-{hashlib.sha256(content).hexdigest()}", None

    def artifact_name(self) -> str:
        return "page.html"

    def fetch_bytes(self) -> list[tuple[str, bytes]]:
        """(artifact name, bytes) pairs — one per page/chapter fetched."""
        return [(self.artifact_name(), render_html(self._url, renderer=self.renderer, headers=self.headers))]

    def fetch(self, since_version: str | None = None) -> FetchResult | None:
        parts = self.fetch_bytes()
        artifacts = [
            Artifact(
                name=name,
                content=content,
                content_type="application/pdf" if content[:5] == b"%PDF-" else "text/html",
            )
            for name, content in parts
        ]
        tree = self.parse_many([c for _, c in parts])
        label, as_of = self.version_of(parts[0][1] if parts else b"")
        return FetchResult(
            version_label=label,
            artifacts=artifacts,
            tree=tree,
            version_kind=self.version_kind,
            as_of_date=as_of,
        )

    def parse_many(self, contents: list[bytes]) -> list[DocNode]:
        root = DocNode(node_type="title", ref=self._source_key, heading=self._title)
        seen: set[str] = {self._source_key}
        for i, content in enumerate(contents, start=1):
            root.children.extend(self._parse_into(content, seen, part=i))
        return [root]

    def parse(self, content: bytes) -> list[DocNode]:
        """Golden-set entry point: parse one artifact into a tree."""
        return self.parse_many([content])

    def parse_pages(self, pages: list[str]) -> list[DocNode]:
        """Golden-set entry point for text pages (no PDF bytes needed)."""
        root = DocNode(node_type="title", ref=self._source_key, heading=self._title)
        root.children.extend(self._parse_pages(pages, {self._source_key}, part=1))
        return [root]

    def _parse_into(self, content: bytes, seen: set[str], *, part: int) -> list[DocNode]:  # pragma: no cover
        raise NotImplementedError

    def _parse_pages(self, pages: list[str], seen: set[str], *, part: int) -> list[DocNode]:
        """Line-based structural parse shared by the PDF adapter and the HTML
        adapter's PDF fallback (publishers that serve a PDF from an HTML url)."""
        nodes: list[DocNode] = []
        section = provision = None
        context = {"part": part, "section": "", "seen": seen}
        seq = 0
        for page_number, page in enumerate(pages, 1):
            for raw in page.splitlines():
                line = raw.strip()
                if not line:
                    continue
                seq += 1
                locator = {"structure": "publisher-pdf-line", "page": page_number, "line": seq, "part": part}
                match = self.PROVISION.match(line) if self.PROVISION else None
                heading = self.HEADING.match(line) if self.HEADING and not match else None
                if heading:
                    ref = f"{self._source_key}/p{part}s{seq}"
                    if ref in seen:
                        raise ValueError("Duplicate PDF heading identity")
                    seen.add(ref)
                    section = DocNode(node_type="group", ref=ref, heading=line, source_locator=locator)
                    context["section"] = line
                    nodes.append(section)
                    provision = None
                elif match:
                    ref = self.make_ref(match, context)
                    if ref in seen:
                        node = DocNode(node_type="paragraph", raw_text=line, source_locator=locator)
                        (provision.children if provision else section.children if section else nodes).append(node)
                        continue
                    seen.add(ref)
                    provision = DocNode(node_type="provision", ref=ref, label=line[:match.end()].strip(),
                                        raw_text=line[match.end():].strip(), status=self.status_of(match), source_locator=locator)
                    (section.children if section else nodes).append(provision)
                else:
                    node = DocNode(node_type="paragraph", raw_text=line, source_locator=locator)
                    (provision.children if provision else section.children if section else nodes).append(node)
        return nodes


class NumberedHtmlAdapter(_PublisherBase):
    def expected_text(self, artifacts: list[Artifact]) -> list[str]:
        spans: list[str] = []
        for artifact in artifacts:
            if artifact.content[:5] == b"%PDF-":
                for page in extract_pdf_pages(artifact.content):
                    spans.extend(p.strip() for p in page.splitlines() if p.strip())
                continue
            from app.clhear.l1.originals import html_text
            spans.append(html_text(artifact.content))
        return spans

    def _blocks(self, soup: BeautifulSoup) -> list[tuple[str, str, Tag]]:
        """(kind, text, element) in document order; kind ∈ heading|block."""
        body = soup.body or soup
        out: list[tuple[str, str, Tag]] = []
        seen: set[int] = set()
        for el in body.find_all(list(_HEADING | _BLOCK)):
            if id(el) in seen:
                continue
            text = el.get_text(" ", strip=True)
            if not text:
                continue
            ancestor = el.parent
            skip = False
            while ancestor is not None and ancestor is not body:
                if isinstance(ancestor, Tag) and ancestor.name in _BLOCK and ancestor.get_text(" ", strip=True) == text:
                    skip = True
                    break
                ancestor = ancestor.parent
            if skip:
                continue
            seen.add(id(el))
            out.append(("heading" if el.name in _HEADING else "block", text, el))
        return out

    def _parse_into(self, content: bytes, seen: set[str], *, part: int) -> list[DocNode]:
        if content[:5] == b"%PDF-":
            return self._parse_pages(extract_pdf_pages(content), seen, part=part)
        from app.clhear.l1.adapters.html_document import parse
        roots = parse(content, self._source_key, provision=self.PROVISION,
                      make_ref=self.make_ref, status_of=self.status_of, part=part)
        for node in roots[0].children:
            for item in node.walk():
                if item.ref and item.node_type == "provision" and item.ref in seen:
                    item.node_type = "paragraph"
                    item.ref = ""
                    item.label = ""
            for item in node.walk():
                if item.ref:
                    seen.add(item.ref)
        return roots[0].children


class NumberedPdfAdapter(_PublisherBase):
    version_kind = "edition"
    version_policy = "edition"

    def artifact_name(self) -> str:
        return "document.pdf"

    def expected_text(self, artifacts: list[Artifact]) -> list[str]:
        from app.clhear.l1.originals import pdf_original
        return [pdf_original(artifact.content)[0] for artifact in artifacts]

    def _parse_into(self, content: bytes, seen: set[str], *, part: int) -> list[DocNode]:
        if not content.startswith(b"%PDF-"):
            raise ValueError("Expected publisher PDF bytes; a landing page cannot stand in for the document")
        return self._parse_pages(extract_pdf_pages(content), seen, part=part)


class GenericPublisherDocumentAdapter:
    """A discovered original, preserving bytes with the existing source lane.

    Discovery owns catalog traversal; this adapter acquires one exact original.
    A login, access-denied screen or link-only catalog cannot replace its text.
    """
    def __init__(self, source_key, title, url, *, meta, adapter, expected_format="auto"):
        from urllib.parse import urlparse
        parsed = urlparse(url)
        if parsed.scheme != "https" or parsed.username or parsed.password or parsed.port not in (None, 443):
            raise ValueError("Publication requires a validated official HTTPS URL")
        if expected_format not in {"auto", "html", "pdf"}:
            raise ValueError("Unsupported publisher document format")
        self._source_key, self._title, self._url, self._meta = source_key, title, url, meta
        self.key, self.expected_format = adapter, expected_format

    def meta(self):
        return self._meta

    def fetch(self, since_version=None):
        from urllib.parse import urlparse
        content = http.get(self._url, allowed_redirect_hosts={urlparse(self._url).hostname})
        if content.startswith(b"%PDF-"):
            from app.clhear.l1.adapters.pdf_docling import pages_to_tree
            tree = pages_to_tree(extract_pdf_pages(content), self._source_key, self._title)
            artifact = Artifact("publication.pdf", content, "application/pdf")
        else:
            if self.expected_format == "pdf":
                raise ValueError("Original PDF endpoint returned a different representation")
            soup = BeautifulSoup(content, "html.parser")
            area = soup.find("article") or soup.find("main")
            title = soup.find("h1")
            if area is None or title is None:
                raise ValueError("Publication lacks an identified article/main body and title")
            text = area.get_text(" ", strip=True)
            links = " ".join(a.get_text(" ", strip=True) for a in area.find_all("a"))
            if (len(text) < 40 or len(links) >= len(text) * .8 or
                    re.search(r"solve this CAPTCHA|access denied|service is currently unavailable|sign in to (?:view|access)|log in to (?:view|access)", text, re.I)):
                raise ValueError("Publication is unavailable or is a document catalog")
            from app.clhear.l1.adapters.html_document import parse
            tree = parse(content, self._source_key)
            artifact = Artifact("publication.html", content, "text/html")
        return FetchResult(version_label=f"as-published:acquired-sha256-{hashlib.sha256(content).hexdigest()}",
                           artifacts=[artifact], tree=tree, version_kind="as_published", as_of_date=None)

    def expected_text(self, artifacts):
        from app.clhear.l1.originals import html_text, pdf_original
        return [pdf_original(a.content)[0] if a.content.startswith(b"%PDF-") else html_text(a.content) for a in artifacts]
