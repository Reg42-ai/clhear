# Copyright (C) 2026 Reg42 AI
# This file is part of CLHEAR. See LICENSE (AGPL-3.0-only).
"""Lossless CLML/eCFR source tree with independent DOM verification.

Publisher text nodes stay byte-decoded verbatim. Labels are XML tag metadata,
not a reconstruction of punctuation that a separate publisher renderer adds.
The raw artifact retains declarations and out-of-scope administration.
"""
import hashlib
import json
import xml.etree.ElementTree as ET
from xml.dom import Node, minidom

from app.clhear.l1.adapters.base import CLAUSE_TYPES, DocNode

CLML_BODY = {"SecondaryPrelims", "PrimaryPrelims", "EUPrelims", "EUPreamble", "Body", "EUBody", "Schedules", "Attachments", "SignedSection"}
KINDS = {"Part": "part", "Chapter": "chapter", "P1group": "group", "P1": "provision", "P2": "subsection",
         "P3": "paragraph", "P4": "subparagraph", "P5": "point", "Schedule": "schedule", "SignedSection": "signature",
         "Title": "heading", "Number": "heading", "Pnumber": "heading", "DIV8": "section", "P": "paragraph", "HD": "heading"}


def _local(tag):
    return tag.rsplit("}", 1)[-1]


def _check(content):
    if b"<!DOCTYPE" in content.upper() or b"<!ENTITY" in content.upper():
        raise ValueError("External entities/DTDs are unsupported in source XML")


def _identity(source_key, tag, attrs, path):
    if attrs.get("id"):
        return attrs["id"]
    if tag == "DIV8" and attrs.get("N"):
        return attrs["N"]
    return source_key + "/xml" + path if KINDS.get(tag) in CLAUSE_TYPES else ""


def parse(content, source_key, adapter_key, part=1):
    _check(content)
    root = ET.fromstring(content)
    if adapter_key == "uk_legislation":
        document = next((n for n in root.iter() if _local(n.tag) in {"Secondary", "Primary", "EURetained"}), None)
        if document is None:
            raise ValueError("Missing CLML document body")
        scopes = [n for n in document if _local(n.tag) in CLML_BODY]
    else:
        scopes = [n for n in root.iter() if _local(n.tag) == "DIV8"]
    if not scopes:
        raise ValueError("No in-scope publisher XML document")
    seen = set()

    def visit(el, path):
        tag, attrs = _local(el.tag), dict(el.attrib)
        ref = _identity(source_key, tag, attrs, path)
        if ref:
            if ref in seen:
                # CLML amendment alternatives can repeat an id. Preserve every
                # occurrence and its original id attribute; the address suffix
                # explicitly disambiguates the XML occurrence, not legal force.
                ref = ref + "@xml:" + path
            seen.add(ref)
        display_heading = next(("".join(child.itertext()).strip() for child in el if _local(child.tag) == "Title"), "")
        display_label = next(("".join(child.itertext()).strip() for child in el if _local(child.tag) in {"Pnumber", "Number"}), "")
        node = DocNode(node_type=KINDS.get(tag, "note"), ref=ref, label=display_label,
                       source_fragment=ET.tostring(el, encoding="unicode") if KINDS.get(tag) in CLAUSE_TYPES else "",
                       source_locator={"structure": "xml-element", "path": path, "part": part, "attributes": attrs,
                                       "tag": tag, "display_heading": display_heading})
        text_number = 0

        def text(value):
            nonlocal text_number
            if value and value.strip():
                text_number += 1
                node.children.append(DocNode(node_type="paragraph", raw_text=value,
                                            source_locator={"structure": "xml-text", "path": path + f"/text()[{text_number}]", "part": part}))

        text(el.text)
        siblings = {}
        for child in el:
            name = _local(child.tag)
            siblings[name] = siblings.get(name, 0) + 1
            node.children.append(visit(child, path + f"/{name}[{siblings[name]}]"))
            text(child.tail)
        return node

    siblings, result = {}, []
    for scope in scopes:
        name = _local(scope.tag)
        siblings[name] = siblings.get(name, 0) + 1
        result.append(visit(scope, f"/{name}[{siblings[name]}]"))
    return result


def _fragment_shape(nodes):
    """Namespace-aware equivalence, independent of ElementTree serialization."""
    result, text = [], []
    def flush():
        value = " ".join("".join(text).split())
        text.clear()
        if value:
            result.append(("text", value))
    for node in nodes:
        if node.nodeType in {Node.TEXT_NODE, Node.CDATA_SECTION_NODE}:
            text.append(node.data)
        elif node.nodeType == Node.ELEMENT_NODE:
            flush()
            attrs = []
            for i in range(node.attributes.length):
                attr = node.attributes.item(i)
                if attr.namespaceURI != "http://www.w3.org/2000/xmlns/":
                    attrs.append((attr.namespaceURI or "", attr.localName, attr.value))
            result.append((node.namespaceURI or "", node.localName, sorted(attrs), _fragment_shape(node.childNodes)))
    flush()
    return result


