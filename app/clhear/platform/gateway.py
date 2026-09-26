# Copyright (C) 2026 Reg42 AI
# This file is part of CLHEAR. See LICENSE (AGPL-3.0-only).
"""LLM provider abstraction, spend caps, call ledger (HLD v2 §3, I6).

The inference router (`router.run`) is the only production entry; this module
is the provider + ledger layer underneath it. The only production provider is
:class:`InferProvider` (the configured model provider → Bedrock). Every call is logged to
l0_platform.llm_calls; daily fleet/global caps and the monthly premium cap are
hard stops.
"""
import hashlib
import json
import logging
import re
import time
from dataclasses import dataclass, replace
from datetime import datetime, timezone
from typing import Any, Callable, Protocol

import sqlalchemy as sa
from sqlalchemy.engine import Engine

from app.clhear.models import llm_calls
from app.clhear.platform.task_classes import (
    CLAUDE_OPUS_5,
    CLAUDE_SONNET_5,
    COHERE_EMBED_MULTI,
    DEEPSEEK_R1,
    GPT_OSS_120B,
    LLAMA_3_3_70B,
    MISTRAL_LARGE_3,
    NOVA_LITE,
    NOVA_PRO,
    QWEN_3_5_32B,
    TITAN_EMBED_V2,
)
from app.clhear.settings import get_settings

log = logging.getLogger("clhear.gateway")


class SpendCapExceeded(RuntimeError):
    pass


class StructuredOutputError(RuntimeError):
    pass


@dataclass(frozen=True)
class LlmResult:
    text: str
    model: str
    provider: str
    input_tokens: int
    output_tokens: int
    cost_usd: float
    # Empty means unreported, never inferred from the requested model/config.
    model_reported: bool = False
    finish_reason: str | None = None
    request_id: str | None = None
    call_id: int | None = None


class Provider(Protocol):
    name: str

    def complete(
        self,
        *,
        model: str,
        prompt: str,
        system: str | None,
        max_tokens: int,
        temperature: float = 0.0,
        json_schema: dict | None = None,
        task_class: str | None = None,
        data_class: str | None = None,
    ) -> LlmResult: ...


# USD per 1M tokens (input, output) — Bedrock on-demand list prices for the
# frozen model ids. Infer returns `usage.cost_usd` when it has the exact figure;
# this table is the ledger fallback so every call has a cost.
BEDROCK_PRICING: dict[str, tuple[float, float]] = {
    GPT_OSS_120B: (0.15, 0.60),
    MISTRAL_LARGE_3: (2.00, 6.00),
    CLAUDE_SONNET_5: (3.00, 15.00),
    CLAUDE_OPUS_5: (15.00, 75.00),
    NOVA_PRO: (0.80, 3.20),
    NOVA_LITE: (0.06, 0.24),
    LLAMA_3_3_70B: (0.72, 0.72),
    QWEN_3_5_32B: (0.15, 0.60),
    DEEPSEEK_R1: (1.35, 5.40),
    # embeddings: input tokens only (no generation)
    TITAN_EMBED_V2: (0.02, 0.0),
    COHERE_EMBED_MULTI: (0.10, 0.0),
}
# Rungs counted against the monthly premium cap (settings.clhear_frontier_monthly_cap_usd).
PREMIUM_MODELS: frozenset[str] = frozenset({CLAUDE_OPUS_5})
_DEFAULT_PRICING = (3.00, 15.00)
_THINK_RE = re.compile(r"<think>.*?</think>", re.S | re.I)
_THINK_OPEN_RE = re.compile(r"<think>.*", re.S | re.I)
_FENCE_RE = re.compile(r"^```(?:json)?\s*|\s*```$", re.I)


def parse_json_object(text: str) -> dict:
    """Parse a JSON object out of model text (think tags, fences, leading prose)."""
    raw = (text or "").strip()
    raw = _THINK_RE.sub("", raw)
    raw = _THINK_OPEN_RE.sub("", raw)
    raw = _FENCE_RE.sub("", raw.strip()).strip()
    try:
        parsed = json.loads(raw)
    except json.JSONDecodeError:
        start, end = raw.find("{"), raw.rfind("}")
        if start < 0 or end <= start:
            raise
        parsed = json.loads(raw[start : end + 1])
    if not isinstance(parsed, dict):
        raise StructuredOutputError("response is not a JSON object")
    return parsed


def price_for(model: str) -> tuple[float, float]:
    return BEDROCK_PRICING.get(model, _DEFAULT_PRICING)


