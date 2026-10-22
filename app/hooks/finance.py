"""Wallet and caisse transaction routes from the frozen BUMA source."""

from __future__ import annotations

from typing import Any

from fastapi import Depends, Request
from fastapi.responses import JSONResponse
from ppbase.api.deps import get_optional_auth
from ppbase.db.engine import get_engine

from app.auth.context import AUTH_RULE_CONTEXT
from app.finance.service import (
    FinanceForbiddenError,
    FinanceNotFoundError,
    FinanceValidationError,
    accessible_caisses,
    archive_expense_type,
    caisse_add_correction,
    caisse_add_expense,
    caisse_add_versement,
    caisse_bank_versement,
    caisse_cancel_bank_versement,
    caisse_cancel_transaction,
    caisse_change_cashier,
    caisse_change_transaction_customer,
    caisse_close,
    caisse_deposit,
    caisse_employee_debt,
    caisse_open,
    caisse_request_close,
    caisse_request_open,
    caisse_transfer,
    caisse_unallocate,
    caisse_update_amount,
    caisse_update_note,
    caisse_verify_session,
    caisse_withdraw,
    wallet_cancel_transaction,
    wallet_deposit,
    wallet_transfer,
    wallet_update_note,
    wallet_withdraw,
)
from app.legacy.http import UNAUTHORIZED

AUTH_DEPENDENCY = Depends(get_optional_auth)


def _can(permission: str) -> bool:
    context = AUTH_RULE_CONTEXT.get() or {}
    return bool(
        context.get("role.permission.isAdmin") is True
        or context.get(f"role.permission.{permission}") is True
    )


def _can_any(*permissions: str) -> bool:
    context = AUTH_RULE_CONTEXT.get() or {}
    return bool(
        context.get("role.permission.isAdmin") is True
        or any(context.get(f"role.permission.{permission}") is True for permission in permissions)
    )


def _body_id(body: dict[str, Any], key: str) -> str:
    return str(body.get(key) or "")


