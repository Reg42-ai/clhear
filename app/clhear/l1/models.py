# Copyright (C) 2026 Reg42 AI
# This file is part of CLHEAR. See LICENSE (AGPL-3.0-only).
"""l1_sources schema (HLD §6.2) as SQLAlchemy Core metadata.

Same pattern as the l0 models: the metadata carries the `l1_sources` schema
name; on SQLite the engine translates the schema away.
"""
import uuid

import sqlalchemy as sa
from sqlalchemy.dialects.postgresql import JSONB

L1_SCHEMA = "l1_sources"

metadata = sa.MetaData(schema=L1_SCHEMA)

Json = sa.JSON().with_variant(JSONB(), "postgresql")

BigId = sa.BigInteger().with_variant(sa.Integer, "sqlite")


def _uuid() -> str:
    return str(uuid.uuid4())


source_families = sa.Table(
    "source_families",
    metadata,
    sa.Column("id", BigId, sa.Identity(), primary_key=True),
    sa.Column("key", sa.Text, nullable=False, unique=True),
    sa.Column("name", sa.Text, nullable=False),
    sa.Column("scope_charter", Json, nullable=False, default=dict),
)

sources = sa.Table(
    "sources",
    metadata,
    sa.Column("id", BigId, sa.Identity(), primary_key=True),
    sa.Column("family_id", BigId, sa.ForeignKey(f"{L1_SCHEMA}.source_families.id"), nullable=False),
    sa.Column("key", sa.Text, nullable=False, unique=True),
    sa.Column("name", sa.Text, nullable=False),
    sa.Column(
        "kind",
        sa.Text,
        sa.CheckConstraint(
            "kind in ('law','regulation','standard','guidance','form','agreement','enforcement')",
            name="sources_kind_check",
        ),
        nullable=False,
    ),
    sa.Column("issuer", sa.Text, nullable=False, default=""),
    sa.Column("jurisdiction", sa.Text, nullable=False, default=""),
    sa.Column(
        "license",
        sa.Text,
        sa.CheckConstraint("license in ('open','restricted')", name="sources_license_check"),
        nullable=False,
        default="open",
    ),
    sa.Column("license_ref", sa.Text, nullable=False, default=""),
    sa.Column("adapter", sa.Text, nullable=False, default=""),
    sa.Column("canonical_url", sa.Text, nullable=False, default=""),
    # HLD v2 §4.1: rights basis decides what CLHEAR may republish; recorded by
    # the rights recorder (l1/rights.py) with evidence in `rights_records`.
    sa.Column(
        "rights_basis",
        sa.Text,
        sa.CheckConstraint(
            "rights_basis in ('public_domain','open_licence','licensed','byol_only','derived_only')",
            name="sources_rights_basis_check",
        ),
        nullable=False,
        default="open_licence",
        server_default="open_licence",
    ),
    sa.Column("publisher", sa.Text, nullable=False, default="", server_default=""),
    sa.Column("instrument", sa.Text, nullable=False, default="", server_default=""),
    sa.Column("family_root", sa.Boolean, nullable=False, default=False, server_default=sa.false()),
    # Curated semantic context (authored in the adapter's SourceMeta, code-
    # reviewed — deterministic, zero LLM). Generated semantics belong to the
    # clause_annotations table, never here.
    sa.Column("short_name", sa.Text, nullable=False, default=""),
    sa.Column("about", sa.Text, nullable=False, default=""),
    sa.Column("topics", Json, nullable=False, default=list),
)

family_members = sa.Table(
    "family_members",
    metadata,
    sa.Column("family_id", BigId, sa.ForeignKey(f"{L1_SCHEMA}.source_families.id"), primary_key=True),
    sa.Column("source_id", BigId, sa.ForeignKey(f"{L1_SCHEMA}.sources.id"), primary_key=True),
    sa.Column(
        "relation",
        sa.Text,
        sa.CheckConstraint(
            "relation in ('root','amends','consolidates','corrects','supplements','interprets','implements')",
            name="family_members_relation_check",
        ),
        nullable=False,
    ),
    sa.Column(
        "tier",
        sa.Text,
        sa.CheckConstraint("tier in ('binding','guidance','informative')", name="family_members_tier_check"),
        nullable=False,
        default="binding",
    ),
    sa.Column(
        "status",
        sa.Text,
        sa.CheckConstraint("status in ('active','superseded')", name="family_members_status_check"),
        nullable=False,
        default="active",
    ),
    sa.Column(
        "added_via",
        sa.Text,
        sa.CheckConstraint(
            "added_via in ('citator','citation','watchlist','manual')", name="family_members_added_via_check"
        ),
        nullable=False,
        default="manual",
    ),
)

