"""Article, stock and stockage behavior from the frozen BUMA source.

Every mutation in this module runs through PPBase's record/storage
coordinator.  Stock rows are locked in PostgreSQL before their quantities are
read, so a failed batch or a concurrent request cannot leave partial stock or
transaction rows behind.
"""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any

from ppbase.core.id_generator import generate_id
from ppbase.models.record import format_datetime
from ppbase.services.record_service import create_record, resolve_collection
from ppbase.services.record_storage_coordinator import run_record_storage_transaction
from sqlalchemy import text

from app.transactions.idempotency import (
    IdempotencyLookup,
    acquire_idempotency_lock,
    build_idempotency_key,
    find_idempotent_transaction,
)


class StockageValidationError(ValueError):
    """Raised when an article or stock payload is invalid."""


def _number(value: Any, field: str, *, positive: bool = False) -> float:
    try:
        result = float(value)
    except (TypeError, ValueError) as exc:
        raise StockageValidationError(f"{field} invalide") from exc
    if positive and result <= 0:
        raise StockageValidationError(f"{field} doit être supérieur à 0")
    if result < 0:
        raise StockageValidationError(f"{field} invalide")
    return result


async def _rows_by_ids(connection: Any, table: str, ids: list[str]) -> dict[str, dict[str, Any]]:
    wanted = sorted({str(item) for item in ids if str(item)})
    if not wanted:
        return {}
    rows = (
        (
            await connection.execute(
                text(f'SELECT * FROM "{table}" WHERE id = ANY(:ids) ORDER BY id FOR UPDATE'),
                {"ids": wanted},
            )
        )
        .mappings()
        .all()
    )
    return {str(row["id"]): dict(row) for row in rows}


async def _stock_by_storage_article(
    connection: Any, stockage: str, article: str, *, lock: bool = True
) -> dict[str, Any] | None:
    suffix = " FOR UPDATE" if lock else ""
    row = (
        (
            await connection.execute(
                text(
                    'SELECT * FROM "buma_article_stock" '
                    "WHERE stockage = :stockage AND article = :article "
                    f"ORDER BY id LIMIT 1{suffix}"
                ),
                {"stockage": stockage, "article": article},
            )
        )
        .mappings()
        .first()
    )
    return dict(row) if row else None


async def _lock_stock_key(connection: Any, stockage: str, article: str) -> None:
    await connection.execute(
        text("SELECT pg_advisory_xact_lock(hashtextextended(:key, 0))"),
        {"key": f"buma:stock:{stockage}:{article}"},
    )


async def _insert_raw_transaction(active: Any, values: dict[str, Any]) -> dict[str, Any]:
    """Insert a historical transaction value absent from the corrected select metadata.

    The frozen source writes these values (`WALLET_TO_UV`, `UV_TO_WALLET`,
    `CONVERSION_CANCEL_*`).  PostgreSQL has no enum/check constraint for the
    PocketBase select field, so preserving the source value here is safe and
    keeps the mismatch visible in the contract documentation.
    """

    now = datetime.now(UTC)
    record = {
        "id": generate_id(),
        "created": now,
        "updated": now,
        "price": 0,
        "quantity": 0,
        "sale": "",
        "article": "",
        "stockage": "",
        "author": "",
        "article_stock": "",
        "buy": "",
        "type": "",
        "status": True,
        "transfer": "",
        "conversion": "",
        "sim": "",
        "notes": "",
        "return": "",
        "mobile_transaction_date": None,
        "mobile_message_id": "",
        "supplement": 0,
        "transaction_message": "",
        "old_quantity": 0,
        "article_stock_session": "",
        "system_id": "",
        "manual": False,
        "stock_debit_amount": 0,
        "credited_amount": 0,
        "operator_bonus_amount": 0,
        "operator_bonus_rate": 0,
    }
    record.update(values)
    columns = list(record)
    sql = (
        'INSERT INTO "buma_transaction" ('
        + ", ".join(f'"{column}"' for column in columns)
        + ") VALUES ("
        + ", ".join(f":{column}" for column in columns)
        + ")"
    )
    async with active.connect() as connection:
        await connection.execute(text(sql), record)
    return {
        "id": record["id"],
        "created": format_datetime(now),
        "updated": format_datetime(now),
        "collectionName": "buma_transaction",
    }


