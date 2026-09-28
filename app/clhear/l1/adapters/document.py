# Copyright (C) 2026 Reg42 AI
# This file is part of CLHEAR. See LICENSE (AGPL-3.0-only).
"""Any text a host brings: pasted text, a file on disk, or a public URL.

``local_text`` reads ``{"text": ...}`` or ``{"path": ...}``; ``url`` reads
``{"url": "https://..."}``. Plain text, HTML and PDF are accepted. The adapter
renders the original to UTF-8 text once (HTML text blocks, PDF page text) and
stores that rendering as ``source.txt``; the original's hash and content type
are recorded on the root node. Clauses are the document's own units:

* division headings (Part, Title, Chapter, Annex, Schedule ...) nest units;
* unit headings (Article, Section, §, Rule, Clause, numbered headings) own the
  paragraphs that follow them;
* a paragraph starts at a blank line or at a line that opens with an
  enumerator ("(1)", "(a)", "2.", "§ 4"), and very long paragraphs are split
  where a line ends a sentence.

References are readable (``art-32``, ``art-32/1``, ``sec-3/a``, ``p4``) and
stay the same for the same text. ``originals`` checks that every stored node
is a run of whole lines of ``source.txt``, in order, covering all of it.
"""
from __future__ import annotations

import hashlib
import io
import ipaddress
import os
import re
import socket
from dataclasses import dataclass
from pathlib import Path
from urllib.parse import urljoin, urlsplit

from app.clhear.l1.adapters.base import Artifact, DocNode, FetchResult, SourceMeta

KEYS = ("local_text", "url")
TEXT_ARTIFACT = "source.txt"
MAX_BYTES = 25 * 1024 * 1024
MAX_REDIRECTS = 5
LONG_PARAGRAPH = 1200
LOCAL_SOURCES_ENV = "CLHEAR_LOCAL_SOURCES_DIR"
ALLOW_PRIVATE_ENV = "CLHEAR_ALLOW_PRIVATE_URLS"

_DIVISION = re.compile(
    r"^(?P<kind>part|title|chapter|book|annex|schedule|appendix|subpart|division)\s+"
    r"(?P<num>[0-9]+[a-z]?|[ivxlcdm]+|[a-z])\b[.:]?(?:\s|$)", re.I)
_UNIT = re.compile(
    r"^(?P<kind>article|art\.|section|sec\.|§+|rule|clause|regulation|standard|requirement|control|principle)"
    r"\s*(?P<num>[0-9]+(?:\.[0-9]+)*[a-z]?(?:-[0-9]+)?|[ivxlcdm]+)\b[.:]?", re.I)
_NUMBERED_HEADING = re.compile(r"^(?P<num>\d{1,3}(?:\.\d{1,3}){0,5})\.?\s+(?P<title>[A-Z][^.;:!?]{1,118})$")
_ENUMERATOR = re.compile(
    r"^(?:\((?P<paren>[0-9]{1,3}[a-z]?|[a-z]{1,2}|[ivxlc]{1,6})\)"
    r"|(?P<dot>[0-9]{1,3}(?:\.[0-9]{1,3}){0,4})[.)]\s"
    r"|(?P<letter>[a-z])\)\s"
    r"|§\s*(?P<sect>[0-9]+[a-z]?))", re.I)
_BULLET = re.compile(r"^[-•*–]\s")
_SENTENCE_END = re.compile(r"[.;:!?][\"'’”)\]]*$")
_MODAL = re.compile(r"\b(?:shall|must|may|should|will|is required|are required)\b", re.I)

_KIND_SLUG = {"article": "art", "art.": "art", "section": "sec", "sec.": "sec", "§": "sec", "rule": "rule",
              "clause": "cl", "regulation": "reg", "standard": "std", "requirement": "req", "control": "ctl",
              "principle": "prin", "part": "part", "title": "title", "chapter": "ch", "book": "book",
              "annex": "annex", "schedule": "sch", "appendix": "app", "subpart": "subpart", "division": "div"}


# --------------------------------------------------------------------- rendering


def _decode(body: bytes) -> str:
    for encoding in ("utf-8-sig", "cp1252"):
        try:
            text = body.decode(encoding)
            break
        except UnicodeDecodeError:
            continue
    else:  # pragma: no cover - cp1252 decodes almost anything
        raise ValueError("Text is neither UTF-8 nor Windows-1252")
    if any(ord(c) < 32 and c not in "\t\r\n\f" for c in text):
        raise ValueError("This file is binary, not text; supply text, HTML or PDF")
    return text


def _looks_html(body: bytes, content_type: str, name: str) -> bool:
    if "html" in (content_type or "").lower() or name.lower().endswith((".html", ".htm", ".xhtml")):
        return True
    head = body[:2048].lstrip().lower()
    return head.startswith((b"<!doctype html", b"<html")) or b"<body" in head


