from __future__ import annotations

import hashlib

from bot.config import settings

ENC_PREFIX = "enc:v1:"


def encryption_enabled() -> bool:
    return bool((settings.tenant_secrets_master_key or "").strip())


def _fernet():
    from cryptography.fernet import Fernet

    key = (settings.tenant_secrets_master_key or "").strip().encode()
    return Fernet(key)


def is_encrypted(stored: str | None) -> bool:
    return bool(stored) and str(stored).startswith(ENC_PREFIX)


def encrypt_secret(plaintext: str) -> str:
    value = (plaintext or "").strip()
    if not value:
        return ""
    if not encryption_enabled():
        return value
    token = _fernet().encrypt(value.encode()).decode()
    return f"{ENC_PREFIX}{token}"


def decrypt_secret(stored: str | None) -> str:
    raw = (stored or "").strip()
    if not raw:
        return ""
    if not is_encrypted(raw):
        return raw
    ciphertext = raw.removeprefix(ENC_PREFIX)
    return _fernet().decrypt(ciphertext.encode()).decode()


def hash_secret(plaintext: str) -> str:
    value = (plaintext or "").strip()
    if not value:
        return ""
    return hashlib.sha256(value.encode()).hexdigest()


def is_valid_fernet_key(key: str | None) -> bool:
    raw = (key or "").strip()
    if not raw:
        return False
    try:
        from cryptography.fernet import Fernet

        Fernet(raw.encode())
        return True
    except Exception:
        return False


def fernet_key_format_errors(key: str | None) -> list[str]:
    raw = (key or "").strip()
    if not raw:
        return []
    if is_valid_fernet_key(raw):
        return []
    return ["TENANT_SECRETS_MASTER_KEY is not a valid Fernet key"]


def tenant_secrets_master_key_errors() -> list[str]:
    if not settings.tenant_config_strict or not settings.strict_startup_validation:
        return []
    key = (settings.tenant_secrets_master_key or "").strip()
    if not key:
        return [
            "TENANT_SECRETS_MASTER_KEY must be set when TENANT_CONFIG_STRICT "
            "and STRICT_STARTUP_VALIDATION are enabled"
        ]
    return fernet_key_format_errors(key)