async def create_articles_batch(
    engine: Any, articles: list[dict[str, Any]], author: str
) -> list[dict[str, Any]]:
    """Create articles and their zero-quantity row in the main stockage."""

    if not isinstance(articles, list) or not articles:
        raise StockageValidationError("La liste des articles est obligatoire")
    normalized: list[dict[str, Any]] = []
    for item in articles:
        if not isinstance(item, dict) or not str(item.get("designation") or "").strip():
            raise StockageValidationError("Le nom de l'article est obligatoire")
        normalized.append(
            {
                "designation": str(item["designation"]).strip(),
                "sell_price": _number(item.get("sell_price"), "Le prix de vente", positive=True),
                "buy_price": _number(item.get("buy_price", 0), "Le prix d'achat"),
                "categorie": (
                    str(item.get("categorie") or "")
                    if len(str(item.get("categorie") or "")) >= 10
                    else ""
                ),
                "barcode": str(item.get("barcode") or ""),
                "kind": str(item.get("kind") or ""),
                "trackStock": bool(item.get("trackStock")),
            }
        )

    async def operation(active: Any) -> list[dict[str, Any]]:
        article_collection = await resolve_collection(active, "buma_article")
        stock_collection = await resolve_collection(active, "buma_article_stock")
        async with active.connect() as connection:
            main_stock = (
                await connection.execute(
                    text('SELECT id FROM "buma_article_stockage" WHERE id = :id LIMIT 1'),
                    {"id": "STOCK_PRINCIPAL"},
                )
            ).first()
        if main_stock is None:
            raise StockageValidationError("Stockage principal introuvable")

        created: list[dict[str, Any]] = []
        for item in normalized:
            article = await create_record(
                active,
                article_collection,
                {**item, "status": True},
            )
            await create_record(
                active,
                stock_collection,
                {
                    "stockage": "STOCK_PRINCIPAL",
                    "article": article["id"],
                    "quantity": 0,
                    "status": True,
                    "author": author,
                },
            )
            created.append(article)
        return created

    return await run_record_storage_transaction(engine, operation)


async def make_stock_movement(engine: Any, user: str, data: dict[str, Any]) -> dict[str, Any]:
    if not isinstance(data, dict) or not str(data.get("articleStockId") or ""):
        raise StockageValidationError("L'ID de l'article en stock est requis")
    quantity = _number(data.get("quantity"), "La quantité", positive=True)
    if not isinstance(data.get("isEntry"), bool):
        raise StockageValidationError("Le type de mouvement est requis")

    async def operation(active: Any) -> dict[str, Any]:
        transaction_collection = await resolve_collection(active, "buma_transaction")
        async with active.connect() as connection:
            stock = (
                await _rows_by_ids(connection, "buma_article_stock", [str(data["articleStockId"])])
            ).get(str(data["articleStockId"]))
            if stock is None:
                raise StockageValidationError("Article en stock introuvable")
            current = float(stock.get("quantity") or 0)
            if not data["isEntry"] and current < quantity:
                raise StockageValidationError(
                    f"Stock insuffisant. Quantité disponible: {current:g}"
                )
            new_quantity = current + quantity if data["isEntry"] else current - quantity
            await connection.execute(
                text(
                    'UPDATE "buma_article_stock" SET quantity=:quantity, updated=NOW() WHERE id=:id'
                ),
                {"quantity": new_quantity, "id": stock["id"]},
            )
        return await create_record(
            active,
            transaction_collection,
            {
                "article": stock["article"],
                "stockage": stock["stockage"],
                "article_stock": stock["id"],
                "quantity": quantity,
                "old_quantity": current,
                "type": "INPUT" if data["isEntry"] else "OUTPUT",
                "notes": str(
                    data.get("notes")
                    or ("Entrée manuelle" if data["isEntry"] else "Sortie manuelle")
                ),
                "author": user,
                "status": True,
            },
        )

    return await run_record_storage_transaction(engine, operation)


