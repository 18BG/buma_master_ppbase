"""Ordered legacy stock writes from src/entries/stockage.ts."""

from __future__ import annotations

import math
import re
from typing import Any

from ppbase.core.id_generator import generate_id
from ppbase.services.record_service import delete_record, get_all_collections, resolve_collection
from ppbase.services.record_storage_coordinator import run_record_storage_transaction
from sqlalchemy import text

from app.commerce.sequential import create, read, save


def parse_int(value: Any) -> int:
    found = re.match(r"^\s*([+-]?\d+)", str(value))
    if found is None:
        raise ValueError("NaN")
    return int(found[1])


async def transfer(engine: Any, data: dict, author: str):
    source, destination = data.get("from") or "", data.get("destination") or ""
    transfer_record = await create(
        engine,
        "buma_transfer",
        {
            "stockage": source,
            "to_stockage": destination,
            "deliveryMan": data.get("deliveryMan") or "",
            "author": author,
            "note": data.get("note") or "",
            "status": True,
        },
    )
    for item in data["articles"]:
        stock = await read(engine, "buma_article_stock", item["stockId"], "Record not found")
        quantity = float(item["quantity"])
        previous = int(stock.get("quantity") or 0)
        await save(engine, "buma_article_stock", stock["id"], {"quantity": previous - quantity})
        await create(
            engine,
            "buma_transaction",
            {
                "article": item["articleId"],
                "stockage": source,
                "article_stock": stock["id"],
                "quantity": quantity,
                "old_quantity": previous,
                "type": "TRANSFER_OUT",
                "transfer": transfer_record["id"],
                "status": True,
                "author": author,
            },
        )
        async with engine.connect() as connection:
            row = (
                (
                    await connection.execute(
                        text(
                            "SELECT * FROM buma_article_stock "
                            "WHERE stockage=:s AND article=:a LIMIT 1"
                        ),
                        {"s": destination, "a": item["articleId"]},
                    )
                )
                .mappings()
                .first()
            )
        target = dict(row) if row else None
        target_previous = int((target or {}).get("quantity") or 0)
        if target:
            await save(
                engine, "buma_article_stock", target["id"], {"quantity": target_previous + quantity}
            )
        else:
            target = await create(
                engine,
                "buma_article_stock",
                {
                    "stockage": destination,
                    "article": item["articleId"],
                    "quantity": quantity,
                    "author": author,
                    "status": True,
                },
            )
        await create(
            engine,
            "buma_transaction",
            {
                "article": item["articleId"],
                "stockage": destination,
                "article_stock": target["id"],
                "quantity": quantity,
                "old_quantity": target_previous,
                "type": "TRANSFER_IN",
                "transfer": transfer_record["id"],
                "status": True,
                "author": author,
            },
        )
        await create(
            engine,
            "buma_transfer_transaction",
            {
                "article": item["articleId"],
                "stockage": source,
                "to_stockage": destination,
                "article_stock": target["id"],
                "transfer": transfer_record["id"],
                "quantity": quantity,
                "author": author,
                "status": True,
            },
        )
    return {"message": "ok"}


async def stock_info(engine: Any):
    # The registered parameter is :name; source reads the nonexistent :stockId.
    result = {"totalItems": 0, "noStockCount": 0, "stockCount": 0, "value": 0, "benefice": 0}
    queries = {
        "stockCount": (
            "SELECT SUM(quantity) FROM buma_article_stock WHERE stockage=:id AND quantity>0"
        ),
        "noStockCount": "SELECT COUNT(*) FROM buma_article_stock WHERE stockage=:id AND quantity=0",
        "totalItems": "SELECT COUNT(*) FROM buma_article_stock WHERE stockage=:id",
        "value": (
            "SELECT SUM(s.quantity*a.sell_price) FROM buma_article_stock s "
            "JOIN buma_article a ON a.id=s.article WHERE s.stockage=:id"
        ),
        "benefice": (
            "SELECT SUM(s.quantity*a.buy_price) FROM buma_article_stock s "
            "JOIN buma_article a ON a.id=s.article WHERE s.stockage=:id"
        ),
    }
    for field, query in queries.items():
        try:
            async with engine.connect() as connection:
                value = (await connection.execute(text(query), {"id": ""})).scalar_one()
            result[field] = (
                (result["value"] - (value or 0)) if field == "benefice" else (value or 0)
            )
        except Exception:
            result[field] = 0
    return result


