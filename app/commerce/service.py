"""C9 commerce behavior ported from the frozen PocketBase backend."""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any

from ppbase.core.id_generator import generate_id
from ppbase.services.record_service import create_record, resolve_collection
from ppbase.services.record_storage_coordinator import run_record_storage_transaction
from sqlalchemy import text

from app.finance.service import (
    _create_caisse_transaction,
    _current_caisse_session,
    _lock_rows,
    _next_transaction_no,
)
from app.transactions.idempotency import (
    IdempotencyLookup,
    acquire_idempotency_lock,
    build_idempotency_key,
    find_idempotent_transaction,
)


class CommerceValidationError(ValueError):
    """Raised for a source-compatible C9 validation error."""


class CommerceNotFoundError(CommerceValidationError):
    """Legacy explicit HTTP 404."""


class CommerceForbiddenError(CommerceValidationError):
    """Legacy explicit HTTP 403."""


async def _collection(active: Any, name: str):
    return await resolve_collection(active, name)


async def _next_number(connection: Any, table: str, field: str) -> int:
    await connection.execute(
        text("SELECT pg_advisory_xact_lock(hashtextextended(:key, 0))"),
        {"key": f"buma:number:{table}:{field}"},
    )
    return (
        int(
            (
                await connection.execute(
                    text(
                        f'SELECT COALESCE((SELECT "{field}" FROM "{table}" '
                        "ORDER BY created DESC LIMIT 1), 0)"
                    )
                )
            ).scalar_one()
            or 0
        )
        + 1
    )


def _date(value: Any) -> datetime:
    if value:
        try:
            result = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
            return result.replace(tzinfo=UTC) if result.tzinfo is None else result
        except ValueError as exc:
            raise CommerceValidationError("Date invalide") from exc
    return datetime.now(UTC)


async def _raw_history(
    active: Any,
    *,
    previous: float,
    new: float,
    history_type: str,
    why: str,
    sale: str = "",
    caisse_transaction: str = "",
) -> None:
    now = datetime.now(UTC)
    async with active.connect() as connection:
        await connection.execute(
            text(
                'INSERT INTO "buma_customer_solder_history" '
                "(id,created,updated,previous_balance,new_balance,sale,"
                "caisse_transaction,status,type,why) "
                "VALUES (:id,:created,:updated,:previous,:new,:sale,"
                ":caisse_transaction,TRUE,:type,:why)"
            ),
            {
                "id": generate_id(),
                "created": now,
                "updated": now,
                "previous": previous,
                "new": new,
                "sale": sale,
                "caisse_transaction": caisse_transaction,
                "type": history_type,
                "why": why,
            },
        )


async def create_sale(
    engine: Any,
    data: dict[str, Any],
    author: str,
    *,
    allow_sale_date: bool,
) -> str:
    from app.commerce.sequential import create_sale as legacy_operation

    return await legacy_operation(engine, data, author, allow_sale_date=allow_sale_date)


async def _supplement_agent(active: Any, sim: dict):
    async with active.connect() as connection:
        if sim.get("agent"):
            agent = (
                (
                    await connection.execute(
                        text("SELECT * FROM buma_agent WHERE id=:id"), {"id": sim["agent"]}
                    )
                )
                .mappings()
                .first()
            )
            if agent:
                return dict(agent)
        if sim.get("pdv"):
            agent = (
                (
                    await connection.execute(
                        text(
                            "SELECT a.* FROM buma_agent a JOIN buma_pdv p ON p.agent=a.id "
                            "WHERE p.id=:id"
                        ),
                        {"id": sim["pdv"]},
                    )
                )
                .mappings()
                .first()
            )
            if agent:
                return dict(agent)
    return None


def _uses_supplement(sim: dict, kind: str) -> bool:
    return (kind == "NAFAMA" and sim.get("isNafama") is True) or (
        kind == "UV_PAANI" and sim.get("isPaani") is True
    )


