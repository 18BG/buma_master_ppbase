"""caisse.ts handlers, preserving each independently committed DAO save."""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any

from ppbase.core.id_generator import generate_id
from ppbase.services.record_service import (
    delete_record,
    get_all_collections,
    get_record,
    resolve_collection,
)
from ppbase.services.record_storage_coordinator import run_record_storage_transaction
from sqlalchemy import text

from app.commerce.sequential import read, save
from app.legacy.stockage import parse_int


def permission(raw: dict) -> dict:
    result = {"canCancelTransaction": False}
    values = raw.get("caisse") or {}
    if raw.get("isAdmin"):
        result.update(isAdmin=True, canCreate=True, canDelete=True)
    if raw.get("isAdmin") or values.get("canManage") is True:
        result.update(
            dict.fromkeys(
                (
                    "canManage",
                    "canRead",
                    "canViewBalance",
                    "canMakeTransaction",
                    "canManageOptions",
                ),
                True,
            )
        )
    else:
        result["canManage"] = False
        for target, source in (
            ("canRead", "canRead"),
            ("canCreate", "canWrite"),
            ("canDelete", "canDelete"),
            ("canViewBalance", "canViewBalance"),
            ("canMakeTransaction", "canMakeTransaction"),
            ("canCancelTransaction", "canCancelTransaction"),
            ("canManageOptions", "manage_options"),
        ):
            result[target] = values.get(source) is True
    # Non-admin isAdmin is absent in the source, not false.
    return result


class LegacyNotFoundError(LookupError):
    """PocketBase maps an uncaught DAO sql.ErrNoRows to its 404 envelope."""


async def record(engine: Any, table: str, identifier: str):
    result = await read(engine, table, identifier or "")
    if result is None:
        raise LegacyNotFoundError("The requested resource wasn't found.")
    return result


async def rows(engine: Any, query: str, values: dict):
    async with engine.connect() as connection:
        result = await connection.execute(text(query), values)
        return [dict(row) for row in result.mappings()]


async def remove(engine: Any, table: str, identifier: str):
    async def delete_one(active):
        await delete_record(
            active,
            await resolve_collection(active, table),
            identifier,
            all_collections=await get_all_collections(active),
        )

    await run_record_storage_transaction(engine, delete_one)


async def insert_transaction(engine: Any, table: str, values: dict):
    """Internal Record.save bypasses form required/select validation.

    Only these two transaction collections are accepted; unknown source fields
    are dropped just as they are by PocketBase's Record schema projection.
    """
    if table not in {"buma_caisse_transactions", "buma_wallet_transactions"}:
        raise ValueError(table)
    collection = await resolve_collection(engine, table)
    fields = {f["name"]: f for f in collection.schema}
    columns = {key: value for key, value in values.items() if key in fields}
    for key, value in list(columns.items()):
        kind = fields[key]["type"]
        if kind == "number":
            columns[key] = float(value or 0)
        elif kind in {"text", "select", "relation"}:
            columns[key] = str(value or "")
    columns.update(id=generate_id(), created=datetime.now(UTC), updated=datetime.now(UTC))
    names = ",".join(f'"{name}"' for name in columns)
    params = ",".join(f":{name}" for name in columns)
    async with engine.begin() as connection:
        await connection.execute(
            text(f'INSERT INTO "{table}" ({names}) VALUES ({params})'), columns
        )
    return columns


def error(message: str, status=400, key="message"):
    return status, {key: message}


async def open_caisse(engine, data, author, role):
    if role.get("canManage") is False:
        return error("You are not allowed")
    caisse = await record(engine, "buma_caisse", data.get("caisse"))
    if caisse["state"] != "REQUEST_OPEN":
        return error("Caisse is not in opening")
    session = await record(engine, "buma_caisse_open_close", caisse["currentSession"])
    await save(
        engine,
        "buma_caisse_open_close",
        session["id"],
        {"state": "OPEN", "open_time": datetime.now(UTC)},
    )
    await save(engine, "buma_caisse", caisse["id"], {"state": "OPEN"})
    return {"message": "ok"}


