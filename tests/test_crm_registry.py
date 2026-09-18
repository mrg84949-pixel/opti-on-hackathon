from __future__ import annotations

from types import SimpleNamespace

import pytest

from bot.crm import registry
from bot.crm.amocrm import AmoCRMProvider
from bot.crm.generic_rest import GenericRestProvider
from bot.crm.macdent import MacDentProvider
from bot.crm.yclients import YClientsProvider


def _org(**kwargs):
    base = {
        "id": 1,
        "crm_provider": "amocrm",
        "crm_base_url": None,
        "crm_api_token": None,
    }
    base.update(kwargs)
    return SimpleNamespace(**base)


def test_resolve_crm_provider_key_normalizes_demo_aliases(monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setattr(registry.settings, "crm_provider", "demo")
    assert registry.resolve_crm_provider_key(_org(crm_provider="none")) == "demo"
    assert registry.resolve_crm_provider_key(_org(crm_provider="demo")) == "demo"
    assert registry.resolve_crm_provider_key(_org(crm_provider=None)) == "demo"


def test_resolve_crm_provider_key_unknown_falls_back_to_demo():
    assert registry.resolve_crm_provider_key(_org(crm_provider="bitrix24")) == "demo"


def test_build_crm_provider_amocrm_uses_org_credentials():
    provider = registry.build_crm_provider(
        _org(crm_provider="amocrm", crm_base_url="https://org.example", crm_api_token="org-token")
    )
    assert isinstance(provider, AmoCRMProvider)
    assert provider.base_url == "https://org.example"
    assert provider.token == "org-token"
    assert provider.demo_mode is False


def test_build_crm_provider_demo_without_credentials():
    provider = registry.build_crm_provider(_org(crm_provider="demo"))
    assert isinstance(provider, AmoCRMProvider)
    assert provider.demo_mode is True


def test_crm_provider_is_demo_mode_for_non_amocrm():
    assert registry.crm_provider_is_demo_mode(_org(crm_provider="demo")) is True


def test_crm_without_external_system():
    assert registry.crm_without_external_system(_org(crm_provider="none")) is True
    assert registry.crm_without_external_system(_org(crm_provider="demo")) is True
    assert registry.crm_without_external_system(_org(crm_provider=None)) is True
    assert registry.crm_without_external_system(_org(crm_provider="")) is True
    assert registry.crm_without_external_system(_org(crm_provider="amocrm")) is False


def test_build_crm_provider_generic_rest_uses_org_credentials():
    provider = registry.build_crm_provider(
        _org(
            crm_provider="generic_rest",
            crm_base_url="https://rest.example",
            crm_api_token="rest-token",
        )
    )
    assert isinstance(provider, GenericRestProvider)
    assert provider.base_url == "https://rest.example"
    assert provider.token == "rest-token"
    assert provider.demo_mode is False


def test_resolve_crm_provider_key_generic_rest():
    assert registry.resolve_crm_provider_key(_org(crm_provider="generic_rest")) == "generic_rest"


def test_crm_provider_is_demo_mode_for_generic_rest_without_credentials():
    assert registry.crm_provider_is_demo_mode(_org(crm_provider="generic_rest")) is True
    assert (
        registry.crm_provider_is_demo_mode(
            _org(crm_provider="generic_rest", crm_base_url="https://x", crm_api_token="tok")
        )
        is False
    )


def test_resolve_crm_provider_key_yclients():
    assert registry.resolve_crm_provider_key(_org(crm_provider="yclients")) == "yclients"


def test_build_crm_provider_yclients_demo_without_company_id():
    provider = registry.build_crm_provider(_org(crm_provider="yclients", crm_api_token="partner"))
    assert isinstance(provider, YClientsProvider)
    assert provider.demo_mode is True


def test_build_crm_provider_yclients_live_with_user_token():
    provider = registry.build_crm_provider(
        _org(
            crm_provider="yclients",
            crm_api_token="partner",
            crm_user_token="user",
            crm_config={"company_id": "4564"},
        )
    )
    assert isinstance(provider, YClientsProvider)
    assert provider.demo_mode is False
    assert provider.user_token == "user"


def test_crm_provider_is_demo_mode_for_yclients_without_company_id():
    assert registry.crm_provider_is_demo_mode(_org(crm_provider="yclients", crm_api_token="tok")) is True
    assert (
        registry.crm_provider_is_demo_mode(
            _org(crm_provider="yclients", crm_api_token="tok", crm_config={"company_id": "1"})
        )
        is False
    )


def test_crm_without_external_system_yclients():
    assert registry.crm_without_external_system(_org(crm_provider="yclients")) is False


def test_build_crm_provider_macdent_uses_org_credentials():
    provider = registry.build_crm_provider(
        _org(
            crm_provider="macdent",
            crm_api_token="macdent-token",
        )
    )
    assert isinstance(provider, MacDentProvider)
    assert provider.base_url == "https://api-developer.macdent.kz"
    assert provider.access_token == "macdent-token"
    assert provider.demo_mode is False


def test_resolve_crm_provider_key_macdent():
    assert registry.resolve_crm_provider_key(_org(crm_provider="macdent")) == "macdent"


def test_crm_provider_is_demo_mode_for_macdent_without_credentials():
    assert registry.crm_provider_is_demo_mode(_org(crm_provider="macdent")) is True
    assert (
        registry.crm_provider_is_demo_mode(
            _org(crm_provider="macdent", crm_base_url="https://x", crm_api_token="tok")
        )
        is False
    )


def test_crm_without_external_system_macdent():
    assert registry.crm_without_external_system(_org(crm_provider="macdent")) is False


def test_list_crm_integration_meta_includes_amocrm_and_demo():
    metas = registry.list_crm_integration_meta()
    codes = {meta.code for meta in metas}
    assert codes == {"amocrm", "demo", "none", "generic_rest", "yclients", "macdent"}
    assert metas[0].code == "none"
    assert [meta.code for meta in metas] == [
        "none",
        "macdent",
        "amocrm",
        "yclients",
        "generic_rest",
        "demo",
    ]


def test_yclients_integration_meta_includes_default_service_id():
    yclients = next(meta for meta in registry.list_crm_integration_meta() if meta.code == "yclients")
    field_names = {field.name for field in yclients.fields}
    assert "default_service_id" in field_names


def test_registry_keys_match_known_providers():
    assert set(registry.CRM_REGISTRY.keys()) == {
        "amocrm",
        "demo",
        "none",
        "generic_rest",
        "yclients",
        "macdent",
    }


def test_factory_non_amocrm_with_env_fallback_uses_env(monkeypatch: pytest.MonkeyPatch):
    import bot.crm.factory as factory

    monkeypatch.setenv("TENANT_CONFIG_STRICT", "0")
    monkeypatch.setenv("CRM_ENV_FALLBACK_ENABLED", "1")
    monkeypatch.setenv("AMOCRM_BASE_URL", "https://env.example.com")
    monkeypatch.setenv("AMOCRM_TOKEN", "env-token")

    provider = factory.get_crm_provider(_org(crm_provider="demo"))
    assert provider.base_url == "https://env.example.com"
    assert provider.token == "env-token"


def test_factory_macdent_with_env_fallback_not_overridden_by_amocrm_env(
    monkeypatch: pytest.MonkeyPatch,
):
    """Regression: env fallback must never swap a configured non-amocrm
    provider (macdent/yclients/generic_rest) for a hardcoded env AmoCRMProvider.
    """
    import bot.crm.factory as factory

    monkeypatch.setenv("TENANT_CONFIG_STRICT", "0")
    monkeypatch.setenv("CRM_ENV_FALLBACK_ENABLED", "1")
    monkeypatch.setenv("AMOCRM_BASE_URL", "https://env.example.com")
    monkeypatch.setenv("AMOCRM_TOKEN", "env-token")

    provider = factory.get_crm_provider(
        _org(crm_provider="macdent", crm_api_token="real-macdent-token")
    )
    assert isinstance(provider, MacDentProvider)
    assert provider.access_token == "real-macdent-token"


def test_factory_yclients_with_env_fallback_not_overridden_by_amocrm_env(
    monkeypatch: pytest.MonkeyPatch,
):
    import bot.crm.factory as factory

    monkeypatch.setenv("TENANT_CONFIG_STRICT", "0")
    monkeypatch.setenv("CRM_ENV_FALLBACK_ENABLED", "1")
    monkeypatch.setenv("AMOCRM_BASE_URL", "https://env.example.com")
    monkeypatch.setenv("AMOCRM_TOKEN", "env-token")

    provider = factory.get_crm_provider(
        _org(
            crm_provider="yclients",
            crm_api_token="partner",
            crm_config={"company_id": "4564"},
        )
    )
    assert isinstance(provider, YClientsProvider)
    assert provider.partner_token == "partner"
