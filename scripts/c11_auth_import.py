"""C11 accounts, settings and session continuity, PocketBase 0.22 -> PPBase.

Offline primitives for the isolated rehearsal only; there is no production
command. They read the closed, staged snapshot through
``scripts.c11_import.snapshot`` and write on the caller's SQLAlchemy connection,
in the same transaction as ``load_isolated``: any failure rolls records,
accounts and settings back together. No password is regenerated, no token is
minted, and no secret is logged or reported (reports say present, imported and
length only). The caller's engine must hide bound parameters
(``create_async_engine(..., hide_parameters=True)``) and database errors are
re-raised as a value-free ``ImportBlocked("auth_import_database_error")``.

The correspondence follows PocketBase's own 0.22 -> 0.23 upgrade
(v0.23.0 migrations/1717233556_v0.23_migrate.go) wherever PPBase can represent
the value. docs/contracts/c11-auth-import.md cites the sources and lists what
is deliberately not imported, with its client impact.

Read-only diagnosis of a closed backup (no database, aggregated report):
python -m scripts.c11_auth_import --data DIR --schema EXPORT --report NEW.json
"""

from __future__ import annotations

import argparse
import copy
import json
import re
import sqlite3
from pathlib import Path

from scripts.c11_import import (
    ImportBlocked,
    date_value,
    digest,
    load_schema,
    snapshot,
    strict_json,
    write_json_new,
)

# Same guard as load_isolated: never a supplied or long-lived database.
REHEARSAL_DATABASE = "buma_c11_rehearsal"
ARCHIVE_TABLE = "_buma_c11_auth_archive"
SUPERUSERS = "_superusers"
INTERNAL_TABLES = ("_admins", "_params", "_externalAuths")
# PocketBase v0.22.34 models/admin.go:15-23 and models/param.go:13-18.
ADMIN_COLUMNS = frozenset(
    {"id", "avatar", "email", "tokenKey", "passwordHash", "lastResetSentAt", "created", "updated"}
)
PARAM_COLUMNS = frozenset({"id", "key", "value", "created", "updated"})
# PPBase ppbase/db/system_tables.py:96-132 (_superusers) column limits.
ADMIN_TEXT_LIMITS = {"id": 15, "email": 255, "tokenKey": 255, "passwordHash": 255}
INT32 = (-(2**31), 2**31 - 1)
# Archived without activation: PPBase has the column but no reader, and the only
# PocketBase 0.22 reader is the /api/admins/request-password-reset throttle.
ADMIN_ARCHIVED = ("lastResetSentAt",)
# Copied byte for byte (PocketBase's own upgrade: migrate.go:115-118, plus avatar,
# which PPBase still serialises in /api/admins exactly like PocketBase 0.22).
ADMIN_COPIED = ("id", "avatar", "email", "passwordHash", "tokenKey", "created", "updated")

# PPBase per-collection option -> PocketBase 0.22 global settings key, exactly as
# v0.23.0 migrations/1717233556_v0.23_migrate.go:86-109 (superusers) and
# :338-357 (every auth collection) move them.
RECORD_TOKENS = {
    "authToken": "recordAuthToken",
    "passwordResetToken": "recordPasswordResetToken",
    "emailChangeToken": "recordEmailChangeToken",
    "verificationToken": "recordVerificationToken",
    "fileToken": "recordFileToken",
}
SUPERUSER_TOKENS = {
    "authToken": "adminAuthToken",
    "passwordResetToken": "adminPasswordResetToken",
    "fileToken": "adminFileToken",
}
TOKEN_KEYS = (*SUPERUSER_TOKENS.values(), *RECORD_TOKENS.values())
# Values PocketBase itself accepts on save (models/settings/settings.go:379-384).
SECRET_LENGTH = (30, 300)
DURATION_RANGE = (5, 63072000)

