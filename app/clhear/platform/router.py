# Copyright (C) 2026 Reg42 AI
# This file is part of CLHEAR. See LICENSE (AGPL-3.0-only).
"""Inference router — the only LLM entry point (HLD v2 §3, I6, §9).

Every production call is `router.run(task_id, …)`. Each task belongs to a CLHEAR
task class; the class has a procurement-clean ladder of Bedrock models served by
the configured model provider. The router picks the cheapest rung whose measured quality meets the
task threshold, records the decision (chosen model, rejected rungs, reason, cost)
and only then invokes the gateway. A consumer provider
(anthropic, openai_compatible, or bedrock) uses the model the consumer configured.
FakeProvider is used only when CLHEAR_LLM_PROVIDER=fake.
"""
from __future__ import annotations

import logging
from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from typing import Any

import sqlalchemy as sa
from sqlalchemy.engine import Engine

from app.clhear.models import llm_calls, router_quality
from app.clhear.platform.gateway import (
    PREMIUM_MODELS,
    FakeProvider,
    Gateway,
    LlmResult,
    Provider,
    SpendCapExceeded,
)
from app.clhear.platform.task_classes import (
    CLAUDE_OPUS_5,
    CLAUDE_SONNET_5,
    DERIVATION_CLASSES,
    GPT_OSS_120B,
    MISTRAL_LARGE_3,
    NOVA_LITE,
    NOVA_PRO,
    TASK_CLASSES,
    default_ladder,
    is_procurement_clean,
    origin_of,
)
from app.clhear.settings import get_settings

log = logging.getLogger("clhear.router")

NO_PROVIDER_REASON = (
    "No model provider configured. Set CLHEAR_LLM_PROVIDER to anthropic, openai_compatible, or bedrock "
    "and supply that provider's key or role. FakeProvider is only used when CLHEAR_LLM_PROVIDER=fake."
)

CONSUMER_PROVIDERS = ("anthropic", "openai_compatible", "bedrock")

SHAPES = (
    "classification",
    "extraction",
    "structured_drafting",
    "long_reasoning",
    "doc_analysis",
    "graph_mapping",
    "judging",
)


class ProcurementViolation(RuntimeError):
    """A derivation class ladder contains a model that is not procurement-clean."""


@dataclass(frozen=True)
class TaskSpec:
    id: str
    shape: str
    complexity: str  # low | medium | high
    criticality: str  # low | medium | high
    quality_threshold: float
    fleet: str
    layer: str
    task_class: str = "judge"  # Infer task class (app.clhear.platform.task_classes)
    latency_tolerance: str = "nightly"  # interactive | hours | nightly
    domain: str = ""
    context_size: str = "small"
    nightly_volume: int = 0
    determinism: dict = field(default_factory=lambda: {"temperature": 0.0})
    description: str = ""


