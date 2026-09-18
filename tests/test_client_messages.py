from __future__ import annotations

from bot.llm.client_messages import (
    AI_RATE_LIMIT,
    AI_UNAVAILABLE,
    render_booking_created_client_text,
    render_handoff_client_text,
    sanitize_client_reply,
)


def test_sanitize_client_reply_passes_normal_text():
    text = "Проверьте запись: Консультация, 1 июня в 10:00."
    assert sanitize_client_reply(text) == text


def test_sanitize_client_reply_blocks_groq_leak():
    leaked = "Переключите AI_PROVIDER=stub и задайте GROQ_API_KEY в .env"
    assert sanitize_client_reply(leaked) == AI_UNAVAILABLE


def test_sanitize_client_reply_blocks_gemini_leak():
    assert sanitize_client_reply("Некорректный GEMINI_API_KEY") == AI_UNAVAILABLE


def test_default_stub_message_passes_sanitizer():
    msg = "Тестовый режим: ответ без вызова ИИ."
    assert sanitize_client_reply(msg) == msg


def test_rate_limit_message_has_no_leak_markers():
    lowered = AI_RATE_LIMIT.lower()
    assert "groq" not in lowered
    assert "gemini" not in lowered
    assert "api_key" not in lowered
    assert "ai_provider" not in lowered


def test_render_booking_created_manual_has_no_tool_leaks():
    text = render_booking_created_client_text(
        when_label="27.06.2026 10:00",
        timezone_name="Asia/Almaty",
        service_name="Консультация",
        auto_confirmed=False,
    )
    lowered = text.lower()
    assert "сообщите клиенту" not in lowered
    assert "cancel_appointment" not in lowered
    assert "контекст сжат" not in lowered
    assert "статус «новая»" not in lowered
    assert "27.06.2026" in text


def test_render_booking_created_auto_confirmed():
    text = render_booking_created_client_text(
        when_label="27.06.2026 10:00",
        timezone_name="Asia/Almaty",
        service_name="Консультация",
        auto_confirmed=True,
    )
    assert "подтверждена" in text.lower()
    assert "сообщите клиенту" not in text.lower()


def test_render_handoff_client_text_has_no_leak_markers():
    text = render_handoff_client_text()
    lowered = text.lower()
    assert "сообщите клиенту" not in lowered
    assert "администратору" in lowered


def test_sanitize_client_reply_blocks_assistant_tool_leak():
    leaked = (
        "Запрос передан администратору. Сообщите клиенту, что с ним свяжется "
        "сотрудник компании в ближайшее время."
    )
    sanitized = sanitize_client_reply(leaked)
    assert "сообщите клиенту" not in sanitized.lower()
    assert sanitized == render_handoff_client_text()


def test_sanitize_client_reply_blocks_cancel_tool_leak():
    leaked = "Запись отменена. Сообщите клиенту, что запись отменена. cancel_appointment"
    assert sanitize_client_reply(leaked) == AI_UNAVAILABLE


def test_sanitize_client_reply_blocks_function_markup_no_gt():
    leaked = '<function=set_customer_name{"name": "Клиент"}</function>'
    assert sanitize_client_reply(leaked) == AI_UNAVAILABLE


def test_sanitize_client_reply_strips_function_keeps_human_text():
    mixed = 'Здравствуйте! <function=set_customer_name{"name": "Клиент"}</function>'
    assert sanitize_client_reply(mixed) == "Здравствуйте!"


def test_sanitize_client_reply_blocks_set_customer_name_marker():
    assert sanitize_client_reply("Вызовите set_customer_name") == AI_UNAVAILABLE


def test_sanitize_client_reply_passes_past_booking_message():
    from bot.llm.booking_draft import PAST_BOOKING_MESSAGE

    assert sanitize_client_reply(PAST_BOOKING_MESSAGE) == PAST_BOOKING_MESSAGE
    assert "get_available_slots" not in PAST_BOOKING_MESSAGE
