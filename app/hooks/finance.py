"""Wallet and caisse transaction routes from the frozen BUMA source."""

from __future__ import annotations

from typing import Any

from fastapi import Depends, Request
from fastapi.responses import JSONResponse
from ppbase.api.deps import get_optional_auth
from ppbase.db.engine import get_engine

from app.auth.context import AUTH_RULE_CONTEXT
from app.finance.service import (
    FinanceValidationError,
    caisse_deposit,
    caisse_transfer,
    caisse_withdraw,
    wallet_deposit,
    wallet_transfer,
    wallet_withdraw,
)

AUTH_DEPENDENCY = Depends(get_optional_auth)


def _can(permission: str) -> bool:
    context = AUTH_RULE_CONTEXT.get() or {}
    return bool(
        context.get("role.permission.isAdmin") is True
        or context.get(f"role.permission.{permission}") is True
    )


def _body_id(body: dict[str, Any], key: str) -> str:
    return str(body.get(key) or "")


def register(pb: Any) -> None:
    async def run(request: Request, auth: dict[str, Any] | None, permission: str, operation):
        if not auth:
            return JSONResponse(
                status_code=401, content={"message": "The request requires authentication."}
            )
        if not _can(permission):
            return JSONResponse(status_code=403, content={"message": "Vous n'etes pas autoriser"})
        try:
            body = await request.json()
            result = await operation(body, str(auth.get("id") or ""))
            return JSONResponse(status_code=200, content=result)
        except FinanceValidationError as exc:
            return JSONResponse(status_code=400, content={"message": str(exc)})
        except Exception as exc:
            return JSONResponse(status_code=400, content={"message": str(exc)})

    @pb.post("/master/wallets/deposit")
    async def wallet_deposit_route(request: Request, auth: dict[str, Any] | None = AUTH_DEPENDENCY):
        return await run(
            request,
            auth,
            "wallet.deposit",
            lambda body, author: wallet_deposit(
                get_engine(),
                _body_id(body, "wallet"),
                float(body.get("amount", 0)),
                author,
                str(body.get("note") or ""),
            ),
        )

    @pb.post("/master/wallets/withdraw")
    async def wallet_withdraw_route(
        request: Request, auth: dict[str, Any] | None = AUTH_DEPENDENCY
    ):
        return await run(
            request,
            auth,
            "wallet.withdrawal",
            lambda body, author: wallet_withdraw(
                get_engine(),
                _body_id(body, "wallet"),
                float(body.get("amount", 0)),
                author,
                str(body.get("note") or ""),
            ),
        )

    @pb.post("/master/wallets/transfer")
    async def wallet_transfer_route(
        request: Request, auth: dict[str, Any] | None = AUTH_DEPENDENCY
    ):
        return await run(
            request,
            auth,
            "wallet.transfer",
            lambda body, author: wallet_transfer(
                get_engine(),
                _body_id(body, "from"),
                _body_id(body, "to"),
                float(body.get("amount", 0)),
                author,
                str(body.get("note") or ""),
            ),
        )

    @pb.post("/master/caisse/deposit")
    async def caisse_deposit_route(request: Request, auth: dict[str, Any] | None = AUTH_DEPENDENCY):
        return await run(
            request,
            auth,
            "caisse.transaction",
            lambda body, author: caisse_deposit(
                get_engine(),
                _body_id(body, "caisse"),
                _body_id(body, "wallet"),
                float(body.get("amount", 0)),
                author,
                str(body.get("note") or ""),
            ),
        )

    @pb.post("/master/caisse/withdraw")
    async def caisse_withdraw_route(
        request: Request, auth: dict[str, Any] | None = AUTH_DEPENDENCY
    ):
        return await run(
            request,
            auth,
            "caisse.transaction",
            lambda body, author: caisse_withdraw(
                get_engine(),
                _body_id(body, "caisse"),
                _body_id(body, "wallet"),
                float(body.get("amount", 0)),
                author,
                str(body.get("note") or ""),
            ),
        )

    @pb.post("/master/caisse/transfer")
    async def caisse_transfer_route(
        request: Request, auth: dict[str, Any] | None = AUTH_DEPENDENCY
    ):
        return await run(
            request,
            auth,
            "caisse.transaction",
            lambda body, author: caisse_transfer(
                get_engine(),
                _body_id(body, "caisseFrom"),
                _body_id(body, "caisseTo"),
                _body_id(body, "wallet"),
                float(body.get("amount", 0)),
                author,
                str(body.get("note") or ""),
            ),
        )