TASKS: dict[str, TaskSpec] = {
    "l1.translate": TaskSpec(
        "l1.translate", "structured_drafting", "high", "high", 0.95, "l1.translate", "L1", task_class="l1_translate",
        description="Separate complete English view, never a verbatim source"),
    "l1.translation_review": TaskSpec(
        "l1.translation_review", "judging", "high", "high", 0.95, "l1.translation_review", "L1", task_class="judge",
        description="Independent bilingual evaluation of every aligned translation segment"),
    "dummy.triage": TaskSpec(
        "dummy.triage", "classification", "low", "low", 0.80, "dummy", "L0", task_class="judge",
        latency_tolerance="interactive", description="P0 rehearsal classification",
    ),
    "l1.parse_repair": TaskSpec(
        "l1.parse_repair", "extraction", "medium", "high", 0.90, "l1.repair", "L1", task_class="l1_parse",
        latency_tolerance="hours", domain="web-structure",
        description="Extractive parse hints — output must byte-match publisher text",
    ),
    "l1.annotate": TaskSpec(
        "l1.annotate", "structured_drafting", "low", "low", 0.75, "l1.annotate", "L1", task_class="l1_parse",
        latency_tolerance="nightly", description="Grounded clause annotation; origin=llm",
    ),
    "l1.change": TaskSpec(
        "l1.change", "classification", "medium", "high", 0.90, "l1.change", "L1", task_class="l1_change",
        latency_tolerance="hours", domain="legal", description="Classify a version diff: substantive vs editorial",
    ),
    "l2.duty_triage": TaskSpec(
        "l2.duty_triage", "classification", "low", "medium", 0.85, "l2.triage", "L2", task_class="l2_extract",
        latency_tolerance="nightly", domain="legal",
        description="Weak-modality duty verdict + evidence-span contract",
    ),
    "l2.extract": TaskSpec(
        "l2.extract", "extraction", "high", "high", 0.90, "l2.extract", "L2", task_class="l2_extract",
        latency_tolerance="nightly", domain="legal", description="Atomic obligation extraction from spans",
    ),
    "l2.consolidate": TaskSpec(
        "l2.consolidate", "structured_drafting", "medium", "medium", 0.85, "l2.consolidate", "L2",
        task_class="l2_consolidate", latency_tolerance="nightly", domain="legal",
        description="Cross-jurisdiction concept draft; closed-world OBL: members",
    ),
    "l2.change": TaskSpec(
        "l2.change", "classification", "medium", "high", 0.90, "l2.change", "L2", task_class="l2_change",
        latency_tolerance="hours", domain="legal", description="Infer obligation change from an L1 change",
    ),
    "l2.review": TaskSpec(
        "l2.review", "judging", "medium", "high", 0.90, "l2.review", "L2", task_class="judge",
        latency_tolerance="nightly", domain="legal",
        description="Second-model reviewer: is the obligation a correct reading of its clause?",
    ),
    "l3.block_generate": TaskSpec(
        "l3.block_generate", "structured_drafting", "high", "medium", 0.82, "l3.generate", "L3",
        task_class="l3_decompose", latency_tolerance="nightly", domain="legal",
        description="Building-block synthesis grounded on live obligation ids",
    ),
    "l3.characterize": TaskSpec(
        "l3.characterize", "structured_drafting", "medium", "medium", 0.85, "l3.characterize", "L3",
        task_class="l3_characterize", latency_tolerance="nightly", domain="legal",
        description="Fill the per-kind characteristics schema of a block",
    ),
    "l4.license_extract": TaskSpec(
        "l4.license_extract", "extraction", "medium", "high", 0.92, "l4.licenses", "L4", task_class="l4_enumerate",
        latency_tolerance="nightly", domain="legal", context_size="large",
        description="Extract license types only from retrieved clause text",
    ),
    "l4.applicability": TaskSpec(
        "l4.applicability", "extraction", "medium", "high", 0.90, "l4.predicates", "L4", task_class="l4_enumerate",
        latency_tolerance="nightly", domain="legal",
        description="Applicability predicate from an obligation's subject/condition, closed-world over the L4 ontology",
    ),
    "profile.intake": TaskSpec(
        "profile.intake", "extraction", "low", "medium", 0.85, "profile.intake", "L4", task_class="l4_enumerate",
        latency_tolerance="interactive", domain="legal",
        description="Front door: read an organisation description into L4 attributes, closed-world over the ontology",
    ),
    "l5.activity_map": TaskSpec(
        "l5.activity_map", "graph_mapping", "medium", "medium", 0.84, "l5.map", "L5", task_class="l5_map",
        latency_tolerance="nightly", domain="legal",
        description="Closed-world obligation↔activity mapping",
    ),
    "l6.rationale": TaskSpec(
        "l6.rationale", "long_reasoning", "medium", "medium", 0.80, "l6.narrate", "L6", task_class="l6_explain",
        latency_tolerance="interactive", description="Citation-checked program rationale",
    ),
    "l7.narrative": TaskSpec(
        "l7.narrative", "long_reasoning", "medium", "medium", 0.82, "l7.narrate", "L7", task_class="l7_score",
        latency_tolerance="nightly", domain="risk-quant",
        description="Number-echo risk commentary over formula outputs",
    ),
    "l7.link": TaskSpec(
        "l7.link", "graph_mapping", "medium", "medium", 0.85, "l7.link", "L7", task_class="l7_score",
        latency_tolerance="nightly", domain="legal",
        description="Closed-world enforcement notice → obligation linking, quote-bound",
    ),
    "l8.fill": TaskSpec(
        "l8.fill", "structured_drafting", "medium", "medium", 0.85, "l8.fill", "L8", task_class="l8_fill",
        latency_tolerance="nightly", description="Endorsed fill drafting against a blueprint item",
    ),
    "l0.revalidate": TaskSpec(
        "l0.revalidate", "judging", "high", "high", 0.90, "l0.referee", "L0", task_class="judge",
        latency_tolerance="hours", domain="legal",
        description="Correction revalidation judge — premium-eligible",
    ),
    "eval.judge": TaskSpec(
        "eval.judge", "judging", "medium", "medium", 0.85, "eval.studio", "L0", task_class="judge",
        latency_tolerance="interactive", description="Eval Studio disagreement judge",
    ),
}

