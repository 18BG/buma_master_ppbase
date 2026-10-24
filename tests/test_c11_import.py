"""Fail-closed boundaries for the offline import preparation."""

import asyncio
import hashlib
import json
import sqlite3
import zipfile
from datetime import UTC, datetime
from decimal import Decimal

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


def test_dates_preserve_milliseconds_and_require_explicit_timezone():
    assert date_value("2024-02-29 12:34:56.789Z") == datetime(2024, 2, 29, 12, 34, 56, 789000, UTC)
    assert date_value("") is None
    with pytest.raises(ImportBlocked, match="date_without_timezone"):
        date_value("2024-02-29 12:34:56")
    with pytest.raises(ImportBlocked, match="invalid_date"):
        date_value("", required=True)


def test_multifiles_preserve_names_and_order_without_coercion():
    assert array_value('["z.bin","a.bin"]') == ["z.bin", "a.bin"]
    for value in ('"a"', "null", '["a",2]'):
        with pytest.raises(ImportBlocked):
            array_value(value)


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
        "created": "2024-01-01 00:00:00Z",
        "updated": "2024-01-01 00:00:00Z",
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
    import hashlib

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
    assert report["json_representation"] == [
        {
            "collection": "parent",
            "field": "data",
            "key_order_normalized": 1,
            "sql_null_stored_as_json_null": 1,
        }
    ]
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
        ('{"v":-0}', "json_number_not_representable"),
        ('{"v":-0.0e5}', "json_number_not_representable"),
        ('{"v":1e-20000}', "json_number_outside_target_range"),
        ("[1e200000]", "json_number_outside_target_range"),
        ('{"v":NaN}', "non_finite_json"),
        ("not json", "invalid_json"),
        ("", "invalid_json"),
        pytest.param("[" * 257 + "]" * 257, "json_nesting_too_deep", id="deep"),
        pytest.param("[" * 200000 + "]" * 200000, "invalid_json", id="parser_recursion"),
    ],
)
def test_json_must_be_exactly_representable_in_jsonb(text, code):
    with pytest.raises(ImportBlocked, match=code):
        c11.json_text(text)


def test_json_source_text_is_kept_verbatim():
    exact = '{"b": 0.12345678901234567890123, "a": 1e400, "n": [1.50, true]}'
    assert c11.json_text(exact) == exact  # no float round-trip, no re-serialization
    assert (c11.json_text(None), c11.json_text(5), c11.json_text(1.5)) == ("null", "5", "1.5")
    with pytest.raises(ImportBlocked, match="json_number_not_representable"):
        c11.json_text(-0.0)
    assert c11.json_representation_flags('{"aa":1,"b":2,"b":3}') == {
        "duplicate_keys",
        "key_order_normalized",
    }
    assert c11.json_representation_flags('{"b":1,"aa":[{"a":1}]}') == set()


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


class FakeResult:
    def __init__(self, value):
        self.value = value

    def scalar_one(self):
        return self.value


class FakeConnection:
    """Answers only current_database(); proves the checks that precede SQL."""

    def __init__(self, database="buma_c11_rehearsal", transaction=False):
        self.database, self.transaction = database, transaction
        self.statements = []

    async def execute(self, statement, parameters=None):
        self.statements.append(str(statement))
        return FakeResult(self.database)

    def in_transaction(self):
        return self.transaction


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
    with pytest.raises(ImportBlocked, match="isolated_rehearsal_database_required"):
        asyncio.run(c11.load_isolated(FakeConnection("other"), staged, schema))
    with pytest.raises(ImportBlocked, match="caller_transaction_required"):
        asyncio.run(c11.load_isolated(FakeConnection(), staged, schema))
    (staged / "storage").mkdir()
    connection = FakeConnection()
    with pytest.raises(ImportBlocked, match="storage_mode_mismatch"):
        asyncio.run(c11.load_isolated(connection, staged, schema))
    assert connection.statements == []
    # A manifest edited after the diagnosis cannot be staged.
    changed = json.loads(manifest.read_text())
    changed["files"].append(entry("README.md", size=3))
    manifest.unlink()
    write_json_new(manifest, changed)
    with pytest.raises(ImportBlocked, match="manifest_changed_after_diagnosis"):
        stage_snapshot(bundle, tmp_path / "late", report, manifest=manifest)
    assert not (tmp_path / "late").exists()


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


def backup_zip(tmp_path, members, name="backup.zip"):
    path = tmp_path / name
    with zipfile.ZipFile(path, "w", compression=zipfile.ZIP_STORED) as archive:
        for member, content in members:
            archive.writestr(member, content)
    return path


