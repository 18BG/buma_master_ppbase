"""Fail-closed boundaries for the offline import preparation."""

import ast
import asyncio
import hashlib
import inspect
import json
import os
import re
import signal
import sqlite3
import subprocess
import sys
import traceback
import zipfile
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from pathlib import Path
from types import SimpleNamespace

import asyncpg
import pytest

import scripts.c11_import as c11
from scripts.c11_import import (
    ImportBlocked,
    array_value,
    date_value,
    digest,
    field_value,
    file_key,
    idempotency_key_conflicts,
    inspect_archive,
    mapped_row,
    snapshot,
    stage_snapshot,
    storage_inventory,
    write_json_new,
)


def test_multi_relations_are_never_flattened_to_target_scalar(monkeypatch):
    unbounded = {"type": "relation", "options": {"maxSelect": None}}
    # D-0019: element for element, order and repeated elements kept.
    assert field_value(unbounded, "[]") == []
    assert field_value(unbounded, '["b","a","b"]') == ["b", "a", "b"]
    for maximum in (0, -1, 0.0):
        for value in ("", "[]", '["first","second"]'):
            with pytest.raises(ImportBlocked, match="relation_max_select_not_positive"):
                field_value({"type": "relation", "options": {"maxSelect": maximum}}, value)
    for value in ("", '["first"]'):
        with pytest.raises(ImportBlocked, match="relation_max_select_absent_requires_decision"):
            field_value({"type": "relation", "options": {"collectionId": "x"}}, value)
    assert field_value({"type": "relation", "options": {"maxSelect": 3}}, '["a","b"]') == ["a", "b"]
    # A deliberate rollback of the flag never flattens either.
    monkeypatch.setattr(c11, "UNBOUNDED_RELATION_ARRAYS", False)
    for value in ("[]", '["first","second"]'):
        with pytest.raises(ImportBlocked, match="unbounded_relation_target_scalar"):
            field_value(unbounded, value)


@pytest.mark.parametrize("value", [2**53 + 1, float("nan"), float("inf"), "12"])
def test_lossy_numeric_conversions_are_blocked(value):
    with pytest.raises(ImportBlocked):
        field_value({"type": "number"}, value)


def test_integer_range_and_non_finite_json_are_checked():
    with pytest.raises(ImportBlocked, match="integer_outside_target_range"):
        field_value({"type": "number", "options": {"onlyInt": True}}, 2**31)
    # NaN is not JSON for Go: PocketBase serves that text as a JSON string.
    with pytest.raises(ImportBlocked, match="json_text_served_as_string_by_pocketbase"):
        field_value({"type": "json"}, '{"value":NaN}')
    with pytest.raises(ImportBlocked, match="non_finite_json"):
        field_value({"type": "json"}, float("inf"))  # a SQLite REAL Inf
    assert field_value({"type": "json"}, '{"nested":[false,0,"é"]}') == {"nested": [False, 0, "é"]}


def test_dates_keep_the_exact_pocketbase_instant():
    assert date_value("2024-02-29 12:34:56.789Z") == datetime(2024, 2, 29, 12, 34, 56, 789000, UTC)
    assert date_value("0001-01-01 00:00:00.000Z") == datetime(1, 1, 1, tzinfo=UTC)
    assert date_value("") is None
    for value in ("", None, 0, b"2024-02-29 12:34:56.789Z"):
        with pytest.raises(ImportBlocked, match="invalid_date"):
            date_value(value, required=True)
    # The layout matches, the calendar does not: never rolled over or clamped.
    for value in (
        "2023-02-29 00:00:00.000Z",
        "2024-13-01 00:00:00.000Z",
        "2024-01-01 24:00:00.000Z",
        "2024-01-01 23:59:60.000Z",
        "0000-01-01 00:00:00.000Z",
    ):
        with pytest.raises(ImportBlocked, match="invalid_date"):
            date_value(value)


@pytest.mark.parametrize(
    "value",
    [
        "2024-02-29 12:34:56Z",  # no milliseconds
        "2024-02-29 12:34:56.7Z",
        "2024-02-29 12:34:56.1234Z",
        "2024-02-29 12:34:56.123456Z",
        "2024-02-29 12:34:56.1234567Z",  # fromisoformat would truncate silently
        "2024-02-29T12:34:56.789Z",
        "2024-02-29 12:34:56.789+00:00",
        "2024-02-29 12:34:56.789+05:30",  # an offset would be converted
        "2024-02-29 12:34:56.789",
        "2024-02-29 12:34:56,789Z",
        "2024-02-29 12:34:56.789z",
        "20240229T123456Z",
        "2024-W09-4 12:34:56.789Z",
        "2024-02-29 12Z",
        "12:34:56.789Z",
        " 2024-02-29 12:34:56.789Z",
        "2024-02-29 12:34:56.789Z\n",
        "٢٠٢٤-02-29 12:34:56.789Z",  # non-ASCII digits
        "12024-02-29 12:34:56.789Z",
    ],
)
def test_dates_accept_only_the_layout_pocketbase_writes(value):
    with pytest.raises(ImportBlocked, match="date_not_pocketbase_layout"):
        date_value(value)


def test_multifiles_preserve_names_and_order_without_coercion():
    assert array_value('["z.bin","a.bin"]') == ["z.bin", "a.bin"]
    for value in ('"a"', "null", '["a",2]'):
        with pytest.raises(ImportBlocked):
            array_value(value)


def test_text_and_array_elements_refuse_values_postgresql_cannot_store():
    files = {"type": "file", "options": {"maxSelect": 99}}
    assert field_value(files, '["b.bin","a.bin","b.bin"]') == ["b.bin", "a.bin", "b.bin"]
    assert field_value(files, '["\\u00e9t\\u00e9.bin"]') == ["été.bin"]
    with pytest.raises(ImportBlocked, match="invalid_unicode_for_postgresql"):
        field_value(files, '["one.bin","\\ud800.bin"]')
    with pytest.raises(ImportBlocked, match="nul_in_postgresql_text"):
        field_value(files, '["one\\u0000.bin"]')
    with pytest.raises(ImportBlocked, match="invalid_unicode_for_postgresql"):
        field_value({"type": "text"}, "lone \udc80 surrogate")
    for kind, array, value in (
        ("text", True, ["ok", "\ud800"]),
        ("character varying", True, ["\udfff"]),
        ("text", False, "x\ud800"),
    ):
        target = c11.TargetColumn("c", kind, array, 15 if "varying" in kind else None, True, False)
        with pytest.raises(ImportBlocked, match="invalid_unicode_for_postgresql"):
            c11.check_value(target, value)
    c11.check_value(c11.TargetColumn("c", "character varying", False, 4, True, False), "été!")


@pytest.mark.parametrize("part", ["../x", "a/b", "a\\b", "..", ".", "", "x\x00"])
def test_storage_traversal_is_rejected(part):
    with pytest.raises(ImportBlocked, match="unsafe_storage_reference"):
        file_key("collection", "record", part)


def test_snapshot_does_not_create_sidecars_or_modify_source(tmp_path):
    with sqlite3.connect(tmp_path / "data.db") as conn:
        conn.execute("CREATE TABLE sample(id TEXT PRIMARY KEY)")
        conn.execute("INSERT INTO sample(rowid,id) VALUES(-17,'original')")
    before = digest(tmp_path / "data.db")
    with snapshot(tmp_path) as conn:
        assert tuple(conn.execute("SELECT rowid,id FROM sample").fetchone()) == (-17, "original")
        with pytest.raises(sqlite3.OperationalError):
            conn.execute("UPDATE sample SET id='changed'")
    assert digest(tmp_path / "data.db") == before
    assert {p.name for p in tmp_path.iterdir()} == {"data.db"}
    (tmp_path / "data.db-wal").write_bytes(b"incomplete WAL")
    with pytest.raises(ImportBlocked, match="live_or_incomplete"):
        with snapshot(tmp_path):
            pass


def test_original_rowid_required_and_unknown_columns_rejected():
    collection = {"name": "buma_article", "type": "base", "schema": []}
    source = {
        "id": "same",
        "created": "2024-01-01 00:00:00.000Z",
        "updated": "2024-01-01 00:00:00.000Z",
        "__c11_rowid": 7001,
    }
    values, archive = mapped_row(collection, source)
    assert values["id"] == "same" and values["rowid"] == 7001 and archive == {}
    with pytest.raises(ImportBlocked, match="unmapped_source_columns"):
        mapped_row(collection, {**source, "unexpected": "must not disappear"})
    with pytest.raises(KeyError):
        mapped_row(collection, {k: v for k, v in source.items() if k != "__c11_rowid"})


def test_only_the_frozen_source_index_blocks_transaction_history(tmp_path):
    insert = "INSERT INTO buma_transaction VALUES (?,?,?,?,?)"
    with sqlite3.connect(tmp_path / "data.db") as conn:
        conn.execute(
            "CREATE TABLE buma_transaction"
            "(id TEXT PRIMARY KEY, type TEXT, author TEXT, system_id TEXT, mobile_message_id TEXT)"
        )
        # Accepted by PocketBase: duplicate mobile ids and non-idem legacy keys.
        conn.executemany(
            insert,
            [
                ("mobile1", "RETURN", "a", "", "m1"),
                ("mobile2", "RETURN", "a", "", "m1"),
                ("legacy1", "RETURN", "a", "legacy", ""),
                ("legacy2", "RETURN", "a", "legacy", ""),
            ],
        )
    with snapshot(tmp_path) as db:
        assert idempotency_key_conflicts(db) == 0
    with sqlite3.connect(tmp_path / "data.db") as conn:
        # The source index is global and its LIKE folds ASCII case.
        conn.executemany(
            insert,
            [("idem1", "RETURN", "a", "IDEM:x", ""), ("idem2", "RECHARGE", "b", "IDEM:x", "")],
        )
    with snapshot(tmp_path) as db:
        assert idempotency_key_conflicts(db) == 1


def test_private_staging_verifies_bytes_and_never_replaces_existing_files(tmp_path):
    data = tmp_path / "source"
    data.mkdir()
    with sqlite3.connect(data / "data.db") as conn:
        conn.execute("CREATE TABLE sample(id TEXT)")
    (data / "storage").mkdir()
    (data / "storage" / "proof.bin").write_bytes(b"\x00\xfforiginal")
    manifest = storage_inventory(data / "storage")
    report = {
        "ready_for_isolated_rehearsal": True,
        "data_db_sha256": digest(data / "data.db"),
        "file_manifest_sha256": hashlib.sha256(
            json.dumps(manifest, sort_keys=True).encode()
        ).hexdigest(),
    }
    target = tmp_path / "staged"
    stage_snapshot(data, target, report)
    assert (target / "data.db").read_bytes() == (data / "data.db").read_bytes()
    assert storage_inventory(target / "storage") == manifest
    with pytest.raises(FileExistsError):
        stage_snapshot(data, target, report)
    (data / "storage" / "proof.bin").write_bytes(b"changed")
    failed = tmp_path / "changed_attempt"
    with pytest.raises(ImportBlocked, match="storage_changed_after_diagnosis"):
        stage_snapshot(data, failed, report)
    assert not failed.exists()


def test_directory_staging_keeps_every_level_private(tmp_path):
    data = tmp_path / "source"
    data.mkdir()
    with sqlite3.connect(data / "data.db") as conn:
        conn.execute("CREATE TABLE sample(id TEXT)")
    for relative in ("col/rec/a.bin", "col/rec/thumbs_a.bin/100x100_a.bin", "other/r2/b.bin"):
        path = data / "storage" / relative
        path.parent.mkdir(parents=True, exist_ok=True, mode=0o755)
        path.write_bytes(relative.encode())
        path.chmod(0o644)
    inventory = storage_inventory(data / "storage")
    report = {
        "ready_for_isolated_rehearsal": True,
        "data_db_sha256": digest(data / "data.db"),
        "file_manifest_sha256": c11.inventory_fingerprint(inventory),
    }
    staged = tmp_path / "staged"
    assert stage_snapshot(data, staged, report) == inventory
    directories = [p for p in staged.rglob("*") if p.is_dir()]
    assert len(directories) == 6  # storage, col, col/rec, thumbs, other, other/r2
    assert {oct(p.stat().st_mode & 0o777) for p in [staged, *directories]} == {"0o700"}
    assert {oct(p.stat().st_mode & 0o777) for p in staged.rglob("*") if p.is_file()} == {"0o600"}


def test_symlinks_and_report_overwrite_are_rejected(tmp_path):
    (tmp_path / "outside").write_bytes(b"outside")
    (tmp_path / "storage").mkdir()
    (tmp_path / "storage" / "link").symlink_to(tmp_path / "outside")
    with pytest.raises(ImportBlocked, match="symlink_in_storage"):
        storage_inventory(tmp_path / "storage")
    target = tmp_path / "report.json"
    write_json_new(target, {"first": True})
    with pytest.raises(FileExistsError):
        write_json_new(target, {"first": False})
    assert json.loads(target.read_text()) == {"first": True}
    assert target.stat().st_mode & 0o777 == 0o600


def test_a_failed_report_write_leaves_no_partial_file(tmp_path, monkeypatch):
    class Broken:
        def __init__(self, fd, mode):
            self.fd = fd

        def __enter__(self):
            return self

        def __exit__(self, *exc):
            c11.os.close(self.fd)

        def write(self, data):
            raise OSError("disk full")

    target = tmp_path / "report.json"
    monkeypatch.setattr(c11.os, "fdopen", Broken)
    with pytest.raises(OSError, match="disk full"):
        write_json_new(target, {"value": 1})
    assert not target.exists()
    # A value that cannot be encoded fails before any file is created.
    monkeypatch.undo()
    with pytest.raises(ValueError):
        write_json_new(target, {"value": float("nan")})
    assert not target.exists()


def test_archive_inventory_does_not_extract_or_claim_database_validation(tmp_path):
    path = tmp_path / "backup.zip"
    with zipfile.ZipFile(path, "w") as archive:
        archive.writestr("backup/data.db", b"not even a database")
        archive.writestr("backup/storage/id/record/file.bin", b"original")
    result = inspect_archive(path)
    assert result["ready_for_offline_extraction_review"]
    assert not result["ready_for_isolated_rehearsal"]
    assert not result["content_crc_verified"]
    assert {p.name for p in tmp_path.iterdir()} == {"backup.zip"}
    assert not hasattr(c11, "extract_archive")  # one verified extraction: c11_extract


@pytest.mark.parametrize(
    "member,code",
    [
        ("../escape", "unsafe_archive_member"),
        ("nested/data.db", "exactly_one_data_db_required"),
        ("data.db-wal", "live_or_incomplete_sqlite_snapshot"),
    ],
)
def test_archive_anomalies_are_blocked(tmp_path, member, code):
    path = tmp_path / "backup.zip"
    with zipfile.ZipFile(path, "w") as archive:
        archive.writestr("data.db", b"snapshot")
        archive.writestr(member, b"unexpected")
    result = inspect_archive(path)
    assert code in result["blockers"]
    assert not result["ready_for_offline_extraction_review"]


