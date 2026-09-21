"""Atomic wallet/caisse balance operations from the frozen BUMA source."""

from __future__ import annotations

from typing import Any

from ppbase.services.record_service import create_record, resolve_collection
from ppbase.services.record_storage_coordinator import run_record_storage_transaction
from sqlalchemy import text


class FinanceValidationError(ValueError):
    """Raised for invalid wallet or caisse operations."""


async def _lock_rows(connection: Any, table: str, ids: list[str]) -> list[dict[str, Any]]:
    ordered = sorted(set(ids))
    rows = (
        (
            await connection.execute(
                text(f'SELECT * FROM "{table}" WHERE id = ANY(:ids) ORDER BY id FOR UPDATE'),
                {"ids": ordered},
            )
        )
        .mappings()
        .all()
    )
    if len(rows) != len(ordered):
        raise FinanceValidationError(f"{table} introuvable")
    return [dict(row) for row in rows]


async def _next_transaction_no(connection: Any, table: str) -> int:
    value = (
        await connection.execute(text(f'SELECT COALESCE(MAX("transaction_no"), 0) FROM "{table}"'))
    ).scalar_one()
    return int(value or 0) + 1


async def wallet_deposit(
    engine: Any, wallet_id: str, amount: float, author: str, note: str = ""
) -> dict[str, Any]:
    if amount < 0:
        raise FinanceValidationError("Montant non valide")

    async def operation(active: Any) -> dict[str, Any]:
        transactions = await resolve_collection(active, "buma_wallet_transactions")
        async with active.connect() as connection:
            wallet = (await _lock_rows(connection, "buma_wallet", [wallet_id]))[0]
            before = float(wallet.get("balance") or 0)
            await connection.execute(
                text('UPDATE "buma_wallet" SET balance=:balance, updated=NOW() WHERE id=:id'),
                {"balance": before + amount, "id": wallet_id},
            )
        record = await create_record(
            active,
            transactions,
            {
                "amount": amount,
                "author": author,
                "note": note or f"Depot de {amount}",
                "status": True,
                "wallet": wallet_id,
                "type": "DEPOSIT",
                "state": "DONE",
                "wallet_solde_before": before,
            },
        )
        return record

    return await run_record_storage_transaction(engine, operation)


async def wallet_withdraw(
    engine: Any, wallet_id: str, amount: float, author: str, note: str = ""
) -> dict[str, Any]:
    if amount < 0:
        raise FinanceValidationError("Montant non valide")

    async def operation(active: Any) -> dict[str, Any]:
        transactions = await resolve_collection(active, "buma_wallet_transactions")
        async with active.connect() as connection:
            wallet = (await _lock_rows(connection, "buma_wallet", [wallet_id]))[0]
            before = float(wallet.get("balance") or 0)
            await connection.execute(
                text('UPDATE "buma_wallet" SET balance=:balance, updated=NOW() WHERE id=:id'),
                {"balance": before - amount, "id": wallet_id},
            )
        return await create_record(
            active,
            transactions,
            {
                "amount": amount,
                "author": author,
                "note": note or f"Retrait de {amount}",
                "status": True,
                "wallet": wallet_id,
                "type": "WITHDRAWAL",
                "state": "DONE",
                "wallet_solde_before": before,
            },
        )

    return await run_record_storage_transaction(engine, operation)


