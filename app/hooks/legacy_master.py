"""Master PDV, pilotage and rupture route compatibility."""

from typing import Any

from fastapi import Depends, Request
from fastapi.responses import JSONResponse, Response
from ppbase.api.deps import get_optional_auth
from ppbase.db.engine import get_engine

from app.auth.context import AUTH_RULE_CONTEXT, is_record_auth
from app.legacy import master
from app.legacy.http import UNAUTHORIZED

AUTH = Depends(get_optional_auth)


def reply(result):
    status, body = result if isinstance(result, tuple) else (200, result)
    return JSONResponse(status_code=status, content=body)


def register(pb: Any):
    @pb.post("/master/pdv/turnovers")
    async def turnovers(request: Request, auth: dict[str, Any] | None = AUTH):
        if not auth:
            return reply((401, UNAUTHORIZED))
        try:
            permission = await master.pdv_permission(
                get_engine(), (AUTH_RULE_CONTEXT.get() or {}).get("role")
            )
            if not permission.get("read"):
                return reply(
                    (403, {"message": "Vous n'êtes pas autorisé à voir les chiffres d'affaires"})
                )
            data = await request.json() if await request.body() else {}
            result = await master.turnovers(get_engine(), data)
            return reply({"message": "Chiffres d'affaires récupérés avec succès", **result})
        except Exception as exc:
            return reply((400, {"message": str(exc)}))

    async def assignment(request, auth, remove=False):
        if not is_record_auth(auth):
            return reply((401, {"message": "Vous devez être connecté pour effectuer cette action"}))
        agent = (AUTH_RULE_CONTEXT.get() or {}).get("agent")
        if not agent:
            return reply((403, {"message": "Vous n'êtes pas associé à un agent"}))
        try:
            data = await request.json() if await request.body() else {}
            if not data.get("pdv_id"):
                return reply((400, {"message": "ID du point de vente manquant"}))
            result = await master.assign(get_engine(), data["pdv_id"], agent, remove=remove)
            return reply(
                {
                    "message": "PDV dissocié avec succès" if remove else "PDV assigné avec succès",
                    "data": result,
                }
            )
        except Exception as exc:
            return reply((400, {"message": str(exc)}))

    @pb.post("/master/pdv/assign")
    async def assign(request: Request, auth: dict[str, Any] | None = AUTH):
        return await assignment(request, auth)

    @pb.post("/master/pdv/unassign")
    async def unassign(request: Request, auth: dict[str, Any] | None = AUTH):
        return await assignment(request, auth, True)

    @pb.get("/master/pilotage/date-report/{startDate}")
    async def pilotage(startDate: str):
        # No source middleware sets userInfo. Its async rejection occurs before
        # the config lookup and external calls; PB 0.22 returns an empty 200.
        return Response(status_code=200)

    @pb.post("/save-rupture")
    async def rupture(request: Request):
        try:
            data = await request.json() if await request.body() else {}
            return reply(await master.rupture(get_engine(), data))
        except Exception as exc:
            return reply((400, {"message": "Erreur lors de la sauvegarde: " + str(exc)}))
