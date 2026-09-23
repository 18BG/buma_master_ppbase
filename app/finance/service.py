"""Atomic wallet/caisse balance operations from the frozen BUMA source."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from typing import Any

from ppbase.core.id_generator import generate_id
from ppbase.models.record import format_datetime
from ppbase.services.record_service import create_record, resolve_collection
from ppbase.services.record_storage_coordinator import run_record_storage_transaction
from sqlalchemy import text


class FinanceValidationError(ValueError):
    """Raised for invalid wallet or caisse operations."""


class FinanceForbiddenError(FinanceValidationError):
    """Raised when source behavior returns HTTP 403 after record checks."""


class FinanceNotFoundError(FinanceValidationError):
    """Raised when source behavior returns HTTP 404."""


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
    await connection.execute(
        text("SELECT pg_advisory_xact_lock(hashtextextended(:key, 0))"),
        {"key": f"buma:transaction_no:{table}"},
    )
    value = (
        await connection.execute(
            text(
                f'SELECT COALESCE((SELECT "transaction_no" FROM "{table}" '
                'ORDER BY "created" DESC LIMIT 1), 0)'
            )
        )
    ).scalar_one()
    return int(value or 0) + 1


async def _current_caisse_session(
    connection: Any,
    caisse_id: str,
    *,
    allowed_caisse_states: tuple[str, ...] = ("OPEN", "REQUEST_CLOSE"),
    allowed_session_states: tuple[str, ...] = ("OPEN", "PENDING"),
) -> tuple[dict[str, Any], dict[str, Any]]:
    caisse = (await _lock_rows(connection, "buma_caisse", [caisse_id]))[0]
    if str(caisse.get("state") or "") not in allowed_caisse_states:
        raise FinanceValidationError("Caisse non ouverte")
    session_id = str(caisse.get("currentSession") or "")
    if not session_id:
        raise FinanceValidationError("Session non ouverte")
    session = (await _lock_rows(connection, "buma_caisse_open_close", [session_id]))[0]
    if str(session.get("state") or "") not in allowed_session_states:
        raise FinanceValidationError("Session non ouverte")
    return caisse, session


async def _reject_recent_duplicate(
    connection: Any,
    caisse_id: str,
    amount: float,
    transaction_type: str,
    *,
    seconds: int = 2,
) -> None:
    existing = (
        await connection.execute(
            text(
                'SELECT id FROM "buma_caisse_transactions" '
                "WHERE caisse=:caisse AND amount=:amount AND type=:type "
                "AND created>=:since AND status=TRUE LIMIT 1"
            ),
            {
                "caisse": caisse_id,
                "amount": amount,
                "type": transaction_type,
                "since": datetime.now(UTC) - timedelta(seconds=seconds),
            },
        )
    ).first()
    if existing is not None:
        raise FinanceValidationError(
            "Une transaction identique a été effectuée il y a moins de "
            f"{seconds} secondes. Veuillez patienter avant de réessayer."
        )


async def wallet_deposit(
    engine: Any,
    wallet_id: str,
    amount: float,
    author: str,
    note: str | None = None,
    file_path: str = "",
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
                "note": note if note is not None else f"Depot de {amount:g}",
                "status": True,
                "wallet": wallet_id,
                "type": "DEPOSIT",
                "state": "DONE",
                "file_path": file_path,
            },
        )
        return record

    return await run_record_storage_transaction(engine, operation)


async def wallet_withdraw(
    engine: Any,
    wallet_id: str,
    amount: float,
    author: str,
    note: str | None = None,
    file_path: str = "",
) -> dict[str, Any]:
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
                "note": note if note is not None else f"Retrait de {amount:g}",
                "status": True,
                "wallet": wallet_id,
                "type": "WITHDRAWAL",
                "state": "DONE",
                "file_path": file_path,
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
                "note": f"{note} | Transfer a {target.get('name', '')}",
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
                "note": f"{note} | Transfert reçu depuis {source.get('name', '')}",
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
        return {"message": "Transaction effectuée"}

    return await run_record_storage_transaction(engine, operation)


async def caisse_deposit(
    engine: Any,
    caisse_id: str,
    wallet_id: str,
    amount: float,
    author: str,
    note: str = "",
    wallet_note: str = "",
) -> dict[str, Any]:
    if amount < 0:
        raise FinanceValidationError("Montant invalide")
    return await _caisse_wallet_move(
        engine,
        caisse_id,
        wallet_id,
        amount,
        author,
        note,
        wallet_note,
        deposit=True,
    )


async def caisse_withdraw(
    engine: Any,
    caisse_id: str,
    wallet_id: str,
    amount: float,
    author: str,
    note: str = "",
    wallet_note: str = "",
) -> dict[str, Any]:
    if amount < 0:
        raise FinanceValidationError("Montant invalide")
    return await _caisse_wallet_move(
        engine,
        caisse_id,
        wallet_id,
        amount,
        author,
        note,
        wallet_note,
        deposit=False,
    )


async def _caisse_wallet_move(
    engine: Any,
    caisse_id: str,
    wallet_id: str,
    amount: float,
    author: str,
    note: str,
    wallet_note: str,
    *,
    deposit: bool,
) -> dict[str, Any]:
    async def operation(active: Any) -> dict[str, Any]:
        wallet_transactions = await resolve_collection(active, "buma_wallet_transactions")
        caisse_transactions = await resolve_collection(active, "buma_caisse_transactions")
        async with active.connect() as connection:
            rows = await _lock_rows(connection, "buma_wallet", [wallet_id])
            wallet = rows[0]
            caisse, session = await _current_caisse_session(connection, caisse_id)
            wallet_before = float(wallet.get("balance") or 0)
            caisse_before = float(caisse.get("balance") or 0)
            if deposit and wallet_before < amount:
                raise FinanceValidationError("Vous n'avez pas suffisament de solde")
            if not deposit and caisse_before < amount:
                raise FinanceValidationError("Solde insuffisant")
            await _reject_recent_duplicate(
                connection,
                caisse_id,
                amount,
                "DEPOSIT" if deposit else "WITHDRAWAL",
            )
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
                "note": wallet_note
                or (
                    f"Sur la caisse {caisse.get('name', '')}"
                    if deposit
                    else f"de la caisse {caisse.get('name', '')}"
                ),
                "status": True,
                "wallet": wallet_id,
                "caisse": caisse_id,
                "type": wallet_type,
                "state": "DONE",
                "caisseSession": session["id"],
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
                "session": session["id"],
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
        return {
            "success": True,
            "message": "Transaction reussis",
            "wallet_transaction": wt["id"],
            "caisse_transaction": ct["id"],
        }

    return await run_record_storage_transaction(engine, operation)


async def caisse_transfer(
    engine: Any,
    from_caisse: str,
    to_caisse: str,
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
            config = (
                (
                    await connection.execute(
                        text(
                            'SELECT transfer_wallet FROM "buma_config" '
                            "WHERE id='CONFIGURATION_1' LIMIT 1"
                        )
                    )
                )
                .mappings()
                .first()
            )
            wallet_id = str(config.get("transfer_wallet") or "") if config else ""
            if not wallet_id:
                raise FinanceValidationError("Portefeuille de transfert non configuré")
            await connection.execute(
                text("SELECT pg_advisory_xact_lock(hashtextextended(:key, 0))"),
                {"key": "buma:caisse_transfer:" + ":".join(sorted([from_caisse, to_caisse]))},
            )
            wallet = (await _lock_rows(connection, "buma_wallet", [wallet_id]))[0]
            source, source_session = await _current_caisse_session(connection, from_caisse)
            target, target_session = await _current_caisse_session(connection, to_caisse)
            source_before = float(source.get("balance") or 0)
            target_before = float(target.get("balance") or 0)
            if source_before < amount:
                raise FinanceValidationError("Solde insuffisant")
            await _reject_recent_duplicate(connection, from_caisse, amount, "WITHDRAWAL")
            await _reject_recent_duplicate(connection, to_caisse, amount, "DEPOSIT")
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
            wallet_before = float(wallet.get("balance") or 0)
        additional_note = (
            f"Transfert de {amount:g} FCFA de la caisse {source.get('name', '')} "
            f"a la caisse {target.get('name', '')}"
        )
        wallet_note = (
            f"Transfert de {amount:g} FCFA entre la caisse "
            f"{source.get('name', '')} et la caisse {target.get('name', '')}"
        )
        caisse_note = f"{note} | {additional_note}"
        sent_wallet = await create_record(
            active,
            wallet_transactions,
            {
                "amount": amount,
                "author": author,
                "note": wallet_note,
                "status": True,
                "wallet": wallet_id,
                "caisse": from_caisse,
                "type": "CAISSE_WITHDRAWAL",
                "state": "DONE",
                "caisseSession": source_session["id"],
                "wallet_solde_before": wallet_before,
                "transaction_no": wallet_no,
            },
        )
        sent_caisse = await create_record(
            active,
            caisse_transactions,
            {
                "amount": amount,
                "author": author,
                "note": caisse_note,
                "caisse": from_caisse,
                "wallet": wallet_id,
                "walletTransaction": sent_wallet["id"],
                "type": "WITHDRAWAL",
                "session": source_session["id"],
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
                "note": wallet_note,
                "status": True,
                "wallet": wallet_id,
                "caisse": to_caisse,
                "type": "CAISSE_DEPOSIT",
                "state": "DONE",
                "caisseSession": target_session["id"],
                "wallet_solde_before": wallet_before + amount,
                "transaction_no": wallet_no + 1,
            },
        )
        received_caisse = await create_record(
            active,
            caisse_transactions,
            {
                "amount": amount,
                "author": author,
                "note": caisse_note,
                "caisse": to_caisse,
                "wallet": wallet_id,
                "walletTransaction": received_wallet["id"],
                "type": "DEPOSIT",
                "session": target_session["id"],
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
            await connection.execute(
                text('UPDATE "buma_caisse_transactions" SET transfer_caisse=:linked WHERE id=:id'),
                {"linked": received_caisse["id"], "id": sent_caisse["id"]},
            )
            await connection.execute(
                text('UPDATE "buma_caisse_transactions" SET transfer_caisse=:linked WHERE id=:id'),
                {"linked": sent_caisse["id"], "id": received_caisse["id"]},
            )
        return {
            "success": True,
            "message": "Transfert réussi",
            "withdraw_transaction": sent_caisse["id"],
            "deposit_transaction": received_caisse["id"],
        }

    return await run_record_storage_transaction(engine, operation)


async def wallet_cancel_transaction(engine: Any, transaction_id: str) -> dict[str, str]:
    if not transaction_id:
        raise FinanceValidationError("Transaction introuvable")

    async def operation(active: Any) -> dict[str, str]:
        async with active.connect() as connection:
            transaction = (
                (
                    await connection.execute(
                        text('SELECT * FROM "buma_wallet_transactions" WHERE id=:id FOR UPDATE'),
                        {"id": transaction_id},
                    )
                )
                .mappings()
                .first()
            )
            if transaction is None:
                raise FinanceValidationError("Transaction introuvable")
            if str(transaction.get("state") or "") == "CANCELLED":
                raise FinanceValidationError("Transaction déjà annulée")

            transaction = dict(transaction)
            wallet_id = str(transaction.get("wallet") or "")
            wallet = (await _lock_rows(connection, "buma_wallet", [wallet_id]))[0]
            amount = float(transaction.get("amount") or 0)
            tx_type = str(transaction.get("type") or "")
            wallet_balance = float(wallet.get("balance") or 0)

            if tx_type == "DEPOSIT":
                caisse_transaction_id = str(transaction.get("caisse_transaction") or "")
                if caisse_transaction_id:
                    caisse_transaction = (
                        (
                            await connection.execute(
                                text(
                                    'SELECT type FROM "buma_caisse_transactions" '
                                    "WHERE id=:id LIMIT 1"
                                ),
                                {"id": caisse_transaction_id},
                            )
                        )
                        .mappings()
                        .first()
                    )
                    if caisse_transaction and caisse_transaction.get("type") == "BANK_VERSEMENT":
                        raise FinanceValidationError(
                            "Impossible d'annuler une transaction de versement bancaire"
                        )
                wallet_balance -= amount
            elif tx_type == "WITHDRAWAL":
                wallet_balance += amount
            elif tx_type == "TRANSFER":
                other_id = str(transaction.get("to_wallet") or "")
                wallets = await _lock_rows(connection, "buma_wallet", [wallet_id, other_id])
                by_id = {str(row["id"]): row for row in wallets}
                wallet = by_id[wallet_id]
                other = by_id[other_id]
                received = "Transfert reçu depuis" in str(transaction.get("note") or "")
                wallet_after = float(wallet.get("balance") or 0) + (-amount if received else amount)
                other_after = float(other.get("balance") or 0) + (amount if received else -amount)
                await connection.execute(
                    text('UPDATE "buma_wallet" SET balance=:v, updated=NOW() WHERE id=:id'),
                    {"v": wallet_after, "id": wallet_id},
                )
                await connection.execute(
                    text('UPDATE "buma_wallet" SET balance=:v, updated=NOW() WHERE id=:id'),
                    {"v": other_after, "id": other_id},
                )
                link = str(transaction.get("link") or "")
                if link:
                    await connection.execute(
                        text(
                            'UPDATE "buma_wallet_transactions" '
                            "SET state='CANCELLED', status=FALSE, updated=NOW() "
                            "WHERE id=:id"
                        ),
                        {"id": link},
                    )
            elif tx_type in {"CAISSE_DEPOSIT", "CAISSE_WITHDRAWAL"}:
                caisse_id = str(transaction.get("caisse") or "")
                caisse = (await _lock_rows(connection, "buma_caisse", [caisse_id]))[0]
                caisse_after = float(caisse.get("balance") or 0)
                if tx_type == "CAISSE_DEPOSIT":
                    wallet_balance += amount
                    caisse_after -= amount
                else:
                    wallet_balance -= amount
                    caisse_after += amount
                await connection.execute(
                    text('UPDATE "buma_caisse" SET balance=:v, updated=NOW() WHERE id=:id'),
                    {"v": caisse_after, "id": caisse_id},
                )
                caisse_transaction_id = str(transaction.get("caisse_transaction") or "")
                if caisse_transaction_id:
                    await connection.execute(
                        text(
                            'UPDATE "buma_caisse_transactions" '
                            "SET status=FALSE, updated=NOW() WHERE id=:id"
                        ),
                        {"id": caisse_transaction_id},
                    )
            else:
                raise FinanceValidationError("Type de transaction non pris en charge")

            if tx_type != "TRANSFER":
                await connection.execute(
                    text('UPDATE "buma_wallet" SET balance=:v, updated=NOW() WHERE id=:id'),
                    {"v": wallet_balance, "id": wallet_id},
                )
            await connection.execute(
                text(
                    'UPDATE "buma_wallet_transactions" '
                    "SET state='CANCELLED', status=FALSE, updated=NOW() "
                    "WHERE id=:id"
                ),
                {"id": transaction_id},
            )
        return {"message": "Transaction annulée"}

    return await run_record_storage_transaction(engine, operation)


async def wallet_update_note(engine: Any, transaction_id: str, note: str | None) -> dict[str, str]:
    if not transaction_id:
        raise FinanceValidationError("ID de transaction requis")
    if note is None:
        raise FinanceValidationError("Note requise")

    async def operation(active: Any) -> dict[str, str]:
        async with active.connect() as connection:
            transaction = (
                (
                    await connection.execute(
                        text(
                            'SELECT status, state FROM "buma_wallet_transactions" '
                            "WHERE id=:id FOR UPDATE"
                        ),
                        {"id": transaction_id},
                    )
                )
                .mappings()
                .first()
            )
            if transaction is None:
                raise FinanceValidationError("Transaction introuvable")
            if not transaction.get("status") or transaction.get("state") == "CANCELLED":
                raise FinanceValidationError("Cette transaction a été annulée")
            await connection.execute(
                text(
                    'UPDATE "buma_wallet_transactions" SET note=:note, updated=NOW() WHERE id=:id'
                ),
                {"note": note, "id": transaction_id},
            )
        return {"message": "Note mise à jour avec succès"}

    return await run_record_storage_transaction(engine, operation)


async def caisse_request_open(engine: Any, caisse_id: str, author: str) -> dict[str, str]:
    async def operation(active: Any) -> dict[str, str]:
        sessions = await resolve_collection(active, "buma_caisse_open_close")
        async with active.connect() as connection:
            caisse = (await _lock_rows(connection, "buma_caisse", [caisse_id]))[0]
            if str(caisse.get("state") or "") != "CLOSE":
                raise FinanceValidationError("Caisse n'est pas fermer")
        session = await create_record(
            active,
            sessions,
            {
                "caisse": caisse_id,
                "state": "PENDING",
                "initial_amount": float(caisse.get("balance") or 0),
                "cashier": author,
                "status": True,
                "open_time": datetime.now(UTC),
                "author": author,
            },
        )
        async with active.connect() as connection:
            await connection.execute(
                text(
                    "UPDATE \"buma_caisse\" SET state='REQUEST_OPEN', "
                    '"currentCashier"=:author, "currentSession"=:session, '
                    "updated=NOW() WHERE id=:id"
                ),
                {"author": author, "session": session["id"], "id": caisse_id},
            )
        return {"message": "Requete envoyer"}

    return await run_record_storage_transaction(engine, operation)


async def caisse_open(engine: Any, caisse_id: str) -> dict[str, str]:
    async def operation(active: Any) -> dict[str, str]:
        async with active.connect() as connection:
            caisse = (await _lock_rows(connection, "buma_caisse", [caisse_id]))[0]
            if str(caisse.get("state") or "") != "REQUEST_OPEN":
                raise FinanceValidationError("Caisse n'a pas de demande d'ouverture")
            session_id = str(caisse.get("currentSession") or "")
            if not session_id:
                raise FinanceValidationError("Session non ouverte")
            session = (await _lock_rows(connection, "buma_caisse_open_close", [session_id]))[0]
            if str(session.get("state") or "") not in {"PENDING", "OPEN"}:
                raise FinanceValidationError("Session non ouverte")
            await connection.execute(
                text(
                    'UPDATE "buma_caisse_open_close" '
                    "SET state='OPEN', open_time=NOW(), updated=NOW() WHERE id=:id"
                ),
                {"id": session_id},
            )
            await connection.execute(
                text("UPDATE \"buma_caisse\" SET state='OPEN', updated=NOW() WHERE id=:id"),
                {"id": caisse_id},
            )
        return {"message": "Ok"}

    return await run_record_storage_transaction(engine, operation)


async def caisse_request_close(
    engine: Any, caisse_id: str, author: str, *, is_admin: bool
) -> dict[str, str]:
    async def operation(active: Any) -> dict[str, str]:
        async with active.connect() as connection:
            caisse = (await _lock_rows(connection, "buma_caisse", [caisse_id]))[0]
            if str(caisse.get("state") or "") != "OPEN":
                raise FinanceValidationError("Caisse n'est pas ouverte")
            if str(caisse.get("currentCashier") or "") != author and not is_admin:
                raise FinanceForbiddenError("Vous n'etes pas autoriser")
            await connection.execute(
                text(
                    "UPDATE \"buma_caisse\" SET state='REQUEST_CLOSE', updated=NOW() WHERE id=:id"
                ),
                {"id": caisse_id},
            )
        return {"message": "Requete envoyer"}

    return await run_record_storage_transaction(engine, operation)


async def caisse_close(engine: Any, caisse_id: str, validator: str) -> dict[str, str]:
    async def operation(active: Any) -> dict[str, str]:
        async with active.connect() as connection:
            caisse, session = await _current_caisse_session(
                connection,
                caisse_id,
                allowed_caisse_states=("REQUEST_CLOSE",),
                allowed_session_states=("OPEN", "PENDING"),
            )
            await connection.execute(
                text(
                    "UPDATE \"buma_caisse_open_close\" SET state='CLOSE', "
                    "close_time=NOW(), validator=:validator, end_amount=:amount, "
                    "updated=NOW() WHERE id=:id"
                ),
                {
                    "validator": validator,
                    "amount": float(caisse.get("balance") or 0),
                    "id": session["id"],
                },
            )
            await connection.execute(
                text(
                    "UPDATE \"buma_caisse\" SET state='CLOSE', "
                    "\"currentCashier\"='', updated=NOW() WHERE id=:id"
                ),
                {"id": caisse_id},
            )
        return {"message": "Ok"}

    return await run_record_storage_transaction(engine, operation)


async def caisse_change_cashier(engine: Any, caisse_id: str, new_cashier_id: str) -> dict[str, Any]:
    if not caisse_id or not new_cashier_id:
        raise FinanceValidationError("L'ID de la caisse et l'ID du nouveau caissier sont requis")

    async def operation(active: Any) -> dict[str, Any]:
        async with active.connect() as connection:
            caisse = (await _lock_rows(connection, "buma_caisse", [caisse_id]))[0]
            if str(caisse.get("state") or "") != "OPEN":
                raise FinanceValidationError("La caisse n'est pas ouverte")
            old_cashier = str(caisse.get("currentCashier") or "")
            if not old_cashier:
                raise FinanceValidationError(
                    "Aucun caissier n'est actuellement assigné à cette caisse"
                )
            if old_cashier == new_cashier_id:
                raise FinanceValidationError("Le nouveau caissier est identique au caissier actuel")
            session_id = str(caisse.get("currentSession") or "")
            if not session_id:
                raise FinanceValidationError("Aucune session active pour cette caisse")
            session = (await _lock_rows(connection, "buma_caisse_open_close", [session_id]))[0]
            if str(session.get("state") or "") != "OPEN":
                raise FinanceValidationError("La session n'est pas ouverte")
            await connection.execute(
                text(
                    'UPDATE "buma_caisse" SET "currentCashier"=:cashier, updated=NOW() WHERE id=:id'
                ),
                {"cashier": new_cashier_id, "id": caisse_id},
            )
        return {
            "success": True,
            "message": "Caissier changé avec succès",
            "caisseId": caisse_id,
            "oldCashierId": old_cashier,
            "newCashierId": new_cashier_id,
            "sessionId": session_id,
        }

    return await run_record_storage_transaction(engine, operation)


def _public_datetime(value: Any) -> str:
    if isinstance(value, datetime):
        return format_datetime(value)
    return str(value or "")


async def accessible_caisses(engine: Any, user_id: str) -> dict[str, Any]:
    async with engine.connect() as connection:
        access_rows = (
            (
                await connection.execute(
                    text('SELECT * FROM "buma_caisse_access" WHERE "user"=:user ORDER BY created'),
                    {"user": user_id},
                )
            )
            .mappings()
            .all()
        )
        if not access_rows:
            return {"accesses": [], "caisses": []}
        caisse_ids = [str(row.get("caisse") or "") for row in access_rows]
        caisse_rows = (
            (
                await connection.execute(
                    text('SELECT * FROM "buma_caisse" WHERE id=ANY(:ids)'),
                    {"ids": caisse_ids},
                )
            )
            .mappings()
            .all()
        )
    accesses = [
        {
            "id": str(row.get("id") or ""),
            "caisse": str(row.get("caisse") or ""),
            "user": str(row.get("user") or ""),
            "created": _public_datetime(row.get("created")),
            "updated": _public_datetime(row.get("updated")),
        }
        for row in access_rows
    ]
    caisses = [
        {
            "id": str(row.get("id") or ""),
            "name": str(row.get("name") or ""),
            "type": str(row.get("type") or ""),
            "balance": float(row.get("balance") or 0),
            "state": str(row.get("state") or ""),
            "currentCashier": str(row.get("currentCashier") or ""),
            "currentSession": str(row.get("currentSession") or ""),
            "unknown_balance": float(row.get("unknown_balance") or 0),
            "unknow_balance_session": str(row.get("unknow_balance_session") or ""),
            "status": bool(row.get("status")),
            "author": str(row.get("author") or ""),
            "created": _public_datetime(row.get("created")),
            "updated": _public_datetime(row.get("updated")),
        }
        for row in caisse_rows
    ]
    return {"accesses": accesses, "caisses": caisses}


async def caisse_cancel_transaction(
    engine: Any, transaction_id: str, author: str
) -> dict[str, Any]:
    if not transaction_id:
        raise FinanceValidationError("Transaction introuvable")

    async def operation(active: Any) -> dict[str, Any]:
        async with active.connect() as connection:
            await connection.execute(
                text("SELECT pg_advisory_xact_lock(hashtextextended(:key, 0))"),
                {"key": f"buma:caisse_cancel:{transaction_id}"},
            )
            main = (
                (
                    await connection.execute(
                        text('SELECT * FROM "buma_caisse_transactions" WHERE id=:id FOR UPDATE'),
                        {"id": transaction_id},
                    )
                )
                .mappings()
                .first()
            )
            if main is None:
                raise FinanceValidationError("Transaction introuvable")
            main = dict(main)
            linked: dict[str, Any] | None = None
            linked_id = str(main.get("transfer_caisse") or "")
            if linked_id:
                linked_row = (
                    (
                        await connection.execute(
                            text(
                                'SELECT * FROM "buma_caisse_transactions" WHERE id=:id FOR UPDATE'
                            ),
                            {"id": linked_id},
                        )
                    )
                    .mappings()
                    .first()
                )
                linked = dict(linked_row) if linked_row else None

            rows = [main, *([linked] if linked else [])]
            for row in rows:
                if not row.get("status") or row.get("cancelled") is True:
                    raise FinanceValidationError("Cette transaction est déjà annulée")
                caisse, session = await _current_caisse_session(
                    connection, str(row.get("caisse") or "")
                )
                if str(row.get("session") or "") != str(session["id"]):
                    raise FinanceValidationError(
                        "Cette transaction ne peut pas être annulée car elle "
                        "n'appartient pas à la session en cours"
                    )
                row["_caisse"] = caisse

            for row in rows:
                amount = float(row.get("amount") or 0)
                tx_type = str(row.get("type") or "")
                caisse = row["_caisse"]
                caisse_id = str(caisse["id"])
                caisse_balance = float(caisse.get("balance") or 0)
                if tx_type in {"DEPOSIT", "VERSEMENT"}:
                    caisse_after = caisse_balance - amount
                elif tx_type in {"WITHDRAWAL", "EXPENSE", "RETURN"}:
                    caisse_after = caisse_balance + amount
                elif tx_type == "EMPLOYEE_DEBT":
                    caisse_after = caisse_balance + amount
                elif tx_type == "EMPLOYEE_DEBT_REPAYMENT":
                    caisse_after = caisse_balance - amount
                else:
                    raise FinanceValidationError(
                        "Type de transaction non pris en charge pour l'annulation"
                    )
                await connection.execute(
                    text('UPDATE "buma_caisse" SET balance=:v, updated=NOW() WHERE id=:id'),
                    {"v": caisse_after, "id": caisse_id},
                )
                caisse["balance"] = caisse_after

                wallet_id = str(row.get("wallet") or "")
                if wallet_id:
                    wallet = (await _lock_rows(connection, "buma_wallet", [wallet_id]))[0]
                    wallet_balance = float(wallet.get("balance") or 0)
                    wallet_after = wallet_balance + (
                        amount if tx_type in {"DEPOSIT", "VERSEMENT"} else -amount
                    )
                    await connection.execute(
                        text('UPDATE "buma_wallet" SET balance=:v, updated=NOW() WHERE id=:id'),
                        {"v": wallet_after, "id": wallet_id},
                    )

                wallet_transaction_id = str(row.get("walletTransaction") or "")
                if wallet_transaction_id:
                    await connection.execute(
                        text(
                            'UPDATE "buma_wallet_transactions" '
                            "SET status=FALSE, updated=NOW() WHERE id=:id"
                        ),
                        {"id": wallet_transaction_id},
                    )

                customer_id = str(row.get("customer") or "")
                if customer_id and tx_type in {"DEPOSIT", "VERSEMENT"}:
                    customer = (await _lock_rows(connection, "buma_customer", [customer_id]))[0]
                    await connection.execute(
                        text('UPDATE "buma_customer" SET solde=:v, updated=NOW() WHERE id=:id'),
                        {
                            "v": float(customer.get("solde") or 0) - amount,
                            "id": customer_id,
                        },
                    )

                employee_id = str(row.get("employee") or "")
                if employee_id and tx_type in {
                    "EMPLOYEE_DEBT",
                    "EMPLOYEE_DEBT_REPAYMENT",
                }:
                    employee = (await _lock_rows(connection, "buma_employee", [employee_id]))[0]
                    employee_balance = float(employee.get("solde") or 0)
                    employee_after = employee_balance + (
                        amount if tx_type == "EMPLOYEE_DEBT" else -amount
                    )
                    await connection.execute(
                        text('UPDATE "buma_employee" SET solde=:v, updated=NOW() WHERE id=:id'),
                        {"v": employee_after, "id": employee_id},
                    )

                if tx_type == "RETURN" and str(row.get("retour") or ""):
                    await connection.execute(
                        text('UPDATE "buma_return" SET payed=FALSE, updated=NOW() WHERE id=:id'),
                        {"id": str(row["retour"])},
                    )

                await connection.execute(
                    text(
                        'UPDATE "buma_caisse_transactions" '
                        "SET cancelled=TRUE, status=FALSE, updated=NOW() "
                        "WHERE id=:id"
                    ),
                    {"id": str(row["id"])},
                )

        return {"success": True, "message": "Transaction annulée avec succès"}

    return await run_record_storage_transaction(engine, operation)


async def _create_caisse_transaction(
    active: Any,
    payload: dict[str, Any],
) -> dict[str, Any]:
    collection = await resolve_collection(active, "buma_caisse_transactions")
    return await create_record(active, collection, payload)


async def _record_customer_history(
    active: Any,
    customer_id: str,
    previous: float,
    new: float,
    caisse_transaction_id: str,
    author: str,
    history_type: str,
    why: str,
) -> None:
    """Send the same source payload while preserving PocketBase field behavior.

    PocketBase ignores unknown fields on this legacy collection.  PPBase keeps
    the same observable result by writing the physical fields directly; the
    source-only ``customer`` and ``author`` keys are intentionally not
    materialized when the baseline does not contain them.
    """
    source_payload = {
        "customer": customer_id,
        "previous_balance": previous,
        "new_balance": new,
        "type": history_type,
        "why": why,
        "author": author,
        "status": True,
        "caisse_transaction": caisse_transaction_id,
    }
    now = datetime.now(UTC)
    values = {
        "id": generate_id(),
        "created": now,
        "updated": now,
        "previous_balance": source_payload["previous_balance"],
        "new_balance": source_payload["new_balance"],
        "caisse_transaction": source_payload["caisse_transaction"],
        "status": source_payload["status"],
        "type": source_payload["type"],
        "why": source_payload["why"],
    }
    async with active.connect() as connection:
        await connection.execute(
            text(
                'INSERT INTO "buma_customer_solder_history" '
                "(id, created, updated, previous_balance, new_balance, "
                "caisse_transaction, status, type, why) "
                "VALUES (:id, :created, :updated, :previous_balance, :new_balance, "
                ":caisse_transaction, :status, :type, :why)"
            ),
            values,
        )


async def caisse_add_versement(engine: Any, data: dict[str, Any], author: str) -> dict[str, Any]:
    amount = float(data.get("amount") or 0)
    if amount < 0:
        raise FinanceValidationError("Montant invalide")

    async def operation(active: Any) -> dict[str, Any]:
        async with active.connect() as connection:
            caisse, session = await _current_caisse_session(
                connection, str(data.get("caisse") or "")
            )
            await _reject_recent_duplicate(connection, str(caisse["id"]), amount, "VERSEMENT")
            customer = (
                await _lock_rows(connection, "buma_customer", [str(data.get("customer") or "")])
            )[0]
            unknown = bool(data.get("fromUnknown") is True)
            if unknown and float(caisse.get("unknown_balance") or 0) < amount:
                raise FinanceValidationError("Solde insuffisant")
            previous_caisse = float(caisse.get("balance") or 0)
            after_caisse = previous_caisse if unknown else previous_caisse + amount
            previous_customer = float(customer.get("solde") or 0)
            after_customer = previous_customer + amount
            tx_no = await _next_transaction_no(connection, "buma_caisse_transactions")
            session_id = (
                str(caisse.get("unknow_balance_session") or session["id"])
                if unknown
                else str(session["id"])
            )
            await connection.execute(
                text(
                    'UPDATE "buma_caisse" SET balance=:balance, '
                    "unknown_balance=:unknown_balance, updated=NOW() WHERE id=:id"
                ),
                {
                    "balance": after_caisse,
                    "unknown_balance": float(caisse.get("unknown_balance") or 0) - amount
                    if unknown
                    else float(caisse.get("unknown_balance") or 0),
                    "id": caisse["id"],
                },
            )
            await connection.execute(
                text('UPDATE "buma_customer" SET solde=:solde, updated=NOW() WHERE id=:id'),
                {"solde": after_customer, "id": customer["id"]},
            )
        transaction = await _create_caisse_transaction(
            active,
            {
                "author": author,
                "amount": amount,
                "note": data.get("note"),
                "customer": customer["id"],
                "caisse": caisse["id"],
                "type": "VERSEMENT",
                "session": session_id,
                "status": True,
                "previous_caisse_balance": previous_caisse,
                "after_caisse_balance": after_caisse,
                "before_customer_solde": previous_customer,
                "after_customer_solde": after_customer,
                "transaction_no": tx_no,
            },
        )
        await _record_customer_history(
            active,
            str(customer["id"]),
            previous_customer,
            after_customer,
            transaction["id"],
            author,
            "PAYMENT",
            f"Paiement en caisse - Montant: {amount:g}",
        )
        return {
            "success": True,
            "message": "Transaction reussis",
            "caisse_transaction": transaction["id"],
        }

    return await run_record_storage_transaction(engine, operation)


async def caisse_add_expense(engine: Any, data: dict[str, Any], author: str) -> dict[str, Any]:
    amount = float(data.get("amount") or 0)
    if amount < 0:
        raise FinanceValidationError("Montant invalide")
    expense_type = str(data.get("expenseType") or "")
    if not expense_type:
        raise FinanceValidationError("Type invalide")

    async def operation(active: Any) -> dict[str, Any]:
        async with active.connect() as connection:
            caisse, session = await _current_caisse_session(
                connection, str(data.get("caisse") or "")
            )
            balance = float(caisse.get("balance") or 0)
            if amount > balance:
                raise FinanceValidationError("Vous n'avez pas suffisament de solde")
            await _reject_recent_duplicate(connection, str(caisse["id"]), amount, "EXPENSE")
            after = balance - amount
            tx_no = await _next_transaction_no(connection, "buma_caisse_transactions")
            await connection.execute(
                text('UPDATE "buma_caisse" SET balance=:balance, updated=NOW() WHERE id=:id'),
                {"balance": after, "id": caisse["id"]},
            )
        transaction = await _create_caisse_transaction(
            active,
            {
                "author": author,
                "amount": amount,
                "note": data.get("note"),
                "caisse": caisse["id"],
                "type": "EXPENSE",
                "session": session["id"],
                "expenseType": expense_type,
                "status": True,
                "previous_caisse_balance": balance,
                "after_caisse_balance": after,
                "transaction_no": tx_no,
                "document_path": data.get("document_path") or "",
            },
        )
        return {
            "success": True,
            "message": "Transaction reussis",
            "caisse_transaction": transaction["id"],
        }

    return await run_record_storage_transaction(engine, operation)


async def caisse_bank_versement(engine: Any, data: dict[str, Any], author: str) -> dict[str, Any]:
    amount = float(data.get("amount") or 0)
    if amount < 0:
        raise FinanceValidationError("Montant invalide")

    async def operation(active: Any) -> dict[str, Any]:
        wallets = await resolve_collection(active, "buma_wallet_transactions")
        async with active.connect() as connection:
            caisse, session = await _current_caisse_session(
                connection, str(data.get("caisse") or "")
            )
            wallet = (
                await _lock_rows(connection, "buma_wallet", [str(data.get("walletId") or "")])
            )[0]
            since = datetime.now(UTC) - timedelta(seconds=60)
            duplicate = (
                await connection.execute(
                    text(
                        'SELECT id FROM "buma_caisse_transactions" '
                        "WHERE type='BANK_VERSEMENT' AND wallet=:wallet "
                        "AND session=:session AND amount=:amount AND status=TRUE "
                        "AND created>=:since LIMIT 1"
                    ),
                    {
                        "wallet": wallet["id"],
                        "session": session["id"],
                        "amount": amount,
                        "since": since,
                    },
                )
            ).first()
            if duplicate is not None:
                raise FinanceValidationError(
                    "Un versement bancaire identique a déjà été effectué il y a moins d'une minute"
                )
            customer_id = str(data.get("customer") or "")
            customer = None
            before_customer = 0
            after_customer = 0
            if customer_id:
                customer = (await _lock_rows(connection, "buma_customer", [customer_id]))[0]
                before_customer = float(customer.get("solde") or 0)
                after_customer = before_customer + amount
                await connection.execute(
                    text('UPDATE "buma_customer" SET solde=:solde, updated=NOW() WHERE id=:id'),
                    {"solde": after_customer, "id": customer_id},
                )
            caisse_no = await _next_transaction_no(connection, "buma_caisse_transactions")
            wallet_no = await _next_transaction_no(connection, "buma_wallet_transactions")
            note = f"Versement bancaire de {amount:g} FCFA"
            if data.get("note"):
                note += f" | {data['note']}"
            previous_wallet = float(wallet.get("balance") or 0)
        caisse_transaction = await _create_caisse_transaction(
            active,
            {
                "author": author,
                "amount": amount,
                "note": note,
                "caisse": caisse["id"],
                "type": "BANK_VERSEMENT",
                "customer": customer_id,
                "wallet": wallet["id"],
                "session": session["id"],
                "status": True,
                "previous_caisse_balance": float(caisse.get("balance") or 0),
                "after_caisse_balance": float(caisse.get("balance") or 0),
                "before_customer_solde": before_customer if customer else None,
                "after_customer_solde": after_customer if customer else None,
                "transaction_no": caisse_no,
            },
        )
        await create_record(
            active,
            wallets,
            {
                "author": author,
                "amount": amount,
                "note": note,
                "caisse": caisse["id"],
                "type": "DEPOSIT",
                "caisse_transaction": caisse_transaction["id"],
                "wallet": wallet["id"],
                "state": "DONE",
                "caisseSession": session["id"],
                "status": True,
                "wallet_solde_before": previous_wallet,
                "transaction_no": wallet_no,
            },
        )
        async with active.connect() as connection:
            await connection.execute(
                text('UPDATE "buma_wallet" SET balance=:v, updated=NOW() WHERE id=:id'),
                {"v": previous_wallet + amount, "id": wallet["id"]},
            )
        if customer:
            await _record_customer_history(
                active,
                str(customer["id"]),
                before_customer,
                after_customer,
                caisse_transaction["id"],
                author,
                "PAYMENT",
                f"Versement bancaire - Montant: {amount:g}",
            )
        return {
            "success": True,
            "message": "Versement bancaire réussi",
            "caisse_transaction": caisse_transaction["id"],
        }

    return await run_record_storage_transaction(engine, operation)


async def caisse_cancel_bank_versement(
    engine: Any, transaction_id: str, author: str
) -> dict[str, Any]:
    if not transaction_id:
        raise FinanceValidationError("ID de transaction manquant")

    async def operation(active: Any) -> dict[str, Any]:
        async with active.connect() as connection:
            transaction = (
                (
                    await connection.execute(
                        text('SELECT * FROM "buma_caisse_transactions" WHERE id=:id FOR UPDATE'),
                        {"id": transaction_id},
                    )
                )
                .mappings()
                .first()
            )
            if transaction is None:
                raise FinanceValidationError("Transaction introuvable")
            if transaction.get("type") != "BANK_VERSEMENT":
                raise FinanceValidationError("Cette transaction n'est pas un versement bancaire")
            if not transaction.get("status") or transaction.get("cancelled"):
                raise FinanceValidationError("Cette transaction est déjà annulée")
            caisse, session = await _current_caisse_session(
                connection, str(transaction.get("caisse") or "")
            )
            if str(transaction.get("session") or "") != str(session["id"]):
                raise FinanceValidationError(
                    "Cette transaction ne peut pas être annulée car elle "
                    "n'appartient pas à la session en cours"
                )
            amount = float(transaction.get("amount") or 0)
            wallet = (
                await _lock_rows(connection, "buma_wallet", [str(transaction.get("wallet") or "")])
            )[0]
            wallet_balance = float(wallet.get("balance") or 0)
            if wallet_balance < amount:
                raise FinanceValidationError(
                    "Le solde du portefeuille est insuffisant pour annuler cette transaction"
                )
            await connection.execute(
                text('UPDATE "buma_wallet" SET balance=:v, updated=NOW() WHERE id=:id'),
                {"v": wallet_balance - amount, "id": wallet["id"]},
            )
            customer_id = str(transaction.get("customer") or "")
            if customer_id:
                customer = (await _lock_rows(connection, "buma_customer", [customer_id]))[0]
                before = float(customer.get("solde") or 0)
                await connection.execute(
                    text('UPDATE "buma_customer" SET solde=:v, updated=NOW() WHERE id=:id'),
                    {"v": before - amount, "id": customer_id},
                )
            await connection.execute(
                text(
                    'UPDATE "buma_wallet_transactions" SET status=FALSE, cancelled=TRUE, '
                    "cancel_date=NOW(), cancel_author=:author, updated=NOW() "
                    "WHERE caisse_transaction=:id"
                ),
                {"author": author, "id": transaction_id},
            )
            await connection.execute(
                text(
                    'UPDATE "buma_caisse_transactions" SET status=FALSE, cancelled=TRUE, '
                    "cancel_date=NOW(), cancel_author=:author, updated=NOW() WHERE id=:id"
                ),
                {"author": author, "id": transaction_id},
            )
        return {
            "success": True,
            "message": "Versement bancaire annulé avec succès",
            "original_transaction": transaction_id,
        }

    return await run_record_storage_transaction(engine, operation)


async def caisse_add_correction(engine: Any, data: dict[str, Any], author: str) -> dict[str, str]:
    if not data.get("transactionId"):
        raise FinanceValidationError("Transaction ne doit pas etre vide")
    if not data.get("caisse"):
        raise FinanceValidationError("Caisse ne doit pas etre vide")
    amount = float(data.get("amount") or 0)
    if amount < 0:
        raise FinanceValidationError("Montant incorrect")
    if not isinstance(data.get("deposit"), bool):
        raise FinanceValidationError("Quel methode ? Depot ou Retrait.")

    async def operation(active: Any) -> dict[str, str]:
        async with active.connect() as connection:
            caisse, session = await _current_caisse_session(connection, str(data["caisse"]))
            original = (
                (
                    await connection.execute(
                        text('SELECT * FROM "buma_caisse_transactions" WHERE id=:id FOR UPDATE'),
                        {"id": str(data["transactionId"])},
                    )
                )
                .mappings()
                .first()
            )
            if original is None:
                raise FinanceValidationError("Transaction non existant")
            before = float(caisse.get("balance") or 0)
            after = before + amount if data["deposit"] else before - amount
            tx_no = await _next_transaction_no(connection, "buma_caisse_transactions")
            customer_id = str(original.get("customer") or "")
            customer = None
            customer_before = 0
            customer_after = 0
            if customer_id:
                customer = (await _lock_rows(connection, "buma_customer", [customer_id]))[0]
                customer_before = float(customer.get("solde") or 0)
                customer_after = (
                    customer_before + amount if data["deposit"] else customer_before - amount
                )
                await connection.execute(
                    text('UPDATE "buma_customer" SET solde=:v, updated=NOW() WHERE id=:id'),
                    {"v": customer_after, "id": customer_id},
                )
            await connection.execute(
                text('UPDATE "buma_caisse" SET balance=:v, updated=NOW() WHERE id=:id'),
                {"v": after, "id": caisse["id"]},
            )
        correction = await _create_caisse_transaction(
            active,
            {
                "amount": amount,
                "type": "FIX",
                "note": f"{data.get('note') or ''} | " + ("ENTRER" if data["deposit"] else "SORTI"),
                "caisse": caisse["id"],
                "author": author,
                "status": True,
                "session": session["id"],
                "fixId": str(original["id"]),
                "previous_caisse_balance": before,
                "after_caisse_balance": after,
                "before_customer_solde": customer_before if customer else None,
                "after_customer_solde": customer_after if customer else None,
                "transaction_no": tx_no,
            },
        )
        async with active.connect() as connection:
            await connection.execute(
                text(
                    'UPDATE "buma_caisse_transactions" SET "correctionId"=:correction, '
                    "updated=NOW() WHERE id=:id"
                ),
                {"correction": correction["id"], "id": original["id"]},
            )
        if customer:
            await _record_customer_history(
                active,
                customer_id,
                customer_before,
                customer_after,
                correction["id"],
                author,
                "PAYMENT" if data["deposit"] else "DEBT",
                (
                    f"Dépôt en caisse - Montant: {amount:g}"
                    if data["deposit"]
                    else f"Retrait de caisse - Montant: {amount:g}"
                ),
            )
        return {"message": "Ok"}

    return await run_record_storage_transaction(engine, operation)


async def caisse_update_note(engine: Any, transaction_id: str, note: str | None) -> dict[str, str]:
    if not transaction_id:
        raise FinanceValidationError("ID de transaction requis")
    if note is None:
        raise FinanceValidationError("Note requise")

    async def operation(active: Any) -> dict[str, str]:
        async with active.connect() as connection:
            transaction = (
                (
                    await connection.execute(
                        text('SELECT * FROM "buma_caisse_transactions" WHERE id=:id FOR UPDATE'),
                        {"id": transaction_id},
                    )
                )
                .mappings()
                .first()
            )
            if transaction is None:
                raise FinanceValidationError("Transaction introuvable")
            caisse, session = await _current_caisse_session(
                connection, str(transaction.get("caisse") or "")
            )
            if str(transaction.get("session") or "") != str(session["id"]):
                raise FinanceValidationError(
                    "Cette transaction ne peut pas être modifiée car elle "
                    "n'appartient pas à la session en cours"
                )
            if not transaction.get("status"):
                raise FinanceValidationError("Cette transaction a été annulée")
            await connection.execute(
                text(
                    'UPDATE "buma_caisse_transactions" SET note=:note, updated=NOW() WHERE id=:id'
                ),
                {"note": note, "id": transaction_id},
            )
        return {"message": "Note mise à jour avec succès"}

    return await run_record_storage_transaction(engine, operation)


async def archive_expense_type(engine: Any, expense_type_id: str) -> dict[str, bool]:
    if not expense_type_id:
        raise FinanceValidationError("ID du type de dépense requis")

    async def operation(active: Any) -> dict[str, bool]:
        async with active.connect() as connection:
            result = await connection.execute(
                text(
                    'UPDATE "buma_caisse_expense_type" SET status=FALSE, updated=NOW() WHERE id=:id'
                ),
                {"id": expense_type_id},
            )
            if result.rowcount == 0:
                raise FinanceNotFoundError("Type de dépense introuvable")
        return {"success": True}

    return await run_record_storage_transaction(engine, operation)


async def caisse_unallocate(engine: Any, transaction_id: str) -> dict[str, str]:
    async def operation(active: Any) -> dict[str, str]:
        async with active.connect() as connection:
            transaction = (
                (
                    await connection.execute(
                        text('SELECT * FROM "buma_caisse_transactions" WHERE id=:id FOR UPDATE'),
                        {"id": transaction_id},
                    )
                )
                .mappings()
                .first()
            )
            if transaction is None:
                raise FinanceValidationError("Transaction non trouvée")
            if not transaction.get("status"):
                raise FinanceValidationError("Transaction déjà annulée")
            if transaction.get("type") != "VERSEMENT":
                raise FinanceValidationError(
                    "Seules les transactions de type VERSEMENT peuvent être désallouées"
                )
            caisse = (await _lock_rows(connection, "buma_caisse", [str(transaction["caisse"])]))[0]
            session = (
                await _lock_rows(
                    connection,
                    "buma_caisse_open_close",
                    [str(transaction.get("session") or "")],
                )
            )[0]
            if str(session.get("state") or "") != "CLOSE":
                raise FinanceValidationError("Session invalide")
            existing_unknown = float(caisse.get("unknown_balance") or 0)
            unknown_session = str(caisse.get("unknow_balance_session") or "")
            if existing_unknown > 0 and unknown_session and unknown_session != str(session["id"]):
                raise FinanceValidationError("Caisse a deja un solde inconnu pour une session")
            customer = (
                await _lock_rows(
                    connection,
                    "buma_customer",
                    [str(transaction.get("customer") or "")],
                )
            )[0]
            amount = float(transaction.get("amount") or 0)
            previous_customer = float(customer.get("solde") or 0)
            tx_no = await _next_transaction_no(connection, "buma_caisse_transactions")
            await connection.execute(
                text(
                    'UPDATE "buma_caisse" SET unknown_balance=:balance, '
                    "unknow_balance_session=:session, updated=NOW() WHERE id=:id"
                ),
                {
                    "balance": existing_unknown + amount,
                    "session": session["id"],
                    "id": caisse["id"],
                },
            )
            await connection.execute(
                text(
                    'UPDATE "buma_caisse_transactions" SET status=FALSE, updated=NOW() WHERE id=:id'
                ),
                {"id": transaction_id},
            )
            await connection.execute(
                text('UPDATE "buma_customer" SET solde=:v, updated=NOW() WHERE id=:id'),
                {"v": previous_customer - amount, "id": customer["id"]},
            )
        reversal = await _create_caisse_transaction(
            active,
            {
                "caisse": caisse["id"],
                "amount": amount,
                "type": "REVERSAL",
                "note": (
                    f"Annulation du versement de {amount:g} FCFA - "
                    "Montant ajouté au solde non allouer"
                ),
                "author": str(transaction.get("author") or ""),
                "reference": transaction_id,
                "session": session["id"],
                "status": True,
                "previous_caisse_balance": float(caisse.get("balance") or 0),
                "after_caisse_balance": float(caisse.get("balance") or 0),
                "before_customer_solde": previous_customer,
                "after_customer_solde": previous_customer - amount,
                "transaction_no": tx_no,
            },
        )
        await _record_customer_history(
            active,
            str(customer["id"]),
            previous_customer,
            previous_customer - amount,
            reversal["id"],
            str(transaction.get("author") or ""),
            "DEBT",
            f"Désallocation de versement - Montant: {amount:g}",
        )
        return {"message": "OK"}

    return await run_record_storage_transaction(engine, operation)


async def caisse_update_amount(engine: Any, transaction_id: str, amount: float) -> dict[str, str]:
    async def operation(active: Any) -> dict[str, str]:
        async with active.connect() as connection:
            transaction = (
                (
                    await connection.execute(
                        text('SELECT * FROM "buma_caisse_transactions" WHERE id=:id FOR UPDATE'),
                        {"id": transaction_id},
                    )
                )
                .mappings()
                .first()
            )
            if transaction is None:
                raise FinanceValidationError("Transaction non trouvée")
            if not transaction.get("status"):
                raise FinanceValidationError("Transaction déjà annulée")
            caisse = (await _lock_rows(connection, "buma_caisse", [str(transaction["caisse"])]))[0]
            session = (
                await _lock_rows(
                    connection,
                    "buma_caisse_open_close",
                    [str(transaction.get("session") or "")],
                )
            )[0]
            if str(session.get("state") or "") != "OPEN":
                raise FinanceValidationError("La session doit être ouverte")
            customer = (
                await _lock_rows(
                    connection,
                    "buma_customer",
                    [str(transaction.get("customer") or "")],
                )
            )[0]
            old_amount = float(transaction.get("amount") or 0)
            difference = amount - old_amount
            before_customer = float(customer.get("solde") or 0)
            after_customer = before_customer + difference
            await connection.execute(
                text('UPDATE "buma_caisse" SET balance=balance+:diff, updated=NOW() WHERE id=:id'),
                {"diff": difference, "id": caisse["id"]},
            )
            await connection.execute(
                text('UPDATE "buma_customer" SET solde=:v, updated=NOW() WHERE id=:id'),
                {"v": after_customer, "id": customer["id"]},
            )
            await connection.execute(
                text(
                    'UPDATE "buma_caisse_transactions" SET amount=:amount, '
                    "updated=NOW() WHERE id=:id"
                ),
                {"amount": amount, "id": transaction_id},
            )
        await _record_customer_history(
            active,
            str(customer["id"]),
            before_customer,
            after_customer,
            transaction_id,
            str(transaction.get("author") or ""),
            "PAYMENT" if difference > 0 else "DEBT",
            f"Modification de transaction - Différence: {difference:g}",
        )
        return {"message": "OK"}

    return await run_record_storage_transaction(engine, operation)


async def caisse_change_transaction_customer(
    engine: Any, transaction_id: str, new_customer_id: str, author: str
) -> dict[str, Any]:
    async def operation(active: Any) -> dict[str, Any]:
        async with active.connect() as connection:
            transaction = (
                (
                    await connection.execute(
                        text('SELECT * FROM "buma_caisse_transactions" WHERE id=:id FOR UPDATE'),
                        {"id": transaction_id},
                    )
                )
                .mappings()
                .first()
            )
            if transaction is None:
                raise FinanceValidationError("Transaction introuvable")
            tx_type = str(transaction.get("type") or "")
            if tx_type not in {"VERSEMENT", "BANK_VERSEMENT"}:
                raise FinanceValidationError(
                    "Cette opération n'est possible que pour les transactions "
                    "de type VERSEMENT ou BANK_VERSEMENT"
                )
            if not transaction.get("status") or transaction.get("cancelled"):
                raise FinanceValidationError(
                    "Cette transaction a été annulée et ne peut plus être modifiée"
                )
            amount = float(transaction.get("amount") or 0)
            current_id = str(transaction.get("customer") or "")
            await _lock_rows(connection, "buma_customer", [new_customer_id])
            if not current_id and tx_type == "BANK_VERSEMENT":
                await connection.execute(
                    text(
                        'UPDATE "buma_customer" SET solde=solde+:amount, updated=NOW() WHERE id=:id'
                    ),
                    {"amount": amount, "id": new_customer_id},
                )
                await connection.execute(
                    text(
                        'UPDATE "buma_caisse_transactions" SET customer=:customer, '
                        "updated=NOW() WHERE id=:id"
                    ),
                    {"customer": new_customer_id, "id": transaction_id},
                )
                return {
                    "success": True,
                    "message": "Client assigné à la transaction bancaire avec succès",
                    "oldCustomer": None,
                    "newCustomer": new_customer_id,
                    "amount": amount,
                }
            await _lock_rows(connection, "buma_customer", [current_id])
            await connection.execute(
                text('UPDATE "buma_customer" SET solde=solde-:amount, updated=NOW() WHERE id=:id'),
                {"amount": amount, "id": current_id},
            )
            await connection.execute(
                text('UPDATE "buma_customer" SET solde=solde+:amount, updated=NOW() WHERE id=:id'),
                {"amount": amount, "id": new_customer_id},
            )
            await connection.execute(
                text(
                    'UPDATE "buma_caisse_transactions" SET customer=:customer, '
                    "updated=NOW() WHERE id=:id"
                ),
                {"customer": new_customer_id, "id": transaction_id},
            )
        return {
            "success": True,
            "message": (
                "Client de la transaction bancaire modifié avec succès"
                if tx_type == "BANK_VERSEMENT"
                else "Client de la transaction  modifié avec succès"
            ),
            "oldCustomer": current_id,
            "newCustomer": new_customer_id,
            "amount": amount,
        }

    return await run_record_storage_transaction(engine, operation)


async def caisse_employee_debt(
    engine: Any, data: dict[str, Any], author: str, *, repayment: bool = False
) -> dict[str, Any]:
    amount = float(data.get("amount") or 0)
    if amount < 0:
        raise FinanceValidationError("Montant invalide")

    async def operation(active: Any) -> dict[str, Any]:
        async with active.connect() as connection:
            caisse, session = await _current_caisse_session(
                connection, str(data.get("caisse") or "")
            )
            employee = (
                await _lock_rows(connection, "buma_employee", [str(data.get("employee") or "")])
            )[0]
            caisse_before = float(caisse.get("balance") or 0)
            employee_before = float(employee.get("solde") or 0)
            if not repayment and amount > caisse_before:
                raise FinanceValidationError("Solde insuffisant dans la caisse")
            caisse_after = caisse_before + amount if repayment else caisse_before - amount
            employee_after = employee_before + amount if repayment else employee_before - amount
            tx_type = "EMPLOYEE_DEBT_REPAYMENT" if repayment else "EMPLOYEE_DEBT"
            await _reject_recent_duplicate(connection, str(caisse["id"]), amount, tx_type)
            tx_no = await _next_transaction_no(connection, "buma_caisse_transactions")
            await connection.execute(
                text('UPDATE "buma_caisse" SET balance=:v, updated=NOW() WHERE id=:id'),
                {"v": caisse_after, "id": caisse["id"]},
            )
            await connection.execute(
                text('UPDATE "buma_employee" SET solde=:v, updated=NOW() WHERE id=:id'),
                {"v": employee_after, "id": employee["id"]},
            )
        transaction = await _create_caisse_transaction(
            active,
            {
                "author": author,
                "amount": amount,
                "note": data.get("note")
                or (
                    "Remboursement de dette par l'employé"
                    if repayment
                    else "Prêt/avance à l'employé"
                ),
                "employee": employee["id"],
                "caisse": caisse["id"],
                "type": tx_type,
                "session": session["id"],
                "status": True,
                "previous_caisse_balance": caisse_before,
                "after_caisse_balance": caisse_after,
                "transaction_no": tx_no,
                "reference": data.get("debtId") or "",
            },
        )
        debt_id = str(data.get("debtId") or "")
        if debt_id and repayment:
            async with active.connect() as connection:
                await connection.execute(
                    text(
                        'UPDATE "buma_employee_debt" SET paid=paid+:amount, '
                        "state=CASE WHEN paid+:amount >= amount THEN 'PAID' "
                        "ELSE state END, updated=NOW() WHERE id=:id"
                    ),
                    {"amount": amount, "id": debt_id},
                )
        return {
            "success": True,
            "message": (
                "Remboursement de dette enregistré avec succès"
                if repayment
                else "Dette employé créée avec succès"
            ),
            "caisse_transaction": transaction["id"],
        }

    return await run_record_storage_transaction(engine, operation)


async def caisse_verify_session(engine: Any, session_id: str) -> dict[str, Any]:
    async with engine.connect() as connection:
        session = (
            (
                await connection.execute(
                    text('SELECT * FROM "buma_caisse_open_close" WHERE id=:id'),
                    {"id": session_id},
                )
            )
            .mappings()
            .first()
        )
        if session is None:
            raise FinanceValidationError("Session introuvable")
        caisse = (
            (
                await connection.execute(
                    text('SELECT * FROM "buma_caisse" WHERE id=:id'),
                    {"id": session["caisse"]},
                )
            )
            .mappings()
            .first()
        )
        if caisse is None:
            raise FinanceValidationError("Caisse introuvable")
        rows = (
            (
                await connection.execute(
                    text(
                        'SELECT * FROM "buma_caisse_transactions" '
                        "WHERE session=:session ORDER BY transaction_no, created"
                    ),
                    {"session": session_id},
                )
            )
            .mappings()
            .all()
        )
    calculated = float(session.get("initial_amount") or 0)
    anomalies: list[dict[str, Any]] = []
    details: list[dict[str, Any]] = []
    impacts = {
        "VERSEMENT": 1,
        "DEPOSIT": 1,
        "WITHDRAWAL": -1,
        "EXPENSE": -1,
        "RETURN": -1,
        "EMPLOYEE_DEBT": -1,
        "EMPLOYEE_DEBT_REPAYMENT": 1,
        "BANK_VERSEMENT": 0,
        "REVERSAL": 0,
    }
    for row in rows:
        amount = float(row.get("amount") or 0)
        if not row.get("status"):
            details.append(
                {
                    "id": row["id"],
                    "transaction_no": row.get("transaction_no") or 0,
                    "type": row.get("type") or "",
                    "amount": amount,
                    "status": "CANCELLED",
                    "skipped": True,
                }
            )
            continue
        impact = amount * impacts.get(str(row.get("type") or ""), 0)
        expected_after = calculated + impact
        row_anomalies: list[str] = []
        if float(row.get("previous_caisse_balance") or 0) != calculated:
            row_anomalies.append("BALANCE_MISMATCH_BEFORE")
        if float(row.get("after_caisse_balance") or 0) != expected_after:
            row_anomalies.append("BALANCE_MISMATCH_AFTER")
        for code in row_anomalies:
            anomalies.append({"type": code, "transaction_id": row["id"]})
        details.append(
            {
                "id": row["id"],
                "transaction_no": row.get("transaction_no") or 0,
                "type": row.get("type") or "",
                "amount": amount,
                "status": "ACTIVE",
                "calculated_before": calculated,
                "recorded_before": float(row.get("previous_caisse_balance") or 0),
                "calculated_after": expected_after,
                "recorded_after": float(row.get("after_caisse_balance") or 0),
                "has_anomaly": bool(row_anomalies),
                "anomalies": row_anomalies,
            }
        )
        calculated = expected_after
    if str(session.get("state") or "") == "CLOSE" and calculated != float(
        session.get("end_amount") or 0
    ):
        anomalies.append({"type": "FINAL_BALANCE_MISMATCH"})
    return {
        "session": {
            "id": session_id,
            "state": session.get("state") or "",
            "caisse_id": caisse["id"],
            "caisse_name": caisse.get("name") or "",
            "cashier": session.get("cashier") or "",
            "open_time": _public_datetime(session.get("open_time")),
            "close_time": _public_datetime(session.get("close_time")),
        },
        "balances": {
            "initial": float(session.get("initial_amount") or 0),
            "calculated_final": calculated,
            "recorded_final": float(session.get("end_amount") or 0)
            if str(session.get("state") or "") == "CLOSE"
            else None,
            "current_caisse": float(caisse.get("balance") or 0),
            "is_current_session": str(caisse.get("currentSession") or "") == session_id,
        },
        "verification": {
            "is_valid": not anomalies,
            "anomalies_count": len(anomalies),
            "anomalies": anomalies,
        },
        "statistics": {
            "total_transactions": len(rows),
            "active_transactions": sum(1 for row in rows if row.get("status")),
            "cancelled_transactions": sum(1 for row in rows if not row.get("status")),
            "net_change": calculated - float(session.get("initial_amount") or 0),
        },
        "transactions": details,
    }
