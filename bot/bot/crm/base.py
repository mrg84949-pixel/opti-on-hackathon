from dataclasses import dataclass
from datetime import datetime
from typing import Protocol


@dataclass
class Slot:
    start: datetime
    end: datetime


@dataclass
class BookingResult:
    crm_appointment_id: str
    start: datetime
    status: str = "booked"


@dataclass
class StaffMember:
    id: str
    name: str
    work_start: str = "08:00"
    work_end: str = "20:00"
    active: bool = True


@dataclass
class CrmAppointmentSnapshot:
    crm_appointment_id: str
    status: str


class CRMProvider(Protocol):
    async def get_available_slots(
        self, doctor_id: str, date_iso: str, *, tz_name: str | None = None
    ) -> list[Slot]:
        """Возвращает доступные окна врача на дату YYYY-MM-DD."""

    async def book_appointment(
        self,
        doctor_id: str,
        start_iso: str,
        customer_name: str | None = None,
        customer_phone: str | None = None,
    ) -> BookingResult:
        """Создает запись в CRM и возвращает crm appointment id."""

    async def list_staff(self) -> list[StaffMember]:
        """Каталог специалистов и рабочих окон для админки и таймлайна."""

    async def list_recent_appointments(self, *, since_iso: str) -> list[CrmAppointmentSnapshot]:
        """Recent appointments changed since UTC ISO timestamp."""
