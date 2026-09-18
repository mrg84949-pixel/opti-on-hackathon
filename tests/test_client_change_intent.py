from __future__ import annotations

from datetime import datetime, timezone
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from bot.services import client_change_intent as cci


@pytest.mark.parametrize(
    "text,expected",
    [
        ("да", True),
        ("  ОК! ", True),
        ("нет", False),
        ("отменить", False),
        ("может быть", None),
        ("", None),
        ("x" * 80, None),
    ],
)
def test_parse_client_change_intent(text, expected):
    assert cci.parse_client_change_intent(text) is expected


@pytest.mark.asyncio
async def test_try_handle_skips_admin_sandbox():
    assert await cci.try_handle_client_change_reply(
        org_id=1, channel="telegram", user_id="admin-sandbox-1", user_text="да"
    ) is None


@pytest.mark.asyncio
async def test_try_handle_skips_non_yes_no():
    assert await cci.try_handle_client_change_reply(
        org_id=1, channel="telegram", user_id="u1", user_text="привет"
    ) is None


@pytest.mark.asyncio
async def test_try_handle_accept_change(monkeypatch: pytest.MonkeyPatch):
    org = SimpleNamespace(id=1, timezone="Asia/Almaty")
    customer = SimpleNamespace(id=2, phone="tg:9")
    appt = SimpleNamespace(id=7)

    class _Session:
        async def get(self, model, key):
            return org

        async def commit(self):
            return None

    class _SessionManager:
        async def __aenter__(self):
            return _Session()

        async def __aexit__(self, *args):
            return False

    monkeypatch.setattr(cci, "AsyncSessionLocal", lambda: _SessionManager())
    monkeypatch.setattr(
        cci,
        "get_or_create_customer_for_channel",
        AsyncMock(return_value=customer),
    )
    monkeypatch.setattr(
        cci.appointment_service,
        "get_appointment_awaiting_client_change",
        AsyncMock(return_value=(appt, customer)),
    )
    monkeypatch.setattr(
        cci.appointment_service,
        "accept_appointment_change",
        AsyncMock(
            return_value=(
                {"scheduled_at": "2026-07-01T10:00:00+00:00"},
                customer,
            )
        ),
    )
    monkeypatch.setattr(cci.notification_service, "send_customer_message", AsyncMock())
    reply = await cci.try_handle_client_change_reply(
        org_id=1, channel="telegram", user_id="9", user_text="да"
    )
    assert reply is not None
    assert "принято" in reply.lower()


@pytest.mark.asyncio
async def test_try_handle_reject_change(monkeypatch: pytest.MonkeyPatch):
    org = SimpleNamespace(id=1, timezone="UTC")
    customer = SimpleNamespace(id=2, phone="tg:9")
    appt = SimpleNamespace(id=7)

    class _Session:
        async def get(self, model, key):
            return org

        async def commit(self):
            return None

    class _SessionManager:
        async def __aenter__(self):
            return _Session()

        async def __aexit__(self, *args):
            return False

    monkeypatch.setattr(cci, "AsyncSessionLocal", lambda: _SessionManager())
    monkeypatch.setattr(
        cci,
        "get_or_create_customer_for_channel",
        AsyncMock(return_value=customer),
    )
    monkeypatch.setattr(
        cci.appointment_service,
        "get_appointment_awaiting_client_change",
        AsyncMock(return_value=(appt, customer)),
    )
    monkeypatch.setattr(
        cci.appointment_service,
        "reject_appointment_change",
        AsyncMock(
            return_value=(
                {"scheduled_at": "2026-07-01T10:00:00+00:00", "cancel_reason": "client refused"},
                customer,
            )
        ),
    )
    monkeypatch.setattr(cci.notification_service, "send_customer_message", AsyncMock())
    reply = await cci.try_handle_client_change_reply(
        org_id=1, channel="telegram", user_id="9", user_text="нет"
    )
    assert reply is not None
    assert "отменена" in reply.lower()
