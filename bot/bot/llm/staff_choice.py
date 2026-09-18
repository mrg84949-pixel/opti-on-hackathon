"""Human-friendly staff selection helpers (no CRM ids shown to clients)."""

from __future__ import annotations

import re
from typing import Any

AWAITING_STAFF_CHOICE_KEY = "awaiting_staff_choice"

_ORDINAL_WORDS: dict[str, int] = {
    "первый": 1,
    "первая": 1,
    "первого": 1,
    "первую": 1,
    "1-й": 1,
    "1й": 1,
    "второй": 2,
    "вторая": 2,
    "второго": 2,
    "вторую": 2,
    "2-й": 2,
    "2й": 2,
    "третий": 3,
    "третья": 3,
    "третьего": 3,
    "третью": 3,
    "3-й": 3,
    "3й": 3,
}


def normalize_staff_items(items: list[dict[str, Any]]) -> list[dict[str, str]]:
    normalized: list[dict[str, str]] = []
    for row in items:
        staff_id = str(row.get("id") or row.get("doctor_id") or "").strip()
        name = str(row.get("name") or row.get("label") or staff_id).strip()
        if not staff_id:
            continue
        normalized.append({"id": staff_id, "name": name or staff_id})
    return normalized


def format_staff_choice_for_client(items: list[dict[str, Any]]) -> str:
    staff = normalize_staff_items(items)
    if not staff:
        return "Специалисты пока не настроены."
    lines = ["К кому записать?"]
    for index, row in enumerate(staff, start=1):
        lines.append(f"{index}. {row['name']}")
    lines.append("Ответьте номером или именем.")
    return "\n".join(lines)


def _format_staff_id_map_for_llm(items: list[dict[str, str]]) -> str:
    lines = ["[id для tools — клиенту не показывать:]"]
    for index, row in enumerate(items, start=1):
        lines.append(f"{index}: {row['id']}")
    return "\n".join(lines)


def format_staff_choice_for_llm(items: list[dict[str, Any]]) -> str:
    staff = normalize_staff_items(items)
    if len(staff) == 1:
        row = staff[0]
        return (
            f"[Инструкция: один специалист — {row['name']}. "
            f"Используй doctor_id={row['id']} в get_available_slots и show_appointment_card. "
            "Клиенту id не показывай.]\n"
            f"Специалист: {row['name']}."
        )
    client_block = format_staff_choice_for_client(staff)
    id_map = _format_staff_id_map_for_llm(staff)
    return (
        "[Инструкция: покажи клиенту только нумерованный список имён ниже; id не показывай. "
        "Для tools используй doctor_id из блока id.]\n\n"
        f"{client_block}\n\n"
        f"{id_map}"
    )


def _match_by_index(raw: str, staff: list[dict[str, str]]) -> dict[str, str] | None:
    if not raw.isdigit():
        return None
    index = int(raw)
    if 1 <= index <= len(staff):
        return staff[index - 1]
    return None


def _match_by_ordinal(raw: str, staff: list[dict[str, str]]) -> dict[str, str] | None:
    word = raw.lower().strip().rstrip(".")
    index = _ORDINAL_WORDS.get(word)
    if index is None or index < 1 or index > len(staff):
        return None
    return staff[index - 1]


def _match_by_name(raw: str, staff: list[dict[str, str]]) -> dict[str, str] | None:
    lowered = raw.lower()
    matches = [
        row
        for row in staff
        if lowered in row["name"].lower() or row["name"].lower() in lowered
    ]
    if len(matches) == 1:
        return matches[0]
    return None


def parse_staff_choice(user_text: str, items: list[dict[str, Any]]) -> dict[str, str] | None:
    staff = normalize_staff_items(items)
    if not staff:
        return None

    raw = (user_text or "").strip()
    if not raw:
        return None

    for row in staff:
        if raw == row["id"]:
            return row

    by_index = _match_by_index(raw, staff)
    if by_index is not None:
        return by_index

    by_ordinal = _match_by_ordinal(raw, staff)
    if by_ordinal is not None:
        return by_ordinal

    specialist_match = re.match(r"^(?:специалист|врач|доктор)\s*(\d+)$", raw.lower())
    if specialist_match:
        return _match_by_index(specialist_match.group(1), staff)

    return _match_by_name(raw, staff)


def resolve_staff_reference(
    raw: str | None, items: list[dict[str, Any]]
) -> tuple[str | None, str | None]:
    """Returns (doctor_id, doctor_name) or (None, None) if unresolved."""
    if not items:
        return None, None
    if raw is None or not str(raw).strip():
        return None, None
    matched = parse_staff_choice(str(raw).strip(), items)
    if matched is None:
        return None, None
    return matched["id"], matched["name"]


def staff_choice_prompt_for_llm(items: list[dict[str, Any]]) -> str:
    return format_staff_choice_for_llm(items)


def staff_unknown_choice_message(raw: str, items: list[dict[str, Any]]) -> str:
    prompt = format_staff_choice_for_llm(items)
    return (
        f"[Инструкция: не удалось понять выбор специалиста «{raw.strip()}». "
        "Покажи клиенту список и спроси номер или имя. Id клиенту не показывай.]\n\n"
        f"{prompt}"
    )