def _html_text(body: bytes) -> str:
    from bs4 import BeautifulSoup

    from app.clhear.l1.adapters.html_document import blocks

    soup = BeautifulSoup(body, "html.parser")
    main = soup.find("main") or soup.find("article")
    records = blocks(str(main).encode() if main is not None else body)
    text = "\n\n".join(row["text"] for row in records if row["text"].strip())
    if not text.strip():
        raise ValueError("The HTML page has no readable text")
    return text


def _pdf_text(body: bytes) -> str:
    from pypdf import PdfReader

    reader = PdfReader(io.BytesIO(body))
    pages = [(page.extract_text() or "").strip("\n") for page in reader.pages]
    kept = [page for page in pages if page.strip()]
    if not kept:
        raise ValueError("The PDF has no text layer (a scan?); run OCR first and supply the text")
    return "\n\n".join(kept)


def render(body: bytes, *, content_type: str = "", name: str = "") -> tuple[str, str]:
    """(UTF-8 text, extraction method) for text, HTML or PDF bytes."""
    if not body:
        raise ValueError("The source is empty")
    if body.startswith(b"%PDF-"):
        return _pdf_text(body), "pypdf-page-text"
    if _looks_html(body, content_type, name):
        return _html_text(body), "html-text-blocks"
    return _decode(body), "utf-8-text"


# ------------------------------------------------------------------ segmentation


@dataclass
class Block:
    kind: str  # division | unit | paragraph
    lines: list[str]
    ref_hint: str = ""
    rank: int = 0


def _lines(text: str) -> list[str]:
    return [line.strip() for line in text.replace("\r\n", "\n").replace("\r", "\n").replace("\f", "\n").split("\n")]


def _paragraphs(text: str) -> list[list[str]]:
    groups: list[list[str]] = []
    current: list[str] = []
    for line in _lines(text):
        if not line:
            if current:
                groups.append(current)
                current = []
            continue
        starts_unit = bool(_ENUMERATOR.match(line) or _BULLET.match(line) or _DIVISION.match(line)
                           or (_UNIT.match(line) and len(line) <= 160))
        if current and starts_unit:
            groups.append(current)
            current = []
        current.append(line)
        if len(current) == 1 and _is_heading(current):
            groups.append(current)
            current = []
    if current:
        groups.append(current)
    out: list[list[str]] = []
    for group in groups:
        out.extend(_split_long(group))
    return out


def _split_long(lines: list[str]) -> list[list[str]]:
    if sum(len(line) for line in lines) <= LONG_PARAGRAPH:
        return [lines]
    parts, current, size = [], [], 0
    for line in lines:
        current.append(line)
        size += len(line)
        if size >= LONG_PARAGRAPH // 2 and _SENTENCE_END.search(line):
            parts.append(current)
            current, size = [], 0
    if current:
        parts.append(current)
    return parts


def _is_heading(lines: list[str]) -> bool:
    if len(lines) != 1 or len(lines[0]) > 160:
        return False
    line = lines[0]
    if _DIVISION.match(line):
        return True
    unit = _UNIT.match(line)
    if unit:
        rest = line[unit.end():].strip(" .:-–—")
        if not rest:
            return True
        return not _MODAL.search(rest) and (not _SENTENCE_END.search(line) or len(rest) < 90)
    numbered = _NUMBERED_HEADING.match(line)
    return bool(numbered and len(numbered.group("title").split()) <= 12 and not _MODAL.search(line))


def _slug(value: str) -> str:
    return re.sub(r"[^a-z0-9.]+", "-", value.lower()).strip("-.") or "x"


def classify(paragraph: list[str]) -> Block:
    first = paragraph[0]
    if _is_heading(paragraph):
        division = _DIVISION.match(first)
        if division:
            return Block("division", paragraph, f"{_KIND_SLUG[division.group('kind').lower()]}-{_slug(division.group('num'))}", 0)
        unit = _UNIT.match(first)
        if unit:
            kind = unit.group("kind").lower()
            kind = "§" if kind.startswith("§") else kind
            num = unit.group("num")
            return Block("unit", paragraph, f"{_KIND_SLUG[kind]}-{_slug(num)}", 1 + num.count("."))
        numbered = _NUMBERED_HEADING.match(first)
        return Block("unit", paragraph, _slug(numbered.group("num")), 1 + numbered.group("num").count("."))
    enum = _ENUMERATOR.match(first)
    hint = ""
    if enum:
        hint = _slug(enum.group("paren") or enum.group("dot") or enum.group("letter") or enum.group("sect") or "")
    else:
        unit = _UNIT.match(first)
        if unit:
            kind = unit.group("kind").lower()
            kind = "§" if kind.startswith("§") else kind
            hint = f"{_KIND_SLUG[kind]}-{_slug(unit.group('num'))}"
    block = Block("paragraph", paragraph, hint)
    if not enum and hint:
        block.rank = 1  # "Section 3. The organization shall ..." is a unit of its own
    if enum and enum.group("paren") and not enum.group("paren")[0].isdigit():
        block.rank = -1  # "(a)", "(iv)": an item of the preceding lead-in
    return block


