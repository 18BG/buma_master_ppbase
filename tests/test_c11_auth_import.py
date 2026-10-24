"""C11 accounts/settings/session import: mapping, refusals and token continuity."""

import copy
import json
import re
import sqlite3
import time
import traceback
from types import SimpleNamespace

import jwt
import pytest
from ppbase.api.deps import get_optional_auth
from ppbase.db.system_tables import CollectionRecord, SuperuserRecord
from ppbase.services.auth_service import generate_default_auth_options
from sqlalchemy.exc import DBAPIError
from sqlalchemy.sql.elements import TextClause

from scripts.c11_auth_import import (
    ARCHIVE_TABLE,
    NOT_IMPORTED,
    RECORD_IDENTITIES,
    RECORD_TOKENS,
    SUPERUSER_TOKENS,
    build_plan,
    check_source,
    diagnose_auth,
    import_auth,
    merged_options,
    public_report,
    read_source,
)
from scripts.c11_import import ImportBlocked, digest, snapshot

USERS_ID, AGENTS_ID = "_pb_users_auth_", "it0zrlwgubakjn6"
DEFINITIONS = [
    {"name": "users", "id": USERS_ID, "type": "auth"},
    {"name": "agent_users", "id": AGENTS_ID, "type": "auth"},
    {"name": "roles", "id": "6p629b1e32w3hh5", "type": "base"},
]
ADMIN = {
    "id": "c11admin0000001",
    "avatar": 3,
    "email": "c11-admin@example.invalid",
    "tokenKey": "T" * 50,
    "passwordHash": "$2a$12$" + "h" * 53,
    "lastResetSentAt": "2024-05-01 10:00:00.123Z",
    "created": "2023-01-02 03:04:05.678Z",
    "updated": "2024-05-01 10:00:00.123Z",
}
TOKEN_SETTINGS = (*SUPERUSER_TOKENS.values(), *RECORD_TOKENS.values())


def secret_for(key):
    return (key + "-synthetic-secret-").ljust(50, "x")


def pocketbase_settings():
    """Shape of PocketBase 0.22 json.Marshal(settings.Settings); synthetic values."""
    template = {
        "subject": "S {APP_NAME}",
        "body": "B {ACTION_URL}",
        "actionUrl": "{APP_URL}/x/{TOKEN}",
    }
    settings = {
        "meta": {
            "appName": "C11 synthetic",
            "appUrl": "https://c11.example.invalid//",
            "hideControls": True,
            "senderName": "C11 sender",
            "senderAddress": "sender@example.invalid",
            "verificationTemplate": {**template, "hidden": False},
            "resetPasswordTemplate": {**template, "subject": ""},
            "confirmEmailChangeTemplate": {**template, "body": ""},
        },
        "logs": {"maxDays": 9, "minLevel": -4, "logIp": False},
        "smtp": {
            "enabled": True,
            "host": "smtp.example.invalid",
            "port": 2525,
            "username": "c11",
            "password": "c11-smtp-synthetic-password",
            "authMethod": "LOGIN",
            "tls": False,
            "localName": "c11.example.invalid",
        },
        "s3": {"enabled": False, "bucket": "", "accessKey": "", "secret": "c11-s3-secret"},
        "backups": {"cron": "0 3 * * *", "cronMaxKeep": 4, "s3": {"enabled": False, "secret": ""}},
        "emailAuth": {"enabled": False, "minPasswordLength": 8},
        "googleAuth": {"enabled": False, "clientId": "", "clientSecret": ""},
    }
    for index, key in enumerate(TOKEN_SETTINGS):
        settings[key] = {"secret": secret_for(key), "duration": 600 + index}
    return settings


def write_source(
    root, settings=None, *, admins=(ADMIN,), params=(), external_auths=0, extra_sql=()
):
    """PocketBase 0.22 internal tables (migrations/1640988000_init.go DDL).

    ``extra_sql`` alters the closed source afterwards (unexpected columns,
    unrepresentable settings rows), before any digest is taken.
    """
    root.mkdir(exist_ok=True)
    with sqlite3.connect(root / "data.db") as db:
        db.executescript(
            """
            CREATE TABLE _admins (id TEXT PRIMARY KEY NOT NULL, avatar INTEGER DEFAULT 0 NOT NULL,
              email TEXT UNIQUE NOT NULL, tokenKey TEXT UNIQUE NOT NULL,
              passwordHash TEXT NOT NULL, lastResetSentAt TEXT DEFAULT "" NOT NULL,
              created TEXT NOT NULL, updated TEXT NOT NULL);
            CREATE TABLE _params (id TEXT PRIMARY KEY NOT NULL, key TEXT UNIQUE NOT NULL,
              value JSON DEFAULT NULL, created TEXT NOT NULL, updated TEXT NOT NULL);
            CREATE TABLE _externalAuths (id TEXT PRIMARY KEY NOT NULL, collectionId TEXT NOT NULL,
              recordId TEXT NOT NULL, provider TEXT NOT NULL, providerId TEXT NOT NULL,
              created TEXT NOT NULL, updated TEXT NOT NULL);
            """
        )
        for admin in admins:
            db.execute(
                "INSERT INTO _admins VALUES (?,?,?,?,?,?,?,?)",
                [admin[k] for k in ADMIN],
            )
        value = settings if isinstance(settings, str) else json.dumps(settings)
        if settings is not None:
            db.execute(
                "INSERT INTO _params VALUES ('c11param0000001','settings',?,"
                "'2023-01-01 00:00:00.000Z','2024-01-01 00:00:00.000Z')",
                (value,),
            )
        for index, key in enumerate(params):
            db.execute(
                "INSERT INTO _params VALUES (?,?,'{}','2023-01-01 00:00:00.000Z',"
                "'2023-01-01 00:00:00.000Z')",
                (f"c11extra{index:07d}", key),
            )
        for index in range(external_auths):
            db.execute(
                "INSERT INTO _externalAuths VALUES (?,?,?,?,?,'2023-01-01 00:00:00.000Z',"
                "'2023-01-01 00:00:00.000Z')",
                (f"c11ext{index:09d}", USERS_ID, "c11user00000001", "google", str(index)),
            )
        for statement in extra_sql:
            db.execute(statement)
    return root


