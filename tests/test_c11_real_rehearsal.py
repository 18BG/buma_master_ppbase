"""WS-G rehearsal script: pure checks, privacy guard, refusals and cleanup.

Synthetic values only: no backup, no container, no server.
"""

from __future__ import annotations

import json
import os
import sqlite3
import subprocess
import sys
from pathlib import Path
from types import SimpleNamespace

import pytest

from scripts import c11_import as importer
from scripts import c11_real_rehearsal as rehearsal

RUN_ID = "0123456789ab"
VOLUME = "f" * 64


def inspected(**overrides) -> dict:
    """A docker inspect document of a container this run would have created."""
    info = {
        "Name": "/" + rehearsal.CONTAINER_PREFIX + RUN_ID,
        "Config": {"Labels": {"buma.scope": rehearsal.SCOPE_LABEL, "buma.run": RUN_ID}},
        "HostConfig": {
            "AutoRemove": True,
            "Binds": None,
            "Memory": 2 * 1024**3,
            "MemorySwap": 2 * 1024**3,
            "NanoCpus": 2_000_000_000,
            "ShmSize": 512 * 1024**2,
        },
        "Mounts": [{"Type": "volume", "Destination": rehearsal.PGDATA, "Name": VOLUME}],
        "NetworkSettings": {"Ports": {"5432/tcp": [{"HostIp": "127.0.0.1", "HostPort": "54321"}]}},
    }
    for path, value in overrides.items():
        target = info
        keys = path.split(".")
        for key in keys[:-1]:
            target = target[key]
        target[keys[-1]] = value
    return info


def refused(call, code: str):
    with pytest.raises(rehearsal.RehearsalFailure) as caught:
        call()
    assert caught.value.code == code
    return caught.value


# --- refusal of any target that is not this run's disposable container -------


def test_container_of_this_run_is_accepted():
    info = rehearsal.check_container(inspected(), RUN_ID)
    assert info["port"] == 54321
    assert info["anonymous_volumes"] == [VOLUME]


@pytest.mark.parametrize(
    ("path", "value", "reason"),
    [
        ("Name", "/postgres", "container_name"),
        ("Name", "/" + rehearsal.CONTAINER_PREFIX + "other", "container_name"),
        ("Config.Labels", {"buma.run": RUN_ID}, "label"),
        ("Config.Labels", {"buma.scope": rehearsal.SCOPE_LABEL}, "run_label"),
        ("HostConfig.AutoRemove", False, "auto_remove"),
        ("HostConfig.Binds", ["/srv/pgdata:/var/lib/postgresql/data"], "bind_mount"),
        ("HostConfig.Memory", 0, "memory_limit"),
        ("HostConfig.NanoCpus", 0, "cpu_limit"),
        ("Mounts", [{"Type": "volume", "Destination": rehearsal.PGDATA, "Name": "prod"}], "mount"),
        ("Mounts", [{"Type": "bind", "Destination": rehearsal.PGDATA, "Name": VOLUME}], "mount"),
        ("NetworkSettings.Ports", {"5432/tcp": [{"HostIp": "0.0.0.0", "HostPort": "1"}]}, "ip"),
        ("NetworkSettings.Ports", {}, "port"),
    ],
)
def test_container_not_disposable_is_refused(path, value, reason):
    failure = refused(
        lambda: rehearsal.check_container(inspected(**{path: value}), RUN_ID),
        "non_disposable_target",
    )
    assert failure.details == {"reason": reason}


@pytest.mark.parametrize(
    ("url", "reason"),
    [
        ("postgresql+asyncpg://c11:x@10.0.0.5:54321/buma_c11_rehearsal", "host"),
        ("postgresql+asyncpg://c11:x@127.0.0.1:5432/buma_c11_rehearsal", "port"),
        ("postgresql+asyncpg://c11:x@127.0.0.1:54321/buma", "database"),
        ("postgresql+psycopg://c11:x@127.0.0.1:54321/buma_c11_rehearsal", "driver"),
    ],
)
def test_url_other_than_the_owned_container_is_refused(url, reason):
    container = {"name": rehearsal.CONTAINER_PREFIX + RUN_ID, "port": 54321}
    rehearsal.check_disposable_url(
        "postgresql+asyncpg://c11:x@127.0.0.1:54321/buma_c11_rehearsal", container
    )
    failure = refused(
        lambda: rehearsal.check_disposable_url(url, container), "non_disposable_target"
    )
    assert failure.details == {"reason": reason}


def test_url_of_a_container_not_owned_is_refused():
    url = "postgresql+asyncpg://c11:x@127.0.0.1:54321/buma_c11_rehearsal"
    failure = refused(
        lambda: rehearsal.check_disposable_url(url, {"name": "postgres", "port": 54321}),
        "non_disposable_target",
    )
    assert failure.details == {"reason": "container"}


def test_real_data_requires_an_isolated_interpreter():
    refused(
        lambda: rehearsal.require_isolated_interpreter(SimpleNamespace(isolated=0)),
        "isolated_interpreter_required",
    )
    rehearsal.require_isolated_interpreter(SimpleNamespace(isolated=1))


def backup_layout(tmp_path: Path) -> SimpleNamespace:
    data = tmp_path / "backup"
    data.mkdir()
    (data / "data.db").write_bytes(b"synthetic")
    (data / "manifest.json").write_text("{}")
    schema = tmp_path / "schema.json"
    schema.write_text("[]")
    private_parent = tmp_path / "private"
    private_parent.mkdir(mode=0o700)
    private_parent.chmod(0o700)
    return SimpleNamespace(
        data=data,
        manifest=data / "manifest.json",
        schema=schema,
        ppbase=tmp_path / "ppbase",
        report=tmp_path / "report.json",
        private_dir=private_parent / "run",
    )


def test_paths_of_a_well_formed_run_are_accepted(tmp_path):
    rehearsal.check_paths(backup_layout(tmp_path), tmp_path)


def test_paths_refusals(tmp_path):
    args = backup_layout(tmp_path)
    refused(lambda: rehearsal.check_paths(args, args.data), "interpreter_started_inside_the_backup")
    args.report.write_text("{}")
    failure = refused(lambda: rehearsal.check_paths(args, tmp_path), "output_path_must_be_new")
    assert failure.details == {"output": "report"}
    args.report.unlink()
    inside = SimpleNamespace(**{**vars(args), "private_dir": args.data / "private"})
    failure = refused(lambda: rehearsal.check_paths(inside, tmp_path), "output_inside_the_backup")
    assert failure.details == {"output": "private_dir"}
    nested = SimpleNamespace(**{**vars(args), "report": args.private_dir / "report.json"})
    refused(lambda: rehearsal.check_paths(nested, tmp_path), "report_parent_required")
    args.private_dir.parent.chmod(0o755)
    refused(lambda: rehearsal.check_paths(args, tmp_path), "private_parent_not_owner_only")
    args.private_dir.parent.chmod(0o700)
    code_in_backup = SimpleNamespace(**{**vars(args), "ppbase": args.data / "ppbase"})
    refused(lambda: rehearsal.check_paths(code_in_backup, tmp_path), "code_inside_the_backup")


def test_source_migrations_must_be_the_frozen_migrations_directory(tmp_path):
    from scripts.c11_idempotency_source_check import SOURCE_MIGRATION

    args = backup_layout(tmp_path)
    migrations = tmp_path / "migrationsDir"

    def check():
        rehearsal.check_paths(
            SimpleNamespace(**{**vars(args), "source_migrations": migrations}), tmp_path
        )

    refused(check, "source_migrations_directory_required")
    migrations.write_text("not a directory")
    refused(check, "source_migrations_directory_required")
    migrations.unlink()
    migrations.mkdir()
    (migrations / "README.md").write_text("synthetic")
    refused(check, "source_migrations_without_js_migrations")
    (migrations / "1730323350_updated_synthetic.js").write_text("migrate(() => {})")
    refused(check, "source_migrations_without_the_d0022_migration")
    (migrations / SOURCE_MIGRATION).write_text("migrate(() => {})")
    check()
    rehearsal.check_paths(SimpleNamespace(**{**vars(args), "source_migrations": None}), tmp_path)
    parser = rehearsal.build_parser()
    assert "migrationsDir" in parser.format_help()


def test_public_report_in_the_repository_requires_committed_code(tmp_path, monkeypatch):
    args = backup_layout(tmp_path)
    args.report = rehearsal.PROJECT / "docs" / "c11-real-rehearsal-test-only.json"
    monkeypatch.setattr(rehearsal, "git_state", lambda _path: {"tracked_files_clean": False})
    refused(lambda: rehearsal.check_paths(args, tmp_path), "report_requires_committed_code")


# --- cleanup ----------------------------------------------------------------


def test_cleanup_runs_every_step_in_order_and_records_failures():
    calls = []

    def failing():
        calls.append("server")
        raise OSError("stop failed")

    cleanup = rehearsal.Cleanup()
    cleanup.add("children", lambda: calls.append("children"))
    cleanup.add("server", failing)
    cleanup.add("container", lambda: calls.append("container"))
    results = cleanup.run()
    assert calls == ["children", "server", "container"]
    assert results == {"children": "done", "server": "failed:OSError", "container": "done"}
    assert not cleanup.ok
    assert "stop failed" not in json.dumps(results)


def test_stop_process_terminates_a_running_child():
    process = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(60)"])
    rehearsal.stop_process(process, timeout=10)
    assert process.poll() is not None


def test_server_files_are_removed(tmp_path):
    for name in ("server-cwd", "server-data"):
        (tmp_path / name / "sub").mkdir(parents=True)
        (tmp_path / name / "sub" / "file").write_text("x")
    (tmp_path / "server.log").write_text("kept, private")
    rehearsal.remove_server_files(tmp_path)
    assert sorted(p.name for p in tmp_path.iterdir()) == ["server.log"]


def test_failure_entries_carry_codes_never_messages():
    class DriverError(Exception):
        sqlstate = "23505"

    entry = rehearsal.failure_entry(DriverError('Key (email)=("someone@example.org") exists'))
    assert entry == {"type": "DriverError", "sqlstate": "23505"}
    entry = rehearsal.failure_entry(importer.ImportBlocked("target_not_empty", collection="users"))
    assert entry == {
        "type": "ImportBlocked",
        "code": "target_not_empty",
        "details": {"collection": "users"},
    }
    entry = rehearsal.failure_entry(rehearsal.RehearsalFailure("x", value=object()))
    assert entry["details"] == {"value": "object"}


def test_child_failure_is_reported_without_its_message(tmp_path, capsys):
    import ppbase

    params = tmp_path / "params.json"
    params.write_text(json.dumps({"ppbase": str(Path(ppbase.__file__).resolve().parents[1])}))
    assert rehearsal.child_main("unknown-role", params) == 1
    printed = json.loads(capsys.readouterr().out.strip().splitlines()[-1])
    assert printed["failure"] == {"type": "KeyError", "sqlstate": None}
    assert printed["peak_rss_kib"] > 0


