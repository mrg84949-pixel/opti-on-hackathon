from __future__ import annotations

from datetime import datetime, timedelta, timezone
from types import SimpleNamespace

import pytest

from bot.automation import reminders, scheduler
from bot.services import notification_service
from bot.services.outbound_health import reset_outbound_health_cache
from bot.services.outbound_result import OutboundSendResult


def _org(**overrides):
    base = {
        "id": 1,
        "bot_enabled": True,
        "billing_paid_until": None,
        "telegram_bot_token": "org-tg-token",
        "whatsapp_provider": None,
        "whatsapp_instance_id": None,
        "whatsapp_api_token": None,
        "whatsapp_meta_phone_number_id": None,
        "whatsapp_meta_access_token": None,
        "whatsapp_meta_reminder_template_name": None,
        "whatsapp_meta_reminder_template_lang": "ru",
    }
    base.update(overrides)
    return SimpleNamespace(**base)


class _FakeHTTPResponse:
    def __init__(self, *, status_code: int = 200):
        self.status_code = status_code

    @property
    def is_success(self) -> bool:
        return 200 <= self.status_code < 300


class _FakeAsyncClient:
    def __init__(self, response: _FakeHTTPResponse):
        self.response = response
        self.calls: list[dict] = []

    async def __aenter__(self):
        return self

    async def __aexit__(self, exc_type, exc, tb):
        return False

    async def post(self, url, json=None):
        self.calls.append({"url": url, "json": json})
        return self.response


def test_telegram_url_and_digits(monkeypatch: pytest.MonkeyPatch):
    empty_org = _org(telegram_bot_token="")
    monkeypatch.setattr("bot.config.settings.tenant_config_strict", True)
    monkeypatch.setattr("bot.config.settings.telegram_token", "")
    assert notification_service.telegram_url(empty_org) is None
    assert notification_service.telegram_url(_org(telegram_bot_token="token123")) == (
        "https://api.telegram.org/bottoken123/sendMessage"
    )
    assert notification_service.wa_digits_from_phone_field("wa:+7 (777) 111-22-33") == "77771112233"
    assert notification_service.wa_digits_from_phone_field("777@c.us") == "777"


@pytest.mark.asyncio
async def test_send_telegram_and_routing(monkeypatch: pytest.MonkeyPatch):
    client = _FakeAsyncClient(_FakeHTTPResponse(status_code=200))
    org = _org(telegram_bot_token="org-tg-token")
    monkeypatch.setattr(
        "bot.services.telegram_org_service.httpx.AsyncClient",
        lambda timeout=20: client,
    )
    result = await notification_service.send_telegram(org, "777", "hello")
    assert result.ok is True
    assert "botorg-tg-token/sendMessage" in client.calls[0]["url"]
    assert client.calls[0]["json"]["chat_id"] == 777

    async def fake_send_reminder(org, chat_id, text):
        assert chat_id == "777@c.us"
        return OutboundSendResult.success("whatsapp")

    monkeypatch.setattr(notification_service, "send_whatsapp_customer_message", fake_send_reminder)
    assert (await notification_service.send_customer_message(_org(), SimpleNamespace(phone="wa:777@c.us"), "text")).ok
    assert (await notification_service.send_customer_message(_org(), SimpleNamespace(phone="tg:777"), "text")).ok
    assert not (await notification_service.send_customer_message(_org(), SimpleNamespace(phone="email:test@example.com"), "text")).ok