def source_of(root):
    with snapshot(root) as db:
        return read_source(db)


def test_mapping_follows_pocketbase_upgrade_and_ppbase_representation(tmp_path):
    settings = pocketbase_settings()
    plan = build_plan(source_of(write_source(tmp_path / "data", settings)), DEFINITIONS)

    assert set(plan["tokens"]) == {"users", "agent_users", "_superusers"}
    for collection in ("users", "agent_users"):
        for kind, key in RECORD_TOKENS.items():
            assert plan["tokens"][collection][kind] == {
                "source": key,
                "secret": secret_for(key),
                "duration": settings[key]["duration"],
            }
    assert set(plan["tokens"]["_superusers"]) == set(SUPERUSER_TOKENS)
    for kind, key in SUPERUSER_TOKENS.items():
        assert plan["tokens"]["_superusers"][kind]["secret"] == secret_for(key)

    (admin,) = plan["admins"]
    assert admin["row"]["password_hash"] == ADMIN["passwordHash"]
    assert admin["row"]["token_key"] == ADMIN["tokenKey"]
    assert admin["row"]["avatar"] == 3 and admin["row"]["id"] == ADMIN["id"]
    assert admin["row"]["created"].isoformat() == "2023-01-02T03:04:05.678000+00:00"
    assert admin["legacy"] == {"lastResetSentAt": ADMIN["lastResetSentAt"]}
    assert admin["rowid"] == 1

    value = plan["settings_row"]["value"]
    # strings.TrimSuffix removes exactly one trailing slash (migrate.go:193).
    assert value["meta"]["appURL"] == "https://c11.example.invalid/"
    assert value["meta"]["appName"] == "C11 synthetic" and value["meta"]["hideControls"] is True
    assert value["logs"] == {**value["logs"], "maxDays": 9, "minLevel": -4, "logIP": False}
    assert value["smtp"] == {**settings["smtp"]}
    from ppbase.api.settings import _DEFAULT_SETTINGS

    # S3 and backups keep PPBase's disabled defaults; PPBase-only keys untouched.
    for key in ("s3", "backups", "batch", "rateLimits", "trustedProxy"):
        assert value[key] == _DEFAULT_SETTINGS[key]
    assert "emailAuth" not in value and "googleAuth" not in value
    assert plan["settings_row"]["id"] == "c11param0000001"

    templates = plan["templates"]["users"]
    assert templates["verificationTemplate"] == {
        "subject": "S {APP_NAME}",
        "body": "B {APP_URL}/x/{TOKEN}",
    }
    assert templates["resetPasswordTemplate"]["subject"] == ""
    assert templates["confirmEmailChangeTemplate"]["body"] == ""
    assert "_superusers" not in plan["templates"]
    assert {w["code"] for w in plan["warnings"]} == {"backup_schedule_not_imported"}


def test_options_overlay_keeps_target_values_for_zero_or_absent_source():
    current = generate_default_auth_options()
    current["passwordAuth"] = {"enabled": True, "identityFields": ["username"]}
    tokens = {
        "authToken": {"source": "recordAuthToken", "secret": "imported" * 5, "duration": 77},
        "fileToken": {"source": "recordFileToken", "secret": None, "duration": None},
        "verificationToken": {"source": "recordVerificationToken", "secret": None, "duration": 9},
    }
    templates = {"resetPasswordTemplate": {"subject": "", "body": "Imported body"}}
    merged = merged_options(current, tokens, templates)
    assert merged["authToken"] == {"secret": "imported" * 5, "duration": 77}
    assert merged["fileToken"] == current["fileToken"]
    assert merged["verificationToken"] == {**current["verificationToken"], "duration": 9}
    assert merged["resetPasswordTemplate"] == {
        "subject": current["resetPasswordTemplate"]["subject"],
        "body": "Imported body",
    }
    assert merged["passwordAuth"] == current["passwordAuth"]
    assert current["authToken"]["secret"] != "imported" * 5  # input not mutated


def test_absent_or_empty_token_configs_fall_back_like_zero_fallback(tmp_path):
    settings = pocketbase_settings()
    del settings["recordFileToken"]
    settings["adminFileToken"] = {"secret": "", "duration": 0}
    plan = build_plan(source_of(write_source(tmp_path / "data", settings)), DEFINITIONS)
    assert plan["tokens"]["users"]["fileToken"]["secret"] is None
    assert plan["tokens"]["_superusers"]["fileToken"] == {
        "source": "adminFileToken",
        "secret": None,
        "duration": None,
    }
    codes = [(w["code"], w.get("setting")) for w in plan["warnings"]]
    assert ("token_config_absent_target_default_kept", "recordFileToken") in codes
    assert ("token_config_absent_target_default_kept", "adminFileToken") in codes