async def make_batch_stock_movements(
    engine: Any, user: str, movements: list[dict[str, Any]]
) -> list[dict[str, Any]]:
    if not isinstance(movements, list) or not movements:
        raise StockageValidationError("Aucun mouvement de stock à effectuer")
    normalized: list[dict[str, Any]] = []
    for movement in movements:
        if not isinstance(movement, dict) or not str(movement.get("articleStockId") or ""):
            raise StockageValidationError("L'ID de l'article en stock est requis")
        if not isinstance(movement.get("isEntry"), bool):
            raise StockageValidationError("Le type de mouvement est requis")
        normalized.append(
            {
                **movement,
                "quantity": _number(movement.get("quantity"), "La quantité", positive=True),
            }
        )

    async def operation(active: Any) -> list[dict[str, Any]]:
        transaction_collection = await resolve_collection(active, "buma_transaction")
        ids = [str(item["articleStockId"]) for item in normalized]
        async with active.connect() as connection:
            stocks = await _rows_by_ids(connection, "buma_article_stock", ids)
            if len(stocks) != len(set(ids)):
                missing = next(item for item in ids if item not in stocks)
                raise StockageValidationError(f"Article en stock introuvable: {missing}")
        quantities = {stock_id: float(row.get("quantity") or 0) for stock_id, row in stocks.items()}
        results: list[dict[str, Any]] = []
        for movement in normalized:
            stock_id = str(movement["articleStockId"])
            current = quantities[stock_id]
            quantity = float(movement["quantity"])
            if not movement["isEntry"] and current < quantity:
                raise StockageValidationError(
                    f"Stock insuffisant pour l'article {stocks[stock_id]['article']}. "
                    f"Quantité disponible: {current:g}, demandée: {quantity:g}"
                )
            new_quantity = current + quantity if movement["isEntry"] else current - quantity
            async with active.connect() as connection:
                await connection.execute(
                    text(
                        'UPDATE "buma_article_stock" SET quantity=:quantity, '
                        "updated=NOW() WHERE id=:id"
                    ),
                    {"quantity": new_quantity, "id": stock_id},
                )
            quantities[stock_id] = new_quantity
            results.append(
                await create_record(
                    active,
                    transaction_collection,
                    {
                        "article": stocks[stock_id]["article"],
                        "stockage": stocks[stock_id]["stockage"],
                        "article_stock": stock_id,
                        "quantity": quantity,
                        "old_quantity": current,
                        "manual": True,
                        "type": "INPUT" if movement["isEntry"] else "OUTPUT",
                        "notes": str(
                            movement.get("notes")
                            or ("Entrée manuelle" if movement["isEntry"] else "Sortie manuelle")
                        ),
                        "author": user,
                        "status": True,
                    },
                )
            )
        return results

    return await run_record_storage_transaction(engine, operation)


