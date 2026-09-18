from __future__ import annotations

from xml.sax.saxutils import escape

from fastapi import APIRouter, Form, Response

from bot.config import settings
from bot.llm.llm_engine import get_ai_response
from bot.logging_config import get_logger

logger = get_logger(__name__)

router = APIRouter()

_GREETING = "Здравствуйте! Это ИИ-ассистент клиники. Чем могу помочь?"
_NO_SPEECH = "Извините, я не расслышал. Повторите, пожалуйста."
_GOODBYE = "Не расслышал, до свидания."
_VOICE = "Polly.Tatyana"
_LANG = "ru-RU"

# MVP: Twilio's built-in speech recognizer (Gather input="speech") does STT
# for us and gives text back to /voice/gather each turn — no Media Streams
# websocket needed. Simplest path to a working demo; swap for Media Streams
# later if barge-in / lower latency matters more than hackathon speed.


def _twiml(body: str) -> Response:
    return Response(content=body, media_type="application/xml")


def _gather_twiml(*, say: str) -> str:
    safe_say = escape(say)
    return (
        '<?xml version="1.0" encoding="UTF-8"?>'
        "<Response>"
        f'<Gather input="speech" action="/bot/voice/gather" method="POST" '
        f'language="{_LANG}" speechTimeout="auto">'
        f'<Say language="{_LANG}" voice="{_VOICE}">{safe_say}</Say>'
        "</Gather>"
        f'<Say language="{_LANG}" voice="{_VOICE}">{escape(_GOODBYE)}</Say>'
        "<Hangup/>"
        "</Response>"
    )


@router.post("/voice/incoming")
async def voice_incoming() -> Response:
    """Twilio calls this when a phone call comes in — first turn."""
    return _twiml(_gather_twiml(say=_GREETING))


@router.post("/voice/gather")
async def voice_gather(
    CallSid: str = Form(...),
    SpeechResult: str = Form(""),
) -> Response:
    """Twilio posts the recognized speech text here on every turn of the call."""
    user_text = (SpeechResult or "").strip()
    if not user_text:
        return _twiml(_gather_twiml(say=_NO_SPEECH))

    reply = await get_ai_response(
        user_id=f"call:{CallSid}",
        user_text=user_text,
        db_memory={},
        channel="voice",
        org_id=settings.default_org_id,
    )
    logger.info(
        "Voice call turn",
        extra={"extra_data": {"event": "voice_turn", "call_sid": CallSid}},
    )
    return _twiml(_gather_twiml(say=reply))
