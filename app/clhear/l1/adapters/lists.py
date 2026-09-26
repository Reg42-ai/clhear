# Copyright (C) 2026 Reg42 AI
# This file is part of CLHEAR. See LICENSE (AGPL-3.0-only).
"""Sanctions / screening list feeds (Class E).

OFAC SDN XML, UN SC consolidated XML, EU consolidated XML, UK OFSI CSV.
Each list row becomes a `section` (clause grain) with `point` children so
Explorer and diffs use the same pipeline — no second grain.
"""
import csv
import io
import xml.etree.ElementTree as ET
from datetime import date
import hashlib

from app.clhear.l1 import http
from app.clhear.l1.adapters.base import Artifact, DocNode, FetchResult, SourceMeta

FEEDS = {
    "lists/ofac-sdn": {
        "url": "https://www.treasury.gov/ofac/downloads/sdn.xml",
        "name": "sdn.xml",
        "ctype": "application/xml",
        "kind": "ofac",
    },
    "lists/un-consolidated": {
        "url": "https://scsanctions.un.org/resources/xml/en/consolidated.xml",
        "name": "un-consolidated.xml",
        "ctype": "application/xml",
        "kind": "un",
    },
    "lists/eu-consolidated": {
        "url": "https://webgate.ec.europa.eu/fsd/fsf/public/files/xmlFullSanctionsList_1_1/content?token=dG9rZW4tMjAxNw",
        "name": "eu-consolidated.xml",
        "ctype": "application/xml",
        "kind": "eu",
    },
    "lists/uk-ofsi": {
        "url": "https://ofsistorage.blob.core.windows.net/publishlive/2022format/ConList.csv",
        "name": "ofsi.csv",
        "ctype": "text/csv",
        "kind": "ofsi",
    },
}


def _local(tag: str) -> str:
    return tag.split("}")[-1] if tag else tag


def _txt(el: ET.Element | None) -> str:
    if el is None:
        return ""
    return " ".join(piece.strip() for piece in el.itertext() if piece.strip())


def _parse_ofac(root: ET.Element, source_key: str) -> list[DocNode]:
    title = DocNode(node_type="title", ref=source_key, heading="OFAC SDN")
    for i, sdn in enumerate(root.iter(), start=1):
        if _local(sdn.tag) != "sdnEntry":
            continue
        uid = _txt(next((c for c in sdn if _local(c.tag) == "uid"), None)) or str(i)
        first = _txt(next((c for c in sdn if _local(c.tag) == "firstName"), None))
        last = _txt(next((c for c in sdn if _local(c.tag) == "lastName"), None))
        sdn_type = _txt(next((c for c in sdn if _local(c.tag) == "sdnType"), None))
        name = " ".join(p for p in (first, last) if p) or last or f"entry {uid}"
        entry = DocNode(
            node_type="section",
            ref=f"{source_key}/{uid}",
            heading=name,
            label=uid,
        )
        if sdn_type:
            entry.children.append(DocNode(node_type="point", raw_text=f"type {sdn_type}"))
        for child in sdn:
            text = _txt(child)
            if text and _local(child.tag) not in {"uid", "firstName", "lastName", "sdnType"}:
                entry.children.append(DocNode(node_type="point", raw_text=f"{_local(child.tag)}: {text}"))
        title.children.append(entry)
    return [title]


def _parse_un(root: ET.Element, source_key: str) -> list[DocNode]:
    title = DocNode(node_type="title", ref=source_key, heading="UN Security Council Consolidated List")
    i = 0
    for el in root.iter():
        if _local(el.tag) not in {"INDIVIDUAL", "ENTITY"}:
            continue
        i += 1
        dataid = ""
        name = ""
        for child in el:
            loc = _local(child.tag)
            if loc == "DATAID":
                dataid = _txt(child)
            if loc in {"FIRST_NAME", "SECOND_NAME", "THIRD_NAME", "FOURTH_NAME", "NAME_ORIGINAL_SCRIPT"}:
                name = " ".join(p for p in (name, _txt(child)) if p)
        ref = dataid or str(i)
        entry = DocNode(node_type="section", ref=f"{source_key}/{ref}", heading=name or f"entry {ref}", label=ref)
        for child in el:
            text = _txt(child)
            if text:
                entry.children.append(DocNode(node_type="point", raw_text=f"{_local(child.tag)}: {text}"))
        title.children.append(entry)
    return [title]