async def make_stockage_transfer(engine: Any, user: str, data: dict[str, Any]) -> dict[str, Any]:
    source = str(data.get("from") or "")
    destination = str(data.get("destination") or "")
    articles = data.get("articles")
    if not source or not destination or source == destination:
        raise StockageValidationError("Stockage source/destination invalide")
    if not isinstance(articles, list) or not articles:
        raise StockageValidationError("Aucun article à transférer")
    normalized = []
    for item in articles:
        if not isinstance(item, dict) or not item.get("stockId") or not item.get("articleId"):
            raise StockageValidationError("Article de transfert invalide")
        normalized.append(
            {**item, "quantity": _number(item.get("quantity"), "La quantité", positive=True)}
        )

    async def operation(active: Any) -> dict[str, Any]:
        transfer_collection = await resolve_collection(active, "buma_transfer")
        transfer_transaction_collection = await resolve_collection(
            active, "buma_transfer_transaction"
        )
        transaction_collection = await resolve_collection(active, "buma_transaction")
        async with active.connect() as connection:
            for item in normalized:
                await _lock_stock_key(connection, source, str(item["articleId"]))
                await _lock_stock_key(connection, destination, str(item["articleId"]))
            source_stocks = await _rows_by_ids(
                connection, "buma_article_stock", [str(item["stockId"]) for item in normalized]
            )
            for item in normalized:
                stock = source_stocks.get(str(item["stockId"]))
                if (
                    stock is None
                    or str(stock.get("stockage")) != source
                    or str(stock.get("article")) != str(item["articleId"])
                ):
                    raise StockageValidationError("Article source introuvable")
                if float(stock.get("quantity") or 0) < float(item["quantity"]):
                    raise StockageValidationError("Stock insuffisant pour le transfert")
            storage_exists = (
                await connection.execute(
                    text('SELECT id FROM "buma_article_stockage" WHERE id=:id LIMIT 1'),
                    {"id": destination},
                )
            ).first()
            if storage_exists is None:
                raise StockageValidationError("Stockage destination introuvable")
        transfer = await create_record(
            active,
            transfer_collection,
            {
                "stockage": source,
                "to_stockage": destination,
                "deliveryMan": str(data.get("deliveryMan") or ""),
                "author": user,
                "note": str(data.get("note") or ""),
                "status": True,
            },
        )
        for item in normalized:
            stock_id = str(item["stockId"])
            quantity = float(item["quantity"])
            source_stock = source_stocks[stock_id]
            source_old = float(source_stock.get("quantity") or 0)
            async with active.connect() as connection:
                await connection.execute(
                    text(
                        'UPDATE "buma_article_stock" SET quantity=:quantity, '
                        "updated=NOW() WHERE id=:id"
                    ),
                    {"quantity": source_old - quantity, "id": stock_id},
                )
                destination_stock = await _stock_by_storage_article(
                    connection, destination, str(item["articleId"])
                )
            if destination_stock is None:
                destination_record = await create_record(
                    active,
                    await resolve_collection(active, "buma_article_stock"),
                    {
                        "stockage": destination,
                        "article": str(item["articleId"]),
                        "quantity": quantity,
                        "author": user,
                        "status": True,
                    },
                )
                destination_stock = {"id": destination_record["id"], "quantity": quantity}
            else:
                destination_old = float(destination_stock.get("quantity") or 0)
                async with active.connect() as connection:
                    await connection.execute(
                        text(
                            'UPDATE "buma_article_stock" SET quantity=:quantity, '
                            "updated=NOW() WHERE id=:id"
                        ),
                        {"quantity": destination_old + quantity, "id": destination_stock["id"]},
                    )
            await create_record(
                active,
                transaction_collection,
                {
                    "article": item["articleId"],
                    "stockage": source,
                    "article_stock": stock_id,
                    "quantity": quantity,
                    "old_quantity": source_old,
                    "type": "TRANSFER_OUT",
                    "transfer": transfer["id"],
                    "status": True,
                    "author": user,
                },
            )
            await create_record(
                active,
                transaction_collection,
                {
                    "article": item["articleId"],
                    "stockage": destination,
                    "article_stock": destination_stock["id"],
                    "quantity": quantity,
                    "old_quantity": float(destination_stock.get("quantity") or 0) - quantity,
                    "type": "TRANSFER_IN",
                    "transfer": transfer["id"],
                    "status": True,
                    "author": user,
                },
            )
            await create_record(
                active,
                transfer_transaction_collection,
                {
                    "article": item["articleId"],
                    "stockage": source,
                    "to_stockage": destination,
                    "article_stock": destination_stock["id"],
                    "transfer": transfer["id"],
                    "quantity": quantity,
                    "author": user,
                    "status": True,
                },
            )
        return transfer

    return await run_record_storage_transaction(engine, operation)


