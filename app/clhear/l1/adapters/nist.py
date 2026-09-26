# Copyright (C) 2026 Reg42 AI
# This file is part of CLHEAR. See LICENSE (AGPL-3.0-only).
"""NIST spine adapter (HLD §7.3): SP 800-53 rev 5 (OSCAL catalog) and CSF 2.0
(CPRT export). Public domain — the open canonical infosec text.

Emits a typed DocNode tree:
  800-53: group / control / enhancement / statement (raw OSCAL prose, params
          left as `{{ insert: param, … }}` — no substitution)
  CSF:    part (function) / group (category) / provision (subcategory)
"""
import json

from app.clhear.l1 import http
from app.clhear.l1.adapters.base import Artifact, DocNode, FetchResult, SourceMeta

OSCAL_URL = (
    "https://raw.githubusercontent.com/usnistgov/oscal-content/main/"
    "nist.gov/SP800-53/rev5/json/NIST_SP-800-53_rev5_catalog.json"
)
CSF_URL = (
    "https://csrc.nist.gov/extensions/nudp/services/json/nudp/framework/"
    "version/csf_2_0_0/export/json?element=all"
)

FAMILY = dict(
    family_key="nist-spine",
    family_name="NIST spine (SP 800-53 r5 + CSF 2.0)",
    issuer="NIST",
    jurisdiction="US",
    license="open",
    license_ref="public domain (17 U.S.C. 105)",
)


class NistSp80053Adapter:
    key = "nist_sp800_53"

    def meta(self) -> SourceMeta:
        return SourceMeta(
            source_key="nist/sp800-53r5",
            name="NIST SP 800-53 Revision 5 — Security and Privacy Controls",
            kind="standard",
            canonical_url="https://csrc.nist.gov/pubs/sp/800/53/r5/upd1/final",
            adapter="nist",
            scope_charter={"binding": "full text (public domain)"},
            short_name="NIST 800-53",
            about=(
                "NIST's catalog of security and privacy controls for information systems and "
                "organizations — the canonical public-domain control set (AC, AU, IR, SC …) "
                "referenced by FedRAMP, FISMA and most security frameworks. Each control has "
                "a structured statement with parameters and optional enhancements."
            ),
            topics=["infosec", "controls", "nist", "us"],
            version_policy="edition",
            **FAMILY,
        )

    def fetch(self, since_version: str | None = None) -> FetchResult | None:
        content = http.get(OSCAL_URL)
        catalog = json.loads(content)["catalog"]
        version_label = f"edition:{catalog['metadata']['version']}"
        if since_version == version_label:
            return None

        from app.clhear.l1.adapters.json_document import parse
        tree = parse(content, self.meta().source_key)
        return FetchResult(
            version_label=version_label,
            artifacts=[Artifact(name="catalog.json", content=content, content_type="application/json")],
            tree=tree,
            version_kind="edition",
        )

    def _control_node(self, control: dict, node_type: str) -> DocNode:
        statements = []
        for part in control.get("parts", []):
            if part.get("name") == "statement":
                statements.extend(self._statement_nodes(part, control["id"]))
        return DocNode(
            node_type=node_type,
            ref=control["id"],
            label=control["id"].upper(),
            heading=control.get("title", ""),
            source_fragment=json.dumps(
                {"id": control["id"], "title": control.get("title"), "parts": control.get("parts", [])},
                ensure_ascii=False,
            ),
            children=statements,
        )

    def expected_text(self, artifacts: list[Artifact]) -> list[str]:
        from app.clhear.l1.adapters.json_document import original_records
        return [row[-1] for artifact in artifacts
                for row in original_records(artifact.content, self.meta().source_key) if row[-1].strip()]

    def _statement_nodes(self, part: dict, parent_ref: str) -> list[DocNode]:
        label = next((p["value"] for p in part.get("props", []) if p.get("name") == "label"), "")
        prose = part.get("prose") or ""
        # Chain refs through nesting so a./a.1./b.1. stay unique per control.
        ref = f"{parent_ref}:{label.rstrip('.')}" if label else parent_ref
        children = []
        for sub in part.get("parts", []):
            children.extend(self._statement_nodes(sub, ref))
        if not prose and not children:
            return []
        return [
            DocNode(
                node_type="statement",
                ref=ref if label else "",
                label=label,
                raw_text=prose,
                source_fragment=json.dumps(part, ensure_ascii=False),
                children=children,
            )
        ]


class NistCsfAdapter:
    key = "nist_csf"

    def meta(self) -> SourceMeta:
        return SourceMeta(
            source_key="nist/csf-2.0",
            name="NIST Cybersecurity Framework (CSF) 2.0",
            kind="standard",
            canonical_url="https://www.nist.gov/cyberframework",
            adapter="nist",
            scope_charter={"binding": "full text (public domain)"},
            short_name="NIST CSF 2.0",
            about=(
                "The NIST Cybersecurity Framework 2.0: a voluntary, outcome-based taxonomy of "
                "cybersecurity risk management organized into six functions (Govern, Identify, "
                "Protect, Detect, Respond, Recover) with categories and subcategories, used "
                "globally to structure security programs and communicate risk posture."
            ),
            topics=["infosec", "risk-management", "nist", "us"],
            version_policy="edition",
            **FAMILY,
        )

    def fetch(self, since_version: str | None = None) -> FetchResult | None:
        content = http.get(CSF_URL)
        payload = json.loads(content)["response"]["elements"]
        doc = payload["documents"][0]
        number = doc["version"].split()[-1]  # "Version  2.0" -> "2.0"
        version_label = f"edition:{number}"
        if since_version == version_label:
            return None

        from app.clhear.l1.adapters.json_document import parse
        tree = parse(content, self.meta().source_key)
        return FetchResult(
            version_label=version_label,
            artifacts=[Artifact(name="csf-export.json", content=content, content_type="application/json")],
            tree=tree,
            version_kind="edition",
        )

    def expected_text(self, artifacts: list[Artifact]) -> list[str]:
        from app.clhear.l1.adapters.json_document import original_records
        return [row[-1] for artifact in artifacts
                for row in original_records(artifact.content, self.meta().source_key) if row[-1].strip()]