for _spec in TASKS.values():
    if _spec.task_class not in TASK_CLASSES:
        raise RuntimeError(f"task {_spec.id} references unknown task class {_spec.task_class}")

# Seeded from published-style benches; Eval Studio overwrites with agreement scores.
SEED_QUALITY: dict[tuple[str, str], float] = {
    ("dummy.triage", NOVA_LITE): 0.93,
    ("l1.parse_repair", NOVA_LITE): 0.86,
    ("l1.parse_repair", NOVA_PRO): 0.91,
    ("l1.parse_repair", GPT_OSS_120B): 0.95,
    ("l1.annotate", NOVA_LITE): 0.84,
    ("l1.change", GPT_OSS_120B): 0.92,
    ("l2.duty_triage", GPT_OSS_120B): 0.92,
    ("l2.extract", GPT_OSS_120B): 0.90,
    ("l2.extract", MISTRAL_LARGE_3): 0.93,
    ("l2.consolidate", CLAUDE_SONNET_5): 0.94,
    ("l2.change", GPT_OSS_120B): 0.91,
    ("l3.block_generate", CLAUDE_SONNET_5): 0.93,
    ("l3.characterize", GPT_OSS_120B): 0.88,
    ("l4.license_extract", GPT_OSS_120B): 0.93,
    ("l4.applicability", GPT_OSS_120B): 0.91,
    ("l5.activity_map", GPT_OSS_120B): 0.90,
    ("l6.rationale", CLAUDE_SONNET_5): 0.92,
    ("l7.narrative", GPT_OSS_120B): 0.89,
    ("l7.link", GPT_OSS_120B): 0.90,
    ("l8.fill", CLAUDE_SONNET_5): 0.90,
    ("l0.revalidate", CLAUDE_SONNET_5): 0.93,
    ("l0.revalidate", CLAUDE_OPUS_5): 0.97,
    ("eval.judge", NOVA_LITE): 0.86,
    ("l2.review", CLAUDE_SONNET_5): 0.93,
}

# Default quality by ladder position when neither seed nor Eval Studio has a figure:
# the ladder is ordered cheapest → most capable.
RUNG_DEFAULT_QUALITY = (0.86, 0.90, 0.94, 0.97)


@dataclass
class RoutingDecision:
    task_id: str
    chosen_tier: str  # the task class (ledger column `tier`)
    chosen_model: str
    provider_name: str
    quality: float
    threshold: float
    rejected: list[dict]
    reason: str
    deferred: bool = False
    task_class: str = ""
    ladder: list[str] = field(default_factory=list)
    ladder_source: str = "default"  # default | frozen (release model manifest)

    def as_dict(self) -> dict:
        return asdict(self)


def _configured_secret(value: str | None) -> bool:
    return bool(value) and value != "CHANGEME"


def build_providers(settings=None) -> dict[str, Provider]:
    """The consumer's provider, or ``fake`` for the offline quickstart."""
    from app.clhear.platform.gateway import AnthropicProvider, BedrockProvider, OpenAICompatibleProvider

    settings = settings or get_settings()
    mode = (settings.clhear_llm_provider or "").lower()
    model = settings.clhear_llm_model or settings.bedrock_model_id
    if mode == "fake":
        return {"fake": FakeProvider()}
    out: dict[str, Provider] = {}
    if mode == "anthropic" and _configured_secret(settings.anthropic_api_key):
        out["anthropic"] = AnthropicProvider(settings.anthropic_api_key, model or AnthropicProvider.DEFAULT_MODEL)
    elif mode == "openai_compatible" and settings.openai_base_url and _configured_secret(settings.openai_api_key):
        out["openai_compatible"] = OpenAICompatibleProvider(settings.openai_base_url, settings.openai_api_key, model or "gpt-4o")
    elif mode == "bedrock" and model:
        out["bedrock"] = BedrockProvider(model, region=settings.aws_region)
    if not out:
        log.error(NO_PROVIDER_REASON)
    return out


def live_llm(engine: Engine) -> Router | None:
    """CLI/worker helper: Router over the configured provider."""
    providers = build_providers()
    if not providers:
        return None
    return Router(engine, providers=providers)