async def cancel_stockage_transfer(engine: Any, transfer_id: str) -> dict[str, Any]:
    if not transfer_id:
        raise StockageValidationError("ID de transfert manquant")

    async def operation(active: Any) -> dict[str, Any]:
        async with active.connect() as connection:
            transfer = (
                (
                    await connection.execute(
                        text('SELECT * FROM "buma_transfer" WHERE id=:id FOR UPDATE'),
                        {"id": transfer_id},
                    )
                )
                .mappings()
                .first()
            )
            if transfer is None:
                raise StockageValidationError("Transfer non existant")
            if not transfer.get("status"):
                raise StockageValidationError("Transfer déjà annulé")
            rows = (
                (
                    await connection.execute(
                        text(
                            'SELECT * FROM "buma_transfer_transaction" '
                            "WHERE transfer=:id AND status=TRUE FOR UPDATE"
                        ),
                        {"id": transfer_id},
                    )
                )
                .mappings()
                .all()
            )
            if not rows:
                raise StockageValidationError("Transaction non existante")
            for row in rows:
                await _lock_stock_key(connection, str(row["stockage"]), str(row["article"]))
                await _lock_stock_key(connection, str(row["to_stockage"]), str(row["article"]))
        for row in rows:
            quantity = float(row.get("quantity") or 0)
            async with active.connect() as connection:
                destination = await _rows_by_ids(
                    connection, "buma_article_stock", [str(row["article_stock"])]
                )
                destination_stock = destination.get(str(row["article_stock"]))
                source_stock = await _stock_by_storage_article(
                    connection, str(row["stockage"]), str(row["article"])
                )
            if destination_stock is None or source_stock is None:
                raise StockageValidationError("Article en stock introuvable")
            destination_quantity = float(destination_stock.get("quantity") or 0)
            if destination_quantity < quantity:
                raise StockageValidationError("Stock insuffisant pour annuler le transfert")
            async with active.connect() as connection:
                await connection.execute(
                    text(
                        'UPDATE "buma_article_stock" SET quantity=:quantity, '
                        "updated=NOW() WHERE id=:id"
                    ),
                    {"quantity": destination_quantity - quantity, "id": destination_stock["id"]},
                )
                await connection.execute(
                    text(
                        'UPDATE "buma_article_stock" SET quantity=quantity+:quantity, '
                        "updated=NOW() WHERE id=:id"
                    ),
                    {"quantity": quantity, "id": source_stock["id"]},
                )
                await connection.execute(
                    text(
                        'UPDATE "buma_transfer_transaction" SET status=FALSE, '
                        "updated=NOW() WHERE id=:id"
                    ),
                    {"id": row["id"]},
                )
                await connection.execute(
                    text(
                        'UPDATE "buma_transaction" SET status=FALSE, '
                        "updated=NOW() WHERE transfer=:transfer"
                    ),
                    {"transfer": transfer_id},
                )
        async with active.connect() as connection:
            await connection.execute(
                text('UPDATE "buma_transfer" SET status=FALSE, updated=NOW() WHERE id=:id'),
                {"id": transfer_id},
            )
        return {"message": "Transaction annulée"}

    return await run_record_storage_transaction(engine, operation)


