# Copyright (C) 2026 Reg42 AI
# This file is part of CLHEAR. See LICENSE (AGPL-3.0-only).
"""Which model provider a live run may use."""
from __future__ import annotations

from app.clhear.settings import Settings, get_settings

LIVE = ("anthropic", "openai_compatible", "bedrock")


def describe(settings: Settings | None = None) -> dict:
    settings = settings or get_settings()
    mode = (settings.clhear_llm_provider or "").lower()
    if mode == "anthropic":
        ready = bool(settings.anthropic_api_key.strip())
        return {"provider": mode, "configured": ready, "live": ready, "offline": False}
    if mode == "openai_compatible":
        ready = bool(settings.openai_base_url.strip() and settings.openai_api_key.strip())
        return {"provider": mode, "configured": ready, "live": ready, "offline": False}
    if mode == "bedrock":
        ready = bool((settings.bedrock_model_id or settings.clhear_llm_model).strip())
        return {"provider": mode, "configured": ready, "live": ready, "offline": False}
    if mode == "fake":
        return {"provider": "fake", "configured": True, "live": False, "offline": True}
    return {"provider": "", "configured": False, "live": False, "offline": False}