# ---------------------------------------------------------------------------
# WS-B wave 2: preserved anomalies, target types, versions, manifest mode.
# ---------------------------------------------------------------------------

TIMESTAMP = "2024-01-01 00:00:00.000Z"


def text_field(name, kind="text", **options):
    return {"id": "f" + name, "name": name, "type": kind, "system": False, "options": options}


CHILD = {
    "id": "childcol0000001",
    "name": "child",
    "type": "base",
    "system": False,
    "schema": [text_field("label")],
    "indexes": [],
    "options": {},
    "listRule": None,
    "viewRule": None,
    "createRule": None,
    "updateRule": None,
    "deleteRule": None,
}
PARENT = {
    **CHILD,
    "id": "parentcol000001",
    "name": "parent",
    "schema": [
        text_field("child", "relation", collectionId=CHILD["id"], maxSelect=1),
        text_field("photo", "file", maxSelect=1),
        text_field("gallery", "file", maxSelect=99),
        text_field("data", "json"),
        text_field("amount", "number"),
    ],
}
PARENT_ROWS = [
    # id, child, photo, gallery, data, amount
    ("p1", "c1", "a.bin", '["b.bin","missing.bin"]', '{"b":1,"a":2}', 12.75),
    ("p2", "absent", "gone.bin", "[]", None, 0),
    ("p3", "absent", "", "[]", "[1,2]", 5),
]


def tiny_snapshot(tmp_path, monkeypatch, parent_rows=PARENT_ROWS, parent=PARENT):
    """A PB 0.22-shaped SQLite snapshot and its own export (checksum patched)."""
    schema = [CHILD, parent]
    export = tmp_path / "schema.json"
    export.write_text(json.dumps(schema))
    monkeypatch.setattr(c11, "SCHEMA_SHA256", digest(export))
    data = tmp_path / "backup"
    data.mkdir()
    with sqlite3.connect(data / "data.db") as db:
        db.execute(
            "CREATE TABLE _collections (id TEXT PRIMARY KEY, system BOOLEAN, type TEXT, "
            "name TEXT, schema JSON, indexes JSON, listRule TEXT, viewRule TEXT, "
            "createRule TEXT, updateRule TEXT, deleteRule TEXT, options JSON)"
        )
        for c in schema:
            db.execute(
                "INSERT INTO _collections VALUES (?,?,?,?,?,?,?,?,?,?,?,?)",
                (c["id"], c["system"], c["type"], c["name"], json.dumps(c["schema"]))
                + ("[]", None, None, None, None, None, "{}"),
            )
        db.execute(
            "CREATE TABLE child (id TEXT PRIMARY KEY NOT NULL, created TEXT NOT NULL, "
            "updated TEXT NOT NULL, label TEXT DEFAULT '' NOT NULL)"
        )
        db.execute("INSERT INTO child VALUES ('c1', ?, ?, 'kept')", (TIMESTAMP, TIMESTAMP))
        columns = ", ".join(
            f"{f['name']} JSON"
            if f["type"] == "json"
            else f"{f['name']} NUMERIC"
            if f["type"] == "number"
            else f"{f['name']} TEXT"
            for f in parent["schema"]
        )
        db.execute(
            "CREATE TABLE parent (id TEXT PRIMARY KEY NOT NULL, created TEXT NOT NULL, "
            f"updated TEXT NOT NULL, {columns})"
        )
        for row in parent_rows:
            db.execute(
                f"INSERT INTO parent VALUES (?, ?, ?{', ?' * (len(row) - 1)})",
                (row[0], TIMESTAMP, TIMESTAMP, *row[1:]),
            )
        db.execute("CREATE TABLE buma_transaction (id TEXT PRIMARY KEY, system_id TEXT)")
    for record, name, content in (
        ("p1", "a.bin", b"A\x00"),
        ("p1", "b.bin", b"B"),
        ("p9", "orphan.bin", b"kept although unreferenced"),
    ):
        path = data / "storage" / PARENT["id"] / record / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(content)
    return data, export, schema


def manifest_bundle(tmp_path, data, change=None):
    manifest = c11.directory_manifest(data)
    if change:
        change(manifest)
    bundle = tmp_path / "bundle"
    bundle.mkdir(exist_ok=True)
    (bundle / "data.db").write_bytes((data / "data.db").read_bytes())
    path = tmp_path / f"manifest-{len(list(tmp_path.glob('manifest-*')))}.json"
    write_json_new(path, manifest)
    return bundle, path


def spec_for(definitions, overrides=None):
    """Target columns as PPBase creates them for the tiny schema."""
    kinds = {
        "relation": ("character varying", 15),
        "file": ("text", None),
        "json": ("jsonb", None),
        "number": ("double precision", None),
        "text": ("text", None),
    }
    spec = {}
    for c in definitions:
        columns = {
            "id": c11.TargetColumn("id", "character varying", False, 15, True, False),
            "created": c11.TargetColumn(
                "created", "timestamp with time zone", False, None, True, True
            ),
            "updated": c11.TargetColumn(
                "updated", "timestamp with time zone", False, None, True, True
            ),
        }
        for f in c["schema"]:
            kind, length = kinds[f["type"]]
            array = c11.source_multiple(f) and f["type"] != "relation"
            columns[f["name"]] = c11.TargetColumn(f["name"], kind, array, length, True, True)
        spec[c["name"]] = columns
    for (table, column), value in (overrides or {}).items():
        spec[table][column] = value
    return spec


def test_orphans_and_missing_files_are_preserved_and_reported(tmp_path, monkeypatch):
    data, export, _ = tiny_snapshot(tmp_path, monkeypatch)
    private = {}
    report = c11.diagnose(data, export, source_version="0.22.27", private=private)
    assert report["blockers"] == [] and report["ready_for_isolated_rehearsal"]
    anomalies = report["preserved_source_anomalies"]
    assert anomalies["policy"] == c11.ANOMALY_POLICY and anomalies["blocking"] is False
    assert anomalies["dangling_relations"] == {
        "references": 2,
        "records": 2,
        "fields": [
            {
                "collection": "parent",
                "field": "child",
                "target": "child",
                "references": 2,
                "records": 2,
                "distinct_targets": 1,
            }
        ],
    }
    assert anomalies["missing_files"]["references"] == 2
    assert anomalies["missing_files"]["records"] == 2
    assert sorted(
        (f["field"], f["references"], f["records"], f["distinct_files"])
        for f in anomalies["missing_files"]["fields"]
    ) == [("gallery", 1, 1, 1), ("photo", 1, 1, 1)]
    assert (report["files"], report["referenced_files"], report["present_referenced_files"]) == (
        3,
        4,
        2,
    )
    assert report["unreferenced_files"] == 1 and report["relations_checked"] == 3
    # Client-visible jsonb changes are explicit warnings with their affected rows.
    assert report["warnings"] == [
        {
            "code": "jsonb_key_order_normalized",
            "collection": "parent",
            "field": "data",
            "affected_rows": 1,
        },
        {
            "code": "sql_null_json_stored_as_jsonb_null",
            "collection": "parent",
            "field": "data",
            "affected_rows": 1,
        },
    ]
    assert "json_representation" not in report
    # Private details carry the record-level values; the public report never does.
    assert sorted(map(tuple, private["dangling_relations"])) == [
        ("parent", "child", "p2", "absent"),
        ("parent", "child", "p3", "absent"),
    ]
    assert sorted(private["missing_files"]) == [
        f"{PARENT['id']}/p1/missing.bin",
        f"{PARENT['id']}/p2/gone.bin",
    ]
    assert private["unreferenced_files"] == [f"{PARENT['id']}/p9/orphan.bin"]
    public = json.dumps(report)
    assert "absent" not in public and "gone.bin" not in public and '"p2"' not in public


def test_other_value_problems_still_block_with_field_counts(tmp_path, monkeypatch):
    rows = PARENT_ROWS + [("p4", "c1", "", "not json", '{"k":"\\u0000"}', "broken")]
    data, export, _ = tiny_snapshot(tmp_path, monkeypatch, parent_rows=rows)
    report = c11.diagnose(data, export, source_version="0.22.27")
    assert not report["ready_for_isolated_rehearsal"] and report["blocked_rows"] == 1
    assert {(b["code"], b.get("field"), b.get("affected_rows")) for b in report["blockers"]} == {
        ("invalid_array", "gallery", 1),
        ("nul_in_postgresql_jsonb", "data", 1),
        ("invalid_number", "amount", 1),
    }
    # The blocked row does not hide the anomalies of the other rows.
    assert report["preserved_source_anomalies"]["dangling_relations"]["references"] == 2


def test_duplicate_json_keys_block_and_never_collapse(tmp_path, monkeypatch):
    with pytest.raises(ImportBlocked, match="jsonb_duplicate_keys_collapsed"):
        c11.json_text('{"a":1,"a":2}')
    with pytest.raises(ImportBlocked, match="jsonb_duplicate_keys_collapsed"):
        c11.json_text('[{"x":{"k":true,"k":false}}]')
    assert c11.json_text('{"a":{"a":1},"b":{"a":2}}') == '{"a":{"a":1},"b":{"a":2}}'
    rows = PARENT_ROWS + [("p4", "c1", "", "[]", '{"k":1,"k":2}', 1)]
    data, export, _ = tiny_snapshot(tmp_path, monkeypatch, parent_rows=rows)
    report = c11.diagnose(data, export, source_version="0.22.27")
    assert report["blockers"] == [
        {
            "code": "jsonb_duplicate_keys_collapsed",
            "collection": "parent",
            "field": "data",
            "affected_rows": 1,
        }
    ]
    assert not report["ready_for_isolated_rehearsal"]


def test_auth_legacy_dates_are_checked_by_the_diagnosis(tmp_path, monkeypatch):
    assert c11.auth_legacy_text({}) == "{}"
    legacy = {"lastResetSentAt": "", "lastVerificationSentAt": "x", "lastLoginAlertSentAt": ""}
    assert json.loads(c11.auth_legacy_text(legacy)) == legacy
    with pytest.raises(ImportBlocked, match="nul_in_postgresql_jsonb"):
        c11.auth_legacy_text({**legacy, "lastResetSentAt": "2024\x00"})
    with pytest.raises(ImportBlocked, match="invalid_auth_field"):
        c11.auth_legacy_text({**legacy, "lastResetSentAt": float("nan")})
    users = {**CHILD, "id": "_pb_users_auth_", "name": "users", "type": "auth"}
    users["schema"] = []
    export = tmp_path / "schema.json"
    export.write_text(json.dumps([users]))
    monkeypatch.setattr(c11, "SCHEMA_SHA256", digest(export))
    data = tmp_path / "backup"
    (data / "storage").mkdir(parents=True)
    with sqlite3.connect(data / "data.db") as db:
        db.execute(
            "CREATE TABLE _collections (id TEXT PRIMARY KEY, system BOOLEAN, type TEXT, "
            "name TEXT, schema JSON, indexes JSON, listRule TEXT, viewRule TEXT, "
            "createRule TEXT, updateRule TEXT, deleteRule TEXT, options JSON)"
        )
        db.execute(
            "INSERT INTO _collections VALUES (?,?,?,?,?,?,?,?,?,?,?,?)",
            (users["id"], False, "auth", "users", "[]", "[]", None, None, None, None, None, "{}"),
        )
        db.execute(
            "CREATE TABLE users (id TEXT PRIMARY KEY, created TEXT, updated TEXT, username TEXT, "
            "email TEXT, emailVisibility BOOLEAN, verified BOOLEAN, tokenKey TEXT, "
            "passwordHash TEXT, lastResetSentAt TEXT, lastVerificationSentAt TEXT, "
            "lastLoginAlertSentAt TEXT)"
        )
        db.execute(
            "INSERT INTO users VALUES ('u1', ?, ?, 'u', '', 0, 1, 'k', 'h', ?, '', '')",
            (TIMESTAMP, TIMESTAMP, "2024\x00"),
        )
        db.execute("CREATE TABLE buma_transaction (id TEXT PRIMARY KEY, system_id TEXT)")
    report = c11.diagnose(data, export, source_version="0.22.27")
    assert report["blockers"] == [
        {
            "code": "nul_in_postgresql_jsonb",
            "collection": "users",
            "field": "auth_legacy",
            "affected_rows": 1,
        }
    ]


def test_lone_surrogates_block_and_private_reports_stay_valid_json(tmp_path, monkeypatch, capsys):
    rows = PARENT_ROWS + [("p5", "c1", "", '["one.bin","\\ud800.bin"]', "{}", 1)]
    data, export, _ = tiny_snapshot(tmp_path, monkeypatch, parent_rows=rows)
    report = c11.diagnose(data, export, source_version="0.22.27")
    assert {"code": "invalid_unicode_for_postgresql", "field": "gallery"} in [
        {k: b.get(k) for k in ("code", "field")} for b in report["blockers"]
    ]
    public, private = tmp_path / "public.json", tmp_path / "private.json"
    argv = ["c11_import", "--data", str(data), "--schema", str(export)]
    argv += ["--source-version", "0.22.27", "--report", str(public)]
    monkeypatch.setattr("sys.argv", argv + ["--private-report", str(private)])
    assert c11.main() == 1
    capsys.readouterr()
    raw = private.read_bytes()
    raw.decode("utf-8")  # valid UTF-8: the surrogate is an escape, not bytes
    assert b"\\ud800" in raw
    keys = json.loads(raw)["missing_files"]
    assert f"{PARENT['id']}/p5/\ud800.bin" in keys
    assert json.loads(public.read_text())["ready_for_isolated_rehearsal"] is False


def test_unbounded_relations_are_importable_and_their_orphans_are_counted(tmp_path, monkeypatch):
    unbounded = text_field("tags", "relation", collectionId=CHILD["id"], maxSelect=None)
    parent = {**PARENT, "schema": [*PARENT["schema"], unbounded]}
    rows = [row + ('["c1","joined,ids"]',) for row in PARENT_ROWS]
    data, export, schema = tiny_snapshot(tmp_path, monkeypatch, parent_rows=rows, parent=parent)
    reference = c11.diagnose(data, export, source_version="0.22.27")
    # D-0019 (UNBOUNDED_RELATION_ARRAYS): no blocker or warning for the field.
    assert reference["ready_for_isolated_rehearsal"], reference["blockers"]
    assert not [
        entry
        for entry in reference["blockers"] + reference["warnings"]
        if isinstance(entry, dict) and entry.get("field") == "tags"
    ]
    tags = next(
        f
        for f in reference["preserved_source_anomalies"]["dangling_relations"]["fields"]
        if f["field"] == "tags"
    )
    # One stored element is one reference: never split on the comma.
    assert (tags["references"], tags["records"], tags["distinct_targets"]) == (3, 3, 1)
    # The corrected PPBase target (text[]): the same diagnosis.
    spec = spec_for(schema)
    spec["parent"]["tags"] = c11.TargetColumn("tags", "text", True, None, True, True)
    report = c11.diagnose(data, export, source_version="0.22.27", target_columns=spec)
    assert report["blockers"] == reference["blockers"] and report["ready_for_isolated_rehearsal"]
    # A varchar(15)[] column refuses a 16-character element: never truncated.
    rows = [row + (json.dumps(["c1", "x" * 16]),) for row in PARENT_ROWS]
    (tmp_path / "long").mkdir()
    data, export, schema = tiny_snapshot(tmp_path / "long", monkeypatch, rows, parent)
    spec["parent"]["tags"] = c11.TargetColumn("tags", "character varying", True, 15, True, True)
    report = c11.diagnose(data, export, source_version="0.22.27", target_columns=spec)
    assert report["blockers"] == [
        {
            "code": "value_exceeds_target_length",
            "collection": "parent",
            "field": "tags",
            "affected_rows": 3,
        }
    ]


