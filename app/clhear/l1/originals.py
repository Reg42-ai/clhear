# Copyright (C) 2026 Reg42 AI
# This file is part of CLHEAR. See LICENSE (AGPL-3.0-only).
"""Independent artifact-to-record verification, with no network or database I/O.

The reader uses archived bytes, not adapter.expected_text or a re-run of the
structural parser. Unknown source layouts stay explicitly uncertified. The
complete clause projection is checked separately from original-text fidelity.
"""
from __future__ import annotations

import hashlib
import io
import json
import re
from html.parser import HTMLParser

from app.clhear.l1.adapters.base import CLAUSE_TYPES, DocNode
from app.clhear.l1.spans import canonical_text, span_layout

NORMALIZATION_VERSION = "unicode-whitespace-v1"
OFFSET_UNIT = "unicode_code_points"


def digest(value):
    return hashlib.sha256(value).hexdigest()


def normalize(value):
    """Only collapse Unicode whitespace; preserve case, punctuation and words."""
    return " ".join(value.split())


class UnsupportedOriginal(ValueError):
    pass


def _sec_page(content) -> bool:
    from app.clhear.l1.adapters.sec_pages import is_sec_page
    return not content.startswith(b"%PDF-") and is_sec_page(content)


def _publication_page(adapter_key, content) -> bool:
    """SEC pages on the SEC lanes; FTC guidance pages on the US government lane."""
    from app.clhear.l1.adapters.ftc_pages import is_ftc_page
    if adapter_key in {"sec_edgar", "sec_enforcement"}:
        return _sec_page(content)
    return adapter_key == "govinfo_us" and not content.startswith(b"%PDF-") and is_ftc_page(content)


class _HTMLText(HTMLParser):
    """Independent stdlib text reader, distinct from BeautifulSoup adapters."""
    BLOCKS = {"p", "div", "li", "ul", "ol", "table", "tr", "td", "th", "section", "article", "blockquote", "pre", "h1", "h2", "h3", "h4", "h5", "h6", "br"}
    OMIT = {"head", "script", "style", "noscript", "nav", "header", "footer", "aside", "form", "button", "svg", "iframe", "template"}
    VOID = {"area", "base", "br", "col", "embed", "hr", "img", "input", "link", "meta", "param", "source", "track", "wbr"}

    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.stack, self.parts = [], []

    def handle_starttag(self, tag, attrs):
        if tag in self.BLOCKS:
            self.parts.append("\n")
        if tag not in self.VOID:
            self.stack.append(tag)

    def handle_endtag(self, tag):
        if tag in self.BLOCKS:
            self.parts.append("\n")
        if tag in self.stack:
            self.stack = self.stack[:len(self.stack) - 1 - self.stack[::-1].index(tag)]

    def handle_data(self, data):
        if not any(tag in self.OMIT for tag in self.stack):
            self.parts.append(data)


class _HTMLStructure(HTMLParser):
    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.root = {"tag": "[document]", "children": []}
        self.stack = [self.root]

    def handle_starttag(self, tag, attrs):
        node = {"tag": tag, "attrs": {k: v or "" for k, v in attrs}, "children": []}
        self.stack[-1]["children"].append(node)
        if tag not in _HTMLText.VOID:
            self.stack.append(node)

    def handle_endtag(self, tag):
        for i in range(len(self.stack) - 1, 0, -1):
            if self.stack[i]["tag"] == tag:
                del self.stack[i:]
                break

    def handle_data(self, data):
        self.stack[-1]["children"].append(data)


def _html_fragment_shape(node):
    if isinstance(node, str):
        return ("text", normalize(node))
    children = [_html_fragment_shape(child) for child in node["children"]]
    children = [child for child in children if child != ("text", "")]
    return (node["tag"], sorted((key, normalize(value)) for key, value in node.get("attrs", {}).items()), children)


def _html_fragment_digest(node):
    return digest(json.dumps(_html_fragment_shape(node), ensure_ascii=False).encode())


