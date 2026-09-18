from __future__ import annotations

from types import SimpleNamespace

import pytest

from bot.services import telegram_org_service as tg_org
from bot.services.secret_encryption import hash_secret


def _org(org_id: int = 1, token: str = "bot-token"):
    return SimpleNamespace(
        id=org_id,
        telegram_bot_token=token,
        telegram_bot_token_hash=hash_secret(token) if token else None,
    )


class _FakeSession:
    def __init__(self, rows: list):
        self._rows = rows

    async def execute(self, _stmt):
        return _FakeResult(self._rows)


class _FakeResult:
    def __init__(self, rows: list):
        self._rows = rows

    def scalars(self):
        return self

    def all(self):
        return self._rows


class _FakeSessionManager:
    def __init__(self, rows: list):
        self._rows = rows

    async def __aenter__(self):
        return _FakeSession(self._rows)

    async def __aexit__(self, exc_type, exc, tb):
        return False


@pytest.mark.asyncio
async def test_resolve_returns_none_for_empty_token():
    assert await tg_org.resolve_org_by_telegram_bot_token("") is None
    assert await tg_org.resolve_org_by_telegram_bot_token("   ") is None


@pytest.mark.asyncio
async def test_resolve_returns_org_for_unique_token(monkeypatch: pytest.MonkeyPatch):
    org = _org(org_id=42, token="unique-token")
    monkeypatch.setattr(
        tg_org,
        "AsyncSessionLocal",
        lambda: _FakeSessionManager([org]),
    )
    resolved = await tg_org.resolve_org_by_telegram_bot_token("unique-token")
    assert resolved is org
    assert resolved.id == 42


@pytest.mark.asyncio
async def test_resolve_returns_none_when_unknown(monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setattr(
        tg_org,
        "AsyncSessionLocal",
        lambda: _FakeSessionManager([]),
    )
    assert await tg_org.resolve_org_by_telegram_bot_token("missing") is None


@pytest.mark.asyncio
async def test_resolve_returns_none_when_ambiguous(monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setattr(
        tg_org,
        "AsyncSessionLocal",
        lambda: _FakeSessionManager([_org(1), _org(2)]),
    )
    assert await tg_org.resolve_org_by_telegram_bot_token("dup-token") is None


def test_ingress_telegram_bot_token(monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setattr("bot.services.telegram_org_service.settings.telegram_token", "  tok123  ")
    assert tg_org.ingress_telegram_bot_token() == "tok123"


def test_telegram_bot_token_for_send_uses_org_token():
    org = _org(token="org-secret")
    assert tg_org.telegram_bot_token_for_send(org) == "org-secret"
    assert tg_org.telegram_send_url(org) == "https://api.telegram.org/botorg-secret/sendMessage"


def test_telegram_bot_token_for_send_strict_skips_env(monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setattr("bot.config.settings.tenant_config_strict", True)
    monkeypatch.setattr("bot.config.settings.telegram_token", "env-token")
    org = _org(token="")
    assert tg_org.telegram_bot_token_for_send(org) == ""
    assert tg_org.telegram_send_url(org) is None


def test_telegram_bot_token_for_send_dev_env_fallback(monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setattr("bot.config.settings.tenant_config_strict", False)
    monkeypatch.setattr("bot.config.settings.telegram_token", "env-token")
    org = _org(token="")
    assert tg_org.telegram_bot_token_for_send(org) == "env-token"


class _FakeOutboundClient:
    def __init__(self):
        self.calls: list[dict] = []

    async def __aenter__(self):
        return self

    async def __aexit__(self, exc_type, exc, tb):
        return False

    async def post(self, url, json=None):
        self.calls.append({"url": url, "json": json})
        return SimpleNamespace(status_code=200, is_success=True)


@pytest.mark.asyncio
async def test_send_telegram_for_org_uses_org_token(monkeypatch: pytest.MonkeyPatch):
    client = _FakeOutboundClient()
    monkeypatch.setattr("bot.services.telegram_org_service.httpx.AsyncClient", lambda timeout=20: client)
    org = _org(token="send-token")
    ok = await tg_org.send_telegram_for_org(org, 777, "hello")
    assert ok.ok is True
    assert "botsend-token/sendMessage" in client.calls[0]["url"]


@pytest.fixture
def fernet_key(monkeypatch: pytest.MonkeyPatch) -> str:
    from cryptography.fernet import Fernet

    key = Fernet.generate_key().decode()
    monkeypatch.setattr("bot.config.settings.tenant_secrets_master_key", key)
    return key


@pytest.mark.asyncio
async def test_resolve_finds_org_by_hash_when_token_encrypted(monkeypatch: pytest.MonkeyPatch, fernet_key: str):
    from bot.services.org_secrets import set_org_secret

    org = SimpleNamespace(id=9, telegram_bot_token=None, telegram_bot_token_hash=None)
    set_org_secret(org, "telegram_bot_token", "hash-only-token")
    monkeypatch.setattr(
        tg_org,
        "AsyncSessionLocal",
        lambda: _FakeSessionManager([org]),
    )
    resolved = await tg_org.resolve_org_by_telegram_bot_token("hash-only-token")
    assert resolved is org


@pytest.mark.asyncio
async def test_send_telegram_for_org_skips_when_no_token(monkeypatch: pytest.MonkeyPatch):
    called = False

    class _ShouldNotRun:
        async def __aenter__(self):
            nonlocal called
            called = True
            return self

        async def __aexit__(self, *args):
            return False

    monkeypatch.setattr("bot.config.settings.tenant_config_strict", True)
    monkeypatch.setattr("bot.services.telegram_org_service.httpx.AsyncClient", lambda timeout=20: _ShouldNotRun())
    org = _org(token="")
    assert (await tg_org.send_telegram_for_org(org, 777, "hello")).ok is False
    assert called is False


@pytest.mark.asyncio
async def test_resolve_org_by_per_org_webhook_secret(monkeypatch: pytest.MonkeyPatch):
    org_a = SimpleNamespace(id=1, telegram_bot_token="a", telegram_bot_token_hash=None)
    org_b = SimpleNamespace(id=2, telegram_bot_token="b", telegram_bot_token_hash=None)
    monkeypatch.setattr(
        tg_org,
        "AsyncSessionLocal",
        lambda: _FakeSessionManager([org_a, org_b]),
    )
    secret_b = tg_org.telegram_webhook_secret_for_org(2)
    resolved = await tg_org.resolve_org_by_telegram_webhook_secret(secret_b)
    assert resolved is org_b
    assert await tg_org.resolve_org_by_telegram_webhook_secret("bad-secret") is None
