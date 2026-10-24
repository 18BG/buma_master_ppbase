"""PocketBase 0.22.27 vs 0.22.34: what the C11 importer reads (WS-D evidence).

Three independent parts, aggregated into one public report:

1. source: the official repository tags (changed files, storage package trees,
   go.mod modules, CHANGELOG entries and line citations, all verified);
2. binaries: the same synthetic fixtures (profiles ``default`` and
   ``real_shape``) built by both official binaries; their SQLite storage and
   ``storage/`` tree are compared exactly, except values that are random by
   design (salts, token keys, generated IDs and file name parts, build
   timestamps), which are compared by format;
3. real backup (optional): format aggregates of the private 0.22.34 copy,
   computed by a stdlib-only child process started with ``python -I`` outside
   the backup directory. It reads ``data.db`` with ``mode=ro&immutable=1`` and
   reports counts and booleans only: no record value, ID, name, email, hash,
   secret or token.

Run from the BUMA worktree root (the report must be a new path)::

    python -m scripts.c11_version_reference \
      --pocketbase-0-22-27 /path/pocketbase-0.22.27 \
      --pocketbase-0-22-34 /path/pocketbase-0.22.34 \
      --pocketbase-src /path/pocketbase-src \
      --schema /path/buma-master-pb_schema-corrected.json \
      [--real-data /path/private-backup-dir] \
      --report docs/c11-pb-0.22.34-reference-report.json
"""

from __future__ import annotations

import argparse
import hashlib
import json
import re
import sqlite3
import subprocess
import sys
import tempfile
from collections import Counter
from contextlib import closing
from datetime import UTC, datetime
from pathlib import Path

OLD, NEW = "v0.22.27", "v0.22.34"
TAG_COMMITS = {
    OLD: "b83f924c7d35e6108ba6192d0724d67f29deb0c9",
    NEW: "ea7320746f5127b5e5ea360280003682ff41c2e1",
}
# Published in docs/server-session-plan.md; the copy itself stays private.
REAL_DATA_DB_SHA256 = "3539f3eb1c8659421526ede0e47ccc60482aaf0abd2c114282c05719d02b1afd"

DATETIME = re.compile(r"\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2}\.\d{3}Z")
BCRYPT = re.compile(r"\$2a\$12\$[./A-Za-z0-9]{53}")
ALNUM50 = re.compile(r"[A-Za-z0-9]{50}")
PB_ID = re.compile(r"[a-z0-9]{15}")
VIEW_FIELD_ID = re.compile(r"[a-z0-9]{8}")
# tools/filesystem/file.go normalizeName: snake name, "_", RandomString(10), ext.
PB_FILE_NAME = re.compile(
    r"(?P<name>[A-Za-z0-9_]+)_(?P<random>[A-Za-z0-9]{10})(?P<ext>\.[\w.*+=#-]{1,21})?"
)
THUMB_SIZE = re.compile(r"\d+x\d+[tbf]?")
TOKEN_CONFIGS = (
    "adminAuthToken",
    "adminPasswordResetToken",
    "adminFileToken",
    "recordAuthToken",
    "recordPasswordResetToken",
    "recordEmailChangeToken",
    "recordVerificationToken",
    "recordFileToken",
)
SECRET_KEYS = {"secret", "password", "clientSecret", "accessKey"}
AUTH_DATES = ("lastResetSentAt", "lastVerificationSentAt", "lastLoginAlertSentAt")

# Paths whose code decides what a PocketBase 0.22 data.db/storage contains.
STORAGE_PATHS = (
    "models",
    "models/schema",
    "models/settings",
    "daos/base.go",
    "daos/record.go",
    "daos/record_table_sync.go",
    "daos/collection.go",
    "daos/settings.go",
    "daos/param.go",
    "daos/admin.go",
    "daos/table.go",
    "tools/types",
    "tools/security",
    "tools/filesystem",
    "tools/list",
    "tools/inflector",
    "tools/migrate",
    "forms",
    "migrations",
    "core",
    "apis",
    "tokens",
    "resolvers",
    "plugins/migratecmd",
    "cmd",
    "pocketbase.go",
    "examples/base/main.go",
    ".goreleaser.yaml",
)

# (tag, file, line, text that line must contain). Verified on every run.
CITATIONS = {
    "relation_is_multiple": (NEW, "models/schema/schema_field.go", 716, "MaxSelect == nil"),
    "multiple_column": (NEW, "models/schema/schema_field.go", 160, "JSON DEFAULT '[]' NOT NULL"),
    "single_column": (NEW, "models/schema/schema_field.go", 163, "TEXT DEFAULT '' NOT NULL"),
    "number_column": (NEW, "models/schema/schema_field.go", 153, "NUMERIC DEFAULT 0 NOT NULL"),
    "bool_column": (NEW, "models/schema/schema_field.go", 155, "BOOLEAN DEFAULT FALSE NOT NULL"),
    "json_column": (NEW, "models/schema/schema_field.go", 157, "JSON DEFAULT NULL"),
    "relation_prepare_value": (NEW, "models/schema/schema_field.go", 376, "ToUniqueStringSlice"),
    "system_columns": (NEW, "daos/record_table_sync.go", 26, "TEXT PRIMARY KEY"),
    "created_column": (NEW, "daos/record_table_sync.go", 27, "strftime('%Y-%m-%d %H:%M:%fZ')"),
    "token_key_column": (NEW, "daos/record_table_sync.go", 36, "FieldNameTokenKey"),
    "date_layout": (NEW, "tools/types/datetime.go", 12, '"2006-01-02 15:04:05.000Z"'),
    "date_utc": (NEW, "tools/types/datetime.go", 52, "UTC().Format(DefaultDateLayout)"),
    "json_array_value": (NEW, "tools/types/json_array.go", 27, "json.Marshal(m)"),
    "json_raw_value": (NEW, "tools/types/json_raw.go", 47, "len(j) == 0"),
    "record_bcrypt_cost": (
        NEW,
        "models/record.go",
        952,
        "bcrypt.GenerateFromPassword([]byte(password), 12)",
    ),
    "record_token_key": (NEW, "models/record.go", 862, "security.RandomString(50)"),
    "admin_bcrypt_cost": (
        NEW,
        "models/admin.go",
        51,
        "bcrypt.GenerateFromPassword([]byte(password), 12)",
    ),
    "admin_token_key": (NEW, "models/admin.go", 65, "security.RandomString(50)"),
    "id_length": (NEW, "models/base.go", 11, "DefaultIdLength = 15"),
    "id_alphabet": (NEW, "models/base.go", 14, '"abcdefghijklmnopqrstuvwxyz0123456789"'),
    "random_alphabet": (
        NEW,
        "tools/security/random.go",
        10,
        "ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz0123456789",
    ),
    "view_field_id": (NEW, "models/schema/schema.go", 116, "PseudorandomString(8)"),
    "file_name_random": (NEW, "tools/filesystem/file.go", 199, "security.RandomString(10)"),
    "local_fileblob": (NEW, "tools/filesystem/filesystem.go", 97, "fileblob.OpenBucket"),
    "upload_metadata": (NEW, "tools/filesystem/filesystem.go", 211, "Metadata: map[string]string{"),
    "thumb_sizes": (NEW, "apis/file.go", 27, '[]string{"100x100"}'),
    "thumb_path": (NEW, "apis/file.go", 175, '"/thumbs_" + filename'),
    "created_kept": (NEW, "daos/base.go", 317, "m.GetCreated().IsZero()"),
    "settings_defaults_saved": (NEW, "migrations/1640988000_init.go", 98, "settings.New()"),
    "settings_app_name": (NEW, "models/settings/settings.go", 76, '"Acme"'),
    "settings_secret": (NEW, "models/settings/settings.go", 101, "security.RandomString(50)"),
    "migrations_one_transaction": (NEW, "tools/migrate/runner.go", 126, "r.db.Transactional"),
    "command_error_exit_zero": (NEW, "pocketbase.go", 170, "pb.RootCmd.Execute()"),
    "official_noncgo_archive": (NEW, ".goreleaser.yaml", 51, "builds: [build_noncgo]"),
    "noncgo_build": (NEW, ".goreleaser.yaml", 29, "CGO_ENABLED=0"),
    "noncgo_driver": (NEW, "core/db_nocgo.go", 7, '_ "modernc.org/sqlite"'),
    "view_parse_old": (
        OLD,
        "daos/view.go",
        465,
        'joinReplaceRegex.ReplaceAllString(str, " _join_ ")',
    ),
    "view_comments_old": (OLD, "daos/view.go", 467, "commentsReplaceRegex.ReplaceAllString"),
    "view_parse_new": (NEW, "daos/view.go", 465, "commentsReplaceRegex.ReplaceAllString"),
    "view_join_new": (NEW, "daos/view.go", 466, '" __pb_join__ "'),
    "jsvm_script_path": (NEW, "plugins/jsvm/jsvm.go", 40, 'defaultScriptPath = "pb.js"'),
    "jsvm_migration_script": (NEW, "plugins/jsvm/jsvm.go", 206, "vm.RunScript(defaultScriptPath"),
    "oidc_email_verified": (NEW, "tools/auth/oidc.go", 74, "cast.ToBool(extracted.EmailVerified)"),
    "logs_serialization": (NEW, "tools/logger/batch_handler.go", 263, "func normalizeLogAttrValue"),
    "changelog_v0_22_28": (NEW, "CHANGELOG.md", 34, "## v0.22.28"),
    "changelog_v0_22_34": (NEW, "CHANGELOG.md", 1, "## v0.22.34"),
    "gomod_sqlite_old": (OLD, "go.mod", 35, "modernc.org/sqlite v1.32.0"),
    "gomod_sqlite_new": (NEW, "go.mod", 35, "modernc.org/sqlite v1.34.3"),
    "gomod_crypto_new": (NEW, "go.mod", 31, "golang.org/x/crypto v0.31.0"),
    "gomod_dbx_new": (NEW, "go.mod", 26, "github.com/pocketbase/dbx v1.11.0"),
    "gomod_cast_new": (NEW, "go.mod", 28, "github.com/spf13/cast v1.7.1"),
    "gomod_gocloud_new": (NEW, "go.mod", 30, "gocloud.dev v0.40.0"),
    "release_go_old": (OLD, ".github/workflows/release.yaml", 24, ">=1.22.8"),
    "release_go_new": (NEW, ".github/workflows/release.yaml", 24, ">=1.23.5"),
}

