# Copyright (C) 2026 Reg42 AI
# This file is part of CLHEAR. See LICENSE (AGPL-3.0-only).
"""Model calls: the right model id, parameters each model accepts, honest retries."""
from __future__ import annotations

import json

import anthropic
import httpx2
import pytest

from app.clhear.platform import gateway as gw
from app.clhear.platform.gateway import AnthropicProvider, Gateway, ProviderError, StructuredOutputError


def _message(text='{"ok": true}', *, model="claude-opus-5", stop_reason="end_turn"):
    return {"id": "msg_1", "type": "message", "role": "assistant", "model": model,
            "content": [{"type": "text", "text": text}], "stop_reason": stop_reason, "stop_sequence": None,
            "usage": {"input_tokens": 10, "output_tokens": 5}}


class Recorder:
    def __init__(self, responses):
        self.responses = list(responses)
        self.requests = []

    def __call__(self, request):
        self.requests.append(request)
        status, body, headers = self.responses.pop(0)
        return httpx2.Response(status, json=body, headers=headers)


def _provider(recorder, model="claude-opus-5", **kwargs):
    client = anthropic.Anthropic(api_key="test-key", max_retries=0,
                                 http_client=anthropic.DefaultHttpxClient(transport=httpx2.MockTransport(recorder)))
    return AnthropicProvider("test-key", model, client=client, **kwargs)


def _body(request):
    return json.loads(request.content)


def test_the_default_model_is_current_and_never_a_placeholder(install, monkeypatch):
    monkeypatch.setenv("CLHEAR_LLM_PROVIDER", "anthropic")
    monkeypatch.setenv("ANTHROPIC_API_KEY", "test-key")
    from app.clhear.platform.router import Router, build_providers
    from app.clhear.runtime import engine, reset

    reset()
    providers = build_providers()
    assert providers["anthropic"].model == "claude-opus-5"
    ladder, source = Router(engine(), providers=providers).ladder_for("judge")
    assert ladder == ["claude-opus-5"] and source == "configured"


def test_opus_5_request_uses_effort_fallbacks_and_no_sampling(install):
    recorder = Recorder([(200, _message(), {})])
    result = _provider(recorder).complete(model="", prompt="hi", system="sys", max_tokens=300)
    request = recorder.requests[0]
    body = _body(request)
    assert body["model"] == "claude-opus-5"
    assert "temperature" not in body
    assert body["output_config"] == {"effort": "medium"}
    assert body["max_tokens"] >= 16000
    assert body["fallbacks"] == "default"
    assert "server-side-fallback-2026-07-01" in request.headers.get("anthropic-beta", "")
    assert result.text == '{"ok": true}' and result.cost_usd > 0


def test_older_models_keep_temperature_and_skip_effort(install):
    recorder = Recorder([(200, _message(model="claude-haiku-4-5"), {})])
    _provider(recorder, model="claude-haiku-4-5").complete(model="", prompt="hi", system=None, max_tokens=300)
    body = _body(recorder.requests[0])
    assert body["temperature"] == 0.0
    assert "output_config" not in body and "fallbacks" not in body


@pytest.mark.parametrize("model,sampling", [
    ("claude-opus-5", False), ("claude-sonnet-5", False), ("claude-opus-4-7", False), ("claude-fable-5-1", False),
    ("us.anthropic.claude-opus-5-v1:0", False), ("claude-sonnet-4-6", True), ("claude-haiku-4-5", True),
    ("anthropic.claude-3-5-sonnet-20240620-v1:0", True), ("gpt-4o", True),
])
def test_sampling_rules(model, sampling):
    assert gw.accepts_sampling(model) is sampling


def _gateway(provider):
    from app.clhear.runtime import engine

    return Gateway(engine(), provider)


def test_a_bad_request_is_not_retried(install, monkeypatch):
    monkeypatch.setattr(gw.time, "sleep", lambda s: None)
    recorder = Recorder([(400, {"type": "error", "error": {"type": "invalid_request_error", "message": "bad"}}, {})] * 3)
    gateway = _gateway(_provider(recorder))
    with pytest.raises(StructuredOutputError, match="400"):
        gateway.call(fleet="l2", model="claude-opus-5", prompt="hi", max_retries=3)
    assert len(recorder.requests) == 1
    assert gateway.stats["failed"] == 1 and "400" in gateway.stats["last_error"]


def test_overload_and_rate_limits_are_retried_with_the_servers_delay(install, monkeypatch):
    slept = []
    monkeypatch.setattr(gw.time, "sleep", slept.append)
    error = {"type": "error", "error": {"type": "overloaded_error", "message": "busy"}}
    recorder = Recorder([(529, error, {}), (429, {**error, "error": {"type": "rate_limit_error", "message": "slow"}},
                                           {"retry-after": "7"}), (200, _message(), {})])
    result = _gateway(_provider(recorder)).call(fleet="l2", model="claude-opus-5", prompt="hi",
                                                required_keys=["ok"], max_retries=3)
    assert json.loads(result.text) == {"ok": True}
    assert len(recorder.requests) == 3 and slept[-1] == 7.0


def test_a_refusal_is_final(install, monkeypatch):
    monkeypatch.setattr(gw.time, "sleep", lambda s: None)
    recorder = Recorder([(200, _message(text="", stop_reason="refusal"), {})] * 3)
    with pytest.raises(StructuredOutputError, match="refusal"):
        _gateway(_provider(recorder)).call(fleet="l2", model="claude-opus-5", prompt="hi", max_retries=3)
    assert len(recorder.requests) == 1


def test_json_is_read_from_the_first_object_even_with_trailing_prose():
    text = 'Here you go:\n{"duty": true, "note": "a {brace}"}\nHope that helps {really}.'
    assert gw.parse_json_object(text) == {"duty": True, "note": "a {brace}"}


def test_provider_errors_are_classified():
    assert ProviderError("x").retryable is True
    assert ProviderError("x", retryable=False).retryable is False
