from __future__ import annotations

import asyncio
import inspect
import random
from dataclasses import dataclass
from typing import Any

from google.genai import errors as genai_errors
from google.genai import types

from bot.config import settings
from bot.debug_log import debug_log
from bot.llm import gemini
from bot.llm.client_messages import AI_RATE_LIMIT, AI_UNAVAILABLE
from bot.llm.providers.base import SessionSetup


def _is_rate_limit_error(exc: Exception) -> bool:
    if not isinstance(exc, genai_errors.ClientError):
        return False
    code = getattr(exc, "code", None)
    text = str(exc)
    return code == 429 or "RESOURCE_EXHAUSTED" in text or "code': 429" in text or '"code": 429' in text


def _retry_after_seconds(exc: Exception) -> float | None:
    response = getattr(exc, "response", None)
    headers = getattr(response, "headers", None)
    if not headers:
        return None
    try:
        value = headers.get("retry-after") or headers.get("Retry-After")
    except Exception:
        return None
    if value is None:
        return None
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


@dataclass
class GeminiSession:
    chat: object


class GeminiProvider:
    provider_name = "gemini"

    def __init__(self, *, model: str | None = None) -> None:
        self._model = model if model is not None else gemini.get_model_name()

    def credentials_configured(self) -> bool:
        return gemini.get_client() is not None

    def missing_credentials_message(self) -> str:
        if not settings.gemini_api_key:
            return "ИИ временно недоступен: задайте переменную окружения GEMINI_API_KEY."
        return "ИИ временно недоступен: не удалось создать клиент Gemini (проверьте GEMINI_API_KEY)."

    async def create_session(self, setup: SessionSetup) -> GeminiSession:
        client = gemini.get_client()
        if client is None:
            raise RuntimeError("Gemini client unavailable")
        model_name = self._model
        debug_log(
            run_id="audit-pre",
            hypothesis_id="H2",
            location="bot/llm/providers/gemini.py:create_session",
            message="Model selected for Gemini request",
            data={"model_name": model_name},
        )
        debug_log(
            run_id="audit-pre",
            hypothesis_id="H4",
            location="bot/llm/providers/gemini.py:create_session",
            message="About to create chat session",
            data={"tools_count": len(setup.tools)},
        )
        chat = client.aio.chats.create(
            model=model_name,
            config=types.GenerateContentConfig(
                tools=setup.tools,
                system_instruction=setup.system_instruction,
            ),
        )
        debug_log(
            run_id="audit-pre",
            hypothesis_id="H6",
            location="bot/llm/providers/gemini.py:create_session",
            message="Chat object created",
            data={"is_awaitable": inspect.isawaitable(chat), "chat_type": type(chat).__name__},
        )
        return GeminiSession(chat=chat)

    async def send_turn(self, session: GeminiSession, user_text: str) -> str:
        debug_log(
            run_id="audit-pre",
            hypothesis_id="H4",
            location="bot/llm/providers/gemini.py:send_turn",
            message="About to send chat message",
            data={"chat_type": type(session.chat).__name__, "is_awaitable": inspect.isawaitable(session.chat)},
        )
        attempts = max(0, settings.gemini_max_retries) + 1
        last_exc: Exception | None = None
        response = None
        for attempt in range(attempts):
            try:
                response = await session.chat.send_message(user_text)
                break
            except Exception as exc:
                last_exc = exc
                if not _is_rate_limit_error(exc) or attempt == attempts - 1:
                    raise
                delay = _retry_after_seconds(exc)
                if delay is None:
                    delay = settings.gemini_retry_base_delay * (2**attempt)
                    delay += random.uniform(0, settings.gemini_retry_base_delay)
                delay = min(delay, settings.gemini_retry_max_delay)
                debug_log(
                    run_id="audit-pre",
                    hypothesis_id="H4",
                    location="bot/llm/providers/gemini.py:send_turn",
                    message="Gemini rate-limited, retrying",
                    data={"attempt": attempt + 1, "max_attempts": attempts, "delay_s": round(delay, 3)},
                )
                await asyncio.sleep(delay)
        assert response is not None or last_exc is not None  # pragma: no cover
        debug_log(
            run_id="audit-pre",
            hypothesis_id="H4",
            location="bot/llm/providers/gemini.py:send_turn",
            message="Chat response received",
            data={"has_text": bool(response.text), "response_len": len((response.text or "").strip())},
        )
        return (response.text or "").strip()

    def user_message_for_error(self, exc: Exception) -> str:
        if isinstance(exc, genai_errors.ClientError):
            text = str(exc)
            if "RESOURCE_EXHAUSTED" in text or "code': 429" in text or '"code": 429' in text:
                return AI_RATE_LIMIT
            if "API key not valid" in text:
                return AI_UNAVAILABLE
            return AI_UNAVAILABLE
        return AI_UNAVAILABLE
