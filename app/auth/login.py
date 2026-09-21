"""Username/email login compatibility for the two BUMA auth collections."""

from __future__ import annotations

from collections.abc import Awaitable, Callable
from typing import Any

from fastapi import Request
from fastapi.responses import JSONResponse, Response
from ppbase.api.record_auth import (
    _AUTH_INVALID_CREDENTIALS_DATA,
    _apply_fields_filter,
    _build_rule_request_context,
    _error_response,
    _passes_auth_rule,
    _require_auth_collection,
    _trigger_record_auth_request_hook,
    _trigger_record_auth_success_hook,
)
from ppbase.db.engine import get_engine
from ppbase.ext.events import RecordAuthRequestEvent
from ppbase.ext.registry import HOOK_RECORD_AUTH_WITH_PASSWORD_REQUEST
from ppbase.services.expand_service import expand_records
from ppbase.services.record_auth_service import auth_with_password
from ppbase.services.record_service import get_all_collections, resolve_collection

AUTH_IDENTITY_FIELDS: dict[str, tuple[str, ...]] = {
    "users": ("username",),
    "agent_users": ("email", "username"),
}


async def _authenticate_legacy_collection(
    request: Request,
    collection_name: str,
) -> JSONResponse:
    engine = get_engine()
    collection = await resolve_collection(engine, collection_name)
    if collection is None:
        return _error_response(404, "Missing collection context.")

    auth_error = _require_auth_collection(collection)
    if auth_error is not None:
        return auth_error

    try:
        body = await request.json()
    except Exception:
        return _error_response(400, "Invalid JSON body.")
    if not isinstance(body, dict):
        return _error_response(400, "Request body must be a JSON object.")

    event = RecordAuthRequestEvent(
        app=request.app,
        request=request,
        collection=collection,
        collection_id_or_name=collection_name,
        method="password",
        auth_method="password",
        body=body,
        identity=str(body.get("identity", "") or ""),
        identity_field=str(body.get("identityField", "") or "") or None,
        password=str(body.get("password", "") or ""),
    )

    async def default_handler(current: RecordAuthRequestEvent) -> JSONResponse:
        identity = str(current.identity or "").strip()
        password = str(current.password or "")
        identity_field = current.identity_field
        if not identity or not password:
            return _error_response(
                400,
                "Failed to authenticate.",
                {
                    "identity": {
                        "code": "validation_required",
                        "message": "Cannot be blank.",
                    }
                    if not identity
                    else {},
                    "password": {
                        "code": "validation_required",
                        "message": "Cannot be blank.",
                    }
                    if not password
                    else {},
                },
            )

        candidates = (
            (identity_field,)
            if identity_field
            else AUTH_IDENTITY_FIELDS.get(collection_name, ("email",))
        )
        result: dict[str, Any] | None = None
        for field in candidates:
            result = await auth_with_password(
                engine,
                collection,
                identity,
                password,
                request.app.state.settings,
                identity_field=field,
            )
            if result is not None:
                break

        if result is None:
            return _error_response(
                400,
                "Failed to authenticate.",
                _AUTH_INVALID_CREDENTIALS_DATA,
            )

        rule_context = _build_rule_request_context(
            request,
            context="password",
            data=body,
        )
        if not await _passes_auth_rule(
            engine,
            collection,
            result["record"]["id"],
            rule_context,
        ):
            return _error_response(
                400,
                "Failed to authenticate.",
                _AUTH_INVALID_CREDENTIALS_DATA,
            )

        expand_value = request.query_params.get("expand", "")
        if expand_value and result.get("record"):
            records = await expand_records(
                engine,
                collection,
                [result["record"]],
                expand_value,
                await get_all_collections(engine),
            )
            result["record"] = records[0] if records else result["record"]

        fields = request.query_params.get("fields", "")
        if fields and result.get("record"):
            result["record"] = _apply_fields_filter(result["record"], fields)

        hooked = await _trigger_record_auth_success_hook(request, current, result)
        return JSONResponse(content=hooked)

    return await _trigger_record_auth_request_hook(
        request,
        HOOK_RECORD_AUTH_WITH_PASSWORD_REQUEST,
        event,
        default_handler,
    )


def register_login_middleware(pb: Any) -> None:
    @pb.http_middleware(
        id="buma_auth_username_login",
        methods=["POST"],
        path="/api/collections/*/auth-with-password",
    )
    async def username_login(
        request: Request,
        call_next: Callable[[], Awaitable[Response]],
    ) -> Response:
        parts = request.url.path.strip("/").split("/")
        collection_name = parts[2] if len(parts) == 4 else ""
        if collection_name not in AUTH_IDENTITY_FIELDS:
            return await call_next()
        return await _authenticate_legacy_collection(request, collection_name)
