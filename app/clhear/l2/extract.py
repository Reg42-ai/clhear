# Copyright (C) 2026 Reg42 AI
# This file is part of CLHEAR. See LICENSE (AGPL-3.0-only).
"""L2 obligation extraction — deterministic, clause-anchored (HLD principle 2).

One obligation per duty-bearing clause, id = "OBL:{source_key}#{ref}" so the
same corpus always derives the same registry. No LLM in this path: duty
detection is lexical + structural; anything the rules cannot decide simply is
NOT an obligation yet (community/maintainer review can add it later via the
proposals queue). Restricted sources contribute refs + hashes, never text.

Every row stores the basis clause hash; when L1 detects a change on the basis
clause, the obligation is re-derived (or marked stale) on the nightly run.
"""
from __future__ import annotations

import logging
import re
from dataclasses import dataclass

import sqlalchemy as sa
from sqlalchemy.engine import Engine

from app.clhear.derived_models import asserts, obligations
from app.clhear.l1.models import clauses, family_members, source_versions, sources
from app.clhear.platform import record
from app.clhear.platform.ids import next_id

log = logging.getLogger("clhear.l2")

EXTRACTOR_VERSION = "deterministic-v6"

# Duty modality patterns, strongest first. Case-insensitive, matched against
# the clause text. Deliberately conservative: high precision over recall.
MODALITY_PATTERNS: tuple[tuple[str, re.Pattern], ...] = (
    ("must-not", re.compile(r"\b(?:must not|shall not|may not)\b", re.I)),
    ("must", re.compile(r"\bmust\b", re.I)),
    ("shall", re.compile(r"\bshall\b", re.I)),
    ("required", re.compile(r"\b(?:is|are) (?:required|obliged|obligated) to\b", re.I)),
    # Statutory prohibitions: "are hereby declared unlawful", "It shall be unlawful for any investment adviser".
    ("prohibited", re.compile(r"\b(?:is|are) (?:hereby )?(?:declared )?(?:unlawful|prohibited)\b|\bshall be unlawful\b", re.I)),
    ("ensure", re.compile(r"\b(?:is|are) responsible for ensuring\b", re.I)),
)

# Clauses that carry structure, not duties.
NON_DUTY_HEADINGS = re.compile(
    r"\b(?:interpretation|definitions?|citation|commencement|extent|title|scope|"
    r"subject[- ]matter|entry into force|transitional|amendments? to|repeals?|"
    r"short title|signature|annex|recital)\b",
    re.I,
)

# Headings of enforcement machinery: an authority's proceedings, orders,
# hearings and penalties, and the review of them. What they regulate is the
# authority's procedure or the consequence of a breach, not a regulated
# person's conduct ("(b) Proceeding by Commission", "(c) Review of order;
# rehearing", "(l) Penalty for violation of order", "(k) Cease-and-desist
# proceedings").
PROCEDURAL_HEADINGS = re.compile(
    r"\b(?:proceedings?|procedure for|review of (?:an? )?orders?|rehearing|judicial review|service of|"
    r"jurisdiction of|penalt(?:y|ies)|civil actions?|temporary orders?|cease[- ]and[- ]desist|"
    r"notice and (?:opportunity for )?hearing|hearings? (?:before|by)|appeals? (?:against|from|to)|injunctions?)\b"
    r"|^\s*(?:hearings?|appeals?)\s*$",
    re.I,
)

# Definition openers: "When used in this subchapter, unless the context
# otherwise requires—", "As used in this part", "For purposes of this section".
DEFINITION_OPENER = re.compile(
    r"\b(?:when|as) used in this (?:subchapter|chapter|title|part|section|act|regulation)\b"
    r"|\bfor (?:the )?purposes? of this (?:subchapter|chapter|title|part|section|act|regulation)\b[^.;]{0,40}(?:term|means)",
    re.I,
)

# Any modal, strong or weak: the first of them says who the clause addresses.
ANY_MODAL = re.compile(
    r"\b(?:must|shall|may|should|ought to|is required to|are required to|is expected to|are expected to)\b"
    r"|\b(?:is|are) (?:hereby )?(?:declared )?(?:unlawful|prohibited)\b",
    re.I,
)