def record_missing_providers(engine: Engine | None) -> None:
    """Persist a loud ai_ops row when production has no real provider."""
    if engine is None:
        return
    try:
        from app.clhear import ai_ops

        ai_ops.record(
            engine,
            kind="provider_missing",
            layer="L0",
            fleet="router",
            reasoning=NO_PROVIDER_REASON,
            detail={"providers": []},
        )
    except Exception:
        log.exception("ai_ops provider_missing write failed")


def seed_quality(engine: Engine) -> int:
    """Idempotent seed of the quality table from SEED_QUALITY."""
    written = 0
    now = datetime.now(timezone.utc)
    with engine.begin() as conn:
        for (task_id, model), quality in SEED_QUALITY.items():
            exists = conn.execute(
                sa.select(router_quality.c.task_id)
                .where(router_quality.c.task_id == task_id)
                .where(router_quality.c.model == model)
            ).first()
            if exists:
                continue
            conn.execute(
                router_quality.insert().values(
                    task_id=task_id, model=model, quality=quality, n_samples=0,
                    source="seed", updated_at=now,
                )
            )
            written += 1
    return written


def upsert_quality(engine: Engine, task_id: str, model: str, quality: float, n_samples: int, source: str = "eval_studio") -> None:
    now = datetime.now(timezone.utc)
    with engine.begin() as conn:
        exists = conn.execute(
            sa.select(router_quality.c.task_id)
            .where(router_quality.c.task_id == task_id)
            .where(router_quality.c.model == model)
        ).first()
        if exists:
            conn.execute(
                router_quality.update()
                .where(router_quality.c.task_id == task_id)
                .where(router_quality.c.model == model)
                .values(quality=quality, n_samples=n_samples, source=source, updated_at=now)
            )
        else:
            conn.execute(
                router_quality.insert().values(
                    task_id=task_id, model=model, quality=quality, n_samples=n_samples,
                    source=source, updated_at=now,
                )
            )


def quality_table(engine: Engine) -> dict[tuple[str, str], float]:
    seed_quality(engine)
    out = dict(SEED_QUALITY)
    with engine.connect() as conn:
        for row in conn.execute(sa.select(router_quality)):
            out[(row.task_id, row.model)] = float(row.quality)
    return out


