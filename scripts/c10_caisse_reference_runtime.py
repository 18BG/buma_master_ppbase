#!/usr/bin/env python3
"""Paired caisse reference proof; complements PostgreSQL fault injection."""

from __future__ import annotations

import asyncio
import json
import secrets
from datetime import UTC, datetime
from pathlib import Path

from c10_remaining_runtime import Backend


def without_clock(value):
    if isinstance(value, dict):
        return {
            k: "<clock>"
            if k in {"created", "updated", "open_time", "close_time"} and v
            else without_clock(v)
            for k, v in value.items()
        }
    if isinstance(value, list):
        return [without_clock(v) for v in value]
    return value


async def scenario(b):
    user = caisse = wallet = ""
    try:
        role = await b.fixture(
            "roles", {"name": b.prefix, "status": True, "permission": {"isAdmin": True}}
        )
        user = await b.fixture(
            "users",
            {
                "username": b.prefix,
                "email": b.prefix + "@example.com",
                "name": b.prefix,
                "role": role,
                "password": "abcde123",
                "passwordConfirm": "abcde123",
            },
        )
        r = await b.client.post(
            "/api/collections/users/auth-with-password",
            json={"identity": b.prefix, "password": "abcde123"},
        )
        assert r.status_code == 200, r.text
        b.headers = {"Authorization": r.json()["token"]}
        caisse = await b.fixture(
            "buma_caisse",
            {
                "name": b.prefix,
                "state": "REQUEST_OPEN",
                "balance": 100,
                "currentCashier": user,
                "status": True,
                "author": user,
            },
        )
        session = await b.fixture(
            "buma_caisse_open_close",
            {"state": "PENDING", "caisse": caisse, "author": user, "status": True},
        )
        wallet = await b.fixture(
            "buma_wallet",
            {"name": b.prefix, "type": "OTHER", "status": True, "author": user, "balance": 50},
        )
        await b.update("buma_caisse", caisse, {"currentSession": session})

        async def call(label, path, data=None, status=200, *, method="POST", anonymous=False):
            return await b.call(
                label,
                "/api2/caisse/" + path,
                data,
                status,
                method=method,
                headers={} if anonymous else b.headers,
            )

        await call("anonymous_open", "openCaisse", {"caisse": caisse}, 401, anonymous=True)
        await b.update("roles", role, {"permission": {}})
        await call("denied_open", "openCaisse", {"caisse": caisse}, 400)
        await b.update("roles", role, {"permission": {"isAdmin": True}})
        await call("open", "openCaisse", {"caisse": caisse})
        await call("repeat_open", "openCaisse", {"caisse": caisse}, 400)
        await b.update("buma_caisse", caisse, {"state": "REQUEST_CLOSE"})
        await call("reject_closing", "reject_closing", {"caisse": caisse})
        await call("reject_closing_missing", "reject_closing", {"caisse": "missing"})
        await call("withdrawal_missing", "withdrawal", {"caisse": "missing"})
        tx = await call(
            "withdrawal",
            "withdrawal",
            {"caisse": caisse, "wallet": wallet, "amount": "7.9", "note": b.prefix},
        )
        for table, rid in (
            ("buma_caisse_transactions", tx["caisse_transaction"]),
            ("buma_wallet_transactions", tx["wallet_transaction"]),
        ):
            b.made.append((table, rid))
        await b.state(
            "withdrawal_wallet_fields",
            "buma_wallet_transactions",
            tx["wallet_transaction"],
            ["amount", "status", "state", "caisseSession", "transaction_no", "wallet_solde_before"],
        )
        await call(
            "cancel_withdrawal", "transaction/cancel/" + tx["caisse_transaction"], method="GET"
        )
        await call(
            "cancel_repeat",
            "transaction/cancel/" + tx["caisse_transaction"],
            status=400,
            method="GET",
        )
        await b.state("wallet_after_cancel", "buma_wallet", wallet, ["balance"])
        await b.state(
            "wallet_transaction_still_active",
            "buma_wallet_transactions",
            tx["wallet_transaction"],
            ["status", "state"],
        )

        expense_type = await b.fixture(
            "buma_caisse_expense_type", {"name": b.prefix, "status": True, "author": user}
        )
        expense = await b.fixture(
            "buma_caisse_transactions",
            {
                "caisse": caisse,
                "session": session,
                "type": "EXPENSE",
                "amount": 4,
                "status": True,
                "author": user,
                "expenseType": expense_type,
            },
        )
        await call("public_report", "report/" + session, method="GET", anonymous=True)
        await call("public_range", f"rangeReport/{caisse}/2000/2999", method="GET", anonymous=True)
        await call("grouped_expense", f"reportByExpenseType/{caisse}/2000/2999", method="GET")
        await b.update("buma_caisse_transactions", expense, {"amount": 4.5})
        # DynamicModel(0) is an int in the literal TS constructor on PB 0.22.
        await call(
            "fractional_expense",
            f"reportByExpenseType/{caisse}/2000/2999",
            status=400,
            method="GET",
        )
        await b.update("buma_caisse_transactions", expense, {"amount": 4})
        await call("missing_report", "report/missing", status=404, method="GET", anonymous=True)
        await call("cancel_expense", "transaction/cancel/" + expense, method="GET")
        await b.state("caisse_expense_compensated", "buma_caisse", caisse, ["balance"])

        customer = await b.fixture(
            "buma_customer", {"firstname": b.prefix, "solde": 20, "status": True, "author": user}
        )
        versement = await b.fixture(
            "buma_caisse_transactions",
            {
                "caisse": caisse,
                "session": session,
                "type": "VERSEMENT",
                "amount": 3.9,
                "customer": customer,
                "author": user,
                "status": True,
            },
        )
        await call("cancel_versement", "transaction/cancel/" + versement, method="GET")
        await b.state("customer_versement_compensated", "buma_customer", customer, ["solde"])
        deposit_wallet = await b.fixture(
            "buma_wallet_transactions",
            {
                "wallet": wallet,
                "amount": 5,
                "status": True,
                "author": user,
                "type": "CAISSE_DEPOSIT",
                "caisseSession": session,
            },
        )
        deposit = await b.fixture(
            "buma_caisse_transactions",
            {
                "caisse": caisse,
                "session": session,
                "type": "DEPOSIT",
                "amount": 5,
                "wallet": wallet,
                "walletTransaction": deposit_wallet,
                "status": True,
                "author": user,
            },
        )
        await call("cancel_deposit", "transaction/cancel/" + deposit, method="GET")
        await b.state(
            "deposit_wallet_cancelled",
            "buma_wallet_transactions",
            deposit_wallet,
            ["status", "state"],
        )
        await b.state("deposit_balances", "buma_caisse", caisse, ["balance"])
        await b.state("deposit_wallet_balance", "buma_wallet", wallet, ["balance"])

        old = await b.fixture(
            "buma_caisse_open_close",
            {"caisse": caisse, "state": "CLOSE", "author": user, "status": True},
        )
        reversal = await b.fixture(
            "buma_caisse_transactions",
            {
                "caisse": caisse,
                "session": old,
                "type": "EXPENSE",
                "amount": 2,
                "note": b.prefix,
                "author": user,
                "status": True,
            },
        )
        await b.update("roles", role, {"permission": {}})
        await call("non_admin_reversal", "transaction/cancel2/" + reversal, method="GET")
        await call("repeat_reversal", "transaction/cancel2/" + reversal, method="GET")
        await b.state(
            "reversal_original_still_active", "buma_caisse_transactions", reversal, ["status"]
        )
        await b.state("repeat_reversal_balance", "buma_caisse", caisse, ["balance"])
        await b.update("roles", role, {"permission": {"isAdmin": True}})
        await b.update("buma_caisse", caisse, {"state": "CLOSE"})
        await b.update("buma_caisse_open_close", session, {"state": "CLOSE"})
        await call("reopen", "openAgain/" + session, method="GET")
        await b.state("reopened_session", "buma_caisse_open_close", session, ["state", "reopen"])
        rejected = await b.fixture(
            "buma_caisse_open_close",
            {"caisse": caisse, "state": "PENDING", "author": user, "status": True},
        )
        await b.update("buma_caisse", caisse, {"state": "REQUEST_OPEN", "currentSession": rejected})
        await b.fixture(
            "buma_caisse_transactions",
            {
                "caisse": caisse,
                "session": rejected,
                "type": "DEPOSIT",
                "amount": 3,
                "wallet": wallet,
                "author": user,
                "status": False,
            },
        )
        await call("reject_inactive_transaction", "reject", {"caisse": caisse})
        await b.state(
            "rejected_caisse",
            "buma_caisse",
            caisse,
            ["balance", "state", "currentSession", "currentCashier"],
        )
    finally:
        if caisse:
            await b.track("buma_caisse_transactions", caisse=caisse)
        if wallet:
            await b.track("buma_wallet_transactions", wallet=wallet)
        await b.close()
    return without_clock(b.trace)


async def main():
    prefix = "C10CP" + secrets.token_hex(4)
    reference = await scenario(Backend("http://127.0.0.1:8098", True, prefix))
    target = await scenario(Backend("http://127.0.0.1:8090", False, prefix))
    mismatches = {
        key: {"pocketbase": value, "ppbase": target.get(key)}
        for key, value in reference.items()
        if target.get(key) != value
    }
    report = {
        "timestamp_utc": datetime.now(UTC).isoformat(),
        "status": "failed" if mismatches else "passed",
        "reference_version": "0.22.27",
        "routes": 10,
        "comparisons": len(reference),
        "mismatches": mismatches,
        "responses_and_effects": reference,
        "cleanup": True,
    }
    (Path(__file__).parents[1] / "docs/c10-caisse-reference-report.json").write_text(
        json.dumps(report, ensure_ascii=False, indent=2) + "\n"
    )
    assert not mismatches, json.dumps(mismatches, ensure_ascii=False, indent=2)
    print(f"C10 caisse reference: {len(reference)} paired comparisons, cleanup OK")


if __name__ == "__main__":
    asyncio.run(main())
