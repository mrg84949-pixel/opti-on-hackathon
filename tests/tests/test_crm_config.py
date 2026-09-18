from __future__ import annotations

from types import SimpleNamespace

import pytest

from bot.crm.amocrm import AmoCRMProvider, DEFAULT_STAFF_PATH
from bot.crm.generic_rest import GenericRestProvider, DEFAULT_STAFF_PATH as GENERIC_STAFF_PATH
from bot.crm.registry import crm_config, merge_crm_config, public_crm_config


def _org(**kwargs):
    base = {"id": 1, "crm_config": None}
    base.update(kwargs)
    return SimpleNamespace(**base)


def test_crm_config_returns_empty_dict_when_null():
    assert crm_config(_org(crm_config=None)) == {}


def test_merge_crm_config_whitelist_paths_only():
    org = _org()
    merge_crm_config(org, {"staff_path": "/custom/staff", "crm_api_token": "secret"})
    assert org.crm_config == {"staff_path": "/custom/staff"}


def test_public_crm_config_exposes_only_path_keys():
    org = _org(crm_config={"staff_path": "/x", "token": "nope"})
    assert public_crm_config(org) == {"staff_path": "/x"}


def test_amocrm_provider_reads_paths_from_config_when_strict(monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setenv("TENANT_CONFIG_STRICT", "1")
    monkeypatch.setenv("AMOCRM_STAFF_PATH", "/env/staff")
    provider = AmoCRMProvider(
        base_url="https://crm.example",
        token="t",
        config={"staff_path": "/cfg/staff"},
    )
    assert provider.staff_path == "/cfg/staff"


def test_amocrm_provider_uses_defaults_when_no_config(monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setenv("TENANT_CONFIG_STRICT", "0")
    monkeypatch.setenv("AMOCRM_STAFF_PATH", "/env/staff")
    provider = AmoCRMProvider(base_url="https://crm.example", token="t", config={})
    assert provider.staff_path == DEFAULT_STAFF_PATH


def test_amocrm_provider_strict_defaults_without_config(monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setenv("TENANT_CONFIG_STRICT", "1")
    provider = AmoCRMProvider(base_url="https://crm.example", token="t", config={})
    assert provider.staff_path == DEFAULT_STAFF_PATH


def test_generic_rest_provider_reads_paths_from_config():
    provider = GenericRestProvider(
        base_url="https://rest.example",
        token="t",
        config={"staff_path": "/cfg/staff"},
    )
    assert provider.staff_path == "/cfg/staff"
    assert provider.staff_path != GENERIC_STAFF_PATH
