"""C11 fixture profiles, checked directly in SQLite and storage (WS-D).

The tests that build a fixture need the official PocketBase binaries; each
missing variable skips its binary:

    BUMA_C11_POCKETBASE_0_22_27=/path/to/pocketbase-0.22.27
    BUMA_C11_POCKETBASE_0_22_34=/path/to/pocketbase-0.22.34
    BUMA_SCHEMA_EXPORT=/path/to/buma-master-pb_schema-corrected.json

Assertions read the built ``data.db`` and ``storage/`` themselves, never the
codes of the importer diagnostic. The last tests run the version-reference
aggregation of ``scripts/c11_version_reference.py`` on these synthetic files
and check that it reports shapes and formats, never values. The last section
checks the version gate, the import call and the comparison rules of
``scripts/c11_rehearsal.py`` without Docker, PocketBase or PPBase.
"""

import json
import os
import re
import sqlite3
import struct
import sys
from contextlib import closing
from pathlib import Path

import pytest

from scripts import c11_fixture as fixture
from scripts import c11_version_reference as reference

SCHEMA = Path(
    os.environ.get(
        "BUMA_SCHEMA_EXPORT", "/Users/omarsisss/Downloads/buma-master-pb_schema-corrected.json"
    )
)
BINARIES = {"0.22.27": "BUMA_C11_POCKETBASE_0_22_27", "0.22.34": "BUMA_C11_POCKETBASE_0_22_34"}
DEFAULT_DURATIONS = {
    "adminAuthToken": 1209600,
    "adminPasswordResetToken": 1800,
    "adminFileToken": 120,
    "recordAuthToken": 1209600,
    "recordPasswordResetToken": 1800,
    "recordEmailChangeToken": 1800,
    "recordVerificationToken": 604800,
    "recordFileToken": 120,
}
ALNUM50 = re.compile(r"[A-Za-z0-9]{50}")


def binary_for(version: str) -> Path:
    value = os.environ.get(BINARIES[version])
    if not value:
        pytest.skip(f"{BINARIES[version]} is not set")
    if not SCHEMA.is_file():
        pytest.skip("BUMA_SCHEMA_EXPORT is not available")
    # The variable name is only a label: a [0.22.34] test run by another
    # binary would be false evidence, so the binary's own answer decides.
    reported = fixture.pocketbase_version(Path(value))
    if reported != version:
        pytest.fail(f"{BINARIES[version]} runs PocketBase {reported}, not {version}")
    return Path(value)


class Built:
    def __init__(self, data: Path):
        self.data = data
        self.definitions = {c["name"]: c for c in json.loads(SCHEMA.read_text())}
        self.db = sqlite3.connect((data / "data.db").resolve().as_uri() + "?mode=ro", uri=True)

    def rows(self, sql: str, *parameters) -> list[tuple]:
        return self.db.execute(sql, parameters).fetchall()

    def value(self, sql: str, *parameters):
        return self.db.execute(sql, parameters).fetchone()[0]

    def ids(self, table: str) -> set[str]:
        return {row[0] for row in self.rows(f'SELECT id FROM "{table}"')}

    def field(self, collection: str, name: str) -> dict:
        return next(f for f in self.definitions[collection]["schema"] if f["name"] == name)

    def target(self, collection: str, name: str) -> str:
        target = self.field(collection, name)["options"]["collectionId"]
        return next(c for c, d in self.definitions.items() if d["id"] == target)

    def folder(self, collection: str, record: str) -> Path:
        return self.data / "storage" / self.definitions[collection]["id"] / record


@pytest.fixture(scope="module", params=sorted(BINARIES))
def real_shape(request, tmp_path_factory):
    binary = binary_for(request.param)
    root = tmp_path_factory.mktemp("c11-real-shape") / "fixture"
    built = Built(fixture.create_fixture(root, binary, SCHEMA, profile="real_shape"))
    with closing(built.db):
        yield built


@pytest.fixture(scope="module", params=sorted(BINARIES))
def default(request, tmp_path_factory):
    binary = binary_for(request.param)
    root = tmp_path_factory.mktemp("c11-default") / "fixture"
    built = Built(fixture.create_fixture(root, binary, SCHEMA))
    with closing(built.db):
        yield built


def test_default_profile_keeps_the_historical_rehearsal_input(default):
    counts = {
        name: default.value(f'SELECT COUNT(*) FROM "{name}"')
        for name, c in default.definitions.items()
        if c["type"] != "view"
    }
    assert {name: n for name, n in counts.items() if n} == {
        "roles": 1,
        "users": 2,
        "agent_users": 1,
        "buma_caisse_access": 1,
        "buma_article": 1,
        "buma_ressources": 1,
        "buma_supplier": 1,
        "buma_transaction": 1,
    }
    files = sorted(p for p in (default.data / "storage").rglob("*") if p.is_file())
    assert [p.name for p in files] == ["one.bin", "two.bin", "proof.bin"]
    assert sum(p.stat().st_size for p in files) == 38
    assert default.value("SELECT COUNT(*) FROM _admins") == 0
    assert {r[0] for r in default.rows("SELECT file FROM _migrations WHERE file LIKE '1789%'")} == {
        fixture.DEFAULT_MIGRATION
    }


def test_real_shape_snapshot_is_closed_and_extends_the_default_records(real_shape):
    names = {p.name for p in real_shape.data.iterdir()}
    assert "data.db" in names and not {"data.db-wal", "data.db-journal"} & names
    assert real_shape.value("PRAGMA journal_mode") == "delete"
    assert real_shape.value("PRAGMA integrity_check") == "ok"
    applied = {r[0] for r in real_shape.rows("SELECT file FROM _migrations")}
    assert {
        fixture.DEFAULT_MIGRATION,
        fixture.REAL_SHAPE_MIGRATION,
        fixture.REAL_SHAPE_POST_API_MIGRATION,
    } <= applied
    assert dict(real_shape.rows("SELECT id, _rowid_ FROM users")) == {
        "c11user00000001": 41,
        "c11user00000002": 93,
        "c11user00000003": 94,
        "c11user00000004": 95,
    }
    assert dict(real_shape.rows("SELECT id, _rowid_ FROM buma_article")) == {
        "c11art000000001": 7001,
        "c11art000000002": 7002,
    }
    assert real_shape.rows("SELECT _rowid_ FROM buma_supplier") == [(-17,)]
    assert real_shape.ids("buma_ressources") == {"c11file00000001", "c11file00000002"} | {
        "c11file00000003",
        "c11file00000004",
    }


