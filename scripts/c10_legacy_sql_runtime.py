#!/usr/bin/env python3
"""C10 SQL proof with a view snapshot restored and a disposable collection."""

from __future__ import annotations

import asyncio
import json
import secrets
from datetime import UTC, datetime
from pathlib import Path

import httpx
from ppbase.models.collection import CollectionCreate, CollectionUpdate
from ppbase.services import collection_service
from sqlalchemy import text
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

DB = "postgresql+asyncpg://ppbase:ppbase@localhost:55434/buma_master_ppbase"


async def main() -> None:
    engine = create_async_engine(DB, connect_args={"ssl": "disable"})
    sessions = async_sessionmaker(engine, expire_on_commit=False)
    probe_name = "c10_sql_" + secrets.token_hex(4)
    snapshot = None
    try:
        async with sessions() as session:
            original = await collection_service.get_collection(session, "buma_sql_table")
            snapshot = json.loads(
                json.dumps({"schema": original.schema, "options": original.options})
            )
            original_updated = original.updated
            await collection_service.create_collection(
                session, engine, CollectionCreate(name=probe_name)
            )
        async with httpx.AsyncClient(base_url="http://127.0.0.1:8090", timeout=30) as client:

            async def sql(query, fields, status=200, **extra):
                response = await client.post(
                    "/sql",
                    json={"query": query, "fields": fields, "password": "SuperSakone", **extra},
                )
                assert response.status_code == status, response.text
                return response.json()

            assert await sql("SELECT 1 AS value", {"value": 0}) == [{"value": 1}]
            assert await sql("SELECT 1 AS value", {"other": ""}) == [{"other": ""}]
            assert await sql("SELECT 1 AS value", {"value": ""}) == [{"value": "1"}]
            assert await sql("SELECT 1 AS value", {"value": False}) == [{"value": True}]
            assert await sql("SELECT 1 AS value", {}, 400) == {
                "code": 400,
                "message": "Something went wrong while processing your request.",
                "data": {},
            }
            assert await sql("SELECT 1", {}, 403, password="bad") == {"error": "Forbidden"}
            missing = await client.post("/sql", json={"password": "SuperSakone"})
            assert missing.status_code == 400
            assert missing.json() == {
                "error": 'Le champ "query" est requis dans le corps de la requête.'
            }
            for body in ({"sql": "SELECT 2 AS value"}, {}):
                response = await client.post("/api/sql2", json=body)
                assert response.status_code == 200 and response.json() is None, response.text
            query = "SELECT 'c10row000000001' AS id, 7 AS value"
            response = await client.post("/master/article/executeSql", json={"query": query})
            assert response.status_code == 200, response.text
            result = response.json()
            assert result["name"] == "buma_sql_table" and result["type"] == "view"
            assert result["options"]["query"] == query
            assert "fields" not in result and [f["name"] for f in result["schema"]] == ["value"]
            async with engine.connect() as connection:
                row = (
                    await connection.execute(text("SELECT id,value FROM buma_sql_table"))
                ).first()
                assert tuple(row) == ("c10row000000001", 7)
            invalid = await client.post("/master/article/executeSql", json={"query": "SELECT 1"})
            assert invalid.status_code == 400
            async with engine.connect() as connection:
                assert (
                    await connection.execute(text("SELECT value FROM buma_sql_table"))
                ).scalar_one() == 7
            deleted = await client.post(
                "/master/article/deleteSql", json={"collection": probe_name}
            )
            assert deleted.status_code == 200 and deleted.json() == {
                "message": "Operation effectuee"
            }
            absent = await client.post("/master/article/deleteSql", json={"collection": probe_name})
            assert absent.status_code == 400 and absent.json() == {
                "message": "sql: no rows in result set"
            }
            async with engine.connect() as connection:
                assert (
                    await connection.execute(
                        text("SELECT COUNT(*) FROM _collections WHERE name=:n"), {"n": probe_name}
                    )
                ).scalar_one() == 0
                assert (
                    await connection.execute(text("SELECT to_regclass(:n)"), {"n": probe_name})
                ).scalar_one() is None
    finally:
        if snapshot is not None:
            async with sessions() as session:
                await collection_service.update_collection(
                    session,
                    engine,
                    "buma_sql_table",
                    CollectionUpdate(**snapshot),
                    auto_migrate=False,
                )
            async with engine.begin() as connection:
                await connection.execute(
                    text("UPDATE _collections SET updated=:value WHERE name='buma_sql_table'"),
                    {"value": original_updated},
                )
        async with sessions() as session:
            try:
                await collection_service.delete_collection(
                    session, engine, probe_name, auto_migrate=False
                )
            except LookupError:
                pass
        await engine.dispose()
    report = {
        "timestamp_utc": datetime.now(UTC).isoformat(),
        "status": "passed",
        "scope": ["/sql", "/api/sql2", "/master/article/executeSql", "/master/article/deleteSql"],
        "reference": "PocketBase 0.22.27; extracted frozen handlers in isolated reference",
        "cleanup": True,
        "limitations": ["General SQLite-to-PostgreSQL SQL dialect/error parity remains open"],
    }
    (Path(__file__).parents[1] / "docs/c10-sql-runtime-report.json").write_text(
        json.dumps(report, ensure_ascii=False, indent=2) + "\n"
    )
    print(
        "C10 SQL: raw_projection=OK password=OK broken_sql2_null=OK "
        "view_update=OK collection_delete=OK restore=OK"
    )


if __name__ == "__main__":
    asyncio.run(main())
