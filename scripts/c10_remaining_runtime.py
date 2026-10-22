#!/usr/bin/env python3
"""Same legacy scenarios against frozen PocketBase and PPBase, isolated fixtures."""

from __future__ import annotations

import argparse
import asyncio
import json
import re
import secrets
import sys
from datetime import UTC, datetime
from pathlib import Path

import httpx
from sqlalchemy import text
from sqlalchemy.ext.asyncio import create_async_engine

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from app.commerce.sequential import create as native_create  # noqa: E402
from app.legacy.records import create, save  # noqa: E402


def normalized(value):
    if isinstance(value, dict):
        return {k: normalized(v) for k, v in value.items()}
    if isinstance(value, list):
        return [normalized(v) for v in value]
    if isinstance(value, str) and re.fullmatch("[a-z0-9]{15}", value):
        return "<record-id>"
    return value


class Backend:
    def __init__(self, url, reference, prefix):
        self.client = httpx.AsyncClient(base_url=url, timeout=30)
        self.reference, self.prefix = reference, prefix
        self.engine = (
            None
            if reference
            else create_async_engine(
                "postgresql+asyncpg://ppbase:ppbase@localhost:55434/buma_master_ppbase",
                connect_args={"ssl": "disable"},
            )
        )
        self.made, self.headers, self.trace = [], {}, {}

    async def fixture_api(self, **body):
        r = await self.client.post("/__c10/fixture", json=body)
        assert r.status_code == 200, (body.get("table"), r.status_code, r.text)
        return r.json()

    async def fixture(self, table, values):
        if self.reference:
            row = await self.fixture_api(table=table, values=values)
        else:
            fn = native_create if table in {"users", "agent_users"} else create
            row = await fn(self.engine, table, values)
        self.made.append((table, row["id"]))
        return row["id"]

    async def read(self, table, rid):
        if self.reference:
            return await self.fixture_api(action="read", table=table, id=rid)
        async with self.engine.connect() as conn:
            r = await conn.execute(text(f'SELECT * FROM "{table}" WHERE id=:id'), {"id": rid})
            return dict(r.mappings().one())

    async def update(self, table, rid, values):
        if self.reference:
            return await self.fixture_api(action="update", table=table, id=rid, values=values)
        return await save(self.engine, table, rid, values)

    async def matching(self, table, **values):
        if self.reference:
            records = await self.fixture_api(action="list", table=table)
        else:
            async with self.engine.connect() as conn:
                r = await conn.execute(text(f'SELECT * FROM "{table}"'))
                records = [dict(row) for row in r.mappings()]
        return [r for r in records if all(r.get(k) == v for k, v in values.items())]

    async def track(self, table, **values):
        records = await self.matching(table, **values)
        for r in records:
            if (table, r["id"]) not in self.made:
                self.made.append((table, r["id"]))
        return records

    async def call(self, label, path, data=None, status=200, *, method="POST", headers=None):
        r = await self.client.request(
            method, path, json=data, headers=self.headers if headers is None else headers
        )
        body = r.json() if r.content else None
        self.trace[label] = {
            "status": r.status_code,
            "body": normalized(body),
            "content_type": r.headers.get("content-type", "").split(";")[0],
        }
        assert r.status_code == status, (
            "PB" if self.reference else "PPBase",
            label,
            status,
            r.status_code,
            r.text,
        )
        return body

    async def state(self, label, table, rid, fields):
        row = await self.read(table, rid)
        self.trace[label] = normalized({key: row.get(key) for key in fields})
        return row

    async def close(self):
        # Delete only records explicitly created/tracked by these scenarios.
        priority = {
            name: i
            for i, name in enumerate(
                (
                    "buma_sim_balance_activity",
                    "buma_transaction",
                    "buma_payment",
                    "buma_caisse_transactions",
                    "buma_return",
                    "buma_buy",
                    "buma_sale",
                    "buma_customer_pdv",
                    "buma_sim",
                    "buma_pdv",
                    "buma_caisse_open_close",
                    "buma_article_stock",
                    "buma_rupture",
                    "buma_customer",
                    "buma_supplier",
                    "buma_person_entity",
                    "buma_article",
                    "buma_article_stockage",
                    "buma_caisse",
                    "buma_payment_method",
                    "agent_users",
                    "buma_agent",
                    "users",
                    "roles",
                )
            )
        }
        failures = []
        for table, rid in sorted(set(self.made), key=lambda item: priority.get(item[0], 0)):
            try:
                if self.reference:
                    if await self.matching(table, id=rid):
                        await self.fixture_api(action="delete", table=table, id=rid)
                else:
                    async with self.engine.begin() as conn:
                        await conn.execute(text(f'DELETE FROM "{table}" WHERE id=:id'), {"id": rid})
            except Exception as exc:
                failures.append((table, rid, str(exc)))
        for table, rid in self.made:
            if await self.matching(table, id=rid):
                failures.append((table, rid, "record remains"))
        await self.client.aclose()
        if self.engine:
            await self.engine.dispose()
        assert not failures, failures


