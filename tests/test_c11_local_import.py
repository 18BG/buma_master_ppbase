"""scripts/c11_local_import.py: the user's own local import (WS-L), synthetic data only.

The unit tests need nothing else. The integration tests (last section) need
Docker with the postgres:16-alpine image, the export and the official
PocketBase 0.22.34 binary, like tests/test_c11_fixture.py; each missing one
skips them:

    BUMA_C11_POCKETBASE_0_22_34=/path/to/pocketbase-0.22.34
    BUMA_SCHEMA_EXPORT=/path/to/buma-master-pb_schema-corrected.json

They own one PostgreSQL container (buma-c11-ws-l-<random>, labels, loopback
port, removed at the end, even on failure) holding three databases, build the
real_shape fixture with that binary in a path with spaces, and import it three
ways: a folder with storage/, a backup ZIP made by PocketBase itself, a folder
with the manifest only. PPBase is started by the displayed command.
"""

import asyncio
import errno
import importlib.util
import inspect
import json
import os
import re
import secrets
import shlex
import shutil
import signal
import subprocess
import sys
import time
import zipfile
from contextlib import contextmanager
from pathlib import Path
from types import SimpleNamespace

import pytest

from scripts import c11_fixture as fixture
from scripts import c11_import as c11
from scripts import c11_local_import as local
from scripts.c11_import import ImportBlocked, check_target_database, stage_snapshot
from scripts.c11_local_import import LocalImport, LocalImportRefused
from tests.test_c11_auth_import import DEFINITIONS as AUTH_DEFINITIONS
from tests.test_c11_auth_import import scripted, staged_copy
from tests.test_c11_import import FakeConnection, ImportEngine, tiny_snapshot

PROJECT = Path(__file__).resolve().parents[1]
SCHEMA = Path(
    os.environ.get(
        "BUMA_SCHEMA_EXPORT", "/Users/omarsisss/Downloads/buma-master-pb_schema-corrected.json"
    )
)
POCKETBASE = "BUMA_C11_POCKETBASE_0_22_34"
URL = "postgresql://buma:secret@127.0.0.1:5433/buma_local"
C5_ROWS = {
    "buma_apps_roles": 18,
    "buma_article_stockage": 1,
    "buma_customer": 1,
    "buma_payment_method": 3,
    "buma_supplier": 1,
}


def refusal(call) -> LocalImportRefused:
    with pytest.raises(LocalImportRefused) as caught:
        call()
    return caught.value


# ---------------------------------------------------------------------------
# Arguments, defaults and the backup forms.
# ---------------------------------------------------------------------------


def test_two_positional_arguments_and_no_option():
    args = local.parse_args(["backup.zip", URL])
    assert (args.backup, args.database_url) == (Path("backup.zip"), URL)
    for argv in (["backup.zip"], ["backup.zip", URL, "--port", "8091"], []):
        with pytest.raises(SystemExit):
            local.parse_args(argv)


def test_main_runs_with_the_documented_defaults(monkeypatch):
    seen = {}

    def execute(job):
        seen["job"] = job
        return {}

    monkeypatch.setattr(LocalImport, "execute", execute)
    assert local.main(["backup.zip", URL]) == 0
    job = seen["job"]
    assert job.backup_path == Path("backup.zip") and job.raw_url == URL
    assert job.data_dir == PROJECT / "pb_data"
    assert job.work_root == PROJECT / "backups"
    assert job.port == 8090
    assert job.environ is os.environ
    assert job.python == sys.executable
    assert local.SOURCE_VERSION == "0.22.34" and local.SCHEMA_ENV == "BUMA_SCHEMA_EXPORT"


@pytest.mark.parametrize(
    "command",
    [
        [sys.executable, "-m", "scripts.c11_local_import", "--help"],
        # Run by path with an isolated interpreter, as for real data.
        [sys.executable, "-I", "-B", "scripts/c11_local_import.py", "--help"],
    ],
)
def test_the_help_states_the_defaults(command):
    done = subprocess.run(command, cwd=PROJECT, capture_output=True, text=True, timeout=120)
    assert done.returncode == 0, done.stderr
    help_text = " ".join(done.stdout.split())
    for expected in ("BACKUP", "ADRESSE_POSTGRESQL", "0.22.34", "BUMA_SCHEMA_EXPORT", "pb_data/"):
        assert expected in help_text
    assert "8090" in help_text


def test_a_refusal_before_any_write_prints_french_advice(monkeypatch, capsys):
    monkeypatch.delenv("BUMA_SCHEMA_EXPORT", raising=False)
    before = sorted(p.name for p in (PROJECT / "backups").glob("c11-local-*"))
    pb_data = (PROJECT / "pb_data").exists()
    assert local.main(["backup.zip", URL]) == 1
    printed = capsys.readouterr().out
    assert "Échec : La variable BUMA_SCHEMA_EXPORT n'est pas définie" in printed
    assert "Rien n'a été écrit dans la base." in printed
    assert sorted(p.name for p in (PROJECT / "backups").glob("c11-local-*")) == before
    assert (PROJECT / "pb_data").exists() == pb_data


@pytest.mark.parametrize(
    "raw",
    [
        "postgresql://buma:secret@127.0.0.1:5433/buma_local",
        "postgres://buma:secret@127.0.0.1:5433/buma_local",
        "postgresql+asyncpg://buma:secret@127.0.0.1:5433/buma_local",
    ],
)
def test_postgresql_addresses_become_the_asyncpg_url_ppbase_uses(raw):
    url = local.database_url(raw)
    assert local.rendered(url) == "postgresql+asyncpg://buma:secret@127.0.0.1:5433/buma_local"


def test_a_password_with_reserved_characters_survives_the_displayed_url():
    from sqlalchemy.engine import make_url

    url = local.database_url("postgresql://buma:p%40ss%3Aw%2Frd@localhost/base")
    assert url.password == "p@ss:w/rd"
    assert make_url(local.rendered(url)).password == "p@ss:w/rd"


