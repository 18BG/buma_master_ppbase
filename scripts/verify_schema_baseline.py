#!/usr/bin/env python3
"""Verify an applied PPBase collection baseline against the final export.

The PPBase runtime adds generated authentication defaults and normalizes field
metadata. This verifier compares the stable contract only: collection identity,
type, fields, relations, indexes, rules, and source-declared auth options.
"""

from __future__ import annotations

import argparse
import asyncio
import copy
import importlib.util
import json
import os
from pathlib import Path
from typing import Any

from sqlalchemy import text
from sqlalchemy.ext.asyncio import create_async_engine

try:
    from scripts.generate_native_baseline import load_schema
except ImportError:  # pragma: no cover - direct script execution
    from generate_native_baseline import load_schema

DEFAULT_SCHEMA = Path(
    os.environ.get(
        "BUMA_SCHEMA_EXPORT", "/Users/omarsisss/Downloads/buma-master-pb_schema-corrected.json"
    )
)
DEFAULT_DB = os.getenv(
    "PPBASE_DATABASE_URL",
    "postgresql+asyncpg://ppbase:ppbase@localhost:55434/buma_master_ppbase",
)
ALLOWED_EXTRAS: set[str] = set()
IGNORED_NON_SYSTEM_TYPES = {"view"}
COMPATIBILITY_FIELDS = {"users": {"username"}, "agent_users": {"username"}}
AUTH_RUNTIME_FIELDS = {"password"}
D0022_MIGRATION = (
    Path(__file__).resolve().parents[1]
    / "pb_migrations/1789236849_c11_source_transaction_idempotency.py"
)


def _source_index_deltas() -> dict[str, list[str]]:
    """Index of the only frozen source migration production never applied.

    The corrected export mirrors production, whose _migrations table lacks only
    1785599959_updated_buma_transaction.js. Migration 1789236849 applies its
    index, so the expected metadata is the export plus that statement (D-0022).
    """
    spec = importlib.util.spec_from_file_location("d0022_source_index", D0022_MIGRATION)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return {module.COLLECTION: [module.SOURCE_INDEX]}


SOURCE_INDEX_DELTAS = _source_index_deltas()


def _expected_projection(collection: dict[str, Any]) -> dict[str, Any]:
    return {
        "id": collection["id"],
        "name": collection["name"],
        "type": collection["type"],
        "system": collection["system"],
        "schema": collection.get("schema") or [],
        "indexes": [
            *(collection.get("indexes") or []),
            *SOURCE_INDEX_DELTAS.get(collection["name"], []),
        ],
        "list_rule": collection.get("listRule"),
        "view_rule": collection.get("viewRule"),
        "create_rule": collection.get("createRule"),
        "update_rule": collection.get("updateRule"),
        "delete_rule": collection.get("deleteRule"),
        "options": collection.get("options") or {},
    }


def _actual_projection(row: dict[str, Any]) -> dict[str, Any]:
    return {
        "id": row["id"],
        "name": row["name"],
        "type": row["type"],
        "system": row["system"],
        "schema": row["schema"] or [],
        "indexes": row["indexes"] or [],
        "list_rule": row["list_rule"],
        "view_rule": row["view_rule"],
        "create_rule": row["create_rule"],
        "update_rule": row["update_rule"],
        "delete_rule": row["delete_rule"],
        "options": row["options"] or {},
    }


def _stable_options(
    collection_type: str,
    expected_options: dict[str, Any],
    actual_options: dict[str, Any],
) -> dict[str, Any]:
    """Ignore PPBase-generated auth secrets/defaults during comparison."""
    if collection_type == "auth":
        return {key: actual_options.get(key) for key in expected_options}
    return actual_options


def _auth_password_minimum(schema: list[dict[str, Any]]) -> int | None:
    for field in schema:
        if field.get("name") != "password" or field.get("type") != "password":
            continue
        options = field.get("options") if isinstance(field.get("options"), dict) else {}
        value = options.get("min")
        try:
            return int(value)
        except (TypeError, ValueError):
            return None
    return None


def _compare_collection(name: str, expected: dict[str, Any], actual: dict[str, Any]) -> list[str]:
    expected_projection = _expected_projection(expected)
    actual_projection = _actual_projection(actual)
    if expected_projection["type"] == "auth":
        actual_projection["schema"] = [
            field
            for field in actual_projection["schema"]
            if field.get("name") not in AUTH_RUNTIME_FIELDS
        ]
        expected_min = expected_projection["options"].get("minPasswordLength")
        if expected_min is not None and "minPasswordLength" not in actual_projection["options"]:
            actual_min = _auth_password_minimum(actual.get("schema") or [])
            if actual_min is not None:
                actual_projection["options"] = dict(actual_projection["options"])
                actual_projection["options"]["minPasswordLength"] = actual_min
    expected_stable = dict(expected_projection)
    actual_stable = dict(actual_projection)
    actual_stable["options"] = _stable_options(
        expected_projection["type"],
        expected_projection["options"],
        actual_projection["options"],
    )

    mismatches: list[str] = []
    for key in (
        "id",
        "name",
        "type",
        "system",
        "schema",
        "indexes",
        "list_rule",
        "view_rule",
        "create_rule",
        "update_rule",
        "delete_rule",
        "options",
    ):
        if expected_stable[key] != actual_stable[key]:
            mismatches.append(f"{name}: {key}")
    return mismatches


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
            return [dict(row) for row in result.mappings().all()]
    finally:
        await engine.dispose()


def verify(
    expected_definitions: list[dict[str, Any]],
    actual_rows: list[dict[str, Any]],
    allowed_extras: set[str] = ALLOWED_EXTRAS,
) -> dict[str, Any]:
    expected = {str(item["name"]): item for item in expected_definitions}
    # This verifier is intentionally scoped to the base/auth baseline. Views
    # are checked by verify_view_queries.py and verify_complete_schema.py, so
    # their presence must not make this targeted check fail on a full install.
    actual = {
        str(item["name"]): item
        for item in actual_rows
        if not item["system"] and item.get("type") not in IGNORED_NON_SYSTEM_TYPES
    }
    actual_all = {str(item["name"]): item for item in actual_rows}

    missing = sorted(set(expected) - set(actual))
    unexpected = sorted((set(actual) - set(expected)) - allowed_extras)
    allowed = sorted((set(actual) - set(expected)) & allowed_extras)
    mismatches: list[str] = []
    for name in sorted(set(expected) & set(actual)):
        actual_item = copy.deepcopy(actual[name])
        ignored_fields = COMPATIBILITY_FIELDS.get(name, set())
        if ignored_fields:
            actual_item["schema"] = [
                field
                for field in (actual_item.get("schema") or [])
                if field.get("name") not in ignored_fields
            ]
        mismatches.extend(_compare_collection(name, expected[name], actual_item))

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
        "actual_non_system": len(actual),
        "missing": missing,
        "unexpected": unexpected,
        "allowed_extras": allowed,
        "metadata_mismatches": mismatches,
        "relation_errors": relation_errors,
        "ok": not (missing or unexpected or mismatches or relation_errors),
    }


async def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--db", default=DEFAULT_DB)
    parser.add_argument("--schema", type=Path, default=DEFAULT_SCHEMA)
    args = parser.parse_args()

    definitions, digest, _ = load_schema(args.schema, include_views=False)
    actual_rows = await read_collections(args.db)
    report = verify(definitions, actual_rows)
    report["schema_sha256"] = digest
    print(json.dumps(report, indent=2, ensure_ascii=False))
    return 0 if report["ok"] else 1


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
