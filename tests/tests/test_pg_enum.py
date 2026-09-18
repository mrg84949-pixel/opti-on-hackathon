from __future__ import annotations

from unittest.mock import MagicMock

from bot.db.pg_enum import create_postgres_enum_if_not_exists


def test_create_postgres_enum_if_not_exists_executes_sql():
    connection = MagicMock()
    create_postgres_enum_if_not_exists(connection, "appointment_status", ("new", "confirmed"))
    connection.execute.assert_called_once()
    sql = str(connection.execute.call_args[0][0])
    assert "appointment_status" in sql
    assert "'new'" in sql