@pytest.mark.parametrize(
    "raw,code",
    [
        ("mysql://buma:secret@127.0.0.1/buma_local", "database_url_invalid"),
        ("postgresql+psycopg://buma@127.0.0.1/buma_local", "database_url_invalid"),
        ("not an address", "database_url_invalid"),
        ("postgresql://buma:secret@127.0.0.1:5433", "database_url_without_database"),
        ("postgresql://buma@127.0.0.1/buma_local?sslmode=require", "database_url_with_query"),
    ],
)
def test_other_addresses_are_refused(raw, code):
    assert refusal(lambda: local.database_url(raw)).code == code


def test_the_schema_export_comes_from_its_variable(tmp_path):
    assert refusal(lambda: local.schema_export({})).code == "schema_export_unset"
    missing = {"BUMA_SCHEMA_EXPORT": str(tmp_path / "absent.json")}
    assert refusal(lambda: local.schema_export(missing)).code == "schema_export_missing"
    export = tmp_path / "export avec espace.json"
    export.write_text("[]")
    assert local.schema_export({"BUMA_SCHEMA_EXPORT": str(export)}) == export


def test_the_installed_ppbase_must_carry_what_buma_requires(monkeypatch):
    import ppbase

    found = local.ppbase_installation()
    assert found["root"] == Path(ppbase.__file__).resolve().parent.parent
    monkeypatch.setattr(type(ppbase.pb), "set_error_compat", None)
    found = refusal(local.ppbase_installation)
    assert found.code == "ppbase_too_old" and "fix/d0019-relation-maxselect" in found.message


def test_a_missing_ppbase_is_refused_clearly(monkeypatch):
    monkeypatch.setitem(sys.modules, "ppbase", None)
    assert refusal(local.ppbase_installation).code == "ppbase_missing"


def write_zip(path: Path, names) -> Path:
    with zipfile.ZipFile(path, "w") as archive:
        for name in names:
            archive.writestr(name, b"synthetic")
    return path


def test_backup_forms_are_detected(tmp_path):
    root = tmp_path / "dossier avec espaces"
    root.mkdir()
    archive = write_zip(root / "backup complet.zip", ["data.db", "storage/c/r/f.png"])
    assert local.resolve_backup(archive) == local.Backup("zip", archive, None, None)
    folder = root / "extrait"
    (folder / "storage").mkdir(parents=True)
    (folder / "data.db").write_bytes(b"x")
    assert local.resolve_backup(folder) == local.Backup("directory", folder, folder, None)
    (folder / local.MANIFEST_NAME).write_text("{}")
    manifest = folder / local.MANIFEST_NAME
    assert local.resolve_backup(folder) == local.Backup("directory", folder, folder, manifest)
    (folder / "storage").rmdir()
    assert local.resolve_backup(folder) == local.Backup("manifest", folder, folder, manifest)


def test_unusable_backups_are_refused(tmp_path):
    def code(path):
        return refusal(lambda: local.resolve_backup(path)).code

    assert code(tmp_path / "absent.zip") == "backup_missing"
    text = tmp_path / "notes.txt"
    text.write_text("not a zip")
    assert code(text) == "backup_not_zip"
    assert code(write_zip(tmp_path / "a.zip", ["README.md"])) == "zip_without_data_db"
    assert code(write_zip(tmp_path / "b.zip", ["pb_data/data.db"])) == "zip_without_data_db"
    empty = tmp_path / "vide"
    empty.mkdir()
    assert code(empty) == "backup_without_data_db"
    (empty / "data.db").write_bytes(b"x")
    assert code(empty) == "backup_without_files"
    (empty / "storage").write_text("a file")
    assert code(empty) == "storage_not_a_directory"
    link = tmp_path / "lien"
    link.symlink_to(empty)
    assert code(link) == "backup_symlink"


# ---------------------------------------------------------------------------
# Steps 1 and 2: an entirely empty database, an absent or empty storage.
# ---------------------------------------------------------------------------

EMPTY = {
    "name": "buma_local",
    "version": "16.4",
    "version_num": 160004,
    "encoding": "UTF8",
    "relations": 0,
    "routines": 0,
    "types": 0,
    "extensions": 0,
}


def test_an_entirely_empty_database_passes():
    local.check_empty(EMPTY)


@pytest.mark.parametrize(
    "change,code",
    [
        ({"relations": 1}, "database_not_empty"),
        ({"routines": 2}, "database_not_empty"),
        ({"types": 1}, "database_not_empty"),
        # An extension (other than plpgsql) may own tables outside public.
        ({"extensions": 1}, "database_not_empty"),
        ({"version_num": 150008, "version": "15.8"}, "postgresql_too_old"),
        ({"encoding": "LATIN1"}, "database_not_utf8"),
    ],
)
def test_a_database_that_is_not_entirely_empty_is_refused(change, code):
    found = refusal(lambda: local.check_empty({**EMPTY, **change}))
    assert found.code == code
    if code == "database_not_empty":
        assert "entièrement vide" in found.message and "buma_local" in found.message


def test_the_storage_of_pb_data_must_be_absent_or_empty(tmp_path):
    data_dir = tmp_path / "pb data"
    assert local.check_storage_target(data_dir) == data_dir / "storage"
    (data_dir / "storage").mkdir(parents=True)
    assert local.check_storage_target(data_dir) == data_dir / "storage"
    (data_dir / "storage" / ".DS_Store").write_bytes(b"x")
    found = refusal(lambda: local.check_storage_target(data_dir))
    assert found.code == "storage_target_not_empty" and "1 élément" in found.message
    (data_dir / "storage" / ".DS_Store").unlink()
    (data_dir / "storage").rmdir()
    (data_dir / "storage").write_text("a file")
    assert refusal(lambda: local.check_storage_target(data_dir)).code == (
        "storage_target_not_a_directory"
    )
    (data_dir / "storage").unlink()
    elsewhere = tmp_path / "ailleurs"
    elsewhere.mkdir()
    (data_dir / "storage").symlink_to(elsewhere)
    assert refusal(lambda: local.check_storage_target(data_dir)).code == (
        "storage_target_not_a_directory"
    )
    other = tmp_path / "fichier"
    other.write_text("x")
    assert refusal(lambda: local.check_storage_target(other)).code == "data_dir_not_a_directory"


