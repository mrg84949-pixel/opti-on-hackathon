from __future__ import annotations

import logging
from datetime import datetime, timedelta, timezone
from typing import Any

import httpx

from bot.crm.amocrm import _parse_staff_row
from bot.crm.base import BookingResult, CrmAppointmentSnapshot, Slot, StaffMember
from bot.crm.demo_slots import build_demo_slots
from bot.crm.demo_staff import DEFAULT_DEMO_STAFF

logger = logging.getLogger(__name__)

DEFAULT_BASE_URL = "https://api.yclients.com/api/v1"
ACCEPT_HEADER = "application/vnd.yclients.v2+json"
DEFAULT_EMAIL = "noreply@optibot.local"


class YClientsProvider:
    """
    YClients online booking adapter (public book_* endpoints, partner token).
    Without partner token + company_id runs in demo mode.
    """

    def __init__(
        self,
        *,
        partner_token: str | None,
        company_id: str | None,
        user_token: str | None = None,
        config: dict[str, Any] | None = None,
    ):
        self.partner_token = (partner_token or "").strip()
        self.company_id = (company_id or "").strip()
        self.user_token = (user_token or "").strip()
        cfg = config or {}
        self.default_service_id = str(cfg.get("default_service_id") or "").strip()
        self.cancelled_status_value = (
            str(cfg.get("cancelled_status_value") or "cancelled").strip().lower()
        )
        self.demo_mode = not (self.partner_token and self.company_id)

    def _headers(self, *, require_user: bool = False) -> dict[str, str]:
        auth = f"Bearer {self.partner_token}"
        if self.user_token:
            auth = f"{auth}, User {self.user_token}"
        elif require_user:
            raise ValueError("YClients user token is required for this operation")
        return {
            "Authorization": auth,
            "Accept": ACCEPT_HEADER,
        }

    async def get_available_slots(
        self, doctor_id: str, date_iso: str, *, tz_name: str | None = None
    ) -> list[Slot]:
        if self.demo_mode:
            return build_demo_slots(date_iso, tz_name=tz_name or "UTC")

        url = f"{DEFAULT_BASE_URL}/book_times/{self.company_id}/{doctor_id}/{date_iso}"
        params: dict[str, Any] = {}
        if self.default_service_id.isdigit():
            params["service_ids[]"] = int(self.default_service_id)

        async with httpx.AsyncClient(timeout=20) as client:
            response = await client.get(url, headers=self._headers(), params=params or None)
            response.raise_for_status()
            payload = response.json()

        return _parse_book_times(payload)

    async def book_appointment(
        self,
        doctor_id: str,
        start_iso: str,
        customer_name: str | None = None,
        customer_phone: str | None = None,
    ) -> BookingResult:
        start_dt = datetime.fromisoformat(start_iso.replace("Z", "+00:00"))
        if self.demo_mode:
            fake_id = f"demo-{doctor_id}-{start_iso.replace(':', '').replace('-', '').replace('T', '')}"
            return BookingResult(crm_appointment_id=fake_id, start=start_dt)

        phone = _normalize_phone(customer_phone)
        fullname = (customer_name or "Client").strip() or "Client"
        appointment_item: dict[str, Any] = {
            "id": 1,
            "staff_id": int(doctor_id) if str(doctor_id).isdigit() else doctor_id,
            "datetime": _to_yclients_datetime(start_dt),
        }
        if self.default_service_id.isdigit():
            appointment_item["services"] = [int(self.default_service_id)]

        body = {
            "phone": phone,
            "fullname": fullname,
            "email": DEFAULT_EMAIL,
            "appointments": [appointment_item],
        }
        url = f"{DEFAULT_BASE_URL}/book_record/{self.company_id}"
        async with httpx.AsyncClient(timeout=20) as client:
            response = await client.post(url, headers=self._headers(), json=body)
            response.raise_for_status()
            payload = response.json()

        crm_id = _parse_book_record_id(payload)
        if not crm_id:
            raise ValueError("YClients booking response does not contain record_id")
        return BookingResult(crm_appointment_id=crm_id, start=start_dt)

    async def list_staff(self) -> list[StaffMember]:
        if self.demo_mode:
            return [_parse_staff_row(row) for row in DEFAULT_DEMO_STAFF]

        url = f"{DEFAULT_BASE_URL}/book_staff/{self.company_id}"
        params: dict[str, Any] = {}
        if self.default_service_id.isdigit():
            params["service_ids[]"] = int(self.default_service_id)

        async with httpx.AsyncClient(timeout=20) as client:
            response = await client.get(url, headers=self._headers(), params=params or None)
            response.raise_for_status()
            payload = response.json()

        return _parse_book_staff(payload)

    async def list_recent_appointments(self, *, since_iso: str) -> list[CrmAppointmentSnapshot]:
        if self.demo_mode:
            return []
        if not self.user_token:
            logger.debug(
                "YClients list_recent_appointments skipped (user token required); since=%s",
                since_iso[:32],
            )
            return []

        changed_after = _since_iso_to_date(since_iso)
        snapshots: list[CrmAppointmentSnapshot] = []
        page = 1
        max_pages = 2
        page_size = 100

        async with httpx.AsyncClient(timeout=20) as client:
            while page <= max_pages:
                url = f"{DEFAULT_BASE_URL}/records/{self.company_id}"
                params = {
                    "changed_after": changed_after,
                    "with_deleted": 1,
                    "page": page,
                    "count": page_size,
                }
                response = await client.get(url, headers=self._headers(require_user=True), params=params)
                response.raise_for_status()
                payload = response.json()
                rows = _extract_data_list(payload)
                if not rows:
                    break
                for row in rows:
                    if not isinstance(row, dict):
                        continue
                    if not row.get("deleted"):
                        continue
                    record_id = row.get("id")
                    if record_id is None:
                        continue
                    snapshots.append(
                        CrmAppointmentSnapshot(
                            crm_appointment_id=str(record_id),
                            status=self.cancelled_status_value,
                        )
                    )
                if len(rows) < page_size:
                    break
                page += 1

        return snapshots


