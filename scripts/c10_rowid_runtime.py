#!/usr/bin/env python3
"""Compare SQLite rowid allocation and the three real BUMA views with PPBase.

Requires the disposable reference helper /__rowid (no production database).
"""

from __future__ import annotations

import argparse
import asyncio
import json
import secrets
import sqlite3
from datetime import UTC, datetime
from pathlib import Path

from c10_caisse_reference_runtime import without_clock
from c10_remaining_runtime import Backend, normalized
from sqlalchemy import text


class RowidBackend(Backend):
    def __init__(self, url, reference, prefix, reference_data):
        super().__init__(url, reference, prefix)
        self.reference_data = reference_data

    async def fixture_api(self, **body):
        response = await self.client.post("/__rowid", json=body)
        assert response.status_code == 200, response.text
        return response.json()

    async def rowid(self, table, identifier):
        assert table in {"buma_article", "users"}
        if self.reference:
            with sqlite3.connect("file:" + str(self.reference_data) + "?mode=ro", uri=True) as db:
                return db.execute(
                    f'SELECT rowid FROM "{table}" WHERE id=?', (identifier,)
                ).fetchone()[0]
        async with self.engine.connect() as conn:
            return (
                await conn.execute(
                    text(f'SELECT rowid FROM "{table}" WHERE id=:id'), {"id": identifier}
                )
            ).scalar_one()

    async def delete(self, table, identifier):
        if self.reference:
            await self.fixture_api(action="delete", table=table, id=identifier)
        else:
            async with self.engine.begin() as conn:
                await conn.execute(text(f'DELETE FROM "{table}" WHERE id=:id'), {"id": identifier})


async def scenario(b):
    try:
        assert not await b.matching("users"), "Use an empty isolated target, never production"
        assert not await b.matching("buma_article"), "Use an empty isolated target"
        role = await b.fixture(
            "roles", {"name": b.prefix, "permission": {"isAdmin": True}, "status": True}
        )
        user = await b.fixture(
            "users",
            {
                "username": b.prefix,
                "email": b.prefix + "@example.com",
                "role": role,
                "password": "abcde123",
                "passwordConfirm": "abcde123",
            },
        )
        login = await b.client.post(
            "/api/collections/users/auth-with-password",
            json={"identity": b.prefix, "password": "abcde123"},
        )
        assert login.status_code == 200, login.text
        b.headers = {"Authorization": login.json()["token"]}

        async def article(label):
            return await b.fixture(
                "buma_article", {"designation": b.prefix + label, "status": True, "kind": "AUTRE"}
            )

        first, second = await article("a"), await article("b")
        allocation = [await b.rowid("buma_article", first), await b.rowid("buma_article", second)]
        await b.update("buma_article", second, {"designation": b.prefix + "updated"})
        allocation.append(await b.rowid("buma_article", second))
        await b.delete("buma_article", first)
        third = await article("c")
        allocation.append(await b.rowid("buma_article", third))
        await b.delete("buma_article", third)
        replacement = await article("d")
        allocation.append(await b.rowid("buma_article", replacement))
        if b.reference:
            attempted = (
                await b.fixture_api(
                    action="rollback",
                    table="buma_article",
                    values={"designation": b.prefix + "rollback", "status": True},
                )
            )["attempted"]
        else:
            async with b.engine.connect() as conn:
                tx = await conn.begin()
                attempted = (
                    await conn.execute(
                        text(
                            "INSERT INTO buma_article(id,designation,status,created,updated) "
                            "VALUES ('rowidrollback01',:name,TRUE,NOW(),NOW()) RETURNING rowid"
                        ),
                        {"name": b.prefix + "rollback"},
                    )
                ).scalar_one()
                await tx.rollback()
        assert attempted == 4
        after_rollback = await article("e")
        allocation.append(await b.rowid("buma_article", after_rollback))
        assert allocation == [1, 2, 2, 3, 3, 4], allocation
        b.trace["allocation"] = allocation
        b.trace["rolled_back_rowid"] = attempted

        storage = await b.fixture("buma_article_stockage", {"name": b.prefix, "status": True})
        transaction = await b.fixture(
            "buma_transaction",
            {"article": second, "stockage": storage, "type": "SALE", "quantity": 2, "status": True},
        )
        for label, view, identifier, field, expected in (
            ("article_view", "buma_article_with_no_stock_list_view", second, "rowid", 2),
            ("user_view", "buma_stockage_no_manager", user, "rowid", 1),
            ("transaction_view", "buma_stock_transaction_list_view", transaction, "article_id", 2),
        ):
            row = await b.call(label, f"/api/collections/{view}/records/{identifier}", method="GET")
            assert type(row[field]) is int and row[field] == expected, row
            # Native filter/fields are part of the exported JSON-field contract.
            response = await b.client.get(
                f"/api/collections/{view}/records",
                params={
                    "filter": f"id='{identifier}' && {field}={expected}",
                    "fields": f"id,{field}",
                },
                headers=b.headers,
            )
            assert response.status_code == 200, response.text
            assert response.json()["items"] == [{"id": identifier, field: expected}], response.text
            b.trace[label + "_filter"] = normalized(response.json())
        base = await b.call(
            "base_record", f"/api/collections/buma_article/records/{second}", method="GET"
        )
        assert "rowid" not in base
        created = await b.call(
            "native_create_ignores_rowid",
            "/api/collections/buma_article/records",
            {"designation": b.prefix + "native", "kind": "AUTRE", "status": True, "rowid": 900},
        )
        b.made.append(("buma_article", created["id"]))
        assert "rowid" not in created
        assert await b.rowid("buma_article", created["id"]) == 5
        b.trace["user_rowid"] = await b.rowid("users", user)
        # SQLite's documented fallback after the largest signed 64-bit rowid.
        largest = 9223372036854775807
        if b.reference:
            await b.fixture_api(
                action="assign_rowid", table="buma_article", id=created["id"], value=str(largest)
            )
        else:
            async with b.engine.begin() as conn:
                await conn.execute(
                    text("UPDATE buma_article SET rowid=:n WHERE id=:id"),
                    {"n": largest, "id": created["id"]},
                )
        assert await b.rowid("buma_article", created["id"]) == largest
        random_id = await article("overflow")
        candidate = await b.rowid("buma_article", random_id)
        assert 0 < candidate < largest and candidate not in {2, 3, 4}
        b.trace["overflow_generated_positive_unused"] = True
    finally:
        await b.close()
    return without_clock(b.trace)