def test_free_space_is_checked_before_any_copy(tmp_path):
    local.check_free_space(tmp_path / "pas encore créé", 0)
    found = refusal(lambda: local.check_free_space(tmp_path, 1 << 62))
    assert found.code == "insufficient_free_space" and " Go" in found.message


# ---------------------------------------------------------------------------
# Step 4: only the rows that migration C5 inserts.
# ---------------------------------------------------------------------------


class _Statements:
    """Records the (table, id) of each DELETE of a migration's down()."""

    def __init__(self):
        self.rows = set()

    async def execute_sql(self, sql, params=None):
        table = re.match(r'DELETE FROM "([^"]+)" WHERE "id" = :id', sql).group(1)
        self.rows.add((table, params["id"]))


def test_c5_rows_are_read_from_the_migration_itself():
    rows = asyncio.run(local.c5_seed_rows())
    assert len(rows) == 24
    assert {table: sum(t == table for t, _ in rows) for table, _ in rows} == C5_ROWS
    assert ("buma_customer", "CUSTOMER_DIVERS") in rows
    assert ("buma_supplier", "SUPPLIER_DIVERS") in rows
    # Exactly what the migration's own down() deletes.
    spec = importlib.util.spec_from_file_location("c5", local.MIGRATIONS / local.C5_MIGRATION)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    deleted = _Statements()
    asyncio.run(module.down(deleted))
    assert deleted.rows == rows


class _Result:
    def __init__(self, rows=(), rowcount=0):
        self.rows, self.rowcount = list(rows), rowcount

    def __iter__(self):
        return iter(self.rows)

    def one(self):
        (row,) = self.rows
        return row


class SeedDatabase:
    """An engine whose single transaction holds the given rows per table."""

    def __init__(self, rows, accounts=(0, 0)):
        self.rows = {table: set(ids) for table, ids in rows.items()}
        self.accounts = accounts
        self.statements, self.outcome = [], None

    def begin(self):
        database = self

        class Transaction:
            async def __aenter__(self):
                return database

            async def __aexit__(self, failure, *_):
                database.outcome = "rollback" if failure else "commit"
                return False

        return Transaction()

    async def execute(self, statement, params=None):
        sql = str(statement)
        self.statements.append(sql)
        if sql.startswith("LOCK TABLE"):
            return _Result()
        if match := re.fullmatch(r'SELECT id FROM "([^"]+)"', sql):
            return _Result((record,) for record in sorted(self.rows.get(match.group(1), ())))
        if match := re.fullmatch(r'DELETE FROM "([^"]+)" WHERE id = :id', sql):
            found = params["id"] in self.rows.get(match.group(1), set())
            self.rows.get(match.group(1), set()).discard(params["id"])
            return _Result(rowcount=int(found))
        if "_superusers" in sql and "_params" in sql:
            return _Result([self.accounts])
        raise AssertionError(sql)


SEED_TABLES = [{"name": name, "type": "base"} for name in (*C5_ROWS, "users")] + [
    {"name": "a_view", "type": "view"}
]


def seed_rows(expected):
    rows = {}
    for table, record in expected:
        rows.setdefault(table, set()).add(record)
    return rows


def test_only_the_c5_rows_are_removed():
    expected = asyncio.run(local.c5_seed_rows())
    database = SeedDatabase(seed_rows(expected))
    assert asyncio.run(local.remove_c5_seeds(database, SEED_TABLES, expected)) == C5_ROWS
    assert database.outcome == "commit"
    assert all(not ids for ids in database.rows.values())
    assert sum(s.startswith("DELETE") for s in database.statements) == 24
    assert not any('"a_view"' in s for s in database.statements)


@pytest.mark.parametrize(
    "change,code",
    [
        (
            lambda rows: rows.setdefault("users", set()).add("u1"),
            "unexpected_rows_after_migrations",
        ),
        (lambda rows: rows["buma_customer"].clear(), "unexpected_rows_after_migrations"),
    ],
)
def test_any_other_row_stops_before_any_removal(change, code):
    expected = asyncio.run(local.c5_seed_rows())
    rows = seed_rows(expected)
    change(rows)
    database = SeedDatabase(rows)
    found = refusal(lambda: asyncio.run(local.remove_c5_seeds(database, SEED_TABLES, expected)))
    assert found.code == code
    assert not any(s.startswith("DELETE") for s in database.statements)
    assert database.outcome == "rollback"


def test_an_existing_superuser_or_setting_rolls_the_removal_back():
    expected = asyncio.run(local.c5_seed_rows())
    for accounts in ((1, 0), (0, 1)):
        database = SeedDatabase(seed_rows(expected), accounts=accounts)
        found = refusal(
            lambda d=database: asyncio.run(local.remove_c5_seeds(d, SEED_TABLES, expected))
        )
        assert found.code == "auth_target_not_empty" and database.outcome == "rollback"


# ---------------------------------------------------------------------------
# Step 6: the importer's guard, unchanged by default, explicit for this command.
# ---------------------------------------------------------------------------


def test_the_target_database_guard():
    check_target_database("buma_c11_rehearsal")
    with pytest.raises(ImportBlocked, match="isolated_rehearsal_database_required"):
        check_target_database("buma_local")
    check_target_database("buma_local", "buma_local")
    with pytest.raises(ImportBlocked, match="verified_empty_database_mismatch"):
        check_target_database("buma_c11_rehearsal", "buma_local")
    for value in ("", 3):
        with pytest.raises(ValueError):
            check_target_database("buma_local", value)


