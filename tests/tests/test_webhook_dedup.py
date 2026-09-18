from __future__ import annotations

import pytest

from bot.services import webhook_dedup


class _ScalarOneOrNoneResult:
    def __init__(self, value):
        self._value = value

    def scalar_one_or_none(self):
        return self._value


class _ClaimSession:
    def __init__(self, *, first_id: int | None = 1, duplicate_id: int | None = None):
        self._responses = [_ScalarOneOrNoneResult(first_id)]
        if duplicate_id is not None:
            self._responses.append(_ScalarOneOrNoneResult(duplicate_id))
        self.execute_calls = 0

    async def execute(self, _stmt):
        self.execute_calls += 1
        if not self._responses:
            return _ScalarOneOrNoneResult(None)
        return self._responses.pop(0)


def test_extract_telegram_event_key():
    assert webhook_dedup.extract_telegram_event_key({"update_id": 42}) == "42"
    assert webhook_dedup.extract_telegram_event_key({}) is None


def test_extract_meta_wa_event_key():
    payload = {
        "entry": [
            {
                "changes": [
                    {
                        "value": {
                            "messages": [
                                {"type": "text", "id": "wamid.abc", "text": {"body": "hi"}},
                            ]
                        }
                    }
                ]
            }
        ]
    }
    assert webhook_dedup.extract_meta_wa_event_key(payload) == "wamid.abc"
    assert webhook_dedup.extract_meta_wa_event_key({"entry": []}) is None


def test_extract_green_wa_event_key():
    assert webhook_dedup.extract_green_wa_event_key({"idMessage": "BAE5..."}) == "BAE5..."
    assert webhook_dedup.extract_green_wa_event_key({}) is None


@pytest.mark.asyncio
async def test_try_claim_first_insert_returns_true():
    session = _ClaimSession(first_id=7)
    claimed = await webhook_dedup.try_claim_inbound_event(session, channel="telegram", event_key="1")
    assert claimed is True
    assert session.execute_calls == 1


@pytest.mark.asyncio
async def test_try_claim_duplicate_returns_false():
    session = _ClaimSession(first_id=1, duplicate_id=None)
    first = await webhook_dedup.try_claim_inbound_event(session, channel="telegram", event_key="1")
    second = await webhook_dedup.try_claim_inbound_event(session, channel="telegram", event_key="1")
    assert first is True
    assert second is False


@pytest.mark.asyncio
async def test_claim_missing_key_skips_dedup():
    session = _ClaimSession()
    should_process = await webhook_dedup.claim_inbound_event_or_duplicate(
        session, channel="telegram", event_key=None
    )
    assert should_process is True
    assert session.execute_calls == 0
