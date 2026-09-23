"""Return identity resolution and mutations from master.retour*.ts."""

from __future__ import annotations

import re
from typing import Any

from ppbase.services.record_service import (
    create_record,
    get_record,
    resolve_collection,
    update_record,
)
from ppbase.services.record_storage_coordinator import run_record_storage_transaction
from sqlalchemy import text

from app.commerce.service import (
    CommerceForbiddenError,
    CommerceNotFoundError,
    CommerceValidationError,
    _date,
)
from app.finance.service import _create_caisse_transaction, _next_transaction_no
from app.transactions.idempotency import (
    IdempotencyLookup,
    acquire_idempotency_lock,
    build_idempotency_key,
    find_idempotent_transaction,
)


async def record(active: Any, table: str, record_id: str, message: str, *, lock=True):
    async with active.connect() as connection:
        row = (
            (
                await connection.execute(
                    text(f'SELECT * FROM "{table}" WHERE id=:id' + (" FOR UPDATE" if lock else "")),
                    {"id": record_id},
                )
            )
            .mappings()
            .first()
        )
    if row is None:
        raise CommerceValidationError(message)
    return dict(row)


async def save(active: Any, table: str, record_id: str, payload: dict):
    collection = await resolve_collection(active, table)
    return await update_record(active, collection, record_id, payload)


async def create(active: Any, table: str, payload: dict):
    collection = await resolve_collection(active, table)
    return await create_record(active, collection, payload)


async def _sim_customer(engine: Any, number: str, network: str, author: str):
    if network not in {
        "RS",
        "RNS",
        "KIOSQUE",
        "TELECEL",
        "MOOV_MONEY",
        "PAANI",
        "SEMI_PAANI",
        "FLASH",
    }:
        raise CommerceValidationError("Réseau invalide")
    if not author.strip():
        raise CommerceValidationError("Auteur invalide")

    async def operation(active):
        async with active.connect() as connection:
            sim = (
                (
                    await connection.execute(
                        text("SELECT * FROM buma_sim WHERE number=:n LIMIT 1"), {"n": number}
                    )
                )
                .mappings()
                .first()
            )
        pdv = customer = None
        link = None
        if sim:
            sim = dict(sim)
            if sim.get("pdv"):
                collection = await resolve_collection(active, "buma_pdv")
                pdv = await get_record(active, collection, sim["pdv"])
                if pdv:
                    collection = await resolve_collection(active, "buma_customer")
                    if pdv.get("client"):
                        customer = await get_record(active, collection, pdv["client"])
                    async with active.connect() as connection:
                        link = (
                            (
                                await connection.execute(
                                    text("SELECT * FROM buma_customer_pdv WHERE pdv=:id LIMIT 1"),
                                    {"id": pdv["id"]},
                                )
                            )
                            .mappings()
                            .first()
                        )
                    if not customer and link and link.get("customer"):
                        customer = await get_record(active, collection, link["customer"])
            elif sim.get("agent"):
                async with active.connect() as connection:
                    row = (
                        await connection.execute(
                            text("""SELECT id FROM buma_customer
                        WHERE agent=:agent AND kind='CUSTOMER_AGENT' LIMIT 1"""),
                            {"agent": sim["agent"]},
                        )
                    ).first()
                if row:
                    customer = await get_record(
                        active, await resolve_collection(active, "buma_customer"), row[0]
                    )
        if not customer:
            customer = await create(
                active,
                "buma_customer",
                {
                    "firstname": "Client Inconnu",
                    "lastname": "",
                    "name": "Client Inconnu",
                    "kind": "CUSTOMER_PDV",
                    "status": True,
                    "solde": 0,
                    "due": 0,
                    "author": author,
                },
            )
            if not pdv:
                pdv = await create(
                    active,
                    "buma_pdv",
                    {
                        "firstname": "Client Inconnu",
                        "lastname": "",
                        "name": "Client Inconnu",
                        "client": customer["id"],
                        "status": True,
                        "daily_goal": 10000 if sim else 0,
                        "author": author,
                    },
                )
                if sim:
                    sim["pdv"] = pdv["id"]
        if pdv and customer.get("kind") == "CUSTOMER_PDV":
            await save(active, "buma_pdv", pdv["id"], {"client": customer["id"]})
            await save(active, "buma_customer", customer["id"], {"pdv": pdv["id"]})
            if link:
                await save(active, "buma_customer_pdv", link["id"], {"customer": customer["id"]})
            else:
                await create(
                    active,
                    "buma_customer_pdv",
                    {"customer": customer["id"], "pdv": pdv["id"], "author": author},
                )
        if sim:
            sim = await save(
                active, "buma_sim", sim["id"], {"network": network, "pdv": sim.get("pdv") or ""}
            )
        else:
            sim = await create(
                active,
                "buma_sim",
                {
                    "number": number,
                    "network": network,
                    "status": True,
                    "pdv": pdv["id"],
                    "author": author,
                },
            )
        return sim, pdv, customer

    # Source resolves/creates identities before its stock/balance transaction.
    return await operation(engine)