def provider_from_org(
    partner_token: str | None,
    company_id: str | None,
    *,
    user_token: str | None = None,
    config: dict[str, Any] | None = None,
) -> YClientsProvider:
    return YClientsProvider(
        partner_token=partner_token,
        company_id=company_id,
        user_token=user_token,
        config=config,
    )


def _since_iso_to_date(since_iso: str) -> str:
    try:
        parsed = datetime.fromisoformat(since_iso.replace("Z", "+00:00"))
    except ValueError:
        return since_iso[:10]
    return parsed.date().isoformat()


def _normalize_phone(raw: str | None) -> str:
    phone = (raw or "79000000000").strip()
    for prefix in ("tg:", "wa:", "+"):
        if phone.startswith(prefix):
            phone = phone[len(prefix) :]
    digits = "".join(ch for ch in phone if ch.isdigit())
    return digits or "79000000000"


def _to_yclients_datetime(dt: datetime) -> int:
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return int(dt.timestamp())


def _parse_book_staff(payload: Any) -> list[StaffMember]:
    rows = _extract_data_list(payload)
    staff: list[StaffMember] = []
    for row in rows:
        if not isinstance(row, dict):
            continue
        staff_id = str(row.get("id") or "").strip()
        name = str(row.get("name") or staff_id).strip()
        if not staff_id and not name:
            continue
        bookable = row.get("bookable", True)
        staff.append(
            StaffMember(
                id=staff_id or name,
                name=name or staff_id,
                work_start="08:00",
                work_end="20:00",
                active=bool(bookable),
            )
        )
    return staff


def _parse_book_times(payload: Any) -> list[Slot]:
    data = payload.get("data") if isinstance(payload, dict) else None
    seances: list[Any] = []
    if isinstance(data, dict):
        seances = data.get("seances") or []
    elif isinstance(data, list):
        seances = data

    slots: list[Slot] = []
    for item in seances:
        if not isinstance(item, dict):
            continue
        start = _parse_seance_start(item)
        if start is None:
            continue
        length_sec = int(item.get("seance_length") or 1800)
        slots.append(Slot(start=start, end=start + timedelta(seconds=length_sec)))
    return slots


def _parse_seance_start(item: dict[str, Any]) -> datetime | None:
    raw_dt = item.get("datetime")
    if isinstance(raw_dt, (int, float)):
        return datetime.fromtimestamp(raw_dt, tz=timezone.utc).replace(tzinfo=None)
    if isinstance(raw_dt, str) and raw_dt.strip():
        try:
            return datetime.fromisoformat(raw_dt.replace("Z", "+00:00")).replace(tzinfo=None)
        except ValueError:
            pass
    time_str = str(item.get("time") or "").strip()
    if not time_str:
        return None
    try:
        hour, minute = (int(part) for part in time_str.split(":", 1))
        today = datetime.now().replace(hour=hour, minute=minute, second=0, microsecond=0)
        return today
    except (ValueError, TypeError):
        return None


def _parse_book_record_id(payload: Any) -> str | None:
    rows = _extract_data_list(payload)
    for row in rows:
        if not isinstance(row, dict):
            continue
        record_id = row.get("record_id") or row.get("id")
        if record_id is not None:
            return str(record_id)
    if isinstance(payload, dict):
        meta = payload.get("meta")
        if isinstance(meta, dict) and meta.get("record_id") is not None:
            return str(meta["record_id"])
    return None


def _extract_data_list(payload: Any) -> list[Any]:
    if isinstance(payload, list):
        return payload
    if not isinstance(payload, dict):
        return []
    data = payload.get("data")
    if isinstance(data, list):
        return data
    if isinstance(data, dict):
        for key in ("seances", "items", "staff"):
            value = data.get(key)
            if isinstance(value, list):
                return value
    return []