@pytest.mark.asyncio
async def test_send_whatsapp_reminder_meta_template_and_fallback(monkeypatch: pytest.MonkeyPatch):
    meta_org = _org(
        whatsapp_provider="meta",
        whatsapp_meta_reminder_template_name="reminder_24h",
        whatsapp_meta_reminder_template_lang="kk",
    )
    called: dict[str, object] = {}

    async def fake_template(org, to_phone, *, template_name, language_code, body_parameters):
        called["template"] = {
            "org": org,
            "to_phone": to_phone,
            "template_name": template_name,
            "language_code": language_code,
            "body_parameters": body_parameters,
        }
        return OutboundSendResult.success("whatsapp")

    async def fake_text(org, chat_id, text):
        called["text"] = {"org": org, "chat_id": chat_id, "text": text}
        return OutboundSendResult.success("whatsapp")

    monkeypatch.setattr(notification_service, "send_whatsapp_template", fake_template)
    monkeypatch.setattr(notification_service, "send_whatsapp_text", fake_text)
    assert (await notification_service.send_whatsapp_customer_message(meta_org, "777@c.us", "hello")).ok
    assert called["template"]["to_phone"] == "777"
    assert called["template"]["language_code"] == "kk"

    plain_org = _org(whatsapp_provider="meta", whatsapp_meta_reminder_template_name=None)
    assert (await notification_service.send_whatsapp_customer_message(plain_org, "777@c.us", "plain")).ok
    assert called["text"]["chat_id"] == "777@c.us"


def test_render_reminder_texts():
    when = datetime(2026, 6, 1, 10, 0, tzinfo=timezone.utc)
    text_2h = notification_service.render_2h_reminder_text(when)
    text_24h = notification_service.render_24h_reminder_text(when)
    assert "2 часа" in text_2h
    assert "24 часа" in text_24h
    assert "01.06.2026 10:00 UTC" in text_2h
    assert "ответьте в чат" in text_2h.lower()


@pytest.mark.asyncio
async def test_process_2h_reminders_marks_sent(monkeypatch: pytest.MonkeyPatch):
    now = datetime.now(timezone.utc)
    ok_org = _org(id=1)
    ok_customer = SimpleNamespace(id=10, phone="wa:777@c.us", organization=ok_org)
    no_org_customer = SimpleNamespace(id=11, phone="wa:888@c.us", organization=None)
    failing_customer = SimpleNamespace(id=12, phone="wa:999@c.us", organization=_org(id=3))

    ok_appt = SimpleNamespace(
        customer=ok_customer,
        scheduled_at=now + timedelta(hours=2, minutes=5),
        status=None,
        reminder_2h_sent_at=None,
    )
    no_org_appt = SimpleNamespace(
        customer=no_org_customer,
        scheduled_at=now + timedelta(hours=2, minutes=6),
        status=None,
        reminder_2h_sent_at=None,
    )
    failing_appt = SimpleNamespace(
        customer=failing_customer,
        scheduled_at=now + timedelta(hours=2, minutes=7),
        status=None,
        reminder_2h_sent_at=None,
    )

    class _FakeResult:
        def scalars(self):
            return self

        def all(self):
            return [ok_appt, no_org_appt, failing_appt]

    class _FakeSession:
        committed = False

        async def execute(self, _):
            return _FakeResult()

        async def commit(self):
            self.committed = True

    fake_session = _FakeSession()

    class _FakeSessionManager:
        async def __aenter__(self):
            return fake_session

        async def __aexit__(self, exc_type, exc, tb):
            return False

    sent_texts: list[str] = []

    async def fake_send(org_arg, customer_arg, text_arg):
        sent_texts.append(text_arg)
        if customer_arg is ok_customer:
            assert "2 часа" in text_arg
            return OutboundSendResult.success()
        if customer_arg is failing_customer:
            raise RuntimeError("network")
        return OutboundSendResult.retryable_error()

    monkeypatch.setattr(reminders, "AsyncSessionLocal", lambda: _FakeSessionManager())
    monkeypatch.setattr(notification_service, "send_customer_message", fake_send)
    await reminders.process_2h_reminders()

    assert fake_session.committed is True
    assert ok_appt.reminder_2h_sent_at is not None
    assert no_org_appt.reminder_2h_sent_at is None
    assert failing_appt.reminder_2h_sent_at is None
    assert len(sent_texts) == 2