def test_load_isolated_refuses_another_database_unless_it_is_named(tmp_path, monkeypatch):
    data, export, schema = tiny_snapshot(tmp_path, monkeypatch)
    staged = tmp_path / "staged"
    stage_snapshot(data, staged, c11.diagnose(data, export, source_version="0.22.27"))
    connection = FakeConnection("buma_local")
    with pytest.raises(ImportBlocked, match="isolated_rehearsal_database_required"):
        asyncio.run(c11.load_isolated(connection, staged, schema))
    assert connection.statements == ["SELECT current_database()"]
    connection = FakeConnection("buma_local")
    with pytest.raises(ImportBlocked, match="verified_empty_database_mismatch"):
        asyncio.run(c11.load_isolated(connection, staged, schema, verified_empty_database="x"))
    assert connection.statements == ["SELECT current_database()"]
    # Named: past the guard, to the lock and the emptiness check of the tables.
    connection = FakeConnection("buma_local")
    with pytest.raises(ImportBlocked, match="target_not_empty"):
        asyncio.run(
            c11.load_isolated(connection, staged, schema, verified_empty_database="buma_local")
        )
    assert connection.statements[1].startswith("LOCK TABLE")


async def test_accounts_import_refuses_another_database_unless_it_is_named(tmp_path):
    from scripts.c11_auth_import import check_ready, import_auth

    staged = staged_copy(tmp_path / "data")
    for call in (check_ready, import_auth):
        db = scripted(staged, database="buma_local")
        with pytest.raises(ImportBlocked, match="isolated_rehearsal_database_required"):
            await call(db, staged, AUTH_DEFINITIONS)
        assert db.statements == ["SELECT current_database()"]
        db = scripted(staged, database="buma_local")
        with pytest.raises(ImportBlocked, match="verified_empty_database_mismatch"):
            await call(db, staged, AUTH_DEFINITIONS, verified_empty_database="other")
        assert db.writes() == []
    await check_ready(
        scripted(staged, database="buma_local"),
        staged,
        AUTH_DEFINITIONS,
        verified_empty_database="buma_local",
    )
    db = scripted(staged, database="buma_local")
    report = await import_auth(db, staged, AUTH_DEFINITIONS, verified_empty_database="buma_local")
    assert report["imported"] is True and len(db.superusers) == 1


def test_import_snapshot_names_the_database_only_when_asked(tmp_path, monkeypatch):
    import scripts.c11_auth_import as auth

    data, export, schema = tiny_snapshot(tmp_path, monkeypatch)
    diagnostic = c11.diagnose(data, export, source_version="0.22.27")
    calls = {}

    def recorder(name, value):
        async def step(_connection, *_args, **kwargs):
            calls[name] = kwargs
            return value

        return step

    monkeypatch.setattr(auth, "check_ready", recorder("check_ready", None))
    monkeypatch.setattr(c11, "load_isolated", recorder("load_isolated", 1))
    monkeypatch.setattr(c11, "recount_relation_arrays", recorder("recount", []))
    monkeypatch.setattr(auth, "import_auth", recorder("import_auth", {"imported": True}))
    asyncio.run(c11.import_snapshot(ImportEngine(), data, tmp_path / "a", diagnostic, schema))
    for name in ("check_ready", "load_isolated", "import_auth"):
        assert "verified_empty_database" not in calls[name]
    asyncio.run(
        c11.import_snapshot(
            ImportEngine(),
            data,
            tmp_path / "b",
            diagnostic,
            schema,
            verified_empty_database="buma_local",
        )
    )
    for name in ("check_ready", "load_isolated", "import_auth"):
        assert calls[name]["verified_empty_database"] == "buma_local"
    engine = ImportEngine()
    with pytest.raises(ValueError):
        asyncio.run(
            c11.import_snapshot(
                engine, data, tmp_path / "c", diagnostic, schema, verified_empty_database=""
            )
        )
    assert engine.events == [] and not (tmp_path / "c").exists()


# ---------------------------------------------------------------------------
# Step 7: the verified staged storage/ into pb_data/storage.
# ---------------------------------------------------------------------------


def staged_storage(root: Path, mode="directory") -> Path:
    staging = root / "staging"
    files = {
        "col1/rec1/photo_abc.png": b"\x89PNG synthetic",
        "col1/rec1/photo_abc.png.attrs": b'{"user.metadata":{}}',
        "col1/rec1/thumbs_photo_abc.png/100x100_photo_abc.png": b"thumb",
        "col2/rec2/vide.txt": b"",
    }
    for relative, content in files.items():
        path = staging / "storage" / relative
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(content)
    inventory = c11.storage_inventory(staging / "storage")
    if mode == "manifest":
        shutil.rmtree(staging / "storage")
    (staging / "manifest.json").write_text(json.dumps({"storage": inventory, "storage_mode": mode}))
    return staging


def test_files_are_moved_and_every_size_and_sha256_checked(tmp_path):
    staging = staged_storage(tmp_path)
    expected = c11.storage_inventory(staging / "storage")
    data_dir = tmp_path / "pb data"
    (data_dir / "storage").mkdir(parents=True)  # empty: allowed
    result = local.install_files(staging, data_dir)
    size = sum(item["bytes"] for item in expected.values())
    assert result == {"copied": True, "method": "moved", "files": 4, "bytes": size}
    assert c11.storage_inventory(data_dir / "storage") == expected
    assert not (staging / "storage").exists()


def test_files_are_copied_when_the_move_crosses_filesystems(tmp_path, monkeypatch):
    staging = staged_storage(tmp_path)
    expected = c11.storage_inventory(staging / "storage")

    def cross_device(*_args):
        raise OSError(errno.EXDEV, "cross-device link")

    monkeypatch.setattr(local.os, "rename", cross_device)
    result = local.install_files(staging, tmp_path / "pb data")
    assert result["method"] == "copied"
    target = tmp_path / "pb data" / "storage"
    assert c11.storage_inventory(target) == expected
    assert {oct(p.stat().st_mode & 0o777) for p in target.rglob("*") if p.is_file()} == {"0o600"}
    assert {oct(p.stat().st_mode & 0o777) for p in target.rglob("*") if p.is_dir()} == {"0o700"}


