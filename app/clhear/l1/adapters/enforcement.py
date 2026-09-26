# Copyright (C) 2026 Reg42 AI
# This file is part of CLHEAR. See LICENSE (AGPL-3.0-only).
"""Enforcement sources (HLD v2 §4.7): final notices, enforcement actions and
disciplinary decisions ingested as L1 sources that L7 reads.

Same verbatim contract as every adapter — retrieval + structural parse only.
A listing page (or RSS feed) of outcomes becomes one ``provision`` node per
outcome: the ref is the regulator's stable slug for the notice, the label is
its title and the raw text is the entry as published (title, date, summary,
penalty line). Nothing is normalised here; :mod:`app.clhear.l7.enforcement`
reads the stored clauses and derives the structured event (date, respondent,
amount, cited provisions) with a why-trail.

Enforcement sources are *informative* family members (``kind="enforcement"``):
the L2 extractor never derives obligations from a notice's prose.

``FcaFinalNoticesAdapter``    fca.org.uk final notices (licensed; derived facts published)
``SecEnforcementAdapter``     sec.gov litigation releases + administrative proceedings
                              (RSS; public domain)
``FinraDisciplinaryAdapter``  finra.org disciplinary actions (derived-only rights)
"""
from __future__ import annotations

import re
import warnings
from urllib.parse import urlparse

from bs4 import BeautifulSoup, Tag, XMLParsedAsHTMLWarning

warnings.filterwarnings("ignore", category=XMLParsedAsHTMLWarning)

from app.clhear.l1.adapters.base import DocNode
from app.clhear.l1.adapters.official_html import _strip_chrome, _visible_strings
from app.clhear.l1.adapters.publisher import NumberedHtmlAdapter, unique_ref

__all__ = ["EnforcementListAdapter", "FcaFinalNoticesAdapter", "FinraDisciplinaryAdapter", "SecEnforcementAdapter"]

_ITEM_TAGS = ("article", "li", "tr")
_SLUG_JUNK = re.compile(r"[^A-Za-z0-9._-]+")


def _slug(href: str, fallback: str) -> str:
    path = urlparse(href or "").path.rstrip("/")
    last = path.rsplit("/", 1)[-1] if path else ""
    last = re.sub(r"\.(html?|pdf|xml)$", "", last, flags=re.I)
    slug = _SLUG_JUNK.sub("-", last).strip("-")
    return slug or _SLUG_JUNK.sub("-", fallback).strip("-")[:80] or "notice"


def _feed_link(item: Tag) -> str:
    link = item.find("link")
    if link is not None:
        text = link.get_text(strip=True)
        if text:
            return text
        sib = link.next_sibling
        if isinstance(sib, str) and sib.strip():
            return sib.strip()
    guid = item.find("guid")
    return guid.get_text(strip=True) if guid is not None else ""