# --- privacy of the public report -------------------------------------------


def test_privacy_patterns_are_reported_by_path_only():
    report = {
        "ok": "imported_as_is_no_target_or_file_invented_no_reference_removed_or_redirected",
        "migration": "1785599959_updated_buma_transaction.js",
        "digest": "a" * 64,
        "commit": "b" * 40,
        "fingerprint": "12:" + "c" * 64,
        "leak": {"mail": "contact: someone@example.org"},
        "hash": "$2a$10$" + "x" * 53,
        "jwt": "eyJhbGciOiJIUzI1NiJ9.eyJpZCI6ImFiYyJ9.signature",
        "url": "postgresql://user:secret@host/db",
        "token": "Ab3" * 12,
    }
    violations = rehearsal.privacy_violations(report, rehearsal.HashedValues())
    assert violations == [
        {"path": "$.leak.mail", "reason": "email_address"},
        {"path": "$.hash", "reason": "bcrypt_hash"},
        {"path": "$.jwt", "reason": "jwt"},
        {"path": "$.url", "reason": "url_credentials"},
        {"path": "$.token", "reason": "long_token"},
    ]
    assert "someone" not in json.dumps(violations)


def test_privacy_finds_real_values_whole_or_inside_a_string_and_in_keys():
    sensitive = rehearsal.HashedValues(["k7d2m9q4w1x8z3c", "Mariam", "Diallo Store"])
    report = {
        "ok": {"rows": 12, "status": "passed"},
        "record": "k7d2m9q4w1x8z3c",
        "embedded": "first id k7d2m9q4w1x8z3c of the page",
        "name": "Mariam",
        "nested": [{"label": "Diallo Store"}],
        "Mariam": 1,
    }
    reasons = {v["path"]: v["reason"] for v in rehearsal.privacy_violations(report, sensitive)}
    assert reasons == {
        "$.record": "real_value",
        "$.embedded": "real_value",
        "$.name": "real_value",
        "$.nested[0].label": "real_value",
        "$.<key>": "real_value",
    }


def test_privacy_exempts_schema_names_code_literals_and_digests():
    definitions = [
        {
            "id": "ynd8qrm418yp975",
            "name": "buma_customer",
            "schema": [{"name": "pdv", "type": "relation", "options": {"values": ["CASH"]}}],
        }
    ]
    exempt = rehearsal.schema_vocabulary(definitions) | {"passed"}
    # Real values that coincide with public names or code words are not leaks.
    sensitive = rehearsal.HashedValues(["buma_customer", "ynd8qrm418yp975", "CASH", "passed"])
    report = {
        "buma_customer.pdv": {"collection_id": "ynd8qrm418yp975", "value": "CASH"},
        "status": "passed",
        "digest": "0" * 64,
    }
    assert rehearsal.privacy_violations(report, sensitive, exempt) == []
    assert "buma_customer.pdv" in exempt


def test_code_literals_are_the_string_constants_of_the_sources(tmp_path):
    source = tmp_path / "module.py"
    source.write_text(
        'A = "first"\n\ndef f():\n    return {"second": 1, "x": "a claim of PostgreSQL"}\n'
    )
    literals = rehearsal.code_literals(source)
    # Whole constants, and their tokens of 8+ characters (code words, not data).
    assert literals == {"first", "second", "x", "a claim of PostgreSQL", "PostgreSQL"}


def test_a_code_word_equal_to_a_real_value_is_not_a_leak(tmp_path):
    source = tmp_path / "module.py"
    source.write_text('CLAIM = "every record imported in one transaction"\n')
    sensitive = rehearsal.HashedValues(["transaction", "a-real-secret-value"])
    exempt = rehearsal.code_literals(source)
    report = {"proves": ["every record imported in one transaction"]}
    assert rehearsal.privacy_violations(report, sensitive, exempt) == []
    leaked = {"proves": ["every record imported in one transaction a-real-secret-value"]}
    assert rehearsal.privacy_violations(leaked, sensitive, exempt) == [
        {"path": "$.proves[0]", "reason": "real_value"}
    ]


def test_hashed_values_membership_survives_compaction(monkeypatch):
    monkeypatch.setattr(rehearsal.HashedValues, "LIMIT", 8)
    values = rehearsal.HashedValues()
    for index in range(100):
        values.add(f"value-{index:03d}")
        values.add(f"value-{index:03d}")  # duplicates are compacted away
    values.add("abc")  # shorter than 4 characters: never checked
    assert len(values) == 100
    assert "value-042" in values
    assert "value-100" not in values
    assert "abc" not in values
    assert 42 not in values


def test_collect_sensitive_hashes_every_text_value_and_manifest_key(tmp_path):
    data = tmp_path / "backup"
    data.mkdir()
    db = sqlite3.connect(data / "data.db")
    db.executescript(
        """
        CREATE TABLE buma_wallet (id TEXT, managers TEXT, balance NUMERIC, more TEXT);
        INSERT INTO buma_wallet VALUES
            ('w1a2b3c4d5e6f7g', '["u1a2b3c4d5e6f7g,u7g6f5e4d3c2b1a"]', 12.5, '{"note": "Fatou"}');
        CREATE TABLE _admins (id TEXT, email TEXT, passwordHash TEXT, tokenKey TEXT);
        INSERT INTO _admins VALUES ('a1b2c3d4e5f6g7h', 'boss@example.org', '$2a$10$hash', 'tk1');
        CREATE TABLE _params (id TEXT, key TEXT, value TEXT);
        INSERT INTO _params VALUES ('p1', 'settings', '{"smtp": {"password": "s3cr3t-pass"}}');
        """
    )
    db.commit()
    db.close()
    sha, crc, size = importer.file_checksums(data / "data.db")
    manifest = tmp_path / "manifest.json"
    manifest.write_text(
        json.dumps(
            {
                "archive_sha256": "1" * 64,
                "files": [
                    {"path": "data.db", "bytes": size, "sha256": sha, "crc32": crc},
                    {
                        "path": "storage/ri4223ezesmoai3/w1a2b3c4d5e6f7g/photo_k1.png",
                        "bytes": 3,
                        "sha256": "2" * 64,
                        "crc32": "0000abcd",
                    },
                ],
            }
        )
    )
    values = rehearsal.collect_sensitive(data, manifest)
    for real in (
        "w1a2b3c4d5e6f7g",
        "u1a2b3c4d5e6f7g,u7g6f5e4d3c2b1a",
        "Fatou",
        "boss@example.org",
        "$2a$10$hash",
        "s3cr3t-pass",
        "photo_k1.png",
        "ri4223ezesmoai3/w1a2b3c4d5e6f7g/photo_k1.png",
    ):
        assert real in values, real
    assert "12.5" not in values  # numbers are not text values
    assert sorted(p.name for p in data.iterdir()) == ["data.db"]  # read only, no sidecar


# --- pure checks of HTTP bodies ----------------------------------------------


