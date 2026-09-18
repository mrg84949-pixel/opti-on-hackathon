from __future__ import annotations

from web.user_auth import hash_password, normalize_email, validate_email, verify_password


def test_normalize_email():
    assert normalize_email("  USER@Example.COM ") == "user@example.com"


def test_validate_email():
    assert validate_email("user@example.com") is True
    assert validate_email("broken-email") is False


def test_password_hash_roundtrip():
    stored = hash_password("strong-password")
    assert verify_password("strong-password", stored) is True
    assert verify_password("wrong-password", stored) is False
