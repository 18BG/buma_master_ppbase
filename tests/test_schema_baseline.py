import copy
import json
import os
from pathlib import Path

from scripts.generate_native_baseline import load_schema
from scripts.verify_schema_baseline import SOURCE_INDEX_DELTAS, verify

ROOT = Path(__file__).resolve().parents[1]
SCHEMA = Path(
    os.environ.get(
        "BUMA_SCHEMA_EXPORT", "/Users/omarsisss/Downloads/buma-master-pb_schema-corrected.json"
    )
)


def _apply_source_index_deltas(rows: list[dict]) -> None:
    # Same metadata effect as migration 1789236849 on the installed schema.
    for name, statements in SOURCE_INDEX_DELTAS.items():
        next(row for row in rows if row["name"] == name)["indexes"].extend(statements)


def test_native_baseline_report_matches_final_schema_export() -> None:
    report = json.loads((ROOT / "docs/schema-baseline-report.json").read_text())
    assert report["include_views"] is False
    assert report["definitions"] == 72
    assert report["base_auth"] == 72
    assert report["views"] == 0
    assert report["schema_sha256"] == (
        "9d99e7b54118c41e0a303dd4805b900c7ee7b526b0cc63c83d044b7305b2313d"
    )


def test_native_baseline_is_a_snapshot_migration() -> None:
    migration = (ROOT / "pb_migrations/1789236840_buma_master_schema_baseline.py").read_text()
    assert "async def up(app)" in migration
    assert "async def down(app)" in migration
    assert "_CREATED_COLLECTIONS" in migration
    assert "Apply the complete collections snapshot" in migration
    assert "destructive rollback" in migration


def test_targeted_baseline_verifier_ignores_views_on_a_full_install() -> None:
    definitions, _, _ = load_schema(SCHEMA, include_views=False)
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
    rows.extend(
        [
            {
                "id": "example_view",
                "name": "example_view",
                "type": "view",
                "system": False,
                "schema": [],
                "indexes": [],
                "list_rule": None,
                "view_rule": None,
                "create_rule": None,
                "update_rule": None,
                "delete_rule": None,
                "options": {},
            },
        ]
    )
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
    _apply_source_index_deltas(rows)

    report = verify(definitions, rows)
    assert report["actual_non_system"] == 72
    assert report["unexpected"] == []
    assert report["allowed_extras"] == []
    assert report["ok"] is True


def test_baseline_verifier_accepts_materialized_auth_password_field() -> None:
    definitions, _, _ = load_schema(SCHEMA, include_views=False)
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
    for name, minimum in (("users", 5), ("agent_users", 8)):
        row = next(row for row in rows if row["name"] == name)
        row["schema"].append(
            {
                "id": "password_field",
                "name": "password",
                "type": "password",
                "required": True,
                "system": True,
                "hidden": True,
                "presentable": False,
                "options": {"min": minimum, "max": 0, "cost": 10, "pattern": ""},
            }
        )
        row["options"].pop("minPasswordLength", None)
    _apply_source_index_deltas(rows)

    report = verify(definitions, rows)
    assert report["metadata_mismatches"] == []
    assert report["ok"] is True


def test_verifier_requires_the_unapplied_source_index_on_top_of_the_export() -> None:
    export_indexes = ['CREATE INDEX "idx_bEWTmoj" ON "buma_transaction" ("mobile_message_id")']
    definition = {
        "id": "wv9d4bsvf0i5kki",
        "name": "buma_transaction",
        "type": "base",
        "system": False,
        "schema": [],
        "indexes": export_indexes,
        "listRule": "",
        "viewRule": "",
        "createRule": "",
        "updateRule": "",
        "deleteRule": None,
        "options": {},
    }
    row = {
        "id": "wv9d4bsvf0i5kki",
        "name": "buma_transaction",
        "type": "base",
        "system": False,
        "schema": [],
        "indexes": list(export_indexes),
        "list_rule": "",
        "view_rule": "",
        "create_rule": "",
        "update_rule": "",
        "delete_rule": None,
        "options": {},
    }
    assert verify([definition], [row])["metadata_mismatches"] == ["buma_transaction: indexes"]
    _apply_source_index_deltas([row])
    assert verify([definition], [row])["ok"] is True
