#!/usr/bin/env python3
"""WS-H runtime proof of migration 1789236851 against the PocketBase 0.22.34 binary.

Synthetic data only (no real backup is read). PocketBase 0.22.34 builds the
source itself: the corrected export imported by a JS migration, then records
saved in a chosen order (rowid order, created order and id order differ on
purpose), an admin created by its CLI. Its answers for the ten views are
recorded over HTTP, then the stopped snapshot is diagnosed and imported by
``scripts.c11_import.import_snapshot`` into an owned, disposable PostgreSQL 16
(labelled container, random loopback port, always removed) migrated with every
BUMA migration, and the same requests are sent to PPBase.

Checks: every served value of the ten views (each page sorted by id, default
page, view by id), filters on every changed field (the review forms = "",
!= "", = true, = false, != true, = 1, = 0, id = 3, and the other operators,
values and operand orders) and sorts on them, ``payedPercent`` over five data
phases written in both stores (integer truncation, negative, real, zero
divisor, no row), the order without the rowid archive (SQL only), the
1789236851 operators against SQLite's JSON_EXTRACT comparisons (a grid, with
Python's sqlite3) and what they do outside their domains, then 1789236851 down
(the previous answers: regressions and new errors are listed and pinned) and up
again (same answers). The remaining differences are pinned with their
PocketBase and PPBase statuses (``KNOWN_AFTER``), for a PPBase with or without
WS-A11 (its json sort and empty/NULL json filters). Reports contain field
names, counts and synthetic values only.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import math
import secrets
import shutil
import sqlite3
import subprocess
import sys
import tempfile
import time
import uuid
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
VIEWS = (
    "buma_agent_list_view",
    "buma_customer_list_view",
    "buma_customer_list_view_with_users",
    "buma_customer_mobile_list_view",
    "buma_return_list_view",
    "buma_transaction_list_view",
    "buma_sale_info",
    "buma_caisse_transaction_by_categorie_list_view",
    "buma_article_with_no_stock_list_view",
    "buma_stockage_no_manager",
)
# Fields whose served value 1789236851 changes (codes a to e of WS-H).
TARGET_FIELDS = {
    "buma_agent_list_view": {"number"},
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
    "buma_transaction_list_view": {"payed", "salePayed"},
    "buma_sale_info": {"payedPercent"},
    "buma_caisse_transaction_by_categorie_list_view": {"id", "amount", "created", "label"},
    "buma_article_with_no_stock_list_view": {"stockages"},
    "buma_stockage_no_manager": {"stockage"},
}
# Probes whose answer still differs from PocketBase 0.22.34 after 1789236851:
# label -> (reason, PocketBase status, PPBase status). Any other difference
# fails the run, and so does a status other than the one pinned here.
BY_ID_ROWID_ORDER = "view_by_id_concatenates_in_rowid_order"
KNOWN_AFTER = {
    # PocketBase's plan for a view by id reads the stocks without the automatic
    # covering index (rowid order); its list, which PPBase follows, sorts them.
    "buma_article_with_no_stock_list_view:by_id": (BY_ID_ROWID_ORDER, 200, 200),
    "buma_stockage_no_manager:by_id": (BY_ID_ROWID_ORDER, 200, 200),
    # No sort: PocketBase keeps SQLite's row order, PPBase sorts by the first
    # column (ORDER BY 1), now the text id ("1", "10", "11", "2"...).
    "buma_caisse_transaction_by_categorie_list_view:default": (
        "default_order_is_the_text_id",
        200,
        200,
    ),
    # A number compared with the text id: PocketBase compares '3' with '3.0'
    # (TEXT affinity applied to the float64 parameter) and finds nothing;
    # PostgreSQL has no text = integer. id = "3" works.
    "buma_caisse_transaction_by_categorie_list_view:filter:id = 3": (
        "number_compared_with_text_id_is_an_error",
        200,
        500,
    ),
    # A string compared with a SQLite integer: PocketBase finds nothing
    # (INTEGER < TEXT); PPBase binds the string as a smallint (an error, as
    # with the boolean columns before 1789236851).
    'buma_customer_mobile_list_view:filter:isNafama = "1"': (
        "string_compared_with_sqlite_integer_is_an_error",
        200,
        500,
    ),
    # PocketBase filters JSON_EXTRACT of the stored text, where a JSON value
    # may follow whitespace (' 70000011' is the number 70000011), and serves
    # PrepareValue, for which that text is a string: PPBase holds the served
    # value (no such sim number in the real backup).
    "buma_customer_list_view:filter:pdv_sim < 73000002": (
        "json_text_with_leading_whitespace_filters_as_its_value",
        200,
        200,
    ),
}
# Only on a PPBase without WS-A11 (detected by its json sort): it sorts a json
# field by its own column, and json has no ordering operator; it compares
# x = "" with an untyped parameter, which a smallint cannot take.
KNOWN_AFTER_BEFORE_WS_A11 = {
    "buma_customer_list_view:sort:pdv_sim,id": ("sort_on_json_domain_is_an_error", 200, 500),
    "buma_customer_list_view:sort:-pdv_sim,id": ("sort_on_json_domain_is_an_error", 200, 500),
    "buma_customer_list_view:sort:agent_sim,id": ("sort_on_json_domain_is_an_error", 200, 500),
    "buma_agent_list_view:sort:number,id": ("sort_on_json_domain_is_an_error", 200, 500),
    # A string compared with a SQLite integer, see isNafama = "1"; WS-A11
    # rewrites x = "" of a json field as to_jsonb(x) = '""'.
    'buma_customer_mobile_list_view:filter:isNafama = ""': (
        "string_compared_with_sqlite_integer_is_an_error",
        200,
        500,
    ),
    'buma_transaction_list_view:filter:payed = ""': (
        "string_compared_with_sqlite_integer_is_an_error",
        200,
        500,
    ),
    'buma_customer_mobile_list_view:filter:isNafama != ""': (
        "string_compared_with_sqlite_integer_is_an_error",
        200,
        500,
    ),
    # json != value is not NULL-safe there (PocketBase uses IS NOT): the rows
    # whose payed is NULL are missing; and a NULL sorts last, first in SQLite.
    "buma_transaction_list_view:filter:payed != 1": ("json_not_equal_skips_null_rows", 200, 200),
    "buma_transaction_list_view:sort:payed,id": ("null_sorts_last", 200, 200),
}
# Only on a PPBase with WS-A11, which sorts json fields by
# NULLIF(to_jsonb(column), 'null'): jsonb order (strings before numbers)
# against JSON_EXTRACT's (numbers before text).
KNOWN_AFTER_WITH_WS_A11 = {
    "buma_customer_list_view:sort:pdv_sim,id": ("json_sort_order_is_jsonb_order", 200, 200),
    "buma_customer_list_view:sort:-pdv_sim,id": ("json_sort_order_is_jsonb_order", 200, 200),
    "buma_customer_list_view:sort:agent_sim,id": ("json_sort_order_is_jsonb_order", 200, 200),
    "buma_agent_list_view:sort:number,id": ("json_sort_order_is_jsonb_order", 200, 200),
}
# Probes equal to PocketBase before 1789236851 that may differ after it: a
# view by id of the two stock views (PPBase now serves the list's order, the
# one PocketBase serves for a list; PocketBase serves rowid order by id).
REGRESSIONS = {
    "buma_article_with_no_stock_list_view:by_id",
    "buma_stockage_no_manager:by_id",
}
# Answered 200 before 1789236851 and an error after it.
REGRESSIONS_BEFORE_WS_A11 = {
    label
    for label, (_, _, status) in KNOWN_AFTER_BEFORE_WS_A11.items()
    if ":sort:" in label and status == 500
}


def ppbase_sorts_json_by_jsonb() -> bool:
    """PPBase WS-A11 sorts a json field by NULLIF(to_jsonb(column), 'null')."""
    import inspect

    from ppbase.services import record_service

    return "json_fields" in inspect.signature(record_service._sort_term).parameters


def known_after() -> dict:
    variant = KNOWN_AFTER_WITH_WS_A11 if ppbase_sorts_json_by_jsonb() else KNOWN_AFTER_BEFORE_WS_A11
    return {**KNOWN_AFTER, **variant}


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
}


def ident(prefix: str, number: int) -> str:
    """A 15-character synthetic record id."""
    return f"{prefix}{number:0{15 - len(prefix)}d}"


def stamp(day: int, second: int = 0) -> str:
    return f"2024-03-{day:02d} 10:00:{second:02d}.000Z"


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
            status=True,
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
    # Articles, storages, stocks (stockages ordered by value, one duplicate).
    articles = [ident("wsharticle", i) for i in (1, 2)]
    for i, article in enumerate(articles, 1):
        add("buma_article", id=article, designation=f"Article {i}", status=True)
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
    # Caisse expenses: insertion, created and id orders all differ.
    add("buma_caisse", id=ident("wshcaisse", 1), name="Caisse depenses", type="EXPENSE")
    add("buma_caisse", id=ident("wshcaisse", 2), name="Caisse ventes", type="SALE_BUY")
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
    return {
        "python_sqlite_version_reading_fixture": version,
        "caisse_rowid_order_equals_created_order": [r[1] for r in caisse]
        == sorted(r[1] for r in caisse),
        "caisse_rowid_order_equals_id_order": [r[0] for r in caisse]
        == sorted(r[0] for r in caisse),
        "pdv_rowid_order_equals_created_order": [r[1] for r in pdvs] == sorted(r[1] for r in pdvs),
        "sale_storage_classes": sale,
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


CUSTOMERS = "buma_customer_list_view"
MULTIPLE = "72000001,72000002,71000001"  # pdv_sim of client 1 (rowid order)
# Filters on every changed field: the forms of the WS-H review (= "", != "",
# = true, = false, != true, = 1, = 0, id = 3) and the other operators, values
# and operand orders a PocketBase filter can send.
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
        )
    ),
    (CUSTOMERS, '"" = pdv_sim'),
    (CUSTOMERS, "73000001 = pdv_sim"),
    (CUSTOMERS, "true = pdv_sim"),
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
        for form in ('= ""', '!= ""', "= 73000001", '= "73000001"')
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
    ("buma_caisse_transaction_by_categorie_list_view", "id = 3"),
    ("buma_caisse_transaction_by_categorie_list_view", 'id = "3"'),
    ("buma_sale_info", "payedPercent > 50"),
)
SORTS = (
    (CUSTOMERS, "pdv_sim,id"),
    (CUSTOMERS, "-pdv_sim,id"),
    (CUSTOMERS, "agent_sim,id"),
    ("buma_agent_list_view", "number,id"),
    ("buma_customer_mobile_list_view", "-isNafama,id"),
    ("buma_customer_mobile_list_view", "isNafama,id"),
    ("buma_transaction_list_view", "payed,id"),
    ("buma_transaction_list_view", "-salePayed,id"),
)


def probes() -> list[tuple[str, str, dict]]:
    """(label, path, params): identical requests for both servers."""
    items: list[tuple[str, str, dict]] = []
    for view in VIEWS:
        path = f"/api/collections/{view}/records"
        if view == "buma_caisse_transaction_by_categorie_list_view":
            for page in (1, 2, 3):
                items.append(
                    (f"{view}:page{page}", path, {"page": page, "perPage": 5, "sort": "id"})
                )
            items.append((f"{view}:desc", path, {"perPage": 5, "sort": "-id"}))
            items.append((f"{view}:default", path, {"perPage": 5}))
            items.append((f"{view}:label", path, {"filter": 'label = "Transport"', "sort": "id"}))
            items.append((f"{view}:by_id", path + "/3", {}))
        else:
            items.append((f"{view}:all", path, {"perPage": 500, "sort": "id"}))
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
        ("buma_sale_info", "1"),
    ):
        items.append((f"{view}:by_id", f"/api/collections/{view}/records/{record}", {}))
    items.append(
        (
            "buma_customer_list_view:by_id",
            f"/api/collections/buma_customer_list_view/records/{ident('wshcust', 1)}",
            {},
        )
    )
    items.append(
        (
            "buma_article_with_no_stock_list_view:by_id",
            "/api/collections/buma_article_with_no_stock_list_view/records/"
            + ident("wsharticle", 1),
            {},
        )
    )
    return items


async def admin_token(client: httpx.AsyncClient) -> str:
    response = await client.post(
        "/api/admins/auth-with-password", json={"identity": ADMIN[0], "password": ADMIN[1]}
    )
    response.raise_for_status()
    return response.json()["token"]


async def answers(base: str, items) -> dict:
    out = {}
    # No keep-alive: a server error that closes its connection cannot make
    # the next probe fail with a transport error.
    limits = httpx.Limits(max_keepalive_connections=0)
    async with httpx.AsyncClient(base_url=base, timeout=60, limits=limits) as client:
        token = await admin_token(client)
        for label, path, params in items:
            started = time.monotonic()
            try:
                response = await client.get(path, params=params, headers={"Authorization": token})
            except httpx.TransportError as exc:
                # The server dropped the connection (an unhandled error).
                out[label] = {"status": f"transport_error:{type(exc).__name__}", "body": None}
                out[label]["seconds"] = round(time.monotonic() - started, 3)
                continue
            try:
                body = response.json()
            except ValueError:
                body = None
            out[label] = {
                "status": response.status_code,
                "body": body,
                "seconds": round(time.monotonic() - started, 3),
            }
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


def kind(left, right) -> str:
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
    return "equal" if same(left, right) else "differs"


def compare(view: str, reference: dict, mine: dict) -> dict:
    """Outcome of one probe: status, ids in order, then every field of every item."""
    result = {"status": [reference["status"], mine["status"]]}
    left, right = records(reference["body"]), records(mine["body"])
    result["ids_in_order_equal"] = [r.get("id") for r in left] == [r.get("id") for r in right]
    pairs = list(zip(left, right, strict=False))
    result["id_types"] = sorted(
        {f"{type(a.get('id')).__name__}/{type(b.get('id')).__name__}" for a, b in pairs}
    )
    for key in ("totalItems", "page", "perPage"):
        if isinstance(reference["body"], dict) and key in reference["body"]:
            result[key] = [reference["body"].get(key), (mine["body"] or {}).get(key)]
    fields: dict[str, dict] = {}
    multiset = sorted(json.dumps(r, sort_keys=True) for r in left) == sorted(
        json.dumps(r, sort_keys=True) for r in right
    )
    for a, b in pairs:
        for key in sorted(set(a) | set(b)):
            if key in ("id",) and view != "buma_caisse_transaction_by_categorie_list_view":
                continue
            outcome = kind(a.get(key), b.get(key)) if key in a and key in b else "missing_key"
            counter = fields.setdefault(key, {})
            counter[outcome] = counter.get(outcome, 0) + 1
    result["fields"] = fields
    result["items"] = [len(left), len(right)]
    result["same_multiset_of_items"] = multiset
    targets = TARGET_FIELDS.get(view, set())
    ids = [r.get("id") for r in left]
    if len(set(map(str, ids))) < len(ids):
        # Duplicated ids (a view row per joined row): compare the target fields
        # of the rows as multisets, the order of equal ids being unspecified.
        def rows_of(items):
            return sorted(
                json.dumps([r.get("id"), *[r.get(f) for f in sorted(targets)]], sort_keys=True)
                for r in items
            )

        result["duplicate_ids"] = True
        result["target_fields_equal"] = rows_of(left) == rows_of(right)
        result["other_fields_differing"] = []
        return result
    result["target_fields_equal"] = all(
        set(fields.get(f, {"equal": 1}))
        <= {"equal", "json_key_order_D-0029", "json_number_notation_D-0029"}
        for f in targets
    )
    result["other_fields_differing"] = sorted(
        f for f, c in fields.items() if f not in targets and set(c) - {"equal"}
    )
    return result


def compare_all(reference: dict, mine: dict) -> dict:
    out = {}
    for label in reference:
        view = label.split(":", 1)[0]
        out[label] = compare(view, reference[label], mine[label])
    return out


def summary(comparison: dict) -> dict:
    return {
        "probes": len(comparison),
        "status_equal": sum(1 for c in comparison.values() if c["status"][0] == c["status"][1]),
        "ids_in_order_equal": sum(1 for c in comparison.values() if c["ids_in_order_equal"]),
        "target_fields_equal": sum(1 for c in comparison.values() if c["target_fields_equal"]),
        "not_equal": sorted(
            label
            for label, c in comparison.items()
            if c["status"][0] != c["status"][1]
            or not c["ids_in_order_equal"]
            or not c["target_fields_equal"]
        ),
        "other_fields_differing": sorted(
            {
                f"{label.split(':', 1)[0]}.{f}"
                for label, c in comparison.items()
                for f in c["other_fields_differing"]
            }
        ),
    }


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


GRID_TEXTS = (
    None,
    "",
    "73000001",
    MULTIPLE,
    "070000009",
    "1e3",
    "12.50",
    "-5",
    "null",
    "true",
    "false",
    "[1,2]",
    '"x"',
    '{"b":1,"a":2}',
    "abc",
    "Abc",
    "7",
)
GRID_VALUES = (
    "",
    "73000001",
    MULTIPLE,
    "7",
    "abc",
    "x",
    "1000",
    73000001,
    1000,
    12.5,
    -5,
    0,
    1,
    7,
    True,
    False,
)
GRID_OPERATORS = ("=", "!=", "<", "<=", ">", ">=")
GRID_INTEGERS = (None, 0, 1)


def sqlite_filter(value, operator: str, other, *, json_field: bool) -> bool | None:
    """PocketBase's SQL for ``field <operator> other`` evaluated by SQLite."""
    with closing(sqlite3.connect(":memory:")) as db:
        left = (
            "(CASE WHEN json_valid(:v) THEN JSON_EXTRACT(:v, '$') "
            "ELSE JSON_EXTRACT(json_object('pb', :v), '$.pb') END)"
            if json_field
            else ":v"
        )
        sql_operator = {"=": "IS", "!=": "IS NOT"}.get(operator, operator)
        if isinstance(other, bool):
            right, params = ("1" if other else "0"), {"v": value}
        else:
            number = isinstance(other, int | float)
            right, params = ":o", {"v": value, "o": float(other) if number else other}
        row = db.execute(f"SELECT {left} {sql_operator} {right}", params).fetchone()
    return None if row[0] is None else bool(row[0])


