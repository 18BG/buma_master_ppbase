#!/usr/bin/env python3
"""HTTP/PostgreSQL proof of caisse.ts quirks, with fixture-scoped cleanup."""

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
from app.legacy.caisse import insert_transaction  # noqa: E402


async def main():
    engine = create_async_engine(
        "postgresql+asyncpg://ppbase:ppbase@localhost:55434/buma_master_ppbase",
        connect_args={"ssl": "disable"},
    )
    name = "C10C" + secrets.token_hex(4)
    made, user, caisse, wallet = [], "", "", ""
    trigger = "c10_caisse_fail_" + secrets.token_hex(4)

    async def query(sql, params=None):
        async with engine.begin() as connection:
            result = await connection.execute(text(sql), params or {})
            return [dict(row) for row in result.mappings()] if result.returns_rows else []

    async def fixture(table, values):
        row = await create(engine, table, values)
        made.append((table, row["id"]))
        return row["id"]

    async def get(table, rid, field):
        return (await read(engine, table, rid))[field]

    async def transaction(kind, amount, session, **extra):
        row = await insert_transaction(
            engine,
            "buma_caisse_transactions",
            {
                "author": user,
                "caisse": caisse,
                "session": session,
                "status": True,
                "type": kind,
                "amount": amount,
                "note": name,
                **extra,
            },
        )
        return row["id"]

    async def role_values(value):
        await query(
            "UPDATE roles SET permission=CAST(:p AS jsonb) WHERE id=:r",
            {"p": json.dumps(value), "r": role},
        )

    try:
        role = await fixture(
            "roles", {"name": name, "status": True, "permission": {"isAdmin": True}}
        )
        user = await fixture(
            "users",
            {
                "name": name,
                "username": name,
                "email": name + "@example.com",
                "password": "abcde",
                "passwordConfirm": "abcde",
                "role": role,
            },
        )
        caisse = await fixture(
            "buma_caisse",
            {
                "name": name,
                "status": True,
                "state": "REQUEST_OPEN",
                "balance": 100,
                "currentCashier": user,
                "author": user,
            },
        )
        wallet = await fixture(
            "buma_wallet",
            {"name": name, "status": True, "type": "OTHER", "balance": 40, "author": user},
        )
        session = await fixture(
            "buma_caisse_open_close",
            {"caisse": caisse, "author": user, "status": True, "state": "CLOSE"},
        )
        await save(engine, "buma_caisse", caisse, {"currentSession": session})
        async with httpx.AsyncClient(base_url="http://127.0.0.1:8090", timeout=30) as client:
            login = await client.post(
                "/api/collections/users/auth-with-password",
                json={"identity": name, "password": "abcde"},
            )
            assert login.status_code == 200, login.text
            headers = {"Authorization": login.json()["token"]}

            async def call(method, path, data=None, status=200, anonymous=False):
                result = await client.request(
                    method, "/api2/caisse/" + path, json=data, headers={} if anonymous else headers
                )
                assert result.status_code == status, (path, result.status_code, result.text)
                if result.content:
                    assert result.headers["content-type"].startswith("application/json")
                    return result.json()
                assert "content-type" not in result.headers
                return None

            assert await call("POST", "openCaisse", {"caisse": caisse}, 401, True) == {
                "code": 401,
                "message": "The request requires admin or record authorization token to be set.",
                "data": {},
            }
            await role_values({})
            assert await call("POST", "openCaisse", {"caisse": caisse}, 400) == {
                "message": "You are not allowed"
            }
            await role_values({"caisse": {"canManage": True}})
            assert await call("POST", "openCaisse", {"caisse": caisse}) == {"message": "ok"}
            assert await get("buma_caisse", caisse, "state") == "OPEN"
            assert await get("buma_caisse_open_close", session, "state") == "OPEN"
            assert await get("buma_caisse_open_close", session, "open_time")
            assert await call("POST", "openCaisse", {"caisse": caisse}, 400) == {
                "message": "Caisse is not in opening"
            }
            await save(engine, "buma_caisse", caisse, {"state": "REQUEST_CLOSE"})
            assert await call("POST", "reject_closing", {"caisse": caisse}) is None
            assert await get("buma_caisse", caisse, "state") == "OPEN"
            assert await call("POST", "reject_closing", {"caisse": "missing"}) is None
            assert await call("POST", "withdrawal", {"caisse": "missing"}) is None

            # Fractional amount persists, but balances use parseInt/getInt.
            body = {"caisse": caisse, "wallet": wallet, "amount": "7.9", "note": name}
            result = await call("POST", "withdrawal", body)
            assert result["success"] is True and result["message"] == "Transaction reussis"
            wt, ct = result["wallet_transaction"], result["caisse_transaction"]
            assert await get("buma_wallet", wallet, "balance") == 47
            assert await get("buma_caisse", caisse, "balance") == 93
            assert await get("buma_wallet_transactions", wt, "amount") == 7.9
            assert await get("buma_wallet_transactions", wt, "transaction_no") == 1
            assert await get("buma_wallet_transactions", wt, "state") == ""
            assert await get("buma_wallet_transactions", wt, "caisseSession") == ""
            assert await call("GET", "transaction/cancel/" + ct) == {"message": "ok"}
            assert await get("buma_caisse", caisse, "balance") == 100
            assert await get("buma_wallet", wallet, "balance") == 40
            assert await get("buma_caisse_transactions", ct, "status") is False
            assert await get("buma_wallet_transactions", wt, "status") is True
            assert await call("GET", "transaction/cancel/" + ct, status=400) == {
                "message": "Transaction is already cancelled"
            }

            expense_type = await fixture("buma_caisse_expense_type", {"name": name, "status": True})
            expense = await transaction("EXPENSE", 5, session, expenseType=expense_type)
            await transaction("EXPENSE", 3, session)
            # No canRead check; the two report routes do not require authentication.
            await role_values({})
            report = await call("GET", "report/" + session, anonymous=True)
            assert set(report) == {"caisse", "cashier", "report", "transactions"}
            assert report["cashier"]["id"] == user and "password_hash" not in report["cashier"]
            assert "email" not in report["cashier"]
            assert report["report"]["id"] == session
            assert report["report"]["close_time"] == ""
            assert len(report["transactions"]) == 2
            expanded = next(t for t in report["transactions"] if t["id"] == expense)["expand"]
            assert "email" not in expanded["author"]
            assert (
                expanded["expenseType"]["id"] == expense_type and expanded["author"]["id"] == user
            )
            ranged = await call("GET", f"rangeReport/{caisse}/2000/2999", anonymous=True)
            assert set(ranged) == {"caisse", "transactions"} and len(ranged["transactions"]) == 2
            assert await call("GET", f"rangeReport/{caisse}/2998/2999", anonymous=True) == {
                "caisse": ranged["caisse"],
                "transactions": [],
            }
            grouped = await call("GET", f"reportByExpenseType/{caisse}/2000/2999")
            assert sorted(grouped["datas"], key=lambda x: x["amount"]) == [
                {"amount": 3, "count": 1, "label": "Autres"},
                {"amount": 5, "count": 1, "label": name},
            ]
            await call("GET", f"reportByExpenseType/{caisse}/2000/2999", status=401, anonymous=True)
            assert await call("GET", f"reportByExpenseType/{caisse}/2000'/2999", status=400) == {
                "message": "Error",
                "error": {"value": {}},
            }
            assert await call("GET", "transaction/cancel/" + expense, status=400) == {
                "message": "You are not allowed"
            }
            await role_values({"caisse": {"canCancelTransaction": True}})
            assert await call("GET", "transaction/cancel/" + expense) == {"message": "ok"}
            assert await get("buma_caisse", caisse, "balance") == 105

            # Reversal intentionally works for a non-admin, and retries credit again.
            old = await fixture(
                "buma_caisse_open_close",
                {"caisse": caisse, "author": user, "status": True, "state": "CLOSE"},
            )
            reversal = await transaction("EXPENSE", 4, old)
            await role_values({})
            for balance in (109, 113):
                assert await call("GET", "transaction/cancel2/" + reversal) == {"message": "ok"}
                assert await get("buma_caisse", caisse, "balance") == balance
            assert await get("buma_caisse_transactions", reversal, "status") is True
            reversals = await query(
                "SELECT * FROM buma_caisse_transactions WHERE caisse=:c AND type='REVERSAL'",
                {"c": caisse},
            )
            assert len(reversals) == 2 and all(
                t["session"] == session and t["author"] == user for t in reversals
            )
            await save(engine, "buma_caisse", caisse, {"state": "CLOSE"})
            await save(engine, "buma_caisse_open_close", session, {"state": "CLOSE"})
            await role_values({"isAdmin": True})
            assert await call("GET", "openAgain/" + session) == {"message": "ok"}
            assert await get("buma_caisse_open_close", session, "reopen") is True
            assert await get("buma_caisse", caisse, "currentCashier") == user

            # Reject reverses even inactive rows; source does not filter status.
            rejected = await fixture(
                "buma_caisse_open_close",
                {"caisse": caisse, "author": user, "status": True, "state": "CLOSE"},
            )
            await save(
                engine, "buma_caisse", caisse, {"state": "REQUEST_OPEN", "currentSession": rejected}
            )
            deposit = await transaction("DEPOSIT", 10, rejected, wallet=wallet, status=False)
            assert await call("POST", "reject", {"caisse": caisse}) == {"message": "ok"}
            assert await read(engine, "buma_caisse_transactions", deposit) is None
            assert await read(engine, "buma_caisse_open_close", rejected) is None
            assert await get("buma_caisse", caisse, "balance") == 103
            assert await get("buma_wallet", wallet, "balance") == 50
            assert await get("buma_caisse", caisse, "currentSession") == ""

            # Fault injection is restricted to this fixture, never other users.
            await save(
                engine, "buma_caisse", caisse, {"state": "REQUEST_OPEN", "currentSession": session}
            )
            await save(engine, "buma_caisse_open_close", session, {"state": "REQUEST_OPEN"})
            await query(
                f"CREATE FUNCTION {trigger}() RETURNS trigger LANGUAGE plpgsql AS $$ "
                f"BEGIN IF NEW.id='{caisse}' THEN "
                "RAISE EXCEPTION 'C10 injected late save'; END IF; RETURN NEW; END $$"
            )
            await query(
                f"CREATE TRIGGER {trigger} BEFORE UPDATE ON buma_caisse "
                f"FOR EACH ROW EXECUTE FUNCTION {trigger}()"
            )
            await call("POST", "openCaisse", {"caisse": caisse}, status=400)
            assert await get("buma_caisse_open_close", session, "state") == "OPEN"
            assert await get("buma_caisse", caisse, "state") == "REQUEST_OPEN"
            await query(f"DROP TRIGGER {trigger} ON buma_caisse")
            await query(f"DROP FUNCTION {trigger}()")
            await save(engine, "buma_caisse", caisse, {"state": "OPEN"})
            await query(
                f"CREATE FUNCTION {trigger}() RETURNS trigger LANGUAGE plpgsql AS $$ "
                f"BEGIN IF NEW.author='{user}' THEN "
                "RAISE EXCEPTION 'C10 injected late insert'; END IF; RETURN NEW; END $$"
            )
            await query(
                f"CREATE TRIGGER {trigger} BEFORE INSERT ON buma_caisse_transactions "
                f"FOR EACH ROW EXECUTE FUNCTION {trigger}()"
            )
            before = await query(
                "SELECT id FROM buma_wallet_transactions WHERE wallet=:w", {"w": wallet}
            )
            assert await call("POST", "withdrawal", body) is None
            after = await query(
                "SELECT id,transaction_no FROM buma_wallet_transactions WHERE wallet=:w",
                {"w": wallet},
            )
            assert len(after) == len(before) + 1 and all(t["transaction_no"] == 1 for t in after)
            assert await get("buma_wallet", wallet, "balance") == 50
            assert await get("buma_caisse", caisse, "balance") == 103
    finally:
        await query(f"DROP TRIGGER IF EXISTS {trigger} ON buma_caisse")
        await query(f"DROP TRIGGER IF EXISTS {trigger} ON buma_caisse_transactions")
        await query(f"DROP FUNCTION IF EXISTS {trigger}()")
        if caisse:
            await query("DELETE FROM buma_caisse_transactions WHERE caisse=:c", {"c": caisse})
        if wallet:
            await query("DELETE FROM buma_wallet_transactions WHERE wallet=:w", {"w": wallet})
        for table, rid in reversed(made):
            await query(f'DELETE FROM "{table}" WHERE id=:id', {"id": rid})
        for table, rid in made:
            assert await read(engine, table, rid) is None
        await engine.dispose()
    report = {
        "timestamp_utc": datetime.now(UTC).isoformat(),
        "status": "passed",
        "routes": 10,
        "proofs": [
            "permissions",
            "public_reports_with_expands",
            "open_reject_reopen",
            "withdrawal_fraction_and_counter",
            "cancel_wallet_stays_active",
            "repeat_non_admin_reversal",
            "inactive_reject_transaction",
            "partial_open_failure",
            "partial_withdrawal_failure",
        ],
        "cleanup": True,
    }
    (Path(__file__).parents[1] / "docs/c10-caisse-runtime-report.json").write_text(
        json.dumps(report, indent=2) + "\n"
    )
    print(
        "C10 caisse: 10 routes, permissions, reports, persistence, "
        "partial failures, retries, cleanup OK"
    )


if __name__ == "__main__":
    asyncio.run(main())