def mutate(path, value):
    def apply(settings):
        target = settings
        for key in path[:-1]:
            target = target[key]
        target[path[-1]] = value
        return settings

    return apply


@pytest.mark.parametrize(
    ("change", "code"),
    [
        (lambda s: "c11-encrypted-base64-not-json", "settings_not_plain_json"),
        (lambda s: [1, 2], "settings_not_plain_json"),
        (lambda s: None, "settings_missing"),
        (mutate(("s3", "enabled"), True), "s3_storage_enabled_requires_decision"),
        (mutate(("githubAuth",), {"enabled": True}), "oauth2_provider_enabled_requires_decision"),
        (mutate(("recordAuthToken", "duration"), "600"), "invalid_token_config"),
        (mutate(("adminAuthToken",), "secret"), "invalid_token_config"),
        (mutate(("smtp", "port"), "2525"), "invalid_settings_value"),
        (mutate(("smtp", "tls"), 0), "invalid_settings_value"),
        (mutate(("meta", "resetPasswordTemplate"), "x"), "invalid_settings_value"),
    ],
)
def test_unrepresentable_settings_are_refused(tmp_path, change, code):
    settings = change(pocketbase_settings())
    if isinstance(settings, list):
        settings = json.dumps(settings)
    source = source_of(write_source(tmp_path / "data", settings))
    blockers, _warnings = check_source(source, DEFINITIONS)
    assert code in {b["code"] for b in blockers}
    with pytest.raises(ImportBlocked, match=code):
        build_plan(source, DEFINITIONS)


@pytest.mark.parametrize(
    ("admin", "code"),
    [
        ({**ADMIN, "id": "x" * 16}, "admin_value_not_representable"),
        ({**ADMIN, "tokenKey": ""}, "admin_value_not_representable"),
        ({**ADMIN, "email": "e" * 256}, "admin_value_not_representable"),
        ({**ADMIN, "avatar": 2**31}, "admin_value_not_representable"),
        ({**ADMIN, "created": "2023-01-02 03:04:05"}, "admin_value_not_representable"),
    ],
)
def test_unrepresentable_admins_are_refused_never_repaired(tmp_path, admin, code):
    source = source_of(write_source(tmp_path / "data", pocketbase_settings(), admins=[admin]))
    with pytest.raises(ImportBlocked, match=code):
        build_plan(source, DEFINITIONS)


def test_unknown_internal_rows_and_columns_are_refused(tmp_path):
    source = source_of(
        write_source(tmp_path / "a", pocketbase_settings(), params=["other"], external_auths=2)
    )
    codes = {b["code"] for b in check_source(source, DEFINITIONS)[0]}
    assert {"unknown_param_keys", "external_auths_present"} <= codes

    root = write_source(tmp_path / "b", pocketbase_settings())
    with sqlite3.connect(root / "data.db") as db:
        db.execute("ALTER TABLE _admins ADD COLUMN extra TEXT DEFAULT ''")
    with pytest.raises(ImportBlocked, match="unmapped_admin_columns"):
        build_plan(source_of(root), DEFINITIONS)

    root = write_source(tmp_path / "c", pocketbase_settings())
    with sqlite3.connect(root / "data.db") as db:
        db.execute("DROP TABLE _externalAuths")
    with pytest.raises(ImportBlocked, match="missing_internal_table"):
        build_plan(source_of(root), DEFINITIONS)


SETTINGS_ROW = "UPDATE _params SET {} WHERE key = 'settings'"
SOURCE_STRUCTURE_REFUSALS = (
    # Record tokens name their collection: without an auth collection there is
    # nothing to carry the record secrets to.
    (DEFINITIONS[2:], (), {"code": "source_auth_collections_missing"}),
    (
        DEFINITIONS,
        ("ALTER TABLE _params ADD COLUMN extra TEXT DEFAULT ''",),
        {"code": "unmapped_param_columns"},
    ),
    # _params.id is VARCHAR(15) in PPBase (ppbase/db/system_tables.py ParamRecord).
    (
        DEFINITIONS,
        (SETTINGS_ROW.format("id = 'c11param00000001'"),),
        {"code": "param_value_not_representable", "field": "id"},
    ),
    (
        DEFINITIONS,
        (SETTINGS_ROW.format("id = 'c11param' || char(0) || '000001'"),),
        {"code": "nul_in_postgresql_text", "field": "id"},
    ),
    (
        DEFINITIONS,
        (SETTINGS_ROW.format("created = ''"),),
        {"code": "param_value_not_representable", "field": "created"},
    ),
    (
        DEFINITIONS,
        (SETTINGS_ROW.format("created = '2023-01-01 00:00:00.000'"),),
        {"code": "param_value_not_representable", "field": "created"},
    ),
    (
        DEFINITIONS,
        (SETTINGS_ROW.format("updated = 'c11-not-a-date'"),),
        {"code": "param_value_not_representable", "field": "updated"},
    ),
)


