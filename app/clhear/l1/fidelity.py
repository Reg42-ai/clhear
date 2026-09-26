# Copyright (C) 2026 Reg42 AI
# This file is part of CLHEAR. See LICENSE (AGPL-3.0-only).
"""Fidelity gate for the adapter fleet (E2/E3 pulled forward; HLD principle 5:
evals are gates, not reports).

Root-cause machinery against silent text loss:

- Every adapter provides an `expected_text(artifacts) -> list[str]` ORACLE —
  the complete ordered visible text of the artifact, extracted trivially (all
  text nodes minus declared exclusions). It is deliberately dumb so it cannot
  share bugs with the structural parser.
- `check()` measures token coverage of the oracle spans by the DocNode tree
  and lints contract invariants (no label duplicated in text, unique refs,
  non-empty tree, clause-grain nodes have refs).
- `apply_hints()` deterministically recovers missing spans as PROPERLY TYPED
  nodes using learned/LLM-proposed parse hints. `salvage()` recovers small
  residual gaps as flagged note nodes. In both cases the recovered text IS the
  artifact text (spans come from the oracle) — nothing is ever generated.
"""
from dataclasses import dataclass, field

from app.clhear.l1.adapters.base import CLAUSE_TYPES, DocNode, flatten

SALVAGE_REF = "fidelity-recovered"


def ws(text: str) -> str:
    return " ".join(text.split())


@dataclass
class FidelityReport:
    coverage: float
    total_tokens: int
    missing_spans: list[str] = field(default_factory=list)  # original (un-normalized) spans
    violations: list[str] = field(default_factory=list)

    def ok(self, threshold: float) -> bool:
        return self.coverage >= threshold and not self.violations

    def summary(self) -> dict:
        return {
            "coverage": round(self.coverage, 5),
            "missing_spans": len(self.missing_spans),
            "missing_preview": [ws(s)[:120] for s in self.missing_spans[:8]],
            "violations": self.violations[:8],
        }


def tree_text(tree: list[DocNode]) -> str:
    """Whitespace-normalized concatenation of every node's label/heading/text."""
    parts: list[str] = []
    for node in flatten(tree):
        for piece in (node.label, node.heading, node.raw_text):
            normalized = ws(piece)
            if normalized:
                parts.append(normalized)
    return " ".join(parts)


def lint(tree: list[DocNode]) -> list[str]:
    """Deterministic contract invariants over the DocNode tree."""
    violations: list[str] = []
    nodes = flatten(tree)
    if not nodes:
        return ["empty tree"]
    seen_refs: set[str] = set()

    def _dup(text: str, label: str) -> bool:
        """True when text starts with the label as a whole token (not a mere
        alphanumeric prefix — 'IDENTIFY' does not duplicate label 'ID')."""
        normalized = ws(text)
        if not normalized.startswith(label):
            return False
        rest = normalized[len(label) :]
        return not rest[:1].isalnum()

    for node in nodes:
        label = ws(node.label)
        if label:
            if _dup(node.raw_text, label):
                violations.append(f"label duplicated in raw_text: {node.ref or node.node_type} '{label}'")
            if _dup(node.heading, label) and ws(node.heading) != label:
                violations.append(f"label duplicated in heading: {node.ref or node.node_type} '{label}'")
        if node.ref:
            if node.ref in seen_refs:
                violations.append(f"duplicate ref: {node.ref}")
            seen_refs.add(node.ref)
        elif node.node_type in CLAUSE_TYPES:
            violations.append(f"clause-grain node without ref: {node.node_type}")
    return violations


def check(tree: list[DocNode], expected_spans: list[str]) -> FidelityReport:
    """Token coverage of the oracle spans by the tree + invariant lint."""
    haystack = f" {tree_text(tree)} "
    # Structured feeds contain many repeated metadata fields. Exact field
    # membership avoids repeatedly scanning a multi-megabyte document while
    # retaining the same diagnostic semantics. The independent originals gate
    # separately requires ordered multiplicity and complete structure.
    field_values = {ws(piece) for node in flatten(tree) for piece in (node.label, node.heading, node.raw_text) if piece}
    total_tokens = 0
    missing: list[str] = []
    missing_tokens = 0
    for span in expected_spans:
        normalized = ws(span)
        if not normalized:
            continue
        tokens = len(normalized.split())
        total_tokens += tokens
        if normalized not in field_values and f" {normalized} " not in haystack and normalized not in haystack:
            missing.append(span)
            missing_tokens += tokens
    coverage = 0.0 if total_tokens == 0 else 1.0 - (missing_tokens / total_tokens)
    return FidelityReport(
        coverage=coverage,
        total_tokens=total_tokens,
        missing_spans=missing,
        violations=lint(tree) + (["empty original text oracle"] if total_tokens == 0 else []),
    )


def span_tokens(spans: list[str]) -> int:
    return sum(len(ws(s).split()) for s in spans if ws(s))


def apply_hints(
    tree: list[DocNode], missing_spans: list[str], hints: list[dict]
) -> tuple[list[str], list[int]]:
    """Recover missing spans as typed nodes per hints, IN PLACE on the tree.

    A hint is `{"match": <substring of the span>, "node_type": …, "label": …,
    "ref": …, "hint_id": …}`. The recovered node's text is the span itself —
    verbatim artifact text; the hint only classifies it. Returns
    (remaining_spans, used_hint_ids).
    """
    from app.clhear.l1.models import NODE_TYPES

    remaining: list[str] = []
    used: list[int] = []
    recovered: list[DocNode] = []
    for span in missing_spans:
        normalized = ws(span)
        hit = None
        for hint in hints:
            match = ws(str(hint.get("match", "")))
            if match and match in normalized and hint.get("node_type") in NODE_TYPES:
                hit = hint
                break
        if hit is None:
            remaining.append(span)
            continue
        label = str(hit.get("label", ""))
        text = span
        if label and ws(text).startswith(ws(label)):
            # keep verbatim reconstruction (label + rest == span) without duplication
            text = ws(text)[len(ws(label)) :].lstrip()
        recovered.append(
            DocNode(
                node_type=str(hit["node_type"]),
                ref=str(hit.get("ref", "")),
                label=label,
                raw_text=text,
                source_fragment="",
            )
        )
        if hit.get("hint_id") is not None:
            used.append(int(hit["hint_id"]))
    if recovered:
        tree.extend(recovered)
    return remaining, sorted(set(used))


def salvage(tree: list[DocNode], missing_spans: list[str]) -> int:
    """Recover residual spans as flagged note nodes, IN PLACE. Returns count."""
    if not missing_spans:
        return 0
    container = next((n for n in tree if n.ref == SALVAGE_REF), None)
    if container is None:
        container = DocNode(
            node_type="note",
            ref=SALVAGE_REF,
            heading="Recovered content (fidelity salvage — pending adapter fix)",
        )
        tree.append(container)
    for span in missing_spans:
        container.children.append(DocNode(node_type="note", raw_text=span))
    return len(missing_spans)
