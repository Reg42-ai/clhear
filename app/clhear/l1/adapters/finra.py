# Copyright (C) 2026 Reg42 AI
# This file is part of CLHEAR. See LICENSE (AGPL-3.0-only).
"""FINRA rule articles: ordered source text and addressable paragraph structure.

The website's article element is its book navigation. Rule text lives in the
body field under #the-rule; never infer a successful rule import from navigation.
"""
from __future__ import annotations

import re
from dataclasses import dataclass
from urllib.parse import urlparse

from bs4 import BeautifulSoup, Comment, NavigableString, Tag

from app.clhear.l1.adapters.base import Artifact, DocNode

_BODY = "#the-rule #block-body .field--name-body"
_BLOCKS = frozenset({"div", "p", "li", "ul", "ol", "table", "tbody", "thead", "tfoot", "tr", "td", "th", "blockquote", "pre", "section", "article", "h1", "h2", "h3", "h4", "h5", "h6"})
_IGNORED = frozenset({"script", "style", "noscript", "template"})
_RULE = re.compile(r"^(?P<rule>\d{4,5}[A-Z]?)\.\s+(?P<title>\S.*)$", re.S)
# Official FINRA markers only: (a)/(A)/(1)/(i)/(iv) and .01 supplementary.
# Parentheticals such as (Date) in Uniform Practice Code notes are not markers;
# treating them as labels crashed 11630/11870 with duplicate 11630.01(Date).
_MARKER = re.compile(
    r"^(?P<label>(?:\((?:[A-Za-z]|\d{1,2}|[ivxlcdm]{2,4}|[IVXLCDM]{2,4})\))+|\.\d{2})(?=\s|$)"
)
_TOKENS = re.compile(r"\(([A-Za-z0-9]+)\)")
_ROMAN = re.compile(r"^[ivxlcdm]+$")


def normalize(text: str) -> str:
    return " ".join(text.split())


def _heading_page(soup: BeautifulSoup, title: Tag) -> bool:
    """Series titles and reserved stubs have a numbered h1 but no official body.

    Live 20 Sep 2026 examples: 1018 'Reserved', 1100 'MEMBER APPLICATION',
    11300 'DELIVERY OF SECURITIES'. Child / next / parent links live in
    book-navigation; the article field is absent by design.
    """
    heading = normalize(title.get_text())
    if re.search(r"\bReserved\b", heading):
        return True
    nav = soup.select_one(".node__content .book-navigation, main .book-navigation, #the-rule .book-navigation")
    return bool(nav and nav.find_all("a", href=True))


def _scope(content: bytes) -> tuple[Tag, Tag, str]:
    soup = BeautifulSoup(content, "html.parser")
    title = soup.find("h1")
    if title is None or not (match := _RULE.match(normalize(title.get_text()))):
        # Live 21 Sep 2026: 13400 returned 200 with no numbered rule h1.
        # That is a catalog/index shape, not a fetch crash.
        from app.clhear.l1.adapters.finra_document import NavigationPage
        raise NavigationPage("FINRA article has no numbered rule h1 (rulebook indexes are not rule articles)")
    body = soup.select_one(_BODY)
    if body is None:
        # Series headings and reserved numbers are catalog structure. Treating
        # them as fetch crashes keeps the rulebook cycle retrying forever.
        if _heading_page(soup, title):
            from app.clhear.l1.adapters.finra_document import NavigationPage
            raise NavigationPage("FINRA rule path is a series heading or reserved stub; child pages carry the text")
        # Retain the small, chrome-free historical golden fixtures. A full site
        # page with a missing field is a publisher/selector failure, never a fallback.
        if soup.select_one("#the-rule, #block-body, article, main, nav, header, footer"):
            raise ValueError("FINRA article is missing its official rule body field")
        body = soup.body
        if body is None:
            raise ValueError("FINRA article is missing its rule body")
    if not normalize(_visible_text(body, exclude=title)):
        # Live 21 Sep 2026: 9130/9140/9260/9350/9520 published an empty official
        # field. Retrying them as failed keeps execution_failed true forever.
        from app.clhear.l1.adapters.finra_document import NavigationPage
        raise NavigationPage("FINRA rule body is empty; child or sibling pages carry the text")
    return title, body, match.group("rule")