# Every non-UI file changed between the tags must be assessed here.
CHANGES = (
    {
        "release": "v0.22.30 (backport of v0.24.4)",
        "paths": ["daos/view.go", "daos/view_test.go"],
        "change": "View field extraction strips SQL comments before replacing JOIN and "
        "WHERE/GROUP BY/... keywords; the internal markers become __pb_join__/__pb_discard__.",
        "citations": ["view_parse_old", "view_comments_old", "view_parse_new", "view_join_new"],
        "importer_input": "_collections.schema of view collections, regenerated when a view "
        "is saved. The importer imports no view row and compares view definitions with the "
        "export without their random field IDs.",
        "effect": "None for BUMA: 4 of the 42 views contain '--' comments, but both binaries "
        "extract identical view schemas (binary comparison) that match the export.",
    },
    {
        "release": "v0.22.31 (backport of v0.25.5)",
        "paths": ["plugins/jsvm/binds.go", "plugins/jsvm/jsvm.go"],
        "change": "JS migrations, hooks, cron and route handlers are compiled with "
        "<cwd>/pb.js as script path so that require() walks parent node_modules.",
        "citations": ["jsvm_script_path", "jsvm_migration_script"],
        "importer_input": "None: module resolution of JS code, not stored data.",
        "effect": "None on storage; fixtures migrations run unchanged on both binaries.",
    },
    {
        "release": "v0.22.33 (backport of v0.26.3)",
        "paths": ["tools/logger/batch_handler.go", "tools/logger/batch_handler_test.go"],
        "change": "Normalized serialization of error attributes in log entries.",
        "citations": ["logs_serialization"],
        "importer_input": "logs.db only, which is never imported.",
        "effect": "None on data.db and storage.",
    },
    {
        "release": "v0.22.34 (backport of v0.26.6)",
        "paths": ["tools/auth/oidc.go"],
        "change": "OIDC userinfo email_verified accepted as int or boolean string.",
        "citations": ["oidc_email_verified"],
        "importer_input": "None: OAuth2 login only (_externalAuths is empty and no OAuth2 "
        "provider is enabled in the real settings).",
        "effect": "None on storage.",
    },
    {
        "release": "v0.22.28 to v0.22.32",
        "paths": ["go.mod", "go.sum"],
        "change": "Dependency upgrades, see dependency_changes.",
        "citations": [
            "gomod_sqlite_old",
            "gomod_sqlite_new",
            "gomod_crypto_new",
            "gomod_dbx_new",
            "gomod_cast_new",
            "gomod_gocloud_new",
        ],
        "importer_input": "Indirect: SQLite engine, query builder, value casting, bcrypt, "
        "local blob storage and MIME detection.",
        "effect": "None observed: identical storage in the binary comparison; both binaries "
        "embed SQLite 3.46.0.",
    },
    {
        "release": "v0.22.30",
        "paths": [".github/workflows/release.yaml"],
        "change": "Release workflow requires Go >= 1.23.5 instead of >= 1.22.8.",
        "citations": ["release_go_old", "release_go_new"],
        "importer_input": "Toolchain of the official binaries.",
        "effect": "None observed in the binary comparison.",
    },
    {
        "release": "v0.22.28 to v0.22.34",
        "paths": ["CHANGELOG.md"],
        "change": "Release notes of the seven backport releases.",
        "citations": ["changelog_v0_22_28", "changelog_v0_22_34"],
        "importer_input": "None.",
        "effect": "None.",
    },
)
UI_CHANGE = {
    "release": "v0.22.28 (backport of v0.23.10)",
    "change": "Admin UI: renew the superuser file token cache from thumb previews/downloads; "
    "rebuilt ui/dist and ui version string.",
    "importer_input": "None: embedded dashboard assets.",
    "effect": "None on storage.",
}
DEPENDENCY_NOTES = {
    "modernc.org/sqlite": "Pure-Go SQLite driver of the official CGO_ENABLED=0 binaries; both "
    "binaries embed SQLite 3.46.0 (probe), same file format.",
    "github.com/mattn/go-sqlite3": "CGO driver; the official archives use the non-CGO build.",
    "github.com/pocketbase/dbx": "Query builder (DDL and writes); v1.11.0 only adds "
    "SelectQuery.PreFragment/PostFragment, unused by PocketBase 0.22.34.",
    "github.com/spf13/cast": "Casts in SchemaField.PrepareValue; v1.7.1 only fixes ToUint64E "
    "for strings above int64, not used for stored values.",
    "golang.org/x/crypto": "bcrypt (cost 12) and ACME; same $2a$12$ format for both binaries, "
    "whose hashes verify the synthetic passwords.",
    "gocloud.dev": "blob/fileblob local storage; the v0.40.0 blob changes concern uploads with "
    "WriterOptions.ContentMD5 (not set by PocketBase), io/fs helpers and cloud drivers; "
    "identical local storage observed.",
    "github.com/gabriel-vasile/mimetype": "Content type stored in .attrs sidecars; identical "
    ".attrs observed for the uploaded PNG and text files.",
    "github.com/goccy/go-json": "HTTP response JSON encoder, not storage.",
    "github.com/golang-jwt/jwt/v4": "JWT parsing security fix (GHSA-mh63-6h87-95cp); tokens are "
    "not stored and tokens/ is identical.",
    "golang.org/x/net": "CVE-2024-45338 fix (HTML parsing for mails), not storage.",
    "golang.org/x/oauth2": "OAuth2 providers, unused by the real backup, not storage.",
    "golang.org/x/sync": "Concurrency helpers, not storage.",
    "github.com/fatih/color": "Terminal colors, not storage.",
}
# What scripts/c11_import.py reads, the 0.22 code producing it and the evidence.
IMPORTER_INPUTS = (
    {
        "input": "_collections rows compared with the export (views without field IDs)",
        "citations": ["view_field_id", "view_parse_new"],
        "binary_evidence": "/tables/_collections identical; view_schemas equal to the export",
        "real_evidence": "collections_rows; 114 definitions matched on 2026-09-28",
    },
    {
        "input": "sqlite_master table SQL and PRAGMA table_info (no WITHOUT ROWID, no rowid alias)",
        "citations": ["system_columns", "created_column", "token_key_column"],
        "binary_evidence": "/sqlite_master and /tables/*/columns identical",
        "real_evidence": "ddl_compared_with_0_22_34_real_shape_fixture",
    },
    {
        "input": "SELECT _rowid_, * per collection: integer rowids",
        "citations": ["noncgo_driver"],
        "binary_evidence": "rowids of every row identical",
        "real_evidence": "value_formats.rowid",
    },
    {
        "input": "date, created and updated: 'YYYY-MM-DD HH:MM:SS.mmmZ' (UTC) or ''",
        "citations": ["date_layout", "date_utc", "created_column", "created_kept"],
        "binary_evidence": "fixed dates identical, generated dates pb_datetime_now",
        "real_evidence": "value_formats.date, created_updated, auth_legacy_dates",
    },
    {
        "input": "bool as integer 0/1, number as integer or real",
        "citations": ["bool_column", "number_column"],
        "binary_evidence": "values and SQLite storage classes identical",
        "real_evidence": "value_formats.bool, value_formats.number",
    },
    {
        "input": "json as JSON text or SQL NULL",
        "citations": ["json_column", "json_raw_value"],
        "binary_evidence": "JSON texts identical (object, text null, SQL NULL)",
        "real_evidence": "value_formats.json",
    },
    {
        "input": "multiple select/file/relation (relation maxSelect null) as JSON string arrays",
        "citations": ["relation_is_multiple", "multiple_column", "json_array_value"],
        "binary_evidence": "array texts identical, order kept",
        "real_evidence": "value_formats.multiple_relation, multiple_file",
    },
    {
        "input": "single select/file/relation and text fields as text",
        "citations": ["single_column", "relation_prepare_value"],
        "binary_evidence": "texts identical (orphans, custom IDs, undeclared values)",
        "real_evidence": "value_formats.single_*, shape_inventory",
    },
    {
        "input": "auth passwordHash, tokenKey, emailVisibility, verified, username, email",
        "citations": ["record_bcrypt_cost", "record_token_key", "random_alphabet"],
        "binary_evidence": "bcrypt_2a_cost_12 verified, random_alnum_50",
        "real_evidence": "value_formats.auth_*",
    },
    {
        "input": "storage/<collectionId>/<recordId>/<file>, .attrs sidecars, thumbs_ directories",
        "citations": ["file_name_random", "local_fileblob", "upload_metadata", "thumb_sizes"]
        + ["thumb_path"],
        "binary_evidence": "/storage paths (random part masked), sizes and SHA-256 identical",
        "real_evidence": "storage_manifest, value_formats.file_names",
    },
    {
        "input": "internal tables counted; _admins and _params read by WS-C",
        "citations": ["admin_bcrypt_cost", "admin_token_key", "settings_defaults_saved"]
        + ["settings_secret", "settings_app_name"],
        "binary_evidence": "/tables/_admins, _params, _migrations identical by format",
        "real_evidence": "admins, params, migrations, external_auths",
    },
)
STATEMENT = (
    "PocketBase v0.22.34 changes none of the code that decides what data.db and storage/ "
    "contain (models, models/schema, tools/types, tools/security, tools/filesystem, forms, "
    "migrations, core, apis and the record/table/settings daos have identical git objects); its "
    "only daos change, daos/view.go, regenerates view schemas and gives the same fields for the "
    "42 BUMA views. Built from the same synthetic fixtures, both official binaries store "
    "identical values, rowids, DDL and storage objects, apart from values that are random by "
    "design, which keep the same formats; the importer at this commit reads both identically "
    "and only its version gate rejects 0.22.34. The real 0.22.34 backup uses the same formats. "
    "Accepting source_version 0.22.34 in scripts/c11_import.py needs no reader change; the "
    "D-0019 and D-0021 blockers are independent of the version."
)
DEFAULT_DEPENDENCY_NOTE = (
    "S3/AWS, Google API or indirect dependency, not on the local storage path."
)


