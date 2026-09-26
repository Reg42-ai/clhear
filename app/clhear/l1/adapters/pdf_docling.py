# Copyright (C) 2026 Reg42 AI
# This file is part of CLHEAR. See LICENSE (AGPL-3.0-only).
"""PDF → DocNode helper (Class C).

Uses pypdf for parsing and an independent pdfminer text/layout oracle. Numbered
sections are retained; ambiguous or image-only documents require review.
"""
from datetime import date
import hashlib
import re

from app.clhear.l1 import http
from app.clhear.l1.adapters.base import Artifact, DocNode, FetchResult, SourceMeta


def extract_pdf_pages(content: bytes) -> list[str]:
    """Deterministic pypdf extraction; pdfminer independently verifies it.

    Never silently switch decoders after a failure or collapse a whole PDF
    into one pretend page. Scanned/empty pages require explicit review.
    """
    from pypdf import PdfReader
    import io

    reader = PdfReader(io.BytesIO(content))
    pages = []
    for page in reader.pages:
        pages.append(page.extract_text() or "")
    if not pages or any(not page.strip() for page in pages):
        raise ValueError("PDF has empty/scanned pages; independent OCR/visual review required")
    return pages


def _demote_contents(root: DocNode) -> None:
    """The numbered lines seen so far were a table of contents, not the body.

    Keep every line verbatim as an unnumbered paragraph of the title node so the
    independent readback still accounts for the text, and free the markers for
    the body. ``originals._pdf_structure_rows`` applies the same resolution.
    """
    lines: list[DocNode] = []

    def flatten(node: DocNode) -> None:
        line = " ".join(part for part in (node.label, node.raw_text) if part).strip()
        if line:
            lines.append(DocNode(node_type="paragraph", raw_text=line, source_locator=node.source_locator))
        for child in node.children:
            flatten(child)

    for node in root.children:
        flatten(node)
    root.children = lines


def pages_to_tree(pages: list[str], source_key: str, title: str) -> list[DocNode]:
    root = DocNode(node_type="title", ref=source_key, heading=title, source_locator={"structure": "pdf-root"})
    current, sections, seen, order = root, [], set(), []
    contents_resolved = False
    for page_number, page in enumerate(pages, 1):
        for line_number, raw in enumerate(page.splitlines(), 1):
            text = raw.strip()
            if not text:
                continue
            locator = {"structure": "pdf-line", "page": page_number, "line": line_number}
            match = SECTION.match(text)
            if match:
                marker = match.group("ref")
                if marker in seen:
                    # Rule filings and standards open with a numbered contents
                    # list, then the body restarts at the same first marker. That
                    # exact shape is resolvable once; any other repeat is not.
                    if not contents_resolved and order and marker == order[0] and all(
                            child.node_type == "section" for child in root.children):
                        _demote_contents(root)
                        sections, seen, order, contents_resolved = [], set(), [], True
                        current = root
                    else:
                        raise ValueError("PDF repeats a section/control marker; contents/header/body scope must be resolved")
                seen.add(marker)
                order.append(marker)
                depth = marker.count(".")
                while sections and sections[-1][0] >= depth:
                    sections.pop()
                node = DocNode(node_type="section", ref=f"{source_key}/section/{marker}",
                               label=text[:match.end()].strip(), raw_text=text[match.end():].strip(), source_locator=locator)
                (sections[-1][1] if sections else root).children.append(node)
                sections.append((depth, node))
                current = node
            else:
                current.children.append(DocNode(node_type="paragraph", raw_text=text, source_locator=locator))
    if not seen:
        raise ValueError("PDF section/control numbering is unrecognized; a publisher-specific structural parser is required")
    return [root]


# Numeric standard sections / Annex A controls and AICPA TSC identifiers.
SECTION = re.compile(r"^(?P<ref>(?:A\.)?\d+(?:\.\d+){0,5}|(?:CC|PI|P|A|C)\d+(?:\.\d+){0,5})(?:[.)])?\s+(?=\S)")


class PdfOfficialAdapter:
    """Fetch an explicit PDF artifact; a landing page is not its full text."""

    key = "pdf_official"

    def __init__(
        self,
        source_key: str,
        title: str,
        url: str,
        *,
        adapter: str = "pdf_official",
        meta: SourceMeta | None = None,
        jurisdiction: str = "",
        issuer: str = "",
        kind: str = "guidance",
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
            version_policy="edition",
        )

    def fetch(self, since_version: str | None = None) -> FetchResult | None:
        content = http.get(self._url)
        ctype = "application/pdf" if content[:5] == b"%PDF-" else "application/octet-stream"
        if content[:5] != b"%PDF-":
            raise ValueError("Expected a publisher PDF artifact; landing pages require a resolved document URL")
        pages = extract_pdf_pages(content)
        tree = pages_to_tree(pages, self._source_key, self._title)
        return FetchResult(
            version_label=f"edition:acquired-sha256-{hashlib.sha256(content).hexdigest()}",
            artifacts=[Artifact(name="document.pdf", content=content, content_type=ctype)],
            tree=tree,
            version_kind="edition",
            as_of_date=None,
        )

    def expected_text(self, artifacts: list[Artifact]) -> list[str]:
        spans: list[str] = []
        for artifact in artifacts:
            if artifact.content[:5] == b"%PDF-":
                from app.clhear.l1.originals import pdf_original
                spans.append(pdf_original(artifact.content)[0])
            else:
                from bs4 import BeautifulSoup

                soup = BeautifulSoup(artifact.content, "html.parser")
                for el in list(soup.find_all(["script", "style", "nav", "header", "footer"])):
                    el.decompose()
                spans.extend(s.strip() for s in soup.stripped_strings if s.strip())
        return spans