def test_unlimited_relations_stay_json_arrays_in_their_order(real_shape):
    for path, records in fixture.REAL_SHAPE["unlimited_relations"].items():
        collection, name = path.split(".")
        assert real_shape.field(collection, name)["options"]["maxSelect"] is None
        column = next(
            c for c in real_shape.rows(f'PRAGMA table_info("{collection}")') if c[1] == name
        )
        assert column[2:5] == ("JSON", 1, "'[]'")
        stored = dict(real_shape.rows(f'SELECT id, "{name}" FROM "{collection}"'))
        assert stored == {
            record: json.dumps(items, separators=(",", ":")) for record, items in records.items()
        }
        targets = real_shape.ids(real_shape.target(collection, name))
        for items in records.values():
            assert all(item in targets for item in items) or path == "buma_wallet.managers"
    lengths = {
        len(json.loads(v))
        for (v,) in real_shape.rows("SELECT pdv FROM buma_customer")
        + real_shape.rows("SELECT tag FROM buma_pilotage_ticket")
    }
    assert {0, 1, 2} <= lengths


def test_joined_managers_element_is_one_31_character_item(real_shape):
    (managers,) = real_shape.rows("SELECT managers FROM buma_wallet WHERE id='c11wallet000001'")[0]
    (element,) = json.loads(managers)
    users = real_shape.ids("users")
    assert len(element) == 31 and element.count(",") == 1
    assert element not in users and set(element.split(",")) <= users


def test_orphan_references_are_stored_as_is(real_shape):
    assert real_shape.target("buma_sale", "customer") == "buma_customer_old"
    assert real_shape.value("SELECT COUNT(*) FROM buma_customer_old") == 0
    sales = dict(real_shape.rows("SELECT id, customer FROM buma_sale"))
    assert sales == fixture.REAL_SHAPE["orphan_relations"]["buma_sale.customer"]
    assert sales["c11sale00000001"] in real_shape.ids("buma_customer")
    assert sales["c11sale00000002"] not in real_shape.ids("buma_customer")
    for path in (
        "buma_sim_balance_activity.author",
        "buma_article.categorie",
        "buma_customer.type",
    ):
        collection, name = path.split(".")
        for record, value in fixture.REAL_SHAPE["orphan_relations"][path].items():
            stored = real_shape.value(f'SELECT "{name}" FROM "{collection}" WHERE id=?', record)
            assert stored == value
            assert value not in real_shape.ids(real_shape.target(collection, name))


def test_referenced_files_absent_from_storage(real_shape):
    for path, records in fixture.REAL_SHAPE["missing_files"].items():
        collection, name = path.split(".")
        for record, names in records.items():
            stored = real_shape.value(f'SELECT "{name}" FROM "{collection}" WHERE id=?', record)
            assert (json.loads(stored) if stored.startswith("[") else [stored]) == names
            assert not any((real_shape.folder(collection, record) / n).exists() for n in names)


def png_size(path: Path) -> tuple[int, int]:
    content = path.read_bytes()
    assert content[:8] == b"\x89PNG\r\n\x1a\n" and content[12:16] == b"IHDR"
    return struct.unpack(">II", content[16:24])


def test_files_uploaded_through_the_pocketbase_api(real_shape):
    assert fixture.REAL_SHAPE["api_uploads"] == {
        "c11file00000003": "buma_ressources.file",
        "C11_CONFIG_REC1": "buma_config.logo",
        "c11deleted00001": "buma_ressources.file",
    }
    collection, fields, field, files, _ = fixture.UPLOADS[0]
    stored = json.loads(
        real_shape.value(f'SELECT "{field}" FROM "{collection}" WHERE id=?', fields["id"])
    )
    folder = real_shape.folder(collection, fields["id"])
    assert len(stored) == len(files)
    for name, (client_name, content) in zip(stored, files, strict=True):
        stem, extension = client_name.lower().rsplit(".", 1)
        assert re.fullmatch(
            re.escape(stem.replace(" ", "_")) + r"_[A-Za-z0-9]{10}\." + extension, name
        )
        assert (folder / name).read_bytes() == content
        attrs = json.loads((folder / f"{name}.attrs").read_text())
        assert attrs["user.metadata"] == {"original-filename": client_name}
        assert attrs["user.content_type"].startswith(
            "image/png" if extension == "png" else "text/plain"
        )
    image = stored[0]
    thumb = folder / f"thumbs_{image}" / f"100x100_{image}"
    assert png_size(thumb) == (100, 100) and Path(f"{thumb}.attrs").is_file()
    # Files under a custom record ID, with their thumbnail.
    logo = real_shape.value("SELECT logo FROM buma_config WHERE id='C11_CONFIG_REC1'")
    config = real_shape.folder("buma_config", "C11_CONFIG_REC1")
    assert (config / logo).is_file() and (config / f"thumbs_{logo}" / f"100x100_{logo}").is_file()
    # The deleted record has no row anymore, but PocketBase kept its files.
    for collection, record in fixture.DELETED_RECORDS:
        assert record not in real_shape.ids(collection)
        left = sorted(p.name for p in real_shape.folder(collection, record).iterdir())
        assert len(left) == 2 and left[1] == left[0] + ".attrs"


def test_duplicate_mobile_return_keys_keep_every_row(real_shape):
    for key, expected in fixture.RETURN_DUPLICATES.items():
        rows = real_shape.rows(
            "SELECT id, _rowid_, created, type, author, system_id, quantity, price, notes "
            "FROM buma_transaction WHERE mobile_message_id=? ORDER BY _rowid_",
            key,
        )
        assert [r[0] for r in rows] == [record for record, *_ in expected]
        assert {r[3:6] for r in rows} == {("RETURN", fixture.USER, "")}
        assert len({r[6:] for r in rows}) == len(rows)  # business values differ
        assert [r[2] for r in rows] == [created for _, created, *_ in expected]
    tie = real_shape.rows(
        "SELECT created FROM buma_transaction WHERE mobile_message_id='c11-mobile-dup-tie'"
    )
    assert sorted(tie).count(min(tie)) == 2
    inverted = real_shape.rows(
        "SELECT created FROM buma_transaction WHERE mobile_message_id='c11-mobile-dup-inverted' "
        "ORDER BY _rowid_"
    )
    assert inverted[0] > inverted[1]