async def convert_stock(engine: Any, user: str, data: dict[str, Any]) -> str:
    quantity = _number(data.get("quantity"), "La quantité", positive=True)
    from_id, to_id = str(data.get("fromId") or ""), str(data.get("toId") or "")
    if not from_id or not to_id or from_id == to_id:
        raise StockageValidationError("Conversion de stock invalide")
    mobile_id = str(data.get("mobile_message_id") or "").strip()
    raw_system = str(data.get("system_id") or "").strip()
    if not raw_system and data.get("transaction_date"):
        raw_system = f"{from_id}:{to_id}:{data['transaction_date']}:{quantity:g}"
    idem = build_idempotency_key("conversion", f"{user}:{raw_system}" if raw_system else "")
    lookup = IdempotencyLookup(
        type="CONVERSION_OUT",
        author=user,
        system_id=idem,
        legacy_system_id=str(data.get("system_id") or "").strip(),
        mobile_message_id=mobile_id,
    )
    existing = await find_idempotent_transaction(engine, lookup)
    if existing:
        return str(existing.get("conversion") or existing["id"])

    async def operation(active: Any) -> str:
        transaction_collection = await resolve_collection(active, "buma_transaction")
        conversion_collection = await resolve_collection(active, "buma_conversion")
        async with active.connect() as connection:
            await acquire_idempotency_lock(connection, lookup)
            concurrent = await find_idempotent_transaction(connection, lookup)
            if concurrent:
                return str(concurrent.get("conversion") or concurrent["id"])
            stocks = await _rows_by_ids(connection, "buma_article_stock", [from_id, to_id])
            if len(stocks) != 2:
                raise StockageValidationError("Stock introuvable")
            config = (
                (
                    await connection.execute(
                        text('SELECT * FROM "buma_config" WHERE id=:id LIMIT 1'),
                        {"id": "CONFIGURATION_1"},
                    )
                )
                .mappings()
                .first()
            )
            negative = bool(config and config.get("enable_conversion_negative"))
            from_stock, to_stock = stocks[from_id], stocks[to_id]
            from_quantity = float(from_stock.get("quantity") or 0)
            if not negative and from_quantity < quantity:
                raise StockageValidationError("Stock insuffisant")
            article = (
                (
                    await connection.execute(
                        text('SELECT kind FROM "buma_article" WHERE id=:id LIMIT 1'),
                        {"id": to_stock["article"]},
                    )
                )
                .mappings()
                .first()
            )
            percent = float(config.get("nafama_percent") or 0) if config else 0
            updated_quantity = (
                quantity + quantity * percent / 100
                if article and article.get("kind") == "NAFAMA"
                else quantity
            )
            transaction_date = data.get("transaction_date")
            if transaction_date:
                try:
                    transaction_date = datetime.fromisoformat(
                        str(transaction_date).replace("Z", "+00:00")
                    )
                except ValueError as exc:
                    raise StockageValidationError("Date de transaction invalide") from exc
            else:
                transaction_date = datetime.now(UTC)
        conversion = await create_record(
            active,
            conversion_collection,
            {
                "from_article": from_id,
                "to_article": to_id,
                "stockage": to_stock["stockage"],
                "quantity": quantity,
                "author": user,
                "status": True,
                "transaction_date": transaction_date,
            },
        )
        to_old_quantity = float(to_stock.get("quantity") or 0)
        async with active.connect() as connection:
            await connection.execute(
                text(
                    'UPDATE "buma_article_stock" SET quantity=:quantity, updated=NOW() WHERE id=:id'
                ),
                {"quantity": from_quantity - quantity, "id": from_id},
            )
            await connection.execute(
                text(
                    'UPDATE "buma_article_stock" SET quantity=:quantity, updated=NOW() WHERE id=:id'
                ),
                {"quantity": to_old_quantity + updated_quantity, "id": to_id},
            )
        await create_record(
            active,
            transaction_collection,
            {
                "article": from_stock["article"],
                "stockage": from_stock["stockage"],
                "article_stock": from_id,
                "quantity": quantity,
                "old_quantity": from_quantity,
                "type": "CONVERSION_OUT",
                "status": True,
                "system_id": idem,
                "mobile_transaction_date": transaction_date,
                "mobile_message_id": mobile_id,
                "conversion": conversion["id"],
                "author": user,
            },
        )
        await create_record(
            active,
            transaction_collection,
            {
                "article": to_stock["article"],
                "stockage": to_stock["stockage"],
                "article_stock": to_id,
                "quantity": updated_quantity,
                "old_quantity": to_old_quantity,
                "type": "CONVERSION_IN",
                "status": True,
                "mobile_transaction_date": transaction_date,
                "mobile_message_id": mobile_id,
                "conversion": conversion["id"],
                "author": user,
            },
        )
        return str(conversion["id"])

    try:
        return await run_record_storage_transaction(engine, operation)
    except Exception:
        existing = await find_idempotent_transaction(engine, lookup)
        if existing:
            return str(existing.get("conversion") or existing["id"])
        raise