@pytest.mark.parametrize(("definitions", "extra_sql", "blocker"), SOURCE_STRUCTURE_REFUSALS)
async def test_source_structure_refusals_stop_before_any_lock_or_write(
    tmp_path, definitions, extra_sql, blocker
):
    source = source_of(write_source(tmp_path / "a", pocketbase_settings(), extra_sql=extra_sql))
    blockers, _warnings = check_source(source, definitions)
    assert blockers == [blocker]  # by location only, never by value
    with pytest.raises(ImportBlocked, match=blocker["code"]):
        build_plan(source, definitions)

    staged = staged_copy(tmp_path / "b", extra_sql=extra_sql)
    db = scripted(staged)
    with pytest.raises(ImportBlocked, match=blocker["code"]):
        await import_auth(db, staged, definitions)
    assert db.statements == ["SELECT current_database()"]


async def test_warnings_do_not_block_keep_stored_values_and_are_reported_without_values(
    tmp_path,
):
    settings = pocketbase_settings()
    # Stored values PocketBase 0.22 itself would refuse on save
    # (models/settings/settings.go:379-384) but still signs and verifies with.
    settings["recordFileToken"]["secret"] = "c11-short-secret"
    settings["adminFileToken"]["duration"] = 63072001
    settings["backups"]["s3"]["enabled"] = True
    settings["c11FutureKey"] = {"enabled": False}
    settings["c11OtherKey"] = 1
    source = source_of(write_source(tmp_path / "a", settings))
    blockers, warnings = check_source(source, DEFINITIONS)
    assert blockers == []
    for warning in (
        {"code": "token_config_outside_pocketbase_limits", "setting": "recordFileToken"},
        {"code": "token_config_outside_pocketbase_limits", "setting": "adminFileToken"},
        {"code": "backup_s3_target_not_imported"},
        {"code": "unknown_settings_keys_ignored", "keys": 2},
    ):
        assert warning in warnings
    plan = build_plan(source, DEFINITIONS)
    # Imported as stored: a different secret would invalidate live file tokens.
    assert plan["tokens"]["users"]["fileToken"]["secret"] == "c11-short-secret"
    assert plan["tokens"]["_superusers"]["fileToken"]["duration"] == 63072001
    from ppbase.api.settings import _DEFAULT_SETTINGS

    value = plan["settings_row"]["value"]
    assert value["backups"] == _DEFAULT_SETTINGS["backups"]
    assert "c11FutureKey" not in value and "c11OtherKey" not in value
    with snapshot(tmp_path / "a") as db:
        diagnosis = diagnose_auth(db, DEFINITIONS)
    assert diagnosis["ready_for_auth_import"] is True
    assert diagnosis["token_configs"]["recordFileToken"] == {
        "present": True,
        "secret_present": True,
        "secret_length_within_pocketbase_limits": False,
        "duration_within_pocketbase_limits": True,
    }
    assert not diagnosis["token_configs"]["adminFileToken"]["duration_within_pocketbase_limits"]

    staged = staged_copy(tmp_path / "b", settings)
    db = scripted(staged)
    report = await import_auth(db, staged, DEFINITIONS)
    assert db.options(USERS_ID)["fileToken"]["secret"] == "c11-short-secret"
    assert db.options(SUPERUSERS_ID)["fileToken"]["duration"] == 63072001
    assert {(w["code"], w.get("setting")) for w in report["warnings"]} >= {
        ("token_config_outside_pocketbase_limits", "recordFileToken"),
        ("token_config_outside_pocketbase_limits", "adminFileToken"),
        ("backup_s3_target_not_imported", None),
        ("unknown_settings_keys_ignored", None),
    }
    rendered = json.dumps([report, diagnosis])
    assert "c11-short-secret" not in rendered and "c11FutureKey" not in rendered


def test_reports_carry_no_secret_identity_or_hash_value(tmp_path):
    settings = pocketbase_settings()
    root = write_source(tmp_path / "data", settings)
    before = digest(root / "data.db")
    plan = build_plan(source_of(root), DEFINITIONS)
    with snapshot(root) as db:
        diagnosis = diagnose_auth(db, DEFINITIONS)
    assert digest(root / "data.db") == before
    assert sorted(p.name for p in root.iterdir()) == ["data.db"]
    rendered = json.dumps([public_report(plan, imported=True), diagnosis])
    forbidden = [secret_for(key) for key in TOKEN_SETTINGS] + [
        settings["smtp"]["password"],
        settings["s3"]["secret"],
        ADMIN["email"],
        ADMIN["tokenKey"],
        ADMIN["passwordHash"],
        ADMIN["id"],
        settings["meta"]["appName"],
        settings["smtp"]["host"],
    ]
    for value in forbidden:
        assert value not in rendered
    report = public_report(plan, imported=True)
    assert report["token_configs"]["users"]["authToken"]["secret"] == {
        "present": True,
        "imported": True,
        "length": 50,
    }
    assert report["settings"]["mapped"]["smtp.password"]["length"] == len(
        settings["smtp"]["password"]
    )
    assert report["settings"]["not_imported_secrets"]["s3.secret"] == {"present": True}
    assert set(report["settings"]["not_imported"]) == {
        "s3",
        "backups",
        "emailAuth",
        "googleAuth",
    }
    assert report["settings"]["not_imported"]["s3"] == NOT_IMPORTED["s3"]
    assert report["admins"]["passwords_regenerated"] == 0
    assert diagnosis["ready_for_auth_import"] is True


class _Result:
    def __init__(self, value):
        self.value = value

    def scalars(self):
        return self

    def mappings(self):
        return self

    def first(self):
        return self.value


