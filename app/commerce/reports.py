"""C9 reports: preserve the frozen SQL grouping and legacy response keys."""

from __future__ import annotations

import math
import re
from datetime import UTC, datetime
from typing import Any

from sqlalchemy import text

from app.commerce.service import CommerceValidationError

DATE_REQUIRED = "startDate et endDate sont requis (format: YYYY-MM-DD ou YYYY-MM-DD HH:MM:SS)"


def report_date(value: str, *, end: bool = False) -> datetime:
    value = value.strip()
    if len(value) <= 10:
        value += " 23:59:59" if end else " 00:00:00"
    result = datetime.fromisoformat(value.replace("Z", "+00:00"))
    return result.replace(tzinfo=UTC) if result.tzinfo is None else result


def report_params(data: dict[str, Any]) -> dict[str, Any]:
    start, end = str(data.get("startDate") or "").strip(), str(data.get("endDate") or "").strip()
    if not start or not end:
        raise CommerceValidationError(DATE_REQUIRED)
    return {
        "start": report_date(start),
        "end": report_date(end, end=True),
        "filter": "%" + re.sub(r"[^a-zA-Z0-9]", "", str(data.get("filter") or "")) + "%",
    }


def revenue(tx: str, sale: str) -> str:
    return (
        f"CASE WHEN {tx}.type='RECHARGE' THEN COALESCE({sale}.credited_amount,"
        f"{sale}.amount,{tx}.quantity*COALESCE({tx}.price,0)) "
        f"ELSE {tx}.quantity*COALESCE({tx}.price,0) END"
    )


async def rows(engine: Any, sql: str, params: dict[str, Any]) -> list[dict[str, Any]]:
    async with engine.connect() as connection:
        return [dict(row) for row in (await connection.execute(text(sql), params)).mappings()]


async def paginated(
    engine: Any, sql: str, params: dict[str, Any], data: dict[str, Any]
) -> dict[str, Any]:
    # JavaScript parseInt also accepts strings with a numeric prefix.
    def positive(value: Any, default: int) -> int:
        match = re.match(r"^[\s]*([+-]?\d+)", str(value or default))
        if not match:
            raise CommerceValidationError("Invalid pagination")
        return max(1, int(match[1]))

    page, per_page = positive(data.get("page"), 1), positive(data.get("perPage"), 30)
    count = (await rows(engine, f"SELECT COUNT(*) AS n FROM ({sql}) grouped_rows", params))[0]["n"]
    items = await rows(
        engine,
        sql + " LIMIT :limit OFFSET :offset",
        {**params, "limit": per_page, "offset": (page - 1) * per_page},
    )
    return {
        "page": page,
        "perPage": per_page,
        "totalPages": max(1, math.ceil(count / per_page)),
        "totalItems": count,
        "items": items,
    }


async def sold_products(engine: Any, data: dict[str, Any], *, info: bool = False):
    params = report_params(data)
    rev = revenue("t", "bs")
    base = """
        FROM buma_transaction t
        LEFT JOIN buma_article a ON a.id=t.article
        LEFT JOIN buma_article_stockage s ON s.id=t.stockage
        LEFT JOIN buma_sale bs ON bs.id=t.sale
        WHERE t.type IN ('SALE','RECHARGE') AND t.status=TRUE
          AND t.created>=:start AND t.created<=:end
          AND (a.designation ILIKE :filter OR s.id ILIKE :filter OR s.name ILIKE :filter)
    """
    profit = f"({rev})-t.quantity*COALESCE(a.buy_price,0)"
    if info:
        return (
            await rows(
                engine,
                f"""SELECT COALESCE(SUM(t.quantity),0) AS "totalQuantity",
            COALESCE(SUM({rev}),0) AS "totalRevenue",
            COALESCE(SUM({profit}),0) AS "totalProfit" {base}""",
                params,
            )
        )[0]
    sql = f"""SELECT a.id, COALESCE(a.designation,'UNKNOWN') AS name,
        COALESCE(SUM(t.quantity),0) AS quantity,
        TRUNC(COALESCE(AVG(t.price),0)) AS price, COUNT(*) AS "saleCount",
        COALESCE(SUM({rev}),0) AS revenue, COALESCE(SUM({profit}),0) AS profit,
        COALESCE(s.id,'') AS stockage, COALESCE(t.article_stock,'') AS "articleStock"
        {base} GROUP BY a.id,a.designation,s.id,t.article_stock ORDER BY revenue DESC"""
    return await paginated(engine, sql, params, data)


