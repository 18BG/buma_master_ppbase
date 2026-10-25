"""WS-G: complete C11 rehearsal on the real backup, without file bytes.

One run makes ``--passes`` complete passes (default 2, compared for
repeatability). Each pass owns a disposable PostgreSQL 16 container (name
buma-c11-ws-g-<random>, labels, loopback port, no host volume, memory and CPU
limits, removed at the end) holding the only database the importer accepts,
buma_c11_rehearsal, and:

1. applies every BUMA migration on the virgin database, verifies the schema
   (definitions, views, physical relation columns), removes the C5 seeds of
   this database only and describes the target columns;
2. diagnoses the snapshot in manifest mode with type checks (child process);
3. imports it through ``run_import``, the only call into the import API
   (child process): ``import_snapshot`` stages data.db, never a file byte, and
   loads records, accounts and settings in one transaction;
4. checks conservation after the commit, in new sessions: counts, per-table
   fingerprints of the converted rows re-read, rowid archive and native
   rowids, the three multiple relation arrays against the raw SQLite lists,
   preserved anomalies, accounts, token configs, settings, schema unchanged
   (indexes and view bodies included), every view's rows and distinct IDs
   against the same view in SQLite;
5. starts an isolated PPBase server (BUMA hooks, jobs and migrations off, a
   fresh data directory without file bytes, the database URL in its
   environment, never on its command line) and reads it over HTTP as an
   anonymous client and as a synthetic superuser created in this database
   only (random credentials, never written anywhere). Expected values come
   from the SQLite source with PocketBase 0.22.34 semantics: totalItems as
   COUNT(DISTINCT id), list and view rules read statically for a request
   without auth, served values modeled from the raw SQLite values
   independently of the importer, expand from the targets that exist, sort
   in SQLite's BINARY order;
6. stops the server, checks that it and the reads changed nothing (the
   synthetic superuser aside), removes the staging copy, the server files and
   the container (by force if its stop fails), and proves it.

The backup directory is only read (SQLite mode=ro&immutable=1); its listing
and data.db SHA-256 are compared before and after the run. The public report
holds aggregates only (counts, statuses, codes, names of the public schema,
durations, sizes) and is written only after an automatic privacy check against
the real values of the snapshot; record-level evidence stays in --private-dir
(0700, files 0600). Token continuity is proven on the synthetic fixture by
WS-C (docs/c11-auth-runtime-report.json), referenced, not repeated here. A
divergence from PocketBase 0.22.34 passes only through --accept-divergence
CODE=D-NNNN naming a recorded decision; every other problem fails the pass.

Run from a BUMA checkout, never from the backup directory, with an isolated
interpreter (-I keeps PYTHONPATH and the current directory out of sys.path;
--ppbase names the PPBase checkout that this script, its children and the
isolated server load):

  .venv/bin/python -I -B scripts/c11_real_rehearsal.py --ppbase PPBASE_ROOT \\
      --data BACKUP_DIR --manifest MANIFEST.json --schema EXPORT.json \\
      --source-version 0.22.34 --report NEW_PUBLIC.json --private-dir NEW_DIR
"""

from __future__ import annotations

import argparse
import ast
import asyncio
import hashlib
import json
import math
import os
import platform
import re
import secrets
import shutil
import signal
import socket
import sqlite3
import stat
import subprocess
import sys
import threading
import time
import traceback
import uuid
from array import array
from bisect import bisect_left
from collections import Counter
from contextlib import contextmanager
from datetime import UTC, datetime
from pathlib import Path
from types import SimpleNamespace
from urllib.parse import quote as url_quote
from urllib.parse import urlsplit

PROJECT = Path(__file__).resolve().parents[1]
SCRIPT = Path(__file__).resolve()
if __name__ == "__main__" and not __package__:
    # Run by path with -I: neither scripts/ nor PYTHONPATH is on sys.path.
    # Only this checkout is added here; PPBase comes from --ppbase.
    sys.dont_write_bytecode = True
    if str(PROJECT) not in sys.path:
        sys.path.insert(0, str(PROJECT))

from scripts import c11_auth_import as auth_import  # noqa: E402
from scripts import c11_import as importer  # noqa: E402

DATABASE = "buma_c11_rehearsal"  # the only name load_isolated and import_auth accept
IMAGE = "postgres:16-alpine"
CONTAINER_PREFIX = "buma-c11-ws-g-"
SCOPE_LABEL = "c11-ws-g-real-rehearsal"
PGDATA = "/var/lib/postgresql/data"
URL_ENV = "C11_WS_G_DATABASE_URL"
MIGRATIONS = PROJECT / "pb_migrations"
SUPERUSERS = "_superusers"
ARCHIVE_TABLES = ("_buma_c11_rowids", "_buma_c11_auth_archive")
MULTIPLE_RELATIONS = (
    ("buma_customer", "pdv"),
    ("buma_pilotage_ticket", "tag"),
    ("buma_wallet", "managers"),
)
AUTH_RUNTIME_REPORT = "docs/c11-auth-runtime-report.json"
DECISIONS = PROJECT / "docs" / "decisions-and-divergences.md"
# PocketBase 0.22.34 MaxPerPage (tools/search/provider.go:17): pages of the
# served-value, sort and full-collection reads.
SERVED_PAGE = 500
# Every record of these collections is read over HTTP and compared with the
# source (the three multiple relations and the auth collections).
FULL_READ_COLLECTIONS = (
    "buma_customer",
    "buma_pilotage_ticket",
    "buma_wallet",
    "users",
    "agent_users",
)
CHILD_TIMEOUT_SECONDS = 7200
VIEW_TIMEOUT_MS = 900_000
HTTP_TIMEOUT_SECONDS = 900
ENV_KEEP = ("PATH", "HOME", "LANG", "LC_ALL", "TZ")
SAMPLE_PAGE = 50
MISMATCH_SAMPLES = 5  # per collection.field, in the private evidence only
# Option keys import_auth may change in an auth collection (token kinds and
# the 0.22 mail templates copied into record auth collections).
IMPORTED_OPTION_KEYS = frozenset(
    {*auth_import.RECORD_TOKENS, *auth_import.SUPERUSER_TOKENS, *auth_import.TEMPLATES}
)

# PocketBase v0.22.34 error bodies: apis/record_crud.go:57 and :155 (list and
# view without rule), apis/api_error.go:32-37 (default not found message).
PB_ADMIN_ONLY = "Only admins can perform this action."
PB_NOT_FOUND = "The requested resource wasn't found."
ENVELOPE_KEYS = frozenset({"page", "perPage", "totalItems", "totalPages", "items"})
SYSTEM_KEYS = frozenset({"id", "created", "updated", "collectionId", "collectionName"})
AUTH_KEYS = frozenset({"username", "email", "emailVisibility", "verified"})
FORBIDDEN_KEYS = frozenset(
    {"password", "passwordHash", "password_hash", "tokenKey", "token_key", "rowid"}
)

# Public aggregates of the 2026-09-28 backup (docs/c11-master-diagnostic-report.json,
# docs/c11-master-detailed-audit.json), compared only when data.db has this SHA-256.
SEPTEMBER_BACKUP_SHA256 = "3539f3eb1c8659421526ede0e47ccc60482aaf0abd2c114282c05719d02b1afd"
SEPTEMBER_EXPECTED = {
    "rows": 1188247,
    "tables": 72,
    "views": 42,
    "relations_checked": 4896135,
    "dangling_references": 195795,
    # collection.field: [references, records, distinct targets]
    "dangling_fields": {
        "buma_article.categorie": [7, 7, 1],
        "buma_customer.type": [1, 1, 1],
        "buma_sale.customer": [115325, 115325, 1097],
        "buma_sim_balance_activity.author": [80461, 80461, 1],
        "buma_wallet.managers": [1, 1, 1],
    },
    "missing_file_references": 2303,
    # collection.field: [references, records, distinct files]
    "missing_file_fields": {
        "buma_ressources.file": [2302, 2302, 2302],
        "buma_wallet.image": [1, 1, 1],
    },
    "manifest_storage_objects": 1282,
    "referenced_files": 2899,
    "present_referenced_files": 596,
    "unreferenced_files": 686,
    "auth_records": {"agent_users": 3, "users": 20},
    "superusers": 1,
    "array_length_distribution": {
        "buma_customer.pdv": {"0": 5495, "1": 331},
        "buma_pilotage_ticket.tag": {"1": 4},
        "buma_wallet.managers": {"0": 10, "1": 1},
    },
    "max_array_element_length": {
        "buma_customer.pdv": 15,
        "buma_pilotage_ticket.tag": 15,
        "buma_wallet.managers": 31,
    },
    "token_configs": 8,
}


# ---------------------------------------------------------------------------
# Failures and small helpers.
# ---------------------------------------------------------------------------


class RehearsalFailure(RuntimeError):
    """A failed check: ``code`` and ``details`` carry names, codes and counts only."""

    def __init__(self, code: str, **details):
        super().__init__(code)
        self.code = code
        self.details = details


def require(condition, code: str, **details) -> None:
    if not condition:
        raise RehearsalFailure(code, **details)


def command(*args, timeout: float = 300) -> str:
    return subprocess.check_output(
        [str(arg) for arg in args], text=True, stderr=subprocess.STDOUT, timeout=timeout
    ).strip()


def safe_value(value, depth: int = 0):
    """Scalars, and lists/dicts of them, as they are; anything else by type name."""
    if value is None or isinstance(value, str | int | float | bool):
        return value
    if depth < 4 and isinstance(value, list | tuple):
        return [safe_value(item, depth + 1) for item in list(value)[:50]]
    if depth < 4 and isinstance(value, dict):
        return {str(k): safe_value(v, depth + 1) for k, v in list(value.items())[:50]}
    return type(value).__name__


def failure_entry(exc: BaseException) -> dict:
    """Value-free description of a failure: never the text of a driver error."""
    if isinstance(exc, RehearsalFailure | importer.ImportBlocked):
        return {"type": type(exc).__name__, "code": exc.code, "details": safe_value(exc.details)}
    sqlstate = getattr(getattr(exc, "orig", exc), "sqlstate", None)
    return {"type": type(exc).__name__, "sqlstate": sqlstate if isinstance(sqlstate, str) else None}


def make_private_dir(path: Path) -> Path:
    path.mkdir(mode=0o700)
    path.chmod(0o700)  # whatever the umask
    return path


def tree_bytes(path: Path) -> int:
    if not path.exists():
        return 0
    if path.is_file():
        return path.stat().st_size
    return sum(p.stat().st_size for p in path.rglob("*") if p.is_file() and not p.is_symlink())


def directory_listing(path: Path) -> dict:
    """Relative name -> [size, mtime_ns, mode] of every entry, recursively (no content)."""
    listing = {}
    for item in sorted(path.rglob("*")):
        info = item.lstat()
        listing[item.relative_to(path).as_posix()] = [
            info.st_size,
            info.st_mtime_ns,
            stat.S_IMODE(info.st_mode),
        ]
    return listing


def inside(path: Path, root: Path) -> bool:
    path, root = Path(path).resolve(), Path(root).resolve()
    return path == root or root in path.parents


def free_port() -> int:
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        return sock.getsockname()[1]


def git_state(path: Path) -> dict:
    try:
        commit = command("git", "-C", path, "rev-parse", "HEAD", timeout=30)
        tracked = command("git", "-C", path, "status", "--porcelain", "--untracked-files=no")
        untracked = command("git", "-C", path, "status", "--porcelain", "--untracked-files=all")
    except (OSError, subprocess.SubprocessError):
        return {"commit": None, "tracked_files_clean": None, "untracked_entries": None}
    return {
        "commit": commit,
        "tracked_files_clean": not tracked,
        "untracked_entries": len([line for line in untracked.splitlines() if line[:2] == "??"]),
    }


def git_ignored(path: Path) -> bool:
    completed = subprocess.run(
        ["git", "-C", str(PROJECT), "check-ignore", "-q", str(path)], capture_output=True
    )
    return completed.returncode == 0


def length_distribution(lengths) -> dict[str, int]:
    return {str(length): count for length, count in sorted(Counter(lengths).items())}


def most_frequent(counter: Counter) -> tuple:
    """(value, count) with the highest count, ties broken by the smallest value."""
    if not counter:
        return None, 0
    return min(counter.items(), key=lambda item: (-item[1], item[0]))


def setting(document, path) -> tuple[bool, object]:
    for key in path:
        if not isinstance(document, dict) or key not in document:
            return False, None
        document = document[key]
    return True, document


def as_json(value):
    return json.loads(value) if isinstance(value, str) else value


class Phases:
    """Wall time of each named phase, with the container memory of that phase.

    ``completed`` lists the phases that ended without an exception: only their
    durations time the runbook.
    """

    def __init__(self):
        self.seconds: dict[str, float] = {}
        self.memory: dict[str, dict] = {}
        self.completed: list[str] = []
        self.monitor = None

    @contextmanager
    def phase(self, name: str):
        started = time.monotonic()
        try:
            yield
            self.completed.append(name)
        finally:
            self.seconds[name] = round(time.monotonic() - started, 3)
            if self.monitor is not None and self.monitor.root is not None:
                self.memory[name] = self.monitor.take_phase()


class DiskMonitor:
    """Lowest free space seen on each watched filesystem (one sample per second)."""

    def __init__(self, paths: dict[str, Path]):
        self.paths = paths
        self.minimum = disk_free(paths)
        self.stop = threading.Event()
        self.thread = threading.Thread(target=self.run, daemon=True)

    def run(self):
        while not self.stop.wait(1.0):
            for label, path in self.paths.items():
                try:
                    self.minimum[label] = min(self.minimum[label], shutil.disk_usage(path).free)
                except OSError:
                    pass

    def start(self):
        self.thread.start()
        return self

    def finish(self) -> dict:
        self.stop.set()
        if self.thread.is_alive():
            self.thread.join()
        return dict(self.minimum)


def disk_free(paths: dict[str, Path]) -> dict:
    return {label: shutil.disk_usage(path).free for label, path in paths.items()}


class Cleanup:
    """Steps run in the order added; every step runs and its outcome is recorded."""

    def __init__(self):
        self.steps: list[tuple[str, object]] = []
        self.results: dict[str, str] = {}

    def add(self, name: str, action) -> None:
        self.steps.append((name, action))

    def run(self) -> dict:
        while self.steps:
            name, action = self.steps.pop(0)
            try:
                action()
            except BaseException as exc:  # recorded; never stops the other steps
                self.results[name] = "failed:" + type(exc).__name__
            else:
                self.results[name] = "done"
        return dict(self.results)

    @property
    def ok(self) -> bool:
        return all(value == "done" for value in self.results.values())


# ---------------------------------------------------------------------------
# Guards: isolated interpreter, inputs and outputs, the owned disposable target.
# ---------------------------------------------------------------------------


def require_isolated_interpreter(flags=None) -> None:
    """Real backup data is read only by an interpreter started with -I."""
    flags = flags if flags is not None else sys.flags
    require(bool(flags.isolated), "isolated_interpreter_required")


def check_paths(args, cwd: Path) -> None:
    """Inputs are read only, outputs are new; private outputs stay private."""
    data = Path(args.data)
    require(data.is_dir() and not data.is_symlink(), "data_directory_required")
    database = data / "data.db"
    require(database.is_file() and not database.is_symlink(), "data_db_required")
    require(not inside(cwd, data), "interpreter_started_inside_the_backup")
    for code_root in (PROJECT, Path(args.ppbase)):
        require(not inside(code_root, data), "code_inside_the_backup")
    require(Path(args.manifest).is_file(), "manifest_required")
    require(Path(args.schema).is_file(), "schema_export_required")
    if getattr(args, "source_migrations", None) is not None:
        check_source_migrations(Path(args.source_migrations))
    report, private = Path(args.report), Path(args.private_dir)
    for name, path in (("report", report), ("private_dir", private)):
        require(not importer.path_present(path), "output_path_must_be_new", output=name)
        require(not inside(path, data), "output_inside_the_backup", output=name)
    require(report.parent.is_dir(), "report_parent_required")
    parent = private.parent
    require(parent.is_dir(), "private_parent_required")
    require(stat.S_IMODE(parent.stat().st_mode) & 0o077 == 0, "private_parent_not_owner_only")
    if inside(private, PROJECT):
        require(git_ignored(private), "private_dir_not_ignored_by_git")
    if inside(report, PROJECT):
        # A published report must come from committed code.
        require(git_state(PROJECT)["tracked_files_clean"] is True, "report_requires_committed_code")


def check_source_migrations(path: Path) -> None:
    """The frozen source's migrationsDir: a directory of *.js migrations that
    holds the source migration of D-0022, never a guess."""
    from scripts.c11_idempotency_source_check import SOURCE_MIGRATION

    require(path.is_dir(), "source_migrations_directory_required")
    migrations = {entry.name for entry in path.glob("*.js") if entry.is_file()}
    require(migrations, "source_migrations_without_js_migrations")
    require(SOURCE_MIGRATION in migrations, "source_migrations_without_the_d0022_migration")


def check_container(info: dict, run_id: str) -> dict:
    """The inspected container is this run's: name, labels, limits, loopback, no host volume."""
    name = str(info.get("Name", "")).lstrip("/")
    labels = (info.get("Config") or {}).get("Labels") or {}
    host = info.get("HostConfig") or {}
    require(name == CONTAINER_PREFIX + run_id, "non_disposable_target", reason="container_name")
    require(labels.get("buma.scope") == SCOPE_LABEL, "non_disposable_target", reason="label")
    require(labels.get("buma.run") == run_id, "non_disposable_target", reason="run_label")
    require(host.get("AutoRemove") is True, "non_disposable_target", reason="auto_remove")
    require(not host.get("Binds"), "non_disposable_target", reason="bind_mount")
    require((host.get("Memory") or 0) > 0, "non_disposable_target", reason="memory_limit")
    require((host.get("NanoCpus") or 0) > 0, "non_disposable_target", reason="cpu_limit")
    anonymous = []
    for mount in info.get("Mounts") or []:
        # Only the anonymous volume the image declares for PGDATA (removed with --rm).
        require(
            mount.get("Type") == "volume"
            and mount.get("Destination") == PGDATA
            and re.fullmatch(r"[0-9a-f]{64}", str(mount.get("Name", ""))),
            "non_disposable_target",
            reason="mount",
        )
        anonymous.append(mount["Name"])
    ports = ((info.get("NetworkSettings") or {}).get("Ports") or {}).get("5432/tcp") or []
    require(len(ports) >= 1, "non_disposable_target", reason="port")
    require(
        all(p.get("HostIp") == "127.0.0.1" for p in ports), "non_disposable_target", reason="ip"
    )
    return {
        "name": name,
        "port": int(ports[0]["HostPort"]),
        "anonymous_volumes": anonymous,
        "memory_limit_bytes": host["Memory"],
        "memory_swap_bytes": host.get("MemorySwap"),
        "nano_cpus": host["NanoCpus"],
        "shm_size_bytes": host.get("ShmSize"),
    }


def check_disposable_url(url: str, container: dict) -> None:
    """The URL can only designate this run's container and the rehearsal database."""
    from sqlalchemy.engine import make_url

    parsed = make_url(url)
    require(parsed.drivername == "postgresql+asyncpg", "non_disposable_target", reason="driver")
    require(parsed.host == "127.0.0.1", "non_disposable_target", reason="host")
    require(parsed.port == container["port"], "non_disposable_target", reason="port")
    require(parsed.database == DATABASE, "non_disposable_target", reason="database")
    require(
        str(container.get("name", "")).startswith(CONTAINER_PREFIX),
        "non_disposable_target",
        reason="container",
    )


def select_ppbase(root: Path) -> dict:
    """Load PPBase from ``root`` and nowhere else; it must carry the D-0019 rule."""
    import importlib
    import importlib.util

    root = Path(root).resolve()
    package = root / "ppbase"
    require((package / "__init__.py").is_file(), "ppbase_checkout_required")
    if "ppbase" not in sys.modules and str(root) not in sys.path:
        sys.path.insert(0, str(root))
    module = importlib.import_module("ppbase")
    require(
        Path(module.__file__).resolve().parent == package,
        "ppbase_not_loaded_from_selected_checkout",
    )
    require(
        importlib.util.find_spec("ppbase.models.field_multiplicity") is not None,
        "ppbase_lacks_d0019_relation_multiplicity",
    )
    return {
        "root": str(root),
        **git_state(root),
        "version": getattr(module, "__version__", None),
        "relation_multiplicity_rule": True,
    }


# ---------------------------------------------------------------------------
# Privacy: the public report against the real values of the snapshot.
# ---------------------------------------------------------------------------

MIN_FULL_LENGTH = 4
MIN_TOKEN_LENGTH = 8
TOKEN_SPLIT = re.compile(r"[^A-Za-z0-9_@+\-]+")
HEX_DIGEST = re.compile(r"(?:[0-9]+:)?(?:[0-9a-f]{40}|[0-9a-f]{64})")
SNAKE_CASE = re.compile(r"[a-z0-9][a-z0-9_]*")  # codes and migration file names
PRIVACY_PATTERNS = {
    "email_address": re.compile(r"[^\s@/]+@[^\s@/]+\.[A-Za-z]{2,}"),
    "bcrypt_hash": re.compile(r"\$2[abxy]\$\d\d\$"),
    "jwt": re.compile(r"eyJ[A-Za-z0-9_\-]{8,}\.[A-Za-z0-9_\-]{8,}"),
    "url_credentials": re.compile(r"://[^/\s:@]+:[^/\s@]+@"),
    "long_token": re.compile(r"[A-Za-z0-9_\-]{30,}"),
}


