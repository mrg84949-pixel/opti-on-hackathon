from __future__ import annotations

import asyncio
import random
import re
from dataclasses import dataclass, field
from typing import Any, Callable

from openai import AsyncOpenAI, RateLimitError

from bot.config import settings
from bot.llm.client_messages import AI_RATE_LIMIT, AI_UNAVAILABLE, TOOL_LOOP_EXHAUSTED
from bot.llm.providers.base import SessionSetup
from bot.llm.tool_schema import execute_tool_call, make_openai_tooling
from bot.logging_config import get_logger

logger = get_logger(__name__)

MAX_TOOL_ROUNDS = 8
DEFAULT_GROQ_BASE_URL = "https://api.groq.com/openai/v1"


def _is_rate_limit_error(exc: Exception) -> bool:
    if isinstance(exc, RateLimitError):
        return True
    status = getattr(exc, "status_code", None)
    text = str(exc).lower()
    return status == 429 or "429" in text or "rate_limit" in text


# Well-formed: <function=name>...</function> or <function=name/>
_FUNCTION_MARKUP_RE = re.compile(r"<function=[^>]*>.*?</function>", re.DOTALL | re.IGNORECASE)
_SELF_CLOSING_FUNCTION_RE = re.compile(r"<function=[^>]*/>", re.IGNORECASE)
# Groq sometimes omits '>' before JSON args: <function=set_customer_name{"name": "X"}</function>
_FUNCTION_MARKUP_NO_GT_RE = re.compile(
    r"<function=[a-zA-Z0-9_]+\s*\{.*?</function>",
    re.DOTALL | re.IGNORECASE,
)
_FUNCTION_OPEN_LEAK_RE = re.compile(r"<function\s*=", re.IGNORECASE)


def strip_function_markup(text: str) -> str:
    """Remove Groq/OpenAI-compat tool markup that leaked into assistant content."""
    cleaned = _FUNCTION_MARKUP_RE.sub("", text or "")
    cleaned = _SELF_CLOSING_FUNCTION_RE.sub("", cleaned)
    cleaned = _FUNCTION_MARKUP_NO_GT_RE.sub("", cleaned)
    return cleaned.strip()


def _extract_tool_use_failed_reply(exc: Exception) -> str | None:
    """Groq may reject malformed tool calls but still include the assistant text."""
    body = getattr(exc, "body", None)
    if not isinstance(body, dict):
        return None
    if body.get("code") != "tool_use_failed":
        return None
    failed = (body.get("failed_generation") or "").strip()
    if not failed:
        return None
    cleaned = strip_function_markup(failed)
    if _FUNCTION_OPEN_LEAK_RE.search(cleaned) or len(cleaned) < 5:
        return None
    return cleaned


def _retry_after_seconds(exc: Exception) -> float | None:
    response = getattr(exc, "response", None)
    headers = getattr(response, "headers", None)
    if not headers:
        return None
    value = None
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
class OpenAISession:
    client: AsyncOpenAI
    model: str
    messages: list[dict[str, Any]]
    tools: list[dict[str, Any]]
    tool_registry: dict[str, Callable[..., Any]]
    pending_tool_calls: list[dict[str, Any]] = field(default_factory=list)