def test_unbounded_relation_is_reported_once_per_field(tmp_path, monkeypatch):
    unbounded = text_field("tags", "relation", collectionId=CHILD["id"], maxSelect=None)
    parent = {**PARENT, "schema": [*PARENT["schema"], unbounded]}
    rows = [row + ('["c1"]',) for row in PARENT_ROWS]
    data, export, schema = tiny_snapshot(tmp_path, monkeypatch, parent_rows=rows, parent=parent)
    spec = spec_for(schema)
    # A scalar target (a database not converted by 1789236850): D-0019 blocker.
    spec["parent"]["tags"] = c11.TargetColumn("tags", "character varying", False, 15, True, True)
    report = c11.diagnose(data, export, source_version="0.22.27", target_columns=spec)
    entries = [
        entry
        for entry in report["blockers"] + report["warnings"]
        if isinstance(entry, dict) and entry["code"] == "unbounded_relation_target_scalar"
    ]
    # The preflight details and the shape refusal of each row join one blocker.
    assert entries == [
        {
            "code": "unbounded_relation_target_scalar",
            "collection": "parent",
            "field": "tags",
            "expected": "character varying[]|text[]",
            "actual": "character varying(15)",
            "affected_rows": 3,
        }
    ]
    assert [b["code"] for b in report["blockers"]] == ["unbounded_relation_target_scalar"]
    # Empty table: the field is still reported, once, as a warning.
    (tmp_path / "empty").mkdir()
    data, export, schema = tiny_snapshot(tmp_path / "empty", monkeypatch, [], parent)
    report = c11.diagnose(data, export, source_version="0.22.27", target_columns=spec)
    assert report["blockers"] == []
    assert [w["code"] for w in report["warnings"]] == ["unbounded_relation_target_scalar"]
    assert report["warnings"][0]["actual"] == "character varying(15)"


def test_unbounded_relation_arrays_are_copied_exactly():
    field = {"type": "relation", "options": {"maxSelect": None}}
    assert c11.UNBOUNDED_RELATION_ARRAYS is True
    assert field_value(field, '["b","a","b"]') == ["b", "a", "b"]  # order, no dedup
    column = c11.TargetColumn("managers", "character varying", True, 15, True, True)
    element = "x" * 15 + "," + "y" * 15  # the 31-character legacy element
    with pytest.raises(ImportBlocked, match="value_exceeds_target_length"):
        c11.check_value(column, field_value(field, json.dumps([element])))
    c11.check_value(c11.TargetColumn("managers", "text", True, None, True, True), [element])
    for value in ('["a",1]', '["a",null]', '{"a":"b"}', "a", ""):
        with pytest.raises(ImportBlocked, match="invalid_array"):
            field_value(field, value)  # what the source stores is a list of texts


def test_relation_definition_problems_are_refused_whatever_the_target(tmp_path, monkeypatch):
    zero = text_field("zero", "relation", collectionId=CHILD["id"], maxSelect=0)
    absent = text_field("absent", "relation", collectionId=CHILD["id"])
    parent = {**PARENT, "schema": [*PARENT["schema"], zero, absent]}
    rows = [row + ("", '["c1"]') for row in PARENT_ROWS]
    data, export, schema = tiny_snapshot(tmp_path, monkeypatch, parent_rows=rows, parent=parent)
    report = c11.diagnose(data, export, source_version="0.22.27")
    refused = {
        (b["code"], b["field"]): b["affected_rows"]
        for b in report["blockers"]
        if b["code"] in c11.RELATION_DEFINITION_CODES
    }
    # Every row, its value empty or not: the definition itself has no meaning.
    assert refused == {
        ("relation_max_select_not_positive_requires_decision", "zero"): 3,
        ("relation_max_select_absent_requires_decision", "absent"): 3,
    }
    assert not report["ready_for_isolated_rehearsal"]
    # Absent key: references read as 0.22.34 stored them (a list): one "c1"
    # per row, present, so no orphan for that field.
    dangling = report["preserved_source_anomalies"]["dangling_relations"]["fields"]
    assert not [f for f in dangling if f["field"] in ("zero", "absent")]
    assert report["relations_checked"] == 3 + 3  # child (scalar) + absent (list)
    for column in (
        c11.TargetColumn("zero", "text", False, None, True, True),
        c11.TargetColumn("zero", "text", True, None, True, True),
    ):
        spec = spec_for(schema, {("parent", "zero"): column})
        spec["parent"]["absent"] = c11.TargetColumn("absent", "text", True, None, True, True)
        checked = c11.preflight(schema, spec, {"parent": 3})
        assert {(b["code"], b["field"]) for b in checked["blockers"]} == {
            ("relation_max_select_not_positive_requires_decision", "zero"),
            ("relation_max_select_absent_requires_decision", "absent"),
        }
        empty = c11.preflight(schema, spec, {"parent": 0})
        assert empty["blockers"] == [] and len(empty["warnings"]) == 2
    (tmp_path / "empty").mkdir()
    data, export, schema = tiny_snapshot(tmp_path / "empty", monkeypatch, [], parent)
    report = c11.diagnose(data, export, source_version="0.22.27")
    assert report["blockers"] == [] and {
        (w["code"], w["field"]) for w in report["warnings"] if isinstance(w, dict)
    } >= {
        ("relation_max_select_not_positive_requires_decision", "zero"),
        ("relation_max_select_absent_requires_decision", "absent"),
    }


def test_multiplicity_is_the_ppbase_rule_except_the_refused_absent_key():
    from ppbase.models import field_multiplicity as rule

    export = Path(os.environ["BUMA_SCHEMA_EXPORT"])
    fields = [
        field
        for collection in json.loads(export.read_text())
        if collection["type"] != "view"
        for field in collection["schema"]
    ]
    relations = [f for f in fields if f["type"] == "relation"]
    assert len(relations) == 191
    assert (
        sorted(f["options"]["maxSelect"] for f in relations if f["options"]["maxSelect"])
        == [1] * 188
    )
    assert sum(f["options"]["maxSelect"] is None for f in relations) == 3
    synthetic = [
        {"type": kind, "options": {"maxSelect": maximum}}
        for kind in ("relation", "select", "file", "text")
        for maximum in (None, 1, 2, 99)
    ] + [{"type": "relation", "maxSelect": None}, {"type": "select", "options": {}}]
    for field in fields + synthetic:
        assert c11.relation_definition_problem(field) is None
        assert c11.source_multiple(field) is rule.field_is_multiple(field), field
        limit = rule.relation_max_select(rule.field_options(field))
        assert c11.relation_unbounded(field) is (field["type"] == "relation" and limit is None)
    unbounded = {
        (c["name"], f["name"])
        for c in json.loads(export.read_text())
        if c["type"] != "view"
        for f in c["schema"]
        if c11.relation_unbounded(f)
    }
    assert unbounded == {
        ("buma_customer", "pdv"),
        ("buma_pilotage_ticket", "tag"),
        ("buma_wallet", "managers"),
    }


def test_source_version_registry_requires_passing_evidence(monkeypatch):
    assert c11.source_version_status(None) == ("source_pocketbase_version_unknown", None)
    assert c11.source_version_status("0.22.33") == ("source_pocketbase_version_unvalidated", None)
    assert c11.source_version_status("0.22.27") == (None, "docs/c11-rehearsal-report.json")
    # D-0024: the WS-D comparison of the official 0.22.27 and 0.22.34 binaries.
    assert c11.source_version_status("0.22.34") == (
        None,
        "docs/c11-pb-0.22.34-reference-report.json",
    )
    assert sorted(c11.VALIDATED_SOURCE_VERSIONS) == ["0.22.27", "0.22.34"]
    for version, entry in c11.VALIDATED_SOURCE_VERSIONS.items():
        evidence = json.loads((c11.PROJECT / entry["evidence"]).read_text())
        assert evidence["status"] == "passed", version
        stated = evidence
        for key in entry["version_path"]:
            stated = stated[key]
        assert stated == version  # a field of the evidence equal to the version
    broken = {"0.22.27": {"evidence": "docs/missing.json", "version_path": ["x"]}}
    monkeypatch.setattr(c11, "VALIDATED_SOURCE_VERSIONS", broken)
    assert c11.source_version_status("0.22.27")[0] == "source_version_evidence_invalid"
    wrong = {"0.22.27": {"evidence": "docs/c11-rehearsal-report.json", "version_path": ["scope"]}}
    monkeypatch.setattr(c11, "VALIDATED_SOURCE_VERSIONS", wrong)
    assert c11.source_version_status("0.22.27")[0] == "source_version_evidence_invalid"


def test_0_22_34_gate_rereads_its_evidence_and_its_conclusion(tmp_path, monkeypatch):
    entry = c11.VALIDATED_SOURCE_VERSIONS["0.22.34"]
    assert entry["evidence"] == "docs/c11-pb-0.22.34-reference-report.json"
    assert entry["true_paths"] == [["conclusion", "importer_can_accept_0_22_34"]]
    original = json.loads((c11.PROJECT / entry["evidence"]).read_text())
    assert original["conclusion"]["importer_can_accept_0_22_34"] is True

    def status_with(change):
        evidence = json.loads(json.dumps(original))
        change(evidence)
        path = tmp_path / f"evidence-{len(list(tmp_path.iterdir()))}.json"
        path.write_text(json.dumps(evidence))
        monkeypatch.setitem(c11.VALIDATED_SOURCE_VERSIONS, "0.22.34", {**entry, "evidence": path})
        return c11.source_version_status("0.22.34")

    assert status_with(lambda e: None)[0] is None  # the same document, read again
    invalid = "source_version_evidence_invalid"
    for value in (False, None, "true", 1):

        def conclusion(evidence, value=value):
            evidence["conclusion"]["importer_can_accept_0_22_34"] = value

        assert status_with(conclusion)[0] == invalid
    assert status_with(lambda e: e["conclusion"].clear())[0] == invalid
    assert status_with(lambda e: e.update(status="failed"))[0] == invalid

    def other_version(evidence):
        stated = evidence
        for key in entry["version_path"][:-1]:
            stated = stated[key]
        stated[entry["version_path"][-1]] = "0.22.27"

    assert status_with(other_version)[0] == invalid


def test_unvalidated_version_blocks_the_diagnosis(tmp_path, monkeypatch):
    data, export, _ = tiny_snapshot(tmp_path, monkeypatch)
    report = c11.diagnose(data, export, source_version="0.22.33")
    assert report["blockers"] == [{"code": "source_pocketbase_version_unvalidated"}]
    assert report["validated_source_versions"] == ["0.22.27", "0.22.34"]
    assert report["source_version_validated"] is False
    report = c11.diagnose(data, export, source_version="0.22.34")
    assert report["blockers"] == [] and report["ready_for_isolated_rehearsal"]
    assert report["source_version_validated"] is True
    assert report["source_version_evidence"] == "docs/c11-pb-0.22.34-reference-report.json"


@pytest.mark.parametrize(
    "column,value,code",
    [
        (("character varying", False, 15), "x" * 16, "value_exceeds_target_length"),
        # PostgreSQL would truncate these excess spaces silently.
        (("character varying", False, 15), "abc" + " " * 13, "value_exceeds_target_length"),
        (("character varying", True, 15), ["ok", "y" * 31], "value_exceeds_target_length"),
        (("text", False, None), "a\x00b", "nul_in_postgresql_text"),
        (("text", True, None), ["a\x00"], "nul_in_postgresql_text"),
        (("text", True, None), "scalar", "scalar_value_for_array_column"),
        (("text", False, None), ["list"], "array_value_for_scalar_column"),
        (("text", False, None), 3, "invalid_text"),
        (("text", False, None), None, "null_in_not_null_column"),
        (("integer", False, None), 2**31, "integer_outside_target_range"),
        (("integer", False, None), True, "invalid_number"),
        (("bigint", False, None), 2**63, "integer_outside_target_range"),
        (("double precision", False, None), float("inf"), "invalid_number"),
        (("double precision", False, None), 1, "invalid_number"),
        (("boolean", False, None), 1, "invalid_boolean"),
        (("timestamp with time zone", False, None), datetime(2024, 1, 1), "invalid_timestamp"),
        (("jsonb", False, None), {"parsed": True}, "invalid_json"),
        (("numeric", False, None), 1.0, "unsupported_target_column_type"),
    ],
)
def test_values_are_checked_against_target_columns_without_coercion(column, value, code):
    kind, array, length = column
    target = c11.TargetColumn("c", kind, array, length, True, False)
    with pytest.raises(ImportBlocked, match=code):
        c11.check_value(target, value)


def test_accepted_values_and_nullable_columns():
    c11.check_value(
        c11.TargetColumn("d", "timestamp with time zone", False, None, False, False), None
    )
    c11.check_value(c11.TargetColumn("i", "character varying", False, 15, True, False), "x" * 15)
    c11.check_value(c11.TargetColumn("r", "bigint", False, None, True, False), -17)
    c11.check_value(c11.TargetColumn("n", "double precision", False, None, True, True), -0.0)


@pytest.mark.parametrize(
    "text,code",
    [
        ('{"k":"\\u0000"}', "nul_in_postgresql_jsonb"),
        ('{"\\u0000":1}', "nul_in_postgresql_jsonb"),
        ('"\\ud800"', "invalid_unicode_for_postgresql"),
        ('{"\\udfff":1}', "invalid_unicode_for_postgresql"),
        ('{"v":-0}', "json_number_not_representable"),
        ('{"v":-0.0e5}', "json_number_not_representable"),
        ('{"v":1e-20000}', "json_number_outside_target_range"),
        ("[1e200000]", "json_number_outside_target_range"),
        ('{"v":1,"v":1}', "jsonb_duplicate_keys_collapsed"),
        # PocketBase serves these stored texts as JSON strings (PrepareValue).
        ('{"v":NaN}', "json_text_served_as_string_by_pocketbase"),
        ("not json", "json_text_served_as_string_by_pocketbase"),
        ("", "json_text_served_as_string_by_pocketbase"),
        (' {"b":1}', "json_text_served_as_string_by_pocketbase"),
        ("\n[1]", "json_text_served_as_string_by_pocketbase"),
        ("true ", "json_text_served_as_string_by_pocketbase"),
        ("True", "json_text_served_as_string_by_pocketbase"),
        ("NULL", "json_text_served_as_string_by_pocketbase"),
        ("+1", "json_text_served_as_string_by_pocketbase"),
        (".5", "json_text_served_as_string_by_pocketbase"),
        ("'a'", "json_text_served_as_string_by_pocketbase"),
        ("01", "json_text_served_as_string_by_pocketbase"),
        ("[1,2", "json_text_served_as_string_by_pocketbase"),
        # Invalid as a whole: served as a string, whatever its duplicate keys.
        ('{"a":1,"a":2} x', "json_text_served_as_string_by_pocketbase"),
        pytest.param("[" * 257 + "]" * 257, "json_nesting_too_deep", id="deep"),
        pytest.param("[" * 200000 + "]" * 200000, "json_nesting_too_deep", id="parser_recursion"),
    ],
)
def test_json_must_be_exactly_representable_in_jsonb(text, code):
    with pytest.raises(ImportBlocked, match=code):
        c11.json_text(text)


