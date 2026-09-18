from __future__ import annotations

from typing import Any

from bot.config import settings
from bot.llm.client_messages import AI_UNAVAILABLE
from bot.llm.providers.base import SessionSetup


class StubProvider:
    provider_name = "stub"

    def credentials_configured(self) -> bool:
        return True

    def missing_credentials_message(self) -> str:
        return ""

    async def create_session(self, setup: SessionSetup) -> Any:
        return object()

    async def send_turn(self, session: Any, user_text: str) -> str:
        _ = session, user_text
        return settings.ai_stub_message or "Тестовый режим: ответ без вызова ИИ."

    def user_message_for_error(self, exc: Exception) -> str:
        _ = exc
        return AI_UNAVAILABLE
