import copy
from pathlib import Path

from scripts.generate_native_baseline import load_schema
from scripts.verify_complete_schema import verify as verify_complete
from scripts.verify_view_queries import dependency_order

SCHEMA = Path("/Users/omarsisss/Downloads/buma-master-pb_schema-corrected.json")


def test_view_dependency_order_contains_all_views_once() -> None:
    definitions, _, _ = load_schema(SCHEMA, include_views=True)
    ordered = dependency_order(definitions)
    names = [str(item["name"]) for item in ordered]
    assert len(names) == 42
    assert len(names) == len(set(names))
    assert "buma_transaction_list_view" in names
    assert names.index("buma_transaction_list_view") < names.index("test")


def test_complete_schema_verifier_requires_all_definitions_and_username_compat() -> None:
    definitions, _, _ = load_schema(SCHEMA, include_views=True)
    rows = [
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
        for item in definitions
    ]
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

    report = verify_complete(definitions, rows)
    assert report["expected"] == 114
    assert report["expected_base_auth"] == 72
    assert report["expected_views"] == 42
    assert report["allowed_extras"] == []
    assert report["required_compat_fields"] == ["agent_users.username", "users.username"]
    assert report["missing_compat_fields"] == []
    assert report["ok"] is True