def test_json_source_text_is_kept_verbatim_and_key_order_changes_are_detected():
    exact = '{"b": 0.12345678901234567890123, "a": 1e400, "n": [1.50, true]}'
    assert c11.json_text(exact) == exact  # no float round-trip, no re-serialization
    assert (c11.json_text(None), c11.json_text(5), c11.json_text(1.5)) == ("null", "5", "1.5")
    # Texts PocketBase keeps raw, trailing whitespace included (valid JSON).
    for raw in ("null", "true", "false", '"text"', "-1", '{"b":1} ', "[]"):
        assert c11.json_text(raw) == raw
    with pytest.raises(ImportBlocked, match="json_number_not_representable"):
        c11.json_text(-0.0)
    key_order, notation = c11.JSONB_KEY_ORDER, c11.JSONB_NUMBER_NOTATION
    # jsonb orders keys by UTF-8 length, then bytes, at every depth.
    assert c11.jsonb_text_changes('{"aa":1,"b":2}') == {key_order}
    assert c11.jsonb_text_changes('[{"ok":1},{"x":{"zz":1,"y":2}}]') == {key_order}
    assert c11.jsonb_text_changes('{"é":1,"z":2}') == {key_order}  # é is two bytes
    assert not c11.jsonb_text_changes('{"b":1,"aa":[{"a":1}]}')
    assert not c11.jsonb_text_changes('{"z":1,"é":2}')
    assert not c11.jsonb_text_changes("null")
    assert not c11.jsonb_text_changes("[3,[]]")
    # Exponent notation is the only number text jsonb rewrites.
    for text in ("1e2", '{"x":1E+2}', "[1.5e-1]", '{"a":[0e0]}'):
        assert c11.jsonb_text_changes(text) == {notation}, text
    for text in ("100", "[1.50, 0.15, -2, 0.0, 123456789012345678901234567890]"):
        assert not c11.jsonb_text_changes(text), text
    assert c11.jsonb_text_changes('{"z":1e2,"a":1}') == {key_order, notation}


def test_row_keys_compare_exact_typed_values():
    # jsonb renders 1e2 as 100, keeps 1.50, sorts keys: the same JSON value.
    assert c11.canonical_json('{"b":[1.50,"x"],"a":1e2}') == c11.canonical_json(
        '{"a": 100, "b": [1.50, "x"]}'
    )
    assert c11.canonical_json('{"a":true}') != c11.canonical_json('{"a":1}')
    assert c11.decimal_key(Decimal("1E+400")) == c11.decimal_key(Decimal("1" + "0" * 400))
    stamp = date_value("2024-02-29 12:34:56.789Z")
    base = ("id", 1.5, True, stamp, ["a"], '{"b":1,"a":[1.0]}', None)
    same = ("id", 1.5, True, stamp, ["a"], '{"a": [1.0], "b": 1}', None)
    assert c11.row_key(base, (5,)) == c11.row_key(same, (5,))
    for position, other in (
        (1, -0.0 if base[1] == 0 else 1.5000000000000002),
        (2, 1),  # bool and int differ
        (3, stamp.replace(microsecond=789001)),
        (4, ["a", ""]),
        (5, '{"b":1,"a":[1.5]}'),
        (6, ""),
        (0, "id "),
    ):
        changed = list(base)
        changed[position] = other
        assert c11.row_key(changed, (5,)) != c11.row_key(base, (5,)), position
    assert c11.row_key((-0.0,)) != c11.row_key((0.0,))
    assert c11.row_key(("1",)) != c11.row_key((1,))


def test_table_fingerprint_is_an_order_independent_multiset():
    rows = [c11.row_key(("a", 1.0)), c11.row_key(("b", None)), c11.row_key(("b", None))]
    first, second, other = c11.Fingerprint(), c11.Fingerprint(), c11.Fingerprint()
    for row in rows:
        first.add(c11.row_digest(row))
    for row in reversed(rows):
        second.add(c11.row_digest(row))
    for row in rows[:2]:
        other.add(c11.row_digest(row))
    assert first.hexdigest() == second.hexdigest() != other.hexdigest()
    assert first.hexdigest().startswith("3:")


def test_target_columns_are_parsed_from_introspection():
    def row(name, data_type, **extra):
        base = {
            "column_name": name,
            "data_type": data_type,
            "character_maximum_length": None,
            "is_nullable": "NO",
            "has_default": True,
            "atttypmod": -1,
            "element_type": None,
        }
        return {**base, **extra}

    managers = c11.target_column(row("m", "ARRAY", atttypmod=19, element_type="varchar"))
    assert (managers.type, managers.array, managers.length) == ("character varying", True, 15)
    files = c11.target_column(row("f", "ARRAY", element_type="text"))
    assert (files.type, files.array, files.length) == ("text", True, None)
    email = c11.target_column(
        row("e", "character varying", character_maximum_length=255, is_nullable="YES")
    )
    assert (email.length, email.not_null) == (255, False)
    spec = {"t": {"m": managers, "e": email}}
    assert c11.target_spec_from_json(json.loads(json.dumps(c11.target_spec_json(spec)))) == spec
    with pytest.raises(ImportBlocked, match="target_spec_invalid"):
        c11.target_spec_from_json({"tables": {"t": [{"name": "x"}]}})


def test_preflight_requires_each_exported_field_with_expected_type():
    definitions = [CHILD, PARENT]
    assert c11.preflight(definitions, spec_for(definitions), {})["blockers"] == []
    bad = spec_for(
        definitions,
        {
            ("parent", "amount"): c11.TargetColumn("amount", "integer", False, None, True, True),
            ("parent", "gallery"): c11.TargetColumn("gallery", "text", False, None, True, True),
            ("parent", "data"): c11.TargetColumn("data", "numeric", False, None, True, True),
            ("child", "extra"): c11.TargetColumn("extra", "text", False, None, True, False),
            ("child", "noted"): c11.TargetColumn("noted", "text", False, None, True, True),
        },
    )
    del bad["parent"]["photo"]
    result = c11.preflight(definitions, bad, {})
    assert {(b["code"], b["collection"], b["field"]) for b in result["blockers"]} == {
        ("target_column_type_mismatch", "parent", "amount"),
        ("target_column_shape_mismatch", "parent", "gallery"),
        ("unsupported_target_column_type", "parent", "data"),
        ("target_column_missing", "parent", "photo"),
        ("target_required_column_not_supplied", "child", "extra"),
    }
    assert result["defaulted_target_columns"] == [{"collection": "child", "field": "noted"}]
    del bad["child"]
    assert {"code": "target_table_missing", "collection": "child"} in c11.preflight(
        definitions, bad, {}
    )["blockers"]


def test_preflight_unbounded_relation_is_a_warning_only_when_the_table_is_empty(monkeypatch):
    unbounded = text_field("tags", "relation", collectionId=CHILD["id"], maxSelect=None)
    parent = {**PARENT, "schema": [*PARENT["schema"], unbounded]}
    spec = spec_for([CHILD, parent])
    spec["parent"]["tags"] = c11.TargetColumn("tags", "character varying", False, 15, True, True)
    empty = c11.preflight([CHILD, parent], spec, {"parent": 0})
    assert empty["blockers"] == [] and empty["warnings"][0]["code"] == (
        "unbounded_relation_target_scalar"
    )
    full = c11.preflight([CHILD, parent], spec, {"parent": 3})
    assert [b["code"] for b in full["blockers"]] == ["unbounded_relation_target_scalar"]
    assert full["blockers"][0]["expected"] == "character varying[]|text[]"
    assert full["blockers"][0]["actual"] == "character varying(15)"
    # The corrected PPBase target (text[], or a converted varchar(15)[]).
    for column in (
        c11.TargetColumn("tags", "text", True, None, True, True),
        c11.TargetColumn("tags", "character varying", True, 15, True, True),
    ):
        spec["parent"]["tags"] = column
        for rows in (0, 3):
            checked = c11.preflight([CHILD, parent], spec, {"parent": rows})
            assert checked["blockers"] == checked["warnings"] == []
    # A deliberate rollback of the flag names the importer, not the target.
    monkeypatch.setattr(c11, "UNBOUNDED_RELATION_ARRAYS", False)
    arrays = c11.preflight([CHILD, parent], spec, {"parent": 3})
    assert [(b["code"], b["field"]) for b in arrays["blockers"]] == [
        ("unbounded_relation_arrays_not_enabled", "tags")
    ]
    empty = c11.preflight([CHILD, parent], spec, {"parent": 0})
    assert empty["blockers"] == [] and [w["code"] for w in empty["warnings"]] == [
        "unbounded_relation_arrays_not_enabled"
    ]


def test_diagnosis_checks_values_against_a_supplied_target_spec(tmp_path, monkeypatch):
    data, export, schema = tiny_snapshot(tmp_path, monkeypatch)
    narrow = c11.TargetColumn("child", "character varying", False, 3, True, True)
    spec = spec_for(schema, {("parent", "child"): narrow})
    report = c11.diagnose(data, export, source_version="0.22.27", target_columns=spec)
    assert report["target_types_checked"] is True
    assert report["blockers"] == [
        {
            "code": "value_exceeds_target_length",
            "collection": "parent",
            "field": "child",
            "affected_rows": 2,
        }
    ]


def test_manifest_mode_checks_references_without_file_bytes(tmp_path, monkeypatch):
    data, export, _ = tiny_snapshot(tmp_path, monkeypatch)
    directory = c11.diagnose(data, export, source_version="0.22.27")
    bundle, manifest = manifest_bundle(tmp_path, data)
    report = c11.diagnose(bundle, export, source_version="0.22.27", manifest=manifest)
    assert report["ready_for_isolated_rehearsal"]
    assert (report["storage_mode"], report["file_bytes_copied"], report["file_bytes_verified"]) == (
        "manifest",
        False,
        False,
    )
    # Same storage, same fingerprint, same anomalies as the directory mode.
    for key in ("file_manifest_sha256", "files", "file_bytes", "unreferenced_files"):
        assert report[key] == directory[key], key
    assert report["preserved_source_anomalies"] == directory["preserved_source_anomalies"]
    assert report["manifest"]["data_db_matches_manifest"] is True
    assert report["manifest"]["root_entries"] == ["data.db"]
    (bundle / "storage").mkdir()
    with pytest.raises(ImportBlocked, match="storage_directory_and_manifest_both_supplied"):
        c11.diagnose(bundle, export, source_version="0.22.27", manifest=manifest)
    (bundle / "storage").rmdir()
    (bundle / "storage").symlink_to(tmp_path / "nowhere")  # even a dangling link
    with pytest.raises(ImportBlocked, match="storage_directory_and_manifest_both_supplied"):
        c11.diagnose(bundle, export, source_version="0.22.27", manifest=manifest)


def entry(path, size=0, sha=c11.EMPTY_SHA256, crc=c11.EMPTY_CRC32):
    """A manifest entry; by default a consistent 0-byte object."""
    return {"path": path, "bytes": size, "sha256": sha, "crc32": crc}


@pytest.mark.parametrize(
    "change,code",
    [
        (lambda m: m.update(extra=1), "manifest_invalid"),
        (lambda m: m.update(archive_sha256="ABC"), "manifest_invalid"),
        (lambda m: m["files"].append({"path": "x"}), "manifest_invalid"),
        (lambda m: m["files"].append(entry("x", size=-1)), "manifest_invalid"),
        (lambda m: m["files"].append(entry("x", crc="123")), "manifest_invalid"),
        # A 0-byte object has exactly the empty SHA-256 and CRC-32.
        (lambda m: m["files"].append(entry("data.db-wal", sha="0" * 64)), "manifest_invalid"),
        (lambda m: m["files"].append(entry("data.db-wal", crc="1" * 8)), "manifest_invalid"),
        (lambda m: m["files"].append(entry("storage/c/r/f", sha="0" * 64)), "manifest_invalid"),
        (lambda m: m["files"].append(entry("storage/../x")), "manifest_unsafe_entry"),
        (lambda m: m["files"].append(entry("/abs")), "manifest_unsafe_entry"),
        (lambda m: m["files"].append(entry("storage\\x")), "manifest_unsafe_entry"),
        (lambda m: m["files"].append(entry("storage//x")), "manifest_unsafe_entry"),
        (lambda m: m["files"].append(entry("./storage/x")), "manifest_unsafe_entry"),
        (lambda m: m["files"].append(dict(m["files"][0])), "manifest_duplicate_entry"),
        (lambda m: m["files"].append(entry("pb_data/x")), "unexpected_manifest_entry"),
        (
            lambda m: m["files"].append(entry("data.db-wal", size=1, sha="1" * 64)),
            "live_or_incomplete_sqlite_snapshot",
        ),
        (
            lambda m: m["files"].append(entry("data.db-journal", size=8, sha="1" * 64)),
            "live_or_incomplete_sqlite_snapshot",
        ),
        (lambda m: m["files"].pop(0), "manifest_data_db_entry_required"),
        (lambda m: m["files"][0].update(sha256="1" * 64), "data_db_manifest_mismatch"),
        (lambda m: m["files"][0].update(crc32="1" * 8), "data_db_manifest_mismatch"),
        (lambda m: m["files"][0].update(bytes=1), "data_db_manifest_mismatch"),
    ],
)
def test_manifest_is_validated_and_must_describe_this_data_db(tmp_path, monkeypatch, change, code):
    data, export, _ = tiny_snapshot(tmp_path, monkeypatch)

    def first_data_db(manifest):
        manifest["files"].sort(key=lambda item: item["path"] != "data.db")
        change(manifest)

    bundle, path = manifest_bundle(tmp_path, data, first_data_db)
    with pytest.raises(ImportBlocked, match=code):
        c11.read_manifest(path, bundle / "data.db")


def test_empty_sidecars_are_accepted_and_reported(tmp_path, monkeypatch):
    data, export, _ = tiny_snapshot(tmp_path, monkeypatch)
    bundle, path = manifest_bundle(
        tmp_path, data, lambda m: m["files"].append(entry("data.db-wal"))
    )
    checked = c11.read_manifest(path, bundle / "data.db")
    assert checked["summary"]["empty_sqlite_sidecars"] == ["data.db-wal"]
    assert checked["storage"] == storage_inventory(data / "storage")


