import pytest

from bot.services.outbound_health import channel_send_healthy, record_auth_failure, reset_outbound_health_cache
from bot.services.outbound_result import (
    OutboundSendResult,
    is_auth_http_status,
    telegram_response_auth_failed,
)


@pytest.fixture(autouse=True)
def _clean_health_cache():
    reset_outbound_health_cache()
    yield
    reset_outbound_health_cache()


def test_is_auth_http_status():
    assert is_auth_http_status(401) is True
    assert is_auth_http_status(403) is True
    assert is_auth_http_status(500) is False


def test_telegram_response_auth_failed():
    assert telegram_response_auth_failed(401, "") is True
    assert telegram_response_auth_failed(400, "Unauthorized: bot token") is True
    assert telegram_response_auth_failed(400, "bad request") is False


def test_outbound_send_result_bool():
    assert bool(OutboundSendResult.success("telegram")) is True
    assert bool(OutboundSendResult.auth_error("telegram")) is False


def test_record_and_clear_channel_health():
    from bot.services.outbound_health import clear_channel_health

    assert channel_send_healthy(1, "telegram") is True
    record_auth_failure(1, "telegram")
    assert channel_send_healthy(1, "telegram") is False
    clear_channel_health(1, "telegram")
    assert channel_send_healthy(1, "telegram") is True


def test_channel_health_isolated_per_org():
    record_auth_failure(1, "whatsapp")
    assert channel_send_healthy(1, "whatsapp") is False
    assert channel_send_healthy(2, "whatsapp") is True


def test_dashboard_setup_send_healthy_flags():
    from types import SimpleNamespace

    from bot.services.stats_service import _telegram_connected, _whatsapp_connected

    reset_outbound_health_cache()
    org = SimpleNamespace(
        id=5,
        telegram_bot_token="enc",
        whatsapp_provider="green",
        whatsapp_instance_id="inst",
        whatsapp_api_token="enc",
        whatsapp_meta_phone_number_id=None,
        whatsapp_meta_access_token=None,
    )
    assert _telegram_connected(org) is True
    assert channel_send_healthy(5, "telegram") is True
    record_auth_failure(5, "telegram")
    assert channel_send_healthy(5, "telegram") is False
    assert _whatsapp_connected(org) is True
    assert channel_send_healthy(5, "whatsapp") is True
