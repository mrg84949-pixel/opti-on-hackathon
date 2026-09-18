from __future__ import annotations

from types import SimpleNamespace

import pytest

import bot.api.whatsapp as whatsapp_api


class _ScalarOneOrNoneResult:
    def __init__(self, value):
        self.value = value

    def scalar_one_or_none(self):
        return self.value


def _org(**overrides):
    base = {
        "id": 1,
        "whatsapp_instance_id": None,
        "whatsapp_meta_phone_number_id": None,
    }
    base.update(overrides)
    return SimpleNamespace(**base)


def _match_org_from_stmt(stmt, orgs: list):
    crit = stmt.whereclause
    if crit is None:
        return None
    field = crit.left.key
    value = crit.right.value
    for org in orgs:
        if getattr(org, field, None) == value:
            return org
    return None


class _ResolveFakeSession:
    def __init__(self, orgs: list):
        self.orgs = orgs
        self.get_calls: list[int] = []

    async def execute(self, stmt):
        return _ScalarOneOrNoneResult(_match_org_from_stmt(stmt, self.orgs))

    async def get(self, _model, org_id: int):
        self.get_calls.append(org_id)
        for org in self.orgs:
            if org.id == org_id:
                return org
        return None


class _ResolveFakeSessionManager:
    def __init__(self, session: _ResolveFakeSession):
        self.session = session

    async def __aenter__(self):
        return self.session

    async def __aexit__(self, exc_type, exc, tb):
        return False


def _patch_resolve_session(monkeypatch: pytest.MonkeyPatch, orgs: list) -> _ResolveFakeSession:
    session = _ResolveFakeSession(orgs)
    monkeypatch.setattr(
        whatsapp_api,
        "AsyncSessionLocal",
        lambda: _ResolveFakeSessionManager(session),
    )
    return session


@pytest.mark.asyncio
async def test_resolve_org_green_matches_instance(monkeypatch: pytest.MonkeyPatch):
    org1 = _org(id=1, whatsapp_instance_id="42")
    org2 = _org(id=2, whatsapp_instance_id="77")
    _patch_resolve_session(monkeypatch, [org1, org2])

    result = await whatsapp_api._resolve_org_green("42")

    assert result is org1


@pytest.mark.asyncio
async def test_resolve_org_green_unknown_instance_returns_none(monkeypatch: pytest.MonkeyPatch):
    org1 = _org(id=1, whatsapp_instance_id="42")
    org2 = _org(id=2, whatsapp_instance_id="77")
    session = _patch_resolve_session(monkeypatch, [org1, org2])

    result = await whatsapp_api._resolve_org_green("99")

    assert result is None
    assert session.get_calls == []


@pytest.mark.asyncio
async def test_resolve_org_green_empty_hint_returns_none(monkeypatch: pytest.MonkeyPatch):
    org1 = _org(id=1, whatsapp_instance_id="42")
    session = _patch_resolve_session(monkeypatch, [org1])

    assert await whatsapp_api._resolve_org_green(None) is None
    assert await whatsapp_api._resolve_org_green("") is None
    assert await whatsapp_api._resolve_org_green("   ") is None
    assert session.get_calls == []


@pytest.mark.asyncio
async def test_resolve_org_meta_matches_phone(monkeypatch: pytest.MonkeyPatch):
    org1 = _org(id=1, whatsapp_meta_phone_number_id="111")
    org2 = _org(id=2, whatsapp_meta_phone_number_id="222")
    _patch_resolve_session(monkeypatch, [org1, org2])

    result = await whatsapp_api._resolve_org_meta("222")

    assert result is org2


@pytest.mark.asyncio
async def test_resolve_org_meta_unknown_phone_returns_none(monkeypatch: pytest.MonkeyPatch):
    org1 = _org(id=1, whatsapp_meta_phone_number_id="111")
    org2 = _org(id=2, whatsapp_meta_phone_number_id="222")
    session = _patch_resolve_session(monkeypatch, [org1, org2])

    result = await whatsapp_api._resolve_org_meta("999")

    assert result is None
    assert session.get_calls == []


@pytest.mark.asyncio
async def test_resolve_org_meta_empty_phone_returns_none(monkeypatch: pytest.MonkeyPatch):
    org1 = _org(id=1, whatsapp_meta_phone_number_id="111")
    session = _patch_resolve_session(monkeypatch, [org1])

    assert await whatsapp_api._resolve_org_meta(None) is None
    assert await whatsapp_api._resolve_org_meta("") is None
    assert await whatsapp_api._resolve_org_meta("   ") is None
    assert session.get_calls == []