async def new_return(engine: Any, data: dict, author: str):
    match = re.match(r"^\s*([+-]?\d+)", str(data.get("amount") or ""))
    amount = int(match[1]) if match else 0
    stock_id, number = str(data.get("articleStock") or ""), str(data.get("sim_number") or "")
    mobile = str(data.get("mobile_message_id") or "")
    raw = str(data.get("system_id") or "").strip()
    key = raw or (
        f"message:{mobile}"
        if mobile
        else (
            f"{stock_id}:{number}:{data['transaction_date']}:{amount}"
            if data.get("transaction_date")
            else ""
        )
    )
    lookup = IdempotencyLookup(
        type="RETURN",
        author=author,
        system_id=build_idempotency_key("return", f"{author}:{key}" if key else ""),
        legacy_system_id=raw,
        mobile_message_id=mobile,
    )
    existing = await find_idempotent_transaction(engine, lookup)
    if existing:
        return str(existing["id"])
    if not number:
        raise CommerceValidationError("Numéro de SIM manquant")
    if len(number.strip()) != 8:
        raise CommerceValidationError("Le numéro de SIM doit faire exactement 8 caractères")
    if amount <= 0:
        raise CommerceValidationError("Montant invalide")
    if not stock_id:
        raise CommerceValidationError("Article stock manquant")
    if number == "75030827":
        raise CommerceValidationError("Cette retour est invalide")
    stock = await record(engine, "buma_article_stock", stock_id, "Article non existant", lock=False)
    article = await record(
        engine, "buma_article", stock["article"], "Article non existant", lock=False
    )
    if not article.get("status"):
        raise CommerceValidationError("Article désactivé")
    sim, pdv, customer = await _sim_customer(
        engine, number, str(article.get("kind") or "").replace("UV_", "", 1), author
    )

    async def operation(active):
        async with active.connect() as connection:
            await acquire_idempotency_lock(connection, lookup)
            found = await find_idempotent_transaction(connection, lookup)
        if found:
            return str(found["id"])
        stock = await record(active, "buma_article_stock", stock_id, "Article non existant")
        current_customer = await record(
            active, "buma_customer", customer["id"], "Client non existant"
        )
        payload = {
            "payed": False,
            "receipt": data.get("receipt") or False,
            "customer": customer["id"],
            "amount": amount,
            "status": True,
            "number": number,
            "note": data.get("note") or "",
            "mobile_message_id": mobile,
            "return_date": _date(data.get("transaction_date")),
            "author": author,
            "article_stock": stock_id,
        }
        if pdv:
            payload["pdv"] = pdv["id"]
        retour = await create(active, "buma_return", payload)
        old = int(stock.get("quantity") or 0)
        await save(active, "buma_article_stock", stock_id, {"quantity": old + amount})
        transaction = await create(
            active,
            "buma_transaction",
            {
                "article": stock["article"],
                "stockage": stock["stockage"],
                "article_stock": stock_id,
                "quantity": amount,
                "system_id": lookup.system_id,
                "mobile_transaction_date": _date(data.get("transaction_date")),
                "mobile_message_id": mobile,
                "transaction_message": data.get("transaction_message") or "",
                "sim": sim["id"],
                "old_quantity": old,
                "type": "RETURN",
                "price": 1,
                "note": "Retour d'UV",
                "return": retour["id"],
                "status": True,
                "author": author,
            },
        )
        await save(
            active,
            "buma_customer",
            customer["id"],
            {"due": int(current_customer.get("due") or 0) + amount},
        )
        return transaction["id"]

    try:
        return await run_record_storage_transaction(engine, operation)
    except Exception:
        existing = await find_idempotent_transaction(engine, lookup)
        if existing:
            return str(existing["id"])
        raise