async def product_customers(engine: Any, data: dict[str, Any]):
    stock_id = str(data.get("articleStockId") or "").strip()
    if not stock_id:
        raise CommerceValidationError("articleStockId requis")
    params = {**report_params(data), "stock": stock_id}
    rev = revenue("v", "s")
    sql = f"""SELECT COALESCE(v.customer,'') AS customer,
        COALESCE(MAX(v.complete_name),'') AS "customerName",
        COALESCE(SUM(v.quantity),0) AS "totalQuantity",
        COALESCE(SUM({rev}),0) AS "totalRevenue",
        COALESCE(SUM(({rev})-v.quantity*COALESCE(a.buy_price,0)),0) AS "totalProfit",
        COUNT(*) AS "salesCount"
        FROM buma_transaction_list_view v
        LEFT JOIN buma_article a ON a.id=v.article LEFT JOIN buma_sale s ON s.id=v.sale
        WHERE v.type IN ('SALE','RECHARGE') AND v.status=TRUE AND v.article_stock=:stock
          AND v.created>=:start AND v.created<=:end
          AND v.customer IS NOT NULL AND TRIM(v.customer)<>''
        GROUP BY v.customer ORDER BY "totalRevenue" DESC"""
    return await paginated(engine, sql, params, data)


async def check_sale_caisse(engine: Any, data: dict[str, Any]):
    sale_id = str(data.get("saleId") or "").strip()
    if not sale_id:
        raise CommerceValidationError("saleId requis")
    params = {"id": sale_id}
    if not await rows(engine, "SELECT id FROM buma_sale WHERE id=:id", params):
        raise CommerceValidationError("Vente introuvable")
    found = await rows(
        engine,
        """SELECT id FROM buma_caisse_transactions
        WHERE sale=:id AND status=TRUE LIMIT 1""",
        params,
    )
    return {"hasCaisseTransactions": bool(found)}


def customer_aggregate() -> str:
    return f"""SELECT s.customer AS "customerId",
        COALESCE(c.firstname||' '||c.lastname,'') AS "customerName",
        COALESCE(SUM({revenue("v", "s")}),0) AS "totalRevenue", 0 AS "totalVersements"
        FROM buma_transaction_list_view v LEFT JOIN buma_sale s ON s.id=v.sale
        LEFT JOIN buma_customer c ON c.id=s.customer
        WHERE v.type IN ('SALE','RECHARGE') AND v.status=TRUE
          AND v.created>=:start AND v.created<=:end
          AND s.customer IS NOT NULL AND s.customer<>''
          AND (COALESCE(c.firstname,'')||' '||COALESCE(c.lastname,'')) ILIKE :filter
        GROUP BY s.customer,c.firstname,c.lastname
        UNION ALL
        SELECT ct.customer AS "customerId",
        COALESCE(c2.firstname||' '||c2.lastname,'') AS "customerName",
        0 AS "totalRevenue", COALESCE(SUM(ct.amount),0) AS "totalVersements"
        FROM buma_caisse_transactions ct LEFT JOIN buma_customer c2 ON c2.id=ct.customer
        WHERE ct.status=TRUE AND ct.type IN ('VERSEMENT','BANK_VERSEMENT')
          AND ct.created>=:start AND ct.created<=:end
          AND ct.customer IS NOT NULL AND ct.customer<>''
          AND (COALESCE(c2.firstname,'')||' '||COALESCE(c2.lastname,'')) ILIKE :filter
        GROUP BY ct.customer,c2.firstname,c2.lastname"""


