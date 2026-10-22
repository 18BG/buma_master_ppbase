"""Observable caisse.ts permission quirks and ordered late failures."""

import json
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from app.hooks import legacy_caisse
from app.legacy import caisse
from tests.test_legacy_stockage_contract import Routes, req


def test_raw_record_serialization_matches_pocketbase_public_export():
    collection = SimpleNamespace(type="auth", schema=[{"name": "date", "type": "date"}])
    output = caisse.public_record(
        collection, {"emailVisibility": False, "email": "private", "date": None}
    )
    assert output == {"emailVisibility": False, "date": ""}
    assert (
        caisse.public_record(collection, {"emailVisibility": True, "email": "visible"})["email"]
        == "visible"
    )


async def test_fractional_expense_sum_keeps_integer_scan_error(monkeypatch):
    monkeypatch.setattr(
        caisse, "rows", AsyncMock(return_value=[{"amount": 2.5, "count": 1, "label": "X"}])
    )
    assert await caisse.expense_report(object(), "c", "2000", "2999") == (
        400,
        {"message": "Error", "error": {"value": {}}},
    )


def test_non_admin_flag_is_absent_even_for_manager():
    for raw in ({}, {"caisse": {"canManage": True}}, {"isAdmin": False}):
        assert "isAdmin" not in caisse.permission(raw)
    admin = caisse.permission({"isAdmin": True})
    assert admin["isAdmin"] is True and admin["canCancelTransaction"] is False
    assert caisse.permission({"caisse": {"canWrite": True}})["canCreate"] is True


@pytest.mark.parametrize(
    "path,public",
    [
        ("report/{id}", True),
        ("rangeReport/{id}/{start}/{end}", True),
        ("reportByExpenseType/{id}/{start}/{end}", False),
    ],
)
async def test_anonymous_reports_have_source_guards(monkeypatch, path, public):
    routes = Routes()
    legacy_caisse.register(routes)
    monkeypatch.setattr(legacy_caisse, "get_engine", lambda: object())
    permission = AsyncMock(side_effect=AssertionError("anonymous must not load a role"))
    monkeypatch.setattr(legacy_caisse, "source_permission", permission)
    for op in ("report", "range_report", "expense_report"):
        monkeypatch.setattr(caisse, op, AsyncMock(return_value={"transactions": []}))
    args = {"id": "session", "request": req({}), "auth": None}
    if "{start}" in path:
        args.update(start="2026", end="2027")
    response = await routes.routes["GET", "/api2/caisse/" + path](**args)
    assert response.status_code == (200 if public else 401)
    permission.assert_not_awaited()


async def test_open_commits_session_before_caisse_failure(monkeypatch):
    monkeypatch.setattr(
        caisse,
        "record",
        AsyncMock(
            side_effect=[{"id": "c", "state": "REQUEST_OPEN", "currentSession": "s"}, {"id": "s"}]
        ),
    )
    persist = AsyncMock(side_effect=[None, RuntimeError("late save")])
    monkeypatch.setattr(caisse, "save", persist)
    with pytest.raises(RuntimeError, match="late save"):
        await caisse.open_caisse(object(), {"caisse": "c"}, "u", {"canManage": True})
    assert [call.args[1] for call in persist.await_args_list] == [
        "buma_caisse_open_close",
        "buma_caisse",
    ]
    assert persist.await_args_list[0].args[3]["state"] == "OPEN"


async def test_withdrawal_leaves_wallet_transaction_when_second_insert_fails(monkeypatch):
    monkeypatch.setattr(
        caisse,
        "record",
        AsyncMock(
            side_effect=[
                {"id": "c", "name": "Till", "balance": 20, "currentSession": "s"},
                {"id": "s"},
                {"id": "w", "balance": 10},
            ]
        ),
    )
    insert = AsyncMock(side_effect=[{"id": "wt"}, RuntimeError("late insert")])
    persist = AsyncMock()
    monkeypatch.setattr(caisse, "insert_transaction", insert)
    monkeypatch.setattr(caisse, "save", persist)
    assert (
        await caisse.withdrawal(
            object(), {"caisse": "c", "wallet": "w", "amount": 2}, "u", {"canManage": True}
        )
        is None
    )
    first = insert.await_args_list[0].args[2]
    assert first["transaction_no"] == 1 and "caisseSession" not in first and "state" not in first
    persist.assert_not_awaited()


async def test_empty_success_and_swallowed_error_keep_empty_http_body(monkeypatch):
    monkeypatch.setattr(
        legacy_caisse, "source_permission", AsyncMock(return_value={"canManage": True})
    )
    result = await legacy_caisse.run(req({}), {"id": "u"}, AsyncMock(return_value=None))
    assert result.status_code == 200 and result.body == b"" and "content-type" not in result.headers


async def test_reversal_preserves_original_status_and_original_author(monkeypatch):
    monkeypatch.setattr(
        caisse,
        "record",
        AsyncMock(
            side_effect=[
                {
                    "id": "t",
                    "caisse": "c",
                    "session": "old",
                    "author": "original",
                    "status": True,
                    "amount": 4,
                    "note": "x",
                },
                {"id": "c", "currentSession": "now", "balance": 10},
                {"id": "old", "state": "CLOSE"},
                {"id": "now"},
            ]
        ),
    )
    persist, insert = AsyncMock(), AsyncMock()
    monkeypatch.setattr(caisse, "save", persist)
    monkeypatch.setattr(caisse, "insert_transaction", insert)
    assert await caisse.cancel_reversal(object(), "t", "other", caisse.permission({})) == {
        "message": "ok"
    }
    assert persist.await_args_list[0].args[3] == {"balance": 14}
    assert persist.await_args_list[1].args[3] == {"state": "CANCELLED"}
    assert insert.await_args.args[2]["author"] == "original"
    assert insert.await_args.args[2]["type"] == "REVERSAL"


async def test_missing_role_is_not_swallowed_by_handler_catch(monkeypatch):
    monkeypatch.setattr(
        legacy_caisse, "source_permission", AsyncMock(side_effect=ValueError("missing role"))
    )
    operation = AsyncMock()
    result = await legacy_caisse.run(req({}), {"id": "u"}, operation)
    assert result.status_code == 400
    assert json.loads(result.body)["data"] == {}
    operation.assert_not_awaited()