# (PocketBase 0.22 path, PPBase _params.settings path, JSON type), as
# v0.23.0 migrate.go:192-209 maps meta, logs and smtp.
SETTINGS_MAP = (
    (("meta", "appName"), ("meta", "appName"), str),
    (("meta", "appUrl"), ("meta", "appURL"), str),
    (("meta", "hideControls"), ("meta", "hideControls"), bool),
    (("meta", "senderName"), ("meta", "senderName"), str),
    (("meta", "senderAddress"), ("meta", "senderAddress"), str),
    (("logs", "maxDays"), ("logs", "maxDays"), int),
    (("logs", "minLevel"), ("logs", "minLevel"), int),
    (("logs", "logIp"), ("logs", "logIP"), bool),
    (("smtp", "enabled"), ("smtp", "enabled"), bool),
    (("smtp", "host"), ("smtp", "host"), str),
    (("smtp", "port"), ("smtp", "port"), int),
    (("smtp", "username"), ("smtp", "username"), str),
    (("smtp", "password"), ("smtp", "password"), str),
    (("smtp", "authMethod"), ("smtp", "authMethod"), str),
    (("smtp", "tls"), ("smtp", "tls"), bool),
    (("smtp", "localName"), ("smtp", "localName"), str),
)
SECRET_SETTINGS = frozenset({("smtp", "password")})
# Global 0.22 templates copied into each record auth collection (migrate.go:441-463).
TEMPLATES = ("verificationTemplate", "resetPasswordTemplate", "confirmEmailChangeTemplate")
OAUTH2_PROVIDERS = (
    "googleAuth",
    "facebookAuth",
    "githubAuth",
    "gitlabAuth",
    "discordAuth",
    "twitterAuth",
    "microsoftAuth",
    "spotifyAuth",
    "kakaoAuth",
    "twitchAuth",
    "stravaAuth",
    "giteeAuth",
    "livechatAuth",
    "giteaAuth",
    "oidcAuth",
    "oidc2Auth",
    "oidc3Auth",
    "appleAuth",
    "instagramAuth",
    "vkAuth",
    "yandexAuth",
    "patreonAuth",
    "mailcowAuth",
    "bitbucketAuth",
    "planningcenterAuth",
)
# Present in PocketBase 0.22 settings, deliberately left out of PPBase.
NOT_IMPORTED = {
    "s3": "ppbase_applies_s3_to_its_storage_runtime_requires_decision",
    "backups": "ppbase_backup_cron_would_start_a_different_backup_mechanism",
    "emailAuth": "deprecated_ignored_by_pocketbase_0_22",
    **{name: "oauth2_provider_not_enabled" for name in OAUTH2_PROVIDERS},
}
# Secrets redacted by PocketBase (models/settings/settings.go:281-334) that stay out.
NOT_IMPORTED_SECRETS = (
    ("s3", "secret"),
    ("backups", "s3", "secret"),
    *((name, "clientSecret") for name in OAUTH2_PROVIDERS),
)
KNOWN_SETTINGS = frozenset({"meta", "logs", "smtp", *TOKEN_KEYS, *NOT_IMPORTED})
FAIL_STAGES = ("admins", "options", "settings")
DATABASE_ERROR = "auth_import_database_error"
SQLSTATE = re.compile(r"^[0-9A-Z]{5}$")
# Same fingerprint as scripts.c11_import.diagnose ("rowid:id\n" in rowid order),
# computed by PostgreSQL over the rows load_isolated archived.
RECORD_IDENTITIES = (
    "SELECT collection_id, COUNT(*) AS row_count, encode(sha256(convert_to(string_agg("
    "source_rowid::text || ':' || record_id || E'\\n', '' ORDER BY source_rowid), 'UTF8')), "
    "'hex') AS identity FROM \"_buma_c11_rowids\" GROUP BY collection_id"
)


def _rows(db: sqlite3.Connection, sql: str) -> list[dict]:
    cursor = db.execute(sql)
    names = [column[0] for column in cursor.description]
    return [dict(zip(names, row, strict=True)) for row in cursor]


def _get(mapping, path):
    current = mapping
    for key in path:
        if not isinstance(current, dict) or key not in current:
            return False, None
        current = current[key]
    return True, current


def _set(mapping: dict, path, value) -> None:
    for key in path[:-1]:
        mapping = mapping.setdefault(key, {})
    mapping[path[-1]] = value


def _typed(value, kind) -> bool:
    if kind is bool:
        return isinstance(value, bool)
    if kind is int:
        return isinstance(value, int) and not isinstance(value, bool)
    return isinstance(value, kind)


