#!/usr/bin/env python3
"""Isolated C9 HTTP/SQL proof; every fixture is removed even after assertion failure."""

from __future__ import annotations

import asyncio
import json
import re
import secrets
import sys
from datetime import UTC, datetime
from pathlib import Path

import httpx
from ppbase.core.id_generator import generate_id
from ppbase.services.auth_service import generate_token_key, hash_password
from sqlalchemy import text
from sqlalchemy.ext.asyncio import create_async_engine

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from app.commerce.returns import create  # noqa: E402

ROOT = Path(__file__).resolve().parents[1]
BASE_URL = "http://127.0.0.1:8090"
DATABASE_URL = "postgresql+asyncpg://ppbase:ppbase@localhost:55434/buma_master_ppbase"
# A harness replaying C9 elsewhere overrides these so the record stays exact.
TARGET = "local isolated PostgreSQL on 55434 and PPBase HTTP on 8090"
REPORT = ROOT / "docs/c9-runtime-report.json"


async def main() -> None:
    suffix = secrets.token_hex(4)
    name = f"C9{suffix}"
    user, role = generate_id(), generate_id()
    engine = create_async_engine(DATABASE_URL, connect_args={"ssl": "disable"})
    made: list[tuple[str, str]] = []
    covered = set()
    inventory = json.loads((ROOT / "docs/contracts/source-inventory.json").read_text())
    route_contracts = [
        r
        for r in inventory["routes"]
        if r["source_file"].split("/")[-1]
        in {"master.sale.ts", "master.purchase.ts", "master.retour.ts"}
        or (
            r["source_file"].endswith("master.caisse.ts")
            and (
                r["path"]
                in {
                    "/master/caisse/return",
                    "/master/caisse/versement_uv",
                    "/master/caisse/add_payment_to_sale",
                }
                or "/reportBy" in r["path"]
            )
        )
    ]
    cleanup_ok = False

    async def sql(query, params=None):
        async with engine.begin() as connection:
            result = await connection.execute(text(query), params or {})
            return [dict(row) for row in result.mappings()] if result.returns_rows else []

    async def get(table, rid):
        found = await sql(f'SELECT * FROM "{table}" WHERE id=:id', {"id": rid})
        assert found, (table, rid)
        return found[0]

    async def fixture(table, payload):
        result = await create(engine, table, payload)
        made.append((table, result["id"]))
        return result["id"]

    async def set_permissions(permission):
        await sql(
            "UPDATE roles SET permission=CAST(:p AS jsonb) WHERE id=:id",
            {"p": json.dumps(permission), "id": role},
        )

    try:
        now = datetime.now(UTC)
        await sql(
            "INSERT INTO roles (id,created,updated,name,permission,status) "
            "VALUES (:id,:now,:now,:name,CAST(:p AS jsonb),TRUE)",
            {"id": role, "now": now, "name": name, "p": json.dumps({"isAdmin": True})},
        )
        made.append(("roles", role))
        await sql(
            "INSERT INTO users (id,created,updated,email,email_visibility,verified,"
            "password_hash,token_key,role,name,username) VALUES "
            "(:id,:now,:now,:email,FALSE,TRUE,:pw,:key,:role,:name,:name)",
            {
                "id": user,
                "now": now,
                "email": name + "@example.com",
                "name": name,
                "pw": hash_password("abcde"),
                "key": generate_token_key(),
                "role": role,
            },
        )
        made.append(("users", user))
        customer = await fixture(
            "buma_customer",
            {
                "firstname": name,
                "lastname": "Client",
                "name": name,
                "kind": "CUSTOMER_PDV",
                "status": True,
                "author": user,
            },
        )
        target = await fixture(
            "buma_customer",
            {
                "firstname": name,
                "lastname": "Cible",
                "kind": "CUSTOMER_SIMPLE",
                "status": True,
                "author": user,
            },
        )
        supplier = await fixture(
            "buma_supplier", {"firstname": name, "name": name, "status": True, "author": user}
        )
        article = await fixture(
            "buma_article",
            {
                "designation": name,
                "sell_price": 10,
                "buy_price": 5,
                "trackStock": True,
                "status": True,
                "kind": "UV_PAANI",
            },
        )
        storage = await fixture(
            "buma_article_stockage", {"name": name, "status": True, "author": user}
        )
        stock = await fixture(
            "buma_article_stock",
            {
                "stockage": storage,
                "article": article,
                "quantity": 1000,
                "status": True,
                "author": user,
            },
        )
        stock2 = await fixture(
            "buma_article_stock",
            {
                "stockage": storage,
                "article": article,
                "quantity": 1000,
                "status": True,
                "author": user,
            },
        )
        agent = await fixture(
            "buma_agent",
            {
                "firstname": name,
                "lastname": "Agent",
                "name": name,
                "percent": 5,
                "status": True,
                "author": user,
            },
        )
        pdv = await fixture(
            "buma_pdv",
            {"name": name, "client": customer, "agent": agent, "status": True, "author": user},
        )
        await fixture("buma_customer_pdv", {"pdv": pdv, "customer": customer, "author": user})
        number = str(secrets.randbelow(80000000) + 10000000)
        sim = await fixture(
            "buma_sim",
            {
                "number": number,
                "network": "PAANI",
                "pdv": pdv,
                "status": True,
                "author": user,
                "isPaani": True,
                "operator_bonus_rate": 20,
            },
        )
        wallet = await fixture(
            "buma_wallet",
            {"name": name, "balance": 1000, "status": True, "author": user, "type": "OTHER"},
        )
        caisse = await fixture(
            "buma_caisse",
            {
                "name": name,
                "balance": 1000,
                "status": True,
                "state": "OPEN",
                "author": user,
                "currentCashier": user,
            },
        )
        session = await fixture(
            "buma_caisse_open_close",
            {
                "caisse": caisse,
                "state": "OPEN",
                "initial_amount": 1000,
                "status": True,
                "author": user,
            },
        )
        await sql(
            'UPDATE buma_caisse SET "currentSession"=:s WHERE id=:id', {"s": session, "id": caisse}
        )

        async with httpx.AsyncClient(base_url=BASE_URL, timeout=30) as client:
            login = await client.post(
                "/api/collections/users/auth-with-password",
                json={"identity": name, "password": "abcde"},
            )
            assert login.status_code == 200, login.text
            headers = {"Authorization": login.json()["token"]}

            async def request(method, path, body=None, status=200, *, anonymous=False):
                response = await client.request(
                    method, path, json=body, headers={} if anonymous else headers
                )
                assert response.status_code == status, (
                    method,
                    path,
                    status,
                    response.status_code,
                    response.text,
                )
                for contract in route_contracts:
                    pattern = re.sub(r":[^/]+", "[^/]+", contract["path"])
                    if contract["method"] == method and re.fullmatch(pattern, path.split("?")[0]):
                        covered.add((method, contract["path"]))
                if response.content:
                    assert response.headers.get("content-type", "").startswith(
                        "application/json"
                    ), response.headers
                    return response.json()
                return None

            async def post(path, body, status=200, **kwargs):
                return await request("POST", path, body, status, **kwargs)

            item = {
                "article": article,
                "article_stock": stock,
                "stockage": storage,
                "quantity": 2,
                "sell_price": 10,
                "buy_price": 5,
            }
            sale_body = {"customer": customer, "debt": True, "payed": 0, "items": [item]}
            assert await post("/master/sale/new", sale_body) == {"message": "Operation effectuee"}
            sale = (
                await sql(
                    "SELECT * FROM buma_sale WHERE author=:u ORDER BY created DESC LIMIT 1",
                    {"u": user},
                )
            )[0]
            assert sale["kind"] == "AUTRES" and sale["state"] == "" and sale["remaining"] == 20, (
                sale
            )
            assert (await get("buma_article_stock", stock))["quantity"] == 998
            assert (await get("buma_customer", customer))["solde"] == -20
            pay_body = {
                "saleId": sale["id"],
                "caisse": caisse,
                "amount": 7,
                "method": "PAYMENT_CASH_12",
            }
            paid = await post("/master/caisse/add_payment_to_sale", pay_body)
            assert paid["sale"] == {"id": sale["id"], "newPayed": 7, "newRemaining": 13}, paid
            before_bad = await get("buma_caisse", caisse)
            over = await post("/master/caisse/add_payment_to_sale", {**pay_body, "amount": 14}, 400)
            assert "dépasse" in over["message"]
            assert (await get("buma_caisse", caisse)) == before_bad
            race = await asyncio.gather(
                *[
                    client.post(
                        "/master/caisse/add_payment_to_sale",
                        json={**pay_body, "amount": 13},
                        headers=headers,
                    )
                    for _ in range(2)
                ]
            )
            assert sorted(r.status_code for r in race) == [200, 400], [r.text for r in race]
            assert (await get("buma_sale", sale["id"]))["payed"] == 20
            assert (await get("buma_customer", customer))["solde"] == 0
            assert await post(
                "/master/sale/check_caisse_transactions", {"saleId": sale["id"]}, anonymous=True
            ) == {"hasCaisseTransactions": True}
            assert await post("/master/sale/cancel", {"saleId": sale["id"]}) == {
                "message": "Enregistrement reussi !"
            }
            assert (await get("buma_customer", customer))["solde"] == 20
            assert (await get("buma_article_stock", stock))["quantity"] == 1000
            assert (await get("buma_caisse", caisse))["balance"] == 1020
            assert all(
                not p["status"]
                for p in await sql(
                    "SELECT status FROM buma_payment WHERE sale=:id", {"id": sale["id"]}
                )
            )
            assert all(
                r["status"]
                for r in await sql(
                    "SELECT status FROM buma_caisse_transactions WHERE sale=:id", {"id": sale["id"]}
                )
            )
            # Failure after a stock write must roll the aggregate back.
            snapshot = (await get("buma_article_stock", stock))["quantity"]
            await post(
                "/master/sale/new", {**sale_body, "items": [item, {**item, "quantity": "bad"}]}, 400
            )
            assert (await get("buma_article_stock", stock))["quantity"] == snapshot

            recharge_body = {
                "amount": 10,
                "sim": sim,
                "articleStock": stock,
                "transaction_date": now.isoformat(),
                "system_id": name + "recharge",
                "mobile_message_id": name + "sms",
                "sent": True,
            }
            races = await asyncio.gather(
                *[
                    client.post("/master/sale/recharge", json=recharge_body, headers=headers)
                    for _ in range(3)
                ]
            )
            assert all(r.status_code == 200 for r in races), [r.text for r in races]
            assert len({r.json()["id"] for r in races}) == 1
            recharge = await post("/master/sale/recharge", recharge_body)
            tx = await get("buma_transaction", recharge["id"])
            assert (
                tx["quantity"] == 10.5 and tx["credited_amount"] == 12 and tx["supplement"] == 0.5
            ), tx
            assert (await get("buma_article_stock", stock))["quantity"] == 989.5
            assert (await get("buma_customer", customer))["solde"] == 8
            assert await post("/master/sale/cancel_recharge", {"sale_id": tx["sale"]}) == {
                "message": "Enregistrement reussi !"
            }
            assert (await get("buma_article_stock", stock))["quantity"] == 1000
            assert (await get("buma_customer", customer))["solde"] == 20
            assert (await get("buma_sale", tx["sale"]))["state"] == ""
            again = await post("/master/sale/cancel_recharge", {"sale_id": tx["sale"]}, 400)
            assert again == {
                "message": "Impossible d'annuler le rechargement, Cette recharge a déjà été annulée"
            }

            purchase_body = {"supplier": supplier, "payed": 0, "received": False, "items": [item]}
            purchase = await post("/master/purchase/new", purchase_body)
            assert set(purchase) == {"id", "message"}
            pid = purchase["id"]
            assert (await get("buma_supplier", supplier))["solde"] == -10
            assert await post("/master/purchase/receive", {"buyId": pid}) == {
                "message": "Achat marqué comme reçu avec succès"
            }
            assert (await get("buma_article_stock", stock))["quantity"] == 1002
            payment = await post(
                "/master/purchase/add-payment", {"purchaseId": pid, "walletId": wallet, "amount": 4}
            )
            wtx = await get("buma_wallet_transactions", payment["transactionId"])
            assert wtx["state"] == "" and wtx["wallet_solde_before"] == 1000, wtx
            assert (await get("buma_wallet", wallet))["balance"] == 996
            assert (await get("buma_supplier", supplier))["solde"] == -6
            await post("/master/purchase/cancel", {"buyId": pid}, 400)
            await post("/master/purchase/cancel-wallet-transaction", {"id": wtx["id"]})
            assert (await get("buma_wallet", wallet))["balance"] == 1000
            assert (await get("buma_buy", pid))["payed"] == 0
            assert await post("/master/purchase/cancel", {"buyId": pid}) == {"message": "ok"}
            assert (await get("buma_article_stock", stock))["quantity"] == 1000
            assert (await get("buma_supplier", supplier))["solde"] == 0
            # Paid initial purchase snapshot is after the debit in the frozen source.
            paid_purchase = await post(
                "/master/purchase/new", {**purchase_body, "payed": 10, "wallet": wallet}
            )
            initial_tx = (
                await sql(
                    "SELECT * FROM buma_wallet_transactions WHERE purchase=:id",
                    {"id": paid_purchase["id"]},
                )
            )[0]
            assert initial_tx["wallet_solde_before"] == 990 and initial_tx["state"] == "DONE", (
                initial_tx
            )
            await post("/master/purchase/cancel-wallet-transaction", {"id": initial_tx["id"]})
            await post("/master/purchase/cancel", {"buyId": paid_purchase["id"]})

            async def new_return(amount=5):
                rid = generate_id()
                body = {
                    "articleStock": stock,
                    "sim_number": number,
                    "amount": str(amount),
                    "transaction_date": now.isoformat(),
                    "mobile_message_id": name + rid,
                    "transaction_message": "C9 proof",
                }
                result = await post("/master/retour/new", body)
                assert await post("/master/retour/new", body) == result
                transaction = await get("buma_transaction", result["id"])
                return transaction["return"], result["id"]

            rid, return_tx = await new_return()
            assert (await get("buma_customer", customer))["due"] == 5
            assert (await get("buma_article_stock", stock))["quantity"] == 1005
            info = await request("GET", f"/master/retour/info/{rid}")
            assert set(info) == {"pdv", "customer", "articleStock", "caisse", "returnRecord"}
            assert info["returnRecord"]["collectionName"] == "buma_return"
            assert info["returnRecord"]["pdv"] == pdv and info["caisse"] is None
            assert (
                await post(
                    "/master/retour/updatePDV", {"returnId": rid, "pdv": pdv, "pdv_number": number}
                )
                is None
            )
            assert await post(
                "/master/retour/update",
                {
                    "returnId": rid,
                    "articleStock": stock2,
                    "amount": 7,
                    "pdv": pdv,
                    "customer": customer,
                    "payed": False,
                },
            ) == {"message": "Retour mis à jour"}
            assert (await get("buma_article_stock", stock))["quantity"] == 1000
            assert (await get("buma_article_stock", stock2))["quantity"] == 1007
            # Cancellation uses the original transaction stock and does not undo due.
            assert await request("GET", f"/master/retour/cancel/{rid}") == {
                "message": "Retour annulé"
            }
            assert (await get("buma_article_stock", stock))["quantity"] == 993
            assert (await get("buma_customer", customer))["due"] == 5
            assert (await get("buma_transaction", return_tx))["status"] is True
            rid2, _ = await new_return(6)
            before = (await get("buma_caisse", caisse))["balance"]
            assert await post("/master/caisse/return", {"returnId": rid2, "caisse": caisse}) == {
                "message": "Ok"
            }
            assert (await get("buma_caisse", caisse))["balance"] == before - 6
            assert (await get("buma_return", rid2))["payed"] is True
            await post("/master/caisse/return", {"returnId": rid2, "caisse": caisse}, 400)
            rid3, _ = await new_return(8)
            uv = await post(
                "/master/caisse/versement_uv",
                {"returnId": rid3, "caisse": caisse, "target_customer": target, "note": "single"},
            )
            assert (
                set(uv) == {"success", "message", "caisse_transaction"}
                and uv["message"] == "Versement UV réussi"
            )
            assert (await get("buma_customer", target))["solde"] == 8
            assert (await get("buma_caisse", caisse))["balance"] == before - 6
            vtx = await get("buma_caisse_transactions", uv["caisse_transaction"])
            assert vtx["retour"] == rid3 and "| single" in vtx["note"]
            rid4, _ = await new_return(3)
            grouped = await post(
                "/master/caisse/versement_uv",
                {
                    "grouped": True,
                    "returnIds": [rid4, rid4],
                    "caisse": caisse,
                    "target_customer": target,
                },
            )
            assert (
                grouped["return_count"] == 1
                and grouped["amount"] == 3
                and grouped["message"] == "Versement UV groupé réussi"
            ), grouped
            assert (await get("buma_customer", target))["solde"] == 11
            assert (await get("buma_caisse_transactions", grouped["caisse_transaction"]))[
                "retour"
            ] == ""
            rid5, _ = await new_return(2)
            # Group failure rolls back the first return's paid flag and all transactions.
            n_before = len(
                await sql("SELECT id FROM buma_caisse_transactions WHERE author=:u", {"u": user})
            )
            await post(
                "/master/caisse/versement_uv",
                {
                    "grouped": True,
                    "returnIds": [rid5, rid4],
                    "caisse": caisse,
                    "target_customer": target,
                },
                400,
            )
            assert (await get("buma_return", rid5))["payed"] is False
            assert (
                len(
                    await sql(
                        "SELECT id FROM buma_caisse_transactions WHERE author=:u", {"u": user}
                    )
                )
                == n_before
            )

            # Native SDK-shaped reads, filter, expand and CRUD remain available.
            native = await request(
                "GET", f"/api/collections/buma_return/records/{rid5}?expand=customer,article_stock"
            )
            assert native["id"] == rid5 and native["expand"]["customer"]["id"] == customer
            listed = await request(
                "GET", "/api/collections/buma_return/records?filter=author%3D%22" + user + "%22"
            )
            assert listed["totalItems"] >= 5 and set(listed) >= {
                "page",
                "perPage",
                "totalPages",
                "totalItems",
                "items",
            }
            await request(
                "PATCH", f"/api/collections/buma_return/records/{rid5}", {"note": "native"}
            )
            assert (await get("buma_return", rid5))["note"] == "native"

            # Active sale/recharge fixtures for non-empty reports.
            await post("/master/sale/new", sale_body)
            await post(
                "/master/sale/recharge",
                {**recharge_body, "system_id": name + "other", "mobile_message_id": name + "other"},
            )
            period = {
                "startDate": now.date().isoformat(),
                "endDate": now.date().isoformat(),
                "filter": name,
            }
            for endpoint, extra in [
                ("sold_products_report", {}),
                ("sold_products_info", {}),
                ("product_sales_by_customer", {"articleStockId": stock}),
                ("customers_summary_by_period", {}),
                ("customers_summary_info_by_period", {}),
                ("customer_products_report", {"customerId": customer}),
                ("customers_products_report", {"customers": [customer]}),
            ]:
                result = await post("/master/sale/" + endpoint, {**period, **extra}, anonymous=True)
                if endpoint.endswith("info") or "info_by" in endpoint:
                    assert result["totalRevenue"] == 32, (endpoint, result)
                else:
                    assert result["items"], (endpoint, result)
                    if "page" in result:
                        assert result["page"] == 1 and result["perPage"] == 30
                assert (await post("/master/sale/" + endpoint, extra, 400, anonymous=True))[
                    "message"
                ].startswith("startDate")
            summary = await post(
                "/master/sale/customers_summary_info_by_period", period, anonymous=True
            )
            assert summary["totalMissing"] == summary["totalRevenue"], summary
            stats_payload = {
                **period,
                "endDate": now.date().isoformat() + "T23:59:59Z",
                "phoneNumber": number,
                "author": name,
                "network": "PAANI",
            }
            recharge_stats = await post("/master/sale/recharge_stats", stats_payload)
            assert recharge_stats["stats"]["total_amount"] == 12, recharge_stats
            retour_stats = await post("/master/retour/return_stats", stats_payload)
            assert set(retour_stats["stats"]) == {
                "total_count",
                "total_amount",
                "total_payed",
                "total_not_payed",
                "total_payed_count",
            }
            for endpoint in ("reportByExpenseType", "reportByCaisseType"):
                result = await request(
                    "GET",
                    f"/master/caisse/{endpoint}/{caisse}/2000-01-01/2100-01-01",
                    anonymous=endpoint == "reportByCaisseType",
                )
                assert set(result) == {"datas"}
            await request("GET", f"/master/retour/info/{generate_id()}", status=404)
            await post("/master/sale/new", sale_body, 401, anonymous=True)
            await set_permissions({"sale": {"create": False}, "retour": {"remove": True}})
            assert await post("/master/sale/new", sale_body, 403) == {
                "message": "Vous étes pas authorisé a effectuer une vente"
            }
            await request("GET", f"/master/retour/cancel/{rid2}", status=403)
            await set_permissions({"isAdmin": True})
            # Initial cash/partial sale preserves author-approved backdating on all records.
            dated = {
                **sale_body,
                "debt": True,
                "payed": 4,
                "caisse": caisse,
                "method": "PAYMENT_CASH_12",
                "created": "2001-02-03T04:05:06Z",
            }
            balance_before = (await get("buma_caisse", caisse))["balance"]
            customer_before = (await get("buma_customer", customer))["solde"]
            await post("/master/sale/new", dated)
            dated_sale = (
                await sql(
                    "SELECT * FROM buma_sale WHERE author=:u AND created=:d",
                    {"u": user, "d": datetime(2001, 2, 3, 4, 5, 6, tzinfo=UTC)},
                )
            )[0]
            assert dated_sale["payed"] == 4 and dated_sale["remaining"] == 16
            assert (await get("buma_caisse", caisse))["balance"] == balance_before + 4
            assert (await get("buma_customer", customer))["solde"] == customer_before - 16
            for table in ("buma_payment", "buma_caisse_transactions", "buma_transaction"):
                related = await sql(
                    f"SELECT created FROM {table} WHERE sale=:id", {"id": dated_sale["id"]}
                )
                assert related and all(r["created"] == dated_sale["created"] for r in related)

            # An unknown SIM uses the exact source Client Inconnu flow, with links.
            unknown_number = str(secrets.randbelow(80000000) + 10000000)
            unknown_payload = {
                "articleStock": stock,
                "sim_number": unknown_number,
                "amount": "4",
                "mobile_message_id": name + "unknown",
                "transaction_date": now.isoformat(),
            }
            unknown = await post("/master/retour/new", unknown_payload)
            unknown_tx = await get("buma_transaction", unknown["id"])
            unknown_return = await get("buma_return", unknown_tx["return"])
            unknown_customer = await get("buma_customer", unknown_return["customer"])
            unknown_pdv = await get("buma_pdv", unknown_return["pdv"])
            assert unknown_customer["firstname"] == "Client Inconnu"
            assert unknown_customer["kind"] == "CUSTOMER_PDV" and unknown_customer["due"] == 4
            assert unknown_customer["pdv"] == unknown_pdv["id"]
            assert unknown_pdv["client"] == unknown_customer["id"]
            assert (await get("buma_sim", unknown_tx["sim"]))["network"] == "PAANI"
            assert await post("/master/retour/new", unknown_payload) == unknown

            # Concurrent return requests have one transaction and one stock/due effect.
            retbody = {
                "articleStock": stock,
                "sim_number": number,
                "amount": "9",
                "mobile_message_id": name + "concurrent-return",
                "transaction_date": now.isoformat(),
            }
            stock_before = (await get("buma_article_stock", stock))["quantity"]
            due_before = (await get("buma_customer", customer))["due"]
            concurrent_returns = await asyncio.gather(
                *[
                    client.post("/master/retour/new", json=retbody, headers=headers)
                    for _ in range(3)
                ]
            )
            assert all(r.status_code == 200 for r in concurrent_returns), [
                r.text for r in concurrent_returns
            ]
            assert len({r.json()["id"] for r in concurrent_returns}) == 1
            assert (await get("buma_article_stock", stock))["quantity"] == stock_before + 9
            assert (await get("buma_customer", customer))["due"] == due_before + 9

            # Paid update, empty updatePDV success, and source-only fields ignored.
            update_rid, _ = await new_return(1)
            await post(
                "/master/retour/update",
                {
                    "returnId": update_rid,
                    "articleStock": stock2,
                    "amount": 2,
                    "pdv": pdv,
                    "customer": customer,
                    "caisse": caisse,
                    "payed": True,
                },
            )
            assert (await get("buma_return", update_rid))["payed"] is True
            assert (
                await post(
                    "/master/retour/updatePDV",
                    {"returnId": update_rid, "pdv": pdv, "pdv_number": "12345678"},
                )
                is None
            )
            assert "pdv_number" not in await get("buma_return", update_rid)

            # Negative permission flags and missing flags follow the two === false guards.
            await set_permissions({"retour": {"update": False}, "caisse": {"transaction": False}})
            await post("/master/retour/updatePDV", {"returnId": rid5, "pdv": pdv}, 403)
            await post("/master/caisse/return", {"returnId": rid5, "caisse": caisse}, 400)
            await set_permissions({})
            assert await post("/master/retour/updatePDV", {"returnId": rid5, "pdv": pdv}) is None
            assert await post("/master/caisse/return", {"returnId": rid5, "caisse": caisse}) == {
                "message": "Ok"
            }
            await set_permissions({"isAdmin": True})

            # NAFAMA receipt applies the configured percentage in both entry paths.
            config_rows = await sql("SELECT * FROM buma_config WHERE id='CONFIGURATION_1'")
            if not config_rows:
                await sql(
                    "INSERT INTO buma_config (id,created,updated,nafama_percent) "
                    "VALUES ('CONFIGURATION_1',NOW(),NOW(),4)"
                )
                made.append(("buma_config", "CONFIGURATION_1"))
                percent = 4
            else:
                percent = float(config_rows[0].get("nafama_percent") or 0)
            nafama = await fixture(
                "buma_article",
                {
                    "designation": name + "Nafama",
                    "kind": "NAFAMA",
                    "status": True,
                    "trackStock": True,
                },
            )
            nafama_stock = await fixture(
                "buma_article_stock",
                {
                    "article": nafama,
                    "stockage": storage,
                    "quantity": 0,
                    "status": True,
                    "author": user,
                },
            )
            nitem = {
                "article": nafama,
                "article_stock": nafama_stock,
                "stockage": storage,
                "quantity": 100,
                "buy_price": 1,
            }
            for received in (True, False):
                npurchase = await post(
                    "/master/purchase/new",
                    {"supplier": supplier, "items": [nitem], "payed": 0, "received": received},
                )
                if not received:
                    await post("/master/purchase/receive", {"buyId": npurchase["id"]})
                assert (await get("buma_article_stock", nafama_stock))["quantity"] == 100 + percent
                await post("/master/purchase/cancel", {"buyId": npurchase["id"]})
                assert (await get("buma_article_stock", nafama_stock))["quantity"] == 0

            # Native CRUD writes do not silently execute the custom-return aggregate.
            qty_before = (await get("buma_article_stock", stock))["quantity"]
            native_return = await request(
                "POST",
                "/api/collections/buma_return/records",
                {
                    "amount": 1,
                    "customer": customer,
                    "article_stock": stock,
                    "pdv": pdv,
                    "status": True,
                    "author": user,
                },
            )
            assert (await get("buma_article_stock", stock))["quantity"] == qty_before
            await request(
                "DELETE", "/api/collections/buma_return/records/" + native_return["id"], status=204
            )

            # Frozen source preserves prior saves after a forced late storage failure.
            trigger = "c9_failure_" + suffix
            await sql(f"""CREATE FUNCTION {trigger}() RETURNS trigger LANGUAGE plpgsql AS $$
                BEGIN IF NEW.author='{user}' AND NEW.quantity=13 THEN
                    RAISE EXCEPTION 'C9 forced late storage failure'; END IF; RETURN NEW; END $$""")
            try:
                await sql(
                    f"CREATE TRIGGER {trigger} BEFORE INSERT ON buma_transaction "
                    f"FOR EACH ROW EXECUTE FUNCTION {trigger}()"
                )
                before_stock = await get("buma_article_stock", stock)
                before_customer = await get("buma_customer", customer)
                before_sales = await sql("SELECT id FROM buma_sale WHERE author=:u", {"u": user})
                failure = await post(
                    "/master/sale/new",
                    {**sale_body, "items": [item, {**item, "quantity": 13}]},
                    400,
                )
                assert "C9 forced late storage failure" in failure["message"]
                # The second item's stock save precedes its failed transaction save.
                assert (await get("buma_article_stock", stock))["quantity"] == before_stock[
                    "quantity"
                ] - 15
                assert (await get("buma_customer", customer))["solde"] == int(
                    before_customer["solde"]
                ) - 150
                after_sales = await sql("SELECT id FROM buma_sale WHERE author=:u", {"u": user})
                assert len(after_sales) == len(before_sales) + 1
                prior_ids = {row["id"] for row in before_sales}
                partial_sale = next(row["id"] for row in after_sales if row["id"] not in prior_ids)
                partial_transactions = await sql(
                    "SELECT * FROM buma_transaction WHERE sale=:id", {"id": partial_sale}
                )
                assert len(partial_transactions) == 1 and partial_transactions[0]["quantity"] == 2
                assert (await get("buma_sale", partial_sale))["amount"] == 150
            finally:
                await sql(f"DROP TRIGGER IF EXISTS {trigger} ON buma_transaction")
                await sql(f"DROP FUNCTION IF EXISTS {trigger}()")

            supplier_before = (await get("buma_supplier", supplier))["solde"]
            invalid_wallet = await post(
                "/master/purchase/new", {**purchase_body, "payed": 4, "wallet": ""}, 400
            )
            assert invalid_wallet == {
                "message": "Caisse ou porte-monnaie non défini pour le paiement"
            }
            assert (await get("buma_supplier", supplier))["solde"] == int(supplier_before) - 6
            empty_purchase = await fixture(
                "buma_buy",
                {
                    "supplier": supplier,
                    "amount": 0,
                    "payed": 0,
                    "status": True,
                    "received": False,
                    "author": user,
                    "type": "BUY",
                },
            )
            assert await post("/master/purchase/receive", {"buyId": empty_purchase}, 400) == {
                "message": "Aucun article trouvé pour cet achat"
            }
            assert (await get("buma_buy", empty_purchase))["received"] is True
            assert await post("/master/purchase/receive", {"buyId": empty_purchase}, 400) == {
                "message": "Cet achat a déjà été marqué comme reçu"
            }

            # Identity preparation in newReturn precedes the stock transaction and
            # its own saves are sequential too: failed PDV insertion leaves customer.
            identity_trigger = "c9_identity_" + suffix
            customers_before = await sql(
                "SELECT id FROM buma_customer WHERE author=:u", {"u": user}
            )
            await sql(
                f"""CREATE FUNCTION {identity_trigger}() RETURNS trigger """
                f"""LANGUAGE plpgsql AS $$
                BEGIN IF NEW.author='{user}' THEN
                    RAISE EXCEPTION 'C9 forced identity failure';
                END IF; RETURN NEW; END $$"""
            )
            try:
                await sql(
                    f"CREATE TRIGGER {identity_trigger} BEFORE INSERT ON buma_pdv "
                    f"FOR EACH ROW EXECUTE FUNCTION {identity_trigger}()"
                )
                await post(
                    "/master/retour/new",
                    {
                        "articleStock": stock,
                        "sim_number": str(secrets.randbelow(80000000) + 10000000),
                        "amount": "3",
                        "mobile_message_id": name + "identity-failure",
                    },
                    400,
                )
                customers_after = await sql(
                    "SELECT id FROM buma_customer WHERE author=:u", {"u": user}
                )
                assert len(customers_after) == len(customers_before) + 1
            finally:
                await sql(f"DROP TRIGGER IF EXISTS {identity_trigger} ON buma_pdv")
                await sql(f"DROP FUNCTION IF EXISTS {identity_trigger}()")

            print(
                "C9 assertions: core flows, reports, permissions and native CRUD OK",
                flush=True,
            )
    finally:
        # All domain rows are scoped to this random fixture author, never a global prefix wipe.
        async with engine.begin() as connection:
            await connection.execute(
                text(
                    "DELETE FROM buma_customer_solder_history WHERE sale IN "
                    "(SELECT id FROM buma_sale WHERE author=:u) OR caisse_transaction IN "
                    "(SELECT id FROM buma_caisse_transactions WHERE author=:u)"
                ),
                {"u": user},
            )
            for table in (
                "buma_payment",
                "buma_transaction",
                "buma_caisse_transactions",
                "buma_wallet_transactions",
                "buma_buy_item",
                "buma_buy",
                "buma_sale",
                "buma_return",
                "buma_customer_pdv",
                "buma_sim",
                "buma_pdv",
                "buma_customer",
            ):
                await connection.execute(
                    text(f'DELETE FROM "{table}" WHERE author=:u'), {"u": user}
                )
            for table, rid in reversed(made):
                await connection.execute(text(f'DELETE FROM "{table}" WHERE id=:id'), {"id": rid})
            for table in (
                "buma_transaction",
                "buma_sale",
                "buma_buy",
                "buma_return",
                "buma_customer",
                "buma_sim",
                "buma_pdv",
            ):
                count = (
                    await connection.execute(
                        text(f'SELECT COUNT(*) FROM "{table}" WHERE author=:u'), {"u": user}
                    )
                ).scalar_one()
                assert count == 0, (table, count)
        cleanup_ok = True
        await engine.dispose()
        print("C9 cleanup=OK", flush=True)
    assert cleanup_ok
    assert len(covered) == 29, sorted(covered)
    report = {
        "timestamp_utc": datetime.now(UTC).isoformat(),
        "source_commit": inventory["source"]["commit"],
        "target": TARGET,
        "status": "passed",
        "routes_exercised": len(covered),
        "routes": [{"method": m, "path": p} for m, p in sorted(covered)],
        "proofs": [
            "legacy_envelopes",
            "permissions",
            "sale_backdating",
            "sale_payment_concurrency",
            "recharge_bonus_and_idempotency",
            "return_concurrency",
            "unknown_sim_identity",
            "purchase_payments_and_cancellation",
            "nafama_stock",
            "return_updates",
            "caisse_return_and_grouped_uv",
            "reports_nonempty",
            "native_crud_filter_expand",
            "legacy_partial_failure_persistence",
            "legacy_identity_preparation_partial_failure",
            "fixture_cleanup",
        ],
        "limitations": [
            "Frozen TypeScript compared statically; no side-by-side PocketBase server",
            "D-0009 corrected: legacy sequential save boundaries preserved",
        ],
        "cleanup": True,
    }
    REPORT.write_text(json.dumps(report, ensure_ascii=False, indent=2) + "\n")
    print(f"C9 runtime PASSED; {len(covered)} distinct called paths", flush=True)


if __name__ == "__main__":
    asyncio.run(main())