# A provision whose modal governs a public body — an authority's powers, a
# court's review, a department's procedure — states no duty of the addressees:
# "Whenever the Commission shall have reason to believe ...", "The Secretary
# shall publish ...". The test is grammatical: the words just before the first
# modal name a public body by its generic kind, whatever the sector. So "it
# shall be unlawful for any adviser" and "Member States shall ensure that ..."
# stay duties.
AUTHORITY_SUBJECT = re.compile(
    r"(?:^|[.;:\n)]\s*|\b(?:whenever|where|when|if|unless|and|or|then)\s+)\s*(?:\d+[.)]\s*)?"
    r"(?:the|such|any|each|every|a|an)\s+"
    # Kinds that only ever name a public body, with up to three modifiers ("the competent authority").
    r"(?:(?:[\w\-]+\s+){0,3}?(?:authorit(?:y|ies)|courts?|tribunal|regulator|ombudsman|attorney general)"
    # Words that also name an organisation's own units or charges ("the finance department", "a company
    # secretary", "the commission charged") count only as a defined, capitalised name ("the Department").
    r"|(?-i:(?:[A-Z][\w\-]*\s+){0,3}(?:Commission|Agency|Department|Secretary(?: of State)?|Minister|Ministry|"
    r"Inspectorate|Government)))"
    r"\b(?:\s*,[^.;]{0,40},)?\s*$",
    re.I,
)

# Scope, deeming, construction and penalty-schedule provisions use modals
# without imposing conduct: "The provisions of subsection (a) shall not apply
# to", "shall be deemed", "the maximum amount of penalty ... shall be $5,000".
CONSTRUCTION_SUBJECT = re.compile(
    r"(?:\b(?:the provisions? of|any provision of|nothing in|the (?:maximum )?amount of (?:the )?penalty)\b[^.;]{0,80}"
    # "The term 'personal data' shall include ...", "References in this Act to ... shall be read as": only
    # as the sentence's own subject, so "where the term of the agreement exceeds a year" stays a duty.
    r"|(?:^|[.;:\n)]\s*)(?:\d+[.)]\s*)?(?:the (?:term|expression|word|phrase|definition of)\s+[\"'“‘]?[^.;,]{1,60}"
    r"|references? (?:in this [a-z]+ )?to\b[^.;]{0,80})"
    r"|\bthis (?:subsection|section|paragraph|subparagraph)\s*)$",
    re.I,
)
NON_DUTY_PREDICATE = re.compile(
    r"(?:must|shall|may)\s+(?:not\s+)?(?:apply\s+(?:to|only|in|with respect|where)|be deemed|be construed|be treated|"
    r"be considered|be subject to|"
    r"mean|become final|have no authority|have jurisdiction|in anywise|"
    r"forfeit|be liable (?:for|to)|be fined|be imprisoned|be punished)\b",
    re.I,
)

# Addressee: the noun phrase directly before the first modal.
ADDRESSEE = re.compile(
    r"(?:^|\.\s+)(?:\d+[\.\)]\s*)?(?:\([\w\d]+\)\s*)*(?:each|every|an?|the)\s+"
    r"([A-Za-z][\w\s\-,']{2,80}?)\s+(?:must|shall|may not|is required|are required)",
    re.I,
)

MAX_STATEMENT = 480


@dataclass
class Candidate:
    source_key: str
    ref: str
    title: str
    statement: str
    addressee: str
    modality: str
    confidence: float
    text_hash: str
    public: bool
    clause_id: int | None = None
    clause_text: str = ""
    sentence: str = ""
    own_text: str = ""
    lead_clause: dict | None = None


def _title_from(text: str, ref: str) -> str:
    first = text.strip().split("\n", 1)[0].strip()
    first = re.sub(r"\s+", " ", first)
    if len(first) > 110:
        first = first[:107].rsplit(" ", 1)[0] + "…"
    return first or ref


