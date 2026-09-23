#!/usr/bin/env python3
"""Isolated HTTP proof of the five legacy stock routes, including source defects."""

from __future__ import annotations

import asyncio
import json
import secrets
import sys
from datetime import UTC, datetime
from pathlib import Path

import httpx
from ppbase.core.id_generator import generate_id
from sqlalchemy import text
from sqlalchemy.ext.asyncio import create_async_engine

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from app.commerce.sequential import create, read  # noqa: E402


async def main():
    engine = create_async_engine(
        "postgresql+asyncpg://ppbase:ppbase@localhost:55434/buma_master_ppbase",
        connect_args={"ssl": "disable"},
    )
    made = []
    user = ""
    name = "C10" + secrets.token_hex(4)

    async def fixture(table, data):
        row = await create(engine, table, data)
        made.append((table, row["id"]))
        return row["id"]

    async def query(sql, params=None):
        async with engine.begin() as connection:
            result = await connection.execute(text(sql), params or {})
            return [dict(row) for row in result.mappings()] if result.returns_rows else []

    try:
        role = await fixture("roles", {"name": name, "permission": {}, "status": True})
        user = await fixture(
            "users",
            {
                "email": name + "@example.com",
                "username": name,
                "name": name,
                "role": role,
                "password": "abcde",
                "passwordConfirm": "abcde",
            },
        )
        a = await fixture(
            "buma_article",
            {
                "designation": name,
                "sell_price": 10,
                "buy_price": 5,
                "status": True,
                "kind": "AUTRE",
            },
        )
        b = await fixture(
            "buma_article", {"designation": name + "Nafama", "status": True, "kind": "NAFAMA"}
        )
        source = await fixture(
            "buma_article_stockage", {"name": name + "Source", "status": True, "author": user}
        )
        destination = await fixture(
            "buma_article_stockage", {"name": name + "Target", "status": True, "author": user}
        )
        stock = await fixture(
            "buma_article_stock",
            {"stockage": source, "article": a, "quantity": 5, "author": user, "status": True},
        )
        nafama = await fixture(
            "buma_article_stock",
            {"stockage": destination, "article": b, "quantity": 0, "author": user, "status": True},
        )
        _empty = await fixture(
            "buma_article_stock", {"article": a, "quantity": 3, "author": user, "status": True}
        )
        async with httpx.AsyncClient(base_url="http://127.0.0.1:8090", timeout=30) as client:
            auth = await client.post(
                "/api/collections/users/auth-with-password",
                json={"identity": name, "password": "abcde"},
            )
            assert auth.status_code == 200, auth.text
            headers = {"Authorization": auth.json()["token"]}

            async def post(path, data, status=200):
                r = await client.post("/api2/stockage/" + path, json=data, headers=headers)
                if r.status_code != status:
                    print("FAILED", path, r.status_code, r.text)
                assert r.status_code == status, (path, r.status_code, r.text)
                return r.json() if r.content else None

            body = {
                "from": source,
                "destination": destination,
                "articles": [{"stockId": stock, "articleId": a, "quantity": 12}],
            }
            # No new permission or insufficient-stock check is added to legacy transfer.
            assert await post("transfer", body) == {"message": "ok"}
            assert (await read(engine, "buma_article_stock", stock))["quantity"] == -7
            target = (
                await query(
                    "SELECT * FROM buma_article_stock WHERE stockage=:s AND article=:a",
                    {"s": destination, "a": a},
                )
            )[0]
            assert target["quantity"] == 12
            assert (
                len(
                    await query(
                        "SELECT id FROM buma_transfer_transaction WHERE author=:u", {"u": user}
                    )
                )
                == 1
            )
            for route_id in (source, destination, "does-not-exist"):
                r = await client.get("/api2/stockage/stock_info/" + route_id, headers=headers)
                assert r.status_code == 200, r.text
                expected = (
                    await query("""SELECT COUNT(*) AS count,
                    COUNT(*) FILTER(WHERE s.quantity=0) AS empty,
                    COALESCE(SUM(s.quantity) FILTER(WHERE s.quantity>0),0) AS qty,
                    COALESCE(SUM(s.quantity*a.sell_price),0) AS value,
                    COALESCE(SUM(s.quantity*(a.sell_price-a.buy_price)),0) AS profit
                    FROM buma_article_stock s
                    LEFT JOIN buma_article a ON a.id=s.article
                    WHERE s.stockage='' """)
                )[0]
                assert r.json() == {
                    "totalItems": expected["count"],
                    "noStockCount": expected["empty"],
                    "stockCount": expected["qty"],
                    "value": expected["value"],
                    "benefice": expected["profit"],
                }, r.text
            assert await post("add_article", {"stockage": source, "articles": [b, b]}) == {
                "message": "ok"
            }
            additions = await query(
                "SELECT * FROM buma_article_stock WHERE stockage=:s AND article=:a",
                {"s": source, "a": b},
            )
            assert len(additions) == 2 and all(row["status"] is False for row in additions)
            # Remove only the first match, no permission gate; a missing record gives empty 200.
            assert await post("remove_article", {"stockage": source, "articles": [b]}) == {
                "message": "ok"
            }
            assert (
                len(
                    await query(
                        "SELECT id FROM buma_article_stock WHERE stockage=:s AND article=:a",
                        {"s": source, "a": b},
                    )
                )
                == 1
            )
            assert (
                await post("remove_article", {"stockage": source, "articles": [generate_id()]})
                is None
            )
            await query(
                "UPDATE roles SET permission=CAST(:p AS jsonb) WHERE id=:id",
                {"p": json.dumps({"stockage": {"update": False, "canConvert": True}}), "id": role},
            )
            assert await post("add_article", {"stockage": source, "articles": [b]}, 401) == {
                "message": "Unauthorized"
            }
            assert await post(
                "convert", {"fromId": target["id"], "toId": nafama, "quantity": "5"}, 403
            ) == {"message": "Not allow to convert stock"}
            await query(
                "UPDATE roles SET permission=CAST(:p AS jsonb) WHERE id=:id",
                {"p": json.dumps({"isAdmin": True}), "id": role},
            )
            assert await post(
                "convert", {"fromId": target["id"], "toId": nafama, "quantity": "5"}
            ) == {"message": "okay"}
            assert (await read(engine, "buma_article_stock", target["id"]))["quantity"] == 7
            assert (await read(engine, "buma_article_stock", nafama))["quantity"] == 6
            conversions = await query("SELECT * FROM buma_conversion WHERE author=:u", {"u": user})
            assert len(conversions) == 1
            assert (
                conversions[0]["from_article"] == target["id"]
                and conversions[0]["to_article"] == nafama
            )
            transactions = await query(
                "SELECT * FROM buma_transaction WHERE conversion=:id", {"id": conversions[0]["id"]}
            )
            assert sorted((row["type"], row["quantity"]) for row in transactions) == [
                ("CONVERSION_IN", 6),
                ("CONVERSION_OUT", 5),
            ]
            assert await post(
                "convert", {"fromId": target["id"], "toId": nafama, "quantity": 100}, 400
            ) == {"message": "stock not enough"}
            assert await post(
                "convert", {"fromId": generate_id(), "toId": nafama, "quantity": "5"}
            ) == {"message": "okay"}
            # A later item fails after the first movement was already committed.
            before = (await read(engine, "buma_article_stock", target["id"]))["quantity"]
            await post(
                "transfer",
                {
                    "from": destination,
                    "destination": source,
                    "articles": [
                        {"stockId": target["id"], "articleId": a, "quantity": 2},
                        {"stockId": generate_id(), "articleId": a, "quantity": 1},
                    ],
                },
                400,
            )
            assert (await read(engine, "buma_article_stock", target["id"]))[
                "quantity"
            ] == before - 2
            assert (
                len(await query("SELECT id FROM buma_transfer WHERE author=:u", {"u": user})) == 2
            )
    finally:
        if user:
            for table in (
                "buma_transfer_transaction",
                "buma_transaction",
                "buma_transfer",
                "buma_conversion",
                "buma_article_stock",
            ):
                await query(f'DELETE FROM "{table}" WHERE author=:u', {"u": user})
        for table, rid in reversed(made):
            await query(f'DELETE FROM "{table}" WHERE id=:id', {"id": rid})
        await engine.dispose()
    report = {
        "timestamp_utc": datetime.now(UTC).isoformat(),
        "status": "passed",
        "routes": 5,
        "proofs": [
            "legacy_transfer_negative_stock",
            "partial_transfer_failure",
            "nafama_ceil_5_percent",
            "conversion_swallowed_error",
            "stock_info_wrong_path_parameter",
            "permission_flags",
            "first_match_deletion",
            "new_stock_default_status_false",
            "cleanup",
        ],
        "cleanup": True,
    }
    (Path(__file__).parents[1] / "docs/c10-stockage-runtime-report.json").write_text(
        json.dumps(report, indent=2) + "\n"
    )
    print(
        "C10 legacy stock: 5 routes, persisted effects, partial failures, permissions, cleanup OK"
    )


if __name__ == "__main__":
    asyncio.run(main())