def _text_problem(value) -> str | None:
    """Explicit code for a string PostgreSQL would reject with a raw driver error.

    text, varchar and jsonb refuse NUL (as load_isolated's nul_in_postgresql_text);
    a lone surrogate decoded from a JSON escape cannot be encoded to UTF-8.
    """
    if not isinstance(value, str):
        return None
    if "\x00" in value:
        return "nul_in_postgresql_text"
    try:
        value.encode("utf-8")
    except UnicodeEncodeError:
        return "text_not_utf8_encodable"
    return None


def read_source(db: sqlite3.Connection) -> dict:
    """Read PocketBase 0.22 internal auth tables from an open read-only snapshot."""
    tables = {row[0] for row in db.execute("SELECT name FROM sqlite_master WHERE type='table'")}
    source = {
        "tables": tables,
        "admin_columns": [],
        "admins": [],
        "param_columns": [],
        "params": [],
        "external_auths": 0,
    }
    if "_admins" in tables:
        source["admin_columns"] = [row[1] for row in db.execute('PRAGMA table_info("_admins")')]
        source["admins"] = _rows(
            db, 'SELECT _rowid_ AS __c11_rowid, * FROM "_admins" ORDER BY _rowid_'
        )
    if "_params" in tables:
        source["param_columns"] = [row[1] for row in db.execute('PRAGMA table_info("_params")')]
        source["params"] = _rows(
            db, 'SELECT _rowid_ AS __c11_rowid, * FROM "_params" ORDER BY _rowid_'
        )
    if "_externalAuths" in tables:
        source["external_auths"] = db.execute('SELECT COUNT(*) FROM "_externalAuths"').fetchone()[0]
    return source


def parse_settings(params: list[dict]) -> tuple[dict | None, str | None]:
    """Decode _params.settings like daos/settings.go:19-56, without decryption."""
    rows = [row for row in params if row.get("key") == "settings"]
    if not rows:
        return None, "settings_missing"
    raw = rows[0].get("value")
    if isinstance(raw, bytes):
        try:
            raw = raw.decode("utf-8")
        except UnicodeDecodeError:
            return None, "settings_not_plain_json"
    try:
        parsed = strict_json(raw)
    except (ImportBlocked, TypeError, ValueError):
        # PocketBase encrypts settings only with --encryptionEnv; no key is accepted.
        return None, "settings_not_plain_json"
    if not isinstance(parsed, dict):
        return None, "settings_not_plain_json"
    return parsed, None