async def wallet_transfer(
    engine: Any, from_wallet: str, to_wallet: str, amount: float, author: str, note: str = ""
) -> dict[str, Any]:
    if amount <= 0 or from_wallet == to_wallet:
        raise FinanceValidationError("Transfert invalide")

    async def operation(active: Any) -> dict[str, Any]:
        transactions = await resolve_collection(active, "buma_wallet_transactions")
        async with active.connect() as connection:
            rows = await _lock_rows(connection, "buma_wallet", [from_wallet, to_wallet])
            by_id = {row["id"]: row for row in rows}
            source = by_id[from_wallet]
            target = by_id[to_wallet]
            source_before = float(source.get("balance") or 0)
            target_before = float(target.get("balance") or 0)
            if source_before < amount:
                raise FinanceValidationError("Solde insuffisant")
            await connection.execute(
                text('UPDATE "buma_wallet" SET balance=:balance, updated=NOW() WHERE id=:id'),
                {"balance": source_before - amount, "id": from_wallet},
            )
            await connection.execute(
                text('UPDATE "buma_wallet" SET balance=:balance, updated=NOW() WHERE id=:id'),
                {"balance": target_before + amount, "id": to_wallet},
            )
            transaction_no = await _next_transaction_no(connection, "buma_wallet_transactions")
        sent = await create_record(
            active,
            transactions,
            {
                "amount": amount,
                "author": author,
                "note": note,
                "status": True,
                "wallet": from_wallet,
                "to_wallet": to_wallet,
                "type": "TRANSFER",
                "state": "DONE",
                "wallet_solde_before": source_before,
                "transaction_no": transaction_no,
            },
        )
        received = await create_record(
            active,
            transactions,
            {
                "amount": amount,
                "author": author,
                "note": f"Transfert reçu depuis {source.get('name', '')}",
                "status": True,
                "wallet": to_wallet,
                "to_wallet": from_wallet,
                "type": "TRANSFER",
                "state": "DONE",
                "wallet_solde_before": target_before,
                "transaction_no": transaction_no + 1,
                "link": sent["id"],
            },
        )
        async with active.connect() as connection:
            await connection.execute(
                text('UPDATE "buma_wallet_transactions" SET link=:link WHERE id=:id'),
                {"link": received["id"], "id": sent["id"]},
            )
        return {"sent": sent, "received": received}

    return await run_record_storage_transaction(engine, operation)


async def caisse_deposit(
    engine: Any, caisse_id: str, wallet_id: str, amount: float, author: str, note: str = ""
) -> dict[str, Any]:
    if amount < 0:
        raise FinanceValidationError("Montant invalide")
    return await _caisse_wallet_move(
        engine, caisse_id, wallet_id, amount, author, note, deposit=True
    )


async def caisse_withdraw(
    engine: Any, caisse_id: str, wallet_id: str, amount: float, author: str, note: str = ""
) -> dict[str, Any]:
    if amount < 0:
        raise FinanceValidationError("Montant invalide")
    return await _caisse_wallet_move(
        engine, caisse_id, wallet_id, amount, author, note, deposit=False
    )


async def _caisse_wallet_move(
    engine: Any,
    caisse_id: str,
    wallet_id: str,
    amount: float,
    author: str,
    note: str,
    *,
    deposit: bool,
) -> dict[str, Any]:
    async def operation(active: Any) -> dict[str, Any]:
        wallet_transactions = await resolve_collection(active, "buma_wallet_transactions")
        caisse_transactions = await resolve_collection(active, "buma_caisse_transactions")
        async with active.connect() as connection:
            rows = await _lock_rows(connection, "buma_wallet", [wallet_id])
            wallet = rows[0]
            caisse = (await _lock_rows(connection, "buma_caisse", [caisse_id]))[0]
            wallet_before = float(wallet.get("balance") or 0)
            caisse_before = float(caisse.get("balance") or 0)
            if deposit and wallet_before < amount:
                raise FinanceValidationError("Vous n'avez pas suffisament de solde")
            if not deposit and caisse_before < amount:
                raise FinanceValidationError("Solde insuffisant")
            wallet_after = wallet_before - amount if deposit else wallet_before + amount
            caisse_after = caisse_before + amount if deposit else caisse_before - amount
            await connection.execute(
                text('UPDATE "buma_wallet" SET balance=:v, updated=NOW() WHERE id=:id'),
                {"v": wallet_after, "id": wallet_id},
            )
            await connection.execute(
                text('UPDATE "buma_caisse" SET balance=:v, updated=NOW() WHERE id=:id'),
                {"v": caisse_after, "id": caisse_id},
            )
            wallet_no = await _next_transaction_no(connection, "buma_wallet_transactions")
            caisse_no = await _next_transaction_no(connection, "buma_caisse_transactions")
        wallet_type = "CAISSE_DEPOSIT" if deposit else "CAISSE_WITHDRAWAL"
        caisse_type = "DEPOSIT" if deposit else "WITHDRAWAL"
        wt = await create_record(
            active,
            wallet_transactions,
            {
                "amount": amount,
                "author": author,
                "note": note,
                "status": True,
                "wallet": wallet_id,
                "caisse": caisse_id,
                "type": wallet_type,
                "state": "DONE",
                "wallet_solde_before": wallet_before,
                "transaction_no": wallet_no,
            },
        )
        ct = await create_record(
            active,
            caisse_transactions,
            {
                "amount": amount,
                "author": author,
                "note": note,
                "caisse": caisse_id,
                "wallet": wallet_id,
                "walletTransaction": wt["id"],
                "type": caisse_type,
                "status": True,
                "previous_caisse_balance": caisse_before,
                "after_caisse_balance": caisse_after,
                "transaction_no": caisse_no,
            },
        )
        async with active.connect() as connection:
            await connection.execute(
                text('UPDATE "buma_wallet_transactions" SET caisse_transaction=:ct WHERE id=:wt'),
                {"ct": ct["id"], "wt": wt["id"]},
            )
        return {"wallet_transaction": wt, "caisse_transaction": ct}

    return await run_record_storage_transaction(engine, operation)


