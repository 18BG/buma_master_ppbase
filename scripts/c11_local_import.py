"""C11: import a PocketBase 0.22.34 backup into an empty local PostgreSQL database.

For the user's own manual tests of PPBase with the BUMA hooks, on a database
the user created and keeps (docs/c11-local-test.md). It is not the production
import tool of the cutover runbook (section 10.2). Run from the root of this
repository:

    python -m scripts.c11_local_import BACKUP POSTGRESQL_URL

BACKUP is the complete PocketBase backup ZIP (data.db and storage/), extracted
with verification by scripts.c11_extract into a new folder under backups/; or
a folder already extracted holding data.db and storage/ (files copied), or
data.db and c11-extraction-manifest.json without storage/ (manifest mode, no
file byte). POSTGRESQL_URL is postgresql://USER:PASSWORD@HOST:PORT/DATABASE.

The command stops at the first refusal, with a French message that holds no
value of the backup (counts, codes and schema names only):

1. the database must be entirely empty (no table, view, sequence, function or
   type outside the system schemas, hence no applied migration): the only
   objective protection, so that nothing existing is overwritten or mixed;
   whether a database is a test database is the user's call, never claimed here;
2. pb_data/storage must be absent or empty;
   (a ZIP is extracted at this point, before anything is written to the database)
3. the BUMA migrations and schema checks of the real rehearsal
   (scripts.c11_real_rehearsal.migrate_virgin_target);
4. the starting rows of migration C5 are removed, and only them: the rows found
   must be exactly those its up() inserts, else nothing is removed;
5. target columns described, then the read-only diagnosis must be ready;
6. scripts.c11_import.import_snapshot loads records, accounts and settings in
   one transaction, told the name of this database (verified_empty_database);
7. the verified staged storage/ is moved (or copied) to pb_data/storage, the
   PocketBase layout PPBase serves, and every size and SHA-256 is checked
   (against c11-extraction-manifest.json too, when there is one);
8. final checks: per-table counts equal to the diagnosis, superusers imported;
9. aggregated summary and the exact command that starts PPBase.

Defaults, no option: source version 0.22.34 (D-0024), schema export from
BUMA_SCHEMA_EXPORT, PPBase data directory pb_data/ at the root of this
repository, port 8090 in the displayed command. The backup is only read
(SQLite mode=ro&immutable=1). Nothing here needs procfs or a Linux-only call
(the importer's peak memory reading falls back to getrusage): it runs on macOS.
An interpreter isolated with -I runs it by path:
python -I scripts/c11_local_import.py BACKUP POSTGRESQL_URL.
"""

from __future__ import annotations

import argparse
import asyncio
import errno
import importlib.util
import json
import os
import re
import secrets
import shlex
import shutil
import sys
import time
import zipfile
from collections import Counter
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path, PurePosixPath

PROJECT = Path(__file__).resolve().parents[1]
if __name__ == "__main__" and not __package__:
    # Run by path (python -I scripts/c11_local_import.py): this checkout is not on
    # sys.path; PPBase comes from the interpreter's environment.
    sys.dont_write_bytecode = True
    if str(PROJECT) not in sys.path:
        sys.path.insert(0, str(PROJECT))

from scripts import c11_extract  # noqa: E402
from scripts import c11_import as importer  # noqa: E402

SOURCE_VERSION = "0.22.34"  # the version of the user's backup (D-0024)
SCHEMA_ENV = "BUMA_SCHEMA_EXPORT"
DATA_DIR = PROJECT / "pb_data"
WORK_ROOT = PROJECT / "backups"
MIGRATIONS = PROJECT / "pb_migrations"
C5_MIGRATION = "1789236846_c5_reference_defaults.py"
MANIFEST_NAME = "c11-extraction-manifest.json"
HOST = "127.0.0.1"
PORT = 8090
DRIVER = "postgresql+asyncpg"
SCHEMES = frozenset({"postgresql", "postgres", DRIVER})
MIN_SERVER_VERSION = 160000  # PPBase supports PostgreSQL 16 and 17
FREE_SPACE_MARGIN = 1 << 30
GIB = 1 << 30
MIB = 1 << 20
# BUMA needs these PPBase features (AGENTS.md: fix/d0019-relation-maxselect,
# 069e0bb or later): D-0019 relation multiplicity, the PocketBase 0.22 error
# mode (D-0034) and the auth record rule context.
PPBASE_FEATURES = ("set_error_compat", "set_auth_record_rule_context")
PPBASE_MODULE = "ppbase.models.field_multiplicity"
INSERT_TABLE = re.compile(r'INSERT INTO "([^"]+)"')

# Every object outside the system schemas, extension members aside: tables,
# views, sequences, indexes (relations), functions and procedures (routines),
# enums, domains and ranges (types: neither row types nor arrays); and every
# extension but plpgsql (neither BUMA nor PPBase creates one: an extension may
# own tables outside public, e.g. pg_cron).
EMPTINESS_SQL = """
WITH spaces AS (
    SELECT oid FROM pg_catalog.pg_namespace
    WHERE nspname NOT IN ('pg_catalog', 'information_schema')
      AND nspname !~ '^pg_(toast|temp_)'
), members AS (
    SELECT classid, objid FROM pg_catalog.pg_depend WHERE deptype = 'e'
)
SELECT
    (SELECT count(*) FROM pg_catalog.pg_class c
      WHERE c.relnamespace IN (SELECT oid FROM spaces)
        AND (c.tableoid, c.oid) NOT IN (SELECT classid, objid FROM members)) AS relations,
    (SELECT count(*) FROM pg_catalog.pg_proc p
      WHERE p.pronamespace IN (SELECT oid FROM spaces)
        AND (p.tableoid, p.oid) NOT IN (SELECT classid, objid FROM members)) AS routines,
    (SELECT count(*) FROM pg_catalog.pg_type t
      WHERE t.typnamespace IN (SELECT oid FROM spaces) AND t.typrelid = 0 AND t.typelem = 0
        AND (t.tableoid, t.oid) NOT IN (SELECT classid, objid FROM members)) AS types,
    (SELECT count(*) FROM pg_catalog.pg_extension WHERE extname <> 'plpgsql') AS extensions
"""
DATABASE_SQL = """
SELECT current_database() AS name,
       current_setting('server_version_num')::int AS version_num,
       current_setting('server_version') AS version,
       pg_catalog.pg_encoding_to_char(encoding) AS encoding
FROM pg_catalog.pg_database WHERE datname = current_database()
"""
# Byte order (SQLite BINARY, the order PocketBase sorts in) puts 'B' before
# 'a', 'Z' before 'a' and 'f' before 'é'; a linguistic collation does not.
TEXT_ORDER_SQL = "SELECT 'a' < 'B' AS a_b, 'Z' < 'a' AS z_a, 'f' < 'é' AS f_e"