def register(pb: Any) -> None:
    async def run(
        request: Request,
        auth: dict[str, Any] | None,
        permission: str | tuple[str, ...] | None,
        operation,
        *,
        forbidden_status: int = 403,
        forbidden_message: str = "Vous n'etes pas autoriser",
    ):
        if not auth:
            return JSONResponse(status_code=401, content=UNAUTHORIZED)
        if permission is not None:
            permissions = (permission,) if isinstance(permission, str) else permission
            if not _can_any(*permissions):
                return JSONResponse(
                    status_code=forbidden_status,
                    content={"message": forbidden_message},
                )
        try:
            body = await request.json()
            result = await operation(body, str(auth.get("id") or ""))
            return JSONResponse(status_code=200, content=result)
        except FinanceForbiddenError as exc:
            return JSONResponse(status_code=403, content={"message": str(exc)})
        except FinanceNotFoundError as exc:
            return JSONResponse(status_code=404, content={"message": str(exc)})
        except FinanceValidationError as exc:
            return JSONResponse(status_code=400, content={"message": str(exc)})
        except Exception as exc:
            return JSONResponse(status_code=400, content={"message": str(exc)})

    @pb.post("/master/wallets/deposit")
    async def wallet_deposit_route(request: Request, auth: dict[str, Any] | None = AUTH_DEPENDENCY):
        if not auth:
            return JSONResponse(
                status_code=401,
                content=UNAUTHORIZED,
            )
        try:
            body = await request.json()
            amount = float(body.get("amount", 0))
        except Exception:
            return JSONResponse(status_code=403, content={"message": "Montant non valide"})
        if amount < 0:
            return JSONResponse(status_code=403, content={"message": "Montant non valide"})
        if not _can_any("wallet.deposit"):
            return JSONResponse(
                status_code=403,
                content={"message": "Vous n'avez pas les droits pour effectuez cette action"},
            )
        try:
            result = await wallet_deposit(
                get_engine(),
                _body_id(body, "wallet"),
                amount,
                str(auth.get("id") or ""),
                body.get("note"),
                str(body.get("file_path") or ""),
            )
            return JSONResponse(status_code=200, content=result)
        except Exception as exc:
            return JSONResponse(status_code=400, content={"message": str(exc)})

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
                body.get("note"),
                str(body.get("file_path") or ""),
            ),
            forbidden_message="Vous n'avez pas les droits pour effectuez cette action",
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

    @pb.post("/master/wallets/cancel_transaction")
    async def wallet_cancel_route(request: Request, auth: dict[str, Any] | None = AUTH_DEPENDENCY):
        return await run(
            request,
            auth,
            "wallet.cancel_transaction",
            lambda body, _author: wallet_cancel_transaction(get_engine(), _body_id(body, "id")),
            forbidden_message="Vous n'etes pas autorisé a effectuer une telle transaction",
        )

    @pb.post("/master/wallets/updateNote")
    async def wallet_update_note_route(
        request: Request, auth: dict[str, Any] | None = AUTH_DEPENDENCY
    ):
        return await run(
            request,
            auth,
            "wallet.change_transaction_note",
            lambda body, _author: wallet_update_note(
                get_engine(), _body_id(body, "transactionId"), body.get("note")
            ),
            forbidden_message=(
                "Vous n'avez pas les permissions pour modifier la note d'une transaction"
            ),
        )

    @pb.post("/master/caisse/deposit")
    async def caisse_deposit_route(request: Request, auth: dict[str, Any] | None = AUTH_DEPENDENCY):
        return await run(
            request,
            auth,
            ("caisse.update", "caisse.manage"),
            lambda body, author: caisse_deposit(
                get_engine(),
                _body_id(body, "caisse"),
                _body_id(body, "wallet"),
                float(body.get("amount", 0)),
                author,
                str(body.get("note") or ""),
                str(body.get("walletNote") or ""),
            ),
            forbidden_message="Vous n'etes pas autoriser a faire sa",
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
                str(body.get("walletNote") or ""),
            ),
            forbidden_status=400,
            forbidden_message="Vous n'avez pas les autorisations",
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
                float(body.get("amount", 0)),
                author,
                str(body.get("note") or ""),
            ),
            forbidden_status=400,
            forbidden_message="Vous n'avez pas les autorisations",
        )

    @pb.post("/master/caisse/requestOpen")
    async def caisse_request_open_route(
        request: Request, auth: dict[str, Any] | None = AUTH_DEPENDENCY
    ):
        return await run(
            request,
            auth,
            ("caisse.transaction", "caisse.manage"),
            lambda body, author: caisse_request_open(
                get_engine(), _body_id(body, "caisse"), author
            ),
        )

    @pb.post("/master/caisse/requestClose")
    async def caisse_request_close_route(
        request: Request, auth: dict[str, Any] | None = AUTH_DEPENDENCY
    ):
        return await run(
            request,
            auth,
            ("caisse.transaction", "caisse.manage"),
            lambda body, author: caisse_request_close(
                get_engine(),
                _body_id(body, "caisse"),
                author,
                is_admin=(AUTH_RULE_CONTEXT.get() or {}).get("role.permission.isAdmin") is True,
            ),
        )

    @pb.post("/master/caisse/openCaisse")
    async def caisse_open_route(request: Request, auth: dict[str, Any] | None = AUTH_DEPENDENCY):
        return await run(
            request,
            auth,
            "caisse.manage",
            lambda body, _author: caisse_open(get_engine(), _body_id(body, "caisse")),
        )

    @pb.post("/master/caisse/closeCaisse")
    async def caisse_close_route(request: Request, auth: dict[str, Any] | None = AUTH_DEPENDENCY):
        return await run(
            request,
            auth,
            "caisse.manage",
            lambda body, author: caisse_close(get_engine(), _body_id(body, "caisse"), author),
        )

    @pb.post("/master/caisse/changeCashier")
    async def caisse_change_cashier_route(
        request: Request, auth: dict[str, Any] | None = AUTH_DEPENDENCY
    ):
        return await run(
            request,
            auth,
            ("caisse.switch_user", "caisse.manage"),
            lambda body, _author: caisse_change_cashier(
                get_engine(), _body_id(body, "caisseId"), _body_id(body, "newCashierId")
            ),
        )

    @pb.post("/master/caisse/cancel")
    async def caisse_cancel_route(request: Request, auth: dict[str, Any] | None = AUTH_DEPENDENCY):
        return await run(
            request,
            auth,
            ("caisse.transaction", "caisse.manage"),
            lambda body, author: caisse_cancel_transaction(
                get_engine(), _body_id(body, "transactionId"), author
            ),
        )

    @pb.get("/master/caisse/accessible")
    async def caisse_accessible_route(
        request: Request, auth: dict[str, Any] | None = AUTH_DEPENDENCY
    ):
        if not auth:
            return JSONResponse(
                status_code=401,
                content=UNAUTHORIZED,
            )
        try:
            return JSONResponse(
                status_code=200,
                content=await accessible_caisses(get_engine(), str(auth.get("id") or "")),
            )
        except Exception as exc:
            return JSONResponse(status_code=400, content={"message": str(exc)})

    @pb.post("/master/caisse/new_correction")
    async def caisse_correction_route(
        request: Request, auth: dict[str, Any] | None = AUTH_DEPENDENCY
    ):
        return await run(
            request,
            auth,
            None,
            lambda body, author: caisse_add_correction(get_engine(), body, author),
        )

    @pb.post("/master/caisse/new_versement")
    async def caisse_versement_route(
        request: Request, auth: dict[str, Any] | None = AUTH_DEPENDENCY
    ):
        return await run(
            request,
            auth,
            ("caisse.transaction", "caisse.manage"),
            lambda body, author: caisse_add_versement(get_engine(), body, author),
            forbidden_message="Vous n'etes pas autoriser a faire sa",
        )

    @pb.post("/master/caisse/expense")
    async def caisse_expense_route(request: Request, auth: dict[str, Any] | None = AUTH_DEPENDENCY):
        return await run(
            request,
            auth,
            "caisse.transaction",
            lambda body, author: caisse_add_expense(get_engine(), body, author),
            forbidden_status=400,
            forbidden_message="Vous n'avez pas les autorisations",
        )

    @pb.post("/master/caisse/bank_versement")
    async def caisse_bank_versement_route(
        request: Request, auth: dict[str, Any] | None = AUTH_DEPENDENCY
    ):
        return await run(
            request,
            auth,
            ("caisse.transaction", "caisse.manage"),
            lambda body, author: caisse_bank_versement(get_engine(), body, author),
        )

    @pb.post("/master/caisse/cancel_bank_versement")
    async def caisse_cancel_bank_versement_route(
        request: Request, auth: dict[str, Any] | None = AUTH_DEPENDENCY
    ):
        return await run(
            request,
            auth,
            ("caisse.transaction", "caisse.manage"),
            lambda body, author: caisse_cancel_bank_versement(
                get_engine(), _body_id(body, "transactionId"), author
            ),
        )

    @pb.post("/master/caisse/unallocate")
    async def caisse_unallocate_route(
        request: Request, auth: dict[str, Any] | None = AUTH_DEPENDENCY
    ):
        return await run(
            request,
            auth,
            "caisse.manage",
            lambda body, _author: caisse_unallocate(
                get_engine(), _body_id(body, "caisseTransactionId")
            ),
        )

    @pb.post("/master/caisse/updateAmount")
    async def caisse_update_amount_route(
        request: Request, auth: dict[str, Any] | None = AUTH_DEPENDENCY
    ):
        return await run(
            request,
            auth,
            ("caisse.cancel_transaction", "caisse.manage"),
            lambda body, _author: caisse_update_amount(
                get_engine(),
                _body_id(body, "caisseTransactionId"),
                float(body.get("amount", 0)),
            ),
        )

    @pb.post("/master/caisse/changeTransactionCustomer")
    async def caisse_change_customer_route(
        request: Request, auth: dict[str, Any] | None = AUTH_DEPENDENCY
    ):
        return await run(
            request,
            auth,
            "caisse.manage",
            lambda body, author: caisse_change_transaction_customer(
                get_engine(),
                _body_id(body, "transactionId"),
                _body_id(body, "newCustomerId"),
                author,
            ),
        )

    @pb.post("/master/caisse/updateNote")
    async def caisse_update_note_route(
        request: Request, auth: dict[str, Any] | None = AUTH_DEPENDENCY
    ):
        return await run(
            request,
            auth,
            ("caisse.change_transaction_note", "caisse.manage"),
            lambda body, _author: caisse_update_note(
                get_engine(), _body_id(body, "transactionId"), body.get("note")
            ),
        )

    @pb.post("/master/caisse/expense_type/archive")
    async def caisse_archive_expense_type_route(
        request: Request, auth: dict[str, Any] | None = AUTH_DEPENDENCY
    ):
        return await run(
            request,
            auth,
            ("caisse.archive_expense_type", "caisse.manage"),
            lambda body, _author: archive_expense_type(get_engine(), _body_id(body, "id")),
        )

    @pb.get("/master/caisse/verifySession/{sessionId}")
    async def caisse_verify_session_route(
        sessionId: str,
        request: Request,
        auth: dict[str, Any] | None = AUTH_DEPENDENCY,
    ):
        if not auth:
            return JSONResponse(
                status_code=401,
                content=UNAUTHORIZED,
            )
        if not _can_any("caisse.manage"):
            return JSONResponse(
                status_code=403,
                content={"message": "Vous n'avez pas les permissions pour vérifier les sessions"},
            )
        try:
            return JSONResponse(
                status_code=200,
                content=await caisse_verify_session(get_engine(), sessionId),
            )
        except FinanceValidationError as exc:
            return JSONResponse(status_code=400, content={"message": str(exc)})

    @pb.post("/master/caisse/employee_debt")
    async def caisse_employee_debt_route(
        request: Request, auth: dict[str, Any] | None = AUTH_DEPENDENCY
    ):
        return await run(
            request,
            auth,
            ("caisse.manage_debt", "caisse.manage"),
            lambda body, author: caisse_employee_debt(get_engine(), body, author),
        )

    @pb.post("/master/caisse/employee_debt_repayment")
    async def caisse_employee_debt_repayment_route(
        request: Request, auth: dict[str, Any] | None = AUTH_DEPENDENCY
    ):
        return await run(
            request,
            auth,
            ("caisse.manage_debt", "caisse.manage"),
            lambda body, author: caisse_employee_debt(get_engine(), body, author, repayment=True),
        )
