from __future__ import annotations

import importlib.util
import sys
from pathlib import Path
from types import SimpleNamespace

import pytest
from cryptography.fernet import Fernet

from bot.services.secret_encryption import is_encrypted

ROOT = Path(__file__).resolve().parents[1]


def _load_encrypt_script():
    path = ROOT / "scripts" / "encrypt_tenant_secrets_at_rest.py"
    spec = importlib.util.spec_from_file_location("encrypt_tenant_secrets_at_rest", path)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules["encrypt_tenant_secrets_at_rest"] = module
    spec.loader.exec_module(module)
    return module


class _FakeResult:
    def __init__(self, rows):
        self._rows = rows

    def scalars(self):
        return self

    def all(self):
        return self._rows


class _FakeSession:
    def __init__(self, orgs):
        self.orgs = orgs
        self.committed = False

    async def execute(self, _stmt):
        return _FakeResult(self.orgs)

    async def commit(self):
        self.committed = True


class _FakeSessionManager:
    def __init__(self, session):
        self.session = session

    async def __aenter__(self):
        return self.session

    async def __aexit__(self, exc_type, exc, tb):
        return False


@pytest.mark.asyncio
async def test_encrypt_script_dry_run_counts_plaintext(monkeypatch: pytest.MonkeyPatch, capsys):
    encrypt_script = _load_encrypt_script()
    key = Fernet.generate_key().decode()
    monkeypatch.setattr("bot.config.settings.tenant_secrets_master_key", key)
    org = SimpleNamespace(
        id=1,
        whatsapp_api_token="plain-wa",
        whatsapp_meta_access_token=None,
        telegram_bot_token="plain-tg",
        telegram_bot_token_hash=None,
        crm_api_token=None,
    )
    fake_session = _FakeSession([org])
    monkeypatch.setattr(encrypt_script, "AsyncSessionLocal", lambda: _FakeSessionManager(fake_session))

    await encrypt_script.main(dry_run=True)
    out = capsys.readouterr().out
    assert "Fields to encrypt/backfill: 2" in out
    assert not is_encrypted(org.whatsapp_api_token)


@pytest.mark.asyncio
async def test_encrypt_script_apply_encrypts_fields(monkeypatch: pytest.MonkeyPatch, capsys):
    encrypt_script = _load_encrypt_script()
    key = Fernet.generate_key().decode()
    monkeypatch.setattr("bot.config.settings.tenant_secrets_master_key", key)
    org = SimpleNamespace(
        id=1,
        whatsapp_api_token="plain-wa",
        whatsapp_meta_access_token=None,
        telegram_bot_token="plain-tg",
        telegram_bot_token_hash=None,
        crm_api_token=None,
    )
    fake_session = _FakeSession([org])
    monkeypatch.setattr(encrypt_script, "AsyncSessionLocal", lambda: _FakeSessionManager(fake_session))

    await encrypt_script.main(dry_run=False)
    out = capsys.readouterr().out
    assert "Committed encrypted fields: 2" in out
    assert is_encrypted(org.whatsapp_api_token)
    assert is_encrypted(org.telegram_bot_token)
    assert fake_session.committed is True