class _Session:
    """Just enough of AsyncSession for ppbase.api.deps.get_optional_auth."""

    def __init__(self, collections, superusers, token_keys):
        self.collections, self.superusers, self.token_keys = collections, superusers, token_keys

    async def get(self, model, key):
        return self.superusers.get(key) if model is SuperuserRecord else None

    async def execute(self, statement, params=None):
        if isinstance(statement, TextClause):
            table = re.search(r'FROM "([^"]+)"', statement.text).group(1)
            key = self.token_keys.get((table, params["rid"]))
            return _Result({"token_key": key} if key else None)
        sql = str(statement.compile(compile_kwargs={"literal_binds": True}))
        for collection in self.collections:
            if f"_collections.name = '{collection.name}'" in sql:
                return _Result(collection)
            if f"_collections.id = '{collection.id}'" in sql:
                return _Result(collection)
        return _Result(None)


def _request(token):
    settings = SimpleNamespace(get_jwt_secret=lambda: "ppbase-fallback-not-used")
    return SimpleNamespace(
        headers={"Authorization": token},
        app=SimpleNamespace(state=SimpleNamespace(settings=settings)),
    )


def pocketbase_022_token(claims, signing_key, seconds):
    """tools/security/jwt.go:45-58 NewJWT: HS256, exp plus the payload claims only."""
    return jwt.encode({"exp": int(time.time()) + seconds, **claims}, signing_key, "HS256")


async def test_pocketbase_022_sessions_verify_in_ppbase_only_with_imported_secrets(tmp_path):
    settings = pocketbase_settings()
    plan = build_plan(source_of(write_source(tmp_path / "data", settings)), DEFINITIONS)
    record_key, admin_key = "R" * 50, ADMIN["tokenKey"]
    record_secret = settings["recordAuthToken"]["secret"]
    admin_secret = settings["adminAuthToken"]["secret"]
    # tokens/record.go:12-27 and tokens/admin.go:11-17 (PocketBase v0.22.34).
    record_token = pocketbase_022_token(
        {"id": "c11user00000001", "type": "authRecord", "collectionId": USERS_ID},
        record_key + record_secret,
        1209600,
    )
    admin_token = pocketbase_022_token(
        {"id": ADMIN["id"], "type": "admin"}, admin_key + admin_secret, 1209600
    )

    def session(imported):
        options = {}
        for name in ("users", "agent_users", "_superusers"):
            default = generate_default_auth_options(is_superusers=name == "_superusers")
            options[name] = (
                merged_options(default, plan["tokens"][name], plan["templates"].get(name))
                if imported
                else default
            )
        collections = [
            CollectionRecord(id=USERS_ID, name="users", type="auth", options=options["users"]),
            CollectionRecord(
                id=AGENTS_ID, name="agent_users", type="auth", options=options["agent_users"]
            ),
            CollectionRecord(
                id="c11superusers01",
                name="_superusers",
                type="auth",
                options=options["_superusers"],
            ),
        ]
        superuser = SuperuserRecord(id=ADMIN["id"], email=ADMIN["email"], token_key=admin_key)
        keys = {("users", "c11user00000001"): record_key}
        return _Session(collections, {ADMIN["id"]: superuser}, keys)

    # Before the import PPBase's own random per-collection secrets reject them.
    assert await get_optional_auth(_request(record_token), session(False)) is None
    assert await get_optional_auth(_request(admin_token), session(False)) is None

    record = await get_optional_auth(_request(record_token), session(True))
    assert record["type"] == "authRecord" and record["id"] == "c11user00000001"
    assert record["collectionName"] == "users"
    admin = await get_optional_auth(_request("Bearer " + admin_token), session(True))
    assert admin["type"] == "admin" and admin["collectionName"] == "_superusers"

    header, payload, signature = record_token.split(".")
    tampered = ".".join([header, payload, ("A" if signature[0] != "A" else "B") + signature[1:]])
    expired = pocketbase_022_token(
        {"id": "c11user00000001", "type": "authRecord", "collectionId": USERS_ID},
        record_key + record_secret,
        -5,
    )
    other_collection = pocketbase_022_token(
        {"id": "c11user00000001", "type": "authRecord", "collectionId": AGENTS_ID},
        record_key + record_secret,
        1209600,
    )
    admin_with_record_secret = pocketbase_022_token(
        {"id": ADMIN["id"], "type": "admin"}, admin_key + record_secret, 1209600
    )
    for token in (tampered, expired, other_collection, admin_with_record_secret):
        assert await get_optional_auth(_request(token), session(True)) is None

    # PocketBase invalidates sessions by renewing tokenKey on password reset or
    # email change (v0.22.34 models/record.go:942-962, models/admin.go:45-67):
    # once the stored key differs from the signing key, PPBase refuses too.
    rotated = session(True)
    rotated.token_keys[("users", "c11user00000001")] = "S" * 50
    rotated.superusers[ADMIN["id"]] = SuperuserRecord(
        id=ADMIN["id"], email=ADMIN["email"], token_key="U" * 50
    )
    assert await get_optional_auth(_request(record_token), rotated) is None
    assert await get_optional_auth(_request(admin_token), rotated) is None
    renewed = pocketbase_022_token(
        {"id": "c11user00000001", "type": "authRecord", "collectionId": USERS_ID},
        "S" * 50 + record_secret,
        1209600,
    )
    assert (await get_optional_auth(_request(renewed), rotated))["id"] == "c11user00000001"