source_versions = sa.Table(
    "source_versions",
    metadata,
    sa.Column("id", BigId, sa.Identity(), primary_key=True),
    sa.Column("source_id", BigId, sa.ForeignKey(f"{L1_SCHEMA}.sources.id"), nullable=False),
    sa.Column("version_label", sa.Text, nullable=False),
    sa.Column(
        "version_kind",
        sa.Text,
        sa.CheckConstraint(
            "version_kind in ('as_published','consolidated','edition')",
            name="source_versions_kind_check",
        ),
        nullable=False,
        default="consolidated",
    ),
    # The date the text STATE represents (consolidation date, OJ publication
    # date, made date) — distinct from retrieved_at (when we fetched it).
    sa.Column("as_of_date", sa.Date, nullable=True),
    sa.Column("effective_date", sa.Date, nullable=True),
    sa.Column("retrieved_at", sa.DateTime(timezone=True), server_default=sa.func.now()),
    sa.Column("s3_uri", sa.Text, nullable=False, default=""),
    sa.Column("content_hash", sa.Text, nullable=False),
    sa.Column(
        "status",
        sa.Text,
        sa.CheckConstraint("status in ('in_force','superseded','revoked')", name="source_versions_status_check"),
        nullable=False,
        default="in_force",
    ),
    sa.UniqueConstraint("source_id", "version_label", name="source_versions_source_version_key"),
)

# Typed raw document-node tree (P1 redesign). One record per structural
# element in its rawest form; the Explorer reconstructs the original document
# from these rows. `clauses` is the provision-level projection used by the
# diff engine, search, and future L2 obligation anchors.
# The standardized version model: every publisher's versioning reduces to
# three kinds; a source carries whichever subset exists (declared per adapter
# in SourceMeta.version_policy). Currency is a STATUS (source_versions.status
# in_force/superseded), kind is a DESCRIPTOR of the text state — never
# conflate them. Definitions are served to the UI via /api/clhear/meta.
VERSION_KINDS = {
    "as_published": {
        "label": "as published",
        "definition": (
            "An original publication, such as an EU Official Journal act or UK instrument "
            "as made. It can include preambles and recitals absent from later consolidated "
            "texts. Import accuracy and edition identity require separate verification."
        ),
    },
    "consolidated": {
        "label": "consolidated",
        "definition": (
            "A publisher's compilation incorporating amendments for a stated date. It may "
            "omit preambles or recitals. This classification does not establish legal "
            "effect, currentness or successful verification of the stored text."
        ),
    },
    "edition": {
        "label": "edition",
        "definition": (
            "A numbered or dated release of the entire text (U.S. Code 2023 edition, NIST "
            "SP 800-53 rev 5.2.0). Applicability and replacement of earlier editions depend "
            "on the publisher's recorded publication and effective-date evidence."
        ),
    },
}

