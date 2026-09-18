"""Pending booking draft and appointment card rendering."""
from __future__ import annotations

import re
from datetime import date, datetime, timedelta, timezone
from typing import Any
from zoneinfo import ZoneInfo

PENDING_BOOKING_KEY = "pending_booking"

PAST_BOOKING_MESSAGE = (
    "Нельзя записать на прошедшую дату или время. "
    "Предложи клиенту сегодня или другую будущую дату из свободных окон."
)
TIME_VAGUE_MESSAGE = (
    "Время «утром/вечером» слишком расплывчато. "
    "Уточни у клиента конкретный час (например 15:00) из свободных окон."
)
TIME_UNPARSEABLE_MESSAGE = (
    "Не удалось разобрать время «{time}». "
    "Уточни у клиента время (например 15, в 15, 15:00, 15 часов) из свободных окон."
)
FUTURE_BOOKING_LIMIT_DAYS = 365

_WEEKDAY_INDEX: dict[str, int] = {
    "понедельник": 0,
    "понедельника": 0,
    "понедельнику": 0,
    "пн": 0,
    "вторник": 1,
    "вторника": 1,
    "вторнику": 1,
    "вт": 1,
    "среда": 2,
    "среду": 2,
    "среде": 2,
    "ср": 2,
    "четверг": 3,
    "четверга": 3,
    "четвергу": 3,
    "чт": 3,
    "пятница": 4,
    "пятницу": 4,
    "пятнице": 4,
    "пт": 4,
    "суббота": 5,
    "субботу": 5,
    "субботе": 5,
    "сб": 5,
    "воскресенье": 6,
    "воскресенья": 6,
    "воскресенью": 6,
    "вс": 6,
}
_WEEKDAY_NAMES_RU = (
    "понедельник",
    "вторник",
    "среда",
    "четверг",
    "пятница",
    "суббота",
    "воскресенье",
)

_DATE_FORMATS = ("%d.%m.%Y", "%d/%m/%Y", "%d-%m-%Y", "%d.%m.%y")
_TIME_RE = re.compile(r"^(\d{1,2})[:.](\d{2})$")
_HOUR_ONLY_RE = re.compile(r"^(\d{1,2})$")
_HOUR_WORD_RE = re.compile(r"^(\d{1,2})\s*(?:час(?:а|ов)?|ч)\.?$", re.IGNORECASE)
_VAGUE_TIME_WORDS = frozenset({"утром", "вечером", "днём", "днем", "ночью"})
_TIME_PREFIXES = ("в ", "на ")
_RELATIVE_DAYS_RE = re.compile(r"^через\s+(\d+)\s+дн", re.IGNORECASE)

def empty_draft() -> dict[str, Any]:
    return {
        "customer_name": None,
        "service": None,
        "date": None,
        "time": None,
        "doctor_id": None,
        "doctor_name": None,
    }


def get_pending_booking(dialog_context: dict[str, Any] | None) -> dict[str, Any] | None:
    if not dialog_context:
        return None
    draft = dialog_context.get(PENDING_BOOKING_KEY)
    if not isinstance(draft, dict):
        return None
    return draft


def merge_draft(existing: dict[str, Any] | None, **fields: Any) -> dict[str, Any]:
    merged = empty_draft()
    if existing:
        for key in merged:
            if existing.get(key) is not None:
                merged[key] = existing.get(key)
    for key, value in fields.items():
        if key in merged and value is not None:
            stripped = value.strip() if isinstance(value, str) else value
            if stripped != "":
                merged[key] = stripped
    return merged


def _org_tz(tz_name: str) -> ZoneInfo:
    try:
        return ZoneInfo(tz_name or "UTC")
    except Exception:
        return ZoneInfo("UTC")


def _local_today(tz_name: str, *, now: datetime | None = None) -> date:
    ref = now or datetime.now(timezone.utc)
    return ref.astimezone(_org_tz(tz_name)).date()


def _next_weekday_on_or_after(base: date, weekday: int) -> date:
    delta = (weekday - base.weekday()) % 7
    return base + timedelta(days=delta)


def normalize_booking_date(
    raw: str,
    tz_name: str,
    *,
    now: datetime | None = None,
) -> str | None:
    """Parse YYYY-MM-DD, DD.MM.YYYY, or Russian relative dates (завтра, в пятницу)."""
    text = (raw or "").strip()
    if not text:
        return None

    try:
        datetime.strptime(text, "%Y-%m-%d")
        return text
    except ValueError:
        pass

    for fmt in _DATE_FORMATS:
        try:
            return datetime.strptime(text, fmt).date().isoformat()
        except ValueError:
            continue

    lowered = text.lower().strip()
    for prefix in ("в ", "на "):
        if lowered.startswith(prefix):
            lowered = lowered[len(prefix) :].strip()

    today = _local_today(tz_name, now=now)
    if lowered in {"сегодня", "today"}:
        return today.isoformat()
    if lowered in {"завтра", "tomorrow"}:
        return (today + timedelta(days=1)).isoformat()
    if lowered in {"послезавтра", "day after tomorrow"}:
        return (today + timedelta(days=2)).isoformat()

    relative = _RELATIVE_DAYS_RE.match(lowered)
    if relative:
        return (today + timedelta(days=int(relative.group(1)))).isoformat()

    weekday = _WEEKDAY_INDEX.get(lowered)
    if weekday is not None:
        return _next_weekday_on_or_after(today, weekday).isoformat()

    return None


