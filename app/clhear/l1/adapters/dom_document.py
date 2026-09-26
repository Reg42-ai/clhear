# Copyright (C) 2026 Reg42 AI
# This file is part of CLHEAR. See LICENSE (AGPL-3.0-only).
"""Complete legal HTML DOM records with source ids and typed containers.

EUR-Lex and GPO publications retain apparatus and notes in source order. No
table separators, section markers or missing headings are invented. The
independent parser uses Python HTMLParser; production parsing uses bs4.
"""
import re
from bs4 import BeautifulSoup, Comment, Doctype, ProcessingInstruction, NavigableString, Tag

from app.clhear.l1.adapters.base import CLAUSE_TYPES, DocNode
from app.clhear.l1.adapters.html_document import OMIT



# "§80b–3." prints an en dash inside the section number; it is one number.
USC_SECTION = r"\s*§\s*(\d+[A-Za-z0-9]*(?:\s*[-–—]\s*[A-Za-z0-9]+)*)"
# GPO head classes, outermost first. Each nests under the level before it.
_USC_LEVELS = {"subsection-head": 0, "paragraph-head": 1, "subparagraph-head": 2, "clause-head": 3, "subclause-head": 4}


def _usc_number(printed: str) -> str:
    return re.sub(r"\s*[–—-]\s*", "-", printed)


def _usc_level_ref(section: str, levels: list[str], classes: list[str], number: str) -> str:
    """sec45(b), sec80b-3(i)(1)(A): a head's reference is its enclosing heads plus its own number."""
    depth = next((d for cls, d in _USC_LEVELS.items() if cls in classes), None)
    if depth is None or depth > len(levels):
        return ""
    del levels[depth:]
    levels.append(number)
    return section + "".join(f"({n})" for n in levels)


def _kind(tag, attrs):
    identity = attrs.get("id", "")
    number = r"(?:[IVXLC]+|\d+)"
    if re.fullmatch(rf"(?:prt_{number}\.)?tis_{number}|prt_{number}", identity):
        return "part"
    if re.fullmatch(r"art_\d+[a-z]*", identity):
        return "article"
    if re.fullmatch(r"rct_\d+", identity):
        return "recital"
    if identity == "pbl_1" or re.fullmatch(r"cit_\d+", identity):
        return "preamble"
    if identity == "fnp_1":
        return "signature"
    if re.fullmatch(rf"(?:(?:prt_{number}\.)?tis_{number}\.)?cpt_{number}", identity):
        return "chapter"
    if re.fullmatch(rf"(?:(?:prt_{number}\.)?tis_{number}\.)?cpt_{number}\.sct_\d+", identity):
        return "group"
    if re.fullmatch(rf"anx_{number}", identity):
        return "schedule"
    if identity == "tit_1":
        return "title"
    if tag == "p" and any(c.startswith("oj-ti-grseq-") for c in attrs.get("class", "").split()):
        return "heading"
    if tag in {"h1", "h2", "h3"}:
        return "section"
    if tag in {"h4", "h5", "h6"}:
        # GPO editorial notes (amendments, effective dates, codification) are
        # headed by note-head / futureamend-note-head; they are not provisions.
        if any(c.endswith("note-head") for c in attrs.get("class", "").split()):
            return "note"
        return "subsection"
    return "note"


STRUCTURAL = {"title", "part", "chapter", "article", "group", "schedule", "section", "subsection"}
LABEL_CLASSES = {"article": {"oj-ti-art", "title-article-norm"}, "schedule": {"oj-doc-ti"}}
HEADING_CLASSES = {"article": {"oj-sti-art", "stitle-article-norm"}, "title": {"title-doc-first", "title-doc-last", "oj-doc-ti"}}


def _presentation_bs4(el, kind):
    """Only publisher-printed labels/titles, scoped outside child divisions."""
    def candidates(parent):
        for child in parent.children:
            if not isinstance(child, Tag):
                continue
            attrs = {k: " ".join(v) if isinstance(v, list) else v or "" for k, v in child.attrs.items()}
            if _kind(child.name, attrs) in STRUCTURAL:
                continue
            yield child
            yield from candidates(child)
    if re.fullmatch(r"h[1-6]", el.name) or kind == "heading":
        return "", " ".join(el.get_text().split())
    nodes = list(candidates(el))
    labels = LABEL_CLASSES.get(kind, {"oj-ti-section-1", "title-division-1"} if kind in {"part", "chapter", "group"} else set())
    headings = HEADING_CLASSES.get(kind, {"oj-ti-section-2", "title-division-2"} if kind in {"part", "chapter", "group"} else set())
    label = next((" ".join(n.get_text().split()) for n in nodes if labels & set(n.get("class") or [])), "")
    heading = next((" ".join(n.get_text().split()) for n in nodes if headings & set(n.get("class") or [])), "")
    if el.name == "tr":
        cells = el.find_all(["td", "th"], recursive=False)
        marker = " ".join(cells[0].get_text().split()) if len(cells) >= 2 else ""
        if re.fullmatch(r"\([A-Za-z0-9]+\)|\d+[.)]", marker):
            label = marker
    return label, heading