def digest(path: Path) -> str:
    with path.open("rb") as stream:
        return hashlib.file_digest(stream, "sha256").hexdigest()


def quote(name: str) -> str:
    return '"' + name.replace('"', '""') + '"'


def connect_read_only(path: Path) -> sqlite3.Connection:
    db = sqlite3.connect(path.resolve().as_uri() + "?mode=ro&immutable=1", uri=True)
    db.execute("PRAGMA query_only=ON")
    return db


def header(path: Path) -> dict:
    with path.open("rb") as stream:
        raw = stream.read(100)
    if raw[:16] != b"SQLite format 3\x00":
        raise ValueError("Not an SQLite database")
    page_size = int.from_bytes(raw[16:18], "big")
    return {
        "page_size": 65536 if page_size == 1 else page_size,
        "file_format_write_version": raw[18],
        "file_format_read_version": raw[19],
        "schema_format": int.from_bytes(raw[44:48], "big"),
        "text_encoding": {1: "UTF-8", 2: "UTF-16le", 3: "UTF-16be"}[
            int.from_bytes(raw[56:60], "big")
        ],
        "user_version": int.from_bytes(raw[60:64], "big"),
        "application_id": int.from_bytes(raw[68:72], "big"),
        "last_writer_sqlite_version": int.from_bytes(raw[96:100], "big"),
    }


def go_compact_json(value) -> str:
    """encoding/json Marshal output for JSON values (HTML characters escaped)."""
    text = json.dumps(value, separators=(",", ":"), ensure_ascii=False)
    for raw, escaped in (("<", "\\u003c"), (">", "\\u003e"), ("&", "\\u0026")):
        text = text.replace(raw, escaped)
    return text.replace("\u2028", "\\u2028").replace("\u2029", "\\u2029")


def is_multiple(field: dict) -> bool:
    options = field.get("options") or {}
    if field["type"] == "relation":
        return options.get("maxSelect") is None or options["maxSelect"] > 1
    return field["type"] in {"select", "file"} and (options.get("maxSelect") or 0) > 1


def paths_and_types(value, prefix="") -> dict:
    if isinstance(value, dict):
        out = {}
        for key, item in value.items():
            out.update(paths_and_types(item, prefix + "/" + key))
        return out
    return {prefix: "list" if isinstance(value, list) else type(value).__name__}


# ---------------------------------------------------------------------------
# 1. Source comparison


def git(src: Path, *args: str) -> str:
    return subprocess.run(
        ["git", "-C", str(src), *args], capture_output=True, text=True, check=True
    ).stdout


def verify_citations(src: Path) -> dict:
    cache, verified = {}, {}
    for key, (tag, file, line, text) in CITATIONS.items():
        if (tag, file) not in cache:
            cache[tag, file] = git(src, "show", f"{tag}:{file}").splitlines()
        actual = cache[tag, file][line - 1]
        if text not in actual:
            raise SystemExit(f"Citation {key} no longer matches {tag}:{file}:{line}")
        verified[key] = f"{tag}:{file}:{line}"
    return verified


def go_modules(content: str) -> dict[str, tuple[str, bool]]:
    modules, block = {}, False
    for line in content.splitlines():
        stripped = line.strip()
        if stripped.startswith("require ("):
            block = True
        elif block and stripped == ")":
            block = False
        elif block and stripped:
            parts = stripped.split()
            modules[parts[0]] = (parts[1], "// indirect" in stripped)
    return modules


def changelog_between(content: str) -> list[dict]:
    entries, current = [], None
    for line in content.splitlines():
        if line.startswith("## "):
            if line.strip() == "## " + OLD:
                break
            current = {"version": line[3:].strip(), "entries": []}
            entries.append(current)
        elif current and line.startswith("- "):
            current["entries"].append(re.sub(r"\s*\(\[#\d+\].*$", "", line[2:]).strip())
    return entries