def test_user_dates_are_stored_by_pocketbase_in_its_layout(real_shape):
    layout = re.compile(r"[0-9]{4}-[0-9]{2}-[0-9]{2} [0-9]{2}:[0-9]{2}:[0-9]{2}\.[0-9]{3}Z")
    for path, records in fixture.REAL_SHAPE["user_dates"].items():
        collection, name = path.split(".")
        assert real_shape.field(collection, name)["type"] == "date"
        stored = dict(real_shape.rows(f'SELECT id, "{name}" FROM "{collection}"'))
        for record, value in records.items():
            assert layout.fullmatch(value) and stored[record] == value
        # The other rows of these collections keep PocketBase's empty date.
        assert {v for r, v in stored.items() if r not in records} <= {""}
    # Given as 10:00+02:00 to the Dao: PocketBase stored the UTC instant.
    delais = fixture.REAL_SHAPE["user_dates"]["buma_pilotage_ticket.delais"]
    assert delais == {"c11ticket000001": "2025-03-01 08:00:00.000Z"}
    mobile = fixture.REAL_SHAPE["user_dates"]["buma_transaction.mobile_transaction_date"]
    assert set(mobile) == {r for rows in fixture.RETURN_DUPLICATES.values() for r, *_ in rows}


def test_empty_multi_file_list_is_stored_as_an_empty_json_array(real_shape):
    ((path, records),) = fixture.REAL_SHAPE["empty_multi_file"].items()
    collection, name = path.split(".")
    assert real_shape.field(collection, name)["options"]["maxSelect"] > 1
    for record, items in records.items():
        assert items == []
        stored = real_shape.rows(
            f'SELECT "{name}", typeof("{name}") FROM "{collection}" WHERE id=?', record
        )
        assert stored == [("[]", "text")]
        assert not real_shape.folder(collection, record).exists()


def test_documented_maps_keep_one_entry_per_record():
    # Keyed by record ID: a second record of one collection must not replace
    # the first (the api_uploads defect fixed in 012e46c).
    deleted = fixture.REAL_SHAPE["deleted_record_files"]
    assert deleted == {record: collection for collection, record in fixture.DELETED_RECORDS}
    assert len(deleted) == len(fixture.DELETED_RECORDS)
    assert len(fixture.REAL_SHAPE["api_uploads"]) == len(fixture.UPLOADS)


def test_custom_record_ids_and_undeclared_select_values(real_shape):
    for collection, record in fixture.REAL_SHAPE["custom_record_ids"].items():
        assert record in real_shape.ids(collection) and not re.fullmatch(r"[a-z0-9]{15}", record)
    ids = fixture.REAL_SHAPE["custom_record_ids"]
    assert re.fullmatch(r"[a-z]{3,9}", ids["buma_apps_roles"])
    assert re.fullmatch(r"(?=.*[A-Z])(?=.*_)[A-Za-z0-9_]{15}", ids["buma_payment_method"])
    method = real_shape.value("SELECT method FROM buma_payment WHERE id='c11payment00001'")
    assert method == "C11_PAY_METHOD1" and method in real_shape.ids("buma_payment_method")
    kind = real_shape.value("SELECT kind FROM buma_sale WHERE id='c11sale00000002'")
    assert kind == "C11_UNDECLARED"
    assert kind not in real_shape.field("buma_sale", "kind")["options"]["values"]


def test_auth_records_without_email_and_json_null_shapes(real_shape):
    bcrypt = pytest.importorskip("bcrypt")
    accounts = fixture.ACCOUNTS["default"] + fixture.ACCOUNTS["real_shape"]
    token_keys = []
    for collection, record, username, email, password in accounts:
        if collection == "_admins":
            continue
        row = real_shape.rows(
            f'SELECT username, email, passwordHash, tokenKey FROM "{collection}" WHERE id=?', record
        )[0]
        assert row[:2] == (username, email)
        assert row[2].startswith("$2a$12$") and bcrypt.checkpw(password.encode(), row[2].encode())
        assert ALNUM50.fullmatch(row[3])
        token_keys.append(row[3])
    assert len(set(token_keys)) == len(token_keys)
    assert sum(1 for *_, email, _ in accounts if email == "") == 3
    more = dict(real_shape.rows("SELECT id, typeof(more) FROM users"))
    assert more["c11user00000003"] == "text" and more["c11user00000004"] == "null"
    assert real_shape.value("SELECT more FROM users WHERE id='c11user00000003'") == "null"


def test_pocketbase_admin_and_generated_settings(real_shape):
    bcrypt = pytest.importorskip("bcrypt")
    ((admin_id, avatar, email, token_key, password_hash, reset),) = real_shape.rows(
        "SELECT id, avatar, email, tokenKey, passwordHash, lastResetSentAt FROM _admins"
    )
    assert re.fullmatch(r"[a-z0-9]{15}", admin_id) and (avatar, email, reset) == (
        0,
        fixture.ADMIN_EMAIL,
        "",
    )
    assert ALNUM50.fullmatch(token_key) and password_hash.startswith("$2a$12$")
    assert bcrypt.checkpw(fixture.ADMIN_PASSWORD.encode(), password_hash.encode())
    ((key, value),) = real_shape.rows("SELECT key, value FROM _params")
    settings = json.loads(value)
    assert key == "settings" and isinstance(settings, dict)
    assert settings["meta"]["appName"] == "Acme"  # PocketBase default, as in the real backup
    secrets = [settings[name]["secret"] for name in DEFAULT_DURATIONS]
    assert all(ALNUM50.fullmatch(s) for s in secrets) and len(set(secrets)) == len(secrets)
    assert {name: settings[name]["duration"] for name in DEFAULT_DURATIONS} == DEFAULT_DURATIONS
    assert (settings["logs"]["maxDays"], settings["logs"]["minLevel"]) == (7, 4)
    assert settings["backups"]["cron"] == fixture.SETTINGS_OVERRIDES["backups.cron"]
    assert not (settings["smtp"]["enabled"] or settings["s3"]["enabled"])


