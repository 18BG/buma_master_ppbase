"""Regression coverage for source rowid field identity and migration safety."""

import importlib.util
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from app.auth.context import merge_live_auth_context
from app.legacy.views import source_view_row


def migration():
    path = Path(__file__).parents[1] / "pb_migrations/1789236848_c10_sqlite_rowid.py"
    spec = importlib.util.spec_from_file_location("rowid_migration", path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_internal_rowid_does_not_become_an_auth_record_field():
    context = merge_live_auth_context(
        {"type": "auth", "id": "u"}, {"id": "u", "rowid": 5, "name": "User"}, None
    )
    assert "rowid" not in context and context["name"] == "User"


async def test_migration_rejects_existing_records_instead_of_guessing_source_rowids():
    app = SimpleNamespace(execute_sql=AsyncMock(return_value=SimpleNamespace(scalar_one=lambda: 1)))
    with pytest.raises(RuntimeError, match="refusing to invent SQLite rowids"):
        await migration().up(app)
    assert all("ALTER" not in call.args[0] for call in app.execute_sql.await_args_list)


async def test_rollback_does_not_destroy_imported_rowids():
    app = SimpleNamespace(execute_sql=AsyncMock(return_value=SimpleNamespace(scalar_one=lambda: 1)))
    with pytest.raises(RuntimeError, match="rollback requires an empty target"):
        await migration().down(app)
    assert all("DROP" not in call.args[0] for call in app.execute_sql.await_args_list)


def test_left_join_nulls_follow_record_prepare_value_without_changing_numeric_json():
    schema = [
        {"name": "rowid", "type": "json"},
        {"name": "network", "type": "select"},
        {"name": "mobile_transaction_date", "type": "date"},
    ]
    assert source_view_row(
        {"rowid": 2, "network": None, "mobile_transaction_date": None}, schema
    ) == {
        "rowid": 2,
        "network": "",
        "mobile_transaction_date": "",
    }