def _fragment_hash(nodes):
    return hashlib.sha256(json.dumps(_fragment_shape(nodes), ensure_ascii=False).encode()).hexdigest()


def _stored_fragment_hash(value):
    if not value:
        return ""
    _check(value.encode())
    document = minidom.parseString("<fragment>" + value + "</fragment>")
    return _fragment_hash(document.documentElement.childNodes)


def original_records(content, source_key, adapter_key, part=1):
    """Independent minidom reader; never calls parse or ElementTree."""
    _check(content)
    doc = minidom.parseString(content)
    elements = list(doc.getElementsByTagName("*"))
    if adapter_key == "uk_legislation":
        document = next((n for n in elements if n.localName in {"Secondary", "Primary", "EURetained"}), None)
        if document is None:
            raise ValueError("No independent CLML document scope")
        scopes = [n for n in document.childNodes if n.nodeType == Node.ELEMENT_NODE and n.localName in CLML_BODY]
    else:
        scopes = [n for n in elements if n.localName == "DIV8"]
    if not scopes:
        raise ValueError("No independent XML document scope")
    rows, seen = [], set()

    def visit(el, path, parent):
        tag = el.localName
        attrs = {}
        for i in range(el.attributes.length):
            attr = el.attributes.item(i)
            if attr.namespaceURI == "http://www.w3.org/2000/xmlns/":
                continue
            key = ("{" + attr.namespaceURI + "}" if attr.namespaceURI else "") + attr.localName
            attrs[key] = attr.value
        kind = KINDS.get(tag, "note")
        ref = attrs.get("id", "")
        if not ref and tag == "DIV8" and attrs.get("N"):
            ref = attrs["N"]
        if not ref and kind in CLAUSE_TYPES:
            ref = source_key + "/xml" + path
        if ref and ref in seen:
            ref = ref + "@xml:" + path
        if ref:
            seen.add(ref)
        label, display_heading = "", ""
        def value(n):
            return "".join(c.data if c.nodeType in {Node.TEXT_NODE, Node.CDATA_SECTION_NODE} else value(c)
                           for c in n.childNodes if c.nodeType in {Node.ELEMENT_NODE, Node.TEXT_NODE, Node.CDATA_SECTION_NODE})
        for child in el.childNodes:
            if child.nodeType == Node.ELEMENT_NODE:
                if child.localName in {"Pnumber", "Number"} and not label:
                    label = value(child).strip()
                if child.localName == "Title" and not display_heading:
                    display_heading = value(child).strip()
        fragment_nodes = [el]
        tail = el.nextSibling
        while tail is not None and tail.nodeType != Node.ELEMENT_NODE:
            fragment_nodes.append(tail)
            tail = tail.nextSibling
        fragment_hash = _fragment_hash(fragment_nodes) if kind in CLAUSE_TYPES else ""
        rows.append((part, path, parent, "xml-element", kind, ref, label, "", attrs, display_heading, fragment_hash))
        counts, text_number = {}, 0
        for child in el.childNodes:
            if child.nodeType == Node.ELEMENT_NODE:
                counts[child.localName] = counts.get(child.localName, 0) + 1
                visit(child, path + f"/{child.localName}[{counts[child.localName]}]", path)
            elif child.nodeType in {Node.TEXT_NODE, Node.CDATA_SECTION_NODE} and child.data.strip():
                text_number += 1
                rows.append((part, path + f"/text()[{text_number}]", path, "xml-text", "paragraph", "", "", child.data, {}, "", ""))

    counts = {}
    for scope in scopes:
        counts[scope.localName] = counts.get(scope.localName, 0) + 1
        visit(scope, f"/{scope.localName}[{counts[scope.localName]}]", None)
    return rows


def verify(artifacts, source_key, adapter_key, tree):
    expected = [row for part, artifact in enumerate(artifacts, 1) for row in original_records(artifact.content, source_key, adapter_key, part)]
    actual = []

    def visit(node, parent=None):
        loc = node.source_locator
        if node.heading:
            return False
        actual.append((loc.get("part"), loc.get("path"), parent, loc.get("structure"), node.node_type, node.ref,
                       node.label, node.raw_text, loc.get("attributes", {}), loc.get("display_heading", ""), _stored_fragment_hash(node.source_fragment)))
        return all(visit(child, loc.get("path")) for child in node.children)

    return all(visit(root) for root in tree) and actual == expected