async def add_articles(engine: Any, data: dict, author: str):
    for article in data["articles"]:
        # dao.saveRecord bypasses request-form required-field validation. The
        # source omits status although its schema marks it required; it is false.
        async with engine.begin() as connection:
            await connection.execute(
                text("""INSERT INTO buma_article_stock
                (id,created,updated,article,stockage,quantity,author,status)
                VALUES (:id,NOW(),NOW(),:article,:stockage,0,:author,FALSE)"""),
                {
                    "id": generate_id(),
                    "article": article,
                    "stockage": data.get("stockage") or "",
                    "author": author,
                },
            )
    return {"message": "ok"}


async def remove_articles(engine: Any, data: dict):
    try:
        for article in data["articles"]:
            async with engine.connect() as connection:
                row = (
                    (
                        await connection.execute(
                            text(
                                "SELECT * FROM buma_article_stock "
                                "WHERE article=:a AND stockage=:s LIMIT 1"
                            ),
                            {"a": article, "s": data.get("stockage") or ""},
                        )
                    )
                    .mappings()
                    .first()
                )
            if row is None:
                raise LookupError("sql: no rows in result set")
            if int(row.get("quantity") or 0) == 0:

                async def delete_one(active, identifier=row["id"]):
                    collection = await resolve_collection(active, "buma_article_stock")
                    await delete_record(
                        active,
                        collection,
                        identifier,
                        all_collections=await get_all_collections(active),
                    )

                await run_record_storage_transaction(engine, delete_one)
        return {"message": "ok"}
    except Exception:
        # Source logs the exception but writes no response body.
        return None


async def convert(engine: Any, data: dict, author: str):
    try:
        quantity = parse_int(data.get("quantity"))
        source = await read(engine, "buma_article_stock", data.get("fromId") or "")
        target = await read(engine, "buma_article_stock", data.get("toId") or "")
        # The source accesses these before its explicit not-found branch.
        payload = {
            "from_article": source["id"],
            "to_article": target["id"],
            "stockage": target["stockage"],
            "quantity": quantity,
            "author": author,
            "status": True,
        }
        if int(source.get("quantity") or 0) < quantity:
            return 400, {"message": "stock not enough"}
        before_from, before_to = int(source.get("quantity") or 0), int(target.get("quantity") or 0)
        article = await read(
            engine, "buma_article", target.get("article") or "", "Record not found"
        )
        credited = (
            quantity + math.ceil(quantity * 0.05) if article.get("kind") == "NAFAMA" else quantity
        )
        await save(
            engine,
            "buma_article_stock",
            source["id"],
            {"quantity": float(source.get("quantity") or 0) - quantity},
        )
        await save(engine, "buma_article_stock", target["id"], {"quantity": before_to + credited})
        conversion = await create(engine, "buma_conversion", payload)
        for stock, q, old, kind in (
            (source, quantity, before_from, "CONVERSION_OUT"),
            (target, credited, before_to, "CONVERSION_IN"),
        ):
            await create(
                engine,
                "buma_transaction",
                {
                    "article": stock["article"],
                    "stockage": stock["stockage"],
                    "article_stock": stock["id"],
                    "quantity": q,
                    "old_quantity": old,
                    "type": kind,
                    "status": True,
                    "conversion": conversion["id"],
                    "author": author,
                },
            )
    except Exception:
        pass
    return 200, {"message": "okay"}