def verify_html_fragments(source_key, artifacts, tree):
    """Bind retained HTML snippets to independently decoded source elements.

    Block locators name their exact publisher element, and the node's wording
    must appear in that element.
    """
    nodes = [n for r in tree for n in r.walk() if n.source_fragment]
    if not nodes:
        return True
    indexes = {}
    for part, artifact in enumerate(artifacts, 1):
        reader = _HTMLStructure()
        reader.feed(_decode_html(artifact.content))
        by_path, signatures = {}, set()
        def visit(node, path):
            signature = _html_fragment_digest(node)
            by_path[path] = signature
            signatures.add(signature)
            counts = {}
            for child in node["children"]:
                if isinstance(child, dict):
                    tag = child["tag"]
                    counts[tag] = counts.get(tag, 0) + 1
                    visit(child, path + f"/{tag}[{counts[tag]}]")
        visit(reader.root, "")
        indexes[part] = (by_path, signatures)
    for node in nodes:
        reader = _HTMLStructure()
        reader.feed(node.source_fragment)
        children = [n for n in reader.root["children"] if not isinstance(n, str) or n.strip()]
        fragment = children[0] if len(children) == 1 and isinstance(children[0], dict) else reader.root
        signature = _html_fragment_digest(fragment)
        loc = node.source_locator
        index = indexes.get(loc.get("part", 1))
        if not index:
            return False
        if loc.get("structure") == "html-block":
            path = re.sub(r"/text-block\(\)\[\d+\]$", "", loc.get("path", ""))
            if index[0].get(path) != signature:
                return False
        else:
            return False
        wording = normalize(" ".join(v for v in (node.label, node.heading, node.raw_text) if v))
        if wording and wording not in html_text(node.source_fragment.encode()):
            return False
    return True


def html_blocks(content):
    """Independent source block/path enumeration from stdlib parser events."""
    reader = _HTMLStructure()
    match = re.search(br"charset\s*=\s*['\"]?([A-Za-z0-9._-]+)", content[:4096], re.I)
    reader.feed(content.decode(match.group(1).decode("ascii") if match else "utf-8-sig"))
    result = []
    block_tags = _HTMLText.BLOCKS | {"tbody", "thead", "tfoot"}

    def boundary(node):
        return node["tag"] in block_tags | _HTMLText.OMIT or any(boundary(n) for n in node["children"] if isinstance(n, dict))

    def all_text(node):
        return "".join(n if isinstance(n, str) else all_text(n) for n in node["children"])

    def visit(node, path):
        text, number, siblings = [], 0, {}

        def emit():
            nonlocal number
            value = normalize("".join(text))
            text.clear()
            if value:
                number += 1
                result.append({"path": path + f"/text-block()[{number}]", "tag": node["tag"], "text": value})

        for child in node["children"]:
            if isinstance(child, str):
                text.append(child)
                continue
            tag = child["tag"]
            siblings[tag] = siblings.get(tag, 0) + 1
            if tag in _HTMLText.OMIT:
                continue
            if boundary(child):
                emit()
                if tag != "br":
                    visit(child, path + f"/{tag}[{siblings[tag]}]")
            else:
                text.append(all_text(child))
        emit()

    visit(reader.root, "")
    return result


def _html_pattern(adapter_key):
    # Only these HTML publisher grammars are currently independently bound.
    if adapter_key in {"fca_handbook", "bis_basel", "sec_edgar"}:
        from app.clhear.l1.adapters import publisher_adapter_class
        return publisher_adapter_class(adapter_key).PROVISION
    return None