@pytest.mark.asyncio
async def test_process_2h_reminders_skips_frozen_org(monkeypatch: pytest.MonkeyPatch):
    now = datetime.now(timezone.utc)
    ok_org = _org(id=1, bot_enabled=True, billing_paid_until=None)
    paused_org = _org(id=2, bot_enabled=False, billing_paid_until=None)
    expired_org = _org(
        id=3,
        bot_enabled=True,
        billing_paid_until=now - timedelta(hours=1),
    )
    ok_customer = SimpleNamespace(id=10, phone="wa:777@c.us", organization=ok_org)
    paused_customer = SimpleNamespace(id=11, phone="wa:888@c.us", organization=paused_org)
    expired_customer = SimpleNamespace(id=12, phone="wa:999@c.us", organization=expired_org)

    ok_appt = SimpleNamespace(
        customer=ok_customer,
        scheduled_at=now + timedelta(hours=2, minutes=5),
        status=None,
        reminder_2h_sent_at=None,
    )
    paused_appt = SimpleNamespace(
        customer=paused_customer,
        scheduled_at=now + timedelta(hours=2, minutes=6),
        status=None,
        reminder_2h_sent_at=None,
    )
    expired_appt = SimpleNamespace(
        customer=expired_customer,
        scheduled_at=now + timedelta(hours=2, minutes=7),
        status=None,
        reminder_2h_sent_at=None,
    )

    class _FakeResult:
        def scalars(self):
            return self

        def all(self):
            return [ok_appt, paused_appt, expired_appt]

    class _FakeSession:
        committed = False

        async def execute(self, _):
            return _FakeResult()

        async def commit(self):
            self.committed = True

    fake_session = _FakeSession()

    class _FakeSessionManager:
        async def __aenter__(self):
            return fake_session

        async def __aexit__(self, exc_type, exc, tb):
            return False

    sent_customers: list[object] = []

    async def fake_send(org_arg, customer_arg, _text_arg):
        sent_customers.append(customer_arg)
        return OutboundSendResult.success() if customer_arg is ok_customer else OutboundSendResult.retryable_error()

    monkeypatch.setattr(reminders, "AsyncSessionLocal", lambda: _FakeSessionManager())
    monkeypatch.setattr(notification_service, "send_customer_message", fake_send)
    await reminders.process_2h_reminders()

    assert fake_session.committed is True
    assert ok_appt.reminder_2h_sent_at is not None
    assert paused_appt.reminder_2h_sent_at is None
    assert expired_appt.reminder_2h_sent_at is None
    assert sent_customers == [ok_customer]


@pytest.mark.asyncio
async def test_process_24h_reminders_skips_frozen_org(monkeypatch: pytest.MonkeyPatch):
    now = datetime.now(timezone.utc)
    ok_org = _org(id=1, bot_enabled=True, billing_paid_until=None)
    paused_org = _org(id=2, bot_enabled=False, billing_paid_until=None)
    ok_customer = SimpleNamespace(id=10, phone="wa:777@c.us", organization=ok_org)
    paused_customer = SimpleNamespace(id=11, phone="wa:888@c.us", organization=paused_org)

    ok_appt = SimpleNamespace(
        customer=ok_customer,
        scheduled_at=now + timedelta(hours=24, minutes=5),
        status=None,
        reminder_24h_sent_at=None,
    )
    paused_appt = SimpleNamespace(
        customer=paused_customer,
        scheduled_at=now + timedelta(hours=24, minutes=6),
        status=None,
        reminder_24h_sent_at=None,
    )

    class _FakeResult:
        def scalars(self):
            return self

        def all(self):
            return [ok_appt, paused_appt]

    class _FakeSession:
        committed = False

        async def execute(self, _):
            return _FakeResult()

        async def commit(self):
            self.committed = True

    fake_session = _FakeSession()

    class _FakeSessionManager:
        async def __aenter__(self):
            return fake_session

        async def __aexit__(self, exc_type, exc, tb):
            return False

    sent_customers: list[object] = []

    async def fake_send(org_arg, customer_arg, _text_arg):
        sent_customers.append(customer_arg)
        return OutboundSendResult.success() if customer_arg is ok_customer else OutboundSendResult.retryable_error()

    monkeypatch.setattr(reminders, "AsyncSessionLocal", lambda: _FakeSessionManager())
    monkeypatch.setattr(notification_service, "send_customer_message", fake_send)
    await reminders.process_24h_reminders()

    assert ok_appt.reminder_24h_sent_at is not None
    assert paused_appt.reminder_24h_sent_at is None
    assert sent_customers == [ok_customer]


