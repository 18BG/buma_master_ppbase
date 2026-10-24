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
    source.write_text('A = "first"\n\ndef f():\n    return {"second": 1}\n')
    assert rehearsal.code_literals(source) == {"first", "second"}


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


def test_filter_cases_follow_the_source_samples():
    samples = {
        "buma_customer.pdv": {"value": "p1", "rows_containing": 7, "empty_rows": 90, "rows": 100},
        "buma_pilotage_ticket.tag": {
            "value": "t1",
            "rows_containing": 2,
            "empty_rows": 0,
            "rows": 4,
        },
        "buma_wallet.managers": {
            "value": "a,b",
            "rows_containing": 1,
            "empty_rows": 10,
            "rows": 11,
        },
        "orphan": {"value": "o1", "rows": 5},
        "present_single": {"value": "c1", "rows": 3},
    }
    cases = {
        code: (name, expr, total) for code, name, expr, total in rehearsal.filter_cases(samples)
    }
    assert cases["pdv_any_equals"] == ("buma_customer", 'pdv ?= "p1"', 7)
    assert cases["pdv_length_positive"] == ("buma_customer", "pdv:length > 0", 10)
    assert cases["managers_any_equals_31_characters_orphan"] == (
        "buma_wallet",
        'managers ?= "a,b"',
        1,
    )
    assert cases["orphan_single_equals"][2] == 5
    assert cases["orphan_single_target_id"] == ("buma_sale", 'customer.id = "o1"', 0)
    assert len(cases) == 9


def passed_pass(index: int, seconds: float) -> dict:
    return {
        "pass": index,
        "status": "passed",
        "checks": {"rows": 3},
        "measures": {"phase_seconds": {"import": seconds, "diagnosis": 2.0}},
    }


def test_repeatability_and_runbook_durations():
    passes = [passed_pass(1, 10.0), passed_pass(2, 12.0)]
    private = {"fingerprints": {"t": "1:ab"}, "row_hashes_after_import": {"t": "1:5"}}
    result = rehearsal.repeatability(passes, [private, json.loads(json.dumps(private))])
    assert result["identical"] is True
    other = {**private, "row_hashes_after_import": {"t": "1:6"}}
    assert rehearsal.repeatability(passes, [private, other])["identical"] is False
    assert rehearsal.repeatability(passes[:1], [private])["identical"] is None
    assert rehearsal.runbook_durations(passes) == {
        "import": {"seconds": [10.0, 12.0], "max": 12.0},
        "diagnosis": {"seconds": [2.0, 2.0], "max": 2.0},
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