# Plain-language definitions of every pipeline stage (Fleet view education;
# served via /api/clhear/meta). Every stage the pipeline can emit is here.
STAGE_INFO = {
    "fetch": "Download the official artifact from the publisher (polite client: identifying user-agent, backoff, caching — never hammering official endpoints).",
    "parse": "Structural parse of the artifact into typed document nodes (parts, chapters, articles, paragraphs …) with the text kept verbatim.",
    "gate": "Verify complete ordered original text and the expected encoded structure under the recorded normalization policy, alongside coverage and contract checks. Unresolved discrepancies block verified publication.",
    "hints": "Apply parse fixes the fleet learned on earlier runs (deterministic — no AI involved). Each hint was gate-validated when first learned.",
    "llm_repair": "AI escalation for novel gaps: the model proposes how to classify missed spans; recovered text still comes only from the artifact, and the gate re-validates everything.",
    "salvage": "Legacy recovery diagnostics for missed spans. Salvage cannot bypass mandatory original-text and structure verification or establish corpus acceptance.",
    "persist": "Write the new version, its document nodes and the clause projection to the corpus in one transaction.",
    "annotate": "Deterministically classify every clause (definition, requirement, enforcement, other) and inherit topic tags from the curated source metadata — the orientation layer for readers.",
    "index": "Build the hybrid search units: each clause in distilled form (short name + path + classification + text) plus substantial paragraphs with their clause heading — the corpus becomes findable by citation, exact tokens, or plain words.",
    "citations": "Mine every clause for references to other instruments (EU acts, UK SIs/Acts, US CFR/USC); resolve them to family members, mark cross-family references, and file unknown instruments as discovery candidates for a human — the family-completeness signal.",
    "diff": "Clause-level comparison against the previous version (aligned by stable references) producing the change event.",
    "relay": "Ship the recorded change events from the transactional outbox to the SQS event queue.",
    "drain": "Consume the queued events worker-style (idempotent on event id), leaving the queue clean.",
}

# The fleet's automatic schedules. Mirrors infra/eventbridge.tf (EventBridge
# cron rules -> SQS AdapterRunRequested -> clhear-workers). Times are UTC;
# the UI shows this dictionary verbatim so users know when fresh text lands.
def _daily(covers: str) -> dict:
    return {"cadence": "daily", "utc_time": "00:00", "cron": "cron(0 0 * * ? *)", "covers": covers}


FLEET_SCHEDULES = {
    "uk_legislation": _daily("UK statutes & SIs (legislation.gov.uk) — MLRs, FSMA, POCA, ECCTA, e-money/payments, ISA, CRS, SDRT …"),
    "eur_lex": _daily("EU law (EUR-Lex/Cellar) — GDPR + corrigenda, MiFID II/MiFIR + RTS, MAR, EMIR, MiCA, DORA, AML package, AI Act, DAC8 …"),
    "govinfo_us": _daily("US federal law (GovInfo/eCFR) + NIST — FATCA, Exchange Act, Securities Act, SOX, 17 CFR 240, Reg BI/S-P/S-ID, 31 CFR X, §871(m)."),
    "fca_handbook": _daily("FCA Handbook (PRIN/SYSC/COBS/CASS/PROD/SUP/DISP/MIFIDPRU) — handbook.fca.org.uk, one artifact per chapter, rule status R/G/E kept."),
    "sec_edgar": _daily("SEC final rules and releases (sec.gov, public domain)."),
    "fca_enforcement": _daily("FCA final notices (fca.gov.uk) — enforcement outcomes as an L1 source for L7; licensed text, derived facts published."),
    "sec_enforcement": _daily("SEC enforcement actions — litigation releases and administrative proceedings (sec.gov, public domain) for L7."),
    "esma": _daily("ESMA guidelines and Q&A (esma.europa.eu PDF/HTML) — MiFID II/MAR/EMIR guidelines."),
    "bis_basel": _daily("BIS Basel Framework chapters (bis.org/basel_framework) — CRE/OPE/SRP/BCP."),
    "iosco": _daily("IOSCO Objectives and Principles + methodology (iosco.org PDF)."),
    "asic": _daily("ASIC Regulatory Guides (asic.gov.au PDF) — RG 227, RG 181, RG 271."),
    "isa": _daily("Israel Securities Authority — Securities Law 5728-1968 + regulations (English translations, derived-only)."),
    "au_legislation": _daily("Australia — Corporations Act Ch 7, AML/CTF, ASIC DTR, Privacy Act (legislation.gov.au)."),
    "sg_legislation": _daily("Singapore — SFA 2001, PDPA (sso.agc.gov.sg)."),
    "adgm": _daily("ADGM FSMR + FSRA GEN/COBS/PRU/AML."),
    "nydfs": _daily("NYDFS 23 NYCRR Parts 200 and 500."),
    "nasdaq": _daily("Nasdaq Listing Rules 5600 series."),
    "malta": _daily("Malta Cap 376 + PMLFTR (legislation.mt)."),
    "uae": _daily("UAE Federal Decree-Law 20/2018 (uaelegislation.gov.ae)."),
    "cysec": _daily("Cyprus Investment Services Law L.87(I)/2017 (PDF/HTML)."),
    "mas": _daily("MAS Notices (SFA04-N02 AML/CFT, PSN02) — numbered-paragraph PDF/HTML."),
    "fatf": _daily("FATF 40 Recommendations + interpretive notes (open PDF), recommendation-grain clauses."),
    "wolfsberg": _daily("Wolfsberg Group standards."),
    "irs_gov": _daily("IRS Revenue Procedures (Rev. Proc. 2022-43 QI agreement; 2017-15) — SECTION/.NN grain."),
    "lists": _daily("Sanctions list feeds — OFAC SDN, UN SC, EU consolidated, UK OFSI."),
    "overlay": _daily("EU/EEA host-state overlays (BE/DE/ES/FR/IT)."),
    "restricted_file": _daily("Restricted BYOL prefix watch — ISO 27001, SOC 2 TSC, PCI DSS, IFRS."),
    "seychelles": _daily("Seychelles Securities Act 2007."),
    "gibraltar": _daily("Gibraltar FSA 2019 DLT framework."),
    "israel": _daily("Israeli Privacy Protection Law 5741-1981."),
}