class ProviderError(RuntimeError):
    pass


class InferError(ProviderError):
    pass


class InferProvider:
    """the configured model provider — the only production provider (HLD v2 I6, §9).

    OpenAI-compatible wire (`/chat/completions`) fronting Bedrock inside the
    account. Every request carries the employee id, the CLHEAR task class and the
    data class; Infer applies the task-class ladder (fallback, procurement policy)
    and reports the model it actually used, which is what the ledger and the
    release model manifest record.
    """

    name = "infer"

    def __init__(
        self,
        base_url: str | None = None,
        token: str | None = None,
        *,
        employee_id: str | None = None,
        data_class: str | None = None,
        timeout: float = 180.0,
        client=None,
    ):
        settings = get_settings()
        self._base_url = (base_url or settings.infer_base_url or "").rstrip("/")
        self._token = token if token is not None else settings.infer_token
        if not self._base_url:
            raise RuntimeError("INFER_BASE_URL is not configured")
        if not self._token or self._token == "CHANGEME":
            raise RuntimeError("INFER_TOKEN is not configured")
        self.employee_id = employee_id or settings.infer_employee_id
        self.data_class = data_class or settings.infer_data_class
        self._timeout = timeout
        self._client = client

    def _headers(self, task_class: str | None, data_class: str | None = None) -> dict[str, str]:
        if data_class is not None and data_class not in {"public", "members", "restricted"}:
            raise ValueError("Unsupported inference data classification")
        headers = {
            "content-type": "application/json",
            "authorization": f"Bearer {self._token}",
            "X-Employee-Id": self.employee_id,
            "X-Data-Class": data_class or self.data_class,
        }
        if task_class:
            headers["X-Task-Class"] = task_class
        return headers

    def _http(self):
        if self._client is None:
            import httpx

            self._client = httpx.Client(timeout=self._timeout)
        return self._client

    def complete(
        self,
        *,
        model: str,
        prompt: str,
        system: str | None,
        max_tokens: int,
        temperature: float = 0.0,
        json_schema: dict | None = None,
        task_class: str | None = None,
        data_class: str | None = None,
    ) -> LlmResult:
        messages = []
        if system:
            messages.append({"role": "system", "content": system})
        messages.append({"role": "user", "content": prompt})
        body: dict[str, Any] = {
            "model": model,
            "messages": messages,
            "max_tokens": max_tokens,
            "temperature": temperature,
        }
        if json_schema:
            body["response_format"] = {
                "type": "json_schema",
                "json_schema": {"name": "clhear", "schema": json_schema, "strict": False},
            }
        else:
            body["response_format"] = {"type": "json_object"}
        if task_class:
            body["metadata"] = {"task_class": task_class, "employee_id": self.employee_id}
        resp = self._http().post(f"{self._base_url}/chat/completions", headers=self._headers(task_class, data_class), json=body)
        if resp.status_code >= 400:
            raise InferError(f"infer {resp.status_code}: {resp.text[:300]}")
        data = resp.json()
        try:
            text = data["choices"][0]["message"].get("content") or ""
        except (KeyError, IndexError, TypeError) as exc:
            raise InferError(f"malformed infer response: {str(data)[:200]}") from exc
        used_model = str(data.get("model") or model)
        usage = data.get("usage") or {}
        in_tok = int(usage.get("prompt_tokens") or usage.get("input_tokens") or 0)
        out_tok = int(usage.get("completion_tokens") or usage.get("output_tokens") or 0)
        if usage.get("cost_usd") is not None:
            cost = float(usage["cost_usd"])
        else:
            price_in, price_out = price_for(used_model)
            cost = (in_tok * price_in + out_tok * price_out) / 1_000_000
        return LlmResult(
            text=text, model=used_model, provider=self.name,
            input_tokens=in_tok, output_tokens=out_tok, cost_usd=cost,
            model_reported=bool(data.get("model")),
            finish_reason=data["choices"][0].get("finish_reason"),
            request_id=str(data["id"]) if data.get("id") else None,
        )

    def route_explain(self, task_class: str) -> dict:
        """`GET /route/explain?task_class=` — the ladder Infer will apply and the
        rung it would select now. Used to freeze the release model manifest."""
        resp = self._http().get(
            f"{self._base_url}/route/explain",
            headers=self._headers(task_class),
            params={"task_class": task_class, "employee_id": self.employee_id},
        )
        if resp.status_code >= 400:
            raise InferError(f"infer route/explain {resp.status_code}: {resp.text[:300]}")
        data = resp.json()
        if not isinstance(data, dict):
            raise InferError("route/explain did not return an object")
        return data

    def models(self) -> list[dict]:
        resp = self._http().get(f"{self._base_url}/models", headers=self._headers(None))
        if resp.status_code >= 400:
            raise InferError(f"infer models {resp.status_code}: {resp.text[:300]}")
        data = resp.json()
        return list(data.get("data") or []) if isinstance(data, dict) else list(data)


