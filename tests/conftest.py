"""Shared test fixtures: keep bot.config.settings in sync with env var patches."""
from __future__ import annotations

import os

import pytest

from bot.config import Settings, settings

_SETTINGS_FIELDS = Settings.model_fields


def _sync_field_from_env(key: str, value: str | None) -> None:
    """Update settings singleton when an env var known to settings changes."""
    field_name = key.lower()
    if field_name not in _SETTINGS_FIELDS:
        return
    field_info = _SETTINGS_FIELDS[field_name]
    field_type = field_info.annotation
    if value is None:
        default = field_info.default if field_info.default is not None else ""
        object.__setattr__(settings, field_name, default)
        return
    try:
        if field_type is bool:
            parsed = value.strip().lower() in ("1", "true", "yes", "on")
            object.__setattr__(settings, field_name, parsed)
        elif field_type is int:
            object.__setattr__(settings, field_name, int(value))
        else:
            object.__setattr__(settings, field_name, value)
    except (ValueError, TypeError):
        pass


@pytest.fixture(autouse=True)
def _sync_settings(monkeypatch: pytest.MonkeyPatch):
    """Intercept monkeypatch.setenv/delenv to keep settings in sync."""
    _orig_setenv = monkeypatch.setenv

    def _setenv(name: str, value: str, prepend: str | None = None):
        _orig_setenv(name, value, prepend=prepend)
        _sync_field_from_env(name, value)

    monkeypatch.setenv = _setenv  # type: ignore[assignment]

    _orig_delenv = monkeypatch.delenv

    def _delenv(name: str, raising: bool = True):
        _orig_delenv(name, raising=raising)
        _sync_field_from_env(name, None)

    monkeypatch.delenv = _delenv  # type: ignore[assignment]

    monkeypatch.setattr(settings, "rate_limit_enabled", False)
    monkeypatch.delenv("CRM_STAFF_JSON", raising=False)
    monkeypatch.setattr(settings, "telegram_webhook_secret", "")
    monkeypatch.setattr(settings, "telegram_token", "")
    monkeypatch.setattr(settings, "telegram_webhook_url", "")
    monkeypatch.setattr(settings, "whatsapp_verify_token", "")
    monkeypatch.setattr(settings, "whatsapp_app_secret", "")
    monkeypatch.setattr(settings, "tenant_secrets_master_key", "")

    yield
