"""Executable C9 HTTP contracts, compared with the frozen route inventory."""

from __future__ import annotations

import json
import re
from datetime import UTC, datetime
from pathlib import Path
from unittest.mock import AsyncMock

import pytest
from starlette.requests import Request

from app.auth.context import AUTH_RULE_CONTEXT
from app.commerce.reports import report_date, report_params
from app.commerce.service import CommerceValidationError
from app.hooks import commerce
from app.hooks.register import HOOK_MODULES


class Routes:
    def __init__(self):
        self.handlers = {}

    def route(self, method, path):
        def decorator(handler):
            self.handlers[method, path] = handler
            return handler

        return decorator

    def post(self, path):
        return self.route("POST", path)

    def get(self, path):
        return self.route("GET", path)


def request(body=None, method="POST"):
    raw = b"" if body is None else json.dumps(body).encode()

    async def receive():
        return {"type": "http.request", "body": raw, "more_body": False}

    return Request({"type": "http", "method": method, "headers": []}, receive)


@pytest.fixture
def routes(monkeypatch):
    collector = Routes()
    commerce.register(collector)
    monkeypatch.setattr(commerce, "get_engine", lambda: object())
    return collector.handlers


def canonical(path):
    return re.sub(r"\{[^}]+\}|:[^/]+", ":param", path)


def test_all_29_methods_and_legacy_paths_match_frozen_inventory(routes):
    inventory = json.loads(
        (Path(__file__).parents[1] / "docs/contracts/source-inventory.json").read_text()
    )
    expected = set()
    for route in inventory["routes"]:
        path = route["path"]
        file = route["source_file"].split("/")[-1]
        if file in {"master.sale.ts", "master.purchase.ts", "master.retour.ts"} or (
            file == "master.caisse.ts"
            and (
                path
                in {
                    "/master/caisse/return",
                    "/master/caisse/versement_uv",
                    "/master/caisse/add_payment_to_sale",
                }
                or "/reportBy" in path
            )
        ):
            expected.add((route["method"], canonical(path)))
    actual = {(method, canonical(path)) for method, path in routes}
    assert len(expected) == 29
    assert actual == expected
    assert "app.hooks.commerce" in HOOK_MODULES


DENIALS = [
    ("/master/sale/new", 403, "Vous étes pas authorisé a effectuer une vente"),
    ("/master/sale/recharge", 403, "Vous n'avez pas les permissions necessaires"),
    ("/master/sale/cancel_recharge", 403, "Vous n'avez pas les permissions necessaires"),
    ("/master/sale/cancel", 403, "Vous n'etes pas autorisé"),
    ("/master/sale/recharge_stats", 403, "Vous n'avez pas les permissions nécessaires"),
    ("/master/purchase/new", 403, "Vous n'avez pas les droits pour créer un achat"),
    ("/master/purchase/cancel", 403, "Vous n'avez pas les droits"),
    ("/master/purchase/receive", 403, "Vous n'avez pas les droits pour mettre à jour un achat"),
    (
        "/master/purchase/add-payment",
        403,
        "Vous n'avez pas les droits pour effectuer cette opération",
    ),
    (
        "/master/purchase/cancel-wallet-transaction",
        403,
        "Vous n'avez pas les droits pour annuler une transaction",
    ),
    ("/master/retour/new", 403, "Vous n'avez pas les droits"),
    ("/master/retour/update", 403, "Vous n'avez pas la permission de modifier ce retour"),
    ("/master/retour/updatePDV", 403, "Vous n'avez pas les droits"),
    ("/master/retour/return_stats", 403, "Vous n'avez pas les permissions nécessaires"),
    ("/master/caisse/return", 400, "Vous n'avez pas les autorisations"),
    (
        "/master/caisse/versement_uv",
        403,
        "Vous n'avez pas les permissions pour effectuer cette op�ration",
    ),
    (
        "/master/caisse/add_payment_to_sale",
        403,
        "Vous n'avez pas les permissions pour effectuer cette op�ration",
    ),
]


@pytest.mark.parametrize("path,status,message", DENIALS)
async def test_permission_and_anonymous_envelopes(routes, monkeypatch, path, status, message):
    monkeypatch.setattr(commerce, "_explicitly_denied", AsyncMock(return_value=True))
    token = AUTH_RULE_CONTEXT.set({})
    try:
        handler = routes["POST", path]
        unauthorized = await handler(request({}), auth=None)
        assert unauthorized.status_code == 401
        assert json.loads(unauthorized.body) == {"message": "The request requires authentication."}
        denied = await handler(request({}), auth={"id": "user", "type": "auth"})
        assert denied.status_code == status
        assert json.loads(denied.body) == {"message": message}
    finally:
        AUTH_RULE_CONTEXT.reset(token)