class _ChatProvider:
    """Shared JSON chat completion used by the consumer providers."""

    name = "chat"

    def __init__(self, model: str, timeout: float = 180.0, client=None):
        self.model = model
        self._timeout = timeout
        self._client = client

    def _http(self):
        if self._client is None:
            import httpx

            self._client = httpx.Client(timeout=self._timeout)
        return self._client


class AnthropicProvider(_ChatProvider):
    """Anthropic Messages API. The consumer supplies the key and model."""

    name = "anthropic"

    def __init__(self, api_key: str, model: str, *, timeout: float = 180.0, client=None):
        super().__init__(model, timeout=timeout, client=client)
        if not api_key or api_key == "CHANGEME":
            raise ProviderError("ANTHROPIC_API_KEY is not configured")
        self._api_key = api_key

    def complete(self, *, model: str, prompt: str, system: str | None, max_tokens: int,
                 temperature: float = 0.0, json_schema: dict | None = None,
                 task_class: str | None = None, data_class: str | None = None) -> LlmResult:
        body: dict[str, Any] = {
            "model": model or self.model,
            "max_tokens": max_tokens,
            "temperature": temperature,
            "messages": [{"role": "user", "content": prompt}],
        }
        if system:
            body["system"] = system
        resp = self._http().post(
            "https://api.anthropic.com/v1/messages",
            headers={
                "x-api-key": self._api_key,
                "anthropic-version": "2023-06-01",
                "content-type": "application/json",
            },
            json=body,
        )
        if resp.status_code >= 400:
            raise ProviderError(f"anthropic {resp.status_code}: {resp.text[:300]}")
        data = resp.json()
        parts = data.get("content") or []
        text = "".join(part.get("text", "") for part in parts if isinstance(part, dict))
        usage = data.get("usage") or {}
        in_tok = int(usage.get("input_tokens") or 0)
        out_tok = int(usage.get("output_tokens") or 0)
        price_in, price_out = price_for(body["model"])
        return LlmResult(
            text=text, model=str(data.get("model") or body["model"]), provider=self.name,
            input_tokens=in_tok, output_tokens=out_tok,
            cost_usd=(in_tok * price_in + out_tok * price_out) / 1_000_000,
            model_reported=bool(data.get("model")), finish_reason=data.get("stop_reason"),
            request_id=str(data["id"]) if data.get("id") else None,
        )


class OpenAICompatibleProvider(_ChatProvider):
    """Any service that speaks the OpenAI chat-completions API."""

    name = "openai_compatible"

    def __init__(self, base_url: str, api_key: str, model: str, *, timeout: float = 180.0, client=None):
        super().__init__(model, timeout=timeout, client=client)
        if not base_url:
            raise ProviderError("OPENAI_BASE_URL is not configured")
        if not api_key or api_key == "CHANGEME":
            raise ProviderError("OPENAI_API_KEY is not configured")
        self._base_url = base_url.rstrip("/")
        self._api_key = api_key

    def complete(self, *, model: str, prompt: str, system: str | None, max_tokens: int,
                 temperature: float = 0.0, json_schema: dict | None = None,
                 task_class: str | None = None, data_class: str | None = None) -> LlmResult:
        messages = []
        if system:
            messages.append({"role": "system", "content": system})
        messages.append({"role": "user", "content": prompt})
        used = model or self.model
        body: dict[str, Any] = {
            "model": used, "messages": messages, "max_tokens": max_tokens, "temperature": temperature,
        }
        if json_schema:
            body["response_format"] = {"type": "json_object"}
        resp = self._http().post(
            f"{self._base_url}/chat/completions",
            headers={"authorization": f"Bearer {self._api_key}", "content-type": "application/json"},
            json=body,
        )
        if resp.status_code >= 400:
            raise ProviderError(f"openai_compatible {resp.status_code}: {resp.text[:300]}")
        data = resp.json()
        try:
            text = data["choices"][0]["message"].get("content") or ""
        except (KeyError, IndexError, TypeError) as exc:
            raise ProviderError(f"malformed chat response: {str(data)[:200]}") from exc
        usage = data.get("usage") or {}
        in_tok = int(usage.get("prompt_tokens") or 0)
        out_tok = int(usage.get("completion_tokens") or 0)
        price_in, price_out = price_for(str(data.get("model") or used))
        return LlmResult(
            text=text, model=str(data.get("model") or used), provider=self.name,
            input_tokens=in_tok, output_tokens=out_tok,
            cost_usd=(in_tok * price_in + out_tok * price_out) / 1_000_000,
            model_reported=bool(data.get("model")),
            finish_reason=(data.get("choices") or [{}])[0].get("finish_reason"),
            request_id=str(data["id"]) if data.get("id") else None,
        )


