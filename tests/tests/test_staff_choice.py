from __future__ import annotations

from bot.llm.staff_choice import (
    AWAITING_STAFF_CHOICE_KEY,
    format_staff_choice_for_client,
    format_staff_choice_for_llm,
    parse_staff_choice,
    resolve_staff_reference,
)

_DEMO_STAFF = [
    {"id": "doc-1", "name": "Специалист 1"},
    {"id": "doc-2", "name": "Специалист 2"},
]


def test_format_staff_choice_for_client_numbered_names_only():
    text = format_staff_choice_for_client(_DEMO_STAFF)
    assert "1. Специалист 1" in text
    assert "2. Специалист 2" in text
    assert "doc-1" not in text
    assert "doc-2" not in text
    assert "Ответьте номером или именем" in text


def test_format_staff_choice_for_llm_hides_ids_from_client_block():
    text = format_staff_choice_for_llm(_DEMO_STAFF)
    client_part = text.split("[id для tools")[0]
    assert "doc-1" not in client_part
    assert "doc-2" not in client_part
    assert "doc-1" in text
    assert "[Инструкция" in text


def test_parse_staff_choice_by_index():
    assert parse_staff_choice("2", _DEMO_STAFF) == {"id": "doc-2", "name": "Специалист 2"}


def test_parse_staff_choice_by_name():
    assert parse_staff_choice("Специалист 1", _DEMO_STAFF) == {
        "id": "doc-1",
        "name": "Специалист 1",
    }


def test_parse_staff_choice_by_ordinal():
    assert parse_staff_choice("первый", _DEMO_STAFF) == {
        "id": "doc-1",
        "name": "Специалист 1",
    }


def test_parse_staff_choice_by_exact_id():
    assert parse_staff_choice("doc-2", _DEMO_STAFF) == {
        "id": "doc-2",
        "name": "Специалист 2",
    }


def test_parse_staff_choice_ambiguous_name_returns_none():
    staff = [
        {"id": "a", "name": "Иванов"},
        {"id": "b", "name": "Иванова"},
    ]
    assert parse_staff_choice("иван", staff) is None


def test_resolve_staff_reference():
    doctor_id, doctor_name = resolve_staff_reference("2", _DEMO_STAFF)
    assert doctor_id == "doc-2"
    assert doctor_name == "Специалист 2"


def test_awaiting_staff_choice_key_constant():
    assert AWAITING_STAFF_CHOICE_KEY == "awaiting_staff_choice"