def _visible_text(root: Tag, *, exclude: Tag | None = None) -> str:
    """Independent oracle: text-node walk with block breaks, no structural parse.

    Inline adjacency is retained (e.g. an inline link followed by punctuation).
    Only HTML whitespace is normalized when comparing, never words or punctuation.
    """
    parts: list[str] = []

    def visit(node) -> None:
        if node is exclude or isinstance(node, Comment):
            return
        if isinstance(node, NavigableString):
            parts.append(str(node))
        elif isinstance(node, Tag) and node.name not in _IGNORED:
            boundary = node.name in _BLOCKS or node.name == "br"
            if boundary:
                parts.append("\n")
            for child in node.children:
                visit(child)
            if boundary:
                parts.append("\n")

    visit(root)
    return "".join(parts)


def expected_text(content: bytes) -> list[str]:
    title, body, _ = _scope(content)
    return [normalize(_visible_text(title)), normalize(_visible_text(body, exclude=title))]


def validate_identity(rule: str, source_key: str, url: str = "") -> None:
    match = re.fullmatch(r"finra/rule/(\d{4,5}[A-Z]?)", source_key)
    if not match or match.group(1) != rule:
        raise ValueError(f"FINRA source identity mismatch: {source_key!r} contains rule {rule}")
    parsed = urlparse(url)
    if parsed.hostname in {"finra.org", "www.finra.org"}:
        requested = parsed.path.rstrip("/").rsplit("/", 1)[-1]
        if re.fullmatch(r"\d{4,5}[A-Z]?", requested) and requested != rule:
            raise ValueError(f"FINRA acquisition URL rule {requested} does not match source rule {rule}")


@dataclass
class _Segment:
    text: str
    element: Tag
    ancestors: tuple[Tag, ...]


def _segments(root: Tag, *, exclude: Tag | None = None):
    """Emit each inline run once, splitting at nested block elements and br."""
    def visit(element: Tag, ancestors: tuple[Tag, ...]):
        chunks: list[str] = []

        def flush():
            text = normalize("".join(chunks))
            chunks.clear()
            return _Segment(text, element, ancestors) if text else None

        for child in element.children:
            if child is exclude or isinstance(child, Comment):
                continue
            if isinstance(child, NavigableString):
                chunks.append(str(child))
            elif isinstance(child, Tag):
                if child.name in _IGNORED:
                    continue
                if child.name == "br" or child.name in _BLOCKS:
                    if segment := flush():
                        yield segment
                    if child.name != "br":
                        yield from visit(child, (*ancestors, element))
                else:
                    # Inline markup may contain a block in malformed publisher
                    # HTML. Recurse in that case rather than concatenate it twice.
                    if child.find(list(_BLOCKS)) or child.find("br"):
                        if segment := flush():
                            yield segment
                        yield from visit(child, (*ancestors, element))
                    else:
                        chunks.append(child.get_text())
        if segment := flush():
            yield segment

    yield from visit(root, ())


def _is_note(segment: _Segment) -> bool:
    for element in (segment.element, *segment.ancestors):
        classes = " ".join(element.get("class", []))
        ident = str(element.get("id", ""))
        if "footnote" in classes.lower() or ident.lower().startswith("footnote"):
            return True
    return False


def _level(token: str, current: dict[int, str], segment: _Segment, parent: dict[int, str]) -> int:
    if token.isdigit():
        return 1
    if token.isupper():
        return 2
    if len(token) > 1 and _ROMAN.fullmatch(token):
        return 3
    # A letter i at the top of a rule is not a roman subparagraph. FINRA's
    # explicit firstpara class and a enclosing numbered block disambiguate it.
    first = "indent_firstpara" in segment.element.get("class", []) or (
        not parent and any("indent_firstpara" in a.get("class", []) for a in segment.ancestors)
    )
    if token in {"i", "v", "x"} and not first and (parent or 2 in current):
        return 3
    return 0


