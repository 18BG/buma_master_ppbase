"""Frozen-source regressions for C10, including observed PB 0.22 envelopes."""

import ast
import json
import re
from pathlib import Path
from unittest.mock import AsyncMock

import pytest

from app.hooks import legacy_commerce, legacy_master
from app.legacy import customer, master
from app.legacy.caisse import LegacyNotFoundError
from app.legacy.http import NOT_FOUND, UNAUTHORIZED
from tests.test_legacy_stockage_contract import Routes, req


def test_active_source_routes_are_registered_and_dormant_test_stays_absent():
    root = Path(__file__).parents[1]
    inventory = json.loads((root / "docs/contracts/source-inventory.json").read_text())
    registered = set()
    for path in (root / "app/hooks").glob("*.py"):
        for node in ast.walk(ast.parse(path.read_text())):
            if not isinstance(node, ast.Call) or not isinstance(node.func, ast.Attribute):
                continue
            if node.func.attr not in {"get", "post", "patch", "put", "delete"} or not node.args:
                continue
            try:
                value = ast.literal_eval(node.args[0])
            except (ValueError, TypeError):
                continue
            if isinstance(value, str):
                registered.add((node.func.attr.upper(), re.sub(r"\{[^}]+\}", ":param", value)))
    for route in inventory["routes"]:
        if route["source_file"] == "src/entries/return.ts":
            continue
        assert (route["method"], re.sub(r":[A-Za-z_]+", ":param", route["path"])) in registered
    assert ("POST", "/api2/test") not in registered
    for bootstrap in ("index.pb.ts", "master.pb.ts"):
        source = (root.parent / "buma_master_hooks/src/entries" / bootstrap).read_text()
        assert "require(`${__hooks}/return.js`)" not in source


def test_customer_manager_does_not_gain_create_from_source_branch():
    assert customer.permission({"customer": {"canManage": True, "create": True}}) == {
        "canManage": True,
        "read": True,
    }
    assert customer.permission({"isAdmin": True})["create"] is True


@pytest.mark.parametrize(
    "failure,status,body",
    [
        ("uncaught", 404, NOT_FOUND),
        ("json500", 500, {"message": {"value": {}}}),
        ("empty", 200, None),
    ],
)
async def test_same_dao_failure_keeps_each_handler_error_boundary(
    monkeypatch, failure, status, body
):
    monkeypatch.setattr(legacy_commerce, "get_engine", lambda: object())
    monkeypatch.setattr(legacy_commerce, "record", AsyncMock(return_value={"permission": {}}))
    operation = AsyncMock(side_effect=LegacyNotFoundError("missing"))
    result = await legacy_commerce.run(req({}), {"id": "u"}, "sale", operation, failure)
    assert result.status_code == status
    assert (json.loads(result.body) if result.body else None) == body


async def test_js_error_serializes_differently_from_go_dao_error(monkeypatch):
    monkeypatch.setattr(legacy_commerce, "get_engine", lambda: object())
    monkeypatch.setattr(legacy_commerce, "record", AsyncMock(return_value={"permission": {}}))
    result = await legacy_commerce.run(
        req({}), {"id": "u"}, "sale", AsyncMock(side_effect=TypeError("replace"))
    )
    assert result.status_code == 500 and json.loads(result.body) == {"message": {}}


async def test_legacy_auth_envelope_matches_reference():
    result = await legacy_commerce.run(req({}), None, "sale", AsyncMock())
    assert result.status_code == 401 and json.loads(result.body) == UNAUTHORIZED


async def test_pilotage_reproduces_empty_response_without_external_work(monkeypatch):
    routes = Routes()
    legacy_master.register(routes)
    engine = AsyncMock(side_effect=AssertionError("source fails before reading configuration"))
    monkeypatch.setattr(legacy_master, "get_engine", engine)
    result = await routes.routes["GET", "/master/pilotage/date-report/{startDate}"]("2026-01-01")
    assert result.status_code == 200 and result.body == b""
    engine.assert_not_called()


async def test_missing_admin_app_permission_preserves_source_failure(monkeypatch):
    monkeypatch.setattr(master, "read", AsyncMock(return_value={"permission": {"isAdmin": True}}))
    monkeypatch.setattr(master, "rows", AsyncMock(return_value=[]))
    with pytest.raises(ValueError, match="Cannot read property 'length' of undefined"):
        await master.pdv_permission(object(), "role")
