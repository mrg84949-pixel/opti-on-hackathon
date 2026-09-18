from __future__ import annotations

from bot.db.models import Organization
from bot.services.secret_encryption import decrypt_secret, encrypt_secret, hash_secret

SECRET_FIELDS: tuple[str, ...] = (
    "whatsapp_api_token",
    "whatsapp_meta_access_token",
    "telegram_bot_token",
    "crm_api_token",
    "crm_user_token",
)


def secret_is_set(org: Organization, field: str) -> bool:
    return bool(str(getattr(org, field, None) or "").strip())


def get_org_secret(org: Organization, field: str) -> str | None:
    raw = getattr(org, field, None)
    if not str(raw or "").strip():
        return None
    value = decrypt_secret(str(raw))
    return value or None


def set_org_secret(org: Organization, field: str, plaintext: str | None) -> None:
    if field not in SECRET_FIELDS:
        raise ValueError(f"Unsupported secret field: {field}")
    cleaned = (plaintext or "").strip() or None
    if cleaned is None:
        setattr(org, field, None)
        if field == "telegram_bot_token":
            org.telegram_bot_token_hash = None
        return
    setattr(org, field, encrypt_secret(cleaned))
    if field == "telegram_bot_token":
        org.telegram_bot_token_hash = hash_secret(cleaned)
