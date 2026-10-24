"""Live auth-record context used by PocketBase-style permission rules."""

from __future__ import annotations

import json
from contextvars import ContextVar
from typing import Any

from ppbase.db.engine import get_engine
from ppbase.ext.events import RouteRequestEvent
from ppbase.services.record_service import resolve_collection
from sqlalchemy import text

AUTH_RULE_CONTEXT: ContextVar[dict[str, Any] | None] = ContextVar(
    "buma_auth_rule_context",
    default=None,
)
_PATCH_INSTALLED = False
AUTH_RECORD_TYPES = frozenset({"auth", "authRecord"})


def is_record_auth(auth: dict[str, Any] | None) -> bool:
    """Return whether a token payload represents a record-auth session.

    PocketBase 0.22 signs record tokens with ``type=authRecord``
    (v0.22.34 tokens/tokens.go l.4-7); current PocketBase and PPBase sign
    ``type=auth`` (v0.40.6 core/record_tokens.go l.13 and l.62). Both are accepted so
    that sessions issued by PocketBase 0.22.34 stay valid after the import.
    """
    return bool(auth and auth.get("type") in AUTH_RECORD_TYPES)


def _flatten(value: Any, prefix: str = "") -> dict[str, Any]:
    if not isinstance(value, dict):
        return {prefix: value} if prefix else {}
    result: dict[str, Any] = {}
    for key, child in value.items():
        name = f"{prefix}.{key}" if prefix else str(key)
        if isinstance(child, dict):
            result.update(_flatten(child, name))
        else:
            result[name] = child
    return result


def merge_live_auth_context(
    auth: dict[str, Any] | None,
    record: dict[str, Any] | None,
    role: dict[str, Any] | None,
    app_roles: list[dict[str, Any]] | None = None,
) -> dict[str, Any]:
    """Build flat ``@request.auth.*`` values from current DB state."""
    result = dict(auth or {})
    for key, value in (record or {}).items():
        if key not in {"password_hash", "token_key", "rowid"}:
            result.setdefault(key, value)
    permission: dict[str, Any] = {}
    if role is not None:
        raw_permission = role.get("permission")
        if isinstance(raw_permission, str):
            try:
                raw_permission = json.loads(raw_permission)
            except (TypeError, ValueError):
                raw_permission = {}
        if isinstance(raw_permission, dict):
            permission = raw_permission
    permission.setdefault("isAdmin", False)
    for app_role in app_roles or []:
        app_name = str(app_role.get("name") or "").strip()
        if not app_name:
            continue
        app_permission = permission.setdefault(app_name, {})
        if not isinstance(app_permission, dict):
            app_permission = {}
            permission[app_name] = app_permission
        info = app_role.get("info")
        if isinstance(info, str):
            try:
                info = json.loads(info)
            except (TypeError, ValueError):
                info = {}
        actions = info.get("actions", []) if isinstance(info, dict) else []
        for action in actions if isinstance(actions, list) else []:
            key = str(action).split(":", 1)[0].strip()
            if key:
                app_permission.setdefault(key, False)
    for app_name in ("role", "user"):
        app_permission = permission.setdefault(app_name, {})
        if isinstance(app_permission, dict):
            for key in ("create", "update", "remove", "read", "manage"):
                app_permission.setdefault(key, False)
    result.update(
        {f"role.{key}": value for key, value in _flatten({"permission": permission}).items()}
    )
    return result


def install_rule_context_patch() -> None:
    """Make PPBase's native rule context consume the live auth context."""
    global _PATCH_INSTALLED
    if _PATCH_INSTALLED:
        return
    import ppbase.api.records as records_api

    original = records_api._prepare_rule_context

    def patched(request, token_payload, data=None):
        auth_ctx, request_ctx = original(request, token_payload, data=data)
        live = AUTH_RULE_CONTEXT.get() or {}
        if live:
            request_ctx.setdefault("auth", {}).update(live)
        return auth_ctx, request_ctx

    records_api._prepare_rule_context = patched
    _PATCH_INSTALLED = True


async def load_live_auth_context(event: RouteRequestEvent) -> dict[str, Any]:
    """Resolve the current auth record and role for one HTTP request."""
    auth = await event.load_auth()
    return await load_auth_record_context(get_engine(), auth)


async def load_auth_record_context(engine, auth):
    """Resolve the identity's own live role for HTTP or realtime evaluation."""
    if not is_record_auth(auth):
        # Keep boolean permission macros type-safe for anonymous requests.
        # PocketBase evaluates missing auth permissions as false; returning an
        # empty string would make PostgreSQL reject ``'' = TRUE`` instead.
        return merge_live_auth_context(None, None, None, [])

    collection = await resolve_collection(
        engine,
        str(auth.get("collectionName") or auth.get("collectionId") or ""),
    )
    if collection is None:
        return {}

    async with engine.connect() as conn:
        result = await conn.execute(
            text(f'SELECT * FROM "{collection.name}" WHERE "id" = :id LIMIT 1'),
            {"id": str(auth.get("id") or "")},
        )
        record = dict(result.mappings().first() or {})

        role: dict[str, Any] | None = None
        role_id = str(record.get("role") or "")
        if role_id:
            role_result = await conn.execute(
                text('SELECT * FROM "roles" WHERE "id" = :id LIMIT 1'),
                {"id": role_id},
            )
            role = dict(role_result.mappings().first() or {}) or None

        app_role_result = await conn.execute(text('SELECT * FROM "buma_apps_roles"'))
        app_roles = [dict(row) for row in app_role_result.mappings().all()]

    return merge_live_auth_context(auth, record, role, app_roles)


def register_context_middleware(pb: Any) -> None:
    install_rule_context_patch()

    @pb.http_middleware(id="buma_auth_rule_context", priority=100)
    async def auth_rule_context(request, call_next):
        event = RouteRequestEvent(app=request.app, request=request)
        context = await load_live_auth_context(event)
        token = AUTH_RULE_CONTEXT.set(context)
        try:
            request.state.buma_auth_context = context
            return await call_next()
        finally:
            AUTH_RULE_CONTEXT.reset(token)
