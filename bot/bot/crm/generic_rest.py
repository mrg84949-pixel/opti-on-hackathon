from __future__ import annotations

from datetime import datetime
from typing import Any

import httpx

from bot.crm.amocrm import (
    DEFAULT_CANCELLED_STATUS_VALUE,
    _extract_appointment_rows,
    _extract_staff_rows,
    _parse_appointment_snapshot,
    _parse_staff_row,
    _resolve_path,
)
from bot.crm.base import BookingResult, CrmAppointmentSnapshot, Slot, StaffMember
from bot.crm.demo_slots import build_demo_slots
from bot.crm.demo_staff import DEFAULT_DEMO_STAFF

DEFAULT_SLOTS_PATH = "/api/slots"
DEFAULT_BOOKING_PATH = "/api/appointments"
DEFAULT_STAFF_PATH = "/api/staff"
DEFAULT_LIST_APPOINTMENTS_PATH = "/api/appointments"


class GenericRestProvider:
    """
    REST-адаптер для произвольного CRM API с JSON-контрактом, совместимым с amoCRM MVP.
    Без base_url/token работает в demo-режиме.
    """

    def __init__(
        self,
        *,
        base_url: str | None,
        token: str | None,
        config: dict[str, Any] | None = None,
    ):
        self.base_url = (base_url or "").rstrip("/")
        self.token = token or ""
        self.demo_mode = not (self.base_url and self.token)
        cfg = config or {}
        self.slots_path = _resolve_path(cfg, "slots_path", DEFAULT_SLOTS_PATH)
        self.booking_path = _resolve_path(cfg, "booking_path", DEFAULT_BOOKING_PATH)
        self.staff_path = _resolve_path(cfg, "staff_path", DEFAULT_STAFF_PATH)
        self.list_appointments_path = _resolve_path(
            cfg, "list_appointments_path", DEFAULT_LIST_APPOINTMENTS_PATH
        )
        self.cancelled_status_value = (
            str(cfg.get("cancelled_status_value") or DEFAULT_CANCELLED_STATUS_VALUE).strip().lower()
        )

    async def get_available_slots(
        self, doctor_id: str, date_iso: str, *, tz_name: str | None = None
    ) -> list[Slot]:
        if self.demo_mode:
            return build_demo_slots(date_iso, tz_name=tz_name or "UTC")

        url = f"{self.base_url}{self.slots_path}"
        headers = {"Authorization": f"Bearer {self.token}"}
        params = {"doctor_id": doctor_id, "date": date_iso}
        async with httpx.AsyncClient(timeout=20) as client:
            response = await client.get(url, headers=headers, params=params)
            response.raise_for_status()
            payload = response.json()

        slots: list[Slot] = []
        for item in payload.get("slots", []):
            start = datetime.fromisoformat(item["start"])
            end = datetime.fromisoformat(item["end"])
            slots.append(Slot(start=start, end=end))
        return slots

    async def book_appointment(
        self,
        doctor_id: str,
        start_iso: str,
        customer_name: str | None = None,
        customer_phone: str | None = None,
    ) -> BookingResult:
        if self.demo_mode:
            fake_id = f"demo-{doctor_id}-{start_iso.replace(':', '').replace('-', '').replace('T', '')}"
            return BookingResult(crm_appointment_id=fake_id, start=datetime.fromisoformat(start_iso))

        url = f"{self.base_url}{self.booking_path}"
        headers = {"Authorization": f"Bearer {self.token}"}
        body = {
            "doctor_id": doctor_id,
            "start": start_iso,
            "customer_name": customer_name,
            "customer_phone": customer_phone,
        }
        async with httpx.AsyncClient(timeout=20) as client:
            response = await client.post(url, headers=headers, json=body)
            response.raise_for_status()
            payload = response.json()

        crm_id = payload.get("id") or payload.get("appointment_id")
        if not crm_id:
            raise ValueError("generic REST booking response does not contain appointment id")
        crm_id = str(crm_id)
        return BookingResult(crm_appointment_id=crm_id, start=datetime.fromisoformat(start_iso))

    async def list_staff(self) -> list[StaffMember]:
        if self.demo_mode:
            return [_parse_staff_row(row) for row in DEFAULT_DEMO_STAFF]

        url = f"{self.base_url}{self.staff_path}"
        headers = {"Authorization": f"Bearer {self.token}"}
        async with httpx.AsyncClient(timeout=20) as client:
            response = await client.get(url, headers=headers)
            response.raise_for_status()
            payload = response.json()

        rows = _extract_staff_rows(payload)
        return [_parse_staff_row(row) for row in rows]

    async def list_recent_appointments(self, *, since_iso: str) -> list[CrmAppointmentSnapshot]:
        if self.demo_mode:
            return []

        url = f"{self.base_url}{self.list_appointments_path}"
        headers = {"Authorization": f"Bearer {self.token}"}
        params = {"updated_since": since_iso}
        async with httpx.AsyncClient(timeout=20) as client:
            response = await client.get(url, headers=headers, params=params)
            response.raise_for_status()
            payload = response.json()

        snapshots: list[CrmAppointmentSnapshot] = []
        for row in _extract_appointment_rows(payload):
            parsed = _parse_appointment_snapshot(row)
            if parsed is not None:
                snapshots.append(parsed)
        return snapshots


def provider_from_org(
    base_url: str | None,
    token: str | None,
    *,
    config: dict[str, Any] | None = None,
) -> GenericRestProvider:
    return GenericRestProvider(base_url=base_url, token=token, config=config)