class BedrockProvider:
    """AWS Bedrock in the consumer's account. The consumer supplies the role."""

    name = "bedrock"

    def __init__(self, model: str, *, region: str = "", client=None):
        if not model:
            raise ProviderError("CLHEAR_LLM_MODEL or BEDROCK_MODEL_ID is not configured")
        self.model = model
        self.region = region
        self._client = client

    def _runtime(self):
        if self._client is None:
            import boto3

            self._client = boto3.client("bedrock-runtime", region_name=self.region or None)
        return self._client

    def complete(self, *, model: str, prompt: str, system: str | None, max_tokens: int,
                 temperature: float = 0.0, json_schema: dict | None = None,
                 task_class: str | None = None, data_class: str | None = None) -> LlmResult:
        used = model or self.model
        kwargs: dict[str, Any] = {
            "modelId": used,
            "messages": [{"role": "user", "content": [{"text": prompt}]}],
            "inferenceConfig": {"maxTokens": max_tokens, "temperature": temperature},
        }
        if system:
            kwargs["system"] = [{"text": system}]
        data = self._runtime().converse(**kwargs)
        parts = ((data.get("output") or {}).get("message") or {}).get("content") or []
        text = "".join(part.get("text", "") for part in parts if isinstance(part, dict))
        usage = data.get("usage") or {}
        in_tok = int(usage.get("inputTokens") or 0)
        out_tok = int(usage.get("outputTokens") or 0)
        price_in, price_out = price_for(used)
        return LlmResult(
            text=text, model=used, provider=self.name, input_tokens=in_tok, output_tokens=out_tok,
            cost_usd=(in_tok * price_in + out_tok * price_out) / 1_000_000,
            model_reported=True, finish_reason=data.get("stopReason"),
        )