def source_comparison(src: Path) -> dict:
    commits = {tag: git(src, "rev-parse", tag + "^{commit}").strip() for tag in (OLD, NEW)}
    if commits != TAG_COMMITS:
        raise SystemExit("PocketBase tags do not resolve to the verified release commits")
    changed = git(src, "diff", "--name-only", OLD, NEW).split()
    numstat = {}
    for line in git(src, "diff", "--numstat", OLD, NEW).splitlines():
        added, removed, path = line.split("\t")
        numstat[path] = {"added": int(added), "removed": int(removed)}
    code = sorted(p for p in changed if not p.startswith("ui/"))
    assessed = {p for change in CHANGES for p in change["paths"]}
    if set(code) != assessed:
        raise SystemExit("Unassessed PocketBase change: " + ", ".join(sorted(set(code) ^ assessed)))
    trees = []
    for path in STORAGE_PATHS:
        objects = [git(src, "rev-parse", f"{tag}:{path}").strip() for tag in (OLD, NEW)]
        trees.append(
            {"path": path, "git_object": objects[1], "identical": objects[0] == objects[1]}
        )
    modules = {tag: go_modules(git(src, "show", f"{tag}:go.mod")) for tag in (OLD, NEW)}
    dependencies = []
    for module in sorted(set(modules[OLD]) | set(modules[NEW])):
        before, after = modules[OLD].get(module), modules[NEW].get(module)
        if before == after:
            continue
        users = subprocess.run(
            ["git", "-C", str(src), "grep", "-l", "-F", f'"{module}', NEW, "--", "*.go"]
            + [":!*_test.go"],
            capture_output=True,
            text=True,
        )
        if users.returncode not in (0, 1):  # 1: no non-test Go file imports it
            raise SystemExit("git grep failed")
        packages = sorted({str(Path(p.split(":", 1)[1]).parent) for p in users.stdout.split()})
        dependencies.append(
            {
                "module": module,
                OLD: before[0] if before else None,
                NEW: after[0] if after else None,
                "indirect": bool(after and after[1]),
                "imported_by_packages": packages,
                "assessment": DEPENDENCY_NOTES.get(module, DEFAULT_DEPENDENCY_NOTE),
            }
        )
    citations = verify_citations(src)
    return {
        "repository": "https://github.com/pocketbase/pocketbase",
        "tags": {tag: commits[tag] for tag in (OLD, NEW)},
        "command": f"git diff --stat {OLD} {NEW}",
        "changed_files": len(changed),
        "changed_ui_files": len(changed) - len(code),
        "changed_code_files": [{"path": p, **numstat[p]} for p in code],
        "changes": [
            {**change, "citations": [citations[c] for c in change["citations"]]}
            for change in CHANGES
        ]
        + [UI_CHANGE | {"paths": ["ui/**"], "citations": []}],
        "storage_paths": trees,
        "storage_paths_identical": [t["path"] for t in trees if t["identical"]],
        "storage_paths_changed": [t["path"] for t in trees if not t["identical"]],
        "dependency_changes": dependencies,
        "importer_inputs": [
            {**item, "citations": [citations[c] for c in item["citations"]]}
            for item in IMPORTER_INPUTS
        ],
        "changelog": changelog_between(git(src, "show", f"{NEW}:CHANGELOG.md")),
        "citations": citations,
    }


# ---------------------------------------------------------------------------
# 2. Binary comparison of the same synthetic fixtures


def embedded_sqlite_version(binary: Path, work: Path) -> str:
    migrations = work / "migrations"
    migrations.mkdir(parents=True)
    (migrations / "1789236999_c11_sqlite_version.js").write_text(
        'migrate((db)=>{const m=new DynamicModel({v:""});'
        'db.newQuery("SELECT sqlite_version() AS v").one(m);'
        'console.log("C11_SQLITE_VERSION="+m.v);},()=>{});\n'
    )
    completed = subprocess.run(
        [
            str(binary),
            "migrate",
            "up",
            "--dir",
            str(work / "data"),
            "--migrationsDir",
            str(migrations),
        ],
        capture_output=True,
        text=True,
        check=True,
    )
    match = re.search(r"C11_SQLITE_VERSION=([0-9.]+)", completed.stdout + completed.stderr)
    if not match:
        raise RuntimeError("PocketBase did not report its SQLite version")
    return match.group(1)


class Masker:
    """Replace values that are random by design with their verified format."""

    def __init__(self, definitions, fixed_dates, known_files, passwords):
        self.file_columns = {
            (c["name"], f["name"])
            for c in definitions
            for f in c["schema"]
            if c["type"] != "view" and f["type"] == "file"
        }
        self.fixed_dates = fixed_dates
        self.known_files = known_files
        self.passwords = passwords
        self.classes = Counter()
        self.unexpected = []

    def token(self, kind: str, ok: bool, where: str) -> str:
        self.classes[kind] += 1
        if not ok:
            self.unexpected.append(where)
        return f"<{kind}>" if ok else f"<unexpected {kind}>"

    def file_name(self, name: str, where: str) -> str:
        if name in self.known_files:
            return name
        match = PB_FILE_NAME.fullmatch(name)
        self.token("pb_generated_file_name", bool(match), where)
        return f"{match['name']}_<random10>{match['ext'] or ''}" if match else "<unexpected>"

    def storage_path(self, relative: str) -> str:
        parts = relative.split("/")
        masked = []
        for part in parts:
            suffix = ".attrs" if part.endswith(".attrs") else ""
            core = part[: -len(suffix)] if suffix else part
            prefix = ""
            if core.startswith("thumbs_"):
                prefix, core = "thumbs_", core[len("thumbs_") :]
            elif "_" in core and THUMB_SIZE.fullmatch(core.split("_", 1)[0]):
                prefix, core = core.split("_", 1)[0] + "_", core.split("_", 1)[1]
            if PB_FILE_NAME.fullmatch(core) and core not in self.known_files:
                match = PB_FILE_NAME.fullmatch(core)
                core = f"{match['name']}_<random10>{match['ext'] or ''}"
            masked.append(prefix + core + suffix)
        return "/".join(masked)

    def value(self, table: str, column: str, value, row: dict):
        where = f"{table}.{column}"
        if column == "tokenKey":
            return self.token("random_alnum_50", bool(ALNUM50.fullmatch(value)), where)
        if column == "passwordHash":
            ok = bool(BCRYPT.fullmatch(value))
            identity = row.get("email") if table == "_admins" else row.get("id")
            password = self.passwords.get((table, identity))
            if ok and password:
                import bcrypt  # synthetic fixture only; never used on real data

                ok = bcrypt.checkpw(password.encode(), value.encode())
                self.classes["bcrypt_verified_with_synthetic_password"] += int(ok)
            return self.token("bcrypt_2a_cost_12", ok, where)
        if column in {"created", "updated"} and value not in self.fixed_dates:
            return self.token("pb_datetime_now", bool(DATETIME.fullmatch(value)), where)
        if table in {"_admins", "_params"} and column == "id":
            return self.token("random_id_15", bool(PB_ID.fullmatch(value)), where)
        if table == "_migrations" and column == "applied":
            ok = isinstance(value, int) and 10**15 <= value < 10**17
            return self.token("unix_microseconds", ok, where)
        if table == "_params" and column == "value":
            return self.settings(json.loads(value), where)
        if table == "_collections" and column == "schema" and row.get("type") == "view":
            fields = json.loads(value)
            for field in fields:
                field["id"] = self.token(
                    "random_view_field_id_8", bool(VIEW_FIELD_ID.fullmatch(field["id"])), where
                )
            return fields
        if (table, column) in self.file_columns and value:
            if value.startswith("["):
                return [self.file_name(name, where) for name in json.loads(value)]
            return self.file_name(value, where)
        return value

    def settings(self, settings: dict, where: str) -> dict:
        for name in TOKEN_CONFIGS:
            secret = settings[name]["secret"]
            settings[name]["secret"] = self.token(
                "random_alnum_50", bool(ALNUM50.fullmatch(secret)), where
            )
        return settings