def parse(content: bytes, source_key: str, url: str = "") -> list[DocNode]:
    title, body, rule = _scope(content)
    validate_identity(rule, source_key, url)
    title_text = normalize(_visible_text(title))
    match = _RULE.match(title_text)
    assert match is not None
    root = DocNode(node_type="title", ref=source_key)
    title_node = DocNode(node_type="provision", ref=rule, label=f"{rule}.", raw_text=match.group("title"), source_fragment=str(title))
    root.children.append(title_node)
    current: dict[int, str] = {}
    groups: dict[str, DocNode] = {}
    clauses: dict[str, DocNode] = {rule: title_node}
    ancestor_paths: dict[int, tuple[str, dict[int, str]]] = {}
    touched: set[int] = set()
    latest = title_node
    base = rule
    notes: DocNode | None = None

    for segment in _segments(body, exclude=title):
        text = segment.text
        parents = [ancestor_paths[id(a)] for a in reversed(segment.ancestors) if id(a) in ancestor_paths]
        parent_base, parent_path = parents[0] if parents else (base, {})
        marker = _MARKER.match(text)
        if _is_note(segment):
            if notes is None:
                notes = DocNode(node_type="note", ref=f"{rule}/history-notes")
                root.children.append(notes)
            notes.children.append(DocNode(node_type="paragraph", raw_text=text, source_fragment=str(segment.element)))
        elif "supplementary material" in text.lower() and len(text) < 120 and marker is None:
            base, current = rule, {}
            latest = DocNode(node_type="note", ref=f"{rule}/supplementary-heading", raw_text=text, source_fragment=str(segment.element))
            root.children.append(latest)
        elif marker:
            label = marker.group("label")
            if label.startswith("."):
                base, current = f"{rule}{label}", {}
                ref, parent_ref = base, ""
            else:
                for token in _TOKENS.findall(label):
                    level = _level(token, current, segment, parent_path)
                    current = {k: v for k, v in current.items() if k < level}
                    current[level] = token
                ref = base + "".join(f"({v})" for _, v in sorted(current.items()))
                parent_ref = base + "".join(f"({v})" for k, v in sorted(current.items()) if k < max(current))
            if ref in clauses:
                raise ValueError(f"FINRA duplicate paragraph reference {ref}")
            group = DocNode(node_type="group", ref=f"{ref}/structure")
            latest = DocNode(node_type="provision", ref=ref, label=label, raw_text=text[marker.end():].lstrip(), source_fragment=str(segment.element))
            group.children.append(latest)
            # A publisher can introduce two levels together, e.g. (D)(i),
            # without a standalone (D) paragraph. Attach to the closest actual
            # ancestor rather than moving that paragraph to the end of the rule.
            while parent_ref not in groups and parent_ref.endswith(")"):
                parent_ref = re.sub(r"\([^()]+\)$", "", parent_ref)
            (groups[parent_ref].children if parent_ref in groups else root.children).append(group)
            groups[ref], clauses[ref] = group, latest
            for ancestor in (*segment.ancestors, segment.element):
                if ancestor is not body and id(ancestor) not in touched:
                    ancestor_paths[id(ancestor)] = (base, dict(current))
        else:
            # Continuation text belongs to the enclosing source paragraph when
            # present, not necessarily the deepest paragraph most recently seen.
            parent_ref = parent_base + "".join(f"({v})" for _, v in sorted(parent_path.items()))
            target = clauses.get(parent_ref, latest) if parents else latest
            # A continuation after a nested list must remain after that list
            # in the document, not move into the parent's earlier text leaf.
            if target.ref in groups and len(groups[target.ref].children) > 1:
                target = groups[target.ref]
            target.children.append(DocNode(node_type="paragraph", raw_text=text, source_fragment=str(segment.element)))
        touched.update(id(a) for a in (*segment.ancestors, segment.element))
    return [root]