def _title_line(block: Block) -> bool:
    line = block.lines[0]
    return (block.kind == "paragraph" and len(block.lines) == 1 and len(line) <= 120 and not block.ref_hint
            and not _SENTENCE_END.search(line) and not _MODAL.search(line))


def segment(text: str) -> list[Block]:
    """Classified blocks; a bare "Article 5" heading absorbs its title line."""
    blocks: list[Block] = []
    for paragraph in _paragraphs(text):
        block = classify(paragraph)
        previous = blocks[-1] if blocks else None
        if (previous is not None and previous.kind in {"division", "unit"} and len(previous.lines) == 1
                and _bare_marker(previous.lines[0]) and _title_line(block)):
            previous.lines.append(block.lines[0])
            continue
        blocks.append(block)
    return blocks


def _bare_marker(line: str) -> bool:
    match = _DIVISION.match(line) or _UNIT.match(line)
    return bool(match) and not line[match.end():].strip(" .:-–—")


def build_tree(text: str, source_key: str, *, locator: dict | None = None) -> list[DocNode]:
    """The DocNode tree for a rendered document (root title plus clauses)."""
    root = DocNode(node_type="title", ref=source_key, source_locator={"structure": "text-document", **(locator or {})})
    used: set[str] = {source_key}
    stack: list[tuple[int, DocNode]] = []  # (rank, heading node)
    counters: dict[int, int] = {}
    lead: tuple[DocNode, DocNode] | None = None  # (container, paragraph ending in ":")

    def unique(ref: str) -> str:
        base, n = ref, 2
        while ref in used:
            ref, n = f"{base}-{n}", n + 1
        used.add(ref)
        return ref

    for block in segment(text):
        body = "\n".join(block.lines)
        if block.kind in {"division", "unit"}:
            while stack and stack[-1][0] >= block.rank:
                stack.pop()
            parent = stack[-1][1] if stack else root
            node = DocNode(node_type="section", ref=unique(block.ref_hint), heading=body,
                           source_locator={"structure": "text-heading"})
            parent.children.append(node)
            stack.append((block.rank, node))
            continue
        if block.rank == 1:
            while stack and stack[-1][0] >= 1:
                stack.pop()
        parent = stack[-1][1] if stack else root
        if block.rank == -1 and lead is not None and lead[0] is parent:
            parent = lead[1]
        key = id(parent)
        counters[key] = counters.get(key, 0) + 1
        local = block.ref_hint or f"p{counters[key]}"
        ref = local if parent is root or block.rank == 1 else f"{parent.ref}/{local}"
        node = DocNode(node_type="subsection" if parent is not root else "section", ref=unique(ref),
                       raw_text=body, source_locator={"structure": "text-paragraph"})
        parent.children.append(node)
        if block.rank != -1:
            lead = (parent, node) if body.rstrip().endswith((":", "—", "-")) else None
    if not root.children:
        raise ValueError("The source has no text")
    return [root]


# ------------------------------------------------------------------ acquisition


def local_sources_dir() -> Path:
    return Path(os.environ.get(LOCAL_SOURCES_ENV, "sources")).resolve()


def read_local_path(path: str) -> tuple[bytes, str]:
    """Bytes of a file inside ``CLHEAR_LOCAL_SOURCES_DIR`` (default ``./sources``)."""
    root = local_sources_dir()
    candidate = Path(path)
    target = (candidate if candidate.is_absolute() else root / candidate).resolve()
    if target != root and root not in target.parents:
        raise PermissionError(f"Local sources must live under {root} (set {LOCAL_SOURCES_ENV} to change it)")
    if not target.is_file():
        raise FileNotFoundError(f"No such source file: {target}")
    body = target.read_bytes()
    if len(body) > MAX_BYTES:
        raise ValueError("The file is larger than 25 MB")
    return body, target.name


def _public_address(host: str) -> None:
    if os.environ.get(ALLOW_PRIVATE_ENV, "").lower() in {"1", "true", "yes"}:
        return
    try:
        infos = socket.getaddrinfo(host, 443, proto=socket.IPPROTO_TCP)
    except socket.gaierror as exc:
        raise ValueError(f"Cannot resolve {host}") from exc
    for info in infos:
        address = ipaddress.ip_address(info[4][0])
        if (address.is_private or address.is_loopback or address.is_link_local or address.is_reserved
                or address.is_multicast or address.is_unspecified):
            raise PermissionError(f"{host} resolves to a non-public address; set {ALLOW_PRIVATE_ENV}=1 to allow it")