def test_text_problems_and_duplicates_are_reported_by_location_never_by_value(tmp_path):
    settings = pocketbase_settings()
    settings["smtp"]["host"] = "c11\x00host"
    settings["meta"]["resetPasswordTemplate"]["actionUrl"] = "c11\ud800"
    source = source_of(write_source(tmp_path / "data", settings))
    # UNIQUE in PocketBase's own _admins DDL; guarded anyway, on the read source.
    source["admins"] = [ADMIN, {**ADMIN, "id": "c11admin0000002"}]
    blockers, _warnings = check_source(source, DEFINITIONS)
    assert {"code": "nul_in_postgresql_text", "path": "smtp.host"} in blockers
    assert {
        "code": "text_not_utf8_encodable",
        "path": "meta.resetPasswordTemplate.actionUrl",
    } in blockers
    codes = {b["code"] for b in blockers}
    assert {"duplicate_admin_email", "duplicate_admin_token_key"} <= codes
    rendered = json.dumps(blockers)
    assert "c11" not in rendered and ADMIN["email"] not in rendered


def test_plan_is_deterministic_and_does_not_share_mutable_state(tmp_path):
    settings = pocketbase_settings()
    source = source_of(write_source(tmp_path / "data", settings))
    first, second = build_plan(source, DEFINITIONS), build_plan(copy.deepcopy(source), DEFINITIONS)
    assert first["settings_row"]["value"] == second["settings_row"]["value"]
    first["settings_row"]["value"]["meta"]["appName"] = "changed"
    from ppbase.api.settings import _DEFAULT_SETTINGS

    assert _DEFAULT_SETTINGS["meta"]["appName"] != "changed"


WRITES = ("INSERT", "UPDATE", "DELETE", "CREATE", "ALTER", "DROP", "TRUNCATE")
SUPERUSERS_ID = "pbc_3142635823"


class _Rows:
    def __init__(self, scalar=None, rows=()):
        self.scalar, self.rows = scalar, [dict(row) for row in rows]

    def scalar_one(self):
        return self.scalar

    def mappings(self):
        return self

    def all(self):
        return self.rows

    def one(self):
        (row,) = self.rows
        return row


class _DriverError(Exception):
    sqlstate = "23505"


class _ScriptedDatabase:
    """In-memory stand-in for the rehearsal PostgreSQL answering import_auth.

    Statements are matched on their SQL text; an unexpected one fails the test.
    ``tamper`` alters what one readback returns, ``fail_on`` raises a
    DBAPIError that (like an engine without hide_parameters) echoes the values.
    """

    def __init__(self, diagnostic, **state):
        self.sync_engine = SimpleNamespace(hide_parameters=state.pop("hide", True))
        self.database = state.pop("database", "buma_c11_rehearsal")
        self.superusers = state.pop("superusers", [])
        self.params = state.pop("params", [])
        self.archive = state.pop("archive", None)
        self.rowids = state.pop("rowids", True)
        self.identities = state.pop(
            "identities",
            {
                c["id"]: (c["rows"], c["identity_sha256"])
                for c in diagnostic.get("collections", [])
                if c["rows"]
            },
        )
        default = [
            {"id": USERS_ID, "name": "users", "options": generate_default_auth_options()},
            {"id": AGENTS_ID, "name": "agent_users", "options": generate_default_auth_options()},
            {
                "id": SUPERUSERS_ID,
                "name": "_superusers",
                "options": generate_default_auth_options(is_superusers=True),
            },
        ]
        self.collections = state.pop("collections", default)
        self.tamper = state.pop("tamper", None)
        self.fail_on = state.pop("fail_on", None)
        assert not state, state
        self.statements = []

    def writes(self):
        return [s for s in self.statements if s.lstrip().upper().startswith(WRITES)]

    def options(self, collection_id):
        return next(c for c in self.collections if c["id"] == collection_id)["options"]

    async def execute(self, statement, params=None):
        sql = str(statement)
        self.statements.append(sql)
        params = params or {}
        if self.fail_on and sql.startswith(self.fail_on):
            raise DBAPIError(sql, params, _DriverError("synthetic"), hide_parameters=False)
        if sql == "SELECT current_database()":
            return _Rows(self.database)
        if sql.startswith("LOCK TABLE"):
            return _Rows()
        if sql == 'SELECT COUNT(*) FROM "_superusers"':
            return _Rows(len(self.superusers))
        if sql.startswith('SELECT COUNT(*) FROM "_params"'):
            return _Rows(sum(row["key"] == "settings" for row in self.params))
        if sql == "SELECT to_regclass(:name)":
            assert params["name"] == ARCHIVE_TABLE
            return _Rows(None if self.archive is None else ARCHIVE_TABLE)
        if sql == "SELECT to_regclass('_buma_c11_rowids')":
            return _Rows("_buma_c11_rowids" if self.rowids else None)
        if sql == RECORD_IDENTITIES:
            return _Rows(
                rows=[
                    {"collection_id": key, "row_count": rows, "identity": identity}
                    for key, (rows, identity) in self.identities.items()
                ]
            )
        if sql.startswith('SELECT "id", "name", "options" FROM "_collections"'):
            return _Rows(rows=copy.deepcopy(self.collections))
        if sql.startswith(f'CREATE TABLE "{ARCHIVE_TABLE}"'):
            self.archive = []
            return _Rows()
        if sql.startswith(f'INSERT INTO "{ARCHIVE_TABLE}"'):
            self.archive.append({**params, "legacy": json.loads(params["legacy"])})
            return _Rows()
        if sql.startswith('INSERT INTO "_superusers"'):
            row = {**params, "last_reset_sent_at": None}
            if self.tamper == "superusers":
                row["avatar"] += 1
            self.superusers.append(row)
            return _Rows()
        if sql.startswith('SELECT "id", "avatar", "email"'):
            return _Rows(rows=self.superusers)
        if sql.startswith('UPDATE "_collections"'):
            options = json.loads(params["options"])
            if self.tamper == "options":
                options["c11Tampered"] = True
            next(c for c in self.collections if c["id"] == params["id"])["options"] = options
            return _Rows()
        if sql.startswith('SELECT "options" FROM "_collections"'):
            return _Rows(copy.deepcopy(self.options(params["id"])))
        if sql.startswith('INSERT INTO "_params"'):
            value = json.loads(params["value"])
            if self.tamper == "settings":
                value["c11Tampered"] = True
            self.params.append({**params, "key": "settings", "value": value})
            return _Rows()
        if sql.startswith('SELECT "id", "value", "created", "updated" FROM "_params"'):
            return _Rows(rows=[row for row in self.params if row["key"] == "settings"])
        raise AssertionError("unexpected statement: " + sql)