class FakeProvider:
    """Deterministic offline provider for tests and the dummy-fleet rehearsal."""

    name = "fake"

    def __init__(
        self,
        canned_text: str = '{"classification": "relevant", "confidence": 0.9}',
        script: Callable[..., str] | None = None,
    ):
        self.canned_text = canned_text
        self.script = script
        self.calls = 0
        self.last_kwargs: dict = {}

    def complete(
        self,
        *,
        model: str,
        prompt: str,
        system: str | None,
        max_tokens: int,
        temperature: float = 0.0,
        json_schema: dict | None = None,
        task_class: str | None = None,
        data_class: str | None = None,
    ) -> LlmResult:
        self.calls += 1
        self.last_kwargs = {
            "model": model, "prompt": prompt, "system": system,
            "max_tokens": max_tokens, "temperature": temperature, "json_schema": json_schema,
            "task_class": task_class,
            "data_class": data_class,
        }
        text = self.script(prompt=prompt, system=system, model=model) if self.script else self.canned_text
        return LlmResult(
            text=text, model=model, provider=self.name,
            input_tokens=max(1, len(prompt) // 4), output_tokens=max(1, len(text) // 4),
            cost_usd=0.0001,
            model_reported=True, finish_reason="stop", request_id=f"fixture-{self.calls}",
        )


def _day_start_utc() -> datetime:
    now = datetime.now(timezone.utc)
    return now.replace(hour=0, minute=0, second=0, microsecond=0)


def _month_start_utc() -> datetime:
    now = datetime.now(timezone.utc)
    return now.replace(day=1, hour=0, minute=0, second=0, microsecond=0)


class Gateway:
    def __init__(
        self,
        engine: Engine,
        provider: Provider,
        fleet_daily_cap_usd: float | None = None,
        global_daily_cap_usd: float | None = None,
        frontier_monthly_cap_usd: float | None = None,
    ):
        settings = get_settings()
        self._engine = engine
        self._provider = provider
        self._fleet_cap = fleet_daily_cap_usd if fleet_daily_cap_usd is not None else settings.clhear_gateway_fleet_daily_cap_usd
        self._global_cap = global_daily_cap_usd if global_daily_cap_usd is not None else settings.clhear_gateway_global_daily_cap_usd
        self._frontier_month_cap = (
            frontier_monthly_cap_usd
            if frontier_monthly_cap_usd is not None
            else settings.clhear_frontier_monthly_cap_usd
        )

    def _spend_today(self, fleet: str | None = None) -> float:
        query = sa.select(sa.func.coalesce(sa.func.sum(llm_calls.c.cost_usd), 0)).where(
            llm_calls.c.created_at >= _day_start_utc()
        )
        if fleet is not None:
            query = query.where(llm_calls.c.fleet == fleet)
        with self._engine.connect() as conn:
            return float(conn.execute(query).scalar_one())

    def premium_spend_month(self) -> float:
        """Month-to-date spend on premium rungs (Opus-class) — the hard monthly cap."""
        query = sa.select(sa.func.coalesce(sa.func.sum(llm_calls.c.cost_usd), 0)).where(
            llm_calls.c.created_at >= _month_start_utc()
        ).where(sa.or_(llm_calls.c.model.in_(sorted(PREMIUM_MODELS)), llm_calls.c.tier == "frontier"))
        with self._engine.connect() as conn:
            return float(conn.execute(query).scalar_one())

    frontier_spend_month = premium_spend_month

    def call(
        self,
        *,
        fleet: str,
        model: str,
        prompt: str,
        system: str | None = None,
        max_tokens: int = 1024,
        required_keys: list[str] | None = None,
        max_retries: int = 3,
        temperature: float = 0.0,
        json_schema: dict | None = None,
        provider: Provider | None = None,
        task_id: str | None = None,
        tier: str | None = None,
        rejected_alternatives: list | None = None,
        routing_reason: str | None = None,
        quality_at_decision: float | None = None,
        task_class: str | None = None,
        data_class: str | None = None,
    ) -> LlmResult:
        """One gated LLM call: cap check -> provider (retry/backoff) -> ledger.

        If required_keys is given the response must be a JSON object containing
        all of them (structured-output validation), retried within the budget.
        """
        if self._spend_today(fleet) >= self._fleet_cap:
            raise SpendCapExceeded(f"fleet '{fleet}' daily cap ${self._fleet_cap} reached — hard stop")
        if self._spend_today() >= self._global_cap:
            raise SpendCapExceeded(f"global daily cap ${self._global_cap} reached — hard stop")
        if (model in PREMIUM_MODELS or tier == "frontier") and self.premium_spend_month() >= self._frontier_month_cap:
            raise SpendCapExceeded(
                f"premium monthly cap ${self._frontier_month_cap} reached — hard stop"
            )

        actor = provider or self._provider
        last_error: Exception | None = None
        result: LlmResult | None = None
        for attempt in range(max_retries):
            try:
                extra = {"task_class": task_class} if task_class else {}
                if data_class is not None:
                    extra["data_class"] = data_class
                result = actor.complete(
                    model=model, prompt=prompt, system=system, max_tokens=max_tokens,
                    temperature=temperature, json_schema=json_schema, **extra,
                )
                if required_keys is not None:
                    parsed = parse_json_object(result.text)
                    missing = [k for k in required_keys if k not in parsed]
                    if missing:
                        raise StructuredOutputError(f"missing keys: {missing}")
                    result = replace(result, text=json.dumps(parsed))
                break
            except (json.JSONDecodeError, StructuredOutputError, ConnectionError, TimeoutError, ProviderError) as exc:
                last_error = exc
                result = None
                time.sleep(2**attempt * 0.5)
        if result is None:
            raise StructuredOutputError(f"gateway call failed after {max_retries} attempts: {last_error}")

        with self._engine.begin() as conn:
            call_id = conn.execute(
                llm_calls.insert().values(
                    fleet=fleet,
                    provider=result.provider,
                    model=result.model,
                    prompt_hash=hashlib.sha256(prompt.encode()).hexdigest(),
                    input_tokens=result.input_tokens,
                    output_tokens=result.output_tokens,
                    cost_usd=result.cost_usd,
                    task_id=task_id,
                    tier=tier,
                    rejected_alternatives=rejected_alternatives,
                    routing_reason=routing_reason,
                    quality_at_decision=quality_at_decision,
                ).returning(llm_calls.c.id)
            ).scalar_one()
        return replace(result, call_id=call_id)