def storage_model(data: Path, masker: Masker) -> dict:
    db_path = data / "data.db"
    model = {"header": header(db_path), "sqlite_master": [], "tables": {}, "storage": []}
    with closing(connect_read_only(db_path)) as db:
        db.row_factory = sqlite3.Row
        model["sqlite_master"] = [
            list(row)
            for row in db.execute(
                "SELECT type,name,tbl_name,sql FROM sqlite_master ORDER BY type,name"
            )
        ]
        tables = [r[0] for r in db.execute("SELECT name FROM sqlite_master WHERE type='table'")]
        for table in sorted(tables):
            info = [list(r) for r in db.execute(f"PRAGMA table_info({quote(table)})")]
            rows = []
            for raw in db.execute(f"SELECT _rowid_ AS __rowid, * FROM {quote(table)} ORDER BY 1"):
                record = dict(raw)
                rowid = record.pop("__rowid")
                values = {}
                for column, value in record.items():
                    kind = type(value).__name__
                    values[column] = [kind, masker.value(table, column, value, record)]
                rows.append([rowid, values])
            model["tables"][table] = {"columns": info, "rows": rows}
    storage = data / "storage"
    for path in sorted(p for p in storage.rglob("*") if p.is_file()):
        relative = path.relative_to(storage).as_posix()
        model["storage"].append([masker.storage_path(relative), path.stat().st_size, digest(path)])
    return model


def differences(left, right, path="") -> list[str]:
    if isinstance(left, dict) and isinstance(right, dict):
        out = []
        for key in sorted(set(left) | set(right), key=str):
            if key not in left or key not in right:
                out.append(f"{path}/{key}: present in one binary only")
            else:
                out.extend(differences(left[key], right[key], f"{path}/{key}"))
        return out
    if isinstance(left, list) and isinstance(right, list):
        if len(left) != len(right):
            return [f"{path}: {len(left)} != {len(right)} items"]
        out = []
        for index, (a, b) in enumerate(zip(left, right, strict=True)):
            out.extend(differences(a, b, f"{path}[{index}]"))
        return out
    return [] if left == right else [path]


def model_summary(model: dict) -> dict:
    rows = sum(len(t["rows"]) for t in model["tables"].values())
    return {
        "sqlite_master_entries": Counter(entry[0] for entry in model["sqlite_master"]),
        "tables": len(model["tables"]),
        "rows": rows,
        "values": sum(len(r[1]) for t in model["tables"].values() for r in t["rows"]),
        "storage_objects": len(model["storage"]),
        "storage_attrs_sidecars": sum(1 for s in model["storage"] if s[0].endswith(".attrs")),
        "storage_thumbnails": sum(
            1 for s in model["storage"] if "/thumbs_" in s[0] and not s[0].endswith(".attrs")
        ),
        "header": model["header"],
    }


def importer_view(data: Path, schema: Path, version: str) -> dict:
    """What the importer at this commit reads, with and without the version gate."""
    from scripts.c11_import import diagnose

    neutral = diagnose(data, schema, source_version=None)
    gated = diagnose(data, schema, source_version=version)
    return {
        "read": {
            "blocker_codes_besides_unknown_version": sorted(
                {b["code"] for b in neutral["blockers"]} - {"source_pocketbase_version_unknown"}
            ),
            "warnings": sorted(json.dumps(w, sort_keys=True) for w in neutral["warnings"]),
            "rows": neutral["rows"],
            "relations_checked": neutral["relations_checked"],
            "files": neutral["files"],
            "unreferenced_files": neutral["unreferenced_files"],
            "identity_sha256": {c["name"]: c["identity_sha256"] for c in neutral["collections"]},
            "internal_tables_not_imported": neutral["internal_tables_not_imported"],
        },
        "with_source_version": {
            "argument": version,
            "blocker_codes": sorted({b["code"] for b in gated["blockers"]}),
        },
    }


def view_schemas(data: Path, definitions: list[dict]) -> dict:
    """View fields regenerated by the binary, compared with the export (no IDs)."""
    expected = {
        c["name"]: [{k: v for k, v in f.items() if k != "id"} for f in c["schema"]]
        for c in definitions
        if c["type"] == "view"
    }
    with closing(connect_read_only(data / "data.db")) as db:
        stored = {
            name: [{k: v for k, v in f.items() if k != "id"} for f in json.loads(schema)]
            for name, schema in db.execute(
                "SELECT name, schema FROM _collections WHERE type='view'"
            )
        }
    commented = {
        c["name"]
        for c in definitions
        if c["type"] == "view" and ("--" in c["options"]["query"] or "/*" in c["options"]["query"])
    }
    return {
        "views": len(stored),
        "equal_to_export_without_field_ids": sum(stored[n] == expected.get(n) for n in stored),
        "views_with_sql_comments": len(commented),
        "commented_views_equal_to_export": sum(stored[n] == expected[n] for n in commented),
    }


def load_baseline_fixture(source: Path):
    """The fixture module of an earlier commit, to prove the default profile unchanged."""
    import importlib.util

    spec = importlib.util.spec_from_file_location("c11_fixture_baseline", source)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def binary_comparison(
    binaries: dict[str, Path], schema: Path, work: Path, baseline: tuple[str, Path] | None = None
) -> dict:
    from scripts import c11_fixture as fixture

    definitions = json.loads(schema.read_text())
    result = {"binaries": {}, "profiles": {}}
    for version, binary in binaries.items():
        if fixture.pocketbase_version(binary) != version:
            raise SystemExit(f"{binary} is not PocketBase {version}")
        result["binaries"][version] = {
            "sha256": digest(binary),
            "embedded_sqlite_version": embedded_sqlite_version(binary, work / f"sqlite-{version}"),
        }
    fixed_dates = {"2024-02-29 12:34:56.789Z", "2024-03-01 00:00:00.000Z"} | {
        created for rows in fixture.RETURN_DUPLICATES.values() for _, created, *_ in rows
    }
    known_files = {"proof.bin", "one.bin", "two.bin"} | {
        name
        for files in fixture.REAL_SHAPE["missing_files"].values()
        for names in files.values()
        for name in names
    }
    passwords = {
        (collection, record): password
        for accounts in fixture.ACCOUNTS.values()
        for collection, record, _, _, password in accounts
        if collection != "_admins"
    } | {("_admins", fixture.ADMIN_EMAIL): fixture.ADMIN_PASSWORD}
    default_models = {}
    for profile in fixture.PROFILES:
        models, maskers, diagnostics, views = {}, {}, {}, {}
        for version, binary in binaries.items():
            data = fixture.create_fixture(
                work / f"{profile}-{version}", binary, schema, profile=profile
            )
            maskers[version] = Masker(definitions, fixed_dates, known_files, passwords)
            models[version] = storage_model(data, maskers[version])
            diagnostics[version] = importer_view(data, schema, version)
            views[version] = view_schemas(data, definitions)
        if profile == "default":
            default_models = models
        found = differences(models["0.22.27"], models["0.22.34"])
        same_diagnostic = differences(
            diagnostics["0.22.27"]["read"], diagnostics["0.22.34"]["read"]
        )
        result["profiles"][profile] = {
            "storage_identical": not found,
            "differences": found,
            "random_value_classes": {
                version: dict(sorted(masker.classes.items())) for version, masker in maskers.items()
            },
            "unexpected_formats": {v: m.unexpected for v, m in maskers.items()},
            "summary": model_summary(models["0.22.34"]),
            "importer_diagnostic": diagnostics,
            "importer_diagnostic_differences": same_diagnostic,
            "view_schemas": views,
        }
    if baseline:
        commit, source = baseline
        module = load_baseline_fixture(source)
        regression = {}
        for version, binary in binaries.items():
            data = module.create_fixture(work / f"baseline-{version}", binary, schema)
            masker = Masker(definitions, fixed_dates, known_files, passwords)
            found = differences(storage_model(data, masker), default_models[version])
            regression[version] = {"identical": not found, "differences": found}
        result["default_profile_vs_" + commit] = regression
    return result


# ---------------------------------------------------------------------------
# 3. Real backup format aggregates (stdlib only, started with python -I)


def _classify_date(value) -> str:
    if value == "":
        return "empty"
    return "pb_datetime" if isinstance(value, str) and DATETIME.fullmatch(value) else "other"


def _counter(counter: Counter) -> dict:
    return dict(sorted(counter.items()))


def id_shape(value: str) -> str:
    """Length and character classes of an identifier, never the identifier itself."""
    classes = (
        ("lower", "[a-z]"),
        ("upper", "[A-Z]"),
        ("digit", "[0-9]"),
        ("other", "[^A-Za-z0-9]"),
    )
    return f"len{len(value)}:" + "+".join(k for k, pattern in classes if re.search(pattern, value))