def test_profile_and_version_are_checked_before_writing(tmp_path):
    fake = tmp_path / "pocketbase"
    fake.write_text(f"#!{sys.executable}\nprint('pocketbase version 0.22.20')\n")
    fake.chmod(0o700)
    with pytest.raises(ValueError, match="profile"):
        fixture.create_fixture(tmp_path / "unknown", fake, tmp_path / "schema.json", profile="x")
    with pytest.raises(ValueError, match="0.22.27 or 0.22.34"):
        fixture.create_fixture(tmp_path / "old", fake, tmp_path / "schema.json")
    assert not (tmp_path / "unknown").exists() and not (tmp_path / "old").exists()


def test_a_mislabeled_binary_fails_instead_of_covering_another_version(tmp_path, monkeypatch):
    fake = tmp_path / "pocketbase"
    fake.write_text(f"#!{sys.executable}\nprint('pocketbase version 0.22.27')\n")
    fake.chmod(0o700)
    schema = tmp_path / "schema.json"
    schema.write_text("[]")
    monkeypatch.setattr(sys.modules[__name__], "SCHEMA", schema)
    monkeypatch.setenv(BINARIES["0.22.34"], str(fake))
    with pytest.raises(pytest.fail.Exception, match="runs PocketBase 0.22.27, not 0.22.34"):
        binary_for("0.22.34")
    monkeypatch.setenv(BINARIES["0.22.27"], str(fake))
    assert binary_for("0.22.27") == fake


@pytest.mark.parametrize("output", ["Error: Failed to apply migration x.js", ""])
def test_silent_pocketbase_failures_are_not_accepted(tmp_path, output):
    # PocketBase 0.22 exits 0 when a migration fails and rolls the run back.
    fake = tmp_path / "pocketbase"
    fake.write_text(
        f"#!{sys.executable}\nimport sqlite3, sys\n"
        "if sys.argv[1:] == ['--version']:\n    print('pocketbase version 0.22.34')\n"
        "else:\n"
        "    data = sys.argv[sys.argv.index('--dir') + 1]\n"
        "    sqlite3.connect(data + '/data.db').execute('CREATE TABLE _migrations(file TEXT)')\n"
        f"    print({output!r})\n"
    )
    fake.chmod(0o700)
    (tmp_path / "schema.json").write_text("[]")
    root = tmp_path / "fixture"
    root.mkdir()
    (root / "data").mkdir()
    with pytest.raises(RuntimeError):
        fixture._migrate(fake, root / "data", root, {fixture.DEFAULT_MIGRATION})


def test_reference_helpers_keep_formats_not_values():
    assert reference.go_compact_json(["a", "<b>&"]) == '["a","\\u003cb\\u003e\\u0026"]'
    assert reference.id_shape("C11_PAY_METHOD1") == "len15:upper+digit+other"
    assert reference.id_shape("synthetic") == "len9:lower"
    masker = reference.Masker([], set(), set(), {})
    assert masker.value("users", "tokenKey", "A" * 50, {}) == "<random_alnum_50>"
    assert masker.value("users", "created", "2024-01-01 00:00:00.000Z", {}) == "<pb_datetime_now>"
    assert masker.value("users", "created", "2024-01-01T00:00:00Z", {}).startswith("<unexpected")
    assert masker.unexpected == ["users.created"]
    assert masker.storage_path("c/r/thumbs_a_b_AbCdEf1234.png/100x100_a_b_AbCdEf1234.png") == (
        "c/r/thumbs_a_b_<random10>.png/100x100_a_b_<random10>.png"
    )
    assert reference.differences({"a": [1, 2]}, {"a": [1, 3]}) == ["/a[1]"]


def _manifest_copy(built: Built, target: Path) -> Path:
    # A private-manifest look-alike for the synthetic storage, beside a copy of data.db.
    target.mkdir()
    (target / "data.db").write_bytes((built.data / "data.db").read_bytes())
    files = [
        {"path": "storage/" + p.relative_to(built.data / "storage").as_posix()}
        for p in sorted((built.data / "storage").rglob("*"))
        if p.is_file()
    ]
    manifest = {"files": [{"path": "data.db"}, *files]}
    (target / "c11-extraction-manifest.json").write_text(json.dumps(manifest))
    return target


def test_real_format_aggregation_reports_shapes_and_no_values(real_shape, default, tmp_path):
    copy = _manifest_copy(real_shape, tmp_path / "copy")
    result = reference.real_formats(
        copy, SCHEMA, default.data / "data.db", real_shape.data / "data.db"
    )
    assert reference.assess_real(result)["all_match"] is True
    ddl = result["ddl_compared_with_0_22_34_real_shape_fixture"]
    assert ddl["tables"] == {"identical_sql": 77} and ddl["views"] == {"identical_sql": 42}
    assert ddl["view_queries_in_collections_equal_export"] == 42
    shapes = result["shape_inventory"]
    assert shapes["custom_record_ids"] == {
        "buma_apps_roles": {"len9:lower": 1},
        "buma_config": {"len15:upper+digit+other": 1},
        "buma_payment_method": {"len15:upper+digit+other": 1},
        "buma_transaction": {"len14:lower+digit": 1},  # historical default c11tx000000001
    }
    assert shapes["relation_values_not_pb_ids"]["buma_payment.method"] == {
        "len15:upper+digit+other:target_present": 1
    }
    assert shapes["relation_values_not_pb_ids"]["buma_wallet.managers"] == {
        "len31:lower+digit+other:target_absent": 1
    }
    assert shapes["undeclared_select_values"]["buma_sale.kind"] == {"rows": 1, "distinct_values": 1}
    directories = result["storage_manifest"]["record_directories"]
    assert directories["known_collection:pb_id:record_absent"] == 2  # deleted record
    assert directories["known_collection:custom_id_len15:upper+digit+other:record_present"] == 4
    published = json.dumps(result)
    secrets = [r[0] for r in real_shape.rows("SELECT tokenKey FROM users")]
    values = [data["id"] for _, data, _ in fixture.REAL_SHAPE_RECORDS] + secrets
    values += [fixture.ADMIN_EMAIL, "$2a$", "c11_absent_resource", "Acme", "C11_UNDECLARED"]
    assert not [value for value in values if value in published]


