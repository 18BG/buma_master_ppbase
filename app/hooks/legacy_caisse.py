"""The ten active /api2/caisse routes from the frozen caisse.ts bootstrap."""

from typing import Any

from fastapi import Depends, Request
from fastapi.encoders import jsonable_encoder
from fastapi.responses import JSONResponse, Response
from ppbase.api.deps import get_optional_auth
from ppbase.db.engine import get_engine

from app.auth.context import AUTH_RULE_CONTEXT
from app.auth.permissions import parse_permission
from app.legacy import caisse
from app.legacy.http import NOT_FOUND, UNAUTHORIZED

AUTH = Depends(get_optional_auth)


async def source_permission():
    context = AUTH_RULE_CONTEXT.get() or {}
    role = await caisse.record(get_engine(), "roles", context.get("role") or "")
    return caisse.permission(parse_permission(role.get("permission")))


async def run(request, auth, operation, *, public=False):
    if not auth and not public:
        return JSONResponse(status_code=401, content=UNAUTHORIZED)
    try:
        role = await source_permission() if auth else {}
        data = await request.json() if await request.body() else {}
        result = await operation(data, str((auth or {}).get("id") or ""), role)
        if result is None:
            return Response(status_code=200)
        status, body = result if isinstance(result, tuple) else (200, result)
        return JSONResponse(status_code=status, content=jsonable_encoder(body))
    except caisse.LegacyNotFoundError:
        return JSONResponse(status_code=404, content=NOT_FOUND)
    except Exception:
        return JSONResponse(
            status_code=400,
            content={
                "code": 400,
                "message": "Something went wrong while processing your request.",
                "data": {},
            },
        )


def register(pb: Any):
    @pb.post("/api2/caisse/openCaisse")
    async def open_caisse(request: Request, auth: dict[str, Any] | None = AUTH):
        return await run(
            request,
            auth,
            lambda data, author, role: caisse.open_caisse(get_engine(), data, author, role),
        )

    @pb.post("/api2/caisse/reject")
    async def reject(request: Request, auth: dict[str, Any] | None = AUTH):
        return await run(
            request,
            auth,
            lambda data, author, role: caisse.reject(get_engine(), data, author, role),
        )

    @pb.post("/api2/caisse/reject_closing")
    async def reject_closing(request: Request, auth: dict[str, Any] | None = AUTH):
        return await run(
            request,
            auth,
            lambda data, author, role: caisse.reject_closing(get_engine(), data, author, role),
        )

    @pb.post("/api2/caisse/withdrawal")
    async def withdrawal(request: Request, auth: dict[str, Any] | None = AUTH):
        return await run(
            request,
            auth,
            lambda data, author, role: caisse.withdrawal(get_engine(), data, author, role),
        )

    @pb.get("/api2/caisse/openAgain/{id}")
    async def reopen(id: str, request: Request, auth: dict[str, Any] | None = AUTH):
        return await run(
            request, auth, lambda data, author, role: caisse.reopen(get_engine(), id, author, role)
        )

    @pb.get("/api2/caisse/report/{id}")
    async def report(id: str, request: Request, auth: dict[str, Any] | None = AUTH):
        return await run(request, auth, lambda *_: caisse.report(get_engine(), id), public=True)

    @pb.get("/api2/caisse/rangeReport/{id}/{start}/{end}")
    async def range_report(
        id: str, start: str, end: str, request: Request, auth: dict[str, Any] | None = AUTH
    ):
        return await run(
            request, auth, lambda *_: caisse.range_report(get_engine(), id, start, end), public=True
        )

    @pb.get("/api2/caisse/reportByExpenseType/{id}/{start}/{end}")
    async def expenses(
        id: str, start: str, end: str, request: Request, auth: dict[str, Any] | None = AUTH
    ):
        return await run(
            request, auth, lambda *_: caisse.expense_report(get_engine(), id, start, end)
        )

    @pb.get("/api2/caisse/transaction/cancel/{id}")
    async def cancel(id: str, request: Request, auth: dict[str, Any] | None = AUTH):
        return await run(
            request, auth, lambda data, author, role: caisse.cancel(get_engine(), id, author, role)
        )

    @pb.get("/api2/caisse/transaction/cancel2/{id}")
    async def cancel2(id: str, request: Request, auth: dict[str, Any] | None = AUTH):
        return await run(
            request,
            auth,
            lambda data, author, role: caisse.cancel_reversal(get_engine(), id, author, role),
        )
