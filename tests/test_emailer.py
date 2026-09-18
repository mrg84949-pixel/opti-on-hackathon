from __future__ import annotations

import smtplib

import pytest

import web.emailer as emailer


class _FakeSMTP:
    def __init__(self, host, port, timeout):
        self.host = host
        self.port = port
        self.timeout = timeout
        self.started_tls = False
        self.logged_in = None
        self.sent = None

    def __enter__(self):
        return self

    def __exit__(self, exc_type, exc, tb):
        return False

    def starttls(self):
        self.started_tls = True

    def login(self, username, password):
        self.logged_in = (username, password)

    def send_message(self, message):
        self.sent = message


def test_send_email_returns_false_without_smtp_config(monkeypatch: pytest.MonkeyPatch):
    monkeypatch.delenv("SMTP_HOST", raising=False)
    monkeypatch.delenv("SMTP_FROM_EMAIL", raising=False)
    monkeypatch.delenv("SMTP_USERNAME", raising=False)
    assert emailer.send_email(to_email="user@example.com", subject="Hi", text="Body") is False


def test_send_email_success_with_tls_and_login(monkeypatch: pytest.MonkeyPatch):
    fake = _FakeSMTP("smtp.example.com", 587, 15)
    monkeypatch.setenv("SMTP_HOST", "smtp.example.com")
    monkeypatch.setenv("SMTP_PORT", "587")
    monkeypatch.setenv("SMTP_USERNAME", "user")
    monkeypatch.setenv("SMTP_PASSWORD", "pass")
    monkeypatch.setenv("SMTP_FROM_EMAIL", "noreply@example.com")
    monkeypatch.setenv("SMTP_USE_TLS", "1")
    monkeypatch.setattr(smtplib, "SMTP", lambda host, port, timeout=15: fake)

    ok = emailer.send_email(to_email="client@example.com", subject="Hello", text="Body text")
    assert ok is True
    assert fake.started_tls is True
    assert fake.logged_in == ("user", "pass")
    assert fake.sent["To"] == "client@example.com"
    assert fake.sent["From"] == "noreply@example.com"


def test_send_email_success_without_tls(monkeypatch: pytest.MonkeyPatch):
    fake = _FakeSMTP("smtp.example.com", 25, 15)
    monkeypatch.setenv("SMTP_HOST", "smtp.example.com")
    monkeypatch.setenv("SMTP_PORT", "25")
    monkeypatch.setenv("SMTP_USERNAME", "")
    monkeypatch.setenv("SMTP_PASSWORD", "")
    monkeypatch.setenv("SMTP_FROM_EMAIL", "noreply@example.com")
    monkeypatch.setenv("SMTP_USE_TLS", "0")
    monkeypatch.setattr(smtplib, "SMTP", lambda host, port, timeout=15: fake)

    ok = emailer.send_email(to_email="client@example.com", subject="Hello", text="Body text")
    assert ok is True
    assert fake.started_tls is False
    assert fake.logged_in is None


def test_send_email_handles_exception(monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setenv("SMTP_HOST", "smtp.example.com")
    monkeypatch.setenv("SMTP_FROM_EMAIL", "noreply@example.com")

    class _BoomSMTP:
        def __init__(self, host, port, timeout):
            raise RuntimeError("smtp down")

    monkeypatch.setattr(smtplib, "SMTP", _BoomSMTP)
    ok = emailer.send_email(to_email="client@example.com", subject="Hello", text="Body text")
    assert ok is False
