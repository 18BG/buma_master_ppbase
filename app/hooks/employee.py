"""Employee creation routes from the frozen BUMA source."""

from __future__ import annotations

from typing import Any

from fastapi import Depends, Request
from fastapi.responses import JSONResponse
from ppbase.api.deps import get_optional_auth
from ppbase.db.engine import get_engine

from app.auth.context import AUTH_RULE_CONTEXT
from app.employees.service import EmployeeValidationError, create_employees_batch

AUTH_DEPENDENCY = Depends(get_optional_auth)


def _allowed() -> bool:
    context = AUTH_RULE_CONTEXT.get() or {}
    return bool(
        context.get("role.permission.isAdmin") is True
        or context.get("role.permission.employee.create") is True
    )


def register(pb: Any) -> None:
    async def handler(request: Request, auth: dict[str, Any] | None) -> JSONResponse:
        if not auth:
            return JSONResponse(
                status_code=401, content={"message": "The request requires authentication."}
            )
        if not _allowed():
            return JSONResponse(
                status_code=403,
                content={"message": "Vous n'êtes pas autorisé à créer des employés"},
            )
        try:
            body = await request.json()
            accounts = body.get("accounts") if isinstance(body, dict) else None
            await create_employees_batch(get_engine(), accounts, str(auth.get("id") or ""))
            return JSONResponse(status_code=200, content={"message": "ok"})
        except EmployeeValidationError as exc:
            return JSONResponse(status_code=400, content={"message": str(exc)})
        except Exception as exc:
            return JSONResponse(status_code=400, content={"message": str(exc)})

    @pb.post("/master/employee/create")
    async def master_employee_create(
        request: Request, auth: dict[str, Any] | None = AUTH_DEPENDENCY
    ):
        return await handler(request, auth)

    @pb.post("/api2/employee/create")
    async def legacy_employee_create(
        request: Request, auth: dict[str, Any] | None = AUTH_DEPENDENCY
    ):
        return await handler(request, auth)
