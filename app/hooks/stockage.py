"""Article, stock and stockage routes from the frozen BUMA source."""

from __future__ import annotations

from typing import Any

from fastapi import Depends, Request
from fastapi.responses import JSONResponse
from ppbase.api.deps import get_optional_auth
from ppbase.db.engine import get_engine

from app.auth.context import AUTH_RULE_CONTEXT
from app.stockage.service import (
    StockageValidationError,
    cancel_stock_conversion,
    cancel_stockage_transfer,
    convert_stock,
    create_articles_batch,
    make_batch_stock_movements,
    make_stock_movement,
    make_stockage_transfer,
    uv_to_wallet,
    wallet_to_uv,
)

AUTH_DEPENDENCY = Depends(get_optional_auth)


def _allowed(auth: dict[str, Any] | None, permission: str) -> bool:
    if not auth:
        return False
    context = AUTH_RULE_CONTEXT.get() or {}
    return bool(
        context.get("role.permission.isAdmin") is True
        or context.get(f"role.permission.{permission}") is True
    )


def _body_id(body: dict[str, Any], key: str) -> str:
    return str(body.get(key) or "")


def register(pb: Any) -> None:
    async def guard(request: Request, auth: dict[str, Any] | None, permission: str):
        if not auth:
            return None, JSONResponse(
                status_code=401, content={"message": "The request requires authentication."}
            )
        if not _allowed(auth, permission):
            return None, JSONResponse(
                status_code=403, content={"message": "Vous n'avez pas les permissions necessaires"}
            )
        try:
            body = await request.json()
        except Exception:
            body = {}
        return body, None

    @pb.post("/master/article/creates")
    async def create_articles(request: Request, auth: dict[str, Any] | None = AUTH_DEPENDENCY):
        body, error = await guard(request, auth, "article.create")
        if error:
            return error
        try:
            await create_articles_batch(
                get_engine(), body.get("articles"), str(auth.get("id") or "")
            )
            return JSONResponse(status_code=200, content={"message": "Operation effectuee"})
        except StockageValidationError as exc:
            return JSONResponse(status_code=400, content={"message": str(exc)})
        except Exception as exc:
            return JSONResponse(status_code=400, content={"message": str(exc)})

    @pb.post("/master/stockage/movement")
    async def stock_movement(request: Request, auth: dict[str, Any] | None = AUTH_DEPENDENCY):
        body, error = await guard(request, auth, "stockage.input_output")
        if error:
            return error
        try:
            result = await make_stock_movement(get_engine(), str(auth.get("id") or ""), body)
            return JSONResponse(
                status_code=200,
                content={
                    "message": "Entrée de stock effectuée avec succès"
                    if body.get("isEntry")
                    else "Sortie de stock effectuée avec succès",
                    "transaction": result["id"],
                    "type": "INPUT" if body.get("isEntry") else "OUTPUT",
                },
            )
        except StockageValidationError as exc:
            return JSONResponse(status_code=400, content={"message": str(exc)})
        except Exception as exc:
            return JSONResponse(status_code=400, content={"message": str(exc)})

    @pb.post("/master/stockage/batch-movement")
    async def batch_stock_movement(request: Request, auth: dict[str, Any] | None = AUTH_DEPENDENCY):
        body, error = await guard(request, auth, "stockage.input_output")
        if error:
            return error
        try:
            results = await make_batch_stock_movements(
                get_engine(), str(auth.get("id") or ""), body.get("movements")
            )
            return JSONResponse(
                status_code=200,
                content={
                    "message": f"{len(results)} mouvements de stock effectués avec succès",
                    "transactions": [
                        {"id": item["id"], "type": item.get("type")} for item in results
                    ],
                },
            )
        except StockageValidationError as exc:
            return JSONResponse(status_code=400, content={"message": str(exc)})
        except Exception as exc:
            return JSONResponse(status_code=400, content={"message": str(exc)})

    @pb.post("/master/stockage/transfer")
    async def stockage_transfer(request: Request, auth: dict[str, Any] | None = AUTH_DEPENDENCY):
        body, error = await guard(request, auth, "stockage.transfer")
        if error:
            return error
        try:
            result = await make_stockage_transfer(get_engine(), str(auth.get("id") or ""), body)
            return JSONResponse(status_code=200, content=result)
        except StockageValidationError as exc:
            return JSONResponse(status_code=400, content={"message": str(exc)})
        except Exception as exc:
            return JSONResponse(status_code=400, content={"message": str(exc)})

    @pb.post("/master/stockage/cancel_transfer")
    async def cancel_transfer(request: Request, auth: dict[str, Any] | None = AUTH_DEPENDENCY):
        body, error = await guard(request, auth, "stockage.cancel_transfer")
        if error:
            return error
        try:
            return JSONResponse(
                status_code=200,
                content=await cancel_stockage_transfer(get_engine(), _body_id(body, "id")),
            )
        except StockageValidationError as exc:
            return JSONResponse(status_code=400, content={"message": str(exc)})
        except Exception as exc:
            return JSONResponse(status_code=400, content={"message": str(exc)})

    @pb.post("/master/stockage/convert")
    async def stockage_convert(request: Request, auth: dict[str, Any] | None = AUTH_DEPENDENCY):
        body, error = await guard(request, auth, "stockage.conversion")
        if error:
            return error
        try:
            conversion_id = await convert_stock(get_engine(), str(auth.get("id") or ""), body)
            return JSONResponse(
                status_code=200,
                content={"id": conversion_id, "message": "Conversion éffectuée"},
            )
        except StockageValidationError as exc:
            return JSONResponse(status_code=400, content={"message": str(exc)})
        except Exception as exc:
            return JSONResponse(status_code=400, content={"message": str(exc)})

    @pb.post("/master/stockage/cancel_conversion")
    async def stockage_cancel_conversion(
        request: Request, auth: dict[str, Any] | None = AUTH_DEPENDENCY
    ):
        body, error = await guard(request, auth, "stockage.cancel_conversion")
        if error:
            return error
        try:
            result = await cancel_stock_conversion(
                get_engine(), _body_id(body, "conversionId"), str(auth.get("id") or "")
            )
            return JSONResponse(status_code=200, content=result)
        except StockageValidationError as exc:
            return JSONResponse(status_code=400, content={"message": str(exc)})
        except Exception as exc:
            return JSONResponse(status_code=400, content={"message": str(exc)})

    @pb.post("/master/stockage/wallet_uv")
    async def stockage_wallet_uv(request: Request, auth: dict[str, Any] | None = AUTH_DEPENDENCY):
        body, error = await guard(request, auth, "wallet.wallet_uv")
        if error:
            return error
        try:
            result = await wallet_to_uv(get_engine(), str(auth.get("id") or ""), body)
            return JSONResponse(status_code=200, content=result)
        except StockageValidationError as exc:
            return JSONResponse(status_code=400, content={"message": str(exc)})
        except Exception as exc:
            return JSONResponse(status_code=400, content={"message": str(exc)})

    @pb.post("/master/stockage/uv_wallet")
    async def stockage_uv_wallet(request: Request, auth: dict[str, Any] | None = AUTH_DEPENDENCY):
        body, error = await guard(request, auth, "wallet.uv_wallet")
        if error:
            return error
        try:
            result = await uv_to_wallet(get_engine(), str(auth.get("id") or ""), body)
            return JSONResponse(status_code=200, content=result)
        except StockageValidationError as exc:
            return JSONResponse(status_code=400, content={"message": str(exc)})
        except Exception as exc:
            return JSONResponse(status_code=400, content={"message": str(exc)})