def test_a_storage_filled_meanwhile_or_a_changed_file_is_refused(tmp_path):
    staging = staged_storage(tmp_path)
    data_dir = tmp_path / "pb data"
    (data_dir / "storage").mkdir(parents=True)
    (data_dir / "storage" / "arrived.txt").write_text("x")
    found = refusal(lambda: local.install_files(staging, data_dir))
    assert found.code == "storage_target_not_empty"
    assert (staging / "storage").is_dir()
    other = tmp_path / "other"
    other.mkdir()
    staging = staged_storage(other)
    (staging / "storage" / "col2" / "rec2" / "vide.txt").write_bytes(b"changed")
    found = refusal(lambda: local.install_files(staging, tmp_path / "pb other"))
    assert found.code == "storage_copy_mismatch"


def test_manifest_mode_copies_no_file(tmp_path):
    staging = staged_storage(tmp_path, mode="manifest")
    assert local.install_files(staging, tmp_path / "pb data") == {
        "copied": False,
        "files": 0,
        "bytes": 0,
    }
    assert not (tmp_path / "pb data").exists()


# ---------------------------------------------------------------------------
# Step 9: the start command, as the real rehearsal starts PPBase.
# ---------------------------------------------------------------------------


def test_the_start_command_is_the_rehearsal_server_command(tmp_path):
    from scripts.c11_real_rehearsal import PPBaseServer

    url = local.database_url(URL)
    command = local.start_command(url, local.DATA_DIR, 8090, Path("/opt/pp base"), "/usr/bin/py")
    words = shlex.split(command)
    assert words[:3] == ["cd", str(PROJECT), "&&"]
    environment = dict(word.split("=", 1) for word in words[3:8])
    argv = words[8:]
    assert environment == {
        "PYTHONPATH": f"{PROJECT}{os.pathsep}/opt/pp base",
        "PPBASE_DATABASE_URL": "postgresql+asyncpg://buma:secret@127.0.0.1:5433/buma_local",
        "PPBASE_AUTO_MIGRATE": "false",
        "PPBASE_STORAGE_BACKEND": "local",
        "BUMA_MASTER_ENABLE_INTERNAL_JOBS": "false",
    }
    assert argv == [
        "/usr/bin/py", "-m", "ppbase", "serve", "--dir", "pb_data",
        "--migrations-dir", "pb_migrations", "--hooks", "app.hooks.register:register",
        "--host", "127.0.0.1", "--port", "8090", "--no-automigrate",
        "--no-apply-migrations-on-start", "--no-generate-migrations",
    ]  # fmt: skip
    # The rehearsal's PPBaseServer: same arguments but paths and port, same settings.
    server = PPBaseServer("postgresql+asyncpg://c11:x@127.0.0.1:1/db", tmp_path, Path("/opt/pp"))
    rehearsal = server.command_line()
    assert rehearsal[1:4] == argv[1:4]
    assert [w for w in rehearsal[4:] if w.startswith("--")] == [
        w for w in argv[4:] if w.startswith("--")
    ]
    for flag in ("--hooks", "--host"):
        assert rehearsal[rehearsal.index(flag) + 1] == argv[argv.index(flag) + 1]
    for key in (
        "PPBASE_AUTO_MIGRATE",
        "PPBASE_STORAGE_BACKEND",
        "BUMA_MASTER_ENABLE_INTERNAL_JOBS",
    ):
        assert server.env[key] == environment[key]
    # The database URL is in the environment of the server, never in its arguments.
    assert not any("secret" in word or word.startswith("--db") for word in argv)


def test_the_start_command_quotes_paths_with_spaces(tmp_path):
    data_dir = tmp_path / "pb data"
    python = str(tmp_path / "mon venv" / "bin" / "python")
    words = shlex.split(
        local.start_command(local.database_url(URL), data_dir, 18090, tmp_path / "pp b", python)
    )
    assert python in words and str(data_dir.resolve()) in words
    assert words[words.index("--port") + 1] == "18090"


def test_every_flag_of_the_start_command_is_known_to_ppbase_serve():
    done = subprocess.run(
        [sys.executable, "-m", "ppbase", "serve", "--help"],
        capture_output=True,
        text=True,
        timeout=120,
    )
    assert done.returncode == 0, done.stderr
    command = local.start_command(local.database_url(URL), local.DATA_DIR, 8090, PROJECT, "py")
    for flag in (word for word in shlex.split(command) if word.startswith("--")):
        assert flag in done.stdout, flag


def test_the_failure_advice_follows_the_phase(tmp_path):
    lines = []
    job = LocalImport("b", URL, data_dir=tmp_path / "pb", out=lines.append)
    expected = {
        "checks": "Rien n'a été écrit dans la base.",
        "migrations": "supprime-la, recrée-la vide, puis relance.",
        "import": "une seule transaction, annulée : aucune donnée du backup",
        "files": f"supprime la base et le dossier {tmp_path / 'pb' / 'storage'}",
    }
    for phase, text in expected.items():
        job.phase = phase
        lines.clear()
        job.report_failure("message")
        assert lines[1] == "Échec : message" and text in lines[2]


def summary_lines(**changes) -> list[str]:
    lines = []
    job = LocalImport("b", URL, port=8090, out=lines.append)
    state = {**EMPTY, "byte_order": True}
    diagnostic = {
        "rows": 1188247,
        "collections": [{}] * 72,
        "present_referenced_files": 596,
        "preserved_source_anomalies": {
            "dangling_relations": {"references": 195795},
            "missing_files": {"references": 2303},
        },
    }
    auth = {"admins": {"imported_rows": 1}, "warnings": []}
    files = {"copied": True, "files": 1282, "bytes": 5 << 30}
    ppbase = {"dashboard_built": True, "admin_ui": Path("/x/ppbase/admin-ui")}
    arguments = {"state": state, "diagnostic": diagnostic, "auth": auth, "files": files}
    arguments["ppbase"] = ppbase
    for key, value in changes.items():
        arguments[key] = {**arguments[key], **value}
    job.summary(**arguments, command="cd /x && python -m ppbase serve")
    return lines


