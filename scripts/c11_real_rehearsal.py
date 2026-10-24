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
   fingerprints re-read, rowid archive and native rowids, the three multiple
   relation arrays, preserved anomalies, accounts, token configs, settings,
   schema unchanged, every view counted;
5. starts an isolated PPBase server (BUMA hooks, jobs and migrations off, a
   fresh data directory without file bytes) and reads it over HTTP as an
   anonymous client and as a synthetic superuser created in this database
   only (random credentials, never written anywhere);
6. stops the server, removes the staging copy, the server files and the
   container, and proves it.

The backup directory is only read (SQLite mode=ro&immutable=1); its listing
and data.db SHA-256 are compared before and after the run. The public report
holds aggregates only (counts, statuses, codes, names of the public schema,
durations, sizes) and is written only after an automatic privacy check against
the real values of the snapshot; record-level evidence stays in --private-dir
(0700, files 0600). Token continuity is proven on the synthetic fixture by
WS-C (docs/c11-auth-runtime-report.json), referenced, not repeated here.

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
CHILD_TIMEOUT_SECONDS = 7200
VIEW_TIMEOUT_MS = 900_000
HTTP_TIMEOUT_SECONDS = 900
ENV_KEEP = ("PATH", "HOME", "LANG", "LC_ALL", "TZ")
SAMPLE_PAGE = 50
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
    """Wall time of each named phase, with the container memory of that phase."""

    def __init__(self):
        self.seconds: dict[str, float] = {}
        self.memory: dict[str, dict] = {}
        self.monitor = None

    @contextmanager
    def phase(self, name: str):
        started = time.monotonic()
        try:
            yield
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
SNAKE_CASE = re.compile(r"[a-z][a-z0-9_]*")
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
    """String constants written in these source files: code, never backup data."""
    literals = set()
    for path in paths:
        for node in ast.walk(ast.parse(Path(path).read_text())):
            if isinstance(node, ast.Constant) and isinstance(node.value, str):
                literals.add(node.value)
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
        problems.append("total_pages")
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


def record_shape_problems(collection: dict, record, *, email_visible: bool) -> list[str]:
    """Keys and JSON kinds of a served base/auth record (problem codes, no value)."""
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
        if field["name"] in record and not kind_matches(
            record[field["name"]], expected_kind(field)
        ):
            problems.append("kind:" + field["name"])
    return problems


def error_body_problems(body, status: int, message: str) -> list[str]:
    """PocketBase 0.22 ApiError body {code, message, data: {}} (apis/api_error.go)."""
    if not isinstance(body, dict):
        return ["not_an_object"]
    problems = []
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


def signature_difference(before: list[str], after: list[str]) -> dict:
    """Schema lines added or removed; only the two import archives may appear."""
    added = sorted(set(after) - set(before))
    removed = sorted(set(before) - set(after))
    unexpected = [line for line in added if not any(table in line for table in ARCHIVE_TABLES)]
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
    """Per phase: seconds of each passed pass (runbook timing)."""
    durations: dict[str, list[float]] = {}
    for summary in passes:
        if summary.get("status") != "passed":
            continue
        for name, seconds in summary["measures"]["phase_seconds"].items():
            durations.setdefault(name, []).append(seconds)
    return {name: {"seconds": values, "max": max(values)} for name, values in durations.items()}


# ---------------------------------------------------------------------------
# The owned disposable container.
# ---------------------------------------------------------------------------


class Container:
    def __init__(self, run_id: str, memory: str, cpus: str, shm: str, workdir: Path):
        self.run_id = run_id
        self.name = CONTAINER_PREFIX + run_id
        self.memory, self.cpus, self.shm = memory, cpus, shm
        self.workdir = workdir
        self.password = secrets.token_hex(24)
        self.created = False
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
        try:
            command(
                "docker", "run", "--detach", "--rm", "--name", self.name,
                "--label", f"buma.scope={SCOPE_LABEL}", "--label", f"buma.run={self.run_id}",
                "--memory", self.memory, "--memory-swap", self.memory, "--cpus", self.cpus,
                "--shm-size", self.shm, "--publish", "127.0.0.1::5432",
                "--env-file", env_file, IMAGE,
            )  # fmt: skip
            self.created = True
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

    def data_bytes(self) -> int | None:
        try:
            return int(command("docker", "exec", self.name, "du", "-sb", PGDATA).split()[0])
        except (subprocess.SubprocessError, ValueError, IndexError):
            return None

    def stop(self) -> None:
        if self.created:
            command("docker", "stop", "--time", "20", self.name, timeout=300)

    def removal_evidence(self) -> dict:
        """Container gone (auto-removed) and its anonymous volume with it."""
        gone = False
        for _ in range(300):
            listed = command("docker", "ps", "--all", "--quiet", "--filter", f"name=^/{self.name}$")
            if not listed:
                gone = True
                break
            time.sleep(0.2)
        left = 0
        for volume in self.info.get("anonymous_volumes", []):
            inspected = subprocess.run(
                ["docker", "volume", "inspect", volume], capture_output=True, text=True
            )
            left += inspected.returncode == 0
        return {"container_removed": gone, "anonymous_volumes_left": left}


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
    return {
        "ready": report["ready_for_isolated_rehearsal"],
        "rows": report["rows"],
        "blocker_codes": sorted(
            {str(b.get("code") if isinstance(b, dict) else b) for b in report["blockers"]}
        ),
        "elapsed_seconds": report["elapsed_seconds"],
    }


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