async def create_recharge(engine: Any, data: dict[str, Any], author: str) -> str:
    from app.commerce.returns import create, record, save

    amount = float(data.get("amount") or 0)
    sim_id, stock_id = str(data.get("sim") or ""), str(data.get("articleStock") or "")
    raw = str(data.get("system_id") or "").strip()
    key = raw or (
        f"{stock_id}:{sim_id}:{data['transaction_date']}:{amount:g}"
        if data.get("transaction_date")
        else ""
    )
    lookup = IdempotencyLookup(
        type="RECHARGE",
        author=author,
        system_id=build_idempotency_key("recharge", f"{author}:{key}" if key else ""),
        legacy_system_id=raw,
        mobile_message_id=str(data.get("mobile_message_id") or ""),
    )
    existing = await find_idempotent_transaction(engine, lookup)
    if existing:
        return str(existing["id"])
    if amount <= 0:
        raise CommerceValidationError("Montant invalide")
    if not sim_id:
        raise CommerceValidationError("Client invalide")
    if not stock_id:
        raise CommerceValidationError("Article invalide")
    stock = await record(engine, "buma_article_stock", stock_id, "Article invalide", lock=False)
    async with engine.connect() as connection:
        sim_view = (
            (
                await connection.execute(
                    text("SELECT * FROM buma_sim_list_view WHERE id=:id LIMIT 1"), {"id": sim_id}
                )
            )
            .mappings()
            .first()
        )
    if sim_view is None:
        raise CommerceValidationError("Sim invalide")
    sim = await record(engine, "buma_sim", sim_id, "Sim invalide", lock=False)
    article = await record(engine, "buma_article", stock["article"], "Article invalide", lock=False)
    customer_id = str(sim_view.get("customer") or "").replace('"', "", 2)
    await record(engine, "buma_customer", customer_id, "Client invalide", lock=False)
    bonus_rate = (
        float(sim.get("operator_bonus_rate") or 0)
        if sim.get("isPaani") and article.get("kind") == "UV_PAANI"
        else 0
    )
    bonus = amount * bonus_rate / 100
    credited = amount + bonus
    supplement = 0
    if _uses_supplement(sim, article.get("kind") or ""):
        agent = await _supplement_agent(engine, sim)
        supplement = amount * float((agent or {}).get("percent") or 0) / 100
    debit = amount + supplement

    async def operation(active):
        async with active.connect() as connection:
            await acquire_idempotency_lock(connection, lookup)
            existing = await find_idempotent_transaction(connection, lookup)
        if existing:
            return str(existing["id"])
        stock = await record(active, "buma_article_stock", stock_id, "Article invalide")
        customer = await record(active, "buma_customer", customer_id, "Client invalide")
        old = float(stock.get("quantity") or 0)
        await save(active, "buma_article_stock", stock_id, {"quantity": old - debit})
        sale = await create(
            active,
            "buma_sale",
            {
                "customer": customer_id,
                "amount": data.get("amount"),
                "total": data.get("amount"),
                "recharge_sent": data.get("sent") or False,
                "sim": sim_id,
                "author": author,
                "percent_amount": debit if debit > amount else 0,
                "stock_debit_amount": debit,
                "credited_amount": credited,
                "operator_bonus_amount": bonus,
                "operator_bonus_rate": bonus_rate,
                "status": True,
                "kind": "UV",
                "type": "SALE",
            },
        )
        transaction = await create(
            active,
            "buma_transaction",
            {
                "sale": sale["id"],
                "system_id": lookup.system_id,
                "article": stock["article"],
                "stockage": stock["stockage"],
                "article_stock": stock_id,
                "mobile_transaction_date": _date(data.get("transaction_date")),
                "mobile_message_id": data.get("mobile_message_id") or "",
                "quantity": debit,
                "supplement": supplement,
                "stock_debit_amount": debit,
                "credited_amount": credited,
                "operator_bonus_amount": bonus,
                "operator_bonus_rate": bonus_rate,
                "notes": data.get("notes") or "",
                "price": 1,
                "sim": sim_id,
                "old_quantity": old,
                "type": "RECHARGE",
                "author": author,
                "status": True,
            },
        )
        previous = float(customer.get("solde") or 0)
        await save(active, "buma_customer", customer_id, {"solde": previous - credited})
        why = (
            f"Recharge UV - Montant saisi: {amount:g} - Bonus opérateur: {bonus:g}"
            f" - Montant crédité: {credited:g}"
            if bonus > 0
            else f"Recharge UV - Montant: {credited:g}"
        )
        await _raw_history(
            active,
            previous=previous,
            new=previous - credited,
            history_type="DEBT",
            why=why,
            sale=sale["id"],
        )
        return transaction["id"]

    try:
        return await run_record_storage_transaction(engine, operation)
    except Exception:
        existing = await find_idempotent_transaction(engine, lookup)
        if existing:
            return str(existing["id"])
        raise