class Router:
    """Cheapest sufficient rung of the task class ladder, on Infer.

    ``model_manifest`` (the release-frozen manifest from
    :mod:`app.clhear.platform.manifest`) pins the ladder per task class so a
    replay of a release routes exactly as the release did (I3, I6)."""

    def __init__(
        self,
        engine: Engine,
        providers: dict[str, Provider] | None = None,
        quality: dict[tuple[str, str], float] | None = None,
        gateway: Gateway | None = None,
        model_manifest: dict | None = None,
    ):
        self.engine = engine
        self.providers = providers if providers is not None else build_providers()
        if self.providers:
            lead = next(iter(self.providers.values()))
        else:
            record_missing_providers(engine)
            lead = _UnconfiguredProvider()
        self.gateway = gateway or Gateway(engine, lead)
        self._quality_override = quality
        self.model_manifest = model_manifest

    # ---------------------------------------------------------------- ladders
    def ladder_for(self, task_class: str) -> tuple[list[str], str]:
        provider = self._provider_name()
        if provider in CONSUMER_PROVIDERS:
            configured = getattr(self.providers.get(provider), "model", "") if self.providers else ""
            model = configured or get_settings().clhear_llm_model or get_settings().bedrock_model_id
            return [model], "configured"
        classes = (self.model_manifest or {}).get("task_classes") or {}
        entry = classes.get(task_class)
        if entry and entry.get("ladder"):
            ladder = list(entry["ladder"])
            # the frozen model is the rung the release actually used; try it first
            frozen = entry.get("model_id")
            if frozen in ladder:
                ladder.remove(frozen)
                ladder.insert(0, frozen)
            return ladder, "frozen"
        return list(default_ladder(task_class)), "default"

    def _provider_name(self) -> str:
        return next(iter(self.providers), "unconfigured")

    def _quality(self, task_id: str, model: str, rung: int) -> float:
        table = self._quality_override if self._quality_override is not None else quality_table(self.engine)
        if (task_id, model) in table:
            return table[(task_id, model)]
        return RUNG_DEFAULT_QUALITY[min(rung, len(RUNG_DEFAULT_QUALITY) - 1)]

    def _premium_ok(self, task: TaskSpec) -> tuple[bool, str]:
        if task.criticality != "high":
            return False, f"criticality {task.criticality} — premium rung reserved for high"
        try:
            spent = self.gateway.premium_spend_month()
        except Exception:
            spent = 0.0
        cap = get_settings().clhear_frontier_monthly_cap_usd
        if spent >= cap:
            return False, f"premium monthly cap ${cap} exhausted (${spent:.2f} spent)"
        return True, "premium eligible"

    # ---------------------------------------------------------------- decide
    def decide(self, task_id: str) -> RoutingDecision:
        if task_id not in TASKS:
            raise KeyError(f"unknown task {task_id}")
        task = TASKS[task_id]
        if not self.providers:
            raise SpendCapExceeded(f"no provider available for task {task_id}: {NO_PROVIDER_REASON}")
        ladder, source = self.ladder_for(task.task_class)
        if task.task_class in DERIVATION_CLASSES and self._provider_name() not in CONSUMER_PROVIDERS and self._provider_name() != "fake":
            dirty = [m for m in ladder if not is_procurement_clean(m)]
            if dirty:
                raise ProcurementViolation(
                    f"{task.task_class} ladder contains non-procurement-clean model(s) {dirty} ({[origin_of(m) for m in dirty]})"
                )
        provider_name = self._provider_name()
        rejected: list[dict] = []
        eligible: list[tuple[str, float]] = []
        chosen: RoutingDecision | None = None
        for rung, model in enumerate(ladder):
            quality = self._quality(task_id, model, rung)
            if model in PREMIUM_MODELS:
                ok, why = self._premium_ok(task)
                if not ok:
                    rejected.append({"tier": task.task_class, "rung": rung, "model": model, "quality": quality, "reason": why})
                    continue
            eligible.append((model, quality))
            if quality + 1e-9 < task.quality_threshold:
                rejected.append({
                    "tier": task.task_class, "rung": rung, "model": model, "quality": quality,
                    "reason": f"quality {quality:.2f} < {task.quality_threshold:.2f} threshold",
                })
                continue
            reason = (
                f"{task.id} → {model} ({task.task_class} rung {rung}, {source} ladder): measured quality "
                f"{quality:.2f} ≥ {task.quality_threshold:.2f} threshold"
            )
            if rung + 1 < len(ladder):
                reason += "; higher rungs skipped (cheaper sufficient rung)"
            chosen = RoutingDecision(
                task_id=task_id, chosen_tier=task.task_class, chosen_model=model,
                provider_name=provider_name, quality=quality, threshold=task.quality_threshold,
                rejected=rejected, reason=reason, task_class=task.task_class, ladder=ladder,
                ladder_source=source,
            )
            break
        if chosen is None:
            if not eligible:
                raise SpendCapExceeded(f"no eligible rung for task {task_id}: {[r['reason'] for r in rejected]}")
            # Never fail silently: use the most capable eligible rung and say so.
            model, quality = eligible[-1]
            chosen = RoutingDecision(
                task_id=task_id, chosen_tier=task.task_class, chosen_model=model,
                provider_name=provider_name, quality=quality, threshold=task.quality_threshold,
                rejected=rejected, task_class=task.task_class, ladder=ladder, ladder_source=source,
                reason=(
                    f"{task.id} → {model} ({task.task_class}): no rung met the {task.quality_threshold:.2f} "
                    f"threshold; using the most capable eligible rung (quality {quality:.2f})"
                ),
            )
        return chosen

    def explain(self, task_id: str) -> dict:
        """Route/explain contract: what would run, on which ladder, and why."""
        d = self.decide(task_id)
        task = TASKS[task_id]
        return {
            "task_id": task_id,
            "task_class": task.task_class,
            "derivation_class": task.task_class in DERIVATION_CLASSES,
            "ladder": d.ladder,
            "ladder_source": d.ladder_source,
            "selected": d.chosen_model,
            "origin": origin_of(d.chosen_model),
            "procurement_clean": is_procurement_clean(d.chosen_model),
            "provider": d.provider_name,
            "quality": d.quality,
            "threshold": d.threshold,
            "rejected": d.rejected,
            "reason": d.reason,
        }

    # ---------------------------------------------------------------- run
    def run(
        self,
        task_id: str,
        *,
        prompt: str,
        system: str | None = None,
        max_tokens: int = 1024,
        required_keys: list[str] | None = None,
        json_schema: dict | None = None,
        max_retries: int = 3,
        data_class: str | None = None,
    ) -> LlmResult:
        task = TASKS[task_id]
        decision = self.decide(task_id)
        provider = self.providers.get(decision.provider_name) or next(iter(self.providers.values()))
        temperature = float((task.determinism or {}).get("temperature", 0.0))
        try:
            from app.clhear import ai_ops

            ai_ops.record(
                self.engine,
                kind="router_decision",
                layer=task.layer,
                fleet=task.fleet,
                reasoning=decision.reason,
                detail=decision.as_dict(),
            )
        except Exception:
            log.exception("ai_ops router_decision write failed")
        return self.gateway.call(
            fleet=task.fleet,
            model=decision.chosen_model,
            prompt=prompt,
            system=system,
            max_tokens=max_tokens,
            required_keys=required_keys,
            max_retries=max_retries,
            temperature=temperature,
            json_schema=json_schema,
            provider=provider,
            task_id=task_id,
            tier=decision.chosen_tier,
            rejected_alternatives=decision.rejected,
            routing_reason=decision.reason,
            quality_at_decision=decision.quality,
            task_class=decision.task_class,
            data_class=data_class,
        )

    def call(self, *, fleet: str, model: str, prompt: str, **kwargs) -> LlmResult:
        """Legacy Gateway.call surface — resolve fleet to a registered task."""
        for spec in TASKS.values():
            if spec.fleet == fleet:
                kwargs.pop("model", None)
                return self.run(spec.id, prompt=prompt, **{k: v for k, v in kwargs.items() if k in {
                    "system", "max_tokens", "required_keys", "json_schema", "max_retries", "data_class",
                }})
        return self.gateway.call(fleet=fleet, model=model, prompt=prompt, **kwargs)


