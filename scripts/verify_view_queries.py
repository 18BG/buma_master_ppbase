#!/usr/bin/env python3
"""Validate translated PocketBase view queries on PostgreSQL.

Views are checked in dependency order inside one disposable transaction. A
successful temporary view remains available to later dependent checks, while a
failed query is rolled back to its savepoint and reported without hiding the
remaining failures.

The ten views recreated by migration 1789236851 are checked with the query
that migration stores (``migrated_view_definitions``): it calls the functions
the migration creates, so the database must be migrated through 1789236851.
"""

from __future__ import annotations

import argparse
import asyncio
import importlib.util
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

DEFAULT_SCHEMA = Path(
    os.environ.get(
        "BUMA_SCHEMA_EXPORT", "/Users/omarsisss/Downloads/buma-master-pb_schema-corrected.json"
    )
)
DEFAULT_DB = os.getenv(
    "PPBASE_DATABASE_URL",
    "postgresql+asyncpg://ppbase:ppbase@localhost:55434/buma_master_ppbase",
)
VIEW_SEMANTICS_MIGRATION = (
    Path(__file__).resolve().parents[1] / "pb_migrations/1789236851_c11_view_sqlite_semantics.py"
)


def _view_semantics_module():
    spec = importlib.util.spec_from_file_location(
        "buma_1789236851_view_semantics", VIEW_SEMANTICS_MIGRATION
    )
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


VIEW_SEMANTICS = _view_semantics_module()


def migrated_view_definitions(definitions: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """The definitions with the view queries stored by migration 1789236851.

    The translation of the corrected export is the query 1789236851 expects
    (its SHA-256 guard refuses any other text); the rewritten query replaces it
    for the views that migration recreates. Other definitions are unchanged.
    """
    migrated = []
    for item in definitions:
        name = str(item.get("name"))
        if item.get("type") == "view" and name in VIEW_SEMANTICS.CHANGES:
            options = dict(item.get("options") or {})
            options["query"] = VIEW_SEMANTICS.rewrite(name, str(options.get("query") or ""), True)
            item = {**item, "options": options}
        migrated.append(item)
    return migrated


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
            for definition in dependency_order(migrated_view_definitions(definitions)):
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
