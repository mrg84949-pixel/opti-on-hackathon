"""Shared demo CRM staff catalog for timeline and CRM adapters."""

from __future__ import annotations

from typing import Any

DEFAULT_DEMO_STAFF: list[dict[str, Any]] = [
    {
        "id": "doc-1",
        "name": "Специалист 1",
        "work_start": "08:00",
        "work_end": "20:00",
        "active": True,
    },
    {
        "id": "doc-2",
        "name": "Специалист 2",
        "work_start": "09:00",
        "work_end": "18:00",
        "active": True,
    },
]
