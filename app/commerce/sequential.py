"""Literal save ordering for the non-transactional master sale/purchase functions.

Each save commits independently, as dao.saveRecord does outside runInTransaction.
Do not wrap these flows in an aggregate transaction: partial failure is observable.
"""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any

from ppbase.services.record_service import create_record, resolve_collection
from ppbase.services.record_storage_coordinator import run_record_storage_transaction
from sqlalchemy import text

from app.commerce.service import CommerceValidationError, _date, _raw_history


async def read(engine: Any, table: str, identifier: str, message: str | None = None):
    async with engine.connect() as connection:
        row = (
            (
                await connection.execute(
                    text(f'SELECT * FROM "{table}" WHERE id=:id'), {"id": identifier}
                )
            )
            .mappings()
            .first()
        )
    if row is None and message:
        raise CommerceValidationError(message)
    return dict(row) if row is not None else None


async def related(engine: Any, table: str, field: str, identifier: str):
    async with engine.connect() as connection:
        result = await connection.execute(
            text(f'SELECT * FROM "{table}" WHERE "{field}"=:id'), {"id": identifier}
        )
        return [dict(row) for row in result.mappings()]


async def save(engine: Any, table: str, identifier: str, values: dict[str, Any]):
    collection = await resolve_collection(engine, table)
    fields = {
        field.get("name") if isinstance(field, dict) else field.name for field in collection.schema
    }
    fields.update({"created", "updated"})
    values = {key: value for key, value in values.items() if key in fields}
    values["updated"] = datetime.now(UTC)
    assignments = ",".join(f'"{key}"=:{key}' for key in values)
    async with engine.begin() as connection:
        await connection.execute(
            text(f'UPDATE "{table}" SET {assignments} WHERE id=:_id'), {**values, "_id": identifier}
        )


async def create(engine: Any, table: str, values: dict[str, Any]):
    collection = await resolve_collection(engine, table)
    if table != "buma_sale" or values.get("kind") != "AUTRES":
        return await create_record(engine, collection, dict(values))

    async def single_record(active):
        record = await create_record(active, collection, {**values, "kind": ""})
        async with active.connect() as connection:
            await connection.execute(
                text("UPDATE buma_sale SET kind='AUTRES' WHERE id=:id"), {"id": record["id"]}
            )
        return {**record, "kind": "AUTRES"}

    return await run_record_storage_transaction(engine, single_record)


async def next_number(engine: Any, table: str, field: str):
    async with engine.connect() as connection:
        value = (
            await connection.execute(
                text(f'SELECT "{field}" FROM "{table}" ORDER BY created DESC LIMIT 1')
            )
        ).scalar_one_or_none()
    return int(value or 0) + 1


async def history(engine: Any, previous: float, new: float, kind: str, why: str, sale: str):
    async def one_record(active):
        await _raw_history(
            active, previous=previous, new=new, history_type=kind, why=why, sale=sale
        )

    await run_record_storage_transaction(engine, one_record)


