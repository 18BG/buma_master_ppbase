#!/usr/bin/env python3
"""Runtime C4 proof for idempotency, rollback, and concurrent uniqueness."""

from __future__ import annotations

import asyncio
import secrets
from datetime import UTC, datetime

from ppbase.services.record_storage_coordinator import run_record_storage_transaction
from sqlalchemy import text
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import create_async_engine

from app.transactions.idempotency import (
    IdempotencyLookup,
    acquire_idempotency_lock,
    build_idempotency_key,
    find_idempotent_transaction,
)

DATABASE_URL = "postgresql+asyncpg://ppbase:ppbase@localhost:55434/buma_master_ppbase"
AUTHOR = "c4-author"
TYPE = "C4_TEST"


def _row_values(system_id: str, mobile_message_id: str, record_id: str) -> dict:
    now = datetime.now(UTC)
    return {
        "id": record_id,
        "created": now,
        "updated": now,
        "price": 1,
        "quantity": 1,
        "sale": "",
        "article": "",
        "stockage": "",
        "author": AUTHOR,
        "article_stock": "",
        "buy": "",
        "type": TYPE,
        "status": True,
        "transfer": "",
        "conversion": "",
        "sim": "",
        "notes": "",
        "return": "",
        "mobile_transaction_date": None,
        "mobile_message_id": mobile_message_id,
        "supplement": 0,
        "transaction_message": "",
        "old_quantity": 0,
        "article_stock_session": "",
        "system_id": system_id,
        "manual": False,
        "stock_debit_amount": 0,
        "credited_amount": 0,
        "operator_bonus_amount": 0,
        "operator_bonus_rate": 0,
    }


INSERT_SQL = text(
    'INSERT INTO "buma_transaction" '
    "(id,created,updated,price,quantity,sale,article,stockage,author,"
    "article_stock,buy,type,status,transfer,conversion,sim,notes,return,"
    "mobile_transaction_date,mobile_message_id,supplement,transaction_message,"
    "old_quantity,article_stock_session,system_id,manual,stock_debit_amount,"
    "credited_amount,operator_bonus_amount,operator_bonus_rate) "
    "VALUES (:id,:created,:updated,:price,:quantity,:sale,:article,:stockage,:author,"
    ":article_stock,:buy,:type,:status,:transfer,:conversion,:sim,:notes,:return,"
    ":mobile_transaction_date,:mobile_message_id,:supplement,:transaction_message,"
    ":old_quantity,:article_stock_session,:system_id,:manual,:stock_debit_amount,"
    ":credited_amount,:operator_bonus_amount,:operator_bonus_rate)"
)


async def main() -> None:
    engine = create_async_engine(DATABASE_URL, connect_args={"ssl": "disable"})

    async def rollback_operation(active_engine) -> None:
        key = build_idempotency_key("recharge", f"{AUTHOR}:same-key")
        async with active_engine.connect() as connection:
            await connection.execute(INSERT_SQL, _row_values(key, "", "c4rollback"))
        raise RuntimeError("forced C4 rollback")

    try:
        await run_record_storage_transaction(engine, rollback_operation)
    except RuntimeError as exc:
        assert str(exc) == "forced C4 rollback"

    async with engine.connect() as connection:
        rollback_count = (
            await connection.execute(
                text('SELECT COUNT(*) FROM "buma_transaction" WHERE "id" = :id'),
                {"id": "c4rollback"},
            )
        ).scalar_one()
        assert rollback_count == 0

    concurrent_key = build_idempotency_key("recharge", f"{AUTHOR}:concurrent")

    async def attempt(index: int) -> str:
        record_id = f"c4{index}{secrets.token_hex(4)}"

        async def operation(active_engine) -> str:
            async with active_engine.connect() as connection:
                lookup = IdempotencyLookup(
                    type=TYPE,
                    author=AUTHOR,
                    system_id=concurrent_key,
                )
                await acquire_idempotency_lock(connection, lookup)
                if await find_idempotent_transaction(connection, lookup):
                    return "duplicate"
                await connection.execute(
                    INSERT_SQL,
                    _row_values(concurrent_key, f"mobile-{index}", record_id),
                )
                return "inserted"

        try:
            return await run_record_storage_transaction(engine, operation)
        except IntegrityError:
            return "duplicate"

    results = await asyncio.gather(attempt(1), attempt(2))
    assert sorted(results) == ["duplicate", "inserted"]
    existing = await find_idempotent_transaction(
        engine,
        IdempotencyLookup(type=TYPE, author=AUTHOR, system_id=concurrent_key),
    )
    assert existing is not None
    assert existing["system_id"] == concurrent_key

    async with engine.begin() as connection:
        await connection.execute(
            INSERT_SQL,
            _row_values("legacy-key", "legacy-mobile", "c4legacy"),
        )
    by_legacy = await find_idempotent_transaction(
        engine,
        IdempotencyLookup(
            type=TYPE,
            author=AUTHOR,
            system_id="missing-current-key",
            legacy_system_id="legacy-key",
            mobile_message_id="legacy-mobile",
        ),
    )
    assert by_legacy is not None and by_legacy["id"] == "c4legacy"
    by_mobile = await find_idempotent_transaction(
        engine,
        IdempotencyLookup(
            type=TYPE,
            author=AUTHOR,
            mobile_message_id="legacy-mobile",
        ),
    )
    assert by_mobile is not None and by_mobile["id"] == "c4legacy"

    async with engine.begin() as connection:
        await connection.execute(
            text('DELETE FROM "buma_transaction" WHERE "author" = :author'),
            {"author": AUTHOR},
        )
    await engine.dispose()
    print("C4 idempotency runtime: rollback=OK concurrency=OK lookup=OK cleanup=OK")


if __name__ == "__main__":
    asyncio.run(main())