async def target_signature(engine) -> list[str]:
    from scripts.c11_volume_benchmark import target_signature as signature

    return await signature(engine)


async def table_fingerprints(engine, definitions: list[dict], spec: dict) -> dict:
    """Full typed re-read of every table in a new session, as load_isolated does.

    Same digest as the importer's (``_reread``: multiset of the typed row keys),
    so that it is compared with the source digest taken before COPY; plus the
    sum of PostgreSQL's hash of every stored row.
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


async def stored_row_hashes(engine, tables: list[str]) -> dict[str, str]:
    """count:sum of PostgreSQL's hash of each stored row, per table (cheap, server side)."""
    result = {}
    async with engine.connect() as conn:
        for table in tables:
            row = (
                await rows_of(
                    conn,
                    "SELECT count(*) AS n, coalesce(sum(hashtextextended(t::text, 0)), 0)::text "
                    f"AS s FROM {importer.quote(table)} t",
                )
            )[0]
            result[table] = f"{row['n']}:{row['s']}"
    return result


async def count_views(engine, definitions: list[dict]) -> tuple[dict, dict, dict]:
    """Row count of each view on the imported data: counts, seconds, failures."""
    counts, seconds, failures = {}, {}, {}
    for c in definitions:
        if c["type"] != "view":
            continue
        started = time.monotonic()
        try:
            async with engine.connect() as conn:
                await conn.exec_driver_sql(f"SET statement_timeout = {VIEW_TIMEOUT_MS}")
                counts[c["name"]] = await scalar(
                    conn, f"SELECT count(*) FROM {importer.quote(c['name'])}"
                )
        except Exception as exc:  # recorded by SQLSTATE, never by message
            failures[c["name"]] = failure_entry(exc)
        seconds[c["name"]] = round(time.monotonic() - started, 3)
    return counts, seconds, failures


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
    facts: dict = {"auth": {}, "arrays": {}, "samples": {}}
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
            value, rows = most_frequent(holders)
            samples[f"{table}.{field}"] = {
                "value": value,
                "rows_containing": rows,
                "empty_rows": sum(1 for items in arrays.values() if not items),
                "rows": len(arrays),
            }
        # Orphan single relation: most referenced missing target of buma_sale.customer.
        target = relation_target(definitions, "buma_sale", "customer")
        row = db.execute(
            f"SELECT s.customer, count(*) FROM {q('buma_sale')} s WHERE s.customer != '' "
            f"AND NOT EXISTS (SELECT 1 FROM {q(target)} t WHERE t.id = s.customer) "
            "GROUP BY s.customer ORDER BY count(*) DESC, s.customer LIMIT 1"
        ).fetchone()
        samples["orphan"] = {"value": row[0], "rows": row[1]} if row else None
        # Present single relation: most referenced existing target of buma_customer.type.
        target = relation_target(definitions, "buma_customer", "type")
        row = db.execute(
            f"SELECT c.type, count(*) FROM {q('buma_customer')} c WHERE c.type != '' "
            f"AND EXISTS (SELECT 1 FROM {q(target)} t WHERE t.id = c.type) "
            "GROUP BY c.type ORDER BY count(*) DESC, c.type LIMIT 1"
        ).fetchone()
        samples["present_single"] = {"value": row[0], "rows": row[1]} if row else None
        # PocketBase 0.22 sorts in SQLite (BINARY collation): the expected pages.
        samples["sort_created_desc"] = [
            r[0]
            for r in db.execute(
                f"SELECT id FROM {q('buma_sale')} ORDER BY created DESC, id ASC LIMIT {SAMPLE_PAGE}"
            )
        ]
        samples["sort_text_asc"] = [
            r[0]
            for r in db.execute(
                f"SELECT id FROM {q('buma_customer')} ORDER BY name ASC, id ASC LIMIT {SAMPLE_PAGE}"
            )
        ]
        # First record (by id) of each collection whose view rule is admin only,
        # and of one collection whose view rule is an expression.
        samples["admin_only_view"] = {}
        for c in definitions:
            if c["type"] != "view" and c.get("viewRule") is None:
                found = db.execute(f"SELECT id FROM {q(c['name'])} ORDER BY id LIMIT 1").fetchone()
                if found:
                    samples["admin_only_view"][c["name"]] = found[0]
        found = db.execute(
            f"SELECT id FROM {q('buma_pilotage_ticket')} ORDER BY id LIMIT 1"
        ).fetchone()
        samples["expression_view"] = found[0] if found else None
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
    mismatched = sorted(
        name
        for name, value in fingerprints.items()
        if not loaded.get(name)
        or not value["fingerprint"]
        == loaded[name].get("source_fingerprint")
        == loaded[name].get("target_fingerprint")
    )
    require(not mismatched, "fingerprints_differ_after_commit", tables=mismatched)
    checks["fingerprints"] = {
        "tables": len(fingerprints),
        "source_digest_before_copy_equals_committed_rows": len(fingerprints),
        "method": "load_isolated multiset digest of typed rows, re-read in a new session",
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
            require(set(stored) == set(source), "array_rows_differ", field=key)
            identical = sum(1 for record_id, items in source.items() if stored[record_id] == items)
            require(identical == len(source), "array_values_differ", field=key)
            arrays[key] = {
                "rows": len(source),
                "identical_rows_order_and_elements": identical,
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
    checks["views"] = {"queried": len(view_counts), "row_counts": dict(sorted(view_counts.items()))}
    seconds["views"] = round(time.monotonic() - started, 3)
    return checks, private, {"seconds": seconds, "view_seconds": view_seconds, "counts": counts}


async def check_accounts(engine, facts: dict) -> dict:
    """Auth records and the PocketBase admin, compared value by value with the source."""
    result = {}
    async with engine.connect() as conn:
        for name, source in sorted(facts["auth"].items()):
            stored = {
                row["id"]: (
                    row["username"],
                    row["email"],
                    row["email_visibility"],
                    row["verified"],
                    row["password_hash"],
                    row["token_key"],
                )
                for row in await rows_of(
                    conn,
                    "SELECT id, username, email, email_visibility, verified, password_hash, "
                    f"token_key FROM {importer.quote(name)}",
                )
            }
            equal = sum(
                1 for record_id, values in source.items() if stored.get(record_id) == values
            )
            require(len(stored) == len(source) == equal, "auth_records_differ", collection=name)
            result[name] = {"records": len(source), "identity_hash_token_key_equal": equal}
        superusers = {
            row["id"]: (row["email"], row["password_hash"], row["token_key"], row["avatar"])
            for row in await rows_of(
                conn, 'SELECT id, email, password_hash, token_key, avatar FROM "_superusers"'
            )
        }
    require(superusers == facts["admins"], "superusers_differ_from_source_admins")
    result[SUPERUSERS] = {
        "source_admins": len(facts["admins"]),
        "imported": len(superusers),
        "email_hash_token_key_avatar_equal": len(superusers),
    }
    return result


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
    target_settings = as_json(values[0]["value"])
    plan = [(c["name"], auth_import.RECORD_TOKENS) for c in definitions if c["type"] == "auth"] + [
        (SUPERUSERS, auth_import.SUPERUSER_TOKENS)
    ]
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
    require(not failures, "token_configs_differ", configs=failures)
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
    unequal = sorted(name for name, entry in mapped.items() if entry.get("equal") is False)
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
            }
        )
        self.process: subprocess.Popen | None = None
        self.log = None
        self.peak_rss_kib: int | None = None

    @property
    def base(self) -> str:
        return f"http://127.0.0.1:{self.port}"

    def loaded_modules(self) -> dict:
        """ppbase and app as the server's interpreter, environment and cwd resolve them."""
        probe = subprocess.run(
            [sys.executable, "-c", SERVER_PROBE],
            cwd=self.cwd,
            env=self.env,
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
        """PYTHONPATH, cwd and command of the running server, read from /proc."""
        proc = Path("/proc") / str(self.process.pid)
        variables = (proc / "environ").read_bytes().decode().split("\0")
        environment = dict(item.split("=", 1) for item in variables if "=" in item)
        cwd = Path(os.readlink(proc / "cwd"))
        argv = (proc / "cmdline").read_bytes().decode().split("\0")
        require(environment.get("PYTHONPATH") == self.env["PYTHONPATH"], "server_pythonpath")
        require(cwd == self.cwd.resolve(), "server_cwd")
        require(argv[:4] == [sys.executable, "-m", "ppbase", "serve"], "server_command")
        require("app.hooks.register:register" in argv, "server_hooks")
        return {
            "source": "/proc/<server pid>",
            "pythonpath_is_this_checkout_then_selected_ppbase": True,
            "cwd_is_fresh_private_directory": True,
            "jobs_disabled": environment.get("BUMA_MASTER_ENABLE_INTERNAL_JOBS") == "false",
            "auto_migrate_disabled": environment.get("PPBASE_AUTO_MIGRATE") == "false",
            "loopback_only": argv[argv.index("--host") + 1] == "127.0.0.1",
            "smtp_variables": sum(1 for key in environment if "SMTP" in key),
        }

    async def start(self) -> dict:
        import httpx

        make_private_dir(self.cwd)
        make_private_dir(self.data_dir)
        verified = self.loaded_modules()
        fd = os.open(self.log_path, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
        self.log = os.fdopen(fd, "w")
        self.process = subprocess.Popen(
            [
                sys.executable, "-m", "ppbase", "serve", "--db", self.url,
                "--dir", str(self.data_dir), "--migrations-dir", str(MIGRATIONS),
                "--hooks", "app.hooks.register:register", "--host", "127.0.0.1",
                "--port", str(self.port), "--no-automigrate", "--no-apply-migrations-on-start",
                "--no-generate-migrations",
            ],
            cwd=self.cwd, env=self.env, stdout=self.log, stderr=subprocess.STDOUT,
        )  # fmt: skip
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


async def create_synthetic_superuser(url: str) -> tuple[str, str]:
    """A random superuser in the disposable database only; never written anywhere."""
    from ppbase.services import admin_service
    from sqlalchemy.ext.asyncio import AsyncSession

    email = f"ws-g-{secrets.token_hex(8)}@example.com"
    password = secrets.token_urlsafe(32)
    engine = make_engine(url)
    try:
        async with AsyncSession(engine) as session:
            await admin_service.create_admin(session, email, password)
            await session.commit()
    finally:
        await engine.dispose()
    return email, password


async def http_checks(base: str, credentials, ctx, counts: dict, view_counts: dict, sql) -> dict:
    """Aggregated HTTP reads: statuses, counts, shapes and booleans, never a value."""
    import httpx

    definitions, samples = ctx.definitions, ctx.facts["samples"]
    by_name = {c["name"]: c for c in definitions}
    failures: list[str] = []
    result: dict = {}
    timer: dict[str, float] = {}

    async with httpx.AsyncClient(base_url=base, timeout=HTTP_TIMEOUT_SECONDS) as client:

        async def get(path: str, token: str | None = None, **params):
            headers = {"Authorization": token} if token else None
            response = await client.get(path, params=params or None, headers=headers)
            try:
                body = response.json()
            except ValueError:
                body = None
            return response.status_code, body

        def records(name: str) -> str:
            return f"/api/collections/{name}/records"

        started = time.monotonic()
        status, _ = await get("/api/health")
        require(status == 200, "server_health", status=status)
        response = await client.post(
            "/api/admins/auth-with-password",
            json={"identity": credentials[0], "password": credentials[1]},
        )
        body = response.json() if response.status_code == 200 else None
        require(
            isinstance(body, dict) and set(body) == {"token", "admin"},
            "superuser_auth_failed",
            status=response.status_code,
        )
        token = body["token"]
        result["superuser_auth"] = {
            "route": "POST /api/admins/auth-with-password (PocketBase 0.22 SDK)",
            "status": response.status_code,
            "response_keys": sorted(body),
            "admin_keys": sorted(body["admin"]),
        }
        timer["auth"] = time.monotonic() - started

        # Every collection listed by the superuser: envelope, totalItems = SQL count,
        # served record shape (keys and JSON kinds) of the first item.
        started = time.monotonic()
        listed = {}
        for c in definitions:
            name = c["name"]
            expected = counts[name] if c["type"] != "view" else view_counts.get(name)
            status, body = await get(records(name), token, page=1, perPage=1)
            problems = (
                envelope_problems(body, page=1, per_page=1, expected_total=expected)
                if status == 200
                else ["status"]
            )
            if status == 200 and c["type"] != "view" and not problems and body["items"]:
                problems += record_shape_problems(c, body["items"][0], email_visible=True)
            listed[name] = {"status": status, "problems": problems}
        bad = {name: entry for name, entry in listed.items() if entry["problems"]}
        failures += [f"superuser_list:{name}" for name in bad]
        result["superuser_lists"] = {
            "collections": len(listed),
            "base_and_auth_total_items_equal_sql_count": sum(
                1 for c in definitions if c["type"] != "view" and not listed[c["name"]]["problems"]
            ),
            "views_total_items_equal_sql_count": sum(
                1 for c in definitions if c["type"] == "view" and not listed[c["name"]]["problems"]
            ),
            "failing": bad,
        }
        timer["superuser_lists"] = time.monotonic() - started

        # Anonymous lists by rule: admin only -> 403 with PocketBase's body; public
        # -> 200 and every record; expression -> 200, the rule filters.
        started = time.monotonic()
        anonymous = {}
        for c in definitions:
            name, rule = c["name"], c.get("listRule")
            status, body = await get(records(name), None, page=1, perPage=1)
            if rule is None:
                problems = (
                    error_body_problems(body, 403, PB_ADMIN_ONLY) if status == 403 else ["status"]
                )
                anonymous[name] = {"rule": "admin_only", "status": status, "problems": problems}
                continue
            expected = None
            if rule == "":
                expected = counts[name] if c["type"] != "view" else view_counts.get(name)
            problems = (
                envelope_problems(body, page=1, per_page=1, expected_total=expected)
                if status == 200
                else ["status"]
            )
            entry = {"rule": rule_kind(rule), "status": status, "problems": problems}
            if rule != "" and status == 200 and isinstance(body, dict):
                entry["total_items"] = body.get("totalItems")
            anonymous[name] = entry
        bad = {name: entry for name, entry in anonymous.items() if entry["problems"]}
        failures += [f"anonymous_list:{name}" for name in bad]
        result["anonymous_lists"] = {
            "by_rule": dict(Counter(entry["rule"] for entry in anonymous.values())),
            "admin_only_refused_403_with_pocketbase_body": sum(
                1 for e in anonymous.values() if e["rule"] == "admin_only" and not e["problems"]
            ),
            "public_total_items_equal_sql_count": sum(
                1 for e in anonymous.values() if e["rule"] == "public" and not e["problems"]
            ),
            "expression_rules_status_200": sum(
                1 for e in anonymous.values() if e["rule"] == "expression" and not e["problems"]
            ),
            "expression_rules_zero_items": sum(
                1 for e in anonymous.values() if e.get("total_items") == 0
            ),
            "failing": bad,
        }

        # Anonymous view of one record: admin only -> 403, expression rule -> 404.
        views = {}
        for name, record_id in sorted(samples["admin_only_view"].items()):
            status, body = await get(f"{records(name)}/{record_id}")
            problems = (
                error_body_problems(body, 403, PB_ADMIN_ONLY) if status == 403 else ["status"]
            )
            views[name] = {"expected": 403, "status": status, "problems": problems}
        if samples["expression_view"]:
            status, body = await get(
                f"{records('buma_pilotage_ticket')}/{samples['expression_view']}"
            )
            problems = error_body_problems(body, 404, PB_NOT_FOUND) if status == 404 else ["status"]
            views["buma_pilotage_ticket"] = {
                "expected": 404,
                "status": status,
                "problems": problems,
            }
        failures += [f"anonymous_view:{name}" for name, entry in views.items() if entry["problems"]]
        result["anonymous_record_views"] = views

        # Auth e-mail visibility for anonymous readers of the public users list.
        status, body = await get(records("users"), None, page=1, perPage=200)
        items = body.get("items", []) if status == 200 and isinstance(body, dict) else []
        hidden_shown = sum(1 for i in items if "email" in i and not i.get("emailVisibility"))
        visible_missing = sum(1 for i in items if i.get("emailVisibility") and "email" not in i)
        shapes = sum(
            1
            for i in items
            if not record_shape_problems(by_name["users"], i, email_visible=True)
            or not record_shape_problems(by_name["users"], i, email_visible=False)
        )
        result["anonymous_users_email_visibility"] = {
            "status": status,
            "items": len(items),
            "email_served_while_not_visible": hidden_shown,
            "email_missing_while_visible": visible_missing,
            "items_with_pocketbase_shape": shapes,
        }
        if status != 200 or hidden_shown or visible_missing or shapes != len(items):
            failures.append("anonymous_users_email_visibility")
        timer["anonymous"] = time.monotonic() - started

        # Filters, against counts taken in the source (SQLite) and in PostgreSQL.
        started = time.monotonic()
        filters = {}
        for code, name, expression, expected in filter_cases(samples):
            status, body = await get(records(name), token, page=1, perPage=1, filter=expression)
            problems = (
                envelope_problems(body, page=1, per_page=1, expected_total=expected)
                if status == 200
                else ["status"]
            )
            served = body.get("totalItems") if status == 200 and isinstance(body, dict) else None
            filters[code] = {
                "collection": name,
                "status": status,
                "expected_total_items": expected,
                "postgresql_count": sql.get(code),
                "total_items": served,
                "problems": problems,
            }
            if problems or sql.get(code, expected) != expected:
                failures.append("filter:" + code)
        result["filters"] = filters
        timer["filters"] = time.monotonic() - started

        # Expand: ordered lists for multiple relations, absent targets omitted.
        started = time.monotonic()
        result["expand"] = await expand_checks(get, records, samples, token, failures)
        timer["expand"] = time.monotonic() - started

        # Sort: the first page against SQLite's order (BINARY collation).
        started = time.monotonic()
        sorts = {}
        for code, name, order, expected in (
            ("created_desc_then_id", "buma_sale", "-created,id", samples["sort_created_desc"]),
            ("text_name_then_id", "buma_customer", "name,id", samples["sort_text_asc"]),
        ):
            status, body = await get(records(name), token, page=1, perPage=SAMPLE_PAGE, sort=order)
            served = [i.get("id") for i in body.get("items", [])] if status == 200 else []
            sorts[code] = {
                "collection": name,
                "status": status,
                "items": len(served),
                "order_equals_sqlite_source": served == expected,
            }
        failures += [
            f"sort:{code}" for code, e in sorts.items() if not e["order_equals_sqlite_source"]
        ]
        result["sort"] = sorts
        timer["sort"] = time.monotonic() - started

        # Files: no byte on this server. A file present in the manifest is served
        # by PocketBase with its storage (not provable here); a referenced file
        # absent from the backup is 404 in PocketBase 0.22.34 (apis/file.go:211-215).
        started = time.monotonic()
        files = {}
        for code, key, expected in (
            ("present_in_manifest_no_bytes_here", samples["present_file"], 404),
            ("absent_from_backup", samples["missing_file"], 404),
        ):
            if key is None:
                files[code] = {"sampled": False}
                continue
            collection_id, record_id, filename = key.split("/", 2)
            status, body = await get(
                f"/api/files/{collection_id}/{record_id}/{url_quote(filename)}"
            )
            problems = error_body_problems(body, 404, PB_NOT_FOUND) if status == 404 else ["status"]
            files[code] = {
                "sampled": True,
                "status": status,
                "expected": expected,
                "problems": problems,
            }
            if problems:
                failures.append("file:" + code)
        files["present_in_manifest_no_bytes_here"]["expected_with_storage_bytes"] = 200
        result["files"] = files
        timer["files"] = time.monotonic() - started

    result["failed_checks"] = sorted(failures)
    result["seconds"] = {name: round(value, 3) for name, value in timer.items()}
    return result


def filter_cases(samples: dict) -> list[tuple[str, str, str, int]]:
    """(code, collection, filter, expected totalItems from the source)."""
    pdv, tag, managers = (
        samples["buma_customer.pdv"],
        samples["buma_pilotage_ticket.tag"],
        samples["buma_wallet.managers"],
    )
    cases = [
        ("pdv_any_equals", "buma_customer", f'pdv ?= "{pdv["value"]}"', pdv["rows_containing"]),
        ("pdv_length_zero", "buma_customer", "pdv:length = 0", pdv["empty_rows"]),
        ("pdv_length_positive", "buma_customer", "pdv:length > 0", pdv["rows"] - pdv["empty_rows"]),
        (
            "tag_any_equals",
            "buma_pilotage_ticket",
            f'tag ?= "{tag["value"]}"',
            tag["rows_containing"],
        ),
        (
            "managers_any_equals_31_characters_orphan",
            "buma_wallet",
            f'managers ?= "{managers["value"]}"',
            managers["rows_containing"],
        ),
        ("managers_length_zero", "buma_wallet", "managers:length = 0", managers["empty_rows"]),
    ]
    if samples["orphan"]:
        orphan = samples["orphan"]
        cases += [
            (
                "orphan_single_equals",
                "buma_sale",
                f'customer = "{orphan["value"]}"',
                orphan["rows"],
            ),
            # The related row does not exist: no join, no match (PocketBase 0.22 LEFT JOIN).
            ("orphan_single_target_id", "buma_sale", f'customer.id = "{orphan["value"]}"', 0),
        ]
    if samples["present_single"]:
        present = samples["present_single"]
        cases.append(
            ("single_equals", "buma_customer", f'type = "{present["value"]}"', present["rows"])
        )
    return cases


async def filter_sql_counts(engine, samples: dict) -> dict:
    """The same filters counted in PostgreSQL (the imported data itself)."""
    pdv, tag, managers = (
        samples["buma_customer.pdv"],
        samples["buma_pilotage_ticket.tag"],
        samples["buma_wallet.managers"],
    )
    queries = {
        "pdv_any_equals": ("SELECT count(*) FROM buma_customer WHERE :v = ANY(pdv)", pdv["value"]),
        "pdv_length_zero": ("SELECT count(*) FROM buma_customer WHERE cardinality(pdv) = 0", None),
        "pdv_length_positive": (
            "SELECT count(*) FROM buma_customer WHERE cardinality(pdv) > 0",
            None,
        ),
        "tag_any_equals": (
            "SELECT count(*) FROM buma_pilotage_ticket WHERE :v = ANY(tag)",
            tag["value"],
        ),
        "managers_any_equals_31_characters_orphan": (
            "SELECT count(*) FROM buma_wallet WHERE :v = ANY(managers)",
            managers["value"],
        ),
        "managers_length_zero": (
            "SELECT count(*) FROM buma_wallet WHERE cardinality(managers) = 0",
            None,
        ),
    }
    if samples["orphan"]:
        value = samples["orphan"]["value"]
        queries["orphan_single_equals"] = (
            "SELECT count(*) FROM buma_sale WHERE customer = :v",
            value,
        )
        queries["orphan_single_target_id"] = (
            "SELECT count(*) FROM buma_sale s JOIN buma_customer_old o ON o.id = s.customer "
            "WHERE o.id = :v",
            value,
        )
    if samples["present_single"]:
        queries["single_equals"] = (
            "SELECT count(*) FROM buma_customer WHERE type = :v",
            samples["present_single"]["value"],
        )
    counts = {}
    async with engine.connect() as conn:
        for code, (sql, value) in queries.items():
            counts[code] = await scalar(conn, sql, {"v": value} if value is not None else None)
    return counts


async def expand_checks(get, records, samples: dict, token: str, failures: list) -> dict:
    """PocketBase 0.22.34 daos/record_expand.go:217-263 and models/record.go:557-559."""
    pdv, managers = samples["buma_customer.pdv"], samples["buma_wallet.managers"]
    result = {}

    async def items_of(name, **params):
        status, body = await get(records(name), token, page=1, perPage=SAMPLE_PAGE, **params)
        return status, (body.get("items", []) if status == 200 and isinstance(body, dict) else [])

    def ordered_lists(items, field):
        return sum(
            1
            for item in items
            if isinstance((item.get("expand") or {}).get(field), list)
            and [r.get("id") for r in item["expand"][field]] == item.get(field)
        )

    status, items = await items_of("buma_customer", filter=f'pdv ?= "{pdv["value"]}"', expand="pdv")
    result["multiple_present_targets_pdv"] = {
        "status": status,
        "items": len(items),
        "expand_is_list_in_stored_order": ordered_lists(items, "pdv"),
    }
    status, items = await items_of("buma_pilotage_ticket", expand="tag")
    result["multiple_present_targets_tag"] = {
        "status": status,
        "items": len(items),
        "expand_is_list_in_stored_order": ordered_lists(items, "tag"),
    }
    for code in ("multiple_present_targets_pdv", "multiple_present_targets_tag"):
        entry = result[code]
        if (
            entry["status"] != 200
            or not entry["items"]
            or entry["items"] != entry["expand_is_list_in_stored_order"]
        ):
            failures.append("expand:" + code)
    status, items = await items_of(
        "buma_wallet", filter=f'managers ?= "{managers["value"]}"', expand="managers"
    )
    result["multiple_absent_target_managers"] = {
        "status": status,
        "items": len(items),
        "expand_key_omitted": sum(1 for item in items if "expand" not in item),
    }
    if samples["orphan"]:
        status, items = await items_of(
            "buma_sale", filter=f'customer = "{samples["orphan"]["value"]}"', expand="customer"
        )
        result["single_absent_target_customer"] = {
            "status": status,
            "items": len(items),
            "expand_key_omitted": sum(1 for item in items if "expand" not in item),
        }
    for code in ("multiple_absent_target_managers", "single_absent_target_customer"):
        entry = result.get(code)
        if entry and (
            entry["status"] != 200
            or not entry["items"]
            or entry["items"] != entry["expand_key_omitted"]
        ):
            failures.append("expand:" + code)
    if samples["present_single"]:
        value = samples["present_single"]["value"]
        status, items = await items_of("buma_customer", filter=f'type = "{value}"', expand="type")
        objects = sum(
            1
            for item in items
            if isinstance((item.get("expand") or {}).get("type"), dict)
            and item["expand"]["type"].get("id") == item.get("type")
        )
        result["single_present_target_type"] = {
            "status": status,
            "items": len(items),
            "expand_is_one_object": objects,
        }
        if status != 200 or not items or objects != len(items):
            failures.append("expand:single_present_target_type")
    return result


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
                },
                pass_dir,
                running,
            )
            measures["diagnosis_child"] = {
                k: child[k] for k in ("peak_rss_kib", "child_seconds", "parent_wall_seconds")
            }
            checks["diagnosis"] = {
                "ready_for_isolated_rehearsal": child["ready"],
                "rows": child["rows"],
                "blocker_codes": child["blocker_codes"],
            }
            require(child["ready"], "diagnosis_not_ready", blockers=child["blocker_codes"])
            diagnostic = json.loads(diagnostic_path.read_text())
            checks["diagnosis"].update(diagnosis_summary(diagnostic))

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
            credentials = await create_synthetic_superuser(container.url)
            server = PPBaseServer(container.url, pass_dir, ctx.ppbase_root)
            summary["server"] = await server.start()

        with phases.phase("http_reads"):
            sql = await filter_sql_counts(engine, ctx.facts["samples"])
            http = await http_checks(
                server.base,
                credentials,
                ctx,
                counts,
                checks["views"]["row_counts"],
                sql,
            )
            measures["http_seconds"] = http.pop("seconds")
            checks["http"] = http
            credentials = None
            require(not http["failed_checks"], "http_checks_failed", checks=http["failed_checks"])

        with phases.phase("after_http_checks"):
            server.stop()
            summary["server"]["stopped"] = server.stopped()
            summary["server"]["peak_rss_kib"] = server.peak_rss_kib
            summary["server"]["log"] = server.log_summary()
            after = await stored_row_hashes(engine, hashed_tables)
            changed = sorted(
                table
                for table in after
                if after[table] != private["row_hashes_after_import"][table]
            )
            async with engine.connect() as conn:
                superusers = await scalar(conn, 'SELECT count(*) FROM "_superusers"')
            signature = await target_signature(engine)
            checks["after_http"] = {
                "tables_changed_by_server_and_reads": [t for t in changed if t != SUPERUSERS],
                "superusers_after_synthetic_one": superusers,
                "schema_unchanged_by_server": signature_difference(before["signature"], signature)[
                    "only_import_archives_added"
                ],
            }
            require(
                changed in ([SUPERUSERS], []) and superusers == len(ctx.facts["admins"]) + 1,
                "data_changed_by_server_or_reads",
                tables=changed,
            )
            measures["database_bytes_after_http"] = await database_size(engine)
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
            container.removal_evidence() if container.created else {"created": False}
        )
        summary["cleanup"]["staging_removed"] = not staging.exists()
        summary["cleanup"]["server_files_removed"] = not any(
            (pass_dir / name).exists() for name in ("server-cwd", "server-data")
        )
        summary["cleanup"]["server_stopped"] = server is None or server.stopped()
        measures["disk_free_minimum_during_pass"] = disk.finish()
        measures["disk_free_after"] = disk_free(disk_paths)
        measures["phase_seconds"] = phases.seconds
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
    passed = [p for p in passes if p.get("status") == "passed"]
    if len(passed) < 2:
        return {"compared_passes": len(passed), "identical": None}
    differences = compare_repeatable(passed[0]["checks"], passed[1]["checks"])
    fingerprints = privates[0].get("fingerprints") == privates[1].get("fingerprints")
    row_hashes = privates[0].get("row_hashes_after_import", {})
    other = privates[1].get("row_hashes_after_import", {})
    # _collections.updated is set to now() by import_auth: excluded.
    stable = sorted(table for table in row_hashes if table != "_collections")
    hashes = bool(stable) and all(row_hashes[table] == other.get(table) for table in stable)
    return {
        "compared_passes": 2,
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


def main(argv=None) -> int:
    argv = sys.argv[1:] if argv is None else argv
    if argv[:1] == ["--child"]:
        return child_main(argv[1], Path(argv[2]))
    args = build_parser().parse_args(argv)
    try:
        require_isolated_interpreter()
        check_paths(args, Path.cwd())
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
    ctx = SimpleNamespace(
        args=args,
        data=data,
        manifest=args.manifest.resolve(),
        schema=args.schema.resolve(),
        ppbase_root=args.ppbase.resolve(),
        private_dir=private_dir.resolve(),
        definitions=definitions,
        inventory=inventory,
        facts=source_facts(data, definitions, inventory),
        docker_root=Path(command("docker", "info", "--format", "{{.DockerRootDir}}")),
        url="",
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
            summary, private = asyncio.run(run_pass(ctx, index))
            report["passes"].append(summary)
            privates.append(private)
            if summary["status"] != "passed":
                break
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
    if data_sha == SEPTEMBER_BACKUP_SHA256 and passed:
        report["public_expected_aggregates"] = {
            "sources": [
                "docs/c11-master-diagnostic-report.json",
                "docs/c11-master-detailed-audit.json",
            ],
            "differences": expected_differences(
                observed_aggregates(report["passes"][0]["checks"]), SEPTEMBER_EXPECTED
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
    report["proves"] = PROVES if ok else []
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


PROVES = [
    "every record of the real backup imported in one transaction into a disposable PostgreSQL "
    "built by the BUMA migrations, counts and per-table fingerprints equal to the source",
    "source rowids archived for every table and kept as native rowid for users and buma_article",
    "the three maxSelect-null relations copied as arrays, element for element and in order",
    "orphan relations and references to absent files imported as they are and recounted",
    "accounts, the PocketBase admin, token secrets and mapped settings equal to the source",
    "schema unchanged by the import apart from the two import archives; every view queryable",
    "an isolated PPBase server serves the imported data with PocketBase 0.22 envelopes, rules, "
    "filters, expand and sort on the sampled reads",
    "the run is repeatable: two complete passes give identical aggregates and fingerprints",
]
DOES_NOT_PROVE = [
    "file bytes: no storage/ on this server; byte-for-byte copy is proven on the synthetic "
    "fixture and must be repeated with the complete archive",
    "token continuity on real accounts: proven on the synthetic fixture (WS-C report)",
    "desktop, mobile and SDK clients against this data",
    "write routes, hooks and realtime on the imported data",
    "production: no production server, backup freshness or cutover window is involved",
    "the target PostgreSQL configuration and collation of production",
]


if __name__ == "__main__":
    raise SystemExit(main())
