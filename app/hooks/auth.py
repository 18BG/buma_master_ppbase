"""Authentication compatibility routes from the frozen PocketBase source."""

from __future__ import annotations

from typing import Any

from fastapi import Depends
from fastapi.responses import JSONResponse, PlainTextResponse
from ppbase.api.deps import get_optional_auth

from app.auth.context import is_record_auth, register_context_middleware
from app.auth.login import register_login_middleware

AUTH_DEPENDENCY = Depends(get_optional_auth)


async def is_connected(auth: dict[str, Any] | None) -> JSONResponse | dict[str, str]:
    """Return the legacy JSON connection probe response."""
    if not is_record_auth(auth):
        return JSONResponse(status_code=400, content={"message": "false"})
    return {"message": "true"}


async def user_health(auth: dict[str, Any] | None) -> PlainTextResponse:
    """Return the legacy text auth probe response."""
    connected = is_record_auth(auth)
    return PlainTextResponse("true" if connected else "false", status_code=200)


def register(pb: Any) -> None:
    register_context_middleware(pb)
    register_login_middleware(pb)

    @pb.get("/isConnected")
    async def legacy_connection_probe(auth: dict[str, Any] | None = AUTH_DEPENDENCY):
        return await is_connected(auth)

    @pb.get("/master/user/health")
    async def legacy_user_health(auth: dict[str, Any] | None = AUTH_DEPENDENCY):
        return await user_health(auth)