def test_the_summary_says_how_to_connect_and_what_to_avoid():
    lines = summary_lines()
    text = "\n".join(lines)
    assert "  Records importés : 1\u202f188\u202f247 dans 72 tables" in lines
    assert "  Fichiers : 1\u202f282 objets, 5,0 Go" in lines
    assert "  cd /x && python -m ppbase serve" in lines
    assert "Tableau de bord : http://127.0.0.1:8090/_/" in lines
    assert "aucun superuser à créer" in text and "e-mail de test" in text
    assert "SMTP de la production" in text and "PPBASE_SMTP_*" in text
    for absent in ("npm ci", "octet par octet", "non repris", "copie extraite"):
        assert absent not in text


def test_the_summary_warns_about_what_differs_locally():
    lines = summary_lines(
        state={"byte_order": False},
        files={"copied": False, "files": 0, "bytes": 0},
        ppbase={"dashboard_built": False},
        auth={"warnings": [{"code": "backup_schedule_not_imported"}]},
    )
    text = "\n".join(lines)
    assert "cd /x/ppbase/admin-ui && npm ci && npm run build" in text
    assert "ne trie pas le texte octet par octet comme PocketBase" in text
    assert "aucun (mode manifeste) ; 596 fichiers référencés : 404" in text
    assert "comptes et paramètres (voir D-0027) : backup_schedule_not_imported." in text


def test_the_summary_says_to_create_an_admin_when_none_was_imported():
    imported = "\n".join(summary_lines())
    assert "aucun superuser à créer" in imported
    none = "\n".join(summary_lines(auth={"admins": {"imported_rows": 0}}))
    assert "aucun superuser à créer" not in none
    assert "lien d'installation" in none


def test_no_procfs_or_linux_only_call():
    source = Path(local.__file__).read_text(encoding="utf-8")
    assert "/proc" not in source and "sys.platform" not in source
    from scripts import c11_real_rehearsal

    assert "/proc" not in inspect.getsource(c11_real_rehearsal.migrate_virgin_target)


def test_main_reports_any_failure_with_its_code_and_the_advice(monkeypatch, capsys):
    def blocked_late(job):
        job.phase = "files"
        raise ImportBlocked("symlink_in_storage", detail="never printed")

    monkeypatch.setattr(LocalImport, "execute", blocked_late)
    assert local.main(["backup.zip", URL]) == 1
    printed = capsys.readouterr().out
    assert (
        "Échec : Refus : storage/ contient un lien symbolique (code symlink_in_storage)." in printed
    )
    assert "never printed" not in printed
    assert "Les données du backup sont dans la base" in printed

    def interrupted(job):
        job.phase = "import"
        raise KeyboardInterrupt

    monkeypatch.setattr(LocalImport, "execute", interrupted)
    assert local.main(["backup.zip", URL]) == 130
    assert "une seule transaction, annulée" in capsys.readouterr().out


def test_errors_are_described_without_their_text():
    class Driver(Exception):
        sqlstate = "23505"

    class Wrapped(Exception):
        orig = Driver("Key (id)=(secret-value) already exists")

    assert local.unexpected(Wrapped()) == "erreur inattendue (Wrapped, SQLSTATE 23505)."
    assert "secret-value" not in local.unexpected(Wrapped())


# ---------------------------------------------------------------------------
# Integration: disposable PostgreSQL, real_shape fixture built by PocketBase 0.22.34.
# ---------------------------------------------------------------------------


def docker(*args, timeout: float = 300) -> str:
    return subprocess.check_output(
        ["docker", *map(str, args)], text=True, stderr=subprocess.STDOUT, timeout=timeout
    ).strip()


def free_port() -> int:
    import socket

    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        return sock.getsockname()[1]


class Postgres:
    """This module's own container, found again only by its name and labels."""

    IMAGE = "postgres:16-alpine"
    SCOPE = "c11-ws-l-local-import"

    def __init__(self, root: Path):
        self.run = secrets.token_hex(6)
        self.name = "buma-c11-ws-l-" + self.run
        self.password = secrets.token_hex(16)
        self.root = root
        self.port = None

    def start(self, databases) -> None:
        env_file = self.root / "postgres.env"  # the password never on a command line
        env_file.write_text(
            f"POSTGRES_USER=c11\nPOSTGRES_PASSWORD={self.password}\nPOSTGRES_DB={databases[0]}\n"
        )
        env_file.chmod(0o600)
        try:
            docker(
                "run", "--detach", "--rm", "--name", self.name,
                "--label", f"buma.scope={self.SCOPE}", "--label", f"buma.run={self.run}",
                "--memory", "1g", "--publish", "127.0.0.1::5432", "--env-file", env_file,
                self.IMAGE,
            )  # fmt: skip
        finally:
            env_file.unlink()
        ports = json.loads(docker("inspect", self.name))[0]["NetworkSettings"]["Ports"]
        self.port = int(ports["5432/tcp"][0]["HostPort"])
        deadline = time.monotonic() + 120
        ready = (
            "exec",
            self.name,
            "pg_isready",
            "-U",
            "c11",
            "-d",
            databases[0],
            "-h",
            "127.0.0.1",
        )
        while subprocess.run(["docker", *ready], capture_output=True).returncode:
            assert time.monotonic() < deadline, "PostgreSQL not ready"
            time.sleep(0.2)
        for name in databases[1:]:
            docker("exec", self.name, "psql", "-U", "c11", "-d", databases[0], "-c",
                   f'CREATE DATABASE "{name}"')  # fmt: skip

    def url(self, database: str) -> str:
        return f"postgresql://c11:{self.password}@127.0.0.1:{self.port}/{database}"

    def remove(self) -> None:
        owned = docker(
            "ps", "--all", "--quiet", "--filter", f"name=^/{self.name}$",
            "--filter", f"label=buma.scope={self.SCOPE}", "--filter", f"label=buma.run={self.run}",
        ).split()  # fmt: skip
        for container in owned:
            docker("rm", "--force", "--volumes", container)


def docker_available() -> bool:
    if shutil.which("docker") is None:
        return False
    return subprocess.run(["docker", "info"], capture_output=True, timeout=60).returncode == 0


