from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Protocol

from bot.llm.context import TurnContext


class LLMProviderError(Exception):
    """Normalized provider failure with optional user-facing text."""

    def __init__(self, message: str, *, user_message: str | None = None):
        super().__init__(message)
        self.user_message = user_message


class QuotaExceeded(LLMProviderError):
    pass


class InvalidApiKey(LLMProviderError):
    pass


@dataclass
class SessionSetup:
    system_instruction: str
    tools: list[Any]
    ctx: TurnContext
    channel: str = "web"
    tool_mode: str = "booking"


class LLMProvider(Protocol):
    provider_name: str

    def credentials_configured(self) -> bool: ...

    def missing_credentials_message(self) -> str: ...

    async def create_session(self, setup: SessionSetup) -> Any: ...

    async def send_turn(self, session: Any, user_text: str) -> str: ...

    def user_message_for_error(self, exc: Exception) -> str: ...