def verify_html_structure(source_key, adapter_key, artifacts, tree):
    pattern = _html_pattern(adapter_key)
    expected = []
    for part, artifact in enumerate(artifacts, 1):
        records = html_blocks(artifact.content)
        numbered = any(pattern and pattern.match(row["text"]) for row in records)
        headings, active = [], None
        for row in records:
            path, text = row["path"], row["text"]
            match = pattern.match(text) if pattern else None
            level = int(row["tag"][1]) if re.fullmatch(r"h[1-6]", row["tag"]) else None
            if level and not match:
                headings = [h for h in headings if h[0] < level]
                parent = headings[-1][1] if headings else None
                kind, ref = "group" if numbered else "section", f"{source_key}/html/{part}{path}"
                headings.append((level, path))
                active = None
            elif match:
                parent = headings[-1][1] if headings else None
                kind, ref = "provision", normalize(match.group("ref"))
                active = path
            else:
                parent = active or (headings[-1][1] if headings else None)
                kind, ref = "paragraph", ""
            expected.append((part, path, parent, kind, ref, text))
    observed = []

    def visit(node, parent=None):
        loc = node.source_locator
        current = parent
        if loc.get("structure") == "html-block":
            value = normalize(" ".join(v for v in (node.label, node.heading, node.raw_text) if v))
            observed.append((loc.get("part"), loc.get("path"), parent, node.node_type, node.ref, value))
            current = loc.get("path")
        elif node.heading or node.raw_text or node.label:
            if not (node.node_type == "title" and node.ref == source_key and not node.source_fragment):
                return False
        for child in node.children:
            if not visit(child, current):
                return False
        return True

    return all(visit(root) for root in tree) and expected == observed


def _decode_html(content):
    match = re.search(br"charset\s*=\s*['\"]?([A-Za-z0-9._-]+)", content[:4096], re.I)
    return content.decode(match.group(1).decode("ascii") if match else "utf-8-sig")


def html_text(content):
    reader = _HTMLText()
    # HTML encoding is declared independently, with strict decoding. Never
    # replace undecodable characters while claiming fidelity.
    match = re.search(br"charset\s*=\s*['\"]?([A-Za-z0-9._-]+)", content[:4096], re.I)
    encoding = match.group(1).decode("ascii") if match else "utf-8-sig"
    reader.feed(content.decode(encoding))
    reader.close()
    return normalize("".join(reader.parts))


