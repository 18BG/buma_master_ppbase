"""Fail-closed boundaries for the offline import preparation."""

import asyncio
import hashlib
import json
import re
import sqlite3
import zipfile
from datetime import UTC, datetime, timedelta
from decimal import Decimal

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


def test_multi_relations_are_never_flattened_to_target_scalar():
    for maximum in (None, 0):
        for value in ("[]", '["first","second"]'):
            with pytest.raises(ImportBlocked, match="unbounded_relation_target_scalar"):
                field_value({"type": "relation", "options": {"maxSelect": maximum}}, value)
    assert field_value({"type": "relation", "options": {"maxSelect": 3}}, '["a","b"]') == ["a", "b"]


@pytest.mark.parametrize("value", [2**53 + 1, float("nan"), float("inf"), "12"])
def test_lossy_numeric_conversions_are_blocked(value):
    with pytest.raises(ImportBlocked):
        field_value({"type": "number"}, value)


def test_integer_range_and_non_finite_json_are_checked():
    with pytest.raises(ImportBlocked, match="integer_outside_target_range"):
        field_value({"type": "number", "options": {"onlyInt": True}}, 2**31)
    with pytest.raises(ImportBlocked, match="non_finite_json"):
        field_value({"type": "json"}, '{"value":NaN}')
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


def test_unbounded_relations_stay_blocked_but_their_orphans_are_counted(tmp_path, monkeypatch):
    unbounded = text_field("tags", "relation", collectionId=CHILD["id"], maxSelect=None)
    parent = {**PARENT, "schema": [*PARENT["schema"], unbounded]}
    rows = [row + ('["c1","joined,ids"]',) for row in PARENT_ROWS]
    data, export, _ = tiny_snapshot(tmp_path, monkeypatch, parent_rows=rows, parent=parent)
    report = c11.diagnose(data, export, source_version="0.22.27")
    assert {"code": "unbounded_relation_target_scalar", "collection": "parent"} in [
        {k: b.get(k) for k in ("code", "collection")} for b in report["blockers"]
    ]
    tags = next(
        f
        for f in report["preserved_source_anomalies"]["dangling_relations"]["fields"]
        if f["field"] == "tags"
    )
    # One stored element is one reference: never split on the comma.
    assert (tags["references"], tags["records"], tags["distinct_targets"]) == (3, 3, 1)


def test_unbounded_relation_is_reported_once_per_field(tmp_path, monkeypatch):
    unbounded = text_field("tags", "relation", collectionId=CHILD["id"], maxSelect=None)
    parent = {**PARENT, "schema": [*PARENT["schema"], unbounded]}
    rows = [row + ('["c1"]',) for row in PARENT_ROWS]
    data, export, schema = tiny_snapshot(tmp_path, monkeypatch, parent_rows=rows, parent=parent)
    spec = spec_for(schema)
    spec["parent"]["tags"] = c11.TargetColumn("tags", "character varying", False, 15, True, True)
    for target in (None, spec):
        report = c11.diagnose(data, export, source_version="0.22.27", target_columns=target)
        entries = [
            entry
            for entry in report["blockers"] + report["warnings"]
            if isinstance(entry, dict) and entry["code"] == "unbounded_relation_target_scalar"
        ]
        expected = {
            "code": "unbounded_relation_target_scalar",
            "collection": "parent",
            "field": "tags",
            "affected_rows": 3,
        }
        if target is not None:  # the preflight details join the same blocker
            expected["expected"] = "character varying[]|text[]"
            expected["actual"] = "character varying(15)"
        assert entries == [expected]
    # Empty table: the field is still reported, once, as a warning.
    (tmp_path / "empty").mkdir()
    data, export, schema = tiny_snapshot(tmp_path / "empty", monkeypatch, [], parent)
    report = c11.diagnose(data, export, source_version="0.22.27", target_columns=spec)
    assert report["blockers"] == []
    assert [w["code"] for w in report["warnings"]] == ["unbounded_relation_target_scalar"]
    assert report["warnings"][0]["actual"] == "character varying(15)"


def test_unbounded_relation_arrays_are_copied_exactly_once_enabled(monkeypatch):
    field = {"type": "relation", "options": {"maxSelect": None}}
    monkeypatch.setattr(c11, "UNBOUNDED_RELATION_ARRAYS", True)
    assert field_value(field, '["b","a","b"]') == ["b", "a", "b"]  # order, no dedup
    column = c11.TargetColumn("managers", "character varying", True, 15, True, True)
    element = "x" * 15 + "," + "y" * 15  # the 31-character legacy element
    with pytest.raises(ImportBlocked, match="value_exceeds_target_length"):
        c11.check_value(column, field_value(field, json.dumps([element])))
    c11.check_value(c11.TargetColumn("managers", "text", True, None, True, True), [element])