async def customer_summary(engine: Any, data: dict[str, Any], *, info: bool = False):
    params = report_params(data)
    # Source sums positive differences per UNION row, not the final net balance.
    sums = """COALESCE(SUM(agg."totalRevenue"),0) AS "totalRevenue",
        COALESCE(SUM(agg."totalVersements"),0) AS "totalVersements",
        COALESCE(SUM(CASE WHEN agg."totalRevenue">agg."totalVersements"
          THEN agg."totalRevenue"-agg."totalVersements" ELSE 0 END),0) AS "totalMissing"
        """
    base = f'FROM ({customer_aggregate()}) agg WHERE agg."customerId" IS NOT NULL'
    if info:
        return (
            await rows(
                engine,
                f"""SELECT {sums},
            COUNT(DISTINCT agg."customerId") AS "customersCount" {base}""",
                params,
            )
        )[0]
    sql = f"""SELECT agg."customerId", COALESCE(MAX(agg."customerName"),'') AS "customerName",
        {sums} {base} GROUP BY agg."customerId" ORDER BY "totalRevenue" DESC"""
    return await paginated(engine, sql, params, data)


async def customer_products(engine: Any, data: dict[str, Any]):
    customer_id = str(data.get("customerId") or "").strip()
    if not customer_id:
        raise CommerceValidationError("customerId requis")
    params = {**report_params(data), "customer": customer_id}
    sql = f"""SELECT a.id AS "articleId",COALESCE(a.designation,'UNKNOWN') AS "articleName",
        COALESCE(SUM(v.quantity),0) AS "totalQuantity",
        COALESCE(SUM({revenue("v", "s")}),0) AS "totalRevenue"
        FROM buma_transaction_list_view v LEFT JOIN buma_sale s ON s.id=v.sale
        LEFT JOIN buma_article a ON a.id=v.article
        WHERE v.type IN ('SALE','RECHARGE') AND v.status=TRUE
          AND v.created>=:start AND v.created<=:end AND s.customer=:customer
          AND COALESCE(a.designation,'') ILIKE :filter
        GROUP BY a.id,a.designation ORDER BY "totalRevenue" DESC"""
    return await paginated(engine, sql, params, data)


async def customers_products(engine: Any, data: dict[str, Any]):
    params = report_params(data)
    params["filter"] = "%"
    customers = data.get("customers")
    ids = (
        [re.sub(r"[^a-zA-Z0-9]", "", v.strip()) for v in customers if isinstance(v, str)]
        if isinstance(customers, list)
        else []
    )
    ids = [v for v in ids if v]
    condition = " AND s.customer=ANY(:customers)" if ids else ""
    params["customers"] = ids
    rev = revenue("v", "s")
    product_rows = await rows(
        engine,
        f"""SELECT s.customer AS "customerId",
        COALESCE(c.firstname||' '||c.lastname,'') AS "customerName",
        a.id AS "articleId",COALESCE(a.designation,'UNKNOWN') AS "articleName",
        COALESCE(SUM(v.quantity),0) AS "totalQuantity",
        COALESCE(SUM({rev}),0) AS "totalRevenue",
        COALESCE(SUM(({rev})-v.quantity*COALESCE(a.buy_price,0)),0) AS "totalProfit"
        FROM buma_transaction_list_view v LEFT JOIN buma_sale s ON s.id=v.sale
        LEFT JOIN buma_article a ON a.id=v.article LEFT JOIN buma_customer c ON c.id=s.customer
        WHERE v.type IN ('SALE','RECHARGE') AND v.status=TRUE
          AND v.created>=:start AND v.created<=:end
          AND s.customer IS NOT NULL AND s.customer<>'' {condition}
        GROUP BY s.customer,a.id,a.designation,c.firstname,c.lastname
        ORDER BY "customerName" ASC,"totalRevenue" DESC""",
        params,
    )
    totals = await rows(
        engine,
        f"""SELECT agg."customerId",
        SUM(agg."totalRevenue") AS "totalRevenue", SUM(agg."totalVersements") AS "totalVersements"
        FROM ({customer_aggregate()}) agg GROUP BY agg."customerId"
        """,
        params,
    )
    totals_by_id = {row["customerId"]: row for row in totals}
    grouped = {}
    for row in product_rows:
        cid = row["customerId"]
        if cid not in grouped:
            total = totals_by_id.get(cid, {"totalRevenue": 0, "totalVersements": 0})
            grouped[cid] = {
                "customerId": cid,
                "customerName": row["customerName"],
                "totalRevenue": total["totalRevenue"],
                "totalVersements": total["totalVersements"],
                "totalMissing": max(0, total["totalRevenue"] - total["totalVersements"]),
                "products": [],
            }
        grouped[cid]["products"].append(
            {
                key: row[key]
                for key in (
                    "articleId",
                    "articleName",
                    "totalQuantity",
                    "totalRevenue",
                    "totalProfit",
                )
            }
        )
    return {"items": list(grouped.values())}


