from __future__ import annotations

import pytest
from cryptography.fernet import Fernet

from bot.services import secret_encryption as enc


@pytest.fixture
def fernet_key(monkeypatch: pytest.MonkeyPatch) -> str:
    key = Fernet.generate_key().decode()
    monkeypatch.setattr("bot.config.settings.tenant_secrets_master_key", key)
    return key


def test_encrypt_decrypt_roundtrip(fernet_key: str):
    ciphertext = enc.encrypt_secret("super-secret-token")
    assert enc.is_encrypted(ciphertext)
    assert enc.decrypt_secret(ciphertext) == "super-secret-token"


def test_decrypt_plaintext_passthrough():
    assert enc.decrypt_secret("plain-token") == "plain-token"
    assert enc.is_encrypted("plain-token") is False


def test_hash_secret_stable():
    assert enc.hash_secret("abc") == enc.hash_secret("abc")
    assert enc.hash_secret("abc") != enc.hash_secret("xyz")


def test_encrypt_noop_without_master_key(monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setattr("bot.config.settings.tenant_secrets_master_key", "")
    assert enc.encrypt_secret("plain") == "plain"


def test_tenant_secrets_master_key_errors_strict(monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setattr("bot.config.settings.tenant_config_strict", True)
    monkeypatch.setattr("bot.config.settings.strict_startup_validation", True)
    monkeypatch.setattr("bot.config.settings.tenant_secrets_master_key", "")
    assert enc.tenant_secrets_master_key_errors()


def test_is_valid_fernet_key_valid():
    key = Fernet.generate_key().decode()
    assert enc.is_valid_fernet_key(key) is True


def test_is_valid_fernet_key_invalid():
    assert enc.is_valid_fernet_key("not-a-fernet-key") is False
    assert enc.is_valid_fernet_key("") is False
    assert enc.is_valid_fernet_key(None) is False


def test_tenant_secrets_master_key_errors_invalid_key(monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setattr("bot.config.settings.tenant_config_strict", True)
    monkeypatch.setattr("bot.config.settings.strict_startup_validation", True)
    monkeypatch.setattr("bot.config.settings.tenant_secrets_master_key", "not-a-fernet-key")
    errors = enc.tenant_secrets_master_key_errors()
    assert any("valid Fernet" in e for e in errors)
