from __future__ import annotations

from types import SimpleNamespace

from bot.llm.prompts import DEFAULT_DENTISTRY_SYSTEM, PLATFORM_GUARDRAILS
from bot.services.bot_branding import (
    DEFAULT_BOT_DISPLAY_NAME,
    build_org_system_instruction,
    resolve_bot_display_name,
    resolve_bot_welcome_message,
    tone_system_snippet,
)


def test_resolve_bot_display_name_default():
    org = SimpleNamespace(bot_display_name=None)
    assert resolve_bot_display_name(org) == DEFAULT_BOT_DISPLAY_NAME


def test_resolve_bot_display_name_custom():
    org = SimpleNamespace(bot_display_name="  Айша  ")
    assert resolve_bot_display_name(org) == "Айша"


def test_resolve_bot_welcome_message_custom():
    org = SimpleNamespace(bot_display_name="Айша", bot_welcome_message="Привет из клиники!")
    assert resolve_bot_welcome_message(org) == "Привет из клиники!"


def test_resolve_bot_welcome_message_default_uses_name():
    org = SimpleNamespace(bot_display_name="Айша", bot_welcome_message=None)
    msg = resolve_bot_welcome_message(org)
    assert "Айша" in msg


def test_tone_system_snippet_formal():
    assert "официаль" in tone_system_snippet("formal").lower()


def test_build_org_system_instruction_includes_name_and_tone():
    org = SimpleNamespace(
        bot_display_name="Айша",
        bot_tone="friendly",
        system_prompt="Дополнительно: не обещай скидки.",
    )
    text = build_org_system_instruction(
        org,
        base=DEFAULT_DENTISTRY_SYSTEM,
        manage_addon="",
        context_summary="",
    )
    assert text.startswith(PLATFORM_GUARDRAILS.strip()[:40])
    assert "Платформенные правила" in text
    assert "дословно" in text
    assert "transfer_to_human" in text
    assert "не должны противоречить платформенным правилам" in text
    assert "Айша" in text
    assert "дружелюб" in text.lower()
    assert "не обещай скидки" in text
    assert text.index("Платформенные правила") < text.index("не обещай скидки")
