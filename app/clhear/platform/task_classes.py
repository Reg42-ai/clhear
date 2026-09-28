# Copyright (C) 2026 Reg42 AI
# This file is part of CLHEAR. See LICENSE (AGPL-3.0-only).
"""CLHEAR task classes for the configured model provider (HLD v2 §3, §8 item 2, §9).

Derivation classes produce determinations that enter the record; they may only run
on procurement-clean models (US/EU origin, hosted on Bedrock inside the account).
Non-derivation classes (classification, summarization, judging) may use any Bedrock
model a client policy allows. Chinese-origin open weights are never in a derivation
ladder. This module is the single source of truth mirrored into
``handoff/deployment notes/tasks.clhear.yaml``.
"""
from __future__ import annotations

from dataclasses import dataclass

# Bedrock model ids (frozen per release via the model manifest).
GPT_OSS_120B = "openai.gpt-oss-120b-1:0"
MISTRAL_LARGE_3 = "mistral.mistral-large-3-v1:0"
CLAUDE_SONNET_5 = "anthropic.claude-sonnet-5-v1:0"
CLAUDE_OPUS_5 = "anthropic.claude-opus-5-v1:0"
NOVA_PRO = "amazon.nova-pro-v1:0"
NOVA_LITE = "amazon.nova-lite-v1:0"
LLAMA_3_3_70B = "meta.llama3-3-70b-instruct-v1:0"
# Permitted only for non-derivation classes unless a client policy says otherwise.
QWEN_3_5_32B = "qwen.qwen3-5-32b-instruct-v1:0"
DEEPSEEK_R1 = "deepseek.r1-v1:0"
# Embedding models (1024 dims) for the pgvector clause index — an index, not a determination.
TITAN_EMBED_V2 = "amazon.titan-embed-text-v2:0"
COHERE_EMBED_MULTI = "cohere.embed-multilingual-v3"

MODEL_ORIGIN: dict[str, str] = {
    TITAN_EMBED_V2: "US",
    COHERE_EMBED_MULTI: "US",
    GPT_OSS_120B: "US",
    MISTRAL_LARGE_3: "EU",
    CLAUDE_SONNET_5: "US",
    CLAUDE_OPUS_5: "US",
    NOVA_PRO: "US",
    NOVA_LITE: "US",
    LLAMA_3_3_70B: "US",
    QWEN_3_5_32B: "CN",
    DEEPSEEK_R1: "CN",
}

PROCUREMENT_CLEAN_ORIGINS = frozenset({"US", "EU"})


def origin_of(model_id: str) -> str:
    if model_id in MODEL_ORIGIN:
        return MODEL_ORIGIN[model_id]
    low = model_id.lower()
    if any(tag in low for tag in ("qwen", "deepseek", "yi-", "glm", "baichuan", "internlm", "kimi", "minimax")):
        return "CN"
    if any(tag in low for tag in ("anthropic", "amazon", "openai", "meta", "cohere", "ai21", "gpt-oss", "nova")):
        return "US"
    if "mistral" in low:
        return "EU"
    return "unknown"


def is_procurement_clean(model_id: str) -> bool:
    return origin_of(model_id) in PROCUREMENT_CLEAN_ORIGINS


@dataclass(frozen=True)
class TaskClass:
    id: str
    layer: str
    shape: str  # extraction | classification | structured_drafting | long_reasoning | judging | mapping
    derivation: bool
    ladder: tuple[str, ...]
    quality_threshold: float
    description: str
    data_class_default: str = "public"  # public | restricted


_DERIVATION_LADDER = (GPT_OSS_120B, MISTRAL_LARGE_3, CLAUDE_SONNET_5, CLAUDE_OPUS_5)
_HARD_LADDER = (CLAUDE_SONNET_5, CLAUDE_OPUS_5, MISTRAL_LARGE_3)
_CHEAP_LADDER = (NOVA_LITE, NOVA_PRO, GPT_OSS_120B)
_NONDERIVATION_LADDER = (NOVA_LITE, LLAMA_3_3_70B, GPT_OSS_120B)