def ppbase_operand(other) -> tuple[str, dict]:
    """The operand as PPBase renders it: TRUE/FALSE, a literal, an untyped parameter."""
    if isinstance(other, bool):
        return ("TRUE" if other else "FALSE"), {}
    if isinstance(other, int | float):
        return str(other), {}
    return ":o", {"o": other}


async def operator_grid(engine) -> dict:
    """The 1789236851 operators against SQLite, both operand orders (rolled back)."""
    mismatches, checked = [], 0
    async with engine.connect() as conn:
        transaction = await conn.begin()
        try:
            await conn.execute(text("CREATE TEMP TABLE wsh_grid (k int, v text, i int)"))
            for k, value in enumerate(GRID_TEXTS):
                await conn.execute(
                    text("INSERT INTO wsh_grid VALUES (:k, :v, :i)"),
                    {"k": k, "v": value, "i": GRID_INTEGERS[k % 3]},
                )
            await conn.execute(
                text(
                    "CREATE TEMP VIEW wsh_grid_view AS SELECT k, buma_pb_json_text(v) AS j, "
                    "CAST(i AS buma_sqlite_integer) AS i FROM wsh_grid"
                )
            )
            for k, value in enumerate(GRID_TEXTS):
                for operator in GRID_OPERATORS:
                    flipped = {"<": ">", "<=": ">=", ">": "<", ">=": "<="}.get(operator, operator)
                    for other in GRID_VALUES:
                        expected = sqlite_filter(value, operator, other, json_field=True)
                        operand, params = ppbase_operand(other)
                        for sql in (
                            f"SELECT j {operator} {operand} FROM wsh_grid_view WHERE k = {k}",
                            f"SELECT {operand} {flipped} j FROM wsh_grid_view WHERE k = {k}",
                        ):
                            served = (await conn.execute(text(sql), params)).scalar_one()
                            checked += 1
                            if served != expected:
                                mismatches.append([value, sql.split(" FROM")[0], other])
                    for other in (True, False):
                        integer = GRID_INTEGERS[k % 3]
                        expected = sqlite_filter(integer, operator, other, json_field=False)
                        operand, _ = ppbase_operand(other)
                        for sql in (
                            f"SELECT i {operator} {operand} FROM wsh_grid_view WHERE k = {k}",
                            f"SELECT {operand} {flipped} i FROM wsh_grid_view WHERE k = {k}",
                        ):
                            served = (await conn.execute(text(sql))).scalar_one()
                            checked += 1
                            if served != expected:
                                mismatches.append([integer, sql.split(" FROM")[0], other])
        finally:
            await transaction.rollback()
    return {
        "sqlite_version": sqlite3.sqlite_version,
        "stored_texts": len(GRID_TEXTS),
        "compared_values": len(GRID_VALUES),
        "checked": checked,
        "mismatches": mismatches,
    }


