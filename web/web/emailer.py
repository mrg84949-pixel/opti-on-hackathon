from __future__ import annotations

import smtplib
from email.message import EmailMessage

from bot.config import settings
from bot.logging_config import get_logger

logger = get_logger(__name__)


def send_email(*, to_email: str, subject: str, text: str) -> bool:
    host = settings.smtp_host
    port = settings.smtp_port
    username = settings.smtp_username
    password = settings.smtp_password
    sender = settings.smtp_from_email or username
    use_tls = settings.smtp_use_tls

    if not host or not sender:
        logger.warning("SMTP is not configured", extra={"extra_data": {"event": "smtp_not_configured"}})
        return False

    message = EmailMessage()
    message["Subject"] = subject
    message["From"] = sender
    message["To"] = to_email
    message.set_content(text)

    try:
        with smtplib.SMTP(host, port, timeout=15) as server:
            if use_tls:
                server.starttls()
            if username and password:
                server.login(username, password)
            server.send_message(message)
        return True
    except Exception:
        logger.exception("Failed to send email", extra={"extra_data": {"event": "smtp_send_failed"}})
        return False
