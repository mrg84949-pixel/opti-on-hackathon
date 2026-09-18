"""Tests for scripts/onboard_partner_clinic.py (dry-run + idempotent update)."""

from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

import scripts.onboard_partner_clinic as onboard


class _ScalarResult:
    def __init__(self, value):
        self._value = value

    def scalar_one_or_none(self):
        return self._value


class _FakeSession:
    def __init__(self, org=None, admin=None):
        self.org = org
        self.admin = admin
        self.added: list = []
        self.committed = False
        self.flushed = False

    async def execute(self, stmt):
        text = str(stmt).lower()
        if "organizations" in text and self.org is not None:
            return _ScalarResult(self.org)
        if "organizations" in text:
            return _ScalarResult(None)
        if "admins" in text:
            return _ScalarResult(self.admin)
        return _ScalarResult(None)

    def add(self, obj):
        self.added.append(obj)

    async def flush(self):
        self.flushed = True

    async def commit(self):
        self.committed = True

    async def refresh(self, obj):
        return None


def test_load_services_requires_nonempty_array(tmp_path):
    path = tmp_path / "services.json"
    path.write_text("[]", encoding="utf-8")
    with pytest.raises(ValueError, match="non-empty"):
        onboard._load_services(str(path))


def test_normalize_crm_provider_demo_alias():
    assert onboard._normalize_crm_provider("demo") == "none"
    assert onboard._normalize_crm_provider("yclients") == "yclients"
    with pytest.raises(SystemExit):
        onboard._normalize_crm_provider("bitrix")


@pytest.mark.asyncio
async def test_dry_run_does_not_open_db(tmp_path, monkeypatch):
    services = [{"name": "Консультация", "price_label": "5000 ₸"}]
    path = tmp_path / "services.json"
    path.write_text(json.dumps(services), encoding="utf-8")

    def _boom():
        raise AssertionError("AsyncSessionLocal must not be used in dry-run")

    monkeypatch.setattr(onboard, "AsyncSessionLocal", _boom)

    code = await onboard.main(
        [
            "--name",
            "Dry Run Clinic",
            "--admin-login",
            "dryadmin",
            "--password",
            "secret-pass",
            "--services-file",
            str(path),
            "--bot-display-name",
            "Dry",
            "--crm-provider",
            "demo",
            "--dry-run",
        ]
    )
    assert code == 0


@pytest.mark.asyncio
async def test_run_onboard_idempotent_same_org_id(monkeypatch):
    existing = SimpleNamespace(
        id=42,
        name="Клиника Ромашка",
        timezone="UTC",
        billing_paid_until=None,
        bot_display_name=None,
        bot_welcome_message=None,
        bot_tone=None,
        system_prompt=None,
        crm_provider="none",
        crm_config=None,
        crm_api_token=None,
    )
    session = _FakeSession(org=existing, admin=SimpleNamespace(id=1, login="romashka"))

    create_calls: list = []

    async def _create(*_a, **_k):
        create_calls.append(1)
        raise AssertionError("create_organization_core must not run on update")

    replace_calls: list = []

    async def _replace(session, org_id, services):
        replace_calls.append((org_id, list(services)))
        return []

    monkeypatch.setattr(onboard, "create_organization_core", _create)
    monkeypatch.setattr(onboard, "replace_org_services", _replace)
    monkeypatch.setattr(onboard, "telegram_webhook_secret_for_org", lambda oid: f"secret-{oid}-xxxx")
    monkeypatch.setattr(onboard, "hash_password", lambda p: f"hash:{p}")

    args = onboard.build_parser().parse_args(
        [
            "--name",
            "Клиника Ромашка",
            "--admin-login",
            "romashka",
            "--password",
            "x",
            "--services-file",
            "unused.json",
            "--bot-display-name",
            "Ромашка",
            "--bot-tone",
            "friendly",
            "--crm-provider",
            "none",
        ]
    )
    billing = datetime.now(timezone.utc) + timedelta(days=30)
    services = [
        {"name": "Первичная", "price_label": "6000 ₸"},
        {"name": "Повторная", "price_label": "4000 ₸"},
    ]

    first = await onboard.run_onboard(
        session,
        name="Клиника Ромашка",
        timezone_name="Asia/Almaty",
        admin_login="romashka",
        password="x",
        services=services,
        billing_until=billing,
        args=args,
    )
    second = await onboard.run_onboard(
        session,
        name="Клиника Ромашка",
        timezone_name="Asia/Almaty",
        admin_login="romashka",
        password="x",
        services=services[:1],
        billing_until=billing,
        args=args,
    )

    assert first["org_id"] == 42
    assert second["org_id"] == 42
    assert first["created"] is False
    assert second["created"] is False
    assert create_calls == []
    assert replace_calls[0][0] == 42
    assert len(replace_calls[0][1]) == 2
    assert len(replace_calls[1][1]) == 1
    assert existing.bot_display_name == "Ромашка"
    assert existing.crm_provider == "none"
    assert session.committed is True


@pytest.mark.asyncio
async def test_run_onboard_creates_when_missing(monkeypatch):
    session = _FakeSession(org=None)

    async def _create(session, **kwargs):
        org = SimpleNamespace(
            id=7,
            name=kwargs["name"],
            timezone=kwargs["org_timezone"],
            billing_paid_until=kwargs["billing_paid_until"],
            bot_display_name=None,
            bot_welcome_message=None,
            bot_tone=None,
            system_prompt=None,
            crm_provider="none",
            crm_config=None,
            crm_api_token=None,
        )
        session.org = org
        return org, SimpleNamespace(login=kwargs["admin_login"])

    monkeypatch.setattr(onboard, "create_organization_core", _create)
    monkeypatch.setattr(onboard, "replace_org_services", AsyncMock(return_value=[]))
    monkeypatch.setattr(onboard, "telegram_webhook_secret_for_org", lambda oid: "abc123def456")
    monkeypatch.setattr(onboard, "hash_password", lambda p: "h")

    args = onboard.build_parser().parse_args(
        [
            "--name",
            "New Clinic",
            "--admin-login",
            "admin1",
            "--password",
            "p",
            "--services-file",
            "unused.json",
            "--bot-display-name",
            "New",
            "--crm-provider",
            "yclients",
            "--yclients-company-id",
            "999",
        ]
    )
    billing = datetime.now(timezone.utc) + timedelta(days=14)
    result = await onboard.run_onboard(
        session,
        name="New Clinic",
        timezone_name="Asia/Almaty",
        admin_login="admin1",
        password="p",
        services=[{"name": "A"}],
        billing_until=billing,
        args=args,
    )
    assert result["created"] is True
    assert result["org_id"] == 7
    assert result["crm_provider"] == "yclients"
    assert session.org.crm_config == {"company_id": "999"}
