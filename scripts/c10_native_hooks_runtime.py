#!/usr/bin/env python3
"""Native CRUD request hooks with isolated references and guaranteed cleanup."""

from __future__ import annotations

import asyncio
import json
import secrets
import sys
from datetime import UTC, datetime
from pathlib import Path

import httpx
from sqlalchemy import text
from sqlalchemy.ext.asyncio import create_async_engine

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from app.commerce.sequential import create, read, save  # noqa: E402


async def main():
    engine = create_async_engine(
        "postgresql+asyncpg://ppbase:ppbase@localhost:55434/buma_master_ppbase",
        connect_args={"ssl": "disable"},
    )
    name = "C10H" + secrets.token_hex(4)
    made = []

    async def fixture(table, data):
        row = await create(engine, table, data)
        made.append((table, row["id"]))
        return row["id"]

    try:
        role = await fixture(
            "roles", {"name": name, "permission": {"isAdmin": True}, "status": True}
        )
        user = await fixture(
            "users",
            {
                "email": name + "@example.com",
                "username": name,
                "name": name,
                "role": role,
                "password": "abcde",
                "passwordConfirm": "abcde",
            },
        )
        async with httpx.AsyncClient(base_url="http://127.0.0.1:8090", timeout=30) as client:
            login = await client.post(
                "/api/collections/users/auth-with-password",
                json={"identity": name, "password": "abcde"},
            )
            assert login.status_code == 200, login.text
            headers = {"Authorization": login.json()["token"]}

            async def native(method, table, body=None, identifier="", status=200):
                url = (
                    "/api/collections/"
                    + table
                    + "/records"
                    + ("/" + identifier if identifier else "")
                )
                result = await client.request(method, url, json=body, headers=headers)
                if method == "POST" and result.status_code == 200:
                    made.append((table, result.json()["id"]))
                assert result.status_code == status, (
                    method,
                    table,
                    result.status_code,
                    result.text,
                )
                return result.json() if result.content else None

            customer = await native(
                "POST",
                "buma_customer",
                {
                    "firstname": name,
                    "lastname": "Client",
                    "kind": "CUSTOMER_SIMPLE",
                    "status": False,
                    "author": "fake00000000000",
                },
            )
            assert (
                customer["author"] == user
                and customer["status"] is True
                and customer["customer_no"] > 0
            )
            caisse = await native(
                "POST",
                "buma_caisse",
                {"name": name, "status": False, "state": "OPEN", "author": "fake00000000000"},
            )
            assert (
                caisse["state"] == "CLOSE" and caisse["author"] == user and caisse["status"] is True
            )
            wallet = await native(
                "POST", "buma_wallet", {"name": name, "type": "OTHER", "status": False}
            )
            assert wallet["status"] is True and wallet["author"] == user
            employee = await fixture(
                "buma_employee", {"firstname": name, "status": True, "author": user, "solde": 100}
            )
            debt = await native(
                "POST",
                "buma_employee_debt",
                {"employee": employee, "amount": 20, "status": False, "state": "PAID"},
            )
            assert debt["author"] == user and debt["status"] is True and debt["state"] == "UNPAID"
            assert (await read(engine, "buma_employee", employee))["solde"] == 80
            for amount, expected_paid, expected_solde, expected_state in (
                (7, 7, 87, "UNPAID"),
                (13, 20, 100, "PAID"),
                (1, 21, 101, "PAID"),
            ):
                paid = await client.post(
                    "/master/employee/payDebt",
                    json={"debt": debt["id"], "amount": str(amount)},
                    headers=headers,
                )
                assert paid.status_code == 200 and paid.json() == {"message": "Debt paid"}, (
                    paid.text
                )
                row = await read(engine, "buma_employee_debt", debt["id"])
                assert (
                    row["paid"] == expected_paid
                    and row["status"] is False
                    and row["state"] == expected_state
                )
                assert (await read(engine, "buma_employee", employee))["solde"] == expected_solde
            await native(
                "POST", "buma_employee_debt", {"employee": employee, "amount": 0}, status=400
            )
            assert (await read(engine, "buma_employee", employee))["solde"] == 101
            # PocketBase 0.22.27 rejects invalid IDs without a persisted hook debit.
            await native(
                "POST",
                "buma_employee_debt",
                {"employee": employee, "amount": 20, "id": "bad"},
                status=400,
            )
            assert (await read(engine, "buma_employee", employee))["solde"] == 101
            trigger = "c10_hook_fail_" + secrets.token_hex(4)
            async with engine.begin() as connection:
                await connection.execute(
                    text(f"""CREATE FUNCTION {trigger}() RETURNS trigger
                    LANGUAGE plpgsql AS $$ BEGIN
                    IF NEW.employee='{employee}' THEN
                        RAISE EXCEPTION 'C10 hook save failure';
                    END IF;
                    RETURN NEW; END $$""")
                )
            try:
                async with engine.begin() as connection:
                    await connection.execute(
                        text(
                            f"CREATE TRIGGER {trigger} BEFORE INSERT "
                            f"ON buma_employee_debt FOR EACH ROW EXECUTE FUNCTION {trigger}()"
                        )
                    )
                await native(
                    "POST", "buma_employee_debt", {"employee": employee, "amount": 20}, status=400
                )
                assert (await read(engine, "buma_employee", employee))["solde"] == 101
            finally:
                async with engine.begin() as connection:
                    await connection.execute(
                        text(f"DROP TRIGGER IF EXISTS {trigger} ON buma_employee_debt")
                    )
                    await connection.execute(text(f"DROP FUNCTION IF EXISTS {trigger}()"))
            article = await fixture(
                "buma_article", {"designation": name, "kind": "UV_RS", "status": True}
            )
            storage = await fixture(
                "buma_article_stockage", {"name": name, "status": True, "author": user}
            )
            stock = await fixture(
                "buma_article_stock",
                {
                    "article": article,
                    "stockage": storage,
                    "quantity": 1,
                    "status": True,
                    "author": user,
                },
            )
            sim = await fixture(
                "buma_sim",
                {
                    "number": "8" + str(secrets.randbelow(10000000)).zfill(7),
                    "network": "RS",
                    "status": True,
                    "author": user,
                },
            )
            tx = await fixture(
                "buma_transaction",
                {
                    "sim": sim,
                    "article": article,
                    "article_stock": stock,
                    "type": "RECHARGE",
                    "quantity": 1,
                    "status": True,
                    "author": user,
                },
            )
            await save(engine, "buma_transaction", tx, {"status": False})
            denied = await native("PATCH", "buma_sim", {"number": "11111111"}, sim, status=400)
            assert "champs critiques" in denied["message"]
            changed = await native("PATCH", "buma_sim", {"network": "PAANI", "pdv": ""}, sim)
            assert changed["network"] == "PAANI"
            for table, rid, fragment in (
                ("buma_sim", sim, "Cette SIM"),
                ("buma_article", article, "Cet article"),
                ("buma_article_stockage", storage, "Ce stockage"),
                ("buma_employee", employee, "dettes"),
            ):
                denied = await native("DELETE", table, identifier=rid, status=400)
                assert fragment in denied["message"], denied
                assert await read(engine, table, rid)
            ctx = await fixture(
                "buma_caisse_transactions",
                {
                    "caisse": caisse["id"],
                    "customer": customer["id"],
                    "type": "VERSEMENT",
                    "amount": 1,
                    "status": True,
                    "author": user,
                },
            )
            denied = await native("DELETE", "buma_customer", identifier=customer["id"], status=400)
            assert "Ce client" in denied["message"]
            await save(engine, "buma_caisse_transactions", ctx, {"status": False})
            denied = await native("DELETE", "buma_caisse", identifier=caisse["id"], status=400)
            assert "Cette caisse" in denied["message"]
            supplier = await fixture(
                "buma_supplier", {"firstname": name, "status": True, "author": user}
            )
            buy = await fixture(
                "buma_buy", {"supplier": supplier, "type": "BUY", "status": True, "author": user}
            )
            await save(engine, "buma_buy", buy, {"status": False})
            denied = await native("DELETE", "buma_supplier", identifier=supplier, status=400)
            assert "Ce fournisseur" in denied["message"]
            wtx = await fixture(
                "buma_wallet_transactions",
                {
                    "wallet": wallet["id"],
                    "type": "DEPOSIT",
                    "amount": 1,
                    "status": True,
                    "author": user,
                },
            )
            denied = await native("DELETE", "buma_wallet", identifier=wallet["id"], status=400)
            assert "porte-feuille" in denied["message"]
            await save(engine, "buma_wallet_transactions", wtx, {"status": False})
            await native("DELETE", "buma_wallet", identifier=wallet["id"], status=204)
            agent = await fixture("buma_agent", {"firstname": name, "status": True, "author": user})
            await save(engine, "buma_customer", customer["id"], {"agent": agent})
            await save(engine, "buma_caisse_transactions", ctx, {"status": True})
            denied = await native("DELETE", "buma_agent", identifier=agent, status=400)
            assert "transactions actives" in denied["message"]
    finally:
        for table, rid in reversed(made):
            async with engine.begin() as connection:
                await connection.execute(text(f'DELETE FROM "{table}" WHERE id=:id'), {"id": rid})
        await engine.dispose()
    report = {
        "timestamp_utc": datetime.now(UTC).isoformat(),
        "status": "passed",
        "cleanup": True,
        "proofs": [
            "author_status_override",
            "caisse_initial_close",
            "employee_debt_and_partial_payment",
            "sim_protected_fields",
            "network_not_protected",
            "nine_delete_guards",
            "inactive_reference_semantics",
            "native_form_hook_rollback_matches_pocketbase",
        ],
    }
    (Path(__file__).parents[1] / "docs/c10-native-hooks-runtime-report.json").write_text(
        json.dumps(report, indent=2) + "\n"
    )
    print("C10 native hooks: create defaults, debt, SIM updates, deletion guards and cleanup OK")


if __name__ == "__main__":
    asyncio.run(main())
