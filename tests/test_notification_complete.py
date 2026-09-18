from __future__ import annotations

from types import SimpleNamespace

from bot.services.notification_service import render_complete_text


def test_render_complete_text_with_2gis_url():
    org = SimpleNamespace(review_2gis_url="https://2gis.ru/firm/123")
    text = render_complete_text(org)
    assert "2ГИС" in text
    assert "https://2gis.ru/firm/123" in text


def test_render_complete_text_without_url():
    org = SimpleNamespace(review_2gis_url=None)
    text = render_complete_text(org)
    assert "Спасибо" in text
    assert "оставьте отзыв" in text.lower()
    assert "https://" not in text


def test_render_complete_text_with_care_message():
    org = SimpleNamespace(review_2gis_url=None, post_service_upsell_message=None)
    text = render_complete_text(org, care_message="Не есть 2 часа после процедуры.")
    assert "Совет по уходу:" in text
    assert "Не есть 2 часа после процедуры." in text


def test_render_complete_text_with_upsell_message():
    org = SimpleNamespace(
        name="Demo Clinic",
        review_2gis_url=None,
        post_service_upsell_message="Запишитесь на чистку в {org_name}.",
    )
    text = render_complete_text(org)
    assert "Запишитесь на чистку в Demo Clinic." in text


def test_render_complete_text_with_care_and_upsell():
    org = SimpleNamespace(
        name="Clinic",
        review_2gis_url="https://2gis.ru/firm/1",
        post_service_upsell_message="Ждём вас в {org_name}!",
    )
    text = render_complete_text(org, care_message="Пейте воду.")
    assert "2ГИС" in text
    assert "Совет по уходу:" in text
    assert "Пейте воду." in text
    assert "Ждём вас в Clinic!" in text