def staged_copy(root, settings=None, *, ready=True, checksum=None, identities=True, **source):
    write_source(root, pocketbase_settings() if settings is None else settings, **source)
    diagnostic = {
        "ready_for_isolated_rehearsal": ready,
        "data_db_sha256": checksum or digest(root / "data.db"),
    }
    if identities:
        # Synthetic fingerprints of the records load_isolated would have archived.
        diagnostic["collections"] = [
            {"id": USERS_ID, "name": "users", "rows": 2, "identity_sha256": "a" * 64},
            {"id": AGENTS_ID, "name": "agent_users", "rows": 1, "identity_sha256": "b" * 64},
            {"id": "6p629b1e32w3hh5", "name": "roles", "rows": 0, "identity_sha256": "c" * 64},
        ]
    (root / "manifest.json").write_text(json.dumps({"diagnostic": diagnostic}))
    return root


def scripted(staged, **state):
    manifest = json.loads((staged / "manifest.json").read_text())
    return _ScriptedDatabase(manifest["diagnostic"], **state)


async def test_import_writes_the_plan_in_order_and_reports_without_values(tmp_path):
    staged = staged_copy(tmp_path / "data")
    db = scripted(staged)
    report = await import_auth(db, staged, DEFINITIONS)

    (admin,) = db.superusers
    assert admin["password_hash"] == ADMIN["passwordHash"]
    assert admin["token_key"] == ADMIN["tokenKey"]
    assert (admin["id"], admin["email"], admin["avatar"]) == (ADMIN["id"], ADMIN["email"], 3)
    assert admin["last_reset_sent_at"] is None
    assert sorted((row["table"], row["id"], row["rowid"]) for row in db.archive) == [
        ("_admins", ADMIN["id"], 1),
        ("_params", "c11param0000001", 1),
    ]
    assert db.archive[0]["legacy"] == {"lastResetSentAt": ADMIN["lastResetSentAt"]}
    for collection_id in (USERS_ID, AGENTS_ID):
        for kind, key in RECORD_TOKENS.items():
            assert db.options(collection_id)[kind]["secret"] == secret_for(key)
    superusers = db.options(SUPERUSERS_ID)
    for kind, key in SUPERUSER_TOKENS.items():
        assert superusers[kind]["secret"] == secret_for(key)
    default = generate_default_auth_options(is_superusers=True)
    # Not a 0.22 token type: PPBase's own random secret is kept.
    assert superusers["verificationToken"]["secret"] != secret_for("recordVerificationToken")
    assert set(superusers) >= set(default)
    (settings,) = db.params
    assert settings["value"]["smtp"]["password"] == pocketbase_settings()["smtp"]["password"]
    writes = [statement.split("(")[0].strip() for statement in db.writes()]
    assert writes == [
        f'CREATE TABLE "{ARCHIVE_TABLE}"',
        'INSERT INTO "_superusers"',
        f'INSERT INTO "{ARCHIVE_TABLE}" VALUES',
        *['UPDATE "_collections" SET "options" = CAST'] * 3,
        'INSERT INTO "_params"',
        f'INSERT INTO "{ARCHIVE_TABLE}" VALUES',
    ]
    assert report["records_snapshot"] == {"identity_verified": True, "collections": 2, "rows": 3}
    rendered = json.dumps(report)
    for value in (ADMIN["passwordHash"], ADMIN["tokenKey"], ADMIN["email"]):
        assert value not in rendered
    for key in TOKEN_SETTINGS:
        assert secret_for(key) not in rendered


def _other_identity(staged):
    db = scripted(staged)
    return {**db.identities, USERS_ID: (2, "f" * 64)}


