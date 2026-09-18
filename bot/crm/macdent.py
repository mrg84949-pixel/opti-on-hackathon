from __future__ import annotations

import logging
from datetime import datetime, timedelta
from typing import Any

import httpx

from bot.crm.base import BookingResult, CrmAppointmentSnapshot, Slot, StaffMember
from bot.crm.demo_slots import build_demo_slots
from bot.crm.demo_staff import DEFAULT_DEMO_STAFF

logger = logging.getLogger(__name__)

# Confirmed live against a real MacDent account (see docs/macdent-api-reference.md).
# Paths are NOT configurable via org.crm_config — unlike generic_rest, MacDent's
# API shape is fixed, so there is nothing safe to override per-org here.
DEFAULT_BASE_URL = "https://api-developer.macdent.kz"
DOCTOR_FIND_PATH = "/doctor/find"
DOCTOR_FREE_TIME_PATH = "/doctor/get_free_time"
# zapis_read confirmed granted 2026-08-21; response envelope + list key
# ("zapisi", not "zapis") confirmed live. STILL UNCONFIRMED: whether
# "updated_since" below is a real filter param zapis.find recognizes. This
# account has 18,911 total zapis records across 190 pages (count/maxPage seen
# live, unscoped call) — if updated_since is silently ignored, every call
# here pages through the ENTIRE history instead of a real delta. DO NOT wire
# this into a live org's sync job before verifying the filter actually
# narrows `count` on a real call — see "Что нужно доуточнить" in the
# reference doc.
ZAPIS_FIND_PATH = "/zapis/find"
# Confirmed live 2026-08-29 (real create+immediate-remove round trip, non-
# working hours, test patient/slot — see docs/macdent-api-reference.md
# "zapis.add"). Required fields (validation order): doctor, patient (an id —
# NOT patient_name/patient_phone like appointment.send), start, end, filial.
# patient.add's success shape confirmed the same day: {"patient": {"id", ...},
# "response": 1} — same envelope pattern as appointment.send.
ZAPIS_ADD_PATH = "/zapis/add"
ZAPIS_REMOVE_PATH = "/zapis/remove"
PATIENT_ADD_PATH = "/patient/add"

# MacDent has no appointment-duration concept we can read back (doctor.get_free_time
# returns whole open blocks, not per-service slot length) — same gap other
# providers paper over with a fixed demo duration (see demo_slots.DEMO_SLOT_DURATION_MIN).
DEFAULT_APPOINTMENT_DURATION_MINUTES = 30

# MacDent's own date format, confirmed live on doctor.get_free_time (e.g.
# "12.08.2026 13:00:00") — NOT ISO 8601, datetime.fromisoformat() cannot read it.
_MACDENT_DT_FORMAT = "%d.%m.%Y %H:%M:%S"


class MacDentError(Exception):
    """MacDent-side failure: response=0 in an HTTP-200 body, or a redacted HTTP error.

    Never built from a raw httpx exception's str() — those embed the full
    request URL, and MacDent auth is a query-string access_token, not a
    header, so that string would contain the live credential.
    """


def _parse_macdent_dt(raw: str) -> datetime:
    return datetime.strptime(raw.strip(), _MACDENT_DT_FORMAT)


def _format_macdent_dt(dt: datetime) -> str:
    return dt.strftime(_MACDENT_DT_FORMAT)


def _normalize_macdent_phone(raw: str | None) -> str:
    """channel_phone() gives us things like 'tg:123456789' — MacDent expects
    an actual phone number. Strip channel prefixes, keep digits only."""
    phone = (raw or "").strip()
    for prefix in ("tg:", "wa:", "+"):
        if phone.startswith(prefix):
            phone = phone[len(prefix) :]
    digits = "".join(ch for ch in phone if ch.isdigit())
    return digits or "70000000000"


