"""Audit all references even when one source record cannot be imported."""

import sqlite3

from scripts.c11_snapshot_audit import files_audit, relation_audit


def database():
    db = sqlite3.connect(":memory:")
    db.row_factory = sqlite3.Row
    return db


def test_unlimited_array_relations_are_audited_without_flattening():
    with database() as db:
        db.executescript(
            "CREATE TABLE parent(id TEXT PRIMARY KEY, children TEXT);"
            "CREATE TABLE child(id TEXT PRIMARY KEY); INSERT INTO child VALUES('a');"
            "INSERT INTO parent VALUES('p1','[\"a\",\"absent\"]');"
            "INSERT INTO parent VALUES('p2','[]');"
            "INSERT INTO parent VALUES('p3','malformed');"
        )
        result = relation_audit(
            db,
            {"name": "parent"},
            {"name": "children", "type": "relation", "options": {"maxSelect": None}},
            {"name": "child"},
        )
        assert result["references"] == 2
        assert result["missing_references"] == 1
        assert result["invalid_array_rows"] == 1
        assert result["array_length_distribution"] == {"0": 1, "2": 1}
        assert (
            db.execute("SELECT children FROM parent WHERE id='p1'").fetchone()[0]
            == '["a","absent"]'
        )


def test_scalar_missing_relations_count_records_and_distinct_targets_separately():
    with database() as db:
        db.executescript(
            "CREATE TABLE parent(id TEXT PRIMARY KEY, child TEXT);"
            "CREATE TABLE child(id TEXT PRIMARY KEY); INSERT INTO child VALUES('a');"
            "INSERT INTO parent VALUES('p1','absent'),('p2','absent'),('p3','a'),('p4','');"
        )
        result = relation_audit(
            db,
            {"name": "parent"},
            {"name": "child", "type": "relation", "options": {"maxSelect": 1}},
            {"name": "child"},
        )
        assert result["references"] == 3
        assert result["rows_with_missing_references"] == 2
        assert result["distinct_missing_targets"] == 1


def test_files_continue_after_missing_entries_and_check_all_fields():
    with database() as db:
        db.executescript(
            "CREATE TABLE record(id TEXT PRIMARY KEY, files TEXT, photo TEXT);"
            "INSERT INTO record VALUES('r','[\"missing\",\"found\"]','other_missing');"
        )
        collection = {
            "name": "record",
            "id": "c",
            "type": "base",
            "schema": [
                {"name": "files", "type": "file", "options": {"maxSelect": 99}},
                {"name": "photo", "type": "file", "options": {"maxSelect": 1}},
            ],
        }
        result, private = files_audit(
            db, [collection], {"c/r/found": {"bytes": 4}, "c/r/cache": {"bytes": 7}}
        )
        assert result["missing_unique_files"] == 2
        assert result["present_referenced_files"] == 1
        assert result["unreferenced_files"] == 1
        assert result["fields"][0]["references"] == 2
        assert private["missing"] == ["c/r/missing", "c/r/other_missing"]