async def reject_closing(engine, data, author, role):
    try:
        if role.get("canManage") is False:
            return error("You are not allowed")
        caisse = await record(engine, "buma_caisse", data.get("caisse"))
        if caisse["state"] != "REQUEST_CLOSE":
            return error("Caisse is not about to close")
        await save(engine, "buma_caisse", caisse["id"], {"state": "OPEN"})
    except Exception:
        pass


async def reject(engine, data, author, role):
    try:
        if role.get("canManage") is False:
            return error("You are not allowed")
        caisse = await record(engine, "buma_caisse", data.get("caisse"))
        if caisse["state"] != "REQUEST_OPEN":
            return error("Caisse is not requested")
        session = await record(engine, "buma_caisse_open_close", caisse["currentSession"])
        try:
            transactions = await rows(
                engine,
                "SELECT * FROM buma_caisse_transactions WHERE caisse=:c AND session=:s",
                {"c": caisse["id"], "s": session["id"]},
            )
        except Exception:
            transactions = []
        balance = int(caisse.get("balance") or 0)
        for tx in transactions:
            wallet = await record(engine, "buma_wallet", tx["wallet"])
            amount = int(tx.get("amount") or 0) * (1 if tx["type"] == "DEPOSIT" else -1)
            await save(
                engine,
                "buma_wallet",
                wallet["id"],
                {"balance": int(wallet.get("balance") or 0) + amount},
            )
            balance -= amount
            await remove(engine, "buma_caisse_transactions", tx["id"])
        try:
            transactions = await rows(
                engine,
                'SELECT * FROM buma_wallet_transactions WHERE "caisseSession"=:s',
                {"s": session["id"]},
            )
        except Exception:
            transactions = []
        for tx in transactions:
            await remove(engine, "buma_wallet_transactions", tx["id"])
        await remove(engine, "buma_caisse_open_close", session["id"])
        await save(
            engine,
            "buma_caisse",
            caisse["id"],
            {"state": "CLOSE", "currentSession": "", "currentCashier": "", "balance": balance},
        )
        return {"message": "ok"}
    except Exception:
        return None


async def reopen(engine, identifier, author, role):
    if role.get("canManage") is False:
        return error("Vous n'etes pas autoriser, permission de gerer necessaire")
    session = await record(engine, "buma_caisse_open_close", identifier)
    caisse = await record(engine, "buma_caisse", session["caisse"])
    if session["state"] != "CLOSE" or caisse["state"] != "CLOSE":
        return error("La caisse doit etre fermer!")
    await save(
        engine,
        "buma_caisse",
        caisse["id"],
        {"state": "OPEN", "currentCashier": author, "currentSession": session["id"]},
    )
    await save(engine, "buma_caisse_open_close", session["id"], {"state": "OPEN", "reopen": True})
    return {"message": "ok"}


async def withdrawal(engine, data, author, role):
    try:
        caisse = await record(engine, "buma_caisse", data.get("caisse"))
        session = await record(engine, "buma_caisse_open_close", caisse["currentSession"])
        if not role.get("canManage"):
            return error("Vous n'etes pas autoriser a faire sa", 403, "error")
        amount = float(data.get("amount") or 0)
        if amount < 0:
            return error("Montant invalide", 403, "error")
        if amount > int(caisse.get("balance") or 0):
            return error("Solde insuffisant", 403, "error")
        wallet = await record(engine, "buma_wallet", data.get("wallet"))
        wallet_tx = await insert_transaction(
            engine,
            "buma_wallet_transactions",
            {
                "author": author,
                "amount": data.get("amount"),
                "note": "de la caisse " + caisse["name"],
                "caisse": caisse["id"],
                "type": "CAISSE_WITHDRAWAL",
                "wallet": wallet["id"],
                "status": True,
                "wallet_solde_before": int(wallet.get("balance") or 0),
                # The source's invalid WHERE ORDER BY query always leaves the counter at 1.
                "transaction_no": 1,
            },
        )
        caisse_tx = await insert_transaction(
            engine,
            "buma_caisse_transactions",
            {
                "author": author,
                "amount": data.get("amount"),
                "note": data.get("note"),
                "caisse": caisse["id"],
                "type": "WITHDRAWAL",
                "wallet": wallet["id"],
                "walletTransaction": wallet_tx["id"],
                "session": session["id"],
                "status": True,
            },
        )
        amount = parse_int(data.get("amount"))
        await save(
            engine,
            "buma_wallet",
            wallet["id"],
            {"balance": int(wallet.get("balance") or 0) + amount},
        )
        await save(
            engine,
            "buma_caisse",
            caisse["id"],
            {"balance": int(caisse.get("balance") or 0) - amount},
        )
        return {
            "success": True,
            "message": "Transaction reussis",
            "wallet_transaction": wallet_tx["id"],
            "caisse_transaction": caisse_tx["id"],
        }
    except Exception:
        return None