# ---------------------------------------------------------------------------
# Load: transaction checks, verification layers and their failure codes.
# ---------------------------------------------------------------------------


class FakeResult:
    def __init__(self, value):
        self.value = value

    def scalar_one(self):
        return self.value


class FakeRaw:
    def __init__(self, transaction):
        self.driver_connection = self
        self.transaction = transaction

    def is_in_transaction(self):
        return self.transaction


class FakeConnection:
    """Answers only current_database(); proves the checks that precede SQL."""

    def __init__(self, database="buma_c11_rehearsal", transaction=True, driver_transaction=True):
        self.database, self.transaction = database, transaction
        self.driver_transaction = driver_transaction
        self.statements = []

    async def execute(self, statement, parameters=None):
        self.statements.append(str(statement))
        return FakeResult(self.database)

    def in_transaction(self):
        return self.transaction

    async def get_raw_connection(self):
        return FakeRaw(self.driver_transaction)


def test_manifest_staging_copies_only_data_db_and_load_checks_the_mode(tmp_path, monkeypatch):
    data, export, schema = tiny_snapshot(tmp_path, monkeypatch)
    bundle, manifest = manifest_bundle(tmp_path, data)
    report = c11.diagnose(bundle, export, source_version="0.22.27", manifest=manifest)
    with pytest.raises(ImportBlocked, match="storage_mode_mismatch"):
        stage_snapshot(bundle, tmp_path / "wrong", report)
    assert not (tmp_path / "wrong").exists()
    staged = tmp_path / "staged"
    inventory = stage_snapshot(bundle, staged, report, manifest=manifest)
    assert sorted(p.name for p in staged.iterdir()) == ["data.db", "manifest.json"]
    assert inventory == storage_inventory(data / "storage")
    written = json.loads((staged / "manifest.json").read_text())
    assert (written["storage_mode"], written["file_bytes_copied"]) == ("manifest", False)
    assert (staged / "manifest.json").stat().st_mode & 0o777 == 0o600
    (staged / "storage").mkdir()
    connection = FakeConnection()
    with pytest.raises(ImportBlocked, match="storage_mode_mismatch"):
        asyncio.run(c11.load_isolated(connection, staged, schema))
    assert connection.statements == []
    (staged / "storage").rmdir()
    # A manifest edited after the diagnosis cannot be staged.
    changed = json.loads(manifest.read_text())
    changed["files"].append(entry("README.md", size=3))
    manifest.unlink()
    write_json_new(manifest, changed)
    with pytest.raises(ImportBlocked, match="manifest_changed_after_diagnosis"):
        stage_snapshot(bundle, tmp_path / "late", report, manifest=manifest)
    assert not (tmp_path / "late").exists()


def test_load_requires_a_caller_transaction_before_any_statement(tmp_path, monkeypatch):
    data, export, schema = tiny_snapshot(tmp_path, monkeypatch)
    report = c11.diagnose(data, export, source_version="0.22.27")
    staged = tmp_path / "staged"
    stage_snapshot(data, staged, report)
    # engine.connect() without begin(): refused before SQLAlchemy can autobegin.
    connection = FakeConnection(transaction=False)
    with pytest.raises(ImportBlocked, match="caller_transaction_required"):
        asyncio.run(c11.load_isolated(connection, staged, schema))
    assert connection.statements == []
    # An AUTOCOMMIT driver connection: refused before LOCK or any write.
    connection = FakeConnection(driver_transaction=False)
    with pytest.raises(ImportBlocked, match="caller_transaction_required"):
        asyncio.run(c11.load_isolated(connection, staged, schema))
    assert connection.statements == ["SELECT current_database()"]
    connection = FakeConnection("other")
    with pytest.raises(ImportBlocked, match="isolated_rehearsal_database_required"):
        asyncio.run(c11.load_isolated(connection, staged, schema))
    assert connection.statements == ["SELECT current_database()"]


class ImportEngine:
    """AsyncEngine stand-in for import_snapshot: connect() and begin() contexts.

    ``events`` records each context and how it ended: an exception leaving
    ``begin()`` is the rollback of the single import transaction.
    """

    def __init__(self, connection=None, *, hide=True):
        self.sync_engine = SimpleNamespace(hide_parameters=hide)
        self.connection = connection or FakeConnection()
        self.events = []

    def connect(self):
        return self._context("connect")

    def begin(self):
        return self._context("begin")

    def _context(self, kind):
        engine = self

        class Context:
            async def __aenter__(self):
                engine.events.append(kind)
                return engine.connection

            async def __aexit__(self, failure, *_):
                engine.events.append(f"{kind}:{'rollback' if failure else 'end'}")
                return False

        return Context()


def recorded_import(monkeypatch, engine, fail=None):
    """Replace the steps of import_snapshot by recorders sharing engine.events."""
    import scripts.c11_auth_import as auth

    def step(name, value):
        async def run(connection, *args, **kwargs):
            engine.events.append(name)
            if name != "check_ready":
                assert connection is engine.connection  # the begin() connection
            if fail == name:
                raise ImportBlocked(f"injected_{name}_failure")
            return value

        return run

    monkeypatch.setattr(auth, "check_ready", step("check_ready", None))
    monkeypatch.setattr(c11, "load_isolated", step("load", 3))
    monkeypatch.setattr(c11, "recount_relation_arrays", step("recount", [{"field": "tags"}]))
    monkeypatch.setattr(auth, "import_auth", step("auth", {"imported": True}))


def test_import_snapshot_removes_its_staging_after_any_failure(tmp_path, monkeypatch):
    data, export, schema = tiny_snapshot(tmp_path, monkeypatch)
    report = c11.diagnose(data, export, source_version="0.22.27")
    import scripts.c11_auth_import as auth

    async def ready(connection, *args):
        engine.events.append("check_ready")

    monkeypatch.setattr(auth, "check_ready", ready)
    engine = ImportEngine(FakeConnection("other"))
    destination = tmp_path / "attempt"
    with pytest.raises(ImportBlocked, match="isolated_rehearsal_database_required"):
        asyncio.run(c11.import_snapshot(engine, data, destination, report, schema))
    assert engine.events == ["connect", "check_ready", "connect:end", "begin", "begin:rollback"]
    assert not destination.exists()
    # The same new path can be used again by a clean retry.
    assert stage_snapshot(data, destination, report) == storage_inventory(data / "storage")


def test_import_snapshot_loads_records_then_accounts_in_one_transaction(tmp_path, monkeypatch):
    data, export, schema = tiny_snapshot(tmp_path, monkeypatch)
    diagnostic = c11.diagnose(data, export, source_version="0.22.27")
    engine = ImportEngine()
    recorded_import(monkeypatch, engine)
    evidence = {}
    destination = tmp_path / "imported"
    count = asyncio.run(
        c11.import_snapshot(engine, data, destination, diagnostic, schema, report=evidence)
    )
    assert count == 3
    assert engine.events == [
        "connect",
        "check_ready",
        "connect:end",
        "begin",
        "load",
        "recount",
        "auth",
        "begin:end",
    ]
    assert destination.is_dir()  # kept for serving after a commit
    assert evidence["relation_arrays"] == [{"field": "tags"}]
    assert evidence["auth"] == {"imported": True}
    assert evidence["single_transaction"] == {
        "records": 3,
        "relation_arrays_recounted": 1,
        "accounts_and_settings": True,
        "committed": True,
    }
    assert set(evidence["timings_seconds"]) == {
        "readiness_check",
        "staging",
        "relation_arrays",
        "auth_import",
        "commit",
    }


@pytest.mark.parametrize("failing", ["load", "recount", "auth"])
def test_import_snapshot_failure_of_one_step_imports_nothing(tmp_path, monkeypatch, failing):
    data, export, schema = tiny_snapshot(tmp_path, monkeypatch)
    diagnostic = c11.diagnose(data, export, source_version="0.22.27")
    engine = ImportEngine()
    recorded_import(monkeypatch, engine, fail=failing)
    destination, evidence = tmp_path / "attempt", {}
    with pytest.raises(ImportBlocked, match=f"injected_{failing}_failure"):
        asyncio.run(
            c11.import_snapshot(engine, data, destination, diagnostic, schema, report=evidence)
        )
    steps = ["load", "recount", "auth"]
    ran = steps[: steps.index(failing) + 1]
    # Records, recount and accounts share begin(): the failure rolls all back.
    expected = ["connect", "check_ready", "connect:end", "begin", *ran, "begin:rollback"]
    assert engine.events == expected
    assert "single_transaction" not in evidence and "auth" not in evidence
    assert not destination.exists()


def test_import_snapshot_refuses_before_any_database_access(tmp_path, monkeypatch):
    data, export, schema = tiny_snapshot(tmp_path, monkeypatch)
    diagnostic = c11.diagnose(data, export, source_version="0.22.27")
    destination = tmp_path / "attempt"
    cases = (
        (ImportEngine(hide=False), diagnostic, {}, "engine_hide_parameters_required"),
        (ImportEngine(), {**diagnostic, "ready_for_isolated_rehearsal": False}, {}, "diagnostic"),
    )
    for engine, report, options, code in cases:
        recorded_import(monkeypatch, engine)
        with pytest.raises(ImportBlocked, match=code):
            asyncio.run(c11.import_snapshot(engine, data, destination, report, schema, **options))
        assert engine.events == [] and not destination.exists()
    engine = ImportEngine()
    with pytest.raises(ValueError, match="unknown failure stage"):
        asyncio.run(
            c11.import_snapshot(
                engine, data, destination, diagnostic, schema, auth_fail_after="records"
            )
        )
    assert engine.events == []
    # An account or settings refusal comes before staging, not after the load.
    recorded_import(monkeypatch, engine, fail="check_ready")
    with pytest.raises(ImportBlocked, match="injected_check_ready_failure"):
        asyncio.run(c11.import_snapshot(engine, data, destination, diagnostic, schema))
    assert engine.events == ["connect", "check_ready", "connect:rollback"]
    assert not destination.exists()


def test_import_snapshot_keeps_the_planned_signature():
    parameters = inspect.signature(c11.import_snapshot).parameters
    assert list(parameters)[:5] == ["engine", "data", "destination", "diagnostic", "definitions"]
    keyword = {
        name: parameter.default
        for name, parameter in parameters.items()
        if parameter.kind is inspect.Parameter.KEYWORD_ONLY
    }
    assert keyword == {
        "manifest": None,
        "fail_after": None,
        "report": None,
        "batch_size": c11.DEFAULT_BATCH_SIZE,
        "auth_fail_after": None,
    }


def server_hash(row) -> int:
    """Deterministic stand-in for hashtextextended(row::text, 0) (signed 64-bit)."""
    return int.from_bytes(hashlib.sha256(repr(row).encode()).digest()[:8], "big", signed=True)


class FakePostgres:
    """In-memory stand-in for the asyncpg calls of the load (COPY, fetch, cursor).

    Rows are stored in COPY column order. ``on_copy`` may alter rows as they are
    stored, like a BEFORE INSERT trigger; ``on_execute`` runs for each plain
    statement (SET CONSTRAINTS), like deferred work. Readback and re-read
    return the selected order (the archive is read key first) plus a row hash
    computed from the stored row at query time, like PostgreSQL.
    """

    def __init__(self, on_copy=None, on_execute=None):
        self.tables, self.calls, self.on_copy = {}, [], on_copy
        self.on_execute = on_execute

    async def execute(self, sql):
        self.calls.append(("execute", sql))
        if self.on_execute:
            self.on_execute(self, sql)

    async def copy_records_to_table(self, table, records, columns, schema_name):
        self.calls.append(("copy", table, len(records)))
        rows = [tuple(record) for record in records]
        if self.on_copy:
            rows = [self.on_copy(table, row) for row in rows]
        self.tables.setdefault(table, []).extend(rows)

    def selected(self, sql):
        table = re.search(r'FROM "([^"]+)"', sql).group(1)
        rows = self.tables.get(table, [])
        if table == c11.ARCHIVE_TABLE:
            return [(r[1], r[2], r[3], r[0], server_hash(r)) for r in rows]
        return [(*r, server_hash(r)) for r in rows]

    async def fetch(self, sql, *arguments):
        self.calls.append(("fetch", sql))
        rows = self.selected(sql)
        if c11.ARCHIVE_TABLE in sql:
            collection, ids = arguments
            return [r for r in rows if r[3] == collection and r[0] in ids]
        return [r for r in rows if r[0] in arguments[0]]

    def cursor(self, sql, prefetch=None):
        self.calls.append(("cursor", sql, prefetch))
        rows = self.selected(sql)

        class Cursor:
            def __aiter__(self):
                self.rows = iter(rows)
                return self

            async def __anext__(self):
                try:
                    return next(self.rows)
                except StopIteration:
                    raise StopAsyncIteration from None

        return Cursor()


def parent_load(
    tmp_path, monkeypatch, fake, *, spec=None, batch_size=2, after_batches=None, rows=PARENT_ROWS
):
    """_load_table, then _final_verification (layer 2), on FakePostgres."""
    tmp_path.mkdir(parents=True, exist_ok=True)
    data, export, schema = tiny_snapshot(tmp_path, monkeypatch, parent_rows=rows)
    plan = c11.RowPlan(PARENT)
    spec = spec or spec_for(schema)
    specs = [spec["parent"][column] for column in plan.columns]
    state = c11._LoadState(fake, batch_size, None)
    check = c11._Verification()
    entry = {"name": "parent", "rows": 0, "copy_batches": 0}

    async def run():
        with snapshot(data, worker_threads=True) as db:
            await c11._load_table(state, db, PARENT, plan, specs, check, entry)
        if after_batches:
            after_batches(fake)
        await c11._final_verification(
            state, [(entry, specs, check)], {"parent": len(rows)}, len(rows)
        )

    asyncio.run(run())
    return state, check, entry


def test_load_pipeline_verifies_batches_and_the_final_reread(tmp_path, monkeypatch):
    fake = FakePostgres()
    state, check, entry = parent_load(tmp_path, monkeypatch, fake)
    assert (entry["rows"], entry["copy_batches"]) == (3, 2)
    assert check.source.hexdigest() == check.target.hexdigest()
    assert check.server_rows == 3 and check.final_hash == check.server_hash
    assert state.archive.target.rows == 3
    assert dict(state.statements) == {"copy": 4, "readback": 4, "final_check": 2, "other": 1}
    # Deferred work is pulled in before the first full re-read.
    kinds = [call[0] for call in fake.calls]
    assert ("execute", "SET CONSTRAINTS ALL IMMEDIATE") in fake.calls
    assert kinds.index("execute") > max(i for i, kind in enumerate(kinds) if kind == "fetch")
    assert kinds.index("execute") < kinds.index("cursor")
    cursors = [call for call in fake.calls if call[0] == "cursor"]
    assert [call[2] for call in cursors] == [c11.FINAL_PREFETCH] * 2
    assert all("ANY" not in call[1] for call in cursors)  # full scans, no key list
    # The archive keeps the source rowids and the collection of each record.
    stored = fake.tables[c11.ARCHIVE_TABLE]
    assert [(r[0], r[1], r[2], r[3]) for r in stored] == [
        (PARENT["id"], record, rowid, "{}") for rowid, record in enumerate(("p1", "p2", "p3"), 1)
    ]


