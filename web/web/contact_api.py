from __future__ import annotations

import re

from fastapi import APIRouter
from pydantic import BaseModel, Field

from bot.config import settings
from bot.logging_config import get_logger
from web.emailer import send_email

logger = get_logger(__name__)

router = APIRouter()


class ContactFormPayload(BaseModel):
    name: str = Field(min_length=1, max_length=200)
    phone: str = Field(min_length=5, max_length=40)
    comment: str = Field(default="", max_length=4000)


_PHONE_RE = re.compile(r"^[\d\s+\-()]+$")


@router.post("/contact")
async def submit_contact_form(payload: ContactFormPayload):
    name = payload.name.strip()
    phone = payload.phone.strip()
    comment = (payload.comment or "").strip()
    if not _PHONE_RE.match(phone):
        return {"status": "error", "detail": "invalid_phone"}

    dest = (settings.contact_form_to_email or settings.smtp_from_email).strip()
    if not dest:
        logger.warning(
            "Contact form: no recipient email configured",
            extra={"extra_data": {"event": "contact_form_no_dest"}},
        )
        return {"status": "error", "detail": "email_not_configured"}

    subject = f"Контактная форма: {name}"
    body = f"Имя: {name}\nТелефон: {phone}\n\nСообщение:\n{comment or '(пусто)'}\n"
    ok = send_email(to_email=dest, subject=subject, text=body)
    if not ok:
        return {"status": "error", "detail": "send_failed"}
    return {"status": "ok"}