class MacDentProvider:
    """
    MacDent (стоматологическая CRM) adapter.

    book_appointment() creates a real MacDent zapis (calendar booking) — see
    its own docstring for the two-step patient.add + zapis.add flow, and
    docs/macdent-api-reference.md for the live verification behind it
    (2026-08-29).
    """

    def __init__(
        self,
        *,
        access_token: str | None,
        base_url: str | None = None,
        config: dict[str, Any] | None = None,
    ):
        self.access_token = (access_token or "").strip()
        self.base_url = (base_url or DEFAULT_BASE_URL).rstrip("/")
        self.demo_mode = not self.access_token
        cfg = config or {}
        self.cancelled_status_value = str(
            cfg.get("cancelled_status_value") or "declined"
        ).strip().lower()
        # Required for real bookings — zapis.add rejects requests without it.
        self.default_filial = str(cfg.get("filial") or "").strip()

    async def _get(self, path: str, params: dict[str, Any] | None = None) -> dict[str, Any]:
        query = dict(params or {})
        query["access_token"] = self.access_token
        url = f"{self.base_url}{path}"
        try:
            async with httpx.AsyncClient(timeout=20) as client:
                response = await client.get(url, params=query)
                response.raise_for_status()
                payload = response.json()
        except httpx.HTTPStatusError as exc:
            raise MacDentError(f"MacDent HTTP {exc.response.status_code} on {path}") from None
        except httpx.RequestError as exc:
            raise MacDentError(f"MacDent request failed on {path}: {type(exc).__name__}") from None

        if not isinstance(payload, dict) or payload.get("response") != 1:
            error = payload.get("error") if isinstance(payload, dict) else None
            raise MacDentError(str(error or f"MacDent request failed on {path} (response != 1)"))
        return payload

    async def _post(self, path: str, data: dict[str, Any]) -> dict[str, Any]:
        body = dict(data)
        body["access_token"] = self.access_token
        url = f"{self.base_url}{path}"
        try:
            async with httpx.AsyncClient(timeout=20) as client:
                response = await client.post(url, data=body)
                response.raise_for_status()
                payload = response.json()
        except httpx.HTTPStatusError as exc:
            raise MacDentError(f"MacDent HTTP {exc.response.status_code} on {path}") from None
        except httpx.RequestError as exc:
            raise MacDentError(f"MacDent request failed on {path}: {type(exc).__name__}") from None

        if not isinstance(payload, dict) or payload.get("response") != 1:
            error = payload.get("error") if isinstance(payload, dict) else None
            raise MacDentError(str(error or f"MacDent request failed on {path} (response != 1)"))
        return payload

    async def get_available_slots(
        self, doctor_id: str, date_iso: str, *, tz_name: str | None = None
    ) -> list[Slot]:
        if self.demo_mode:
            return build_demo_slots(date_iso, tz_name=tz_name or "UTC")

        try:
            payload = await self._get(DOCTOR_FREE_TIME_PATH, {"doctor": doctor_id})
        except MacDentError as exc:
            logger.warning("MacDent get_free_time failed: %s", exc)
            return []

        target_date = date_iso.strip()
        slots: list[Slot] = []
        for row in payload.get("schedules") or []:
            if not isinstance(row, dict):
                continue
            raw_from, raw_to = row.get("from"), row.get("to")
            if not raw_from or not raw_to:
                continue
            try:
                start = _parse_macdent_dt(str(raw_from))
                end = _parse_macdent_dt(str(raw_to))
            except ValueError:
                continue
            if start.strftime("%Y-%m-%d") != target_date:
                continue
            slots.append(Slot(start=start, end=end))
        return slots

    async def _create_patient(self, name: str, phone: str) -> str:
        """Always creates a NEW patient card — deliberately no patient.find
        search-by-phone first. See book_appointment docstring for why."""
        payload = await self._post(PATIENT_ADD_PATH, {"name": name, "phone": phone})
        patient = payload.get("patient")
        patient_id = patient.get("id") if isinstance(patient, dict) else None
        if not patient_id:
            raise MacDentError("MacDent patient.add response missing patient.id")
        return str(patient_id)

    async def book_appointment(
        self,
        doctor_id: str,
        start_iso: str,
        customer_name: str | None = None,
        customer_phone: str | None = None,
    ) -> BookingResult:
        """Creates a real MacDent zapis (calendar booking) — confirmed live
        2026-08-29 via a real create + immediate zapis.remove round trip in
        the clinic's non-working hours (snapshots in
        docs/stability/macdent-write-test-snapshots/, details in
        docs/macdent-api-reference.md "zapis.add").

        Two-step flow, both required by MacDent's own API shape:
        1. patient.add — creates a NEW patient card every time. Deliberately
           does NOT search patient.find by phone first: avoids ambiguity on
           0/2+ matches and an extra PII-adjacent query on the riskiest write
           path in this integration. Trade-off: a returning bot customer
           accumulates duplicate patient cards in MacDent over time — an
           accepted cost for the pilot, revisit only if the clinic reports it
           as an actual operational problem (see PATIENT_ADD_PATH comment).
        2. zapis.add — the real calendar write. Requires org.crm_config
           "filial" to be set (see CRM_INTEGRATION_META in registry.py); we
           refuse to guess a filial id, since the one used during testing
           (200) was specific to that test doctor/account, not a safe default
           for any clinic.

        No automatic compensation here if patient.add succeeds but zapis.add
        then fails: that leaves an unused, harmless patient card (not a
        phantom calendar slot) — logged via MacDentError, not auto-cleaned.
        If zapis.add itself fails partway (network error after MacDent
        applied it), the caller (bot/llm/tools.py) must treat a raised
        MacDentError as "unknown state" and surface it for manual check —
        same as every other provider here.
        """
        start_dt = datetime.fromisoformat(start_iso)
        if self.demo_mode:
            fake_id = f"demo-{doctor_id}-{start_iso.replace(':', '').replace('-', '').replace('T', '')}"
            return BookingResult(crm_appointment_id=fake_id, start=start_dt)

        if not self.default_filial:
            raise MacDentError(
                "MacDent booking requires a filial id configured for this org "
                "(Бот → Интеграции → MacDent → Филиал) — zapis.add rejects requests without one."
            )

        name = (customer_name or "").strip() or "Клиент Optibot"
        phone = _normalize_macdent_phone(customer_phone)
        patient_id = await self._create_patient(name, phone)

        end_dt = start_dt + timedelta(minutes=DEFAULT_APPOINTMENT_DURATION_MINUTES)
        body: dict[str, Any] = {
            "doctor": doctor_id,
            "patient": patient_id,
            "start": _format_macdent_dt(start_dt),
            "end": _format_macdent_dt(end_dt),
            "filial": self.default_filial,
            "comment": "Запись через Optibot",
        }
        payload = await self._post(ZAPIS_ADD_PATH, body)
        zapis = payload.get("zapis")
        crm_id = zapis.get("id") if isinstance(zapis, dict) else None
        if not crm_id:
            raise MacDentError("MacDent zapis.add response missing zapis.id")
        return BookingResult(crm_appointment_id=str(crm_id), start=start_dt)

    async def list_staff(self) -> list[StaffMember]:
        if self.demo_mode:
            return [
                StaffMember(
                    id=str(row["id"]),
                    name=str(row["name"]),
                    work_start=str(row.get("work_start", "08:00")),
                    work_end=str(row.get("work_end", "20:00")),
                    active=bool(row.get("active", True)),
                )
                for row in DEFAULT_DEMO_STAFF
            ]

        try:
            payload = await self._get(DOCTOR_FIND_PATH)
        except MacDentError as exc:
            logger.warning("MacDent doctor.find failed: %s", exc)
            return []

        staff: list[StaffMember] = []
        for row in payload.get("doctors") or []:
            if not isinstance(row, dict):
                continue
            doctor_id = str(row.get("id") or "").strip()
            name = str(row.get("name") or "").strip()
            if not doctor_id or not name:
                continue
            # MacDent doctor rows don't include work_start/work_end (they have
            # specialnosti/filials instead) — real availability comes from
            # get_available_slots, these are display-only placeholders.
            staff.append(StaffMember(id=doctor_id, name=name, work_start="08:00", work_end="20:00"))
        return staff

    async def list_recent_appointments(self, *, since_iso: str) -> list[CrmAppointmentSnapshot]:
        if self.demo_mode:
            return []

        try:
            payload = await self._get(ZAPIS_FIND_PATH, {"updated_since": since_iso})
        except MacDentError as exc:
            text = str(exc)
            if "appointments_read" in text.lower() or "прав" in text.lower():
                logger.info("MacDent zapis.find skipped (missing permission): %s", exc)
            else:
                logger.warning("MacDent zapis.find failed: %s", exc)
            return []

        snapshots: list[CrmAppointmentSnapshot] = []
        for row in payload.get("zapisi") or []:
            if not isinstance(row, dict):
                continue
            crm_id = row.get("id")
            status = row.get("status")
            if crm_id is None or status is None:
                continue
            snapshots.append(
                CrmAppointmentSnapshot(crm_appointment_id=str(crm_id), status=str(status).strip().lower())
            )
        return snapshots


def provider_from_org(
    access_token: str | None,
    *,
    base_url: str | None = None,
    config: dict[str, Any] | None = None,
) -> MacDentProvider:
    return MacDentProvider(access_token=access_token, base_url=base_url, config=config)