async def concurrency():
    b = RowidBackend("http://127.0.0.1:8090", False, "C10ROWCONC", None)
    try:
        assert not await b.matching("buma_article")

        async def insert(index):
            identifier = await b.fixture(
                "buma_article", {"designation": f"C10ROWCONC{index}", "status": True}
            )
            return await b.rowid("buma_article", identifier)

        values = await asyncio.gather(*(insert(i) for i in range(20)))
        assert sorted(values) == list(range(1, 21)), values
        return {"concurrent_inserts": 20, "unique_rowids": 20, "contiguous_after_commit": True}
    finally:
        await b.close()


async def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--reference-data", type=Path, required=True)
    args = parser.parse_args()
    prefix = "C10ROW" + secrets.token_hex(3)
    reference = await scenario(
        RowidBackend("http://127.0.0.1:8099", True, prefix, args.reference_data)
    )
    target = await scenario(RowidBackend("http://127.0.0.1:8090", False, prefix, None))
    differences = {
        key: {"pocketbase": value, "ppbase": target.get(key)}
        for key, value in reference.items()
        if value != target.get(key)
    }
    concurrent = await concurrency()
    report = {
        "timestamp_utc": datetime.now(UTC).isoformat(),
        "source_commit": "530018cd20c2acbe88f38b3639e500b5271e2eb2",
        "pocketbase_version": "0.22.27",
        "sqlite_version": "3.46.0",
        "status": "failed" if differences else "passed",
        "comparisons": len(reference),
        "reference": reference,
        "differences": differences,
        "concurrency": concurrent,
        "cleanup": True,
    }
    (Path(__file__).parents[1] / "docs/c10-rowid-runtime-report.json").write_text(
        json.dumps(report, ensure_ascii=False, indent=2) + "\n"
    )
    assert not differences, json.dumps(differences, ensure_ascii=False, indent=2)
    print(
        "C10 rowid: three native views, filters, allocation/update/delete/rollback, "
        "20 concurrent inserts, cleanup OK"
    )


if __name__ == "__main__":
    asyncio.run(main())
