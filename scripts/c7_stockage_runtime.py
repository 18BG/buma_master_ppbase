#!/usr/bin/env python3
"""Runtime proof for the C7 article/stock/stockage checkpoint."""

from __future__ import annotations

import asyncio
import json
import secrets
from datetime import UTC, datetime

import httpx
from ppbase.core.id_generator import generate_id
from ppbase.services.auth_service import generate_token_key, hash_password
from sqlalchemy import text
from sqlalchemy.ext.asyncio import create_async_engine

BASE_URL = "http://127.0.0.1:8090"
DATABASE_URL = "postgresql+asyncpg://ppbase:ppbase@localhost:55434/buma_master_ppbase"


async def main() -> None:
    suffix = secrets.token_hex(4)
    role_id = f"c7role{suffix}"
    username = f"c7user{suffix}"
    email = f"{username}@example.com"
    password = "abcde"
    storage_id = f"c7st{suffix}"
    wallet_id = f"c7wa{suffix}"
    engine = create_async_engine(DATABASE_URL, connect_args={"ssl": "disable"})
    user_id = generate_id()
    article_ids: list[str] = []
    transfer_id = ""
    conversion_id = ""

    async with engine.begin() as connection:
        now = datetime.now(UTC)
        await connection.execute(
            text(
                'INSERT INTO "roles" '
                "(id, created, updated, name, permission, status, author) "
                "VALUES (:id, :created, :updated, :name, CAST(:permission AS jsonb), TRUE, '')"
            ),
            {
                "id": role_id,
                "created": now,
                "updated": now,
                "name": role_id,
                "permission": json.dumps({"isAdmin": True}),
            },
        )
        await connection.execute(
            text(
                'INSERT INTO "users" '
                "(id, created, updated, email, email_visibility, verified, password_hash, "
                "token_key, role, name, username) "
                "VALUES (:id, :created, :updated, :email, FALSE, TRUE, :password_hash, "
                ":token_key, :role, :name, :username)"
            ),
            {
                "id": user_id,
                "created": now,
                "updated": now,
                "email": email,
                "password_hash": hash_password(password),
                "token_key": generate_token_key(),
                "role": role_id,
                "name": "C7 Runtime",
                "username": username,
            },
        )
        await connection.execute(
            text(
                'INSERT INTO "buma_wallet" '
                "(id, created, updated, name, balance, author, description, status, type) "
                "VALUES (:id, :created, :updated, :name, 20, :author, '', TRUE, 'OTHER')"
            ),
            {
                "id": wallet_id,
                "created": now,
                "updated": now,
                "name": f"C7 Wallet {suffix}",
                "author": user_id,
            },
        )

    async with httpx.AsyncClient(base_url=BASE_URL, timeout=30) as client:
        login = await client.post(
            "/api/collections/users/auth-with-password",
            json={"identity": username, "password": password},
        )
        assert login.status_code == 200, login.text
        token = login.json()["token"]
        headers = {"Authorization": token}

        anonymous = await client.post(
            "/master/stockage/movement",
            json={"articleStockId": "missing", "quantity": 1, "isEntry": True},
        )
        assert anonymous.status_code == 401

        article_response = await client.post(
            "/master/article/creates",
            headers=headers,
            json={
                "articles": [
                    {
                        "designation": f"C7 Article {suffix}",
                        "sell_price": 10,
                        "buy_price": 5,
                        "kind": "AUTRE",
                        "trackStock": True,
                    }
                ]
            },
        )
        assert article_response.status_code == 200, article_response.text
        async with engine.connect() as connection:
            article_row = (
                await connection.execute(
                    text('SELECT id FROM "buma_article" WHERE designation=:designation'),
                    {"designation": f"C7 Article {suffix}"},
                )
            ).first()
        assert article_row is not None
        article_id = str(article_row[0])
        article_ids.append(article_id)

        second_article_response = await client.post(
            "/master/article/creates",
            headers=headers,
            json={
                "articles": [
                    {
                        "designation": f"C7 Article 2 {suffix}",
                        "sell_price": 12,
                        "buy_price": 6,
                        "kind": "AUTRE",
                        "trackStock": True,
                    }
                ]
            },
        )
        assert second_article_response.status_code == 200, second_article_response.text
        async with engine.connect() as connection:
            rows = (
                await connection.execute(
                    text(
                        'SELECT id FROM "buma_article" WHERE designation LIKE :prefix ORDER BY id'
                    ),
                    {"prefix": f"C7 Article%{suffix}"},
                )
            ).all()
        article_ids = [str(row[0]) for row in rows]
        assert len(article_ids) == 2

        async with engine.connect() as connection:
            stocks = (
                await connection.execute(
                    text(
                        'SELECT id, article FROM "buma_article_stock" '
                        "WHERE stockage='STOCK_PRINCIPAL' AND article = ANY(:articles) "
                        "ORDER BY article"
                    ),
                    {"articles": article_ids},
                )
            ).all()
        assert len(stocks) == 2
        stock_by_article = {str(row[1]): str(row[0]) for row in stocks}

        input_response = await client.post(
            "/master/stockage/movement",
            headers=headers,
            json={
                "articleStockId": stock_by_article[article_ids[0]],
                "quantity": 10,
                "isEntry": True,
            },
        )
        assert input_response.status_code == 200, input_response.text

        batch_response = await client.post(
            "/master/stockage/batch-movement",
            headers=headers,
            json={
                "movements": [
                    {
                        "articleStockId": stock_by_article[article_ids[0]],
                        "quantity": 2,
                        "isEntry": False,
                    },
                    {"articleStockId": "missing-c7-stock", "quantity": 1, "isEntry": True},
                ]
            },
        )
        assert batch_response.status_code == 400
        async with engine.connect() as connection:
            quantity = (
                await connection.execute(
                    text('SELECT quantity FROM "buma_article_stock" WHERE id=:id'),
                    {"id": stock_by_article[article_ids[0]]},
                )
            ).scalar_one()
        assert float(quantity) == 10

        async with engine.begin() as connection:
            now = datetime.now(UTC)
            await connection.execute(
                text(
                    'INSERT INTO "buma_article_stockage" '
                    "(id, created, updated, name, location, comment, type, status, author) "
                    "VALUES (:id, :created, :updated, :name, '', '', 'DEPOT', TRUE, '')"
                ),
                {"id": storage_id, "created": now, "updated": now, "name": f"C7 Storage {suffix}"},
            )

        transfer = await client.post(
            "/master/stockage/transfer",
            headers=headers,
            json={
                "from": "STOCK_PRINCIPAL",
                "destination": storage_id,
                "deliveryMan": "",
                "note": "C7",
                "articles": [
                    {
                        "stockId": stock_by_article[article_ids[0]],
                        "articleId": article_ids[0],
                        "quantity": 3,
                    }
                ],
            },
        )
        assert transfer.status_code == 200, transfer.text
        transfer_id = transfer.json()["id"]

        async with engine.connect() as connection:
            source_quantity, destination_quantity = (
                await connection.execute(
                    text(
                        "SELECT "
                        '(SELECT quantity FROM "buma_article_stock" WHERE id=:source), '
                        '(SELECT quantity FROM "buma_article_stock" '
                        "WHERE stockage=:storage AND article=:article)"
                    ),
                    {
                        "source": stock_by_article[article_ids[0]],
                        "storage": storage_id,
                        "article": article_ids[0],
                    },
                )
            ).one()
        assert float(source_quantity) == 7
        assert float(destination_quantity) == 3

        conversion = await client.post(
            "/master/stockage/convert",
            headers=headers,
            json={
                "fromId": stock_by_article[article_ids[0]],
                "toId": stock_by_article[article_ids[1]],
                "quantity": 2,
                "system_id": f"c7-{suffix}",
            },
        )
        assert conversion.status_code == 200, conversion.text
        conversion_id = conversion.json()["id"]
        retry = await client.post(
            "/master/stockage/convert",
            headers=headers,
            json={
                "fromId": stock_by_article[article_ids[0]],
                "toId": stock_by_article[article_ids[1]],
                "quantity": 2,
                "system_id": f"c7-{suffix}",
            },
        )
        assert retry.status_code == 200 and retry.json()["id"] == conversion_id
        async with engine.connect() as connection:
            quantities = (
                await connection.execute(
                    text(
                        'SELECT id, quantity FROM "buma_article_stock" '
                        "WHERE id = ANY(:ids) ORDER BY id"
                    ),
                    {"ids": [stock_by_article[article_ids[0]], stock_by_article[article_ids[1]]]},
                )
            ).all()
        assert sorted(float(row[1]) for row in quantities) == [2, 5]

        cancelled_conversion = await client.post(
            "/master/stockage/cancel_conversion",
            headers=headers,
            json={"conversionId": conversion_id},
        )
        assert cancelled_conversion.status_code == 200, cancelled_conversion.text

        cancelled_transfer = await client.post(
            "/master/stockage/cancel_transfer",
            headers=headers,
            json={"id": transfer_id},
        )
        assert cancelled_transfer.status_code == 200, cancelled_transfer.text

        async with engine.connect() as connection:
            source_quantity, destination_quantity = (
                await connection.execute(
                    text(
                        "SELECT "
                        '(SELECT quantity FROM "buma_article_stock" WHERE id=:source), '
                        '(SELECT quantity FROM "buma_article_stock" '
                        "WHERE stockage=:storage AND article=:article)"
                    ),
                    {
                        "source": stock_by_article[article_ids[0]],
                        "storage": storage_id,
                        "article": article_ids[0],
                    },
                )
            ).one()
        assert float(source_quantity) == 10
        assert float(destination_quantity) == 0

        wallet_uv = await client.post(
            "/master/stockage/wallet_uv",
            headers=headers,
            json={
                "walletId": wallet_id,
                "amount": 2,
                "article_stock": stock_by_article[article_ids[0]],
            },
        )
        assert wallet_uv.status_code == 200, wallet_uv.text
        uv_wallet = await client.post(
            "/master/stockage/uv_wallet",
            headers=headers,
            json={
                "walletId": wallet_id,
                "amount": 1,
                "article_stock": stock_by_article[article_ids[0]],
            },
        )
        assert uv_wallet.status_code == 200, uv_wallet.text
        async with engine.connect() as connection:
            wallet_balance, stock_quantity = (
                await connection.execute(
                    text(
                        'SELECT (SELECT balance FROM "buma_wallet" WHERE id=:wallet), '
                        '(SELECT quantity FROM "buma_article_stock" WHERE id=:stock)'
                    ),
                    {"wallet": wallet_id, "stock": stock_by_article[article_ids[0]]},
                )
            ).one()
        assert float(wallet_balance) == 19
        assert float(stock_quantity) == 11

        async def concurrent_output() -> int:
            response = await client.post(
                "/master/stockage/movement",
                headers=headers,
                json={
                    "articleStockId": stock_by_article[article_ids[0]],
                    "quantity": 6,
                    "isEntry": False,
                },
            )
            return response.status_code

        status_codes = sorted(await asyncio.gather(concurrent_output(), concurrent_output()))
        assert status_codes == [200, 400]

        async with engine.begin() as connection:
            await connection.execute(
                text('UPDATE "roles" SET permission=CAST(:permission AS jsonb) WHERE id=:id'),
                {"permission": json.dumps({"isAdmin": False}), "id": role_id},
            )
        forbidden = await client.post(
            "/master/stockage/movement",
            headers=headers,
            json={
                "articleStockId": stock_by_article[article_ids[0]],
                "quantity": 1,
                "isEntry": True,
            },
        )
        assert forbidden.status_code == 403

    async with engine.begin() as connection:
        await connection.execute(
            text('DELETE FROM "buma_transaction" WHERE author=:author'), {"author": user_id}
        )
        await connection.execute(
            text('DELETE FROM "buma_wallet_transactions" WHERE wallet=:wallet'),
            {"wallet": wallet_id},
        )
        await connection.execute(
            text('DELETE FROM "buma_transfer_transaction" WHERE transfer=:id'), {"id": transfer_id}
        )
        await connection.execute(
            text('DELETE FROM "buma_transfer" WHERE id=:id'), {"id": transfer_id}
        )
        await connection.execute(
            text('DELETE FROM "buma_conversion" WHERE id=:id'), {"id": conversion_id}
        )
        await connection.execute(
            text('DELETE FROM "buma_article_stock" WHERE article = ANY(:ids)'), {"ids": article_ids}
        )
        await connection.execute(
            text('DELETE FROM "buma_article" WHERE id = ANY(:ids)'), {"ids": article_ids}
        )
        await connection.execute(
            text('DELETE FROM "buma_article_stockage" WHERE id=:id'), {"id": storage_id}
        )
        await connection.execute(text('DELETE FROM "buma_wallet" WHERE id=:id'), {"id": wallet_id})
        await connection.execute(text('DELETE FROM "users" WHERE id=:id'), {"id": user_id})
        await connection.execute(text('DELETE FROM "roles" WHERE id=:id'), {"id": role_id})
    await engine.dispose()
    print(
        "C7 stockage runtime: article=OK movement=OK batch_rollback=OK "
        "transfer=OK conversion_idempotency=OK cancellation=OK wallet_uv=OK "
        "permission=OK concurrency=OK cleanup=OK"
    )


if __name__ == "__main__":
    asyncio.run(main())
