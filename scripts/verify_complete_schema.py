#!/usr/bin/env python3
"""Verify the complete corrected PocketBase schema on PostgreSQL.

This check covers both ordinary/auth collections and translated views. The
``users.username`` and ``agent_users.username`` compatibility fields are
intentionally required because the corrected PocketBase export exposes them as
auth system fields while PPBase 0.3.x does not create the physical columns
itself.
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
    from scripts.generate_native_baseline import load_schema
    from scripts.verify_schema_baseline import (
        ALLOWED_EXTRAS,
        _compare_collection,
    )
except ImportError:  # pragma: no cover - direct script execution
    from generate_native_baseline import load_schema
    from verify_schema_baseline import ALLOWED_EXTRAS, _compare_collection

DEFAULT_SCHEMA = Path("/Users/omarsisss/Downloads/buma-master-pb_schema-corrected.json")
DEFAULT_DB = os.getenv(
    "PPBASE_DATABASE_URL",
    "postgresql+asyncpg://ppbase:ppbase@localhost:55434/buma_master_ppbase",
)
REQUIRED_COMPAT_FIELDS = {"users": {"username"}, "agent_users": {"username"}}


async def read_collections(database_url: str) -> list[dict[str, Any]]:
    engine = create_async_engine(database_url)
    try:
        async with engine.connect() as connection:
            result = await connection.execute(
                text(
                    """
                    SELECT id, name, type, system, schema, indexes,
                           list_rule, view_rule, create_rule, update_rule,
                           delete_rule, options
                    FROM _collections
                    """
                )
            )
            definitions = [dict(row) for row in result.mappings().all()]
            for definition in definitions:
                if definition["type"] != "view":
                    continue
                columns = await connection.execute(
                    text(
                        "SELECT column_name FROM information_schema.columns "
                        "WHERE table_schema='public' AND table_name=:name"
                    ),
                    {"name": definition["name"]},
                )
                definition["physical_columns"] = list(columns.scalars())
            return definitions
    finally:
        await engine.dispose()


def _without_compat_fields(name: str, fields: list[dict[str, Any]]) -> list[dict[str, Any]]:
    allowed = REQUIRED_COMPAT_FIELDS.get(name, set())
    return [field for field in fields if field.get("name") not in allowed]


def verify(
    expected_definitions: list[dict[str, Any]],
    actual_rows: list[dict[str, Any]],
) -> dict[str, Any]:
    expected = {str(item["name"]): item for item in expected_definitions}
    actual = {str(item["name"]): item for item in actual_rows if not item["system"]}
    actual_all = {str(item["name"]): item for item in actual_rows}

    missing = sorted(set(expected) - set(actual))
    unexpected = sorted((set(actual) - set(expected)) - ALLOWED_EXTRAS)
    allowed = sorted((set(actual) - set(expected)) & ALLOWED_EXTRAS)
    mismatches: list[str] = []
    for name in sorted(set(expected) & set(actual)):
        expected_item = expected[name]
        actual_item = dict(actual[name])
        actual_item["schema"] = _without_compat_fields(name, actual_item.get("schema") or [])
        mismatches.extend(_compare_collection(name, expected_item, actual_item))

    missing_compat_fields: list[str] = []
    for name, fields in REQUIRED_COMPAT_FIELDS.items():
        row = actual.get(name)
        actual_names = {str(field.get("name")) for field in (row or {}).get("schema") or []}
        for field in sorted(fields - actual_names):
            missing_compat_fields.append(f"{name}.{field}")

    projection_errors = []
    for name, item in actual.items():
        if item.get("type") != "view" or "physical_columns" not in item:
            continue
        for field in item.get("schema") or []:
            if field["name"] in item["physical_columns"]:
                continue
            path = name + "." + field["name"]
            projection_errors.append(path)

    known_ids = {str(item["id"]) for item in actual_all.values()}
    relation_errors: list[str] = []
    for name, definition in expected.items():
        for field in definition.get("schema") or []:
            if field.get("type") != "relation":
                continue
            target = (field.get("options") or {}).get("collectionId")
            if target and str(target) not in known_ids:
                relation_errors.append(f"{name}.{field.get('name')}: {target}")

    return {
        "expected": len(expected),
        "expected_base_auth": sum(item.get("type") != "view" for item in expected.values()),
        "expected_views": sum(item.get("type") == "view" for item in expected.values()),
        "actual_non_system": len(actual),
        "missing": missing,
        "unexpected": unexpected,
        "allowed_extras": allowed,
        "required_compat_fields": sorted(
            f"{name}.{field}" for name, fields in REQUIRED_COMPAT_FIELDS.items() for field in fields
        ),
        "missing_compat_fields": missing_compat_fields,
        "metadata_mismatches": mismatches,
        "relation_errors": relation_errors,
        "view_projection_errors": projection_errors,
        "ok": not (
            missing
            or unexpected
            or mismatches
            or missing_compat_fields
            or relation_errors
            or projection_errors
        ),
    }


async def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--db", default=DEFAULT_DB)
    parser.add_argument("--schema", type=Path, default=DEFAULT_SCHEMA)
    args = parser.parse_args()

    definitions, digest, _ = load_schema(args.schema, include_views=True)
    actual_rows = await read_collections(args.db)
    report = verify(definitions, actual_rows)
    report["schema_sha256"] = digest
    print(json.dumps(report, indent=2, ensure_ascii=False))
    return 0 if report["ok"] else 1


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