ADVICE = {
    "checks": "Rien n'a été écrit dans la base.",
    "migrations": (
        "Aucune donnée du backup n'est dans la base, mais elle a peut-être été migrée : "
        "supprime-la, recrée-la vide, puis relance."
    ),
    "import": (
        "L'import tient en une seule transaction, annulée : aucune donnée du backup n'est "
        "dans la base. Elle reste migrée : supprime-la, recrée-la vide, puis relance."
    ),
    "files": (
        "Les données du backup sont dans la base, mais l'import n'est pas terminé : "
        "supprime la base et le dossier {storage}, recrée la base vide, puis relance."
    ),
    "done": "L'import lui-même est terminé et contrôlé.",
}
BLOCKED_MESSAGES = {
    "schema_export_checksum_mismatch": (
        "le fichier désigné par BUMA_SCHEMA_EXPORT n'est pas l'export corrigé du schéma "
        "(empreinte SHA-256 différente)"
    ),
    "standalone_data_db_required": "data.db est absent du backup, ou c'est un lien symbolique",
    "live_or_incomplete_sqlite_snapshot": (
        "le backup n'est pas fermé : data.db-wal ou data.db-journal n'est pas vide"
    ),
    "data_db_manifest_mismatch": f"data.db ne correspond pas à {MANIFEST_NAME}",
    "local_storage_directory_required": "storage/ n'est pas un dossier",
    "symlink_in_storage": "storage/ contient un lien symbolique",
    "non_regular_storage_object": "storage/ contient un objet qui n'est pas un fichier",
    "target_not_empty": "la base n'est plus vide au moment de l'import",
    "superusers_target_not_empty": "la base n'est plus vide au moment de l'import",
    "settings_target_not_empty": "la base n'est plus vide au moment de l'import",
    "verified_empty_database_mismatch": "la connexion ne vise plus la base vérifiée vide",
}


class LocalImportRefused(Exception):
    """A refusal: a stable code and a French message holding no backup value."""

    def __init__(self, code: str, message: str):
        super().__init__(code)
        self.code = code
        self.message = message


def refused(code: str, message: str) -> LocalImportRefused:
    return LocalImportRefused(code, message)


def blocked(exc: importer.ImportBlocked, context: str) -> LocalImportRefused:
    """An importer refusal: its code (never a value) with a French explanation."""
    reason = BLOCKED_MESSAGES.get(exc.code)
    if reason is None and (exc.code.startswith("manifest") or exc.code.endswith("manifest_entry")):
        reason = f"{MANIFEST_NAME} est invalide"
    text = f"{context} : {reason} (code {exc.code})." if reason else f"{context} (code {exc.code})."
    return refused(exc.code, text)


def size_text(size: int) -> str:
    """Size in French units: 4,6 Go, 12,3 Mo, 2,2 Ko."""
    unit, scale = ("Go", GIB) if size >= GIB else ("Mo", MIB) if size >= MIB else ("Ko", 1024)
    return f"{size / scale:.1f}".replace(".", ",") + " " + unit


def grouped(number: int) -> str:
    """French thousands separator: 1 188 247."""
    return f"{number:,}".replace(",", "\u202f")  # narrow no-break space


# ---------------------------------------------------------------------------
# Arguments and local prerequisites.
# ---------------------------------------------------------------------------


def parse_args(argv=None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        prog="python -m scripts.c11_local_import",
        description=(
            "Importe un backup PocketBase 0.22.34 dans une base PostgreSQL vide que tu as "
            "créée, pour tes tests avec PPBase (docs/c11-local-test.md)."
        ),
        epilog=(
            "Valeurs par défaut : version source 0.22.34, export du schéma lu dans "
            "BUMA_SCHEMA_EXPORT, dossier de données PPBase pb_data/ à la racine du dépôt, "
            "port 8090 dans la commande de démarrage affichée."
        ),
    )
    parser.add_argument(
        "backup",
        type=Path,
        metavar="BACKUP",
        help=(
            "ZIP complet du backup, ou dossier extrait contenant data.db et storage/ "
            f"(ou data.db et {MANIFEST_NAME})"
        ),
    )
    parser.add_argument(
        "database_url",
        metavar="ADRESSE_POSTGRESQL",
        help="postgresql://UTILISATEUR:MOT_DE_PASSE@HÔTE:PORT/BASE d'une base vide",
    )
    return parser.parse_args(argv)