async def create_sale(engine: Any, data: dict, author: str, *, allow_sale_date: bool):
    cid = data.get("customer") or ""
    if not cid:
        raise CommerceValidationError("Le client est absent")
    customer = await read(engine, "buma_customer", cid, "Client introuvable")
    paid = float(data.get("payed") or 0)
    debt = data.get("debt")
    caisse = session = None
    if (debt and paid > 0) or debt is False:
        caisse = await read(engine, "buma_caisse", data.get("caisse") or "", "Caisse non ouverte")
        if caisse.get("state") not in ("OPEN", "REQUEST_OPEN", "REQUEST_CLOSE"):
            raise CommerceValidationError("Caisse non ouverte")
        session = await read(
            engine,
            "buma_caisse_open_close",
            caisse.get("currentSession") or "",
            "Session non ouverte",
        )
        if session.get("state") not in ("OPEN", "PENDING"):
            raise CommerceValidationError("Session non ouverte")
        if caisse.get("currentCashier") != author:
            raise CommerceValidationError("Vous n'étes pas le caissier")
    items = data["items"]
    total = sum(float(i.get("quantity") or 0) * float(i.get("sell_price") or 0) for i in items)
    remaining = total - paid
    created = None
    if data.get("created") and allow_sale_date:
        try:
            created = _date(data["created"])
        except CommerceValidationError:
            pass
    sale = await create(
        engine,
        "buma_sale",
        {
            "customer": cid,
            "amount": total,
            "payed": paid,
            "profit": 0,
            "kind": "AUTRES",
            "remaining": remaining,
            "type": "SALE",
            "note": data.get("note") or "",
            "author": author,
            "status": True,
        },
    )
    sid = sale["id"]

    async def backdate(table, identifier):
        if created:
            await save(engine, table, identifier, {"created": created})

    await backdate("buma_sale", sid)
    if (not debt or paid > 0) and caisse and session:
        before = int(caisse.get("balance") or 0)
        previous = int(customer.get("solde") or 0)
        after = previous
        transaction = await create(
            engine,
            "buma_caisse_transactions",
            {
                "caisse": caisse["id"],
                "session": session["id"],
                "amount": paid,
                "type": "VERSEMENT",
                "author": author,
                "status": True,
                "sale": sid,
                "note": "Payment de la vente au client "
                + str(customer.get("firstname") or "")
                + " "
                + str(customer.get("lastname") or ""),
                "customer": cid,
                "previous_caisse_balance": before,
                "after_caisse_balance": before + paid,
                "before_customer_solde": previous,
                "after_customer_solde": previous,
                "transaction_no": await next_number(
                    engine, "buma_caisse_transactions", "transaction_no"
                ),
            },
        )
        await backdate("buma_caisse_transactions", transaction["id"])
        payment = await create(
            engine,
            "buma_payment",
            {
                "sale": sid,
                "amount": paid,
                "method": data.get("method") or "",
                "caisse_transaction": transaction["id"],
                "author": author,
                "status": True,
            },
        )
        await backdate("buma_payment", payment["id"])
        await save(engine, "buma_payment", payment["id"], {})
        await save(engine, "buma_caisse", caisse["id"], {"balance": before + paid})
        if remaining > 0:
            after = previous - remaining
            await save(engine, "buma_customer", cid, {"solde": after})
            await history(
                engine,
                previous,
                after,
                "DEBT",
                f"Vente à crédit partielle - Montant restant: {remaining:g}",
                sid,
            )
        await save(
            engine, "buma_caisse_transactions", transaction["id"], {"after_customer_solde": after}
        )
    elif debt and paid <= 0:
        previous = int(customer.get("solde") or 0)
        await save(engine, "buma_customer", cid, {"solde": previous - total})
        await history(
            engine,
            previous,
            previous - total,
            "DEBT",
            f"Vente à crédit complète - Montant: {total:g}",
            sid,
        )
    for item in items:
        old = None
        if item.get("stockage"):
            stock = await read(engine, "buma_article_stock", item.get("article_stock") or "")
            if stock is None:
                async with engine.connect() as connection:
                    row = (
                        (
                            await connection.execute(
                                text(
                                    "SELECT * FROM buma_article_stock WHERE article=:article "
                                    "AND stockage=:stockage LIMIT 1"
                                ),
                                {
                                    "article": item.get("article") or "",
                                    "stockage": item["stockage"],
                                },
                            )
                        )
                        .mappings()
                        .first()
                    )
                stock = dict(row) if row else None
            if stock:
                old = int(stock.get("quantity") or 0)
                await save(
                    engine,
                    "buma_article_stock",
                    stock["id"],
                    {"quantity": old - float(item["quantity"])},
                )
        transaction = await create(
            engine,
            "buma_transaction",
            {
                "article": item.get("article") or "",
                "sale": sid,
                "type": "SALE",
                "quantity": item.get("quantity"),
                "author": author,
                "price": item.get("sell_price"),
                "article_stock": item.get("article_stock") or "",
                "old_quantity": old,
                "status": True,
                "stockage": item.get("stockage") or "",
            },
        )
        await backdate("buma_transaction", transaction["id"])
        await save(engine, "buma_transaction", transaction["id"], {})
    return sid


