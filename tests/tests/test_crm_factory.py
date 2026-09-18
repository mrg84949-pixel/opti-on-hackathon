from __future__ import annotations

from types import SimpleNamespace

import pytest

import bot.crm.factory as factory


def _org(**kwargs):
    base = {
        "crm_provider": "amocrm",
        "crm_base_url": None,
        "crm_api_token": None,
    }
    base.update(kwargs)
    return SimpleNamespace(**base)


def test_factory_prefers_org_credentials_without_env_fallback(monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setenv("TENANT_CONFIG_STRICT", "0")
    monkeypatch.setenv("CRM_ENV_FALLBACK_ENABLED", "0")
    monkeypatch.setenv("AMOCRM_BASE_URL", "https://env.example.com")
    monkeypatch.setenv("AMOCRM_TOKEN", "env-token")
    provider = factory.get_crm_provider(
        _org(crm_base_url="https://org.example.com", crm_api_token="org-token")
    )
    assert provider.base_url == "https://org.example.com"
    assert provider.token == "org-token"


def test_factory_ignores_env_when_org_missing_and_fallback_disabled(monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setenv("TENANT_CONFIG_STRICT", "0")
    monkeypatch.setenv("CRM_ENV_FALLBACK_ENABLED", "0")
    monkeypatch.setenv("AMOCRM_BASE_URL", "https://env.example.com")
    monkeypatch.setenv("AMOCRM_TOKEN", "env-token")
    provider = factory.get_crm_provider(_org())
    assert provider.base_url == ""
    assert provider.token == ""
    assert provider.demo_mode is True


def test_factory_uses_env_when_fallback_enabled(monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setenv("TENANT_CONFIG_STRICT", "0")
    monkeypatch.setenv("CRM_ENV_FALLBACK_ENABLED", "1")
    monkeypatch.setenv("AMOCRM_BASE_URL", "https://env.example.com")
    monkeypatch.setenv("AMOCRM_TOKEN", "env-token")
    provider = factory.get_crm_provider(_org())
    assert provider.base_url == "https://env.example.com"
    assert provider.token == "env-token"