def validate_tree(tree: list[DocNode], artifacts: list[Artifact], source_key: str, url: str = "") -> list[str]:
    violations: list[str] = []
    try:
        expected = normalize(" ".join(span for artifact in artifacts for span in expected_text(artifact.content)))
        rules = [_scope(artifact.content)[2] for artifact in artifacts]
        for rule in rules:
            validate_identity(rule, source_key, url)
    except ValueError as exc:
        return [str(exc)]
    nodes = [node for root in tree for node in root.walk()]
    actual = normalize(" ".join(piece for node in nodes for piece in (node.label, node.heading, node.raw_text) if piece))
    if actual != expected:
        limit = min(len(actual), len(expected))
        first = next((i for i in range(limit) if actual[i] != expected[i]), limit)
        violations.append(f"FINRA ordered source text mismatch at character {first}: expected {len(expected)} chars, parsed {len(actual)} chars")
    seen: set[str] = set()
    for node in nodes:
        if node.node_type != "provision":
            continue
        if not any(node.ref == rule or node.ref.startswith((rule + "(", rule + ".")) for rule in rules):
            violations.append(f"FINRA paragraph reference belongs to the wrong source rule: {node.ref}")
        if node.ref in seen:
            violations.append(f"FINRA duplicate paragraph reference {node.ref}")
        seen.add(node.ref)
        label = node.label.strip()
        if label.startswith("(") or label.startswith("."):
            if not node.ref.endswith(label):
                violations.append(f"FINRA paragraph reference does not match its source marker: {node.ref}")
    # Empty structural groups carry the full address independently of the leaf
    # ref, and must contain precisely their own paragraph followed by children.
    for node in nodes:
        if node.node_type == "group" and node.ref.endswith("/structure"):
            expected_ref = node.ref.removesuffix("/structure")
            if not node.children or node.children[0].node_type != "provision" or node.children[0].ref != expected_ref:
                violations.append(f"FINRA structural parent mismatch: {expected_ref}")
            for child in node.children:
                if child.node_type == "group" and child.ref.endswith("/structure") and not child.ref.startswith(expected_ref + "("):
                    violations.append(f"FINRA structural child {child.ref} has the wrong parent {expected_ref}")
    # Source numbering is checked separately from the parser's reference stack:
    # every source marker must be represented exactly once, in order, and an
    # explicit enclosing numbered DOM block must be its reference ancestor.
    source_markers: list[tuple[str, int | None]] = []
    for artifact in artifacts:
        title, body, rule = _scope(artifact.content)
        source_markers.append((f"{rule}.", None))
        first_markers: dict[int, int] = {}
        visited: set[int] = set()
        for segment in _segments(body, exclude=title):
            marker = _MARKER.match(segment.text)
            if marker and not _is_note(segment):
                label = marker.group("label")
                parent = next((first_markers[id(a)] for a in reversed(segment.ancestors) if id(a) in first_markers), None)
                index = len(source_markers)
                source_markers.append((label, parent if label.startswith("(") else None))
                for ancestor in (*segment.ancestors, segment.element):
                    if ancestor is not body and id(ancestor) not in visited:
                        first_markers[id(ancestor)] = index
            visited.update(id(a) for a in (*segment.ancestors, segment.element))
    provisions = [n for n in nodes if n.node_type == "provision"]
    if [n.label for n in provisions] != [label for label, _ in source_markers]:
        violations.append("FINRA source paragraph markers are missing, duplicated or out of order")
    else:
        observed_addresses: set[str] = set()
        active_address = ""
        for node, (label, parent_index) in zip(provisions, source_markers):
            if re.fullmatch(r"\d{4,5}[A-Z]?\.", label):
                expected_rule = label[:-1]
                if node.ref != expected_rule:
                    violations.append(f"FINRA title reference {node.ref} does not match source rule {expected_rule}")
                observed_addresses.add(expected_rule)
                active_address = expected_rule
                continue
            prefix = node.ref[:-len(label)] if label and node.ref.endswith(label) else ""
            if prefix not in observed_addresses or not (
                active_address == prefix or active_address.startswith((prefix + "(", prefix + "."))
            ):
                violations.append(f"FINRA reference {node.ref} invents or reopens an unobserved source ancestor {prefix}")
            # Combined source markers, such as (D)(i), establish the implicit D
            # ancestor without inventing a paragraph or extra source text.
            address = prefix
            markers = [f"({token})" for token in _TOKENS.findall(label)] if label.startswith("(") else [label]
            for marker in markers:
                address += marker
                observed_addresses.add(address)
            active_address = node.ref
            if parent_index is not None:
                parent_ref = provisions[parent_index].ref
                # A wrapper beginning (D)(i) owns D; its later (ii) is a
                # sibling of i, not a child of i.
                if len(_TOKENS.findall(source_markers[parent_index][0])) > 1:
                    parent_ref = re.sub(r"\([^()]+\)$", "", parent_ref)
                if not node.ref.startswith(parent_ref + "("):
                    violations.append(f"FINRA reference {node.ref} disagrees with source DOM ancestor {parent_ref}")
    return violations