async def cancel(engine, identifier, author, role):
    if not identifier:
        return error("No transaction id")
    if role.get("canCancelTransaction") is False and role.get("canManage") is False:
        return error("You are not allowed")
    tx = await record(engine, "buma_caisse_transactions", identifier)
    caisse = await record(engine, "buma_caisse", tx["caisse"])
    session = await record(engine, "buma_caisse_open_close", tx["session"])
    if tx["status"] is False:
        return error("Transaction is already cancelled")
    if session["state"] != "OPEN":
        return error("Caisse is not open")
    if caisse["currentCashier"] != author and not role.get("canManage"):
        raise ValueError("You are not the cashier")
    amount, balance = int(tx.get("amount") or 0), int(caisse.get("balance") or 0)
    changes = []
    if tx["type"] == "EXPENSE":
        balance += amount
    elif tx["type"] == "VERSEMENT":
        balance -= amount
        if tx.get("customer"):
            customer = await record(engine, "buma_customer", tx["customer"])
            changes.append(
                (
                    "buma_customer",
                    customer["id"],
                    {"solde": float(customer.get("solde") or 0) - float(tx.get("amount") or 0)},
                )
            )
    elif tx["type"] in {"DEPOSIT", "WITHDRAWAL"}:
        deposit = tx["type"] == "DEPOSIT"
        if deposit and balance < amount:
            return error("Solde insuffisant")
        balance += -amount if deposit else amount
        wallet = await record(engine, "buma_wallet", tx["wallet"])
        changes.append(
            (
                "buma_wallet",
                wallet["id"],
                {"balance": int(wallet.get("balance") or 0) + (amount if deposit else -amount)},
            )
        )
        if not deposit or tx.get("walletTransaction"):
            wallet_tx = await record(engine, "buma_wallet_transactions", tx["walletTransaction"])
            changes.append(
                (
                    "buma_wallet_transactions",
                    wallet_tx["id"],
                    {"state": "CANCELLED", "status": False} if deposit else {},
                )
            )
    else:
        return error("Type de transaction non supporter")
    for table, rid, values in changes:
        await save(engine, table, rid, values)
    await save(engine, "buma_caisse", caisse["id"], {"balance": balance})
    await save(
        engine, "buma_caisse_transactions", identifier, {"status": False, "state": "CANCELLED"}
    )
    return {"message": "ok"}


async def cancel_reversal(engine, identifier, author, role):
    if not identifier:
        return error("Pas de transaction")
    if role.get("isAdmin") is False:
        return error("Vous n'etes pas autoriser")
    tx = await record(engine, "buma_caisse_transactions", identifier)
    caisse = await record(engine, "buma_caisse", tx["caisse"])
    old_session = await record(engine, "buma_caisse_open_close", tx["session"])
    if old_session["state"] != "CLOSE":
        return error("Caisse n'est pas fermer")
    if tx["status"] is False:
        return error("Transaction a deja eter annuler")
    session = await record(engine, "buma_caisse_open_close", caisse["currentSession"])
    await save(
        engine,
        "buma_caisse",
        caisse["id"],
        {"balance": int(caisse.get("balance") or 0) + int(tx.get("amount") or 0)},
    )
    await save(engine, "buma_caisse_transactions", identifier, {"state": "CANCELLED"})
    await insert_transaction(
        engine,
        "buma_caisse_transactions",
        {
            "amount": int(tx.get("amount") or 0),
            "author": tx["author"],
            "caisse": tx["caisse"],
            "session": session["id"],
            "note": "Reversement de (" + (tx.get("note") or "") + ")",
            "type": "REVERSAL",
            "status": True,
        },
    )
    return {"message": "ok"}