def _columns(db, table: str) -> list[tuple]:
    return [tuple(r[1:]) for r in db.execute(f"PRAGMA table_info({quote(table)})")]


def real_formats(data: Path, schema: Path, fixture_default: Path, fixture_real: Path) -> dict:
    db_path = data / "data.db"
    before = digest(db_path)
    definitions = json.loads(schema.read_text())
    report = {
        "mode": "read_only_aggregates_mode_ro_immutable",
        "data_db_sha256_matches_published": before == REAL_DATA_DB_SHA256,
        "header": header(db_path),
    }
    with (
        closing(connect_read_only(db_path)) as db,
        closing(connect_read_only(fixture_real)) as fx,
        closing(connect_read_only(fixture_default)) as defaults_db,
    ):
        report["integrity_check"] = db.execute("PRAGMA integrity_check").fetchone()[0]
        master = {}
        for name, source in (("real", db), ("fixture", fx)):
            master[name] = {
                (t, n): (tbl, re.sub(r"\s+", " ", sql or "").strip())
                for t, n, tbl, sql in source.execute(
                    "SELECT type,name,tbl_name,sql FROM sqlite_master"
                )
                if not n.startswith("sqlite_autoindex_")
            }
        ddl = {"tables": Counter(), "views": Counter(), "indexes": Counter()}
        column_differences = []
        for (kind, name), (_, sql) in sorted(master["fixture"].items()):
            key = {"table": "tables", "view": "views", "index": "indexes"}[kind]
            other = master["real"].get((kind, name))
            if other is None:
                ddl[key]["missing_in_real"] += 1
                continue
            if other[1] == sql:
                ddl[key]["identical_sql"] += 1
                continue
            if kind != "table":
                # SQLite's ALTER TABLE rename rewrites double-quoted string literals of
                # dependent views as single-quoted ones; the importer reads no view SQL.
                quotes = other[1].replace('"', "'") == sql.replace('"', "'")
                ddl[key]["different_sql_only_quote_characters" if quotes else "different_sql"] += 1
                continue
            real_columns, fixture_columns = _columns(db, name), _columns(fx, name)
            if real_columns == fixture_columns:
                ddl[key]["same_columns_same_order_other_sql_text"] += 1
            elif sorted(real_columns) == sorted(fixture_columns):
                ddl[key]["same_column_declarations_other_order"] += 1
            else:
                ddl[key]["different_column_declarations"] += 1
                real_set, fixture_set = set(real_columns), set(fixture_columns)
                column_differences.append(
                    {
                        "table": name,
                        "real_only": sorted(map(list, real_set - fixture_set)),
                        "fixture_only": sorted(map(list, fixture_set - real_set)),
                    }
                )
        extra = sorted(
            f"{kind}:{name}" for kind, name in set(master["real"]) - set(master["fixture"])
        )
        ddl = {key: _counter(value) for key, value in ddl.items()}
        ddl["column_declaration_differences"] = column_differences
        ddl["entries_only_in_real"] = extra
        ddl["view_queries_in_collections_equal_export"] = sum(
            json.loads(options)["query"] == expected
            for name, options in db.execute(
                "SELECT name, options FROM _collections WHERE type='view'"
            )
            for expected in [next(c["options"]["query"] for c in definitions if c["name"] == name)]
        )
        ddl["without_rowid_tables"] = sum(
            "WITHOUT ROWID" in (sql or "").upper()
            for (sql,) in db.execute("SELECT sql FROM sqlite_master WHERE type='table'")
        )
        report["ddl_compared_with_0_22_34_real_shape_fixture"] = ddl

        formats = {}

        def count(kind: str, cls: str, amount: int = 1):
            formats.setdefault(kind, Counter())[cls] += amount

        multi_lengths = Counter()
        by_id = {c["id"]: c["name"] for c in definitions}
        custom_ids, foreign, undeclared = {}, {}, {}

        def exists(collection_id: str, record: str) -> bool:
            name = by_id.get(collection_id)
            query = f"SELECT 1 FROM {quote(name)} WHERE id=? LIMIT 1" if name else None
            return bool(query and db.execute(query, (record,)).fetchone())

        def reference(path: str, field: dict, item: str):
            # Relation values that are not PocketBase-generated IDs, by shape.
            present = exists(field["options"]["collectionId"], item)
            target = "target_present" if present else "target_absent"
            foreign.setdefault(path, Counter())[f"{id_shape(item)}:{target}"] += 1

        for collection in definitions:
            if collection["type"] == "view":
                continue
            table = quote(collection["name"])
            for rowid_type, amount in db.execute(
                f"SELECT typeof(_rowid_), COUNT(*) FROM {table} GROUP BY 1"
            ):
                count("rowid", rowid_type, amount)
            for (value,) in db.execute(f"SELECT id FROM {table}"):
                if PB_ID.fullmatch(value):
                    count("id", "pb_id_15")
                else:
                    count("id", "other")
                    custom_ids.setdefault(collection["name"], Counter())[id_shape(value)] += 1
            for column in ("created", "updated"):
                for (value,) in db.execute(f"SELECT {column} FROM {table}"):
                    count("created_updated", _classify_date(value))
            if collection["type"] == "auth":
                rows = db.execute(
                    f"SELECT passwordHash,tokenKey,emailVisibility,verified,username,email,"
                    f"{','.join(AUTH_DATES)} FROM {table}"
                )
                for password_hash, token_key, visibility, verified, username, email, *dates in rows:
                    count(
                        "auth_password_hash",
                        "bcrypt_2a_12" if BCRYPT.fullmatch(password_hash) else "other",
                    )
                    count("auth_token_key", "alnum_50" if ALNUM50.fullmatch(token_key) else "other")
                    for value in (visibility, verified):
                        count(
                            "auth_bool",
                            "integer_0_1"
                            if value in (0, 1) and isinstance(value, int)
                            else "other",
                        )
                    count("auth_username", "non_empty" if username else "empty")
                    count("auth_email", "non_empty" if email else "empty")
                    for value in dates:
                        count("auth_legacy_dates", _classify_date(value))
            for field in collection["schema"]:
                column, kind = quote(field["name"]), field["type"]
                values = db.execute(f"SELECT {column} FROM {table}")
                if kind == "bool":
                    for (value,) in values:
                        ok = isinstance(value, int) and value in (0, 1)
                        count("bool", "integer_0_1" if ok else "other")
                elif kind == "number":
                    for (value,) in values:
                        count("number", {int: "integer", float: "real"}.get(type(value), "other"))
                elif kind == "date":
                    for (value,) in values:
                        count("date", _classify_date(value))
                elif kind == "json":
                    for (value,) in values:
                        if value is None:
                            count("json", "sql_null")
                            continue
                        try:
                            parsed = json.loads(value)
                        except ValueError:
                            count("json", "invalid")
                            continue
                        count("json", "json_" + type(parsed).__name__.replace("NoneType", "null"))
                elif is_multiple(field):
                    for (value,) in values:
                        try:
                            parsed = json.loads(value)
                        except (TypeError, ValueError):
                            count("multiple_" + kind, "not_json")
                            continue
                        if not isinstance(parsed, list) or not all(
                            isinstance(i, str) for i in parsed
                        ):
                            count("multiple_" + kind, "not_string_array")
                            continue
                        compact = go_compact_json(parsed) == value
                        count(
                            "multiple_" + kind,
                            "go_compact_string_array" if compact else "other_json_layout",
                        )
                        multi_lengths[
                            f"{kind}:{min(len(parsed), 2)}{'+' if len(parsed) > 1 else ''}"
                        ] += 1
                        for item in parsed:
                            if kind == "relation":
                                ok = bool(PB_ID.fullmatch(item))
                                count("relation_items", "pb_id_15" if ok else "other")
                                if not ok:
                                    reference(f"{collection['name']}.{field['name']}", field, item)
                            elif kind == "file":
                                count(
                                    "file_names",
                                    "pb_0_22_pattern" if PB_FILE_NAME.fullmatch(item) else "other",
                                )
                else:
                    for (value,) in values:
                        if not isinstance(value, str):
                            count("single_" + kind, "not_text")
                        elif value == "":
                            count("single_" + kind, "empty")
                        elif kind == "relation":
                            ok = bool(PB_ID.fullmatch(value))
                            count("single_relation", "pb_id_15" if ok else "other")
                            if not ok:
                                reference(f"{collection['name']}.{field['name']}", field, value)
                        elif kind == "file":
                            count(
                                "file_names",
                                "pb_0_22_pattern" if PB_FILE_NAME.fullmatch(value) else "other",
                            )
                        elif kind == "select":
                            allowed = (field.get("options") or {}).get("values") or []
                            declared = value in allowed
                            count("single_select", "declared_value" if declared else "other")
                            if not declared:
                                path = f"{collection['name']}.{field['name']}"
                                undeclared.setdefault(path, set()).add(value)
                                count("undeclared_select_rows:" + path, "rows")
                        else:
                            count("single_" + kind, "text_with_nul" if "\x00" in value else "text")
        selects = {
            key.split(":", 1)[1]: formats.pop(key)["rows"]
            for key in [k for k in formats if k.startswith("undeclared_select_rows:")]
        }
        report["value_formats"] = {key: _counter(value) for key, value in sorted(formats.items())}
        report["multiple_value_lengths"] = _counter(multi_lengths)
        report["shape_inventory"] = {
            "custom_record_ids": {k: _counter(v) for k, v in sorted(custom_ids.items())},
            "relation_values_not_pb_ids": {k: _counter(v) for k, v in sorted(foreign.items())},
            "undeclared_select_values": {
                path: {"rows": selects[path], "distinct_values": len(values)}
                for path, values in sorted(undeclared.items())
            },
        }

        admins = db.execute(
            "SELECT id,avatar,email,tokenKey,passwordHash,lastResetSentAt,created,updated "
            "FROM _admins"
        ).fetchall()
        admin_formats = Counter()
        for admin_id, avatar, email, token_key, password_hash, reset, created, updated in admins:
            admin_formats["id_pb_id_15"] += bool(PB_ID.fullmatch(admin_id))
            admin_formats["avatar_integer"] += isinstance(avatar, int)
            admin_formats["email_non_empty"] += bool(email)
            admin_formats["token_key_alnum_50"] += bool(ALNUM50.fullmatch(token_key))
            admin_formats["password_hash_bcrypt_2a_12"] += bool(BCRYPT.fullmatch(password_hash))
            admin_formats["last_reset_" + _classify_date(reset)] += 1
            admin_formats["created_updated_pb_datetime"] += all(
                _classify_date(v) == "pb_datetime" for v in (created, updated)
            )
        report["admins"] = {
            "rows": len(admins),
            "columns_identical_to_fixture": _columns(db, "_admins") == _columns(fx, "_admins"),
            "formats": _counter(admin_formats),
        }

        def settings_of(source):
            return json.loads(
                source.execute("SELECT value FROM _params WHERE key='settings'").fetchone()[0]
            )

        real_settings, fixture_settings = settings_of(db), settings_of(fx)
        default_settings = settings_of(defaults_db)
        real_paths, fixture_paths = (
            paths_and_types(real_settings),
            paths_and_types(fixture_settings),
        )
        differing = []
        for path in sorted(real_paths):
            if path.rsplit("/", 1)[-1] in SECRET_KEYS:
                continue
            node_real, node_default = real_settings, default_settings
            for part in path.strip("/").split("/"):
                node_real, node_default = node_real[part], node_default.get(part)
            if node_real != node_default:
                differing.append(path)
        report["params"] = {
            "rows": db.execute("SELECT COUNT(*) FROM _params").fetchone()[0],
            "keys_equal_fixture": sorted(r[0] for r in db.execute("SELECT key FROM _params"))
            == sorted(r[0] for r in fx.execute("SELECT key FROM _params")),
            "settings_plain_json_object": isinstance(real_settings, dict),
            "settings_key_paths": len(real_paths),
            "settings_key_paths_and_types_equal_fixture": real_paths == fixture_paths,
            "settings_key_order_equal_fixture": list(real_settings) == list(fixture_settings),
            "token_secrets_alnum_50": sum(
                bool(ALNUM50.fullmatch(real_settings[name]["secret"])) for name in TOKEN_CONFIGS
            ),
            "token_durations_equal_pocketbase_defaults": sum(
                real_settings[name]["duration"] == default_settings[name]["duration"]
                for name in TOKEN_CONFIGS
            ),
            "token_configs": len(TOKEN_CONFIGS),
            "app_name_equals_pocketbase_default": real_settings["meta"]["appName"]
            == default_settings["meta"]["appName"],
            "non_secret_paths_differing_from_pocketbase_defaults": differing,
            "smtp_s3_backups_s3_enabled": [
                real_settings["smtp"]["enabled"],
                real_settings["s3"]["enabled"],
                real_settings["backups"]["s3"]["enabled"],
            ],
            "oauth2_providers_enabled": sum(
                1
                for key, value in real_settings.items()
                if key.endswith("Auth") and key != "emailAuth" and value.get("enabled")
            ),
        }
        real_migrations = db.execute("SELECT file, applied FROM _migrations").fetchall()
        system = {r[0] for r in fx.execute("SELECT file FROM _migrations") if r[0].endswith(".go")}
        report["migrations"] = {
            "rows": len(real_migrations),
            "pocketbase_system_migrations_equal_fixture": {
                f for f, _ in real_migrations if f.endswith(".go")
            }
            == system,
            "pocketbase_system_migrations": len(system),
            "application_migrations": sum(1 for f, _ in real_migrations if not f.endswith(".go")),
            "applied_unix_microseconds": sum(
                isinstance(a, int) and 10**15 <= a < 10**17 for _, a in real_migrations
            ),
        }
        report["external_auths"] = db.execute("SELECT COUNT(*) FROM _externalAuths").fetchone()[0]
        report["collections_rows"] = db.execute("SELECT COUNT(*) FROM _collections").fetchone()[0]
        manifest_path = data / "c11-extraction-manifest.json"
        if manifest_path.is_file():
            # Private manifest of the archive members: paths are classified, never output.
            manifest = json.loads(manifest_path.read_text())
            shapes, directories = Counter(), Counter()
            names = {entry["path"] for entry in manifest["files"]}
            for entry in manifest["files"]:
                parts = entry["path"].split("/")
                if parts[0] != "storage":
                    shapes["outside_storage"] += 1
                    continue
                parts = parts[1:]
                if len(parts) < 3:
                    shapes["other_depth"] += 1
                    continue
                collection_id, record = parts[0], parts[1]
                directories[
                    ("known_collection" if collection_id in by_id else "unknown_collection")
                    + (":pb_id" if PB_ID.fullmatch(record) else ":custom_id_" + id_shape(record))
                    + (":record_present" if exists(collection_id, record) else ":record_absent")
                ] += 1
                leaf = parts[-1]
                core = leaf.removesuffix(".attrs")
                if len(parts) == 3 and leaf.endswith(".attrs"):
                    shapes["attrs_sidecar"] += 1
                    original = "/".join(["storage", collection_id, record, core])
                    shapes["attrs_with_original" if original in names else "attrs_alone"] += 1
                elif len(parts) == 3:
                    shapes["original"] += 1
                    pb_name = PB_FILE_NAME.fullmatch(core)
                    shapes["original_pb_0_22_name" if pb_name else "original_other_name"] += 1
                elif len(parts) == 4 and parts[2].startswith("thumbs_"):
                    size, _, thumb_of = core.partition("_")
                    named = thumb_of == parts[2].removeprefix("thumbs_") and THUMB_SIZE.fullmatch(
                        size
                    )
                    kind = "thumb_attrs_sidecar" if leaf.endswith(".attrs") else "thumbnail"
                    shapes[kind + ("" if named else "_unexpected_name")] += 1
                else:
                    shapes["other_depth"] += 1
            report["storage_manifest"] = {
                "objects": _counter(shapes),
                "record_directories": _counter(directories),
            }
    report["data_db_unchanged"] = digest(db_path) == before
    return report