async def cancel_stock_conversion(engine: Any, conversion_id: str, author: str) -> dict[str, Any]:
    if not conversion_id:
        raise StockageValidationError("ID de conversion non spécifié")

    async def operation(active: Any) -> dict[str, Any]:
        async with active.connect() as connection:
            conversion = (
                (
                    await connection.execute(
                        text('SELECT * FROM "buma_conversion" WHERE id=:id FOR UPDATE'),
                        {"id": conversion_id},
                    )
                )
                .mappings()
                .first()
            )
            if conversion is None:
                raise StockageValidationError("Conversion introuvable")
            if not conversion.get("status"):
                raise StockageValidationError("Cette conversion est déjà annulée")
            tx_rows = (
                (
                    await connection.execute(
                        text(
                            'SELECT * FROM "buma_transaction" '
                            "WHERE conversion=:id AND status=TRUE FOR UPDATE"
                        ),
                        {"id": conversion_id},
                    )
                )
                .mappings()
                .all()
            )
            out_row = next((row for row in tx_rows if row.get("type") == "CONVERSION_OUT"), None)
            in_row = next((row for row in tx_rows if row.get("type") == "CONVERSION_IN"), None)
            if out_row is None or in_row is None:
                raise StockageValidationError("Transactions de conversion incomplètes")
            stocks = await _rows_by_ids(
                connection,
                "buma_article_stock",
                [out_row["article_stock"], in_row["article_stock"]],
            )
            from_stock, to_stock = (
                stocks.get(out_row["article_stock"]),
                stocks.get(in_row["article_stock"]),
            )
            if from_stock is None or to_stock is None:
                raise StockageValidationError("Articles en stock introuvables")
            out_quantity = float(out_row.get("quantity") or 0)
            article = (
                (
                    await connection.execute(
                        text('SELECT kind FROM "buma_article" WHERE id=:id LIMIT 1'),
                        {"id": to_stock["article"]},
                    )
                )
                .mappings()
                .first()
            )
            config = (
                (
                    await connection.execute(
                        text('SELECT nafama_percent FROM "buma_config" WHERE id=:id LIMIT 1'),
                        {"id": "CONFIGURATION_1"},
                    )
                )
                .mappings()
                .first()
            )
            percent = float(config.get("nafama_percent") or 0) if config else 0
            expected = (
                out_quantity + out_quantity * percent / 100
                if article and article.get("kind") == "NAFAMA"
                else out_quantity
            )
            if float(to_stock.get("quantity") or 0) < expected:
                raise StockageValidationError("Stock insuffisant pour annuler la conversion")
            await connection.execute(
                text(
                    'UPDATE "buma_article_stock" SET quantity=quantity+:quantity, '
                    "updated=NOW() WHERE id=:id"
                ),
                {"quantity": out_quantity, "id": from_stock["id"]},
            )
            await connection.execute(
                text(
                    'UPDATE "buma_article_stock" SET quantity=quantity-:quantity, '
                    "updated=NOW() WHERE id=:id"
                ),
                {"quantity": expected, "id": to_stock["id"]},
            )
            await connection.execute(
                text(
                    'UPDATE "buma_transaction" SET status=FALSE, updated=NOW() WHERE conversion=:id'
                ),
                {"id": conversion_id},
            )
            # The corrected export has no cancel_author/cancel_date columns;
            # cancellation audit rows below retain the source event and author.
            await connection.execute(
                text('UPDATE "buma_conversion" SET status=FALSE, updated=NOW() WHERE id=:id'),
                {"id": conversion_id},
            )
        await _insert_raw_transaction(
            active,
            {
                "article": from_stock["article"],
                "stockage": from_stock["stockage"],
                "article_stock": from_stock["id"],
                "quantity": out_quantity,
                "old_quantity": float(from_stock.get("quantity") or 0),
                "type": "CONVERSION_CANCEL_IN",
                "conversion": conversion_id,
                "author": author,
                "notes": f"Annulation de conversion #{conversion_id}",
            },
        )
        await _insert_raw_transaction(
            active,
            {
                "article": to_stock["article"],
                "stockage": to_stock["stockage"],
                "article_stock": to_stock["id"],
                "quantity": expected,
                "old_quantity": float(to_stock.get("quantity") or 0),
                "type": "CONVERSION_CANCEL_OUT",
                "conversion": conversion_id,
                "author": author,
                "notes": f"Annulation de conversion #{conversion_id}",
            },
        )
        return {"success": True, "message": "Conversion annulée avec succès"}

    return await run_record_storage_transaction(engine, operation)


