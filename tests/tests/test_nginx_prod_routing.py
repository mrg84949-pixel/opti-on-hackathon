"""Static gate: prod nginx configs route only webhook paths to backend."""

from __future__ import annotations

import re
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
DEPLOY = REPO_ROOT / "deploy"

PROD_NGINX_CONFIGS = (
    DEPLOY / "nginx.bootstrap.conf",
    DEPLOY / "nginx.ssl.conf.template",
    DEPLOY / "nginx.runtime.conf",
    DEPLOY / "nginx.local.conf",
)

BLANKET_BOT_LOCATION = re.compile(r"^\s*location\s+/bot/\s*\{", re.MULTILINE)
WEBHOOK_LOCATION = "location /bot/webhook"
WHATSAPP_LOCATION = "location /bot/whatsapp/"


def test_prod_nginx_no_blanket_bot_prefix() -> None:
    for path in PROD_NGINX_CONFIGS:
        text = path.read_text(encoding="utf-8")
        assert not BLANKET_BOT_LOCATION.search(text), (
            f"{path.name} must not use blanket location /bot/ (STAB-OPS-02)"
        )


def test_prod_nginx_has_webhook_locations() -> None:
    for path in PROD_NGINX_CONFIGS:
        text = path.read_text(encoding="utf-8")
        assert WEBHOOK_LOCATION in text, f"{path.name} missing {WEBHOOK_LOCATION}"
        assert WHATSAPP_LOCATION in text, f"{path.name} missing {WHATSAPP_LOCATION}"


def test_ssl_template_listens_telegram_alt_port_8443() -> None:
    text = (DEPLOY / "nginx.ssl.conf.template").read_text(encoding="utf-8")
    assert "listen 8443 ssl" in text
    assert text.count(WEBHOOK_LOCATION) >= 2