def database_url(raw: str):
    """The asyncpg URL of the given address; refuses other drivers and query strings."""
    from sqlalchemy.engine import make_url
    from sqlalchemy.exc import ArgumentError

    expected = "forme attendue : postgresql://UTILISATEUR:MOT_DE_PASSE@HÔTE:PORT/BASE"
    try:
        url = make_url(raw)
    except (ArgumentError, ValueError):
        raise refused(
            "database_url_invalid", f"Adresse PostgreSQL invalide ({expected})."
        ) from None
    if url.drivername not in SCHEMES:
        raise refused("database_url_invalid", f"Adresse PostgreSQL invalide ({expected}).")
    if not url.database:
        raise refused(
            "database_url_without_database",
            f"L'adresse PostgreSQL doit nommer la base à la fin ({expected}).",
        )
    if url.query:
        raise refused(
            "database_url_with_query",
            "L'adresse PostgreSQL ne doit pas contenir de paramètres après « ? ».",
        )
    return url.set(drivername=DRIVER)


def rendered(url) -> str:
    return url.render_as_string(hide_password=False)


def schema_export(environ) -> Path:
    value = environ.get(SCHEMA_ENV)
    if not value:
        raise refused(
            "schema_export_unset",
            f"La variable {SCHEMA_ENV} n'est pas définie : indique le chemin de "
            "l'export du schéma, par exemple "
            f"export {SCHEMA_ENV}=/chemin/buma-master-pb_schema-corrected.json",
        )
    path = Path(value).expanduser()
    if not path.is_file():
        raise refused(
            "schema_export_missing", f"{SCHEMA_ENV} ne désigne pas un fichier existant : {path}"
        )
    return path


def ppbase_installation() -> dict:
    """The PPBase this interpreter loads: it must carry what BUMA requires."""
    try:
        import ppbase
    except ImportError:
        raise refused(
            "ppbase_missing",
            "PPBase n'est pas installé dans cet environnement Python : active le venv du dépôt "
            "(source .venv/bin/activate) ou lance pip install -e ../ppbase.",
        ) from None

    package = Path(ppbase.__file__).resolve().parent
    missing = [] if importlib.util.find_spec(PPBASE_MODULE) else ["field_multiplicity"]
    missing += [name for name in PPBASE_FEATURES if not callable(getattr(ppbase.pb, name, None))]
    if missing:
        raise refused(
            "ppbase_too_old",
            f"Le PPBase installé ({package}) n'a pas ce qu'exige BUMA "
            f"({', '.join(missing)}) : installe la branche fix/d0019-relation-maxselect "
            "(069e0bb ou plus récente) avec pip install -e ../ppbase.",
        )
    return {
        "root": package.parent,
        # From a source clone, /_/ is served only once admin-ui/ is built (PPBase README).
        "dashboard_built": (package / "admin" / "dist" / "index.html").is_file(),
        "admin_ui": package.parent / "admin-ui",
    }


@dataclass
class Backup:
    kind: str  # "zip", "directory" (storage/ copied) or "manifest" (no file byte)
    source: Path
    data: Path | None  # the folder holding data.db (None before a ZIP is extracted)
    manifest: Path | None  # c11-extraction-manifest.json, when there is one


def resolve_backup(path: Path) -> Backup:
    """ZIP, extracted folder with storage/, or folder with the manifest only."""
    path = Path(path)
    if not importer.path_present(path):
        raise refused("backup_missing", f"Backup introuvable : {path}")
    if path.is_symlink():
        raise refused(
            "backup_symlink", f"Le backup est un lien symbolique : donne son chemin réel ({path})."
        )
    if path.is_dir():
        database = path / "data.db"
        if not database.is_file() or database.is_symlink():
            raise refused("backup_without_data_db", f"Le dossier {path} ne contient pas data.db.")
        manifest = path / MANIFEST_NAME
        if importer.path_present(manifest) and (manifest.is_symlink() or not manifest.is_file()):
            raise refused("manifest_not_a_file", f"{manifest} n'est pas un fichier.")
        manifest = manifest if importer.path_present(manifest) else None
        storage = path / "storage"
        if importer.path_present(storage):
            if storage.is_symlink() or not storage.is_dir():
                raise refused("storage_not_a_directory", f"{storage} n'est pas un dossier.")
            return Backup("directory", path, path, manifest)
        if manifest is None:
            raise refused(
                "backup_without_files",
                f"Le dossier {path} contient data.db mais ni storage/ (les fichiers) ni "
                f"{MANIFEST_NAME} (mode manifeste, sans fichiers).",
            )
        return Backup("manifest", path, path, manifest)
    if not path.is_file() or not zipfile.is_zipfile(path):
        raise refused("backup_not_zip", f"{path} n'est ni un dossier ni un fichier ZIP.")
    try:
        with zipfile.ZipFile(path) as archive:
            names = {info.filename for info in archive.infolist()}
    except (zipfile.BadZipFile, OSError):
        raise refused("backup_not_zip", f"{path} n'est pas un ZIP lisible.") from None
    if "data.db" not in names:
        raise refused(
            "zip_without_data_db",
            "Le ZIP ne contient pas data.db à sa racine : ce n'est pas un backup PocketBase.",
        )
    return Backup("zip", path, None, None)


def zip_declared_bytes(path: Path) -> int:
    with zipfile.ZipFile(path) as archive:
        return sum(info.file_size for info in archive.infolist() if not info.is_dir())


def storage_sizes(root: Path) -> dict[str, int]:
    """Relative path -> size of every file under root, by stat only (no read)."""
    sizes = {}
    for directory, directories, files in os.walk(root, followlinks=False):
        for name in directories + files:
            path = Path(directory) / name
            if path.is_symlink():
                raise refused("symlink_in_storage", f"{root} contient un lien symbolique.")
            if path.is_file():
                sizes[path.relative_to(root).as_posix()] = path.stat().st_size
            elif not path.is_dir():
                raise refused("non_regular_storage_object", f"{root} contient un objet spécial.")
    return sizes


