from __future__ import annotations

from datetime import datetime, timezone

from bot.services import notification_service


def test_render_reminder_texts_include_when_and_cta():
    when = datetime(2026, 6, 1, 10, 0, tzinfo=timezone.utc)
    text_2h = notification_service.render_2h_reminder_text(when)
    text_24h = notification_service.render_24h_reminder_text(when)
    assert "2 часа" in text_2h
    assert "24 часа" in text_24h
    assert "01.06.2026 10:00 UTC" in text_2h
    assert "01.06.2026 10:00 UTC" in text_24h
    assert "ответьте в чат" in text_2h.lower()
    assert "ответьте в чат" in text_24h.lower()


def test_render_reminder_texts_include_org_name_when_provided():
    when = datetime(2026, 6, 1, 10, 0, tzinfo=timezone.utc)
    text = notification_service.render_24h_reminder_text(when, org_name="Smoke Clinic B")
    assert "Smoke Clinic B" in text
