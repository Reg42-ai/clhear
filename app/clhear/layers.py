# Copyright (C) 2026 Reg42 AI
# This file is part of CLHEAR. See LICENSE (AGPL-3.0-only).
"""Eight-layer contract (HLD §2) — the system's spine.

Every layer carries a machine-readable *derivation contract*: how the layer
determines the data it has (inputs, method, human/eval gates, evidence).
Statuses:
  live   — real data, produced by the described method (L0, L1 today)
  demo   — browsable illustrative data, authored to preview the layer's shape;
           every demo item still chains down to REAL L1 clauses (lineage)
  locked — closed by design (L8): definition visible, data not

App clients MUST feature-detect via release.layers and /v1/releases/{id}/l{n}
status. Only `live` layers are `published` for the /v1 contract; demo layers
answer with layer_status="demo" and demo-labeled bodies, never as real data.
"""

from __future__ import annotations

LAYER_CATALOG: dict[str, dict] = {
    "L0": {
        "slug": "l0",
        "name": "Platform rails",
        "schema": "l0_platform",
        "published": True,
        "status": "live",
        "purpose": "The rails every layer runs on: one event bus, one LLM gateway "
        "with cost control, one human-approval queue, one release pipeline.",
        "derivation": {
            "inputs": [],
            "method": "Deterministic infrastructure. Events are written to a transactional "
            "outbox in the same transaction as the data change, then relayed to SQS; "
            "every fleet run is recorded in an append-only ledger; every LLM call is "
            "logged with prompt hash, tokens and cost under hard daily spend caps.",
            "generation": {
                "nature": "infrastructure — no generation",
                "technique": "Inference router + spend caps + AI ops ledger",
                "guarantee": "No LLM call except router.run; every decision logs task, model, rejected alternatives, cost, reasoning",
                "may": ["route, cap, ledger, revalidate corrections"],
                "must_not": ["bypass the router", "exceed the $50/month frontier cap"],
                "gates": ["fleet/global daily caps", "premium monthly cap", "procurement-clean ladders"],
            },
            "gates": [
                "Eval-gated publish; admin override is last resort on the correction loop",
                "Hard LLM spend caps ($20/day per fleet, $100/day global, $50/month frontier) — hard stop, not a warning",
            ],
            "evidence": [
                "runs ledger (replayable: same inputs => same corpus)",
                "llm_calls ledger with per-call cost",
                "outbox relayed_at stamps",
            ],
        },
    },
    "L1": {
        "slug": "l1",
        "name": "Verbatim sources",
        "schema": "l1_sources",
        "published": True,
        "status": "live",
        "purpose": "The vault of regulatory truth: official texts fetched from "
        "authoritative sources, split into clauses, stored immutably, watched forever.",
        "derivation": {
            "inputs": ["L0"],
            "method": "Deterministic fetch -> parse -> hash -> diff. Adapters retrieve the "
            "official artifact from the issuing authority (legislation.gov.uk, EUR-Lex, "
            "govinfo/eCFR, NIST); the pipeline stores originals in WORM S3, versions the "
            "text, aligns clauses by ref and emits clause-level change events. Every mention of "
            "another text or provision is recorded with its quote and offsets; a build resolves the "
            "mentions against its scope and names each cited text it does not hold. Source "
            "text is NEVER generated, cleaned up, or summarized into the record.",
            "generation": {
                "nature": "reproduction, zero generation",
                "technique": "Deterministic fetch/parse/hash; LLM only for extractive parse repair and grounded annotation",
                "guarantee": "Parse-repair output must byte-match publisher text (fidelity oracle) or it is discarded; annotations are origin=llm and never replace text",
                "may": ["propose parse hints", "annotate with origin=llm"],
                "must_not": ["write or clean source text", "store generated law"],
                "gates": ["E1 fidelity", "coverage"],
            },
            "gates": [
                "Fidelity checks against the fetched original before a version is accepted",
                "Family membership changes go through the L0 proposals queue (human ratifies)",
                "Restricted-license texts (ISO, TSC, PCI, IFRS) expose refs and hashes only",
            ],
            "evidence": [
                "content_hash per version, text_hash per clause",
                "immutable S3 original per version (Object Lock)",
                "E1-E7 eval scorecards per source",
                "clause-level change_events with diffs",
            ],
        },
    },
    "L2": {
        "slug": "l2",
        "name": "Obligations",
        "schema": "l2_obligations",
        "published": False,
        "status": "derived",
        "purpose": "Atomic obligations — who must do what, when — extracted from L1 "
        "clauses and kept current by clause-level change inference.",
        "derivation": {
            "inputs": ["L1"],
            "method": "Each obligation is anchored to the exact L1 clause(s) that impose it. "
            "Extraction is deterministic-first (clause structure, duty verbs, addressee "
            "detection); LLM assistance is confined to the gated triage step; every "
            "obligation stores its basis refs so it can be re-verified against the "
            "verbatim text at any time. When L1 detects a clause change, affected "
            "obligations are flagged for re-derivation automatically.",
            "generation": {
                "nature": "anchored extraction + cross-jurisdiction synthesis",
                "technique": "Deterministic duty extract; LLM duty-triage with evidence-span contract; auto-applied consolidation",
                "guarantee": "Triage span must be a literal substring; concept members must be existing OBL: ids (closed-world) plus n-gram restricted guard",
                "may": ["verdict + quoted span", "draft canonical statement"],
                "must_not": ["invent obligation ids", "copy restricted 8-grams"],
                "gates": ["l2_coverage", "l2_precision", "l2_dedupe", "l2_change_inference",
                          "l2_extraction_quality", "l2_basis_integrity", "l2_concept_integrity"],
            },
            "gates": [
                "Coverage >= 99% of normative clauses, second-model precision >= 95%, unmerged duplicates < 1%, change inference >= 95% (HLD v2 §4.2)",
                "Re-derivation flag on any L1 change event touching a basis clause",
                "Restricted sources contribute obligation refs only, never text",
                "Consolidation auto-applies as AI-GENERATED; Eval Studio samples for audit coverage",
            ],
            "evidence": [
                "basis clause refs + hashes per obligation (inspect the lineage)",
                "change-propagation trail from L1 change_events",
            ],
        },
    },
    "L3": {
        "slug": "l3",
        "name": "Components",
        "schema": "l3_building_blocks",
        "published": False,
        "status": "derived",
        "purpose": "What an organisation must do or have to meet each duty, named in the duty's own words, "
        "with the kind of thing it is (System, Document, Role, Configuration, Process, Workflow, Asset, Body, "
        "or Unspecified when the words do not say) and the characteristics the clauses state.",
        "derivation": {
            "inputs": ["L2"],
            "method": "A model groups duties into measures, and a name is kept only when every word of it "
            "occurs in the cited clauses. Any duty still without one gets the measure its own words name "
            "(the quoted action, or the thing to have), with its kind read from the same words. A duty "
            "that names nothing concrete gets no measure and an evidence gap. Characteristics are "
            "recorded only as quotes of a clause; each field the clauses leave open is an evidence gap.",
            "generation": {
                "nature": "deterministic naming from the duty's words; LLM grouping kept only in the text's words",
                "technique": "Quoted action / object phrase; kind from the head noun or verb; quoted characteristics",
                "guarantee": "Every measure name, kind and characteristic quotes a clause of a duty it satisfies; what the text does not state is an evidence gap",
                "may": ["group duties under one measure named in their words", "quote a characteristic from a clause"],
                "must_not": ["cite obligations that do not exist", "name a measure with words not in the text", "fill a characteristic the text does not state"],
                "gates": ["l3_completeness", "l3_characteristics", "l3_reuse", "l3_precision", "l3_l5_referential"],
            },
            "gates": [
                "Every requires edge, measure name and characteristic passes the lineage check against the clause text",
                "L2 'revoked' invalidates the obligation's requires edges; 'updated' re-stamps them and reopens characteristics it backed",
            ],
            "evidence": ["quoted measure name and kind", "quoted duty per requires edge", "quoted characteristic values"],
        },
    },
    "L4": {
        "slug": "l4",
        "name": "Profile attributes and applicability",
        "schema": "l4_profiles",
        "published": False,
        "status": "derived",
        "purpose": "The questions the texts raise about an organisation (where it operates, which "
        "addressee it is, which facts in a duty's own 'where / if / unless' clause hold) and the "
        "applies_to edges that say which duty depends on which answer.",
        "derivation": {
            "inputs": ["L1", "L2"],
            "method": "Each duty's edges are read from its own words: the jurisdiction its source "
            "declares, the addressee it names unless that is everyone, and its condition when the "
            "condition is about the addressee. Licence types are read only from the scope's licensing "
            "clauses and the entries of its registers (sources of kind register), and kept only in their "
            "words; they are the permitted values of the profile's licences. A profile answers the questions; a duty applies, is "
            "not applicable, or is undetermined while a question is unanswered. Candidate business profiles "
            "are offered per quoted role and per quoted licence type.",
            "generation": {
                "nature": "deterministic reading of the duty's grammar; LLM confined to quoting licence types",
                "technique": "Subject / condition quotes; three-valued evaluation; licensing-clause extraction with a "
                             "quote check; register entries read as labelled fields or table columns, quoted",
                "guarantee": "Every applies_to edge quotes the words it was read from; no role, condition or licence exists that is not in the texts in scope",
                "may": ["quote a licence type from a licensing clause"],
                "must_not": ["invent a role, condition or licence", "decide an unanswered question"],
                "gates": ["l4_applicability", "l4_grounding"],
            },
            "gates": [
                "Every applies_to edge points at a live obligation and carries its quotes",
                "L2 'revoked' withdraws the obligation's applies_to edges; 'updated' re-reads them",
            ],
            "evidence": ["quoted subject or condition per edge", "declared jurisdiction per source", "quoted licensing clause per licence type"],
        },
    },
    "L5": {
        "slug": "l5",
        "name": "Compliance activities",
        "schema": "l5_activities",
        "published": False,
        "status": "derived",
        "purpose": "Who does what: each duty's quoted action, operated by the addressee the clause "
        "names, and the measures it operates.",
        "derivation": {
            "inputs": ["L2", "L3"],
            "method": "For every duty with a measure, the activity is the duty's action as quoted and its "
            "operator the addressee the clause names. A duty addressed to everyone, or written in the "
            "passive, names no operator: the gap is recorded. operates edges link an activity to the "
            "measures its duties require. No activity catalogue is used.",
            "generation": {
                "nature": "deterministic, from the duty's words",
                "technique": "Quoted action and addressee; operates via requires edges",
                "guarantee": "Every activity and operator quotes a clause; trigger anchors resolve to live obligations",
                "may": [],
                "must_not": ["invent an activity, operator or business process"],
                "gates": ["l5_completeness", "l3_l5_referential"],
            },
            "gates": [
                "Every activity and operates edge passes the lineage check",
                "L2 'revoked' unlights the edges the obligation lit",
            ],
            "evidence": ["quoted action and operator per activity", "quoted duty per operates edge"],
        },
    },
    "L6": {
        "slug": "l6",
        "name": "Composition and reference blueprints",
        "schema": "l6_composer",
        "published": False,
        "status": "computed",
        "purpose": "Composes the leanest complete compliance program for one profile: the set of "
        "L3 building blocks (items) that satisfies every applicable obligation, with a minimality "
        "proof and a full evidence chain per item (HLD v2 §4.6).",
        "derivation": {
            "inputs": ["L2", "L3", "L4", "L5"],
            "method": "Deterministic set-cover with hard constraints: take the profile (L4), collect "
            "every applicable obligation (L4 predicates + L5 activity triggers over L2), make every "
            "block an obligation requires (L3) mandatory, then pick the fewest further blocks whose "
            "selectors satisfy what is left and prune anything redundant. Each item carries its "
            "characteristics resolved for the profile, the obligations it satisfies, the compliance "
            "activities that operate it (L5) and whether it is load-bearing. A blueprint is stored "
            "under a BLU- id with ITM- items and its proof; when any lower layer changes it is "
            "recomposed and the diff published (clhear.l6.changed); the old one is superseded, never deleted.",
            "generation": {
                "nature": "deterministic optimization; LLM as explainer / citing narrator only",
                "technique": "Set-cover math (LLM-free) + rubric-gated item explanations + citation-checked rationale",
                "guarantee": "Every explanation names its block, cites obligations in the blueprint, states the "
                "trigger and the item's role, and cites nothing outside the blueprint; rewrites that fail are dropped",
                "may": ["rephrase item explanations and narrate coverage, blocks, and gaps already in the blueprint"],
                "must_not": ["cite obligations or blocks outside the blueprint", "add or remove items"],
                "gates": ["l6_completeness", "l6_minimality", "l6_reference", "l6_explanation", "l6_citation"],
            },
            "gates": [
                "Completeness 100 %: every applicable obligation satisfied by ≥ 1 item",
                "Minimality checked: no item removable without breaking coverage (independently re-verified)",
                "Reference-program agreement ≥ 90 % against expert-authored programs",
                "Explanation quality ≥ 90 % on the rubric",
                "Coverage gaps are surfaced, never silently accepted; programs pinned to a release",
            ],
            "evidence": ["items with obligations satisfied and characteristics; minimality proof with removal impact; "
                         "diff against any earlier blueprint; OSCAL system-security-plan export"],
        },
    },
    "L7": {
        "slug": "l7",
        "name": "Risk scoring",
        "schema": "l7_risk",
        "published": False,
        "status": "computed",
        "purpose": "Quantifies enforcement exposure per obligation: how often and how hard "
        "regulators have acted on it, how fast its law is changing, and how much of a "
        "program it touches.",
        "derivation": {
            "inputs": ["L1", "L2", "L3", "L5"],
            "method": "Scores are computed, not judged. Enforcement outcomes are read from L1 "
            "enforcement sources (one event per charged firm, each quoting its in-force clause) "
            "and linked to the L2 obligations they cite. Each obligation's score weighs "
            "published dimensions: recency-weighted enforcement history, a likelihood calibrated "
            "on past years and scored on a held-out year, financial and reputational impact of "
            "the linked outcomes, L2 change velocity, L3/L5 operational reach, and the most severe "
            "penalty the binding texts in scope state for a breach (type and maximum, quoted, linked to the "
            "obligations the penalty clause refers to). A company's "
            "item priority is a view that lays these scores onto its L6 blueprint items; no "
            "score reads a blueprint. Every score publishes its weights, dimensions and evidence. With no "
            "enforcement source in scope the scores rest on the stated penalties alone, and the source advice "
            "says what to add; with neither, the layer is not built.",
            "generation": {
                "nature": "quantitative + grounded commentary",
                "technique": "Formula-deterministic scores; number-echo narratives over a versioned facts file",
                "guarantee": "Every figure in the narrative equals a figure in the score vector; external context only from curated facts",
                "may": ["comment on the published input vector", "cite FACT: ids"],
                "must_not": ["invent numbers", "recall external stats from model memory"],
                "gates": ["formula determinism", "l7_number_echo", "facts-file provenance"],
            },
            "gates": ["Formula and weights are versioned; a score without its inputs is invalid"],
            "evidence": ["per-score input vector including live L1 change counts"],
        },
    },
    "L8": {
        "slug": "l8",
        "name": "Practices",
        "schema": "l8_benchmarks",
        "published": False,
        "status": "reference",
        "purpose": "Benchmarks: how a program compares with what others were found doing. Today a "
        "reference benchmark of public regulator examination findings, mapped to blueprint "
        "blocks; closed peer benchmarks across anonymized organisations follow once k is met.",
        "derivation": {
            "inputs": ["L1", "L3", "L7"],
            "method": "Each reference row is one in-force block of a public examination report or "
            "guidance publication in L1 (a regulator's FAQs, bulletins, inspection findings or decisions), "
            "or the remediation an enforcement source orders ('is ordered to', 'agreed to', corrective "
            "action, a duty with a deadline); a clause that is the basis of an obligation is not a practice. "
            "Each row is quoted exactly, and names the L3 block whose name, purpose and required obligations "
            "share the most words with it; a row that shares too little names no block. Peer "
            "aggregates combine L7 scores across participating organisations within a profile "
            "cluster, stay inside the enclave, and publish only as k-anonymous aggregates.",
            "generation": {
                "nature": "derived from verbatim L1 blocks; pure computation for peer aggregates; zero LLM",
                "technique": "Word-overlap mapping of quoted blocks to L3; k≥5 cohort aggregates over accumulated blueprint requests",
                "guarantee": "Every reference row quotes the current clause it cites and is labeled not peer data; "
                "real peer aggregates publish only when k is met",
                "may": ["publish reference findings with their quote and block", "publish k-anonymous means"],
                "must_not": ["use an LLM", "present a reference row as peer data", "publish a cohort smaller than k"],
                "gates": ["l8_k_anonymity"],
            },
            "gates": [
                "Reference rows are emitted only when their quote is in the in-force clause",
                "k-anonymity threshold before any peer aggregate exists",
                "Participation is opt-in and contractual",
            ],
            "evidence": ["reference rows: quote, clause, block", "peer aggregate definitions (visible); data (locked)"],
        },
    },
}