def _presentation_events(el, kind):
    def visible(node):
        return "".join(c if isinstance(c, str) else visible(c) for c in node["children"])
    if re.fullmatch(r"h[1-6]", el["tag"]) or kind == "heading":
        return "", " ".join(visible(el).split())
    label_classes = LABEL_CLASSES.get(kind, {"oj-ti-section-1", "title-division-1"} if kind in {"part", "chapter", "group"} else set())
    heading_classes = HEADING_CLASSES.get(kind, {"oj-ti-section-2", "title-division-2"} if kind in {"part", "chapter", "group"} else set())
    label = heading = ""
    pending = list(reversed([n for n in el["children"] if isinstance(n, dict)]))
    while pending:
        child = pending.pop()
        attrs = child.get("attrs", {})
        if _kind(child["tag"], attrs) in STRUCTURAL:
            continue
        classes = set(attrs.get("class", "").split())
        if not label and classes & label_classes:
            label = " ".join(visible(child).split())
        if not heading and classes & heading_classes:
            heading = " ".join(visible(child).split())
        pending.extend(reversed([n for n in child["children"] if isinstance(n, dict)]))
    if el["tag"] == "tr":
        cells = [n for n in el["children"] if isinstance(n, dict) and n["tag"] in {"td", "th"}]
        marker = " ".join(visible(cells[0]).split()) if len(cells) >= 2 else ""
        if re.fullmatch(r"\([A-Za-z0-9]+\)|\d+[.)]", marker):
            label = marker
    return label, heading


def parse(content, source_key, part=1):
    soup = BeautifulSoup(content, "html.parser")
    seen = set()
    usc_section = None
    usc_levels: list[str] = []

    def visit(el, path):
        nonlocal usc_section
        tag = el.name
        attrs = {key: " ".join(value) if isinstance(value, list) else (value or "") for key, value in el.attrs.items()} if isinstance(el, Tag) else {}
        kind = _kind(tag, attrs)
        label, heading = _presentation_bs4(el, kind)
        if tag == "tr" and label:
            kind = "point"
        ref = attrs.get("id", "")
        if source_key.startswith("usc/") and tag in {"h3", "h4"}:
            text = el.get_text()
            section = re.match(USC_SECTION, text)
            subsection = re.match(r"\s*\(([A-Za-z0-9]+)\)", text)
            if tag == "h3" and section:
                ref = usc_section = "sec" + _usc_number(section[1])
                usc_levels.clear()
            elif tag == "h4" and subsection and usc_section:
                ref = _usc_level_ref(usc_section, usc_levels, attrs.get("class", "").split(), subsection[1]) or ref
        if ref and ref in seen:
            ref += "@dom:" + path
        if not ref and kind in CLAUSE_TYPES:
            ref = f"{source_key}/html/{part}{path}"
        if ref:
            seen.add(ref)
        presentation_fields = [name for name, value in (("label", label), ("heading", heading)) if value]
        node = DocNode(node_type=kind, ref=ref, label=label, heading=heading,
                       source_locator={"structure": "legal-html-element", "path": path, "part": part, "attributes": attrs,
                                       "tag": tag, "presentation_fields": presentation_fields})
        counts, number = {}, 0
        for child in el.children:
            if isinstance(child, (Comment, Doctype, ProcessingInstruction)):
                continue
            if isinstance(child, Tag):
                counts[child.name] = counts.get(child.name, 0) + 1
                if child.name not in OMIT:
                    node.children.append(visit(child, path + f"/{child.name}[{counts[child.name]}]"))
            elif isinstance(child, NavigableString) and str(child).strip():
                number += 1
                node.children.append(DocNode(node_type="paragraph", raw_text=str(child),
                                            source_locator={"structure": "legal-html-text", "path": path + f"/text()[{number}]", "part": part}))
        return node

    tree = [visit(soup, "")]
    def legal_flow(node):
        original = node.children
        node.children = []
        stack = []
        for child in original:
            legal_flow(child)
            tag = child.source_locator.get("tag", "")
            level = int(tag[1]) if re.fullmatch(r"h[1-6]", tag) else None
            if level is not None:
                while stack and stack[-1][0] >= level:
                    stack.pop()
            (stack[-1][1] if stack else node).children.append(child)
            if level is not None:
                stack.append((level, child))
    for root in tree:
        legal_flow(root)
    # OJ corrigenda carry a publisher title (`tit_1`) and correction
    # paragraphs, not articles. Title/heading are structure, not clauses.
    publisher_structure = CLAUSE_TYPES | STRUCTURAL | {"heading"}
    if not any(n.node_type in publisher_structure for n in tree[0].walk()):
        raise ValueError("Legal HTML has no publisher article or heading structure")
    return tree


