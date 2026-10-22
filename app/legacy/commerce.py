"""Sequential purchase.ts/sale.ts legacy handlers. No master service aliases."""

from app.commerce.sequential import read, related
from app.legacy.caisse import error, record, rows
from app.legacy.records import create, js_add, number, save
from app.legacy.stockage import parse_int


def permission(raw, domain):
    values = raw.get(domain) or {}
    actions = (
        ("canMakeTransaction", "canCancelTransaction")
        if domain == "purchase"
        else ("canCancel", "canEditInvoice")
    )
    if raw.get("isAdmin") or values.get("canManage") is True:
        return dict.fromkeys(("canManage", "read", "update", "create", "remove", *actions), True)
    return {
        "canManage": False,
        "update": values.get("update"),
        **{key: values.get(key) is True for key in ("read", "create", "remove", *actions)},
    }


async def purchase(engine, data, author, role):
    supplier = await record(engine, "buma_supplier", data.get("supplier"))
    items = data["items"]
    amount = sum(number(i.get("quantity")) * number(i.get("buy_price")) for i in items)
    paid = number(data.get("payed"))
    remaining = max(0, amount - paid)
    caisse = session = None
    if paid > 0:
        if not data.get("caisse"):
            return error("Caisse non défini")
        if not data.get("method"):
            return error("Méthode non définie")
        caisse = await record(engine, "buma_caisse", data["caisse"])
        if caisse["state"] != "OPEN":
            return error("Caisse non ouverte")
        session = await record(engine, "buma_caisse_open_close", caisse["currentSession"])
        if session["state"] != "OPEN":
            return error("Session non ouverte")
        if caisse["currentCashier"] != author:
            return error("Vous n'étes pas le caisseur")
        if int(caisse["balance"]) < paid:
            return error("Solde insuffisant")
    buy = await create(
        engine,
        "buma_buy",
        {
            "supplier": supplier["id"],
            "amount": amount,
            "discount": 0,
            "payed": data.get("payed"),
            "profit": 0,
            "remaining": remaining,
            "type": "BUY",
            "note": data.get("note"),
            "author": author,
            "status": True,
            "invoice_no": 1,
        },
    )
    await save(
        engine,
        "buma_supplier",
        supplier["id"],
        {"solde": int(supplier["solde"]) - (remaining if paid > 0 else amount)},
    )
    if paid > 0:
        tx = await create(
            engine,
            "buma_caisse_transactions",
            {
                "caisse": caisse["id"],
                "session": session["id"],
                "amount": data["payed"],
                "type": "EXPENSE",
                "author": author,
                "status": True,
                "note": "Paiement d'une commande",
                "supplier": supplier["id"],
            },
        )
        await create(
            engine,
            "buma_payment",
            {
                "buy": buy["id"],
                "amount": data["payed"],
                "method": data["method"],
                "caisse_transaction": tx["id"],
                "author": author,
                "status": True,
            },
        )
        await save(engine, "buma_caisse", caisse["id"], {"balance": int(caisse["balance"]) - paid})
    for item in items:
        stock = await record(engine, "buma_article_stock", item.get("article_stock"))
        previous = int(stock["quantity"])
        await save(
            engine,
            "buma_article_stock",
            stock["id"],
            {"quantity": js_add(previous, item["quantity"])},
        )
        await create(
            engine,
            "buma_transaction",
            {
                "buy": buy["id"],
                "article": item.get("article"),
                "stockage": item.get("stockage"),
                "article_stock": item.get("article_stock"),
                "quantity": item.get("quantity"),
                "price": item.get("buy_price"),
                "old_quantity": previous,
                "type": "BUY",
                "author": author,
                "status": True,
            },
        )
    return {"message": "ok", "id": buy["id"]}


async def cancel_purchase(engine, data, author, role):
    if role.get("canCancelTransaction") is not True:
        return error("Vous n'avez pas les droits", 403)
    buy = await record(engine, "buma_buy", data.get("buyId"))
    supplier = await read(engine, "buma_supplier", buy.get("supplier") or "")
    await save(engine, "buma_buy", buy["id"], {"status": False, "state": "CANCELED"})
    for tx in await related(engine, "buma_transaction", "buy", buy["id"]):
        await save(engine, "buma_transaction", tx["id"], {"status": False})
        stock = await record(engine, "buma_article_stock", tx.get("article_stock"))
        await save(
            engine,
            "buma_article_stock",
            stock["id"],
            {"quantity": int(stock["quantity"]) - int(tx["quantity"])},
        )
    if supplier:
        await save(
            engine, "buma_supplier", supplier["id"], {"solde": supplier["solde"] + buy["remaining"]}
        )
    await cancel_payments(engine, buy["id"], 1)
    return {"message": "ok"}


