from __future__ import annotations

from bot.config import settings
from bot.db.models import Organization
from bot.llm.providers.base import LLMProvider
from bot.llm.providers.gemini import GeminiProvider
from bot.llm.providers.registry import resolve_ai_model, resolve_ai_provider_key
from bot.llm.providers.stub import StubProvider


def get_llm_provider(org: Organization | None = None) -> LLMProvider:
    if settings.ai_use_stub:
        return StubProvider()
    key = resolve_ai_provider_key(org)
    model = resolve_ai_model(org, key)
    if key == "stub":
        return StubProvider()
    if key == "groq":
        from bot.llm.providers.openai_compat import OpenAICompatProvider

        return OpenAICompatProvider(model=model)
    return GeminiProvider(model=model)