@pytest.mark.parametrize(
    "column,alter",
    [
        ("amount", lambda value: value + 2**-40 if isinstance(value, float) else value),
        ("updated", lambda value: value + timedelta(microseconds=1)),
        ("gallery", lambda value: list(reversed(value))),
        ("data", lambda value: value.replace("2", "3") if isinstance(value, str) else value),
        ("child", lambda value: value + " "),
        ("photo", lambda value: None if value == "" else value),
    ],
)
def test_batch_readback_detects_a_value_changed_while_stored(tmp_path, monkeypatch, column, alter):
    position = c11.RowPlan(PARENT).columns.index(column)

    def trigger(table, row):
        if table != "parent" or row[0] != ("p3" if column == "photo" else "p1"):
            return row
        changed = list(row)
        changed[position] = alter(changed[position])
        return tuple(changed)

    with pytest.raises(ImportBlocked, match="target_readback_mismatch") as raised:
        parent_load(tmp_path, monkeypatch, FakePostgres(trigger))
    assert raised.value.details == {"collection": "parent", "field": column}


TAGS = text_field("tags", "relation", collectionId=CHILD["id"], maxSelect=None)
TAGGED = {**PARENT, "schema": [*PARENT["schema"], TAGS]}
JOINED = "c" * 15 + "," + "d" * 15  # two IDs joined by a comma: one element
TAG_LISTS = [["c1", "c1", JOINED], [], ["absent", "c1"]]


def tagged_snapshot(tmp_path, monkeypatch, lists=TAG_LISTS):
    tmp_path.mkdir(parents=True, exist_ok=True)
    rows = [
        row + (value if isinstance(value, str) else json.dumps(value),)
        for row, value in zip(PARENT_ROWS, lists, strict=True)
    ]
    return tiny_snapshot(tmp_path, monkeypatch, parent_rows=rows, parent=TAGGED)


def tagged_load(tmp_path, monkeypatch, fake):
    """_load_table and _final_verification of TAGGED over a text[] tags column."""
    data, export, schema = tagged_snapshot(tmp_path, monkeypatch)
    plan = c11.RowPlan(TAGGED)
    spec = spec_for(schema)
    spec["parent"]["tags"] = c11.TargetColumn("tags", "text", True, None, True, True)
    specs = [spec["parent"][column] for column in plan.columns]
    state = c11._LoadState(fake, 2, None)
    check = c11._Verification()
    entry = {"name": "parent", "rows": 0, "copy_batches": 0}

    async def run():
        with snapshot(data, worker_threads=True) as db:
            await c11._load_table(state, db, TAGGED, plan, specs, check, entry)
        await c11._final_verification(state, [(entry, specs, check)], {"parent": 3}, 3)

    asyncio.run(run())
    return plan.columns.index("tags")


def test_unbounded_relation_lists_are_loaded_element_for_element(tmp_path, monkeypatch):
    fake = FakePostgres()
    position = tagged_load(tmp_path / "exact", monkeypatch, fake)
    # Order, the repeated "c1", the 31-character element and the empty list.
    assert [row[position] for row in fake.tables["parent"]] == TAG_LISTS
    for name, alter in (
        ("reversed", lambda value: list(reversed(value))),
        ("deduplicated", lambda value: list(dict.fromkeys(value))),
        ("split", lambda value: [part for item in value for part in item.split(",")]),
    ):

        def trigger(table, row, alter=alter):
            if table != "parent" or row[0] != "p1":
                return row
            return (*row[:position], alter(row[position]), *row[position + 1 :])

        with pytest.raises(ImportBlocked, match="target_readback_mismatch") as raised:
            tagged_load(tmp_path / name, monkeypatch, FakePostgres(trigger))
        assert raised.value.details == {"collection": "parent", "field": "tags"}, name


class ArrayDriver:
    """asyncpg stand-in for the recount: {table: [(id, list or None)]}."""

    def __init__(self, rows):
        self.rows, self.sql = rows, []

    def table(self, sql):
        self.sql.append(sql)
        return self.rows[re.search(r'FROM "([^"]+)"', sql).group(1)]

    async def fetchrow(self, sql):
        rows = self.table(sql)
        return len(rows), sum(value is None for _, value in rows)

    async def fetchval(self, sql):
        return len(self.table(sql))

    def cursor(self, sql, prefetch=None):
        assert "unnest" in sql and "WITH ORDINALITY" in sql and prefetch
        items = iter(
            [
                (record, position, value)
                for record, values in self.table(sql)
                for position, value in enumerate(values or [])
            ]
        )

        class Cursor:
            def __aiter__(self):
                return self

            async def __anext__(self):
                try:
                    return next(items)
                except StopIteration:
                    raise StopAsyncIteration from None

        return Cursor()


class ArrayConnection:
    def __init__(self, driver):
        self.driver_connection = driver

    async def get_raw_connection(self):
        return self


def test_relation_arrays_are_recounted_by_json_each_and_unnest(tmp_path, monkeypatch):
    data, export, schema = tagged_snapshot(tmp_path / "source", monkeypatch)
    columns = {"tags": c11.TargetColumn("tags", "text", True, None, True, True)}

    async def introspect(connection, table):
        return columns

    monkeypatch.setattr(c11, "introspect_table", introspect)
    loaded = [("p1", TAG_LISTS[0]), ("p2", []), ("p3", TAG_LISTS[2])]

    def recount(rows):
        connection = ArrayConnection(ArrayDriver({"parent": rows}))
        return asyncio.run(c11.recount_relation_arrays(connection, data, schema))

    assert recount(loaded) == [
        {
            "collection": "parent",
            "field": "tags",
            "target_column": "text[]",
            "sqlite_json_each_equals_postgresql_unnest": True,
            "rows": 3,
            "rows_with_elements": 2,
            "elements": 5,  # repeated elements count
            "longest_element": 31,
        }
    ]
    for name, rows in (
        ("order", [("p1", ["c1", JOINED, "c1"]), *loaded[1:]]),
        ("deduplicated", [("p1", ["c1", JOINED]), *loaded[1:]]),
        ("split", [("p1", ["c1", "c1", *JOINED.split(",")]), *loaded[1:]]),
        ("owner", [("p2", TAG_LISTS[0]), ("p1", []), loaded[2]]),
        ("missing_row", loaded[:2]),
        ("null", [*loaded[:2], ("p3", None)]),
    ):
        with pytest.raises(ImportBlocked, match="relation_array_recount_mismatch") as raised:
            recount(rows)
        assert raised.value.details == {"collection": "parent", "field": "tags"}, name
    # A stored value that SQLite does not read as a list of texts.
    for index, value in enumerate(('["c1",1]', '{"c1":1}', "not json")):
        lists = [TAG_LISTS[0], [], value]
        bad, export, schema = tagged_snapshot(tmp_path / f"bad{index}", monkeypatch, lists)
        connection = ArrayConnection(ArrayDriver({"parent": loaded}))
        with pytest.raises(ImportBlocked, match="relation_array_recount_mismatch"):
            asyncio.run(c11.recount_relation_arrays(connection, bad, schema))
    # A scalar column passes the preflight only for an empty table.
    columns["tags"] = c11.TargetColumn("tags", "character varying", False, 15, True, True)
    with pytest.raises(ImportBlocked, match="unbounded_relation_target_scalar"):
        recount(loaded)
    (tmp_path / "empty").mkdir()
    empty, export, schema = tiny_snapshot(tmp_path / "empty", monkeypatch, [], TAGGED)
    connection = ArrayConnection(ArrayDriver({"parent": []}))
    result = asyncio.run(c11.recount_relation_arrays(connection, empty, schema))
    assert result == [
        {
            "collection": "parent",
            "field": "tags",
            "target_column": "character varying(15)",
            "empty_table_over_scalar_column": True,
            "rows": 0,
            "rows_with_elements": 0,
            "elements": 0,
            "longest_element": 0,
        }
    ]
    assert connection.driver_connection.sql == ['SELECT count(*) FROM "parent"']


def test_batch_readback_detects_missing_extra_and_duplicate_rows():
    names = ("id", "label")
    values = [("a", "x"), ("b", "y")]
    expected = [(v[0], v, c11.row_key(v)) for v in values]

    def run(found):
        state = c11._LoadState(type("D", (), {"fetch": staticmethod(fetch(found))})(), 5, None)
        check = c11._Verification()
        asyncio.run(c11._verify_batch(state, "SQL", [], expected, (), names, check, "t"))
        return check

    def fetch(found):
        async def answer(sql, *arguments):
            return found

        return answer

    check = run([(*values[0], 5), (*values[1], -7)])
    assert (check.server_rows, check.server_hash) == (2, -2)
    for found in (
        [(*values[0], 5)],  # a copied row is absent
        [(*values[0], 5), (*values[1], 1), ("c", "z", 1)],  # an extra row
        [(*values[0], 5), (*values[0], 5)],  # the same key twice, one missing
        [(*values[0], 5), ("c", "y", 1)],  # another key
    ):
        with pytest.raises(ImportBlocked, match="target_readback_mismatch"):
            run(found)
    with pytest.raises(ImportBlocked, match="target_readback_mismatch") as raised:
        run([(*values[0], 5), ("b", "Y", 1)])
    assert raised.value.details == {"collection": "t", "field": "label"}


def test_final_reread_detects_changes_after_the_batch_readback(tmp_path, monkeypatch):
    amount = c11.RowPlan(PARENT).columns.index("amount")

    def update(fake):  # like a later UPDATE of an already verified row
        row = list(fake.tables["parent"][0])
        row[amount] = 99.0
        fake.tables["parent"][0] = tuple(row)

    with pytest.raises(ImportBlocked, match="target_changed_after_readback"):
        parent_load(tmp_path / "update", monkeypatch, FakePostgres(), after_batches=update)

    def insert(fake):  # like a trigger inserting into an already verified table
        fake.tables["parent"].append(("p9", *fake.tables["parent"][0][1:]))

    with pytest.raises(ImportBlocked, match="target_row_count_mismatch"):
        parent_load(tmp_path / "insert", monkeypatch, FakePostgres(), after_batches=insert)

    def archive(fake):
        row = list(fake.tables[c11.ARCHIVE_TABLE][1])
        row[2] = -1  # another source rowid
        fake.tables[c11.ARCHIVE_TABLE][1] = tuple(row)

    with pytest.raises(ImportBlocked, match="target_changed_after_readback") as raised:
        parent_load(tmp_path / "archive", monkeypatch, FakePostgres(), after_batches=archive)
    assert raised.value.details == {"collection": c11.ARCHIVE_TABLE}


def test_final_reread_catches_what_a_broken_batch_comparison_would_miss(tmp_path, monkeypatch):
    """Regression guard: the re-read digest is independent of _verify_batch."""
    amount = c11.RowPlan(PARENT).columns.index("amount")

    def trigger(table, row):
        if table == "parent" and row[0] == "p2":
            row = (*row[:amount], 1.0, *row[amount + 1 :])
        return row

    async def accepts_anything(state, sql, arguments, expected, json_positions, names, check, t):
        found = await state.driver.fetch(sql, *arguments)
        for record in found:  # sums the hashes but compares nothing
            check.server_rows += 1
            check.server_hash += record[len(names)]

    monkeypatch.setattr(c11, "_verify_batch", accepts_anything)
    with pytest.raises(ImportBlocked, match="target_fingerprint_mismatch"):
        parent_load(tmp_path, monkeypatch, FakePostgres(trigger))


def test_check_final_reports_each_layer_with_its_own_code():
    def verification(source=("a",), server=1, target=("a",), hashes=(5, 5)):
        check = c11._Verification()
        for key in source:
            check.source.add(c11.row_digest(key))
        for key in target:
            check.target.add(c11.row_digest(key))
        check.server_rows = server
        check.server_hash, check.final_hash = hashes
        return check

    c11.check_final("t", verification(), 1)
    for check, expected, code in (
        (verification(), 2, "source_row_count_mismatch"),
        (verification(server=0), 1, "target_row_count_mismatch"),
        (verification(target=("a", "b")), 1, "target_row_count_mismatch"),
        (verification(hashes=(5, 6)), 1, "target_changed_after_readback"),
        (verification(target=("b",)), 1, "target_fingerprint_mismatch"),
    ):
        with pytest.raises(ImportBlocked, match=code) as raised:
            c11.check_final("t", check, expected)
        assert raised.value.details == {"collection": "t"}


def test_values_are_checked_before_any_copy(tmp_path, monkeypatch):
    narrow = c11.TargetColumn("child", "character varying", False, 3, True, True)
    fake = FakePostgres()
    spec = spec_for([CHILD, PARENT], {("parent", "child"): narrow})
    with pytest.raises(ImportBlocked, match="value_exceeds_target_length") as raised:
        parent_load(tmp_path, monkeypatch, fake, spec=spec, batch_size=5)
    assert raised.value.details == {"collection": "parent", "field": "child"}
    assert fake.calls == []  # the failing batch was never sent


def test_copy_failures_are_coded_with_their_sqlstate():
    def state_raising(error):
        class Driver:
            async def copy_records_to_table(self, *args, **kwargs):
                raise error

        return c11._LoadState(Driver(), 5, None)

    check = asyncpg.exceptions.CheckViolationError.new(
        {"C": "23514", "M": "violates check constraint", "n": "c11_negative_check"}
    )
    for error, code, sqlstate, constraint in (
        (check, "target_constraint_violation", "23514", "c11_negative_check"),
        (asyncpg.exceptions.RaiseError("raised by a trigger"), "target_copy_failed", "P0001", None),
        (asyncpg.exceptions.DiskFullError("disk full"), "target_copy_failed", "53100", None),
    ):
        with pytest.raises(ImportBlocked, match=code) as raised:
            asyncio.run(c11._copy(state_raising(error), "t", ("id",), [("a",)], "t"))
        assert raised.value.details == {
            "collection": "t",
            "sqlstate": sqlstate,
            "constraint": constraint,
        }
    unicode_error = UnicodeEncodeError("utf-8", "\ud800", 0, 1, "surrogates not allowed")
    with pytest.raises(ImportBlocked, match="invalid_unicode_for_postgresql"):
        asyncio.run(c11._copy(state_raising(unicode_error), "t", ("id",), [("\ud800",)], "t"))


