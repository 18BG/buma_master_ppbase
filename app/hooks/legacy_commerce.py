"""Nine active legacy commerce routes with their distinct error boundaries."""

from typing import Any

from fastapi import Depends, Request
from fastapi.encoders import jsonable_encoder
from fastapi.responses import JSONResponse, Response
from ppbase.api.deps import get_optional_auth
from ppbase.db.engine import get_engine
from sqlalchemy.exc import SQLAlchemyError

from app.auth.context import AUTH_RULE_CONTEXT
from app.auth.permissions import parse_permission
from app.legacy import commerce
from app.legacy.caisse import LegacyNotFoundError, record
from app.legacy.http import NOT_FOUND, UNAUTHORIZED

AUTH = Depends(get_optional_auth)


async def run(request, auth, domain, operation, failure="json500"):
    if not auth:
        return JSONResponse(status_code=401, content=UNAUTHORIZED)
    try:
        raw = await record(get_engine(), "roles", (AUTH_RULE_CONTEXT.get() or {}).get("role"))
        role = commerce.permission(parse_permission(raw.get("permission")), domain)
        data = await request.json() if await request.body() else {}
    except LegacyNotFoundError:
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
    try:
        result = await operation(data, str(auth.get("id") or ""), role)
        status, body = result if isinstance(result, tuple) else (200, result)
        return JSONResponse(status_code=status, content=jsonable_encoder(body))
    except Exception as exc:
        if failure == "empty":
            return Response(status_code=200)
        if failure == "uncaught":
            if isinstance(exc, LegacyNotFoundError):
                return JSONResponse(status_code=404, content=NOT_FOUND)
            return JSONResponse(
                status_code=400,
                content={
                    "code": 400,
                    "message": "Something went wrong while processing your request.",
                    "data": {},
                },
            )
        # A caught Go DAO error is exposed by Goja as {value:{}}. Native
        # JavaScript errors (e.g. missing data.startDate.replace) serialize {}.
        return JSONResponse(
            status_code=500,
            content={
                "message": {"value": {}}
                if isinstance(exc, (LegacyNotFoundError, SQLAlchemyError))
                else {}
            },
        )


def register(pb: Any):
    @pb.post("/api2/purchase/add")
    async def purchase(request: Request, auth: dict[str, Any] | None = AUTH):
        return await run(
            request,
            auth,
            "purchase",
            lambda data, author, role: commerce.purchase(get_engine(), data, author, role),
        )

    @pb.post("/api2/purchase/cancel")
    async def cancel_purchase(request: Request, auth: dict[str, Any] | None = AUTH):
        return await run(
            request,
            auth,
            "purchase",
            lambda data, author, role: commerce.cancel_purchase(get_engine(), data, author, role),
        )

    @pb.post("/api2/purchase/infos")
    async def purchase_info(request: Request, auth: dict[str, Any] | None = AUTH):
        return await run(
            request,
            auth,
            "purchase",
            lambda data, *_: commerce.infos(get_engine(), data, purchase=True),
        )

    @pb.post("/api2/purchase/payment/add")
    async def purchase_payment(request: Request, auth: dict[str, Any] | None = AUTH):
        return await run(
            request,
            auth,
            "purchase",
            lambda data, author, role: commerce.payment(
                get_engine(), data, author, role, purchase=True
            ),
            "empty",
        )

    @pb.post("/api2/sale/cancel")
    async def cancel_sale(request: Request, auth: dict[str, Any] | None = AUTH):
        return await run(
            request,
            auth,
            "sale",
            lambda data, author, role: commerce.cancel_sale(get_engine(), data, author, role),
        )

    @pb.post("/api2/sale/infos")
    async def sale_info(request: Request, auth: dict[str, Any] | None = AUTH):
        return await run(request, auth, "sale", lambda data, *_: commerce.infos(get_engine(), data))

    @pb.post("/api2/sale/payment/add")
    async def sale_payment(request: Request, auth: dict[str, Any] | None = AUTH):
        return await run(
            request,
            auth,
            "sale",
            lambda data, author, role: commerce.payment(get_engine(), data, author, role),
            "uncaught",
        )

    @pb.post("/api2/return")
    async def make_return(request: Request, auth: dict[str, Any] | None = AUTH):
        return await run(
            request,
            auth,
            "sale",
            lambda data, author, role: commerce.make_return(get_engine(), data, author, role),
        )

    @pb.post("/api2/return/confirm")
    async def confirm_return(request: Request, auth: dict[str, Any] | None = AUTH):
        return await run(
            request,
            auth,
            "sale",
            lambda data, author, role: commerce.confirm_return(get_engine(), data, author, role),
            "uncaught",
        )