async def update_return(engine: Any, data: dict, author: str):
    rid = str(data.get("returnId") or "")
    if not rid:
        raise CommerceValidationError("Identifiant de retour non existant")

    async def operation(active):
        try:
            retour = await record(active, "buma_return", rid, "Retour non existante")
        except CommerceValidationError as exc:
            raise CommerceNotFoundError(str(exc)) from exc
        if retour.get("payed"):
            raise CommerceValidationError("Retour deja valider")
        sid = str(data.get("articleStock") or "")
        stock = await record(active, "buma_article_stock", sid, "Article non existant")
        await record(active, "buma_pdv", str(data.get("pdv") or ""), "PDV non existant")
        caisse = session = None
        amount = float(data.get("amount") or 0)
        if data.get("caisse"):
            caisse = await record(active, "buma_caisse", data["caisse"], "Caisse non existante")
            session = await record(
                active,
                "buma_caisse_open_close",
                caisse.get("currentSession") or "",
                "Session de caisse non ouverte",
            )
            if data.get("payed") and int(caisse.get("balance") or 0) < amount:
                raise CommerceValidationError("Le solde de votre caisse est insufisant")
        old_stock = await record(
            active, "buma_article_stock", retour["article_stock"], "Article non existant"
        )
        await save(
            active,
            "buma_article_stock",
            old_stock["id"],
            {"quantity": int(old_stock.get("quantity") or 0) - int(retour.get("amount") or 0)},
        )
        # Source holds two separately fetched records even when both stock IDs match.
        await save(
            active,
            "buma_article_stock",
            sid,
            {"quantity": int(stock.get("quantity") or 0) + amount},
        )
        await save(
            active,
            "buma_return",
            rid,
            {
                "amount": data.get("amount"),
                "customer": data.get("customer") or "",
                "caisse": data.get("caisse") or "",
                "article_stock": sid,
                "pdv": data.get("pdv") or "",
                "pdv_number": data.get("pdv_number"),
            },
        )
        if data.get("payed") and caisse and session:
            balance = int(caisse.get("balance") or 0)
            customer = None
            if data.get("customer"):
                customer = await get_record(
                    active, await resolve_collection(active, "buma_customer"), data["customer"]
                )
            previous = int((customer or {}).get("solde") or 0)
            async with active.connect() as connection:
                number = await _next_transaction_no(connection, "buma_caisse_transactions")
            await save(active, "buma_caisse", caisse["id"], {"balance": balance - amount})
            await _create_caisse_transaction(
                active,
                {
                    "caisse": caisse["id"],
                    "type": "RETURN",
                    "amount": str(data["amount"]),
                    "customer": data.get("customer") or "",
                    "session": session["id"],
                    "note": "Paiment de retour d'UV",
                    "status": True,
                    "author": author,
                    "previous_caisse_balance": balance,
                    "after_caisse_balance": balance - amount,
                    "before_customer_solde": previous,
                    "after_customer_solde": previous,
                    "transaction_no": number,
                },
            )
            await save(active, "buma_return", rid, {"payed": True})
        return {"message": "Retour mis à jour"}

    return await run_record_storage_transaction(engine, operation)


async def update_return_pdv(engine: Any, data: dict):
    rid = str(data.get("returnId") or "")
    try:
        retour = await record(engine, "buma_return", rid, "Retour non existant", lock=False)
    except CommerceValidationError as exc:
        raise CommerceNotFoundError(str(exc)) from exc
    await save(
        engine,
        "buma_return",
        rid,
        {"pdv": data.get("pdv") or "", "pdv_number": data.get("pdv_number")},
    )
    if retour.get("payed"):
        await record(
            engine, "buma_caisse", retour.get("caisse") or "", "Caisse non existante", lock=False
        )
        async with engine.connect() as connection:
            row = (
                await connection.execute(
                    text("SELECT id FROM buma_caisse_transactions WHERE retour=:id LIMIT 1"),
                    {"id": rid},
                )
            ).first()
        if row:
            await save(
                engine,
                "buma_caisse_transactions",
                row[0],
                {
                    "note": "Paiment de retour d'UV (Au Point de vente "
                    f"{data.get('pdv_number', 'undefined')})"
                },
            )
    # The source does not write a success body.
    return None


async def return_info(engine: Any, rid: str):
    retour = await get_record(engine, await resolve_collection(engine, "buma_return"), rid)
    if retour is None:
        raise CommerceNotFoundError("Retour non existant")
    result = {"returnRecord": retour}
    for key, table, field in (
        ("pdv", "buma_pdv", "pdv"),
        ("customer", "buma_customer", "customer"),
        ("articleStock", "buma_article_stock", "article_stock"),
        ("caisse", "buma_caisse", "caisse"),
    ):
        if key == "caisse" and not retour.get(field):
            result[key] = None
            continue
        related = await get_record(
            engine, await resolve_collection(engine, table), retour.get(field) or ""
        )
        if related is None:
            raise CommerceValidationError("{}")
        result[key] = related
    return result


async def cancel_return_checked(engine: Any, rid: str, *, is_admin: bool):
    from app.commerce.service import cancel_return

    try:
        retour = await record(engine, "buma_return", rid, "Retour non existant", lock=False)
    except CommerceValidationError as exc:
        raise CommerceNotFoundError(str(exc)) from exc
    if not retour.get("status"):
        raise CommerceValidationError("Retour déjà annulé")
    if retour.get("payed") and not is_admin:
        raise CommerceForbiddenError("Vous n'avez pas les droits")
    return {"message": await cancel_return(engine, rid, is_admin=is_admin)}