class HashedValues:
    """Membership of strings without keeping them: sorted 64-bit BLAKE2b keys."""

    LIMIT = 4_000_000

    def __init__(self, values=()):
        self._items = array("Q")
        self._sorted = True
        self._next = self.LIMIT
        for value in values:
            self.add(value)

    @staticmethod
    def key(value: str) -> int:
        digest = hashlib.blake2b(
            value.encode("utf-8", "surrogatepass"), digest_size=8, person=b"c11-ws-g"
        ).digest()
        return int.from_bytes(digest, "big")

    def add(self, value) -> None:
        if isinstance(value, str) and len(value) >= MIN_FULL_LENGTH:
            self._items.append(self.key(value))
            self._sorted = False
            if len(self._items) >= self._next:
                self._compact()

    def _compact(self) -> None:
        unique, previous = array("Q"), None
        for item in sorted(self._items):
            if item != previous:
                unique.append(item)
                previous = item
        self._items, self._sorted = unique, True
        self._next = max(self.LIMIT, 2 * len(unique))

    def __contains__(self, value) -> bool:
        if not isinstance(value, str):
            return False
        if not self._sorted:
            self._compact()
        key = self.key(value)
        index = bisect_left(self._items, key)
        return index < len(self._items) and self._items[index] == key

    def __len__(self) -> int:
        if not self._sorted:
            self._compact()
        return len(self._items)


def candidates(text: str) -> set[str]:
    """The whole string (>= 4 characters) and its tokens (>= 8 characters)."""
    found = {text} if len(text) >= MIN_FULL_LENGTH else set()
    found.update(token for token in TOKEN_SPLIT.split(text) if len(token) >= MIN_TOKEN_LENGTH)
    return found


def report_strings(value, path="$"):
    """(path, string) of every key and string value of a JSON document."""
    if isinstance(value, dict):
        for key, item in value.items():
            yield f"{path}.<key>", str(key)
            yield from report_strings(item, f"{path}.{key}")
    elif isinstance(value, list):
        for index, item in enumerate(value):
            yield from report_strings(item, f"{path}[{index}]")
    elif isinstance(value, str):
        yield path, value


def schema_vocabulary(definitions: list[dict]) -> set[str]:
    """Strings of the public schema export, their tokens and collection.field names."""
    vocabulary = set()
    for _path, text in report_strings(definitions):
        vocabulary.add(text)
        vocabulary.update(candidates(text))
    for collection in definitions:
        for field in collection.get("schema") or []:
            vocabulary.add(f"{collection['name']}.{field['name']}")
    return vocabulary


def code_literals(*paths: Path) -> set[str]:
    """String constants written in these source files, and their tokens: code,
    never backup data (a claim's word may equal a real value by chance)."""
    literals = set()
    for path in paths:
        for node in ast.walk(ast.parse(Path(path).read_text())):
            if isinstance(node, ast.Constant) and isinstance(node.value, str):
                literals.add(node.value)
                literals.update(candidates(node.value))
    return literals


def _benign(pattern: str, token: str) -> bool:
    return pattern == "long_token" and bool(
        HEX_DIGEST.fullmatch(token) or SNAKE_CASE.fullmatch(token)
    )


def privacy_violations(report, sensitive, exempt=frozenset()) -> list[dict]:
    """Paths (never values) of strings that look secret or equal a real value.

    Patterns: e-mail address, bcrypt hash, JWT, credentials in a URL and runs
    of 30+ token characters other than lowercase hex digests and snake_case
    codes. Real values: the whole string and its tokens (``candidates``)
    against ``sensitive``, except ``exempt`` strings (public schema, code
    literals) and hex digests.
    """
    violations = []
    for path, text in report_strings(report):
        for name, pattern in PRIVACY_PATTERNS.items():
            if any(not _benign(name, match.group(0)) for match in pattern.finditer(text)):
                violations.append({"path": path, "reason": name})
                break
        for candidate in candidates(text):
            if candidate in exempt or HEX_DIGEST.fullmatch(candidate):
                continue
            if candidate in sensitive:
                violations.append({"path": path, "reason": "real_value"})
                break
    return violations


def collect_sensitive(data: Path, manifest: Path) -> HashedValues:
    """Every distinct text value of the snapshot, read only.

    Each column of each table (record IDs, relations, orphans, file names, auth
    identities, hashes, token keys, JSON documents and their string elements),
    the PocketBase admins and settings, and every storage key of the manifest
    with its parts.
    """
    values = HashedValues()

    def add(value) -> None:
        if not isinstance(value, str):
            return
        values.add(value)
        if value[:1] in "[{":
            try:
                parsed = json.loads(value)
            except ValueError:
                return
            for _path, text in report_strings(parsed):
                values.add(text)

    with importer.snapshot(data) as db:
        tables = [row[0] for row in db.execute("SELECT name FROM sqlite_master WHERE type='table'")]
        for table in tables:
            columns = [row[1] for row in db.execute(f"PRAGMA table_info({importer.quote(table)})")]
            for column in columns:
                sql = (
                    f"SELECT DISTINCT {importer.quote(column)} FROM {importer.quote(table)} "
                    f"WHERE typeof({importer.quote(column)}) = 'text'"
                )
                for (value,) in db.execute(sql):
                    add(value)
    storage = importer.read_manifest(manifest, data / "data.db")["storage"]
    for key in storage:
        values.add(key)
        for part in key.split("/"):
            values.add(part)
    return values


# ---------------------------------------------------------------------------
# Pure checks of HTTP bodies and comparisons (unit-tested without real data).
# ---------------------------------------------------------------------------


def envelope_problems(body, *, page: int, per_page: int, expected_total: int | None) -> list[str]:
    """PocketBase 0.22 list envelope (tools/search/provider.go): problem codes."""
    if not isinstance(body, dict):
        return ["not_an_object"]
    problems = []
    if set(body) != ENVELOPE_KEYS:
        problems.append("envelope_keys")
    if body.get("page") != page:
        problems.append("page")
    if body.get("perPage") != per_page:
        problems.append("per_page")
    total = body.get("totalItems")
    if isinstance(total, bool) or not isinstance(total, int):
        problems.append("total_items_type")
        total = None
    elif expected_total is not None and total != expected_total:
        problems.append("total_items_mismatch")
    if total is not None and body.get("totalPages") != math.ceil(total / per_page):
        empty = total == 0 and body.get("totalPages") == 1
        problems.append("total_pages_one_for_empty_list" if empty else "total_pages")
    items = body.get("items")
    if not isinstance(items, list):
        problems.append("items_type")
    elif total is not None and len(items) != max(0, min(per_page, total - (page - 1) * per_page)):
        problems.append("items_count")
    return problems


def expected_kind(field: dict) -> str:
    """JSON kind PocketBase 0.22 serves for a field (multiple: IsMultiple)."""
    if field["type"] in importer.LIST_KINDS and importer.source_multiple(field):
        return "list"
    return {"number": "number", "bool": "bool", "json": "any"}.get(field["type"], "string")


def kind_matches(value, kind: str) -> bool:
    if kind == "any":
        return True
    if kind == "list":
        return isinstance(value, list) and all(isinstance(item, str) for item in value)
    if kind == "number":
        return isinstance(value, int | float) and not isinstance(value, bool)
    if kind == "bool":
        return isinstance(value, bool)
    return isinstance(value, str)


def record_shape_problems(
    collection: dict, record, *, email_visible: bool, source: dict | None = None
) -> list[str]:
    """Keys and JSON kinds of a served base/auth record (problem codes, no value).

    A date served as null is ``empty_date_served_as_null`` only when ``source``
    (the raw SQLite row) holds an empty date there; any other null date, or one
    without its source row, is ``kind:<field>``.
    """
    if not isinstance(record, dict):
        return ["not_an_object"]
    problems = []
    keys = set(record) - {"expand"}
    wanted = SYSTEM_KEYS | {field["name"] for field in collection["schema"]}
    if collection["type"] == "auth":
        wanted |= AUTH_KEYS if email_visible else AUTH_KEYS - {"email"}
    if keys & FORBIDDEN_KEYS:
        problems.append("forbidden_key")
    if keys != wanted:
        problems.append("keys")
    if record.get("collectionId") != collection["id"]:
        problems.append("collection_id")
    if record.get("collectionName") != collection["name"]:
        problems.append("collection_name")
    for field in collection["schema"]:
        name = field["name"]
        if name in record and not kind_matches(record[name], expected_kind(field)):
            empty_source = source is not None and source.get(name) in (None, "")
            empty_date = field["type"] == "date" and record[name] is None and empty_source
            problems.append(("empty_date_served_as_null:" if empty_date else "kind:") + name)
    return problems


# ---------------------------------------------------------------------------
# PocketBase 0.22.34 list and view rules for a request without auth, read
# statically (unit-tested). PocketBase applies a list rule as a filter
# (apis/record_crud.go:71-72) and a view rule to the fetch (:158-173). For a
# request without auth record every @request.auth.* field resolves to NULL
# (resolvers/record_field_resolver.go:175-189, record_field_resolve_runner.go:
# 185-194), and tools/search/filter.go:290-366 compiles a comparison with an
# empty operand as follows: both empty ('' or "" or null or NULL) -> '' = ''
# or '' IS NOT ''; one known non-empty literal (text, number, true, false) ->
# '' = literal; a column -> ('' = col OR col IS NULL). LIKE and ordering
# operators with NULL give NULL (:155-176). The expressions are joined in SQL
# order (:87-127): AND binds before OR.
# ---------------------------------------------------------------------------

RULE_TOKEN = re.compile(
    r"""(?P<ws>\s+)
    |(?P<text>'(?:\\.|[^'\\])*'|"(?:\\.|[^"\\])*")
    |(?P<number>-?[0-9]+(?:\.[0-9]+)?)
    |(?P<ident>[A-Za-z_@#][A-Za-z0-9_@#.:]*)
    |(?P<sign>\?!=|\?!~|\?<=|\?>=|\?=|\?~|\?<|\?>|!=|!~|<=|>=|=|~|<|>)
    |(?P<join>&&|\|\|)
    |(?P<open>\()
    |(?P<close>\))""",
    re.X,
)
OPERAND_TOKENS = ("ident", "text", "number")
EQUAL_SIGNS = frozenset({"=", "?="})
NOT_EQUAL_SIGNS = frozenset({"!=", "?!="})
NULL_SIGNS = frozenset({"~", "!~", "?~", "?!~", "<", "<=", ">", ">=", "?<", "?<=", "?>", "?>="})


def rule_tokens(rule: str) -> list[tuple[str, str]] | None:
    tokens, position = [], 0
    while position < len(rule):
        match = RULE_TOKEN.match(rule, position)
        if match is None:
            return None
        position = match.end()
        if match.lastgroup != "ws":
            tokens.append((match.lastgroup, match.group()))
    return tokens


def parse_rule(tokens: list[tuple[str, str]]) -> list | None:
    """[(join, node)]: node ("group", [...]) or ("expr", left, sign, right); None if unparsed."""
    position = 0

    def item():
        nonlocal position
        if position < len(tokens) and tokens[position][0] == "open":
            position += 1
            inner = sequence(nested=True)
            return None if inner is None else ("group", inner)
        if position + 3 > len(tokens):
            return None
        left, sign, right = tokens[position : position + 3]
        if left[0] not in OPERAND_TOKENS or sign[0] != "sign" or right[0] not in OPERAND_TOKENS:
            return None
        position += 3
        return ("expr", left, sign[1], right)

    def sequence(nested: bool):
        nonlocal position
        items, join = [], None
        while True:
            node = item()
            if node is None:
                return None
            items.append((join, node))
            if position == len(tokens):
                return None if nested else items
            kind, value = tokens[position]
            position += 1
            if kind == "close":
                return items if nested else None
            if kind != "join":
                return None
            join = value

    return sequence(nested=False) if tokens else None


def _rule_operand(token: tuple[str, str], ids_never_empty: bool) -> str:
    kind, value = token
    if kind == "text":
        return "empty" if len(value) == 2 else "non_empty"
    if kind == "number":
        return "non_empty"
    if value.startswith("@request.auth.") and ":" not in value:
        return "null"
    if value.lower() == "null":
        return "empty"
    if value.lower() in ("true", "false"):
        return "non_empty"
    # A base or auth record id is never empty (importer record_id_value).
    return "record_id" if value == "id" and ids_never_empty else "other"


def _comparison_never_true(left: str, sign: str, right: str) -> bool:
    if "null" not in (left, right):
        return False
    other = right if left == "null" else left
    if sign in EQUAL_SIGNS:
        return other in ("non_empty", "record_id")  # '' = 'x', '' = id
    if sign in NOT_EQUAL_SIGNS:
        return other in ("empty", "null")  # '' IS NOT ''
    return sign in NULL_SIGNS  # NULL LIKE x, x LIKE NULL, NULL < x


def _sequence_never_true(items: list, ids_never_empty: bool) -> bool:
    branches: list[list] = [[]]
    for join, node in items:
        if join == "||":
            branches.append([])
        branches[-1].append(node)
    return all(any(_node_never_true(n, ids_never_empty) for n in chain) for chain in branches)


def _node_never_true(node, ids_never_empty: bool) -> bool:
    if node[0] == "group":
        return _sequence_never_true(node[1], ids_never_empty)
    _, left, sign, right = node
    return _comparison_never_true(
        _rule_operand(left, ids_never_empty), sign, _rule_operand(right, ids_never_empty)
    )


def anonymous_rule_outcome(rule: str, *, ids_never_empty: bool) -> str:
    """ "never_true" when no record can match without auth, else "undetermined"."""
    tokens = rule_tokens(rule)
    parsed = parse_rule(tokens) if tokens is not None else None
    if parsed is not None and _sequence_never_true(parsed, ids_never_empty):
        return "never_true"
    return "undetermined"


# ---------------------------------------------------------------------------
# PocketBase 0.22.34 serialization of a raw SQLite value, written from its
# source and independent of the importer's conversion (unit-tested). PB reads
# every column as a database/sql NullString (models/record.go:65-91), then
# Record.Set normalizes it (models/record.go:292-325) with
# SchemaField.PrepareValue (models/schema/schema_field.go:305-392) and
# PublicExport serves it (models/record.go:528-560).
# ---------------------------------------------------------------------------


class Unmodeled(ValueError):
    """A raw value outside this model: counted, never compared."""


PB_DATE = re.compile(r"[0-9]{4}-[0-9]{2}-[0-9]{2} [0-9]{2}:[0-9]{2}:[0-9]{2}\.[0-9]{3}Z")
GO_DECIMAL = re.compile(r"[+-]?(?:[0-9]+\.?[0-9]*|\.[0-9]+)(?:[eE][+-]?[0-9]+)?")
# strconv.ParseBool, through cast.ToBool of the NullString text.
GO_PARSE_BOOL = {
    **dict.fromkeys(("1", "t", "T", "TRUE", "true", "True"), True),
    **dict.fromkeys(("0", "f", "F", "FALSE", "false", "False"), False),
}
TEXT_KINDS = frozenset({"text", "email", "url", "editor"})


def pb_is_multiple(field: dict) -> bool:
    """IsMultiple: select and file maxSelect > 1, relation maxSelect nil or > 1
    (models/schema/schema_field.go:626-627, 667-668, 715-716)."""
    options = field.get("options") or {}
    if field["type"] == "relation":
        maximum = options.get("maxSelect")
        return maximum is None or (isinstance(maximum, int) and maximum > 1)
    if field["type"] in ("select", "file"):
        maximum = options.get("maxSelect")
        return isinstance(maximum, int) and not isinstance(maximum, bool) and maximum > 1
    return False


def _no_constant(_):
    raise ValueError("constant")


def pb_date(raw) -> str:
    """types.DateTime served for a stored date: "" when empty (tools/types/datetime.go)."""
    if raw is None or raw == "":
        return ""
    if isinstance(raw, str) and PB_DATE.fullmatch(raw):
        try:
            datetime.fromisoformat(raw[:-1] + "+00:00")
        except ValueError:
            raise Unmodeled("date") from None
        return raw
    raise Unmodeled("date")


def pb_string_list(raw) -> list[str]:
    """list.ToUniqueStringSlice of the NullString text (tools/list/list.go:90-140)."""
    if raw is None or raw == "":
        return []
    if not isinstance(raw, str):
        raise Unmodeled("list")
    items = [raw]
    if "[" in raw:
        try:
            parsed = json.loads(raw, parse_constant=_no_constant)
        except (ValueError, RecursionError):
            parsed = None  # not JSON: the text itself, as Go appends it
        if isinstance(parsed, list):
            if any(item is not None and not isinstance(item, str) for item in parsed):
                raise Unmodeled("list")  # Go's partial unmarshal is not modeled
            items = ["" if item is None else item for item in parsed]
        elif parsed is not None:
            raise Unmodeled("list")
    return [item for item in dict.fromkeys(items) if item != ""]  # NonzeroUniques


def pb_json(raw):
    """JsonRaw served for a stored json value (schema_field.go:312-343)."""
    if raw is None:
        return None
    if isinstance(raw, bool):
        raise Unmodeled("json")
    if isinstance(raw, int):
        return raw
    if isinstance(raw, float):
        if not math.isfinite(raw):
            raise Unmodeled("json")
        return raw
    if not isinstance(raw, str):
        raise Unmodeled("json")
    if raw == "":
        return ""
    if raw in ("null", "true", "false") or raw[0] in '0123456789-"[{':
        try:
            return json.loads(raw, parse_constant=_no_constant)
        except (ValueError, RecursionError):
            pass  # not valid JSON: PocketBase quotes it
    return raw


def pb_served(field: dict, raw):
    """JSON value PocketBase 0.22.34 serves for the raw SQLite value of a schema field."""
    kind = field["type"]
    if kind in TEXT_KINDS:
        if raw is None:
            return ""
        if isinstance(raw, str):
            return raw
        if isinstance(raw, int) and not isinstance(raw, bool):
            return str(raw)
        raise Unmodeled(kind)
    if kind == "number":
        if raw is None:
            return 0.0
        if isinstance(raw, int | float) and not isinstance(raw, bool):
            try:
                value = float(raw)
            except OverflowError:
                raise Unmodeled(kind) from None
            if math.isfinite(value):
                return value
        if isinstance(raw, str) and GO_DECIMAL.fullmatch(raw):
            value = float(raw)
            if math.isfinite(value):  # out of range: ParseFloat fails, cast gives 0
                return value
        raise Unmodeled(kind)
    if kind == "bool":
        return pb_bool(raw)
    if kind == "date":
        return pb_date(raw)
    if kind == "json":
        return pb_json(raw)
    if kind in ("select", "file", "relation"):
        items = pb_string_list(raw)
        if pb_is_multiple(field):
            return items
        return items[-1] if items else ""
    raise Unmodeled(kind)


def pb_bool(raw) -> bool:
    """cast.ToBool of the NullString text: strconv.ParseBool, false otherwise."""
    if raw is None:
        return False
    if isinstance(raw, bool):
        raise Unmodeled("bool")
    if isinstance(raw, int | float):
        return raw == 1  # the text "1" parses as true, "0" and anything else as false
    if isinstance(raw, str):
        return GO_PARSE_BOOL.get(raw, False)
    raise Unmodeled("bool")


def pb_text(raw) -> str:
    if raw is None:
        return ""
    if isinstance(raw, str):
        return raw
    raise Unmodeled("text")


def same_json(left, right) -> bool:
    """Equal JSON values: true is not 1, numbers by value, object keys in any order."""
    if isinstance(left, bool) or isinstance(right, bool):
        return type(left) is type(right) and left == right
    if isinstance(left, int | float) and isinstance(right, int | float):
        if isinstance(left, int) and isinstance(right, int):
            return left == right
        return float(left) == float(right)
    if left is None or right is None:
        return left is None and right is None
    if isinstance(left, str) or isinstance(right, str):
        return isinstance(left, str) and isinstance(right, str) and left == right
    if isinstance(left, list) and isinstance(right, list):
        return len(left) == len(right) and all(map(same_json, left, right))
    if isinstance(left, dict) and isinstance(right, dict):
        return left.keys() == right.keys() and all(same_json(left[k], right[k]) for k in left)
    return False


def same_key_order(left, right) -> bool:
    """Same order of object keys at every level (values already equal)."""
    if isinstance(left, dict) and isinstance(right, dict):
        return list(left) == list(right) and all(same_key_order(left[k], right[k]) for k in left)
    if isinstance(left, list) and isinstance(right, list):
        return all(map(same_key_order, left, right))
    return True


def expected_record(collection: dict, raw: dict, *, email_visible: bool) -> dict:
    """{key: served value or Unmodeled} of one record, as PublicExport builds it."""
    expected: dict = {}

    def put(key, model, value):
        try:
            expected[key] = model(value)
        except Unmodeled as exc:
            expected[key] = exc

    put("id", pb_text, raw.get("id"))
    view = collection["type"] == "view"
    for key in ("created", "updated"):
        # A view omits a zero created/updated (models/record.go:544-550).
        if not view or raw.get(key) not in (None, ""):
            put(key, pb_date, raw.get(key))
    expected["collectionId"] = collection["id"]
    expected["collectionName"] = collection["name"]
    for field in collection["schema"]:
        put(field["name"], lambda value, f=field: pb_served(f, value), raw.get(field["name"]))
    if collection["type"] == "auth":
        put("username", pb_text, raw.get("username"))
        put("verified", pb_bool, raw.get("verified"))
        put("emailVisibility", pb_bool, raw.get("emailVisibility"))
        if email_visible:
            put("email", pb_text, raw.get("email"))
    return expected


def date_keys(collection: dict) -> set[str]:
    """Keys PocketBase 0.22.34 serves as types.DateTime."""
    return {"created", "updated"} | {f["name"] for f in collection["schema"] if f["type"] == "date"}


# Outcomes of a served value that differs from PocketBase's in a named,
# recognizable form -> the problem code (a DIVERGENCES entry). Any other
# difference is "differs" (value_differs, never acceptable).
NAMED_DIFFERENCES = {
    "json_key_order": "jsonb_key_order_normalized",
    "empty_date_null": "empty_date_served_as_null",
    "null_for_zero_value": "null_served_for_pocketbase_zero_value",
    "number_as_text": "number_served_as_text",
    "integer_boolean_as_boolean": "sqlite_integer_boolean_served_as_boolean",
    "concatenation_order": "concatenated_items_in_another_order",
}


