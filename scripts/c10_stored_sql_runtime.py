#!/usr/bin/env python3
"""Replay SQL copied verbatim from BUMA's provided export and source migration.

This proves stored server queries, not frontend call sites or production use.
All rows are synthetic and both view definitions are restored in finally.
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import os
import re
from datetime import UTC, datetime
from pathlib import Path

from c10_remaining_runtime import Backend
from ppbase.models.collection import CollectionUpdate
from ppbase.services import collection_service
from sqlalchemy import text
from sqlalchemy.ext.asyncio import async_sessionmaker

ROOT = Path(__file__).resolve().parents[1]
EXPORT = Path(
    os.environ.get(
        "BUMA_SCHEMA_EXPORT", "/Users/omarsisss/Downloads/buma-master-pb_schema-corrected.json"
    )
)
MIGRATION = ROOT.parent / "buma_master_hooks/migrationsDir/1736833029_created_buma_sql_table.js"


def queries():
    definition = next(c for c in json.loads(EXPORT.read_text()) if c["name"] == "buma_sql_table")
    match = re.search(r'"query"\s*:\s*("(?:[^"\\]|\\.)*")', MIGRATION.read_text())
    return [
        {
            "name": "configured_caisse_summary",
            "query": definition["options"]["query"],
            "origin": str(EXPORT),
            "fields": {"id": "", "total_amount": 0},
        },
        {
            "name": "initial_view_query",
            "query": json.loads(match[1]),
            "origin": str(MIGRATION),
            "fields": {"id": ""},
        },
    ]


def response_value(response):
    try:
        body = response.json()
    except ValueError:
        body = response.text
    return {
        "status": response.status_code,
        "content_type": response.headers.get("content-type", "").split(";")[0],
        "body": body,
    }


async def replay(reference):
    b = Backend(
        "http://127.0.0.1:8098" if reference else "http://127.0.0.1:8090", reference, "C10SOURCE"
    )
    snapshot = None
    session_id = "bra4vsynv2vrpih"
    output = {}
    try:
        if reference:
            snapshot = await b.fixture_api(action="collection", table="buma_sql_table")
        else:
            async with async_sessionmaker(b.engine, expire_on_commit=False)() as session:
                view = await collection_service.get_collection(session, "buma_sql_table")
                snapshot = {"schema": view.schema, "options": view.options, "updated": view.updated}
        assert not await b.matching("buma_caisse_transactions", session=session_id), (
            "Fixture session already has data; refusing to mix records"
        )
        for kind, amount, active in [
            ("DEPOSIT", 10, True),
            ("DEPOSIT", 5, True),
            ("EXPENSE", 3, True),
            ("EXPENSE", 900, False),
        ]:
            await b.fixture(
                "buma_caisse_transactions",
                {
                    "session": session_id,
                    "type": kind,
                    "amount": amount,
                    "status": active,
                    "note": "C10 source query proof",
                },
            )
        for item in queries():
            entry = {}
            raw = await b.client.post(
                "/sql",
                json={"query": item["query"], "fields": item["fields"], "password": "SuperSakone"},
            )
            entry["sql"] = response_value(raw)
            if raw.status_code == 200:
                expected = (
                    [{"id": "DEPOSIT", "total_amount": 15}, {"id": "EXPENSE", "total_amount": 3}]
                    if item["name"] == "configured_caisse_summary"
                    else [{"id": "1"}]
                )
                assert sorted(raw.json(), key=lambda r: r["id"]) == expected, raw.text
            changed = await b.client.post(
                "/master/article/executeSql", json={"query": item["query"]}
            )
            entry["executeSql"] = response_value(changed)
            if changed.status_code == 200:
                records = await b.client.get(
                    "/api/collections/buma_sql_table/records", params={"sort": "id", "perPage": 20}
                )
                entry["native_list"] = response_value(records)
                row_id = "DEPOSIT" if item["name"] == "configured_caisse_summary" else "1"
                one = await b.client.get("/api/collections/buma_sql_table/records/" + row_id)
                entry["native_one"] = response_value(one)
            output[item["name"]] = entry
    finally:
        try:
            if snapshot is not None:
                if reference:
                    await b.fixture_api(
                        action="restore_collection", table="buma_sql_table", snapshot=snapshot
                    )
                    restored = await b.fixture_api(action="collection", table="buma_sql_table")
                    assert restored["options"] == snapshot["options"]
                    assert restored["schema"] == snapshot["schema"]
                else:
                    async with async_sessionmaker(b.engine, expire_on_commit=False)() as session:
                        await collection_service.update_collection(
                            session,
                            b.engine,
                            "buma_sql_table",
                            CollectionUpdate(
                                schema=snapshot["schema"], options=snapshot["options"]
                            ),
                            auto_migrate=False,
                        )
                    async with b.engine.begin() as connection:
                        await connection.execute(
                            text("UPDATE _collections SET updated=:u WHERE name='buma_sql_table'"),
                            {"u": snapshot["updated"]},
                        )
                    async with async_sessionmaker(b.engine, expire_on_commit=False)() as session:
                        restored = await collection_service.get_collection(
                            session, "buma_sql_table"
                        )
                        assert (
                            restored.options == snapshot["options"]
                            and restored.schema == snapshot["schema"]
                        )
        finally:
            await b.close()
    return output


def comparable(value, *, metadata=False):
    if isinstance(value, dict):
        return {
            k: ("<clock>" if k in {"created", "updated"} and v else comparable(v))
            for k, v in value.items()
        }
    if isinstance(value, list):
        return [comparable(v) for v in value]
    return value


async def main():
    pb = await replay(True)
    pp = await replay(False)
    differences = {}
    for item in queries():
        name = item["name"]
        for operation in set(pb[name]) | set(pp[name]):
            left, right = pb[name].get(operation), pp[name].get(operation)
            if comparable(left) != comparable(right):
                differences[f"{name}:{operation}"] = {"pocketbase": left, "ppbase": right}
    report = {
        "timestamp_utc": datetime.now(UTC).isoformat(),
        "scope": "Actual stored SQL, synthetic rows and fields probe; no frontend caller verified",
        "query_sources": [
            {**q, "query_sha256": hashlib.sha256(q["query"].encode()).hexdigest()}
            for q in queries()
        ],
        "pocketbase": pb,
        "ppbase": pp,
        "differences": differences,
        "cleanup": True,
        "status": "differences_found" if differences else "matched",
    }
    (ROOT / "docs/c10-stored-sql-runtime-report.json").write_text(
        json.dumps(report, ensure_ascii=False, indent=2) + "\n"
    )
    print(
        json.dumps({"status": report["status"], "differences": list(differences), "cleanup": True})
    )


if __name__ == "__main__":
    asyncio.run(main())