def _format_hour_minute(hour: int, minute: int) -> str | None:
    if hour > 23 or minute > 59:
        return None
    return f"{hour:02d}:{minute:02d}"


def _strip_time_prefixes(text: str) -> str:
    lowered = text.lower().strip()
    for prefix in _TIME_PREFIXES:
        if lowered.startswith(prefix):
            return text.strip()[len(prefix) :].strip()
    return text.strip()


def is_vague_booking_time(raw: str) -> bool:
    """True for расплывчатые фразы вроде «утром», «вечером»."""
    text = (raw or "").strip().lower()
    if not text:
        return False
    text = _strip_time_prefixes(text).lower()
    return text in _VAGUE_TIME_WORDS


def normalize_booking_time(raw: str) -> str | None:
    """Parse HH:MM, bare hour (15), «в 15», «15 часов», optional leading «в»/«на»."""
    text = (raw or "").strip()
    if not text:
        return None
    if is_vague_booking_time(text):
        return None
    text = _strip_time_prefixes(text)

    match = _TIME_RE.match(text)
    if match:
        return _format_hour_minute(int(match.group(1)), int(match.group(2)))

    hour_word = _HOUR_WORD_RE.match(text)
    if hour_word:
        return _format_hour_minute(int(hour_word.group(1)), 0)

    hour_only = _HOUR_ONLY_RE.match(text)
    if hour_only:
        return _format_hour_minute(int(hour_only.group(1)), 0)

    return None


def build_booking_clock_context(tz_name: str, *, now: datetime | None = None) -> str:
    """Current local date hint for the LLM system prompt."""
    tz = _org_tz(tz_name)
    local_now = (now or datetime.now(timezone.utc)).astimezone(tz)
    today = local_now.date()
    tomorrow = today + timedelta(days=1)
    weekday = _WEEKDAY_NAMES_RU[today.weekday()]
    return (
        f"Сейчас в часовом поясе организации ({tz_name}): {today.isoformat()} ({weekday}), "
        f"завтра — {tomorrow.isoformat()}. "
        "Клиент может писать «завтра», «в субботу», «24.06» — понимай сам; "
        "в инструменты можно передать и так, и YYYY-MM-DD. "
        "Время — «в 15», «15 часов» или 15:00."
    )


def validate_draft_for_card(draft: dict[str, Any]) -> list[str]:
    errors: list[str] = []
    name = (draft.get("customer_name") or "").strip()
    service = (draft.get("service") or "").strip()
    date = (draft.get("date") or "").strip()
    time = (draft.get("time") or "").strip()
    if len(name) < 2:
        errors.append("customer_name (минимум 2 символа)")
    if len(service) < 2:
        errors.append("service (название услуги)")
    if not date:
        errors.append("date")
    if not time:
        errors.append("time (HH:MM)")
    else:
        try:
            datetime.strptime(f"{date} {time}", "%Y-%m-%d %H:%M")
        except ValueError:
            errors.append("date/time (некорректный формат)")
    return errors


def validate_draft_schedule(
    draft: dict[str, Any],
    tz_name: str,
    *,
    now: datetime | None = None,
) -> list[str]:
    """Format + schedule window checks (no past dates, not too far in future)."""
    errors = validate_draft_for_card(draft)
    if errors:
        return errors
    try:
        _, utc_dt = parse_scheduled_local(draft, tz_name)
    except (KeyError, ValueError, TypeError):
        return ["date/time (некорректный формат)"]
    ref = now or datetime.now(timezone.utc)
    if utc_dt.tzinfo is None:
        utc_dt = utc_dt.replace(tzinfo=timezone.utc)
    if utc_dt <= ref:
        errors.append(PAST_BOOKING_MESSAGE)
    elif utc_dt > ref + timedelta(days=FUTURE_BOOKING_LIMIT_DAYS):
        errors.append(
            f"date/time (не более {FUTURE_BOOKING_LIMIT_DAYS} дней вперёд; уточни дату у клиента)"
        )
    return errors


def parse_scheduled_local(draft: dict[str, Any], tz_name: str) -> tuple[datetime, datetime]:
    """Return (local_dt, utc_dt)."""
    try:
        tz = ZoneInfo(tz_name or "UTC")
    except Exception:
        tz = ZoneInfo("UTC")
    local_dt = datetime.strptime(
        f"{draft['date'].strip()} {draft['time'].strip()}",
        "%Y-%m-%d %H:%M",
    ).replace(tzinfo=tz)
    return local_dt, local_dt.astimezone(ZoneInfo("UTC"))


def render_appointment_card(draft: dict[str, Any], *, tz_name: str) -> str:
    """Human-readable card shown to the client before confirmation."""
    local_dt, _ = parse_scheduled_local(draft, tz_name)
    when = local_dt.strftime("%d.%m.%Y %H:%M")
    lines = [
        "📋 Проверьте запись:",
        "",
        f"👤 Имя: {draft['customer_name']}",
        f"🦷 Услуга: {draft['service']}",
        f"📅 Дата и время: {when} ({tz_name})",
    ]
    doctor_name = (draft.get("doctor_name") or "").strip()
    if doctor_name:
        lines.append(f"👨‍⚕️ Специалист: {doctor_name}")
    lines.extend(
        [
            "",
            "Всё верно? Ответьте «Да» для подтверждения или напишите, что нужно изменить.",
        ]
    )
    return "\n".join(lines)
