"""Restore exact PocketBase view aliases folded to lowercase by PostgreSQL.

This repairs the Python translation, not the frozen schema or business logic.
ALTER VIEW preserves the view data dependencies and column identities.
"""

import json
import re


ALIASES = {
    "buma_article_list_stat_view": ("articleCount", "categorieCount"),
    "buma_caisse_transaction_by_categorie_list_view": ("caisseName",),
    "buma_customer_mobile_list_view": ("uvUser", "globalUpdated"),
    "buma_customer_pdv_list_view": ("realId", "pdvId"),
    "buma_pdv_turnover": ("pdvId",),
    "buma_sale_info": (
        "totalAmount",
        "totalPayed",
        "totalNotPayed",
        "totalSale",
        "totalProfit",
        "payedPercent",
    ),
    "buma_sim_list_view": ("uvUser", "globalUpdated"),
    "buma_transaction_list_view": ("salePayed", "returnPayed", "authorName", "authorUsername"),
    "test": (
        "customerId",
        "customerName",
        "articleId",
        "articleName",
        "totalQuantity",
        "totalRevenue",
        "totalProfit",
    ),
}


async def change(app, forward):
    for table, aliases in ALIASES.items():
        collection = await app.find_collection(table)
        options = dict(collection.options or {})
        query = options["query"]
        for alias in aliases:
            old, new = (alias.lower(), alias) if forward else (alias, alias.lower())
            await app.execute_sql(f'ALTER VIEW "{table}" RENAME COLUMN "{old}" TO "{new}"')
            if forward:
                query = re.sub(
                    r"\bAS\s+" + re.escape(alias) + r"\b",
                    'AS "' + alias + '"',
                    query,
                    flags=re.IGNORECASE,
                )
            else:
                query = query.replace('AS "' + alias + '"', "AS " + alias)
        options["query"] = query
        if forward and table == "buma_customer_mobile_list_view":
            options["query"] = query.replace('as "isNafama"', 'AS "isNafama"')
        if table == "test":
            if forward:
                options["query"] = query.replace(
                    "ORDER BY customerName ASC, totalRevenue DESC",
                    'ORDER BY "customerName" ASC, "totalRevenue" DESC',
                )
            else:
                options["query"] = query.replace(
                    'ORDER BY "customerName" ASC, "totalRevenue" DESC',
                    "ORDER BY customerName ASC, totalRevenue DESC",
                )
        # Avoid recreating/dropping views with dependents; only the stored query
        # changes after the atomic ALTER VIEW renames above.
        await app.execute_sql(
            "UPDATE _collections SET options=CAST(:options AS jsonb) WHERE id=:id",
            {"options": json.dumps(options), "id": collection.id},
        )


async def up(app):
    await change(app, True)


async def down(app):
    await change(app, False)