@pytest.mark.parametrize(
    "path,symbol,payload",
    [
        ("/master/sale/sold_products_report", "sold_products", {"items": [], "totalItems": 0}),
        ("/master/sale/sold_products_info", "sold_products", {"totalRevenue": 0}),
        ("/master/sale/product_sales_by_customer", "product_customers", {"items": []}),
        (
            "/master/sale/check_caisse_transactions",
            "check_sale_caisse",
            {"hasCaisseTransactions": False},
        ),
        ("/master/sale/customers_summary_by_period", "customer_summary", {"items": []}),
        (
            "/master/sale/customers_summary_info_by_period",
            "customer_summary",
            {"customersCount": 0},
        ),
        ("/master/sale/customer_products_report", "customer_products", {"items": []}),
        ("/master/sale/customers_products_report", "customers_products", {"items": []}),
    ],
)
async def test_source_public_reports_do_not_gain_an_auth_guard(
    routes, monkeypatch, path, symbol, payload
):
    operation = AsyncMock(return_value=payload)
    monkeypatch.setattr(commerce, symbol, operation)
    response = await routes["POST", path](request({"startDate": "2026-09-23"}))
    assert response.status_code == 200
    assert json.loads(response.body) == payload
    operation.assert_awaited_once()


async def test_update_pdv_preserves_empty_success_body_and_missing_permission(routes, monkeypatch):
    monkeypatch.setattr(commerce, "_explicitly_denied", AsyncMock(return_value=False))
    operation = AsyncMock(return_value=None)
    monkeypatch.setattr(commerce, "update_return_pdv", operation)
    response = await routes["POST", "/master/retour/updatePDV"](
        request({"returnId": "x"}), auth={"id": "u"}
    )
    assert response.status_code == 200 and response.body == b""
    assert "content-type" not in response.headers
    operation.assert_awaited_once()


async def test_return_info_requires_auth_but_no_read_permission(routes, monkeypatch):
    monkeypatch.setattr(
        commerce, "return_info", AsyncMock(return_value={"returnRecord": {"id": "r"}})
    )
    token = AUTH_RULE_CONTEXT.set({})
    try:
        response = await routes["GET", "/master/retour/info/{return_id}"](
            "r", request(method="GET"), auth={"id": "u"}
        )
        assert response.status_code == 200
        assert json.loads(response.body) == {"returnRecord": {"id": "r"}}
    finally:
        AUTH_RULE_CONTEXT.reset(token)


async def test_recharge_cancellation_error_prefix_is_legacy(routes, monkeypatch):
    token = AUTH_RULE_CONTEXT.set({"role.permission.sale.remove": True})
    monkeypatch.setattr(
        commerce,
        "cancel_recharge",
        AsyncMock(side_effect=CommerceValidationError("Vente invalide")),
    )
    try:
        response = await routes["POST", "/master/sale/cancel_recharge"](
            request({"sale_id": "x"}), auth={"id": "u"}
        )
        assert response.status_code == 400
        assert json.loads(response.body) == {
            "message": "Impossible d'annuler le rechargement, Vente invalide"
        }
    finally:
        AUTH_RULE_CONTEXT.reset(token)


async def test_paid_purchase_requires_wallet_permission_before_writing(routes, monkeypatch):
    token = AUTH_RULE_CONTEXT.set({"role.permission.purchase.create": True})
    operation = AsyncMock(return_value="purchase")
    monkeypatch.setattr(commerce, "create_purchase", operation)
    try:
        response = await routes["POST", "/master/purchase/new"](
            request({"payed": 1}), auth={"id": "u"}
        )
        assert response.status_code == 403
        assert json.loads(response.body) == {
            "message": "Vous n'avez pas les droits pour effectuer des paiements"
        }
        operation.assert_not_awaited()
    finally:
        AUTH_RULE_CONTEXT.reset(token)


@pytest.mark.parametrize(
    "path,message",
    [
        ("/master/purchase/receive", "ID d'achat non spécifié"),
        ("/master/purchase/cancel-wallet-transaction", "ID de transaction non spécifié"),
    ],
)
async def test_purchase_required_identifier_envelope(routes, path, message):
    token = AUTH_RULE_CONTEXT.set({"role.permission.isAdmin": True})
    try:
        response = await routes["POST", path](request({}), auth={"id": "u"})
        assert response.status_code == 400 and json.loads(response.body) == {"message": message}
    finally:
        AUTH_RULE_CONTEXT.reset(token)


def test_report_dates_and_filter_keep_legacy_day_boundaries():
    params = report_params(
        {"startDate": "2026-09-23", "endDate": "2026-09-23", "filter": " L'été-12 "}
    )
    assert params == {
        "start": datetime(2026, 9, 23, tzinfo=UTC),
        "end": datetime(2026, 9, 23, 23, 59, 59, tzinfo=UTC),
        "filter": "%Lt12%",
    }
    assert report_date("2026-09-23T10:11:12.123Z").microsecond == 123000
