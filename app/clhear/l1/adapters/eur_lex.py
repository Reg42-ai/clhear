# Copyright (C) 2026 Reg42 AI
# This file is part of CLHEAR. See LICENSE (AGPL-3.0-only).
"""EUR-Lex / Cellar adapter (HLD §7.2 adapter 2): GDPR (CELEX 32016R0679).

Two artifact formats, one adapter:
- ORIGINAL OJ act (celex `32016R0679`): title block, full preamble (citations
  "Having regard…" + recitals (1)–(173)), chapters/sections, articles with
  numbered paragraphs and point tables, final provisions, signatures, notes.
- CONSOLIDATED text (celex `02016R0679-YYYYMMDD`, CONVEX output): title block,
  chapters/sections, articles with `no-parag` markers and grid-list points,
  final provisions, footnotes. Consolidations legitimately carry no preamble.

The fidelity oracle (`expected_text`) is a dumb visible-text extraction with
declared exclusions (consolidation banners/markers, separators) — independent
of the structural parse by construction.

License: EUR-Lex legal notice permits reuse of legal texts (Commission
Decision 2011/833/EU) — verbatim reproduction with source acknowledgement.
"""
import re
from datetime import date
from urllib.parse import quote

from bs4 import BeautifulSoup, Tag

from app.clhear.l1 import http
from app.clhear.l1.adapters.base import Artifact, DocNode, EffectRecord, FetchResult, SourceMeta, flatten

CELLAR = "http://publications.europa.eu/resource/celex"


def cellar_resource_url(celex: str) -> str:
    """Cellar resource URLs require encoded parentheses (CELEX corrigenda)."""
    return f"{CELLAR}/{quote(celex, safe='-')}"
GDPR_CELEX = "32016R0679"
# GDPR's only consolidation; new consolidations get new date suffixes.
# ARCH: consolidation discovery via the Cellar work tree lands with the P2
# relations work; until then the adapter pins the current consolidated version.
GDPR_CONSOLIDATED = "02016R0679-20160504"

# OJ publication dates of the original acts we track (as_published as_of).
OJ_DATES = {"32016R0679": date(2016, 5, 4)}  # OJ L 119, 4.5.2016

_HEADERS = {"Accept": "application/xhtml+xml", "Accept-Language": "eng"}

# Presentation/consolidation artifacts, not legal text (declared oracle
# exclusions — the parser skips them for the same reason).
_EXCLUDED_CLASSES = {
    "disclaimer",
    "modref",
    "hd-modifiers",
    "arrow",
    "separator",
    "separator-short",
    "reference",  # CONVEX consolidation banner line ("02016R0679 — EN — …")
    "title-doc-oj-reference",
    "title-fam-member",
    "title-fam-member-star",  # CONVEX "amended by" apparatus (act list, not enacted text)
    "oj-separator",
}


def _ws(text: str) -> str:
    return " ".join(text.split())


def _txt(el) -> str:
    return _ws(el.get_text(" ", strip=True)) if el is not None else ""


def _strip_excluded(soup: BeautifulSoup) -> BeautifulSoup:
    for el in list(soup.find_all(["script", "style"])):
        el.decompose()
    doomed = [
        el
        for el in soup.find_all(True)
        if isinstance(el, Tag) and set(el.get("class") or []) & _EXCLUDED_CLASSES
    ]
    for el in doomed:
        el.decompose()
    return soup


