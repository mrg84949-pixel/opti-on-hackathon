from __future__ import annotations

from types import SimpleNamespace

import pytest

from bot.channels.whatsapp import outbound
from bot.services import stats_service
from bot.services.tenant_env_migration import (
    apply_tenant_env_migration,
    mask_secret,
    plan_tenant_env_migration,
    tenant_strict_env_org_mismatches,
)


def _org(**overrides):
    base = {
        "id": 1,
        "whatsapp_provider": None,
        "whatsapp_instance_id": None,
        "whatsapp_api_token": None,
        "whatsapp_meta_phone_number_id": None,
        "whatsapp_meta_access_token": None,
        "telegram_bot_token": None,
        "crm_base_url": None,
        "crm_api_token": None,
        "crm_provider": None,
    }
    base.update(overrides)
    return SimpleNamespace(**base)


def _cfg(**overrides):
    base = {
        "telegram_token": "",
        "green_api_instance_id": "",
        "green_api_token": "",
        "whatsapp_phone_number_id": "",
        "whatsapp_graph_access_token": "",
        "whatsapp_provider": "green",
        "amocrm_base_url": "",
        "amocrm_token": "",
        "crm_provider": "amocrm",
    }
    base.update(overrides)
    return SimpleNamespace(**base)


def test_mask_secret():
    assert mask_secret("") == "(empty)"
    assert mask_secret("ab") == "****"
    assert mask_secret("abcdefgh") == "ab...gh"


def test_outbound_ignores_env_when_strict(monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setattr("bot.config.settings.tenant_config_strict", True)
    monkeypatch.setenv("WHATSAPP_GRAPH_ACCESS_TOKEN", "env-meta")
    monkeypatch.setenv("WHATSAPP_PHONE_NUMBER_ID", "env-pnid")
    monkeypatch.setenv("GREEN_API_INSTANCE_ID", "env-inst")
    monkeypatch.setenv("GREEN_API_TOKEN", "env-green")
    monkeypatch.setenv("WHATSAPP_PROVIDER", "meta")

    empty_org = _org()
    assert outbound.meta_access_token(empty_org) == ""
    assert outbound.meta_phone_number_id(empty_org) == ""
    assert outbound.green_instance_id(empty_org) == ""
    assert outbound.green_api_token(empty_org) == ""
    assert outbound.resolve_whatsapp_provider(empty_org) == "green"


def test_outbound_env_fallback_when_not_strict(monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setattr("bot.config.settings.tenant_config_strict", False)
    monkeypatch.setenv("WHATSAPP_GRAPH_ACCESS_TOKEN", "env-meta")
    monkeypatch.setenv("WHATSAPP_PHONE_NUMBER_ID", "env-pnid")

    empty_org = _org()
    assert outbound.meta_access_token(empty_org) == "env-meta"
    assert outbound.meta_phone_number_id(empty_org) == "env-pnid"


@pytest.mark.asyncio
async def test_outbound_send_skips_env_when_strict(monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setattr("bot.config.settings.tenant_config_strict", True)
    monkeypatch.setenv("GREEN_API_INSTANCE_ID", "env-inst")
    monkeypatch.setenv("GREEN_API_TOKEN", "env-green")

    called = False

    async def fake_green_send(**_kwargs):
        nonlocal called
        called = True
        return True, None, False

    monkeypatch.setattr(outbound.green_api, "send_message", fake_green_send)
    ok = await outbound.send_whatsapp_text(_org(whatsapp_provider="green"), "777@c.us", "hello")
    assert ok.ok is False
    assert called is False


def test_telegram_connected_strict_ignores_env(monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setattr("bot.config.settings.tenant_config_strict", True)
    monkeypatch.setattr("bot.config.settings.telegram_token", "global-tg-token")
    assert stats_service._telegram_connected(_org()) is False
    assert stats_service._telegram_connected(_org(telegram_bot_token="org-token")) is True


def test_telegram_connected_non_strict_uses_env(monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setattr("bot.config.settings.tenant_config_strict", False)
    monkeypatch.setattr("bot.config.settings.telegram_token", "global-tg-token")
    assert stats_service._telegram_connected(_org()) is True


def test_migration_plan_dry_run():
    org = _org(whatsapp_instance_id="already-set")
    cfg = _cfg(
        telegram_token="my-telegram-secret",
        green_api_instance_id="new-inst",
        green_api_token="green-secret",
        whatsapp_phone_number_id="pnid-1",
    )
    plan = plan_tenant_env_migration(org, cfg)
    by_field = {item.org_field: item for item in plan}

    assert by_field["telegram_bot_token"].action == "copy"
    assert "my" in by_field["telegram_bot_token"].display_value
    assert by_field["whatsapp_instance_id"].action == "skip_db_set"
    assert by_field["whatsapp_api_token"].action == "copy"
    assert by_field["whatsapp_meta_phone_number_id"].action == "copy"
    assert by_field["crm_base_url"].action == "skip_no_env"


def test_migration_apply_only_empty_fields():
    org = _org(crm_api_token="keep-me")
    cfg = _cfg(telegram_token="tg-copy", amocrm_token="env-crm-should-not-overwrite")
    plan = apply_tenant_env_migration(org, cfg)

    assert org.telegram_bot_token == "tg-copy"
    assert org.crm_api_token == "keep-me"
    assert any(item.action == "copy" for item in plan)
    assert any(item.action == "skip_db_set" and item.org_field == "crm_api_token" for item in plan)


def test_tenant_strict_env_org_mismatches():
    org = _org()
    cfg = _cfg(telegram_token="tg-only-env", green_api_token="green-only")
    errors = tenant_strict_env_org_mismatches(org, cfg)
    assert any("TELEGRAM_TOKEN" in e for e in errors)
    assert any("GREEN_API_TOKEN" in e for e in errors)
    assert not tenant_strict_env_org_mismatches(
        _org(telegram_bot_token="x"),
        _cfg(telegram_token="tg", whatsapp_provider="green", crm_provider="amocrm"),
    )
