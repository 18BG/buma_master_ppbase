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


def test_container_memory_separates_page_cache_from_unreclaimable_memory(tmp_path):
    monitor = benchmark.ContainerMemory("absent")
    assert monitor.root is None and monitor.finish() == {"cgroup": "unavailable"}
    (tmp_path / "memory.current").write_text("4096\n")
    (tmp_path / "memory.stat").write_text("anon 1024\nfile 3072\nshmem 512\nactive_file 9\n")
    (tmp_path / "memory.peak").write_text("8192\n")
    (tmp_path / "memory.max").write_text("2147483648\n")
    (tmp_path / "memory.events").write_text("low 0\nhigh 0\nmax 12\noom 0\noom_kill 0\n")
    monitor = benchmark.ContainerMemory("absent")
    monitor.root = tmp_path
    values = monitor.read()
    assert values == {
        "anon": 1024,
        "file": 3072,
        "shmem": 512,
        "current": 4096,
        "anon_plus_shmem": 1536,
    }
    monitor.samples = dict(values)
    result = monitor.finish()
    assert result["cgroup_memory_peak_bytes_including_page_cache"] == 8192
    assert result["sampled_max_anon_plus_shmem_bytes"] == 1536
    assert result["events"] == {"max": 12, "oom": 0, "oom_kill": 0}
    assert "page cache" in result["interpretation"]
    assert set(monitor.take_phase()) == {
        *(f"max_{key}_bytes" for key in benchmark.MEMORY_KEYS),
        "max_host_loadavg_1min",
    }


def test_negative_bundle_has_rows_in_every_generated_table():
    counts = benchmark.negative_counts()
    assert set(counts) == set(benchmark.profile_counts(1.0))
    assert min(counts.values()) == benchmark.NEGATIVE_MINIMUM_ROWS
    assert sum(counts.values()) < 5000


def test_negative_cases_alter_one_owned_object_each_and_undo_it():
    definitions = [
        {"name": "users", "type": "auth"},
        {"name": "early", "type": "base"},
        {"name": benchmark.NEGATIVE_TABLE, "type": "base"},
        {"name": "late", "type": "base"},
        {"name": "a_view", "type": "view"},
    ]
    column = importer.TargetColumn
    spec = {
        "users": {"id": column("id", "character varying", False, 15, True, False)},
        "early": {
            "id": column("id", "character varying", False, 15, True, False),
            "updated": column("updated", "timestamp with time zone", False, None, True, True),
        },
        benchmark.NEGATIVE_TABLE: {
            "id": column("id", "character varying", False, 15, True, False),
            benchmark.NEGATIVE_COLUMN: column("firstname", "text", False, None, True, True),
        },
        "late": {"id": column("id", "character varying", False, 15, True, False)},
    }
    diagnostic = {
        "rows": 20,
        "collections": [{"name": d["name"], "rows": 5} for d in definitions[:4]],
        "preserved_source_anomalies": {
            "dangling_relations": {
                "references": 2,
                "records": 2,
                "fields": [{"collection": "late", "field": "x", "references": 2}],
            }
        },
    }
    definitions[2]["schema"] = [{"name": "firstname", "type": "text"}]
    definitions[3]["schema"] = [{"name": "note", "type": "text"}]
    spec["late"]["note"] = column("note", "text", False, None, True, True)
    for d in definitions[:2] + definitions[4:]:
        d["schema"] = []
    used_by_views = {(benchmark.NEGATIVE_TABLE, "firstname")}
    narrow = benchmark.narrowable_text_field(definitions, spec, diagnostic, used_by_views)
    assert narrow == ("late", "note")
    cases = benchmark.negative_cases(definitions, spec, diagnostic, narrow, "''::text")
    names = [case["name"] for case in cases]
    assert names == [
        "rowid_archive_exists",
        "target_constraint_violation",
        "value_exceeds_target_length_at_load",
        "target_readback_mismatch",
        "target_changed_after_readback",
        "target_row_count_mismatch",
        "anomaly_conservation_mismatch",
        "source_row_count_mismatch",
        "injected_import_failure",
    ]
    by_name = {case["name"]: case for case in cases}
    assert by_name["target_changed_after_readback"]["details"] == {"collection": "early"}
    assert 'AFTER INSERT ON "late"' in by_name["target_row_count_mismatch"]["setup"][1]
    # Every created object is dropped by the same case; the diagnosis is a copy.
    for case in cases:
        created = [s.split()[2] for s in case.get("setup", []) if s.startswith("CREATE")]
        dropped = " ".join(case.get("teardown", []))
        assert all(name.split("(")[0] in dropped for name in created), case["name"]
    assert diagnostic["preserved_source_anomalies"]["dangling_relations"]["references"] == 2
    tampered = by_name["anomaly_conservation_mismatch"]["diagnostic"]
    assert tampered["preserved_source_anomalies"]["dangling_relations"]["references"] == 1
    assert by_name["injected_import_failure"]["fail_after"] == 10
    narrowing = by_name["value_exceeds_target_length_at_load"]
    assert narrowing["details"] == {"collection": "late", "field": "note"}
    assert narrowing["teardown"] == [
        'ALTER TABLE "late" ALTER COLUMN "note" TYPE text',
        'ALTER TABLE "late" ALTER COLUMN "note" SET DEFAULT \'\'::text',
    ]
