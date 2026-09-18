"""Wave 3: P1 regression audit gate — KI-01/KI-02."""
from __future__ import annotations

from bot.llm.client_messages import render_handoff_client_text, sanitize_client_reply


def test_regression_ki01_confirm_leak_rewritten():
    """KI-01: raw confirm tool text must not reach the client."""
    leaked = (
        "Запись создана (номер 3): 27.06.2026 10:00 (Asia/Almaty), статус «новая». "
        "Сообщите клиенту, что администратор подтвердит время. "
        "Контекст сжат в БД. Для отмены или переноса — cancel_appointment / edit_appointment."
    )
    sanitized = sanitize_client_reply(leaked)
    assert "сообщите клиенту" not in sanitized.lower()
    assert "cancel_appointment" not in sanitized.lower()
    assert "оформлена" in sanitized.lower()


def test_regression_ki02_assistant_markers_blocked():
    """KI-02: assistant instructions and handoff copy stay client-safe."""
    leaked = (
        "Запрос передан администратору. Сообщите клиенту, что с ним свяжется "
        "сотрудник компании в ближайшее время."
    )
    sanitized = sanitize_client_reply(leaked)
    assert "сообщите клиенту" not in sanitized.lower()
    assert sanitized == render_handoff_client_text()

    handoff = render_handoff_client_text()
    assert "сообщите клиенту" not in handoff.lower()
    assert "администратору" in handoff.lower()
