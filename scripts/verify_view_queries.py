#!/usr/bin/env python3
"""Validate translated PocketBase view queries on PostgreSQL.

Views are checked in dependency order inside one disposable transaction. A
successful temporary view remains available to later dependent checks, while a
failed query is rolled back to its savepoint and reported without hiding the
remaining failures.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import os
from pathlib import Path
from typing import Any

from sqlalchemy import text
from sqlalchemy.ext.asyncio import create_async_engine

try:
    from scripts.generate_native_baseline import load_schema, view_dependencies
except ImportError:  # pragma: no cover - direct script execution
    from generate_native_baseline import load_schema, view_dependencies

DEFAULT_SCHEMA = Path("/Users/omarsisss/Downloads/buma-master-pb_schema-corrected.json")
DEFAULT_DB = os.getenv(
    "PPBASE_DATABASE_URL",
    "postgresql+asyncpg://ppbase:ppbase@localhost:55434/buma_master_ppbase",
)


def dependency_order(definitions: list[dict[str, Any]]) -> list[dict[str, Any]]:
    views = {str(item["name"]): item for item in definitions if item.get("type") == "view"}
    dependencies = view_dependencies(definitions)
    ordered: list[dict[str, Any]] = []
    remaining = set(views)
    while remaining:
        ready = sorted(
            filter(
                lambda name: not (dependencies.get(name, set()) & remaining),
                remaining,
            )
        )
        if not ready:
            raise ValueError(f"Cyclic view dependency: {sorted(remaining)}")
        ordered.extend(views[name] for name in ready)
        remaining.difference_update(ready)
    return ordered


async def verify(database_url: str, definitions: list[dict[str, Any]]) -> list[dict[str, str]]:
    engine = create_async_engine(database_url)
    results: list[dict[str, str]] = []
    try:
        async with engine.begin() as connection:
            for definition in dependency_order(definitions):
                name = str(definition["name"])
                query = str((definition.get("options") or {}).get("query") or "")
                savepoint = await connection.begin_nested()
                try:
                    await connection.execute(text(f'CREATE TEMP VIEW "{name}" AS {query}'))
                except Exception as exc:
                    await savepoint.rollback()
                    results.append({"name": name, "status": "FAIL", "error": str(exc)})
                else:
                    await savepoint.commit()
                    results.append({"name": name, "status": "OK", "error": ""})
    finally:
        await engine.dispose()
    return results


async def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--db", default=DEFAULT_DB)
    parser.add_argument("--schema", type=Path, default=DEFAULT_SCHEMA)
    args = parser.parse_args()

    definitions, digest, _ = load_schema(args.schema, include_views=True)
    results = await verify(args.db, definitions)
    report = {
        "schema_sha256": digest,
        "total": len(results),
        "ok": sum(item["status"] == "OK" for item in results),
        "fail": sum(item["status"] == "FAIL" for item in results),
        "results": results,
    }
    print(json.dumps(report, indent=2, ensure_ascii=False))
    return 0 if report["fail"] == 0 else 1


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