def test_verified_extraction_writes_private_files_and_the_manifest_format(tmp_path):
    with sqlite3.connect(tmp_path / "source.db") as conn:
        conn.execute("CREATE TABLE sample(id TEXT)")
    database = (tmp_path / "source.db").read_bytes()
    archive = backup_zip(
        tmp_path,
        [
            ("data.db", database),
            ("data.db-wal", b""),
            ("storage/col/rec/file.bin", b"\x00\xffbytes"),
            ("storage/col/rec/thumbs_file.bin/100x100_file.bin", b"thumb"),
        ],
    )
    destination, manifest = tmp_path / "extracted", tmp_path / "manifest.json"
    result = c11.extract_archive(archive, destination, manifest)
    assert result["crc_all_files_ok"] and result["files"] == 4
    assert result["archive_sha256"] == digest(archive)
    written = json.loads(manifest.read_text())
    assert written["archive_sha256"] == digest(archive)
    assert [entry["path"] for entry in written["files"]] == [
        "data.db",
        "data.db-wal",
        "storage/col/rec/file.bin",
        "storage/col/rec/thumbs_file.bin/100x100_file.bin",
    ]
    for entry in written["files"]:
        path = destination / entry["path"]
        sha, crc, size = c11.file_checksums(path)
        assert (sha, crc, size) == (entry["sha256"], entry["crc32"], entry["bytes"])
        assert path.stat().st_mode & 0o777 == 0o600
        assert path.parent.stat().st_mode & 0o777 == 0o700
    assert manifest.stat().st_mode & 0o777 == 0o600
    # The same manifest drives both storage modes.
    checked = c11.read_manifest(manifest, destination / "data.db")
    assert checked["storage"] == storage_inventory(destination / "storage")
    with pytest.raises(FileExistsError):
        c11.extract_archive(archive, destination, tmp_path / "other.json")
    assert not (tmp_path / "other.json").exists()


@pytest.mark.parametrize(
    "members,code",
    [
        ([("data.db", b"x"), ("../escape", b"x")], "unsafe_archive_member"),
        ([("data.db", b"x"), ("data.db-wal", b"live")], "live_or_incomplete_sqlite_snapshot"),
        ([("nested/data.db", b"x")], "data_db_must_be_at_archive_root"),
        ([("storage/a", b"x")], "exactly_one_data_db_required"),
    ],
)
def test_extraction_refuses_unsafe_archives_without_writing(tmp_path, members, code):
    archive = backup_zip(tmp_path, members)
    destination = tmp_path / "extracted"
    with pytest.raises(ImportBlocked, match=code):
        c11.extract_archive(archive, destination, tmp_path / "manifest.json")
    assert not destination.exists() and not (tmp_path / "manifest.json").exists()


def test_extraction_detects_corrupted_member_bytes_and_removes_everything(tmp_path):
    archive = backup_zip(tmp_path, [("data.db", b"database"), ("storage/c/r/f", b"ORIGINAL")])
    raw = bytearray(archive.read_bytes())
    offset = raw.index(b"ORIGINAL")
    raw[offset] = ord("X")  # stored member: same size, wrong CRC
    archive.write_bytes(bytes(raw))
    destination = tmp_path / "extracted"
    with pytest.raises(ImportBlocked, match="archive_member_crc_mismatch"):
        c11.extract_archive(archive, destination, tmp_path / "manifest.json")
    assert not destination.exists() and not (tmp_path / "manifest.json").exists()


def test_extraction_requires_disk_space_for_declared_sizes(tmp_path, monkeypatch):
    archive = backup_zip(tmp_path, [("data.db", b"database")])
    usage = type("Usage", (), {"free": 4})
    monkeypatch.setattr(c11.shutil, "disk_usage", lambda path: usage)
    with pytest.raises(ImportBlocked, match="insufficient_disk_space"):
        c11.extract_archive(archive, tmp_path / "extracted", tmp_path / "manifest.json")
    assert not (tmp_path / "extracted").exists()


def test_extraction_cli_reports_without_record_values(tmp_path, monkeypatch, capsys):
    archive = backup_zip(tmp_path, [("data.db", b"database"), ("storage/c/r/f", b"x")])
    report = tmp_path / "extraction.json"
    argv = ["c11_import", "--extract", str(archive), "--destination", str(tmp_path / "out")]
    monkeypatch.setattr(
        "sys.argv", argv + ["--manifest-out", str(tmp_path / "m.json"), "--report", str(report)]
    )
    assert c11.main() == 0
    written = json.loads(report.read_text())
    assert written["crc_all_files_ok"] and written["files"] == 2
    assert written["ready_for_isolated_rehearsal"] is False
    monkeypatch.setattr("sys.argv", argv + ["--report", str(tmp_path / "other.json")])
    with pytest.raises(SystemExit):
        c11.main()  # an extraction always names its new manifest
