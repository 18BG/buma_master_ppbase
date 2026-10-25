#!/usr/bin/env python3
"""WS-H runtime proof of migration 1789236851 against the PocketBase 0.22.34 binary.

Synthetic data only (no real backup is read). PocketBase 0.22.34 builds the
source itself: the corrected export imported by a JS migration, then records
saved in a chosen order (rowid order, created order and id order differ on
purpose), an admin created by its CLI. The stopped snapshot is diagnosed and
imported by ``scripts.c11_import.import_snapshot`` into an owned, disposable
PostgreSQL 16 (labelled container, random loopback port, always removed)
migrated with every BUMA migration, and the same requests are sent to PPBase
with 1789236851 (after), after its down (before) and after its up again.

Proofs:

1. Before/after matrix (generated): on every column of the thirteen recreated
   views, filters with the operators = != ?= ?!= ~ < > and the literals "",
   null, 1, true, false and a value of the data (as a string and as a
   number), sorts in both directions, and the comparison of two columns.
   Every answer of PPBase after 1789236851 is the answer it gave before,
   except: an error that is now answered; rows that follow PocketBase where
   PPBase did not; rows explained by a corrected value (the same filter or
   sort evaluated on the values each side serves, by a model of PPBase's SQL);
   and the new errors of ``NEW_ERROR_RULES`` (a number compared with a text
   id). Any other difference fails the run.
2. Paired comparison with PocketBase 0.22.34 (curated): every served value of
   the fifteen views (the thirteen and the two left unchanged), pages, views
   by id, filters and sorts; the remaining differences are pinned with their
   PocketBase and PPBase statuses (``KNOWN_AFTER``); D-0039 forms (a text
   served for PocketBase's JSON value, true/false for its 1/0) are counted
   per field, not hidden. ``payedPercent`` and the sums of ``buma_sale_info``
   are compared over data phases written in both stores (integer truncation,
   negative, real, zero divisor, no row, compensated sum, large reals).
3. Pagination of the numbered views: every page size and sort serves every
   row once.
4. Column types and collations of every view: only the numbered ids and
   ``uvUser`` change; down restores them exactly; up again equals the first.

Reports contain field names, counts and synthetic values only.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import math
import re
import secrets
import shutil
import sqlite3
import subprocess
import sys
import tempfile
import time
import uuid
from collections import Counter
from contextlib import closing
from datetime import UTC, datetime
from pathlib import Path

import httpx
from ppbase.services.database_preparation import prepare_database
from ppbase.services.migration_runner import run_migrations_down, run_migrations_up
from sqlalchemy import text
from sqlalchemy.ext.asyncio import create_async_engine

from scripts.c11_auth_runtime import free_port, ppbase_server_environment, stop, wait_health
from scripts.c11_import import diagnose, digest, import_snapshot, load_schema, write_json_new

PROJECT = Path(__file__).resolve().parents[1]
MIGRATIONS = PROJECT / "pb_migrations"
MIGRATION = "1789236851_c11_view_sqlite_semantics.py"
SOURCE_VERSION = "0.22.34"
ADMIN = ("wsh-admin@example.invalid", "WSH-synthetic-password-1")
PASSWORD = "WSH-synthetic-password-2"
NUMBERED = (
    "buma_sale_info",
    "buma_caisse_transaction_by_categorie_list_view",
    "buma_caisse_session_info",
    "test",
    "buma_article_list_stat_view",
    "buma_customer_supplier_info_view",
    "buma_return_info_view",
)
CHANGED = (
    "buma_customer_list_view",
    "buma_customer_list_view_with_users",
    "buma_customer_mobile_list_view",
    "buma_return_list_view",
    "buma_article_with_no_stock_list_view",
    "buma_stockage_no_manager",
    *NUMBERED,
)
# Not recreated by 1789236851 (their values keep the types of D-0039).
UNCHANGED = ("buma_agent_list_view", "buma_transaction_list_view")
VIEWS = (*CHANGED, *UNCHANGED)
MULTI_ROW_NUMBERED = (
    "buma_caisse_transaction_by_categorie_list_view",
    "buma_caisse_session_info",
    "test",
)
CUSTOMERS = "buma_customer_list_view"
MULTIPLE = "72000001,72000002,71000001"  # pdv_sim of client 1 (rowid order)
# Fields whose served value 1789236851 corrects; a numbered view's id names
# another row (and is text), so all its fields are compared per id.
CORRECTED = {
    "buma_customer_list_view": {"pdv_sim", "agent_sim"},
    "buma_customer_list_view_with_users": {"pdv_sim", "agent_sim", "assigned_users"},
    "buma_customer_mobile_list_view": {
        "pdv_sim",
        "agent_sim",
        "isNafama",
        "pdv_sim_network",
        "agent_sim_network",
        "uvUser",
    },
    "buma_return_list_view": {"pdv_sim", "agent_sim"},
    "buma_article_with_no_stock_list_view": {"stockages"},
    "buma_stockage_no_manager": {"stockage"},
    "buma_sale_info": {"id", "payedPercent"},
    **{view: {"id"} for view in NUMBERED if view != "buma_sale_info"},
}
# D-0039 (acceptance requested): PocketBase serves the JSON value of a text
# that is valid JSON and SQLite's integers 1/0; PPBase the text and true/false.
D0039 = {
    "number_served_as_text_D-0039",
    "json_text_served_as_text_D-0039",
    "integer_boolean_served_as_boolean_D-0039",
}
NOTATION = {"json_key_order_D-0029", "json_number_notation_D-0029"}

# Curated probes whose answer still differs from PocketBase 0.22.34 after
# 1789236851: label -> (reason, PocketBase status, PPBase status). Any other
# difference fails the run, and so does a status other than the one pinned.
JSON_FILTER = "json_field_filtered_through_json_extract_D-0039"
INTEGER_FILTER = "sqlite_integer_filtered_as_integer_D-0039"
IS_NOT = "pocketbase_not_equal_is_is_not_keeping_null_rows"
JSON_SORT = "json_field_sorted_through_json_extract_D-0039"
NULL_FIRST = "sqlite_sorts_null_first"
TEXT_ID_NUMBER = "number_compared_with_a_text_id_is_an_error"
# Probe -> (reason, PocketBase status, PPBase status); the probes of
# CHANGED_BY_1789236851 changed with it, every other one is PPBase's answer
# before it too.
KNOWN_AFTER: dict[str, tuple[str, int, int]] = {
    # PocketBase compares JSON_EXTRACT of a view expression column (json
    # field): a numeric text is a number there ('73000001' = 73000001, not
    # "73000001"), 'null' is NULL, '"x"' is x, '1e3' is 1000.0; numbers sort
    # before text. PPBase compares the text, and a number or boolean literal
    # with a text column is an error (resolvers/record_field_resolve_runner.go
    # l.465-474, tools/dbutils/json.go l.30-47, tools/search/filter.go
    # l.226-317 of v0.22.34).
    **{
        f"{CUSTOMERS}:filter:pdv_sim {form}": (JSON_FILTER, 200, status)
        for form, status in (
            ('= "73000001"', 200),
            ("= 73000001", 500),
            ("!= null", 200),
            ("!= true", 500),
            ('!~ "7200"', 200),
            ("< 73000002", 500),
            ('<= "73000001"', 200),
            ('= "1e3"', 200),
            ('= "x"', 200),
            ("= -5", 500),
            ("= 0", 500),
            ("= 1", 500),
            ("= 1000", 500),
            ("= 12.5", 500),
            ("= false", 500),
            ("= null", 200),
            ("= true", 500),
            ('> "7"', 200),
            (">= 1000", 500),
            ('?!= "73000001"', 200),
            ('?= "73000001"', 200),
        )
    },
    f"{CUSTOMERS}:filter:73000001 = pdv_sim": (JSON_FILTER, 200, 500),
    f"{CUSTOMERS}:filter:true = pdv_sim": (JSON_FILTER, 200, 500),
    f'{CUSTOMERS}:filter:agent_sim = "79000003"': (JSON_FILTER, 200, 200),
    f"{CUSTOMERS}:filter:agent_sim = 79000003": (JSON_FILTER, 200, 500),
    **{
        f"{view}:filter:{field} {form}": (JSON_FILTER, 200, status)
        for view in (
            "buma_customer_list_view_with_users",
            "buma_customer_mobile_list_view",
            "buma_return_list_view",
        )
        for field, form, status in (
            ("pdv_sim", '= "73000001"', 200),
            ("pdv_sim", "= 73000001", 500),
            ("agent_sim", "= 73000001", 500),
        )
    },
    'buma_agent_list_view:filter:number = "79000003"': (JSON_FILTER, 200, 200),
    "buma_agent_list_view:filter:number = 79000003": (JSON_FILTER, 200, 500),
    "buma_agent_list_view:filter:number = true": (JSON_FILTER, 200, 500),
    # PocketBase compares SQLite's integers 1/0 (JSON_EXTRACT) with a number
    # or a text; PPBase compares a boolean: a number or a string is an error.
    **{
        f"buma_customer_mobile_list_view:filter:isNafama {form}": (INTEGER_FILTER, 200, 500)
        for form in ('!= ""', '= ""', '= "1"', "= 0", "= 1")
    },
    **{
        f"buma_transaction_list_view:filter:{form}": (INTEGER_FILTER, 200, 500)
        for form in ("payed != 1", 'payed = ""', "payed = 0", "payed = 1")
    },
    "buma_transaction_list_view:filter:salePayed = 0": (INTEGER_FILTER, 200, 500),
    "buma_transaction_list_view:filter:salePayed = 1": (INTEGER_FILTER, 200, 500),
    # payed is NULL for a transaction with neither sale nor return.
    "buma_transaction_list_view:filter:payed != true": (IS_NOT, 200, 200),
    "buma_transaction_list_view:filter:payed != false": (IS_NOT, 200, 200),
    f"{CUSTOMERS}:sort:pdv_sim,id": (JSON_SORT, 200, 200),
    f"{CUSTOMERS}:sort:-pdv_sim,id": (JSON_SORT, 200, 200),
    f"{CUSTOMERS}:sort:agent_sim,id": (JSON_SORT, 200, 200),
    "buma_agent_list_view:sort:number,id": (JSON_SORT, 200, 200),
    "buma_transaction_list_view:sort:payed,id": (NULL_FIRST, 200, 200),
    # PocketBase compares '3' with 3.0 (TEXT affinity, datatype3.html 4.2)
    # and finds nothing; PPBase inlines the number for a text column.
    "buma_customer:filter:id = 3": (TEXT_ID_NUMBER, 200, 500),
    **{f"{view}:filter:id = 3": (TEXT_ID_NUMBER, 200, 500) for view in MULTI_ROW_NUMBERED},
    # No sort: PocketBase serves SQLite's order (the numbering, or the ORDER BY
    # of test), PPBase orders by the first column (ORDER BY 1): the text id.
    **{
        f"{view}:default": ("default_order_is_the_text_id", 200, 200) for view in MULTI_ROW_NUMBERED
    },
    # SQLite plans a view by id of the stock views without the automatic
    # covering index and concatenates in rowid order; PPBase serves the list's.
    "buma_article_with_no_stock_list_view:by_id": (
        "view_by_id_concatenates_in_rowid_order",
        200,
        200,
    ),
    "buma_stockage_no_manager:by_id": ("view_by_id_concatenates_in_rowid_order", 200, 200),
}
# Pinned probes whose PPBase answer 1789236851 changes (every other pinned
# probe must be answered as before it).
CHANGED_BY_1789236851 = {
    *(f"{view}:filter:id = 3" for view in MULTI_ROW_NUMBERED),
    *(f"{view}:default" for view in MULTI_ROW_NUMBERED),
    "buma_article_with_no_stock_list_view:by_id",
    "buma_stockage_no_manager:by_id",
}

SHAPES = (
    "",
    "1e3",
    "12.50",
    "-5",
    "070000009",
    "+22370000010",
    " 70000011",
    "70000012 ",
    "null",
    "true",
    "[1,2]",
    '"x"',
    '{"b":1,"a":2}',
    "123456789012345678901234567890",
)
SALE_PHASES = {
    # (amount, payed, remaining, status) of the three sales, in rowid order.
    "integer_truncation": ((100, 100, 0, True), (200, 40, 160, True), (50, 0, 50, False)),
    "negative_truncation": ((100, 100, 0, True), (200, 40, -160, True), (50, 0, 50, False)),
    "real_operand": ((100, 100, 0, True), (200, 40, 160.5, True), (50, 0, 50, False)),
    "zero_divisor": ((0, 0, 0, True), (0, 0, 160, True), (50, 0, 50, False)),
    "no_status_true_row": ((100, 100, 0, False), (200, 40, 160, False), (50, 0, 50, False)),
    # SQLite adds reals with compensation, PostgreSQL plainly.
    "compensated_sum": ((0.1, 0.1, 0.1, True), (0.2, 0.2, 0.2, True), (0.3, 0.3, 0.3, True)),
    # A real of at least 1e6: PocketBase serves Go's shortest 'g' text.
    "large_real": (
        (1500000.5, 0, 1500000.5, True),
        (2000000.25, 0, 0.25, True),
        (50, 0, 50, False),
    ),
}
SALE_INFO_FIELDS = (
    "totalAmount",
    "totalPayed",
    "totalNotPayed",
    "totalSale",
    "totalProfit",
    "payedPercent",
)


def ident(prefix: str, number: int) -> str:
    """A 15-character synthetic record id."""
    return f"{prefix}{number:0{15 - len(prefix)}d}"


def stamp(day: int, second: int = 0, month: str = "2024-03") -> str:
    return f"{month}-{day:02d} 10:00:{second:02d}.000Z"


def dataset() -> list[tuple[str, dict]]:
    """Synthetic records in insertion (rowid) order."""
    rows: list[tuple[str, dict]] = []
    add = lambda collection, **data: rows.append((collection, data))  # noqa: E731
    users = [ident("wshuser", i) for i in (1, 2)]
    for i, user in enumerate(users, 1):
        add(
            "users",
            id=user,
            username=f"wshuser{i}",
            email=f"wsh{i}@example.invalid",
            name=f"WSH user {i}",
            password=True,
        )
    agents = [ident("wshagent", i) for i in (1, 2, 3)]
    for i, agent in enumerate(agents, 1):
        add("buma_agent", id=agent, created=stamp(3, i), firstname=f"Agent{i}", lastname="WSH")
    customers = {i: ident("wshcust", i) for i in range(1, 6 + len(SHAPES))}
    for i, customer in customers.items():
        add(
            "buma_customer",
            id=customer,
            created=stamp(4, i),
            firstname=f"Client{i}",
            lastname="WSH",
            kind="CUSTOMER_SIMPLE",
            agent=agents[0] if i == 1 else agents[1] if i == 2 else "",
            solde=(i * 37) % 11 * 10,
            status=i != 5,
        )
    sim_no = iter(range(1, 1000))

    def sim(number, network, pdv="", agent="", nafama=False, created=None, updated=None):
        n = next(sim_no)
        add(
            "buma_sim",
            id=ident("wshsim", n),
            created=created or stamp(5, n % 60),
            updated=updated or stamp(6, n % 60),
            number=number,
            network=network,
            pdv=pdv,
            agent=agent,
            isNafama=nafama,
            status=True,
        )

    def pdv(record_id, customer, day, name):
        add("buma_pdv", id=record_id, client=customer, created=stamp(day), name=name)

    # Client 1: first pdv in rowid order is the later one by created and by id.
    pdv("wshpdvz00000001", customers[1], 9, "PDV 1z")
    pdv("wshpdva00000002", customers[1], 8, "PDV 1a")
    sim("72000001", "TELECEL", pdv="wshpdvz00000001")
    sim("72000002", "MOOV_MONEY", pdv="wshpdvz00000001")
    sim("71000001", "RS", pdv="wshpdva00000002")
    # Client 2: one sim, a JSON number.  Client 3: rowid, created and id agree.
    pdv(ident("wshpdv", 3), customers[2], 10, "PDV 2")
    sim("73000001", "RS", pdv=ident("wshpdv", 3))
    pdv(ident("wshpdv", 4), customers[3], 11, "PDV 3a")
    pdv(ident("wshpdv", 5), customers[3], 12, "PDV 3b")
    sim("74000001", "RS", pdv=ident("wshpdv", 4))
    sim("74000002", "TELECEL", pdv=ident("wshpdv", 5))
    # Client 4: a pdv without sim.  Client 5: no pdv.
    pdv(ident("wshpdv", 6), customers[4], 13, "PDV 4")
    for offset, shape in enumerate(SHAPES):
        record = ident("wshpdv", 20 + offset)
        pdv(record, customers[6 + offset], 14, f"PDV shape {offset}")
        sim(shape, "KIOSQUE", pdv=record)
    # Agent 1: greatest network TELECEL first reached by the second sim
    # (isNafama false); the third is created first and has isNafama true.
    sim("79000001", "RS", agent=agents[0], nafama=True, created=stamp(20))
    sim("79000003", "TELECEL", agent=agents[0], nafama=False, created=stamp(22))
    sim("79000002", "TELECEL", agent=agents[0], nafama=True, created=stamp(18))
    sim("070000023", "RS", agent=agents[1], nafama=True)
    # Customer users: same greatest updated, the first in rowid order wins.
    add(
        "buma_customer_user",
        id=ident("wshcu", 2),
        customer=customers[1],
        user=users[1],
        created=stamp(25),
        updated=stamp(26),
    )
    add(
        "buma_customer_user",
        id=ident("wshcu", 1),
        customer=customers[1],
        user=users[0],
        created=stamp(24),
        updated=stamp(26),
    )
    add(
        "buma_customer_user",
        id=ident("wshcu", 3),
        customer=customers[3],
        user=users[0],
        created=stamp(24),
        updated=stamp(27),
    )
    # Articles, categories, storages, stocks (stockages ordered by value).
    articles = [ident("wsharticle", i) for i in (1, 2)]
    for i, article in enumerate(articles, 1):
        add("buma_article", id=article, designation=f"Article {i}", status=True)
    for i in (1, 2):
        add("buma_article_categorie", id=ident("wshcategorie", i), name=f"Cat {i}", status=True)
    storages = [ident("wshstockage", i) for i in (1, 2, 3)]
    for i, storage in enumerate(storages, 1):
        add("buma_article_stockage", id=storage, name=f"Stockage {i}", status=True)
    for n, storage in enumerate((storages[2], storages[0], storages[1], storages[0]), 1):
        add(
            "buma_article_stock",
            id=ident("wshstock", n),
            article=articles[0],
            stockage=storage,
            quantity=n,
            status=True,
        )
    for n, storage in enumerate((storages[1], storages[0]), 1):
        add("buma_stockage_user", id=ident("wshsu", n), user=users[0], stockage=storage)
    # Suppliers (buma_customer_supplier_info_view).
    for i, (solde, status) in enumerate(((150, True), (-40, True), (-5, True), (999, False)), 1):
        add(
            "buma_supplier",
            id=ident("wshsupplier", i),
            firstname=f"Fournisseur{i}",
            solde=solde,
            status=status,
        )
    # Sales (values replaced by each SALE_PHASES phase), returns, transactions.
    sales = [ident("wshsale", i) for i in (1, 2, 3)]
    for sale, (amount, payed, remaining, status) in zip(
        sales, SALE_PHASES["integer_truncation"], strict=True
    ):
        add(
            "buma_sale",
            id=sale,
            customer=customers[1],
            amount=amount,
            payed=payed,
            remaining=remaining,
            status=status,
        )
    returns = [ident("wshreturn", i) for i in (1, 2)]
    for i, (record, customer, payed) in enumerate(
        ((returns[0], customers[1], True), (returns[1], customers[2], False)), 1
    ):
        add(
            "buma_return",
            id=record,
            customer=customer,
            pdv="wshpdvz00000001",
            number=f"R{i}",
            note=f"note {i}",
            amount=100 * i,
            payed=payed,
            article_stock=ident("wshstock", 1),
            status=True,
        )
    for n, (sale, retour, kind) in enumerate(
        (
            (sales[0], "", "SALE"),
            (sales[1], "", "SALE"),
            ("", returns[0], "RETURN"),
            ("", returns[1], "RETURN"),
            ("", "", "SALE"),
            ("wshmissingsale1", "", "SALE"),
        ),
        1,
    ):
        add(
            "buma_transaction",
            id=ident("wshtx", n),
            created=stamp(28, n),
            sale=sale,
            article=articles[0],
            article_stock=ident("wshstock", 1),
            author=users[0],
            sim=ident("wshsim", 4),
            type=kind,
            price=10,
            quantity=n,
            status=True,
            mobile_transaction_date=stamp(28, n),
            **{"return": retour},
        )
    # The view test: sales of August 2025 (status false, so buma_sale_info
    # keeps its three sales) and 12 groups (customer, article), one with a
    # missing article (a NULL article id, first in SQLite's order).
    test_sales = [ident("wshtsale", i) for i in range(1, 6)]
    for i, sale in enumerate(test_sales):
        add(
            "buma_sale",
            id=sale,
            customer=customers[(5, 2, 4, 3, 6)[i]],
            amount=100 + i,
            status=False,
        )
    missing_article = "wshmissingart01"
    for n, (sale, article, quantity, price) in enumerate(
        (
            (4, articles[1], 2, 15),
            (0, articles[0], 1, 10),
            (2, missing_article, 3, 7),
            (1, articles[1], 4, 9),
            (3, articles[0], 5, 11),
            (0, articles[1], 1, 20),
            (2, articles[0], 2, 8),
            (4, articles[0], 6, 3),
            (1, articles[0], 2, 12),
            (3, articles[1], 1, 30),
            (0, missing_article, 2, 5),
            (2, articles[1], 3, 4),
            (1, articles[0], 1, 2),
        ),
        1,
    ):
        add(
            "buma_transaction",
            id=ident("wshttx", n),
            created=stamp(n, n, month="2025-08"),
            sale=test_sales[sale],
            article=article,
            type="SALE",
            price=price,
            quantity=quantity,
            status=True,
        )
    # Caisse expenses: insertion, created and id orders all differ.
    add("buma_caisse", id=ident("wshcaisse", 1), name="Caisse depenses", type="EXPENSE")
    add("buma_caisse", id=ident("wshcaisse", 2), name="Caisse ventes", type="SALE_BUY")
    add("buma_caisse", id=ident("wshcaisse", 3), name="Caisse mixte", type="HYBRID")
    add("buma_caisse_expense_type", id=ident("wshexpense", 1), name="Transport")
    add("buma_caisse_expense_type", id=ident("wshexpense", 2), name="Loyer")
    order = (7, 3, 11, 1, 9, 5, 12, 2, 8, 4, 10, 6)
    for n in order:
        add(
            "buma_caisse_transactions",
            id=ident("wshct", 100 - n),
            created=stamp(10 + (n * 5) % 13, n),
            caisse=ident("wshcaisse", 1),
            expenseType=(ident("wshexpense", 1), ident("wshexpense", 2), "")[n % 3],
            amount=1000 + n,
            type="EXPENSE",
            status=True,
        )
    excluded = (
        {"type": "DEPOSIT"},
        {"status": False},
        {"created": "2023-12-31 10:00:00.000Z"},
        {"caisse": ident("wshcaisse", 2)},
    )
    for n, change in enumerate(excluded, 50):
        values = {
            "id": ident("wshct", n),
            "created": stamp(15, n % 60),
            "caisse": ident("wshcaisse", 1),
            "amount": 5000 + n,
            "type": "EXPENSE",
            "status": True,
        }
        values.update(change)
        add("buma_caisse_transactions", **values)
    # Caisse sessions (buma_caisse_session_info): 12 (caisse, session) groups
    # inserted out of their order, besides the groups of the empty session
    # above; ids "1".."15" sort as text "1", "10".."15", "2"...
    sessions = [ident("wshsession", i) for i in range(1, 13)]
    kinds = (
        "DEPOSIT",
        "WITHDRAWAL",
        "VERSEMENT",
        "RETURN",
        "REVERSAL",
        "EMPLOYEE_DEBT",
        "EMPLOYEE_DEBT_REPAYMENT",
        "DEPOSIT",
    )
    for k in range(26):
        add(
            "buma_caisse_transactions",
            id=ident("wshcs", 99 - k),
            created=stamp(1 + k % 27, k % 60),
            caisse=ident("wshcaisse", (2, 3, 1)[(k * 7) % 3]),
            session=sessions[(k * 5) % 12],
            amount=10 * k + 5,
            type="BUY" if k == 24 else kinds[k % 8],
            status=k != 25,
        )
    return rows


def fixture_migration(definitions: list[dict], rows: list[tuple[str, dict]]) -> str:
    saves = []
    for collection, data in rows:
        values = {k: v for k, v in data.items() if k != "password"}
        password = json.dumps(PASSWORD) if data.get("password") else "null"
        saves.append(f"save({json.dumps(collection)},{json.dumps(values)},{password});")
    return (
        "migrate((db)=>{const dao=new Dao(db);dao.importCollections("
        + json.dumps(definitions)
        + ".map(c=>new Collection(c)),true);"
        + "const save=(name,data,password)=>{"
        + "const r=new Record(dao.findCollectionByNameOrId(name),data);"
        + "if(password) r.setPassword(password);dao.saveRecord(r);};"
        + "".join(saves)
        + "},()=>{});\n"
    )


def run(*args, **kwargs) -> str:
    completed = subprocess.run([str(a) for a in args], capture_output=True, text=True, **kwargs)
    if completed.returncode or "Error:" in completed.stdout + completed.stderr:
        raise RuntimeError(f"{Path(str(args[0])).name} {args[1]} failed")
    return completed.stdout


def build_fixture(root: Path, binary: Path, schema: Path) -> Path:
    migrations = root / "pb-migrations"
    migrations.mkdir()
    definitions = json.loads(schema.read_text())
    (migrations / "1789236951_wsh_fixture.js").write_text(fixture_migration(definitions, dataset()))
    data = root / "pb-data"
    run(binary, "migrate", "up", "--dir", data, "--migrationsDir", migrations)
    run(binary, "admin", "create", ADMIN[0], ADMIN[1], "--dir", data)
    with closing(sqlite3.connect(data / "data.db")) as db:
        applied = {r[0] for r in db.execute("SELECT file FROM _migrations")}
        assert "1789236951_wsh_fixture.js" in applied, "PocketBase did not apply the fixture"
    (data / "storage").mkdir(exist_ok=True)  # no file: an empty local storage
    return data


def source_facts(data: Path) -> dict:
    """Storage classes and orders the fixture really has (synthetic values)."""
    with closing(sqlite3.connect(f"file:{data / 'data.db'}?mode=ro", uri=True)) as db:
        version = db.execute("SELECT sqlite_version()").fetchone()[0]
        caisse = db.execute(
            "SELECT id, created FROM buma_caisse_transactions WHERE type='EXPENSE' "
            "AND status=1 AND created >= '2024-01-04' ORDER BY rowid"
        ).fetchall()
        sale = db.execute(
            "SELECT group_concat(typeof(amount) || '/' || typeof(remaining)) FROM buma_sale"
        ).fetchone()[0]
        pdvs = db.execute("SELECT id, created FROM buma_pdv ORDER BY rowid").fetchall()
        numbered = {
            view: db.execute(
                f"SELECT count(*), sum(typeof(id) = 'text') FROM \"{view}\""  # noqa: S608
            ).fetchone()
            for view in NUMBERED
        }
    return {
        "python_sqlite_version_reading_fixture": version,
        "caisse_rowid_order_equals_created_order": [r[1] for r in caisse]
        == sorted(r[1] for r in caisse),
        "caisse_rowid_order_equals_id_order": [r[0] for r in caisse]
        == sorted(r[0] for r in caisse),
        "pdv_rowid_order_equals_created_order": [r[1] for r in pdvs] == sorted(r[1] for r in pdvs),
        "sale_storage_classes": sale,
        "numbered_views_rows_and_text_ids": {k: list(v) for k, v in numbered.items()},
    }


def write_sale_phase_sqlite(data: Path, phase: str) -> None:
    with closing(sqlite3.connect(data / "data.db")) as db:
        for n, (amount, payed, remaining, status) in enumerate(SALE_PHASES[phase], 1):
            db.execute(
                "UPDATE buma_sale SET amount=?, payed=?, remaining=?, status=? WHERE id=?",
                (float(amount), float(payed), float(remaining), int(status), ident("wshsale", n)),
            )
        db.commit()


async def write_sale_phase_postgres(engine, phase: str) -> None:
    async with engine.begin() as conn:
        for n, (amount, payed, remaining, status) in enumerate(SALE_PHASES[phase], 1):
            await conn.execute(
                text(
                    "UPDATE buma_sale SET amount=:a, payed=:p, remaining=:r, status=:s WHERE id=:id"
                ),
                {
                    "a": float(amount),
                    "p": float(payed),
                    "r": float(remaining),
                    "s": status,
                    "id": ident("wshsale", n),
                },
            )


# Curated filters: the forms of the WS-H reviews on the fields whose value or
# type the corrections concern, and the id forms of the numbered views.
FILTERS = (
    *(
        (CUSTOMERS, f"pdv_sim {form}")
        for form in (
            '= ""',
            '!= ""',
            "= null",
            "!= null",
            '= "73000001"',
            "= 73000001",
            f'= "{MULTIPLE}"',
            f'!= "{MULTIPLE}"',
            "= true",
            "= false",
            "!= true",
            "= 1",
            "= 0",
            '> "7"',
            "< 73000002",
            ">= 1000",
            '<= "73000001"',
            '~ "7200"',
            '!~ "7200"',
            '= "x"',
            '= "1e3"',
            "= 1000",
            "= 12.5",
            '= "070000009"',
            "= -5",
            f'?= "{MULTIPLE}"',
            '?= "73000001"',
            '?!= "73000001"',
        )
    ),
    (CUSTOMERS, '"" = pdv_sim'),
    (CUSTOMERS, "73000001 = pdv_sim"),
    (CUSTOMERS, "true = pdv_sim"),
    (CUSTOMERS, "pdv_sim = agent_sim"),
    (CUSTOMERS, "pdv_sim != agent_sim"),
    *(
        (CUSTOMERS, f"agent_sim {form}")
        for form in (
            '= ""',
            '!= ""',
            "= 79000003",
            '= "79000003"',
            '= "070000023"',
            '~ "7900"',
        )
    ),
    *(
        (view, f"{field} {form}")
        for view in (
            "buma_customer_list_view_with_users",
            "buma_customer_mobile_list_view",
            "buma_return_list_view",
        )
        for field in ("pdv_sim", "agent_sim")
        for form in ('= ""', '!= ""', "= 73000001", '= "73000001"', f'= "{MULTIPLE}"')
    ),
    *(
        ("buma_agent_list_view", f"number {form}")
        for form in (
            '= ""',
            '!= ""',
            "= 79000003",
            '= "79000003"',
            '~ "7900"',
            "= true",
        )
    ),
    *(
        ("buma_customer_mobile_list_view", f"isNafama {form}")
        for form in (
            "= true",
            "= false",
            "!= true",
            "!= false",
            "= 1",
            "= 0",
            "= null",
            "> false",
            '= ""',
            '!= ""',
            '= "1"',
        )
    ),
    ("buma_customer_mobile_list_view", 'pdv_sim_network = "TELECEL,MOOV_MONEY,RS"'),
    ("buma_customer_list_view_with_users", f'assigned_users ~ "{ident("wshuser", 2)}"'),
    *(
        ("buma_transaction_list_view", f"payed {form}")
        for form in (
            "= true",
            "= false",
            "!= true",
            "!= false",
            "= 1",
            "= 0",
            "!= 1",
            "= null",
            '= ""',
        )
    ),
    ("buma_transaction_list_view", "true = payed"),
    *(
        ("buma_transaction_list_view", f"salePayed {form}")
        for form in (
            "= true",
            "= false",
            "!= true",
            "= 1",
            "= 0",
        )
    ),
    *(
        (view, f"id {form}")
        for view in MULTI_ROW_NUMBERED
        for form in ("= 3", '= "3"', '> "3"', '~ "1"', '!= ""', "= null")
    ),
    ("buma_caisse_session_info", f'caisse = "{ident("wshcaisse", 3)}"'),
    ("buma_caisse_session_info", "total_deposit > 100"),
    ("test", f'articleId = "{ident("wsharticle", 1)}"'),
    ("test", "articleId = null"),
    ("buma_article_with_no_stock_list_view", f'stockages ~ "{ident("wshstockage", 3)}"'),
    ("buma_sale_info", "payedPercent > 50"),
    ("buma_sale_info", 'id = "1"'),
    ("buma_return_info_view", 'id = "1"'),
    # A base collection: PPBase answers a number compared with its text id so.
    ("buma_customer", "id = 3"),
    ("buma_customer", f'id = "{ident("wshcust", 3)}"'),
)
SORTS = (
    (CUSTOMERS, "pdv_sim,id"),
    (CUSTOMERS, "-pdv_sim,id"),
    (CUSTOMERS, "agent_sim,id"),
    ("buma_agent_list_view", "number,id"),
    ("buma_customer_mobile_list_view", "-isNafama,id"),
    ("buma_customer_mobile_list_view", "isNafama,id"),
    ("buma_customer_mobile_list_view", "pdv_sim_network,id"),
    ("buma_transaction_list_view", "payed,id"),
    ("buma_transaction_list_view", "-salePayed,id"),
    ("buma_caisse_session_info", "caisse,session"),
    ("buma_caisse_session_info", "-total_deposit,id"),
    ("test", "-totalRevenue,id"),
    ("buma_caisse_transaction_by_categorie_list_view", "-amount,id"),
)


def probes() -> list[tuple[str, str, dict]]:
    """(label, path, params): identical curated requests for every server."""
    items: list[tuple[str, str, dict]] = []
    for view in VIEWS:
        path = f"/api/collections/{view}/records"
        items.append((f"{view}:all", path, {"perPage": 500, "sort": "id"}))
        if view in MULTI_ROW_NUMBERED:
            for page in (1, 2, 3):
                items.append(
                    (f"{view}:page{page}", path, {"page": page, "perPage": 5, "sort": "id"})
                )
            items.append((f"{view}:desc", path, {"perPage": 5, "sort": "-id"}))
            items.append((f"{view}:default", path, {"perPage": 5}))
            items.append((f"{view}:by_id", path + "/3", {}))
        elif view in NUMBERED:
            items.append((f"{view}:by_id", path + "/1", {}))
    items.append(
        (
            "buma_caisse_transaction_by_categorie_list_view:label",
            "/api/collections/buma_caisse_transaction_by_categorie_list_view/records",
            {"filter": 'label = "Transport"', "sort": "id"},
        )
    )
    for view, expression in FILTERS:
        items.append(
            (
                f"{view}:filter:{expression}",
                f"/api/collections/{view}/records",
                {"filter": expression, "sort": "id", "perPage": 500},
            )
        )
    for view, sort in SORTS:
        items.append(
            (
                f"{view}:sort:{sort}",
                f"/api/collections/{view}/records",
                {"sort": sort, "perPage": 500},
            )
        )
    for view, record in (
        ("buma_customer_mobile_list_view", ident("wshcust", 1)),
        ("buma_customer_list_view_with_users", ident("wshcust", 1)),
        ("buma_return_list_view", ident("wshreturn", 1)),
        ("buma_stockage_no_manager", ident("wshuser", 1)),
        ("buma_agent_list_view", ident("wshagent", 2)),
        ("buma_customer_list_view", ident("wshcust", 1)),
        ("buma_article_with_no_stock_list_view", ident("wsharticle", 1)),
        ("buma_transaction_list_view", ident("wshtx", 1)),
    ):
        items.append((f"{view}:by_id", f"/api/collections/{view}/records/{record}", {}))
    return items


# --- Before/after matrix -------------------------------------------------

MATRIX_OPERATORS = ("=", "!=", "?=", "?!=", "~", "<", ">")
COMPARE_OPERATORS = ("=", "!=", "<", ">")
SKIP_KEYS = {"collectionId", "collectionName", "expand"}


def filter_literal(value) -> str | None:
    """The PocketBase filter literal of a served JSON scalar, or None."""
    if value is None:
        return "null"
    if isinstance(value, bool):
        return "true" if value else "false"
    if isinstance(value, int):
        return str(value)
    if isinstance(value, float):
        return repr(value) if math.isfinite(value) else None
    if isinstance(value, str) and not any(c in value for c in "\"'\\\n"):
        return f'"{value}"'
    return None


def number_of(value) -> int | float | None:
    if isinstance(value, bool):
        return None
    if isinstance(value, int | float):
        return value
    if isinstance(value, str) and re.fullmatch(r"-?\d{1,15}(\.\d{1,6})?", value):
        return float(value) if "." in value else int(value)
    return None


def matrix(reference_all: dict[str, list]) -> list[tuple[str, str, dict, dict]]:
    """(label, path, params, meta) of every generated probe (from PocketBase's rows)."""
    items = []
    for view in CHANGED:
        rows = reference_all[view]
        columns = [key for key in rows[0] if key not in SKIP_KEYS]
        path = f"/api/collections/{view}/records"
        for position, column in enumerate(columns):
            literals = ['""', "null", "1", "true", "false"]
            sample = next(
                (
                    row.get(column)
                    for row in rows
                    if row.get(column) not in (None, "", True, False)
                    and not isinstance(row.get(column), list | dict)
                ),
                None,
            )
            if sample is not None:
                as_text = sample if isinstance(sample, str) else json.dumps(sample)
                for value in (as_text, number_of(sample)):
                    literal = filter_literal(value) if value is not None else None
                    if literal:
                        literals.append(literal)
            for operator in MATRIX_OPERATORS:
                for literal in dict.fromkeys(literals):
                    expression = f"{column} {operator} {literal}"
                    items.append(
                        (
                            f"{view}|filter|{expression}",
                            path,
                            {"filter": expression, "sort": "id", "perPage": 500},
                            {
                                "view": view,
                                "kind": "filter",
                                "columns": [column],
                                "operator": operator,
                                "literal": literal,
                            },
                        )
                    )
            directions = (
                (("id", "ASC"), ("-id", "DESC"))
                if column == "id"
                else (
                    (f"{column},id", "ASC"),
                    (f"-{column},-id", "DESC"),
                )
            )
            for sort, direction in directions:
                items.append(
                    (
                        f"{view}|sort|{sort}",
                        path,
                        {"sort": sort, "perPage": 500},
                        {"view": view, "kind": "sort", "columns": [column], "direction": direction},
                    )
                )
            other = columns[(position + 1) % len(columns)]
            if other != column:
                for operator in COMPARE_OPERATORS:
                    expression = f"{column} {operator} {other}"
                    items.append(
                        (
                            f"{view}|compare|{expression}",
                            path,
                            {"filter": expression, "sort": "id", "perPage": 500},
                            {
                                "view": view,
                                "kind": "compare",
                                "columns": [column, other],
                                "operator": operator,
                            },
                        )
                    )
    return items


async def admin_token(client: httpx.AsyncClient) -> str:
    response = await client.post(
        "/api/admins/auth-with-password", json={"identity": ADMIN[0], "password": ADMIN[1]}
    )
    response.raise_for_status()
    return response.json()["token"]


async def answers(base: str, items, *, raw: bool = False, concurrency: int = 6) -> dict:
    out = {}
    # No keep-alive: a server error that closes its connection cannot make
    # the next probe fail with a transport error.
    limits = httpx.Limits(max_keepalive_connections=0, max_connections=concurrency)
    async with httpx.AsyncClient(base_url=base, timeout=120, limits=limits) as client:
        token = await admin_token(client)
        semaphore = asyncio.Semaphore(concurrency)

        async def one(label, path, params):
            async with semaphore:
                started = time.monotonic()
                try:
                    response = await client.get(
                        path, params=params, headers={"Authorization": token}
                    )
                except httpx.TransportError as exc:
                    out[label] = {"status": f"transport_error:{type(exc).__name__}", "body": None}
                    return
                try:
                    body = response.json()
                except ValueError:
                    body = None
                out[label] = {
                    "status": response.status_code,
                    "body": body,
                    "seconds": round(time.monotonic() - started, 3),
                }
                if raw:
                    out[label]["raw"] = response.text

        await asyncio.gather(*(one(*item[:3]) for item in items))
    return out


def sale_info_probe() -> list[tuple[str, str, dict]]:
    return [("buma_sale_info", "/api/collections/buma_sale_info/records", {})]


def records(body) -> list:
    if isinstance(body, dict) and isinstance(body.get("items"), list):
        return body["items"]
    if isinstance(body, dict) and "id" in body:
        return [body]
    return []


def same(left, right) -> bool:
    """JSON value equality: numbers by value, true is not 1, keys in any order."""
    if isinstance(left, bool) or isinstance(right, bool):
        return type(left) is type(right) and left == right
    if isinstance(left, int | float) and isinstance(right, int | float):
        return float(left) == float(right) if math.isfinite(float(left)) else left == right
    if isinstance(left, dict) and isinstance(right, dict):
        return left.keys() == right.keys() and all(same(left[k], right[k]) for k in left)
    if isinstance(left, list) and isinstance(right, list):
        return len(left) == len(right) and all(map(same, left, right))
    return type(left) is type(right) and left == right


_MISSING = object()


def kind(left, right) -> str:
    """How PPBase's value (right) differs from PocketBase's (left)."""
    if isinstance(left, dict) and isinstance(right, dict) and list(left) != list(right):
        return "json_key_order_D-0029" if same(left, right) else "differs"
    if (
        isinstance(left, int | float)
        and isinstance(right, int | float)
        and not isinstance(left, bool)
        and not isinstance(right, bool)
        and type(left) is not type(right)
        and same(left, right)
    ):
        return "json_number_notation_D-0029"
    if same(left, right):
        return "equal"
    if isinstance(right, str) and right != left:
        try:
            parsed = json.loads(right)
        except ValueError:
            parsed = _MISSING
        if parsed is not _MISSING and same(parsed, left):
            number = isinstance(left, int | float) and not isinstance(left, bool)
            return "number_served_as_text_D-0039" if number else "json_text_served_as_text_D-0039"
    if (
        isinstance(left, int)
        and not isinstance(left, bool)
        and left in (0, 1)
        and isinstance(right, bool)
        and int(right) == left
    ):
        return "integer_boolean_served_as_boolean_D-0039"
    return "differs"


def targets_of(view: str, reference_items: list) -> set[str]:
    """The fields compared with PocketBase: the corrected ones, every field of a numbered view."""
    if view in NUMBERED:
        return {key for item in reference_items for key in item} - SKIP_KEYS
    return CORRECTED.get(view, set())


def compare(view: str, reference: dict, mine: dict) -> dict:
    """Outcome of one probe: status, ids in order, then every field of every item."""
    result = {"status": [reference["status"], mine["status"]]}
    left, right = records(reference["body"]), records(mine["body"])
    result["ids_in_order_equal"] = [str(r.get("id")) for r in left] == [
        str(r.get("id")) for r in right
    ]
    pairs = list(zip(left, right, strict=False))
    result["id_types"] = sorted(
        {f"{type(a.get('id')).__name__}/{type(b.get('id')).__name__}" for a, b in pairs}
    )
    for key in ("totalItems", "page", "perPage"):
        if isinstance(reference["body"], dict) and key in reference["body"]:
            result[key] = [reference["body"].get(key), (mine["body"] or {}).get(key)]
    fields: dict[str, dict] = {}
    for a, b in pairs:
        for key in sorted(set(a) | set(b)):
            outcome = kind(a.get(key), b.get(key)) if key in a and key in b else "missing_key"
            counter = fields.setdefault(key, {})
            counter[outcome] = counter.get(outcome, 0) + 1
    result["fields"] = fields
    result["items"] = [len(left), len(right)]
    targets = targets_of(view, left)
    accepted = {"equal"} | NOTATION | D0039
    ids = [r.get("id") for r in left]
    if len(set(map(str, ids))) < len(ids):
        # Duplicated ids (a view row per joined row): compare the target fields
        # of the rows as multisets, the order of equal ids being unspecified.
        def rows_of(items, loose):
            return sorted(
                json.dumps(
                    [str(r.get("id")), *[loose(r.get(f)) for f in sorted(targets)]],
                    sort_keys=True,
                )
                for r in items
            )

        result["duplicate_ids"] = True
        result["target_fields_equal"] = rows_of(left, loosen) == rows_of(right, loosen)
        result["other_fields_differing"] = []
        return result
    result["target_fields_equal"] = len(left) == len(right) and all(
        set(fields.get(f, {"equal": 1})) <= accepted for f in targets
    )
    # Other fields compared position by position: only when the ids align.
    result["other_fields_differing"] = (
        sorted(f for f, c in fields.items() if f not in targets and set(c) - accepted)
        if result["ids_in_order_equal"]
        else []
    )
    return result


def compare_all(reference: dict, mine: dict) -> dict:
    return {
        label: compare(label.split(":", 1)[0], reference[label], mine[label]) for label in reference
    }


def not_equal(comparison: dict) -> list[str]:
    return sorted(
        label
        for label, c in comparison.items()
        if c["status"][0] != c["status"][1]
        or not c["ids_in_order_equal"]
        or not c["target_fields_equal"]
    )


def summary(comparison: dict) -> dict:
    kinds: dict[str, Counter] = {}
    for label, c in comparison.items():
        view = label.split(":", 1)[0]
        for field, outcomes in c["fields"].items():
            for outcome, count in outcomes.items():
                if outcome in D0039 | NOTATION:
                    kinds.setdefault(outcome, Counter())[f"{view}.{field}"] += count
    return {
        "probes": len(comparison),
        "status_equal": sum(1 for c in comparison.values() if c["status"][0] == c["status"][1]),
        "ids_in_order_equal": sum(1 for c in comparison.values() if c["ids_in_order_equal"]),
        "target_fields_equal": sum(1 for c in comparison.values() if c["target_fields_equal"]),
        "not_equal": not_equal(comparison),
        "other_fields_differing": sorted(
            {
                f"{label.split(':', 1)[0]}.{f}"
                for label, c in comparison.items()
                for f in c["other_fields_differing"]
            }
        ),
        "documented_value_forms": {k: dict(sorted(v.items())) for k, v in sorted(kinds.items())},
    }


# --- Matrix verdicts -------------------------------------------------------


def loosen(value):
    """A served value with D-0039's forms folded (1/0 and true/false, JSON text)."""
    if isinstance(value, bool):
        return int(value)
    if isinstance(value, int | float):
        return float(value)
    if isinstance(value, str):
        try:
            parsed = json.loads(value)
        except ValueError:
            return value
        return loosen(parsed) if not isinstance(parsed, str) else value
    if isinstance(value, list):
        return [loosen(v) for v in value]
    if isinstance(value, dict):
        return {k: loosen(v) for k, v in sorted(value.items())}
    return value


def content(record: dict, *, drop: set) -> str:
    kept = {k: loosen(v) for k, v in record.items() if k not in SKIP_KEYS | drop}
    return json.dumps(kept, sort_keys=True)


def signature(meta: dict, answer: dict):
    """What a probe served: ids in order, or (numbered views) the rows, their
    corrected fields (the id included) aside."""
    items = records(answer["body"])
    if meta["view"] not in NUMBERED:
        return [str(item.get("id")) for item in items]
    drop = CORRECTED[meta["view"]]
    rows = sorted(content(item, drop=drop) for item in items)
    if meta["kind"] == "sort" and meta["columns"][0] == "id":
        return [content(item, drop=drop) for item in items]
    if meta["kind"] == "sort":
        column = meta["columns"][0]
        return [json.dumps(loosen(item.get(column))) for item in items], rows
    return rows


class Unmodeled(Exception):
    pass


def pg_text(value):
    """The text PostgreSQL gives a served value (``column::text``)."""
    if value is None:
        return None
    if isinstance(value, bool):
        return "true" if value else "false"
    if isinstance(value, float) and value.is_integer():
        return str(int(value))
    if isinstance(value, int | float):
        return repr(value)
    if isinstance(value, str):
        return value
    raise Unmodeled("json value")


def parse_literal(literal: str):
    if literal == "null":
        return None
    if literal in ("true", "false"):
        return literal == "true"
    if literal.startswith('"'):
        return literal[1:-1]
    return float(literal) if "." in literal else int(literal)


def ilike(value: str, pattern: str) -> bool:
    """PostgreSQL ILIKE (PPBase renders ~ as ILIKE '%value%')."""
    regex = "".join(".*" if c == "%" else "." if c == "_" else re.escape(c) for c in pattern)
    return re.fullmatch(regex, value, re.S | re.I) is not None


def literal_text(value) -> str | None:
    """``literal::text``: PPBase inlines TRUE/FALSE and numbers, binds strings."""
    if value is None:
        return None
    if isinstance(value, bool):
        return "true" if value else "false"
    return str(value)


def ordered(left, right) -> int:
    """PostgreSQL's comparison of two non-NULL values of the same SQL type."""
    for kind_ in (str, bool):
        if isinstance(left, kind_) and isinstance(right, kind_):
            return (left > right) - (left < right)
    numbers = (int, float)
    if (
        isinstance(left, numbers)
        and isinstance(right, numbers)
        and not isinstance(left, bool)
        and not isinstance(right, bool)
    ):
        return (left > right) - (left < right)
    raise Unmodeled("types")


def holds(operator: str, left, right, *, literal: bool) -> bool:
    """PPBase's SQL for ``column <operator> right`` on one row (NULL is false)."""
    if literal and right is None and operator in ("=", "!="):
        return (left is None) == (operator == "=")
    if operator in ("=", "!=", "<", ">"):
        if left is None or right is None:
            return False
        sign = ordered(left, right)
        return {"=": sign == 0, "!=": sign != 0, "<": sign < 0, ">": sign > 0}[operator]
    if operator == "~":
        if right is None or left is None:
            return False
        return ilike(pg_text(left), f"%{right}%")  # Python's str of the literal
    if operator in ("?=", "?!="):
        # CASE ... ELSE literal::text <op> column::text END (a scalar column).
        if left is None or right is None:
            return False
        equal = pg_text(left) == literal_text(right)
        return equal if operator == "?=" else not equal
    raise Unmodeled(operator)


def sort_key(value):
    """PostgreSQL's default order: NULL after every value (ASC), first when reversed."""
    if value is None:
        return (1, 0)
    if isinstance(value, bool | int | float | str):
        return (0, value)
    raise Unmodeled("sort type")


def sorted_rows(rows: list, key, *, reverse: bool = False) -> list:
    try:
        return sorted(rows, key=key, reverse=reverse)
    except TypeError as exc:  # values of several SQL types in one column
        raise Unmodeled("mixed types") from exc


def predicted(meta: dict, side_all: list):
    """The signature PPBase's SQL gives on ``side_all`` (the rows that side serves)."""
    kind_, columns = meta["kind"], meta["columns"]
    if kind_ == "sort":
        column = columns[0]
        reverse = meta["direction"] == "DESC"
        if column == "id":
            chosen = sorted_rows(side_all, lambda r: sort_key(r.get("id")), reverse=reverse)
        else:
            chosen = sorted_rows(
                side_all,
                lambda r: (sort_key(r.get(column)), sort_key(r.get("id"))),
                reverse=reverse,
            )
        return signature(meta, {"body": {"items": chosen}})
    if kind_ == "filter":
        right = parse_literal(meta["literal"])
        chosen = [
            r for r in side_all if holds(meta["operator"], r.get(columns[0]), right, literal=True)
        ]
    else:
        chosen = [
            r
            for r in side_all
            if holds(meta["operator"], r.get(columns[0]), r.get(columns[1]), literal=False)
        ]
    chosen = sorted_rows(chosen, lambda r: sort_key(r.get("id")))
    return signature(meta, {"body": {"items": chosen}})


def new_error_rule(meta: dict, after_all: list) -> str | None:
    """A new error that is the documented consequence of a text id."""
    if meta["view"] not in NUMBERED or "id" not in meta["columns"]:
        return None
    if meta["kind"] == "filter" and not meta["literal"].startswith('"'):
        if meta["literal"] != "null" and meta["operator"] != "~":
            return "number_or_boolean_compared_with_text_id"
    if meta["kind"] == "compare":
        other = meta["columns"][1] if meta["columns"][0] == "id" else meta["columns"][0]
        values = [r.get(other) for r in after_all]
        if all(v is None or isinstance(v, int | float | bool) for v in values):
            return "text_id_compared_with_a_number_or_boolean_column"
    return None


def verdicts(items, reference, before, after, alls) -> dict:
    """Verdict of every matrix probe (see the module docstring, proof 1)."""
    out, counts, failures = {}, Counter(), []
    for label, _, _, meta in items:
        view = meta["view"]
        b, a, p = before[label], after[label], reference[label]
        statuses = [p["status"], b["status"], a["status"]]
        if b["status"] != 200 and a["status"] != 200:
            verdict = "error_unchanged" if b["status"] == a["status"] else "error_changed"
        elif b["status"] != 200:
            verdict = "error_now_answered"
        elif a["status"] != 200:
            rule = new_error_rule(meta, alls["after"][view])
            verdict = f"new_error:{rule}" if rule else "NEW_ERROR"
        elif signature(meta, b) == signature(meta, a):
            verdict = "unchanged"
        elif set(meta["columns"]) & CORRECTED.get(view, set()):
            try:
                explained = predicted(meta, alls["before"][view]) == signature(
                    meta, b
                ) and predicted(meta, alls["after"][view]) == signature(meta, a)
                verdict = "explained_by_corrected_values" if explained else "UNEXPLAINED"
            except Unmodeled as exc:
                verdict = f"UNMODELED:{exc}"
        elif p["status"] == 200 and signature(meta, p) == signature(meta, a):
            verdict = "now_equal_to_pocketbase"
        else:
            verdict = "UNEXPLAINED"
        bucket = verdict.split(":", 1)[0] if verdict.startswith("new_error") else verdict
        counts[bucket if not bucket.startswith("UNMODELED") else "UNMODELED"] += 1
        record = {"verdict": verdict, "statuses_pocketbase_before_after": statuses}
        record["after_equal_to_pocketbase"] = p["status"] == a["status"] and (
            a["status"] != 200 or signature(meta, p) == signature(meta, a)
        )
        if verdict.startswith(("UNMODELED", "NEW_ERROR", "UNEXPLAINED", "error_changed")):
            failures.append(label)
            record["items_pocketbase_before_after"] = [
                [str(item.get("id")) for item in records(answer["body"])][:20]
                for answer in (p, b, a)
            ]
        out[label] = record
    return {"verdicts": dict(sorted(counts.items())), "failures": failures, "probes": out}


def matrix_summary(result: dict, items) -> dict:
    """Aggregates of the matrix: new errors by rule, answers equal to PocketBase."""
    rules = Counter()
    after_vs_pocketbase = Counter()
    for label, _, _, meta in items:
        record = result["probes"][label]
        if record["verdict"].startswith("new_error:"):
            rules[f"{meta['view']}:{record['verdict'].split(':', 1)[1]}"] += 1
        key = "equal" if record["after_equal_to_pocketbase"] else "differs"
        after_vs_pocketbase[f"{meta['view']}.{meta['columns'][0]}:{key}"] += 1
    differing = {k: v for k, v in after_vs_pocketbase.items() if k.endswith(":differs")}
    return {
        "probes": len(items),
        "verdicts": result["verdicts"],
        "failures": result["failures"],
        "new_errors_by_view_and_rule": dict(sorted(rules.items())),
        "after_differs_from_pocketbase_by_column": dict(sorted(differing.items())),
        "after_equal_to_pocketbase": sum(
            1 for r in result["probes"].values() if r["after_equal_to_pocketbase"]
        ),
    }


# --- Pagination ------------------------------------------------------------


def pagination_probes(reference_all: dict) -> list[tuple[str, str, dict, dict]]:
    """Every page of the numbered views with several rows, several sizes and sorts."""
    items = []
    for view in MULTI_ROW_NUMBERED:
        rows = reference_all[view]
        total = len(rows)
        columns = [k for k in rows[0] if k not in SKIP_KEYS | {"id"}]
        sorts = (None, "id", "-id", f"{columns[0]},id", f"-{columns[-1]},-id")
        for sort in sorts:
            for per_page in (1, 2, 3, 7):
                for page in range(1, math.ceil(total / per_page) + 1):
                    params = {"page": page, "perPage": per_page}
                    if sort:
                        params["sort"] = sort
                    label = f"{view}|pages|{sort}|{per_page}|{page}"
                    meta = {"view": view, "sort": sort, "per_page": per_page, "page": page}
                    items.append((label, f"/api/collections/{view}/records", params, meta))
    return items


def pagination_check(items, answer: dict, side_all: dict) -> dict:
    groups: dict[tuple, list] = {}
    for label, _, _, meta in items:
        groups.setdefault((meta["view"], meta["sort"], meta["per_page"]), []).append(label)
    out, failures = {}, []
    for (view, sort, per_page), labels in groups.items():
        served = [item for label in labels for item in records(answer[label]["body"])]
        statuses = {answer[label]["status"] for label in labels}
        ids = Counter(str(item.get("id")) for item in served)
        expected = Counter(str(item.get("id")) for item in side_all[view])
        rows = Counter(content(item, drop=set()) for item in served)
        expected_rows = Counter(content(item, drop=set()) for item in side_all[view])
        key = f"{view}|{sort}|{per_page}"
        out[key] = {
            "pages": len(labels),
            "statuses": sorted(statuses),
            "served": len(served),
            "rows": sum(expected.values()),
            "ids_served_twice": sum(1 for n in ids.values() if n > 1),
            "ids_never_served": sum(1 for i in expected if i not in ids),
            "rows_equal_to_the_full_listing": rows == expected_rows,
        }
        if statuses != {200} or ids != expected or rows != expected_rows:
            failures.append(key)
    return {"groups": len(out), "failures": failures, "by_view_sort_size": out}


# --- Servers ---------------------------------------------------------------


class PocketBase:
    def __init__(self, binary: Path, data: Path, root: Path):
        self.binary, self.data, self.root = binary, data, root

    async def __aenter__(self):
        port = free_port()
        self.base = f"http://127.0.0.1:{port}"
        self.log = (self.root / f"pocketbase-{port}.log").open("w")
        self.process = subprocess.Popen(
            [
                str(self.binary),
                "serve",
                "--http",
                f"127.0.0.1:{port}",
                "--dir",
                str(self.data),
                "--hooksDir",
                str(self.root / "no-hooks"),
                "--publicDir",
                str(self.root / "no-public"),
                "--automigrate=false",
            ],
            cwd=self.root,
            stdout=self.log,
            stderr=subprocess.STDOUT,
        )
        try:
            await wait_health(self.process, self.base, "PocketBase")
        except BaseException:
            stop(self.process)
            self.log.close()
            raise
        return self

    async def __aexit__(self, *exc):
        stop(self.process)
        self.log.close()


class PPBase:
    def __init__(self, url: str, published: Path, root: Path, label: str):
        self.url, self.published, self.root, self.label = url, published, root, label

    async def __aenter__(self):
        cwd = self.root / f"ppbase-cwd-{self.label}"
        cwd.mkdir()
        port = free_port()
        self.base = f"http://127.0.0.1:{port}"
        self.log = (self.root / f"ppbase-{self.label}.log").open("w")
        self.process = subprocess.Popen(
            [
                sys.executable,
                "-m",
                "ppbase",
                "serve",
                "--db",
                self.url,
                "--dir",
                str(self.published),
                "--migrations-dir",
                str(MIGRATIONS),
                "--hooks",
                "app.hooks.register:register",
                "--host",
                "127.0.0.1",
                "--port",
                str(port),
            ],
            cwd=cwd,
            env=ppbase_server_environment(),
            stdout=self.log,
            stderr=subprocess.STDOUT,
        )
        try:
            await wait_health(self.process, self.base, "PPBase")
        except BaseException:
            stop(self.process)
            self.log.close()
            raise
        return self

    async def __aexit__(self, *exc):
        stop(self.process)
        self.log.close()


COLUMN_TYPES = """
SELECT c.relname, a.attnum, a.attname, format_type(a.atttypid, a.atttypmod),
       CASE WHEN a.attcollation = 0 THEN NULL
            ELSE (SELECT collname FROM pg_collation WHERE oid = a.attcollation) END
FROM pg_attribute a JOIN pg_class c ON c.oid = a.attrelid
WHERE c.relnamespace = 'public'::regnamespace AND c.relkind = 'v'
  AND a.attnum > 0 AND NOT a.attisdropped
ORDER BY 1, 2
"""
# The type changes 1789236851 makes (view.column -> (before, after)).
EXPECTED_TYPE_CHANGES = {
    **{f"{view}.id": (["bigint", None], ["text", "C"]) for view in NUMBERED},
    "buma_customer_mobile_list_view.uvUser": (["name", "C"], ["text", "default"]),
}


async def column_types(engine) -> dict:
    async with engine.connect() as conn:
        rows = (await conn.execute(text(COLUMN_TYPES))).all()
    return {f"{r[0]}.{r[2]}": [r[3], r[4]] for r in rows}


async def text_order_is_byte_order(engine) -> bool:
    """The text comparison the model assumes (musl: every locale compares bytes)."""
    async with engine.connect() as conn:
        return (
            await conn.execute(
                text("SELECT 'B' < 'a' AND 'Z_' < 'Za' AND '1,2' < '12' AND 'a b' < 'a-b'")
            )
        ).scalar_one()


async def without_archive(engine) -> dict:
    """Orders served when _buma_c11_rowids is absent (rolled back afterwards)."""
    async with engine.connect() as conn:
        transaction = await conn.begin()
        try:
            await conn.execute(text("ALTER TABLE _buma_c11_rowids RENAME TO _wsh_hidden_rowids"))
            ids = (
                (
                    await conn.execute(
                        text(
                            "SELECT v.amount FROM buma_caisse_transaction_by_categorie_list_view v "
                            "ORDER BY v.id::bigint"
                        )
                    )
                )
                .scalars()
                .all()
            )
            created_order = (
                (
                    await conn.execute(
                        text(
                            "SELECT b.amount FROM buma_caisse_transactions b "
                            "JOIN buma_caisse c ON c.id = b.caisse "
                            "WHERE b.type = 'EXPENSE' AND b.status AND c.type = 'EXPENSE' "
                            "AND b.created >= '2024-01-04' ORDER BY b.created, b.id"
                        )
                    )
                )
                .scalars()
                .all()
            )
            pdv_sim = (
                await conn.execute(
                    text("SELECT pdv_sim FROM buma_customer_list_view WHERE id = :id"),
                    {"id": ident("wshcust", 1)},
                )
            ).scalar_one()
        finally:
            await transaction.rollback()
    return {
        "row_number_follows_created_then_id": list(ids) == list(created_order),
        "client_1_pdv_sim_without_archive": pdv_sim,
    }


def all_rows(answer: dict) -> dict:
    return {view: records(answer[f"{view}:all"]["body"]) for view in VIEWS}


def sale_phase_record(answer: dict) -> dict:
    item = (records(answer["buma_sale_info"]["body"]) or [{}])[0]
    out = {
        "status": answer["buma_sale_info"]["status"],
        **{field: item.get(field) for field in SALE_INFO_FIELDS},
    }
    raw = answer["buma_sale_info"].get("raw") or ""
    out["raw_numbers"] = {
        field: match.group(1)
        for field in ("totalAmount", "totalNotPayed", "payedPercent")
        if (match := re.search(rf'"{field}":([^,}}]*)', raw))
    }
    return out


async def exercise(root: Path, binary: Path, schema: Path, report: dict, start_postgres) -> None:
    data = build_fixture(root, binary, schema)
    report["fixture"] = source_facts(data)
    items = probes()
    sale_phases: dict[str, dict] = {}
    async with PocketBase(binary, data, root) as pocketbase:
        reference = await answers(pocketbase.base, items)
        generated = matrix(all_rows(reference))
        pages = pagination_probes(all_rows(reference))
        reference_matrix = await answers(pocketbase.base, generated)
    for phase in SALE_PHASES:
        write_sale_phase_sqlite(data, phase)
        async with PocketBase(binary, data, root) as pocketbase:
            sale_phases[phase] = {
                "pocketbase": sale_phase_record(
                    await answers(pocketbase.base, sale_info_probe(), raw=True)
                )
            }
    write_sale_phase_sqlite(data, "integer_truncation")
    report["pocketbase_answers"] = {
        "curated_probes": len(reference),
        "matrix_probes": len(generated),
        "statuses": sorted({str(a["status"]) for a in reference.values()}),
    }

    definitions = load_schema(schema)
    diagnostic = diagnose(data, schema, source_version=SOURCE_VERSION)
    assert diagnostic["ready_for_isolated_rehearsal"], diagnostic["blockers"]
    url = start_postgres()
    engine = create_async_engine(url, connect_args={"ssl": "disable"}, hide_parameters=True)
    try:
        applied = await prepare_database(engine, MIGRATIONS)
        files = sorted(p.name for p in MIGRATIONS.glob("*.py") if not p.name.startswith("_"))
        assert applied == files and applied[-1] == MIGRATION, applied
        report["migrations_applied"] = len(applied)
        from scripts.generate_native_baseline import load_schema as translated_schema
        from scripts.verify_complete_schema import read_collections, verify
        from scripts.verify_view_queries import verify as verify_views

        translated = translated_schema(schema, include_views=True)[0]
        complete = verify(translated, await read_collections(url))
        views = await verify_views(url, translated)
        assert complete["ok"], complete["metadata_mismatches"]
        assert all(v["status"] == "OK" for v in views), views
        report["schema"] = {
            "definitions": complete["expected"],
            "views": complete["expected_views"],
            "view_queries_ok": len(views),
        }
        async with engine.begin() as conn:
            for c in definitions:
                if c["type"] != "view":
                    await conn.execute(text(f'DELETE FROM "{c["name"]}"'))
        published = root / "published"
        evidence: dict = {}
        rows = await import_snapshot(
            engine, data, published, diagnostic, definitions, report=evidence
        )
        report["import"] = {"rows": rows}
        async with engine.connect() as conn:
            report["import"]["archive_rows"] = (
                await conn.execute(text("SELECT count(*) FROM _buma_c11_rowids"))
            ).scalar_one()
        report["text_order_is_byte_order"] = await text_order_is_byte_order(engine)
        assert report["text_order_is_byte_order"], "the matrix model assumes byte order"
        types_after = await column_types(engine)

        async with PPBase(url, published, root, "after") as server:
            after = await answers(server.base, items)
            after_matrix = await answers(server.base, generated)
            after_pages = await answers(server.base, pages)
            for phase in SALE_PHASES:
                await write_sale_phase_postgres(engine, phase)
                sale_phases[phase]["after"] = sale_phase_record(
                    await answers(server.base, sale_info_probe(), raw=True)
                )
            await write_sale_phase_postgres(engine, "integer_truncation")
        comparison = compare_all(reference, after)
        report["after_1789236851"] = {"summary": summary(comparison), "probes": comparison}
        remaining = report["after_1789236851"]["summary"]["not_equal"]
        report["after_1789236851"]["known_differences"] = {
            label: (
                dict(
                    zip(
                        ("reason", "pocketbase_status", "ppbase_status"),
                        KNOWN_AFTER[label],
                        strict=True,
                    )
                )
                if label in KNOWN_AFTER
                else {
                    "reason": "UNEXPECTED",
                    "statuses": [reference[label]["status"], after[label]["status"]],
                }
            )
            for label in remaining
        }
        report["pagination_after"] = pagination_check(pages, after_pages, all_rows(after))
        report["without_archive"] = await without_archive(engine)

        down = await run_migrations_down(engine, MIGRATIONS, count=1)
        assert down == [MIGRATION], down
        types_before = await column_types(engine)
        async with PPBase(url, published, root, "before") as server:
            before = await answers(server.base, items)
            before_matrix = await answers(server.base, generated)
            before_pages = await answers(server.base, pages)
            for phase in SALE_PHASES:
                await write_sale_phase_postgres(engine, phase)
                sale_phases[phase]["before"] = sale_phase_record(
                    await answers(server.base, sale_info_probe(), raw=True)
                )
            await write_sale_phase_postgres(engine, "integer_truncation")
        comparison_before = compare_all(reference, before)
        not_equal_before = set(not_equal(comparison_before))
        report["before_1789236851_after_down"] = {
            "summary": summary(comparison_before),
        }
        report["curated_regressions"] = {
            "equal_to_pocketbase_before_and_not_after": sorted(set(remaining) - not_equal_before),
            "status_200_before_and_not_after": sorted(
                label
                for label in reference
                if before[label]["status"] == 200 and after[label]["status"] != 200
            ),
            "fixed_by_1789236851": sorted(not_equal_before - set(remaining)),
        }
        report["pagination_before"] = pagination_check(pages, before_pages, all_rows(before))
        alls = {"before": all_rows(before), "after": all_rows(after)}
        result = verdicts(generated, reference_matrix, before_matrix, after_matrix, alls)
        report["matrix"] = matrix_summary(result, generated)
        report["matrix_probes"] = result["probes"]

        changed = {
            k: [types_before.get(k), types_after.get(k)]
            for k in sorted(set(types_before) | set(types_after))
            if types_before.get(k) != types_after.get(k)
        }
        up = await run_migrations_up(engine, MIGRATIONS)
        assert up == [MIGRATION], up
        types_replay = await column_types(engine)
        report["column_types"] = {
            "view_columns": len(types_after),
            "changed_by_1789236851": changed,
            "only_the_expected_changes": changed
            == {k: list(v) for k, v in EXPECTED_TYPE_CHANGES.items()},
            "up_again_equals_first_up": types_replay == types_after,
        }
        async with PPBase(url, published, root, "replay") as server:
            replay = await answers(server.base, items)
        report["replay_after_up_equal_to_first_after"] = all(
            replay[k]["status"] == after[k]["status"] and replay[k]["body"] == after[k]["body"]
            for k in after
        )
        report["sale_info_phases"] = sale_phases
        report["sale_info_after_equal_to_pocketbase"] = {
            phase: {
                field: same(sides["pocketbase"][field], sides["after"][field])
                for field in SALE_INFO_FIELDS
            }
            for phase, sides in sale_phases.items()
        }
        report["timings_seconds_after"] = {
            k.split(":", 1)[0]: v.get("seconds") for k, v in after.items() if k.endswith(":all")
        }
        report["filters_and_sorts"] = {
            label: {
                side: {
                    "status": answer[label]["status"],
                    "totalItems": (answer[label]["body"] or {}).get("totalItems")
                    if isinstance(answer[label]["body"], dict)
                    else None,
                    "ids": [r.get("id") for r in records(answer[label]["body"])],
                }
                for side, answer in (
                    ("pocketbase", reference),
                    ("after", after),
                    ("before", before),
                )
            }
            for label in reference
            if ":filter:" in label or ":sort:" in label or ":by_id" in label or ":default" in label
        }
        for sides in report["filters_and_sorts"].values():
            # The same rows in the same order (their corrected values aside).
            sides["same_answer_before"] = (
                sides["before"]["status"] == sides["after"]["status"]
                and sides["before"]["ids"] == sides["after"]["ids"]
            )
        report["served_examples"] = examples(reference, after, before)
    finally:
        await engine.dispose()
    check(report)


def check(report: dict) -> None:
    """The assertions of the proof, after the report is complete."""
    known = report["after_1789236851"]["known_differences"]
    unexpected = sorted(k for k, v in known.items() if v["reason"] == "UNEXPECTED")
    report["unexpected_differences"] = unexpected
    pinned = set(KNOWN_AFTER)
    report["pins_not_reached"] = sorted(pinned - set(known))
    assert not unexpected, unexpected
    assert not report["pins_not_reached"], report["pins_not_reached"]
    probes_ = report["after_1789236851"]["probes"]
    for label, (_, pocketbase_status, ppbase_status) in KNOWN_AFTER.items():
        assert probes_[label]["status"] == [pocketbase_status, ppbase_status], label
    changed = sorted(
        label
        for label in KNOWN_AFTER
        if not report["filters_and_sorts"].get(label, {}).get("same_answer_before", False)
    )
    report["pins_answered_otherwise_before"] = changed
    assert set(changed) <= CHANGED_BY_1789236851, sorted(set(changed) - CHANGED_BY_1789236851)
    assert not report["after_1789236851"]["summary"]["other_fields_differing"], report[
        "after_1789236851"
    ]["summary"]["other_fields_differing"]
    regressions = report["curated_regressions"]
    # Only the documented changes: the stock views by id, a number compared
    # with a text id.
    assert set(regressions["equal_to_pocketbase_before_and_not_after"]) <= CHANGED_BY_1789236851
    assert set(regressions["status_200_before_and_not_after"]) == {
        f"{view}:filter:id = 3" for view in MULTI_ROW_NUMBERED
    }, regressions["status_200_before_and_not_after"]
    assert not report["matrix"]["failures"], report["matrix"]["failures"][:20]
    assert not report["pagination_after"]["failures"], report["pagination_after"]["failures"]
    assert report["column_types"]["only_the_expected_changes"], report["column_types"]
    assert report["column_types"]["up_again_equals_first_up"]
    assert report["replay_after_up_equal_to_first_after"]
    for phase, fields in report["sale_info_after_equal_to_pocketbase"].items():
        assert fields["payedPercent"], phase


def examples(reference: dict, after: dict, before: dict) -> dict:
    """Synthetic served values of the corrected fields on the three servers."""
    out = {}
    for label in reference:
        view = label.split(":", 1)[0]
        if not label.endswith(":all") or view not in CORRECTED:
            continue
        fields = CORRECTED[view] | ({"id"} if view in NUMBERED else set())
        rows = []
        for a, b, c in zip(
            records(reference[label]["body"]),
            records(after[label]["body"]),
            records(before[label]["body"]),
            strict=False,
        ):
            rows.append(
                {
                    f: {"pocketbase": a.get(f), "ppbase_after": b.get(f), "ppbase_before": c.get(f)}
                    for f in sorted(fields)
                    if f in a or f in b
                }
            )
        out[label] = rows[:20]
    return out


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--pocketbase", type=Path, required=True)
    parser.add_argument("--schema", type=Path, required=True)
    parser.add_argument("--report", type=Path, required=True)
    parser.add_argument("--private-logs", type=Path)
    args = parser.parse_args()
    if args.report.exists():
        parser.error("Report must be a new path")
    if args.private_logs is not None and args.private_logs.exists():
        parser.error("Private log directory must be a new path")
    if run(args.pocketbase, "--version").strip() != f"pocketbase version {SOURCE_VERSION}":
        parser.error(f"This proof requires PocketBase {SOURCE_VERSION}")
    name = "buma-c11-ws-h-" + uuid.uuid4().hex[:12]
    password = secrets.token_hex(16)
    ppbase_root = Path(__import__("ppbase").__file__).resolve().parents[1]
    report: dict = {
        "timestamp_utc": datetime.now(UTC).isoformat(),
        "scope": "c11_ws_h_view_sqlite_semantics_synthetic_paired_comparison",
        "production_data_used": False,
        "real_backup_read": False,
        "pocketbase": {"version": SOURCE_VERSION, "binary_sha256": digest(args.pocketbase)},
        "migration": MIGRATION,
        "ppbase": {
            "package": str(ppbase_root / "ppbase"),
            "commit": subprocess.run(
                ["git", "-C", str(ppbase_root), "rev-parse", "HEAD"],
                capture_output=True,
                text=True,
            ).stdout.strip(),
        },
        "status": "failed",
    }
    created = False

    def start_postgres() -> str:
        nonlocal created
        run(
            "docker",
            "run",
            "--detach",
            "--rm",
            "--name",
            name,
            "--label",
            "buma.scope=c11-ws-h-view-semantics",
            "--label",
            "buma-c11=ws-h",
            "--publish",
            "127.0.0.1::5432",
            "--env",
            "POSTGRES_USER=c11",
            "--env",
            f"POSTGRES_PASSWORD={password}",
            "--env",
            "POSTGRES_DB=buma_c11_rehearsal",
            "postgres:16-alpine",
        )
        created = True
        port = run("docker", "port", name, "5432/tcp").strip().rsplit(":", 1)[1]
        for _ in range(150):
            try:
                run("docker", "exec", name, "pg_isready", "-U", "c11", "-h", "127.0.0.1")
                break
            except RuntimeError:
                time.sleep(0.2)
        report["postgres"] = {"image": "postgres:16-alpine", "container_label": "c11-ws-h"}
        return f"postgresql+asyncpg://c11:{password}@127.0.0.1:{port}/buma_c11_rehearsal"

    started = time.monotonic()
    try:
        with tempfile.TemporaryDirectory(prefix="buma-c11-ws-h-") as folder:
            try:
                asyncio.run(
                    exercise(
                        Path(folder),
                        args.pocketbase.resolve(),
                        args.schema.resolve(),
                        report,
                        start_postgres,
                    )
                )
                report["status"] = "passed"
            finally:
                if args.private_logs is not None:
                    args.private_logs.mkdir(mode=0o700)
                    for log in Path(folder).glob("*.log"):
                        shutil.copyfile(log, args.private_logs / log.name)
                        (args.private_logs / log.name).chmod(0o600)
    except BaseException as exc:
        report["failure_type"] = type(exc).__name__
        report["failure"] = str(exc)[:4000]
        raise
    finally:
        if created:
            subprocess.run(["docker", "stop", "--time", "5", name], capture_output=True)
            report["container_removed"] = (
                subprocess.run(
                    ["docker", "inspect", name], capture_output=True, text=True
                ).returncode
                != 0
            )
        report["seconds"] = round(time.monotonic() - started, 1)
        write_json_new(args.report, report)
        print(json.dumps({"status": report["status"], "report": str(args.report)}))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