async def cancel_recharge(engine: Any, sale_id: str) -> dict[str, str]:
    from app.commerce.returns import record, save

    async def operation(active):
        sale = await record(active, "buma_sale", sale_id, "Vente invalide")
        if not sale.get("status"):
            raise CommerceValidationError("Cette recharge a déjà été annulée")
        async with active.connect() as connection:
            tx = (
                (
                    await connection.execute(
                        text("SELECT * FROM buma_transaction WHERE sale=:id LIMIT 1 FOR UPDATE"),
                        {"id": sale_id},
                    )
                )
                .mappings()
                .first()
            )
        if tx is None:
            raise CommerceValidationError("Transaction invalide")
        stock = await record(active, "buma_article_stock", tx["article_stock"], "Article invalide")
        sim = await record(active, "buma_sim", sale.get("sim") or "", "Sim invalide")
        customer = await record(active, "buma_customer", sale["customer"], "Client invalide")
        article = await record(
            active, "buma_article", stock["article"], "Article invalide", lock=False
        )
        stored_debit = float(tx.get("stock_debit_amount") or 0)
        quantity = float(tx.get("quantity") or 0)
        debit = stored_debit if stored_debit > 0 else quantity
        credited = float(sale.get("credited_amount") or 0)
        refund = credited if credited > 0 else quantity
        if credited <= 0 and _uses_supplement(sim, article.get("kind") or ""):
            agent = await _supplement_agent(active, sim)
            refund = (
                quantity / (1 + float(agent.get("percent") or 0) / 100)
                if agent
                else quantity / 1.05
            )
        bonus = float(sale.get("operator_bonus_amount") or tx.get("operator_bonus_amount") or 0)
        previous = float(customer.get("solde") or 0)
        await save(
            active,
            "buma_article_stock",
            stock["id"],
            {"quantity": float(stock.get("quantity") or 0) + debit},
        )
        await save(active, "buma_customer", customer["id"], {"solde": previous + refund})
        why = (
            f"Annulation de recharge UV - Remboursement client: {refund:g}"
            f" - Bonus opérateur annulé: {bonus:g}"
            if bonus > 0
            else f"Annulation de recharge UV - Remboursement: {refund:g}"
        )
        await _raw_history(
            active,
            previous=previous,
            new=previous + refund,
            history_type="PAYMENT",
            why=why,
            sale=sale_id,
        )
        await save(active, "buma_sale", sale_id, {"status": False})
        await save(active, "buma_transaction", tx["id"], {"status": False})
        return {"message": "Enregistrement reussi !"}

    return await run_record_storage_transaction(engine, operation)


async def create_purchase(engine: Any, data: dict[str, Any], author: str) -> str:
    from app.commerce.sequential import create_purchase as legacy_operation

    return await legacy_operation(engine, data, author)


async def create_return(engine: Any, data: dict[str, Any], author: str) -> str:
    from app.commerce.returns import new_return

    return await new_return(engine, data, author)


async def cancel_sale(engine: Any, sale_id: str) -> dict[str, str]:
    from app.commerce.sequential import cancel_sale as legacy_operation

    return await legacy_operation(engine, sale_id)