async def cancel_payments(engine, identifier, direction):
    # sale.ts also looks up buy, not sale, in its compensation loop.
    for payment in await related(engine, "buma_payment", "buy", identifier):
        await save(engine, "buma_payment", payment["id"], {"status": False})
        if payment.get("caisse_transaction"):
            tx = await record(engine, "buma_caisse_transactions", payment["caisse_transaction"])
            await save(engine, "buma_caisse_transactions", tx["id"], {"status": False})
            caisse = await record(engine, "buma_caisse", tx["caisse"])
            await save(
                engine,
                "buma_caisse",
                caisse["id"],
                {"balance": int(caisse["balance"]) + direction * payment["amount"]},
            )


async def cancel_sale(engine, data, author, role):
    if role.get("canCancel") is not True:
        return error("you can't cancel this sale", 403)
    sale = await record(engine, "buma_sale", data.get("saleId"))
    customer = await record(engine, "buma_customer", sale["customer"])
    for payment in await related(engine, "buma_payment", "sale", sale["id"]):
        await save(engine, "buma_payment", payment["id"], {"status": False})
    for tx in await related(engine, "buma_transaction", "sale", sale["id"]):
        await save(engine, "buma_transaction", tx["id"], {"status": False})
        stock = await record(engine, "buma_article_stock", tx["article_stock"])
        article = await record(engine, "buma_article", tx["article"])
        if article.get("trackStock"):
            await save(
                engine,
                "buma_article_stock",
                stock["id"],
                {"quantity": int(stock["quantity"]) + int(tx["quantity"])},
            )
    solde = int(customer["solde"]) + sale["remaining"]
    await save(engine, "buma_customer", customer["id"], {"solde": solde})
    await save(
        engine, "buma_sale", sale["id"], {"status": False, "state": "CANCELED", "remaining": 0}
    )
    await save(engine, "buma_customer", customer["id"], {"solde": solde})
    await cancel_payments(engine, sale["id"], -1)
    return {"message": "ok"}


async def payment(engine, data, author, role, *, purchase=False):
    key = "canMakeTransaction" if purchase else "update"
    if role.get(key) is not True:
        return error("Vous ne pouvez pas effectuer cette transaction", 403)
    caisse = await record(engine, "buma_caisse", data.get("caisse"))
    if caisse["state"] != "OPEN":
        return error("Caisse n'est pas ouverte")
    if caisse["currentCashier"] != author:
        return error("Vous ne pouvez pas effectuer cette transaction")
    amount = data.get("amount")
    if purchase and number(amount) > caisse["balance"]:
        return error("Solde insuffisant")
    table, field, person_table, person_key = (
        ("buma_buy", "buy", "buma_supplier", "supplier")
        if purchase
        else ("buma_sale", "sale", "buma_customer", "customer")
    )
    invoice = await record(engine, table, data.get(field))
    person = await record(engine, person_table, invoice[person_key])
    if not purchase and number(amount) > invoice["remaining"]:
        return error("Montant trop supérieur")
    values = {
        field: invoice["id"],
        "amount": amount,
        "method": data.get("method"),
        "author": author,
        "status": True,
    }
    if not purchase:
        await create(engine, "buma_payment", values)
    session = await record(engine, "buma_caisse_open_close", caisse["currentSession"])
    tx = await create(
        engine,
        "buma_caisse_transactions",
        {
            "author": author,
            "amount": amount,
            "note": "Paiement d'une commande" if purchase else "Paiement d'une vente",
            "caisse": caisse["id"],
            "type": "EXPENSE" if purchase else "VERSEMENT",
            person_key: person["id"],
            "session": session["id"],
            "status": True,
        },
    )
    balance = (
        int(caisse["balance"]) - number(amount)
        if purchase
        else js_add(int(caisse["balance"]), amount)
    )
    await save(engine, "buma_caisse", caisse["id"], {"balance": balance})
    if purchase:
        await create(engine, "buma_payment", {**values, "caisse_transaction": tx["id"]})
    await save(engine, person_table, person["id"], {"solde": js_add(int(person["solde"]), amount)})
    remaining = int(invoice["remaining"]) - number(amount)
    await save(
        engine,
        table,
        invoice["id"],
        {
            "payed": js_add(int(invoice["payed"]), amount),
            "remaining": max(0, remaining) if purchase else remaining,
        },
    )
    return {"message": "ok"}


async def open_session(engine, identifier):
    caisse = await record(engine, "buma_caisse", identifier)
    if caisse["state"] != "OPEN":
        raise ValueError("Caisse non ouverte")
    session = await record(engine, "buma_caisse_open_close", caisse["currentSession"])
    if session["state"] != "OPEN":
        raise ValueError("Session non ouverte")
    return session