class EnforcementListAdapter(NumberedHtmlAdapter):
    """One provision per outcome on a listing page or feed."""

    key = "enforcement"
    kind = "enforcement"
    version_kind = "as_published"
    version_policy = "as_published"
    # CSS selector for the outcome entries; ``None`` = any article / li / tr with a link.
    ITEM_SELECTOR: str | None = None
    # Feeds: an <item> per outcome.
    FEED_ITEM = "item"

    def _entries_html(self, soup: BeautifulSoup) -> list[Tag]:
        if self.ITEM_SELECTOR:
            found = soup.select(self.ITEM_SELECTOR)
            if found:
                return [el for el in found if el.find("a", href=True)]
        body = soup.body or soup
        out: list[Tag] = []
        for el in body.find_all(_ITEM_TAGS):
            if not el.find("a", href=True):
                continue
            # keep the outermost entry: an <li> inside an <article> is part of the article
            if any(isinstance(p, Tag) and p.name in _ITEM_TAGS and p.find("a", href=True) for p in el.parents):
                continue
            out.append(el)
        return out

    def _parse_into(self, content: bytes, seen: set[str], *, part: int) -> list[DocNode]:
        head = content.lstrip()[:200].lower()
        is_feed = head.startswith(b"<?xml") or b"<rss" in head or b"<feed" in head
        # html.parser for feeds too (no lxml dependency): RSS <link> is a void tag there,
        # so its url is the following text node — see _feed_link.
        soup = BeautifulSoup(content, "html.parser")
        if not is_feed:
            soup = _strip_chrome(soup)
        title_el = soup.find("title")
        page_title = title_el.get_text(" ", strip=True) if title_el else ""
        nodes: list[DocNode] = []
        group = DocNode(node_type="group", ref=unique_ref(f"{self._source_key}/p{part}s1", seen),
                        heading=page_title or self._title)
        nodes.append(group)
        entries = soup.find_all(self.FEED_ITEM) if is_feed else self._entries_html(soup)
        for i, el in enumerate(entries, start=1):
            if is_feed:
                href = _feed_link(el)
                title_node = el.find("title")
                label = title_node.get_text(" ", strip=True) if title_node else ""
                pieces = _visible_strings(el)
            else:
                a = el.find("a", href=True)
                href = a["href"] if a else ""
                label = a.get_text(" ", strip=True) if a else ""
                pieces = _visible_strings(el)
            ref = unique_ref(_slug(href, label or f"p{part}-{i}"), seen)
            if label and label in pieces:  # the label is the node's own field; the body carries the rest
                pieces = list(pieces)
                pieces.remove(label)
            text = "\n".join(p for p in pieces if p)
            # heading == label is allowed by the fidelity linter and puts the title into the
            # clause projection (heading + raw_text) where the L7 ingestor reads it
            group.children.append(DocNode(node_type="provision", ref=ref, label=label or ref, heading=label or ref,
                                          raw_text=text, source_fragment=str(el)[:2000]))
        # fidelity: any visible text outside the entries lands in a note
        haystack = f"{self._title} {page_title} " + " ".join(
            piece for n in nodes for m in n.walk() for piece in (m.label, m.heading, m.raw_text) if piece)
        leftover = []
        for span in _visible_strings(soup):
            if span not in haystack:
                leftover.append(span)
                haystack += " " + span
        if leftover:
            note = DocNode(node_type="note", ref=unique_ref(f"{self._source_key}/p{part}/visible", seen),
                           heading="Visible text not captured as an outcome entry")
            note.children.extend(DocNode(node_type="paragraph", raw_text=s) for s in leftover)
            nodes.append(note)
        return [DocNode(node_type="chapter", ref=unique_ref(f"{self._source_key}/p{part}", seen),
                        heading=page_title or f"Part {part}", children=nodes)]

    def expected_text(self, artifacts):
        spans: list[str] = []
        for artifact in artifacts:
            head = artifact.content.lstrip()[:200].lower()
            is_feed = head.startswith(b"<?xml") or b"<rss" in head or b"<feed" in head
            soup = BeautifulSoup(artifact.content, "html.parser")
            spans.extend(_visible_strings(soup if is_feed else _strip_chrome(soup)))
        return spans


class FcaFinalNoticesAdapter(EnforcementListAdapter):
    key = "fca_enforcement"
    publisher = "Financial Conduct Authority"
    issuer = "Financial Conduct Authority"
    jurisdiction = "UK"
    instrument = "FCA final notices"
    renderer = "crawl4ai"  # the publications search is rendered client-side; plain HTTP still yields the entries
    ITEM_SELECTOR = "li.search-item, article, .views-row, li"


class SecEnforcementAdapter(EnforcementListAdapter):
    key = "sec_enforcement"
    publisher = "U.S. Securities and Exchange Commission"
    issuer = "U.S. Securities and Exchange Commission"
    jurisdiction = "US"
    instrument = "SEC enforcement actions"
    headers = {"User-Agent": "CLHEAR by Reg42 (compliance@reg42.ai)"}


class FinraDisciplinaryAdapter(EnforcementListAdapter):
    key = "finra_enforcement"
    publisher = "FINRA"
    issuer = "FINRA"
    jurisdiction = "US"
    instrument = "FINRA disciplinary actions"
    ITEM_SELECTOR = "tr, article, li"