async def receive_purchase(engine: Any, buy_id: str, author: str) -> str:
    from app.commerce.sequential import receive_purchase as legacy_operation

    return await legacy_operation(engine, buy_id, author)


async def cancel_purchase(engine: Any, buy_id: str, author: str) -> str:
    from app.commerce.sequential import cancel_purchase as legacy_operation

    return await legacy_operation(engine, buy_id, author)


async def add_purchase_payment(engine: Any, data: dict[str, Any], author: str) -> dict[str, Any]:
    from app.commerce.sequential import add_purchase_payment as legacy_operation

    return await legacy_operation(engine, data, author)


async def cancel_purchase_wallet_transaction(engine: Any, transaction_id: str, author: str) -> str:
    from app.commerce.sequential import cancel_purchase_wallet_transaction as legacy_operation

    return await legacy_operation(engine, transaction_id, author)


async def cancel_return(engine: Any, return_id: str, *, is_admin: bool = True) -> str:
    async def operation(active: Any) -> str:
        async with active.connect() as connection:
            retour = (
                (
                    await connection.execute(
                        text('SELECT * FROM "buma_return" WHERE id=:id FOR UPDATE'),
                        {"id": return_id},
                    )
                )
                .mappings()
                .first()
            )
            if retour is None:
                raise CommerceValidationError("Retour non existant")
            if not retour.get("status"):
                raise CommerceValidationError("Retour déjà annulé")
            if retour.get("payed") and not is_admin:
                raise CommerceForbiddenError("Vous n'avez pas les droits")
            transaction = (
                (
                    await connection.execute(
                        text(
                            'SELECT * FROM "buma_transaction" WHERE "return"=:id LIMIT 1 FOR UPDATE'
                        ),
                        {"id": return_id},
                    )
                )
                .mappings()
                .first()
            )
            if transaction is None:
                raise CommerceValidationError("Transaction non existante")
            await connection.execute(
                text(
                    'UPDATE "buma_article_stock" SET quantity=quantity-:q, '
                    "updated=NOW() WHERE id=:id"
                ),
                {"q": float(retour.get("amount") or 0), "id": transaction["article_stock"]},
            )
            await connection.execute(
                text('UPDATE "buma_return" SET status=FALSE,updated=NOW() WHERE id=:id'),
                {"id": return_id},
            )
        return "Retour annulé"

    return await run_record_storage_transaction(engine, operation)


async def caisse_add_return(engine: Any, data: dict[str, Any], author: str) -> str:
    return_id = str(data.get("returnId") or "")
    caisse_id = str(data.get("caisse") or "")
    if not return_id:
        raise CommerceValidationError("Identifiant de retour non existant")

    async def operation(active: Any) -> str:
        async with active.connect() as connection:
            caisse, session = await _current_caisse_session(
                connection,
                caisse_id,
                allowed_caisse_states=("OPEN", "REQUEST_OPEN", "REQUEST_CLOSE"),
            )
            retour = (
                (
                    await connection.execute(
                        text('SELECT * FROM "buma_return" WHERE id=:id FOR UPDATE'),
                        {"id": return_id},
                    )
                )
                .mappings()
                .first()
            )
            if retour is None:
                raise CommerceValidationError("Retour non existant")
            if retour.get("payed"):
                raise CommerceValidationError("Retour deja valider")
            amount = int(retour.get("amount") or 0)
            duplicate = (
                await connection.execute(
                    text("""SELECT id FROM buma_caisse_transactions
                WHERE caisse=:caisse AND type='RETURN' AND retour=:retour
                  AND created>=NOW()-INTERVAL '2 seconds' AND status=TRUE LIMIT 1"""),
                    {"caisse": caisse_id, "retour": return_id},
                )
            ).first()
            if duplicate:
                raise CommerceValidationError(
                    "Une transaction identique a été effectuée il y a moins de 2 secondes. "
                    "Veuillez patienter avant de réessayer."
                )
            customer_balance = (
                await connection.execute(
                    text("SELECT solde FROM buma_customer WHERE id=:id"),
                    {"id": retour.get("customer") or ""},
                )
            ).scalar_one_or_none()
            before = int(caisse.get("balance") or 0)
            transaction_no = await _next_transaction_no(connection, "buma_caisse_transactions")
            await connection.execute(
                text('UPDATE "buma_return" SET payed=TRUE,note=:note,updated=NOW() WHERE id=:id'),
                {"note": data.get("note") or "", "id": return_id},
            )
            await connection.execute(
                text('UPDATE "buma_caisse" SET balance=:balance,updated=NOW() WHERE id=:id'),
                {"balance": before - amount, "id": caisse_id},
            )
        await _create_caisse_transaction(
            active,
            {
                "author": author,
                "amount": amount,
                "note": "Retour UV",
                "caisse": caisse_id,
                "retour": return_id,
                "type": "RETURN",
                "customer": retour.get("customer") or "",
                "session": session["id"],
                "status": True,
                "previous_caisse_balance": before,
                "after_caisse_balance": before - amount,
                **(
                    {
                        "before_customer_solde": int(customer_balance),
                        "after_customer_solde": int(customer_balance),
                    }
                    if customer_balance is not None
                    else {}
                ),
                "transaction_no": transaction_no,
            },
        )
        return "Ok"

    return await run_record_storage_transaction(engine, operation)