async def cancel_sale(engine: Any, sale_id: str):
    sale = await read(engine, "buma_sale", sale_id, "Vente invalide")
    if not sale.get("status"):
        raise CommerceValidationError("Cette vente a déjà été annulée")
    customer = await read(engine, "buma_customer", sale.get("customer") or "", "Client invalide")
    before, amount = int(customer.get("solde") or 0), int(sale.get("amount") or 0)
    await save(engine, "buma_customer", customer["id"], {"solde": before + amount})
    await history(
        engine,
        before,
        before + amount,
        "PAYMENT",
        f"Annulation de vente - Remboursement: {amount:g}",
        sale_id,
    )
    await save(engine, "buma_sale", sale_id, {"status": False, "state": "CANCELED", "remaining": 0})
    for payment in await related(engine, "buma_payment", "sale", sale_id):
        await save(engine, "buma_payment", payment["id"], {"status": False})
    for transaction in await related(engine, "buma_transaction", "sale", sale_id):
        await save(engine, "buma_transaction", transaction["id"], {"status": False})
        stock = await read(engine, "buma_article_stock", transaction.get("article_stock") or "")
        article = await read(
            engine, "buma_article", transaction.get("article") or "", "Article invalide"
        )
        if article.get("trackStock"):
            if stock is None:
                raise CommerceValidationError("Article invalide")
            await save(
                engine,
                "buma_article_stock",
                stock["id"],
                {
                    "quantity": int(stock.get("quantity") or 0)
                    + int(transaction.get("quantity") or 0)
                },
            )
    return {"message": "Enregistrement reussi !"}


async def received_quantity(engine: Any, article: dict | None, quantity: float):
    if article and article.get("kind") == "NAFAMA":
        config = await read(engine, "buma_config", "CONFIGURATION_1", "Configuration introuvable")
        return quantity + quantity * float(config.get("nafama_percent") or 0) / 100
    return quantity


async def create_purchase(engine: Any, data: dict, author: str):
    supplier_id = data.get("supplier") or ""
    if not supplier_id:
        raise CommerceValidationError("Fournisseur non défini")
    items = data.get("items")
    if not items:
        raise CommerceValidationError("Aucun article à acheter")
    amount = sum(float(i.get("quantity") or 0) * float(i.get("buy_price") or 0) for i in items)
    paid = float(data.get("payed") or 0)
    remaining = max(0, amount - paid)
    supplier = await read(engine, "buma_supplier", supplier_id, "Fournisseur introuvable")
    for item in items:
        await read(
            engine,
            "buma_article_stock",
            item.get("article_stock") or "",
            f"Stock introuvable pour l'article {item.get('article') or ''}",
        )
    await save(
        engine, "buma_supplier", supplier_id, {"solde": int(supplier.get("solde") or 0) - remaining}
    )
    wallet = None
    if paid > 0:
        if not data.get("wallet"):
            raise CommerceValidationError("Caisse ou porte-monnaie non défini pour le paiement")
        wallet = await read(engine, "buma_wallet", data["wallet"], "Porte-monnaie non trouvé")
        if int(wallet.get("balance") or 0) < paid:
            raise CommerceValidationError("Solde de porte-monnaie insuffisant")
        wallet["balance"] = int(wallet.get("balance") or 0) - paid
        await save(engine, "buma_wallet", wallet["id"], {"balance": wallet["balance"]})
    invoice = await next_number(engine, "buma_buy", "invoice_no")
    purchase = await create(
        engine,
        "buma_buy",
        {
            "supplier": supplier_id,
            "amount": amount,
            "discount": 0,
            "payed": paid,
            "profit": 0,
            "remaining": remaining,
            "type": "BUY",
            "invoice_no": invoice,
            "note": data.get("note") or "",
            "author": author,
            "status": True,
            "state": "PAYED" if paid == amount else "PENDING",
            "received": data.get("received") or False,
        },
    )
    pid = purchase["id"]
    if wallet:
        tx = await create(
            engine,
            "buma_wallet_transactions",
            {
                "wallet": wallet["id"],
                "amount": paid,
                "type": "PURCHASE",
                "note": f"Paiement pour l'achat #{invoice}",
                "author": author,
                "state": "DONE",
                "status": True,
                "wallet_solde_before": int(wallet["balance"]),
                "transaction_no": await next_number(
                    engine, "buma_wallet_transactions", "transaction_no"
                ),
            },
        )
        await save(engine, "buma_wallet_transactions", tx["id"], {"purchase": pid})
    for item in items:
        stock = await read(engine, "buma_article_stock", item.get("article_stock") or "")
        await create(
            engine,
            "buma_buy_item",
            {
                "buy": pid,
                "article": item.get("article"),
                "article_stock": item.get("article_stock"),
                "price": item.get("buy_price"),
                "received": data.get("received") or False,
                "status": True,
                "author": author,
                "quantity": item.get("quantity"),
            },
        )
        if data.get("received"):
            if stock is None:
                raise CommerceValidationError(
                    f"Stock introuvable pour l'article {item.get('article')}"
                )
            old = float(stock.get("quantity") or 0)
            article = await read(engine, "buma_article", item.get("article") or "")
            quantity = await received_quantity(engine, article, float(item.get("quantity") or 0))
            await save(engine, "buma_article_stock", stock["id"], {"quantity": old + quantity})
            await create(
                engine,
                "buma_transaction",
                {
                    "buy": pid,
                    "article": item.get("article"),
                    "stockage": stock.get("stockage"),
                    "article_stock": stock["id"],
                    "quantity": quantity,
                    "price": item.get("buy_price"),
                    "old_quantity": old,
                    "type": "BUY",
                    "author": author,
                    "status": True,
                },
            )
    return pid


