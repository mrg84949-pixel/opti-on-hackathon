from google import genai

from bot.config import settings

_client: genai.Client | None = None


def get_client() -> genai.Client | None:
    global _client
    if _client is not None:
        return _client
    if not settings.gemini_api_key:
        return None
    _client = genai.Client(api_key=settings.gemini_api_key)
    return _client


def get_model_name() -> str:
    return settings.gemini_model