def test_source_version_registry_requires_passing_evidence(monkeypatch):
    assert c11.source_version_status(None) == ("source_pocketbase_version_unknown", None)
    assert c11.source_version_status("0.22.34") == ("source_pocketbase_version_unvalidated", None)
    assert c11.source_version_status("0.22.27") == (None, "docs/c11-rehearsal-report.json")
    for version, entry in c11.VALIDATED_SOURCE_VERSIONS.items():
        evidence = json.loads((c11.PROJECT / entry["evidence"]).read_text())
        assert evidence["status"] == "passed", version
    broken = {"0.22.27": {"evidence": "docs/missing.json", "version_path": ["x"]}}
    monkeypatch.setattr(c11, "VALIDATED_SOURCE_VERSIONS", broken)
    assert c11.source_version_status("0.22.27")[0] == "source_version_evidence_invalid"
    wrong = {"0.22.27": {"evidence": "docs/c11-rehearsal-report.json", "version_path": ["scope"]}}
    monkeypatch.setattr(c11, "VALIDATED_SOURCE_VERSIONS", wrong)
    assert c11.source_version_status("0.22.27")[0] == "source_version_evidence_invalid"


def test_unvalidated_version_blocks_the_diagnosis(tmp_path, monkeypatch):
    data, export, _ = tiny_snapshot(tmp_path, monkeypatch)
    report = c11.diagnose(data, export, source_version="0.22.34")
    assert report["blockers"] == [{"code": "source_pocketbase_version_unvalidated"}]
    assert report["validated_source_versions"] == ["0.22.27"]
    assert report["source_version_validated"] is False


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
        ('{"v":NaN}', "non_finite_json"),
        ('{"v":1,"v":1}', "jsonb_duplicate_keys_collapsed"),
        ("not json", "invalid_json"),
        ("", "invalid_json"),
        pytest.param("[" * 257 + "]" * 257, "json_nesting_too_deep", id="deep"),
        pytest.param("[" * 200000 + "]" * 200000, "invalid_json", id="parser_recursion"),
    ],
)
def test_json_must_be_exactly_representable_in_jsonb(text, code):
    with pytest.raises(ImportBlocked, match=code):
        c11.json_text(text)


def test_json_source_text_is_kept_verbatim_and_key_order_changes_are_detected():
    exact = '{"b": 0.12345678901234567890123, "a": 1e400, "n": [1.50, true]}'
    assert c11.json_text(exact) == exact  # no float round-trip, no re-serialization
    assert (c11.json_text(None), c11.json_text(5), c11.json_text(1.5)) == ("null", "5", "1.5")
    with pytest.raises(ImportBlocked, match="json_number_not_representable"):
        c11.json_text(-0.0)
    # jsonb orders keys by UTF-8 length, then bytes, at every depth.
    assert c11.jsonb_key_order_changes('{"aa":1,"b":2}')
    assert c11.jsonb_key_order_changes('[{"ok":1},{"x":{"zz":1,"y":2}}]')
    assert c11.jsonb_key_order_changes('{"é":1,"z":2}')  # é is two bytes
    assert not c11.jsonb_key_order_changes('{"b":1,"aa":[{"a":1}]}')
    assert not c11.jsonb_key_order_changes('{"z":1,"é":2}')
    assert not c11.jsonb_key_order_changes("null")
    assert not c11.jsonb_key_order_changes("[3,[]]")


