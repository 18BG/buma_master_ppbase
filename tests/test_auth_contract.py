import json
from pathlib import Path

import pytest
from fastapi import HTTPException
from fastapi.responses import JSONResponse, PlainTextResponse
from ppbase import PPBase

from app.auth.context import is_record_auth, merge_live_auth_context
from app.auth.middleware import require_admin_or_record_auth
from app.auth.permissions import get_app_permission
from app.hooks.auth import is_connected, user_health
from app.hooks.register import HOOK_MODULES, register


def make_ppbase() -> PPBase:
    return PPBase(
        database_url=("postgresql+asyncpg://ppbase:ppbase@localhost:55434/buma_master_ppbase")
    )


@pytest.mark.asyncio
async def test_legacy_connection_routes_keep_source_status_and_payloads() -> None:
    disconnected = await is_connected(None)
    assert isinstance(disconnected, JSONResponse)
    assert disconnected.status_code == 400
    assert json.loads(disconnected.body) == {"message": "false"}

    connected = await is_connected({"type": "authRecord", "id": "user_1"})
    assert connected == {"message": "true"}
    assert await is_connected({"type": "auth", "id": "user_1"}) == {"message": "true"}
    admin = await is_connected({"type": "admin", "id": "admin_1"})
    assert isinstance(admin, JSONResponse)
    assert admin.status_code == 400

    guest_health = await user_health(None)
    assert isinstance(guest_health, PlainTextResponse)
    assert guest_health.status_code == 200
    assert guest_health.body == b"false"

    auth_health = await user_health({"type": "authRecord", "id": "user_1"})
    assert isinstance(auth_health, PlainTextResponse)
    assert auth_health.status_code == 200
    assert auth_health.body == b"true"
    assert (await user_health({"type": "auth", "id": "user_1"})).body == b"true"
    assert (await user_health({"type": "admin", "id": "admin_1"})).body == b"false"


def test_record_auth_context_accepts_standard_and_legacy_claim_types() -> None:
    assert is_record_auth({"type": "auth", "id": "user_1"}) is True
    assert is_record_auth({"type": "authRecord", "id": "user_1"}) is True
    assert is_record_auth({"type": "admin", "id": "admin_1"}) is False
    assert is_record_auth(None) is False


def test_auth_routes_and_native_record_auth_routes_are_registered() -> None:
    pb = make_ppbase()
    register(pb)
    extension_routes = {
        (route.path, method)
        for route in pb._extensions._routes
        if route.methods
        for method in route.methods
    }

    def flatten(routes, prefix=""):
        for route in routes:
            if hasattr(route, "original_router"):
                include_prefix = getattr(route.include_context, "prefix", "")
                yield from flatten(route.original_router.routes, prefix + include_prefix)
                continue
            nested = getattr(route, "routes", None)
            if nested is not None:
                yield from flatten(nested, prefix)
            else:
                path = prefix.rstrip("/") + getattr(route, "path", "")
                for method in getattr(route, "methods", ()):
                    yield path, method

    native_routes = set(flatten(pb.get_app().router.routes))

    assert HOOK_MODULES == (
        "app.hooks.health",
        "app.hooks.auth",
        "app.hooks.customer",
        "app.hooks.supplier",
        "app.hooks.employee",
        "app.hooks.pdv",
        "app.hooks.finance",
        "app.hooks.stockage",
        "app.hooks.commerce",
        "app.hooks.legacy_sql",
        "app.hooks.legacy_stockage",
        "app.hooks.legacy_records",
        "app.hooks.poc",
    )
    assert ("/isConnected", "GET") in extension_routes
    assert ("/master/user/health", "GET") in extension_routes
    assert (
        "/api/collections/{collectionIdOrName}/auth-with-password",
        "POST",
    ) in native_routes
    assert (
        "/api/collections/{collectionIdOrName}/auth-refresh",
        "POST",
    ) in native_routes


def test_permission_matrix_matches_source_admin_and_non_admin_paths() -> None:
    role = {"permission": {"sale": {"read": True, "create": False}}}
    assert get_app_permission(role, "sale") == {"read": True, "create": False}
    assert get_app_permission(role, "wallet") == {}

    admin_role = {"permission": json.dumps({"isAdmin": True})}
    app_roles = [
        {
            "name": "wallet",
            "info": {"actions": ["read:Voir", "transfer:Transferer"]},
        }
    ]
    admin_permission = get_app_permission(admin_role, "wallet", app_roles)
    assert admin_permission["isAdmin"] is True
    assert admin_permission["read"] is True
    assert admin_permission["create"] is True
    assert admin_permission["transfer"] is True

    malformed = {"permission": "not-json"}
    assert get_app_permission(malformed, "wallet") == {"isAdmin": False}


def test_rule_context_contains_live_role_permission_paths_without_secrets() -> None:
    context = merge_live_auth_context(
        {"id": "u1", "type": "authRecord"},
        {"id": "u1", "role": "r1", "agent": "a1", "password_hash": "secret"},
        {"permission": {"isAdmin": False, "sale": {"read": True}}},
    )
    assert context["id"] == "u1"
    assert context["role.permission.isAdmin"] is False
    assert context["role.permission.sale.read"] is True
    assert context["role.permission.role.remove"] is False
    assert context["agent"] == "a1"
    assert "password_hash" not in context


def test_anonymous_rule_context_has_boolean_safe_permissions() -> None:
    context = merge_live_auth_context(None, None, None, [])
    assert context["role.permission.isAdmin"] is False
    assert context["role.permission.role.create"] is False
    assert context["role.permission.user.update"] is False


class _FakeEvent:
    def __init__(self, auth):
        self.auth = auth
        self.next_called = False

    async def load_auth(self):
        return self.auth

    def has_auth(self):
        return self.auth is not None

    def is_superuser(self):
        return bool(self.auth and self.auth.get("type") == "admin")

    def has_record_auth(self):
        return bool(self.auth and self.auth.get("type") in {"auth", "authRecord"})

    def _http_error(self, status, message, data=None):
        return HTTPException(status_code=status, detail={"message": message, "data": data or {}})

    async def next(self):
        self.next_called = True
        return {"ok": True}


@pytest.mark.asyncio
async def test_require_admin_or_record_auth_has_source_401_and_403_split() -> None:
    middleware = require_admin_or_record_auth()

    guest = _FakeEvent(None)
    with pytest.raises(Exception) as guest_error:
        await middleware(guest)
    assert guest_error.value.status_code == 401

    invalid_auth = _FakeEvent({"type": "other"})
    with pytest.raises(Exception) as invalid_error:
        await middleware(invalid_auth)
    assert invalid_error.value.status_code == 403

    record_auth = _FakeEvent({"type": "authRecord", "id": "u1"})
    assert await middleware(record_auth) == {"ok": True}
    assert record_auth.next_called is True

    standard_record_auth = _FakeEvent({"type": "auth", "id": "u1"})
    assert await middleware(standard_record_auth) == {"ok": True}
    assert standard_record_auth.next_called is True

    admin_auth = _FakeEvent({"type": "admin", "id": "a1"})
    assert await middleware(admin_auth) == {"ok": True}
    assert admin_auth.next_called is True


def test_auth_compat_migration_declares_source_identity_policy() -> None:
    migration = (Path(__file__).parents[1] / "pb_migrations/1789236844_auth_compat.py").read_text()
    assert '"users": ["username"]' in migration
    assert '"agent_users": ["email", "username"]' in migration
    assert '"users": 5' in migration
    assert '"agent_users": 8' in migration