# "Article 5", "CHAPTER II", "§ 314.4", "Section 3." — the label, not the heading's words.
_HEADING_MARKER = re.compile(
    r"^\s*(?:part|title|chapter|book|annex|schedule|appendix|subpart|division|article|art\.|section|sec\.|§+|rule|"
    r"clause|regulation)\s*(?:[0-9]+(?:\.[0-9]+)*[a-z]?(?:-[0-9]+)?|[ivxlcdm]+\b|[a-z]\b)?[.:]?\s*",
    re.I)


def heading_words(heading: str) -> str:
    """A heading's own words: numbering labels removed, one line at a time."""
    return " ".join(_HEADING_MARKER.sub("", line).strip() for line in (heading or "").split("\n"))


def _leading_heading_line(text: str) -> str:
    """A clause's first line when it is a heading ("(b) Proceeding by Commission"), else ""."""
    body = text.strip()
    if "\n" not in body:
        return ""
    first = body.split("\n", 1)[0].strip()
    return first if len(first) <= 100 and not ANY_MODAL.search(first) else ""


def not_a_duty(text: str, ref: str = "", heading: str = "") -> bool:
    """True for structure, enforcement procedure and construction: clauses whose
    first modal (strong or weak) governs an authority, a court, the reading of the
    text or the penalty for a breach, rather than a regulated person's conduct.

    Heading tests read headings only (the clause's own heading line, or the
    heading of the unit it sits in) — never words in the body, so "to the
    extent possible" or "the scope of processing" do not hide a duty.
    """
    probe = f"{heading_words(heading)} {heading_words(_leading_heading_line(text))}"
    if NON_DUTY_HEADINGS.search(probe) or PROCEDURAL_HEADINGS.search(probe):
        return True
    if DEFINITION_OPENER.search(text[:240]):
        return True
    first = ANY_MODAL.search(text)
    if first is None:
        return False
    subject = text[max(0, first.start() - 160):first.start()]
    return bool(AUTHORITY_SUBJECT.search(subject) or CONSTRUCTION_SUBJECT.search(subject)
                or NON_DUTY_PREDICATE.match(text, first.start()))


def detect_duty(text: str, ref: str, heading: str = "") -> tuple[str, float] | None:
    """Return (modality, confidence) when the clause imposes a duty."""
    if not text or len(text.strip()) < 40:
        return None
    if not_a_duty(text, ref, heading):
        return None
    first = min((m for _, pattern in MODALITY_PATTERNS if (m := pattern.search(text))), key=lambda m: m.start(), default=None)
    if first is not None:
        subject = text[max(0, first.start() - 160):first.start()]
        if (AUTHORITY_SUBJECT.search(subject) or CONSTRUCTION_SUBJECT.search(subject)
                or NON_DUTY_PREDICATE.match(text, first.start())):
            return None
    for modality, pattern in MODALITY_PATTERNS:
        match = pattern.search(text)
        if not match:
            continue
        confidence = 0.85 if modality in ("must", "must-not", "prohibited") else 0.75
        # Duty stated early in the clause is a stronger signal than one buried
        # in a proviso; definitions sneak modals into subordinate positions.
        if match.start() > len(text) * 0.6:
            confidence -= 0.15
        if len(text) < 120:
            confidence -= 0.1
        return modality, round(confidence, 2)
    return None


def container_clause_ids(conn, source_version_id: int) -> set[int]:
    """Clauses that contain other clauses (a section wrapping provisions).
    Their text is the concatenation of their children, so a duty detected in
    them is the child's duty: obligations anchor to the atomic (leaf) clause."""
    from app.clhear.l1.models import doc_nodes

    parents = {
        r.id: r.parent_id
        for r in conn.execute(sa.select(doc_nodes.c.id, doc_nodes.c.parent_id).where(doc_nodes.c.source_version_id == source_version_id))
    }
    clause_nodes = {
        r.doc_node_id: r.id
        for r in conn.execute(sa.select(clauses.c.id, clauses.c.doc_node_id).where(clauses.c.source_version_id == source_version_id))
        if r.doc_node_id is not None
    }
    containers: set[int] = set()
    for node_id in clause_nodes:
        parent = parents.get(node_id)
        while parent is not None:
            if parent in clause_nodes:
                containers.add(clause_nodes[parent])
            parent = parents.get(parent)
    return containers


