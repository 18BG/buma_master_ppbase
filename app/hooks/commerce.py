"""C9 sale, recharge, purchase and return routes."""

from __future__ import annotations

from typing import Any

from fastapi import Depends, Request
from fastapi.encoders import jsonable_encoder
from fastapi.responses import JSONResponse, Response
from ppbase.api.deps import get_optional_auth
from ppbase.db.engine import get_engine
from sqlalchemy import text

from app.auth.context import AUTH_RULE_CONTEXT
from app.auth.permissions import parse_permission
from app.commerce.reports import (
    caisse_report,
    check_sale_caisse,
    customer_products,
    customer_summary,
    customers_products,
    product_customers,
    sold_products,
    transaction_stats,
)
from app.commerce.returns import (
    cancel_return_checked,
    new_return,
    return_info,
    update_return,
    update_return_pdv,
)
from app.commerce.service import (
    CommerceForbiddenError,
    CommerceNotFoundError,
    CommerceValidationError,
    add_payment_to_sale,
    add_purchase_payment,
    caisse_add_return,
    cancel_purchase,
    cancel_purchase_wallet_transaction,
    cancel_recharge,
    cancel_sale,
    create_purchase,
    create_recharge,
    create_sale,
    receive_purchase,
    versement_uv,
)
from app.legacy.http import UNAUTHORIZED

AUTH_DEPENDENCY = Depends(get_optional_auth)


def _allowed(*permissions: str) -> bool:
    context = AUTH_RULE_CONTEXT.get() or {}
    return bool(
        context.get("role.permission.isAdmin") is True
        or any(context.get(f"role.permission.{permission}") is True for permission in permissions)
    )


def _id(body: dict[str, Any], key: str) -> str:
    return str(body.get(key) or "")


async def _explicitly_denied(permission: str) -> bool:
    """The two source guards using === false must distinguish absent flags."""
    context = AUTH_RULE_CONTEXT.get() or {}
    if context.get("role.permission.isAdmin") is True:
        return False
    async with get_engine().connect() as connection:
        value = (
            await connection.execute(
                text("SELECT permission FROM roles WHERE id=:id"),
                {"id": context.get("role") or ""},
            )
        ).scalar_one_or_none()
    domain, action = permission.split(".", 1)
    selected = parse_permission(value).get(domain)
    return isinstance(selected, dict) and selected.get(action) is False