def test_row_keys_compare_exact_typed_values():
    # jsonb renders 1e2 as 100, keeps 1.50, sorts keys: the same JSON value.
    assert c11.canonical_json('{"b":[1.50,"x"],"a":1e2}') == c11.canonical_json(
        '{"a": 100, "b": [1.5, "x"]}'
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


def test_preflight_unbounded_relation_is_a_warning_only_when_the_table_is_empty():
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
    spec["parent"]["tags"] = c11.TargetColumn("tags", "text", True, None, True, True)
    assert c11.preflight([CHILD, parent], spec, {"parent": 3})["blockers"] == []


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


def entry(path, size=0, sha="0" * 64, crc="0" * 8):
    return {"path": path, "bytes": size, "sha256": sha, "crc32": crc}


@pytest.mark.parametrize(
    "change,code",
    [
        (lambda m: m.update(extra=1), "manifest_invalid"),
        (lambda m: m.update(archive_sha256="ABC"), "manifest_invalid"),
        (lambda m: m["files"].append({"path": "x"}), "manifest_invalid"),
        (lambda m: m["files"].append(entry("x", size=-1)), "manifest_invalid"),
        (lambda m: m["files"].append(entry("x", crc="123")), "manifest_invalid"),
        (lambda m: m["files"].append(entry("storage/../x")), "manifest_unsafe_entry"),
        (lambda m: m["files"].append(entry("/abs")), "manifest_unsafe_entry"),
        (lambda m: m["files"].append(entry("storage\\x")), "manifest_unsafe_entry"),
        (lambda m: m["files"].append(entry("storage//x")), "manifest_unsafe_entry"),
        (lambda m: m["files"].append(entry("./storage/x")), "manifest_unsafe_entry"),
        (lambda m: m["files"].append(dict(m["files"][0])), "manifest_duplicate_entry"),
        (lambda m: m["files"].append(entry("pb_data/x")), "unexpected_manifest_entry"),
        (
            lambda m: m["files"].append(entry("data.db-wal", size=1)),
            "live_or_incomplete_sqlite_snapshot",
        ),
        (
            lambda m: m["files"].append(entry("data.db-journal", size=8)),
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
        tmp_path, data, lambda m: m["files"].append(entry("data.db-wal", crc="00000000"))
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


def test_import_snapshot_removes_its_staging_after_any_failure(tmp_path, monkeypatch):
    data, export, schema = tiny_snapshot(tmp_path, monkeypatch)
    report = c11.diagnose(data, export, source_version="0.22.27")

    class Engine:
        def begin(self):
            engine = self

            class Context:
                async def __aenter__(self):
                    engine.entered = True
                    return FakeConnection("other")

                async def __aexit__(self, *exc):
                    engine.exited = exc[0]
                    return False

            return Context()

    engine = Engine()
    destination = tmp_path / "attempt"
    with pytest.raises(ImportBlocked, match="isolated_rehearsal_database_required"):
        asyncio.run(c11.import_snapshot(engine, data, destination, report, schema))
    assert engine.entered and engine.exited is ImportBlocked
    assert not destination.exists()
    # The same new path can be used again by a clean retry.
    assert stage_snapshot(data, destination, report) == storage_inventory(data / "storage")


def server_hash(row) -> int:
    """Deterministic stand-in for hashtextextended(row::text, 0) (signed 64-bit)."""
    return int.from_bytes(hashlib.sha256(repr(row).encode()).digest()[:8], "big", signed=True)


class FakePostgres:
    """In-memory stand-in for the asyncpg calls of the load (COPY, fetch, cursor).

    Rows are stored in COPY column order. ``on_copy`` may alter rows as they are
    stored, like a BEFORE INSERT trigger. Readback and re-read return the
    selected order (the archive is read key first) plus a row hash computed from
    the stored row at query time, like PostgreSQL.
    """

    def __init__(self, on_copy=None):
        self.tables, self.calls, self.on_copy = {}, [], on_copy

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


def parent_load(tmp_path, monkeypatch, fake, *, spec=None, batch_size=2, after_batches=None):
    """_load_table, then the end-of-load re-read and verdicts, on FakePostgres."""
    tmp_path.mkdir(parents=True, exist_ok=True)
    data, export, schema = tiny_snapshot(tmp_path, monkeypatch)
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
        await c11._reread(state, "parent", specs, c11.json_columns(specs), check)
        c11.check_final("parent", check, 3)
        await c11._reread(
            state, c11.ARCHIVE_TABLE, c11.ARCHIVE_READ, c11.ARCHIVE_JSON, state.archive
        )
        c11.check_final(c11.ARCHIVE_TABLE, state.archive, 3)

    asyncio.run(run())
    return state, check, entry


def test_load_pipeline_verifies_batches_and_the_final_reread(tmp_path, monkeypatch):
    fake = FakePostgres()
    state, check, entry = parent_load(tmp_path, monkeypatch, fake)
    assert (entry["rows"], entry["copy_batches"]) == (3, 2)
    assert check.source.hexdigest() == check.target.hexdigest()
    assert check.server_rows == 3 and check.final_hash == check.server_hash
    assert state.archive.target.rows == 3
    assert dict(state.statements) == {"copy": 4, "readback": 4, "final_check": 2}
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