def difference_kind(expected, served) -> str:
    """The visible form of a served value unequal to PocketBase's: a
    NAMED_DIFFERENCES outcome, or "differs"."""
    number = isinstance(expected, int | float) and not isinstance(expected, bool)
    if served is None:
        # PrepareValue of a SQL NULL (nil): "" (text, email, url, editor and
        # single select, file, relation), 0, false, [] when multiple.
        zero = expected == "" or expected == [] or expected is False or (number and expected == 0)
        return "null_for_zero_value" if zero else "differs"
    if number and isinstance(served, str) and GO_DECIMAL.fullmatch(served):
        if math.isfinite(float(served)) and float(served) == float(expected):
            return "number_as_text"
    if (
        isinstance(expected, int)
        and not isinstance(expected, bool)
        and expected in (0, 1)
        and isinstance(served, bool)
        and int(served) == expected
    ):
        return "integer_boolean_as_boolean"
    if (
        isinstance(expected, str)
        and isinstance(served, str)
        and "," in expected
        and sorted(expected.split(",")) == sorted(served.split(","))
    ):
        return "concatenation_order"
    return "differs"


def outcome_code(outcome: str, key: str) -> str | None:
    """The problem code of one compared value (None when equal or unmodeled)."""
    if outcome in ("equal", "unmodeled"):
        return None
    if outcome == "differs":
        return f"value_differs:{key}"
    if outcome == "unexpected_key":
        return f"unexpected_key:{key}"
    return f"{NAMED_DIFFERENCES[outcome]}:{key}"


def compare_served_record(collection: dict, served, raw: dict, *, email_visible: bool) -> dict:
    """Outcome per key: equal, unmodeled, differs, unexpected_key, or a
    NAMED_DIFFERENCES outcome (json_key_order of D-0029, an empty source date
    served as null, a SQL NULL served as null, ...)."""
    expected = expected_record(collection, raw, email_visible=email_visible)
    if not isinstance(served, dict):
        return dict.fromkeys(expected, "differs")
    dates = date_keys(collection)
    outcomes = {}
    for key, value in expected.items():
        if isinstance(value, Unmodeled):
            outcomes[key] = "unmodeled"
        elif key in dates and value == "" and key in served and served[key] is None:
            outcomes[key] = "empty_date_null"
        elif key not in served:
            outcomes[key] = "differs"
        elif not same_json(served[key], value):
            outcomes[key] = difference_kind(value, served[key])
        elif not same_key_order(served[key], value):
            outcomes[key] = "json_key_order"
        else:
            outcomes[key] = "equal"
    for key in set(served) - set(expected) - {"expand"}:
        outcomes[key] = "unexpected_key"
    return outcomes


class ServedValues:
    """Aggregated outcomes of the served-value comparison (counts and field names only)."""

    def __init__(self):
        self.records = Counter()
        self.outcomes = Counter()
        self.by_type: dict[str, Counter] = {}
        self.differing: Counter = Counter()
        self.named: dict[str, Counter] = {}
        self.unmodeled: Counter = Counter()

    def add(self, collection: dict, outcomes: dict) -> None:
        self.records[collection["type"]] += 1
        types = {f["name"]: f["type"] for f in collection["schema"]}
        for key, outcome in outcomes.items():
            kind = types.get(key, "system")
            self.outcomes[outcome] += 1
            self.by_type.setdefault(kind, Counter())[outcome] += 1
            name = f"{collection['name']}.{key}"
            if outcome in ("differs", "unexpected_key"):
                self.differing[name] += 1
            elif outcome in NAMED_DIFFERENCES:
                self.named.setdefault(NAMED_DIFFERENCES[outcome], Counter())[name] += 1
            elif outcome == "unmodeled":
                self.unmodeled[name] += 1

    def summary(self) -> dict:
        return {
            "records_compared": dict(sorted(self.records.items())),
            "values": dict(sorted(self.outcomes.items())),
            "values_by_field_type": {
                kind: dict(sorted(counter.items()))
                for kind, counter in sorted(self.by_type.items())
            },
            # value_differs and unexpected keys: never an accepted divergence.
            "differing_fields": dict(sorted(self.differing.items())),
            # Named forms (DIVERGENCES codes): fail unless a decision accepts them.
            "named_differences": {
                code: dict(sorted(fields.items())) for code, fields in sorted(self.named.items())
            },
            "unmodeled_fields": dict(sorted(self.unmodeled.items())),
        }


def expected_expand_ids(stored, existing: set, *, multiple: bool):
    """Expand PocketBase 0.22.34 serves: the existing targets of the served
    list (NonzeroUniques), in that order; one object when single; None (no
    expand key) when none exists (daos/record_expand.go:217-263,
    models/record.go:557-559)."""
    items = stored if isinstance(stored, list) else ([stored] if stored else [])
    valid = [item for item in dict.fromkeys(items) if item != "" and item in existing]
    if not valid:
        return None
    return valid if multiple else valid[0]


def expand_problems(items: list, field: str, expected: list) -> list[str]:
    """Served expand of ``field`` against ``expected`` (one entry per item:
    a list of IDs, one ID, or None for no expand key)."""
    problems = [] if items else ["no_items"]
    if len(items) != len(expected):
        return sorted(set(problems + ["items_differ"]))
    for item, wanted in zip(items, expected, strict=True):
        expand = item.get("expand") if isinstance(item, dict) else None
        if wanted is None:
            if expand == {}:
                problems.append("expand_key_without_related_record")
            elif isinstance(item, dict) and "expand" in item:
                problems.append("expand_served_for_absent_record")
            continue
        value = expand.get(field) if isinstance(expand, dict) else None
        if isinstance(wanted, list):
            served = [r.get("id") if isinstance(r, dict) else None for r in value or []]
            if not isinstance(value, list) or served != wanted:
                problems.append("expand_not_the_existing_targets_in_order")
        elif not isinstance(value, dict) or value.get("id") != wanted:
            problems.append("expand_not_one_object")
    return sorted(set(problems))


def error_body_problems(body, status: int, message: str) -> list[str]:
    """PocketBase 0.22 ApiError body {code, message, data: {}} (apis/api_error.go)."""
    if not isinstance(body, dict):
        return ["not_an_object"]
    problems = []
    keys = set(body)
    if keys == {"status", "message", "data"} and body.get("status") == status:
        # The current PocketBase envelope (v0.23.0 tools/router/error.go:41).
        problems.append("error_status_key_instead_of_code")
    elif keys == {"code", "status", "message", "data"} and body["code"] == body["status"] == status:
        # PPBase's additive envelope: 0.22's code next to status
        # (ppbase docs/error-envelope.md, middleware/error_envelope.py).
        problems.append("error_status_key_next_to_code")
    else:
        if set(body) != {"code", "message", "data"}:
            problems.append("keys")
        if body.get("code") != status:
            problems.append("code")
    if body.get("message") != message:
        problems.append("message")
    if body.get("data") != {}:
        problems.append("data")
    return problems


def rule_kind(rule) -> str:
    return "admin_only" if rule is None else "public" if rule == "" else "expression"


def expected_differences(observed, expected, path: str = "$") -> list[str]:
    """Paths where an observed aggregate differs from its public expected value."""
    if isinstance(expected, dict):
        if not isinstance(observed, dict):
            return [path]
        differences = [f"{path}.{key}:unexpected" for key in sorted(set(observed) - set(expected))]
        for key, value in expected.items():
            if key not in observed:
                differences.append(f"{path}.{key}:missing")
            else:
                differences.extend(expected_differences(observed[key], value, f"{path}.{key}"))
        return differences
    return [] if observed == expected else [path]


def compare_repeatable(first, second, path: str = "$") -> list[str]:
    """Paths where two pass aggregates differ."""
    if isinstance(first, dict) and isinstance(second, dict):
        differences = []
        for key in sorted(set(first) | set(second), key=str):
            if key not in first or key not in second:
                differences.append(f"{path}.{key}:missing")
            else:
                differences.extend(compare_repeatable(first[key], second[key], f"{path}.{key}"))
        return differences
    if isinstance(first, list) and isinstance(second, list):
        if len(first) != len(second):
            return [f"{path}:length"]
        differences = []
        for index, (left, right) in enumerate(zip(first, second, strict=True)):
            differences.extend(compare_repeatable(left, right, f"{path}[{index}]"))
        return differences
    return [] if first == second else [path]


# Signature lines an import archive table gives (target_signature): 'table
# NAME', and its columns, defaults, constraints and indexes as 'KIND NAME.x ...'.
ARCHIVE_LINE_KINDS = frozenset({"column", "default", "constraint", "index"})


def archive_line(line: str) -> bool:
    """Whether a signature line describes an import archive table itself: kind
    'table' and exactly an archive name, or a column, default, constraint or
    index whose object starts with 'ARCHIVE.' (never a name merely containing it)."""
    kind, _, rest = line.partition(" ")
    if kind == "table":
        return rest in ARCHIVE_TABLES
    target = rest.split(" ", 1)[0]
    return kind in ARCHIVE_LINE_KINDS and any(
        target.startswith(table + ".") for table in ARCHIVE_TABLES
    )


def signature_difference(before: list[str], after: list[str]) -> dict:
    """Schema lines added or removed; only the two import archives may appear."""
    added = sorted(set(after) - set(before))
    removed = sorted(set(before) - set(after))
    unexpected = [line for line in added if not archive_line(line)]
    return {
        "lines_added": len(added),
        "lines_removed": len(removed),
        "only_import_archives_added": not unexpected and not removed,
        "unexpected_lines": len(unexpected) + len(removed),
    }


def metadata_changes(before: dict, after: dict) -> dict[str, list[str]]:
    """Collection name -> changed metadata keys (options keys detailed)."""
    changes = {}
    for name in sorted(set(before) | set(after)):
        old, new = before.get(name), after.get(name)
        if old is None or new is None:
            changes[name] = ["collection"]
            continue
        keys = sorted(key for key in set(old) | set(new) if old.get(key) != new.get(key))
        if "options" in keys:
            old_options, new_options = old.get("options") or {}, new.get("options") or {}
            keys.remove("options")
            keys += [
                "options." + key
                for key in sorted(set(old_options) | set(new_options))
                if old_options.get(key) != new_options.get(key)
            ]
        if keys:
            changes[name] = keys
    return changes


def import_changes_allowed(changes: dict[str, list[str]], auth_collections: set[str]) -> bool:
    """import_auth may only change token and template options of auth collections."""
    return all(
        name in auth_collections
        and all(key.startswith("options.") and key[8:] in IMPORTED_OPTION_KEYS for key in keys)
        for name, keys in changes.items()
    )


def runbook_durations(passes: list[dict]) -> dict:
    """Per phase: seconds of every pass in which it completed (runbook timing).

    A phase that raised is left out (its time is partial); the pass status
    stays beside each measure, so a failed HTTP check does not hide the
    import timings.
    """
    durations: dict[str, dict] = {}
    for summary in passes:
        measures = summary.get("measures") or {}
        completed = set(measures.get("completed_phases") or [])
        for name, seconds in (measures.get("phase_seconds") or {}).items():
            if name in completed:
                entry = durations.setdefault(name, {"seconds": [], "passes": []})
                entry["seconds"].append(seconds)
                entry["passes"].append(
                    {"pass": summary.get("pass"), "status": summary.get("status")}
                )
    for entry in durations.values():
        entry["max"] = max(entry["seconds"])
    return durations


# ---------------------------------------------------------------------------
# The owned disposable container.
# ---------------------------------------------------------------------------


class Container:
    """The pass's own PostgreSQL container: created with --rm, stopped, and
    removed by force (with its anonymous volumes) when the stop fails or when
    ``docker run`` failed after the daemon created it."""

    def __init__(self, run_id: str, memory: str, cpus: str, shm: str, workdir: Path):
        self.run_id = run_id
        self.name = CONTAINER_PREFIX + run_id
        self.memory, self.cpus, self.shm = memory, cpus, shm
        self.workdir = workdir
        self.password = secrets.token_hex(24)
        self.attempted = False
        self.created = False
        self.forced_removal = False
        self.id = ""
        self.info: dict = {}
        self.url = ""
        self.dsn = ""

    def start(self) -> dict:
        # The password goes through a 0600 file, not the docker command line.
        env_file = self.workdir / "postgres.env"
        importer.write_bytes_new(
            env_file,
            (
                f"POSTGRES_USER=c11\nPOSTGRES_PASSWORD={self.password}\nPOSTGRES_DB={DATABASE}\n"
            ).encode(),
        )
        self.attempted = True
        try:
            command(
                "docker", "run", "--detach", "--rm", "--name", self.name,
                "--label", f"buma.scope={SCOPE_LABEL}", "--label", f"buma.run={self.run_id}",
                "--memory", self.memory, "--memory-swap", self.memory, "--cpus", self.cpus,
                "--shm-size", self.shm, "--publish", "127.0.0.1::5432",
                "--env-file", env_file, IMAGE,
            )  # fmt: skip
            self.created = True
        except BaseException:
            # A timed-out or interrupted run may have created the container.
            try:
                self.force_remove()
            except (OSError, subprocess.SubprocessError):
                pass  # the cleanup's stop() tries again
            raise
        finally:
            env_file.unlink(missing_ok=True)
        inspected = json.loads(command("docker", "inspect", self.name))[0]
        self.id = inspected["Id"]
        self.info = check_container(inspected, self.run_id)
        port = self.info["port"]
        self.url = f"postgresql+asyncpg://c11:{self.password}@127.0.0.1:{port}/{DATABASE}"
        check_disposable_url(self.url, self.info)
        for _ in range(900):
            try:
                command(
                    "docker", "exec", self.name, "pg_isready", "-U", "c11", "-d", DATABASE,
                    "-h", "127.0.0.1", timeout=30,
                )  # fmt: skip
                break
            except subprocess.CalledProcessError:
                time.sleep(0.2)
        else:
            raise RehearsalFailure("postgresql_not_ready")
        return self.info

    def owned(self) -> list[str]:
        """IDs of containers with this pass's name AND labels (never another's)."""
        listed = command(
            "docker", "ps", "--all", "--quiet", "--filter", f"name=^/{self.name}$",
            "--filter", f"label=buma.scope={SCOPE_LABEL}",
            "--filter", f"label=buma.run={self.run_id}",
        )  # fmt: skip
        return listed.split()

    def present(self) -> bool:
        """Anything still named like this pass's container."""
        return bool(command("docker", "ps", "--all", "--quiet", "--filter", f"name=^/{self.name}$"))

    def force_remove(self) -> None:
        for container_id in self.owned():
            self.forced_removal = True
            command("docker", "rm", "--force", "--volumes", container_id, timeout=300)

    def wait_removed(self, seconds: float) -> bool:
        deadline = time.monotonic() + seconds
        while self.present():
            if time.monotonic() > deadline:
                return False
            time.sleep(0.2)
        return True

    def libc(self) -> str | None:
        """The C library of the image: musl's strcoll compares bytes (strcmp)."""
        try:
            command("docker", "exec", self.name, "sh", "-c", "ls /lib/ld-musl-*", timeout=30)
        except (OSError, subprocess.SubprocessError):
            return None
        return "musl"

    def data_bytes(self) -> int | None:
        try:
            return int(command("docker", "exec", self.name, "du", "-sb", PGDATA).split()[0])
        except (subprocess.SubprocessError, ValueError, IndexError):
            return None

    def stop(self) -> None:
        """Stop (auto-removed by --rm); remove by force whatever remains."""
        if not self.attempted:
            return
        stopped = False
        if self.created:
            try:
                command("docker", "stop", "--time", "20", self.name, timeout=300)
                stopped = True
            except (OSError, subprocess.SubprocessError):
                pass  # removed by force below
        if stopped and self.wait_removed(60):
            return
        self.force_remove()
        require(self.wait_removed(60), "container_not_removed")

    def removal_evidence(self) -> dict:
        """Container gone (auto-removed or forced) and its anonymous volume with it."""
        gone = self.wait_removed(60)
        left = 0
        for volume in self.info.get("anonymous_volumes", []):
            inspected = subprocess.run(
                ["docker", "volume", "inspect", volume], capture_output=True, text=True
            )
            left += inspected.returncode == 0
        return {
            "container_removed": gone,
            "anonymous_volumes_left": left,
            "forced_removal": self.forced_removal,
        }


# ---------------------------------------------------------------------------
# Child processes: diagnosis, import and privacy check, each with its own
# peak memory; the parent reads one JSON line and never their stderr.
# ---------------------------------------------------------------------------


async def run_import(url, data, manifest, staging, diagnostic, definitions, report, batch_size):
    """The only call into the import API (WS-B3).

    ``import_snapshot`` stages data.db (manifest mode: no file byte), then
    loads the records (``load_isolated``) and the accounts and settings
    (``import_auth``) in one transaction, on an engine that hides bound
    parameters. SIGTERM and SIGHUP cancel it: rollback and staging removed.
    """
    from sqlalchemy.ext.asyncio import create_async_engine

    engine = create_async_engine(url, connect_args={"ssl": "disable"}, hide_parameters=True)
    try:
        async with importer.cancelled_by_termination():
            return await importer.import_snapshot(
                engine,
                data,
                staging,
                diagnostic,
                definitions,
                manifest=manifest,
                report=report,
                batch_size=batch_size,
            )
    finally:
        await engine.dispose()


def child_diagnose(params: dict) -> dict:
    spec = importer.target_spec_from_json(json.loads(Path(params["spec"]).read_text()))
    details: dict = {}
    report = importer.diagnose(
        Path(params["data"]),
        Path(params["schema"]),
        source_version=params["source_version"],
        manifest=Path(params["manifest"]),
        target_columns=spec,
        private=details,
    )
    importer.write_json_new(Path(params["diagnostic"]), report, ascii=True)
    importer.write_json_new(Path(params["details"]), details, ascii=True)
    result = {
        "ready": report["ready_for_isolated_rehearsal"],
        "rows": report["rows"],
        "blocker_codes": sorted(
            {str(b.get("code") if isinstance(b, dict) else b) for b in report["blockers"]}
        ),
        "seconds": {"diagnosis": report["elapsed_seconds"]},
    }
    # Runbook step 6: the independent audit (its own SQL reading of every
    # relation and file field) and, with the frozen source, the D-0022 check.
    from scripts import c11_idempotency_source_check, c11_snapshot_audit

    started = time.monotonic()
    audit, _files = c11_snapshot_audit.audit(
        Path(params["data"]),
        source_version=params["source_version"],
        manifest=Path(params["manifest"]),
    )
    result["seconds"]["independent_audit"] = round(time.monotonic() - started, 3)
    importer.write_json_new(Path(params["audit"]), audit, ascii=True)
    result["audit"] = {
        "relations": audit["relations_summary"],
        "missing_file_references": sum(f["missing_references"] for f in audit["files"]["fields"]),
        "array_length_distribution": {
            f"{r['collection']}.{r['field']}": r["array_length_distribution"]
            for r in audit["relations"]
            if "array_length_distribution" in r
        },
        "idempotency": audit["idempotency"],
        "source_unchanged": audit["source_unchanged"],
    }
    if params.get("source_migrations"):
        started = time.monotonic()
        check = c11_idempotency_source_check.check(
            Path(params["data"]), Path(params["source_migrations"])
        )
        result["seconds"]["idempotency_source_check"] = round(time.monotonic() - started, 3)
        result["idempotency_source_check"] = {
            key: check[key]
            for key in (
                "source_migrations",
                "source_migrations_not_applied_in_snapshot",
                "source_index_migration_applied",
                "source_index_conflict_groups",
                "mobile_duplicate_groups",
                "mobile_duplicate_rows",
                "mobile_duplicate_first_row_by_rowid_vs_created",
                "source_unchanged",
            )
        }
    return result


def child_import(params: dict) -> dict:
    load: dict = {}
    staging = Path(params["staging"])
    try:
        count = asyncio.run(
            run_import(
                os.environ[URL_ENV],
                Path(params["data"]),
                Path(params["manifest"]),
                staging,
                json.loads(Path(params["diagnostic"]).read_text()),
                importer.load_schema(Path(params["schema"])),
                load,
                params["batch_size"],
            )
        )
    finally:
        importer.write_json_new(Path(params["load_report"]), load, ascii=True)
    return {
        "rows": count,
        "staging_exists": staging.exists(),
        "staging_bytes": tree_bytes(staging),
        "staging_storage_directory": (staging / "storage").exists(),
    }


def child_privacy(params: dict) -> dict:
    definitions = importer.load_schema(Path(params["schema"]))
    sensitive = collect_sensitive(Path(params["data"]), Path(params["manifest"]))
    from scripts import c11_volume_benchmark

    exempt = schema_vocabulary(definitions) | code_literals(
        SCRIPT,
        Path(importer.__file__),
        Path(auth_import.__file__),
        Path(c11_volume_benchmark.__file__),
    )
    candidate = json.loads(Path(params["candidate"]).read_text())
    violations = privacy_violations(candidate, sensitive, exempt)
    importer.write_json_new(Path(params["output"]), {"violations": violations}, ascii=True)
    return {
        "sensitive_values_hashed": len(sensitive),
        "strings_checked": sum(1 for _ in report_strings(candidate)),
        "violations": len(violations),
    }


CHILDREN = {"diagnose": child_diagnose, "import": child_import, "privacy": child_privacy}


def child_main(role: str, params_path: Path) -> int:
    params = json.loads(params_path.read_text())
    started, status = time.monotonic(), 0
    try:
        select_ppbase(Path(params["ppbase"]))
        result = CHILDREN[role](params)
    except BaseException as exc:  # the parent reads a value-free failure
        traceback.print_exc()  # stderr goes to a private 0600 log
        result, status = {"failure": failure_entry(exc)}, 1
    result["child_seconds"] = round(time.monotonic() - started, 3)
    result["peak_rss_kib"] = importer.peak_rss_kib()
    print(json.dumps(result), flush=True)
    return status


def child_environment(url: str | None = None) -> dict:
    env = {key: os.environ[key] for key in ENV_KEEP if key in os.environ}
    if url:
        env[URL_ENV] = url
    return env


