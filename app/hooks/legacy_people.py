"""Literal customer.ts HTTP contracts, kept separate from master customer."""

from typing import Any

from fastapi import Depends, Request
from fastapi.encoders import jsonable_encoder
from fastapi.responses import JSONResponse
from ppbase.api.deps import get_optional_auth
from ppbase.db.engine import get_engine

from app.auth.context import AUTH_RULE_CONTEXT
from app.auth.permissions import parse_permission
from app.legacy import customer
from app.legacy.caisse import LegacyNotFoundError, record
from app.legacy.http import NOT_FOUND, UNAUTHORIZED

AUTH = Depends(get_optional_auth)


async def run(request, auth, operation, message):
    if not auth:
        return JSONResponse(status_code=401, content=UNAUTHORIZED)
    try:
        role = await record(get_engine(), "roles", (AUTH_RULE_CONTEXT.get() or {}).get("role"))
        permission = customer.permission(parse_permission(role.get("permission")))
        if permission.get("create") is not True:
            return JSONResponse(status_code=403, content={"message": message})
        data = await request.json() if await request.body() else {}
        result = await operation(data, str(auth.get("id") or ""))
        return JSONResponse(status_code=200, content=jsonable_encoder(result))
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


def register(pb: Any):
    @pb.post("/api2/customer/create")
    async def create(request: Request, auth: dict[str, Any] | None = AUTH):
        return await run(
            request,
            auth,
            lambda data, author: customer.customers(get_engine(), data, author),
            "Your are not allowed",
        )

    @pb.post("/api2/pvd/importSim")
    async def sims(request: Request, auth: dict[str, Any] | None = AUTH):
        return await run(
            request,
            auth,
            lambda data, author: customer.sims(get_engine(), data, author),
            "Vous n'etes pas autoriser",
        )

    @pb.post("/api2/pvd/import")
    async def pdvs(request: Request, auth: dict[str, Any] | None = AUTH):
        return await run(
            request,
            auth,
            lambda data, author: customer.pdvs(get_engine(), data, author),
            "Vous n'etes pas autoriser",
        )

    @pb.get("/api2/customer/importPDVFromAgent/{agentID}/{customerID}")
    async def import_agent(
        agentID: str, customerID: str, request: Request, auth: dict[str, Any] | None = AUTH
    ):
        return await run(
            request,
            auth,
            lambda data, author: customer.import_agent(get_engine(), agentID, customerID, author),
            "Your are not allowed",
        )
