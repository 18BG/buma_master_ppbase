"""Route/permission behavior of the legacy stockage.ts entrypoint."""

from __future__ import annotations

from typing import Any

from fastapi import Depends, Request
from fastapi.encoders import jsonable_encoder
from fastapi.responses import JSONResponse, Response
from ppbase.api.deps import get_optional_auth
from ppbase.db.engine import get_engine
from sqlalchemy import text

from app.auth.context import AUTH_RULE_CONTEXT
from app.auth.permissions import parse_permission
from app.legacy import stockage

AUTH = Depends(get_optional_auth)


async def source_permission():
    context = AUTH_RULE_CONTEXT.get() or {}
    async with get_engine().connect() as connection:
        raw = (
            await connection.execute(
                text("SELECT permission FROM roles WHERE id=:id"), {"id": context.get("role") or ""}
            )
        ).scalar_one_or_none()
    raw = parse_permission(raw)
    values = raw.get("stockage") if isinstance(raw.get("stockage"), dict) else {}
    if raw.get("isAdmin") or values.get("canManage") is True:
        return dict.fromkeys(
            ("canManage", "read", "update", "create", "remove", "canConvert"), True
        )
    return {
        "canManage": False,
        "read": values.get("read") is True,
        "update": values.get("update"),
        "create": values.get("create") is True,
        "remove": values.get("remove") is True,
        "canConvert": values.get("canConvert") is True,
    }


def register(pb: Any):
    async def run(request, auth, operation, guard=None):
        if not auth:
            return JSONResponse(
                status_code=401, content={"message": "The request requires authentication."}
            )
        try:
            permissions = await source_permission()
            if guard == "update" and permissions.get("update") is False:
                return JSONResponse(status_code=401, content={"message": "Unauthorized"})
            if guard == "convert" and not (
                permissions.get("canManage") and permissions.get("canConvert")
            ):
                return JSONResponse(
                    status_code=403, content={"message": "Not allow to convert stock"}
                )
            data = await request.json() if await request.body() else {}
            result = await operation(data, str(auth.get("id") or ""))
            if result is None:
                return Response(status_code=200)
            if isinstance(result, tuple):
                return JSONResponse(status_code=result[0], content=jsonable_encoder(result[1]))
            return JSONResponse(status_code=200, content=jsonable_encoder(result))
        except Exception:
            return JSONResponse(
                status_code=400,
                content={
                    "code": 400,
                    "message": "Something went wrong while processing your request.",
                    "data": {},
                },
            )

    @pb.post("/api2/stockage/transfer")
    async def transfer(request: Request, auth: dict[str, Any] | None = AUTH):
        return await run(
            request, auth, lambda data, author: stockage.transfer(get_engine(), data, author)
        )

    @pb.get("/api2/stockage/stock_info/{name}")
    async def info(name: str, request: Request, auth: dict[str, Any] | None = AUTH):
        return await run(request, auth, lambda data, author: stockage.stock_info(get_engine()))

    @pb.post("/api2/stockage/add_article")
    async def add(request: Request, auth: dict[str, Any] | None = AUTH):
        return await run(
            request,
            auth,
            lambda data, author: stockage.add_articles(get_engine(), data, author),
            guard="update",
        )

    @pb.post("/api2/stockage/remove_article")
    async def remove(request: Request, auth: dict[str, Any] | None = AUTH):
        return await run(
            request, auth, lambda data, author: stockage.remove_articles(get_engine(), data)
        )

    @pb.post("/api2/stockage/convert")
    async def convert(request: Request, auth: dict[str, Any] | None = AUTH):
        return await run(
            request,
            auth,
            lambda data, author: stockage.convert(get_engine(), data, author),
            guard="convert",
        )