TASK_CLASS_LIST: tuple[TaskClass, ...] = (
    TaskClass("l1_parse", "L1", "extraction", True, _CHEAP_LADDER, 0.90,
              "Extractive parse repair hints; output must byte-match publisher text"),
    TaskClass("l1_change", "L1", "extraction", True, _DERIVATION_LADDER, 0.90,
              "Effective-date extraction and clause-level change classification"),
    TaskClass("l1_translate", "L1", "structured_drafting", True, _DERIVATION_LADDER, 0.95,
              "Complete aligned English view; non-authoritative, independent bilingual evaluation required", data_class_default="restricted"),
    TaskClass("l2_extract", "L2", "extraction", True, _DERIVATION_LADDER, 0.90,
              "Clause -> candidate obligations with span offsets"),
    TaskClass("l2_consolidate", "L2", "structured_drafting", True, _HARD_LADDER, 0.88,
              "Dedupe/merge into canonical obligations; equivalence detection"),
    TaskClass("l2_change", "L2", "extraction", True, _DERIVATION_LADDER, 0.90,
              "L1 clause change -> which obligations change how"),
    TaskClass("l3_decompose", "L3", "structured_drafting", True, _HARD_LADDER, 0.85,
              "Obligation -> building blocks (8 kinds)"),
    TaskClass("l3_characterize", "L3", "extraction", True, _DERIVATION_LADDER, 0.88,
              "Fill fixed characteristic schema from obligation text"),
    TaskClass("l4_enumerate", "L4", "extraction", True, _DERIVATION_LADDER, 0.90,
              "Licences, products, client types, channels from regulator registers"),
    TaskClass("l5_map", "L5", "mapping", True, _DERIVATION_LADDER, 0.85,
              "Activities <-> blocks / products with obligation refs"),
    TaskClass("l6_explain", "L6", "long_reasoning", True, _HARD_LADDER, 0.90,
              "Per-item explanation citing blueprint ids only"),
    TaskClass("l7_score", "L7", "extraction", True, _DERIVATION_LADDER, 0.85,
              "Enforcement case -> breached obligations; dimension inputs"),
    TaskClass("l8_fill", "L8", "structured_drafting", True, _HARD_LADDER, 0.85,
              "Best-practice fills for blocks", data_class_default="members"),
    TaskClass("judge", "L0", "judging", False, _NONDERIVATION_LADDER, 0.85,
              "Second-model review / rubric judging (non-derivation)"),
    TaskClass("embed", "L0", "embedding", False, (TITAN_EMBED_V2, COHERE_EMBED_MULTI), 0.0,
              "Clause embeddings (1024 dims) for the pgvector index; rebuildable, never enters the record"),
)

TASK_CLASSES: dict[str, TaskClass] = {tc.id: tc for tc in TASK_CLASS_LIST}
DERIVATION_CLASSES: frozenset[str] = frozenset(tc.id for tc in TASK_CLASS_LIST if tc.derivation)
REQUIRED_TASK_CLASSES: tuple[str, ...] = (
    "l1_parse", "l1_change", "l1_translate", "l2_extract", "l2_consolidate", "l2_change", "l3_decompose",
    "l3_characterize", "l4_enumerate", "l5_map", "l6_explain", "l7_score", "l8_fill", "judge",
    "embed",
)


def default_ladder(task_class: str) -> list[str]:
    return list(TASK_CLASSES[task_class].ladder)


def validate_ladders(ladders: dict[str, list[str]]) -> list[str]:
    """Violations for a ladder set: missing classes, CN-origin in derivation."""
    problems = []
    for tc in REQUIRED_TASK_CLASSES:
        if tc not in ladders or not ladders[tc]:
            problems.append(f"missing ladder for {tc}")
            continue
        if tc in DERIVATION_CLASSES:
            for m in ladders[tc]:
                if not is_procurement_clean(m):
                    problems.append(f"{tc}: {m} ({origin_of(m)}) not allowed in a derivation class")
    return problems


def to_infer_yaml() -> str:
    """Render tasks.clhear.yaml for deployment notes (kept byte-identical by a test)."""
    lines = [
        "# CLHEAR task classes for the configured model provider — generated from",
        "# app/clhear/platform/task_classes.py. Do not hand-edit.",
        "# Derivation classes: procurement-clean (US/EU origin) Bedrock models only.",
        "employee_prefix: clhear-",
        "tasks:",
    ]
    for tc in TASK_CLASS_LIST:
        lines.append(f"  {tc.id}:")
        lines.append(f"    layer: {tc.layer}")
        lines.append(f"    shape: {tc.shape}")
        lines.append(f"    derivation: {'true' if tc.derivation else 'false'}")
        lines.append(f"    quality_threshold: {tc.quality_threshold}")
        lines.append(f"    data_class_default: {tc.data_class_default}")
        lines.append(f"    description: {tc.description!r}")
        lines.append("    ladder:")
        for m in tc.ladder:
            lines.append(f"      - {m}  # origin={origin_of(m)}")
    lines.append("policy:")
    lines.append("  derivation_classes_forbid_origins: [CN]")
    lines.append("  hosting: aws-bedrock")
    lines.append("  frozen_per_release: true")
    return "\n".join(lines) + "\n"