NODE_TYPES = (
    "title",
    "part",
    "chapter",
    "group",
    "provision",
    "paragraph",
    "subparagraph",
    "point",
    "article",
    "section",
    "subsection",
    "schedule",
    "control",
    "enhancement",
    "statement",
    "heading",
    "preamble",
    "recital",
    "note",
    "signature",
)

# Provision-grain types projected into `clauses` (stable refs for diffs / L2).
CLAUSE_TYPES = frozenset(
    {"provision", "article", "section", "subsection", "control", "enhancement", "schedule"}
)

doc_nodes = sa.Table(
    "doc_nodes",
    metadata,
    sa.Column("id", BigId, sa.Identity(), primary_key=True),
    sa.Column("source_version_id", BigId, sa.ForeignKey(f"{L1_SCHEMA}.source_versions.id"), nullable=False),
    sa.Column("parent_id", BigId, sa.ForeignKey(f"{L1_SCHEMA}.doc_nodes.id"), nullable=True),
    sa.Column("seq", sa.Integer, nullable=False),
    sa.Column("depth", sa.Integer, nullable=False, default=0),
    sa.Column(
        "node_type",
        sa.Text,
        sa.CheckConstraint(
            "node_type in ('" + "','".join(NODE_TYPES) + "')",
            name="doc_nodes_type_check",
        ),
        nullable=False,
    ),
    sa.Column("ref", sa.Text, nullable=False, default=""),
    sa.Column("label", sa.Text, nullable=False, default=""),
    sa.Column("heading", sa.Text, nullable=False, default=""),
    sa.Column("raw_text", sa.Text, nullable=False, default=""),
    sa.Column("source_fragment", sa.Text, nullable=False, default=""),
    sa.Column("source_locator", Json, nullable=False, default=dict, server_default="{}"),
    sa.Column("text_hash", sa.Text, nullable=False),
    sa.Column("public_ok", sa.Boolean, nullable=False, default=False),
    sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now()),
    sa.Index("doc_nodes_version_seq_idx", "source_version_id", "seq"),
    sa.Index("doc_nodes_parent_idx", "parent_id"),
    sa.Index("doc_nodes_ref_idx", "source_version_id", "ref"),
)