async def confirm_return(engine, data, author, role):
    session = await open_session(engine, data.get("caisse"))
    retour = await record(engine, "buma_return", data.get("returnId"))
    await save(engine, "buma_return", retour["id"], {"payed": True})
    caisse = await record(engine, "buma_caisse", data.get("caisse"))
    await save(
        engine,
        "buma_caisse",
        caisse["id"],
        {"balance": int(caisse["balance"]) - int(retour["amount"])},
    )
    await create(
        engine,
        "buma_caisse_transactions",
        {
            "author": author,
            "amount": int(retour["amount"]),
            "note": "Retour d'une vente",
            "caisse": caisse["id"],
            "type": "RETURN",
            "customer": retour["customer"],
            "session": session["id"],
            "status": True,
        },
    )
    return {"message": "ok"}


async def make_return(engine, data, author, role):
    amount = parse_int(data.get("amount"))
    caisse = session = None
    if data.get("caisse"):
        caisse = await record(engine, "buma_caisse", data["caisse"])
        session = await open_session(engine, data["caisse"])
        if data.get("payed") and int(caisse["balance"]) < amount:
            return error("Le solde de votre caisse est insufisante")
    await create(
        engine,
        "buma_return",
        {
            "payed": data.get("payed"),
            "customer": data.get("customer"),
            "pdv": data.get("pdv"),
            "pdv_number": data.get("pdv_number"),
            "caisse": data.get("caisse") if data.get("payed") else "",
            "caisseSession": (session or {}).get("id") if data.get("payed") else "",
            "amount": data.get("amount"),
            "note": data.get("note") or "",
            "article_stock": data.get("articleStock"),
        },
    )
    stock = await record(engine, "buma_article_stock", data.get("articleStock"))
    previous = int(stock["quantity"])
    await save(engine, "buma_article_stock", stock["id"], {"quantity": previous + amount})
    await create(
        engine,
        "buma_transaction",
        {
            "article": stock["article"],
            "stockage": stock["stockage"],
            "article_stock": stock["id"],
            "quantity": data.get("amount"),
            "old_quantity": previous,
            "type": "RETURN",
            "price": "1",
            "status": True,
            "author": author,
        },
    )
    if data.get("payed") and data.get("caisse"):
        await save(
            engine, "buma_caisse", caisse["id"], {"balance": int(caisse["balance"]) - amount}
        )
        await create(
            engine,
            "buma_caisse_transactions",
            {
                "caisse": caisse["id"],
                "type": "RETURN",
                "amount": data.get("amount"),
                "customer": data.get("customer"),
                "session": session["id"],
                "note": "Paiment de retour d'UV",
                "status": True,
                "author": author,
            },
        )
    return {"message": "OK"}


async def infos(engine, data, *, purchase=False):
    start, end = data["startDate"].replace(".", " ", 1), data["endDate"].replace(".", " ", 1)
    result = dict.fromkeys(
        (
            "totalAmount",
            "totalRemaining",
            "totalPayed",
            "totalProfit",
            "totalPayedPercent",
            "totalItems",
        ),
        0,
    )
    table = "buma_buy" if purchase else "buma_sale"
    extra = "" if purchase else " AND sim=''"
    dates = f"""to_char(created AT TIME ZONE 'UTC','YYYY-MM-DD HH24:MI:SS.MS"Z"') >= '{start}'
        AND to_char(created AT TIME ZONE 'UTC','YYYY-MM-DD HH24:MI:SS.MS"Z"') <= '{end}'"""
    try:
        found = await rows(
            engine,
            f"""SELECT SUM(amount) AS "totalAmount", SUM(remaining) AS "totalRemaining",
            SUM(payed) AS "totalPayed", SUM(profit) AS "totalProfit",
            SUM(remaining)*100/NULLIF(SUM(amount),0) AS "totalPayedPercent",
            COUNT(*) AS "totalItems" FROM {table} WHERE status=TRUE {extra} AND {dates}""",
            {},
        )
        # database/sql scans fields in query order, retaining values before a failed scan.
        for key in result:
            value = found[0][key]
            if value is None or float(value) != int(value):
                break
            result[key] = int(value)
    except Exception:
        pass
    if not purchase:
        result["payments"] = []
        try:
            found = await rows(
                engine,
                f"""SELECT COALESCE(pm.name,'UNKNOWN') AS name,
                COALESCE(SUM(bp.amount),0) AS "totalAmount" FROM buma_payment bp
                LEFT JOIN buma_payment_method pm ON pm.id=bp.method
                LEFT JOIN buma_sale bs ON bs.id=bp.sale WHERE bp.buy='' AND bs.sim=''
                AND bp.status=TRUE AND {dates.replace("to_char(created", "to_char(bp.created")}
                GROUP BY pm.name""",
                {},
            )
            for row in found:
                if float(row["totalAmount"]) != int(row["totalAmount"]):
                    raise ValueError("DynamicModel scan error")
                result["payments"].append(
                    {"name": row["name"], "totalAmount": int(row["totalAmount"])}
                )
        except Exception:
            pass
    return result
