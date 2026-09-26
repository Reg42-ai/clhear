# Copyright (C) 2026 Reg42 AI
# This file is part of CLHEAR. See LICENSE (AGPL-3.0-only).
"""Lossless list-record projection and independent DOM/CSV reconciliation.

Field names remain metadata; raw_text contains only the published value.
No alias cap, synthesized prose, row truncation, or unrecognized XML fallback.
"""
import csv
import hashlib
import io
import xml.etree.ElementTree as ET
from xml.dom import minidom

from app.clhear.l1.adapters.base import DocNode

RECORD_TAGS = {"lists/ofac-sdn": "sdnEntry", "lists/un-consolidated": {"INDIVIDUAL", "ENTITY"},
               "lists/eu-consolidated": {"sanctionEntity", "entity"}}


def local(tag):
    return tag.rsplit("}", 1)[-1].rsplit(":", 1)[-1]


def _key(fields, source_key):
    candidates = {"lists/ofac-sdn": {"uid"}, "lists/un-consolidated": {"DATAID"},
                  "lists/eu-consolidated": {"@logicalId"}, "lists/uk-ofsi": {"Unique ID", "Group ID"}}.get(source_key, set())
    def root_identity(field):
        if field["name"] not in candidates or not field["value"].strip():
            return False
        if source_key == "lists/uk-ofsi":
            return field["path"].startswith("/columns/")
        if field["name"].startswith("@"):
            return field["path"] == "/" + field["name"]
        return field["path"] == f"/{field['name']}[1]/text()[1]"
    identifiers = [f["value"] for f in fields if root_identity(f)]
    if len(identifiers) != 1:
        raise ValueError("List record lacks one unambiguous root publisher identity")
    value = identifiers[0]
    if source_key == "lists/uk-ofsi":
        # An OFSI group can have multiple alias rows. Exact row content is an
        # explicit secondary key, not an arrival-order index.
        value += "-" + hashlib.sha256(repr([(f["path"], f["value"]) for f in fields]).encode()).hexdigest()[:16]
    return value


def _csv_fields(header, row):
    if len(row) != len(header):
        raise ValueError("CSV record width differs from its header")
    return [{"name": name, "path": f"/columns/{i}", "value": value} for i, (name, value) in enumerate(zip(header, row))]


def original_records(content, name, source_key):
    """Oracle uses minidom; the adapter uses ElementTree independently."""
    records = []
    if source_key == "lists/uk-ofsi" or name.endswith(".csv"):
        reader = csv.reader(io.StringIO(content.decode("utf-8-sig"), newline=""))
        header = next(reader, None)
        if not header:
            raise ValueError("CSV header is missing")
        for row in reader:
            fields = _csv_fields(header, row)
            records.append({"key": _key(fields, source_key), "fields": fields})
    else:
        if source_key not in RECORD_TAGS:
            raise ValueError("Unrecognized list XML schema; declare record and identity fields")
        if b"<!DOCTYPE" in content.upper() or b"<!ENTITY" in content.upper():
            raise ValueError("External/custom XML entities are not accepted")
        document = minidom.parseString(content)
        tags = RECORD_TAGS[source_key]
        tags = {tags} if isinstance(tags, str) else tags

        def walk(node, path, fields):
            for key in sorted(node.attributes.keys()):
                fields.append({"name": "@" + local(key), "path": path + "/@" + key, "value": node.getAttribute(key)})
            counters, text_index = {}, 0
            for child in node.childNodes:
                if child.nodeType == child.ELEMENT_NODE:
                    counters[child.tagName] = counters.get(child.tagName, 0) + 1
                    walk(child, path + f"/{local(child.tagName)}[{counters[child.tagName]}]", fields)
                elif child.nodeType in {child.TEXT_NODE, child.CDATA_SECTION_NODE} and child.data.strip():
                    text_index += 1
                    fields.append({"name": local(node.tagName), "path": path + f"/text()[{text_index}]", "value": child.data})

        for node in document.getElementsByTagName("*"):
            if local(node.tagName) in tags:
                fields = []
                walk(node, "", fields)
                records.append({"key": _key(fields, source_key), "fields": fields})
    if not records or len({r["key"] for r in records}) != len(records):
        raise ValueError("List has no records or duplicate publisher record identities")
    return records


def parse_records(content, name, source_key, title):
    parsed = []
    if source_key == "lists/uk-ofsi" or name.endswith(".csv"):
        reader = csv.reader(io.StringIO(content.decode("utf-8-sig"), newline=""))
        header = next(reader, None)
        if not header:
            raise ValueError("CSV header is missing")
        parsed = [_csv_fields(header, row) for row in reader]
    else:
        tags = RECORD_TAGS.get(source_key)
        if not tags:
            raise ValueError("Unrecognized list schema")
        tags = {tags} if isinstance(tags, str) else tags
        root = ET.fromstring(content)

        def walk(node, path, fields):
            for key, value in sorted(node.attrib.items()):
                fields.append({"name": "@" + local(key), "path": path + "/@" + key, "value": value})
            text_index = 0
            if node.text and node.text.strip():
                text_index = 1
                fields.append({"name": local(node.tag), "path": path + "/text()[1]", "value": node.text})
            counters = {}
            for child in node:
                tag = local(child.tag)
                counters[tag] = counters.get(tag, 0) + 1
                walk(child, path + f"/{tag}[{counters[tag]}]", fields)
                if child.tail and child.tail.strip():
                    text_index += 1
                    fields.append({"name": local(node.tag), "path": path + f"/text()[{text_index}]", "value": child.tail})

        for node in root.iter():
            if local(node.tag) in tags:
                fields = []
                walk(node, "", fields)
                parsed.append(fields)
    tree = DocNode(node_type="title", ref=source_key, heading=title,
                   source_locator={"structure": "list-record-root"})
    seen = set()
    for fields in parsed:
        key = _key(fields, source_key)
        if key in seen:
            raise ValueError("Duplicate publisher list record identity")
        seen.add(key)
        record = DocNode(node_type="section", ref=f"{source_key}/{key}", label=key,
                         source_locator={"structure": "list-record", "record_key": key})
        for field in fields:
            record.children.append(DocNode(node_type="point", label=field["name"], raw_text=field["value"],
                                          source_locator={"structure": "list-field", "record_key": key, "field_path": field["path"]}))
        tree.children.append(record)
    if not tree.children:
        raise ValueError("No publisher list records")
    return [tree]


def verify_records(artifacts, source_key, tree):
    expected = [r for a in artifacts for r in original_records(a.content, a.name, source_key)]
    if len(tree) != 1 or tree[0].ref != source_key:
        return False
    observed = []
    for record in tree[0].children:
        key = record.source_locator.get("record_key")
        if record.node_type != "section" or record.ref != f"{source_key}/{key}":
            return False
        fields = []
        for node in record.children:
            if node.children or node.node_type != "point" or node.source_locator.get("record_key") != key:
                return False
            fields.append({"name": node.label, "path": node.source_locator.get("field_path"), "value": node.raw_text})
        observed.append({"key": key, "fields": fields})
    return expected == observed