def clause_contexts(conn, source_version_id: int) -> dict[int, dict]:
    """Per clause: the heading of the unit it sits in, and a lead-in it continues.

    A list item "(a) processed lawfully" completes its parent "Personal data
    shall be:"; the duty is read from both. The heading is the nearest
    ancestor heading ("Article 32 Security of processing").
    """
    from app.clhear.l1.models import doc_nodes

    nodes = {r.id: r for r in conn.execute(
        sa.select(doc_nodes.c.id, doc_nodes.c.parent_id, doc_nodes.c.heading, doc_nodes.c.raw_text)
        .where(doc_nodes.c.source_version_id == source_version_id))}
    rows = conn.execute(sa.select(clauses.c.id, clauses.c.doc_node_id, clauses.c.ref, clauses.c.text)
                        .where(clauses.c.source_version_id == source_version_id)).all()
    by_node = {r.doc_node_id: r for r in rows if r.doc_node_id is not None}
    out: dict[int, dict] = {}
    for row in rows:
        node = nodes.get(row.doc_node_id)
        if node is None:
            continue
        heading, lead, lead_clause, parent = "", "", None, nodes.get(node.parent_id)
        if parent is not None and (parent.raw_text or "").rstrip().endswith((":", "—", "-")):
            lead = " ".join(parent.raw_text.split())
            owner = by_node.get(parent.id)
            if owner is not None:
                lead_clause = {"id": owner.id, "ref": owner.ref, "text": owner.text or ""}
        cursor = parent
        while cursor is not None and not heading:
            heading = cursor.heading or ""
            cursor = nodes.get(cursor.parent_id)
        out[row.id] = {"heading": heading, "lead": lead, "lead_clause": lead_clause}
    return out


def duty_text(text: str, context: dict | None) -> str:
    lead = (context or {}).get("lead") or ""
    return f"{lead} {text}" if lead else text


_ITEM_MARKER = re.compile(r"^\s*(?:\((?:[0-9]{1,3}[a-z]?|[a-z]{1,2}|[ivxlc]{1,6})\)|[a-z]\)|[-•*–])\s*", re.I)


def sentence_text(text: str, context: dict | None) -> str:
    """One readable sentence for a list item: "Personal data shall be processed lawfully ..."."""
    lead = (context or {}).get("lead") or ""
    if not lead:
        return text
    return f"{lead.rstrip(' :—-')} {_ITEM_MARKER.sub('', text.strip())}"


def extract_source(engine: Engine, source_row, version_row) -> list[Candidate]:
    """Candidates for one in-force source version. Binding tier only; atomic
    (leaf) clauses only — see :func:`container_clause_ids`."""
    open_source = source_row.license == "open"
    out: list[Candidate] = []
    with engine.connect() as conn:
        rows = conn.execute(
            sa.select(clauses)
            .where(clauses.c.source_version_id == version_row.id)
            .order_by(clauses.c.ordering)
        ).all()
        containers = container_clause_ids(conn, version_row.id)
        contexts = clause_contexts(conn, version_row.id)
    for row in rows:
        if row.id in containers:
            continue
        if not open_source or not row.public_ok:
            # Restricted: we cannot inspect text; no machine derivation.
            continue
        context = contexts.get(row.id) or {}
        own = row.text or ""
        text = duty_text(own, context)
        duty = detect_duty(text, row.ref or "", context.get("heading", ""))
        if duty is None:
            continue
        modality, confidence = duty
        addressee_match = ADDRESSEE.search(text)
        statement = re.sub(r"\s+", " ", text).strip()
        if len(statement) > MAX_STATEMENT:
            statement = statement[: MAX_STATEMENT - 1].rsplit(" ", 1)[0] + "…"
        out.append(
            Candidate(
                source_key=source_row.key,
                ref=row.ref or f"clause-{row.ordering}",
                title=_title_from(own, row.ref or ""),
                statement=statement,
                addressee=(addressee_match.group(1).strip() if addressee_match else ""),
                modality=modality,
                confidence=confidence,
                text_hash=row.text_hash,
                public=True,
                clause_id=row.id,
                clause_text=text,
                sentence=sentence_text(own, context),
                own_text=own,
                lead_clause=context.get("lead_clause"),
            )
        )
    return out