async def scenario(backend):
    b, prefix = backend, backend.prefix
    user = stock = ""
    try:
        role = await b.fixture(
            "roles", {"name": prefix, "permission": {"isAdmin": True}, "status": True}
        )
        user = await b.fixture(
            "users",
            {
                "name": prefix,
                "username": prefix,
                "email": prefix + "@example.com",
                "role": role,
                "password": "abcde123",
                "passwordConfirm": "abcde123",
            },
        )
        login = await b.client.post(
            "/api/collections/users/auth-with-password",
            json={"identity": prefix, "password": "abcde123"},
        )
        assert login.status_code == 200, login.text
        b.headers = {"Authorization": login.json()["token"]}

        async def permission(values):
            await b.update("roles", role, {"permission": values})

        await b.call("legacy_401", "/api2/customer/create", {"infos": []}, 401, headers={})
        await permission({"customer": {"canManage": True, "create": True}})
        assert await b.call(
            "manager_create_denied", "/api2/customer/create", {"infos": []}, 403
        ) == {"message": "Your are not allowed"}
        await permission({"isAdmin": True})
        assert await b.call(
            "customer_batch",
            "/api2/customer/create",
            {
                "infos": [
                    {"firstname": prefix, "salary": 12},
                    {"firstname": prefix + "B", "salary": -3},
                ]
            },
        ) == {"message": "ok"}
        customers = await b.track("buma_customer", author=user)
        people = await b.track("buma_person_entity", author=user)
        assert len(customers) == len(people) == 2
        b.trace["customer_persistence"] = sorted(
            [(r["solde"], r["kind"], r["firstname"], r["status"]) for r in customers]
        )
        customer = customers[0]["id"]
        await b.call(
            "sim_import",
            "/api2/pvd/importSim",
            {"sims": [{"number": prefix + "1", "network": "RS", "notes": "fixture"}]},
        )
        sims = await b.track("buma_sim", number=prefix + "1")
        assert len(sims) == 1
        await b.call(
            "pdv_import_existing_sim",
            "/api2/pvd/import",
            {
                "pdvs": [
                    {
                        "firstname": prefix,
                        "name": prefix,
                        "identifiant": prefix + "1",
                        "network": "RS",
                    }
                ]
            },
        )
        pdvs = await b.track("buma_pdv", author=user)
        await b.track("buma_person_entity", author=user)
        assert len(pdvs) == 1
        point = pdvs[0]["id"]
        await b.state(
            "pdv_defaults", "buma_pdv", point, ["firstname", "lastname", "status", "author"]
        )
        assert (await b.read("buma_sim", sims[0]["id"]))["pdv"] == point
        await b.call(
            "pdv_missing_sim_partial",
            "/api2/pvd/import",
            {"pdvs": [{"firstname": prefix + "partial"}]},
            400,
        )
        assert len(await b.track("buma_pdv", author=user)) == 2
        await b.track("buma_person_entity", author=user)
        await b.call(
            "pdv_new_sim",
            "/api2/pvd/import",
            {"pdvs": [{"firstname": prefix + "new", "identifiant": prefix + "2", "network": "RS"}]},
        )
        await b.track("buma_pdv", author=user)
        await b.track("buma_person_entity", author=user)
        new_sim = (await b.track("buma_sim", number=prefix + "2"))[0]
        await b.state(
            "new_sim_defaults", "buma_sim", new_sim["id"], ["status", "author", "network"]
        )
        await b.call(
            "broken_parent_lookup",
            f"/api2/customer/importPDVFromAgent/missing/{customer}",
            status=400,
            method="GET",
        )

        supplier = await b.fixture(
            "buma_supplier", {"firstname": prefix, "status": True, "solde": 0, "author": user}
        )
        article = await b.fixture(
            "buma_article",
            {
                "designation": prefix,
                "kind": "AUTRE",
                "trackStock": True,
                "status": True,
                "author": user,
            },
        )
        storage = await b.fixture(
            "buma_article_stockage", {"name": prefix, "status": True, "author": user}
        )
        stock = await b.fixture(
            "buma_article_stock",
            {
                "article": article,
                "stockage": storage,
                "quantity": 10,
                "status": True,
                "author": user,
            },
        )
        caisse = await b.fixture(
            "buma_caisse",
            {
                "name": prefix,
                "state": "OPEN",
                "balance": 100,
                "status": True,
                "author": user,
                "currentCashier": user,
            },
        )
        session = await b.fixture(
            "buma_caisse_open_close",
            {"caisse": caisse, "state": "OPEN", "author": user, "status": True},
        )
        # The source has a native before-create caisse hook forcing CLOSE.
        # Fixture helper DAO writes bypass request hooks on both servers.
        await b.update("buma_caisse", caisse, {"currentSession": session})
        method = await b.fixture(
            "buma_payment_method", {"name": prefix, "status": True, "author": user}
        )
        item = {
            "article_stock": stock,
            "article": article,
            "stockage": storage,
            "quantity": 2,
            "buy_price": 5,
        }
        await permission({})
        buy = await b.call(
            "purchase_no_create_guard",
            "/api2/purchase/add",
            {"supplier": supplier, "payed": 0, "items": [item], "note": prefix},
        )
        b.made.append(("buma_buy", buy["id"]))
        await b.state(
            "purchase_values",
            "buma_buy",
            buy["id"],
            ["amount", "remaining", "payed", "invoice_no", "state", "received"],
        )
        assert (await b.read("buma_supplier", supplier))["solde"] == -10
        assert (await b.read("buma_article_stock", stock))["quantity"] == 12
        await b.call(
            "purchase_payment_denied",
            "/api2/purchase/payment/add",
            {"buy": buy["id"], "caisse": caisse, "amount": 3, "method": method},
            403,
        )
        await permission({"purchase": {"canMakeTransaction": True, "canCancelTransaction": True}})
        await b.call(
            "purchase_payment",
            "/api2/purchase/payment/add",
            {"buy": buy["id"], "caisse": caisse, "amount": 3, "method": method},
        )
        assert (await b.read("buma_caisse", caisse))["balance"] == 97
        await b.call("purchase_cancel", "/api2/purchase/cancel", {"buyId": buy["id"]})
        await b.call("purchase_cancel_repeat", "/api2/purchase/cancel", {"buyId": buy["id"]})
        await b.state(
            "purchase_after_repeat",
            "buma_buy",
            buy["id"],
            ["status", "state", "remaining", "payed"],
        )
        await b.state("supplier_after_repeat", "buma_supplier", supplier, ["solde"])
        await b.state("stock_after_repeat", "buma_article_stock", stock, ["quantity"])
        await b.state("caisse_after_repeat", "buma_caisse", caisse, ["balance"])
        assert (await b.read("buma_article_stock", stock))["quantity"] == 8
        assert (await b.read("buma_caisse", caisse))["balance"] == 103
        await b.call(
            "purchase_late_failure",
            "/api2/purchase/add",
            {
                "supplier": supplier,
                "payed": 0,
                "items": [item, {**item, "article_stock": "missing"}],
                "note": prefix + "partial",
            },
            500,
        )
        purchases = await b.track("buma_buy", author=user)
        assert len(purchases) == 2
        await b.state("stock_after_partial_purchase", "buma_article_stock", stock, ["quantity"])
        await b.state("supplier_after_partial_purchase", "buma_supplier", supplier, ["solde"])
        await b.call(
            "purchase_payment_swallowed",
            "/api2/purchase/payment/add",
            {"buy": "missing", "caisse": caisse, "amount": 1, "method": method},
        )
        await b.call(
            "purchase_infos", "/api2/purchase/infos", {"startDate": "2000", "endDate": "2999"}
        )
        await b.call("purchase_infos_malformed", "/api2/purchase/infos", {}, 500)

        sale = await b.fixture(
            "buma_sale",
            {
                "customer": customer,
                "amount": 20,
                "payed": 0,
                "remaining": 20,
                "status": True,
                "author": user,
            },
        )
        sale_tx = await b.fixture(
            "buma_transaction",
            {
                "sale": sale,
                "article": article,
                "article_stock": stock,
                "stockage": storage,
                "quantity": 2,
                "status": True,
                "type": "SALE",
                "author": user,
            },
        )
        await permission({"sale": {"update": True, "canCancel": True}})
        await b.call(
            "sale_payment",
            "/api2/sale/payment/add",
            {"sale": sale, "caisse": caisse, "amount": 5, "method": method},
        )
        payments = await b.track("buma_payment", sale=sale)
        assert payments[0]["caisse_transaction"] == ""
        await b.call(
            "sale_payment_too_large",
            "/api2/sale/payment/add",
            {"sale": sale, "caisse": caisse, "amount": 16, "method": method},
            400,
        )
        await b.call("sale_infos", "/api2/sale/infos", {"startDate": "2000", "endDate": "2999"})
        await b.call("sale_cancel", "/api2/sale/cancel", {"saleId": sale})
        await b.call("sale_cancel_repeat", "/api2/sale/cancel", {"saleId": sale})
        await b.state(
            "sale_after_repeat", "buma_sale", sale, ["status", "state", "remaining", "payed"]
        )
        await b.state("sale_stock_after_repeat", "buma_article_stock", stock, ["quantity"])
        assert (await b.read("buma_transaction", sale_tx))["status"] is False
        assert (await b.read("buma_caisse", caisse))["balance"] == 108
        await b.call(
            "sale_payment_missing",
            "/api2/sale/payment/add",
            {"sale": "missing", "caisse": caisse, "amount": 1},
            404,
        )

        await permission({})
        await b.call(
            "return_no_domain_guard",
            "/api2/return",
            {
                "customer": customer,
                "articleStock": stock,
                "pdv": point,
                "amount": "4.8",
                "payed": False,
            },
        )
        returns = await b.track("buma_return", article_stock=stock)
        assert len(returns) == 1
        retour = returns[0]["id"]
        await b.state(
            "return_defaults",
            "buma_return",
            retour,
            ["amount", "status", "author", "payed", "caisse", "caisseSession"],
        )
        await b.call(
            "return_confirm", "/api2/return/confirm", {"returnId": retour, "caisse": caisse}
        )
        await b.call(
            "return_confirm_repeat", "/api2/return/confirm", {"returnId": retour, "caisse": caisse}
        )
        assert (await b.read("buma_caisse", caisse))["balance"] == 100
        await b.state(
            "return_after_repeat", "buma_return", retour, ["payed", "caisse", "caisseSession"]
        )
        await b.call(
            "return_late_failure",
            "/api2/return",
            {"customer": customer, "articleStock": "missing", "amount": "3", "payed": False},
            500,
        )
        await b.track("buma_return", customer=customer)

        await b.call("assign_unauthenticated", "/master/pdv/assign", {}, 401, headers={})
        await b.call("assign_without_agent", "/master/pdv/assign", {"pdv_id": point}, 403)
        agent = await b.fixture("buma_agent", {"firstname": prefix, "status": True, "author": user})
        await b.fixture(
            "agent_users",
            {
                "username": prefix + "agent",
                "email": prefix + "agent@example.com",
                "agent": agent,
                "password": "abcde123",
                "passwordConfirm": "abcde123",
            },
        )
        agent_login = await b.client.post(
            "/api/collections/agent_users/auth-with-password",
            json={"identity": prefix + "agent", "password": "abcde123"},
        )
        assert agent_login.status_code == 200, agent_login.text
        agent_headers = {"Authorization": agent_login.json()["token"]}
        await b.call("assign_missing_id", "/master/pdv/assign", {}, 400, headers=agent_headers)
        await b.call("assign", "/master/pdv/assign", {"pdv_id": point}, headers=agent_headers)
        await b.call(
            "assign_repeat", "/master/pdv/assign", {"pdv_id": point}, headers=agent_headers
        )
        assert (await b.read("buma_pdv", point))["agent"] == agent
        await b.call("unassign", "/master/pdv/unassign", {"pdv_id": point}, headers=agent_headers)
        await b.call(
            "unassign_repeat", "/master/pdv/unassign", {"pdv_id": point}, 400, headers=agent_headers
        )
        assert (await b.read("buma_pdv", point))["agent"] == ""
        await b.call("turnover_forbidden", "/master/pdv/turnovers", {}, 403)
        await permission({"pdv": {"read": True}})
        await b.call("turnover_dates_required", "/master/pdv/turnovers", {}, 400)
        await b.call(
            "turnover_invalid_dates",
            "/master/pdv/turnovers",
            {"startDate": "bad", "endDate": "bad"},
            400,
        )
        await b.update(
            "buma_pdv", point, {"firstname": prefix, "lastname": "Turnover", "daily_goal": 100}
        )
        for day, amount, single in (
            ("1985-01-01", 20, True),
            ("1985-01-02", 30, False),
            ("1985-01-03", 900, True),
        ):
            await b.fixture(
                "buma_sim_balance_activity",
                {
                    "sim": sims[0]["id"],
                    "date": day + " 00:00:00.000Z",
                    "balance": amount,
                    "isSingleWallet": single,
                    "msisdn": prefix,
                    "revendeur": prefix,
                    "id_date": prefix + day,
                    "author": user,
                },
            )
        turnover = await b.call(
            "turnover_aggregate",
            "/master/pdv/turnovers",
            {
                "startDate": "1985-01-01",
                "endDate": "1985-01-03",
                "name": prefix,
                "sortBy": "totalTurnover",
                "sortOrder": "asc",
            },
        )
        assert len(turnover["data"]) == 1
        summary = turnover["data"][0]
        assert (
            summary["totalTurnover"] == 50
            and summary["numberOfDays"] == 2
            and summary["totalDaysInPeriod"] == 3
        )
        assert (
            summary["total_turnover_single_wallet"] == 20 and summary["total_turnover_nafama"] == 30
        )
        await b.call(
            "turnover_filter_empty",
            "/master/pdv/turnovers",
            {
                "startDate": "1985-01-01",
                "endDate": "1985-01-03",
                "name": prefix,
                "network": "NO_MATCH",
                "page": 2,
                "perPage": 1,
            },
        )
        await b.call(
            "pilotage_missing_middleware",
            "/master/pilotage/date-report/2026-01-01",
            method="GET",
            headers={},
        )
        await b.call(
            "pilotage_authenticated_same", "/master/pilotage/date-report/2026-01-01", method="GET"
        )
        await b.call("rupture_invalid", "/save-rupture", {}, 400, headers={})
        await b.call(
            "rupture_batch",
            "/save-rupture",
            {
                "data": [
                    {"Number": prefix, "Balance": 4},
                    {"Number": prefix, "Balance": 9},
                    {"Number": prefix + "bad", "Balance": "2"},
                    {"Number": prefix + "bool", "Balance": True},
                ],
                "file_name": prefix,
                "total_records": 25,
            },
            headers={},
        )
        rupture = (await b.track("buma_rupture", number=prefix))[0]
        await b.state(
            "rupture_persistence",
            "buma_rupture",
            rupture["id"],
            ["number", "balance", "user_id", "file_name"],
        )
        assert rupture["balance"] == 9
        await b.call(
            "rupture_repeat",
            "/save-rupture",
            {"data": [{"Number": prefix, "Balance": 11}]},
            headers={},
        )
        await b.call(
            "rupture_partial",
            "/save-rupture",
            {"data": [{"Number": prefix, "Balance": 13}, None]},
            400,
            headers={},
        )
        assert (await b.read("buma_rupture", rupture["id"]))["balance"] == 13
    finally:
        if user:
            for table in (
                "buma_person_entity",
                "buma_customer",
                "buma_pdv",
                "buma_transaction",
                "buma_payment",
                "buma_caisse_transactions",
                "buma_buy",
            ):
                await b.track(table, author=user)
        await b.track("buma_rupture", number=prefix)
        for number in (prefix + "1", prefix + "2"):
            await b.track("buma_sim", number=number)
        await b.close()
    return b.trace