async def versement_uv(engine: Any, data: dict[str, Any], author: str) -> dict[str, Any]:
    caisse_id = str(data.get("caisse") or "")
    if data.get("grouped") is True:
        raw_ids = data.get("returnIds")
        if not isinstance(raw_ids, list) or not raw_ids:
            raise CommerceValidationError("Veuillez choisir au moins un retour")
        target_id = str(data.get("target_customer") or "")
        if not target_id:
            raise CommerceValidationError("Veuillez choisir le client cible")
        return_ids = list(dict.fromkeys(str(value) for value in raw_ids if value))
    else:
        return_id = str(data.get("returnId") or "")
        if not return_id:
            raise CommerceValidationError("Identifiant de retour non existant")
        return_ids, target_id = [return_id], str(data.get("target_customer") or "")

    async def operation(active: Any) -> dict[str, Any]:
        async with active.connect() as connection:
            caisse, session = await _current_caisse_session(
                connection,
                caisse_id,
                allowed_caisse_states=("OPEN", "REQUEST_OPEN", "REQUEST_CLOSE"),
            )
            target = (
                (await _lock_rows(connection, "buma_customer", [target_id]))[0]
                if target_id
                else None
            )
            if target is None:
                # The single-return source defaults to the original customer.
                target = None
            returns = []
            total = 0.0
            for return_id in return_ids:
                retour = (
                    (
                        await connection.execute(
                            text('SELECT * FROM "buma_return" WHERE id=:id FOR UPDATE'),
                            {"id": return_id},
                        )
                    )
                    .mappings()
                    .first()
                )
                if retour is None:
                    raise CommerceValidationError("Retour introuvable")
                amount = int(retour.get("amount") or 0)
                if amount < 0:
                    message = (
                        f"Montant invalide pour le retour {retour.get('number') or return_id}"
                        if data.get("grouped") is True
                        else "Montant invalide"
                    )
                    raise CommerceValidationError(message)
                if retour.get("payed"):
                    raise CommerceValidationError(
                        f"Retour {retour.get('number') or return_id} deja valider"
                        if data.get("grouped") is True
                        else "Retour deja valider"
                    )
                original_id = str(retour.get("customer") or "")
                original = (await _lock_rows(connection, "buma_customer", [original_id]))[0]
                if target is None:
                    target = original
                returns.append((dict(retour), original, amount))
                total += amount
            if not returns:
                raise CommerceValidationError("Veuillez choisir au moins un retour")
            if data.get("fromUnknown") is True and data.get("grouped") is not True:
                unknown_session = (
                    await connection.execute(
                        text("SELECT id FROM buma_caisse_open_close WHERE id=:id"),
                        {"id": caisse.get("unknow_balance_session") or ""},
                    )
                ).first()
                if not unknown_session:
                    raise CommerceValidationError("Session introuvable")
                if int(caisse.get("unknown_balance") or 0) < total:
                    raise CommerceValidationError("Solde insuffisant")
            before = int(caisse.get("balance") or 0)
            target_name = (
                str(target.get("firstname") or "") + " " + str(target.get("lastname") or "")
            )
            suffix = f"| {data['note']}" if data.get("note") else ""
            running = before
            tx_no = await _next_transaction_no(connection, "buma_caisse_transactions")
            for retour, original, amount in returns:
                await connection.execute(
                    text(
                        'UPDATE "buma_return" SET payed=TRUE,note=:note,updated=NOW() WHERE id=:id'
                    ),
                    {"note": data.get("note") or "", "id": retour["id"]},
                )
                await _create_caisse_transaction(
                    active,
                    {
                        "author": author,
                        "amount": amount,
                        "note": f"Retour UV versé au client ({target_name})" + suffix,
                        "caisse": caisse_id,
                        "retour": retour["id"],
                        "type": "RETURN",
                        "customer": original["id"],
                        "session": session["id"],
                        "status": True,
                        "previous_caisse_balance": running,
                        "after_caisse_balance": running - amount,
                        "before_customer_solde": original.get("solde") or 0,
                        "after_customer_solde": original.get("solde") or 0,
                        "transaction_no": tx_no,
                    },
                )
                running -= amount
                tx_no += 1
            target_before = float(target.get("solde") or 0)
            if data.get("grouped") is True:
                note_parts = [f"Versement groupé de retours UV au client ({target_name})"]
                if data.get("note"):
                    note_parts.append(data["note"])
                note_parts.append(
                    "\n".join(f"{r.get('number') or r['id']}: {a:g} FCFA" for r, _, a in returns)
                )
                grouped_note = "\n".join(note_parts)
            else:
                original_name = (
                    str(original.get("firstname") or "") + " " + str(original.get("lastname") or "")
                )
                grouped_note = f"Versement de retour par le client ({original_name})" + suffix
            grouped_tx = await _create_caisse_transaction(
                active,
                {
                    "author": author,
                    "amount": total,
                    "note": grouped_note,
                    "caisse": caisse_id,
                    "type": "VERSEMENT",
                    **({"retour": returns[0][0]["id"]} if data.get("grouped") is not True else {}),
                    "customer": target["id"],
                    "session": session["id"],
                    "status": True,
                    "previous_caisse_balance": running,
                    "after_caisse_balance": before,
                    "before_customer_solde": target_before,
                    "after_customer_solde": target_before + total,
                    "transaction_no": tx_no,
                },
            )
            await connection.execute(
                text('UPDATE "buma_customer" SET solde=:solde,updated=NOW() WHERE id=:id'),
                {"solde": target_before + total, "id": target["id"]},
            )
        return {
            "success": True,
            "message": "Versement UV groupé réussi"
            if data.get("grouped") is True
            else "Versement UV réussi",
            "caisse_transaction": grouped_tx["id"],
            **(
                {"return_count": len(returns), "amount": total}
                if data.get("grouped") is True
                else {}
            ),
        }

    return await run_record_storage_transaction(engine, operation)