def _evidence(cand: Candidate, structured: dict) -> dict:
    """Quotes for the duty and each structured field (app.clhear.evidence)."""
    from app.clhear import evidence
    from app.clhear.l2 import registry

    own = {"id": cand.clause_id, "source_key": cand.source_key, "ref": cand.ref, "text": cand.own_text}
    found = [own]
    lead = None
    if cand.lead_clause:
        lead_row = {**cand.lead_clause, "source_key": cand.source_key}
        found.append(lead_row)
        lead = evidence.whole(lead_row)
    span = registry.duty_span(cand.own_text) if cand.own_text else None
    if span is None and cand.own_text.strip():
        duty = evidence.whole(own)
    elif span is not None:
        duty = {**evidence.whole(own), "start": span[0], "end": span[1], "quote": cand.own_text[span[0]:span[1]]}
    else:
        duty = None
    return {"duty": duty, "lead": lead, **registry.structure_evidence(structured, found)}


def _release_derived(conn, oid: str, trail: str) -> None:
    """The duty's words changed: the measures and characteristics read from the old words are
    closed, so L3 derives them again from the new ones."""
    from app.clhear.derived_models import characteristics, requires

    record.invalidate(conn, requires, sa.and_(requires.c.obligation_id == oid, requires.c.valid_to.is_(None)),
                      why=trail, reason="the duty's words changed")
    record.invalidate(conn, characteristics, sa.and_(characteristics.c.backing_obligation_id == oid,
                                                     characteristics.c.valid_to.is_(None)),
                      why=trail, reason="the duty's words changed")


def _in_force_clauses(conn, keys: list[str]) -> dict[tuple[str, str], dict]:
    """(source key, ref) -> the in-force clause with its lead-in, for the given sources."""
    if not keys:
        return {}
    out: dict[tuple[str, str], dict] = {}
    versions = conn.execute(
        sa.select(source_versions.c.id, sources.c.key).join(sources, source_versions.c.source_id == sources.c.id)
        .where(sources.c.key.in_(keys), source_versions.c.status == "in_force")).all()
    for version_id, key in versions:
        contexts = clause_contexts(conn, version_id)
        for r in conn.execute(sa.select(clauses.c.id, clauses.c.ref, clauses.c.text, clauses.c.text_hash)
                              .where(clauses.c.source_version_id == version_id)):
            out[(key, r.ref)] = {"id": r.id, "text": r.text or "", "text_hash": r.text_hash,
                                 "lead_clause": (contexts.get(r.id) or {}).get("lead_clause")}
    return out


def _keep_triaged(conn, row, clause: dict) -> None:
    """A triaged duty whose clause is unchanged follows the in-force copy of that clause, and
    gets its quotes if it was derived before quotes existed."""
    from types import SimpleNamespace

    from app.clhear.l2 import registry

    registry.upsert_assert(conn, obligation_id=row.id, clause_id=clause["id"], source_key=row.source_key,
                           clause_ref=row.clause_ref, text=clause["text"], text_hash=row.text_hash,
                           strength="implied", why=row.why_trail_id or why_id(conn, row.id))
    found = row.evidence if isinstance(row.evidence, dict) else {}
    if found.get("duty") and found["duty"].get("clause_id") == clause["id"]:
        return
    fields = {k: getattr(row, k) or "" for k in ("subject", "action", "condition", "object", "obligation_type")}
    fresh = _evidence(SimpleNamespace(clause_id=clause["id"], source_key=row.source_key, ref=row.clause_ref,
                                      own_text=clause["text"], lead_clause=clause["lead_clause"]), fields)
    conn.execute(obligations.update().where(obligations.c.id == row.id).values(evidence={**found, **fresh}))