async def receive_purchase(engine: Any, buy_id: str, author: str):
    purchase = await read(engine, "buma_buy", buy_id, "Achat introuvable")
    if purchase.get("received"):
        raise CommerceValidationError("Cet achat a déjà été marqué comme reçu")
    if not purchase.get("status") or purchase.get("state") == "CANCELED":
        raise CommerceValidationError("Impossible de marquer un achat annulé comme reçu")
    await save(
        engine,
        "buma_buy",
        buy_id,
        {"received": True, "received_date": True, "received_author": author},
    )
    items = await related(engine, "buma_buy_item", "buy", buy_id)
    if not items:
        raise CommerceValidationError("Aucun article trouvé pour cet achat")
    for item in items:
        stock = await read(
            engine,
            "buma_article_stock",
            item.get("article_stock") or "",
            f"Stock introuvable pour l'article {item.get('article')}",
        )
        article = await read(
            engine,
            "buma_article",
            item.get("article") or "",
            f"Article introuvable: {item.get('article')}",
        )
        quantity = await received_quantity(engine, article, float(item.get("quantity") or 0))
        old = float(stock.get("quantity") or 0)
        await save(engine, "buma_article_stock", stock["id"], {"quantity": old + quantity})
        await save(engine, "buma_buy_item", item["id"], {"received": True})
        await create(
            engine,
            "buma_transaction",
            {
                "buy": buy_id,
                "article": item.get("article"),
                "stockage": stock.get("stockage"),
                "article_stock": stock["id"],
                "quantity": quantity,
                "price": item.get("price"),
                "old_quantity": old,
                "type": "BUY",
                "author": author,
                "status": True,
            },
        )
    return "Achat marqué comme reçu avec succès"


async def cancel_purchase(engine: Any, buy_id: str, author: str):
    purchase = await read(engine, "buma_buy", buy_id, "Achat introuvable")
    if not purchase.get("status"):
        raise CommerceValidationError("Cet achat a déjà été annulé")
    if int(purchase.get("payed") or 0) > 0:
        raise CommerceValidationError("Impossible d'annuler un achat avec un paiement")
    supplier = await read(
        engine, "buma_supplier", purchase.get("supplier") or "", "Fournisseur introuvable"
    )
    await save(
        engine,
        "buma_supplier",
        supplier["id"],
        {"solde": float(supplier.get("solde") or 0) + float(purchase.get("amount") or 0)},
    )
    await save(
        engine,
        "buma_buy",
        buy_id,
        {
            "status": False,
            "state": "CANCELED",
            "remaining": 0,
            "payed": 0,
            "cancel_author": author,
            "cancel_date": datetime.now(UTC),
        },
    )
    if purchase.get("received"):
        for tx in await related(engine, "buma_transaction", "buy", buy_id):
            await save(engine, "buma_transaction", tx["id"], {"status": False})
            stock = await read(
                engine, "buma_article_stock", tx.get("article_stock") or "", "Article invalide"
            )
            await save(
                engine,
                "buma_article_stock",
                stock["id"],
                {"quantity": float(stock.get("quantity") or 0) - float(tx.get("quantity") or 0)},
            )
    for item in await related(engine, "buma_buy_item", "buy", buy_id):
        await save(engine, "buma_buy_item", item["id"], {"status": False})
    return "ok"