async def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--reference", default="http://127.0.0.1:8098")
    args = parser.parse_args()
    prefix = "C10R" + secrets.token_hex(4)
    reference = await scenario(Backend(args.reference, True, prefix))
    target = await scenario(Backend("http://127.0.0.1:8090", False, prefix))
    mismatches = {
        key: {"pocketbase": value, "ppbase": target.get(key)}
        for key, value in reference.items()
        if value != target.get(key)
    }
    report = {
        "timestamp_utc": datetime.now(UTC).isoformat(),
        "source_commit": "530018cd20c2acbe88f38b3639e500b5271e2eb2",
        "reference_version": "0.22.27",
        "status": "failed" if mismatches else "passed",
        "routes": 18,
        "comparisons": len(reference),
        "mismatches": mismatches,
        "responses_and_effects": reference,
        "cleanup": True,
    }
    (Path(__file__).parents[1] / "docs/c10-remaining-runtime-report.json").write_text(
        json.dumps(report, ensure_ascii=False, indent=2) + "\n"
    )
    assert not mismatches, json.dumps(mismatches, ensure_ascii=False, indent=2)
    print(
        f"C10 remaining: 18 routes, {len(reference)} paired response/effect comparisons, cleanup OK"
    )


if __name__ == "__main__":
    asyncio.run(main())