async def _wallet_uv_move(
    engine: Any,
    user: str,
    data: dict[str, Any],
    *,
    to_uv: bool,
) -> dict[str, Any]:
    amount = _number(data.get("amount"), "Montant", positive=True)
    wallet_id = str(data.get("walletId") or "")
    stock_id = str(data.get("article_stock") or "")
    if not wallet_id or not stock_id:
        raise StockageValidationError("Portefeuille ou article en stock manquant")

    async def operation(active: Any) -> dict[str, Any]:
        wallet_transactions = await resolve_collection(active, "buma_wallet_transactions")
        async with active.connect() as connection:
            wallet = (await _rows_by_ids(connection, "buma_wallet", [wallet_id])).get(wallet_id)
            stock = (await _rows_by_ids(connection, "buma_article_stock", [stock_id])).get(stock_id)
            if wallet is None:
                raise StockageValidationError("Portefeuille introuvable")
            if stock is None:
                raise StockageValidationError("Article en stock introuvable")
            wallet_before = float(wallet.get("balance") or 0)
            stock_before = float(stock.get("quantity") or 0)
            if to_uv and wallet_before < amount:
                raise StockageValidationError("Solde insuffisant")
            if not to_uv and stock_before < amount:
                raise StockageValidationError("Solde insuffisant")
            article = (
                await connection.execute(
                    text('SELECT id FROM "buma_article" WHERE id=:id LIMIT 1'),
                    {"id": stock["article"]},
                )
            ).first()
            if article is None:
                raise StockageValidationError("Article introuvable")
            await connection.execute(
                text("SELECT pg_advisory_xact_lock(hashtextextended(:key, 0))"),
                {"key": "buma:wallet_transaction_no"},
            )
            transaction_no = (
                int(
                    (
                        await connection.execute(
                            text(
                                'SELECT COALESCE(MAX("transaction_no"), 0) '
                                'FROM "buma_wallet_transactions"'
                            )
                        )
                    ).scalar_one()
                    or 0
                )
                + 1
            )
            wallet_after = wallet_before - amount if to_uv else wallet_before + amount
            stock_after = stock_before + amount if to_uv else stock_before - amount
            await connection.execute(
                text('UPDATE "buma_wallet" SET balance=:balance, updated=NOW() WHERE id=:id'),
                {"balance": wallet_after, "id": wallet_id},
            )
            await connection.execute(
                text(
                    'UPDATE "buma_article_stock" SET quantity=:quantity, updated=NOW() WHERE id=:id'
                ),
                {"quantity": stock_after, "id": stock_id},
            )
        note = str(data.get("note") or ("Achat d'UV" if to_uv else "Vente d'UV"))
        transaction_date = data.get("transaction_date")
        if transaction_date:
            try:
                transaction_date = datetime.fromisoformat(
                    str(transaction_date).replace("Z", "+00:00")
                )
            except ValueError as exc:
                raise StockageValidationError("Date de transaction invalide") from exc
        else:
            transaction_date = datetime.now(UTC)
        transaction = await _insert_raw_transaction(
            active,
            {
                "article": stock["article"],
                "stockage": stock["stockage"],
                "quantity": amount,
                "type": "WALLET_TO_UV" if to_uv else "UV_TO_WALLET",
                "article_stock": stock_id,
                "notes": note,
                "author": user,
                "status": True,
                "mobile_transaction_date": transaction_date,
            },
        )
        wallet_transaction = await create_record(
            active,
            wallet_transactions,
            {
                "wallet": wallet_id,
                "amount": amount,
                "type": "WITHDRAWAL" if to_uv else "DEPOSIT",
                "note": note,
                "author": user,
                "status": True,
                "state": "DONE",
                "wallet_solde_before": wallet_before,
                "transaction_no": transaction_no,
            },
        )
        return {
            "success": True,
            "transaction": transaction,
            "wallet_transaction": wallet_transaction,
            "message": "Transfert réussi",
        }

    return await run_record_storage_transaction(engine, operation)


async def wallet_to_uv(engine: Any, user: str, data: dict[str, Any]) -> dict[str, Any]:
    return await _wallet_uv_move(engine, user, data, to_uv=True)


async def uv_to_wallet(engine: Any, user: str, data: dict[str, Any]) -> dict[str, Any]:
    return await _wallet_uv_move(engine, user, data, to_uv=False)
