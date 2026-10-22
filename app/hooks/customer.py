"""Customer routes and record-create numbering compatibility."""

from __future__ import annotations

from typing import Any

from fastapi import Depends, Request
from fastapi.responses import JSONResponse
from ppbase.api.deps import get_optional_auth
from ppbase.db.engine import get_engine
from sqlalchemy import text

from app.auth.context import AUTH_RULE_CONTEXT
from app.customers.service import CustomerValidationError, create_customers_batch
from app.legacy.http import UNAUTHORIZED

AUTH_DEPENDENCY = Depends(get_optional_auth)


def _customer_create_allowed() -> bool:
    context = AUTH_RULE_CONTEXT.get() or {}
    return bool(
        context.get("role.permission.isAdmin") is True
        or context.get("role.permission.customer.create") is True
    )


def register(pb: Any) -> None:
    @pb.post("/master/customer/creates")
    async def create_customers_route(
        request: Request,
        auth: dict[str, Any] | None = AUTH_DEPENDENCY,
    ) -> JSONResponse:
        if not auth:
            return JSONResponse(
                status_code=401,
                content=UNAUTHORIZED,
            )
        if not _customer_create_allowed():
            return JSONResponse(
                status_code=403,
                content={"message": "Vous n'etes pas autoriser"},
            )
        try:
            body = await request.json()
            customers = body.get("customers") if isinstance(body, dict) else None
            await create_customers_batch(
                get_engine(),
                customers,
                str(auth.get("id") or ""),
            )
            return JSONResponse(
                status_code=200,
                content={"message": "Operation effectuee"},
            )
        except CustomerValidationError as exc:
            return JSONResponse(status_code=400, content={"message": str(exc)})
        except Exception as exc:
            return JSONResponse(status_code=400, content={"message": str(exc)})

    @pb.on_record_create_request("buma_customer", id="buma_customer_no")
    async def assign_customer_number(event: Any) -> Any:
        if event.data.get("customer_no") not in (None, "", 0):
            return await event.next()
        async with event.engine.connect() as connection:
            await connection.execute(
                text("SELECT pg_advisory_xact_lock(hashtextextended(:key, 0))"),
                {"key": "buma:customer_no"},
            )
            last_number = (
                await connection.execute(
                    text('SELECT COALESCE(MAX("customer_no"), 0) FROM "buma_customer"')
                )
            ).scalar_one()
        event.data["customer_no"] = int(last_number) + 1
        return await event.next()