def _pdf_structure_rows(pages, source_key, adapter_key, part=1):
    """Independent grammar over pdfminer lines, separate from pypdf parsing."""
    from app.clhear.l1.adapters.pdf_docling import SECTION
    from app.clhear.l1.adapters import publisher_adapter_class
    grammar = (publisher_adapter_class(adapter_key) if adapter_key in {
        "fca_handbook", "sec_edgar", "esma", "fatf", "bis_basel", "iosco", "mas", "asic", "isa", "irs_gov"
    } else None)
    pattern = grammar.PROVISION if grammar else SECTION
    heading_pattern = grammar.HEADING if grammar else None
    rows, stack, heading, heading_ref, active, seen, seq = [], [], "", None, None, set(), 0
    first_marker, contents_resolved = None, False
    for page, raw in enumerate(pages, 1):
        for physical_line, line in enumerate(raw.splitlines(), 1):
            text = normalize(line)
            if not text:
                continue
            seq += 1
            marker = pattern.match(text) if pattern else None
            is_heading = heading_pattern.match(text) if heading_pattern and not marker else None
            if (not grammar and marker and not contents_resolved and first_marker is not None
                    and marker.group("ref") == first_marker and rows and rows[0][2] == "section"):
                # Same shape as pdf_docling.pages_to_tree: a numbered contents
                # listing followed by the body restarting at its first marker.
                # The listing becomes unnumbered paragraphs of the title node.
                rows = [(r[0], r[1], "paragraph", "", None, r[5]) for r in rows]
                stack, active, seen, contents_resolved = [], None, set(), True
            if not grammar and marker and first_marker is None:
                first_marker = marker.group("ref")
            if grammar:
                if is_heading:
                    kind, ref, parent = "group", f"{source_key}/p{part}s{seq}", None
                    heading, heading_ref, active = text, ref, None
                elif marker:
                    value = marker.group("ref")
                    if adapter_key == "fatf":
                        note = re.match(r"INTERPRETIVE NOTE TO RECOMMENDATION\s+(\d+)", heading, re.I)
                        ref = f"INR.{note[1]}.{value}" if note else f"R.{value}"
                    elif adapter_key == "esma":
                        group = re.match(r"Guideline\s+(\d+)", heading)
                        ref = f"G{group[1]}.para{value}" if group else f"para{value}"
                    elif adapter_key == "iosco": ref = f"P.{value}"
                    elif adapter_key == "isa": ref = f"s{value}"
                    elif adapter_key == "asic": ref = re.sub(r"^RG\s?", "RG ", value)
                    elif adapter_key == "irs_gov":
                        group = re.match(r"SECTION\s+(\d+)", heading)
                        ref = f"sec{group[1]}{value}" if group else f"sec{value}"
                    else: ref = normalize(value)
                    kind, parent, active = "provision", heading_ref, ref
                else:
                    kind, ref, parent = "paragraph", "", active or heading_ref
                structure, line_number = "publisher-pdf-line", seq
            else:
                if marker:
                    value = marker.group("ref")
                    depth = value.count(".")
                    stack = [p for p in stack if p[0] < depth]
                    kind, ref, parent = "section", f"{source_key}/section/{value}", stack[-1][1] if stack else None
                    stack.append((depth, ref))
                    active = ref
                else:
                    kind, ref, parent = "paragraph", "", active
                structure, line_number = "pdf-line", physical_line
            if ref and ref in seen:
                raise UnsupportedOriginal("Repeated PDF source marker requires explicit section scope")
            if ref:
                seen.add(ref)
            # Physical blank lines differ between independent decoders. The
            # verified location is page + ordered nonempty text block + hash.
            rows.append((part, page, kind, ref, parent, digest(text.encode())))
    if not any(r[2] in {"section", "provision"} for r in rows):
        raise UnsupportedOriginal("No recognized PDF section/control numbering")
    return rows


def pdf_original(content, source_key="", adapter_key="", part=1):
    """Independent pdfminer decoder + page/layout evidence; no OCR fallback."""
    try:
        from pdfminer.high_level import extract_pages
        from pdfminer.layout import LTTextContainer
    except ImportError as exc:
        raise UnsupportedOriginal("Independent PDF decoder pdfminer.six is unavailable") from exc
    pages, raw_pages, evidence = [], [], []
    for number, page in enumerate(extract_pages(io.BytesIO(content)), 1):
        blocks = [item for item in page if isinstance(item, LTTextContainer)]
        raw = "\n".join(item.get_text() for item in blocks)
        raw_pages.append(raw)
        text = normalize(raw)
        from app.clhear.l1.adapters.pdf_docling import SECTION
        markers = [match.group("ref") for line in raw.splitlines() if (match := SECTION.match(line.strip()))]
        pages.append(text)
        evidence.append({"page": number, "width": page.width, "height": page.height,
                         "text_blocks": len(blocks), "normalized_characters": len(text),
                         "section_markers": markers,
                         "text_sha256": digest(text.encode()),
                         "boxes": [[round(v, 3) for v in item.bbox] for item in blocks]})
    if not pages or any(not text for text in pages):
        raise UnsupportedOriginal("PDF contains empty or non-text pages; visual/OCR verification is required")
    if source_key:
        evidence[0]["structure_sha256"] = digest(json.dumps(_pdf_structure_rows(raw_pages, source_key, adapter_key, part), separators=(",", ":")).encode())
    return normalize(" ".join(pages)), evidence


