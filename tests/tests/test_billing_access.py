from __future__ import annotations

from datetime import datetime, timedelta, timezone

from bot.billing_access import (
    BOT_PAUSED_MESSAGE,
    TARIFF_BLOCKED_MESSAGE,
    frozen_reply_for_org,
    org_bot_enabled,
    org_bot_operational,
    org_subscription_active,
)
from bot.db.models import Organization


def test_org_subscription_active_when_no_deadline():
    org = Organization(name="x", billing_paid_until=None)
    assert org_subscription_active(org) is True


def test_org_subscription_active_missing_attr_defaults_true():
    """Test doubles without billing_paid_until stay allowed (getattr)."""
    from types import SimpleNamespace

    assert org_subscription_active(SimpleNamespace(id=1)) is True


def test_org_subscription_active_future_deadline():
    org = Organization(
        name="x",
        billing_paid_until=datetime.now(timezone.utc) + timedelta(days=1),
    )
    assert org_subscription_active(org) is True


def test_org_subscription_inactive_past_deadline():
    org = Organization(
        name="x",
        billing_paid_until=datetime.now(timezone.utc) - timedelta(hours=1),
    )
    assert org_subscription_active(org) is False


def test_org_bot_enabled_defaults_true():
    org = Organization(name="x")
    assert org_bot_enabled(org) is True


def test_org_bot_operational_requires_enabled_and_billing():
    org = Organization(name="x", bot_enabled=False, billing_paid_until=None)
    assert org_bot_operational(org) is False
    org.bot_enabled = True
    assert org_bot_operational(org) is True
    org.billing_paid_until = datetime.now(timezone.utc) - timedelta(days=1)
    assert org_bot_operational(org) is False


def test_tariff_message_non_empty():
    assert len(TARIFF_BLOCKED_MESSAGE) > 20


def test_bot_paused_message_non_empty():
    assert len(BOT_PAUSED_MESSAGE) > 20


def test_frozen_reply_for_org_billing_expired():
    org = Organization(
        name="x",
        bot_enabled=True,
        billing_paid_until=datetime.now(timezone.utc) - timedelta(hours=1),
    )
    assert frozen_reply_for_org(org) == TARIFF_BLOCKED_MESSAGE


def test_frozen_reply_for_org_bot_paused():
    org = Organization(name="x", bot_enabled=False, billing_paid_until=None)
    assert frozen_reply_for_org(org) == BOT_PAUSED_MESSAGE


def test_frozen_reply_for_org_operational():
    org = Organization(
        name="x",
        bot_enabled=True,
        billing_paid_until=datetime.now(timezone.utc) + timedelta(days=1),
    )
    assert frozen_reply_for_org(org) is None
