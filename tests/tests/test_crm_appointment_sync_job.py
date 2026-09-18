from __future__ import annotations

from datetime import datetime, timedelta, timezone
from unittest.mock import AsyncMock

import pytest

from bot.automation import crm_appointment_sync as sync_job
from bot.db.models import Organization


class _ScalarsAll:
    def __init__(self, rows):
        self._rows = rows

    def all(self):
        return self._rows


class _ExecuteResult:
    def __init__(self, rows):
        self._rows = rows

    def scalars(self):
        return _ScalarsAll(self._rows)


class _FakeSession:
    def __init__(self, orgs: list[Organization]):
        self.orgs = orgs
        self.committed = False

    async def execute(self, _stmt):
        return _ExecuteResult(self.orgs)

    async def commit(self):
        self.committed = True


class _FakeSessionManager:
    def __init__(self, session: _FakeSession):
        self._session = session

    async def __aenter__(self):
        return self._session

    async def __aexit__(self, exc_type, exc, tb):
        return False


def _org(**kwargs) -> Organization:
    org = Organization(
        id=1,
        name="Clinic",
        crm_provider="amocrm",
        crm_base_url="https://crm.example",
        crm_api_token="tok",
        timezone="UTC",
        billing_paid_until=datetime.now(timezone.utc) + timedelta(days=30),
    )
    for key, value in kwargs.items():
        setattr(org, key, value)
    return org


@pytest.mark.asyncio
async def test_process_crm_appointment_sync_runs_for_eligible_orgs(monkeypatch: pytest.MonkeyPatch):
    org = _org()
    session = _FakeSession([org])
    sync_mock = AsyncMock(return_value={"synced": 2})
    monkeypatch.setattr(sync_job, "AsyncSessionLocal", lambda: _FakeSessionManager(session))
    monkeypatch.setattr(sync_job, "_crm_sync_eligible", lambda _org: True)
    monkeypatch.setattr(sync_job, "sync_org_crm_cancellations", sync_mock)

    await sync_job.process_crm_appointment_sync()

    sync_mock.assert_awaited_once_with(session, org.id)
    assert session.committed is True


@pytest.mark.asyncio
async def test_process_crm_appointment_sync_skips_ineligible(monkeypatch: pytest.MonkeyPatch):
    org = _org(crm_provider="demo")
    session = _FakeSession([org])
    sync_mock = AsyncMock()
    monkeypatch.setattr(sync_job, "AsyncSessionLocal", lambda: _FakeSessionManager(session))
    monkeypatch.setattr(sync_job, "_crm_sync_eligible", lambda _org: False)
    monkeypatch.setattr(sync_job, "sync_org_crm_cancellations", sync_mock)

    await sync_job.process_crm_appointment_sync()

    sync_mock.assert_not_awaited()
    assert session.committed is True


@pytest.mark.asyncio
async def test_process_crm_appointment_sync_continues_on_org_error(monkeypatch: pytest.MonkeyPatch):
    org1 = _org(id=1)
    org2 = _org(id=2)
    session = _FakeSession([org1, org2])

    async def sync_side_effect(sess, org_id):
        if org_id == 1:
            raise RuntimeError("boom")
        return {"synced": 1}

    monkeypatch.setattr(sync_job, "AsyncSessionLocal", lambda: _FakeSessionManager(session))
    monkeypatch.setattr(sync_job, "_crm_sync_eligible", lambda _org: True)
    monkeypatch.setattr(sync_job, "sync_org_crm_cancellations", AsyncMock(side_effect=sync_side_effect))

    await sync_job.process_crm_appointment_sync()
    assert session.committed is True