def verify_pdf_structure(source_key, tree, artifact_evidence):
    observed = {}
    def visit_line(node, parent=None):
        loc = node.source_locator
        if loc.get("structure") in {"pdf-line", "publisher-pdf-line"}:
            value = normalize(" ".join(v for v in (node.label, node.heading, node.raw_text) if v))
            part = loc.get("part", 1)
            observed.setdefault(part, []).append((part, loc.get("page"), node.node_type, node.ref, parent, digest(value.encode())))
            if node.node_type in {"section", "provision", "group"}:
                parent = node.ref
        elif node.node_type != "title" or node.raw_text:
            return False
        return all(visit_line(child, parent) for child in node.children)
    if not all(visit_line(root) for root in tree):
        return False
    return len(observed) == len(artifact_evidence) and all(
        artifact.get("pages", [{}])[0].get("structure_sha256") == digest(json.dumps(observed.get(part, []), separators=(",", ":")).encode())
        for part, artifact in enumerate(artifact_evidence, 1))




def _local(tag):
    return tag.rsplit("}", 1)[-1]


def _xml_text(content, adapter_key):
    if adapter_key not in {"uk_legislation", "govinfo_us", "govinfo_us_ecfr"}:
        raise UnsupportedOriginal("No independently declared XML document scope for this adapter")
    from app.clhear.l1.adapters.xml_document import original_records
    return normalize(" ".join(row[7] for row in original_records(content, "original-scope", adapter_key)))


def _json_text(content, source_key):
    if source_key in {"nist/sp800-53r5", "nist/csf-2.0"}:
        from app.clhear.l1.adapters.json_document import original_records
        return normalize(" ".join(row[-1] for row in original_records(content, source_key)))
    raise UnsupportedOriginal("No independently declared JSON schema for this adapter")


def original_view(source_key, adapter_key, artifacts, canonical_url=""):
    pieces, evidence = [], []
    names = [a.name for a in artifacts]
    if not names or not all(names) or len(set(names)) != len(names):
        raise ValueError("Original artifact names must be nonempty and unique")
    for part, artifact in enumerate(artifacts, 1):
        body = artifact.content
        item = {"name": artifact.name, "sha256": digest(body), "byte_count": len(body),
                "normalization_version": NORMALIZATION_VERSION, "offset_unit": OFFSET_UNIT}
        if not body:
            raise ValueError("Empty original artifact")
        if body.startswith(b"%PDF-"):
            text, pages = pdf_original(body, source_key, adapter_key, part)
            item.update(method="pdfminer-text-and-layout", pages=pages)
        elif body.lstrip().startswith((b"{", b"[")):
            text = _json_text(body, source_key)
            item["method"] = "independent-json-field-walk"
        elif adapter_key == "lists":
            from app.clhear.l1.adapters.list_records import original_records
            records = original_records(body, artifact.name, source_key)
            text = normalize(" ".join(field["value"] for record in records for field in record["fields"]))
            item.update(method="independent-list-record-fields", record_count=len(records))
        elif adapter_key == "uk_legislation" and b"<" in body[:100]:
            text = _xml_text(body, adapter_key)
            item["method"] = "independent-clml-text-walk"
        elif b"<DIV8" in body or b"<DIV1" in body:
            text = _xml_text(body, adapter_key)
            item["method"] = "independent-ecfr-text-walk"
        elif _publication_page(adapter_key, body):
            from app.clhear.l1.adapters.publication_pages import original_text
            text = normalize(original_text(body))
            item.update(method="publication-blocks-independent-html", scope="publication-title-and-body-blocks",
                        exclusions=sorted(_HTMLText.OMIT))
        elif adapter_key in {"eur_lex", "govinfo_us", "govinfo_us_usc"} and b"<" in body[:1024]:
            from app.clhear.l1.adapters.dom_document import original_records
            text = normalize(" ".join(row[7] for row in original_records(body, source_key)))
            item.update(method="independent-legal-html-dom-text-nodes", exclusions=sorted(_HTMLText.OMIT))
        elif b"<" in body[:1024]:
            text = html_text(body)
            item.update(method="stdlib-html-visible-text", exclusions=sorted(_HTMLText.OMIT))
        else:
            text = normalize(body.decode("utf-8-sig"))
            item["method"] = "strict-utf8-text"
        if not text:
            raise ValueError("Original has no independently extractable in-scope text")
        item.update(normalized_characters=len(text), normalized_text_sha256=digest(text.encode()))
        evidence.append(item)
        pieces.append(text)
    if not pieces:
        raise ValueError("No original artifacts")
    return normalize(" ".join(pieces)), evidence