async def serialized(engine, table, identifier):
    collection = await resolve_collection(engine, table)
    result = await get_record(engine, collection, identifier)
    if result is None:
        raise LegacyNotFoundError("The requested resource wasn't found.")
    return public_record(collection, result)


def public_record(collection, result):
    # PocketBase 0.22 Record.PublicExport applies emailVisibility even to
    # raw DAO records in custom handlers, and empty DateTime marshals as "".
    if collection.type == "auth" and not result.get("emailVisibility"):
        result.pop("email", None)
    for field in collection.schema:
        if field.get("type") == "date" and result.get(field["name"]) is None:
            result[field["name"]] = ""
    return result


async def transactions(engine, identifier, start, end):
    # SQLite compares the stored PB date strings lexically, even with JS Date.toString().
    result = await rows(
        engine,
        """SELECT id FROM buma_caisse_transactions
        WHERE caisse=:id AND status=TRUE
        AND to_char(created AT TIME ZONE 'UTC', 'YYYY-MM-DD HH24:MI:SS.MS"Z"') >= :start
        AND to_char(created AT TIME ZONE 'UTC', 'YYYY-MM-DD HH24:MI:SS.MS"Z"') <= :end""",
        {"id": identifier, "start": start, "end": end},
    )
    output = []
    for row in result:
        tx = await serialized(engine, "buma_caisse_transactions", row["id"])
        expanded = {}
        for field, table in (("expenseType", "buma_caisse_expense_type"), ("author", "users")):
            if tx.get(field):
                collection = await resolve_collection(engine, table)
                value = await get_record(engine, collection, tx[field])
                if value:
                    expanded[field] = public_record(collection, value)
        if expanded:
            tx["expand"] = expanded
        output.append(tx)
    return output


async def report(engine, identifier):
    session = await serialized(engine, "buma_caisse_open_close", identifier)
    cashier = await serialized(engine, "users", session["author"])
    caisse = await serialized(engine, "buma_caisse", session["caisse"])
    end = session.get("close_time") or datetime.now(UTC).strftime(
        "%a %b %d %Y %H:%M:%S GMT+0000 (UTC)"
    )
    return {
        "caisse": caisse,
        "report": session,
        "cashier": cashier,
        "transactions": await transactions(
            engine, caisse["id"], session.get("open_time") or "", end
        ),
    }


async def range_report(engine, identifier, start, end):
    return {
        "caisse": await serialized(engine, "buma_caisse", identifier),
        "transactions": await transactions(engine, identifier, start, end),
    }


async def expense_report(engine, identifier, start, end):
    try:
        # This handler interpolates its path parameters in the frozen source.
        # Preserve its query/error behavior; no new escaping or guard is added.
        result = await rows(
            engine,
            f"""SELECT SUM(b.amount) AS amount, COUNT(*) AS count,
            COALESCE(t.name, 'Autres') AS label FROM buma_caisse_transactions b
            LEFT JOIN buma_caisse_expense_type t ON t.id=b."expenseType"
            WHERE to_char(b.created AT TIME ZONE 'UTC', 'YYYY-MM-DD HH24:MI:SS.MS"Z"') >= '{start}'
            AND to_char(b.created AT TIME ZONE 'UTC', 'YYYY-MM-DD HH24:MI:SS.MS"Z"') <= '{end}'
            AND b.type='EXPENSE' AND b.caisse='{identifier}' AND b.status=TRUE GROUP BY label""",
            {},
        )
        if any(float(row["amount"]) != int(row["amount"]) for row in result):
            raise ValueError("DynamicModel integer scan error")
        return {"datas": [{**row, "amount": int(row["amount"])} for row in result]}
    except Exception:
        return 400, {"message": "Error", "error": {"value": {}}}
