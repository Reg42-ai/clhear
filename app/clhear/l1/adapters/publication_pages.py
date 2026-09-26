# Copyright (C) 2026 Reg42 AI
# This file is part of CLHEAR. See LICENSE (AGPL-3.0-only).
"""Government publication pages, read as their own blocks of text.

A publication page (an SEC press release, staff FAQ, risk alert or adopting
release; an FTC business-guidance page) is a title region and a body region
inside site chrome. Each body block becomes one provision: a paragraph or
other text block, or one list item. A heading opens a group. This keeps a
sweep release's charged firms, a risk alert's observations and an FAQ's
answers individually addressable for L7 and L8.

The adapter reads with BeautifulSoup. ``original_blocks`` and ``verify``
re-read the same regions with the standard-library parser and must produce
the same ordered blocks.
"""
from __future__ import annotations

from dataclasses import dataclass

from bs4 import BeautifulSoup, Comment, NavigableString, Tag

from app.clhear.l1.adapters.base import DocNode

STRUCTURE = "publication-blocks"
HEADINGS = frozenset({"h1", "h2", "h3", "h4", "h5", "h6"})
LISTS = frozenset({"ul", "ol"})
CONTAINERS = frozenset({"div", "section", "article", "main", "header"})
TEXT_BLOCKS = frozenset({"p", "table", "pre", "blockquote", "dl", "figure", "address", "details"})


@dataclass(frozen=True)
class Profile:
    document_type: str
    title_tag: str
    title_class: str | None
    body_tag: str
    body_class: str


PROFILES = {
    "sec_page": Profile("sec_page", "div", "page-title", "div", "node-details-layout__main-region__content"),
    "ftc_page": Profile("ftc_page", "h1", None, "div", "field--name-body"),
}


def profile_for(content: bytes) -> Profile | None:
    if content.startswith(b"%PDF-"):
        return None
    for profile in PROFILES.values():
        body = profile.body_class.encode() in content
        title = (f'"{profile.title_class}'.encode() in content) if profile.title_class else b"<h1" in content
        if body and title:
            return profile
    return None


def _classes(value) -> list[str]:
    return value if isinstance(value, list) else (value or "").split()


def _norm(text: str) -> str:
    return " ".join(text.split())


# --------------------------------------------------------------- adapter reader (BeautifulSoup)


def _omit() -> frozenset:
    from app.clhear.l1.originals import _HTMLText

    return frozenset(_HTMLText.OMIT)


def _block_breaks() -> frozenset:
    from app.clhear.l1.originals import _HTMLText

    return frozenset(_HTMLText.BLOCKS)


def _bs4_raw(node, omit, breaks) -> str:
    if isinstance(node, Comment):
        return ""
    if isinstance(node, NavigableString):
        return str(node)
    if not isinstance(node, Tag) or node.name in omit:
        return ""
    inner = "".join(_bs4_raw(child, omit, breaks) for child in node.children)
    return f"\n{inner}\n" if node.name in breaks else inner


def _bs4_blocks(node, omit, breaks) -> list[tuple[str, str]]:
    out, pending = [], []

    def flush():
        text = _norm("".join(pending))
        pending.clear()
        if text:
            out.append(("text", text))

    for child in node.children:
        if isinstance(child, Comment):
            continue
        if isinstance(child, NavigableString):
            pending.append(str(child))
            continue
        if not isinstance(child, Tag) or child.name in omit:
            continue
        name = child.name
        if name in HEADINGS:
            flush()
            out.append(("heading", _norm(_bs4_raw(child, omit, breaks))))
        elif name in LISTS:
            flush()
            for item in child.children:
                if isinstance(item, Tag) and item.name == "li":
                    text = _norm(_bs4_raw(item, omit, breaks))
                    if text:
                        out.append(("item", text))
                elif isinstance(item, Tag):
                    out.extend(_bs4_blocks(item, omit, breaks))
                elif isinstance(item, NavigableString) and not isinstance(item, Comment) and _norm(str(item)):
                    out.append(("text", _norm(str(item))))
        elif name in CONTAINERS:
            flush()
            out.extend(_bs4_blocks(child, omit, breaks))
        elif name in TEXT_BLOCKS:
            flush()
            text = _norm(_bs4_raw(child, omit, breaks))
            if text:
                out.append(("text", text))
        else:
            pending.append(_bs4_raw(child, omit, breaks))
    flush()
    return out


def _bs4_regions(content: bytes, profile: Profile):
    soup = BeautifulSoup(content, "html.parser")

    def find(tag, cls):
        return next((el for el in soup.find_all(tag) if cls is None or cls in _classes(el.get("class"))), None)

    title, body = find(profile.title_tag, profile.title_class), find(profile.body_tag, profile.body_class)
    if title is None or body is None:
        raise ValueError(f"Expected a {profile.document_type} publication with a title and a body region")
    omit, breaks = _omit(), _block_breaks()
    return _norm(_bs4_raw(title, omit, breaks)), _bs4_blocks(body, omit, breaks)