def check_source(source: dict, definitions: list[dict]) -> tuple[list[dict], list[dict]]:
    """Blockers stop the import; warnings are reported. Never values, only paths."""
    blockers, warnings = [], []

    def block(code, **extra):
        blockers.append({"code": code, **extra})

    for table in INTERNAL_TABLES:
        if table not in source["tables"]:
            block("missing_internal_table", table=table)
    if blockers:
        return blockers, warnings
    if not any(c["type"] == "auth" for c in definitions):
        block("source_auth_collections_missing")
    if set(source["admin_columns"]) != ADMIN_COLUMNS:
        block("unmapped_admin_columns")
    else:
        emails, token_keys = set(), set()
        for admin in source["admins"]:
            for field, limit in ADMIN_TEXT_LIMITS.items():
                value = admin[field]
                if not isinstance(value, str) or not value or len(value) > limit:
                    block("admin_value_not_representable", field=field)
                elif problem := _text_problem(value):
                    block(problem, field=field)
            avatar = admin["avatar"]
            if not _typed(avatar, int) or not INT32[0] <= avatar <= INT32[1]:
                block("admin_value_not_representable", field="avatar")
            if not isinstance(admin["lastResetSentAt"], str):
                block("admin_value_not_representable", field="lastResetSentAt")
            elif problem := _text_problem(admin["lastResetSentAt"]):
                # Written into the archive's jsonb column.
                block(problem, field="lastResetSentAt")
            for field in ("created", "updated"):
                try:
                    date_value(admin[field], required=True)
                except ImportBlocked:
                    block("admin_value_not_representable", field=field)
            if admin["email"] in emails:
                block("duplicate_admin_email")
            if admin["tokenKey"] in token_keys:
                block("duplicate_admin_token_key")
            emails.add(admin["email"])
            token_keys.add(admin["tokenKey"])
    if set(source["param_columns"]) != PARAM_COLUMNS:
        block("unmapped_param_columns")
        return blockers, warnings
    unknown = [row for row in source["params"] if row.get("key") != "settings"]
    if unknown:
        block("unknown_param_keys", rows=len(unknown))
    if source["external_auths"]:
        # Empty in the BUMA backup; mapping OAuth links needs its own study.
        block("external_auths_present", rows=source["external_auths"])
    settings, code = parse_settings(source["params"])
    if code:
        block(code)
        return blockers, warnings
    row = next(row for row in source["params"] if row.get("key") == "settings")
    if not isinstance(row.get("id"), str) or not 0 < len(row["id"]) <= 15:
        block("param_value_not_representable", field="id")
    elif problem := _text_problem(row["id"]):
        block(problem, field="id")
    for field in ("created", "updated"):
        try:
            date_value(row.get(field), required=True)
        except ImportBlocked:
            block("param_value_not_representable", field=field)
    for key in TOKEN_KEYS:
        present, config = _get(settings, (key,))
        if not present:
            warnings.append({"code": "token_config_absent_target_default_kept", "setting": key})
            continue
        if not isinstance(config, dict):
            block("invalid_token_config", setting=key)
            continue
        secret, duration = config.get("secret", ""), config.get("duration", 0)
        if not isinstance(secret, str) or not _typed(duration, int) or duration < 0:
            block("invalid_token_config", setting=key)
            continue
        if problem := _text_problem(secret):
            block(problem, setting=key)
        if not secret or not duration:
            warnings.append({"code": "token_config_absent_target_default_kept", "setting": key})
        if (secret and not SECRET_LENGTH[0] <= len(secret) <= SECRET_LENGTH[1]) or (
            duration and not DURATION_RANGE[0] <= duration <= DURATION_RANGE[1]
        ):
            warnings.append({"code": "token_config_outside_pocketbase_limits", "setting": key})
    for path, _target, kind in SETTINGS_MAP:
        present, value = _get(settings, path)
        if present and not _typed(value, kind):
            block("invalid_settings_value", path=".".join(path))
        elif present and (problem := _text_problem(value)):
            block(problem, path=".".join(path))
    for name in TEMPLATES:
        present, template = _get(settings, ("meta", name))
        if not present:
            continue
        if not isinstance(template, dict) or any(
            not isinstance(template.get(part, ""), str) for part in ("subject", "body", "actionUrl")
        ):
            block("invalid_settings_value", path="meta." + name)
            continue
        for part in ("subject", "body", "actionUrl"):
            if problem := _text_problem(template.get(part, "")):
                block(problem, path=f"meta.{name}.{part}")
    if _get(settings, ("s3", "enabled"))[1] is True:
        # Files would live in S3 and PPBase would switch its storage runtime.
        block("s3_storage_enabled_requires_decision")
    if _get(settings, ("backups", "cron"))[1]:
        warnings.append({"code": "backup_schedule_not_imported"})
    if _get(settings, ("backups", "s3", "enabled"))[1] is True:
        warnings.append({"code": "backup_s3_target_not_imported"})
    for name in OAUTH2_PROVIDERS:
        if _get(settings, (name, "enabled"))[1] is True:
            block("oauth2_provider_enabled_requires_decision", provider=name)
    unknown_keys = set(settings) - KNOWN_SETTINGS
    if unknown_keys:
        warnings.append({"code": "unknown_settings_keys_ignored", "keys": len(unknown_keys)})
    return blockers, warnings


def _token_plan(settings: dict, mapping: dict[str, str]) -> dict:
    plan = {}
    for kind, key in mapping.items():
        _present, config = _get(settings, (key,))
        config = config if isinstance(config, dict) else {}
        secret, duration = config.get("secret") or None, config.get("duration") or None
        # zeroFallback (migrate.go:904-910): empty or zero keeps the target value.
        plan[kind] = {"source": key, "secret": secret, "duration": duration}
    return plan


def template_plan(settings: dict) -> dict:
    """migrate.go:441-463: {ACTION_URL} inlined in the body, zero values fall back."""
    plans = {}
    for name in TEMPLATES:
        present, template = _get(settings, ("meta", name))
        if not present:
            continue
        body = template.get("body", "").replace("{ACTION_URL}", template.get("actionUrl", ""))
        plans[name] = {"subject": template.get("subject", ""), "body": body}
    return plans