async def operator_scope(engine) -> dict:
    """What the operators do outside the two domains (each probe rolled back)."""
    probes = {
        "integer_equals_boolean": ("SELECT 1::integer = TRUE", {}),
        "text_equals_integer": ("SELECT 'a'::text = 3", {}),
        "text_equals_boolean": ("SELECT 'a'::text = TRUE", {}),
        "jsonb_equals_empty_parameter": ("SELECT '1'::jsonb = :p", {"p": ""}),
        "jsonb_equals_parameter": ("SELECT '1'::jsonb = :p", {"p": "1"}),
        "text_equals_parameter": ("SELECT 'a'::text = :p", {"p": "a"}),
        "smallint_equals_boolean": ("SELECT 1::smallint = TRUE", {}),
        "json_equals_parameter": ("SELECT '1'::json = :p", {"p": "1"}),
        "domain_equals_domain": ("SELECT buma_pb_json_text('1') = buma_pb_json_text('1')", {}),
    }
    out = {}
    for label, (sql, params) in probes.items():
        async with engine.connect() as conn:
            try:
                out[label] = {"value": (await conn.execute(text(sql), params)).scalar_one()}
            except Exception as exc:  # noqa: BLE001 - the error itself is the result
                message = str(getattr(exc, "orig", exc)).splitlines()[0]
                out[label] = {"error": message.split(": ", 1)[-1][:120]}
    expected_errors = {
        "integer_equals_boolean",
        "text_equals_integer",
        "text_equals_boolean",
        "jsonb_equals_empty_parameter",
        "domain_equals_domain",
    }
    assert {k for k, v in out.items() if "error" in v} == expected_errors, out
    assert out["jsonb_equals_parameter"]["value"] is True
    assert out["text_equals_parameter"]["value"] is True
    # Documented: an implicit cast lets the operators reach smallint and json.
    assert out["smallint_equals_boolean"]["value"] is True
    assert out["json_equals_parameter"]["value"] is False  # a number is not the text '1'
    return out