# HLD v2 I7: `embedding` is vector(1024) on Aurora (pgvector, m0016) and packed
# float32 on SQLite; platform.embeddings owns the column and rebuilds it from
# the record (never the other way round).
clauses = sa.Table(
    "clauses",
    metadata,
    sa.Column("id", BigId, sa.Identity(), primary_key=True),
    sa.Column("source_version_id", BigId, sa.ForeignKey(f"{L1_SCHEMA}.source_versions.id"), nullable=False),
    sa.Column("doc_node_id", BigId, sa.ForeignKey(f"{L1_SCHEMA}.doc_nodes.id"), nullable=True),
    sa.Column("ref", sa.Text, nullable=False),
    sa.Column("path", sa.Text, nullable=False, default=""),
    sa.Column("ordering", sa.Integer, nullable=False),
    sa.Column("text", sa.Text, nullable=False),
    sa.Column("text_hash", sa.Text, nullable=False),
    sa.Column("public_ok", sa.Boolean, nullable=False, default=False),
    # HLD v2 §4.1: character offsets of this clause inside the version's
    # canonical text (l1.spans.canonical_text) so L2 `asserts` can cite exact
    # spans; `normative` marks clauses that impose/withdraw an obligation.
    sa.Column("span_start", sa.Integer, nullable=True),
    sa.Column("span_end", sa.Integer, nullable=True),
    sa.Column("normative", sa.Boolean, nullable=False, default=False, server_default=sa.false()),
    # HLD v2 I7 vector index: vector(1024) on Aurora (pgvector), packed float32
    # on SQLite; `embedding_hash` = text_hash at embedding time so a rebuild
    # only re-embeds what changed. An index, not the record (platform.embeddings).
    sa.Column("embedding", sa.LargeBinary().with_variant(sa.Text, "postgresql"), nullable=True),
    sa.Column("embedding_model", sa.Text, nullable=True),
    sa.Column("embedding_hash", sa.Text, nullable=True),
    sa.Column("embedded_at", sa.DateTime(timezone=True), nullable=True),
    sa.Index("clauses_source_version_idx", "source_version_id"),
)

citations = sa.Table(
    "citations",
    metadata,
    sa.Column("id", BigId, sa.Identity(), primary_key=True),
    sa.Column("from_clause_id", BigId, sa.ForeignKey(f"{L1_SCHEMA}.clauses.id"), nullable=False),
    sa.Column("raw_text", sa.Text, nullable=False),
    sa.Column("resolved_source_id", BigId, sa.ForeignKey(f"{L1_SCHEMA}.sources.id"), nullable=True),
    sa.Column(
        "disposition",
        sa.Text,
        sa.CheckConstraint(
            "disposition in ('resolved','out_of_scope','open')", name="citations_disposition_check"
        ),
        nullable=False,
        default="open",
    ),
    sa.Column("reason", sa.Text, nullable=False, default=""),
)

# Accept/reject happens via l0.proposals (HLD §6.2).
discovery_candidates = sa.Table(
    "discovery_candidates",
    metadata,
    sa.Column("id", BigId, sa.Identity(), primary_key=True),
    sa.Column("family_id", BigId, sa.ForeignKey(f"{L1_SCHEMA}.source_families.id"), nullable=False),
    sa.Column("url", sa.Text, nullable=False),
    sa.Column("title", sa.Text, nullable=False, default=""),
    sa.Column("found_via", sa.Text, nullable=False, default=""),
    sa.Column("classification", Json, nullable=False, default=dict),
    sa.Column(
        "status",
        sa.Text,
        sa.CheckConstraint(
            "status in ('proposed','accepted','rejected')", name="discovery_candidates_status_check"
        ),
        nullable=False,
        default="proposed",
    ),
)

# clause_refs is JSON (text[] on the HLD's Aurora target) for SQLite parity.
change_events = sa.Table(
    "change_events",
    metadata,
    sa.Column("id", BigId, sa.Identity(), primary_key=True),
    sa.Column("source_id", BigId, sa.ForeignKey(f"{L1_SCHEMA}.sources.id"), nullable=False),
    sa.Column(
        "kind",
        sa.Text,
        sa.CheckConstraint("kind in ('added','amended','revoked')", name="change_events_kind_check"),
        nullable=False,
    ),
    sa.Column("old_version", sa.Text, nullable=True),
    sa.Column("new_version", sa.Text, nullable=False),
    sa.Column("clause_refs", Json, nullable=False, default=list),
    sa.Column("detected_at", sa.DateTime(timezone=True), server_default=sa.func.now()),
    sa.Column("diff_s3_uri", sa.Text, nullable=False, default=""),
    # HLD v2 §4.1 change detectors: clause ids of the new version that changed,
    # the extracted effective date and where it came from (text|publisher|none).
    sa.Column("clause_ids", Json, nullable=False, default=list),
    sa.Column("effective_date", sa.Date, nullable=True),
    sa.Column("effective_date_basis", sa.Text, nullable=False, default="", server_default=""),
)