def register(pb: Any) -> None:
    async def run(
        request: Request,
        auth: dict[str, Any] | None,
        permissions,
        operation,
        *,
        forbidden_status=403,
        forbidden_message="Vous n'avez pas les droits",
        error_prefix="",
        deny_only_false=None,
    ):
        if not auth:
            return JSONResponse(status_code=401, content=UNAUTHORIZED)
        denied = (
            (await _explicitly_denied(deny_only_false))
            if deny_only_false
            else (permissions is not None and not _allowed(*permissions))
        )
        if denied:
            return JSONResponse(
                status_code=forbidden_status, content={"message": forbidden_message}
            )
        try:
            body = await request.json() if await request.body() else {}
            result = await operation(body, str(auth.get("id") or ""))
            if result is None:
                return Response(status_code=200)
            return JSONResponse(status_code=200, content=jsonable_encoder(result))
        except CommerceForbiddenError as exc:
            return JSONResponse(status_code=403, content={"message": str(exc)})
        except CommerceNotFoundError as exc:
            return JSONResponse(status_code=404, content={"message": str(exc)})
        except Exception as exc:
            return JSONResponse(status_code=400, content={"message": error_prefix + str(exc)})

    async def public_report(request: Request, operation):
        try:
            body = await request.json() if await request.body() else {}
            return JSONResponse(status_code=200, content=jsonable_encoder(await operation(body)))
        except Exception as exc:
            return JSONResponse(status_code=400, content={"message": str(exc)})

    @pb.post("/master/sale/new")
    async def sale_new(request: Request, auth: dict[str, Any] | None = AUTH_DEPENDENCY):
        async def operation(body, author):
            await create_sale(
                get_engine(),
                body,
                author,
                allow_sale_date=_allowed("sale.sale_date"),
            )
            return {"message": "Operation effectuee"}

        return await run(
            request,
            auth,
            ("sale.create",),
            operation,
            forbidden_message="Vous étes pas authorisé a effectuer une vente",
        )

    @pb.post("/master/sale/recharge")
    async def sale_recharge(request: Request, auth: dict[str, Any] | None = AUTH_DEPENDENCY):
        return await run(
            request,
            auth,
            ("sale.create",),
            lambda body, author: _recharge_response(body, author),
            forbidden_message="Vous n'avez pas les permissions necessaires",
        )

    @pb.post("/master/sale/cancel_recharge")
    async def sale_cancel_recharge(request: Request, auth: dict[str, Any] | None = AUTH_DEPENDENCY):
        return await run(
            request,
            auth,
            ("sale.remove",),
            lambda body, _author: cancel_recharge(get_engine(), _id(body, "sale_id")),
            forbidden_message="Vous n'avez pas les permissions necessaires",
            error_prefix="Impossible d'annuler le rechargement, ",
        )

    @pb.post("/master/sale/cancel")
    async def sale_cancel(request: Request, auth: dict[str, Any] | None = AUTH_DEPENDENCY):
        return await run(
            request,
            auth,
            ("sale.remove",),
            lambda body, _author: cancel_sale(get_engine(), _id(body, "saleId")),
            forbidden_message="Vous n'etes pas autorisé",
        )

    @pb.post("/master/purchase/new")
    async def purchase_new(request: Request, auth: dict[str, Any] | None = AUTH_DEPENDENCY):
        return await run(
            request,
            auth,
            ("purchase.create",),
            lambda body, author: _purchase_response(body, author),
            forbidden_message="Vous n'avez pas les droits pour créer un achat",
        )

    @pb.post("/master/purchase/cancel")
    async def purchase_cancel(request: Request, auth: dict[str, Any] | None = AUTH_DEPENDENCY):
        return await run(
            request,
            auth,
            ("purchase.remove",),
            lambda body, author: _message_response(
                cancel_purchase(get_engine(), _id(body, "buyId"), author)
            ),
        )

    @pb.post("/master/purchase/receive")
    async def purchase_receive(request: Request, auth: dict[str, Any] | None = AUTH_DEPENDENCY):
        async def operation(body, author):
            if not body.get("buyId"):
                raise CommerceValidationError("ID d'achat non spécifié")
            return {"message": await receive_purchase(get_engine(), _id(body, "buyId"), author)}

        return await run(
            request,
            auth,
            ("purchase.update",),
            operation,
            forbidden_message="Vous n'avez pas les droits pour mettre à jour un achat",
        )

    @pb.post("/master/purchase/add-payment")
    async def purchase_add_payment(request: Request, auth: dict[str, Any] | None = AUTH_DEPENDENCY):
        return await run(
            request,
            auth,
            ("wallet.transaction",),
            lambda body, author: add_purchase_payment(get_engine(), body, author),
            forbidden_message="Vous n'avez pas les droits pour effectuer cette opération",
        )

    @pb.post("/master/purchase/cancel-wallet-transaction")
    async def purchase_cancel_wallet(
        request: Request, auth: dict[str, Any] | None = AUTH_DEPENDENCY
    ):
        async def operation(body, author):
            if not body.get("id"):
                raise CommerceValidationError("ID de transaction non spécifié")
            message = await cancel_purchase_wallet_transaction(
                get_engine(), _id(body, "id"), author
            )
            return {"message": message}

        return await run(
            request,
            auth,
            ("purchase.remove",),
            operation,
            forbidden_message="Vous n'avez pas les droits pour annuler une transaction",
        )

    @pb.post("/master/retour/new")
    async def retour_new(request: Request, auth: dict[str, Any] | None = AUTH_DEPENDENCY):
        return await run(
            request,
            auth,
            ("retour.create",),
            lambda body, author: _return_response(body, author),
        )

    @pb.post("/master/retour/update")
    async def retour_update(request: Request, auth: dict[str, Any] | None = AUTH_DEPENDENCY):
        return await run(
            request,
            auth,
            ("retour.update",),
            lambda body, author: update_return(get_engine(), body, author),
            forbidden_message="Vous n'avez pas la permission de modifier ce retour",
        )

    @pb.post("/master/retour/updatePDV")
    async def retour_update_pdv(request: Request, auth: dict[str, Any] | None = AUTH_DEPENDENCY):
        return await run(
            request,
            auth,
            ("retour.update",),
            lambda body, _author: update_return_pdv(get_engine(), body),
            deny_only_false="retour.update",
        )

    @pb.get("/master/retour/info/{return_id}")
    async def retour_info_route(
        return_id: str, request: Request, auth: dict[str, Any] | None = AUTH_DEPENDENCY
    ):
        return await run(
            request, auth, None, lambda _body, _author: return_info(get_engine(), return_id)
        )

    @pb.post("/master/retour/return_stats")
    async def retour_stats(request: Request, auth: dict[str, Any] | None = AUTH_DEPENDENCY):
        return await run(
            request,
            auth,
            ("retour.read",),
            lambda body, _author: transaction_stats(get_engine(), body, "RETURN"),
            forbidden_message="Vous n'avez pas les permissions nécessaires",
            error_prefix="Erreur lors de la récupération des statistiques: ",
        )

    @pb.get("/master/retour/cancel/{return_id}")
    async def retour_cancel(
        return_id: str,
        request: Request,
        auth: dict[str, Any] | None = AUTH_DEPENDENCY,
    ):
        if not auth:
            return JSONResponse(status_code=401, content=UNAUTHORIZED)
        if not _allowed("retour.remove"):
            return JSONResponse(status_code=403, content={"message": "Vous n'avez pas les droits"})
        try:
            return JSONResponse(
                status_code=200,
                content=await cancel_return_checked(
                    get_engine(),
                    return_id,
                    is_admin=(AUTH_RULE_CONTEXT.get() or {}).get("role.permission.isAdmin") is True,
                ),
            )
        except CommerceForbiddenError as exc:
            return JSONResponse(status_code=403, content={"message": str(exc)})
        except CommerceNotFoundError as exc:
            return JSONResponse(status_code=404, content={"message": str(exc)})
        except CommerceValidationError as exc:
            return JSONResponse(status_code=400, content={"message": str(exc)})

    @pb.post("/master/caisse/return")
    async def caisse_return(request: Request, auth: dict[str, Any] | None = AUTH_DEPENDENCY):
        return await run(
            request,
            auth,
            ("caisse.transaction",),
            lambda body, author: _message_response(caisse_add_return(get_engine(), body, author)),
            forbidden_status=400,
            forbidden_message="Vous n'avez pas les autorisations",
            deny_only_false="caisse.transaction",
        )

    @pb.post("/master/caisse/versement_uv")
    async def caisse_versement_uv(request: Request, auth: dict[str, Any] | None = AUTH_DEPENDENCY):
        return await run(
            request,
            auth,
            ("caisse.transaction", "caisse.manage"),
            lambda body, author: versement_uv(get_engine(), body, author),
            forbidden_message="Vous n'avez pas les permissions pour effectuer cette op�ration",
        )

    @pb.post("/master/caisse/add_payment_to_sale")
    async def caisse_add_payment(request: Request, auth: dict[str, Any] | None = AUTH_DEPENDENCY):
        return await run(
            request,
            auth,
            ("caisse.transaction", "caisse.manage"),
            lambda body, author: add_payment_to_sale(
                get_engine(),
                _id(body, "saleId"),
                _id(body, "caisse"),
                float(body.get("amount") or 0),
                str(body.get("method") or ""),
                author,
                body.get("note"),
            ),
            forbidden_message="Vous n'avez pas les permissions pour effectuer cette op�ration",
        )

    @pb.get("/master/caisse/reportByExpenseType/{caisse_id}/{start}/{end}")
    async def caisse_expense_report(
        caisse_id: str,
        start: str,
        end: str,
        request: Request,
        auth: dict[str, Any] | None = AUTH_DEPENDENCY,
    ):
        return await run(
            request,
            auth,
            ("caisse.read", "caisse.manage"),
            lambda _body, _author: caisse_report(get_engine(), caisse_id, start, end, expense=True),
            forbidden_message="Vous n'avez pas les authorizations necessaires",
        )

    @pb.get("/master/caisse/reportByCaisseType/{caisse_id}/{start}/{end}")
    async def caisse_type_report(
        caisse_id: str,
        start: str,
        end: str,
        request: Request,
        auth: dict[str, Any] | None = AUTH_DEPENDENCY,
    ):
        # The source intentionally omits the middleware for this route.
        return await public_report(
            request, lambda _body: caisse_report(get_engine(), caisse_id, start, end, expense=False)
        )

    @pb.post("/master/sale/recharge_stats")
    async def sale_recharge_stats(request: Request, auth: dict[str, Any] | None = AUTH_DEPENDENCY):
        return await run(
            request,
            auth,
            ("sale.read",),
            lambda body, _author: transaction_stats(get_engine(), body, "RECHARGE"),
            forbidden_message="Vous n'avez pas les permissions nécessaires",
            error_prefix="Erreur lors de la récupération des statistiques: ",
        )

    @pb.post("/master/sale/sold_products_report")
    async def sale_products_report(request: Request):
        return await public_report(request, lambda body: sold_products(get_engine(), body))

    @pb.post("/master/sale/sold_products_info")
    async def sale_products_info(request: Request):
        return await public_report(
            request, lambda body: sold_products(get_engine(), body, info=True)
        )

    @pb.post("/master/sale/product_sales_by_customer")
    async def sale_product_customers(request: Request):
        return await public_report(request, lambda body: product_customers(get_engine(), body))

    @pb.post("/master/sale/check_caisse_transactions")
    async def sale_check_caisse(request: Request):
        return await public_report(request, lambda body: check_sale_caisse(get_engine(), body))

    @pb.post("/master/sale/customers_summary_by_period")
    async def sale_customer_summary(request: Request):
        return await public_report(request, lambda body: customer_summary(get_engine(), body))

    @pb.post("/master/sale/customers_summary_info_by_period")
    async def sale_customer_summary_info(request: Request):
        return await public_report(
            request, lambda body: customer_summary(get_engine(), body, info=True)
        )

    @pb.post("/master/sale/customer_products_report")
    async def sale_customer_products(request: Request):
        return await public_report(request, lambda body: customer_products(get_engine(), body))

    @pb.post("/master/sale/customers_products_report")
    async def sale_customers_products(request: Request):
        return await public_report(request, lambda body: customers_products(get_engine(), body))


async def _recharge_response(body: dict[str, Any], author: str) -> dict[str, str]:
    return {"id": await create_recharge(get_engine(), body, author)}


async def _purchase_response(body: dict[str, Any], author: str) -> dict[str, str]:
    if float(body.get("payed") or 0) > 0 and not _allowed("wallet.transaction"):
        raise CommerceForbiddenError("Vous n'avez pas les droits pour effectuer des paiements")
    purchase_id = await create_purchase(get_engine(), body, author)
    return {"id": purchase_id, "message": "Achat créé avec succès"}


async def _return_response(body: dict[str, Any], author: str) -> dict[str, str]:
    return {"id": await new_return(get_engine(), body, author)}


async def _message_response(awaitable) -> dict[str, str]:
    return {"message": await awaitable}