def settings_value(settings: dict) -> dict:
    """PPBase's default settings document with the representable 0.22 values."""
    from ppbase.api.settings import _DEFAULT_SETTINGS

    value = copy.deepcopy(_DEFAULT_SETTINGS)
    for path, target, _kind in SETTINGS_MAP:
        present, item = _get(settings, path)
        if not present:
            continue
        if target == ("meta", "appURL"):
            # strings.TrimSuffix(appUrl, "/") in migrate.go:193: one slash only.
            item = item[:-1] if item.endswith("/") else item
        _set(value, target, item)
    return value


def build_plan(source: dict, definitions: list[dict]) -> dict:
    """Pure mapping of a checked source; raises on any blocker."""
    blockers, warnings = check_source(source, definitions)
    if blockers:
        raise ImportBlocked(blockers[0]["code"])
    settings, _code = parse_settings(source["params"])
    record_collections = sorted(c["name"] for c in definitions if c["type"] == "auth")
    tokens = {name: _token_plan(settings, RECORD_TOKENS) for name in record_collections}
    tokens[SUPERUSERS] = _token_plan(settings, SUPERUSER_TOKENS)
    templates = template_plan(settings)
    param = next(row for row in source["params"] if row.get("key") == "settings")
    admins = [
        {
            "row": {
                "id": admin["id"],
                "avatar": admin["avatar"],
                "email": admin["email"],
                "token_key": admin["tokenKey"],
                "password_hash": admin["passwordHash"],
                "created": date_value(admin["created"], required=True),
                "updated": date_value(admin["updated"], required=True),
            },
            "rowid": admin["__c11_rowid"],
            "legacy": {key: admin[key] for key in ADMIN_ARCHIVED},
        }
        for admin in source["admins"]
    ]
    return {
        "admins": admins,
        "tokens": tokens,
        "collection_ids": {c["name"]: c["id"] for c in definitions if c["type"] == "auth"},
        "templates": {name: templates for name in record_collections},
        "settings": settings,
        "settings_row": {
            "id": param["id"],
            "rowid": param["__c11_rowid"],
            "created": date_value(param["created"], required=True),
            "updated": date_value(param["updated"], required=True),
            "value": settings_value(settings),
        },
        "external_auths": source["external_auths"],
        "warnings": warnings,
    }


def merged_options(current: dict, tokens: dict, templates: dict | None = None) -> dict:
    """Overlay imported token configs/templates on the target's existing options."""
    options = copy.deepcopy(current or {})
    for kind, config in tokens.items():
        target = dict(options.get(kind) or {})
        if config["secret"] is not None:
            target["secret"] = config["secret"]
        if config["duration"] is not None:
            target["duration"] = config["duration"]
        options[kind] = target
    for name, template in (templates or {}).items():
        target = dict(options.get(name) or {})
        for part in ("subject", "body"):
            if template[part]:
                target[part] = template[part]
        options[name] = target
    return options


def _secret(value, imported: bool) -> dict:
    present = isinstance(value, str) and bool(value)
    return {
        "present": present,
        "imported": present and imported,
        "length": len(value) if present else 0,
    }


def public_report(plan: dict, *, imported: bool) -> dict:
    """Aggregated, value-free report: presence, import status and secret length."""
    settings = plan["settings"]
    tokens = {
        collection: {
            kind: {
                "source": config["source"],
                "secret": _secret(config["secret"], imported),
                "duration": {
                    "present": config["duration"] is not None,
                    "imported": imported and config["duration"] is not None,
                },
            }
            for kind, config in kinds.items()
        }
        for collection, kinds in plan["tokens"].items()
    }
    mapped = {}
    for path, target, _kind in SETTINGS_MAP:
        present, value = _get(settings, path)
        entry = {"target": ".".join(target), "present": present, "imported": present and imported}
        if path in SECRET_SETTINGS:
            entry = {**entry, **_secret(value, imported), "target": ".".join(target)}
        mapped[".".join(path)] = entry
    not_imported = {key: reason for key, reason in NOT_IMPORTED.items() if key in settings}
    secrets_left = {
        ".".join(path): {"present": _secret(_get(settings, path)[1], False)["present"]}
        for path in NOT_IMPORTED_SECRETS
        if _get(settings, path)[0]
    }
    templates = sorted({name for value in plan["templates"].values() for name in value})
    return {
        "scope": "c11_auth_settings_session_import",
        "production_authorized": False,
        "imported": imported,
        "admins": {
            "source_rows": len(plan["admins"]),
            "imported_rows": len(plan["admins"]) if imported else 0,
            "target": SUPERUSERS,
            "copied_unchanged": list(ADMIN_COPIED),
            "archived_without_activation": list(ADMIN_ARCHIVED),
            "passwords_regenerated": 0,
        },
        "token_configs": tokens,
        "settings": {
            "mapped": mapped,
            "templates_copied_to_record_auth_collections": templates,
            "not_imported": not_imported,
            "not_imported_secrets": secrets_left,
            "ppbase_only_keys_left_at_ppbase_defaults": True,
        },
        "external_auths": {"source_rows": plan["external_auths"], "imported_rows": 0},
        "logs_db": "never_imported",
        "warnings": plan["warnings"],
    }