REFUSALS_BEFORE_ANY_WRITE = (
    ({"hide": False}, "engine_hide_parameters_required"),
    ({"database": "postgres"}, "isolated_rehearsal_database_required"),
    ({"superusers": [{"id": "x"}]}, "superusers_target_not_empty"),
    ({"params": [{"id": "x", "key": "settings"}]}, "settings_target_not_empty"),
    ({"archive": []}, "auth_archive_exists"),
    ({"rowids": False}, "records_import_required"),
    ({"identities": "other"}, "records_snapshot_mismatch"),
    ({"identities": {}}, "records_snapshot_mismatch"),
    ({"collections": "without_agent_users"}, "target_auth_collections_mismatch"),
    ({"collections": "agent_users_id_changed"}, "target_auth_collection_id_mismatch"),
)


@pytest.mark.parametrize(("state", "code"), REFUSALS_BEFORE_ANY_WRITE)
async def test_database_guards_refuse_before_any_write(tmp_path, state, code):
    staged = staged_copy(tmp_path / "data")
    state = dict(state)
    if state.get("identities") == "other":
        state["identities"] = _other_identity(staged)
    if state.get("collections") in {"without_agent_users", "agent_users_id_changed"}:
        collections = scripted(staged).collections
        if state["collections"] == "without_agent_users":
            collections = [c for c in collections if c["name"] != "agent_users"]
        else:
            collections[1]["id"] = "c11otherauthid1"
        state["collections"] = collections
    db = scripted(staged, **state)
    with pytest.raises(ImportBlocked, match=code):
        await import_auth(db, staged, DEFINITIONS)
    assert db.writes() == []
    assert db.superusers == state.get("superusers", []) and db.archive == state.get("archive")


def _with_nul(path):
    return mutate(path, "c11\x00synthetic")


@pytest.mark.parametrize(
    ("settings", "source", "code"),
    [
        (lambda s: "c11-encrypted-base64-not-json", {}, "settings_not_plain_json"),
        (_with_nul(("smtp", "password")), {}, "nul_in_postgresql_text"),
        (_with_nul(("meta", "verificationTemplate", "body")), {}, "nul_in_postgresql_text"),
        (_with_nul(("recordAuthToken", "secret")), {}, "nul_in_postgresql_text"),
        (mutate(("meta", "appName"), "c11\ud800"), {}, "text_not_utf8_encodable"),
        (
            None,
            {"admins": [{**ADMIN, "email": "c11\x00@example.invalid"}]},
            "nul_in_postgresql_text",
        ),
        (None, {"admins": [{**ADMIN, "lastResetSentAt": "\x00"}]}, "nul_in_postgresql_text"),
        (None, {"external_auths": 1}, "external_auths_present"),
        (None, {"params": ["other"]}, "unknown_param_keys"),
        (mutate(("s3", "enabled"), True), {}, "s3_storage_enabled_requires_decision"),
    ],
)
async def test_source_blockers_stop_import_auth_before_any_lock_or_write(
    tmp_path, settings, source, code
):
    value = pocketbase_settings() if settings is None else settings(pocketbase_settings())
    staged = staged_copy(tmp_path / "data", value, **source)
    db = scripted(staged)
    with pytest.raises(ImportBlocked, match=code):
        await import_auth(db, staged, DEFINITIONS)
    assert db.statements == ["SELECT current_database()"]


@pytest.mark.parametrize(
    ("stage", "code", "absent"),
    [
        (
            "superusers",
            "superusers_readback_mismatch",
            ('UPDATE "_collections"', 'INSERT INTO "_params"'),
        ),
        ("options", "auth_options_readback_mismatch", ('INSERT INTO "_params"',)),
        ("settings", "settings_readback_mismatch", ()),
    ],
)
async def test_readback_mismatch_stops_before_the_next_stage(tmp_path, stage, code, absent):
    staged = staged_copy(tmp_path / "data")
    db = scripted(staged, tamper=stage)
    with pytest.raises(ImportBlocked, match=code):
        await import_auth(db, staged, DEFINITIONS)
    for prefix in absent:
        assert not any(statement.startswith(prefix) for statement in db.statements)
    if stage == "options":
        # Stops on the first collection whose readback differs.
        assert sum(s.startswith('UPDATE "_collections"') for s in db.statements) == 1


async def test_database_errors_become_value_free_blocked_codes(tmp_path):
    staged = staged_copy(tmp_path / "data")
    db = scripted(staged, fail_on='INSERT INTO "_superusers"')
    with pytest.raises(ImportBlocked) as caught:
        await import_auth(db, staged, DEFINITIONS)
    error = caught.value
    assert str(error) == "auth_import_database_error" and error.sqlstate == "23505"
    assert error.__context__ is None and error.__cause__ is None
    rendered = "".join(traceback.format_exception(error))
    for value in (ADMIN["passwordHash"], ADMIN["tokenKey"], ADMIN["email"], "parameters"):
        assert value not in rendered


async def test_import_refuses_unusable_staged_manifests_before_any_write(tmp_path):
    cases = (
        (staged_copy(tmp_path / "b", ready=False), "diagnostic_blockers"),
        (staged_copy(tmp_path / "c", checksum="0" * 64), "staged_snapshot_changed"),
        (staged_copy(tmp_path / "d", identities=False), "records_snapshot_unverifiable"),
    )
    for staged, code in cases:
        db = scripted(staged)
        with pytest.raises(ImportBlocked, match=code):
            await import_auth(db, staged, DEFINITIONS)
        assert db.writes() == []
    with pytest.raises(ValueError, match="unknown failure stage"):
        await import_auth(scripted(tmp_path / "b"), tmp_path / "b", DEFINITIONS, fail_after="x")
