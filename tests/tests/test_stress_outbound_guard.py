"""Wave 4: stress outbound guard and STRESS_TG_USER config."""
from __future__ import annotations

import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
SCRIPTS = ROOT / "scripts"
if str(SCRIPTS) not in sys.path:
    sys.path.insert(0, str(SCRIPTS))

from stress_outbound_guard import default_stress_tg_user, outbound_notify_patch


def test_default_stress_tg_user_fallback(monkeypatch: pytest.MonkeyPatch):
    import stress_outbound_guard as guard

    monkeypatch.setattr(guard.settings, "stress_tg_user", "")
    assert default_stress_tg_user() == "900000001"


def test_default_stress_tg_user_from_settings(monkeypatch: pytest.MonkeyPatch):
    import stress_outbound_guard as guard

    monkeypatch.setattr(guard.settings, "stress_tg_user", "test-bot-user")
    assert default_stress_tg_user() == "test-bot-user"


@pytest.mark.asyncio
async def test_outbound_notify_patch_stubs_telegram():
    from bot.services import notification_service, telegram_org_service

    org = object()
    customer = object()

    with outbound_notify_patch(live=False):
        tg_result = await telegram_org_service.send_telegram_for_org(org, "1", "hi")
        cust_result = await notification_service.send_customer_message(org, customer, "hi")

    assert tg_result.ok
    assert cust_result.ok
