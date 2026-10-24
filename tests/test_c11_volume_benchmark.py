"""The synthetic volume generator must only produce values the importer accepts."""

import json
import sqlite3

import pytest

from scripts import c11_import as importer
from scripts import c11_volume_benchmark as benchmark

USERS = {
    "id": "_pb_users_auth_",
    "name": "users",
    "type": "auth",
    "schema": [{"name": "name", "type": "text", "options": {}}],
}
ITEMS = {
    "id": "itemscol0000001",
    "name": "items",
    "type": "base",
    "schema": [
        {
            "name": "owner",
            "type": "relation",
            "options": {"collectionId": USERS["id"], "maxSelect": 1},
        },
        {"name": "photo", "type": "file", "options": {"maxSelect": 1}},
        {"name": "gallery", "type": "file", "options": {"maxSelect": 99}},
        {"name": "data", "type": "json", "options": {}},
        {"name": "amount", "type": "number", "options": {}},
        {"name": "flag", "type": "bool", "options": {}},
        {"name": "when", "type": "date", "options": {}},
        {"name": "kind", "type": "select", "options": {"maxSelect": 1, "values": ["A", "B"]}},
        {"name": "mail", "type": "email", "options": {}},
        {"name": "link", "type": "url", "options": {}},
        {"name": "notes", "type": "editor", "options": {}},
    ],
}


def snapshot(tmp_path):
    data = tmp_path / "data"
    data.mkdir()
    with sqlite3.connect(data / "data.db") as db:
        db.execute(
            "CREATE TABLE users (id TEXT PRIMARY KEY, created TEXT, updated TEXT, username TEXT "
            "UNIQUE, email TEXT, emailVisibility BOOLEAN, verified BOOLEAN, tokenKey TEXT UNIQUE, "
            "passwordHash TEXT, lastResetSentAt TEXT, lastVerificationSentAt TEXT, "
            "lastLoginAlertSentAt TEXT, name TEXT)"
        )
        db.execute(
            "INSERT INTO users VALUES ('fixtureuser0001', ?, ?, 'fixture', '', 0, 1, ?, "
            "'$2a$10$synthetic', '', '', '', 'Fixture')",
            (benchmark.pb_time(0), benchmark.pb_time(0), "t" * 50),
        )
        columns = ", ".join(
            f'"{f["name"]}" '
            + {"json": "JSON", "number": "NUMERIC", "bool": "BOOLEAN"}.get(f["type"], "TEXT")
            for f in ITEMS["schema"]
        )
        db.execute(
            f"CREATE TABLE items (id TEXT PRIMARY KEY, created TEXT, updated TEXT, {columns})"
        )
    return data


def test_profile_uses_public_counts_and_excludes_unbounded_tables():
    full = benchmark.profile_counts(1.0)
    assert not set(full) & benchmark.UNBOUNDED_TABLES
    real = json.loads(benchmark.PROFILE.read_text())["rows"]
    assert sum(full.values()) == real - 5826 - 4 - 11
    assert benchmark.profile_counts(0.5)["buma_transaction"] == round(full["buma_transaction"] / 2)


def test_generated_rows_are_importable_and_contain_preserved_anomalies(tmp_path):
    data = snapshot(tmp_path)
    result = benchmark.complete_fixture(data, [USERS, ITEMS], {"users": 5, "items": 4000})
    assert result["records"] == 4005 and result["synthetic_rows_added"] == 4004
    with sqlite3.connect(data / "data.db") as db:
        db.row_factory = sqlite3.Row
        for collection in (USERS, ITEMS):
            plan = importer.RowPlan(collection)
            for raw in importer.source_rows(db, collection):
                problems = []
                plan.convert(dict(raw), problems)
                assert problems == [], (collection["name"], problems)
        ids = {r[0] for r in db.execute("SELECT id FROM users")}
        owners = [r[0] for r in db.execute("SELECT owner FROM items WHERE owner != ''")]
        orphans = [owner for owner in owners if owner not in ids]
        assert orphans and len(orphans) == len(set(orphans))
        assert all(len(owner) == 15 for owner in orphans)
        missing = db.execute("SELECT count(*) FROM items WHERE photo != ''").fetchone()[0]
        assert missing == 3  # rows 1, 2000 and 3999: absent files, kept as references
        created = [r[0] for r in db.execute("SELECT created FROM items")]
        assert len(set(created)) == len(created)  # unique, as some exported indexes require


def test_unbounded_relations_are_not_generated_in_this_wave():
    field = {
        "name": "tags",
        "type": "relation",
        "options": {"collectionId": "x", "maxSelect": None},
    }
    with pytest.raises(ValueError, match="unbounded"):
        benchmark.field_generator(field, "t", {}, {"x": {"name": "x"}})


def test_container_memory_reads_the_cgroup_files(tmp_path):
    monitor = benchmark.ContainerMemory("absent")
    assert monitor.root is None and monitor.finish() == {"cgroup": "unavailable"}
    (tmp_path / "memory.current").write_text("4096\n")
    (tmp_path / "memory.stat").write_text("anon 1024\nfile 3072\n")
    (tmp_path / "memory.peak").write_text("8192\n")
    (tmp_path / "memory.max").write_text("2147483648\n")
    monitor.root = tmp_path
    assert monitor.read() == (4096, 1024)
    assert monitor.finish()["cgroup_memory_peak_bytes"] == 8192