def _tree_fields(tree, source_key, adapter_key):
    """Ordered publisher fields; only explicitly structural display metadata is omitted."""
    for root in tree:
        for node in root.walk():
            for field in ("label", "heading", "raw_text"):
                value = getattr(node, field)
                if not value:
                    continue
                if field == "label" and value == node.heading:
                    continue  # one publisher occurrence, stored twice for display
                if field != "raw_text" and not node.source_fragment:
                    if node.node_type == "title" and node.ref == source_key:
                        continue
                    if node.node_type == "chapter":
                        continue  # generated page/chapter display title
                if (adapter_key == "lists" or node.source_locator.get("structure") in {"json-value", "xml-element", "xml-text", "legal-html-element", "legal-html-text"}) and field != "raw_text":
                    continue  # identifiers/field labels are structured metadata
                yield node, field, normalize(value)


def attach_source_locations(source_key, adapter_key, artifacts, tree, canonical_url=""):
    """Attach verifiable positions only after exact ordered source equality.

    Positions refer to normalized publisher text, not HTML/PDF bytes. The
    immutable artifact set and independent decoder make them reproducible.
    """
    expected, evidence = original_view(source_key, adapter_key, artifacts, canonical_url)
    fields = list(_tree_fields(tree, source_key, adapter_key))
    if normalize(" ".join(value for _, _, value in fields)) != expected:
        return False
    for root in tree:
        for node in root.walk():
            node.source_locator = {**node.source_locator, "normalization_version": NORMALIZATION_VERSION, "offset_unit": OFFSET_UNIT,
                                   "representation": "ordered-normalized-publisher-text", "artifacts": [{k: a[k] for k in ("name", "sha256")} for a in evidence],
                                   "fields": {}}
    cursor = 0
    for node, field, value in fields:
        if not value:
            continue
        node.source_locator["fields"][field] = {"start": cursor, "end": cursor + len(value)}
        cursor += len(value) + 1
    return True


def _stored_tree(nodes):
    rows = [dict(row) for row in nodes]
    if not rows:
        raise ValueError("Empty stored tree")
    rows.sort(key=lambda row: row["seq"])
    if [row["seq"] for row in rows] != list(range(1, len(rows) + 1)):
        raise ValueError("Stored node sequence is incomplete or duplicated")
    indexed = {row["id"]: DocNode(**{k: row.get(k) or {} if k == "source_locator" else row.get(k) or ""
                                     for k in ("node_type", "ref", "label", "heading", "raw_text", "source_fragment", "source_locator")}) for row in rows}
    roots, depths = [], {}
    for row in rows:
        node = indexed[row["id"]]
        node.db_id = row["id"]
        parent = row.get("parent_id")
        if parent is None:
            roots.append(node)
            depths[row["id"]] = 0
        elif parent not in depths:
            raise ValueError("Orphaned, cyclic or out-of-order stored parent")
        else:
            indexed[parent].children.append(node)
            depths[row["id"]] = depths[parent] + 1
        if row.get("depth", 0) != depths[row["id"]]:
            raise ValueError("Stored node depth differs from its parent")
    if [n.db_id for root in roots for n in root.walk()] != [r["id"] for r in rows]:
        raise ValueError("Stored sequence differs from document preorder")
    return roots