def diagnose_auth(db: sqlite3.Connection, definitions: list[dict]) -> dict:
    """Read-only structure diagnosis; reports key names and validity, no values."""
    source = read_source(db)
    blockers, warnings = check_source(source, definitions)
    settings, _code = parse_settings(source["params"])
    configs = {}
    for key in TOKEN_KEYS:
        present, config = _get(settings or {}, (key,))
        config = config if isinstance(config, dict) else {}
        secret, duration = config.get("secret"), config.get("duration")
        configs[key] = {
            "present": present,
            "secret_present": isinstance(secret, str) and bool(secret),
            "secret_length_within_pocketbase_limits": isinstance(secret, str)
            and SECRET_LENGTH[0] <= len(secret) <= SECRET_LENGTH[1],
            "duration_within_pocketbase_limits": _typed(duration, int)
            and DURATION_RANGE[0] <= duration <= DURATION_RANGE[1],
        }
    return {
        "mode": "offline_read_only_auth_structure",
        "production_authorized": False,
        "admins": {
            "rows": len(source["admins"]),
            "columns_match_pocketbase_0_22": set(source["admin_columns"]) == ADMIN_COLUMNS,
        },
        "params": {
            "rows": len(source["params"]),
            "settings_plain_json_object": settings is not None,
        },
        "external_auths_rows": source["external_auths"],
        "token_configs": configs,
        "mapped_settings_present": [
            ".".join(path) for path, _t, _k in SETTINGS_MAP if _get(settings or {}, path)[0]
        ],
        "not_imported_settings_present": sorted(
            key for key in NOT_IMPORTED if key in (settings or {})
        ),
        "blockers": blockers,
        "warnings": warnings,
        "ready_for_auth_import": not blockers,
    }


def hides_parameters(connection) -> bool:
    """True when the engine keeps bound values out of SQLAlchemy error messages.

    Without hide_parameters=True a DBAPIError repeats every bound value
    ("[parameters: ...]"): password hashes, tokenKeys and token secrets here,
    hashes and tokenKeys of every auth record in load_isolated.
    """
    engine = getattr(connection, "sync_engine", None)
    if engine is None:
        engine = getattr(connection, "engine", None)
    return getattr(engine, "hide_parameters", False) is True


async def import_auth(
    connection, data: Path, definitions: list[dict], *, fail_after: str | None = None
) -> dict:
    """Import admins, token secrets and settings on the caller's transaction.

    Call it after load_isolated, on the same connection and transaction, so a
    failure anywhere rolls everything back. Refuses a non-empty target, records
    that do not come from this snapshot and an engine that would echo bound
    values. Database errors become a value-free ImportBlocked(DATABASE_ERROR)
    whose only extra attribute is the SQLSTATE.
    """
    from sqlalchemy.exc import SQLAlchemyError

    if fail_after is not None and fail_after not in FAIL_STAGES:
        raise ValueError("unknown failure stage")
    if not hides_parameters(connection):
        raise ImportBlocked("engine_hide_parameters_required")
    manifest = json.loads((data / "manifest.json").read_text())
    diagnostic = manifest["diagnostic"]
    if not diagnostic["ready_for_isolated_rehearsal"]:
        raise ImportBlocked("diagnostic_blockers")
    if digest(data / "data.db") != diagnostic["data_db_sha256"]:
        raise ImportBlocked("staged_snapshot_changed")
    try:
        return await _import_on_connection(connection, data, definitions, diagnostic, fail_after)
    except SQLAlchemyError as exc:
        sqlstate = getattr(getattr(exc, "orig", None), "sqlstate", None)
        failure = ImportBlocked(DATABASE_ERROR)
        valid = isinstance(sqlstate, str) and SQLSTATE.match(sqlstate)
        failure.sqlstate = sqlstate if valid else None
    # Raised outside the handler: no __context__ keeps the driver error, its SQL
    # text, a PostgreSQL DETAIL ("Failing row contains ...") or bound values.
    raise failure


