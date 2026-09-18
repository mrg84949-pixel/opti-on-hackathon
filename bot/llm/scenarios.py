from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

_DOCTOR_KWARGS = {
    "doctor_id": "doc-1",
    "doctor_name": "Специалист 1",
}


@dataclass
class ScenarioAction:
    tool: str
    kwargs: dict[str, Any] = field(default_factory=dict)


@dataclass
class ScenarioTurn:
    user_text: str
    actions: list[ScenarioAction]


@dataclass
class BotScenario:
    name: str
    turns: list[ScenarioTurn]


BOOKING_HAPPY_PATH = BotScenario(
    name="booking_happy_path",
    turns=[
        ScenarioTurn(
            user_text="Хочу записаться",
            actions=[
                ScenarioAction(
                    tool="show_appointment_card",
                    kwargs={
                        "customer_name": "Alice",
                        "service": "Консультация",
                        "date": "2027-06-01",
                        "time": "10:00",
                        **_DOCTOR_KWARGS,
                    },
                ),
            ],
        ),
        ScenarioTurn(
            user_text="Да",
            actions=[ScenarioAction(tool="confirm_appointment_booking")],
        ),
    ],
)

CONFIRM_WITHOUT_DRAFT = BotScenario(
    name="confirm_without_draft",
    turns=[
        ScenarioTurn(
            user_text="Да",
            actions=[ScenarioAction(tool="confirm_appointment_booking")],
        ),
    ],
)

GET_SERVICES = BotScenario(
    name="get_services",
    turns=[
        ScenarioTurn(
            user_text="Какие услуги?",
            actions=[ScenarioAction(tool="get_services_info")],
        ),
    ],
)

BOOKING_ZAVTRA = BotScenario(
    name="booking_zavtra",
    turns=[
        ScenarioTurn(
            user_text="Запишите на завтра в 15",
            actions=[
                ScenarioAction(
                    tool="show_appointment_card",
                    kwargs={
                        "customer_name": "Alice",
                        "service": "Консультация",
                        "date": "завтра",
                        "time": "в 15",
                        **_DOCTOR_KWARGS,
                    },
                ),
            ],
        ),
    ],
)

BOOKING_PAST_REJECTED = BotScenario(
    name="booking_past_rejected",
    turns=[
        ScenarioTurn(
            user_text="На прошлую дату",
            actions=[
                ScenarioAction(
                    tool="show_appointment_card",
                    kwargs={
                        "customer_name": "Alice",
                        "service": "Консультация",
                        "date": "2020-06-01",
                        "time": "10:00",
                        **_DOCTOR_KWARGS,
                    },
                ),
            ],
        ),
    ],
)

BOOKING_SLOT_REJECTED = BotScenario(
    name="booking_slot_rejected",
    turns=[
        ScenarioTurn(
            user_text="На три ночи",
            actions=[
                ScenarioAction(
                    tool="show_appointment_card",
                    kwargs={
                        "customer_name": "Alice",
                        "service": "Консультация",
                        "date": "2027-06-15",
                        "time": "03:00",
                        **_DOCTOR_KWARGS,
                    },
                ),
            ],
        ),
    ],
)

_BOOKING_TURNS = BOOKING_HAPPY_PATH.turns

BOOKING_THEN_CANCEL = BotScenario(
    name="booking_then_cancel",
    turns=[
        *_BOOKING_TURNS,
        ScenarioTurn(
            user_text="Отмените запись",
            actions=[
                ScenarioAction(
                    tool="cancel_appointment",
                    kwargs={"reason": "Планы изменились"},
                ),
            ],
        ),
    ],
)

BOOKING_THEN_RESCHEDULE = BotScenario(
    name="booking_then_reschedule",
    turns=[
        *_BOOKING_TURNS,
        ScenarioTurn(
            user_text="Перенесите на другое время",
            actions=[
                ScenarioAction(
                    tool="edit_appointment",
                    kwargs={"date": "2027-06-02", "time": "15:00"},
                ),
            ],
        ),
    ],
)