@pytest.mark.asyncio
async def test_process_24h_reminders_skips_or_does_not_mark_on_failure(monkeypatch: pytest.MonkeyPatch):
    now = datetime.now(timezone.utc)
    ok_org = _org(id=1)
    ok_customer = SimpleNamespace(id=10, phone="wa:777@c.us", organization=ok_org)
    no_org_customer = SimpleNamespace(id=11, phone="wa:888@c.us", organization=None)
    failing_customer = SimpleNamespace(id=12, phone="wa:999@c.us", organization=_org(id=3))

    ok_appt = SimpleNamespace(
        customer=ok_customer,
        scheduled_at=now + timedelta(hours=24, minutes=5),
        status=None,
        reminder_24h_sent_at=None,
    )
    no_org_appt = SimpleNamespace(
        customer=no_org_customer,
        scheduled_at=now + timedelta(hours=24, minutes=6),
        status=None,
        reminder_24h_sent_at=None,
    )
    failing_appt = SimpleNamespace(
        customer=failing_customer,
        scheduled_at=now + timedelta(hours=24, minutes=7),
        status=None,
        reminder_24h_sent_at=None,
    )

    class _FakeResult:
        def scalars(self):
            return self

        def all(self):
            return [ok_appt, no_org_appt, failing_appt]

    class _FakeSession:
        committed = False

        async def execute(self, _):
            return _FakeResult()

        async def commit(self):
            self.committed = True

    fake_session = _FakeSession()

    class _FakeSessionManager:
        async def __aenter__(self):
            return fake_session

        async def __aexit__(self, exc_type, exc, tb):
            return False

    async def fake_send(org_arg, customer_arg, _text_arg):
        if customer_arg is ok_customer:
            return OutboundSendResult.success()
        if customer_arg is failing_customer:
            raise RuntimeError("network")
        return OutboundSendResult.retryable_error()

    monkeypatch.setattr(reminders, "AsyncSessionLocal", lambda: _FakeSessionManager())
    monkeypatch.setattr(notification_service, "send_customer_message", fake_send)
    await reminders.process_24h_reminders()

    assert fake_session.committed is True
    assert ok_appt.reminder_24h_sent_at is not None
    assert no_org_appt.reminder_24h_sent_at is None
    assert failing_appt.reminder_24h_sent_at is None


@pytest.mark.asyncio
async def test_process_2h_reminders_skips_reminders_disabled(monkeypatch: pytest.MonkeyPatch):
    now = datetime.now(timezone.utc)
    ok_org = _org(id=1)
    ok_customer = SimpleNamespace(
        id=10,
        phone="wa:777@c.us",
        organization=ok_org,
        disable_reminders=False,
    )
    opted_out_customer = SimpleNamespace(
        id=11,
        phone="wa:888@c.us",
        organization=ok_org,
        disable_reminders=True,
    )

    ok_appt = SimpleNamespace(
        customer=ok_customer,
        scheduled_at=now + timedelta(hours=2, minutes=5),
        status=None,
        reminder_2h_sent_at=None,
    )
    opted_out_appt = SimpleNamespace(
        customer=opted_out_customer,
        scheduled_at=now + timedelta(hours=2, minutes=6),
        status=None,
        reminder_2h_sent_at=None,
    )

    class _FakeResult:
        def scalars(self):
            return self

        def all(self):
            return [ok_appt, opted_out_appt]

    class _FakeSession:
        committed = False

        async def execute(self, _):
            return _FakeResult()

        async def commit(self):
            self.committed = True

    fake_session = _FakeSession()

    class _FakeSessionManager:
        async def __aenter__(self):
            return fake_session

        async def __aexit__(self, exc_type, exc, tb):
            return False

    sent_customers: list[object] = []

    async def fake_send(org_arg, customer_arg, _text_arg):
        sent_customers.append(customer_arg)
        return OutboundSendResult.success() if customer_arg is ok_customer else OutboundSendResult.retryable_error()

    monkeypatch.setattr(reminders, "AsyncSessionLocal", lambda: _FakeSessionManager())
    monkeypatch.setattr(notification_service, "send_customer_message", fake_send)
    await reminders.process_2h_reminders()

    assert ok_appt.reminder_2h_sent_at is not None
    assert opted_out_appt.reminder_2h_sent_at is None
    assert sent_customers == [ok_customer]