def test_conservation_recount_must_equal_the_diagnosis(tmp_path, monkeypatch):
    data, export, _ = tiny_snapshot(tmp_path, monkeypatch)
    diagnostic = c11.diagnose(data, export, source_version="0.22.27")
    preserved = diagnostic["preserved_source_anomalies"]
    recount = {
        **json.loads(json.dumps(preserved)),
        "relations_checked": diagnostic["relations_checked"],
        "unreferenced_files": diagnostic["unreferenced_files"],
    }
    c11.check_conservation(recount, diagnostic)

    def changed(edit):
        value = json.loads(json.dumps(recount))
        edit(value)
        return value

    for variant in (
        changed(lambda v: v["dangling_relations"]["fields"][0].update(references=1)),
        changed(lambda v: v["dangling_relations"]["fields"][0].update(distinct_targets=2)),
        changed(lambda v: v["dangling_relations"].update(records=1)),
        changed(lambda v: v["missing_files"]["fields"].pop()),
        changed(lambda v: v["missing_files"].update(records=3)),
        changed(lambda v: v.update(relations_checked=2)),
        changed(lambda v: v.update(unreferenced_files=0)),
    ):
        with pytest.raises(ImportBlocked, match="anomaly_conservation_mismatch"):
            c11.check_conservation(variant, diagnostic)
    with pytest.raises(ImportBlocked, match="anomaly_conservation_mismatch"):
        c11.check_conservation(recount, {**diagnostic, "preserved_source_anomalies": None})


def test_cli_writes_new_public_and_private_reports(tmp_path, monkeypatch, capsys):
    data, export, _ = tiny_snapshot(tmp_path, monkeypatch)
    public, private = tmp_path / "public.json", tmp_path / "private.json"
    argv = ["c11_import", "--data", str(data), "--schema", str(export)]
    argv += ["--source-version", "0.22.27", "--report", str(public)]
    monkeypatch.setattr("sys.argv", argv + ["--private-report", str(private)])
    assert c11.main() == 0
    assert json.loads(capsys.readouterr().out)["ready_for_isolated_rehearsal"] is True
    for path in (public, private):
        assert path.stat().st_mode & 0o777 == 0o600
    assert len(json.loads(private.read_text())["dangling_relations"]) == 2
    assert "dangling_relations" not in json.loads(public.read_text())
    monkeypatch.setattr("sys.argv", argv + ["--private-report", str(tmp_path / "new.json")])
    with pytest.raises(SystemExit):
        c11.main()  # the public report already exists: nothing is overwritten
    assert not (tmp_path / "new.json").exists()
    monkeypatch.setattr("sys.argv", ["c11_import", "--extract", "x.zip", "--report", "r.json"])
    with pytest.raises(SystemExit):
        c11.main()  # extraction belongs to scripts.c11_extract only


def test_rowid_archive_and_auth_columns_have_a_fixed_order():
    auth = {"name": "users", "type": "auth", "schema": [text_field("name")]}
    plan = c11.RowPlan(auth)
    assert plan.columns == (
        "id",
        "created",
        "updated",
        "username",
        "email",
        "email_visibility",
        "verified",
        "token_key",
        "password_hash",
        "name",
        "rowid",
    )
    row = {
        "id": "u1",
        "created": TIMESTAMP,
        "updated": TIMESTAMP,
        "username": "u",
        "email": "",
        "emailVisibility": 0,
        "verified": 1,
        "tokenKey": "k" * 50,
        "passwordHash": "$2a$10$" + "h" * 53,
        "lastResetSentAt": "",
        "lastVerificationSentAt": "",
        "lastLoginAlertSentAt": "",
        "name": "n",
        "__c11_rowid": 41,
    }
    values, archive = plan.convert(row)
    assert values[-1] == 41 and values[5:7] == [False, True]
    assert list(archive) == list(c11.AUTH_ARCHIVE_ORDER)
    problems = []
    plan.convert({**row, "verified": 2, "name": 3, "extra": "x"}, problems)
    assert sorted(problems, key=str) == sorted(
        [
            (None, "unmapped_source_columns"),
            ("verified", "invalid_boolean"),
            ("name", "invalid_text"),
        ],
        key=str,
    )
    assert c11.inventory_fingerprint({}) == hashlib.sha256(b"{}").hexdigest()


# ---------------------------------------------------------------------------
# Review round 2: unchained refusals, triggers, deferred work, definitions,
# jsonb scale and notation, PocketBase read rules, recount SQL, termination.
# ---------------------------------------------------------------------------

SECRET = "c11-synthetic-secret-value"


def assert_unchained(exc, *forbidden):
    """No chained error and nothing forbidden in the rendered traceback."""
    assert exc.__cause__ is None and exc.__context__ is None, repr(exc.__context__)
    rendered = "".join(traceback.format_exception(exc))
    for text in forbidden:
        assert text not in rendered, text


def test_no_refusal_is_raised_inside_an_exception_handler():
    """Static guard: raised in an ``except`` block, an ImportBlocked would keep
    the decoder or driver error (and the record values it quotes) as context."""
    tree = ast.parse(inspect.getsource(c11))
    offenders = []

    def visit(node, handler):
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.Lambda)):
            handler = None
        if isinstance(node, ast.ExceptHandler):
            handler = node
        if isinstance(node, ast.Raise) and handler is not None and node.exc is not None:
            offenders.append(node.lineno)
        for child in ast.iter_child_nodes(node):
            visit(child, handler)

    visit(tree, None)
    assert offenders == []


def test_copy_refusals_never_chain_the_driver_error():
    def state_raising(error):
        class Driver:
            async def copy_records_to_table(self, *args, **kwargs):
                raise error

        return c11._LoadState(Driver(), 5, None)

    error = asyncpg.exceptions.CheckViolationError.new(
        {
            "C": "23514",
            "M": "new row violates check constraint",
            "D": f"Failing row contains (u1, {SECRET}).",
            "n": "c11_negative_check",
            "t": "users",
        }
    )
    assert "Failing row" in str(error)  # what a chained traceback would show
    with pytest.raises(ImportBlocked, match="target_constraint_violation") as raised:
        asyncio.run(c11._copy(state_raising(error), "users", ("id",), [("u1",)], "users"))
    assert raised.value.details == {
        "collection": "users",
        "sqlstate": "23514",
        "constraint": "c11_negative_check",
    }
    assert_unchained(raised.value, "Failing row", SECRET)
    unicode_error = UnicodeEncodeError("utf-8", SECRET + "\ud800", 0, 1, "surrogates not allowed")
    with pytest.raises(ImportBlocked, match="invalid_unicode_for_postgresql") as raised:
        asyncio.run(c11._copy(state_raising(unicode_error), "t", ("id",), [("x",)], "t"))
    assert_unchained(raised.value, SECRET)


@pytest.mark.parametrize(
    "call,code",
    [
        (lambda: c11.text_value(SECRET + "\ud800"), "invalid_unicode_for_postgresql"),
        (lambda: c11.json_text('{"k":"' + SECRET + '" broken'), "json_text_served"),
        (lambda: c11.json_text("[" * 200000 + SECRET), "json_nesting_too_deep"),
        (lambda: c11.array_value(f'["{SECRET}"'), "invalid_array"),
        (lambda: c11.date_value("2023-02-29 00:00:00.000Z"), "invalid_date"),
        (lambda: c11.auth_legacy_text({"lastResetSentAt": float("nan")}), "invalid_auth_field"),
        (
            lambda: c11.RowPlan(
                {"name": "t", "type": "base", "schema": [text_field("n", "number")]}
            ).convert({"id": "a", "created": TIMESTAMP, "updated": TIMESTAMP, "n": 2**1100}),
            "unmappable_value",
        ),
        (lambda: c11.target_spec_from_json({"tables": {"t": [{"name": SECRET}]}}), "target_spec"),
    ],
)
def test_conversion_refusals_never_chain_the_error_holding_the_value(call, code):
    with pytest.raises(ImportBlocked, match=code) as raised:
        call()
    assert_unchained(raised.value, SECRET)


def test_manifest_and_batch_refusals_are_unchained(tmp_path, monkeypatch):
    path = tmp_path / "manifest.json"
    path.write_text('{"files": [{"path": "storage/c/' + SECRET + '/f"')  # truncated
    with pytest.raises(ImportBlocked, match="manifest_invalid") as raised:
        c11.read_manifest(path, tmp_path / "data.db")
    assert_unchained(raised.value, SECRET)
    narrow = c11.TargetColumn("child", "character varying", False, 3, True, True)
    spec = spec_for([CHILD, PARENT], {("parent", "child"): narrow})
    with pytest.raises(ImportBlocked, match="value_exceeds_target_length") as raised:
        parent_load(tmp_path / "length", monkeypatch, FakePostgres(), spec=spec)
    assert_unchained(raised.value)
    rows = PARENT_ROWS + [("p4", "c1", "", SECRET, "{}", 1)]  # gallery is no JSON list
    with pytest.raises(ImportBlocked, match="invalid_array") as raised:
        parent_load(tmp_path / "array", monkeypatch, FakePostgres(), rows=rows)
    assert raised.value.details == {"collection": "parent", "field": "gallery"}
    assert_unchained(raised.value, SECRET)


def test_deferred_work_runs_before_the_final_reread(tmp_path, monkeypatch):
    """A deferred constraint trigger would change rows at COMMIT, after every
    check; SET CONSTRAINTS ALL IMMEDIATE runs it before the full re-read."""
    amount = c11.RowPlan(PARENT).columns.index("amount")

    def deferred(fake, sql):
        assert sql == "SET CONSTRAINTS ALL IMMEDIATE"
        row = list(fake.tables["parent"][1])
        row[amount] = 7.5
        fake.tables["parent"][1] = tuple(row)

    fake = FakePostgres(on_execute=deferred)
    with pytest.raises(ImportBlocked, match="target_changed_after_readback") as raised:
        parent_load(tmp_path, monkeypatch, fake)
    assert raised.value.details == {"collection": "parent"}
    kinds = [call[0] for call in fake.calls]
    assert kinds.index("execute") < kinds.index("cursor")


def test_deferred_constraint_failures_are_coded_without_values():
    def state_raising(error):
        class Driver:
            async def execute(self, sql):
                raise error

        return c11._LoadState(Driver(), 5, None)

    unique = asyncpg.exceptions.UniqueViolationError.new(
        {
            "C": "23505",
            "M": "duplicate key value violates unique constraint",
            "D": f"Key (status)=({SECRET}) already exists.",
            "n": "c11_deferred_unique",
            "t": "buma_supplier",
        }
    )
    with pytest.raises(ImportBlocked, match="target_constraint_violation") as raised:
        asyncio.run(c11._immediate_constraints(state_raising(unique)))
    assert raised.value.details == {
        "collection": "buma_supplier",
        "sqlstate": "23505",
        "constraint": "c11_deferred_unique",
    }
    assert_unchained(raised.value, SECRET, "already exists")
    raised_by_trigger = asyncpg.exceptions.RaiseError.new({"C": "P0001", "M": SECRET})
    with pytest.raises(ImportBlocked, match="target_deferred_check_failed") as raised:
        asyncio.run(c11._immediate_constraints(state_raising(raised_by_trigger)))
    assert_unchained(raised.value, SECRET)


def trigger_row(table="users", name="buma_sqlite_rowid", **changes):
    return {"table_name": table, "trigger_name": name, **c11.ROWID_TRIGGER_DEFINITION, **changes}


def test_only_the_migrated_rowid_triggers_are_accepted():
    tables = {"users", "buma_article", "buma_supplier"}
    expected = [trigger_row("buma_article"), trigger_row("users")]
    assert c11.check_target_triggers(expected, 0, tables) == [
        "buma_article.buma_sqlite_rowid",
        "users.buma_sqlite_rowid",
    ]
    users = {"kind": "table", "collection": "users", "trigger": "buma_sqlite_rowid"}
    deferred = trigger_row(
        "buma_supplier",
        "c11_deferred",
        type=5,  # AFTER INSERT FOR EACH ROW
        is_constraint=True,
        deferrable=True,
        initially_deferred=True,
        function="public.c11_rewrite",
    )
    for found, details in (
        (
            expected + [deferred],
            {"kind": "table", "collection": "buma_supplier", "trigger": "c11_deferred"},
        ),
        (expected[:1] + [trigger_row(deferrable=True, initially_deferred=True)], users),
        (expected[:1] + [trigger_row(is_constraint=True)], users),
        (expected[:1] + [trigger_row(type=5)], users),  # AFTER instead of BEFORE
        (expected[:1] + [trigger_row(type=7 | 16)], users),  # also on UPDATE
        (expected[:1] + [trigger_row(enabled="D")], users),
        (expected[:1] + [trigger_row(enabled="A")], users),
        (expected[:1] + [trigger_row(function="public.c11_rewrite")], users),
        (expected[:1] + [trigger_row(function_arguments=1)], users),
        (expected[:1] + [trigger_row(arguments=1)], users),
        (expected[:1] + [trigger_row(has_when=True)], users),
        (expected[:1] + [trigger_row(has_columns=True)], users),
        (
            expected + [trigger_row(name="extra")],
            {"kind": "table", "collection": "users", "trigger": "extra"},
        ),
    ):
        with pytest.raises(ImportBlocked, match="unexpected_target_trigger") as raised:
            c11.check_target_triggers(found, 0, tables)
        assert raised.value.details == details
    with pytest.raises(ImportBlocked, match="unexpected_target_trigger") as raised:
        c11.check_target_triggers(expected, 1, tables)
    assert raised.value.details == {"kind": "event", "triggers": 1}
    with pytest.raises(ImportBlocked, match="target_trigger_missing") as raised:
        c11.check_target_triggers(expected[:1], 0, tables)
    assert raised.value.details == {"collection": "users", "trigger": "buma_sqlite_rowid"}
    # Small test schemas without the rowid tables expect no allocator at all.
    assert c11.check_target_triggers([], 0, {"child", "parent"}) == []


def test_load_refuses_definitions_other_than_the_diagnosed_ones(tmp_path, monkeypatch):
    data, export, schema = tiny_snapshot(tmp_path, monkeypatch)
    report = c11.diagnose(data, export, source_version="0.22.27")
    assert report["definitions_sha256"] == c11.definitions_fingerprint(schema)
    staged = tmp_path / "staged"
    stage_snapshot(data, staged, report)
    changed = json.loads(json.dumps(schema))
    changed[1]["schema"][0]["options"]["maxSelect"] = 2  # same names, other field
    for definitions in (schema[1:], schema[:1], list(reversed(schema)), changed):
        connection = FakeConnection()
        with pytest.raises(ImportBlocked, match="definitions_do_not_match_diagnosis"):
            asyncio.run(c11.load_isolated(connection, staged, definitions))
        assert connection.statements == []  # refused before any SQL
    assert c11.diagnosed_collections_match(schema, report)
    assert not c11.diagnosed_collections_match(schema, {**report, "definitions_sha256": None})


def test_final_total_must_equal_the_diagnosed_total(tmp_path, monkeypatch):
    data, export, schema = tiny_snapshot(tmp_path, monkeypatch)
    plan = c11.RowPlan(PARENT)
    specs = [spec_for(schema)["parent"][column] for column in plan.columns]
    state = c11._LoadState(FakePostgres(), 2, None)
    check = c11._Verification()
    entry = {"name": "parent", "rows": 0, "copy_batches": 0}

    async def run(total):
        with snapshot(data, worker_threads=True) as db:
            await c11._load_table(state, db, PARENT, plan, specs, check, entry)
        await c11._final_verification(state, [(entry, specs, check)], {"parent": 3}, total)

    with pytest.raises(ImportBlocked, match="source_row_count_mismatch") as raised:
        asyncio.run(run(4))  # every table agrees, the diagnosed total does not
    assert raised.value.details == {"scope": "all_tables"}


