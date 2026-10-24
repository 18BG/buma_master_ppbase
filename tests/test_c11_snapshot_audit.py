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


def audit_snapshot(tmp_path):
    """Smallest snapshot the complete audit can read: two collections + PB tables."""
    import json

    child = {"id": "childcol0000001", "name": "child", "type": "base", "schema": []}
    parent = {
        "id": "parentcol000001",
        "name": "parent",
        "type": "base",
        "schema": [
            {"name": "child", "type": "relation", "options": {"collectionId": child["id"]}},
            {"name": "files", "type": "file", "options": {"maxSelect": 99}},
        ],
    }
    data = tmp_path / "backup"
    data.mkdir()
    with sqlite3.connect(data / "data.db") as db:
        db.execute("CREATE TABLE _collections (id TEXT, name TEXT, type TEXT, schema JSON)")
        for c in (child, parent):
            db.execute(
                "INSERT INTO _collections VALUES (?,?,?,?)",
                (c["id"], c["name"], c["type"], json.dumps(c["schema"])),
            )
        db.executescript(
            "CREATE TABLE child(id TEXT PRIMARY KEY); INSERT INTO child VALUES('a');"
            "CREATE TABLE parent(id TEXT PRIMARY KEY, child TEXT, files TEXT);"
            'INSERT INTO parent VALUES(\'p1\',\'["a","absent"]\',\'["kept","lost"]\');'
            "CREATE TABLE _params(key TEXT, value JSON);"
            "INSERT INTO _params VALUES('settings','{\"s3\":{\"enabled\":false}}');"
            "CREATE TABLE _admins(id TEXT);"
            "CREATE TABLE buma_transaction(type TEXT, author TEXT, system_id TEXT, "
            "mobile_message_id TEXT);"
            "CREATE TABLE buma_sale(id TEXT, customer TEXT);"
            "CREATE TABLE buma_customer(id TEXT); CREATE TABLE buma_customer_old(id TEXT);"
        )
    stored = data / "storage" / parent["id"] / "p1"
    stored.mkdir(parents=True)
    (stored / "kept").write_bytes(b"bytes")
    (stored / "thumbs_kept").mkdir()
    (stored / "thumbs_kept" / "100x100_kept").write_bytes(b"thumb")
    return data


def test_audit_manifest_mode_matches_directory_mode_without_file_bytes(tmp_path):
    import pytest

    from scripts.c11_import import ImportBlocked, directory_manifest, write_json_new
    from scripts.c11_snapshot_audit import audit

    data = audit_snapshot(tmp_path)
    directory, directory_private = audit(data, source_version="0.22.27")
    manifest = tmp_path / "manifest.json"
    write_json_new(manifest, directory_manifest(data))
    bundle = tmp_path / "bundle"
    bundle.mkdir()
    (bundle / "data.db").write_bytes((data / "data.db").read_bytes())
    report, private = audit(bundle, source_version="0.22.27", manifest=manifest)
    assert (report["storage_mode"], report["file_bytes_verified"], report["file_bytes_copied"]) == (
        "manifest",
        False,
        False,
    )
    assert directory["storage_mode"] == "directory"
    for key in ("relations", "files", "file_manifest_sha256", "relations_summary"):
        assert report[key] == directory[key], key
    assert private == directory_private
    assert private["missing"] == ["parentcol000001/p1/lost"]
    assert private["unreferenced"] == ["parentcol000001/p1/thumbs_kept/100x100_kept"]
    assert report["relations"][0]["missing_references"] == 1
    with pytest.raises(ImportBlocked, match="storage_directory_and_manifest_both_supplied"):
        audit(data, source_version="0.22.27", manifest=manifest)


def test_audit_reads_field_multiplicity_independently_and_agrees_on_the_export():
    import json
    import os
    from pathlib import Path

    from scripts import c11_import, c11_snapshot_audit

    audit_rule, importer_rule = c11_snapshot_audit.source_multiple, c11_import.source_multiple
    assert audit_rule is not importer_rule
    for kind in ("relation", "file", "select"):
        for maximum in (None, 0, 1, 2, 99):
            field = {"type": kind, "options": {"maxSelect": maximum}}
            expected = maximum not in (1,) if kind == "relation" else (maximum or 1) > 1
            assert audit_rule(field) is expected is importer_rule(field), (kind, maximum)
    export = Path(
        os.environ.get(
            "BUMA_SCHEMA_EXPORT", "/Users/omarsisss/Downloads/buma-master-pb_schema-corrected.json"
        )
    )
    fields = [
        field
        for collection in json.loads(export.read_text())
        if collection["type"] != "view"
        for field in collection["schema"]
        if field["type"] in {"relation", "file", "select"}
    ]
    assert len(fields) > 100
    assert [audit_rule(f) for f in fields] == [importer_rule(f) for f in fields]


def test_audit_manifest_mode_refuses_a_storage_symlink(tmp_path):
    import pytest

    from scripts.c11_import import ImportBlocked, directory_manifest, write_json_new
    from scripts.c11_snapshot_audit import audit

    data = audit_snapshot(tmp_path)
    manifest = tmp_path / "manifest.json"
    write_json_new(manifest, directory_manifest(data))
    bundle = tmp_path / "bundle"
    bundle.mkdir()
    (bundle / "data.db").write_bytes((data / "data.db").read_bytes())
    (bundle / "storage").symlink_to(tmp_path / "absent")  # dangling, still refused
    with pytest.raises(ImportBlocked, match="storage_directory_and_manifest_both_supplied"):
        audit(bundle, source_version="0.22.27", manifest=manifest)
