from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from bot.api import macdent_webhook
from bot.config import settings


class _FakeRequest:
    def __init__(self, payload):
        self._payload = payload

    async def json(self):
        if isinstance(self._payload, Exception):
            raise self._payload
        return self._payload

    async def body(self):
        return b"raw-body"


class _FakeResult:
    def __init__(self, rows: list):
        self._rows = rows

    def scalars(self):
        return self

    def all(self):
        return self._rows


class _FakeSession:
    def __init__(self, rows: list):
        self._rows = rows
        self.committed = False

    async def execute(self, _stmt):
        return _FakeResult(self._rows)

    async def commit(self):
        self.committed = True


class _FakeSessionManager:
    def __init__(self, rows: list):
        self.session = _FakeSession(rows)

    async def __aenter__(self):
        return self.session

    async def __aexit__(self, exc_type, exc, tb):
        return False


def _org(org_id: int = 1):
    return SimpleNamespace(id=org_id, crm_provider="macdent")


@pytest.mark.asyncio
async def test_webhook_ignored_when_secret_not_configured(monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setattr(settings, "macdent_webhook_secret", "")
    result = await macdent_webhook.macdent_webhook("anything", _FakeRequest({"event": "onChange"}))
    assert result == {"status": "ignored"}


@pytest.mark.asyncio
async def test_webhook_ignored_on_secret_mismatch(monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setattr(settings, "macdent_webhook_secret", "correct-secret")
    result = await macdent_webhook.macdent_webhook("wrong-secret", _FakeRequest({"event": "onChange"}))
    assert result == {"status": "ignored"}


@pytest.mark.asyncio
async def test_webhook_ok_and_resolves_single_macdent_org(monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setattr(settings, "macdent_webhook_secret", "correct-secret")
    manager = _FakeSessionManager([_org(7)])
    monkeypatch.setattr(macdent_webhook, "AsyncSessionLocal", lambda: manager)
    captured = {}

    def _fake_info(msg, *args, **kwargs):
        captured["msg"] = msg
        captured["extra"] = kwargs.get("extra")

    monkeypatch.setattr(macdent_webhook.logger, "info", _fake_info)

    result = await macdent_webhook.macdent_webhook(
        "correct-secret", _FakeRequest({"event": "onChange", "id": 123})
    )

    assert result == {"status": "ok"}
    extra_data = captured["extra"]["extra_data"]
    assert extra_data["org_id"] == 7
    assert extra_data["outcome"] is None  # not a zapis/REMOVE event — nothing to act on
    assert sorted(extra_data["top_level_keys"]) == ["event", "id"]
    # Never a full raw dump beyond the bounded preview — patient fields, if
    # present, must not reach an unbounded/untruncated log line.
    assert len(extra_data["preview"]) <= 500
    assert manager.session.committed is False  # no action taken, no commit needed


@pytest.mark.asyncio
async def test_webhook_ok_but_org_none_when_ambiguous(monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setattr(settings, "macdent_webhook_secret", "correct-secret")
    monkeypatch.setattr(macdent_webhook, "AsyncSessionLocal", lambda: _FakeSessionManager([]))

    result = await macdent_webhook.macdent_webhook("correct-secret", _FakeRequest({"event": "onCreate"}))

    assert result == {"status": "ok"}


@pytest.mark.asyncio
async def test_webhook_never_retries_on_undecodable_body(monkeypatch: pytest.MonkeyPatch):
    """Non-JSON body must not crash the handler — MacDent still needs a 200."""
    monkeypatch.setattr(settings, "macdent_webhook_secret", "correct-secret")
    monkeypatch.setattr(macdent_webhook, "AsyncSessionLocal", lambda: _FakeSessionManager([_org(1)]))

    result = await macdent_webhook.macdent_webhook(
        "correct-secret", _FakeRequest(ValueError("not json"))
    )

    assert result == {"status": "ok"}


def _zapis_remove_body(object_id: str = "39558214") -> dict:
    return {
        "eventType": "REMOVE",
        "objectType": "zapis",
        "objectId": object_id,
        "eventData": {"id": int(object_id), "status": 0},
        "createdAt": "2026-08-29 03:15:52",
    }


@pytest.mark.asyncio
async def test_webhook_processes_zapis_remove_event_and_commits(monkeypatch: pytest.MonkeyPatch):
    """Real confirmed event shape (webhook.send_debug, 2026-08-29) — REMOVE on
    a zapis we recognize must cancel the local appointment and commit."""
    monkeypatch.setattr(settings, "macdent_webhook_secret", "correct-secret")
    manager = _FakeSessionManager([_org(7)])
    monkeypatch.setattr(macdent_webhook, "AsyncSessionLocal", lambda: manager)

    fake_appt = SimpleNamespace(id=42)
    get_appt = AsyncMock(return_value=fake_appt)
    apply_cancel = AsyncMock(return_value="synced")
    monkeypatch.setattr(macdent_webhook.crm_appointment_sync_service, "get_appointment_by_crm_id", get_appt)
    monkeypatch.setattr(macdent_webhook.crm_appointment_sync_service, "apply_crm_cancellation", apply_cancel)

    result = await macdent_webhook.macdent_webhook("correct-secret", _FakeRequest(_zapis_remove_body()))

    assert result == {"status": "ok"}
    get_appt.assert_awaited_once_with(manager.session, 7, "39558214")
    apply_cancel.assert_awaited_once_with(manager.session, manager.session._rows[0], 7, fake_appt)
    assert manager.session.committed is True


@pytest.mark.asyncio
async def test_webhook_zapis_remove_unknown_id_does_not_crash(monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setattr(settings, "macdent_webhook_secret", "correct-secret")
    manager = _FakeSessionManager([_org(7)])
    monkeypatch.setattr(macdent_webhook, "AsyncSessionLocal", lambda: manager)

    get_appt = AsyncMock(return_value=None)
    apply_cancel = AsyncMock()
    monkeypatch.setattr(macdent_webhook.crm_appointment_sync_service, "get_appointment_by_crm_id", get_appt)
    monkeypatch.setattr(macdent_webhook.crm_appointment_sync_service, "apply_crm_cancellation", apply_cancel)

    result = await macdent_webhook.macdent_webhook("correct-secret", _FakeRequest(_zapis_remove_body("999999")))

    assert result == {"status": "ok"}
    apply_cancel.assert_not_awaited()


@pytest.mark.asyncio
async def test_webhook_ignores_create_events(monkeypatch: pytest.MonkeyPatch):
    """CREATE is logged only, never acted on — see module docstring."""
    monkeypatch.setattr(settings, "macdent_webhook_secret", "correct-secret")
    manager = _FakeSessionManager([_org(7)])
    monkeypatch.setattr(macdent_webhook, "AsyncSessionLocal", lambda: manager)

    get_appt = AsyncMock()
    monkeypatch.setattr(macdent_webhook.crm_appointment_sync_service, "get_appointment_by_crm_id", get_appt)

    body = {"eventType": "CREATE", "objectType": "zapis", "objectId": "1"}
    result = await macdent_webhook.macdent_webhook("correct-secret", _FakeRequest(body))

    assert result == {"status": "ok"}
    get_appt.assert_not_awaited()
    assert manager.session.committed is False


@pytest.mark.asyncio
async def test_webhook_change_event_without_start_field_is_ignored(monkeypatch: pytest.MonkeyPatch):
    """A CHANGE event we can't read a new start time from must not crash or
    guess — just skip."""
    monkeypatch.setattr(settings, "macdent_webhook_secret", "correct-secret")
    manager = _FakeSessionManager([_org(7)])
    monkeypatch.setattr(macdent_webhook, "AsyncSessionLocal", lambda: manager)

    get_appt = AsyncMock()
    monkeypatch.setattr(macdent_webhook.crm_appointment_sync_service, "get_appointment_by_crm_id", get_appt)

    body = {"eventType": "CHANGE", "objectType": "zapis", "objectId": "1"}
    result = await macdent_webhook.macdent_webhook("correct-secret", _FakeRequest(body))

    assert result == {"status": "ok"}
    get_appt.assert_not_awaited()
    assert manager.session.committed is False


@pytest.mark.asyncio
async def test_webhook_processes_zapis_change_event_as_reschedule(monkeypatch: pytest.MonkeyPatch):
    """Real confirmed CHANGE shape includes eventData.start in the org's
    local clinic time (same convention book_appointment() writes with) —
    must convert to UTC using org.timezone before handing off."""
    monkeypatch.setattr(settings, "macdent_webhook_secret", "correct-secret")
    org = _org(7)
    org.timezone = "Asia/Almaty"
    manager = _FakeSessionManager([org])
    monkeypatch.setattr(macdent_webhook, "AsyncSessionLocal", lambda: manager)

    fake_appt = SimpleNamespace(id=42)
    get_appt = AsyncMock(return_value=fake_appt)
    apply_reschedule = AsyncMock(return_value="synced")
    monkeypatch.setattr(macdent_webhook.crm_appointment_sync_service, "get_appointment_by_crm_id", get_appt)
    monkeypatch.setattr(macdent_webhook.crm_appointment_sync_service, "apply_crm_reschedule", apply_reschedule)

    body = {
        "eventType": "CHANGE",
        "objectType": "zapis",
        "objectId": "39558214",
        "eventData": {"id": 39558214, "start": "15.10.2026 11:00:00", "end": "15.10.2026 11:30:00"},
    }
    result = await macdent_webhook.macdent_webhook("correct-secret", _FakeRequest(body))

    assert result == {"status": "ok"}
    get_appt.assert_awaited_once_with(manager.session, 7, "39558214")
    apply_reschedule.assert_awaited_once()
    call_args = apply_reschedule.call_args.args
    assert call_args[0] is manager.session
    assert call_args[1] is org
    assert call_args[2] == 7
    assert call_args[3] is fake_appt
    new_utc = call_args[4]
    # 11:00 Asia/Almaty (UTC+5) == 06:00 UTC
    assert new_utc.hour == 6
    assert new_utc.tzinfo is not None
    assert manager.session.committed is True


@pytest.mark.asyncio
async def test_webhook_change_event_unknown_zapis_id_no_crash(monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setattr(settings, "macdent_webhook_secret", "correct-secret")
    manager = _FakeSessionManager([_org(7)])
    monkeypatch.setattr(macdent_webhook, "AsyncSessionLocal", lambda: manager)

    get_appt = AsyncMock(return_value=None)
    apply_reschedule = AsyncMock()
    monkeypatch.setattr(macdent_webhook.crm_appointment_sync_service, "get_appointment_by_crm_id", get_appt)
    monkeypatch.setattr(macdent_webhook.crm_appointment_sync_service, "apply_crm_reschedule", apply_reschedule)

    body = {
        "eventType": "CHANGE",
        "objectType": "zapis",
        "objectId": "999999",
        "eventData": {"start": "15.10.2026 11:00:00"},
    }
    result = await macdent_webhook.macdent_webhook("correct-secret", _FakeRequest(body))

    assert result == {"status": "ok"}
    apply_reschedule.assert_not_awaited()


@pytest.mark.asyncio
async def test_webhook_ignores_non_zapis_object_types(monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setattr(settings, "macdent_webhook_secret", "correct-secret")
    manager = _FakeSessionManager([_org(7)])
    monkeypatch.setattr(macdent_webhook, "AsyncSessionLocal", lambda: manager)

    get_appt = AsyncMock()
    monkeypatch.setattr(macdent_webhook.crm_appointment_sync_service, "get_appointment_by_crm_id", get_appt)

    body = {"eventType": "REMOVE", "objectType": "patient", "objectId": "1"}
    result = await macdent_webhook.macdent_webhook("correct-secret", _FakeRequest(body))

    assert result == {"status": "ok"}
    get_appt.assert_not_awaited()


@pytest.mark.asyncio
async def test_webhook_processing_error_does_not_crash_or_lose_200(monkeypatch: pytest.MonkeyPatch):
    """A bug in processing must never turn into a 5xx that makes MacDent retry
    for 2 hours — log and still return ok."""
    monkeypatch.setattr(settings, "macdent_webhook_secret", "correct-secret")
    manager = _FakeSessionManager([_org(7)])
    monkeypatch.setattr(macdent_webhook, "AsyncSessionLocal", lambda: manager)

    get_appt = AsyncMock(side_effect=RuntimeError("db exploded"))
    monkeypatch.setattr(macdent_webhook.crm_appointment_sync_service, "get_appointment_by_crm_id", get_appt)

    result = await macdent_webhook.macdent_webhook("correct-secret", _FakeRequest(_zapis_remove_body()))

    assert result == {"status": "ok"}
    assert manager.session.committed is False
