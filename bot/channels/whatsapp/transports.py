"""Concrete WhatsAppTransport implementations — thin adapters over the
existing per-provider send functions, so green_api.py/meta_cloud.py keep
their exact wire-format logic untouched."""

from __future__ import annotations

from dataclasses import dataclass

from bot.channels.whatsapp import green_api, meta_cloud
from bot.channels.whatsapp.base import TransportSendResult


@dataclass(frozen=True)
class GreenApiTransport:
    instance_id: str
    api_token: str

    async def send_text(self, *, chat_id: str, text: str) -> TransportSendResult:
        ok, error, auth_failed = await green_api.send_message(
            instance_id=self.instance_id, api_token=self.api_token, chat_id=chat_id, text=text
        )
        return TransportSendResult(ok=ok, error=error, auth_failed=auth_failed)


@dataclass(frozen=True)
class MetaCloudTransport:
    phone_number_id: str
    access_token: str

    async def send_text(self, *, chat_id: str, text: str) -> TransportSendResult:
        ok, error, auth_failed = await meta_cloud.send_text_message(
            phone_number_id=self.phone_number_id,
            access_token=self.access_token,
            to_e164_or_chat=chat_id,
            text=text,
        )
        return TransportSendResult(ok=ok, error=error, auth_failed=auth_failed)

    async def send_template(
        self,
        *,
        to: str,
        template_name: str,
        language_code: str,
        body_parameters: list[str],
    ) -> TransportSendResult:
        """Templates are a WABA-only concept — Green API has no equivalent,
        so this stays off the shared WhatsAppTransport protocol."""
        ok, error, auth_failed = await meta_cloud.send_template_message(
            phone_number_id=self.phone_number_id,
            access_token=self.access_token,
            to_e164_or_chat=to,
            template_name=template_name,
            language_code=language_code,
            body_parameters=body_parameters,
        )
        return TransportSendResult(ok=ok, error=error, auth_failed=auth_failed)