def envelope(total: int, items: int, *, page=1, per_page=1) -> dict:
    return {
        "page": page,
        "perPage": per_page,
        "totalItems": total,
        "totalPages": -(-total // per_page),
        "items": [{}] * items,
    }


def test_envelope_problems():
    assert rehearsal.envelope_problems(envelope(5, 1), page=1, per_page=1, expected_total=5) == []
    assert rehearsal.envelope_problems(envelope(0, 0), page=1, per_page=1, expected_total=0) == []
    assert rehearsal.envelope_problems(envelope(4, 1), page=1, per_page=1, expected_total=5) == [
        "total_items_mismatch"
    ]
    body = envelope(5, 1) | {"skipTotal": False}
    assert rehearsal.envelope_problems(body, page=1, per_page=1, expected_total=None) == [
        "envelope_keys"
    ]
    body = envelope(5, 1) | {"totalPages": 1}
    assert rehearsal.envelope_problems(body, page=1, per_page=1, expected_total=5) == [
        "total_pages"
    ]
    body = envelope(0, 0) | {"totalPages": 1}
    assert rehearsal.envelope_problems(body, page=1, per_page=1, expected_total=0) == [
        "total_pages_one_for_empty_list"
    ]
    assert rehearsal.envelope_problems(envelope(5, 0), page=1, per_page=1, expected_total=5) == [
        "items_count"
    ]
    body = envelope(5, 1) | {"totalItems": True}
    assert "total_items_type" in rehearsal.envelope_problems(
        body, page=1, per_page=1, expected_total=5
    )
    assert rehearsal.envelope_problems(None, page=1, per_page=1, expected_total=1) == [
        "not_an_object"
    ]


USERS = {
    "id": "_pb_users_auth_",
    "name": "users",
    "type": "auth",
    "schema": [
        {"name": "name", "type": "text", "options": {}},
        {"name": "role", "type": "relation", "options": {"maxSelect": 1}},
        {"name": "set", "type": "bool", "options": {}},
        {"name": "more", "type": "json", "options": {}},
    ],
}
WALLET = {
    "id": "ri4223ezesmoai3",
    "name": "buma_wallet",
    "type": "base",
    "schema": [
        {"name": "balance", "type": "number", "options": {}},
        {"name": "managers", "type": "relation", "options": {"maxSelect": None}},
        {"name": "image", "type": "file", "options": {"maxSelect": 1}},
    ],
}


def served_user(**extra) -> dict:
    return {
        "id": "u1",
        "created": "2026-01-01 00:00:00.000Z",
        "updated": "2026-01-01 00:00:00.000Z",
        "collectionId": "_pb_users_auth_",
        "collectionName": "users",
        "username": "u",
        "email": "e",
        "emailVisibility": True,
        "verified": False,
        "name": "",
        "role": "",
        "set": False,
        "more": None,
        **extra,
    }


def test_record_shape_of_auth_records():
    assert rehearsal.record_shape_problems(USERS, served_user(), email_visible=True) == []
    hidden = served_user()
    del hidden["email"]
    assert rehearsal.record_shape_problems(USERS, hidden, email_visible=False) == []
    assert rehearsal.record_shape_problems(USERS, hidden, email_visible=True) == ["keys"]
    leaked = served_user(passwordHash="x", tokenKey="y")
    assert rehearsal.record_shape_problems(USERS, leaked, email_visible=True) == [
        "forbidden_key",
        "keys",
    ]
    dated = {
        **USERS,
        "schema": [*USERS["schema"], {"name": "since", "type": "date", "options": {}}],
    }
    assert rehearsal.record_shape_problems(dated, served_user(since=""), email_visible=True) == []
    # A null date is the empty-date divergence only when the source date is empty.
    for source in ({"since": ""}, {"since": None}):
        assert rehearsal.record_shape_problems(
            dated, served_user(since=None), email_visible=True, source=source
        ) == ["empty_date_served_as_null:since"]
    for source in ({"since": "2026-01-01 00:00:00.000Z"}, None):
        assert rehearsal.record_shape_problems(
            dated, served_user(since=None), email_visible=True, source=source
        ) == ["kind:since"]
    wrong = served_user(set="false", role=["r1"])
    assert rehearsal.record_shape_problems(USERS, wrong, email_visible=True) == [
        "kind:role",
        "kind:set",
    ]


def test_record_shape_of_a_multiple_relation_is_a_list_of_strings():
    record = {
        "id": "w1",
        "created": "",
        "updated": "",
        "collectionId": "ri4223ezesmoai3",
        "collectionName": "buma_wallet",
        "balance": 0,
        "managers": ["a,b"],
        "image": "",
    }
    assert rehearsal.record_shape_problems(WALLET, record, email_visible=True) == []
    assert rehearsal.record_shape_problems(
        WALLET, record | {"managers": "a,b"}, email_visible=True
    ) == ["kind:managers"]
    assert rehearsal.record_shape_problems(
        WALLET, record | {"balance": True, "collectionName": "x"}, email_visible=True
    ) == ["collection_name", "kind:balance"]


def test_error_body_problems():
    body = {"code": 403, "message": rehearsal.PB_ADMIN_ONLY, "data": {}}
    assert rehearsal.error_body_problems(body, 403, rehearsal.PB_ADMIN_ONLY) == []
    assert rehearsal.error_body_problems(
        {"code": 403, "message": "Forbidden", "data": {}}, 403, rehearsal.PB_ADMIN_ONLY
    ) == ["message"]
    current = {"status": 403, "message": rehearsal.PB_ADMIN_ONLY, "data": {}}
    assert rehearsal.error_body_problems(current, 403, rehearsal.PB_ADMIN_ONLY) == [
        "error_status_key_instead_of_code"
    ]
    # PPBase's additive envelope, with the current PocketBase message.
    additive = {"code": 403, "status": 403, "message": "Only superusers can perform this action."}
    assert rehearsal.error_body_problems(additive | {"data": {}}, 403, rehearsal.PB_ADMIN_ONLY) == [
        "error_status_key_next_to_code",
        "message",
    ]
    assert rehearsal.error_body_problems(
        {"code": 403, "status": 404, "message": rehearsal.PB_ADMIN_ONLY, "data": {}},
        403,
        rehearsal.PB_ADMIN_ONLY,
    ) == ["keys"]
    assert rehearsal.error_body_problems({"detail": "x"}, 404, rehearsal.PB_NOT_FOUND) == [
        "keys",
        "code",
        "message",
        "data",
    ]


def test_rule_kinds():
    assert [rehearsal.rule_kind(rule) for rule in (None, "", "@request.auth.id != ''")] == [
        "admin_only",
        "public",
        "expression",
    ]


# --- aggregation and comparisons ----------------------------------------------


def test_length_distribution_and_most_frequent():
    from collections import Counter

    assert rehearsal.length_distribution([0, 1, 0, 2, 0]) == {"0": 3, "1": 1, "2": 1}
    assert rehearsal.most_frequent(Counter({"b": 2, "a": 2, "c": 1})) == ("a", 2)
    assert rehearsal.most_frequent(Counter()) == (None, 0)


def test_expected_differences():
    expected = {"rows": 3, "fields": {"a.b": [1, 1, 1]}, "dist": {"0": 2}}
    assert rehearsal.expected_differences(expected, expected) == []
    observed = {"rows": 4, "fields": {"a.b": [1, 1, 1], "c.d": [1, 1, 1]}, "dist": {}}
    assert rehearsal.expected_differences(observed, expected) == [
        "$.rows",
        "$.fields.c.d:unexpected",
        "$.dist.0:missing",
    ]


def test_compare_repeatable():
    first = {"rows": 3, "views": {"v": 2}, "list": [1, {"a": 1}]}
    assert rehearsal.compare_repeatable(first, json.loads(json.dumps(first))) == []
    second = {"rows": 3, "views": {"v": 3}, "list": [1, {"a": 2}], "extra": 1}
    assert rehearsal.compare_repeatable(first, second) == [
        "$.extra:missing",
        "$.list[1].a",
        "$.views.v",
    ]
    assert rehearsal.compare_repeatable([1], [1, 2]) == ["$:length"]


def test_signature_difference_allows_only_the_import_archives():
    before = ["table users", "column users.id text not null"]
    archives = [
        "table _buma_c11_rowids",
        "constraint _buma_c11_auth_archive._buma_c11_auth_archive_pkey PRIMARY KEY",
    ]
    assert rehearsal.signature_difference(before, before + archives)["only_import_archives_added"]
    changed = rehearsal.signature_difference(before, before + archives + ["trigger users.x"])
    assert changed == {
        "lines_added": 3,
        "lines_removed": 0,
        "only_import_archives_added": False,
        "unexpected_lines": 1,
    }
    removed = rehearsal.signature_difference(before, before[:1] + archives)
    assert not removed["only_import_archives_added"]


@pytest.mark.parametrize(
    "line",
    [
        # Objects of another table whose name merely contains an archive name.
        "column users._buma_c11_rowids text",
        "index users.users__buma_c11_rowids_idx CREATE INDEX users__buma_c11_rowids_idx "
        "ON public.users USING btree (id)",
        "constraint buma_sale._buma_c11_auth_archive_fk FOREIGN KEY (id) "
        "REFERENCES _buma_c11_auth_archive(record_id)",
        "trigger _buma_c11_rowids.guard O CREATE TRIGGER guard BEFORE INSERT "
        "ON public._buma_c11_rowids FOR EACH ROW EXECUTE FUNCTION f()",
        "function public._buma_c11_rowids_copy() 0123",
        "table _buma_c11_rowids_shadow",
        "table x_buma_c11_auth_archive",
        "view _buma_c11_rowids_view 0123",
        "default buma_sale.note '_buma_c11_rowids'::text",
    ],
)
def test_signature_difference_tolerates_only_the_archive_objects_themselves(line):
    before = ["table users"]
    difference = rehearsal.signature_difference(before, [*before, "table _buma_c11_rowids", line])
    assert difference["only_import_archives_added"] is False
    assert difference["unexpected_lines"] == 1
    assert not rehearsal.archive_line(line)
    for allowed in (
        "table _buma_c11_auth_archive",
        "column _buma_c11_rowids.auth_legacy jsonb not null",
        "default _buma_c11_rowids.x 0",
        "constraint _buma_c11_rowids._buma_c11_rowids_collection_id_source_rowid_key UNIQUE",
        "index _buma_c11_auth_archive._buma_c11_auth_archive_pkey CREATE UNIQUE INDEX",
    ):
        assert rehearsal.archive_line(allowed)


def test_metadata_changes_allowed_only_for_imported_auth_options():
    before = {
        "users": {"schema": [], "options": {"authToken": {"duration": 1}, "minPasswordLength": 8}},
        "buma_sale": {"schema": [], "options": {}},
    }
    after = json.loads(json.dumps(before))
    after["users"]["options"]["authToken"] = {"duration": 1, "secret": "s"}
    after["users"]["options"]["verificationTemplate"] = {"subject": "x"}
    changes = rehearsal.metadata_changes(before, after)
    assert changes == {"users": ["options.authToken", "options.verificationTemplate"]}
    assert rehearsal.import_changes_allowed(changes, {"users"})
    after["users"]["options"]["minPasswordLength"] = 5
    assert not rehearsal.import_changes_allowed(
        rehearsal.metadata_changes(before, after), {"users"}
    )
    after = json.loads(json.dumps(before))
    after["buma_sale"]["schema"] = [{"name": "x"}]
    assert rehearsal.metadata_changes(before, after) == {"buma_sale": ["schema"]}
    assert not rehearsal.import_changes_allowed({"buma_sale": ["schema"]}, {"users"})


def test_filter_cases_follow_pocketbase_0_22_semantics():
    def sample(value, like, json_equal, id_any, empty, rows):
        return {
            "value": value,
            "like_rows": like,
            "json_equal_rows": json_equal,
            "id_any_rows": id_any,
            "empty_rows": empty,
            "rows": rows,
        }

    samples = {
        "buma_customer.pdv": sample("p1", 7, 0, 7, 90, 100),
        "buma_pilotage_ticket.tag": sample("t1", 2, 0, 2, 0, 4),
        "buma_wallet.managers": sample("a,b", 1, 0, 0, 10, 11),
        "orphan": {"collection": "buma_sale", "field": "customer", "value": "o1", "rows": 5},
        "present_single": {
            "collection": "buma_stock",
            "field": "article",
            "value": "c1",
            "rows": 3,
        },
    }
    assert rehearsal.unsampled_filter_codes(samples) == []
    cases = {
        code: (name, expr, total) for code, name, expr, total in rehearsal.filter_cases(samples)
    }
    assert cases["pdv_contains"] == ("buma_customer", 'pdv ~ "p1"', 7)
    # A direct multi-value field is its JSON text: ?= compares it whole.
    assert cases["pdv_any_equals_whole_json_text"] == ("buma_customer", 'pdv ?= "p1"', 0)
    assert cases["pdv_id_any_equals"] == ("buma_customer", 'pdv.id ?= "p1"', 7)
    assert cases["pdv_length_positive"] == ("buma_customer", "pdv:length > 0", 10)
    assert cases["managers_contains"] == ("buma_wallet", 'managers ~ "a,b"', 1)
    assert cases["managers_id_any_equals"] == ("buma_wallet", 'managers.id ?= "a,b"', 0)
    assert cases["orphan_single_target_id"] == ("buma_sale", 'customer.id = "o1"', 0)
    assert cases["orphan_single_equals"] == ("buma_sale", 'customer = "o1"', 5)
    # The present single relation is the first one whose target exists.
    assert cases["single_equals"] == ("buma_stock", 'article = "c1"', 3)
    assert cases["single_target_id"] == ("buma_stock", 'article.id = "c1"', 3)
    assert len(cases) == 19
    assert rehearsal.like_pattern("a_b%c") == "%a\\_b\\%c%"
    # A missing sample is reported as not sampled, never silently dropped.
    missing = {**samples, "orphan": None, "present_single": None}
    assert len(rehearsal.filter_cases(missing)) == 15
    assert rehearsal.unsampled_filter_codes(missing) == [
        "orphan_single_equals",
        "orphan_single_target_id",
        "single_equals",
        "single_target_id",
    ]


def test_expected_expand_follows_pocketbase_0_22_34():
    existing = {"a", "b", "c"}
    # Existing targets only, served order, duplicates and "" dropped (NonzeroUniques).
    assert rehearsal.expected_expand_ids(["b", "x", "a", "b", ""], existing, multiple=True) == [
        "b",
        "a",
    ]
    assert rehearsal.expected_expand_ids(["x", "y"], existing, multiple=True) is None
    assert rehearsal.expected_expand_ids([], existing, multiple=True) is None
    assert rehearsal.expected_expand_ids("c", existing, multiple=False) == "c"
    assert rehearsal.expected_expand_ids("x", existing, multiple=False) is None
    assert rehearsal.expected_expand_ids("", existing, multiple=False) is None


def test_expand_problems():
    lists = [{"pdv": ["a", "b"], "expand": {"pdv": [{"id": "a"}, {"id": "b"}]}}]
    assert rehearsal.expand_problems(lists, "pdv", [["a", "b"]]) == []
    swapped = [{"pdv": ["a", "b"], "expand": {"pdv": [{"id": "b"}, {"id": "a"}]}}]
    assert rehearsal.expand_problems(swapped, "pdv", [["a", "b"]]) == [
        "expand_not_the_existing_targets_in_order"
    ]
    # The whole stored list is not what PocketBase expands when a target is missing.
    assert rehearsal.expand_problems(lists, "pdv", [["a"]]) == [
        "expand_not_the_existing_targets_in_order"
    ]
    assert rehearsal.expand_problems([{"type": "c"}], "type", ["c"]) == ["expand_not_one_object"]
    one = [{"customer": "c", "expand": {"customer": {"id": "c"}}}]
    assert rehearsal.expand_problems(one, "customer", ["c"]) == []
    assert rehearsal.expand_problems([{"customer": "o"}], "customer", [None]) == []
    assert rehearsal.expand_problems([{"customer": "o", "expand": {}}], "customer", [None]) == [
        "expand_key_without_related_record"
    ]
    assert rehearsal.expand_problems(
        [{"customer": "o", "expand": {"customer": {"id": "o"}}}], "customer", [None]
    ) == ["expand_served_for_absent_record"]
    assert rehearsal.expand_problems([], "pdv", []) == ["no_items"]
    assert rehearsal.expand_problems(lists, "pdv", []) == ["items_differ"]


def test_divergences_pass_only_when_accepted_by_a_decision():
    problems = {
        "anonymous_list:a": ["error_status_key_instead_of_code"],
        "superuser_list:b": ["empty_date_served_as_null:since", "total_pages_one_for_empty_list"],
        "anonymous_list:c": ["status:500"],
        "sort:d": [],
    }
    none = rehearsal.classify_problems(problems, {})
    assert sorted(none["failing"]) == ["anonymous_list:a", "anonymous_list:c", "superuser_list:b"]
    assert none["divergences"]["empty_date_served_as_null"]["checks"] == 1
    decisions = (
        "# Decisions\n\n## D-0031 — envelope\n\nCode `error_status_key_instead_of_code`.\n\n"
        "## D-0032 — dates\n\n`empty_date_served_as_null`, and\n"
        "`total_pages_one_for_empty_list`.\n\n## D-0033 — other\n\nNothing named.\n"
    )
    accepted = rehearsal.accepted_divergences(
        [
            "error_status_key_instead_of_code=D-0031",
            "empty_date_served_as_null=D-0032",
            "total_pages_one_for_empty_list=D-0032",
        ],
        decisions,
    )
    some = rehearsal.classify_problems(problems, accepted)
    assert some["failing"] == {"anonymous_list:c": ["status:500"]}
    assert some["divergences"]["error_status_key_instead_of_code"]["decision"] == "D-0031"

    def accept(value):
        return lambda: rehearsal.accepted_divergences([value], decisions)

    refused(accept("status=D-0031"), "unknown_divergence_code")
    refused(accept("message=D-0031"), "unknown_divergence_code")  # a message never passes
    refused(accept("total_pages_one_for_empty_list=yes"), "decision_reference_required")
    # The decision must be recorded, and must name the divergence.
    refused(accept("empty_date_served_as_null=D-9999"), "decision_not_recorded")
    refused(accept("empty_date_served_as_null=D-0033"), "decision_does_not_name_the_divergence")
    refused(accept("empty_date_served_as_null=D-0031"), "decision_does_not_name_the_divergence")


def passed_pass(index: int, seconds: float) -> dict:
    return {
        "pass": index,
        "status": "passed",
        "checks": {"rows": 3},
        "measures": {
            "phase_seconds": {"import": seconds, "diagnosis": 2.0},
            "completed_phases": ["diagnosis", "import"],
        },
    }


def test_repeatability_and_runbook_durations():
    passes = [passed_pass(1, 10.0), passed_pass(2, 12.0)]
    private = {"fingerprints": {"t": "1:ab"}, "row_hashes_after_import": {"t": "1:5"}}
    result = rehearsal.repeatability(passes, [private, json.loads(json.dumps(private))])
    assert result["identical"] is True
    other = {**private, "row_hashes_after_import": {"t": "1:6"}}
    assert rehearsal.repeatability(passes, [private, other])["identical"] is False
    assert rehearsal.repeatability(passes[:1], [private])["identical"] is None
    failed = [passed_pass(1, 10.0) | {"status": "failed"}, passed_pass(2, 12.0)]
    result = rehearsal.repeatability(failed, [private, private])
    assert result["identical"] is True and result["both_passed"] is False
    assert rehearsal.repeatability(passes, [{}, {}])["identical"] is False
    both = [{"pass": 1, "status": "passed"}, {"pass": 2, "status": "passed"}]
    assert rehearsal.runbook_durations(passes) == {
        "import": {"seconds": [10.0, 12.0], "passes": both, "max": 12.0},
        "diagnosis": {"seconds": [2.0, 2.0], "passes": both, "max": 2.0},
    }
    # A pass failed by a later check still times the phases it completed; a
    # phase that raised (partial time) is left out.
    failed_late = passed_pass(1, 10.0) | {"status": "failed"}
    failed_late["measures"] = {
        "phase_seconds": {"import": 10.0, "http_reads": 3.0},
        "completed_phases": ["import"],
    }
    assert rehearsal.runbook_durations([failed_late]) == {
        "import": {"seconds": [10.0], "passes": [{"pass": 1, "status": "failed"}], "max": 10.0}
    }


def test_observed_aggregates_have_the_shape_of_the_public_expectation():
    checks = {
        "row_counts": {"rows": 1, "tables": 72},
        "views": {"queried": 42},
        "preserved_anomalies": {
            key: 0
            for key in (
                "relations_checked",
                "dangling_references",
                "dangling_fields",
                "missing_file_references",
                "missing_file_fields",
                "manifest_storage_objects",
                "referenced_files",
                "present_referenced_files",
                "unreferenced_files",
            )
        },
        "multiple_relation_arrays": {
            "buma_wallet.managers": {"length_distribution": {}, "max_element_length": 0}
        },
        "accounts": {"users": {"records": 1}, "_superusers": {"imported": 1}},
        "tokens_and_settings": {"source_token_configs": 8},
    }
    observed = rehearsal.observed_aggregates(checks)
    assert set(observed) == set(rehearsal.SEPTEMBER_EXPECTED)


def test_private_directories_are_owner_only(tmp_path):
    old = os.umask(0o022)
    try:
        path = rehearsal.make_private_dir(tmp_path / "private")
    finally:
        os.umask(old)
    assert path.stat().st_mode & 0o777 == 0o700


def test_fetch_records_a_dropped_connection_as_a_status():
    import asyncio

    import httpx

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path.endswith("/drop"):
            raise httpx.ReadError("connection closed", request=request)
        if request.url.path.endswith("/text"):
            return httpx.Response(500, text="Internal Server Error")
        assert request.headers.get("Authorization") == "token-value"
        assert request.url.params["perPage"] == "1"
        return httpx.Response(200, json={"items": []})

    async def run():
        transport = httpx.MockTransport(handler)
        async with httpx.AsyncClient(base_url="http://test", transport=transport) as client:
            return (
                await rehearsal.fetch(client, "/drop"),
                await rehearsal.fetch(client, "/text"),
                await rehearsal.fetch(client, "/ok", "token-value", perPage=1),
            )

    dropped, text, ok = asyncio.run(run())
    assert dropped == ("transport_error:ReadError", None)
    assert text == (500, None)
    assert ok == (200, {"items": []})


def test_independent_audit_must_recount_the_diagnosis():
    diagnosis = {
        "relations_checked": 10,
        "dangling_references": 2,
        "missing_file_references": 1,
    }
    audit = {
        "relations": {"references_checked": 10, "missing_references": 2},
        "missing_file_references": 1,
        "source_unchanged": True,
    }
    assert rehearsal.audit_agreement(audit, diagnosis)["agrees_with_diagnosis"] is True
    other = {**audit, "relations": {"references_checked": 10, "missing_references": 3}}
    refused(
        lambda: rehearsal.audit_agreement(other, diagnosis),
        "independent_audit_disagrees_with_diagnosis",
    )


# --- totals, rules and served values as PocketBase 0.22.34 computes them -------


def test_view_total_items_are_its_distinct_ids():
    # PocketBase 0.22.34: COUNT(DISTINCT id) (tools/search/provider.go:238-254).
    pocketbase = envelope(42, 1)
    assert rehearsal.listing_problems(200, pocketbase, 42, rows=989) == []
    # A COUNT(*) total is the named divergence, and fails unless a decision accepts it.
    count_star = envelope(989, 1)
    assert rehearsal.listing_problems(200, count_star, 42, rows=989) == [
        "view_total_items_counts_rows_not_distinct_ids"
    ]
    assert rehearsal.classify_problems(
        {"v": ["view_total_items_counts_rows_not_distinct_ids"]}, {}
    )["failing"] == {"v": ["view_total_items_counts_rows_not_distinct_ids"]}
    # Any other total stays a plain mismatch; the reviewer's case: 42 expected, not 989.
    assert rehearsal.listing_problems(200, envelope(40, 1), 42, rows=989) == [
        "total_items_mismatch"
    ]
    assert rehearsal.listing_problems(200, pocketbase, 989) == ["total_items_mismatch"]
    assert rehearsal.listing_problems(403, None, 42) == ["status:403"]


def test_view_counts_are_compared_with_the_sqlite_source():
    source = {"v1": {"rows": 989, "distinct_ids": 42}, "v2": {"rows": 3, "distinct_ids": 3}}
    assert rehearsal.view_count_differences(json.loads(json.dumps(source)), source) == []
    target = {"v1": {"rows": 989, "distinct_ids": 41}, "v2": {"rows": 3, "distinct_ids": 3}}
    assert rehearsal.view_count_differences(target, source) == ["v1"]
    assert rehearsal.view_count_differences({"v2": source["v2"]}, source) == ["v1"]
    # The SQLite side also counts its non-text IDs: not a PostgreSQL count.
    typed = {name: {**entry, "non_text_ids": 0} for name, entry in source.items()}
    assert rehearsal.view_count_differences(json.loads(json.dumps(source)), typed) == []


def test_views_compared_record_by_record_only_with_distinct_text_ids():
    assert rehearsal.view_not_compared({"rows": 3, "distinct_ids": 3, "non_text_ids": 0}) is None
    duplicated = {"rows": 989, "distinct_ids": 42, "non_text_ids": 0}
    assert rehearsal.view_not_compared(duplicated) == "duplicate_or_null_ids"
    typed = {"rows": 3, "distinct_ids": 3, "non_text_ids": 3}
    assert rehearsal.view_not_compared(typed) == "non_text_ids"


def view_with(query: str) -> dict:
    return {"name": "v", "type": "view", "schema": [], "options": {"query": query}}


def test_row_number_views_are_detected_from_their_query():
    for query in (
        "SELECT (ROW_NUMBER() OVER()) as id, b.amount FROM b",
        "select\n  (ROW_NUMBER() OVER ()) AS id,\n count(*) as n from r",
        "SELECT COUNT(*) as n, (ROW_NUMBER() OVER()) as id;",
        "SELECT ROW_NUMBER() OVER () AS `id`, x FROM t",
    ):
        assert rehearsal.row_number_ids(view_with(query)), query
    for query in (
        "SELECT bc.id, bc.created FROM buma_customer bc",
        "SELECT ROW_NUMBER() OVER (ORDER BY created) AS id FROM t",  # a defined order
        "SELECT (ROW_NUMBER() OVER()) as rank, t.id FROM t",
    ):
        assert not rehearsal.row_number_ids(view_with(query)), query
    assert not rehearsal.row_number_ids({"name": "t", "type": "base", "schema": []})


ROW_VIEW = {
    "id": "v2v2v2v2v2v2v2v",
    "name": "a_row_number_view",
    "type": "view",
    "schema": [
        {"name": "caisse", "type": "relation", "options": {"maxSelect": 1}},
        {"name": "total", "type": "json", "options": {}},
    ],
}


def served_row(record_id: str, caisse: str, total) -> dict:
    return {
        "id": record_id,
        "collectionId": ROW_VIEW["id"],
        "collectionName": ROW_VIEW["name"],
        "caisse": caisse,
        "total": total,
    }


def test_row_number_views_compare_as_multisets_ids_aside():
    raw = [{"id": "1", "caisse": "c1", "total": 5}, {"id": "2", "caisse": "c2", "total": 7.5}]
    expected = [rehearsal.expected_record(ROW_VIEW, row, email_visible=True) for row in raw]
    # PostgreSQL numbered the rows the other way round: same multiset, same IDs.
    swapped = [served_row("1", "c2", 7.5), served_row("2", "c1", 5.0)]
    problems, counts, samples = rehearsal.multiset_comparison(expected, swapped)
    assert problems == [] and counts["rows_only_in_source"] == counts["rows_only_served"] == 0
    assert counts == {
        "rows": 2,
        "served": 2,
        "rows_only_in_source": 0,
        "rows_only_served": 0,
        "rows_served_more_often_than_in_source": 0,
        "source_rows_never_served": 0,
        "rows_explained_by_one_key": {},
        "rows_differing_in_several_keys": 0,
        "unmodeled_keys": [],
    }
    # A changed value or a boolean for a number: one key explains the row.
    for served in (
        [served_row("1", "c2", 7.5), served_row("2", "c1", 6)],
        [served_row("1", "c2", 7.5), served_row("2", "c1", True)],
    ):
        problems, counts, samples = rehearsal.multiset_comparison(expected, served)
        assert problems == ["value_differs:total"]
        assert counts["rows_explained_by_one_key"] == {"total:differs": 1}
        assert samples["only_in_source"]  # private evidence, never in the report
    # A missing or duplicated row: nothing explains it.
    for served in (
        [served_row("1", "c2", 7.5)],
        [served_row("1", "c2", 7.5), served_row("2", "c2", 7.5)],
    ):
        problems, counts, samples = rehearsal.multiset_comparison(expected, served)
        assert "rows_differ_from_sqlite_ids_aside" in problems
        assert counts["rows_only_in_source"] == counts["rows_differing_in_several_keys"] == 1
        assert counts["source_rows_never_served"] == 1
    # Paging that numbers the rows again: one row served twice, another never.
    _problems, counts, _samples = rehearsal.multiset_comparison(
        expected, [served_row("1", "c2", 7.5), served_row("2", "c2", 7.5)]
    )
    assert counts["rows_served_more_often_than_in_source"] == 1
    # A SQL NULL served as null where PocketBase serves "": the named divergence.
    raw_null = [{"id": "1", "caisse": None, "total": 5}, {"id": "2", "caisse": "c2", "total": 7.5}]
    expected_null = [rehearsal.expected_record(ROW_VIEW, r, email_visible=True) for r in raw_null]
    problems, counts, _samples = rehearsal.multiset_comparison(
        expected_null, [served_row("1", "c2", 7.5), served_row("2", None, 5)]
    )
    assert problems == ["null_served_for_pocketbase_zero_value:caisse"]
    assert counts["rows_explained_by_one_key"] == {"caisse:null_for_zero_value": 1}
    # Same rows, other IDs (a page served twice, another skipped).
    problems, _counts, _samples = rehearsal.multiset_comparison(
        expected, [served_row("1", "c2", 7.5), served_row("3", "c1", 5)]
    )
    assert problems == ["ids_differ_from_sqlite"]
    # A key unmodeled in any row is left out on both sides, and named.
    raw_bytes = [{"id": "1", "caisse": "c1", "total": b"\x00"}]
    expected = [rehearsal.expected_record(ROW_VIEW, row, email_visible=True) for row in raw_bytes]
    problems, counts, _samples = rehearsal.multiset_comparison(
        expected, [served_row("1", "c1", "anything")]
    )
    assert problems == [] and counts["unmodeled_keys"] == ["total"]


# The rule shapes of the corrected export (copied, no data).
NEVER_TRUE_RULES = [
    "@request.auth.id != ''",
    "  @request.auth.id != ''",
    "@request.auth.id  != ''\n",
    '@request.auth.id != ""',
    "@request.auth.id !=''",
    "@request.auth.id != '' && @request.auth.id != null",
    "@request.auth.role.permission.isAdmin = true",
    "@request.auth.id != '' && (@request.auth.role.permission.isAdmin = true || "
    "@request.auth.role.permission.caisse.read = true)",
    "@request.auth.id != '' && @request.auth.agent = agent || "
    "@request.auth.role.permission.isAdmin = true || @request.auth.role.permission.pdv.read = true",
    "@request.auth.id != '' && (@request.auth.role.permission.isAdmin = true || "
    "@request.auth.role.permission.wallet.read = true || managers ~ @request.auth.id  ) ",
    "@request.auth.id != '' && (@request.auth.role.permission.isAdmin = true || "
    "@request.auth.id ?= managers) ",
    "@request.auth.id != '' && @request.auth.agent = agent || @request.auth.role.permission"
    ".isAdmin = true  || @request.auth.role.permission.pdv.read = true || "
    "(@request.auth.agent != '' && agent = '')",
    "@request.auth.created > '2020-01-01'",
    "name ~ @request.auth.username",
]


@pytest.mark.parametrize("rule", NEVER_TRUE_RULES)
def test_rules_that_no_record_matches_without_auth(rule):
    assert rehearsal.anonymous_rule_outcome(rule, ids_never_empty=True) == "never_true"


@pytest.mark.parametrize(
    "rule",
    [
        "@request.auth.id = ''",  # '' = '' is true without auth
        "@request.auth.id = @request.auth.username",
        "@request.auth.id != 'x'",  # '' IS NOT 'x' is true
        "owner = @request.auth.id",  # ('' = owner OR owner IS NULL): data dependent
        "@request.auth.id ?= managers",  # LEFT JOIN on an empty list: data dependent
        "@request.auth.id != '' || public = true",
        "@request.auth.id:isset = true",
        "@collection.users.id ?= @request.auth.id",
        "@request.auth.id != '' &&",
        "(@request.auth.id != ''",
        "@request.auth.id != '' )",
        "// comment",
    ],
)
def test_rules_left_undetermined(rule):
    assert rehearsal.anonymous_rule_outcome(rule, ids_never_empty=True) == "undetermined"


def test_record_id_rule_depends_on_non_empty_ids():
    rule = "@request.auth.id = id"
    assert rehearsal.anonymous_rule_outcome(rule, ids_never_empty=True) == "never_true"
    assert rehearsal.anonymous_rule_outcome(rule, ids_never_empty=False) == "undetermined"


def test_every_expression_rule_of_the_export_is_never_true_without_auth():
    export = os.environ.get("BUMA_SCHEMA_EXPORT")
    if not export or not Path(export).is_file():
        pytest.skip("BUMA_SCHEMA_EXPORT is not set")
    definitions = importer.load_schema(Path(export))
    outcomes = {
        (c["name"], key): rehearsal.anonymous_rule_outcome(
            c[key], ids_never_empty=c["type"] != "view"
        )
        for c in definitions
        for key in ("listRule", "viewRule")
        if c.get(key)
    }
    assert len([k for k in outcomes if k[1] == "listRule"]) == 47
    assert set(outcomes.values()) == {"never_true"}


def test_an_anonymous_rule_leak_fails_the_list():
    # Expected 0 for a rule no record matches without auth: a leak is a mismatch.
    leaked = envelope(5826, 1)
    assert rehearsal.listing_problems(200, leaked, 0) == ["total_items_mismatch"]
    assert rehearsal.listing_problems(200, envelope(0, 0), 0) == []


def field(name, kind, **options):
    return {"name": name, "type": kind, "options": options}


def test_pocketbase_serialization_model():
    served = rehearsal.pb_served
    assert served(field("t", "text"), None) == ""
    assert served(field("t", "editor"), "<p>x</p>") == "<p>x</p>"
    assert served(field("n", "number"), 3) == 3.0
    assert served(field("n", "number"), None) == 0.0
    assert served(field("n", "number"), "12.5") == 12.5
    assert served(field("b", "bool"), 1) is True
    assert served(field("b", "bool"), 0) is False
    assert served(field("b", "bool"), 2) is False  # "2" does not parse as a bool
    assert served(field("b", "bool"), "true") is True
    assert served(field("b", "bool"), None) is False
    assert served(field("d", "date"), "") == ""
    assert served(field("d", "date"), None) == ""
    assert served(field("d", "date"), "2026-01-02 03:04:05.678Z") == "2026-01-02 03:04:05.678Z"
    assert served(field("j", "json"), None) is None
    assert served(field("j", "json"), '{"b": 1, "a": [true]}') == {"b": 1, "a": [True]}
    assert served(field("j", "json"), "") == ""
    assert served(field("j", "json"), "plain") == "plain"  # quoted by PocketBase
    assert served(field("j", "json"), "[1,") == "[1,"  # invalid JSON: quoted
    assert served(field("j", "json"), 7) == 7
    many = field("r", "relation", maxSelect=None)
    assert served(many, '["a","","b","a"]') == ["a", "b"]  # NonzeroUniques
    assert served(many, "") == []
    assert served(many, "a") == ["a"]
    one = field("r", "relation", maxSelect=1)
    assert served(one, '["a","b"]') == "b"  # the last element
    assert served(one, "a") == "a"
    assert served(one, "") == ""
    assert served(field("s", "select", maxSelect=2), '["x"]') == ["x"]
    assert served(field("s", "select", maxSelect=1), "x") == "x"
    assert served(field("f", "file"), "photo.png") == "photo.png"  # maxSelect absent: single
    for kind, raw in (
        ("date", "2026-01-02"),
        ("date", "2026-02-30 00:00:00.000Z"),
        ("number", "1_0"),
        ("text", 1.5),
        ("bool", b"1"),
        ("relation", '["a", 1]'),
    ):
        with pytest.raises(rehearsal.Unmodeled):
            served(field("x", kind, maxSelect=None), raw)


def test_is_multiple_follows_0_22_34():
    assert rehearsal.pb_is_multiple(field("r", "relation", maxSelect=None))
    assert rehearsal.pb_is_multiple({"name": "r", "type": "relation", "options": {}})
    assert rehearsal.pb_is_multiple(field("r", "relation", maxSelect=2))
    assert not rehearsal.pb_is_multiple(field("r", "relation", maxSelect=1))
    assert not rehearsal.pb_is_multiple(field("s", "select"))
    assert rehearsal.pb_is_multiple(field("f", "file", maxSelect=99))
    assert not rehearsal.pb_is_multiple(field("t", "text"))


def test_same_json_is_strict_about_types_not_key_order():
    assert rehearsal.same_json({"a": 1, "b": [1.0]}, {"b": [1], "a": 1.0})
    assert not rehearsal.same_json(True, 1)
    assert not rehearsal.same_json(None, "")
    assert not rehearsal.same_json([1, 2], [2, 1])
    assert not rehearsal.same_json(2**63, 2**63 + 1)
    assert rehearsal.same_key_order({"a": {"x": 1, "y": 2}}, {"a": {"x": 1, "y": 2}})
    assert not rehearsal.same_key_order({"a": 1, "b": 2}, {"b": 2, "a": 1})


VIEW = {
    "id": "v1v1v1v1v1v1v1v",
    "name": "a_view",
    "type": "view",
    "schema": [{"name": "total", "type": "number", "options": {}}],
}


def raw_user(**extra) -> dict:
    return {
        "id": "u1",
        "created": "2026-01-01 00:00:00.000Z",
        "updated": "2026-01-01 00:00:00.000Z",
        "username": "u",
        "email": "e",
        "emailVisibility": 1,
        "verified": 0,
        "passwordHash": "never served",
        "tokenKey": "never served",
        "name": "",
        "role": "",
        "set": 0,
        "more": None,
        **extra,
    }


def test_served_record_against_the_raw_source_row():
    compare = rehearsal.compare_served_record
    outcomes = compare(USERS, served_user(), raw_user(), email_visible=True)
    assert set(outcomes.values()) == {"equal"}
    assert "email" in outcomes and "passwordHash" not in outcomes
    hidden = served_user()
    del hidden["email"]
    assert set(compare(USERS, hidden, raw_user(), email_visible=False).values()) == {"equal"}
    assert compare(USERS, served_user(), raw_user(), email_visible=False)["email"] == (
        "unexpected_key"
    )
    changed = compare(USERS, served_user(name="other"), raw_user(), email_visible=True)
    assert changed["name"] == "differs"
    reordered = compare(
        USERS,
        served_user(more={"b": 1, "a": 2}),
        raw_user(more='{"a": 2, "b": 1}'),
        email_visible=True,
    )
    assert reordered["more"] == "json_key_order"
    assert rehearsal.value_problems(reordered) == ["jsonb_key_order_normalized:more"]
    assert rehearsal.value_problems(changed) == ["value_differs:name"]
    unmodeled = compare(USERS, served_user(), raw_user(created="2026-01-01"), email_visible=True)
    assert unmodeled["created"] == "unmodeled"
    assert rehearsal.value_problems(unmodeled) == []
    # A view omits a zero created/updated (models/record.go:544-550).
    row = {"id": "x", "total": 2}
    view_record = {"id": "x", "collectionId": VIEW["id"], "collectionName": "a_view", "total": 2}
    assert set(compare(VIEW, view_record, row, email_visible=True).values()) == {"equal"}
    served_created = view_record | {"created": ""}
    assert compare(VIEW, served_created, row, email_visible=True)["created"] == "unexpected_key"


DATED = {
    "id": "d1d1d1d1d1d1d1d",
    "name": "dated",
    "type": "base",
    "schema": [{"name": "due", "type": "date", "options": {}}],
}


def test_a_null_date_is_the_named_divergence_only_for_an_empty_source_date():
    served = {
        "id": "r1",
        "collectionId": DATED["id"],
        "collectionName": "dated",
        "created": "2026-01-01 00:00:00.000Z",
        "updated": "2026-01-01 00:00:00.000Z",
        "due": None,
    }
    raw = {"id": "r1", "created": served["created"], "updated": served["updated"], "due": ""}
    outcomes = rehearsal.compare_served_record(DATED, served, raw, email_visible=True)
    assert outcomes["due"] == "empty_date_null"
    assert rehearsal.value_problems(outcomes) == ["empty_date_served_as_null:due"]
    # A lost non-empty date is a plain difference, never the accepted divergence.
    lost = rehearsal.compare_served_record(
        DATED, served, raw | {"due": "2026-03-04 05:06:07.089Z"}, email_visible=True
    )
    assert rehearsal.value_problems(lost) == ["value_differs:due"]
    values = rehearsal.ServedValues()
    values.add(DATED, outcomes)
    assert values.summary()["named_differences"] == {"empty_date_served_as_null": {"dated.due": 1}}


def test_an_out_of_range_number_text_is_not_modeled():
    with pytest.raises(rehearsal.Unmodeled):
        rehearsal.pb_served(field("n", "number"), "1e400")  # ParseFloat fails: cast gives 0
    assert rehearsal.pb_served(field("n", "number"), "1e300") == 1e300


def test_mismatches_are_kept_privately_and_capped():
    reads = rehearsal.HttpReads.__new__(rehearsal.HttpReads)
    reads.mismatches = {}
    for index in range(rehearsal.MISMATCH_SAMPLES + 3):
        codes = reads.compare(
            USERS, served_user(name=f"served-{index}"), raw_user(name="stored"), email_visible=True
        )
        assert codes == ["value_differs:name"]
    kept = reads.mismatches["users.name"]
    assert len(kept) == rehearsal.MISMATCH_SAMPLES
    assert kept[0] == {
        "record": "u1",
        "outcome": "differs",
        "expected": "stored",
        "served": "served-0",
    }
    assert list(reads.mismatches) == ["users.name"]  # equal keys are never kept


def test_served_values_summary_holds_counts_and_names_only():
    values = rehearsal.ServedValues()
    values.add(USERS, {"id": "equal", "name": "differs", "more": "json_key_order"})
    values.add(USERS, {"id": "equal", "created": "unmodeled"})
    summary = values.summary()
    assert summary["records_compared"] == {"auth": 2}
    assert summary["values"] == {"differs": 1, "equal": 2, "json_key_order": 1, "unmodeled": 1}
    assert summary["differing_fields"] == {"users.name": 1}
    assert summary["named_differences"] == {"jsonb_key_order_normalized": {"users.more": 1}}
    assert summary["unmodeled_fields"] == {"users.created": 1}
    assert summary["values_by_field_type"]["text"] == {"differs": 1}


# --- post-commit and after-reads comparisons detect a loss --------------------


def test_account_comparison_detects_a_changed_hash_or_a_missing_record():
    source = {
        "u1": ("user", "e", True, False, "$hash1", "key1"),
        "u2": ("b", "f", False, 1, "h", "k"),
    }
    assert rehearsal.mismatched_records(source, dict(source)) == 0
    assert rehearsal.mismatched_records(source, {**source, "u1": source["u1"][:4] + ("x", "key1")})
    assert rehearsal.mismatched_records(source, {"u1": source["u1"]}) == 1
    assert rehearsal.mismatched_records(source, {**source, "u3": source["u1"]}) == 1


def test_array_comparison_detects_order_and_rows():
    source = {"c1": ["a", "b"], "c2": []}
    assert rehearsal.array_mismatches(source, {"c1": ["a", "b"], "c2": []}) == {
        "rows_missing_or_extra": 0,
        "rows_differing": 0,
    }
    assert rehearsal.array_mismatches(source, {"c1": ["b", "a"], "c2": []})["rows_differing"] == 1
    assert rehearsal.array_mismatches(source, {"c1": ["a", "b"]})["rows_missing_or_extra"] == 1


def test_token_and_settings_comparisons_detect_a_changed_secret():
    settings = {
        "recordAuthToken": {"secret": "s" * 30, "duration": 1209600},
        "adminAuthToken": {"secret": "t" * 30, "duration": 86400},
        "meta": {"appName": "App", "appUrl": "https://example.test/"},
    }
    plan = [
        ("users", {"authToken": "recordAuthToken"}),
        ("_superusers", {"authToken": "adminAuthToken"}),
    ]
    options = {
        "users": {"authToken": {"secret": "s" * 30, "duration": 1209600}},
        "_superusers": {"authToken": {"secret": "t" * 30, "duration": 86400}},
    }
    configs, failures = rehearsal.token_config_report(settings, options, plan)
    assert failures == [] and configs["users.authToken"]["secret_equal"] is True
    changed = json.loads(json.dumps(options))
    changed["users"]["authToken"]["secret"] = "x" * 30
    assert rehearsal.token_config_report(settings, changed, plan)[1] == ["users.authToken"]
    mapped, unequal = rehearsal.mapped_settings_report(
        settings, {"meta": {"appName": "App", "appURL": "https://example.test"}}
    )
    assert unequal == [] and mapped["meta.appUrl"] == {"present": True, "equal": True}
    assert rehearsal.mapped_settings_report(settings, {"meta": {"appName": "Other"}})[1] == [
        "meta.appName",
        "meta.appUrl",
    ]


def test_fingerprint_comparison_detects_a_changed_table():
    loaded = {"t": {"source_fingerprint": "1:ab", "target_fingerprint": "1:ab"}}
    assert rehearsal.fingerprint_mismatches({"t": {"fingerprint": "1:ab"}}, loaded) == []
    assert rehearsal.fingerprint_mismatches({"t": {"fingerprint": "1:ac"}}, loaded) == ["t"]
    assert rehearsal.fingerprint_mismatches({}, loaded) == ["t"]
    other = {"t": {"source_fingerprint": "1:ab", "target_fingerprint": "1:ff"}}
    assert rehearsal.fingerprint_mismatches({"t": {"fingerprint": "1:ab"}}, other) == ["t"]


def test_after_reads_outcome_detects_a_changed_imported_admin():
    before = {"users": "20:5", "_superusers": "1:7"}
    signature = {"only_import_archives_added": True}
    clean = rehearsal.after_reads_outcome(
        before, dict(before), superusers=2, admins=1, accounts=True, signature=signature
    )
    assert clean == {
        "tables_changed_by_server_and_reads": [],
        "imported_superusers_unchanged": True,
        "superusers_are_source_admins_plus_synthetic": True,
        "accounts_equal_to_source_after_reads": True,
        "schema_unchanged_by_server": True,
    }
    changed = rehearsal.after_reads_outcome(
        before,
        {**before, "_superusers": "1:8"},
        superusers=2,
        admins=1,
        accounts="superusers_differ_from_source_admins",
        signature=signature,
    )
    assert changed["tables_changed_by_server_and_reads"] == ["_superusers"]
    assert changed["imported_superusers_unchanged"] is False
    assert changed["accounts_equal_to_source_after_reads"] is False


class FakeConnection:
    pass


class FakeEngine:
    def __init__(self):
        self.disposed = False

    def connect(self):
        engine = self

        class Context:
            async def __aenter__(self):
                return FakeConnection()

            async def __aexit__(self, *exc):
                return False

        self.last = engine
        return Context()

    async def dispose(self):
        self.disposed = True


def test_check_accounts_detects_changes_and_leaves_the_synthetic_superuser_out(monkeypatch):
    import asyncio

    facts = {
        "auth": {"users": {"u1": ("u", "e", True, False, "$h", "k")}},
        "admins": {"a1": ("boss@example.org", "$a", "ka", 0)},
    }
    tables = {
        "users": [
            {
                "id": "u1",
                "username": "u",
                "email": "e",
                "email_visibility": True,
                "verified": False,
                "password_hash": "$h",
                "token_key": "k",
            }
        ],
        "_superusers": [
            {
                "id": "a1",
                "email": "boss@example.org",
                "password_hash": "$a",
                "token_key": "ka",
                "avatar": 0,
            },
            {
                "id": "syn",
                "email": "ws-g@example.com",
                "password_hash": "$s",
                "token_key": "s",
                "avatar": 0,
            },
        ],
    }

    async def rows_of(_conn, sql, _params=None):
        return [
            dict(row)
            for row in (tables["_superusers"] if "_superusers" in sql else tables["users"])
        ]

    monkeypatch.setattr(rehearsal, "rows_of", rows_of)
    result = asyncio.run(rehearsal.check_accounts(FakeEngine(), facts, synthetic_superuser="syn"))
    assert result["_superusers"]["imported"] == 1
    # Without leaving the synthetic one out, the admins differ.
    with pytest.raises(rehearsal.RehearsalFailure) as caught:
        asyncio.run(rehearsal.check_accounts(FakeEngine(), facts))
    assert caught.value.code == "superusers_differ_from_source_admins"
    tables["users"][0]["password_hash"] = "$changed"
    with pytest.raises(rehearsal.RehearsalFailure) as caught:
        asyncio.run(rehearsal.check_accounts(FakeEngine(), facts, synthetic_superuser="syn"))
    assert caught.value.code == "auth_records_differ"
    assert "$changed" not in json.dumps(caught.value.details)


# --- container and pass cleanup ------------------------------------------------


class DockerCalls:
    """A fake ``command``: records docker calls; ``fail`` maps a subcommand to an error."""

    def __init__(self, *, fail=None, listed=("c0ffee",)):
        self.calls, self.fail, self.listed, self.removed = [], fail or {}, list(listed), False

    def __call__(self, *args, timeout=300):
        args = [str(a) for a in args]
        self.calls.append(args)
        if args[1] in self.fail:
            raise self.fail[args[1]]
        if args[1] == "ps":
            if "--filter" in args and any(a.startswith("label=") for a in args):
                return "" if self.removed else "\n".join(self.listed)
            return "" if self.removed else "\n".join(self.listed)
        if args[1] == "rm":
            self.removed = True
        return ""


def test_a_timed_out_docker_run_removes_the_container_it_may_have_created(tmp_path, monkeypatch):
    docker = DockerCalls(fail={"run": subprocess.TimeoutExpired("docker run", 300)})
    monkeypatch.setattr(rehearsal, "command", docker)
    container = rehearsal.Container(RUN_ID, "1g", "1", "64m", tmp_path)
    with pytest.raises(subprocess.TimeoutExpired):
        container.start()
    assert not (tmp_path / "postgres.env").exists()  # the password file is gone
    listing = next(call for call in docker.calls if call[1] == "ps")
    assert f"name=^/{rehearsal.CONTAINER_PREFIX}{RUN_ID}$" in listing
    assert f"label=buma.run={RUN_ID}" in listing
    assert f"label=buma.scope={rehearsal.SCOPE_LABEL}" in listing
    assert ["docker", "rm", "--force", "--volumes", "c0ffee"] in docker.calls
    assert container.attempted and not container.created and container.forced_removal


def test_a_failed_docker_stop_falls_back_to_a_forced_removal(tmp_path, monkeypatch):
    docker = DockerCalls(fail={"stop": subprocess.CalledProcessError(1, "docker stop")})
    monkeypatch.setattr(rehearsal, "command", docker)
    container = rehearsal.Container(RUN_ID, "1g", "1", "64m", tmp_path)
    container.attempted = container.created = True
    container.stop()
    assert ["docker", "rm", "--force", "--volumes", "c0ffee"] in docker.calls
    assert container.removal_evidence()["forced_removal"] is True


def test_a_container_never_listed_is_not_removed(tmp_path, monkeypatch):
    docker = DockerCalls(listed=())
    monkeypatch.setattr(rehearsal, "command", docker)
    container = rehearsal.Container(RUN_ID, "1g", "1", "64m", tmp_path)
    container.stop()  # never attempted: nothing at all
    assert docker.calls == []
    container.attempted = True
    container.stop()
    assert not any(call[1] == "rm" for call in docker.calls)


class FakeContainer:
    instances: list = []

    def __init__(self, run_id, memory, cpus, shm, workdir):
        self.run_id, self.workdir = run_id, workdir
        self.name = rehearsal.CONTAINER_PREFIX + run_id
        self.id = "f" * 64
        self.url = f"postgresql+asyncpg://c11:x@127.0.0.1:54321/{rehearsal.DATABASE}"
        self.attempted = self.created = False
        self.stopped = False
        self.stop_error = None
        FakeContainer.instances.append(self)

    def start(self):
        self.attempted = self.created = True
        return {"name": self.name, "port": 54321}

    def libc(self):
        return "musl"

    def stop(self):
        self.stopped = True
        if self.stop_error:
            raise self.stop_error

    def removal_evidence(self):
        return {"container_removed": self.stopped, "anonymous_volumes_left": 0}


def failing_pass_context(tmp_path, monkeypatch, *, stop_error=None):
    FakeContainer.instances = []

    def container(*args):
        made = FakeContainer(*args)
        made.stop_error = stop_error
        return made

    async def database_facts(_engine):
        return {"datcollate": "en_US.utf8", "datctype": "en_US.utf8", "provider": "c"}

    async def prepare_target(_engine, _url, _schema, _definitions):
        # The pass directory holds a leftover staging copy when the step fails.
        (FakeContainer.instances[-1].workdir / "staging" / "storage").mkdir(parents=True)
        raise rehearsal.RehearsalFailure("migrations_not_all_applied", applied=3)

    monkeypatch.setattr(rehearsal, "Container", container)
    monkeypatch.setattr(rehearsal, "make_engine", lambda _url: FakeEngine())
    monkeypatch.setattr(rehearsal, "database_facts", database_facts)
    monkeypatch.setattr(rehearsal, "prepare_target", prepare_target)
    private = tmp_path / "private"
    private.mkdir(mode=0o700)
    args = SimpleNamespace(memory="1g", cpus="1", shm_size="64m", source_version="0.22.34")
    return SimpleNamespace(
        args=args,
        private_dir=private,
        docker_root=tmp_path,
        schema=tmp_path / "schema.json",
        definitions=[],
        url="",
        collation=None,
    )


def test_a_failed_step_still_stops_the_container_and_removes_the_staging(tmp_path, monkeypatch):
    import asyncio

    ctx = failing_pass_context(tmp_path, monkeypatch)
    summary, private = asyncio.run(rehearsal.run_pass(ctx, 1))
    container = FakeContainer.instances[-1]
    assert summary["status"] == "failed"
    assert summary["failure"] == {
        "type": "RehearsalFailure",
        "code": "migrations_not_all_applied",
        "details": {"applied": 3},
    }
    assert container.stopped
    assert not (ctx.private_dir / "pass-1" / "staging").exists()
    assert summary["cleanup"]["staging"] == "done" and summary["cleanup"]["container"] == "done"
    assert summary["cleanup"]["staging_removed"] is True
    assert summary["measures"]["completed_phases"] == ["container_start"]
    assert "migrations_schema_and_target_description" in summary["measures"]["phase_seconds"]
    assert private == {}


def test_a_failed_container_stop_fails_the_pass_and_keeps_the_first_failure(tmp_path, monkeypatch):
    import asyncio

    ctx = failing_pass_context(tmp_path, monkeypatch, stop_error=OSError("daemon gone"))
    summary, _private = asyncio.run(rehearsal.run_pass(ctx, 1))
    assert summary["status"] == "failed"
    assert summary["failure"]["code"] == "migrations_not_all_applied"
    assert summary["cleanup"]["container"] == "failed:OSError"
    assert not (ctx.private_dir / "pass-1" / "staging").exists()
    assert "daemon gone" not in json.dumps(summary)


# --- report: collation, claims ---------------------------------------------------


def test_target_collation_names_the_byte_order_of_musl():
    musl = rehearsal.target_collation({"datcollate": "en_US.utf8", "provider": "c"}, "musl")
    assert musl == {
        "datcollate": "en_US.utf8",
        "provider": "libc",
        "image_libc": "musl",
        "text_order": "byte order, like SQLite BINARY",
    }
    glibc = rehearsal.target_collation({"datcollate": "en_US.utf8", "provider": "c"}, None)
    assert glibc["text_order"] == "linguistic"


def test_glibc_order_difference_counts_positions_only():
    import locale

    named = sorted([("i1", "b"), ("i2", "B"), ("i3", "a"), ("i4", "A")], key=lambda x: x[1])
    result = rehearsal.glibc_order_difference(named)
    if not result["available"]:
        pytest.skip("en_US.utf8 is not available on this host")
    assert result["rows"] == 4
    # Byte order puts capitals first (A B a b); glibc en_US interleaves them.
    assert result["positions_differing_from_binary_order"] > 0
    assert "b" not in json.dumps({k: v for k, v in result.items() if k != "libc"}).split('"')
    assert locale.setlocale(locale.LC_COLLATE) in ("C", "C.UTF-8", "C.utf8", "POSIX")


def test_proven_claims_list_only_what_every_pass_checked():
    everything = {key: {} for claim in rehearsal.CLAIMS for key in claim[1]}
    everything["http"] = {"failed_checks": {}, "served_values": {"differing_fields": {}}}
    report = {
        "passes": [{"pass": 1, "checks": everything}, {"pass": 2, "checks": everything}],
        "repeatability": {"identical": True},
    }
    proven, missing = rehearsal.proven_claims(report, 2)
    assert len(proven) == len(rehearsal.CLAIMS) + 1 and missing == []
    failing = dict(everything)
    failing["http"] = {
        "failed_checks": {"anonymous_list:x": ["message"]},
        "served_values": {"differing_fields": {}},
    }
    report["passes"][1] = {"pass": 2, "checks": failing}
    report["repeatability"] = {"identical": False}
    proven, missing = rehearsal.proven_claims(report, 2)
    assert len(proven) == len(rehearsal.CLAIMS) - 1  # the HTTP parity claim is not proven
    reasons = {entry["claim"][:20]: entry["reasons"] for entry in missing}
    assert ["pass_2_has_failing_checks"] in reasons.values()
    assert ["repeatability_not_identical"] in reasons.values()
    truncated = {"import": {}, "row_counts": {}}
    report["passes"] = [{"pass": 1, "checks": truncated}]
    proven, missing = rehearsal.proven_claims(report, 2)
    assert proven == []


def test_a_rule_left_unverified_keeps_the_http_claim_open():
    everything = {key: {} for claim in rehearsal.CLAIMS for key in claim[1]}
    everything["http"] = {
        "failed_checks": {},
        "served_values": {"differing_fields": {}},
        "anonymous_lists": {"expression_rules_unverified": ["buma_x"]},
        "anonymous_views": {"expression_rules_unverified": []},
    }
    assert rehearsal.unverified_rules(everything["http"]) == ["anonymous_lists:buma_x"]
    report = {"passes": [{"pass": 1, "checks": everything}], "repeatability": {}}
    proven, missing = rehearsal.proven_claims(report, 1)
    http_claim = next(c[0] for c in rehearsal.CLAIMS if c[2] is rehearsal._http_clean)
    assert http_claim not in proven
    assert {"claim": http_claim, "reasons": ["pass_1_left_rules_unverified"]} in missing
    everything["http"]["anonymous_lists"]["expression_rules_unverified"] = []
    assert http_claim in rehearsal.proven_claims(report, 1)[0]


def test_check_tokens_and_settings_detects_a_changed_secret(monkeypatch):
    import asyncio

    secret = "s" * 30
    definitions = [{"name": "users", "type": "auth", "schema": []}]
    facts = {"settings": {"recordAuthToken": {"secret": secret, "duration": 1209600}}}
    stored = {"authToken": {"secret": secret, "duration": 1209600}}

    async def rows_of(_conn, sql, _params=None):
        if "_collections" in sql:
            return [{"name": "users", "options": json.dumps(stored)}]
        return [{"value": json.dumps({})}]

    monkeypatch.setattr(rehearsal, "rows_of", rows_of)
    monkeypatch.setattr(
        rehearsal, "token_plan", lambda _d: [("users", {"authToken": "recordAuthToken"})]
    )
    result = asyncio.run(rehearsal.check_tokens_and_settings(FakeEngine(), definitions, facts))
    assert result["target_token_configs"]["users.authToken"]["secret_equal"] is True
    stored["authToken"]["secret"] = "x" * 30
    failure = refused(
        lambda: asyncio.run(rehearsal.check_tokens_and_settings(FakeEngine(), definitions, facts)),
        "token_configs_differ",
    )
    assert failure.details == {"configs": ["users.authToken"]}
    assert secret not in json.dumps(failure.details) and "x" * 30 not in json.dumps(failure.details)


def test_index_and_view_lines_of_the_signature():
    before = [
        "index users.users_pkey CREATE UNIQUE INDEX users_pkey ON public.users USING btree (id)"
    ]
    archive = [
        "index _buma_c11_rowids._buma_c11_rowids_pkey CREATE UNIQUE INDEX _buma_c11_rowids_pkey "
        "ON public._buma_c11_rowids USING btree (collection_id, record_id)"
    ]
    assert rehearsal.signature_difference(before, before + archive)["only_import_archives_added"]
    dropped = rehearsal.signature_difference(before, archive)
    assert not dropped["only_import_archives_added"]
    view = rehearsal.signature_difference(["view v 1"], ["view v 2"])
    assert view["unexpected_lines"] == 2


def test_a_failing_group_of_reads_is_a_failed_check_not_an_abort(tmp_path):
    import asyncio

    reads = rehearsal.HttpReads.__new__(rehearsal.HttpReads)
    reads.problems, reads.timer, reads.error_log = {}, {}, tmp_path / "errors.log"

    async def broken():
        raise KeyError("a real value never reaches the report")

    asyncio.run(reads.timed("filters", broken))
    assert reads.problems == {"reads:filters": ["error:KeyError"]}
    assert stat_mode(reads.error_log) == 0o600
    with pytest.raises(KeyError):
        asyncio.run(reads.timed("auth", broken, fatal=True))


def stat_mode(path: Path) -> int:
    return path.stat().st_mode & 0o777


def test_value_differences_are_named_by_their_visible_form():
    kind = rehearsal.difference_kind
    # PocketBase serves the field's zero value for a SQL NULL.
    for expected in ("", 0.0, 0, False, []):
        assert kind(expected, None) == "null_for_zero_value", expected
    assert kind("x", None) == kind(1.5, None) == kind(True, None) == "differs"
    assert kind(70123456, "70123456") == kind(12.5, "12.5") == "number_as_text"
    assert kind(12, "13") == kind(12, "twelve") == "differs"
    assert kind(1, True) == kind(0, False) == "integer_boolean_as_boolean"
    assert kind(1, False) == kind(2, True) == kind(1.0, True) == "differs"
    assert kind("b,a,c", "a,b,c") == "concatenation_order"
    assert kind("a,b", "a,c") == kind("ab", "ba") == "differs"
    outcomes = {
        "a": "null_for_zero_value",
        "b": "number_as_text",
        "c": "integer_boolean_as_boolean",
        "d": "concatenation_order",
        "e": "differs",
        "f": "equal",
        "g": "unmodeled",
    }
    codes = rehearsal.value_problems(outcomes)
    assert codes == [
        "null_served_for_pocketbase_zero_value:a",
        "number_served_as_text:b",
        "sqlite_integer_boolean_served_as_boolean:c",
        "concatenated_items_in_another_order:d",
        "value_differs:e",
    ]
    # Every named form is a divergence a decision may accept; value_differs never is.
    classified = rehearsal.classify_problems({"served_values:v": codes}, {})
    assert set(classified["divergences"]) == {
        "null_served_for_pocketbase_zero_value",
        "number_served_as_text",
        "sqlite_integer_boolean_served_as_boolean",
        "concatenated_items_in_another_order",
    }
    assert set(rehearsal.NAMED_DIFFERENCES.values()) <= set(rehearsal.DIVERGENCES)
    accepted = dict.fromkeys(classified["divergences"], "D-9999")
    assert rehearsal.classify_problems({"served_values:v": codes}, accepted)["failing"] == {
        "served_values:v": sorted(codes)
    }
    assert rehearsal.classify_problems({"served_values:v": codes[:4]}, accepted)["failing"] == {}


def test_served_view_text_null_is_named_in_the_record_comparison():
    view = {
        "id": "v3v3v3v3v3v3v3v",
        "name": "joined_view",
        "type": "view",
        "schema": [
            {"name": "label", "type": "text", "options": {}},
            {"name": "count", "type": "json", "options": {}},
            {"name": "paid", "type": "json", "options": {}},
        ],
    }
    raw = {"id": "r1", "label": None, "count": 3, "paid": 1}
    served = {
        "id": "r1",
        "collectionId": view["id"],
        "collectionName": "joined_view",
        "label": None,
        "count": "3",
        "paid": True,
    }
    outcomes = rehearsal.compare_served_record(view, served, raw, email_visible=True)
    assert rehearsal.value_problems(outcomes) == [
        "number_served_as_text:count",
        "null_served_for_pocketbase_zero_value:label",
        "sqlite_integer_boolean_served_as_boolean:paid",
    ]


def test_no_failure_passes_a_code_keyword():
    """require(condition, code, **details): a details key named code is a TypeError."""
    import ast

    tree = ast.parse(Path(rehearsal.__file__).read_text())
    calls = [
        node
        for node in ast.walk(tree)
        if isinstance(node, ast.Call)
        and isinstance(node.func, ast.Name)
        and node.func.id in ("require", "RehearsalFailure")
    ]
    assert calls
    assert [c.lineno for c in calls if any(k.arg == "code" for k in c.keywords)] == []


def test_the_present_single_relation_is_the_first_one_with_an_existing_target():
    definitions = [
        {
            "id": "c_sale",
            "name": "buma_sale",
            "type": "base",
            "schema": [
                {
                    "name": "customer",
                    "type": "relation",
                    "options": {"collectionId": "c_cust", "maxSelect": 1},
                },
            ],
        },
        {"id": "c_cust", "name": "buma_customer", "type": "base", "schema": []},
        {
            "id": "c_stock",
            "name": "buma_stock",
            "type": "base",
            "schema": [
                {
                    "name": "article",
                    "type": "relation",
                    "options": {"collectionId": "c_art", "maxSelect": 1},
                },
                {
                    "name": "tags",
                    "type": "relation",
                    "options": {"collectionId": "c_art", "maxSelect": None},
                },
            ],
        },
        {"id": "c_art", "name": "buma_article", "type": "base", "schema": []},
        {
            "id": "c_view",
            "name": "a_view",
            "type": "view",
            "schema": [
                {
                    "name": "article",
                    "type": "relation",
                    "options": {"collectionId": "c_art", "maxSelect": 1},
                },
            ],
        },
    ]
    assert rehearsal.single_relations(definitions) == [
        ("buma_sale", "customer"),
        ("buma_stock", "article"),
    ]
    db = sqlite3.connect(":memory:")
    db.executescript(
        "CREATE TABLE buma_sale (id TEXT, customer TEXT);"
        "CREATE TABLE buma_customer (id TEXT);"
        "CREATE TABLE buma_stock (id TEXT, article TEXT, tags TEXT);"
        "CREATE TABLE buma_article (id TEXT);"
        "INSERT INTO buma_sale VALUES ('s1', 'gone'), ('s2', 'gone'), ('s3', 'lost'), ('s4', '');"
        "INSERT INTO buma_stock VALUES ('k1', 'a1', '[]'), ('k2', 'a1', '[]'), ('k3', 'a2', '[]');"
        "INSERT INTO buma_article VALUES ('a1'), ('a2');"
    )
    orphan = rehearsal.single_relation_sample(db, definitions, "buma_sale", "customer", False)
    assert orphan == {"collection": "buma_sale", "field": "customer", "value": "gone", "rows": 2}
    # Every buma_sale.customer target is missing: the next single relation is used.
    assert rehearsal.single_relation_sample(db, definitions, "buma_sale", "customer", True) is None
    present = rehearsal.single_relation_sample(db, definitions, "buma_stock", "article", True)
    assert present == {"collection": "buma_stock", "field": "article", "value": "a1", "rows": 2}
