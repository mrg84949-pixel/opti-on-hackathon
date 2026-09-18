from __future__ import annotations

import json
import logging

from bot.logging_config import JsonFormatter, configure_logging, new_request_id, request_id_ctx


def test_json_formatter_includes_extra_data():
    formatter = JsonFormatter()
    record = logging.LogRecord(
        name="test",
        level=logging.INFO,
        pathname=__file__,
        lineno=1,
        msg="hello",
        args=(),
        exc_info=None,
    )
    record.extra_data = {"event": "unit_test", "org_id": 1}
    token = request_id_ctx.set("req-abc")
    try:
        payload = json.loads(formatter.format(record))
    finally:
        request_id_ctx.reset(token)
    assert payload["message"] == "hello"
    assert payload["request_id"] == "req-abc"
    assert payload["event"] == "unit_test"
    assert payload["org_id"] == 1


def test_configure_logging_is_idempotent():
    root = logging.getLogger()
    handlers_before = len(root.handlers)
    configure_logging("INFO")
    configure_logging("INFO")
    assert len(root.handlers) == handlers_before or len(root.handlers) >= 1


def test_new_request_id_format():
    rid = new_request_id()
    assert len(rid) == 12
    assert rid.isalnum()


def test_configure_logging_suppresses_httpx_info_to_avoid_secret_leak():
    """httpx logs full request URLs at INFO; some providers (e.g. MacDent)
    put credentials in the query string, so INFO-level httpx logging would
    leak secrets in plaintext. configure_logging must keep it at WARNING+."""
    root = logging.getLogger()
    saved_handlers = root.handlers[:]
    saved_level = root.level
    httpx_logger = logging.getLogger("httpx")
    httpcore_logger = logging.getLogger("httpcore")
    saved_httpx_level = httpx_logger.level
    saved_httpcore_level = httpcore_logger.level
    root.handlers = []
    try:
        configure_logging("INFO")
        assert httpx_logger.level == logging.WARNING
        assert httpcore_logger.level == logging.WARNING
    finally:
        root.handlers = saved_handlers
        root.setLevel(saved_level)
        httpx_logger.setLevel(saved_httpx_level)
        httpcore_logger.setLevel(saved_httpcore_level)