def run_child(role: str, params: dict, workdir: Path, running: list, url=None) -> dict:
    params_path = workdir / f"{role}-params.json"
    importer.write_json_new(params_path, params, ascii=True)
    fd = os.open(workdir / f"{role}.log", os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
    started = time.monotonic()
    with os.fdopen(fd, "w") as log:
        process = subprocess.Popen(
            [sys.executable, "-I", "-B", str(SCRIPT), "--child", role, str(params_path)],
            cwd=workdir,
            env=child_environment(url),
            stdout=subprocess.PIPE,
            stderr=log,
            text=True,
        )
        running.append(process)
        try:
            output, _ = process.communicate(timeout=CHILD_TIMEOUT_SECONDS)
        finally:
            stop_process(process, timeout=180)
    lines = [line for line in output.splitlines() if line.startswith("{")]
    result = json.loads(lines[-1]) if lines else {}
    result["parent_wall_seconds"] = round(time.monotonic() - started, 3)
    if process.returncode != 0 or "failure" in result or not lines:
        raise RehearsalFailure(
            f"{role}_child_failed", returncode=process.returncode, failure=result.get("failure")
        )
    return result


def stop_process(process: subprocess.Popen, timeout: float = 60) -> None:
    """SIGTERM (the import child then rolls back and removes its staging), then SIGKILL."""
    if process.poll() is None:
        process.terminate()
        try:
            process.wait(timeout=timeout)
        except subprocess.TimeoutExpired:
            process.kill()
            process.wait()


# ---------------------------------------------------------------------------
# Target database: preparation, metadata and post-commit evidence.
# ---------------------------------------------------------------------------


def make_engine(url: str):
    from sqlalchemy.ext.asyncio import create_async_engine

    # Hashes, token keys and secrets are read or written: never echo bound values.
    return create_async_engine(url, connect_args={"ssl": "disable"}, hide_parameters=True)


async def scalar(conn, sql: str, params: dict | None = None):
    from sqlalchemy import text

    return (await conn.execute(text(sql), params or {})).scalar_one()


async def rows_of(conn, sql: str, params: dict | None = None) -> list:
    from sqlalchemy import text

    return list((await conn.execute(text(sql), params or {})).mappings().all())


def describe(column) -> str:
    return importer._describe(column)


async def database_facts(engine) -> dict:
    async with engine.connect() as conn:
        facts = {
            name: await scalar(conn, f"SHOW {name}")
            for name in (
                "server_version",
                "shared_buffers",
                "work_mem",
                "maintenance_work_mem",
                "max_wal_size",
                "synchronous_commit",
                "max_parallel_workers_per_gather",
            )
        }
        locale = (
            await rows_of(
                conn,
                "SELECT datcollate, datctype, datlocprovider::text AS provider "
                "FROM pg_database WHERE datname = current_database()",
            )
        )[0]
    return {**facts, **{key: locale[key] for key in ("datcollate", "datctype", "provider")}}


async def database_size(engine) -> int:
    async with engine.connect() as conn:
        return await scalar(conn, "SELECT pg_database_size(current_database())")


async def collections_metadata(engine) -> dict:
    async with engine.connect() as conn:
        rows = await rows_of(
            conn,
            "SELECT id, name, type, system, schema, indexes, list_rule, view_rule, "
            "create_rule, update_rule, delete_rule, options FROM _collections",
        )
    return {
        row["name"]: {
            key: as_json(value) if key in ("schema", "indexes", "options") else value
            for key, value in row.items()
        }
        for row in rows
    }


async def schema_check(url: str, schema: Path) -> dict:
    from scripts.generate_native_baseline import load_schema as translated_schema
    from scripts.verify_complete_schema import read_collections, verify

    report = verify(translated_schema(schema, include_views=True)[0], await read_collections(url))
    require(
        report["ok"],
        "schema_verification_failed",
        missing=len(report["missing"]),
        unexpected=len(report["unexpected"]),
        mismatches=len(report["metadata_mismatches"]),
        relation_errors=len(report["relation_errors"]),
        projection_errors=len(report["view_projection_errors"]),
    )
    return {
        "definitions": report["expected"],
        "base_and_auth": report["expected_base_auth"],
        "views": report["expected_views"],
        "ok": True,
    }


async def prepare_target(engine, url: str, schema: Path, definitions: list[dict]):
    """Every migration on the virgin database, schema checks, seeds of this database removed."""
    from ppbase.services.database_preparation import prepare_database

    from scripts.generate_native_baseline import load_schema as translated_schema
    from scripts.verify_schema_baseline import read_collections as read_physical
    from scripts.verify_schema_baseline import verify_relation_columns
    from scripts.verify_view_queries import verify as verify_views

    async with engine.connect() as conn:
        public = "SELECT count(*) FROM information_schema.tables WHERE table_schema = 'public'"
        require(await scalar(conn, public) == 0, "target_not_virgin")
    applied = await prepare_database(engine, MIGRATIONS)
    files = sorted(p.name for p in MIGRATIONS.glob("*.py") if not p.name.startswith("_"))
    require(len(applied) == len(files), "migrations_not_all_applied", applied=len(applied))
    complete = await schema_check(url, schema)
    columns = verify_relation_columns(definitions, await read_physical(url), "fresh")
    require(columns["ok"], "relation_columns_failed", errors=len(columns["errors"]))
    views = await verify_views(url, translated_schema(schema, include_views=True)[0])
    failed_views = [view["name"] for view in views if view["status"] != "OK"]
    require(not failed_views, "view_queries_failed", views=failed_views)
    tables = [c for c in definitions if c["type"] != "view"]
    seeds: dict[str, int] = {}
    from sqlalchemy import text

    async with engine.begin() as conn:
        # Only the virgin database this pass created: its C5 seeds, never data.
        for c in tables:
            removed = (
                await conn.execute(text(f"DELETE FROM {importer.quote(c['name'])}"))
            ).rowcount
            if removed:
                seeds[c["name"]] = removed
        superusers = await scalar(conn, 'SELECT count(*) FROM "_superusers"')
        settings = await scalar(conn, "SELECT count(*) FROM \"_params\" WHERE key = 'settings'")
    require(superusers == 0 and settings == 0, "auth_target_not_empty")
    async with engine.connect() as conn:
        spec = await importer.introspect_target(conn, definitions)
    return spec, {
        "migrations_applied": len(applied),
        "migration_files": len(files),
        "schema": complete,
        "relation_columns": {
            "mode": "fresh",
            "multiple": dict(columns["multiple"]),
            "single": dict(columns["single"]),
        },
        "multiple_relation_column_types": {
            f"{table}.{field}": describe(spec[table][field]) for table, field in MULTIPLE_RELATIONS
        },
        "view_queries_ok": len(views),
        "seed_rows_removed": sum(seeds.values()),
        "seed_tables": sorted(seeds),
    }


# Plain indexes and view bodies, which the benchmark signature leaves out.
EXTRA_SIGNATURE_SQL = """
SELECT 'index ' || tablename || '.' || indexname || ' ' || indexdef
FROM pg_indexes WHERE schemaname = 'public'
UNION ALL
SELECT 'view ' || c.relname || ' ' || md5(pg_get_viewdef(c.oid))
FROM pg_class c WHERE c.relnamespace = 'public'::regnamespace AND c.relkind IN ('v', 'm')
"""


async def target_signature(engine) -> list[str]:
    """Schema signature: the benchmark's (tables, columns, defaults, constraints,
    triggers, functions, event triggers) plus every index and view body."""
    from sqlalchemy import text

    from scripts.c11_volume_benchmark import target_signature as signature

    lines = await signature(engine)
    async with engine.connect() as conn:
        lines += [row[0] for row in (await conn.execute(text(EXTRA_SIGNATURE_SQL))).all()]
    return sorted(lines)


async def table_fingerprints(engine, definitions: list[dict], spec: dict) -> dict:
    """Full typed re-read of every table in a new session, as load_isolated does.

    Same digest as the importer's (``_reread``: multiset of the typed row
    keys), compared with the importer's digest of the CONVERTED source rows
    taken before COPY. It proves that the committed rows are the converted
    rows, not that the conversion kept each value: the served-value
    comparison (``compare_served_record``) checks that independently.
    """
    result = {}
    async with engine.connect() as conn:
        raw = await conn.get_raw_connection()
        driver = raw.driver_connection
        state = SimpleNamespace(driver=driver, statements=Counter())
        async with driver.transaction(isolation="repeatable_read", readonly=True):
            for c in definitions:
                if c["type"] == "view":
                    continue
                plan = importer.RowPlan(c)
                specs = [spec[c["name"]][column] for column in plan.columns]
                check = importer._Verification()
                await importer._reread(state, c["name"], specs, importer.json_columns(specs), check)
                result[c["name"]] = {
                    "rows": check.target.rows,
                    "fingerprint": check.target.hexdigest(),
                }
    return result


def fingerprint_mismatches(fingerprints: dict, loaded: dict) -> list[str]:
    """Tables whose re-read digest differs from the load report's source or target digest."""
    return sorted(
        name
        for name, value in fingerprints.items()
        if not loaded.get(name)
        or not value["fingerprint"]
        == loaded[name].get("source_fingerprint")
        == loaded[name].get("target_fingerprint")
    ) + sorted(set(loaded) - set(fingerprints))


async def stored_row_hashes(
    engine, tables: list[str], exclude_ids: dict[str, str] | None = None
) -> dict[str, str]:
    """count:sum of PostgreSQL's hash of each stored row, per table (cheap, server side).

    ``exclude_ids`` leaves one row out by id (the synthetic superuser)."""
    result, exclude_ids = {}, exclude_ids or {}
    async with engine.connect() as conn:
        for table in tables:
            where = " WHERE t.id <> :excluded" if table in exclude_ids else ""
            row = (
                await rows_of(
                    conn,
                    "SELECT count(*) AS n, coalesce(sum(hashtextextended(t::text, 0)), 0)::text "
                    f"AS s FROM {importer.quote(table)} t{where}",
                    {"excluded": exclude_ids[table]} if where else None,
                )
            )[0]
            result[table] = f"{row['n']}:{row['s']}"
    return result


async def count_views(engine, definitions: list[dict]) -> tuple[dict, dict, dict]:
    """Rows and distinct IDs of each view on the imported data: counts, seconds, failures."""
    counts, seconds, failures = {}, {}, {}
    for c in definitions:
        if c["type"] != "view":
            continue
        started = time.monotonic()
        try:
            async with engine.connect() as conn:
                await conn.exec_driver_sql(f"SET statement_timeout = {VIEW_TIMEOUT_MS}")
                row = (
                    await rows_of(
                        conn,
                        "SELECT count(*) AS n, count(DISTINCT id) AS d "
                        f"FROM {importer.quote(c['name'])}",
                    )
                )[0]
                counts[c["name"]] = {"rows": row["n"], "distinct_ids": row["d"]}
        except Exception as exc:  # recorded by SQLSTATE, never by message
            failures[c["name"]] = failure_entry(exc)
        seconds[c["name"]] = round(time.monotonic() - started, 3)
    return counts, seconds, failures


VIEW_COUNT_KEYS = ("rows", "distinct_ids")


def view_count_differences(target: dict, source: dict) -> list[str]:
    """Views whose PostgreSQL rows or distinct IDs differ from the SQLite source."""

    def counts(entry):
        return None if entry is None else tuple(entry.get(key) for key in VIEW_COUNT_KEYS)

    return sorted(
        name
        for name in set(target) | set(source)
        if counts(target.get(name)) != counts(source.get(name))
    )


def view_not_compared(view: dict) -> str | None:
    """Why the served values of a view are not compared at all.

    Pages sorted by id pair a served record with a SQLite row only when the
    IDs are distinct and never null. PocketBase 0.22.34 wraps a view whose
    id is not a text field in ``SELECT cast(id as text) id, ... FROM (...)``
    (daos/collection.go:404-445), so a non-text ID would mean a view
    PocketBase did not create.
    """
    if view["rows"] != view["distinct_ids"]:
        return "duplicate_or_null_ids"
    if view.get("non_text_ids"):
        return "non_text_ids"
    return None


# (ROW_NUMBER() OVER ()) as id: a position in an order neither SQLite nor
# PostgreSQL defines, so the same ID may name another row on each engine.
ROW_NUMBER_ID = re.compile(r"ROW_NUMBER\s*\(\s*\)\s*OVER\s*\(\s*\)\s*\)?\s*AS\s+[`\"']?id\b", re.I)


def row_number_ids(collection: dict) -> bool:
    """The view numbers its rows with ROW_NUMBER() OVER () as its id."""
    query = (collection.get("options") or {}).get("query") or ""
    return collection["type"] == "view" and bool(ROW_NUMBER_ID.search(query))


def canonical_json(value):
    """A JSON value for multiset keys: numbers by value, true never 1."""
    if isinstance(value, bool) or value is None or isinstance(value, str):
        return value
    if isinstance(value, int | float):
        return float(value)
    if isinstance(value, list):
        return [canonical_json(item) for item in value]
    if isinstance(value, dict):
        return {str(key): canonical_json(item) for key, item in value.items()}
    return repr(value)


def multiset_comparison(expected: list[dict], served: list) -> tuple[list[str], dict, dict]:
    """A ROW_NUMBER() view compared as multisets of records, IDs aside.

    ``expected`` holds ``expected_record`` of every SQLite row, ``served``
    every record of every page. Keys unmodeled in any row are left out on
    both sides. The IDs must be the same set ("1".."N" on both engines). An
    unmatched source row equal to an unmatched served row on every key but
    one is explained by that key and its ``difference_kind``.
    Returns (problem codes, counts, private samples).
    """
    unmodeled = sorted(
        {k for record in expected for k, value in record.items() if isinstance(value, Unmodeled)}
    )
    ignored = {"id", "expand", *unmodeled}

    def kept(record):
        return (
            {k: v for k, v in record.items() if k not in ignored}
            if isinstance(record, dict)
            else None
        )

    def key(values) -> str:
        if values is None:
            return "<not an object>"
        canonical = {k: canonical_json(v) for k, v in values.items()}
        return json.dumps(canonical, sort_keys=True, ensure_ascii=True)

    expected_rows, served_rows = [kept(r) for r in expected], [kept(r) for r in served]
    wanted = Counter(key(row) for row in expected_rows)
    got = Counter(key(row) for row in served_rows)
    only_source, only_served = wanted - got, got - wanted

    def unmatched(rows, counter) -> list:
        remaining, left = Counter(counter), []
        for row in rows:
            if remaining[key(row)] > 0:
                remaining[key(row)] -= 1
                left.append(row)
        return left

    source_left, served_left = (
        unmatched(expected_rows, only_source),
        unmatched(served_rows, only_served),
    )
    names = sorted({k for row in source_left + served_left if row for k in row})
    index: dict[tuple, list] = {}
    for position, row in enumerate(served_left):
        for name in names if row else ():
            rest = key({k: v for k, v in row.items() if k != name})
            index.setdefault((name, rest), []).append(position)
    used, explained, several = set(), Counter(), 0
    for row in source_left:
        for name in names if row else ():
            rest = key({k: v for k, v in row.items() if k != name})
            free = [p for p in index.get((name, rest), []) if p not in used]
            if free:
                used.add(free[0])
                explained[
                    (name, difference_kind(row.get(name), served_left[free[0]].get(name)))
                ] += 1
                break
        else:
            several += 1
    ids = Counter(str(r.get("id")) for r in expected if not isinstance(r.get("id"), Unmodeled))
    served_ids = Counter(str(r.get("id")) for r in served if isinstance(r, dict))
    problems = sorted({outcome_code(kind, name) for name, kind in explained})
    if several:
        problems.append("rows_differ_from_sqlite_ids_aside")
    if ids != served_ids:
        problems.append("ids_differ_from_sqlite")
    counts = {
        "rows": len(expected),
        "served": len(served),
        "rows_only_in_source": sum(only_source.values()),
        "rows_only_served": sum(only_served.values()),
        # A row served more often than SQLite holds it, and a source row never
        # served: paging that numbers the rows again on each request.
        "rows_served_more_often_than_in_source": sum(
            n for k, n in only_served.items() if wanted[k]
        ),
        "source_rows_never_served": sum(n for k, n in only_source.items() if not got[k]),
        "rows_explained_by_one_key": {
            f"{name}:{kind}": rows for (name, kind), rows in sorted(explained.items())
        },
        "rows_differing_in_several_keys": several,
        "unmodeled_keys": unmodeled,
    }
    samples = {
        "only_in_source": list(only_source)[:MISMATCH_SAMPLES],
        "only_served": list(only_served)[:MISMATCH_SAMPLES],
    }
    return problems, counts, samples


def mismatched_records(source: dict, stored: dict) -> int:
    """Records missing on either side, or with any value different."""
    common = source.keys() & stored.keys()
    return len(source.keys() ^ stored.keys()) + sum(1 for k in common if source[k] != stored[k])


def array_mismatches(source: dict, stored: dict) -> dict:
    """Rows missing on either side, and rows whose list differs (elements or order)."""
    common = source.keys() & stored.keys()
    return {
        "rows_missing_or_extra": len(source.keys() ^ stored.keys()),
        "rows_differing": sum(1 for k in common if source[k] != stored[k]),
    }


# ---------------------------------------------------------------------------
# Source facts (read only, private): accounts, arrays and HTTP samples.
# ---------------------------------------------------------------------------


def relation_target(definitions: list[dict], collection: str, field: str) -> str:
    by_id = {c["id"]: c["name"] for c in definitions}
    by_name = {c["name"]: c for c in definitions}
    options = next(f for f in by_name[collection]["schema"] if f["name"] == field)["options"]
    return by_id[options["collectionId"]]


def source_facts(data: Path, definitions: list[dict], inventory: dict) -> dict:
    """Values the post-commit and HTTP checks compare with; kept in memory only."""
    q = importer.quote
    facts: dict = {"auth": {}, "arrays": {}, "samples": {}, "views": {}, "seconds": {}}
    with importer.snapshot(data) as db:
        for c in definitions:
            if c["type"] != "auth":
                continue
            sql = (
                "SELECT id, username, email, emailVisibility, verified, passwordHash, tokenKey "
                f"FROM {q(c['name'])}"
            )
            facts["auth"][c["name"]] = {
                row["id"]: (
                    row["username"],
                    row["email"],
                    bool(row["emailVisibility"]),
                    bool(row["verified"]),
                    row["passwordHash"],
                    row["tokenKey"],
                )
                for row in db.execute(sql)
            }
        facts["admins"] = {
            row["id"]: (row["email"], row["passwordHash"], row["tokenKey"], row["avatar"])
            for row in db.execute('SELECT id, email, passwordHash, tokenKey, avatar FROM "_admins"')
        }
        settings = db.execute("SELECT value FROM \"_params\" WHERE key = 'settings'").fetchall()
        facts["settings"] = json.loads(settings[0][0]) if len(settings) == 1 else None
        # Every view in SQLite, PocketBase's engine: rows, the distinct IDs
        # PocketBase counts as totalItems (tools/search/provider.go:238-254),
        # and the IDs SQLite does not hold as text (ROW_NUMBER() OVER ()).
        started = time.monotonic()
        for c in definitions:
            if c["type"] == "view":
                rows, distinct, non_text = db.execute(
                    "SELECT count(*), count(DISTINCT id), "
                    f"coalesce(sum(typeof(id) != 'text'), 0) FROM {q(c['name'])}"
                ).fetchone()
                facts["views"][c["name"]] = {
                    "rows": rows,
                    "distinct_ids": distinct,
                    "non_text_ids": non_text,
                }
        facts["seconds"]["sqlite_view_counts"] = round(time.monotonic() - started, 3)
        samples = facts["samples"]
        for table, field in MULTIPLE_RELATIONS:
            arrays = {}
            for record_id, value in db.execute(f"SELECT id, {q(field)} FROM {q(table)}"):
                parsed = json.loads(value)
                require(
                    isinstance(parsed, list) and all(isinstance(i, str) for i in parsed),
                    "source_array_not_a_list_of_strings",
                    field=f"{table}.{field}",
                )
                arrays[record_id] = parsed
            facts["arrays"][f"{table}.{field}"] = arrays
            holders = Counter(item for items in arrays.values() for item in set(items))
            value, _rows = most_frequent(holders)
            require(value is not None, "no_relation_element_to_sample", field=f"{table}.{field}")
            target = relation_target(definitions, table, field)

            def count(sql: str, *args) -> int:
                return db.execute(sql, args).fetchone()[0]

            samples[f"{table}.{field}"] = {
                "value": value,
                "rows": len(arrays),
                "empty_rows": sum(1 for items in arrays.values() if not items),
                # PocketBase 0.22 semantics evaluated by SQLite on the stored JSON.
                "like_rows": count(
                    f"SELECT count(*) FROM {q(table)} WHERE {q(field)} LIKE ? ESCAPE '\\'",
                    like_pattern(value),
                ),
                "json_equal_rows": count(
                    f"SELECT count(*) FROM {q(table)} WHERE {q(field)} = ?", value
                ),
                "id_any_rows": count(
                    f"SELECT count(*) FROM {q(table)} t WHERE EXISTS (SELECT 1 FROM "
                    f"json_each(t.{q(field)}) je JOIN {q(target)} r ON r.id = je.value "
                    "WHERE je.value = ?)",
                    value,
                ),
            }
        # Single relations: the most referenced missing target of
        # buma_sale.customer (orphan), and the existing target referenced by
        # the most rows over every single relation (every buma_sale.customer
        # reference but one is an orphan in the 2026-09-28 backup).
        started = time.monotonic()
        samples["orphan"] = single_relation_sample(db, definitions, "buma_sale", "customer", False)
        present = [
            sample
            for table, field in single_relations(definitions)
            if (sample := single_relation_sample(db, definitions, table, field, True))
        ]
        samples["present_single"] = (
            min(present, key=lambda s: (-s["rows"], s["collection"], s["field"]))
            if present
            else None
        )
        facts["seconds"]["single_relation_samples"] = round(time.monotonic() - started, 3)
        # PocketBase 0.22 sorts in SQLite (BINARY collation): the expected orders.
        named = [
            (r[0], r[1])
            for r in db.execute(
                f"SELECT id, name FROM {q('buma_customer')} ORDER BY name ASC, id ASC"
            )
        ]
        samples["sort_text_order"] = [record_id for record_id, _name in named]
        facts["collation"] = glibc_order_difference(named)
        sales = db.execute(f"SELECT count(*) FROM {q('buma_sale')}").fetchone()[0]
        last = max(1, math.ceil(sales / SERVED_PAGE))
        samples["sort_created_pages"] = {
            page: [
                r[0]
                for r in db.execute(
                    f"SELECT id FROM {q('buma_sale')} ORDER BY created DESC, id ASC "
                    "LIMIT ? OFFSET ?",
                    (SERVED_PAGE, (page - 1) * SERVED_PAGE),
                )
            ]
            for page in sorted({1, (last + 1) // 2, last})
        }
        # Files: the smallest referenced key present in the manifest, and absent.
        present, missing = [], []
        for c in definitions:
            if c["type"] == "view":
                continue
            for field in c["schema"]:
                if field["type"] != "file":
                    continue
                multiple = importer.source_multiple(field)
                for record_id, value in db.execute(
                    f"SELECT id, {q(field['name'])} FROM {q(c['name'])}"
                ):
                    for name in importer.stored_references(value, multiple):
                        key = f"{c['id']}/{record_id}/{name}"
                        (present if key in inventory else missing).append(key)
        samples["present_file"] = min(present) if present else None
        samples["missing_file"] = min(missing) if missing else None
    return facts


def single_relations(definitions: list[dict]) -> list[tuple[str, str]]:
    """(collection, field) of every single relation of a base or auth
    collection, buma_sale.customer first, then by name."""
    pairs = sorted(
        (c["name"], f["name"])
        for c in definitions
        if c["type"] != "view"
        for f in c["schema"]
        if f["type"] == "relation" and not pb_is_multiple(f)
    )
    first = [pair for pair in pairs if pair == ("buma_sale", "customer")]
    return first + [pair for pair in pairs if pair != ("buma_sale", "customer")]


def single_relation_sample(db, definitions, table: str, field: str, exists: bool) -> dict | None:
    """The most referenced target of ``table.field`` that exists (or is missing)."""
    q = importer.quote
    target = relation_target(definitions, table, field)
    row = db.execute(
        f"SELECT s.{q(field)}, count(*) FROM {q(table)} s WHERE s.{q(field)} != '' "
        f"AND {'' if exists else 'NOT '}EXISTS (SELECT 1 FROM {q(target)} t "
        f"WHERE t.id = s.{q(field)}) GROUP BY s.{q(field)} ORDER BY count(*) DESC, "
        f"s.{q(field)} LIMIT 1"
    ).fetchone()
    if row is None:
        return None
    return {"collection": table, "field": field, "value": row[0], "rows": row[1]}


def glibc_order_difference(named: list[tuple[str, str]], locale_name: str = "en_US.utf8") -> dict:
    """Positions of the BINARY (id, name) order that a glibc en_US.UTF-8 target changes.

    PostgreSQL with a libc collation sorts with strcoll_l, then strcmp for
    equal strings (varstr_cmp), then by id here. Computed on this host's glibc
    for the report (a count only); the rehearsal target itself is musl.
    """
    import locale

    try:
        previous = locale.setlocale(locale.LC_COLLATE)
        locale.setlocale(locale.LC_COLLATE, locale_name)
    except locale.Error:
        return {"locale": locale_name, "available": False}
    try:
        reordered = sorted(
            named,
            key=lambda item: (
                locale.strxfrm(item[1] or ""),
                (item[1] or "").encode("utf-8", "surrogatepass"),
                item[0].encode("utf-8", "surrogatepass"),
            ),
        )
    finally:
        locale.setlocale(locale.LC_COLLATE, previous)
    differing = sum(1 for a, b in zip(named, reordered, strict=True) if a[0] != b[0])
    return {
        "locale": locale_name,
        "available": True,
        "libc": "glibc " + (platform.libc_ver()[1] or "unknown"),
        "collection": "buma_customer",
        "sort": "name,id",
        "rows": len(named),
        "positions_differing_from_binary_order": differing,
        "first_page_of_50_identical": [a[0] for a in named[:50]] == [b[0] for b in reordered[:50]],
    }


# ---------------------------------------------------------------------------
# Post-commit conservation checks.
# ---------------------------------------------------------------------------


async def verify_import(ctx, engine, spec, diagnostic, load, before) -> tuple[dict, dict, dict]:
    """(checks, private evidence, seconds) of the committed import, in new sessions."""
    q = importer.quote
    definitions, facts = ctx.definitions, ctx.facts
    checks, private, seconds = {}, {}, {}
    tables = [c for c in definitions if c["type"] != "view"]
    by_name = {c["name"]: c for c in definitions}
    diagnosed = {c["name"]: c["rows"] for c in diagnostic["collections"]}

    started = time.monotonic()
    async with engine.connect() as conn:
        counts = {
            c["name"]: await scalar(conn, f"SELECT count(*) FROM {q(c['name'])}") for c in tables
        }
    differing = sorted(name for name in counts if counts[name] != diagnosed.get(name))
    require(not differing and len(counts) == len(diagnosed), "row_counts_differ", tables=differing)
    checks["row_counts"] = {
        "tables": len(counts),
        "rows": sum(counts.values()),
        "equal_to_diagnosis": True,
        "per_table": dict(sorted(counts.items())),
    }
    seconds["row_counts"] = round(time.monotonic() - started, 3)

    started = time.monotonic()
    fingerprints = await table_fingerprints(engine, definitions, spec)
    loaded = {entry["name"]: entry for entry in load.get("tables", [])}
    mismatched = fingerprint_mismatches(fingerprints, loaded)
    require(not mismatched, "fingerprints_differ_after_commit", tables=mismatched)
    checks["fingerprints"] = {
        "tables": len(fingerprints),
        "converted_source_digest_before_copy_equals_committed_rows": len(fingerprints),
        "method": (
            "importer multiset digest of the converted source rows, taken before COPY, against "
            "a typed re-read of the committed rows in a new session; values against the raw "
            "SQLite rows: http.served_values"
        ),
    }
    private["fingerprints"] = {name: value["fingerprint"] for name, value in fingerprints.items()}
    seconds["fingerprints"] = round(time.monotonic() - started, 3)

    started = time.monotonic()
    async with engine.connect() as conn:
        identities = {
            row["collection_id"]: (row["row_count"], row["identity"])
            for row in await rows_of(conn, auth_import.RECORD_IDENTITIES)
        }
        archive_rows = await scalar(conn, 'SELECT count(*) FROM "_buma_c11_rowids"')
        native = {}
        for name in sorted(importer.ROWID_TABLES):
            native[name] = await scalar(
                conn,
                f'SELECT count(*) FROM {q(name)} t JOIN "_buma_c11_rowids" a '
                "ON a.collection_id = :id AND a.record_id = t.id AND a.source_rowid = t.rowid",
                {"id": by_name[name]["id"]},
            )
    expected_identities = {
        c["id"]: (c["rows"], c["identity_sha256"]) for c in diagnostic["collections"] if c["rows"]
    }
    require(identities == expected_identities, "rowid_archive_differs")
    require(archive_rows == sum(counts.values()), "rowid_archive_row_count")
    require(all(native[name] == counts[name] for name in native), "native_rowid_differs")
    checks["rowids"] = {
        "archive_rows": archive_rows,
        "archive_collections_matching_source_rowid_and_id_sequence": len(identities),
        "native_rowid_equal_to_source": native,
    }
    seconds["rowids"] = round(time.monotonic() - started, 3)

    started = time.monotonic()
    arrays = {}
    async with engine.connect() as conn:
        for table, field in MULTIPLE_RELATIONS:
            key = f"{table}.{field}"
            source = facts["arrays"][key]
            stored = {
                row["id"]: list(row["value"])
                for row in await rows_of(conn, f"SELECT id, {q(field)} AS value FROM {q(table)}")
            }
            difference = array_mismatches(source, stored)
            require(not difference["rows_missing_or_extra"], "array_rows_differ", field=key)
            require(not difference["rows_differing"], "array_values_differ", field=key)
            arrays[key] = {
                "rows": len(source),
                "identical_rows_order_and_elements": len(source),
                "compared_with": "raw SQLite JSON lists (json.loads), element for element",
                "length_distribution": length_distribution(len(v) for v in stored.values()),
                "source_length_distribution": length_distribution(len(v) for v in source.values()),
                "max_element_length": max((len(i) for v in stored.values() for i in v), default=0),
                "column_type": describe(spec[table][field]),
            }
    checks["multiple_relation_arrays"] = arrays
    seconds["arrays"] = round(time.monotonic() - started, 3)

    started = time.monotonic()
    async with engine.connect() as conn:
        await conn.exec_driver_sql("SET max_parallel_workers_per_gather = 0")
        recount = await importer.target_anomalies(
            conn, definitions, spec, ctx.inventory, row_counts=diagnosed
        )
    importer.check_conservation(recount, diagnostic)
    dangling, missing = recount["dangling_relations"], recount["missing_files"]
    checks["preserved_anomalies"] = {
        "policy": importer.ANOMALY_POLICY,
        "dangling_references": dangling["references"],
        "dangling_records": dangling["records"],
        "dangling_fields": {
            f"{i['collection']}.{i['field']}": [
                i["references"],
                i["records"],
                i["distinct_targets"],
            ]
            for i in sorted(dangling["fields"], key=lambda i: (i["collection"], i["field"]))
        },
        "missing_file_references": missing["references"],
        "missing_file_records": missing["records"],
        "missing_distinct_files": missing["distinct_files"],
        "missing_file_fields": {
            f"{i['collection']}.{i['field']}": [i["references"], i["records"], i["distinct_files"]]
            for i in sorted(missing["fields"], key=lambda i: (i["collection"], i["field"]))
        },
        "relations_checked": recount["relations_checked"],
        "unreferenced_files": recount["unreferenced_files"],
        "referenced_files": diagnostic["referenced_files"],
        "present_referenced_files": diagnostic["present_referenced_files"],
        "manifest_storage_objects": len(ctx.inventory),
        "recount_equal_to_diagnosis": True,
        "file_bytes_copied": bool(load.get("file_bytes_copied")),
    }
    require(load.get("file_bytes_copied") is False, "file_bytes_copied")
    seconds["anomalies"] = round(time.monotonic() - started, 3)

    started = time.monotonic()
    checks["accounts"] = await check_accounts(engine, facts)
    checks["tokens_and_settings"] = await check_tokens_and_settings(engine, definitions, facts)
    seconds["accounts_tokens_settings"] = round(time.monotonic() - started, 3)

    started = time.monotonic()
    signature = await target_signature(engine)
    difference = signature_difference(before["signature"], signature)
    require(difference["only_import_archives_added"], "schema_changed_by_import", **difference)
    changes = metadata_changes(before["metadata"], await collections_metadata(engine))
    auth_names = {c["name"] for c in definitions if c["type"] == "auth"} | {SUPERUSERS}
    require(import_changes_allowed(changes, auth_names), "collections_changed_by_import")
    checks["schema_after_import"] = {
        "signature": difference,
        "collections_with_imported_options": sorted(changes),
        "only_token_and_template_options_changed": True,
        "complete_schema": await schema_check(ctx.url, ctx.schema),
    }
    seconds["schema"] = round(time.monotonic() - started, 3)

    started = time.monotonic()
    view_counts, view_seconds, view_failures = await count_views(engine, definitions)
    require(not view_failures, "views_failed_on_imported_data", views=sorted(view_failures))
    differing = view_count_differences(view_counts, facts["views"])
    require(not differing, "view_counts_differ_from_sqlite_source", views=differing)
    checks["views"] = {
        "queried": len(view_counts),
        "rows_and_distinct_ids_equal_to_sqlite_source": True,
        "rows_and_distinct_ids": {
            name: [entry["rows"], entry["distinct_ids"]]
            for name, entry in sorted(view_counts.items())
        },
        "views_with_duplicate_ids": sorted(
            name for name, entry in view_counts.items() if entry["rows"] != entry["distinct_ids"]
        ),
    }
    seconds["views"] = round(time.monotonic() - started, 3)
    return checks, private, {"seconds": seconds, "view_seconds": view_seconds, "counts": counts}


AUTH_ROW_SQL = (
    "SELECT id, username, email, email_visibility, verified, password_hash, token_key FROM {table}"
)
SUPERUSER_ROW_SQL = 'SELECT id, email, password_hash, token_key, avatar FROM "_superusers"'


async def check_accounts(engine, facts: dict, *, synthetic_superuser: str | None = None) -> dict:
    """Auth records and the PocketBase admin, compared value by value with the source.

    ``synthetic_superuser`` (the rehearsal's own login, after the import) is
    left out of the comparison of ``_superusers``.
    """
    result = {}
    async with engine.connect() as conn:
        stored_auth = {}
        for name in sorted(facts["auth"]):
            stored_auth[name] = {
                row["id"]: (
                    row["username"],
                    row["email"],
                    row["email_visibility"],
                    row["verified"],
                    row["password_hash"],
                    row["token_key"],
                )
                for row in await rows_of(conn, AUTH_ROW_SQL.format(table=importer.quote(name)))
            }
        superusers = {
            row["id"]: (row["email"], row["password_hash"], row["token_key"], row["avatar"])
            for row in await rows_of(conn, SUPERUSER_ROW_SQL)
            if row["id"] != synthetic_superuser
        }
    for name, source in sorted(facts["auth"].items()):
        mismatches = mismatched_records(source, stored_auth[name])
        require(not mismatches, "auth_records_differ", collection=name, records=mismatches)
        result[name] = {"records": len(source), "identity_hash_token_key_equal": len(source)}
    mismatches = mismatched_records(facts["admins"], superusers)
    require(not mismatches, "superusers_differ_from_source_admins", records=mismatches)
    result[SUPERUSERS] = {
        "source_admins": len(facts["admins"]),
        "imported": len(superusers),
        "email_hash_token_key_avatar_equal": len(superusers),
    }
    return result


def token_plan(definitions: list[dict]) -> list[tuple[str, dict]]:
    """(target collection, {option kind: PocketBase 0.22 settings key}) for every token."""
    plan = [(c["name"], auth_import.RECORD_TOKENS) for c in definitions if c["type"] == "auth"]
    return plan + [(SUPERUSERS, auth_import.SUPERUSER_TOKENS)]


def token_config_report(settings: dict, options: dict, plan) -> tuple[dict, list[str]]:
    """Per token kind: source secret and duration against the target options."""
    configs, failures = {}, []
    for collection, mapping in plan:
        for kind, key in mapping.items():
            source = settings.get(key) if isinstance(settings.get(key), dict) else {}
            target = (options.get(collection) or {}).get(kind) or {}
            secret, duration = source.get("secret"), source.get("duration")
            entry = {
                "source_key": key,
                "secret_present": bool(secret),
                # zeroFallback (v0.23.0 migrate.go:904-910): empty keeps the target value.
                "secret_equal": not secret or target.get("secret") == secret,
                "duration_equal": not duration or target.get("duration") == duration,
            }
            configs[f"{collection}.{kind}"] = entry
            if not (entry["secret_equal"] and entry["duration_equal"]):
                failures.append(f"{collection}.{kind}")
    return configs, failures


def mapped_settings_report(settings: dict, target_settings) -> tuple[dict, list[str]]:
    """Each mapped PocketBase 0.22 setting against the PPBase settings row."""
    mapped = {}
    for path, target_path, _kind in auth_import.SETTINGS_MAP:
        present, value = setting(settings, path)
        if not present:
            mapped[".".join(path)] = {"present": False}
            continue
        if target_path == ("meta", "appURL") and isinstance(value, str) and value.endswith("/"):
            value = value[:-1]  # strings.TrimSuffix, v0.23.0 migrate.go:193
        stored_present, stored = setting(target_settings, target_path)
        mapped[".".join(path)] = {"present": True, "equal": stored_present and stored == value}
    return mapped, sorted(name for name, entry in mapped.items() if entry.get("equal") is False)


async def check_tokens_and_settings(engine, definitions: list[dict], facts: dict) -> dict:
    """Token secrets and durations, then mapped settings, against the source settings."""
    settings = facts["settings"]
    require(isinstance(settings, dict), "source_settings_missing")
    async with engine.connect() as conn:
        options = {
            row["name"]: as_json(row["options"]) or {}
            for row in await rows_of(
                conn, "SELECT name, options FROM _collections WHERE type = 'auth'"
            )
        }
        values = await rows_of(conn, "SELECT value FROM \"_params\" WHERE key = 'settings'")
    require(len(values) == 1, "settings_not_imported")
    configs, failures = token_config_report(settings, options, token_plan(definitions))
    require(not failures, "token_configs_differ", configs=failures)
    mapped, unequal = mapped_settings_report(settings, as_json(values[0]["value"]))
    require(not unequal, "settings_differ", settings=unequal)
    source_configs = sum(1 for key in auth_import.TOKEN_KEYS if isinstance(settings.get(key), dict))
    return {
        "source_token_configs": source_configs,
        "target_token_configs": configs,
        "settings_row_imported": True,
        "mapped_settings": mapped,
    }


# ---------------------------------------------------------------------------
# Isolated PPBase server and HTTP reads.
# ---------------------------------------------------------------------------

# The server reads its database URL from this variable (PPBase ppbase/config.py:
# 20-28 and :119, env_prefix PPBASE_; BUMA app/core/config.py:29), never from
# its command line: /proc/<pid>/cmdline is readable by every local user, the
# environment only by the owner.
SERVER_DATABASE_ENV = "PPBASE_DATABASE_URL"
SERVER_PROBE = (
    "import importlib.util, json, app, ppbase; print(json.dumps({'ppbase': ppbase.__file__, "
    "'app': app.__file__, 'multiplicity': "
    "importlib.util.find_spec('ppbase.models.field_multiplicity') is not None}))"
)


class PPBaseServer:
    def __init__(self, url: str, workdir: Path, ppbase_root: Path):
        self.url = url
        self.ppbase_root = Path(ppbase_root).resolve()
        self.cwd = workdir / "server-cwd"
        self.data_dir = workdir / "server-data"
        self.log_path = workdir / "server.log"
        self.port = free_port()
        self.env = {key: os.environ[key] for key in ENV_KEEP if key in os.environ}
        self.env.update(
            {
                # This checkout (app/ and its hooks), then the selected PPBase.
                "PYTHONPATH": os.pathsep.join([str(PROJECT), str(self.ppbase_root)]),
                "PYTHONDONTWRITEBYTECODE": "1",
                "PPBASE_AUTO_MIGRATE": "false",
                "PPBASE_STORAGE_BACKEND": "local",
                "BUMA_MASTER_ENABLE_INTERNAL_JOBS": "false",
                SERVER_DATABASE_ENV: url,
            }
        )
        self.process: subprocess.Popen | None = None
        self.log = None
        self.peak_rss_kib: int | None = None

    @property
    def base(self) -> str:
        return f"http://127.0.0.1:{self.port}"

    def command_line(self) -> list[str]:
        """The server's command: no database URL (it is in the environment)."""
        return [
            sys.executable, "-m", "ppbase", "serve",
            "--dir", str(self.data_dir), "--migrations-dir", str(MIGRATIONS),
            "--hooks", "app.hooks.register:register", "--host", "127.0.0.1",
            "--port", str(self.port), "--no-automigrate", "--no-apply-migrations-on-start",
            "--no-generate-migrations",
        ]  # fmt: skip

    def loaded_modules(self) -> dict:
        """ppbase and app as the server's interpreter, environment and cwd resolve them
        (the database URL, which plays no part in that, is left out of the probe)."""
        probe = subprocess.run(
            [sys.executable, "-c", SERVER_PROBE],
            cwd=self.cwd,
            env={key: value for key, value in self.env.items() if key != SERVER_DATABASE_ENV},
            capture_output=True,
            text=True,
            check=True,
        )
        loaded = json.loads(probe.stdout.strip().splitlines()[-1])
        ppbase_dir = Path(loaded["ppbase"]).resolve().parent
        require(ppbase_dir == self.ppbase_root / "ppbase", "server_ppbase_not_selected")
        require(Path(loaded["app"]).resolve().parent.parent == PROJECT, "server_app_not_project")
        require(loaded["multiplicity"], "server_ppbase_lacks_d0019")
        return {"ppbase_from_selected_checkout": True, "app_from_this_checkout": True}

    def live_process(self) -> dict:
        """PYTHONPATH, cwd, command and database URL of the running server, read from /proc."""
        proc = Path("/proc") / str(self.process.pid)
        variables = (proc / "environ").read_bytes().decode().split("\0")
        environment = dict(item.split("=", 1) for item in variables if "=" in item)
        cwd = Path(os.readlink(proc / "cwd"))
        argv = (proc / "cmdline").read_bytes().decode().split("\0")
        return self.check_process(environment, cwd, argv)

    def check_process(self, environment: dict, cwd: Path, argv: list[str]) -> dict:
        """The live server is this checkout's, in its private cwd, and its
        command line holds neither the database URL nor its password."""
        require(environment.get("PYTHONPATH") == self.env["PYTHONPATH"], "server_pythonpath")
        require(cwd == self.cwd.resolve(), "server_cwd")
        require(argv[:4] == [sys.executable, "-m", "ppbase", "serve"], "server_command")
        require("app.hooks.register:register" in argv, "server_hooks")
        password = urlsplit(self.url).password or self.url
        require(
            not any(arg == "--db" or arg.startswith("--db=") or password in arg for arg in argv),
            "server_database_url_on_command_line",
        )
        require(environment.get(SERVER_DATABASE_ENV) == self.url, "server_database_url_environment")
        return {
            "source": "/proc/<server pid>",
            "pythonpath_is_this_checkout_then_selected_ppbase": True,
            "cwd_is_fresh_private_directory": True,
            "jobs_disabled": environment.get("BUMA_MASTER_ENABLE_INTERNAL_JOBS") == "false",
            "auto_migrate_disabled": environment.get("PPBASE_AUTO_MIGRATE") == "false",
            "loopback_only": argv[argv.index("--host") + 1] == "127.0.0.1",
            "smtp_variables": sum(1 for key in environment if "SMTP" in key),
            "database_url_in_environment_not_command_line": True,
        }

    async def start(self) -> dict:
        import httpx

        make_private_dir(self.cwd)
        make_private_dir(self.data_dir)
        verified = self.loaded_modules()
        fd = os.open(self.log_path, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
        self.log = os.fdopen(fd, "w")
        self.process = subprocess.Popen(
            self.command_line(),
            cwd=self.cwd,
            env=self.env,
            stdout=self.log,
            stderr=subprocess.STDOUT,
        )
        started = time.monotonic()
        async with httpx.AsyncClient() as client:
            for _ in range(1800):
                if self.process.poll() is not None:
                    raise RehearsalFailure(
                        "server_start_failed", returncode=self.process.returncode
                    )
                try:
                    if (await client.get(self.base + "/api/health")).status_code == 200:
                        return {
                            **verified,
                            "live_process": self.live_process(),
                            "startup_seconds": round(time.monotonic() - started, 3),
                        }
                except httpx.TransportError:
                    pass
                await asyncio.sleep(0.1)
        raise RehearsalFailure("server_start_timeout")

    def stop(self) -> None:
        if self.process is not None and self.process.poll() is None:
            try:
                status = (Path("/proc") / str(self.process.pid) / "status").read_text()
                self.peak_rss_kib = int(re.search(r"VmHWM:\s+(\d+)", status).group(1))
            except (OSError, AttributeError, ValueError):
                pass
            stop_process(self.process, timeout=30)
        if self.log is not None and not self.log.closed:
            self.log.close()

    def stopped(self) -> bool:
        return self.process is None or self.process.poll() is not None

    def log_summary(self) -> dict:
        if not self.log_path.is_file():
            return {"lines": 0}
        lines = self.log_path.read_text(errors="replace").splitlines()
        return {
            "lines": len(lines),
            "tracebacks": sum(1 for line in lines if line.startswith("Traceback")),
            "error_lines": sum(1 for line in lines if re.search(r"\bERROR\b", line)),
        }


async def create_synthetic_superuser(url: str) -> tuple[str, str, str]:
    """A random superuser in the disposable database only; never written anywhere.

    Returns (email, password, id); the id leaves it out of the after-reads
    comparison of ``_superusers`` with the source admins.
    """
    from ppbase.services import admin_service
    from sqlalchemy.ext.asyncio import AsyncSession

    email = f"ws-g-{secrets.token_hex(8)}@example.com"
    password = secrets.token_urlsafe(32)
    engine = make_engine(url)
    try:
        async with AsyncSession(engine) as session:
            await admin_service.create_admin(session, email, password)
            await session.commit()
        async with engine.connect() as conn:
            found = await rows_of(
                conn, 'SELECT id FROM "_superusers" WHERE email = :e', {"e": email}
            )
    finally:
        await engine.dispose()
    require(len(found) == 1, "synthetic_superuser_not_created")
    return email, password, found[0]["id"]


async def fetch(client, path: str, token: str | None = None, **params) -> tuple:
    """(status, JSON body or None); a dropped connection is a status code, not an abort."""
    import httpx

    headers = {"Authorization": token} if token else None
    try:
        response = await client.get(path, params=params or None, headers=headers)
    except httpx.TransportError as exc:
        return "transport_error:" + type(exc).__name__, None
    try:
        body = response.json()
    except ValueError:
        body = None
    return response.status_code, body


# PocketBase 0.22.34 behaviors that PPBase served differently on the real data:
# problem code -> the PocketBase source defining the expected behavior. Such a
# divergence passes only through --accept-divergence CODE=D-NNNN, a decision
# recorded in docs/decisions-and-divergences.md that names the code. Any other
# problem (a message, a status, a value, a count) always fails.
DIVERGENCES = {
    "error_status_key_instead_of_code": (
        "v0.22.34 apis/api_error.go:12-15: error body {code, message, data}"
    ),
    "error_status_key_next_to_code": (
        "v0.22.34 apis/api_error.go:12-15: error body {code, message, data}, no status key"
    ),
    "total_pages_one_for_empty_list": (
        "v0.22.34 tools/search/provider.go:254: totalPages = ceil(0 / perPage) = 0"
    ),
    "empty_date_served_as_null": 'v0.22.34 tools/types/datetime.go:47-58: empty date ""',
    "expand_key_without_related_record": (
        "v0.22.34 models/record.go:557-559 and daos/record_expand.go:227-229: no expand key"
    ),
    "view_total_items_counts_rows_not_distinct_ids": (
        "v0.22.34 tools/search/provider.go:63, 238-254: totalItems = COUNT(DISTINCT id), "
        "totalPages from it"
    ),
    "jsonb_key_order_normalized": (
        "v0.22.34 tools/types/json_raw.go:26-32: a json value served with its stored key order"
    ),
    "null_served_for_pocketbase_zero_value": (
        "v0.22.34 models/record.go:50-58 (a SQL NULL is read as nil) and "
        "models/schema/schema_field.go:310-386 (PrepareValue of nil: '' for text, email, url, "
        "editor and single select, file, relation; 0 for number; false for bool; [] multiple)"
    ),
    "number_served_as_text": (
        "v0.22.34 models/record.go:50-58 and models/schema/schema_field.go:312-343: the text "
        "of a SQLite integer or real is valid JSON, so a json field serves a JSON number"
    ),
    "sqlite_integer_boolean_served_as_boolean": (
        "SQLite has no boolean storage class: TRUE, FALSE and comparisons are the integers 1 "
        "and 0 (https://www.sqlite.org/datatype3.html, 2.1); v0.22.34 "
        "models/schema/schema_field.go:312-343 serves them as the numbers 1 and 0"
    ),
    "concatenated_items_in_another_order": (
        "SQLite group_concat: the order of the concatenated elements is arbitrary "
        "(https://www.sqlite.org/lang_aggfunc.html#group_concat); PocketBase serves SQLite's"
    ),
}


def problem_base(problem: str) -> str:
    return problem.split(":", 1)[0]


def classify_problems(problems: dict[str, list[str]], accepted: dict[str, str]) -> dict:
    """Checks failing without an accepted divergence, and every divergence observed."""
    failing, observed = {}, {}
    for check, codes in sorted(problems.items()):
        bases = {problem_base(code) for code in codes}
        for base in sorted(bases & set(DIVERGENCES)):
            entry = observed.setdefault(
                base,
                {
                    "checks": 0,
                    "pocketbase_0_22_34": DIVERGENCES[base],
                    "decision": accepted.get(base),
                },
            )
            entry["checks"] += 1
        if codes and not bases <= set(accepted):
            failing[check] = sorted(codes)
    return {"failing": failing, "divergences": observed}


def listing_problems(
    status, body, expected: int | None, per_page: int = 1, *, rows: int | None = None, page: int = 1
) -> list[str]:
    """Envelope problems of a list; ``rows`` (a view's count(*)) names the
    COUNT(*) divergence when totalItems equals it instead of the distinct IDs."""
    if status != 200:
        return [f"status:{status}"]
    problems = envelope_problems(body, page=page, per_page=per_page, expected_total=expected)
    if (
        rows is not None
        and rows != expected
        and "total_items_mismatch" in problems
        and body.get("totalItems") == rows
    ):
        problems = [p for p in problems if p != "total_items_mismatch"]
        problems.append("view_total_items_counts_rows_not_distinct_ids")
    return problems


def items_of(status, body) -> list:
    if status == 200 and isinstance(body, dict) and isinstance(body.get("items"), list):
        return body["items"]
    return []


def value_problems(outcomes: dict) -> list[str]:
    """Problem codes of a served-record comparison (unmodeled values are counted, not codes)."""
    codes = (outcome_code(outcome, key) for key, outcome in sorted(outcomes.items()))
    return [code for code in codes if code]


def records_path(name: str, record_id: str | None = None) -> str:
    path = f"/api/collections/{name}/records"
    return path if record_id is None else f"{path}/{url_quote(record_id, safe='')}"


class HttpReads:
    """The HTTP reads of one pass, compared with the read-only source snapshot.

    Results hold statuses, counts, field names of the public schema and
    problem codes; record values stay in memory.
    """

    def __init__(self, client, ctx, db, counts: dict, sql: dict, error_log: Path | None = None):
        self.client, self.ctx, self.db = client, ctx, db
        self.error_log = error_log
        self.definitions = ctx.definitions
        self.by_name = {c["name"]: c for c in ctx.definitions}
        self.samples = ctx.facts["samples"]
        self.views = ctx.facts["views"]
        self.counts, self.sql = counts, sql
        self.token: str | None = None
        self.problems: dict[str, list[str]] = {}
        self.result: dict = {}
        self.timer: dict[str, float] = {}
        # Private only (--private-dir, 0600): served and expected values of
        # the first mismatches of each field, to tell a model gap from a
        # PPBase divergence after the run. Never in the public report.
        self.mismatches: dict[str, list[dict]] = {}

    async def get(self, path: str, token: str | None = None, **params):
        return await fetch(self.client, path, token, **params)

    def compare(self, collection: dict, served, raw: dict, *, email_visible: bool, values=None):
        """Problem codes of one served record against its raw source row."""
        outcomes = compare_served_record(collection, served, raw, email_visible=email_visible)
        if values is not None:
            values.add(collection, outcomes)
        wrong = {k: o for k, o in outcomes.items() if o not in ("equal", "unmodeled")}
        if wrong:
            expected = expected_record(collection, raw, email_visible=email_visible)
            for key, outcome in sorted(wrong.items()):
                kept = self.mismatches.setdefault(f"{collection['name']}.{key}", [])
                if len(kept) < MISMATCH_SAMPLES:
                    kept.append(
                        {
                            "record": raw.get("id"),
                            "outcome": outcome,
                            "expected": safe_value(expected.get(key)),
                            "served": safe_value(served.get(key))
                            if isinstance(served, dict)
                            else type(served).__name__,
                        }
                    )
        return value_problems(outcomes)

    # -- source side (read only) ---------------------------------------------

    def source_rows(self, name: str, tail: str = "", args=()) -> list[dict]:
        sql = f"SELECT * FROM {importer.quote(name)} {tail}"
        return [dict(row) for row in self.db.execute(sql, args)]

    def rows_by_id(self, name: str, ids) -> dict[str, dict]:
        found, ids = {}, [i for i in dict.fromkeys(ids) if isinstance(i, str)]
        for start in range(0, len(ids), 500):
            chunk = ids[start : start + 500]
            marks = ",".join("?" * len(chunk))
            for row in self.source_rows(name, f"WHERE id IN ({marks})", chunk):
                found[row["id"]] = row
        return found

    def existing_ids(self, name: str, ids) -> set[str]:
        return set(self.rows_by_id(name, ids))

    def email_visible(self, collection: dict, raw: dict) -> bool:
        return collection["type"] != "auth" or pb_bool(raw.get("emailVisibility"))

    def expected_total(self, name: str) -> tuple[int, int | None]:
        """(totalItems PocketBase 0.22.34 serves for the whole collection, view rows)."""
        if self.by_name[name]["type"] == "view":
            view = self.views[name]
            return view["distinct_ids"], view["rows"]
        return self.counts[name], None

    # -- reads -----------------------------------------------------------------

    async def superuser_auth(self, credentials) -> None:
        response = await self.client.post(
            "/api/admins/auth-with-password",
            json={"identity": credentials[0], "password": credentials[1]},
        )
        body = response.json() if response.status_code == 200 else None
        require(
            isinstance(body, dict) and set(body) == {"token", "admin"},
            "superuser_auth_failed",
            status=response.status_code,
        )
        self.token = body["token"]
        self.result["superuser_auth"] = {
            "route": "POST /api/admins/auth-with-password (PocketBase 0.22 SDK)",
            "status": response.status_code,
            "response_keys": sorted(body),
            "admin_keys": sorted(body["admin"]),
        }

    async def superuser_lists(self) -> None:
        """Every collection: envelope, totalItems as PocketBase counts it (base
        and auth: SQL count, equal to the source; view: COUNT(DISTINCT id) of
        the SQLite view), and the shape of the first record."""
        for c in self.definitions:
            name = c["name"]
            expected, rows = self.expected_total(name)
            status, body = await self.get(records_path(name), self.token, page=1, perPage=1)
            codes = listing_problems(status, body, expected, rows=rows)
            items = items_of(status, body)
            if c["type"] != "view" and items:
                source = self.rows_by_id(name, [items[0].get("id")]).get(items[0].get("id"))
                codes += record_shape_problems(c, items[0], email_visible=True, source=source)
                if source is None:
                    codes.append("record_not_in_source")
            self.problems[f"superuser_list:{name}"] = codes
        self.result["superuser_lists"] = {
            "collections": len(self.definitions),
            "views_with_duplicate_ids": sorted(
                name for name, view in self.views.items() if view["rows"] != view["distinct_ids"]
            ),
            "expected_total_items": "base and auth: SQL count; view: COUNT(DISTINCT id) in SQLite",
        }

    async def served_values(self) -> None:
        """Pages sorted by id against the raw SQLite rows in BINARY order: page 1
        of every collection, every page of FULL_READ_COLLECTIONS. A view whose
        IDs are duplicated, null or not text (``view_not_compared``) is listed
        in views_not_compared, never compared."""
        values, skipped, pages_read = ServedValues(), {}, 0
        multisets = {}
        for c in self.definitions:
            name = c["name"]
            if c["type"] == "view":
                view = self.views[name]
                reason = view_not_compared(view)
                if reason:
                    skipped[name] = reason
                    continue
                if row_number_ids(c) and view["rows"]:
                    multisets[name], pages = await self.view_multiset(c)
                    pages_read += pages
                    continue
                total = view["rows"]
            else:
                total = self.counts[name]
            if not total:
                continue
            pages = math.ceil(total / SERVED_PAGE) if name in FULL_READ_COLLECTIONS else 1
            for page in range(1, pages + 1):
                status, body = await self.get(
                    records_path(name), self.token, page=page, perPage=SERVED_PAGE, sort="id"
                )
                pages_read += 1
                raw = self.source_rows(
                    name, "ORDER BY id LIMIT ? OFFSET ?", (SERVED_PAGE, (page - 1) * SERVED_PAGE)
                )
                items = items_of(status, body)
                codes = [] if status == 200 else [f"status:{status}"]
                if [item.get("id") for item in items] != [row["id"] for row in raw]:
                    codes.append("ids_differ_from_sqlite_order")
                else:
                    for item, row in zip(items, raw, strict=True):
                        codes += self.compare(c, item, row, email_visible=True, values=values)
                suffix = f":page-{page}" if page > 1 else ""
                self.problems[f"served_values:{name}{suffix}"] = sorted(set(codes))
        self.result["served_values"] = {
            "method": (
                "superuser pages sorted by id against the raw SQLite rows (ORDER BY id), each "
                "value modeled as PocketBase 0.22.34 serves it (NullString, PrepareValue, "
                "PublicExport), independently of the importer"
            ),
            "pages_read": pages_read,
            "per_page": SERVED_PAGE,
            "every_record_of": list(FULL_READ_COLLECTIONS),
            "views_not_compared": skipped,
            "row_number_views_compared_as_multisets": multisets,
            **values.summary(),
        }

    async def view_multiset(self, c: dict) -> tuple[dict, int]:
        """Every page of a ROW_NUMBER() view against every SQLite row, IDs aside."""
        name = c["name"]
        raw = self.source_rows(name)
        served, codes, pages = [], [], max(1, math.ceil(len(raw) / SERVED_PAGE))
        for page in range(1, pages + 1):
            status, body = await self.get(
                records_path(name), self.token, page=page, perPage=SERVED_PAGE, sort="id"
            )
            if status != 200:
                codes.append(f"status:{status}")
            served += items_of(status, body)
        expected = [expected_record(c, row, email_visible=True) for row in raw]
        problems, counts, samples = multiset_comparison(expected, served)
        if problems:  # real values: private evidence only
            self.mismatches[f"{name}.<rows>"] = [samples]
        self.problems[f"served_values:{name}"] = sorted(set(codes + problems))
        return counts, pages

    async def anonymous_lists(self) -> None:
        """Lists without auth by rule (apis/record_crud.go:56-58, :71-73): admin
        only -> 403 and PocketBase's body; public -> every record; an
        expression that no record can match without auth -> totalItems 0."""
        rules, never_true, unverified = Counter(), 0, []
        for c in self.definitions:
            name, rule = c["name"], c.get("listRule")
            rules[rule_kind(rule)] += 1
            status, body = await self.get(records_path(name), None, page=1, perPage=1)
            if rule is None:
                codes = (
                    error_body_problems(body, 403, PB_ADMIN_ONLY)
                    if status == 403
                    else [f"status:{status}"]
                )
            elif rule == "":
                expected, rows = self.expected_total(name)
                codes = listing_problems(status, body, expected, rows=rows)
            elif anonymous_rule_outcome(rule, ids_never_empty=c["type"] != "view") == "never_true":
                never_true += 1
                codes = listing_problems(status, body, 0)
            else:
                unverified.append(name)
                codes = listing_problems(status, body, None)
            self.problems[f"anonymous_list:{name}"] = codes
        self.result["anonymous_lists"] = {
            "by_rule": dict(sorted(rules.items())),
            "expression_rules_expected_zero_items": never_true,
            "expression_rules_unverified": sorted(unverified),
            "expression_rule_method": "static reading with PocketBase 0.22.34 semantics",
        }

    async def anonymous_views(self) -> None:
        """The first record (by id) of every base and auth collection without
        auth: admin only -> 403 (:155); public -> 200 and the record as the
        source serves it, e-mail by its source visibility; an expression no
        record can match without auth -> 404 (:173)."""
        outcomes, unverified = Counter(), []
        for c in self.definitions:
            if c["type"] == "view" or not self.counts[c["name"]]:
                continue
            name, rule = c["name"], c.get("viewRule")
            raw = self.source_rows(name, "ORDER BY id LIMIT 1")[0]
            status, body = await self.get(records_path(name, raw["id"]))
            if rule is None:
                outcomes["admin_only_403"] += 1
                codes = (
                    error_body_problems(body, 403, PB_ADMIN_ONLY)
                    if status == 403
                    else [f"status:{status}"]
                )
            elif rule == "":
                outcomes["public_200"] += 1
                visible = self.email_visible(c, raw)
                codes = [] if status == 200 else [f"status:{status}"]
                if status == 200:
                    codes += record_shape_problems(c, body, email_visible=visible, source=raw)
                    codes += self.compare(c, body, raw, email_visible=visible)
            elif anonymous_rule_outcome(rule, ids_never_empty=True) == "never_true":
                outcomes["expression_404"] += 1
                codes = (
                    error_body_problems(body, 404, PB_NOT_FOUND)
                    if status == 404
                    else [f"status:{status}"]
                )
            else:
                unverified.append(name)
                codes = [] if status in (200, 404) else [f"status:{status}"]
            self.problems[f"anonymous_view:{name}"] = sorted(set(codes))
        self.result["anonymous_views"] = {
            "expected": dict(sorted(outcomes.items())),
            "expression_rules_unverified": sorted(unverified),
        }

    async def anonymous_auth_records(self) -> None:
        """Every record of the auth collections with a public list rule, read
        without auth: e-mail served exactly when the SOURCE emailVisibility is
        true (models/record.go:536-541), every value as the source serves it."""
        summary = {}
        for c in self.definitions:
            if c["type"] != "auth" or c.get("listRule") != "":
                continue
            name, served, hidden_shown, visible_missing = c["name"], 0, 0, 0
            codes = []
            for page in range(1, math.ceil(self.counts[name] / SERVED_PAGE) + 1):
                status, body = await self.get(
                    records_path(name), None, page=page, perPage=SERVED_PAGE, sort="id"
                )
                if status != 200:
                    codes.append(f"status:{status}")
                    continue
                items = items_of(status, body)
                raw = self.rows_by_id(name, [item.get("id") for item in items])
                for item in items:
                    row = raw.get(item.get("id"))
                    if row is None:
                        codes.append("record_not_in_source")
                        continue
                    served += 1
                    visible = self.email_visible(c, row)
                    hidden_shown += "email" in item and not visible
                    visible_missing += visible and "email" not in item
                    codes += record_shape_problems(c, item, email_visible=visible, source=row)
                    codes += self.compare(c, item, row, email_visible=visible)
            codes += ["email_served_while_not_visible_in_source"] * bool(hidden_shown)
            codes += ["email_missing_while_visible_in_source"] * bool(visible_missing)
            self.problems[f"anonymous_auth_records:{name}"] = sorted(set(codes))
            summary[name] = {
                "records": served,
                "source_records": self.counts[name],
                "email_served_while_not_visible_in_source": hidden_shown,
                "email_missing_while_visible_in_source": visible_missing,
            }
        self.result["anonymous_auth_records"] = summary

    async def filters(self) -> None:
        """Filters with PocketBase 0.22 semantics, counted in the source and in
        PostgreSQL; PPBase's totalItems must equal both."""
        filters = {}
        for code, name, expression, expected in filter_cases(self.samples):
            status, body = await self.get(
                records_path(name), self.token, page=1, perPage=1, filter=expression
            )
            codes = listing_problems(status, body, expected)
            if self.sql.get(code) != expected:
                codes.append("postgresql_count_differs_from_source")
            served = body.get("totalItems") if status == 200 and isinstance(body, dict) else None
            filters[code] = {
                "sampled": True,
                "collection": name,
                "expected_total_items": expected,
                "total_items": served,
            }
            self.problems[f"filter:{code}"] = codes
        for code in unsampled_filter_codes(self.samples):
            filters[code] = {"sampled": False}
        self.result["filters"] = filters

    async def expands(self) -> None:
        """Expand with the superuser: expected from the raw source values and the
        targets that exist in the source (``expected_expand_ids``)."""
        pdv, managers = self.samples["buma_customer.pdv"], self.samples["buma_wallet.managers"]
        cases = [
            ("multiple_targets_pdv", "buma_customer", f'pdv ~ "{pdv["value"]}"', "pdv"),
            ("multiple_targets_tag", "buma_pilotage_ticket", "", "tag"),
            (
                "multiple_absent_target_managers",
                "buma_wallet",
                f'managers ~ "{managers["value"]}"',
                "managers",
            ),
        ]
        optional = {"single_absent_target": "orphan", "single_present_target": "present_single"}
        expanded = {}
        for code, key in optional.items():
            sample = self.samples.get(key)
            if sample:
                name, field = sample["collection"], sample["field"]
                cases.append((code, name, f'{field} = "{sample["value"]}"', field))
            else:
                expanded[code] = {"sampled": False}
        for code, name, expression, field in cases:
            params = {"page": 1, "perPage": SAMPLE_PAGE, "expand": field, "sort": "id"}
            if expression:
                params["filter"] = expression
            status, body = await self.get(records_path(name), self.token, **params)
            items = items_of(status, body)
            definition = next(f for f in self.by_name[name]["schema"] if f["name"] == field)
            raw = self.rows_by_id(name, [item.get("id") for item in items])
            served_lists = []
            for item in items:
                try:
                    served_lists.append(pb_served(definition, raw[item.get("id")][field]))
                except (KeyError, Unmodeled):
                    served_lists.append(None)
            targets = [i for v in served_lists for i in (v if isinstance(v, list) else [v]) if i]
            existing = self.existing_ids(relation_target(self.definitions, name, field), targets)
            multiple = pb_is_multiple(definition)
            expected = [expected_expand_ids(v, existing, multiple=multiple) for v in served_lists]
            codes = [] if status == 200 else [f"status:{status}"]
            if any(v is None for v in served_lists) and items:
                codes.append("record_not_in_source")
            codes += expand_problems(items, field, expected)
            kinds = Counter(
                "none" if e is None else "list" if isinstance(e, list) else "object"
                for e in expected
            )
            expanded[code] = {
                "sampled": True,
                "relation": f"{name}.{field}",
                "status": status,
                "items": len(items),
                "expected_expand": dict(sorted(kinds.items())),
                "max_expanded_list_length": max(
                    (len(e) for e in expected if isinstance(e, list)), default=0
                ),
            }
            self.problems[f"expand:{code}"] = codes
        self.result["expand"] = expanded

    async def sorts(self) -> None:
        """Sort against SQLite's order (PocketBase 0.22 sorts in SQLite, BINARY
        collation): every page of buma_customer by name then id; the first,
        a middle and the last page of buma_sale by -created then id."""
        sorts = {}
        expected = self.samples["sort_text_order"]
        served, codes = [], []
        for page in range(1, math.ceil(len(expected) / SERVED_PAGE) + 1):
            status, body = await self.get(
                records_path("buma_customer"),
                self.token,
                page=page,
                perPage=SERVED_PAGE,
                sort="name,id",
            )
            if status != 200:
                codes.append(f"status:{status}")
            served += [item.get("id") for item in items_of(status, body)]
        differing = sum(1 for a, b in zip(served, expected, strict=False) if a != b)
        differing += abs(len(served) - len(expected))
        if served != expected:
            codes.append("order_differs_from_sqlite_source")
        self.problems["sort:text_name_then_id_every_page"] = sorted(set(codes))
        sorts["text_name_then_id_every_page"] = {
            "rows": len(expected),
            "positions_differing_from_sqlite": differing,
        }
        pages = self.samples["sort_created_pages"]
        compared = []
        for page, ids in sorted(pages.items()):
            status, body = await self.get(
                records_path("buma_sale"),
                self.token,
                page=page,
                perPage=SERVED_PAGE,
                sort="-created,id",
            )
            codes = [] if status == 200 else [f"status:{status}"]
            if [item.get("id") for item in items_of(status, body)] != ids:
                codes.append("order_differs_from_sqlite_source")
            self.problems[f"sort:created_desc_then_id_page_{page}"] = codes
            compared.append(page)
        sorts["created_desc_then_id"] = {
            "rows": self.counts["buma_sale"],
            "pages_compared": compared,
            "per_page": SERVED_PAGE,
        }
        sorts["target_collation"] = self.ctx.collation
        sorts["glibc_comparison"] = self.ctx.facts["collation"]
        self.result["sort"] = sorts

    async def files(self) -> None:
        """Files, without any byte on this server: a referenced file absent
        from the backup is 404 in PocketBase 0.22.34 (apis/file.go:211-215);
        one the manifest lists is served there from its storage, not here."""
        files = {}
        for code, key in (
            ("present_in_manifest_no_bytes_here", self.samples["present_file"]),
            ("absent_from_backup", self.samples["missing_file"]),
        ):
            if key is None:
                files[code] = {"sampled": False}
                continue
            collection_id, record_id, filename = key.split("/", 2)
            path = f"/api/files/{collection_id}/{record_id}/{url_quote(filename)}"
            status, body = await self.get(path)
            files[code] = {"sampled": True, "status": status, "expected_here": 404}
            self.problems[f"file:{code}"] = (
                error_body_problems(body, 404, PB_NOT_FOUND)
                if status == 404
                else [f"status:{status}"]
            )
        if files["present_in_manifest_no_bytes_here"].get("sampled"):
            files["present_in_manifest_no_bytes_here"]["expected_with_storage"] = 200
        self.result["files"] = files

    async def timed(self, name: str, step, *, fatal: bool = False) -> None:
        """Run one group of reads; an unexpected error fails that group as a
        check (type only) so that the other groups and the after-reads checks
        still run. ``fatal`` re-raises (no superuser token, nothing to read)."""
        started = time.monotonic()
        try:
            await step()
        except Exception as exc:
            if fatal:
                raise
            self.problems[f"reads:{name}"] = [f"error:{type(exc).__name__}"]
            if self.error_log is not None:  # private 0600 log: a message may quote a value
                fd = os.open(self.error_log, os.O_CREAT | os.O_APPEND | os.O_WRONLY, 0o600)
                with os.fdopen(fd, "a") as log:
                    log.write(f"[{name}]\n{traceback.format_exc()}\n")
        finally:
            self.timer[name] = round(time.monotonic() - started, 3)


async def http_checks(
    base: str, credentials, ctx, counts: dict, sql: dict, error_log: Path | None = None
) -> dict:
    """Aggregated HTTP reads: statuses, counts and problem codes, never a value."""
    import httpx

    async with httpx.AsyncClient(base_url=base, timeout=HTTP_TIMEOUT_SECONDS) as client:
        status, _ = await fetch(client, "/api/health")
        require(status == 200, "server_health", status=status)
        with importer.snapshot(ctx.data) as db:
            reads = HttpReads(client, ctx, db, counts, sql, error_log)
            await reads.timed("auth", lambda: reads.superuser_auth(credentials), fatal=True)
            for name in (
                "superuser_lists",
                "served_values",
                "anonymous_lists",
                "anonymous_views",
                "anonymous_auth_records",
                "filters",
                "expands",
                "sorts",
                "files",
            ):
                await reads.timed(name, getattr(reads, name))
    result = reads.result
    if error_log is not None and reads.mismatches:
        # Real values: private 0600 file next to the error log, never published.
        importer.write_json_new(
            error_log.parent / "served-value-mismatches.json", reads.mismatches, ascii=True
        )
    result["served_value_mismatch_fields"] = len(reads.mismatches)
    classified = classify_problems(reads.problems, ctx.accepted)
    result["checks_run"] = len(reads.problems)
    result["checks_without_problem"] = sum(1 for codes in reads.problems.values() if not codes)
    result["divergences"] = classified["divergences"]
    result["failed_checks"] = classified["failing"]
    result["seconds"] = reads.timer
    return result


def filter_cases(samples: dict) -> list[tuple[str, str, str, int]]:
    """(code, collection, filter, totalItems the source gives with PocketBase 0.22).

    A direct multi-value field resolves to its stored JSON text (v0.22.34
    resolvers/record_field_resolve_runner.go:452-456): ``~`` matches a
    substring of it and ``?=`` compares it whole; ``field.id ?=`` traverses
    the relation to existing targets; ``:length`` counts the elements
    (:400-414). A single relation to a missing target matches by value, never
    through the target (LEFT JOIN).
    """
    cases = []
    for key in ("buma_customer.pdv", "buma_pilotage_ticket.tag", "buma_wallet.managers"):
        table, field = key.split(".")
        sample = samples[key]
        value = sample["value"]
        cases += [
            (f"{field}_contains", table, f'{field} ~ "{value}"', sample["like_rows"]),
            (
                f"{field}_any_equals_whole_json_text",
                table,
                f'{field} ?= "{value}"',
                sample["json_equal_rows"],
            ),
            (f"{field}_id_any_equals", table, f'{field}.id ?= "{value}"', sample["id_any_rows"]),
            (f"{field}_length_zero", table, f"{field}:length = 0", sample["empty_rows"]),
            (
                f"{field}_length_positive",
                table,
                f"{field}:length > 0",
                sample["rows"] - sample["empty_rows"],
            ),
        ]
    for prefix, key, through_target in (
        ("orphan_single", "orphan", 0),
        ("single", "present_single", None),
    ):
        sample = samples[key]
        if not sample:
            continue
        table, field, value = sample["collection"], sample["field"], sample["value"]
        target_rows = sample["rows"] if through_target is None else through_target
        cases += [
            (f"{prefix}_equals", table, f'{field} = "{value}"', sample["rows"]),
            (f"{prefix}_target_id", table, f'{field}.id = "{value}"', target_rows),
        ]
    return cases


def unsampled_filter_codes(samples: dict) -> list[str]:
    """Filter cases without a sample in this snapshot: reported as sampled false."""
    codes = []
    if not samples["orphan"]:
        codes += ["orphan_single_equals", "orphan_single_target_id"]
    if not samples["present_single"]:
        codes += ["single_equals", "single_target_id"]
    return codes


def like_pattern(value: str) -> str:
    """%value% with LIKE wildcards escaped (ESCAPE '\\')."""
    escaped = value.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")
    return f"%{escaped}%"


async def filter_sql_counts(engine, definitions: list[dict], samples: dict) -> dict:
    """The same filters counted in PostgreSQL, on the imported data itself."""
    q = importer.quote
    queries = {}
    for key in ("buma_customer.pdv", "buma_pilotage_ticket.tag", "buma_wallet.managers"):
        table, field = key.split(".")
        target = relation_target(definitions, table, field)
        value = samples[key]["value"]
        column = f"array_to_json({q(field)})::text"
        queries[f"{field}_contains"] = (
            f"SELECT count(*) FROM {q(table)} WHERE {column} ILIKE :v ESCAPE '\\'",
            like_pattern(value),
        )
        queries[f"{field}_any_equals_whole_json_text"] = (
            f"SELECT count(*) FROM {q(table)} WHERE {column} = :v",
            value,
        )
        queries[f"{field}_id_any_equals"] = (
            f"SELECT count(*) FROM {q(table)} WHERE :v = ANY({q(field)}) "
            f"AND EXISTS (SELECT 1 FROM {q(target)} WHERE id = :v)",
            value,
        )
        queries[f"{field}_length_zero"] = (
            f"SELECT count(*) FROM {q(table)} WHERE cardinality({q(field)}) = 0",
            None,
        )
        queries[f"{field}_length_positive"] = (
            f"SELECT count(*) FROM {q(table)} WHERE cardinality({q(field)}) > 0",
            None,
        )
    for code, sample in (
        ("orphan_single", samples["orphan"]),
        ("single", samples["present_single"]),
    ):
        if not sample:
            continue
        table, field = sample["collection"], sample["field"]
        target = relation_target(definitions, table, field)
        queries[f"{code}_equals"] = (
            f"SELECT count(*) FROM {q(table)} WHERE {q(field)} = :v",
            sample["value"],
        )
        queries[f"{code}_target_id"] = (
            f"SELECT count(*) FROM {q(table)} s JOIN {q(target)} t ON t.id = s.{q(field)} "
            "WHERE t.id = :v",
            sample["value"],
        )
    counts = {}
    async with engine.connect() as conn:
        for code, (sql, value) in queries.items():
            counts[code] = await scalar(conn, sql, {"v": value} if value is not None else None)
    return counts


# ---------------------------------------------------------------------------
# One complete pass.
# ---------------------------------------------------------------------------


async def run_pass(ctx, index: int) -> tuple[dict, dict]:
    """One complete pass, always cleaned up: (public summary, private evidence)."""
    args = ctx.args
    pass_dir = make_private_dir(ctx.private_dir / f"pass-{index}")
    staging = pass_dir / "staging"
    summary: dict = {"pass": index, "status": "failed", "checks": {}}
    private: dict = {}
    measures: dict = {}
    phases, cleanup, running = Phases(), Cleanup(), []
    disk_paths = {"private_dir": pass_dir, "tmp": Path("/tmp"), "docker_root": ctx.docker_root}
    measures["disk_free_before"] = disk_free(disk_paths)
    disk = DiskMonitor(disk_paths).start()
    container = Container(uuid.uuid4().hex[:12], args.memory, args.cpus, args.shm_size, pass_dir)
    server: PPBaseServer | None = None
    monitor = None
    engine = None
    checks = summary["checks"]
    try:
        with phases.phase("container_start"):
            summary["container"] = container.start()
            from scripts.c11_volume_benchmark import ContainerMemory

            monitor = ContainerMemory(container.id)
            monitor.start()
            phases.monitor = monitor
        ctx.url = container.url
        engine = make_engine(container.url)
        measures["database"] = await database_facts(engine)
        ctx.collation = target_collation(measures["database"], container.libc())

        with phases.phase("migrations_schema_and_target_description"):
            spec, checks["target_before_import"] = await prepare_target(
                engine, container.url, ctx.schema, ctx.definitions
            )
            spec_path = pass_dir / "target-columns.json"
            importer.write_json_new(spec_path, importer.target_spec_json(spec))
            before = {
                "signature": await target_signature(engine),
                "metadata": await collections_metadata(engine),
            }
            measures["database_bytes_after_migrations"] = await database_size(engine)

        with phases.phase("diagnosis"):
            common = {
                "ppbase": str(ctx.ppbase_root),
                "data": str(ctx.data),
                "manifest": str(ctx.manifest),
                "schema": str(ctx.schema),
            }
            diagnostic_path = pass_dir / "diagnostic.json"
            child = run_child(
                "diagnose",
                {
                    **common,
                    "source_version": args.source_version,
                    "spec": str(spec_path),
                    "diagnostic": str(diagnostic_path),
                    "details": str(pass_dir / "diagnostic-details.json"),
                    "audit": str(pass_dir / "independent-audit.json"),
                    # Resolved: the child runs in the pass directory.
                    "source_migrations": (
                        str(args.source_migrations.resolve()) if args.source_migrations else ""
                    ),
                },
                pass_dir,
                running,
            )
            measures["diagnosis_child"] = {
                k: child[k] for k in ("peak_rss_kib", "child_seconds", "parent_wall_seconds")
            }
            measures["diagnosis_step_seconds"] = child["seconds"]
            checks["diagnosis"] = {
                "ready_for_isolated_rehearsal": child["ready"],
                "rows": child["rows"],
                "blocker_codes": child["blocker_codes"],
            }
            require(child["ready"], "diagnosis_not_ready", blockers=child["blocker_codes"])
            diagnostic = json.loads(diagnostic_path.read_text())
            checks["diagnosis"].update(diagnosis_summary(diagnostic))
            checks["independent_audit"] = audit_agreement(child["audit"], checks["diagnosis"])
            if "idempotency_source_check" in child:
                checks["idempotency_source_check"] = child["idempotency_source_check"]

        with phases.phase("import"):
            load_path = pass_dir / "load-report.json"
            child = run_child(
                "import",
                {
                    **common,
                    "diagnostic": str(diagnostic_path),
                    "staging": str(staging),
                    "load_report": str(load_path),
                    "batch_size": args.batch_size,
                },
                pass_dir,
                running,
                url=container.url,
            )
            load = json.loads(load_path.read_text())
            measures["import_child"] = {
                k: child[k] for k in ("peak_rss_kib", "child_seconds", "parent_wall_seconds")
            }
            measures["importer_timings_seconds"] = load.get("timings_seconds")
            measures["staging_bytes"] = child["staging_bytes"]
            measures["database_bytes_after_import"] = await database_size(engine)
            measures["postgresql_data_directory_bytes_after_import"] = container.data_bytes()
            checks["import"] = import_summary(child, load)

        with phases.phase("post_commit_checks"):
            post, private_post, post_measures = await verify_import(
                ctx, engine, spec, diagnostic, load, before
            )
            checks.update(post)
            private.update(private_post)
            measures["post_commit_check_seconds"] = post_measures["seconds"]
            measures["view_count_seconds"] = post_measures["view_seconds"]
            counts = post_measures["counts"]
            hashed_tables = [c["name"] for c in ctx.definitions if c["type"] != "view"]
            hashed_tables += [*ARCHIVE_TABLES, "_collections", "_params", SUPERUSERS]
            private["row_hashes_after_import"] = await stored_row_hashes(engine, hashed_tables)

        with phases.phase("server_start"):
            email, password, synthetic_id = await create_synthetic_superuser(container.url)
            credentials = (email, password)
            server = PPBaseServer(container.url, pass_dir, ctx.ppbase_root)
            summary["server"] = await server.start()

        with phases.phase("http_reads"):
            sql = await filter_sql_counts(engine, ctx.definitions, ctx.facts["samples"])
            http = await http_checks(
                server.base, credentials, ctx, counts, sql, pass_dir / "http-errors.log"
            )
            measures["http_seconds"] = http.pop("seconds")
            checks["http"] = http
            credentials = email = password = None

        with phases.phase("after_http_checks"):
            server.stop()
            summary["server"]["stopped"] = server.stopped()
            summary["server"]["peak_rss_kib"] = server.peak_rss_kib
            summary["server"]["log"] = server.log_summary()
            # The synthetic superuser is the only row allowed to differ.
            after = await stored_row_hashes(engine, hashed_tables, {SUPERUSERS: synthetic_id})
            async with engine.connect() as conn:
                superusers = await scalar(conn, 'SELECT count(*) FROM "_superusers"')
            try:
                await check_accounts(engine, ctx.facts, synthetic_superuser=synthetic_id)
                accounts = True
            except RehearsalFailure as exc:
                accounts = exc.code
            signature = await target_signature(engine)
            outcome = after_reads_outcome(
                private["row_hashes_after_import"],
                after,
                superusers=superusers,
                admins=len(ctx.facts["admins"]),
                accounts=accounts,
                signature=signature_difference(before["signature"], signature),
            )
            checks["after_http"] = outcome
            require(
                not outcome["tables_changed_by_server_and_reads"]
                and outcome["superusers_are_source_admins_plus_synthetic"],
                "data_changed_by_server_or_reads",
                tables=outcome["tables_changed_by_server_and_reads"],
            )
            require(accounts is True, "accounts_changed_by_server_or_reads", failure=accounts)
            require(outcome["schema_unchanged_by_server"], "schema_changed_by_server")
            measures["database_bytes_after_http"] = await database_size(engine)
        # Last, so that a failed read still leaves the after-reads evidence.
        require(
            not http["failed_checks"], "http_checks_failed", checks=sorted(http["failed_checks"])
        )
        summary["status"] = "passed"
    except Exception as exc:
        summary["failure"] = failure_entry(exc)
    finally:
        # In this order: nothing left running, memory read while the cgroup
        # exists, then files and the container.
        cleanup.add("children", lambda: [stop_process(p, timeout=180) for p in running])
        if server is not None:
            cleanup.add("server", server.stop)
        cleanup.run()
        if engine is not None:
            try:
                await engine.dispose()
            except Exception as exc:  # recorded; the container is removed anyway
                summary.setdefault("cleanup_notes", []).append(failure_entry(exc))
        if monitor is not None:
            try:
                measures["container_memory"] = monitor.finish()
            except Exception as exc:
                measures["container_memory"] = {"unavailable": failure_entry(exc)}
            measures["container_memory_by_phase"] = phases.memory
        cleanup.add("server_files", lambda: remove_server_files(pass_dir))
        cleanup.add("staging", lambda: shutil.rmtree(staging, ignore_errors=True))
        cleanup.add("container", container.stop)
        summary["cleanup"] = cleanup.run()
        summary["cleanup"]["container_evidence"] = (
            container.removal_evidence() if container.attempted else {"created": False}
        )
        summary["cleanup"]["staging_removed"] = not staging.exists()
        summary["cleanup"]["server_files_removed"] = not any(
            (pass_dir / name).exists() for name in ("server-cwd", "server-data")
        )
        summary["cleanup"]["server_stopped"] = server is None or server.stopped()
        measures["disk_free_minimum_during_pass"] = disk.finish()
        measures["disk_free_after"] = disk_free(disk_paths)
        measures["phase_seconds"] = phases.seconds
        measures["completed_phases"] = phases.completed
        summary["measures"] = measures
        cleaned = (
            cleanup.ok
            and summary["cleanup"]["staging_removed"]
            and summary["cleanup"]["server_files_removed"]
            and summary["cleanup"]["server_stopped"]
            and summary["cleanup"]["container_evidence"].get("container_removed", True)
            and not summary["cleanup"]["container_evidence"].get("anonymous_volumes_left")
        )
        if not cleaned:
            summary["status"] = "failed"
            summary.setdefault("failure", {"type": "RehearsalFailure", "code": "cleanup_failed"})
    return summary, private


def remove_server_files(pass_dir: Path) -> None:
    for name in ("server-cwd", "server-data"):
        shutil.rmtree(pass_dir / name, ignore_errors=True)


def after_reads_outcome(before: dict, after: dict, *, superusers, admins, accounts, signature):
    """What the server and the reads changed: no table (the synthetic
    superuser left out), the imported accounts still equal to the source, the
    schema as imported."""
    changed = sorted(
        table for table in set(before) | set(after) if before.get(table) != after.get(table)
    )
    return {
        "tables_changed_by_server_and_reads": changed,
        "imported_superusers_unchanged": SUPERUSERS not in changed,
        "superusers_are_source_admins_plus_synthetic": superusers == admins + 1,
        "accounts_equal_to_source_after_reads": accounts is True,
        "schema_unchanged_by_server": signature["only_import_archives_added"],
    }


def target_collation(database: dict, libc: str | None) -> dict:
    """Text order of the target: musl's strcoll is strcmp, so a libc
    collation sorts bytes like SQLite BINARY; glibc and ICU sort linguistically."""
    byte_order = database.get("provider") == "c" and libc == "musl"
    return {
        "datcollate": database.get("datcollate"),
        "provider": {"c": "libc", "i": "icu", "b": "builtin"}.get(database.get("provider")),
        "image_libc": libc,
        "text_order": "byte order, like SQLite BINARY" if byte_order else "linguistic",
    }


def diagnosis_summary(diagnostic: dict) -> dict:
    """Aggregates of the private diagnosis that the public report may carry."""
    anomalies = diagnostic["preserved_source_anomalies"]
    return {
        "source_version": diagnostic.get("source_version"),
        "source_version_validated": diagnostic.get("source_version_validated"),
        "source_version_evidence": diagnostic.get("source_version_evidence"),
        "storage_mode": diagnostic.get("storage_mode"),
        "file_bytes_verified": diagnostic.get("file_bytes_verified"),
        "file_bytes_copied": diagnostic.get("file_bytes_copied"),
        "target_types_checked": diagnostic.get("target_types_checked"),
        "target_columns_checked": (diagnostic.get("target_preflight") or {}).get("columns_checked"),
        "collections": len(diagnostic["collections"]),
        "relations_checked": diagnostic["relations_checked"],
        "dangling_references": anomalies["dangling_relations"]["references"],
        "missing_file_references": anomalies["missing_files"]["references"],
        "manifest_files": diagnostic["files"],
        "referenced_files": diagnostic["referenced_files"],
        "present_referenced_files": diagnostic["present_referenced_files"],
        "unreferenced_files": diagnostic["unreferenced_files"],
        "blocked_rows": diagnostic["blocked_rows"],
        "warnings": [
            {k: w.get(k) for k in ("code", "collection", "field", "affected_rows")}
            if isinstance(w, dict)
            else w
            for w in diagnostic["warnings"]
        ],
        "internal_tables_not_imported": diagnostic.get("internal_tables_not_imported"),
    }


def audit_agreement(audit: dict, diagnosis: dict) -> dict:
    """The independent audit (scripts.c11_snapshot_audit) must recount the diagnosis."""
    relations = audit["relations"]
    agrees = (
        relations["references_checked"] == diagnosis["relations_checked"]
        and relations["missing_references"] == diagnosis["dangling_references"]
        and audit["missing_file_references"] == diagnosis["missing_file_references"]
        and audit["source_unchanged"] is True
    )
    require(agrees, "independent_audit_disagrees_with_diagnosis")
    return {**audit, "agrees_with_diagnosis": True}


def import_summary(child: dict, load: dict) -> dict:
    auth = load.get("auth")
    require(isinstance(auth, dict) and auth.get("imported") is True, "accounts_not_imported")
    return {
        "rows": child["rows"],
        "storage_mode": load.get("storage_mode"),
        "file_bytes_copied": load.get("file_bytes_copied"),
        "staging_storage_directory": child["staging_storage_directory"],
        "staging_kept_until_cleanup": child["staging_exists"],
        "typed_batch_readback": load.get("typed_batch_readback"),
        "final_typed_reread": load.get("final_typed_reread"),
        "fingerprints_match": load.get("fingerprints_match"),
        "rows_equal_diagnosed_total": load.get("rows_equal_diagnosed_total"),
        "archive_rows": load.get("archive_rows"),
        "conservation_matches_diagnostic": (load.get("conservation") or {}).get(
            "matches_diagnostic"
        ),
        "json_normalizations_stated": load.get("json_normalizations"),
        "pocketbase_read_normalizations_stated": load.get("pocketbase_read_normalizations"),
        "relation_arrays": load.get("relation_arrays"),
        "statements": load.get("statements"),
        "accounts_and_settings": {
            key: auth.get(key)
            for key in ("imported", "admins", "external_auths", "logs_db", "records_snapshot")
        },
        "single_transaction": load.get("single_transaction"),
    }


# ---------------------------------------------------------------------------
# Main.
# ---------------------------------------------------------------------------


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    parser.add_argument("--ppbase", type=Path, required=True, help="PPBase checkout to load")
    parser.add_argument("--data", type=Path, required=True, help="Closed backup directory")
    parser.add_argument("--manifest", type=Path, required=True, help="Private manifest")
    parser.add_argument("--schema", type=Path, required=True, help="Corrected schema export")
    parser.add_argument("--source-version", required=True, help="Supplied, never guessed")
    parser.add_argument("--report", type=Path, required=True, help="New public JSON report")
    parser.add_argument("--private-dir", type=Path, required=True, help="New private directory")
    parser.add_argument(
        "--source-migrations",
        type=Path,
        help=(
            "Frozen source migrationsDir (read only; *.js migrations, with "
            "1785599959_updated_buma_transaction.js): adds the D-0022 idempotency check"
        ),
    )
    parser.add_argument(
        "--accept-divergence",
        action="append",
        default=[],
        metavar="CODE=D-NNNN",
        help="A documented divergence (DIVERGENCES code) and its decision; repeatable",
    )
    parser.add_argument("--passes", type=int, default=2, choices=(1, 2, 3))
    parser.add_argument("--memory", default="2g", help="Container memory limit")
    parser.add_argument("--cpus", default="2", help="Container CPU limit")
    parser.add_argument("--shm-size", default="512m", help="Container /dev/shm size")
    parser.add_argument("--batch-size", type=int, default=importer.DEFAULT_BATCH_SIZE)
    return parser


def environment_facts() -> dict:
    import asyncpg
    import sqlalchemy

    meminfo = dict(
        line.split(":", 1) for line in Path("/proc/meminfo").read_text().splitlines() if ":" in line
    )
    return {
        "python": platform.python_version(),
        "isolated_interpreter": bool(sys.flags.isolated),
        "sqlite": sqlite3.sqlite_version,
        "asyncpg": asyncpg.__version__,
        "sqlalchemy": sqlalchemy.__version__,
        "docker": command("docker", "version", "--format", "{{.Server.Version}}"),
        "image": IMAGE,
        "kernel": platform.release(),
        "cpu_count": os.cpu_count(),
        "memory_total_kib": int(meminfo["MemTotal"].split()[0]),
        "loadavg_at_start": [round(x, 2) for x in os.getloadavg()],
        "other_running_containers": len(command("docker", "ps", "--quiet").splitlines()),
    }


def install_termination_handlers() -> None:
    """SIGTERM and SIGHUP unwind like Ctrl-C, so that every cleanup step runs."""

    def handler(signum, _frame):
        raise KeyboardInterrupt(f"signal {signum}")

    for number in (signal.SIGTERM, signal.SIGHUP):
        signal.signal(number, handler)


def repeatability(passes: list[dict], privates: list[dict]) -> dict:
    """The first two passes: every aggregate check, fingerprints and stored row hashes."""
    if len(passes) < 2:
        return {"compared_passes": len(passes), "identical": None}
    differences = compare_repeatable(passes[0]["checks"], passes[1]["checks"])
    first, second = privates[0].get("fingerprints"), privates[1].get("fingerprints")
    fingerprints = bool(first) and first == second
    row_hashes = privates[0].get("row_hashes_after_import", {})
    other = privates[1].get("row_hashes_after_import", {})
    # _collections.updated is set to now() by import_auth: excluded.
    stable = sorted(table for table in row_hashes if table != "_collections")
    hashes = bool(stable) and all(row_hashes[table] == other.get(table) for table in stable)
    return {
        "compared_passes": 2,
        "both_passed": all(p.get("status") == "passed" for p in passes[:2]),
        "aggregate_differences": differences,
        "table_fingerprints_identical": fingerprints,
        "stored_row_hashes_identical": hashes,
        "identical": not differences and fingerprints and hashes,
    }


def privacy_check(ctx, report: dict) -> dict:
    candidate = ctx.private_dir / "public-report-candidate.json"
    importer.write_json_new(candidate, report)
    output = ctx.private_dir / "privacy-check.json"
    child = run_child(
        "privacy",
        {
            "ppbase": str(ctx.ppbase_root),
            "data": str(ctx.data),
            "manifest": str(ctx.manifest),
            "schema": str(ctx.schema),
            "candidate": str(candidate),
            "output": str(output),
        },
        ctx.private_dir,
        [],
    )
    return {
        "method": "patterns and 64-bit keys of every distinct text value of the snapshot",
        "sensitive_values_hashed": child["sensitive_values_hashed"],
        "strings_checked": child["strings_checked"],
        "violations": child["violations"],
        "passed": child["violations"] == 0,
    }


def decision_sections(text: str) -> dict[str, str]:
    """'## D-NNNN' heading -> the text of its section."""
    sections: dict[str, list[str]] = {}
    current = None
    for line in text.splitlines():
        match = re.match(r"## (D-[0-9]{4})\b", line)
        if match:
            current = match.group(1)
            sections[current] = [line]
        elif current is not None:
            sections[current].append(line)
    return {key: "\n".join(lines) for key, lines in sections.items()}


def accepted_divergences(values: list[str], decisions_text: str) -> dict[str, str]:
    """CODE=D-NNNN pairs: a known divergence code and the recorded decision
    (a '## D-NNNN' section of docs/decisions-and-divergences.md) that names it."""
    sections = decision_sections(decisions_text)
    accepted = {}
    for value in values:
        code, _, decision = value.partition("=")
        require(code in DIVERGENCES, "unknown_divergence_code", divergence=code)
        require(re.fullmatch(r"D-[0-9]{4}", decision) is not None, "decision_reference_required")
        require(decision in sections, "decision_not_recorded", decision=decision)
        require(
            code in sections[decision],
            "decision_does_not_name_the_divergence",
            decision=decision,
            divergence=code,
        )
        accepted[code] = decision
    return accepted


def main(argv=None) -> int:
    argv = sys.argv[1:] if argv is None else argv
    if argv[:1] == ["--child"]:
        return child_main(argv[1], Path(argv[2]))
    args = build_parser().parse_args(argv)
    try:
        require_isolated_interpreter()
        check_paths(args, Path.cwd())
        accepted = accepted_divergences(args.accept_divergence, DECISIONS.read_text())
    except RehearsalFailure as exc:
        print(json.dumps({"status": "refused", **failure_entry(exc)}))
        return 2
    ppbase_state = select_ppbase(args.ppbase)
    install_termination_handlers()
    started = datetime.now(UTC)
    private_dir = make_private_dir(args.private_dir)
    data = args.data.resolve()
    listing_before = directory_listing(data)
    data_sha = importer.digest(data / "data.db")
    definitions = importer.load_schema(args.schema)
    inventory = importer.read_manifest(args.manifest, data / "data.db")["storage"]
    facts_started = time.monotonic()
    facts = source_facts(data, definitions, inventory)
    ctx = SimpleNamespace(
        args=args,
        data=data,
        manifest=args.manifest.resolve(),
        schema=args.schema.resolve(),
        ppbase_root=args.ppbase.resolve(),
        private_dir=private_dir.resolve(),
        definitions=definitions,
        inventory=inventory,
        facts=facts,
        docker_root=Path(command("docker", "info", "--format", "{{.DockerRootDir}}")),
        url="",
        collation=None,
        accepted=accepted,
    )
    auth_evidence = json.loads((PROJECT / AUTH_RUNTIME_REPORT).read_text())
    report = {
        "timestamp_utc": started.isoformat(),
        "scope": "c11_real_backup_rehearsal_without_file_bytes",
        "status": "failed",
        "production_authorized": False,
        "production_touched": False,
        "target": "owned disposable postgres:16-alpine container per pass, destroyed",
        "real_backup": {
            "source_version_supplied": args.source_version,
            "data_db_sha256": data_sha,
            "manifest_storage_objects": len(inventory),
            "file_bytes_available": False,
            "storage_mode": "manifest",
            "logs_db_imported": False,
            "source_reads_seconds": {
                **facts["seconds"],
                "total": round(time.monotonic() - facts_started, 3),
            },
            "sqlite_views": {
                "views": len(facts["views"]),
                "with_duplicate_ids": sorted(
                    name for name, v in facts["views"].items() if v["rows"] != v["distinct_ids"]
                ),
                "with_non_text_ids": sorted(
                    name for name, v in facts["views"].items() if v["non_text_ids"]
                ),
            },
        },
        "code": {
            "buma": git_state(PROJECT),
            "ppbase": ppbase_state,
            "import_api": {
                "unbounded_relation_arrays": importer.UNBOUNDED_RELATION_ARRAYS,
                "validated_source_versions": sorted(importer.VALIDATED_SOURCE_VERSIONS),
            },
        },
        "environment": environment_facts(),
        "accepted_divergences": accepted,
        "token_continuity": {
            "evidence": AUTH_RUNTIME_REPORT,
            "status": auth_evidence.get("status"),
            "timestamp_utc": auth_evidence.get("timestamp_utc"),
            "proven_on": "synthetic fixture: tokens issued by PocketBase 0.22 accepted by PPBase",
            "repeated_here": False,
        },
        "passes": [],
    }
    privates: list[dict] = []
    try:
        for index in range(1, args.passes + 1):
            # Every pass runs, even after a failed one: the second still measures
            # repeatability of everything the first could check.
            summary, private = asyncio.run(run_pass(ctx, index))
            report["passes"].append(summary)
            privates.append(private)
    finally:
        listing_after = directory_listing(data)
        report["source_copy"] = {
            "data_db_sha256_unchanged": importer.digest(data / "data.db") == data_sha,
            "listing_unchanged": listing_after == listing_before,
            "entries": len(listing_after),
            "entries_added": len(set(listing_after) - set(listing_before)),
        }
    report["repeatability"] = repeatability(report["passes"], privates)
    passed = report["passes"] and all(p["status"] == "passed" for p in report["passes"])
    data_checked = [p for p in report["passes"] if "views" in p["checks"]]
    if data_sha == SEPTEMBER_BACKUP_SHA256 and data_checked:
        # Every data check of a pass ran (the views are the last ones).
        report["public_expected_aggregates"] = {
            "sources": [
                "docs/c11-master-diagnostic-report.json",
                "docs/c11-master-detailed-audit.json",
            ],
            "differences": expected_differences(
                observed_aggregates(data_checked[0]["checks"]), SEPTEMBER_EXPECTED
            ),
        }
    ok = bool(
        passed
        and len(report["passes"]) == args.passes
        and report["source_copy"]["data_db_sha256_unchanged"]
        and report["source_copy"]["listing_unchanged"]
        and (args.passes < 2 or report["repeatability"]["identical"])
        and not (report.get("public_expected_aggregates") or {}).get("differences")
    )
    report["status"] = "passed" if ok else "failed"
    report["runbook_durations_seconds"] = runbook_durations(report["passes"])
    report["proves"], report["not_proven_here"] = proven_claims(report, args.passes)
    report["does_not_prove"] = DOES_NOT_PROVE
    report["private_evidence"] = "--private-dir (0700): diagnosis, load report, logs"
    report["privacy_check"] = privacy_check(ctx, report)
    if report["privacy_check"]["passed"]:
        importer.write_json_new(args.report, report)
    print(
        json.dumps(
            {
                "status": report["status"],
                "privacy_check_passed": report["privacy_check"]["passed"],
                "report": str(args.report) if report["privacy_check"]["passed"] else None,
                "private_dir": str(private_dir),
            }
        )
    )
    return 0 if ok and report["privacy_check"]["passed"] else 1


def observed_aggregates(checks: dict) -> dict:
    """The pass aggregates in the shape of SEPTEMBER_EXPECTED."""
    anomalies = checks["preserved_anomalies"]
    arrays = checks["multiple_relation_arrays"]
    accounts = checks["accounts"]
    return {
        "rows": checks["row_counts"]["rows"],
        "tables": checks["row_counts"]["tables"],
        "views": checks["views"]["queried"],
        "relations_checked": anomalies["relations_checked"],
        "dangling_references": anomalies["dangling_references"],
        "dangling_fields": anomalies["dangling_fields"],
        "missing_file_references": anomalies["missing_file_references"],
        "missing_file_fields": anomalies["missing_file_fields"],
        "manifest_storage_objects": anomalies["manifest_storage_objects"],
        "referenced_files": anomalies["referenced_files"],
        "present_referenced_files": anomalies["present_referenced_files"],
        "unreferenced_files": anomalies["unreferenced_files"],
        "auth_records": {
            name: entry["records"] for name, entry in accounts.items() if name != SUPERUSERS
        },
        "superusers": accounts[SUPERUSERS]["imported"],
        "array_length_distribution": {
            key: entry["length_distribution"] for key, entry in arrays.items()
        },
        "max_array_element_length": {
            key: entry["max_element_length"] for key, entry in arrays.items()
        },
        "token_configs": checks["tokens_and_settings"]["source_token_configs"],
    }


def unverified_rules(http: dict) -> list[str]:
    """Collections whose rule the static reading left undetermined (lists, views)."""
    return sorted(
        f"{group}:{name}"
        for group in ("anonymous_lists", "anonymous_views")
        for name in (http.get(group) or {}).get("expression_rules_unverified", [])
    )


def _after_http_problems(checks: dict) -> list[str]:
    """[] when the server and the reads changed nothing: no table (the synthetic
    superuser aside), superusers = source admins + 1, imported accounts equal to
    the source, schema as imported. Read from the after_http values, which are
    written before their require() so that a failure keeps its evidence."""
    after = checks.get("after_http")
    clean = (
        isinstance(after, dict)
        and after.get("tables_changed_by_server_and_reads") == []
        and after.get("superusers_are_source_admins_plus_synthetic") is True
        and after.get("accounts_equal_to_source_after_reads") is True
        and after.get("schema_unchanged_by_server") is True
    )
    return [] if clean else ["server_or_reads_changed_data_accounts_or_schema"]


def _http_problems(checks: dict) -> list[str]:
    """[] when every read had its expected value, no rule was left unverified and
    the server and the reads changed nothing."""
    http = checks.get("http")
    problems = []
    if not isinstance(http, dict) or http.get("failed_checks"):
        problems.append("has_failing_checks")
    elif unverified_rules(http):
        problems.append("left_rules_unverified")
    return problems + _after_http_problems(checks)


def _served_values_problems(checks: dict) -> list[str]:
    served = (checks.get("http") or {}).get("served_values")
    if not isinstance(served, dict):
        return ["has_failing_checks"]
    failing = (checks["http"].get("failed_checks") or {}).keys()
    clean = not served.get("differing_fields") and not any(
        name.startswith("served_values:") for name in failing
    )
    return [] if clean else ["has_failing_checks"]


# (claim, check keys every pass must hold, problems of a pass's checks: [] if none)
CLAIMS = (
    (
        "every record of the real backup imported in one transaction into a disposable "
        "PostgreSQL built by the BUMA migrations; counts per table equal to the diagnosis",
        ("import", "row_counts"),
        None,
    ),
    (
        "per-table fingerprints of the CONVERTED source rows (the importer's digest taken "
        "before COPY) equal to the committed rows re-read in a new session",
        ("fingerprints",),
        None,
    ),
    (
        "source rowids archived for every table and kept as native rowid for users and "
        "buma_article",
        ("rowids",),
        None,
    ),
    (
        "the three maxSelect-null relations stored as arrays equal to the raw SQLite lists, "
        "element for element and in order",
        ("multiple_relation_arrays",),
        None,
    ),
    (
        "orphan relations and references to absent files imported as they are and recounted",
        ("preserved_anomalies",),
        None,
    ),
    (
        "accounts, the PocketBase admin, token secrets and mapped settings equal to the source",
        ("accounts", "tokens_and_settings"),
        None,
    ),
    (
        "schema unchanged by the import apart from the two import archives (indexes and view "
        "bodies included); every view queryable, rows and distinct IDs equal to SQLite",
        ("schema_after_import", "views"),
        None,
    ),
    (
        "values served over HTTP for the compared records equal the PocketBase 0.22.34 "
        "serialization of the raw SQLite values (model independent of the importer; "
        "unmodeled values counted apart, views in served_values.views_not_compared left out, "
        "accepted divergences listed in http.divergences)",
        ("http",),
        _served_values_problems,
    ),
    (
        "an isolated PPBase server serves the imported data with PocketBase 0.22.34 envelopes, "
        "totals, rules, error bodies, filters, expand, sort and file statuses on every "
        "sampled read (no rule left unverified)",
        ("http", "after_http"),
        _http_problems,
    ),
)


def proven_claims(report: dict, passes: int) -> tuple[list[str], list[dict]]:
    """Claims every pass proved, and the others with the reason."""
    proven, missing = [], []
    runs = report["passes"]
    for claim, keys, predicate in CLAIMS:
        reasons = []
        if len(runs) != passes:
            reasons.append("passes_missing")
        for run in runs:
            checks = run.get("checks") or {}
            if not all(key in checks for key in keys):
                reasons.append(f"pass_{run.get('pass')}_did_not_reach_or_failed_these_checks")
            elif predicate is not None:
                reasons += [f"pass_{run.get('pass')}_{problem}" for problem in predicate(checks)]
        if reasons:
            missing.append({"claim": claim, "reasons": sorted(set(reasons))})
        else:
            proven.append(claim)
    repeatable = (
        "the run is repeatable: two complete passes give identical aggregates, fingerprints "
        "and stored row hashes"
    )
    if (report.get("repeatability") or {}).get("identical") and passes >= 2:
        proven.append(repeatable)
    else:
        missing.append({"claim": repeatable, "reasons": ["repeatability_not_identical"]})
    return proven, missing


DOES_NOT_PROVE = [
    "file bytes: no storage/ on this server; byte-for-byte copy is proven on the synthetic "
    "fixture and must be repeated with the complete archive",
    "token continuity on real accounts: proven on the synthetic fixture (WS-C report)",
    "the order of multi-element relation arrays: every real array holds at most one "
    "element; order is proven on the synthetic fixture (docs/c11-rehearsal-report.json)",
    "values of the records not read over HTTP: page 1 by id of every collection and every "
    "record of the multiple-relation and auth collections are compared; the rest only "
    "through the importer's digest of the converted rows",
    "served values of the views whose IDs are duplicated or null: their rows and distinct "
    "IDs are compared with SQLite, their records are not (http.served_values."
    "views_not_compared)",
    "which row a ROW_NUMBER() OVER () view ID names: neither engine defines that order; those "
    "views are compared as multisets of records, IDs aside (http.served_values."
    "row_number_views_compared_as_multisets)",
    "text sort order on a glibc or ICU PostgreSQL: this target (musl) sorts bytes like "
    "SQLite; a glibc en_US.UTF-8 target changes the order (count in http.sort)",
    "desktop, mobile and SDK clients against this data",
    "write routes, hooks and realtime on the imported data",
    "production: no production server, backup freshness or cutover window is involved",
    "the target PostgreSQL configuration and collation of production",
]


if __name__ == "__main__":
    raise SystemExit(main())