class EurLexAdapter:
    key = "eur_lex"

    def __init__(
        self,
        celex: str = GDPR_CELEX,
        celex_version: str = GDPR_CONSOLIDATED,
        meta: SourceMeta | None = None,
    ):
        self.celex = celex
        # The Cellar id actually fetched: an original act ("3…") or a
        # consolidated text ("0…-YYYYMMDD").
        self.celex_version = celex_version
        # Registry-supplied SourceMeta (eToro blueprint); default = GDPR.
        self._meta = meta

    def meta(self) -> SourceMeta:
        if self._meta is not None:
            return self._meta
        return SourceMeta(
            family_key="eu-gdpr",
            family_name="EU General Data Protection Regulation",
            source_key=f"celex/{self.celex}",
            name="Regulation (EU) 2016/679 (General Data Protection Regulation)",
            kind="regulation",
            issuer="European Parliament and Council (Publications Office)",
            jurisdiction="EU",
            license="open",
            license_ref="Commission Decision 2011/833/EU",
            canonical_url="https://eur-lex.europa.eu/eli/reg/2016/679/oj",
            adapter=self.key,
            scope_charter={
                "binding": "regulation (OJ original + consolidations) + corrigenda (CELEX relations)",
                "guidance": "EDPB guidelines (watcher, later)",
                "out": ["national DPA guidance"],
            },
            short_name="GDPR",
            about=(
                "The EU General Data Protection Regulation, the union-wide framework for "
                "processing personal data. It binds controllers and processors established in "
                "the EU or targeting EU data subjects, defining lawful bases, data-subject "
                "rights, accountability duties, international-transfer rules and supervisory "
                "enforcement with penalties up to 4% of worldwide turnover."
            ),
            topics=["data-protection", "privacy", "eu"],
            version_policy="as_published+consolidated",
        )

    @property
    def version_kind(self) -> str:
        return "consolidated" if self.celex_version.startswith("0") else "as_published"

    @property
    def as_of_date(self) -> date | None:
        if self.celex_version.startswith("0"):
            suffix = self.celex_version.rsplit("-", 1)[-1]
            try:
                return date(int(suffix[:4]), int(suffix[4:6]), int(suffix[6:8]))
            except (ValueError, IndexError):
                return None
        return OJ_DATES.get(self.celex_version)

    @property
    def version_label(self) -> str:
        as_of = self.as_of_date
        suffix = as_of.isoformat() if as_of else self.celex_version
        return f"{'consolidated' if self.version_kind == 'consolidated' else 'as-published'}:{suffix}"

    def fetch(self, since_version: str | None = None) -> FetchResult | None:
        if since_version == self.version_label:
            return None
        content = http.get(cellar_resource_url(self.celex_version), headers=_HEADERS)
        from app.clhear.l1.adapters.dom_document import parse
        tree = parse(content, self.meta().source_key)
        return FetchResult(
            version_label=self.version_label,
            artifacts=[Artifact(name=f"{self.celex_version}.xhtml", content=content, content_type="application/xhtml+xml")],
            tree=tree,
            version_kind=self.version_kind,
            as_of_date=self.as_of_date,
        )

    def expected_text(self, artifacts: list[Artifact]) -> list[str]:
        from app.clhear.l1.adapters.dom_document import original_records
        return [row[7] for part, artifact in enumerate(artifacts, 1)
                for row in original_records(artifact.content, self.meta().source_key, part) if row[7].strip()]

    # ------------------------------------------------------------------ CONVEX
    def _parse_convex(self, soup: BeautifulSoup) -> list[DocNode]:
        tree: list[DocNode] = []
        current_chapter: DocNode | None = None
        current_section: DocNode | None = None

        title_div = soup.find("div", id="tit_1")
        if title_div is not None:
            tree.append(self._title_container(title_div, ["title-doc-first", "title-doc-last"]))

        for div in soup.find_all("div", id=True):
            div_id = str(div.get("id", ""))
            if re.fullmatch(r"cpt_(?:[IVXLC]+|\d+)", div_id):
                current_chapter = DocNode(
                    node_type="chapter",
                    ref=div_id,
                    label=_txt(div.find("p", class_="title-division-1")),
                    heading=_txt(div.find("p", class_="title-division-2")),
                )
                current_section = None
                tree.append(current_chapter)
                continue
            if re.fullmatch(r"cpt_(?:[IVXLC]+|\d+)\.sct_\d+", div_id):
                current_section = DocNode(
                    node_type="group",
                    ref=div_id,
                    label=_txt(div.find("p", class_="title-division-1")),
                    heading=_txt(div.find("p", class_="title-division-2")),
                )
                (current_chapter.children if current_chapter else tree).append(current_section)
                continue
            if re.fullmatch(r"art_\d+[a-z]*", div_id):
                # amendment-inserted articles carry letter suffixes (art_14a)
                article = self._convex_article(div, div_id)
                target = current_section or current_chapter
                (target.children if target else tree).append(article)
                continue
            if div_id == "fnp_1":
                tree.append(DocNode(node_type="signature", ref="fnp_1", raw_text=_txt(div)))

        notes = [p for p in soup.find_all("p", class_="footnote")]
        if notes:
            container = DocNode(node_type="note", ref="footnotes", heading="Footnotes")
            for p in notes:
                container.children.append(DocNode(node_type="note", raw_text=_txt(p)))
            tree.append(container)
        return tree

    def _convex_article(self, div: Tag, div_id: str) -> DocNode:
        number_el = div.find("p", class_="title-article-norm")
        subtitle_el = div.find("p", class_="stitle-article-norm")
        article = DocNode(
            node_type="article",
            ref=div_id,
            label=_txt(number_el) or f"Article {div_id.split('_')[1]}",
            heading=_txt(subtitle_el),
        )
        for child in list(div.children):
            if not isinstance(child, Tag):
                continue
            classes = set(child.get("class") or [])
            if "grid-container" in classes:
                # article-level point list (e.g. Art. 4 definitions): attach to
                # the introductory paragraph when one precedes it.
                points = self._extract_points(child, div_id, include_self=True)
                last = article.children[-1] if article.children else None
                target = last.children if last is not None and last.node_type == "paragraph" else article.children
                target.extend(points)
                continue
            if "norm" not in classes:
                continue
            marker_el = child.find("span", class_="no-parag")
            label = ""
            if marker_el is not None:
                label = _ws(marker_el.get_text())
                marker_el.extract()
            points = self._extract_points(child, f"{div_id}.{label.rstrip('.')}" if label else div_id)
            text = _txt(child)
            article.children.append(
                DocNode(
                    node_type="paragraph",
                    ref=f"{div_id}.{label.rstrip('.')}" if label else "",
                    label=label,
                    raw_text=text,
                    children=points,
                )
            )
        return article

    def _extract_points(self, block: Tag, ref_prefix: str, include_self: bool = False) -> list[DocNode]:
        """Grid lists -> point nodes; processed innermost-first to avoid text
        duplication, returned flat in document order."""
        if not isinstance(block, Tag):
            return []
        grids = list(block.find_all("div", class_="grid-container"))
        if include_self and "grid-container" in (block.get("class") or []):
            grids.insert(0, block)
        parsed: list[DocNode] = []
        for grid in reversed(grids):
            marker = _txt(grid.find(class_=re.compile("grid-list-column-1")))
            content = _txt(grid.find(class_=re.compile("grid-list-column-2")))
            if not marker and not content:
                continue
            ref = f"{ref_prefix}{marker}" if re.fullmatch(r"\([a-z0-9ivx]+\)", marker) else ""
            parsed.append(DocNode(node_type="point", ref=ref, label=marker, raw_text=content))
            if grid is not block:
                grid.extract()
            else:
                for el in list(grid.children):
                    if isinstance(el, Tag):
                        el.extract()
        parsed.reverse()
        return parsed

    def _title_container(self, div: Tag, classes: list[str]) -> DocNode:
        container = DocNode(node_type="title", ref="tit_1")
        for p in div.find_all("p"):
            p_classes = set(p.get("class") or [])
            if not classes or p_classes & set(classes):
                text = _txt(p)
                if text:
                    container.children.append(DocNode(node_type="title", raw_text=text))
        return container

    # ---------------------------------------------------------------------- OJ
    def _parse_oj(self, soup: BeautifulSoup) -> list[DocNode]:
        tree: list[DocNode] = []

        # OJ page header (date / OJ reference) + document title block.
        header = DocNode(node_type="title", ref="oj-header")
        for p in soup.find_all("p", class_=re.compile(r"^oj-hd-")):
            text = _txt(p)
            if text:
                header.children.append(DocNode(node_type="title", raw_text=text))
        if header.children:
            tree.append(header)
        title_div = soup.find("div", id="tit_1")
        if title_div is not None:
            container = DocNode(node_type="title", ref="tit_1")
            for p in title_div.find_all("p"):
                text = _txt(p)
                if text:
                    container.children.append(DocNode(node_type="title", raw_text=text))
            tree.append(container)

        # Preamble: institution line, citations (cit_N), "Whereas:", recitals (rct_N).
        preamble_div = soup.find("div", id="pbl_1")
        if preamble_div is not None:
            preamble = DocNode(node_type="preamble", ref="pbl_1", heading="")
            for child in preamble_div.children:
                if not isinstance(child, Tag):
                    continue
                child_id = str(child.get("id", ""))
                if child_id.startswith("cit_"):
                    preamble.children.append(
                        DocNode(node_type="preamble", ref=child_id, raw_text=_txt(child))
                    )
                elif child_id.startswith("rct_"):
                    preamble.children.append(self._oj_recital(child, child_id))
                elif child.name == "p":
                    text = _txt(child)
                    if text:
                        preamble.children.append(DocNode(node_type="preamble", raw_text=text))
                elif child.name == "div":
                    # container of citations/recitals (some layouts nest them)
                    for sub in child.find_all("div", id=re.compile(r"^(cit|rct)_\d+$")):
                        sub_id = str(sub.get("id"))
                        if sub_id.startswith("rct_"):
                            preamble.children.append(self._oj_recital(sub, sub_id))
                        else:
                            preamble.children.append(
                                DocNode(node_type="preamble", ref=sub_id, raw_text=_txt(sub))
                            )
            tree.append(preamble)

        _n = r"(?:[IVXLC]+|\d+)"
        current_prt: DocNode | None = None  # PART I (`prt_I`, IFR)
        current_part: DocNode | None = None  # TITLE I (`tis_I` / `prt_I.tis_II`)
        current_chapter: DocNode | None = None
        current_section: DocNode | None = None
        enacting = soup.find("div", id="enc_1") or soup
        for div in enacting.find_all("div", id=True):
            div_id = str(div.get("id", ""))
            # Subtitle wrappers (tis_I.tit_1 / art_1.tit_1) are not structure.
            if re.search(r"\.tit_\d+$", div_id):
                continue
            label, heading = self._oj_section_headings(div)
            if re.fullmatch(rf"prt_{_n}", div_id):
                current_prt = DocNode(node_type="part", ref=div_id, label=label or _txt(div), heading=heading)
                current_part = None
                current_chapter = None
                current_section = None
                tree.append(current_prt)
            elif re.fullmatch(rf"(?:prt_{_n}\.)?tis_{_n}", div_id):
                current_part = DocNode(node_type="part", ref=div_id, label=label or _txt(div), heading=heading)
                current_chapter = None
                current_section = None
                (current_prt.children if current_prt else tree).append(current_part)
            elif re.fullmatch(rf"(?:(?:prt_{_n}\.)?tis_{_n}\.)?cpt_{_n}", div_id):
                # CHAPTER I / CHAPTER 1 (GDPR `cpt_I`; MAR/UCPD `cpt_1`;
                # MiCA `tis_VI.cpt_1`; IFR `prt_I.tis_II.cpt_1`)
                current_chapter = DocNode(node_type="chapter", ref=div_id, label=label, heading=heading)
                current_section = None
                parent = current_part or current_prt
                (parent.children if parent else tree).append(current_chapter)
            elif re.search(rf"\.sct_{_n}$", div_id):
                current_section = DocNode(node_type="group", ref=div_id, label=label, heading=heading)
                parent = current_chapter or current_part or current_prt
                (parent.children if parent else tree).append(current_section)
            elif re.fullmatch(r"art_\d+[a-z]*", div_id):
                if div.find_parent("div", id=re.compile(r"^anx_")) is not None:
                    continue  # quoted article inside an annex: captured by annex flow
                article = self._oj_article(div, div_id)
                target = current_section or current_chapter or current_part or current_prt
                (target.children if target else tree).append(article)
        # Annexes (anx_I, anx_II, …): heterogeneous flow content (amending
        # instructions, quoted blocks, marker tables) captured verbatim.
        for anx in soup.find_all("div", id=re.compile(r"^anx_")):
            if anx.find_parent("div", id=re.compile(r"^anx_")) is not None:
                continue
            anx_id = str(anx.get("id"))
            heading_el = anx.find("p", class_="oj-doc-ti")
            label = _txt(heading_el)
            if heading_el is not None:
                heading_el.extract()
            annex = DocNode(
                node_type="schedule", ref=anx_id, label=label, children=self._oj_flow(anx)
            )
            tree.append(annex)
        # Final provisions block (sits AFTER enc_1): enacting formula,
        # "Done at …", signatories.
        fnp = soup.find("div", id="fnp_1")
        if fnp is not None:
            signature = DocNode(node_type="signature", ref="fnp_1")
            for p in fnp.find_all("p"):
                text = _txt(p)
                if text:
                    signature.children.append(DocNode(node_type="signature", raw_text=text))
            tree.append(signature)

        notes = soup.find_all("p", class_="oj-note")
        if notes:
            container = DocNode(node_type="note", ref="footnotes", heading="Footnotes")
            for p in notes:
                text = _txt(p)
                if text:
                    container.children.append(DocNode(node_type="note", raw_text=text))
            tree.append(container)
        return tree

    def _oj_section_headings(self, div: Tag) -> tuple[str, str]:
        """TITLE/CHAPTER/Section: oj-ti-section-1 number + oj-ti-section-2 title."""
        label = _txt(div.find("p", class_=re.compile(r"^oj-ti-section-1")))
        heading = _txt(div.find("p", class_=re.compile(r"^oj-ti-section-2")))
        if not label and not heading:
            headings = div.find_all("p", class_=re.compile(r"^oj-ti-"), limit=2)
            label = _txt(headings[0]) if headings else ""
            heading = _txt(headings[1]) if len(headings) > 1 else ""
        return label, heading

    def _oj_recital(self, div: Tag, div_id: str) -> DocNode:
        cells = div.find_all("td")
        label = _txt(cells[0]) if cells else ""
        body = " ".join(_txt(c) for c in cells[1:]) if len(cells) > 1 else _txt(div)
        return DocNode(node_type="recital", ref=div_id, label=label, raw_text=body)

    def _oj_article(self, div: Tag, div_id: str) -> DocNode:
        number_el = div.find("p", class_="oj-ti-art")
        subtitle_el = div.find("p", class_="oj-sti-art")
        article = DocNode(
            node_type="article",
            ref=div_id,
            label=_txt(number_el) or f"Article {div_id.split('_')[1]}",
            heading=_txt(subtitle_el),
        )
        # OJ wraps each numbered paragraph in its own div (id="001.001") and
        # point lists in marker tables — walk descendants in document order.
        for el in div.find_all(["p", "table"]):
            if not isinstance(el, Tag):
                continue
            if el.find_parent("table") is not None:
                continue  # cell paragraphs are captured via their table
            classes = set(el.get("class") or [])
            if el.name == "p":
                # Article number/subtitle already live on the parent; footnotes
                # are collected once at document level.
                if classes & {"oj-ti-art", "oj-sti-art"} or "oj-note" in classes:
                    continue
                text = _txt(el)
                if not text:
                    continue
                if any(c.startswith("oj-ti-") for c in classes):
                    article.children.append(DocNode(node_type="heading", heading=text))
                    continue
                match = re.match(r"^(\d+)\.\s+", text)
                label = f"{match.group(1)}." if match else ""
                if match:
                    text = text[match.end() :]
                article.children.append(
                    DocNode(
                        node_type="paragraph",
                        ref=f"{div_id}.{match.group(1)}" if match else "",
                        label=label,
                        raw_text=text,
                    )
                )
            else:  # table -> point rows
                marker, content = self._oj_table_row(el)
                if not marker and not content:
                    continue
                node = DocNode(node_type="point", ref="", label=marker, raw_text=content)
                last = article.children[-1] if article.children else None
                if last is not None and last.node_type == "paragraph":
                    last.children.append(node)
                else:
                    article.children.append(node)
        return article

    def _oj_flow(self, container: Tag) -> list[DocNode]:
        """Generic verbatim walker for heterogeneous OJ flow content (annexes,
        quoted blocks): every visible string captured exactly once, in order."""
        nodes: list[DocNode] = []
        for child in container.children:
            if not isinstance(child, Tag):
                text = _ws(str(child))
                if text:
                    nodes.append(DocNode(node_type="paragraph", raw_text=text))
                continue
            classes = set(child.get("class") or [])
            if child.name == "p":
                if "oj-note" in classes:
                    continue  # collected once in the document-level footnotes block
                text = _txt(child)
                if text:
                    nodes.append(DocNode(node_type="paragraph", raw_text=text))
            elif child.name == "table":
                nodes.extend(self._oj_table_flow(child))
            elif child.name in {"div", "blockquote", "section", "td", "th"}:
                nodes.extend(self._oj_flow(child))
            else:
                text = _txt(child)
                if text:
                    nodes.append(DocNode(node_type="paragraph", raw_text=text))
        return nodes

    def _oj_table_flow(self, table: Tag) -> list[DocNode]:
        """Tables in flow content: simple 2-column marker rows become points;
        complex rows (nested tables/divs) recurse cell by cell."""
        nodes: list[DocNode] = []
        rows = [tr for tr in table.find_all("tr") if tr.find_parent("table") is table]
        if not rows:
            text = _txt(table)
            return [DocNode(node_type="paragraph", raw_text=text)] if text else []
        for tr in rows:
            cells = [td for td in tr.find_all(["td", "th"]) if td.find_parent("tr") is tr]
            if not cells:
                continue
            if all(c.find("table") is None for c in cells):
                if len(cells) == 1:
                    text = _txt(cells[0])
                    if text:
                        nodes.append(DocNode(node_type="paragraph", raw_text=text))
                else:
                    marker = _txt(cells[0])
                    content = " ".join(t for t in (_txt(c) for c in cells[1:]) if t)
                    if re.fullmatch(r"\(?[a-z0-9ivx]+[).]", marker or ""):
                        # genuine list marker -> point node
                        nodes.append(DocNode(node_type="point", label=marker, raw_text=content))
                    elif marker or content:
                        # data/correlation row: cells may legitimately repeat
                        # (e.g. correlation tables) — keep as one flat paragraph.
                        nodes.append(DocNode(node_type="paragraph", raw_text=" — ".join(t for t in (marker, content) if t)))
                continue
            marker = "" if cells[0].find("table") is not None else _txt(cells[0])
            list_marker = bool(re.fullmatch(r"\(?[a-z0-9ivx]+[).]", marker or ""))
            body_cells = cells[1:] if marker else cells
            children: list[DocNode] = []
            if marker and not list_marker:
                children.append(DocNode(node_type="paragraph", raw_text=marker))
            for cell in body_cells:
                children.extend(self._oj_flow(cell))
            if children:
                nodes.append(
                    DocNode(node_type="point" if list_marker else "group",
                            label=marker if list_marker else "", children=children)
                )
        return nodes

    def _oj_table_row(self, table: Tag) -> tuple[str, str]:
        cells = table.find_all("td")
        if not cells:
            return "", _txt(table)
        marker = _txt(cells[0])
        content = " ".join(t for t in (_txt(c) for c in cells[1:]) if t)
        return marker, content

    # --- citator (corrigenda probe) -------------------------------------------
    def family_effects(self) -> list[EffectRecord]:
        """Corrigenda: probe CELEX 3…R(01)… identifiers against Cellar."""
        records: list[EffectRecord] = []
        for i in range(1, 10):
            corrigendum = f"{self.celex}R({i:02d})"
            try:
                http.get(
                    cellar_resource_url(corrigendum),
                    headers={"Accept": "application/xml;notice=identifiers"},
                )
            except Exception:
                break
            records.append(
                EffectRecord(
                    affecting_key=f"celex/{corrigendum}",
                    affecting_name=f"Corrigendum {corrigendum} to Regulation (EU) 2016/679",
                    affecting_url=f"https://eur-lex.europa.eu/legal-content/EN/TXT/?uri=CELEX:{corrigendum}",
                    relation="corrects",
                    kind="regulation",
                )
            )
        return records