async def add_payment_to_sale(
    engine: Any,
    sale_id: str,
    caisse_id: str,
    amount: float,
    method: str,
    author: str,
    note: str | None = None,
) -> dict[str, Any]:
    if not sale_id:
        raise CommerceValidationError("ID de vente manquant")
    if not caisse_id:
        raise CommerceValidationError("ID de caisse manquant")
    if amount <= 0:
        raise CommerceValidationError("Montant invalide")
    if not method:
        raise CommerceValidationError("M�thode de paiement manquante")

    async def operation(active: Any):
        async with active.connect() as connection:
            sale = (
                (
                    await connection.execute(
                        text('SELECT * FROM "buma_sale" WHERE id=:id FOR UPDATE'), {"id": sale_id}
                    )
                )
                .mappings()
                .first()
            )
            if sale is None:
                raise CommerceValidationError("Vente introuvable")
            if not sale.get("status"):
                raise CommerceValidationError("Cette vente a été annulée")
            paid, total = float(sale.get("payed") or 0), float(sale.get("amount") or 0)
            if amount > total - paid:
                raise CommerceValidationError(
                    f"Le montant du paiement ({amount:g}) dépasse le montant "
                    f"restant à payer ({total - paid:g})"
                )
            caisse, session = await _current_caisse_session(connection, caisse_id)
            customer = await _commerce_lock(
                connection,
                "buma_customer",
                sale.get("customer") or "",
                "Client de la vente introuvable",
            )
            before_caisse, before_customer = (
                float(caisse.get("balance") or 0),
                float(customer.get("solde") or 0),
            )
            tx_no = await _next_transaction_no(connection, "buma_caisse_transactions")
            await connection.execute(
                text(
                    'UPDATE "buma_sale" SET payed=:payed,remaining=:remaining,'
                    "updated=NOW() WHERE id=:id"
                ),
                {"payed": paid + amount, "remaining": total - paid - amount, "id": sale_id},
            )
            await connection.execute(
                text('UPDATE "buma_caisse" SET balance=:balance,updated=NOW() WHERE id=:id'),
                {"balance": before_caisse + amount, "id": caisse_id},
            )
            await connection.execute(
                text('UPDATE "buma_customer" SET solde=:solde,updated=NOW() WHERE id=:id'),
                {"solde": before_customer + amount, "id": customer["id"]},
            )
        caisse_tx = await _create_caisse_transaction(
            active,
            {
                "caisse": caisse_id,
                "session": session["id"],
                "amount": amount,
                "type": "VERSEMENT",
                "author": author,
                "status": True,
                "sale": sale_id,
                "note": note or f"Paiement additionnel pour la vente #{sale_id}",
                "customer": customer["id"],
                "previous_caisse_balance": before_caisse,
                "after_caisse_balance": before_caisse + amount,
                "before_customer_solde": before_customer,
                "after_customer_solde": before_customer + amount,
                "transaction_no": tx_no,
            },
        )
        payment = await create_record(
            active,
            await resolve_collection(active, "buma_payment"),
            {
                "sale": sale_id,
                "amount": amount,
                "method": method,
                "caisse_transaction": caisse_tx["id"],
                "author": author,
                "status": True,
            },
        )
        await _raw_history(
            active,
            previous=before_customer,
            new=before_customer + amount,
            history_type="PAYMENT",
            why=f"Paiement de vente - Montant: {amount:g}",
            sale=sale_id,
            caisse_transaction=caisse_tx["id"],
        )
        return {
            "success": True,
            "message": "Paiement ajouté avec succès",
            "caisse_transaction": caisse_tx["id"],
            "payment": payment["id"],
            "sale": {
                "id": sale_id,
                "newPayed": paid + amount,
                "newRemaining": total - paid - amount,
            },
        }

    return await run_record_storage_transaction(engine, operation)


async def _purchase_quantity(active: Any, article_id: str, quantity: float) -> float:
    from app.commerce.returns import record

    article = await record(
        active, "buma_article", article_id, f"Article introuvable: {article_id}", lock=False
    )
    if article.get("kind") == "NAFAMA":
        config = await record(
            active, "buma_config", "CONFIGURATION_1", "Configuration introuvable", lock=False
        )
        return quantity + quantity * float(config.get("nafama_percent") or 0) / 100
    return quantity


async def _commerce_lock(connection: Any, table: str, identifier: str, message: str):
    result = (
        (
            await connection.execute(
                text(f'SELECT * FROM "{table}" WHERE id=:id FOR UPDATE'), {"id": identifier}
            )
        )
        .mappings()
        .first()
    )
    if result is None:
        raise CommerceValidationError(message)
    return dict(result)