def expected_identities(diagnostic: dict) -> dict:
    """{collection id: (rows, identity sha256)} of the diagnosed snapshot."""
    collections = diagnostic.get("collections")
    if not isinstance(collections, list):
        raise ImportBlocked("records_snapshot_unverifiable")
    return {c["id"]: (c["rows"], c["identity_sha256"]) for c in collections if c["rows"]}


async def _import_on_connection(connection, data, definitions, diagnostic, fail_after) -> dict:
    from sqlalchemy import text

    current_db = (await connection.execute(text("SELECT current_database()"))).scalar_one()
    if current_db != REHEARSAL_DATABASE:
        raise ImportBlocked("isolated_rehearsal_database_required")
    with snapshot(data) as db:
        source = read_source(db)
    plan = build_plan(source, definitions)
    identities = expected_identities(diagnostic)

    # Lock before the emptiness checks so a concurrent import cannot pass them too.
    await connection.execute(text('LOCK TABLE "_superusers", "_params" IN ACCESS EXCLUSIVE MODE'))
    if (await connection.execute(text('SELECT COUNT(*) FROM "_superusers"'))).scalar_one():
        raise ImportBlocked("superusers_target_not_empty")
    settings_rows = text('SELECT COUNT(*) FROM "_params" WHERE "key" = \'settings\'')
    if (await connection.execute(settings_rows)).scalar_one():
        raise ImportBlocked("settings_target_not_empty")
    archive = await connection.execute(text("SELECT to_regclass(:name)"), {"name": ARCHIVE_TABLE})
    if archive.scalar_one() is not None:
        raise ImportBlocked("auth_archive_exists")
    # Accounts without their records would be a partial import: load_isolated
    # must have run in this transaction (or committed before it).
    records = await connection.execute(text("SELECT to_regclass('_buma_c11_rowids')"))
    if records.scalar_one() is None:
        raise ImportBlocked("records_import_required")
    # ... and from this very snapshot: same per-collection rowid/id fingerprint
    # as the manifest, so accounts never join records of another backup.
    await connection.execute(text('LOCK TABLE "_buma_c11_rowids" IN SHARE MODE'))
    loaded = {
        row["collection_id"]: (row["row_count"], row["identity"])
        for row in (await connection.execute(text(RECORD_IDENTITIES))).mappings().all()
    }
    if loaded != identities:
        raise ImportBlocked("records_snapshot_mismatch")
    rows = (
        (
            await connection.execute(
                text(
                    'SELECT "id", "name", "options" FROM "_collections" '
                    'WHERE "type" = \'auth\' ORDER BY "name" FOR UPDATE'
                )
            )
        )
        .mappings()
        .all()
    )
    targets = {row["name"]: dict(row) for row in rows}
    if set(targets) != set(plan["tokens"]):
        raise ImportBlocked("target_auth_collections_mismatch")
    for name, collection_id in plan["collection_ids"].items():
        # Record tokens carry collectionId: PPBase must keep the source IDs.
        if targets[name]["id"] != collection_id:
            raise ImportBlocked("target_auth_collection_id_mismatch")

    await connection.execute(
        text(
            f'CREATE TABLE "{ARCHIVE_TABLE}" ('
            "source_table text NOT NULL, record_id text NOT NULL, "
            "source_rowid bigint NOT NULL, legacy jsonb NOT NULL, "
            "PRIMARY KEY (source_table, record_id), UNIQUE (source_table, source_rowid))"
        )
    )
    insert_archive = text(
        f'INSERT INTO "{ARCHIVE_TABLE}" VALUES (:table, :id, :rowid, CAST(:legacy AS jsonb))'
    )
    for admin in plan["admins"]:
        # last_reset_sent_at stays NULL: archived only, never activated.
        await connection.execute(
            text(
                'INSERT INTO "_superusers" ("id", "avatar", "email", "token_key", '
                '"password_hash", "last_reset_sent_at", "created", "updated") VALUES '
                "(:id, :avatar, :email, :token_key, :password_hash, NULL, :created, :updated)"
            ),
            admin["row"],
        )
        await connection.execute(
            insert_archive,
            {
                "table": "_admins",
                "id": admin["row"]["id"],
                "rowid": admin["rowid"],
                "legacy": json.dumps(admin["legacy"], ensure_ascii=False),
            },
        )
    readback = (
        (
            await connection.execute(
                text(
                    'SELECT "id", "avatar", "email", "token_key", "password_hash", '
                    '"last_reset_sent_at", "created", "updated" FROM "_superusers"'
                )
            )
        )
        .mappings()
        .all()
    )
    expected = {
        admin["row"]["id"]: {**admin["row"], "last_reset_sent_at": None} for admin in plan["admins"]
    }
    if {row["id"]: dict(row) for row in readback} != expected:
        raise ImportBlocked("superusers_readback_mismatch")
    if fail_after == "admins":
        raise ImportBlocked("injected_auth_import_failure")

    for name, tokens in plan["tokens"].items():
        current = targets[name]["options"]
        current = json.loads(current) if isinstance(current, str) else current
        options = merged_options(current, tokens, plan["templates"].get(name))
        await connection.execute(
            text(
                'UPDATE "_collections" SET "options" = CAST(:options AS jsonb), '
                '"updated" = now() WHERE "id" = :id'
            ),
            {"options": json.dumps(options, ensure_ascii=False), "id": targets[name]["id"]},
        )
        stored = (
            await connection.execute(
                text('SELECT "options" FROM "_collections" WHERE "id" = :id'),
                {"id": targets[name]["id"]},
            )
        ).scalar_one()
        stored = json.loads(stored) if isinstance(stored, str) else stored
        if stored != options:
            raise ImportBlocked("auth_options_readback_mismatch")
    if fail_after == "options":
        raise ImportBlocked("injected_auth_import_failure")

    row = plan["settings_row"]
    await connection.execute(
        text(
            'INSERT INTO "_params" ("id", "key", "value", "created", "updated") '
            "VALUES (:id, 'settings', CAST(:value AS jsonb), :created, :updated)"
        ),
        {
            "id": row["id"],
            "value": json.dumps(row["value"], ensure_ascii=False, allow_nan=False),
            "created": row["created"],
            "updated": row["updated"],
        },
    )
    await connection.execute(
        insert_archive,
        {
            "table": "_params",
            "id": row["id"],
            "rowid": row["rowid"],
            "legacy": json.dumps({"key": "settings"}),
        },
    )
    stored = (
        (
            await connection.execute(
                text(
                    'SELECT "id", "value", "created", "updated" FROM "_params" '
                    "WHERE \"key\" = 'settings'"
                )
            )
        )
        .mappings()
        .one()
    )
    value = stored["value"]
    value = json.loads(value) if isinstance(value, str) else value
    if (stored["id"], value, stored["created"], stored["updated"]) != (
        row["id"],
        row["value"],
        row["created"],
        row["updated"],
    ):
        raise ImportBlocked("settings_readback_mismatch")
    if fail_after == "settings":
        raise ImportBlocked("injected_auth_import_failure")
    report = public_report(plan, imported=True)
    report["records_snapshot"] = {
        "identity_verified": True,
        "collections": len(identities),
        "rows": sum(count for count, _identity in identities.values()),
    }
    return report


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--data", type=Path, required=True, help="Closed backup directory")
    parser.add_argument("--schema", type=Path, required=True)
    parser.add_argument("--report", type=Path, required=True, help="New private JSON report")
    args = parser.parse_args()
    try:
        definitions = load_schema(args.schema)
        with snapshot(args.data) as db:
            report = diagnose_auth(db, definitions)
    except (ImportBlocked, sqlite3.Error, OSError) as exc:
        report = {
            "ready_for_auth_import": False,
            "production_authorized": False,
            "error": str(exc) if isinstance(exc, ImportBlocked) else type(exc).__name__,
        }
    write_json_new(args.report, report)
    print(json.dumps({"report": str(args.report), "ready": report["ready_for_auth_import"]}))
    return 0 if report["ready_for_auth_import"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
