"""Fail-closed boundaries for the offline import preparation."""

import json
import sqlite3
import zipfile
from datetime import UTC, datetime

import pytest

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