def test_decimal_keys_keep_the_scale_jsonb_stores(tmp_path, monkeypatch):
    key = c11.canonical_json
    assert key("1e2") == key("100") and key("1E+2") == key("100")
    assert key("1.5E-1") == key("0.15") and key("1.50E1") == key("15.0")
    assert key("1.0") != key("1") and key("2.50") != key("2.5") and key("0.0") != key("0")
    assert key('{"v":1.0,"w":[2.50]}') == key('{"w": [2.50], "v": 1.0}')
    assert c11.decimal_key(Decimal("1.0")) == "1e0/1"
    assert c11.decimal_key(Decimal("0.00")) == "0/2"
    data_at = c11.RowPlan(PARENT).columns.index("data")
    rows = [(*PARENT_ROWS[0][:4], '{"v":1.0,"w":[2.50]}', 1), *PARENT_ROWS[1:]]

    def rewriting(stored):
        def trigger(table, row):
            if table == "parent" and row[0] == "p1":
                row = (*row[:data_at], stored, *row[data_at + 1 :])
            return row

        return trigger

    for stored in ('{"v": 1, "w": [2.50]}', '{"v": 1.0, "w": [2.5]}'):
        with pytest.raises(ImportBlocked, match="target_readback_mismatch") as raised:
            parent_load(
                tmp_path / stored[6:9], monkeypatch, FakePostgres(rewriting(stored)), rows=rows
            )
        assert raised.value.details == {"collection": "parent", "field": "data"}
    # jsonb's own rendering (sorted keys, spacing) is the same stored value.
    parent_load(
        tmp_path / "same",
        monkeypatch,
        FakePostgres(rewriting('{"v": 1.0, "w": [2.50]}')),
        rows=rows,
    )


@pytest.mark.parametrize(
    "value,text",
    [
        (1234567.5, "1.2345675e+06"),
        (-1234567.0, "-1.234567e+06"),
        (1e6, "1e+06"),
        (999999.5, "999999.5"),
        (123456.0, "123456"),
        (12345.5, "12345.5"),
        (100.25, "100.25"),
        (1.5, "1.5"),
        (0.1, "0.1"),
        (0.0001, "0.0001"),
        (1e-05, "1e-05"),
        (-2.5e-07, "-2.5e-07"),
        (1e20, "1e+20"),
        (1e100, "1e+100"),
        (5e-324, "5e-324"),
        (1.7976931348623157e308, "1.7976931348623157e+308"),
        (0.0, "0"),
    ],
)
def test_real_json_values_get_the_text_go_gives_pocketbase(value, text):
    """strconv.FormatFloat(v, 'g', -1, 64), what database/sql hands PB 0.22."""
    assert c11.go_float_text(value) == text
    assert c11.json_text(value) == text


def test_pocketbase_read_rules_of_list_fields():
    single = c11.pocketbase_single_value
    assert single('["a","c"]') == "c"
    assert single('["a","c","a"]') == "c"
    assert single('["a",""]') == "a"
    assert single("[]") == ""
    assert single('[null,"b"]') == "b"
    assert single(' ["x"] ') == "x"  # Go's decoder accepts surrounding whitespace
    for kept in ('["a",1]', "plain", "a[b", '{"a":"["}', '"[x]"', "", "[NaN]"):
        assert single(kept) == kept, kept
    assert c11.nonzero_uniques(["b", "", "a", "b"]) == ["b", "a"]
    assert c11.pocketbase_list_changed(["a", "a"]) and c11.pocketbase_list_changed(["", "b"])
    assert not c11.pocketbase_list_changed(["b", "a"]) and not c11.pocketbase_list_changed([])


SELECTS = {
    **PARENT,
    "schema": [
        *PARENT["schema"],
        text_field("kind", "select", maxSelect=1, values=["a", "c"]),
        text_field("tags", "select", maxSelect=3, values=["a", "b"]),
    ],
}


def test_number_notation_and_pocketbase_read_rules_are_warnings(tmp_path, monkeypatch):
    rows = [
        # id, child, photo, gallery, data, amount, kind, tags
        ("p1", "c1", "a.bin", '["b.bin"]', '{"b":1,"a":2}', 1, "a", '["a"]'),
        ("p2", "c1", "", "[]", None, 2, '["a","c"]', '["b","b",""]'),
        ("p3", "c1", '["x.bin","y.bin"]', "[]", '{"x":1e2}', 3, "", "[]"),
        ("p4", "c1", "", "[]", 1234567.5, 4, "c", '["a","b"]'),
    ]
    data, export, _ = tiny_snapshot(tmp_path, monkeypatch, parent_rows=rows, parent=SELECTS)
    report = c11.diagnose(data, export, source_version="0.22.27")
    assert report["blockers"] == [] and report["ready_for_isolated_rehearsal"]
    warnings = {(w["code"], w["field"]): w["affected_rows"] for w in report["warnings"]}
    assert warnings == {
        ("jsonb_key_order_normalized", "data"): 1,
        # '{"x":1e2}' and the REAL 1234567.5, which PB serves as 1.2345675e+06
        ("jsonb_number_notation_normalized", "data"): 2,
        ("sql_null_json_stored_as_jsonb_null", "data"): 1,
        ("single_value_served_as_last_element", "photo"): 1,
        ("single_value_served_as_last_element", "kind"): 1,
        ("list_served_deduplicated_by_pocketbase", "tags"): 1,
    }
    # The stored values are kept: the single file is one (absent) reference.
    missing = report["preserved_source_anomalies"]["missing_files"]["fields"]
    assert {(f["field"], f["references"]) for f in missing} == {("photo", 1)}


def test_unbounded_relation_over_an_array_target_names_the_importer_flag(tmp_path, monkeypatch):
    unbounded = text_field("tags", "relation", collectionId=CHILD["id"], maxSelect=None)
    parent = {**PARENT, "schema": [*PARENT["schema"], unbounded]}
    rows = [row + ('["c1"]',) for row in PARENT_ROWS]
    data, export, schema = tiny_snapshot(tmp_path, monkeypatch, parent_rows=rows, parent=parent)
    spec = spec_for(schema)
    spec["parent"]["tags"] = c11.TargetColumn("tags", "text", True, None, True, True)
    report = c11.diagnose(data, export, source_version="0.22.27", target_columns=spec)
    assert report["blockers"] == [] and report["ready_for_isolated_rehearsal"]
    # A deliberate rollback of UNBOUNDED_RELATION_ARRAYS keeps its own cause.
    monkeypatch.setattr(c11, "UNBOUNDED_RELATION_ARRAYS", False)
    report = c11.diagnose(data, export, source_version="0.22.27", target_columns=spec)
    assert report["blockers"] == [
        {
            "code": "unbounded_relation_arrays_not_enabled",
            "collection": "parent",
            "field": "tags",
            "expected": "character varying[]|text[]",
            "actual": "text[]",
            "affected_rows": 3,
        }
    ]
    assert not [w for w in report["warnings"] if isinstance(w, dict) and "tags" == w.get("field")]
    # Without a target description the documented D-0019 cause stays.
    report = c11.diagnose(data, export, source_version="0.22.27")
    assert [b["code"] for b in report["blockers"]] == ["unbounded_relation_target_scalar"]


class SqliteResult:
    def __init__(self, rows):
        self.rows = rows

    def one(self):
        assert len(self.rows) == 1
        return self.rows[0]

    def all(self):
        return self.rows


class SqliteConnection:
    """Runs the recount SQL of target_anomalies on SQLite, arrays as JSON text.

    Only PostgreSQL's array syntax is mapped (unnest(X) e(ref) -> json_each(X)
    e, e.ref -> e.value, CROSS JOIN LATERAL -> CROSS JOIN); every predicate,
    join and aggregate runs as written.
    """

    def __init__(self, db):
        self.db, self.statements = db, []

    async def execute(self, statement, parameters=None):
        sql = str(statement)
        self.statements.append(sql)
        sql = sql.replace("CROSS JOIN LATERAL", "CROSS JOIN")
        sql = re.sub(r"unnest\(([^()]*)\) e\(ref\)", r"json_each(\1) e", sql)
        return SqliteResult(self.db.execute(sql.replace("e.ref", "e.value")).fetchall())


def test_target_recount_counts_each_anomaly_kind_exactly():
    column = c11.TargetColumn
    links = text_field("links", "relation", collectionId=CHILD["id"], maxSelect=5)
    parent = {**PARENT, "schema": [*PARENT["schema"], links]}
    empty = {**CHILD, "id": "emptycol0000001", "name": "empty"}
    definitions = [CHILD, parent, empty]
    spec = spec_for([CHILD, PARENT, empty])
    spec["parent"]["links"] = column("links", "character varying", True, 15, True, True)
    db = sqlite3.connect(":memory:")
    db.executescript(
        "CREATE TABLE child (id TEXT); INSERT INTO child VALUES ('c1');"
        "CREATE TABLE empty (id TEXT);"
        "CREATE TABLE parent (id TEXT, child TEXT, photo TEXT, gallery TEXT, data TEXT, "
        "amount REAL, links TEXT);"
    )
    db.executemany(
        "INSERT INTO parent VALUES (?,?,?,?,'null',0,?)",
        [
            ("p1", "c1", "a.bin", '["b.bin","missing.bin"]', '["c1","x"]'),
            ("p2", "absent", "gone.bin", "[]", "[]"),
            ("p3", "absent", "", '["c.bin"]', '["y","y"]'),
            ("p4", "", "", "[]", '["c1"]'),
        ],
    )
    collection = PARENT["id"]
    inventory = {
        f"{collection}/p1/a.bin": {},
        f"{collection}/p1/b.bin": {},
        f"{collection}/p9/orphan.bin": {},
    }
    connection = SqliteConnection(db)
    recount = asyncio.run(
        c11.target_anomalies(
            connection, definitions, spec, inventory, {"child": 1, "parent": 4, "empty": 0}
        )
    )
    relations = {(f["field"]): f for f in recount["dangling_relations"]["fields"]}
    assert relations == {
        "child": {
            "collection": "parent",
            "field": "child",
            "target": "child",
            "references": 2,
            "records": 2,
            "distinct_targets": 1,
        },
        "links": {
            "collection": "parent",
            "field": "links",
            "target": "child",
            "references": 3,
            "records": 2,
            "distinct_targets": 2,
        },
    }
    # p1 (links), p2 (child) and p3 (child and links): each record once.
    assert recount["dangling_relations"]["references"] == 5
    assert recount["dangling_relations"]["records"] == 3
    assert recount["relations_checked"] == 3 + 5
    files = {f["field"]: f for f in recount["missing_files"]["fields"]}
    assert files == {
        "photo": {
            "collection": "parent",
            "field": "photo",
            "references": 1,
            "records": 1,
            "distinct_files": 1,
        },
        "gallery": {
            "collection": "parent",
            "field": "gallery",
            "references": 2,
            "records": 2,
            "distinct_files": 2,
        },
    }
    assert recount["missing_files"]["records"] == 3  # p1, p2 and p3
    assert recount["missing_files"]["distinct_files"] == 3
    assert recount["unreferenced_files"] == 1
    # Parent only (child has no relation or file field, empty is skipped): the
    # scalar relations with the any-orphan count, the array relation, 2 file fields.
    assert recount["statements"] == len(connection.statements) == 4
    assert not [sql for sql in connection.statements if '"empty"' in sql]


def test_registry_refuses_evidence_that_did_not_pass(tmp_path, monkeypatch):
    for status in ("failed", None):
        evidence = tmp_path / f"evidence-{status}.json"
        evidence.write_text(
            json.dumps({"status": status, "diagnostic": {"source_version": "0.22.27"}})
        )
        registry = {
            "0.22.27": {"evidence": str(evidence), "version_path": ["diagnostic", "source_version"]}
        }
        monkeypatch.setattr(c11, "VALIDATED_SOURCE_VERSIONS", registry)
        assert c11.source_version_status("0.22.27") == (
            "source_version_evidence_invalid",
            str(evidence),
        )
    evidence = tmp_path / "evidence-other.json"
    evidence.write_text(
        json.dumps({"status": "passed", "diagnostic": {"source_version": "0.22.26"}})
    )
    registry = {
        "0.22.27": {"evidence": str(evidence), "version_path": ["diagnostic", "source_version"]}
    }
    monkeypatch.setattr(c11, "VALIDATED_SOURCE_VERSIONS", registry)
    assert c11.source_version_status("0.22.27")[0] == "source_version_evidence_invalid"


TERMINATION_SCRIPT = r"""
import asyncio, hashlib, json, sqlite3, sys
from pathlib import Path
from types import SimpleNamespace
import scripts.c11_auth_import as auth
import scripts.c11_import as c11

root = Path(sys.argv[1])
data = root / "source"
(data / "storage").mkdir(parents=True)
with sqlite3.connect(data / "data.db") as db:
    db.execute("CREATE TABLE sample(id TEXT)")
report = {
    "ready_for_isolated_rehearsal": True,
    "data_db_sha256": c11.digest(data / "data.db"),
    "file_manifest_sha256": c11.inventory_fingerprint({}),
}


class Engine:
    sync_engine = SimpleNamespace(hide_parameters=True)

    def connect(self):
        class Probe:
            async def __aenter__(self):
                return None

            async def __aexit__(self, *exc):
                return False

        return Probe()

    def begin(self):
        class Context:
            async def __aenter__(self):
                print("loading", flush=True)
                await asyncio.sleep(60)  # stands for a long load

            async def __aexit__(self, *exc):
                return False

        return Context()


async def ready(*args):
    pass  # the accounts and settings preconditions (check_ready) hold


auth.check_ready = ready


async def main():
    async with c11.cancelled_by_termination():
        await c11.import_snapshot(Engine(), data, root / "staged", report, [])


asyncio.run(main())
"""


def test_termination_signals_still_remove_the_staging(tmp_path):
    root = Path(c11.__file__).resolve().parents[1]
    script = tmp_path / "terminate.py"
    script.write_text(TERMINATION_SCRIPT)
    for number in (signal.SIGTERM, signal.SIGHUP):
        work = tmp_path / f"run-{number}"
        work.mkdir()
        process = subprocess.Popen(
            [sys.executable, str(script), str(work)],
            cwd=root,
            env={
                **os.environ,
                # This worktree first, then the caller's path (the PPBase in use).
                "PYTHONPATH": os.pathsep.join(
                    filter(None, [str(root), os.environ.get("PYTHONPATH")])
                ),
                "PYTHONDONTWRITEBYTECODE": "1",
            },
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
        )
        try:
            assert process.stdout.readline().strip() == "loading"
            assert (work / "staged" / "data.db").is_file()  # the private copy exists
            process.send_signal(number)
            _, errors = process.communicate(timeout=30)
        finally:
            if process.poll() is None:
                process.kill()
                process.wait()
        assert process.returncode != 0 and "CancelledError" in errors
        assert not (work / "staged").exists()  # cleaned up despite the signal