async def exercise(root: Path, binary: Path, schema: Path, report: dict, start_postgres) -> None:
    data = build_fixture(root, binary, schema)
    report["fixture"] = source_facts(data)
    items = probes()
    sale_phases = {}
    async with PocketBase(binary, data, root) as pocketbase:
        reference = await answers(pocketbase.base, items)
    for phase in SALE_PHASES:
        write_sale_phase_sqlite(data, phase)
        async with PocketBase(binary, data, root) as pocketbase:
            sale_phases[phase] = {"pocketbase": await answers(pocketbase.base, sale_info_probe())}
    write_sale_phase_sqlite(data, "integer_truncation")
    report["pocketbase_answers"] = {
        "probes": len(reference),
        "statuses": sorted({a["status"] for a in reference.values()}),
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
        report["import"] = {"rows": rows, "archive_rows": None}
        async with engine.connect() as conn:
            report["import"]["archive_rows"] = (
                await conn.execute(text("SELECT count(*) FROM _buma_c11_rowids"))
            ).scalar_one()

        async with PPBase(url, published, root, "after") as server:
            after = await answers(server.base, items)
            for phase in SALE_PHASES:
                await write_sale_phase_postgres(engine, phase)
                sale_phases[phase]["after"] = await answers(server.base, sale_info_probe())
            await write_sale_phase_postgres(engine, "integer_truncation")
        comparison = compare_all(reference, after)
        report["after_1789236851"] = {"summary": summary(comparison), "probes": comparison}
        known = known_after()
        report["ppbase_sorts_json_by_jsonb"] = ppbase_sorts_json_by_jsonb()
        remaining = report["after_1789236851"]["summary"]["not_equal"]
        report["after_1789236851"]["known_differences"] = {
            label: (
                dict(
                    zip(("reason", "pocketbase_status", "ppbase_status"), known[label], strict=True)
                )
                if label in known
                else "UNEXPECTED"
            )
            for label in remaining
        }
        assert set(remaining) == set(known), sorted(set(remaining) ^ set(known))
        for label, (_, pocketbase_status, ppbase_status) in known.items():
            statuses = [reference[label]["status"], after[label]["status"]]
            assert statuses == [pocketbase_status, ppbase_status], (label, statuses)
        report["without_archive"] = await without_archive(engine)
        report["operator_grid"] = await operator_grid(engine)
        assert report["operator_grid"]["mismatches"] == [], report["operator_grid"]
        report["operator_scope"] = await operator_scope(engine)

        down = await run_migrations_down(engine, MIGRATIONS, count=1)
        assert down == [MIGRATION], down
        async with PPBase(url, published, root, "before") as server:
            before = await answers(server.base, items)
            for phase in SALE_PHASES:
                await write_sale_phase_postgres(engine, phase)
                sale_phases[phase]["before"] = await answers(server.base, sale_info_probe())
            await write_sale_phase_postgres(engine, "integer_truncation")
        comparison = compare_all(reference, before)
        report["before_1789236851_after_down"] = {
            "summary": summary(comparison),
            "probes": comparison,
        }
        not_equal_before = set(report["before_1789236851_after_down"]["summary"]["not_equal"])
        regressions = sorted(set(remaining) - not_equal_before)
        new_errors = sorted(
            label
            for label in reference
            if before[label]["status"] == 200 and after[label]["status"] != 200
        )
        expected_errors = {"buma_caisse_transaction_by_categorie_list_view:filter:id = 3"}
        if not report["ppbase_sorts_json_by_jsonb"]:
            expected_errors |= REGRESSIONS_BEFORE_WS_A11
        report["regressions"] = {
            "equal_to_pocketbase_before_and_not_after": regressions,
            "status_200_before_and_not_after": new_errors,
            "fixed_by_1789236851": sorted(not_equal_before - set(remaining)),
        }
        assert set(new_errors) == expected_errors, new_errors
        assert set(regressions) <= expected_errors | REGRESSIONS, regressions
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
            if ":filter:" in label or ":sort:" in label or ":by_id" in label
        }
        up = await run_migrations_up(engine, MIGRATIONS)
        assert up == [MIGRATION], up
        async with PPBase(url, published, root, "replay") as server:
            replay = await answers(server.base, items)
        report["replay_after_up_equal_to_first_after"] = all(
            replay[k]["status"] == after[k]["status"] and replay[k]["body"] == after[k]["body"]
            for k in after
        )
        report["sale_info_phases"] = {
            phase: {
                side: {
                    "status": answer["buma_sale_info"]["status"],
                    "payedPercent": [
                        r.get("payedPercent") for r in records(answer["buma_sale_info"]["body"])
                    ],
                }
                for side, answer in sides.items()
            }
            for phase, sides in sale_phases.items()
        }
        report["sale_info_after_equal_to_pocketbase"] = all(
            sides["after"]["buma_sale_info"]["status"]
            == sides["pocketbase"]["buma_sale_info"]["status"]
            and same(
                [r.get("payedPercent") for r in records(sides["after"]["buma_sale_info"]["body"])],
                [
                    r.get("payedPercent")
                    for r in records(sides["pocketbase"]["buma_sale_info"]["body"])
                ],
            )
            for sides in sale_phases.values()
        )
        report["timings_seconds_after"] = {
            k.split(":", 1)[0]: v["seconds"] for k, v in after.items() if k.endswith(":all")
        }
        report["served_examples"] = examples(reference, after, before)
    finally:
        await engine.dispose()


