"""C11 fixture profiles, checked directly in SQLite and storage (WS-D).

The tests that build a fixture need the official PocketBase binaries; each
missing variable skips its binary:

    BUMA_C11_POCKETBASE_0_22_27=/path/to/pocketbase-0.22.27
    BUMA_C11_POCKETBASE_0_22_34=/path/to/pocketbase-0.22.34
    BUMA_SCHEMA_EXPORT=/path/to/buma-master-pb_schema-corrected.json

Assertions read the built ``data.db`` and ``storage/`` themselves, never the
codes of the importer diagnostic. The last tests run the version-reference
aggregation of ``scripts/c11_version_reference.py`` on these synthetic files
and check that it reports shapes and formats, never values.
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