# Rights recorder ledger (HLD v2 §4.1): one row per rights determination for a
# source, with the evidence (licence page, statute, publisher notice) behind it.
RIGHTS_BASES = ("public_domain", "open_licence", "licensed", "byol_only", "derived_only")

rights_records = sa.Table(
    "rights_records",
    metadata,
    sa.Column("id", BigId, sa.Identity(), primary_key=True),
    sa.Column("source_id", BigId, sa.ForeignKey(f"{L1_SCHEMA}.sources.id"), nullable=False),
    sa.Column(
        "rights_basis",
        sa.Text,
        sa.CheckConstraint(
            "rights_basis in ('" + "','".join(RIGHTS_BASES) + "')", name="rights_records_basis_check"
        ),
        nullable=False,
    ),
    sa.Column("basis_ref", sa.Text, nullable=False, default=""),
    sa.Column("evidence_url", sa.Text, nullable=False, default=""),
    sa.Column("republish_text", sa.Boolean, nullable=False, default=False),
    sa.Column("recorded_by", sa.Text, nullable=False, default="l1.rights"),
    sa.Column("recorded_at", sa.DateTime(timezone=True), server_default=sa.func.now()),
    sa.Index("rights_records_source_idx", "source_id"),
)

# "Watch this instrument" (HLD v2 §5): a user follows a source key; the change
# feed filters on it. Keyed by the community user id (session) or an app id.
watchlists = sa.Table(
    "watchlists",
    metadata,
    sa.Column("id", sa.Uuid(as_uuid=False), primary_key=True, default=_uuid),
    sa.Column("watcher_id", sa.Text, nullable=False),
    sa.Column("source_key", sa.Text, nullable=False),
    sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now()),
    sa.UniqueConstraint("watcher_id", "source_key", name="watchlists_watcher_source_key"),
)

# Clause understanding layer: enrichment about the verbatim text, NEVER the
# text itself (verbatim principle). origin 'heuristic' = deterministic
# classification from stable signals; origin 'llm' = gateway-generated
# plain-language explainer with full model provenance, clearly marked
# non-authoritative in the UI.
# Deliberately minimal (4 types) so readers can actually use them:
# definition = what terms mean; requirement = what someone must or must not
# do; enforcement = offences/penalties/liability; other = everything else.
ANNOTATION_CATEGORIES = (
    "definition",
    "requirement",
    "enforcement",
    "other",
)

clause_annotations = sa.Table(
    "clause_annotations",
    metadata,
    sa.Column("id", BigId, sa.Identity(), primary_key=True),
    sa.Column("clause_id", BigId, sa.ForeignKey(f"{L1_SCHEMA}.clauses.id"), nullable=False),
    sa.Column(
        "origin",
        sa.Text,
        sa.CheckConstraint("origin in ('heuristic','llm')", name="clause_annotations_origin_check"),
        nullable=False,
    ),
    sa.Column("summary", sa.Text, nullable=False, default=""),
    sa.Column("category", sa.Text, nullable=False, default=""),
    sa.Column("topics", Json, nullable=False, default=list),
    sa.Column("model", sa.Text, nullable=False, default=""),
    sa.Column("prompt_hash", sa.Text, nullable=False, default=""),
    sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now()),
    sa.UniqueConstraint("clause_id", "origin", name="clause_annotations_clause_origin_key"),
    sa.Index("clause_annotations_clause_idx", "clause_id"),
)