@pytest.mark.asyncio
async def test_process_24h_reminders_skips_reminders_disabled(monkeypatch: pytest.MonkeyPatch):
    now = datetime.now(timezone.utc)
    ok_org = _org(id=1)
    ok_customer = SimpleNamespace(
        id=10,
        phone="wa:777@c.us",
        organization=ok_org,
        disable_reminders=False,
    )
    opted_out_customer = SimpleNamespace(
        id=11,
        phone="wa:888@c.us",
        organization=ok_org,
        disable_reminders=True,
    )

    ok_appt = SimpleNamespace(
        customer=ok_customer,
        scheduled_at=now + timedelta(hours=24, minutes=5),
        status=None,
        reminder_24h_sent_at=None,
    )
    opted_out_appt = SimpleNamespace(
        customer=opted_out_customer,
        scheduled_at=now + timedelta(hours=24, minutes=6),
        status=None,
        reminder_24h_sent_at=None,
    )

    class _FakeResult:
        def scalars(self):
            return self

        def all(self):
            return [ok_appt, opted_out_appt]

    class _FakeSession:
        committed = False

        async def execute(self, _):
            return _FakeResult()

        async def commit(self):
            self.committed = True

    fake_session = _FakeSession()

    class _FakeSessionManager:
        async def __aenter__(self):
            return fake_session

        async def __aexit__(self, exc_type, exc, tb):
            return False

    sent_customers: list[object] = []

    async def fake_send(org_arg, customer_arg, _text_arg):
        sent_customers.append(customer_arg)
        return OutboundSendResult.success() if customer_arg is ok_customer else OutboundSendResult.retryable_error()

    monkeypatch.setattr(reminders, "AsyncSessionLocal", lambda: _FakeSessionManager())
    monkeypatch.setattr(notification_service, "send_customer_message", fake_send)
    await reminders.process_24h_reminders()

    assert ok_appt.reminder_24h_sent_at is not None
    assert opted_out_appt.reminder_24h_sent_at is None
    assert sent_customers == [ok_customer]


@pytest.mark.asyncio
async def test_process_2h_reminders_skips_muted_customer(monkeypatch: pytest.MonkeyPatch):
    now = datetime.now(timezone.utc)
    ok_org = _org(id=1)
    ok_customer = SimpleNamespace(
        id=10,
        phone="wa:777@c.us",
        organization=ok_org,
        disable_reminders=False,
        muted_until=now - timedelta(days=1),
    )
    muted_customer = SimpleNamespace(
        id=11,
        phone="wa:888@c.us",
        organization=ok_org,
        disable_reminders=False,
        muted_until=now + timedelta(days=7),
    )

    ok_appt = SimpleNamespace(
        customer=ok_customer,
        scheduled_at=now + timedelta(hours=2, minutes=5),
        status=None,
        reminder_2h_sent_at=None,
    )
    muted_appt = SimpleNamespace(
        customer=muted_customer,
        scheduled_at=now + timedelta(hours=2, minutes=6),
        status=None,
        reminder_2h_sent_at=None,
    )

    class _FakeResult:
        def scalars(self):
            return self

        def all(self):
            return [ok_appt, muted_appt]

    class _FakeSession:
        committed = False

        async def execute(self, _):
            return _FakeResult()

        async def commit(self):
            self.committed = True

    fake_session = _FakeSession()

    class _FakeSessionManager:
        async def __aenter__(self):
            return fake_session

        async def __aexit__(self, exc_type, exc, tb):
            return False

    sent_customers: list[object] = []

    async def fake_send(org_arg, customer_arg, _text_arg):
        sent_customers.append(customer_arg)
        return OutboundSendResult.success()

    monkeypatch.setattr(reminders, "AsyncSessionLocal", lambda: _FakeSessionManager())
    monkeypatch.setattr(notification_service, "send_customer_message", fake_send)
    await reminders.process_2h_reminders()

    assert ok_appt.reminder_2h_sent_at is not None
    assert muted_appt.reminder_2h_sent_at is None
    assert sent_customers == [ok_customer]