def verify_clause_projection(tree, clauses):
    """Exact bijection, including missing/extra rows; no sampling or set loss."""
    expected = [(seq, node) for seq, node in enumerate((n for r in tree for n in r.walk()), 1)
                if node.node_type in CLAUSE_TYPES and node.ref]
    rows = [dict(row) for row in clauses]
    findings = []
    if not expected or len(rows) != len(expected):
        findings.append({"code": "clause_cardinality_mismatch", "detail": "Stored clauses do not match the complete addressable source-node set"})
    by_id = {getattr(node, "db_id", None): (seq, node) for seq, node in expected}
    layout, canonical, seen = span_layout(tree), canonical_text(tree), set()
    for row in rows:
        node_id = row.get("doc_node_id")
        if node_id not in by_id or node_id in seen:
            findings.append({"code": "clause_node_bijection_mismatch", "detail": "Extra, duplicate or incorrectly linked clause"})
            continue
        seen.add(node_id)
        seq, node = by_id[node_id]
        text = node.subtree_text()
        start, end = layout[id(node)]
        if (row.get("ref"), row.get("ordering"), row.get("text"), row.get("text_hash"), row.get("span_start"), row.get("span_end")) != (node.ref, seq, text, digest(text.encode()), start, end) or canonical[start:end] != text:
            findings.append({"code": "clause_projection_mismatch", "detail": "Clause reference, order, wording, digest or exact canonical span differs from its source node"})
    return findings, len(expected), len(rows)


