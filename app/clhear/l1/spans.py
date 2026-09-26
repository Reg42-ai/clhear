# Copyright (C) 2026 Reg42 AI
# This file is part of CLHEAR. See LICENSE (AGPL-3.0-only).
"""Clause span offsets and the normative flag (HLD v2 §4.1).

The *canonical text* of a version is the deterministic concatenation of every
root node's ``subtree_text()`` joined by newlines. Because ``subtree_text``
builds a node's text from ``heading``, ``raw_text`` and the children in order,
each node's text is one contiguous slice of the canonical text; the layout
below reproduces that construction and records ``[start, end)`` per node so
``canonical[start:end] == clause.text`` holds exactly. L2 ``asserts`` cite
these offsets.
"""
import re

from app.clhear.l1.adapters.base import DocNode

SEP = "\n"


def canonical_text(tree: list[DocNode]) -> str:
    return SEP.join(t for t in (root.subtree_text() for root in tree) if t)


def span_layout(tree: list[DocNode]) -> dict[int, tuple[int, int]]:
    """Map ``id(node)`` -> (start, end) offsets in :func:`canonical_text`."""
    spans: dict[int, tuple[int, int]] = {}

    def layout(node: DocNode, start: int) -> int:
        cursor = start
        first = True
        parts: list[str] = []
        if node.heading and "heading" not in node.source_locator.get("presentation_fields", []):
            parts.append(node.heading)
        if node.raw_text:
            parts.append(node.raw_text)
        for part in parts:
            if not first:
                cursor += len(SEP)
            cursor += len(part)
            first = False
        for child in node.children:
            child_text = child.subtree_text()
            if not child_text:
                # Empty subtree contributes nothing; still record a zero-width span.
                spans[id(child)] = (cursor, cursor)
                continue
            if not first:
                cursor += len(SEP)
            cursor = layout(child, cursor)
            first = False
        spans[id(node)] = (start, cursor)
        return cursor

    cursor = 0
    first = True
    for root in tree:
        text = root.subtree_text()
        if not text:
            spans[id(root)] = (cursor, cursor)
            continue
        if not first:
            cursor += len(SEP)
        cursor = layout(root, cursor)
        first = False
    return spans


# --- normative flag ---------------------------------------------------------

# Deontic markers that impose, withdraw or condition an obligation. Applied to
# the clause text only (never headings) so definitions and recitals stay
# non-normative even when they mention "must".
_NORMATIVE = re.compile(
    r"\b(must( not)?|shall( not)?|may not|is (not )?(required|permitted|prohibited|obliged) to|"
    r"are (not )?(required|permitted|prohibited|obliged) to|is prohibited|are prohibited|"
    r"ensure(s)? that|has a duty to|have a duty to|it is an offence|commits an offence|"
    r"is liable|are liable|shall be liable|subject to a penalty)\b",
    re.I,
)
_RECITAL = re.compile(r"^\s*\(\d+\)\s+(whereas|the)\b", re.I)


def is_normative(text: str, *, status_hint: str = "") -> bool:
    """Deterministic normative classifier.

    ``status_hint`` lets publisher-declared rule status win: FCA ``R`` and ``D``
    are rules, ``G``/``E`` are guidance/evidential; ESMA "should" guidelines are
    not normative here (they are guidance the L2 layer weighs separately).
    """
    hint = (status_hint or "").upper()
    if hint in {"R", "D", "RULE", "UK", "EU"}:
        return True
    if hint in {"G", "E", "GUIDANCE", "EVIDENTIAL", "C"}:
        return False
    body = text or ""
    if not body.strip():
        return False
    head = body[:300]
    if _RECITAL.match(head):
        return False
    if _NORMATIVE.search(body):
        return True
    return False


__all__ = ["SEP", "canonical_text", "is_normative", "span_layout"]