def check_url(url: str) -> str:
    parts = urlsplit(url or "")
    if parts.scheme != "https" or not parts.hostname or parts.username or parts.password:
        raise ValueError("A URL source must be an https:// address without credentials")
    _public_address(parts.hostname)
    return url


def fetch_url(url: str) -> tuple[bytes, str]:
    """GET a public https URL: redirects re-checked, bounded size.

    ``CLHEAR_HTTP_MODE=replay`` (the test default when set) reads recorded
    fixtures through ``l1.http``; otherwise the publisher is contacted live.
    """
    from app.clhear.l1 import http

    if os.environ.get("CLHEAR_HTTP_MODE") in {"replay", "record"}:
        return http.get(url), ""
    import httpx

    current = check_url(url)
    for _ in range(MAX_REDIRECTS + 1):
        http._pace(current)
        with httpx.stream("GET", current, headers={"User-Agent": http.USER_AGENT}, timeout=30,
                          follow_redirects=False) as response:
            if response.is_redirect:
                current = check_url(urljoin(current, response.headers.get("location", "")))
                continue
            response.raise_for_status()
            body = bytearray()
            for chunk in response.iter_bytes():
                body.extend(chunk)
                if len(body) > MAX_BYTES:
                    raise ValueError("The page is larger than 25 MB")
            content = bytes(body)
            http._observe(current, "live", content)
            return content, response.headers.get("content-type", "")
    raise ValueError("Too many redirects")


class DocumentAdapter:
    """Pasted text, a local file (``local_text``) or a public URL (``url``)."""

    def __init__(self, entry: dict, meta: SourceMeta):
        self.key = entry["adapter"]
        self._entry = entry
        self._meta = meta
        self._source_key = entry["key"]
        self.fetch_origin = "local_snapshot" if self.key == "local_text" else None

    def meta(self) -> SourceMeta:
        return self._meta

    def _original(self) -> tuple[bytes, str, str]:
        locator = self._entry.get("fetch") or {}
        if self.key == "url" or (locator.get("url") and not (locator.get("text") or locator.get("path"))):
            body, content_type = fetch_url(locator.get("url") or self._meta.canonical_url)
            return body, content_type, urlsplit(locator.get("url") or "").path.rsplit("/", 1)[-1]
        if isinstance(locator.get("text"), str) and locator["text"].strip():
            fmt = (locator.get("format") or "text").lower()
            return locator["text"].encode("utf-8"), "text/html" if fmt == "html" else "text/plain", ""
        if locator.get("path"):
            body, name = read_local_path(locator["path"])
            return body, "", name
        raise ValueError('This source needs a locator with "text", "path" or "url"')

    def fetch(self, since_version: str | None = None) -> FetchResult | None:
        body, content_type, name = self._original()
        if content_type == "text/plain":
            text, method = _decode(body), "utf-8-text"
        else:
            text, method = render(body, content_type=content_type, name=name)
        rendered = text.encode("utf-8")
        original = {"original_sha256": hashlib.sha256(body).hexdigest(), "original_bytes": len(body),
                    "original_content_type": content_type or ("application/pdf" if body.startswith(b"%PDF-") else ""),
                    "extraction": method}
        tree = build_tree(text, self._source_key, locator=original)
        digest = hashlib.sha256(rendered).hexdigest()
        return FetchResult(
            version_label=f"edition:sha256-{digest}",
            artifacts=[Artifact(name=TEXT_ARTIFACT, content=rendered, content_type="text/plain; charset=utf-8")],
            tree=tree,
            version_kind="edition" if self.key == "local_text" else "consolidated",
        )

    def expected_text(self, artifacts: list[Artifact]) -> list[str]:
        return [line.strip() for artifact in artifacts
                for line in artifact.content.decode("utf-8").splitlines() if line.strip()]


def verify(artifacts: list[Artifact], tree: list[DocNode]) -> bool:
    """Every stored node is a run of whole, consecutive lines of source.txt.

    Written without the segmenter: it walks the stored nodes in document order
    and consumes the rendered file line by line, so a node that splits a line,
    skips a line, reorders text or adds words fails.
    """
    if len(artifacts) != 1 or artifacts[0].name != TEXT_ARTIFACT:
        return False
    lines = [line.strip() for line in artifacts[0].content.decode("utf-8").splitlines()]
    lines = [line for line in lines if line]
    cursor = 0
    for root in tree:
        for node in root.walk():
            for value in (node.heading, node.raw_text):
                if not value:
                    continue
                chunk = [line.strip() for line in value.split("\n")]
                if lines[cursor:cursor + len(chunk)] != chunk:
                    return False
                cursor += len(chunk)
    return cursor == len(lines)