def assess_real(real: dict) -> dict:
    """Formats the importer must accept, beside the measured D-0019/D-0021 data."""
    checks = {
        "integrity_ok": real["integrity_check"] == "ok",
        "same_sqlite_header_format": real["header"]["schema_format"] == 4
        and real["header"]["text_encoding"] == "UTF-8",
        "no_without_rowid_table": real["ddl_compared_with_0_22_34_real_shape_fixture"][
            "without_rowid_tables"
        ]
        == 0,
        "rowids_integer": set(real["value_formats"]["rowid"]) == {"integer"},
        "dates_pocketbase_layout_or_empty": all(
            set(real["value_formats"].get(k, {})) <= {"empty", "pb_datetime"}
            for k in ("date", "created_updated", "auth_legacy_dates")
        ),
        "bools_integer_0_1": set(real["value_formats"]["bool"]) == {"integer_0_1"},
        "numbers_integer_or_real": set(real["value_formats"]["number"]) <= {"integer", "real"},
        "json_valid_or_null": "invalid" not in real["value_formats"]["json"],
        "multiple_values_go_json_string_arrays": all(
            set(v) == {"go_compact_string_array"}
            for k, v in real["value_formats"].items()
            if k.startswith("multiple_")
        ),
        "auth_bcrypt_2a_12": set(real["value_formats"]["auth_password_hash"]) == {"bcrypt_2a_12"},
        "auth_token_keys_alnum_50": set(real["value_formats"]["auth_token_key"]) == {"alnum_50"},
        "admins_formats": real["admins"]["formats"].get("password_hash_bcrypt_2a_12")
        == real["admins"]["rows"]
        and real["admins"]["formats"].get("token_key_alnum_50") == real["admins"]["rows"],
        "settings_structure_equal_fixture": real["params"][
            "settings_key_paths_and_types_equal_fixture"
        ],
        "system_migrations_equal_fixture": real["migrations"][
            "pocketbase_system_migrations_equal_fixture"
        ],
        "source_unchanged": real["data_db_unchanged"],
    }
    return {"checks": checks, "all_match": all(checks.values())}