PUBLISHED_LAYERS = tuple(k for k, v in LAYER_CATALOG.items() if v["published"])
RESERVED_LAYERS = tuple(k for k, v in LAYER_CATALOG.items() if not v["published"])
# Layers that answer with data but are not yet the /v1-published contract.
PREVIEW_STATUSES = ("derived", "curated", "computed", "reference")
PREVIEW_LAYERS = tuple(k for k, v in LAYER_CATALOG.items() if v["status"] in PREVIEW_STATUSES)
LAYER_ORDER = tuple(sorted(LAYER_CATALOG, key=lambda k: int(k[1:])))
LAYER_SLUGS = {v["slug"]: k for k, v in LAYER_CATALOG.items()}


def normalize_layer(raw: str) -> str | None:
    token = (raw or "").strip()
    if not token:
        return None
    upper = token.upper()
    if upper in LAYER_CATALOG:
        return upper
    return LAYER_SLUGS.get(token.lower())


def layer_public_meta(layer: str) -> dict:
    """Catalog entry shaped for API/UI consumption."""
    meta = LAYER_CATALOG[layer]
    return {
        "layer": layer,
        "slug": meta["slug"],
        "name": meta["name"],
        "schema": meta["schema"],
        "status": meta["status"],
        "published": meta["published"],
        "purpose": meta["purpose"],
        "derivation": meta["derivation"],
    }