def examples(reference: dict, after: dict, before: dict) -> dict:
    """Synthetic served values of the target fields on the three servers."""
    out = {}
    for label in reference:
        view = label.split(":", 1)[0]
        if not label.endswith(":all") and ":page" not in label and ":by_id" not in label:
            continue
        fields = TARGET_FIELDS.get(view, set())
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
                | {"id": [a.get("id"), b.get("id"), c.get("id")]}
            )
        out[label] = rows
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
    report: dict = {
        "timestamp_utc": datetime.now(UTC).isoformat(),
        "scope": "c11_ws_h_view_sqlite_semantics_synthetic_paired_comparison",
        "production_data_used": False,
        "real_backup_read": False,
        "pocketbase": {"version": SOURCE_VERSION, "binary_sha256": digest(args.pocketbase)},
        "migration": MIGRATION,
        "ppbase": {
            "package": str(Path(__import__("ppbase").__file__).resolve().parent),
            "commit": subprocess.run(
                [
                    "git",
                    "-C",
                    str(Path(__import__("ppbase").__file__).resolve().parents[1]),
                    "rev-parse",
                    "HEAD",
                ],
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
        report["failure"] = str(exc)[:2000]
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
        write_json_new(args.report, report)
        print(json.dumps({"status": report["status"], "report": str(args.report)}))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