@pytest.mark.asyncio
async def test_process_24h_reminders_skips_muted_customer(monkeypatch: pytest.MonkeyPatch):
    now = datetime.now(timezone.utc)
    ok_org = _org(id=1)
    ok_customer = SimpleNamespace(
        id=10,
        phone="wa:777@c.us",
        organization=ok_org,
        disable_reminders=False,
        muted_until=None,
    )
    muted_customer = SimpleNamespace(
        id=11,
        phone="wa:888@c.us",
        organization=ok_org,
        disable_reminders=False,
        muted_until=now + timedelta(days=7),
    )

    ok_appt = SimpleNamespace(
        customer=ok_customer,
        scheduled_at=now + timedelta(hours=24, minutes=5),
        status=None,
        reminder_24h_sent_at=None,
    )
    muted_appt = SimpleNamespace(
        customer=muted_customer,
        scheduled_at=now + timedelta(hours=24, minutes=6),
        status=None,
        reminder_24h_sent_at=None,
    )

    class _FakeResult:
        def scalars(self):
            return self

        def all(self):
            return [ok_appt, muted_appt]

    class _FakeSession:
        committed = False

        async def execute(self, _):
            return _FakeResult()

        async def commit(self):
            self.committed = True

    fake_session = _FakeSession()

    class _FakeSessionManager:
        async def __aenter__(self):
            return fake_session

        async def __aexit__(self, exc_type, exc, tb):
            return False

    sent_customers: list[object] = []

    async def fake_send(org_arg, customer_arg, _text_arg):
        sent_customers.append(customer_arg)
        return OutboundSendResult.success()

    monkeypatch.setattr(reminders, "AsyncSessionLocal", lambda: _FakeSessionManager())
    monkeypatch.setattr(notification_service, "send_customer_message", fake_send)
    await reminders.process_24h_reminders()

    assert ok_appt.reminder_24h_sent_at is not None
    assert muted_appt.reminder_24h_sent_at is None
    assert sent_customers == [ok_customer]


@pytest.mark.asyncio
async def test_process_2h_reminders_fair_queue_small_org_not_starved(monkeypatch: pytest.MonkeyPatch):
    now = datetime.now(timezone.utc)
    big_org = _org(id=1)
    small_org = _org(id=2)
    small_customer = SimpleNamespace(
        id=99,
        phone="wa:222@c.us",
        organization=small_org,
        disable_reminders=False,
        muted_until=None,
    )
    small_appt = SimpleNamespace(
        customer=small_customer,
        scheduled_at=now + timedelta(hours=2, minutes=5),
        status=None,
        reminder_2h_sent_at=None,
    )
    big_appts = []
    for i in range(50):
        customer = SimpleNamespace(
            id=100 + i,
            phone=f"wa:{100 + i}@c.us",
            organization=big_org,
            disable_reminders=False,
            muted_until=None,
        )
        big_appts.append(
            SimpleNamespace(
                customer=customer,
                scheduled_at=now + timedelta(hours=2, minutes=6 + i),
                status=None,
                reminder_2h_sent_at=None,
            )
        )
    all_rows = big_appts + [small_appt]

    class _FakeResult:
        def scalars(self):
            return self

        def all(self):
            return all_rows

    class _FakeSession:
        committed = False

        async def execute(self, _):
            return _FakeResult()

        async def commit(self):
            self.committed = True

    fake_session = _FakeSession()

    class _FakeSessionManager:
        async def __aenter__(self):
            return fake_session

        async def __aexit__(self, exc_type, exc, tb):
            return False

    sent_org_ids: list[int] = []

    async def fake_send(org_arg, customer_arg, _text_arg):
        sent_org_ids.append(org_arg.id)
        return OutboundSendResult.success()

    monkeypatch.setattr(reminders, "AsyncSessionLocal", lambda: _FakeSessionManager())
    monkeypatch.setattr(notification_service, "send_customer_message", fake_send)
    monkeypatch.setattr("bot.config.settings.reminder_fair_max_per_org_per_tick", 10)
    await reminders.process_2h_reminders()

    assert 2 in sent_org_ids
    assert len(sent_org_ids) == 11
    assert small_appt.reminder_2h_sent_at is not None
    assert sum(1 for appt in big_appts if appt.reminder_2h_sent_at is not None) == 10