def test_real_format_aggregation_flags_a_foreign_date_layout(real_shape, default, tmp_path):
    copy = _manifest_copy(real_shape, tmp_path / "copy")
    with closing(sqlite3.connect(copy / "data.db")) as db:
        db.execute("UPDATE buma_supplier SET created='2024-02-29T12:34:56Z'")
        db.commit()
    result = reference.real_formats(
        copy, SCHEMA, default.data / "data.db", real_shape.data / "data.db"
    )
    checks = reference.assess_real(result)["checks"]
    assert checks["dates_pocketbase_layout_or_empty"] is False
    assert result["value_formats"]["created_updated"]["other"] == 1


# ---------------------------------------------------------------------------
# scripts/c11_rehearsal.py: version gate, import call and comparison rules,
# without Docker, PocketBase or PPBase.
# ---------------------------------------------------------------------------


def num(value) -> tuple[str, str]:
    """A number literal as typed_json keeps it."""
    return ("number", str(value))


def _rehearsal():
    from scripts import c11_rehearsal

    return c11_rehearsal


def _error(status, message="The requested resource wasn't found.", key="code", data=None):
    return {key: num(status), "message": message, "data": {} if data is None else data}


def test_rehearsal_probe_tables_are_consistent():
    rehearsal = _rehearsal()
    labels = [probe.label for probe in rehearsal.PROBES]
    assert len(labels) == len(set(labels))
    logins = [row[0] for row in rehearsal.LOGINS]
    assert len(logins) == len(set(logins)) and not set(logins) & set(labels)
    assert set(rehearsal.KNOWN_DIVERGENCES) <= set(labels)
    assert {row[0] for row in rehearsal.RELATION_FILTERS} <= set(labels)
    by_label = {probe.label: probe for probe in rehearsal.PROBES}
    # Declared divergences exist only where the real_shape answers are checked.
    assert all(by_label[label].profiles == rehearsal.REAL for label in rehearsal.KNOWN_DIVERGENCES)
    credentials = {probe.credential for probe in rehearsal.PROBES} - {None}
    sessions = {row[0] for row in rehearsal.SESSIONS} | {
        "admin",
        "users_tampered",
        "admin_tampered",
    }
    assert credentials <= sessions


@pytest.mark.parametrize(
    ("path", "reference", "mine", "expected"),
    [
        (
            "/code: pocketbase only",
            _error(404),
            _error(404, key="status"),
            "error_envelope_code_vs_status",
        ),
        ("/code: pocketbase only", _error(404), _error(400, key="status"), None),
        (
            "/message",
            _error(401, "a"),
            _error(401, "b", key="status"),
            "auth_error_message_wording",
        ),
        ("/message", _error(404, "a"), _error(404, "b", key="status"), None),
        (
            "/data/identity: ppbase only",
            _error(400, "Failed to authenticate."),
            _error(400, "Failed to authenticate.", key="status", data={"identity": {}}),
            "failed_login_identity_detail",
        ),
        (
            "/data/identity: ppbase only",
            _error(404),
            _error(404, key="status", data={"identity": {}}),
            None,
        ),
    ],
)
def test_error_divergence_classes_are_narrow(path, reference, mine, expected):
    found = _rehearsal().divergence_class(path, reference, mine, credential="users", field_types={})
    assert found == expected


def test_record_divergence_classes_are_narrow():
    rehearsal = _rehearsal()
    types = {("buma_pdv", "start_date"): "date", ("buma_pdv", "name"): "text"}

    def item(**values):
        return {"items": [{"collectionName": "buma_pdv", **values}]}

    def classify(path, reference, mine, credential="users"):
        return rehearsal.divergence_class(
            path, reference, mine, credential=credential, field_types=types
        )

    empty, null = item(start_date="", name=""), item(start_date=None, name=None)
    assert classify("/items[0]/start_date", empty, null) == "empty_date_served_null"
    assert classify("/items[0]/name", empty, null) is None  # not a date field
    dated = item(start_date="2024-01-15 00:00:00.000Z")
    assert classify("/items[0]/start_date", dated, null) is None  # a value lost, not empty
    users = {"items": [{"collectionName": "users", "email": "x"}]}
    hidden = {"items": [{"collectionName": "users"}]}
    path = "/items[0]/email: pocketbase only"
    assert classify(path, users, hidden) == "auth_email_hidden_despite_manage_rule"
    assert classify(path, users, hidden, credential="denied") is None  # no manage access
    assert classify(path, item(email="x"), item()) is None  # not an auth collection
    expanded = {"id": "a", "expand": {}}
    assert classify("/expand: ppbase only", {"id": "a"}, expanded) == "empty_expand_object"
    assert classify("/expand: ppbase only", {"id": "a"}, {"expand": {"x": {}}}) is None
    pages = {"totalItems": num(0), "totalPages": num(0)}
    assert classify("/totalPages", pages, {"totalItems": num(0), "totalPages": num(1)}) == (
        "empty_list_total_pages"
    )
    assert classify("/totalPages", pages, {"totalItems": num(1), "totalPages": num(1)}) is None


def _png(width, height):
    return b"\x89PNG\r\n\x1a\n" + b"\x00\x00\x00\x0dIHDR" + struct.pack(">II", width, height)


def _file(status=200, content=b"x", content_type="application/octet-stream", disposition=None):
    import hashlib

    return {
        "status": status,
        "content": content,
        "sha256": hashlib.sha256(content).hexdigest(),
        "content_type": content_type,
        "content_disposition": disposition,
    }


