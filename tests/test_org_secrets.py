from __future__ import annotations

from types import SimpleNamespace

import pytest
from cryptography.fernet import Fernet

from bot.services.org_secrets import get_org_secret, secret_is_set, set_org_secret
from bot.services.secret_encryption import hash_secret, is_encrypted


@pytest.fixture
def fernet_key(monkeypatch: pytest.MonkeyPatch) -> str:
    key = Fernet.generate_key().decode()
    monkeypatch.setattr("bot.config.settings.tenant_secrets_master_key", key)
    return key


def test_set_and_get_org_secret_encrypts(fernet_key: str):
    org = SimpleNamespace(telegram_bot_token=None, telegram_bot_token_hash=None)
    set_org_secret(org, "telegram_bot_token", "tg-secret")
    assert secret_is_set(org, "telegram_bot_token")
    assert is_encrypted(org.telegram_bot_token)
    assert org.telegram_bot_token_hash == hash_secret("tg-secret")
    assert get_org_secret(org, "telegram_bot_token") == "tg-secret"


def test_clear_org_secret_clears_hash(fernet_key: str):
    org = SimpleNamespace(telegram_bot_token="x", telegram_bot_token_hash="y")
    set_org_secret(org, "telegram_bot_token", None)
    assert org.telegram_bot_token is None
    assert org.telegram_bot_token_hash is None


def test_plaintext_roundtrip_without_master_key(monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setattr("bot.config.settings.tenant_secrets_master_key", "")
    org = SimpleNamespace(whatsapp_api_token=None, telegram_bot_token_hash=None)
    set_org_secret(org, "whatsapp_api_token", "green-token")
    assert org.whatsapp_api_token == "green-token"
    assert get_org_secret(org, "whatsapp_api_token") == "green-token"
