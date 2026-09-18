from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from bot.config import settings
from bot.db.models import Organization
from bot.llm.prompts import PROMPT_VERSION

KNOWN_AI_PROVIDER_KEYS = frozenset({"gemini", "groq", "stub"})
AI_CONFIG_KEYS = frozenset({"model"})

_DEFAULT_MODEL_BY_PROVIDER = {
    "gemini": lambda: settings.gemini_model,
    "groq": lambda: settings.groq_model,
    "stub": lambda: "stub",
}


def ai_config(org: Organization | None) -> dict[str, Any]:
    if org is None:
        return {}
    raw = getattr(org, "ai_config", None)
    return dict(raw) if isinstance(raw, dict) else {}


def public_ai_config(org: Organization | None) -> dict[str, str]:
    return {
        key: str(value).strip()
        for key, value in ai_config(org).items()
        if key in AI_CONFIG_KEYS and str(value or "").strip()
    }


def merge_ai_config(org: Organization, patch: dict[str, Any] | None) -> None:
    if not patch:
        return
    current = ai_config(org)
    for key, value in patch.items():
        if key not in AI_CONFIG_KEYS:
            continue
        cleaned = (str(value).strip() if value is not None else "") or None
        if cleaned is None:
            current.pop(key, None)
        else:
            current[key] = cleaned
    org.ai_config = current or None


def resolve_ai_provider_key(org: Organization | None) -> str:
    raw = getattr(org, "ai_provider", None) if org is not None else None
    key = (str(raw).strip().lower() if raw else "") or (settings.ai_provider or "gemini")
    key = key.lower().strip()
    if key not in KNOWN_AI_PROVIDER_KEYS:
        return (settings.ai_provider or "gemini").lower().strip() or "gemini"
    return key


def default_model_for_provider(key: str) -> str:
    factory = _DEFAULT_MODEL_BY_PROVIDER.get(key)
    if factory is None:
        return settings.gemini_model
    return factory()


def resolve_ai_model(org: Organization | None, provider_key: str | None = None) -> str:
    key = provider_key or resolve_ai_provider_key(org)
    override = public_ai_config(org).get("model")
    if override:
        return override
    return default_model_for_provider(key)


def platform_credentials_ready(key: str) -> bool:
    normalized = key.lower().strip()
    if normalized == "stub":
        return True
    if normalized == "gemini":
        return bool((settings.gemini_api_key or "").strip())
    if normalized == "groq":
        return bool((settings.groq_api_key or "").strip())
    return False


def runtime_fingerprint(org: Organization | None) -> str:
    key = resolve_ai_provider_key(org)
    model = resolve_ai_model(org, key)
    return f"{key}|{model}|{PROMPT_VERSION}"


@dataclass(frozen=True)
class AiIntegrationMeta:
    code: str
    display_name: str
    default_model: str
    platform_ready: bool


def list_ai_integration_meta() -> list[AiIntegrationMeta]:
    items: list[AiIntegrationMeta] = []
    for code in sorted(KNOWN_AI_PROVIDER_KEYS):
        items.append(
            AiIntegrationMeta(
                code=code,
                display_name={
                    "gemini": "Google Gemini",
                    "groq": "Groq (OpenAI-compatible)",
                    "stub": "Stub (tests)",
                }.get(code, code),
                default_model=default_model_for_provider(code),
                platform_ready=platform_credentials_ready(code),
            )
        )
    return items