def test_file_divergence_classes_are_narrow():
    rehearsal = _rehearsal()
    original = _file(content=_png(4, 4), content_type="image/png")
    thumb = _file(content=_png(100, 100), content_type="image/png", disposition="inline; x")
    served_original = _file(content=_png(4, 4), content_type="image/png")
    assert rehearsal.file_divergences("a:thumb", thumb, served_original, original) == (
        ["default_thumb_size_serves_original", "file_content_disposition_missing"],
        [],
    )
    other = _file(content=_png(50, 50), content_type="image/png")
    assert rehearsal.file_divergences("a:thumb", thumb, other, original)[1] == ["thumbnail"]
    pb_404 = _file(404, json.dumps(_error(404)).encode(), "application/json; charset=UTF-8")
    pp_404 = _file(404, json.dumps(_error(404, key="status")).encode(), "application/json")
    assert rehearsal.file_divergences("b", pb_404, pp_404, None) == (
        ["error_envelope_code_vs_status", "json_content_type_charset"],
        [],
    )
    assert rehearsal.file_divergences("c", _file(), _file(content=b"y"), None)[1] == ["bytes"]
    assert rehearsal.file_divergences("d", _file(), _file(404), None)[1] == ["status 200 != 404"]


def _answers(json_answers, logins=None):
    return {
        "json": json_answers,
        "logins": logins or {},
        "files": {},
        "realtime": {"results": {"event": True}, "update_body": {"id": "x"}},
    }


def _answer(status, body, token=None, content_type="application/json"):
    return {
        "status": status,
        "body": body,
        "token": token,
        "json": None,
        "content_type": content_type,
    }


def test_compare_servers_accepts_only_declared_pairs_and_classes(monkeypatch):
    rehearsal = _rehearsal()
    declared = {"pocketbase": 200, "ppbase": 500, "cause": "c", "client_impact": "i"}
    monkeypatch.setattr(
        rehearsal, "KNOWN_DIVERGENCES", {"declared": declared | {"documented_in": "d"}}
    )
    pocketbase = _answers(
        {
            "declared": _answer(200, {"items": []}),
            "classed": _answer(404, _error(404)),
            "changed": _answer(200, {"name": "a"}),
        }
    )
    ppbase = _answers(
        {
            "declared": _answer(500, _error(500, key="status")),
            "classed": _answer(404, _error(404, key="status")),
            "changed": _answer(200, {"name": "b"}),
        }
    )
    result = rehearsal.compare_servers(pocketbase, ppbase, "real_shape", {})
    entry = result["declared_divergences"]["declared"]
    assert (entry["state"], entry["ppbase_status"], entry["client_impact"]) == ("present", 500, "i")
    assert result["divergence_classes"]["error_envelope_code_vs_status"]["probes"] == ["classed"]
    assert result["divergence_classes"]["error_envelope_code_vs_status"]["state"] == "open"
    assert result["unexpected_differences"] == [{"probe": "changed", "differences": ["/name"]}]
    assert (result["probes"], result["identical"], result["with_open_divergences"]) == (4, 1, 3)
    # Another PPBase status than the declared one is unexpected.
    ppbase["json"]["declared"] = _answer(403, _error(403, key="status"))
    result = rehearsal.compare_servers(pocketbase, ppbase, "real_shape", {})
    assert result["unexpected_differences"][0]["probe"] == "declared"
    assert result["declared_divergences"]["declared"]["state"] == "unexplained"
    # Once resolved, the probe is compared like any other: classes still apply.
    ppbase["json"]["declared"] = _answer(200, {"items": []})
    result = rehearsal.compare_servers(pocketbase, ppbase, "real_shape", {})
    assert result["declared_divergences"]["declared"]["state"] == "resolved"
    pocketbase["json"]["declared"] = _answer(200, {"totalItems": num(0), "totalPages": num(0)})
    ppbase["json"]["declared"] = _answer(200, {"totalItems": num(0), "totalPages": num(1)})
    result = rehearsal.compare_servers(pocketbase, ppbase, "real_shape", {})
    assert result["declared_divergences"]["declared"]["state"] == "resolved"
    assert result["divergence_classes"]["empty_list_total_pages"]["probes"] == ["declared"]
    assert [u["probe"] for u in result["unexpected_differences"]] == ["changed"]


def test_declared_json_path_divergence_is_present_then_resolved(monkeypatch):
    rehearsal = _rehearsal()
    paths = ("/items", "/totalItems", "/totalPages")
    monkeypatch.setattr(
        rehearsal,
        "KNOWN_DIVERGENCES",
        {"every": {"pocketbase": 200, "ppbase": 200, "paths": paths, "cause": "c"}},
    )

    def listing(*items, pages=None):
        total = num(len(items))
        return {"items": list(items), "totalItems": total, "totalPages": pages or total}

    pocketbase = _answers({"every": _answer(200, listing())})

    def compare(body):
        ppbase = _answers({"every": _answer(200, body)})
        return rehearsal.compare_servers(pocketbase, ppbase, "real_shape", {})

    result = compare(listing({"id": "a"}))  # any element matched: the defect
    assert result["declared_divergences"]["every"]["state"] == "present"
    assert not result["unexpected_differences"]
    result = compare(listing(pages=num(1)))  # fixed; totalPages 1 is its own class
    assert result["declared_divergences"]["every"]["state"] == "resolved"
    assert result["divergence_classes"]["empty_list_total_pages"]["probes"] == ["every"]
    assert not result["unexpected_differences"]
    result = compare({**listing({"id": "a"}), "other": 1})  # outside the declared paths
    assert result["unexpected_differences"][0]["probe"] == "every"


def test_json_content_types_are_compared():
    rehearsal = _rehearsal()
    charset = "application/json; charset=UTF-8"
    pocketbase = _answers(
        {"a": _answer(200, {"id": "x"}, content_type=charset), "b": _answer(200, {"id": "x"})}
    )
    ppbase = _answers({"a": _answer(200, {"id": "x"}), "b": _answer(200, {"id": "x"}, None, "x")})
    result = rehearsal.compare_servers(pocketbase, ppbase, "real_shape", {})
    assert result["divergence_classes"]["json_content_type_charset"]["probes"] == ["a"]
    assert result["unexpected_differences"] == [{"probe": "b", "differences": ["content_type"]}]
    assert result["results"]["a"] == {
        "status": 200,
        "same_status_and_body": True,
        "same_as_pocketbase": False,
    }
    ppbase["json"].pop("b")
    result = rehearsal.compare_servers(pocketbase, ppbase, "real_shape", {})
    assert result["unexpected_differences"] == [
        {"probe": "json", "differences": ["probe_sets_differ"]}
    ]


