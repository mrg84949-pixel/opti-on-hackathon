from __future__ import annotations

import httpx

from bot.logging_config import get_logger
from bot.services.outbound_result import is_auth_http_status

logger = get_logger(__name__)


async def send_message(
    *, instance_id: str, api_token: str, chat_id: str, text: str
) -> tuple[bool, str | None, bool]:
    url = f"https://api.green-api.com/waInstance{instance_id}/sendMessage/{api_token}"
    payload = {"chatId": chat_id, "message": text[:12000]}
    async with httpx.AsyncClient(timeout=25) as client:
        response = await client.post(url, json=payload)
        if response.is_success:
            return True, None, False
        err = response.text[:500]
        auth_failed = is_auth_http_status(response.status_code)
        logger.warning(
            "Green-API send failed",
            extra={"extra_data": {"event": "green_wa_send_fail", "status": response.status_code}},
        )
        return False, err, auth_failed