@pytest.fixture(scope="module")
def integration(tmp_path_factory):
    binary = os.environ.get(POCKETBASE)
    if not binary:
        pytest.skip(f"{POCKETBASE} is not set")
    if not SCHEMA.is_file():
        pytest.skip("BUMA_SCHEMA_EXPORT is not available")
    if fixture.pocketbase_version(Path(binary)) != "0.22.34":
        pytest.fail(f"{POCKETBASE} does not run PocketBase 0.22.34")
    if not docker_available():
        pytest.skip("Docker is not available")
    root = tmp_path_factory.mktemp("c11-local-import") / "dossier avec espaces"
    root.mkdir()
    data = fixture.create_fixture(root / "fixture", Path(binary), SCHEMA, profile="real_shape")
    postgres = Postgres(root)
    try:
        postgres.start(("local_dir", "local_zip", "local_manifest"))
        yield SimpleNamespace(root=root, data=data, binary=Path(binary), postgres=postgres)
    finally:
        postgres.remove()
        assert not docker("ps", "--all", "--quiet", "--filter", f"name=^/{postgres.name}$")


def environment() -> dict:
    return {"BUMA_SCHEMA_EXPORT": str(SCHEMA)}


def source_counts(data: Path) -> dict:
    definitions = json.loads(SCHEMA.read_text())
    with c11.snapshot(data) as db:
        return {
            c["name"]: db.execute(f'SELECT count(*) FROM "{c["name"]}"').fetchone()[0]
            for c in definitions
            if c["type"] != "view"
        }


async def database_content(url: str) -> dict:
    """Every table outside the system schemas: row count and digest of its rows."""
    from sqlalchemy import text

    engine = local.make_engine(local.database_url(url))
    try:
        async with engine.connect() as conn:
            tables = [
                row[0]
                for row in await conn.execute(
                    text(
                        "SELECT format('%I.%I', schemaname, tablename) FROM pg_tables "
                        "WHERE schemaname NOT IN ('pg_catalog', 'information_schema') ORDER BY 1"
                    )
                )
            ]
            content = {}
            for table in tables:
                content[table] = tuple(
                    (
                        await conn.execute(
                            text(
                                f"SELECT count(*), md5(coalesce(string_agg(t::text, '|' "
                                f"ORDER BY t::text), '')) FROM {table} t"
                            )
                        )
                    ).one()
                )
            return content
    finally:
        await engine.dispose()


@contextmanager
def ppbase(command: str, log: Path):
    """PPBase started by the displayed command itself, through a shell."""
    import httpx

    keep = ("PATH", "HOME", "LANG", "LC_ALL", "TZ", "PYTHONDONTWRITEBYTECODE")
    env = {key: os.environ[key] for key in keep if key in os.environ}
    port = int(re.search(r"--port (\d+)", command).group(1))
    with log.open("w") as stream:
        process = subprocess.Popen(
            command,
            shell=True,
            env=env,
            stdout=stream,
            stderr=subprocess.STDOUT,
            start_new_session=True,
        )
    try:
        with httpx.Client(
            base_url=f"http://127.0.0.1:{port}", trust_env=False, timeout=60
        ) as client:
            deadline = time.monotonic() + 180
            while True:
                assert process.poll() is None, "PPBase stopped:\n" + log.read_text()[-3000:]
                try:
                    if client.get("/api/health").status_code == 200:
                        break
                except httpx.TransportError:
                    pass
                assert time.monotonic() < deadline, "PPBase did not start"
                time.sleep(0.2)
            yield client
    finally:
        if process.poll() is None:
            os.killpg(process.pid, signal.SIGTERM)
            try:
                process.wait(timeout=30)
            except subprocess.TimeoutExpired:
                os.killpg(process.pid, signal.SIGKILL)
                process.wait()


def fixture_values(data: Path) -> set[str]:
    """Synthetic values that the output must never show (IDs, file names, accounts)."""
    values = {fixture.ADMIN_EMAIL, fixture.ADMIN_PASSWORD, fixture.PASSWORD, "$2a$"}
    with c11.snapshot(data) as db:
        for (name,) in db.execute("SELECT name FROM _collections WHERE type != 'view'"):
            values.update(row[0] for row in db.execute(f'SELECT id FROM "{name}"'))
        values.update(row[0] for row in db.execute("SELECT tokenKey FROM _admins"))
    values.update(p.name for p in (data / "storage").rglob("*") if p.is_file())
    return values


def assert_value_free(lines: list[str], data: Path) -> None:
    text = "\n".join(lines)
    leaked = sorted(value for value in fixture_values(data) if value and value in text)
    assert leaked == []


def test_folder_import_then_ppbase_serves_the_admin_the_records_and_the_files(integration):
    data, root = integration.data, integration.root
    url = integration.postgres.url("local_dir")
    lines = []
    port = free_port()
    job = LocalImport(
        data,
        url,
        data_dir=root / "pb data",
        work_root=root / "work dir",
        port=port,
        environ=environment(),
        out=lines.append,
    )
    result = job.execute()
    counts = source_counts(data)
    assert result["backup_kind"] == "directory"
    assert result["records"] == sum(counts.values()) and result["tables"] == len(counts)
    assert result["superusers"] == 1 and result["seeds"] == C5_ROWS
    assert result["files"]["copied"] is True
    assert c11.storage_inventory(root / "pb data" / "storage") == c11.storage_inventory(
        data / "storage"
    )
    assert not list((root / "work dir").iterdir())  # staging removed, no private copy left
    command = result["start_command"]
    assert f"  {command}" in lines
    assert_value_free(lines, data)
    definitions = {c["name"]: c for c in json.loads(SCHEMA.read_text())}
    resources = definitions["buma_ressources"]["id"]
    with c11.snapshot(data) as db:
        uploaded = json.loads(
            db.execute("SELECT file FROM buma_ressources WHERE id = 'c11file00000003'").fetchone()[
                0
            ]
        )
    png = next(name for name in uploaded if name.endswith(".png"))
    with ppbase(command, root / "ppbase.log") as client:
        login = client.post(
            "/api/admins/auth-with-password",
            json={"identity": fixture.ADMIN_EMAIL, "password": fixture.ADMIN_PASSWORD},
        )
        assert login.status_code == 200, login.text
        token = login.json()["token"]
        assert login.json()["admin"]["email"] == fixture.ADMIN_EMAIL
        listing = client.get(
            "/api/collections/buma_ressources/records",
            params={"perPage": 50},
            headers={"Authorization": token},
        )
        assert listing.status_code == 200, listing.text
        assert listing.json()["totalItems"] == counts["buma_ressources"]
        for record, name in (("c11file00000001", "one.bin"), ("c11file00000003", png)):
            served = client.get(f"/api/files/{resources}/{record}/{name}")
            assert served.status_code == 200
            assert served.content == (data / "storage" / resources / record / name).read_bytes()
        user = client.post(
            "/api/collections/users/auth-with-password",
            json={"identity": "c11admin", "password": fixture.PASSWORD},
        )
        assert user.status_code == 200, user.text
    # The same command again on this database, now populated: refused at step 1,
    # the database left exactly as it was and nothing written anywhere.
    before = asyncio.run(database_content(url))
    again = LocalImport(
        data,
        url,
        data_dir=root / "pb again",
        work_root=root / "work again",
        port=port,
        environ=environment(),
        out=[].append,
    )
    assert refusal(again.execute).code == "database_not_empty"
    assert again.phase == "checks"
    assert asyncio.run(database_content(url)) == before
    assert not (root / "pb again").exists() and not (root / "work again").exists()