def check_storage_target(data_dir: Path) -> Path:
    """Step 2: pb_data/storage absent or empty; pb_data itself a directory if present."""
    storage = data_dir / "storage"
    if importer.path_present(data_dir) and not data_dir.is_dir():
        raise refused("data_dir_not_a_directory", f"{data_dir} existe mais n'est pas un dossier.")
    if importer.path_present(storage):
        if storage.is_symlink() or not storage.is_dir():
            raise refused(
                "storage_target_not_a_directory", f"{storage} existe mais n'est pas un dossier."
            )
        entries = sum(1 for _ in storage.iterdir())
        if entries:
            raise refused(
                "storage_target_not_empty",
                f"Le dossier des fichiers de PPBase {storage} n'est pas vide ({entries} "
                "élément(s), fichiers cachés compris) : supprime-le ou vide-le, puis relance.",
            )
    return storage


def check_free_space(where: Path, needed: int) -> None:
    probe = where
    while not probe.exists():
        probe = probe.parent
    free = shutil.disk_usage(probe).free
    if free < needed + FREE_SPACE_MARGIN:
        raise refused(
            "insufficient_free_space",
            f"Espace disque insuffisant dans {probe} : il faut environ "
            f"{size_text(needed + FREE_SPACE_MARGIN)} libres, il y en a {size_text(free)}.",
        )


def new_run_dir(work_root: Path) -> Path:
    work_root.mkdir(mode=0o700, parents=True, exist_ok=True)
    stamp = datetime.now(UTC).strftime("%Y%m%dT%H%M%SZ")
    run = work_root / f"c11-local-{stamp}-{secrets.token_hex(3)}"
    run.mkdir(mode=0o700)
    run.chmod(0o700)  # whatever the umask
    return run


# ---------------------------------------------------------------------------
# Database: emptiness, migrations, C5 starting rows, final counts.
# ---------------------------------------------------------------------------


def make_engine(url):
    from sqlalchemy.ext.asyncio import create_async_engine

    # Like PPBase itself (ppbase/db/engine.py): no TLS, bound values never echoed.
    return create_async_engine(url, connect_args={"ssl": "disable"}, hide_parameters=True)


async def database_state(engine) -> dict:
    """Name, version, encoding, objects outside the system schemas, text order."""
    from sqlalchemy import text

    async with engine.connect() as conn:
        state = dict((await conn.execute(text(DATABASE_SQL))).mappings().one())
        state.update((await conn.execute(text(EMPTINESS_SQL))).mappings().one())
        state["byte_order"] = None
        if state["encoding"] == "UTF8":
            order = (await conn.execute(text(TEXT_ORDER_SQL))).mappings().one()
            state["byte_order"] = not order["a_b"] and order["z_a"] and order["f_e"]
    return state


def check_empty(state: dict) -> None:
    """Step 1: PostgreSQL 16+, UTF8 and entirely empty, else nothing is written."""
    if state["version_num"] < MIN_SERVER_VERSION:
        raise refused(
            "postgresql_too_old",
            f"PostgreSQL {state['version']} : PPBase demande PostgreSQL 16 ou plus récent.",
        )
    if state["encoding"] != "UTF8":
        raise refused(
            "database_not_utf8",
            f"La base « {state['name']} » est encodée en {state['encoding']} : il faut UTF8.",
        )
    if state["relations"] or state["routines"] or state["types"] or state["extensions"]:
        raise refused(
            "database_not_empty",
            f"La base « {state['name']} » n'est pas vide ({state['relations']} tables, index, "
            f"vues ou séquences, {state['routines']} fonctions, {state['types']} types, "
            f"{state['extensions']} extensions). "
            "L'import n'accepte qu'une base entièrement vide, pour ne jamais écraser ni "
            "mélanger des données : crée une nouvelle base vide et relance.",
        )


async def probe_database(url) -> dict:
    engine = make_engine(url)
    try:
        return await database_state(engine)
    finally:
        await engine.dispose()


class _SeedRecorder:
    """Stands in for the migration app: records the (table, id) of each INSERT."""

    def __init__(self):
        self.rows: set[tuple[str, str]] = set()

    async def execute_sql(self, sql: str, params: dict | None = None) -> None:
        match = INSERT_TABLE.match(sql)
        if match is None or not params or not isinstance(params.get("p_id"), str):
            raise refused("c5_seeds_unreadable", "Migration C5 illisible : rien n'a été retiré.")
        self.rows.add((match.group(1), params["p_id"]))


