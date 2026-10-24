"""C11 accounts/settings/session import: mapping, refusals and token continuity."""

import copy
import json
import re
import sqlite3
import time
from types import SimpleNamespace

import jwt
import pytest
from ppbase.api.deps import get_optional_auth
from ppbase.db.system_tables import CollectionRecord, SuperuserRecord
from ppbase.services.auth_service import generate_default_auth_options
from sqlalchemy.sql.elements import TextClause

from scripts.c11_auth_import import (
    NOT_IMPORTED,
    RECORD_TOKENS,
    SUPERUSER_TOKENS,
    build_plan,
    check_source,
    diagnose_auth,
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


def write_source(root, settings=None, *, admins=(ADMIN,), params=(), external_auths=0):
    """PocketBase 0.22 internal tables (migrations/1640988000_init.go DDL)."""
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


def test_plan_is_deterministic_and_does_not_share_mutable_state(tmp_path):
    settings = pocketbase_settings()
    source = source_of(write_source(tmp_path / "data", settings))
    first, second = build_plan(source, DEFINITIONS), build_plan(copy.deepcopy(source), DEFINITIONS)
    assert first["settings_row"]["value"] == second["settings_row"]["value"]
    first["settings_row"]["value"]["meta"]["appName"] = "changed"
    from ppbase.api.settings import _DEFAULT_SETTINGS

    assert _DEFAULT_SETTINGS["meta"]["appName"] != "changed"


class _Connection:
    """Records statements; answers current_database() with a chosen name."""

    def __init__(self, database):
        self.database, self.statements = database, []

    async def execute(self, statement, params=None):
        self.statements.append(str(statement))
        return SimpleNamespace(scalar_one=lambda: self.database)


def staged_copy(root, *, ready=True, checksum=None):
    write_source(root, pocketbase_settings())
    manifest = {
        "diagnostic": {
            "ready_for_isolated_rehearsal": ready,
            "data_db_sha256": checksum or digest(root / "data.db"),
        }
    }
    (root / "manifest.json").write_text(json.dumps(manifest))
    return root


async def test_import_refuses_before_any_write_outside_its_preconditions(tmp_path):
    from scripts.c11_auth_import import import_auth

    cases = (
        (staged_copy(tmp_path / "a"), "postgres", "isolated_rehearsal_database_required"),
        (staged_copy(tmp_path / "b", ready=False), "buma_c11_rehearsal", "diagnostic_blockers"),
        (
            staged_copy(tmp_path / "c", checksum="0" * 64),
            "buma_c11_rehearsal",
            "staged_snapshot_changed",
        ),
    )
    for staged, database, code in cases:
        connection = _Connection(database)
        with pytest.raises(ImportBlocked, match=code):
            await import_auth(connection, staged, DEFINITIONS)
        assert all(statement == "SELECT current_database()" for statement in connection.statements)
    with pytest.raises(ValueError, match="unknown failure stage"):
        await import_auth(
            _Connection("buma_c11_rehearsal"), tmp_path / "a", DEFINITIONS, fail_after="x"
        )