def pocketbase_backup(binary: Path, data: Path, root: Path) -> Path:
    """A backup ZIP made by PocketBase 0.22.34 itself (POST /api/backups) from a copy."""
    import httpx

    copy = root / "copie pocketbase"
    shutil.copytree(data, copy)
    migrations = root / "aucune migration"
    migrations.mkdir()
    port = free_port()
    with (root / "pocketbase.log").open("w") as log:
        server = subprocess.Popen(
            [binary, "serve", "--http", f"127.0.0.1:{port}", "--dir", copy,
             "--migrationsDir", migrations, "--automigrate=false"],
            stdout=log,
            stderr=subprocess.STDOUT,
        )  # fmt: skip
    try:
        with httpx.Client(
            base_url=f"http://127.0.0.1:{port}", trust_env=False, timeout=60
        ) as client:
            deadline = time.monotonic() + 60
            while True:
                assert server.poll() is None
                try:
                    if client.get("/api/health").status_code == 200:
                        break
                except httpx.TransportError:
                    pass
                assert time.monotonic() < deadline
                time.sleep(0.1)
            token = client.post(
                "/api/admins/auth-with-password",
                json={"identity": fixture.ADMIN_EMAIL, "password": fixture.ADMIN_PASSWORD},
            ).json()["token"]
            created = client.post(
                "/api/backups",
                json={"name": "c11_local_test.zip"},
                headers={"Authorization": token},
            )
            assert created.status_code == 204, created.text
    finally:
        server.terminate()
        try:
            server.wait(timeout=20)
        except subprocess.TimeoutExpired:
            server.kill()
            server.wait()
    return copy / "backups" / "c11_local_test.zip"


def test_pocketbase_zip_import_after_a_refused_non_empty_storage(integration):
    root = integration.root / "zip"
    root.mkdir()
    url = integration.postgres.url("local_zip")
    archive = pocketbase_backup(integration.binary, integration.data, root)
    busy = root / "pb occupé"
    (busy / "storage").mkdir(parents=True)
    (busy / "storage" / ".DS_Store").write_bytes(b"finder")
    job = LocalImport(
        archive, url, data_dir=busy, work_root=root / "work", environ=environment(), out=[].append
    )
    assert refusal(job.execute).code == "storage_target_not_empty"
    state = asyncio.run(local.probe_database(local.database_url(url)))
    assert (state["relations"], state["routines"], state["types"], state["extensions"]) == (
        0,
        0,
        0,
        0,
    )
    assert not (root / "work").exists()  # refused before the extraction
    lines = []
    job = LocalImport(
        archive,
        url,
        data_dir=root / "pb data",
        work_root=root / "work",
        port=free_port(),
        environ=environment(),
        out=lines.append,
    )
    result = job.execute()
    extracted = Path(result["extracted"])
    assert extracted.parent.parent == root / "work"
    assert (extracted.parent / local.MANIFEST_NAME).is_file()
    assert sorted(p.name for p in extracted.parent.iterdir()) == ["backup", local.MANIFEST_NAME]
    assert result["backup_kind"] == "directory" and result["files"]["copied"] is True
    inventory = c11.storage_inventory(root / "pb data" / "storage")
    assert inventory == c11.storage_inventory(extracted / "storage")
    assert inventory == c11.storage_inventory(integration.data / "storage")
    assert result["records"] == sum(source_counts(extracted).values())
    assert any(local.MANIFEST_NAME in line and line.startswith("[7/9]") for line in lines)
    assert_value_free(lines, integration.data)


def test_manifest_mode_imports_records_and_accounts_without_file_bytes(integration):
    root = integration.root / "manifeste"
    full = root / "complet"
    full.mkdir(parents=True)
    shutil.copyfile(integration.data / "data.db", full / "data.db")
    shutil.copytree(integration.data / "storage", full / "storage")
    bundle = root / "paquet"
    bundle.mkdir()
    shutil.copyfile(integration.data / "data.db", bundle / "data.db")
    c11.write_json_new(bundle / local.MANIFEST_NAME, c11.directory_manifest(full))
    lines = []
    job = LocalImport(
        bundle,
        integration.postgres.url("local_manifest"),
        data_dir=root / "pb data",
        work_root=root / "work",
        port=free_port(),
        environ=environment(),
        out=lines.append,
    )
    result = job.execute()
    assert result["backup_kind"] == "manifest" and result["files"]["copied"] is False
    assert result["records"] == sum(source_counts(bundle).values()) and result["superusers"] == 1
    assert not (root / "pb data").exists()
    assert not (root / "work").exists() or not list((root / "work").iterdir())
    assert any("aucun octet copié" in line for line in lines)
    assert_value_free(lines, integration.data)