def _status_results(rehearsal, profile="real_shape"):
    json_results = {
        probe.label: {"status": probe.expected}
        for probe in rehearsal.PROBES
        if profile in probe.profiles
    }
    logins = {
        label: {"status": status, "token": {"type": "auth"} if status == 200 else None}
        for label, *_rest, profiles, status in rehearsal.LOGINS
        if profile in profiles
    }
    return {"json": json_results, "logins": logins}


def test_statuses_accept_a_declared_divergence_present_or_resolved():
    rehearsal = _rehearsal()
    results = _status_results(rehearsal)
    for server in ("pocketbase", "ppbase"):
        assert rehearsal.check_statuses(results, "real_shape", server)  # resolved everywhere
    label = "denied_views_joined_wallet"
    results["json"][label]["status"] = 500
    assert rehearsal.check_statuses(results, "real_shape", "ppbase")  # declared, present
    with pytest.raises(AssertionError):
        rehearsal.check_statuses(results, "real_shape", "pocketbase")
    results["json"][label]["status"] = 403
    with pytest.raises(AssertionError):
        rehearsal.check_statuses(results, "real_shape", "ppbase")


def _listing(*records):
    return {"json": {"items": [{"id": record} for record in records]}, "status": 200}


def _role_answers(prefix=""):
    customers = sorted(fixture.REAL_SHAPE["unlimited_relations"]["buma_customer.pdv"])
    return {
        prefix + "users_lists_customers_by_role": _listing(*customers),
        prefix + "denied_lists_wallets": _listing("c11wallet000001"),
        prefix + "denied_views_joined_wallet": {"status": 404, "json": None},
    }


def test_role_rules_run_once_a_declared_divergence_is_resolved():
    rehearsal = _rehearsal()
    answers = _role_answers()
    assert rehearsal.check_role_rules(answers, "ppbase") == (
        "role_rules_and_the_joined_managers_element"
    )
    answers["denied_lists_wallets"] = _listing()  # the joined element no longer matched
    with pytest.raises(AssertionError):
        rehearsal.check_role_rules(answers, "ppbase")
    rehearsal.check_role_rules(answers, "ppbase", skip=("denied_lists_wallets",))
    prefix = rehearsal.DECLARED_KEYS_PREFIX
    body = {"items": []}
    results = {
        "declared_keys": {
            key: {**value, "body": body} for key, value in _role_answers(prefix).items()
        },
        "json": {key: {**value, "body": body} for key, value in _role_answers().items()},
    }
    assert rehearsal.check_declared_keys(results, "pocketbase").endswith("with_declared_keys")
    # PocketBase never reads buma_apps_roles: another answer with the keys fails.
    results["json"]["users_lists_customers_by_role"]["body"] = {"items": [1]}
    with pytest.raises(AssertionError):
        rehearsal.check_declared_keys(results, "pocketbase")
    assert rehearsal.check_declared_keys(results, "ppbase").endswith("with_declared_keys")


def test_permission_keys_without_default_are_counted():
    rehearsal = _rehearsal()
    definitions = [
        {
            "name": "a",
            "listRule": "@request.auth.role.permission.isAdmin = true || "
            "@request.auth.role.permission.wallet.read = true",
            "viewRule": "@request.auth.role.permission.role.read = true",
            "options": {"manageRule": "@request.auth.role.permission.customer.read = true"},
        },
        {"name": "b", "listRule": "@request.auth.role.permission.user.update = true"},
    ]
    found = rehearsal.permission_keys_without_default(definitions)
    assert (found["permission_keys"], found["rules"], found["collections"]) == (2, 2, 1)
    assert found["keys_of_the_probed_rules_included"] is False
    if SCHEMA.is_file():
        found = rehearsal.permission_keys_without_default(json.loads(SCHEMA.read_text()))
        assert (found["permission_keys"], found["rules"], found["collections"]) == (20, 28, 11)
        assert found["keys_of_the_probed_rules_included"] is True


def test_settings_are_compared_path_by_path():
    rehearsal = _rehearsal()
    source = {
        "meta": {"appName": "C11", "appUrl": "http://c11.invalid/", "hideControls": False},
        "logs": {"maxDays": 7, "minLevel": 4, "logIp": True},
        "smtp": {"enabled": False, "port": 587, "password": "synthetic"},
        "backups": {"cron": "0 3 29 2 *"},
    }
    imported = {
        "meta": {"appName": "C11", "appURL": "http://c11.invalid", "hideControls": False},
        "logs": {"maxDays": 7, "minLevel": 4, "logIP": True},
        "smtp": {"enabled": False, "port": 587, "password": "other but present"},
        "backups": {"cron": ""},
    }
    found = rehearsal.check_settings(source, imported, "real_shape")
    assert found["mapped_paths_equal_source"] == 9
    assert found["fixture_overrides"]["backups.cron"] == "not_imported_as_documented"
    for path, value in (
        (("logs", "maxDays"), 7.0),  # same number, another JSON type
        (("smtp", "password"), ""),  # a secret lost
        (("meta", "appURL"), "http://c11.invalid/"),  # slash kept
    ):
        changed = json.loads(json.dumps(imported))
        changed[path[0]][path[1]] = value
        with pytest.raises(AssertionError):
            rehearsal.check_settings(source, changed, "default")


def test_rowid_archive_must_hold_every_imported_record():
    rehearsal = _rehearsal()
    definitions = [
        {"id": "c1", "name": "a", "type": "base"},
        {"id": "c2", "name": "v", "type": "view"},
    ]
    final = {"a": [{"id": "r1"}, {"id": "r2"}]}
    archived = [
        {"collection_id": "c1", "record_id": "r1"},
        {"collection_id": "c1", "record_id": "r2"},
    ]
    rehearsal.check_rowid_archive(archived, definitions, final, 2)
    for rows, entries in ((2, archived[:1]), (3, archived), (2, [archived[0], archived[0]])):
        with pytest.raises(AssertionError):
            rehearsal.check_rowid_archive(entries, definitions, final, rows)
    other = [archived[0], {"collection_id": "c1", "record_id": "r3"}]
    with pytest.raises(AssertionError):
        rehearsal.check_rowid_archive(other, definitions, final, 2)