def test_start_scheduler_creates_singleton_and_stop(monkeypatch: pytest.MonkeyPatch):
    created: list[object] = []

    class _FakeScheduler:
        def __init__(self, timezone):
            self.timezone = timezone
            self.running = False
            self.jobs: list[dict] = []
            self.shutdown_called = False
            created.append(self)

        def add_job(self, func, **kwargs):
            self.jobs.append({"func": func, **kwargs})

        def start(self):
            self.running = True

        def shutdown(self, wait=False):
            self.shutdown_called = True
            self.running = False

    monkeypatch.setattr(scheduler, "AsyncIOScheduler", _FakeScheduler)
    monkeypatch.setenv("REMINDER_JOB_INTERVAL_MINUTES", "0")
    scheduler._scheduler = None

    first = scheduler.start_scheduler()
    second = scheduler.start_scheduler()
    assert first is second
    assert len(created) == 1
    assert len(first.jobs) == 5
    assert first.jobs[0]["minutes"] == 1
    assert first.jobs[0]["id"] == "reminders_24h"
    assert first.jobs[1]["id"] == "reminders_2h"
    assert first.jobs[2]["id"] == "client_change_timeouts"
    assert first.jobs[3]["id"] == "retention_followups"
    assert first.jobs[4]["id"] == "crm_appointment_sync"

    scheduler.stop_scheduler()
    assert first.shutdown_called is True
    assert scheduler._scheduler is None


def test_stop_scheduler_when_not_running_is_safe():
    scheduler._scheduler = SimpleNamespace(running=False, shutdown=lambda wait=False: None)
    scheduler.stop_scheduler()
    assert scheduler._scheduler is None


@pytest.mark.asyncio
async def test_process_2h_reminders_marks_sent_on_auth_failure(monkeypatch: pytest.MonkeyPatch):
    reset_outbound_health_cache()
    now = datetime.now(timezone.utc)
    ok_org = _org(id=1)
    auth_customer = SimpleNamespace(id=10, phone="tg:777", organization=ok_org, disable_reminders=False)
    auth_appt = SimpleNamespace(
        customer=auth_customer,
        scheduled_at=now + timedelta(hours=2, minutes=5),
        status=None,
        reminder_2h_sent_at=None,
    )

    class _FakeResult:
        def scalars(self):
            return self

        def all(self):
            return [auth_appt]

    class _FakeSession:
        committed = False

        async def execute(self, _):
            return _FakeResult()

        async def commit(self):
            self.committed = True

    fake_session = _FakeSession()

    class _FakeSessionManager:
        async def __aenter__(self):
            return fake_session

        async def __aexit__(self, exc_type, exc, tb):
            return False

    async def fake_send(_org_arg, customer_arg, _text_arg):
        if customer_arg is auth_customer:
            return OutboundSendResult.auth_error("telegram")
        return OutboundSendResult.retryable_error()

    monkeypatch.setattr(reminders, "AsyncSessionLocal", lambda: _FakeSessionManager())
    monkeypatch.setattr(notification_service, "send_customer_message", fake_send)
    monkeypatch.setattr(
        "bot.services.customer_service.reminders_disabled",
        lambda _c: False,
    )
    await reminders.process_2h_reminders()

    assert fake_session.committed is True
    assert auth_appt.reminder_2h_sent_at is not None
