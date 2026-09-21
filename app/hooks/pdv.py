"""PDV/SIM creation route from the frozen BUMA source."""

from __future__ import annotations

from typing import Any

from fastapi import Depends, Request
from fastapi.responses import JSONResponse
from ppbase.api.deps import get_optional_auth
from ppbase.db.engine import get_engine

from app.auth.context import AUTH_RULE_CONTEXT
from app.pdv.service import PdvValidationError, create_sim_with_pdv

AUTH_DEPENDENCY = Depends(get_optional_auth)


def register(pb: Any) -> None:
    @pb.post("/master/pdv/newSim")
    async def new_sim(request: Request, auth: dict[str, Any] | None = AUTH_DEPENDENCY):
        if not auth:
            return JSONResponse(status_code=401, content={"message": "Vous devez être connecté"})
        context = AUTH_RULE_CONTEXT.get() or {}
        if not (
            context.get("role.permission.isAdmin") is True
            or context.get("role.permission.pdv.create") is True
        ):
            return JSONResponse(
                status_code=403, content={"message": "Vous n'êtes pas autorisé à créer des PDV"}
            )
        try:
            body = await request.json()
            result = await create_sim_with_pdv(
                get_engine(),
                str(body.get("simNumber", "")),
                str(auth.get("id") or ""),
                body.get("articleId"),
            )
            return JSONResponse(
                status_code=200, content={"message": "SIM et PDV créés avec succès", "data": result}
            )
        except PdvValidationError as exc:
            return JSONResponse(status_code=400, content={"message": str(exc)})
        except Exception as exc:
            return JSONResponse(status_code=400, content={"message": str(exc)})