def _main(monkeypatch, tmp_path, outcome, logs=None):
    rehearsal = _rehearsal()
    monkeypatch.setattr(rehearsal.fixture, "pocketbase_version", lambda binary: "0.22.34")
    monkeypatch.setattr(rehearsal, "source_version_status", lambda found: (None, {"ok": True}))
    monkeypatch.setattr(rehearsal, "digest", lambda path: "0" * 64)
    monkeypatch.setattr(rehearsal, "buma_checkout", lambda: {"git_commit": None})
    seen = {}

    async def exercise(root, *args):
        seen["root"] = root
        (root / "server.log").write_text("synthetic log")
        if outcome is not None:
            raise outcome

    monkeypatch.setattr(rehearsal, "exercise", exercise)
    report = tmp_path / "report.json"
    argv = ["c11_rehearsal", "--pocketbase", "pb", "--schema", "s.json", "--report", str(report)]
    monkeypatch.setattr(sys, "argv", argv + (["--private-logs", str(logs)] if logs else []))
    return rehearsal, report, seen


def test_report_status_is_written_after_logs_and_cleanup(monkeypatch, tmp_path):
    rehearsal, report, seen = _main(monkeypatch, tmp_path, None, tmp_path / "logs")
    rehearsal.main()
    written = json.loads(report.read_text())
    assert written["status"] == "passed" and "failure_type" not in written
    assert written["cleanup"] == {"private_logs_copied": True, "temporary_directory_removed": True}
    assert not seen["root"].exists() and (tmp_path / "logs" / "server.log").is_file()
    # A log copy that fails after a successful run fails the report.
    rehearsal, report, seen = _main(monkeypatch, tmp_path / "x", None, tmp_path / "no" / "logs")
    (tmp_path / "x").mkdir()
    with pytest.raises(FileNotFoundError):
        rehearsal.main()
    written = json.loads(report.read_text())
    assert (written["status"], written["failure_type"]) == ("failed", "FileNotFoundError")
    assert not seen["root"].exists()
    rehearsal, report, _seen = _main(monkeypatch, tmp_path / "y", RuntimeError("synthetic"))
    (tmp_path / "y").mkdir()
    with pytest.raises(RuntimeError):
        rehearsal.main()
    assert json.loads(report.read_text())["status"] == "failed"


def test_container_removal_never_raises(monkeypatch):
    import subprocess

    rehearsal = _rehearsal()
    calls = []

    def failing(*args, **kwargs):
        calls.append(args[:2])
        raise subprocess.CalledProcessError(1, args)

    monkeypatch.setattr(rehearsal, "command", failing)
    monkeypatch.setattr(rehearsal.time, "sleep", lambda seconds: None)
    outcome = rehearsal.remove_container("buma-c11-test-x")
    assert outcome == {"container_stop_error": "CalledProcessError", "container_removed": None}
    assert calls[0] == ("docker", "stop")


def test_token_shapes_follow_the_documented_mapping():
    rehearsal = _rehearsal()
    record = {"alg": "HS256", "claims": ["collectionId", "exp", "id", "type"], "type": "authRecord"}
    issued = {
        "alg": "HS256",
        "claims": ["collectionId", "exp", "iat", "id", "refreshable", "type"],
        "type": "auth",
    }
    assert rehearsal.token_comparison(record, issued)["expected"] is True
    assert rehearsal.token_comparison(record, {**issued, "alg": "HS512"})["expected"] is False
    assert rehearsal.token_comparison(record, {**issued, "type": "admin"})["expected"] is False
    assert rehearsal.token_comparison(record, None)["expected"] is False


def test_import_call_requires_the_failure_parameters_it_uses(monkeypatch):
    import asyncio

    rehearsal = _rehearsal()

    async def without_auth_stage(engine, data, destination, diagnostic, definitions, *, report):
        return 7

    async def with_auth_stage(
        engine, data, destination, diagnostic, definitions, *, report, auth_fail_after=None
    ):
        report["auth_fail_after"] = auth_fail_after
        return 7

    monkeypatch.setattr(rehearsal, "import_snapshot", without_auth_stage)
    with pytest.raises(AssertionError, match="auth_fail_after"):
        asyncio.run(rehearsal.import_once(None, None, None, {}, [], auth_fail_after="settings"))
    monkeypatch.setattr(rehearsal, "import_snapshot", with_auth_stage)
    rows, evidence = asyncio.run(
        rehearsal.import_once(None, None, None, {}, [], auth_fail_after="settings")
    )
    assert (rows, evidence) == (7, {"auth_fail_after": "settings"})


@pytest.mark.parametrize(
    ("version", "status", "message"),
    [
        ("0.22.20", None, "fixture builder accepts 0.22.27 or 0.22.34"),
        ("0.22.34", "source_pocketbase_version_unvalidated", "does not accept PocketBase 0.22.34"),
    ],
)
def test_rehearsal_refuses_a_version_before_any_work(
    tmp_path, monkeypatch, capsys, version, status, message
):
    rehearsal = _rehearsal()
    monkeypatch.setattr(rehearsal.fixture, "pocketbase_version", lambda binary: version)
    monkeypatch.setattr(rehearsal, "source_version_status", lambda found: (status, None))
    monkeypatch.setattr(rehearsal, "command", lambda *a, **k: pytest.fail("no command may run"))
    report = tmp_path / "report.json"
    argv = ["c11_rehearsal", "--pocketbase", "pb", "--schema", "s.json", "--report", str(report)]
    monkeypatch.setattr(sys, "argv", argv)
    with pytest.raises(SystemExit) as exit_info:
        rehearsal.main()
    assert exit_info.value.code == 2 and message in capsys.readouterr().err
    assert not report.exists()


def test_isolated_ppbase_servers_keep_the_callers_pythonpath(monkeypatch):
    from scripts import c11_auth_runtime

    project = str(c11_auth_runtime.PROJECT)
    monkeypatch.setenv("PYTHONPATH", os.pathsep.join(["/x/ppbase", project, "/y"]))
    env = c11_auth_runtime.ppbase_server_environment()
    assert env["PYTHONPATH"] == os.pathsep.join([project, "/x/ppbase", "/y"])
    assert env["PPBASE_AUTO_MIGRATE"] == "false"
    monkeypatch.delenv("PYTHONPATH")
    assert c11_auth_runtime.ppbase_server_environment()["PYTHONPATH"] == project