def original_records(content, source_key, part=1):
    from app.clhear.l1.originals import _HTMLStructure, _decode_html
    reader = _HTMLStructure()
    reader.feed(_decode_html(content))
    records, seen = [], set()
    usc_section = None
    usc_levels: list[str] = []

    def visit(el, path, parent):
        nonlocal usc_section
        tag = el["tag"]
        attrs = dict(el.get("attrs", {}))
        # Grammar mapping is declarative; the source walk and text decoder
        # are independent from the structural parser.
        kind = _kind(tag, attrs)
        label, heading = _presentation_events(el, kind)
        if tag == "tr" and label:
            kind = "point"
        ref = attrs.get("id", "")
        if source_key.startswith("usc/") and tag in {"h3", "h4"}:
            def visible(n):
                return "".join(c if isinstance(c, str) else visible(c) for c in n["children"])
            printed = visible(el).strip()
            if tag == "h3" and printed.startswith("§"):
                number = re.match(USC_SECTION, printed)
                if number:
                    usc_section = "sec" + _usc_number(number[1])
                    usc_levels.clear()
                    ref = usc_section
            elif tag == "h4" and usc_section:
                number = re.match(r"\(([A-Za-z0-9]+)\)", printed)
                if number:
                    ref = _usc_level_ref(usc_section, usc_levels, attrs.get("class", "").split(), number[1]) or ref
        if ref and ref in seen:
            ref += "@dom:" + path
        if not ref and kind in CLAUSE_TYPES:
            ref = f"{source_key}/html/{part}{path}"
        if ref:
            seen.add(ref)
        presentation_fields = [name for name, value in (("label", label), ("heading", heading)) if value]
        records.append((part, path, parent, "legal-html-element", kind, ref, label, "", attrs, heading, tag, presentation_fields))
        counters, number = {}, 0
        for child in el["children"]:
            if isinstance(child, dict):
                name = child["tag"]
                counters[name] = counters.get(name, 0) + 1
                if name not in OMIT:
                    visit(child, path + f"/{name}[{counters[name]}]", path)
            elif child.strip():
                number += 1
                records.append((part, path + f"/text()[{number}]", path, "legal-html-text", "paragraph", "", "", child, {}, "", "", []))

    visit(reader.root, "", None)
    # Heading scope is flow-based: all following siblings belong to the last
    # heading until an equal/higher heading. Compute those parents from source
    # events independently of the adapter's in-place tree transformation.
    active, parents = {}, {}
    scoped = []
    for record in records:
        part_no, path, parent, structure, kind, ref, label, text, attrs, heading, tag, presentation_fields = record
        contexts = active.setdefault(parent, [])
        match = re.fullmatch(r"h([1-6])", tag)
        if match:
            level = int(match.group(1))
            contexts[:] = [pair for pair in contexts if pair[0] < level]
        target = contexts[-1][1] if contexts else parent
        scoped.append((part_no, path, target, structure, kind, ref, label, text, attrs, heading, tag, presentation_fields))
        if match:
            contexts.append((level, path))
    return scoped


def verify(artifacts, source_key, tree):
    expected = [row for part, artifact in enumerate(artifacts, 1) for row in original_records(artifact.content, source_key, part)]
    actual = []

    def visit(node, parent=None):
        loc = node.source_locator
        actual.append((loc.get("part"), loc.get("path"), parent, loc.get("structure"), node.node_type, node.ref, node.label,
                       node.raw_text, loc.get("attributes", {}), node.heading, loc.get("tag", ""), loc.get("presentation_fields", [])))
        return all(visit(child, loc.get("path")) for child in node.children)

    return all(visit(root) for root in tree) and expected == actual