async def add_purchase_payment(engine: Any, data: dict, author: str):
    pid, wid = data.get("purchaseId") or "", data.get("walletId") or ""
    amount = float(data.get("amount") or 0)
    if not pid or not wid or amount == 0:
        raise CommerceValidationError(
            "Données incomplètes. Veuillez spécifier l'achat, le portefeuille et le montant."
        )
    if amount < 0:
        raise CommerceValidationError("Montant invalide")
    purchase = await read(engine, "buma_buy", pid, "Achat introuvable")
    if not purchase.get("status") or purchase.get("state") == "CANCELED":
        raise CommerceValidationError("Impossible d'ajouter un paiement à un achat annulé")
    wallet = await read(engine, "buma_wallet", wid, "Portefeuille introuvable")
    if float(wallet.get("balance") or 0) < amount:
        raise CommerceValidationError("Solde du portefeuille insuffisant")
    supplier = await read(
        engine, "buma_supplier", purchase.get("supplier") or "", "Fournisseur introuvable"
    )
    paid, total = float(purchase.get("payed") or 0), float(purchase.get("amount") or 0)
    new_paid, remaining = paid + amount, max(0, total - paid - amount)
    if new_paid > total:
        raise CommerceValidationError(
            f"Le paiement dépasse le montant total de l'achat. Maximum autorisé : {total - paid:g}"
        )
    transaction = await create(
        engine,
        "buma_wallet_transactions",
        {
            "wallet": wid,
            "amount": amount,
            "type": "PURCHASE",
            "note": data.get("note")
            or f"Paiement pour l'achat #{float(purchase.get('invoice_no') or 0):g}",
            "author": author,
            "purchase": pid,
            "status": True,
            "wallet_solde_before": int(wallet.get("balance") or 0),
            "transaction_no": await next_number(
                engine, "buma_wallet_transactions", "transaction_no"
            ),
        },
    )
    await save(engine, "buma_wallet", wid, {"balance": float(wallet.get("balance") or 0) - amount})
    values = {"payed": new_paid, "remaining": remaining}
    if remaining == 0:
        values["state"] = "PAYED"
    await save(engine, "buma_buy", pid, values)
    await save(
        engine,
        "buma_supplier",
        supplier["id"],
        {"solde": float(supplier.get("solde") or 0) + amount},
    )
    return {"message": "Paiement ajouté avec succès", "transactionId": transaction["id"]}


async def cancel_purchase_wallet_transaction(engine: Any, transaction_id: str, author: str):
    transaction = await read(
        engine,
        "buma_wallet_transactions",
        transaction_id,
        "Transaction de portefeuille introuvable",
    )
    if not transaction.get("status"):
        raise CommerceValidationError("Cette transaction est déjà annulée")
    if transaction.get("type") != "PURCHASE":
        raise CommerceValidationError("Cette transaction n'est pas de type achat")
    wallet = await read(
        engine, "buma_wallet", transaction.get("wallet") or "", "Portefeuille introuvable"
    )
    purchase = await read(engine, "buma_buy", transaction.get("purchase") or "")
    await save(
        engine,
        "buma_wallet_transactions",
        transaction_id,
        {
            "status": False,
            "cancel_author": author,
            "cancel_date": datetime.now(UTC),
        },
    )
    amount = float(transaction.get("amount") or 0)
    await save(
        engine, "buma_wallet", wallet["id"], {"balance": float(wallet.get("balance") or 0) + amount}
    )
    if purchase:
        paid = max(0, float(purchase.get("payed") or 0) - amount)
        values = {"payed": paid, "remaining": float(purchase.get("amount") or 0) - paid}
        if paid == 0 and purchase.get("state") == "PAYED":
            values["state"] = "PENDING"
        await save(engine, "buma_buy", purchase["id"], values)
        supplier = await read(engine, "buma_supplier", purchase.get("supplier") or "")
        if supplier:
            await save(
                engine,
                "buma_supplier",
                supplier["id"],
                {"solde": float(supplier.get("solde") or 0) - amount},
            )
    return "Transaction de portefeuille annulée avec succès"
