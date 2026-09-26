# Copyright (C) 2026 Reg42 AI
# This file is part of CLHEAR. See LICENSE (AGPL-3.0-only).
"""Lossless NIST structured records; JSON Pointer is a source address.

Every scalar, parameter, guidance part, relation and administrative field is
retained. Field names are metadata, never fabricated prose. The independent
oracle below uses object-pair events, not this parser's decoded dictionaries.
"""
import json

from app.clhear.l1.adapters.base import DocNode


def _escape(value):
    return str(value).replace("~", "~0").replace("/", "~1")


def _scalar(value):
    return value if isinstance(value, str) else json.dumps(value, ensure_ascii=False, separators=(",", ":"))


def parse(content, source_key):
    value = json.loads(content)
    if source_key == "nist/sp800-53r5":
        if "catalog" not in value or not value["catalog"].get("groups"):
            raise ValueError("Expected a nonempty OSCAL catalog")
    elif source_key == "nist/csf-2.0":
        if not value.get("response", {}).get("elements", {}).get("elements"):
            raise ValueError("Expected a nonempty CPRT framework export")
    else:
        raise ValueError("Unsupported JSON source schema")
    seen = set()

    def visit(item, path, field, ancestors):
        kind, ref = "note", ""
        if isinstance(item, dict):
            if field == "groups":
                kind, ref = "group", item.get("id", "")
            elif field == "controls":
                kind = "enhancement" if "controls" in ancestors else "control"
                ref = item.get("id", "")
            elif item.get("element_type") in {"function", "category", "subcategory"}:
                kind = {"function": "part", "category": "group", "subcategory": "provision"}[item["element_type"]]
                ref = item.get("element_identifier", "")
            elif field == "parts" and item.get("name") in {"statement", "item"}:
                kind, ref = "statement", item.get("id", "")
            if ref:
                if ref in seen:
                    raise ValueError("Duplicate publisher JSON record identifier")
                seen.add(ref)
        if kind in {"control", "enhancement", "statement", "provision"} and not ref:
            raise ValueError("Addressable JSON record has no publisher identifier")
        node = DocNode(node_type=kind, ref=ref, label=field,
                       source_locator={"structure": "json-value", "pointer": path,
                                       "value_type": "object" if isinstance(item, dict) else "array" if isinstance(item, list) else "scalar"})
        if isinstance(item, dict):
            node.children = [visit(v, path + "/" + _escape(k), k, ancestors) for k, v in item.items()]
        elif isinstance(item, list):
            node.children = [visit(v, path + "/" + str(i), field, ancestors + ([field] if field == "controls" else []))
                             for i, v in enumerate(item)]
            # The list itself does not imply a nested control: object children
            # of the outer controls list are the primary control level.
            if field == "controls" and "controls" not in ancestors:
                for child in node.children:
                    if child.node_type == "enhancement":
                        child.node_type = "control"
        else:
            node.raw_text = _scalar(item)
        return node

    return [visit(value, "", "", [])]


class _Pairs(list):
    pass


def original_records(content, source_key):
    """Independent object-pair event traversal, including duplicate-key check."""
    value = json.loads(content, object_pairs_hook=_Pairs)
    rows, pending = [], [(value, "", None, "", 0)]
    seen = set()
    while pending:
        item, path, parent, field, control_depth = pending.pop()
        kind, ref, text = "note", "", ""
        if isinstance(item, _Pairs):
            keys = [key for key, _ in item]
            if len(set(keys)) != len(keys):
                raise ValueError("Duplicate original JSON object key")
            attrs = {key: v for key, v in item if not isinstance(v, (list, _Pairs))}
            if field == "groups":
                kind, ref = "group", attrs.get("id", "")
            elif field == "controls":
                kind, ref = ("control" if control_depth == 1 else "enhancement"), attrs.get("id", "")
            elif attrs.get("element_type") in {"function", "category", "subcategory"}:
                kind = {"function": "part", "category": "group", "subcategory": "provision"}[attrs["element_type"]]
                ref = attrs.get("element_identifier", "")
            elif field == "parts" and attrs.get("name") in {"statement", "item"}:
                kind, ref = "statement", attrs.get("id", "")
            for k, v in reversed(item):
                address = str(k).replace("~", "~0").replace("/", "~1")
                pending.append((v, path + "/" + address, path, k, control_depth))
            value_type = "object"
        elif isinstance(item, list):
            next_depth = control_depth + (1 if field == "controls" else 0)
            pending.extend((v, path + "/" + str(i), path, field, next_depth) for i, v in reversed(list(enumerate(item))))
            value_type = "array"
        else:
            text = item if isinstance(item, str) else json.dumps(item, ensure_ascii=False, separators=(",", ":"))
            value_type = "scalar"
        if ref:
            if ref in seen:
                raise ValueError("Duplicate publisher JSON record identifier")
            seen.add(ref)
        if kind in {"control", "enhancement", "statement", "provision"} and not ref:
            raise ValueError("Missing publisher JSON record identifier")
        rows.append((path, parent, field, kind, ref, value_type, text))
    if not any(row[4] for row in rows):
        raise ValueError("No publisher-identified JSON records")
    return rows


def verify(content, source_key, tree):
    observed = []

    def visit(node, parent=None):
        loc = node.source_locator
        observed.append((loc.get("pointer"), parent, node.label, node.node_type, node.ref, loc.get("value_type"), node.raw_text))
        if node.heading or loc.get("structure") != "json-value":
            return False
        return all(visit(child, loc.get("pointer")) for child in node.children)

    return all(visit(root) for root in tree) and observed == original_records(content, source_key)