# Unified search-unit store (Cerebras-lesson analog of the "one embeddings
# table"): every searchable thing, from every source, lands here in the same
# shape and is queryable through one hybrid interface. Built at ingest from
# PUBLIC clauses only (restricted discipline). Two grains:
#   clause    — the DISTILLED form (short name + path + category/topics +
#               clause text [+ LLM summary when it lands])
#   paragraph — "bursting": one paragraph/point with its clause heading
#               prepended as context, indexed only above a signal threshold.
# `embedding` stays NULL until the P2 embedding retriever plugs in.
search_units = sa.Table(
    "search_units",
    metadata,
    sa.Column("id", BigId, sa.Identity(), primary_key=True),
    sa.Column("source_id", BigId, sa.ForeignKey(f"{L1_SCHEMA}.sources.id"), nullable=False),
    sa.Column("source_version_id", BigId, sa.ForeignKey(f"{L1_SCHEMA}.source_versions.id"), nullable=False),
    sa.Column("clause_id", BigId, sa.ForeignKey(f"{L1_SCHEMA}.clauses.id"), nullable=True),
    sa.Column("doc_node_id", BigId, sa.ForeignKey(f"{L1_SCHEMA}.doc_nodes.id"), nullable=True),
    sa.Column(
        "grain",
        sa.Text,
        sa.CheckConstraint("grain in ('clause','paragraph')", name="search_units_grain_check"),
        nullable=False,
    ),
    sa.Column("ref", sa.Text, nullable=False, default=""),
    sa.Column("text", sa.Text, nullable=False),
    sa.Column("embedding", Json, nullable=True),
    sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now()),
    sa.Index("search_units_version_idx", "source_version_id"),
    sa.Index("search_units_clause_idx", "clause_id"),
)

# Learned parse hints (fidelity repair loop): discovered once (LLM tier or
# manually), applied deterministically on every future run BEFORE any LLM
# call, ratified/retired by maintainers via the L0 proposals rail.
parse_hints = sa.Table(
    "parse_hints",
    metadata,
    sa.Column("id", BigId, sa.Identity(), primary_key=True),
    sa.Column("source_id", BigId, sa.ForeignKey(f"{L1_SCHEMA}.sources.id"), nullable=False),
    sa.Column("hint", Json, nullable=False),
    sa.Column(
        "origin",
        sa.Text,
        sa.CheckConstraint("origin in ('llm','manual')", name="parse_hints_origin_check"),
        nullable=False,
        default="llm",
    ),
    sa.Column(
        "status",
        sa.Text,
        sa.CheckConstraint("status in ('candidate','approved','retired')", name="parse_hints_status_check"),
        nullable=False,
        default="candidate",
    ),
    sa.Column("proposal_id", sa.Uuid(as_uuid=False), nullable=True),
    sa.Column("times_used", sa.Integer, nullable=False, default=0),
    sa.Column("last_used_at", sa.DateTime(timezone=True), nullable=True),
    sa.Column("last_needed_at", sa.DateTime(timezone=True), nullable=True),
    sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now()),
    sa.Index("parse_hints_source_idx", "source_id"),
)

licenses_held = sa.Table(
    "licenses_held",
    metadata,
    sa.Column("id", BigId, sa.Identity(), primary_key=True),
    sa.Column("product", sa.Text, nullable=False),
    sa.Column("vendor", sa.Text, nullable=False),
    sa.Column("scope", sa.Text, nullable=False, default=""),
    sa.Column("seats", sa.Integer, nullable=True),
    sa.Column("purchased_at", sa.Date, nullable=True),
    sa.Column("renewal_at", sa.Date, nullable=True),
    sa.Column("notes", sa.Text, nullable=False, default=""),
)

byol_uploads = sa.Table(
    "byol_uploads",
    metadata,
    sa.Column("id", sa.Uuid(as_uuid=False), primary_key=True, default=_uuid),
    sa.Column("user_id", sa.Text, nullable=False),
    sa.Column("source_id", BigId, sa.ForeignKey(f"{L1_SCHEMA}.sources.id"), nullable=False),
    sa.Column("content_hash", sa.Text, nullable=False),
    sa.Column("verified", sa.Boolean, nullable=False, default=False),
    sa.Column("s3_uri", sa.Text, nullable=False, default=""),
    sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now()),
)

ALL_TABLES = (
    source_families,
    sources,
    family_members,
    source_versions,
    doc_nodes,
    clauses,
    clause_annotations,
    search_units,
    citations,
    discovery_candidates,
    change_events,
    parse_hints,
    licenses_held,
    byol_uploads,
    rights_records,
    watchlists,
)

# HLD v2 §3: every layer table inherits the shared bi-temporal / why-trail schema.
from app.clhear.platform.shared_schema import attach_shared_columns as _attach  # noqa: E402

_attach(*ALL_TABLES)
