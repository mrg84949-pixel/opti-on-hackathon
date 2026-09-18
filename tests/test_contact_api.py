from __future__ import annotations

import pytest
from fastapi import FastAPI
from httpx import ASGITransport, AsyncClient

import web.contact_api as contact_api


def _test_app() -> FastAPI:
    app = FastAPI()
    app.include_router(contact_api.router, prefix="/api/web")
    return app


@pytest.mark.asyncio
async def test_contact_form_success(monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setenv("CONTACT_FORM_TO_EMAIL", "hello@example.com")

    def _fake_send_email(*, to_email: str, subject: str, text: str) -> bool:
        assert to_email == "hello@example.com"
        assert "Контактная форма: Ivan" in subject
        assert "Телефон: +7 777 111 22 33" in text
        return True

    monkeypatch.setattr(contact_api, "send_email", _fake_send_email)
    app = _test_app()
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        response = await client.post(
            "/api/web/contact",
            json={"name": "Ivan", "phone": "+7 777 111 22 33", "comment": "Нужна консультация"},
        )
    assert response.status_code == 200
    assert response.json() == {"status": "ok"}


@pytest.mark.asyncio
async def test_contact_form_invalid_phone(monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setenv("CONTACT_FORM_TO_EMAIL", "hello@example.com")
    app = _test_app()
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        response = await client.post(
            "/api/web/contact",
            json={"name": "Ivan", "phone": "wa:777@c.us", "comment": "bad format"},
        )
    assert response.status_code == 200
    assert response.json()["detail"] == "invalid_phone"
