# Copyright (C) 2026 Reg42 AI
# This file is part of CLHEAR. See LICENSE (AGPL-3.0-only).
"""Adapter contract (HLD §7.2).

Adapters do retrieval + structural parse ONLY. They return the verbatim
artifacts plus a typed DocNode tree (raw text + source fragments). The
pipeline owns hashing, storage, clause projection, diffing, and events.
Never re-generate or "clean up" source text (HLD principle 1).
"""
from dataclasses import dataclass, field
from datetime import date
from typing import Iterator, Protocol, runtime_checkable

from app.clhear.l1.models import CLAUSE_TYPES, NODE_TYPES

__all__ = [
    "Adapter",
    "Artifact",
    "CLAUSE_TYPES",
    "CitatorAdapter",
    "DocNode",
    "EffectRecord",
    "FetchResult",
    "NODE_TYPES",
    "SourceMeta",
    "flatten",
]


@dataclass(frozen=True)
class SourceMeta:
    family_key: str
    family_name: str
    source_key: str
    name: str
    kind: str  # law|regulation|standard|guidance|form|agreement|enforcement
    issuer: str
    jurisdiction: str
    license: str  # open|restricted
    canonical_url: str
    adapter: str
    license_ref: str = ""
    scope_charter: dict = field(default_factory=dict)
    # Curated semantic context (deterministic; reviewed with the code).
    short_name: str = ""  # everyday handle: "GDPR", "UK AML Regulations"
    about: str = ""
    topics: list[str] = field(default_factory=list)
    # Declared version-ingestion policy, e.g. "as_published+consolidated",
    # "consolidated", "edition" — which version kinds this source tracks.
    version_policy: str = ""
    # HLD v2 §4.1 — rights basis (public_domain|open_licence|licensed|
    # byol_only|derived_only; empty = adapter default from l1.rights),
    # publisher (who hosts the official text) and instrument (the legal
    # instrument this source is a text of, e.g. "MiFID II", "FCA PRIN").
    rights_basis: str = ""
    rights_ref: str = ""
    publisher: str = ""
    instrument: str = ""


@dataclass
class DocNode:
    """One typed record of the official document tree.

    Containers have empty raw_text; leaf text blocks carry the verbatim
    string (no renderer normalization). source_fragment, when present, is an
    equivalent XML/HTML element serialization independently checked against
    the official artifact; it is not a byte-for-byte slice of that artifact.
    """

    node_type: str
    ref: str = ""
    label: str = ""
    heading: str = ""
    raw_text: str = ""
    source_fragment: str = ""
    # Machine-verifiable location in the immutable source artifact. Offsets
    # name their text representation and unit; they are never PDF byte offsets.
    source_locator: dict = field(default_factory=dict)
    children: list["DocNode"] = field(default_factory=list)
    # Publisher-declared status of a provision when the publisher has one
    # (FCA R/G/E/D, Basel "standard"/"guidance"); feeds the normative flag.
    status: str = ""

    def walk(self) -> Iterator["DocNode"]:
        yield self
        for child in self.children:
            yield from child.walk()

    def subtree_text(self) -> str:
        """Deterministic concatenation of heading + raw_text over the subtree.

        This is the clause-projection text (diff / search / L2 grain).
        """
        parts: list[str] = []
        if self.heading and "heading" not in self.source_locator.get("presentation_fields", []):
            parts.append(self.heading)
        if self.raw_text:
            parts.append(self.raw_text)
        for child in self.children:
            child_text = child.subtree_text()
            if child_text:
                parts.append(child_text)
        return "\n".join(parts)


@dataclass(frozen=True)
class Artifact:
    """A verbatim original as retrieved (stored immutably in the datalake)."""

    name: str
    content: bytes
    content_type: str = "application/octet-stream"


@dataclass
class FetchResult:
    version_label: str  # standardized: "{kind}:{as_of|identifier}"
    artifacts: list[Artifact]
    tree: list[DocNode]
    version_kind: str = "consolidated"  # as_published|consolidated|edition
    as_of_date: date | None = None      # the date the text state represents
    effective_date: date | None = None


@dataclass(frozen=True)
class EffectRecord:
    """One official-citator record: an instrument affecting the root source."""

    affecting_key: str          # e.g. uksi/2019/1511
    affecting_name: str
    affecting_url: str
    relation: str               # amends|corrects|...
    kind: str = "regulation"    # sources.kind of the affecting instrument


@runtime_checkable
class Adapter(Protocol):
    key: str

    def meta(self) -> SourceMeta: ...

    def fetch(self, since_version: str | None = None) -> FetchResult | None:
        """Return the current version, or None if since_version is still current."""
        ...

    def expected_text(self, artifacts: list[Artifact]) -> list[str]:
        """The fidelity ORACLE: complete ordered visible text of the artifacts
        as a list of spans, extracted trivially (all text nodes in document
        order minus declared exclusions). Deliberately dumb — it must not
        share logic (or bugs) with the structural parse in fetch().
        """
        ...


class CitatorAdapter(Adapter, Protocol):
    """Adapters for sources with an official effects/relations feed."""

    def family_effects(self) -> list[EffectRecord]: ...


def flatten(tree: list[DocNode]) -> list[DocNode]:
    out: list[DocNode] = []
    for node in tree:
        out.extend(node.walk())
    return out