async def caisse_transfer(
    engine: Any,
    from_caisse: str,
    to_caisse: str,
    wallet_id: str,
    amount: float,
    author: str,
    note: str = "",
) -> dict[str, Any]:
    if from_caisse == to_caisse or amount <= 0:
        raise FinanceValidationError("Transfert invalide")

    async def operation(active: Any) -> dict[str, Any]:
        wallet_transactions = await resolve_collection(active, "buma_wallet_transactions")
        caisse_transactions = await resolve_collection(active, "buma_caisse_transactions")
        async with active.connect() as connection:
            rows = await _lock_rows(connection, "buma_caisse", [from_caisse, to_caisse])
            by_id = {row["id"]: row for row in rows}
            source = by_id[from_caisse]
            target = by_id[to_caisse]
            source_before = float(source.get("balance") or 0)
            target_before = float(target.get("balance") or 0)
            if source_before < amount:
                raise FinanceValidationError("Solde insuffisant")
            await connection.execute(
                text('UPDATE "buma_caisse" SET balance=:v, updated=NOW() WHERE id=:id'),
                {"v": source_before - amount, "id": from_caisse},
            )
            await connection.execute(
                text('UPDATE "buma_caisse" SET balance=:v, updated=NOW() WHERE id=:id'),
                {"v": target_before + amount, "id": to_caisse},
            )
            wallet_no = await _next_transaction_no(connection, "buma_wallet_transactions")
            caisse_no = await _next_transaction_no(connection, "buma_caisse_transactions")
        sent_wallet = await create_record(
            active,
            wallet_transactions,
            {
                "amount": amount,
                "author": author,
                "note": note,
                "status": True,
                "wallet": wallet_id,
                "caisse": from_caisse,
                "type": "CAISSE_WITHDRAWAL",
                "state": "DONE",
                "transaction_no": wallet_no,
            },
        )
        sent_caisse = await create_record(
            active,
            caisse_transactions,
            {
                "amount": amount,
                "author": author,
                "note": note,
                "caisse": from_caisse,
                "wallet": wallet_id,
                "walletTransaction": sent_wallet["id"],
                "type": "WITHDRAWAL",
                "status": True,
                "previous_caisse_balance": source_before,
                "after_caisse_balance": source_before - amount,
                "transaction_no": caisse_no,
            },
        )
        received_wallet = await create_record(
            active,
            wallet_transactions,
            {
                "amount": amount,
                "author": author,
                "note": note,
                "status": True,
                "wallet": wallet_id,
                "caisse": to_caisse,
                "type": "CAISSE_DEPOSIT",
                "state": "DONE",
                "transaction_no": wallet_no + 1,
            },
        )
        received_caisse = await create_record(
            active,
            caisse_transactions,
            {
                "amount": amount,
                "author": author,
                "note": note,
                "caisse": to_caisse,
                "wallet": wallet_id,
                "walletTransaction": received_wallet["id"],
                "type": "DEPOSIT",
                "status": True,
                "previous_caisse_balance": target_before,
                "after_caisse_balance": target_before + amount,
                "transaction_no": caisse_no + 1,
            },
        )
        async with active.connect() as connection:
            await connection.execute(
                text('UPDATE "buma_wallet_transactions" SET caisse_transaction=:ct WHERE id=:wt'),
                {"ct": sent_caisse["id"], "wt": sent_wallet["id"]},
            )
            await connection.execute(
                text('UPDATE "buma_wallet_transactions" SET caisse_transaction=:ct WHERE id=:wt'),
                {"ct": received_caisse["id"], "wt": received_wallet["id"]},
            )
        return {"withdraw": sent_caisse, "deposit": received_caisse}

    return await run_record_storage_transaction(engine, operation)