def _parse_eu(root: ET.Element, source_key: str) -> list[DocNode]:
    title = DocNode(node_type="title", ref=source_key, heading="EU consolidated financial sanctions list")
    i = 0
    for el in root.iter():
        if _local(el.tag) not in {"sanctionEntity", "entity"}:
            continue
        i += 1
        logical = el.get("logicalId") or str(i)
        names = [_txt(n) for n in el.iter() if _local(n.tag) in {"wholeName", "nameAlias"} and _txt(n)]
        heading = names[0] if names else f"entity {logical}"
        entry = DocNode(node_type="section", ref=f"{source_key}/{logical}", heading=heading, label=logical)
        for name in names[:8]:
            entry.children.append(DocNode(node_type="point", raw_text=name))
        title.children.append(entry)
    if not title.children:
        # Fallback: one section per distinctive text-bearing element.
        for el in root.iter():
            text = _txt(el)
            if len(text) > 20 and _local(el.tag) in {"nameAlias", "regulation", "remark", "wholeName"}:
                i += 1
                title.children.append(
                    DocNode(node_type="section", ref=f"{source_key}/{i}", heading=text[:180], children=[
                        DocNode(node_type="point", raw_text=text)
                    ])
                )
    return [title]


def _parse_ofsi(content: bytes, source_key: str) -> list[DocNode]:
    title = DocNode(node_type="title", ref=source_key, heading="UK OFSI consolidated list")
    text = content.decode("utf-8-sig", errors="replace")
    reader = csv.DictReader(io.StringIO(text))
    for i, row in enumerate(reader, start=1):
        name = (
            row.get("Name 6")
            or row.get("Name")
            or row.get("Name 1")
            or " ".join(str(v) for v in list(row.values())[:3] if v)
            or f"row {i}"
        )
        group_id = row.get("Group ID") or row.get("Unique ID") or str(i)
        entry = DocNode(node_type="section", ref=f"{source_key}/{group_id}-{i}", heading=name.strip(), label=str(group_id))
        for key, value in row.items():
            if value and str(value).strip():
                entry.children.append(DocNode(node_type="point", raw_text=f"{key}: {value}"))
        title.children.append(entry)
    return [title]


def _parse_xml_generic(root: ET.Element, source_key: str, heading: str) -> list[DocNode]:
    title = DocNode(node_type="title", ref=source_key, heading=heading)
    for i, el in enumerate(root.iter(), start=1):
        text = _txt(el)
        if len(text) < 8:
            continue
        if list(el) and _local(el.tag) in {root.tag.split("}")[-1]}:
            continue
        title.children.append(
            DocNode(
                node_type="section",
                ref=f"{source_key}/{i}",
                heading=text[:180],
                children=[DocNode(node_type="point", raw_text=text)],
            )
        )
        if len(title.children) >= 5000:
            break
    return [title]


class ListsAdapter:
    key = "lists"

    def __init__(self, source_key: str, title: str, url: str | None = None, meta: SourceMeta | None = None):
        self._source_key = source_key
        feed = FEEDS.get(source_key, {})
        self._url = url or feed.get("url") or ""
        self._title = title
        self._meta = meta
        self._kind = feed.get("kind", "xml")
        self._name = feed.get("name", "list.bin")
        self._ctype = feed.get("ctype", "application/octet-stream")

    def meta(self) -> SourceMeta:
        if self._meta is not None:
            return self._meta
        return SourceMeta(
            family_key="sanctions-lists",
            family_name="Global sanctions & screening lists",
            source_key=self._source_key,
            name=self._title,
            kind="guidance",
            issuer="",
            jurisdiction="",
            license="open",
            canonical_url=self._url,
            adapter=self.key,
            short_name=self._title,
            version_policy="consolidated",
        )

    def fetch(self, since_version: str | None = None) -> FetchResult | None:
        content = http.get(self._url, timeout=120.0)
        artifact = Artifact(name=self._name, content=content, content_type=self._ctype)
        from app.clhear.l1.adapters.list_records import parse_records
        tree = parse_records(content, self._name, self._source_key, self._title)
        return FetchResult(
            version_label=f"consolidated:acquired-sha256-{hashlib.sha256(content).hexdigest()}",
            artifacts=[artifact],
            tree=tree,
            version_kind="consolidated",
            as_of_date=None,
        )

    def expected_text(self, artifacts: list[Artifact]) -> list[str]:
        from app.clhear.l1.adapters.list_records import original_records
        return [field["value"] for artifact in artifacts
                for record in original_records(artifact.content, artifact.name, self._source_key)
                for field in record["fields"] if field["value"].strip()]