class OpenAICompatProvider:
    """OpenAI-compatible chat API (Groq dev default)."""

    provider_name = "groq"

    def __init__(
        self,
        *,
        api_key: str | None = None,
        base_url: str | None = None,
        model: str | None = None,
    ) -> None:
        self._api_key = api_key if api_key is not None else settings.groq_api_key
        self._base_url = base_url if base_url is not None else settings.groq_base_url
        self._model = model if model is not None else settings.groq_model

    def credentials_configured(self) -> bool:
        return bool(self._api_key)

    def missing_credentials_message(self) -> str:
        return "ИИ временно недоступен: задайте переменную окружения GROQ_API_KEY."

    def _client(self) -> AsyncOpenAI:
        return AsyncOpenAI(api_key=self._api_key, base_url=self._base_url or DEFAULT_GROQ_BASE_URL)

    async def create_session(self, setup: SessionSetup) -> OpenAISession:
        tools, registry = make_openai_tooling(setup.ctx, setup.tools)
        messages: list[dict[str, Any]] = [{"role": "system", "content": setup.system_instruction}]
        return OpenAISession(
            client=self._client(),
            model=self._model,
            messages=messages,
            tools=tools,
            tool_registry=registry,
        )

    async def _create_completion(self, session: OpenAISession) -> Any:
        """Call the chat API with bounded retry+backoff on 429/rate-limit."""
        attempts = max(0, settings.groq_max_retries) + 1
        last_exc: Exception | None = None
        for attempt in range(attempts):
            try:
                return await session.client.chat.completions.create(
                    model=session.model,
                    messages=session.messages,
                    tools=session.tools,
                    tool_choice="auto",
                )
            except Exception as exc:
                last_exc = exc
                if not _is_rate_limit_error(exc) or attempt == attempts - 1:
                    raise
                delay = _retry_after_seconds(exc)
                if delay is None:
                    delay = settings.groq_retry_base_delay * (2 ** attempt)
                    delay += random.uniform(0, settings.groq_retry_base_delay)
                delay = min(delay, settings.groq_retry_max_delay)
                logger.warning(
                    "Groq rate-limited, retrying",
                    extra={
                        "extra_data": {
                            "event": "groq_rate_limit_retry",
                            "attempt": attempt + 1,
                            "max_attempts": attempts,
                            "delay_s": round(delay, 3),
                        }
                    },
                )
                await asyncio.sleep(delay)
        assert last_exc is not None  # pragma: no cover
        raise last_exc

    async def send_turn(self, session: OpenAISession, user_text: str) -> str:
        session.messages.append({"role": "user", "content": user_text})
        tool_use_failed_retried = False
        for _ in range(MAX_TOOL_ROUNDS):
            try:
                response = await self._create_completion(session)
            except Exception as exc:
                recovered = _extract_tool_use_failed_reply(exc)
                if recovered is not None:
                    if not tool_use_failed_retried:
                        tool_use_failed_retried = True
                        logger.warning(
                            "Groq tool_use_failed; retrying completion once",
                            extra={
                                "extra_data": {
                                    "event": "groq_tool_use_failed_retry",
                                    "preview": recovered[:120],
                                }
                            },
                        )
                        continue
                    logger.warning(
                        "Groq tool_use_failed again after retry; using failed_generation text",
                        extra={
                            "extra_data": {
                                "event": "groq_tool_use_failed_recovered",
                                "preview": recovered[:120],
                            }
                        },
                    )
                    return recovered
                raise
            choice = response.choices[0]
            message = choice.message
            assistant_payload: dict[str, Any] = {"role": "assistant", "content": message.content or ""}
            if message.tool_calls:
                assistant_payload["tool_calls"] = [
                    {
                        "id": tc.id,
                        "type": tc.type,
                        "function": {
                            "name": tc.function.name,
                            "arguments": tc.function.arguments or "{}",
                        },
                    }
                    for tc in message.tool_calls
                ]
            session.messages.append(assistant_payload)

            if not message.tool_calls:
                content = strip_function_markup(message.content or "")
                if _FUNCTION_OPEN_LEAK_RE.search(content):
                    return AI_UNAVAILABLE
                return content

            for tool_call in message.tool_calls:
                fn = tool_call.function
                result = await execute_tool_call(session.tool_registry, fn.name, fn.arguments or "{}")
                session.messages.append(
                    {
                        "role": "tool",
                        "tool_call_id": tool_call.id,
                        "content": result,
                    }
                )

        return TOOL_LOOP_EXHAUSTED

    def user_message_for_error(self, exc: Exception) -> str:
        text = str(exc)
        status = getattr(exc, "status_code", None)
        if status == 429 or "429" in text or "rate_limit" in text.lower():
            return AI_RATE_LIMIT
        if status == 401 or "invalid api key" in text.lower() or "authentication" in text.lower():
            return AI_UNAVAILABLE
        return AI_UNAVAILABLE