def verify_original_projection(source_key, adapter_key, artifacts, nodes, clauses=None, canonical_url=""):
    report = {"status": "failed", "verified": False, "findings": [], "method": "independent-original-and-complete-projection-v1",
              "normalization_version": NORMALIZATION_VERSION, "expected_clause_count": 0, "observed_clause_count": 0, "artifact_evidence": []}
    try:
        tree = list(nodes)
        if tree and not isinstance(tree[0], DocNode):
            tree = _stored_tree(tree)
        if not tree:
            raise ValueError("Empty document tree")
        if clauses is not None:
            findings, expected_n, observed_n = verify_clause_projection(tree, clauses)
            report["findings"].extend(findings)
            report.update(expected_clause_count=expected_n, observed_clause_count=observed_n)
        expected, evidence = original_view(source_key, adapter_key, artifacts, canonical_url)
        report["artifact_evidence"] = evidence
        fields = list(_tree_fields(tree, source_key, adapter_key))
        actual = normalize(" ".join(value for _, _, value in fields))
        if actual != expected:
            report["findings"].append({"code": "ordered_original_text_mismatch", "detail": "Publisher and stored text differ in wording, order, multiplicity or additional content", "expected_characters": len(expected), "observed_characters": len(actual)})
        if adapter_key == "lists":
            from app.clhear.l1.adapters.list_records import verify_records
            if not verify_records(artifacts, source_key, tree):
                report["findings"].append({"code": "publisher_record_mismatch", "detail": "Complete publisher records, identities, fields or record hierarchy disagree"})
        elif source_key in {"nist/sp800-53r5", "nist/csf-2.0"}:
            from app.clhear.l1.adapters.json_document import verify
            if len(artifacts) != 1 or not verify(artifacts[0].content, source_key, tree):
                report["findings"].append({"code": "publisher_hierarchy_mismatch", "detail": "Complete JSON records, property order, scalar values, identifiers or parent pointers disagree"})
        elif adapter_key == "uk_legislation" or (adapter_key in {"govinfo_us", "govinfo_us_ecfr"} and all(b"<DIV8" in a.content or b"<DIV1" in a.content for a in artifacts)):
            from app.clhear.l1.adapters.xml_document import verify
            if not verify(artifacts, source_key, adapter_key, tree):
                report["findings"].append({"code": "publisher_hierarchy_mismatch", "detail": "Complete XML elements, attributes, source text nodes, identifiers or ancestry disagree"})
        elif artifacts and all(_publication_page(adapter_key, a.content) for a in artifacts):
            from app.clhear.l1.adapters.publication_pages import verify as verify_publication
            if not verify_publication(artifacts, source_key, tree):
                report["findings"].append({"code": "publisher_hierarchy_mismatch", "detail": "Publication title, body blocks, headings or block identities disagree"})
        elif adapter_key in {"eur_lex", "govinfo_us", "govinfo_us_usc"} and all(not a.content.startswith(b"%PDF-") for a in artifacts):
            from app.clhear.l1.adapters.dom_document import verify
            if not verify(artifacts, source_key, tree):
                report["findings"].append({"code": "publisher_hierarchy_mismatch", "detail": "Complete legal HTML elements, source text, publisher identifiers or DOM ancestry disagree"})
        elif artifacts and all(a.content.startswith(b"%PDF-") for a in artifacts) and any(n.source_locator.get("structure") in {"pdf-line", "publisher-pdf-line"} for r in tree for n in r.walk()):
            if not verify_pdf_structure(source_key, tree, evidence):
                report["findings"].append({"code": "publisher_hierarchy_mismatch", "detail": "Independent PDF decoder, page evidence, section/control markers or nesting disagree"})
        elif artifacts and all(not a.content.startswith(b"%PDF-") and b"<" in a.content[:1024] for a in artifacts) and any(n.source_locator.get("structure") == "html-block" for r in tree for n in r.walk()):
            if not verify_html_structure(source_key, adapter_key, artifacts, tree):
                report["findings"].append({"code": "publisher_hierarchy_mismatch", "detail": "Independent HTML source blocks, heading hierarchy, identities or source locations disagree"})
        else:
            # Exact wording alone does not prove a publisher's legal hierarchy.
            # Adapters must supply source-address evidence; legacy nodes remain
            # inspectable, but cannot inherit a fabricated certificate.
            if any(not node.source_locator for root in tree for node in root.walk()):
                report["findings"].append({"code": "source_locator_unverified", "detail": "Document structure lacks artifact-bound original locations"})
            report["findings"].append({"code": "publisher_hierarchy_unverified", "detail": "This format still requires an independent publisher-structure comparison"})
        fragment_nodes = [n for root in tree for n in root.walk() if n.source_fragment]
        if fragment_nodes and not all(n.source_locator.get("structure") == "xml-element" for n in fragment_nodes):
            if (not all(not a.content.startswith(b"%PDF-") and b"<" in a.content[:1024] for a in artifacts)
                    or not verify_html_fragments(source_key, artifacts, tree)):
                report["findings"].append({"code": "source_fragment_mismatch", "detail": "Stored markup fragment is not equivalent to its independently decoded original element"})
        expected_artifacts = [{k: a[k] for k in ("name", "sha256")} for a in evidence]
        if any(n.source_locator.get("artifacts") != expected_artifacts for root in tree for n in root.walk()):
            report["findings"].append({"code": "source_locator_unverified", "detail": "Every source node must retain a binding to the independently verified original artifacts"})
        cursor = 0
        for node, field, value in fields:
            if not value:
                continue
            loc = node.source_locator
            if loc:
                position = loc.get("fields", {}).get(field, {})
                if (loc.get("normalization_version") != NORMALIZATION_VERSION or loc.get("offset_unit") != OFFSET_UNIT
                        or loc.get("artifacts") != expected_artifacts or position != {"start": cursor, "end": cursor + len(value)}
                        or expected[cursor:cursor + len(value)] != value):
                    report["findings"].append({"code": "source_locator_mismatch", "detail": "Stored field location disagrees with immutable original text"})
            cursor += len(value) + 1
        unsupported = report["findings"] and all(f["code"].endswith("_unverified") for f in report["findings"])
        report.update(status="verified" if not report["findings"] else "unsupported" if unsupported else "failed", verified=not report["findings"])
    except UnsupportedOriginal as exc:
        report.update(status="unsupported")
        report["findings"].append({"code": "independent_text_comparison_unverified", "detail": str(exc)})
    except Exception as exc:
        report["findings"].append({"code": "invalid_original_or_projection", "detail": "Original or stored structure could not be verified", "error_type": type(exc).__name__})
    return report