def run_real_formats(
    script: Path, data: Path, schema: Path, defaults: Path, real_shape: Path, work: Path
) -> dict:
    """Run the aggregation in an isolated interpreter, outside the backup folder."""
    completed = subprocess.run(
        [
            sys.executable,
            "-I",
            str(script),
            "real-formats",
            "--data",
            str(data),
            "--schema",
            str(schema),
            "--fixture-default",
            str(defaults),
            "--fixture-real-shape",
            str(real_shape),
        ],
        cwd=work,
        capture_output=True,
        text=True,
    )
    if completed.returncode:
        # Never echo the child output: it could quote data in a traceback.
        raise SystemExit("Real backup format aggregation failed")
    return json.loads(completed.stdout)


VERSION_GATE_BLOCKER = "source_pocketbase_version_unvalidated"


def only_version_gate_blocks(gate: dict) -> bool:
    """True when 0.22.34 is refused by nothing but the importer version gate.

    The check must survive the gate itself: once scripts/c11_import.py accepts
    0.22.34 (D-0024), its default fixture has no blocker at all, and a replay of
    this reference must not turn that into a failure that closes the gate again.
    """
    blockers_0_22_34 = gate["0.22.34"]["with_source_version"]["blocker_codes"]
    blockers_0_22_27 = gate["0.22.27"]["with_source_version"]["blocker_codes"]
    return set(blockers_0_22_34) <= {VERSION_GATE_BLOCKER} and blockers_0_22_27 == []


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    commands = parser.add_subparsers(dest="command")
    real = commands.add_parser("real-formats", help="internal: isolated aggregation")
    real.add_argument("--data", type=Path, required=True)
    real.add_argument("--schema", type=Path, required=True)
    real.add_argument("--fixture-default", type=Path, required=True)
    real.add_argument("--fixture-real-shape", type=Path, required=True)
    parser.add_argument("--pocketbase-0-22-27", type=Path, dest="pb_old")
    parser.add_argument("--pocketbase-0-22-34", type=Path, dest="pb_new")
    parser.add_argument("--pocketbase-src", type=Path)
    parser.add_argument("--schema", type=Path)
    parser.add_argument("--real-data", type=Path)
    parser.add_argument(
        "--baseline-commit", help="BUMA commit whose default fixture must stay identical"
    )
    parser.add_argument("--report", type=Path)
    args = parser.parse_args()
    if args.command == "real-formats":
        result = real_formats(args.data, args.schema, args.fixture_default, args.fixture_real_shape)
        print(json.dumps(result, sort_keys=True))
        return
    if not all((args.pb_old, args.pb_new, args.pocketbase_src, args.schema, args.report)):
        parser.error(
            "--pocketbase-0-22-27, --pocketbase-0-22-34, --pocketbase-src, "
            "--schema and --report are required"
        )
    if args.report.exists():
        parser.error("Report must be a new path")
    from scripts.c11_import import load_schema, write_json_new

    load_schema(args.schema)  # checksum of the corrected export
    report = {
        "timestamp_utc": datetime.now(UTC).isoformat(),
        "scope": "pocketbase_0_22_34_storage_reference",
        "command": "python -m scripts.c11_version_reference " + " ".join(sys.argv[1:]),
        "production_data_used": bool(args.real_data),
        "production_data_published": False,
        "status": "failed",
        "schema_sha256": digest(args.schema),
    }
    report["source"] = source_comparison(args.pocketbase_src)
    with tempfile.TemporaryDirectory(prefix="buma-c11-ws-d-") as folder:
        work = Path(folder)
        baseline = None
        if args.baseline_commit:
            source = work / "c11_fixture_baseline.py"
            source.write_text(
                subprocess.run(
                    ["git", "show", f"{args.baseline_commit}:scripts/c11_fixture.py"],
                    cwd=Path(__file__).resolve().parents[1],
                    capture_output=True,
                    text=True,
                    check=True,
                ).stdout
            )
            baseline = (args.baseline_commit, source)
        report["binaries"] = binary_comparison(
            {"0.22.27": args.pb_old.resolve(), "0.22.34": args.pb_new.resolve()},
            args.schema.resolve(),
            work,
            baseline,
        )
        if args.real_data:
            aggregates = run_real_formats(
                Path(__file__).resolve(),
                args.real_data.resolve(),
                args.schema.resolve(),
                work / "default-0.22.34" / "data" / "data.db",
                work / "real_shape-0.22.34" / "data" / "data.db",
                work,
            )
            report["real_backup_formats"] = aggregates | {"assessment": assess_real(aggregates)}
    binaries = report["binaries"]
    profiles = binaries["profiles"].values()
    gate = binaries["profiles"]["default"]["importer_diagnostic"]
    real = report.get("real_backup_formats")
    embedded = {v["embedded_sqlite_version"] for v in binaries["binaries"].values()}
    conclusion = {
        "storage_paths_identical_in_source": not report["source"]["storage_paths_changed"],
        "binary_storage_identical_except_random_values": all(
            p["storage_identical"] for p in profiles
        ),
        "random_values_same_formats": all(
            not any(p["unexpected_formats"].values()) for p in profiles
        ),
        "view_schemas_equal_export_for_both_binaries": all(
            v["equal_to_export_without_field_ids"] == v["views"] == 42
            for p in profiles
            for v in p["view_schemas"].values()
        ),
        "importer_reads_both_identically": all(
            not p["importer_diagnostic_differences"] for p in profiles
        ),
        "only_version_gate_blocks_0_22_34_default_fixture": only_version_gate_blocks(gate),
        "same_embedded_sqlite": len(embedded) == 1,
    }
    for key, value in binaries.items():
        if key.startswith("default_profile_vs_"):
            conclusion[key + "_identical"] = all(v["identical"] for v in value.values())
    if real:
        major, minor, patch = (int(part) for part in sorted(embedded)[0].split("."))
        conclusion["real_backup_last_writer_is_the_embedded_sqlite"] = (
            real["header"]["last_writer_sqlite_version"]
            == major * 1_000_000 + minor * 1_000 + patch
        )
        conclusion["real_backup_formats_match"] = real["assessment"]["all_match"]
    conclusion["importer_can_accept_0_22_34"] = bool(real) and all(conclusion.values())
    conclusion["statement"] = (
        STATEMENT if conclusion["importer_can_accept_0_22_34"] else "Not established: see checks."
    )
    report["conclusion"] = conclusion
    report["status"] = "passed"
    write_json_new(args.report, report)
    print(json.dumps({"status": report["status"], "report": str(args.report)}))


if __name__ == "__main__":
    main()