def not_published_body(layer: str) -> dict:
    meta = LAYER_CATALOG.get(layer, {})
    return {
        "layer": layer,
        "layer_status": "not_published",
        "name": meta.get("name"),
        "schema": meta.get("schema"),
        "detail": f"{layer} is reserved. CLHEAR currently publishes {', '.join(PUBLISHED_LAYERS)} only.",
    }


_STATUS_NOTICES = {
    "derived": "{layer} ({name}) is AI-GENERATED / MACHINE-DERIVED. Extraction is "
    "deterministic; consolidations and triage are routed AI with structural guarantees. "
    "Items carry audit coverage (% human-sampled) and per-item provenance (model, routing reason). "
    "Eval Studio disagreements enter the correction → AI revalidation loop.",
    "curated": "{layer} ({name}) is AI-GENERATED by default, eval-gated, and human-audited "
    "by sampling. Seeded catalog rows remain human-authored; fleet-written rows are "
    "ai_generated until sampled. Every mapping is an anchor into real L1 clauses.",
    "computed": "{layer} ({name}) is COMPUTED: a deterministic, versioned engine over the "
    "derived registry and curated catalog. Same inputs always produce the same output; "
    "every result publishes its formula and its inputs.",
    "locked": "{layer} ({name}) is LOCKED by design: definitions are public, data is not.",
    "reference": "{layer} ({name}) is a REFERENCE benchmark: public regulator findings across examined "
    "firms, quoted from in-force L1 clauses and mapped to blueprint blocks. It is not peer data; "
    "k-anonymous peer aggregates publish only when k is met.",
}


def status_banner(layer: str) -> dict:
    """The honesty label attached to every non-live layer payload."""
    meta = LAYER_CATALOG.get(layer, {})
    status = meta.get("status", "")
    notice = _STATUS_NOTICES.get(status, "")
    return {
        "data_status": status,
        "notice": notice.format(layer=layer, name=meta.get("name")),
    }


# Back-compat alias (previous phase labeled preview layers "demo").
demo_banner = status_banner
