# Copyright (C) 2026 Reg42 AI
# This file is part of CLHEAR. See LICENSE (AGPL-3.0-only).
"""Ordered HTML text blocks with explicit, verifiable heading structure.

BeautifulSoup implements the adapter. A separate stdlib HTMLParser reader
supplies the verifier's expected source blocks; it never calls this parser.
"""
import re
from bs4 import BeautifulSoup, Comment, Declaration, Doctype, NavigableString, ProcessingInstruction, Tag

from app.clhear.l1.adapters.base import DocNode

BLOCKS = {"p", "div", "li", "ul", "ol", "table", "tbody", "thead", "tfoot", "tr", "td", "th", "section", "article", "blockquote", "pre", "h1", "h2", "h3", "h4", "h5", "h6", "br"}
OMIT = {"head", "script", "style", "noscript", "nav", "header", "footer", "aside", "form", "button", "svg", "iframe", "template"}


def _norm(value):
    return " ".join(value.split())


def blocks(content):
    soup = BeautifulSoup(content, "html.parser")
    output = []

    def visit(element, path):
        chunks, occurrence, counters = [], 0, {}

        def flush():
            nonlocal occurrence
            text = _norm("".join(chunks))
            chunks.clear()
            if text:
                occurrence += 1
                output.append({"path": path + f"/text-block()[{occurrence}]", "tag": element.name,
                               "text": text, "fragment": str(element)})

        for child in element.children:
            if isinstance(child, (Comment, Doctype, Declaration, ProcessingInstruction)):
                continue
            if isinstance(child, NavigableString):
                chunks.append(str(child))
            elif isinstance(child, Tag):
                counters[child.name] = counters.get(child.name, 0) + 1
                child_path = path + f"/{child.name}[{counters[child.name]}]"
                if child.name in OMIT:
                    continue
                if child.name in BLOCKS or child.find(list(BLOCKS | OMIT)):
                    flush()
                    if child.name != "br":
                        visit(child, child_path)
                else:
                    chunks.append(child.get_text())
        flush()

    visit(soup, "")
    return output


def parse(content, source_key, *, provision=None, make_ref=None, status_of=None, part=1):
    records = blocks(content)
    if not records:
        raise ValueError("HTML original contains no document text")
    numbered = any(provision and provision.match(row["text"]) for row in records)
    root = DocNode(node_type="title", ref=source_key, source_locator={"structure": "html-root", "part": part})
    headings, active, seen = [], None, {source_key}
    for row in records:
        text = row["text"]
        match = provision.match(text) if provision else None
        level = int(row["tag"][1]) if re.fullmatch(r"h[1-6]", row["tag"]) else None
        locator = {"structure": "html-block", "part": part, "path": row["path"], "tag": row["tag"]}
        if level is not None and not match:
            while headings and headings[-1][0] >= level:
                headings.pop()
            parent = headings[-1][1] if headings else root
            ref = f"{source_key}/html/{part}{row['path']}"
            node = DocNode(node_type="group" if numbered else "section", ref=ref, heading=text,
                           source_fragment=row["fragment"], source_locator=locator)
            parent.children.append(node)
            headings.append((level, node))
            active = None
        elif match:
            context = {"section": headings[-1][1].heading if headings else "", "part": part, "seen": seen}
            ref = make_ref(match, context) if make_ref else _norm(match.group("ref"))
            if ref in seen:
                # Chapter TOC and cross-references reprint the same citation.
                # The first provision keeps the publisher identity; later hits
                # stay as addressable text so a multi-chapter sourcebook can
                # ingest instead of failing closed on a repeated number.
                node = DocNode(node_type="paragraph", raw_text=text,
                               source_fragment=row["fragment"], source_locator=locator)
                (active or (headings[-1][1] if headings else root)).children.append(node)
                continue
            seen.add(ref)
            node = DocNode(node_type="provision", ref=ref, label=text[:match.end()].strip(), raw_text=text[match.end():].strip(),
                           status=status_of(match) if status_of else "", source_fragment=row["fragment"], source_locator=locator)
            (headings[-1][1] if headings else root).children.append(node)
            active = node
        else:
            node = DocNode(node_type="paragraph", raw_text=text, source_fragment=row["fragment"], source_locator=locator)
            (active or (headings[-1][1] if headings else root)).children.append(node)
    # An unnumbered document without headings remains explicitly addressable
    # at document grain, never mislabeled as a publisher-numbered provision.
    if not numbered and not any(n.node_type == "section" for n in root.walk()):
        root.children = [DocNode(node_type="section", ref=f"{source_key}/document", children=root.children,
                                 source_locator={"structure": "html-document", "part": part})]
    return [root]