def obligation_id(source_key: str, ref: str) -> str:
    return f"OBL:{source_key}#{ref}"


def registry_next_id(conn) -> str:
    return next_id(conn, "OBL")


def why_id(conn, oid: str) -> str:
    """The why-trail id the obligation row was just written with (edges and
    change events of the same derivation share it)."""
    return conn.execute(sa.select(obligations.c.why_trail_id).where(obligations.c.id == oid)).scalar_one()


def _stale_not_binding(engine: Engine, source_key: str) -> int:
    """A source that is not binding (an enforcement source or a register) states no
    obligations: those derived from it before it was registered as such go stale."""
    from app.clhear.l2 import registry

    staled = 0
    with engine.begin() as conn:
        live = conn.execute(sa.select(obligations).where(obligations.c.source_key == source_key,
                                                         obligations.c.status.in_(("derived", "validated")))).all()
        for row in live:
            trail = registry.why_for(row.id, clause_id=None, text_hash=row.text_hash, method=EXTRACTOR_VERSION,
                                     confidence=None, summary="source is not binding (an enforcement source or a "
                                     "register): obligation revoked (stale)").write(conn)
            conn.execute(obligations.update().where(obligations.c.id == row.id)
                         .values(status="stale", why_trail_id=trail, version=(row.version or 1) + 1))
            record.invalidate(conn, asserts, sa.and_(asserts.c.obligation_id == row.id, asserts.c.valid_to.is_(None)),
                              why=trail, reason="source is not binding")
            registry.record_change(conn, obligation_id=row.id, kind="revoked", cause_clause_ids=[],
                                   source_key=source_key, old_text_hash=row.text_hash, effective_date=None,
                                   effective_date_basis="none", why=trail)
            staled += 1
    return staled


