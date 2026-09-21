"""Supplier compatibility route from the frozen PocketBase source."""

from __future__ import annotations

from typing import Any

from fastapi import Depends, Request
from fastapi.responses import JSONResponse
from ppbase.api.deps import get_optional_auth
from ppbase.db.engine import get_engine

from app.auth.context import AUTH_RULE_CONTEXT
from app.suppliers.service import SupplierValidationError, create_suppliers_batch

AUTH_DEPENDENCY = Depends(get_optional_auth)


def _supplier_create_allowed() -> bool:
    context = AUTH_RULE_CONTEXT.get() or {}
    return bool(
        context.get("role.permission.isAdmin") is True
        or context.get("role.permission.supplier.create") is True
    )


def register(pb: Any) -> None:
    @pb.post("/api2/supplier/creates")
    async def create_suppliers_route(
        request: Request,
        auth: dict[str, Any] | None = AUTH_DEPENDENCY,
    ) -> JSONResponse:
        if not auth:
            return JSONResponse(
                status_code=401,
                content={"message": "The request requires authentication."},
            )
        if not _supplier_create_allowed():
            return JSONResponse(status_code=403, content={"message": "Your are not allowed"})
        try:
            body = await request.json()
            suppliers = body.get("datas") if isinstance(body, dict) else None
            await create_suppliers_batch(get_engine(), suppliers, str(auth.get("id") or ""))
            return JSONResponse(status_code=200, content={"message": "ok"})
        except SupplierValidationError as exc:
            return JSONResponse(status_code=400, content={"message": str(exc)})
        except Exception as exc:
            return JSONResponse(status_code=400, content={"message": str(exc)})