def is_router(llm: Any) -> bool:
    return isinstance(llm, Router)


def complete(llm: Any, task_id: str, **kwargs) -> LlmResult:
    """Call through the router when we have one; Gateway.call for legacy tests."""
    kwargs.pop("fleet", None)
    model = kwargs.pop("model", None)
    if is_router(llm):
        allowed = {k: kwargs[k] for k in ("prompt", "system", "max_tokens", "required_keys", "json_schema", "max_retries", "data_class") if k in kwargs}
        return llm.run(task_id, **allowed)
    task = TASKS.get(task_id)
    fleet = task.fleet if task else "unknown"
    model = model or default_ladder(task.task_class if task else "judge")[0]
    return llm.call(fleet=fleet, model=model, task_class=task.task_class if task else None, task_id=task_id, **kwargs)


def last_decisions(engine: Engine, limit: int = 40) -> list[dict]:
    with engine.connect() as conn:
        rows = conn.execute(
            sa.select(llm_calls).where(llm_calls.c.task_id.isnot(None)).order_by(llm_calls.c.id.desc()).limit(limit)
        ).all()
    out = []
    for row in rows:
        out.append({
            "task_id": row.task_id,
            "model": row.model,
            "tier": row.tier,
            "task_class": row.tier,
            "provider": row.provider,
            "cost_usd": float(row.cost_usd),
            "routing_reason": row.routing_reason,
            "rejected_alternatives": row.rejected_alternatives if isinstance(row.rejected_alternatives, list) else [],
            "quality": float(row.quality_at_decision) if row.quality_at_decision is not None else None,
            "created_at": str(row.created_at),
        })
    return out


def registry_public() -> list[dict]:
    return [
        {
            "id": t.id, "shape": t.shape, "complexity": t.complexity, "criticality": t.criticality,
            "quality_threshold": t.quality_threshold, "fleet": t.fleet, "layer": t.layer,
            "task_class": t.task_class, "latency_tolerance": t.latency_tolerance, "domain": t.domain,
            "description": t.description,
        }
        for t in TASKS.values()
    ]


def tiers_public() -> list[dict]:
    """Task classes with their default ladders (what the public /ai page shows)."""
    return [
        {
            "id": name,
            "ladder": default_ladder(name),
            "model": default_ladder(name)[0],
            "provider": "infer",
            "hosting": "aws-bedrock",
            "derivation_class": name in DERIVATION_CLASSES,
            "origins": [origin_of(m) for m in default_ladder(name)],
        }
        for name in TASK_CLASSES
    ]


class _UnconfiguredProvider:
    """Ledger stub so the worker can ingest L1 without pretending to have a model."""

    name = "unconfigured"

    def complete(self, **kwargs):
        raise RuntimeError(NO_PROVIDER_REASON)