def run_extraction(engine: Engine, source_key: str | None = None) -> dict:
    """(Re-)derive the obligation registry. Idempotent: deterministic ids;
    unchanged basis hash + same extractor version = untouched row (validated
    rows keep their status); changed basis = re-derived + status reset to
    `derived`; vanished basis = status `stale`."""
    themes_by_source: dict[str, list] = {}
    inserted = updated = unchanged = staled = 0
    with engine.connect() as conn:
        from app.clhear.l1.scopes import limiting

        src_q = sa.select(sources)
        limit = limiting(sources.c.key, source_key)
        if limit is not None:
            src_q = src_q.where(limit)
        source_rows = conn.execute(src_q).all()
        binding = {
            row.source_id
            for row in conn.execute(sa.select(family_members).where(family_members.c.tier == "binding"))
        }
        versions = {
            v.source_id: v
            for v in conn.execute(
                sa.select(source_versions).where(source_versions.c.status == "in_force").order_by(source_versions.c.id)
            )
        }
        for s in source_rows:
            themes_by_source[s.key] = s.topics if isinstance(s.topics, list) else []

    all_candidates: list[Candidate] = []
    scoped_keys: list[str] = []
    for s in source_rows:
        if s.id not in binding or s.id not in versions:
            continue
        scoped_keys.append(s.key)
        all_candidates.extend(extract_source(engine, s, versions[s.id]))
    if source_key and not scoped_keys:
        # A scoped run whose source is not binding / has no in-force version must
        # not fall through to the unscoped path and stale every other source (I2).
        staled = _stale_not_binding(engine, source_key) if any(
            s.key == source_key and s.id in versions and s.id not in binding for s in source_rows) else 0
        return {"extractor": EXTRACTOR_VERSION, "sources_scanned": 0, "candidates": 0, "inserted": 0,
                "re_derived": 0, "unchanged": 0, "stale": staled, "skipped": source_key}

    jurisdictions = {s.key: s.jurisdiction for s in source_rows}
    regulators = {s.key: s.issuer for s in source_rows}
    version_labels = {s.key: versions[s.id].version_label for s in source_rows if s.id in versions}
    version_as_of = {s.key: versions[s.id].as_of_date for s in source_rows if s.id in versions}

    from app.clhear.l1.models import change_events as l1_change_events
    from app.clhear.l2 import registry

    with engine.begin() as conn:
        # Latest L1 change per source: the L2 change inherits its effective date.
        l1_latest: dict[str, object] = {}
        for src in source_rows:
            if src.key not in scoped_keys:
                continue
            row = conn.execute(
                sa.select(l1_change_events)
                .where(l1_change_events.c.source_id == src.id)
                .order_by(l1_change_events.c.id.desc())
                .limit(1)
            ).first()
            if row is not None:
                l1_latest[src.key] = row
        existing = {
            row.id: row
            for row in conn.execute(
                sa.select(obligations).where(obligations.c.source_key.in_(scoped_keys))
                if scoped_keys
                else sa.select(obligations)
            )
        }
        seen: set[str] = set()
        for cand in all_candidates:
            oid = obligation_id(cand.source_key, cand.ref)
            seen.add(oid)
            row = existing.get(oid)
            l1_change = l1_latest.get(cand.source_key)
            effective = getattr(l1_change, "effective_date", None) or version_as_of.get(cand.source_key)
            effective_basis = getattr(l1_change, "effective_date_basis", "") or ("publisher" if effective else "none")
            structured = registry.structured_fields(cand.sentence or cand.clause_text or cand.statement, cand.modality)
            values = dict(evidence=_evidence(cand, structured), 
                source_key=cand.source_key,
                clause_ref=cand.ref,
                title=cand.title,
                statement=cand.statement,
                addressee=cand.addressee,
                modality=cand.modality,
                jurisdiction=jurisdictions.get(cand.source_key, ""),
                jurisdictions=[jurisdictions.get(cand.source_key, "")] if jurisdictions.get(cand.source_key) else [],
                regulator=regulators.get(cand.source_key, "") or "",
                themes=themes_by_source.get(cand.source_key, []),
                confidence=cand.confidence,
                method=EXTRACTOR_VERSION,
                text_hash=cand.text_hash,
                source_version_label=version_labels.get(cand.source_key, ""),
                effective_from=effective,
                **structured,
            )
            if row is None:
                why = registry.why_for(
                    oid, clause_id=cand.clause_id, text_hash=cand.text_hash, method=EXTRACTOR_VERSION,
                    confidence=cand.confidence,
                    summary=f"deterministic duty ({cand.modality}) in {cand.source_key} {cand.ref}",
                )
                record.write(
                    conn, obligations,
                    {"id": oid, "status": "derived", "stable_id": registry_next_id(conn), **values},
                    why=why, valid_from=effective,
                )
                if cand.clause_id is not None:
                    registry.upsert_assert(
                        conn, obligation_id=oid, clause_id=cand.clause_id, source_key=cand.source_key,
                        clause_ref=cand.ref, text=cand.own_text, text_hash=cand.text_hash,
                        strength="explicit", why=why_id(conn, oid),
                    )
                registry.record_change(
                    conn, obligation_id=oid, kind="added",
                    cause_clause_ids=[cand.clause_id] if cand.clause_id is not None else [],
                    source_key=cand.source_key, new_text_hash=cand.text_hash,
                    effective_date=effective, effective_date_basis=effective_basis,
                    cause_l1_change_event_id=getattr(l1_change, "id", None),
                    why=why_id(conn, oid),
                )
                inserted += 1
            elif (duty_changed := (row.text_hash != cand.text_hash or (row.statement or "") != cand.statement
                                   or (row.modality or "") != cand.modality)) or row.method != EXTRACTOR_VERSION:
                # Basis clause (or the lead-in it continues) changed, or the extractor upgraded: re-derive.
                why = registry.why_for(
                    oid, clause_id=cand.clause_id, text_hash=cand.text_hash, method=EXTRACTOR_VERSION,
                    confidence=cand.confidence,
                    summary=f"basis clause changed ({row.text_hash[:8]} -> {cand.text_hash[:8]}): re-derived",
                )
                trail = why.write(conn)
                conn.execute(
                    obligations.update()
                    .where(obligations.c.id == oid)
                    .values(status="derived", validated_by=None, validated_at=None, why_trail_id=trail,
                            version=(row.version or 1) + 1, review_confidence=None, **values)
                )
                if cand.clause_id is not None:
                    registry.upsert_assert(
                        conn, obligation_id=oid, clause_id=cand.clause_id, source_key=cand.source_key,
                        clause_ref=cand.ref, text=cand.own_text, text_hash=cand.text_hash,
                        strength="explicit", why=trail,
                    )
                if duty_changed:
                    _release_derived(conn, oid, trail)
                    registry.record_change(
                        conn, obligation_id=oid, kind="updated",
                        cause_clause_ids=[cand.clause_id] if cand.clause_id is not None else [],
                        source_key=cand.source_key, old_text_hash=row.text_hash, new_text_hash=cand.text_hash,
                        effective_date=effective, effective_date_basis=effective_basis,
                        cause_l1_change_event_id=getattr(l1_change, "id", None),
                        detail={"old_version": row.source_version_label, "new_version": version_labels.get(cand.source_key, "")},
                        why=trail,
                    )
                updated += 1
            else:
                if not row.stable_id:
                    registry.ensure_stable_id(conn, oid)
                # Same words, possibly a new L1 version: the edge and the quotes follow the in-force clause.
                if cand.clause_id is not None:
                    registry.upsert_assert(
                        conn, obligation_id=oid, clause_id=cand.clause_id, source_key=cand.source_key,
                        clause_ref=cand.ref, text=cand.own_text, text_hash=cand.text_hash,
                        strength="explicit", why=row.why_trail_id or why_id(conn, oid),
                    )
                if row.evidence != values["evidence"]:
                    conn.execute(obligations.update().where(obligations.c.id == oid).values(evidence=values["evidence"]))
                unchanged += 1
        in_force = _in_force_clauses(conn, scoped_keys)
        for oid, row in existing.items():
            clause = in_force.get((row.source_key, row.clause_ref))
            if (row.method or "") and not str(row.method).startswith("deterministic") and clause is not None \
                    and clause["text_hash"] == row.text_hash:
                # Found by triage and its own clause is still in force: not this extractor's to revoke.
                _keep_triaged(conn, row, clause)
                continue
            if oid not in seen and row.status != "stale":
                l1_change = l1_latest.get(row.source_key)
                effective = getattr(l1_change, "effective_date", None) or version_as_of.get(row.source_key)
                why = registry.why_for(
                    oid, clause_id=None, text_hash=row.text_hash, method=EXTRACTOR_VERSION, confidence=None,
                    summary="basis clause no longer in force: obligation revoked (stale)",
                )
                trail = why.write(conn)
                conn.execute(
                    obligations.update().where(obligations.c.id == oid)
                    .values(status="stale", effective_to=effective, why_trail_id=trail, version=(row.version or 1) + 1)
                )
                record.invalidate(
                    conn, asserts, sa.and_(asserts.c.obligation_id == oid, asserts.c.valid_to.is_(None)),
                    why=trail, reason="basis clause gone", valid_to=effective,
                )
                registry.record_change(
                    conn, obligation_id=oid, kind="revoked", cause_clause_ids=[],
                    source_key=row.source_key, old_text_hash=row.text_hash,
                    effective_date=effective,
                    effective_date_basis=getattr(l1_change, "effective_date_basis", "") or ("publisher" if effective else "none"),
                    cause_l1_change_event_id=getattr(l1_change, "id", None),
                    why=trail,
                )
                staled += 1
    summary = {
        "extractor": EXTRACTOR_VERSION,
        "sources_scanned": len(scoped_keys),
        "candidates": len(all_candidates),
        "inserted": inserted,
        "re_derived": updated,
        "unchanged": unchanged,
        "stale": staled,
    }
    log.info("L2 extraction: %s", summary)
    return summary