async def transaction_stats(engine: Any, data: dict[str, Any], kind: str):
    params = {
        "phone": f"%{data.get('phoneNumber', 'undefined')}%",
        "author": f"%{data.get('author', 'undefined')}%",
        "type": kind,
    }
    conditions = []
    for key, op in (("startDate", ">="), ("endDate", "<=")):
        if data.get(key):
            conditions.append(f"v.mobile_transaction_date {op} :{key}")
            params[key] = report_date(str(data[key]))
    if data.get("network"):
        conditions.append("v.network=:network")
        params["network"] = data["network"]
    where = "(v.number ILIKE :phone OR v.authorname ILIKE :author OR v.complete_name ILIKE :phone)"
    # Preserve source's lack of type/status constraint if no date/network is supplied.
    if conditions:
        where = "v.type=:type AND v.status=TRUE AND " + " AND ".join(conditions) + " AND " + where
    if kind == "RECHARGE":
        result = await rows(
            engine,
            f"""SELECT COUNT(*) AS total_count,
            COALESCE(SUM(CASE WHEN s.credited_amount>0 THEN s.credited_amount
              ELSE s.amount END),0) AS total_amount
            FROM buma_transaction_list_view v LEFT JOIN buma_sale s ON s.id=v.sale
            WHERE {where}""",
            params,
        )
    else:
        result = await rows(
            engine,
            f"""SELECT COUNT(*) AS total_count,
            COALESCE(SUM(TRUNC(r.amount)),0) AS total_amount,
            COALESCE(SUM(CASE WHEN r.payed THEN TRUNC(r.amount) ELSE 0 END),0) AS total_payed,
            COALESCE(SUM(CASE WHEN NOT r.payed THEN TRUNC(r.amount) ELSE 0 END),0)
              AS total_not_payed,
            COUNT(*) FILTER (WHERE r.payed) AS total_payed_count
            FROM buma_transaction_list_view v JOIN buma_return r ON r.id=v."return"
            WHERE {where} AND r.status=TRUE AND TRUNC(r.amount)>0""",
            params,
        )
    return {"stats": result[0]}


async def caisse_report(engine: Any, caisse_id: str, start: str, end: str, *, expense: bool):
    label = "COALESCE(et.name,'Autres')" if expense else "COALESCE(b.type,'Autres')"
    join = 'LEFT JOIN buma_caisse_expense_type et ON et.id=b."expenseType"' if expense else ""
    condition = "AND b.type='EXPENSE'" if expense else ""
    result = await rows(
        engine,
        f"""SELECT SUM(b.amount) AS amount,COUNT(*) AS count,{label} AS label
        FROM buma_caisse_transactions b {join}
        WHERE b.created>=:start AND b.created<=:end AND b.caisse=:caisse
          AND b.status=TRUE {condition} GROUP BY label""",
        {
            "start": report_date(start),
            "end": report_date(end),
            "caisse": caisse_id,
        },
    )
    return {"datas": result}
