import copy
import os
from pathlib import Path

from scripts.generate_native_baseline import load_schema
from scripts.verify_complete_schema import verify as verify_complete
from scripts.verify_schema_baseline import SOURCE_INDEX_DELTAS
from scripts.verify_view_queries import (
    VIEW_SEMANTICS,
    dependency_order,
    migrated_view_definitions,
)

SCHEMA = Path(
    os.environ.get(
        "BUMA_SCHEMA_EXPORT", "/Users/omarsisss/Downloads/buma-master-pb_schema-corrected.json"
    )
)


def test_view_dependency_order_contains_all_views_once() -> None:
    definitions, _, _ = load_schema(SCHEMA, include_views=True)
    ordered = dependency_order(definitions)
    names = [str(item["name"]) for item in ordered]
    assert len(names) == 42
    assert len(names) == len(set(names))
    assert "buma_transaction_list_view" in names
    assert names.index("buma_transaction_list_view") < names.index("test")


def installed_rows(definitions: list[dict]) -> list[dict]:
    """_collections rows of a database migrated through 1789236851."""
    return [
        {
            "id": item["id"],
            "name": item["name"],
            "type": item["type"],
            "system": item["system"],
            "schema": copy.deepcopy(item.get("schema") or []),
            "indexes": copy.deepcopy(item.get("indexes") or []),
            "list_rule": item.get("listRule"),
            "view_rule": item.get("viewRule"),
            "create_rule": item.get("createRule"),
            "update_rule": item.get("updateRule"),
            "delete_rule": item.get("deleteRule"),
            "options": copy.deepcopy(item.get("options") or {}),
        }
        for item in migrated_view_definitions(definitions)
    ]


def test_complete_schema_verifier_requires_all_definitions_and_username_compat() -> None:
    definitions, _, _ = load_schema(SCHEMA, include_views=True)
    rows = installed_rows(definitions)
    rows[rows.index(next(row for row in rows if row["name"] == "users"))]["schema"].append(
        {
            "id": "users_username_compat",
            "name": "username",
            "type": "text",
            "required": False,
            "presentable": False,
            "options": {"unique": False, "pattern": ""},
        }
    )
    rows[rows.index(next(row for row in rows if row["name"] == "agent_users"))]["schema"].append(
        {
            "id": "agent_users_username_compat",
            "name": "username",
            "type": "text",
            "required": False,
            "presentable": False,
            "options": {"unique": False, "pattern": ""},
        }
    )
    # Same metadata effect as migration 1789236849 on the installed schema.
    for name, statements in SOURCE_INDEX_DELTAS.items():
        next(row for row in rows if row["name"] == name)["indexes"].extend(statements)

    report = verify_complete(definitions, rows)
    assert report["expected"] == 114
    assert report["expected_base_auth"] == 72
    assert report["expected_views"] == 42
    assert report["allowed_extras"] == []
    assert report["required_compat_fields"] == ["agent_users.username", "users.username"]
    assert report["missing_compat_fields"] == []
    assert report["ok"] is True


def compat_rows(definitions: list[dict]) -> list[dict]:
    rows = installed_rows(definitions)
    for name, field in (
        ("users", "users_username_compat"),
        ("agent_users", "agent_users_username_compat"),
    ):
        next(row for row in rows if row["name"] == name)["schema"].append(
            {
                "id": field,
                "name": "username",
                "type": "text",
                "required": False,
                "presentable": False,
                "options": {"unique": False, "pattern": ""},
            }
        )
    for name, statements in SOURCE_INDEX_DELTAS.items():
        next(row for row in rows if row["name"] == name)["indexes"].extend(statements)
    return rows


def test_complete_schema_verifier_expects_the_queries_of_1789236851() -> None:
    definitions, _, _ = load_schema(SCHEMA, include_views=True)
    rows = compat_rows(definitions)
    assert verify_complete(definitions, rows)["ok"] is True
    # A database whose ten views kept the translated query (1789236851 not
    # applied, or reverted) is reported, view by view, and nothing else.
    translated = {i["name"]: i["options"]["query"] for i in definitions if i["type"] == "view"}
    for row in rows:
        if row["name"] in VIEW_SEMANTICS.CHANGES:
            row["options"]["query"] = translated[row["name"]]
    report = verify_complete(definitions, rows)
    assert report["ok"] is False
    assert report["metadata_mismatches"] == sorted(
        f"{name}: options" for name in VIEW_SEMANTICS.CHANGES
    )


def test_view_query_verifier_checks_the_migrated_queries() -> None:
    definitions, _, _ = load_schema(SCHEMA, include_views=True)
    migrated = {item["name"]: item for item in migrated_view_definitions(definitions)}
    original = {item["name"]: item for item in definitions}
    assert len(migrated) == len(original) == 114
    changed = sorted(n for n in migrated if migrated[n] != original[n])
    assert changed == sorted(VIEW_SEMANTICS.CHANGES)
    # Same order (dependencies) and the dependent view keeps its query.
    assert [i["name"] for i in dependency_order(list(migrated.values()))] == [
        i["name"] for i in dependency_order(definitions)
    ]
    assert migrated["test"] == original["test"]


def test_complete_schema_verifier_checks_the_translation_without_1789236851() -> None:
    # A database migrated through 1789236850 only (``_migrations`` without
    # 1789236851, detected by the scripts): the translated queries.
    definitions, _, _ = load_schema(SCHEMA, include_views=True)
    rows = compat_rows(definitions)
    report = verify_complete(definitions, rows, view_semantics=False)
    assert report["metadata_mismatches"] == sorted(
        f"{name}: options" for name in VIEW_SEMANTICS.CHANGES
    )
    translated = {i["name"]: i["options"]["query"] for i in definitions if i["type"] == "view"}
    for row in rows:
        if row["name"] in VIEW_SEMANTICS.CHANGES:
            row["options"]["query"] = translated[row["name"]]
    assert verify_complete(definitions, rows, view_semantics=False)["ok"] is True
