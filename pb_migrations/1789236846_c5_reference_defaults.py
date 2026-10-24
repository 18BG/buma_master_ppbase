"""Seed the explicit reference defaults from the frozen bootstrap setup."""

from __future__ import annotations

import json


APP_ACTIONS = ["read:Voir", "create:Creer", "update:Modifier", "remove:Suppression"]


def _role(name: str, ref: str, description: str, *extra: str) -> dict:
    return {
        "id": name,
        "ref": ref,
        "name": name,
        "description": description,
        "info": {"actions": [*APP_ACTIONS, *extra]},
    }


APP_ROLES = [
    _role(
        "caisse",
        "Caisse",
        "Gestion des caisses",
        "transfer:Faire des transfers",
        "manage_deposit:Faire les deposits",
        "manage_withdrawal:Faire les retrais",
        "see_balance:Voir le solde",
        "expense_kind:Gerer les types de depenses",
        "transaction:Faire des transaction",
        "manage:Gestion completes de la caisse",
        "switch_user:Changer le caissier",
        "cancel_transaction:Annuler des transactions",
        "enter_occupied_caisse:Rentrer dans une caisse occupée",
        "create_expense_type:Creer un type de depense",
        "archive_expense_type:Archiver un type de depense",
        "update_expense_type:Modifier un type de depense",
        "change_transaction_note:Modifier la note d'une transaction",
        "manage_debt:Gérer les dettes",
    ),
    _role("pilotage", "Pilotage", "Gestion des pilotage", "manage_data:Gestion des données"),
    _role("retour", "retour", "Gestion des retours"),
    _role("recharge", "recharge", "Gestion des recharge"),
    _role("sale", "sale", "Gestion des vente", "change_product_price:Changer le prix des produits", "sale_date:Definir la date de vente"),
    _role("rupture", "Rupture", "Gestion des ruptures"),
    _role("purchase", "purchase", "Gestion des achats"),
    _role(
        "stockage",
        "stockage",
        "Gestion des stockage",
        "transfer:Faire des transfers",
        "conversion:Faire des conversions",
        "cancel_transfer:Annuler des transfers",
        "update_transaction_note:Modifier les notes des transactions",
        "cancel_conversion:Annuler des conversions",
        "input_output:Gérer les entrées/sorties de stock",
    ),
    _role("article", "article", "Gestion des articles", "manage_category:Gerer les categories d'articles", "see_buy_price:Peut voir les prix d'achat"),
    _role("customer", "Client", "Gestion des clients", "archive:Archivage de client", "manage_pdv:Gerer les points de vente"),
    _role("supplier", "Fournisseur", "Gestion des fournisseurs"),
    _role("employee", "Employee", "Gestion des employes", "see_employee_balance:Peut voir le solde des employes", "manage_position:Gerer les positions"),
    _role("invoice", "Invoice", "Gestion des factures"),
    _role(
        "wallet",
        "Porte-feuille",
        "Gestion des portefeuilles",
        "balance:Voir le solde",
        "transaction:Peut faire des transactions",
        "transfer:Peut faire des transfers",
        "wallet_uv:Peut acheter des UV avec portefeuille",
        "uv_wallet:Peut vendre des UV avec portefeuille",
        "list:Peut voir la liste",
        "deposit:Peut faire des depots",
        "cancel_transaction:Annuler des transactions",
        "withdrawal:Peut faire des retraits",
        "change_transaction_note:Modifier la note d'une transaction",
    ),
    _role("sim", "Sim", "Gestion des puces"),
    _role(
        "agent",
        "Agent",
        "Gestion des agents",
        "link_sim:Lier une puce",
        "unlink_sim:Délier une puce",
        "add_pdv:Ajouter un point de vente",
        "remove_pdv:Retirer un point de vente",
        "upload_document:Ajouter des documents",
        "delete_document:Supprimer des documents",
    ),
    _role(
        "pdv",
        "Point de vente",
        "Gestion des points de vente",
        "link_sim:Lier une puce",
        "unlink_sim:Délier une puce",
        "link_agent:Lier un agent",
        "unlink_agent:Retirer un agent",
        "link_customer:Lier un client",
        "unlink_customer:Retirer un client",
    ),
    {"id": "assistant", "ref": "Assistant IA", "name": "assistant", "description": "Assistant IA", "info": {"actions": ["read:Voir"]}},
]


async def _insert(app, table: str, values: dict, *, json_fields: set[str] = set()) -> None:
    columns = list(values)
    quoted_columns = ", ".join('"' + column + '"' for column in columns)
    placeholders = []
    params = {}
    for column in columns:
        parameter = f"p_{column}"
        placeholders.append(
            f"CAST(:{parameter} AS jsonb)" if column in json_fields else f":{parameter}"
        )
        params[parameter] = json.dumps(values[column]) if column in json_fields else values[column]
    await app.execute_sql(
        f'INSERT INTO "{table}" ({quoted_columns}) '
        f'VALUES ({", ".join(placeholders)}) ON CONFLICT ("id") DO NOTHING',
        params,
    )


async def up(app):
    await _insert(app, "buma_article_stockage", {
        "id": "STOCK_PRINCIPAL", "name": "STOCKAGE PRINCIPAL", "location": "",
        "comment": "", "type": "STOCKAGE", "status": True, "author": "",
    })
    for item in (
        {"id": "PAYMENT_CASH_12", "name": "CASH", "status": True},
        {"id": "PAYMENT_MOBILE_", "name": "MOBILE", "status": True},
        {"id": "PAYMENT_CARTE_1", "name": "CARTE", "status": True},
    ):
        await _insert(app, "buma_payment_method", item)
    await _insert(app, "buma_customer", {
        "id": "CUSTOMER_DIVERS", "solde": 0, "due": 0, "person_entity": "",
        "status": True, "author": "", "photo": "", "type": "PERSONNE",
        "parent": "", "kind": "CUSTOMER_SIMPLE", "agent": "", "firstname": "CLIENT DIVERS",
        "lastname": "", "address": "", "contact": "", "email": "", "name": "",
        # master.setup.ts l.54-59 omits pdv: PocketBase 0.22.34 stores the empty
        # list of this maxSelect-null relation (PrepareValue, schema_field.go
        # l.375-386), i.e. TEXT[] '{}' since the D-0019 correction (D-XXXX).
        # Databases that recorded this file keep '' until 1789236850 converts it.
        "pdv": [], "customer_no": 0,
    })
    await _insert(app, "buma_supplier", {
        "id": "SUPPLIER_DIVERS", "solde": 0, "author": "", "status": True,
        "person_entity": "", "firstname": "FOURNISSEUR DIVERS", "lastname": "",
        "addresse": "", "contact": "", "email": "", "name": "", "isSociety": False,
    })
    for role in APP_ROLES:
        await _insert(app, "buma_apps_roles", role, json_fields={"info"})


async def down(app):
    for table, ids in (
        ("buma_apps_roles", [role["id"] for role in APP_ROLES]),
        ("buma_supplier", ["SUPPLIER_DIVERS"]),
        ("buma_customer", ["CUSTOMER_DIVERS"]),
        ("buma_payment_method", ["PAYMENT_CASH_12", "PAYMENT_MOBILE_", "PAYMENT_CARTE_1"]),
        ("buma_article_stockage", ["STOCK_PRINCIPAL"]),
    ):
        for record_id in ids:
            await app.execute_sql(
                f'DELETE FROM "{table}" WHERE "id" = :id',
                {"id": record_id},
            )