def build_tree(title_text: str, blocks: list[tuple[str, str]], source_key: str, title: str,
               document_type: str) -> list[DocNode]:
    root = DocNode(node_type="title", ref=source_key, heading=title,
                   source_locator={"structure": STRUCTURE, "region": "document", "document_type": document_type})
    # The publication's own title, once, before its body blocks.
    root.children.append(DocNode(node_type="group", heading=title_text,
                                 source_locator={"structure": STRUCTURE, "block": "title"}))
    parent, number = root, 0
    for kind, text in blocks:
        if kind == "heading":
            if not text:
                parent = root
                continue
            parent = DocNode(node_type="group", heading=text,
                             source_locator={"structure": STRUCTURE, "block": "heading"})
            root.children.append(parent)
            continue
        number += 1
        parent.children.append(DocNode(
            node_type="provision", ref=f"{source_key}/b{number}", raw_text=text,
            source_locator={"structure": STRUCTURE, "block": kind, "index": number}))
    if number == 0:
        raise ValueError("Publication body has no text")
    return [root]


def parse(content: bytes, source_key: str, title: str, profile: Profile | None = None) -> list[DocNode]:
    profile = profile or profile_for(content)
    if profile is None:
        raise ValueError("Unrecognized publication page")
    title_text, blocks = _bs4_regions(content, profile)
    if not title_text:
        raise ValueError("Publication title is empty")
    return build_tree(title_text, blocks, source_key, title, profile.document_type)


# --------------------------------------------------------------- independent reader (stdlib)


def _std_raw(node, omit, breaks) -> str:
    if isinstance(node, str):
        return node
    if node["tag"] in omit:
        return ""
    inner = "".join(_std_raw(child, omit, breaks) for child in node["children"])
    return f"\n{inner}\n" if node["tag"] in breaks else inner


def _std_blocks(node, omit, breaks) -> list[tuple[str, str]]:
    out, pending = [], []

    def flush():
        text = _norm("".join(pending))
        pending.clear()
        if text:
            out.append(("text", text))

    for child in node["children"]:
        if isinstance(child, str):
            pending.append(child)
            continue
        name = child["tag"]
        if name in omit:
            continue
        if name in HEADINGS:
            flush()
            out.append(("heading", _norm(_std_raw(child, omit, breaks))))
        elif name in LISTS:
            flush()
            for item in child["children"]:
                if isinstance(item, dict) and item["tag"] == "li":
                    text = _norm(_std_raw(item, omit, breaks))
                    if text:
                        out.append(("item", text))
                elif isinstance(item, dict):
                    out.extend(_std_blocks(item, omit, breaks))
                elif _norm(item):
                    out.append(("text", _norm(item)))
        elif name in CONTAINERS:
            flush()
            out.extend(_std_blocks(child, omit, breaks))
        elif name in TEXT_BLOCKS:
            flush()
            text = _norm(_std_raw(child, omit, breaks))
            if text:
                out.append(("text", text))
        else:
            pending.append(_std_raw(child, omit, breaks))
    flush()
    return out


def original_blocks(content: bytes, profile: Profile | None = None):
    """Independent read: stdlib DOM, the first title and body regions of the profile."""
    from app.clhear.l1.originals import _HTMLStructure, _decode_html

    profile = profile or profile_for(content)
    if profile is None:
        raise ValueError("Unrecognized publication page")
    reader = _HTMLStructure()
    reader.feed(_decode_html(content))

    def find(node, tag, cls):
        if isinstance(node, dict):
            if node["tag"] == tag and (cls is None or cls in node.get("attrs", {}).get("class", "").split()):
                return node
            for child in node["children"]:
                found = find(child, tag, cls)
                if found is not None:
                    return found
        return None

    title = find(reader.root, profile.title_tag, profile.title_class)
    body = find(reader.root, profile.body_tag, profile.body_class)
    if title is None or body is None:
        raise ValueError("Publication regions are missing")
    omit, breaks = _omit(), _block_breaks()
    return _norm(_std_raw(title, omit, breaks)), _std_blocks(body, omit, breaks)


def original_text(content: bytes) -> str:
    title, blocks = original_blocks(content)
    return " ".join([title, *(text for kind, text in blocks if text)])


def publication_title(root: DocNode) -> str:
    first = root.children[0] if root.children else None
    return first.heading if first is not None and first.source_locator.get("block") == "title" else ""


def _tree_blocks(root: DocNode) -> list[tuple[str, str]]:
    out = []
    for child in root.children[1:]:
        if child.node_type == "group":
            out.append(("heading", child.heading))
            out.extend((n.source_locator.get("block"), n.raw_text) for n in child.children)
        else:
            out.append((child.source_locator.get("block"), child.raw_text))
    return out


def verify(artifacts, source_key: str, tree: list[DocNode]) -> bool:
    if len(artifacts) != 1 or len(tree) != 1:
        return False
    root = tree[0]
    if root.node_type != "title" or root.ref != source_key or root.raw_text:
        return False
    title, blocks = original_blocks(artifacts[0].content)
    expected = [(kind, text) for kind, text in blocks if kind != "heading" or text]
    numbered = [n for n in root.walk() if n.node_type == "provision"]
    refs_ok = all(n.ref == f"{source_key}/b{i}" and not n.children and not n.heading and not n.label
                  and n.source_locator.get("structure") == STRUCTURE for i, n in enumerate(numbered, 1))
    return refs_ok and publication_title(root) == title and _tree_blocks(root) == expected