async def c5_seed_rows(migrations: Path = MIGRATIONS) -> set[tuple[str, str]]:
    """The (table, id) rows that migration C5's own up() inserts, read from its file."""
    path = migrations / C5_MIGRATION
    spec = importlib.util.spec_from_file_location("_buma_c11_local_c5_defaults", path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    recorder = _SeedRecorder()
    await module.up(recorder)
    return recorder.rows


async def remove_c5_seeds(engine, definitions: list[dict], expected: set) -> dict[str, int]:
    """Step 4: under lock, the rows found must be exactly C5's, else nothing is removed."""
    from sqlalchemy import text

    tables = [c["name"] for c in definitions if c["type"] != "view"]
    async with engine.begin() as conn:
        await conn.execute(
            text(
                "LOCK TABLE "
                + ", ".join(importer.quote(name) for name in tables)
                + " IN ACCESS EXCLUSIVE MODE"
            )
        )
        found = set()
        for name in tables:
            result = await conn.execute(text(f"SELECT id FROM {importer.quote(name)}"))
            found.update((name, row[0]) for row in result)
        if found != expected:
            raise refused(
                "unexpected_rows_after_migrations",
                f"Après les migrations, la base contient {len(found)} lignes au lieu des "
                f"{len(expected)} lignes de départ de la migration C5 : rien n'a été retiré.",
            )
        for name, record in sorted(expected):
            deleted = await conn.execute(
                text(f"DELETE FROM {importer.quote(name)} WHERE id = :id"), {"id": record}
            )
            if deleted.rowcount != 1:
                raise refused("c5_seed_not_removed", "Ligne de départ C5 introuvable.")
        accounts = await conn.execute(
            text(
                'SELECT (SELECT count(*) FROM "_superusers") AS superusers, '
                "(SELECT count(*) FROM \"_params\" WHERE key = 'settings') AS settings"
            )
        )
        if any(accounts.one()):
            raise refused(
                "auth_target_not_empty",
                "Après les migrations, la base contient déjà un superuser ou des paramètres.",
            )
    return dict(sorted(Counter(name for name, _record in expected).items()))


async def migrate_and_describe(url, schema: Path, definitions: list[dict]) -> dict:
    """Steps 3 to 5 (database side): migrations, C5 rows, target columns."""
    from scripts.c11_real_rehearsal import RehearsalFailure, migrate_virgin_target

    # Read before any write: an unreadable C5 migration must not leave a migrated base.
    c5_rows = await c5_seed_rows()
    engine = make_engine(url)
    try:
        try:
            migrated = await migrate_virgin_target(engine, rendered(url), schema, definitions)
        except RehearsalFailure as exc:
            raise refused(
                "migrations_failed",
                f"Les migrations BUMA ou leurs contrôles ont échoué (code {exc.code}).",
            ) from None
        seeds = await remove_c5_seeds(engine, definitions, c5_rows)
        async with engine.connect() as conn:
            spec = await importer.introspect_target(conn, definitions)
    finally:
        await engine.dispose()
    return {"migrated": migrated, "seeds": seeds, "spec": spec}


async def import_backup(url, backup: Backup, staging: Path, diagnostic, definitions, name):
    """Step 6: import_snapshot, told the name of the database checked empty and migrated."""
    engine = make_engine(url)
    evidence: dict = {}
    try:
        async with importer.cancelled_by_termination():
            records = await importer.import_snapshot(
                engine,
                backup.data,
                staging,
                diagnostic,
                definitions,
                manifest=backup.manifest if backup.kind == "manifest" else None,
                report=evidence,
                verified_empty_database=name,
            )
    finally:
        await engine.dispose()
    return records, evidence


async def final_counts(url, diagnostic: dict) -> dict:
    """Step 8 (database side): exact count of every imported table, and superusers."""
    from sqlalchemy import text

    engine = make_engine(url)
    try:
        async with engine.connect() as conn:
            counts = {}
            for collection in diagnostic["collections"]:
                table = importer.quote(collection["name"])
                counts[collection["name"]] = (
                    await conn.execute(text(f"SELECT count(*) FROM {table}"))
                ).scalar_one()
            superusers = (
                await conn.execute(text('SELECT count(*) FROM "_superusers"'))
            ).scalar_one()
    finally:
        await engine.dispose()
    return {"counts": counts, "superusers": superusers}


# ---------------------------------------------------------------------------
# Files: the verified staged storage/ into pb_data/storage.
# ---------------------------------------------------------------------------


def copy_storage(source: Path, target: Path, expected: dict) -> None:
    """Copy file by file (0700 directories, 0600 files); remove the copy on failure."""
    target.mkdir(mode=0o700)
    try:
        created = set()
        for relative in expected:
            parent = target
            for part in PurePosixPath(relative).parts[:-1]:
                parent = parent / part
                if parent not in created:
                    parent.mkdir(mode=0o700, exist_ok=True)
                    created.add(parent)
            destination = target / relative
            shutil.copyfile(source / relative, destination)
            destination.chmod(0o600)
    except BaseException:
        shutil.rmtree(target)
        raise


def install_files(staging: Path, data_dir: Path) -> dict:
    """Step 7: move (or copy) the staged storage/ and check every size and SHA-256.

    The staged copy was verified file by file against the diagnosed inventory
    by import_snapshot; the result is compared with that inventory again.
    """
    staged = json.loads((staging / "manifest.json").read_text())
    expected = staged["storage"]
    if staged["storage_mode"] != "directory":
        return {"copied": False, "files": 0, "bytes": 0}
    source, target = staging / "storage", data_dir / "storage"
    data_dir.mkdir(mode=0o700, parents=True, exist_ok=True)
    if importer.path_present(target):
        try:
            target.rmdir()  # empty at step 2; refused if anything appeared since
        except OSError:
            raise refused(
                "storage_target_not_empty",
                f"Le dossier {target} n'est plus vide : aucun fichier n'a été copié.",
            ) from None
    try:
        os.rename(source, target)
        method = "moved"
    except OSError as exc:
        if exc.errno != errno.EXDEV:
            raise
        copy_storage(source, target, expected)
        method = "copied"
    if importer.storage_inventory(target) != expected:
        raise refused(
            "storage_copy_mismatch",
            f"Les fichiers de {target} ne sont pas identiques au backup (taille ou SHA-256).",
        )
    return {
        "copied": True,
        "method": method,
        "files": len(expected),
        "bytes": sum(item["bytes"] for item in expected.values()),
    }


# ---------------------------------------------------------------------------
# Start command and summary.
# ---------------------------------------------------------------------------


def display_path(path: Path) -> str:
    """Relative to this repository when inside it (the command starts with cd)."""
    resolved = Path(path).resolve()
    try:
        return resolved.relative_to(PROJECT.resolve()).as_posix()
    except ValueError:
        return str(resolved)


def start_command(url, data_dir: Path, port: int, ppbase_root: Path, python: str) -> str:
    """The PPBase command of the real rehearsal (PPBaseServer), for this database.

    The URL goes in the environment of the server, never in its arguments.
    """
    environment = {
        "PYTHONPATH": os.pathsep.join([str(PROJECT), str(ppbase_root)]),
        "PPBASE_DATABASE_URL": rendered(url),
        "PPBASE_AUTO_MIGRATE": "false",
        "PPBASE_STORAGE_BACKEND": "local",
        "BUMA_MASTER_ENABLE_INTERNAL_JOBS": "false",
    }
    argv = [
        python, "-m", "ppbase", "serve", "--dir", display_path(data_dir),
        "--migrations-dir", "pb_migrations", "--hooks", "app.hooks.register:register",
        "--host", HOST, "--port", str(port), "--no-automigrate",
        "--no-apply-migrations-on-start", "--no-generate-migrations",
    ]  # fmt: skip
    assignments = [f"{key}={shlex.quote(value)}" for key, value in environment.items()]
    return " ".join(["cd", shlex.quote(str(PROJECT)), "&&", *assignments, shlex.join(argv)])


def warning_codes(entries) -> list[str]:
    return sorted({e["code"] if isinstance(e, dict) else str(e) for e in entries or []})


# ---------------------------------------------------------------------------
# The command.
# ---------------------------------------------------------------------------


class LocalImport:
    """One run; ``phase`` and ``extracted`` tell what a failure leaves behind."""

    def __init__(
        self,
        backup: Path,
        url: str,
        *,
        data_dir: Path = DATA_DIR,
        work_root: Path = WORK_ROOT,
        port: int = PORT,
        environ=None,
        out=None,
        python: str | None = None,
    ):
        self.backup_path = Path(backup)
        self.raw_url = url
        self.data_dir = Path(data_dir)
        self.work_root = Path(work_root)
        self.port = port
        self.environ = os.environ if environ is None else environ
        self.out = out or (lambda line="": print(line, flush=True))
        self.python = python or sys.executable
        self.phase = "checks"
        self.run_dir: Path | None = None
        self.extracted: Path | None = None
        self.started = time.monotonic()

    def step(self, number: int, text: str) -> None:
        elapsed = round(time.monotonic() - self.started)
        self.out(f"[{number}/9] {text}  ({elapsed} s)")

    def note(self, text: str) -> None:
        self.out(f"      {text}")

    def advice(self) -> str:
        return ADVICE[self.phase].format(storage=self.data_dir / "storage")

    def report_failure(self, message: str) -> None:
        self.out("")
        self.out(f"Échec : {message}")
        self.out(self.advice())
        if self.extracted is not None and self.extracted.exists():
            self.out(
                f"La copie extraite du backup est gardée dans {self.extracted} : redonne ce "
                f"dossier comme BACKUP pour relancer sans réextraire, ou supprime {self.run_dir}."
            )
        elif self.run_dir is not None and self.run_dir.exists():
            self.out(f"{self.run_dir} contient une copie privée du backup : supprime-le.")

    def probe(self, url) -> dict:
        """Step 1: reachable, PostgreSQL 16+, UTF8 and entirely empty."""
        try:
            state = asyncio.run(probe_database(url))
        except (LocalImportRefused, KeyboardInterrupt):
            raise
        except Exception as exc:  # a connection error quotes only what the user typed
            raise refused(
                "database_unreachable",
                f"Connexion à PostgreSQL impossible ({connection_error(exc)}) : vérifie que "
                "PostgreSQL tourne, puis l'hôte, le port, l'utilisateur, le mot de passe et "
                "le nom de la base.",
            ) from None
        check_empty(state)
        return state

    def execute(self) -> dict:
        try:
            return self._execute()
        finally:
            if self.run_dir is not None and self.run_dir.is_dir():
                if not any(self.run_dir.iterdir()):
                    self.run_dir.rmdir()

    def _execute(self) -> dict:
        out = self.out
        out("Import local C11 : backup PocketBase 0.22.34 vers PPBase et PostgreSQL")
        url = database_url(self.raw_url)
        schema = schema_export(self.environ)
        try:
            definitions = importer.load_schema(schema)
        except importer.ImportBlocked as exc:
            raise blocked(exc, "Export du schéma refusé") from None
        version_code, _evidence = importer.source_version_status(SOURCE_VERSION)
        if version_code:
            raise refused(
                "source_version_not_validated",
                f"La version {SOURCE_VERSION} n'est pas validée par ce dépôt "
                f"(code {version_code}).",
            )
        ppbase = ppbase_installation()
        backup = resolve_backup(self.backup_path)
        kinds = {
            "zip": "ZIP complet (extraction vérifiée)",
            "directory": "dossier extrait avec storage/ (fichiers copiés)",
            "manifest": "dossier avec data.db et manifeste, sans storage/ (aucun fichier copié)",
        }
        out(f"      Backup : {kinds[backup.kind]} — {backup.source}")

        # Step 1: the database must be entirely empty.
        state = self.probe(url)
        name = state["name"]
        self.step(1, f"Base « {name} » (PostgreSQL {state['version']}) : entièrement vide.")

        # Step 2: pb_data/storage absent or empty.
        storage_target = check_storage_target(self.data_dir)
        self.step(2, f"Dossier des fichiers de PPBase {storage_target} : absent ou vide.")

        # The backup itself, read only, before anything is written to the database.
        if backup.kind == "zip":
            check_free_space(self.work_root, 2 * zip_declared_bytes(backup.source))
            self.run_dir = new_run_dir(self.work_root)
            backup = self.extract(backup)
        manifest_storage = self.check_backup(backup)
        # Step 1 again right before writing: the extraction may have taken minutes.
        if self.probe(url)["name"] != name:
            raise refused(
                "database_changed", "La connexion ne vise plus la même base : rien n'a été écrit."
            )

        # Steps 3 to 5: migrations, C5 starting rows, target columns, diagnosis.
        self.phase = "migrations"
        self.note("Migrations BUMA et contrôles du schéma en cours…")
        prepared = asyncio.run(migrate_and_describe(url, schema, definitions))
        migrated = prepared["migrated"]
        self.step(
            3,
            f"Migrations BUMA appliquées : {migrated['migrations_applied']} ; schéma vérifié "
            f"({migrated['schema']['definitions']} définitions, {migrated['view_queries_ok']} "
            "vues interrogées).",
        )
        seeds = prepared["seeds"]
        self.step(
            4,
            f"Lignes de départ de la migration C5 retirées : {sum(seeds.values())} "
            f"({len(seeds)} tables), et seulement elles.",
        )
        self.note("Diagnostic du backup en cours (deux à quatre minutes pour le backup complet)…")
        try:
            diagnostic = importer.diagnose(
                backup.data,
                schema,
                source_version=SOURCE_VERSION,
                manifest=backup.manifest if backup.kind == "manifest" else None,
                target_columns=prepared["spec"],
            )
        except importer.ImportBlocked as exc:
            raise blocked(exc, "Diagnostic refusé") from None
        if not diagnostic["ready_for_isolated_rehearsal"]:
            codes = warning_codes(diagnostic["blockers"])
            raise refused(
                "diagnosis_not_ready",
                "Le diagnostic du backup a trouvé des blocages : " + ", ".join(codes) + ".",
            )
        if manifest_storage is not None and backup.kind == "directory":
            if (
                importer.inventory_fingerprint(manifest_storage)
                != diagnostic["file_manifest_sha256"]
            ):
                raise refused(
                    "storage_does_not_match_manifest",
                    f"storage/ ne correspond pas à {MANIFEST_NAME} (taille ou SHA-256).",
                )
        self.step(
            5,
            f"Colonnes cibles décrites ; diagnostic prêt : {grouped(diagnostic['rows'])} "
            f"records dans {len(diagnostic['collections'])} tables, version {SOURCE_VERSION}.",
        )

        # Step 6: one transaction.
        self.phase = "import"
        if self.run_dir is None:
            self.run_dir = new_run_dir(self.work_root)
        staging = self.run_dir / "staging"
        self.note("Import en une seule transaction (cinq à dix minutes pour le backup complet)…")
        try:
            records, evidence = asyncio.run(
                import_backup(url, backup, staging, diagnostic, definitions, name)
            )
        except importer.ImportBlocked as exc:
            raise blocked(exc, "Import refusé") from None
        auth = evidence["auth"]
        admins = auth["admins"]["imported_rows"]
        self.step(
            6,
            f"Import validé : {grouped(records)} records, {admins} administrateur(s), comptes, "
            "secrets de jetons et paramètres.",
        )

        # Steps 7 and 8: files, then the final checks.
        self.phase = "files"
        files = install_files(staging, self.data_dir)
        shutil.rmtree(staging)  # its private copy of data.db is no longer needed
        if files["copied"]:
            self.step(
                7,
                f"Fichiers : {grouped(files['files'])} objets ({size_text(files['bytes'])}) dans "
                f"{storage_target}, taille et SHA-256 de chacun vérifiés"
                + (f", identiques à {MANIFEST_NAME}." if manifest_storage is not None else "."),
            )
        else:
            self.step(7, "Fichiers : aucun octet copié (mode manifeste).")
        final = asyncio.run(final_counts(url, diagnostic))
        differing = [
            c["name"] for c in diagnostic["collections"] if final["counts"][c["name"]] != c["rows"]
        ]
        if differing or sum(final["counts"].values()) != diagnostic["rows"]:
            raise refused(
                "final_counts_mismatch",
                f"{len(differing)} table(s) n'ont pas le nombre de records du diagnostic.",
            )
        if final["superusers"] != admins:
            raise refused(
                "superusers_mismatch",
                f"{final['superusers']} superuser(s) dans la base pour {admins} importé(s).",
            )
        self.step(
            8,
            f"Contrôles finaux : comptes des {len(final['counts'])} tables égaux au diagnostic ; "
            f"{final['superusers']} administrateur(s) importé(s).",
        )
        command = start_command(url, self.data_dir, self.port, ppbase["root"], self.python)
        self.phase = "done"
        self.step(9, "Import terminé.")
        self.summary(state, diagnostic, auth, files, ppbase, command)
        return {
            "database": name,
            "backup_kind": backup.kind,
            "records": records,
            "tables": len(final["counts"]),
            "superusers": final["superusers"],
            "files": files,
            "seeds": seeds,
            "start_command": command,
            "extracted": str(self.extracted) if self.extracted else None,
        }

    def check_backup(self, backup: Backup) -> dict | None:
        """Closed snapshot, manifest (data.db included), storage/ listing and free space.

        Returns the storage inventory the manifest declares (None without one);
        its sizes are compared now by listing, its SHA-256 after the diagnosis.
        """
        try:
            with importer.snapshot(backup.data):
                pass
            checked = None
            if backup.manifest is not None:
                checked = importer.read_manifest(backup.manifest, backup.data / "data.db")
        except importer.ImportBlocked as exc:
            raise blocked(exc, "Backup refusé") from None
        manifest_storage = checked["storage"] if checked else None
        needed = (backup.data / "data.db").stat().st_size
        if backup.kind == "directory":
            sizes = storage_sizes(backup.data / "storage")
            if manifest_storage is not None and sizes != {
                key: item["bytes"] for key, item in manifest_storage.items()
            }:
                raise refused(
                    "storage_does_not_match_manifest",
                    f"storage/ ne correspond pas à {MANIFEST_NAME} (fichiers en plus, en moins "
                    "ou de taille différente).",
                )
            needed += sum(sizes.values())
        # The staged private copy: data.db, and storage/ in directory mode.
        check_free_space(self.work_root, needed)
        return manifest_storage

    def extract(self, backup: Backup) -> Backup:
        destination = self.run_dir / "backup"
        manifest = self.run_dir / MANIFEST_NAME
        self.note(f"Extraction vérifiée du ZIP dans {destination}…")
        last = [0.0]

        def progress(event: dict) -> None:
            if event.get("phase") == "extract" and time.monotonic() - last[0] >= 10:
                last[0] = time.monotonic()
                self.note(
                    f"extraction : {event['members_done']}/{event['members_total']} fichiers, "
                    f"{size_text(event['bytes_done'])} sur {size_text(event['bytes_total'])}"
                )
            elif event.get("phase") == "verify":
                self.note("relecture et vérification de chaque fichier extrait…")

        try:
            summary = c11_extract.extract(backup.source, destination, manifest, progress=progress)
        except c11_extract.ExtractionFailed as exc:
            raise refused(
                "extraction_failed",
                f"Extraction du ZIP refusée ou échouée (code {exc.code}) ; le dossier "
                "d'extraction a été supprimé.",
            ) from None
        self.extracted = destination
        self.note(
            f"ZIP extrait et vérifié : {grouped(summary['files'])} fichiers, "
            f"{size_text(summary['bytes'])} (CRC-32 et SHA-256 de chacun)."
        )
        kind = "directory" if importer.path_present(destination / "storage") else "manifest"
        return Backup(kind, backup.source, destination, manifest)

    def summary(self, state, diagnostic, auth, files, ppbase, command) -> None:
        out = self.out
        anomalies = diagnostic["preserved_source_anomalies"]
        out("")
        out("Résumé")
        out(f"  Base PostgreSQL : « {state['name']} »")
        out(
            f"  Records importés : {grouped(diagnostic['rows'])} dans "
            f"{len(diagnostic['collections'])} tables"
        )
        out(
            f"  Administrateurs PocketBase importés : {auth['admins']['imported_rows']} "
            "(e-mail et mot de passe inchangés)"
        )
        out("  Comptes des applications : mots de passe et sessions repris tels quels")
        if files["copied"]:
            out(f"  Fichiers : {grouped(files['files'])} objets, {size_text(files['bytes'])}")
        else:
            present = grouped(diagnostic["present_referenced_files"])
            out(f"  Fichiers : aucun (mode manifeste) ; {present} fichiers référencés : 404")
        out(
            "  Conservés tels quels (D-0021) : "
            f"{grouped(anomalies['dangling_relations']['references'])} références vers des "
            f"records absents, {grouped(anomalies['missing_files']['references'])} références "
            "de fichiers absents"
        )
        out("")
        out("Démarrer PPBase (copie-colle cette commande, Ctrl-C pour l'arrêter) :")
        out("")
        out(f"  {command}")
        out("")
        out(f"Tableau de bord : http://{HOST}:{self.port}/_/")
        if auth["admins"]["imported_rows"] >= 1:
            out(
                "  Connecte-toi avec l'e-mail et le mot de passe de l'administrateur de ton "
                "PocketBase : il est importé, aucun superuser à créer."
            )
        else:
            out(
                "  Ton backup n'a aucun administrateur : crée-en un avec le lien d'installation "
                "que PPBase affiche à son démarrage."
            )
        out(f"Applications : adresse du serveur http://{HOST}:{self.port}")
        out("")
        out("Attention :")
        out(
            "  - n'utilise pas l'envoi d'un e-mail de test du tableau de bord : il passerait par "
            "le serveur SMTP de la production, dont les réglages sont importés (D-0027) ;"
        )
        out(
            "  - ne définis pas PPBASE_SMTP_* (ni PPBASE_DEV), y compris dans le fichier .env "
            "du dépôt, que PPBase lit : sans ces variables, PPBase n'envoie aucun e-mail."
        )
        if not ppbase["dashboard_built"]:
            out(
                "  - le tableau de bord n'est pas construit dans ton PPBase : lance une fois "
                f"cd {shlex.quote(str(ppbase['admin_ui']))} && npm ci && npm run build"
            )
        if state.get("byte_order") is False:
            out(
                "  - cette base ne trie pas le texte octet par octet comme PocketBase : les "
                "listes triées par texte peuvent sortir dans un autre ordre (D-0041 ; "
                "l'image Docker postgres:16-alpine trie comme PocketBase)."
            )
        skipped = warning_codes(auth.get("warnings"))
        if skipped:
            out(
                "  - avertissements de l'import des comptes et paramètres (voir D-0027) : "
                + ", ".join(skipped)
                + "."
            )
        if self.extracted is not None:
            out(
                f"  - la copie extraite du backup est dans {self.run_dir} : supprime ce dossier "
                "après tes tests."
            )


def connection_error(exc: BaseException) -> str:
    """Type and first line of a connection error (it holds only what the user typed)."""
    cause = getattr(exc, "orig", None) or exc
    text = str(cause).strip().splitlines()[0] if str(cause).strip() else ""
    text = text[:200]
    return f"{type(cause).__name__}: {text}" if text else type(cause).__name__


def unexpected(exc: BaseException) -> str:
    """Type and SQLSTATE only: the text of a driver error can quote a backup value."""
    sqlstate = getattr(getattr(exc, "orig", exc), "sqlstate", None)
    suffix = f", SQLSTATE {sqlstate}" if isinstance(sqlstate, str) else ""
    return f"erreur inattendue ({type(exc).__name__}{suffix})."


def main(argv=None) -> int:
    args = parse_args(argv)
    job = LocalImport(args.backup, args.database_url)
    try:
        job.execute()
    except LocalImportRefused as exc:
        job.report_failure(exc.message)
        return 1
    except importer.ImportBlocked as exc:  # its code is value-free by contract
        job.report_failure(blocked(exc, "Refus").message)
        return 1
    except KeyboardInterrupt:
        job.report_failure("interrompu.")
        return 130
    except Exception as exc:
        job.report_failure(unexpected(exc))
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
