#!/usr/bin/env python3
"""Runtime proof for the C8 wallet/caisse checkpoint."""

from __future__ import annotations

import asyncio
import json
import secrets
from datetime import UTC, datetime

import httpx
from ppbase.core.id_generator import generate_id
from ppbase.services.auth_service import generate_token_key, hash_password
from sqlalchemy import text
from sqlalchemy.ext.asyncio import create_async_engine

BASE_URL = "http://127.0.0.1:8090"
DATABASE_URL = "postgresql+asyncpg://ppbase:ppbase@localhost:55434/buma_master_ppbase"


async def main() -> None:
    suffix = secrets.token_hex(4)
    role_id = f"c8role{suffix}"
    user_id = generate_id()
    username = f"c8user{suffix}"
    email = f"{username}@example.com"
    wallet_a = f"c8wa{suffix}"
    wallet_b = f"c8wb{suffix}"
    caisse_a = f"c8ca{suffix}"
    caisse_b = f"c8cb{suffix}"
    access_a = f"c8ac{suffix}"
    access_b = f"c8bc{suffix}"
    customer_a = f"c8ua{suffix}"
    customer_b = f"c8ub{suffix}"
    employee_id = f"c8em{suffix}"
    expense_type_id = f"c8ex{suffix}"
    password = "abcde"
    config_created = False
    original_transfer_wallet = None
    engine = create_async_engine(DATABASE_URL, connect_args={"ssl": "disable"})

    async with engine.begin() as connection:
        now = datetime.now(UTC)
        await connection.execute(
            text(
                'INSERT INTO "roles" '
                "(id, created, updated, name, permission, status, author) "
                "VALUES (:id, :created, :updated, :name, CAST(:permission AS jsonb), TRUE, '')"
            ),
            {
                "id": role_id,
                "created": now,
                "updated": now,
                "name": role_id,
                "permission": json.dumps({"isAdmin": True}),
            },
        )
        await connection.execute(
            text(
                'INSERT INTO "users" '
                "(id, created, updated, email, email_visibility, verified, password_hash, "
                "token_key, role, name, username) "
                "VALUES (:id, :created, :updated, :email, FALSE, TRUE, :password_hash, "
                ":token_key, :role, :name, :username)"
            ),
            {
                "id": user_id,
                "created": now,
                "updated": now,
                "email": email,
                "password_hash": hash_password(password),
                "token_key": generate_token_key(),
                "role": role_id,
                "name": "C8 Runtime",
                "username": username,
            },
        )
        for wallet_id, name, balance in (
            (wallet_a, "C8 Wallet A", 100),
            (wallet_b, "C8 Wallet B", 50),
        ):
            await connection.execute(
                text(
                    'INSERT INTO "buma_wallet" '
                    "(id, created, updated, name, balance, author, description, status, type) "
                    "VALUES (:id, :created, :updated, :name, :balance, :author, '', TRUE, 'OTHER')"
                ),
                {
                    "id": wallet_id,
                    "created": now,
                    "updated": now,
                    "name": name,
                    "balance": balance,
                    "author": user_id,
                },
            )
        for caisse_id, name in ((caisse_a, "C8 Caisse A"), (caisse_b, "C8 Caisse B")):
            await connection.execute(
                text(
                    'INSERT INTO "buma_caisse" '
                    "(id, created, updated, name, type, balance, author, status, state, "
                    '"currentCashier", "currentSession", unknown_balance, '
                    "unknow_balance_session) VALUES (:id, :created, :updated, :name, "
                    "'HYBRID', 0, :author, TRUE, 'CLOSE', '', '', 0, '')"
                ),
                {"id": caisse_id, "created": now, "updated": now, "name": name, "author": user_id},
            )
        for access_id, caisse_id in ((access_a, caisse_a), (access_b, caisse_b)):
            await connection.execute(
                text(
                    'INSERT INTO "buma_caisse_access" '
                    '(id, created, updated, caisse, "user") '
                    "VALUES (:id, :created, :updated, :caisse, :user)"
                ),
                {
                    "id": access_id,
                    "created": now,
                    "updated": now,
                    "caisse": caisse_id,
                    "user": user_id,
                },
            )
        for customer_id, firstname in (
            (customer_a, "C8 Customer A"),
            (customer_b, "C8 Customer B"),
        ):
            await connection.execute(
                text(
                    'INSERT INTO "buma_customer" '
                    "(id, created, updated, solde, status, firstname, author) "
                    "VALUES (:id, :created, :updated, 0, TRUE, :firstname, :author)"
                ),
                {
                    "id": customer_id,
                    "created": now,
                    "updated": now,
                    "firstname": firstname,
                    "author": user_id,
                },
            )
        await connection.execute(
            text(
                'INSERT INTO "buma_employee" '
                "(id, created, updated, solde, status, author) "
                "VALUES (:id, :created, :updated, 0, TRUE, :author)"
            ),
            {"id": employee_id, "created": now, "updated": now, "author": user_id},
        )
        await connection.execute(
            text(
                'INSERT INTO "buma_caisse_expense_type" '
                "(id, created, updated, name, description, amount, status) "
                "VALUES (:id, :created, :updated, :name, '', 0, TRUE)"
            ),
            {
                "id": expense_type_id,
                "created": now,
                "updated": now,
                "name": f"C8 Expense {suffix}",
            },
        )
        config = (
            (
                await connection.execute(
                    text("SELECT transfer_wallet FROM \"buma_config\" WHERE id='CONFIGURATION_1'")
                )
            )
            .mappings()
            .first()
        )
        if config is None:
            config_created = True
            await connection.execute(
                text(
                    'INSERT INTO "buma_config" '
                    "(id, created, updated, transfer_wallet) "
                    "VALUES ('CONFIGURATION_1', :created, :updated, :wallet)"
                ),
                {"created": now, "updated": now, "wallet": wallet_a},
            )
        else:
            original_transfer_wallet = config.get("transfer_wallet")
            await connection.execute(
                text(
                    'UPDATE "buma_config" SET transfer_wallet=:wallet, updated=NOW() '
                    "WHERE id='CONFIGURATION_1'"
                ),
                {"wallet": wallet_a},
            )

    async with httpx.AsyncClient(base_url=BASE_URL, timeout=30) as client:
        login = await client.post(
            "/api/collections/users/auth-with-password",
            json={"identity": username, "password": password},
        )
        assert login.status_code == 200, login.text
        headers = {"Authorization": login.json()["token"]}

        anonymous = await client.post(
            "/master/wallets/deposit", json={"wallet": wallet_a, "amount": 1}
        )
        assert anonymous.status_code == 401

        request_open = await client.post(
            "/master/caisse/requestOpen", headers=headers, json={"caisse": caisse_a}
        )
        assert request_open.status_code == 200, request_open.text
        open_response = await client.post(
            "/master/caisse/openCaisse", headers=headers, json={"caisse": caisse_a}
        )
        assert open_response.status_code == 200, open_response.text
        request_open_b = await client.post(
            "/master/caisse/requestOpen", headers=headers, json={"caisse": caisse_b}
        )
        assert request_open_b.status_code == 200, request_open_b.text
        open_response_b = await client.post(
            "/master/caisse/openCaisse", headers=headers, json={"caisse": caisse_b}
        )
        assert open_response_b.status_code == 200, open_response_b.text

        wallet_deposit = await client.post(
            "/master/wallets/deposit",
            headers=headers,
            json={"wallet": wallet_a, "amount": 5, "note": "C8 deposit"},
        )
        assert wallet_deposit.status_code == 200, wallet_deposit.text
        wallet_transaction_id = wallet_deposit.json()["id"]
        wallet_withdraw = await client.post(
            "/master/wallets/withdraw",
            headers=headers,
            json={"wallet": wallet_a, "amount": 3},
        )
        assert wallet_withdraw.status_code == 200, wallet_withdraw.text
        wallet_note = await client.post(
            "/master/wallets/updateNote",
            headers=headers,
            json={"transactionId": wallet_withdraw.json()["id"], "note": "updated"},
        )
        assert wallet_note.status_code == 200, wallet_note.text
        wallet_transfer = await client.post(
            "/master/wallets/transfer",
            headers=headers,
            json={"from": wallet_a, "to": wallet_b, "amount": 10, "note": "C8 transfer"},
        )
        assert wallet_transfer.status_code == 200, wallet_transfer.text
        wallet_cancel = await client.post(
            "/master/wallets/cancel_transaction",
            headers=headers,
            json={"id": wallet_transaction_id},
        )
        assert wallet_cancel.status_code == 200, wallet_cancel.text

        caisse_deposit = await client.post(
            "/master/caisse/deposit",
            headers=headers,
            json={
                "caisse": caisse_a,
                "wallet": wallet_a,
                "amount": 10,
                "walletNote": "C8 caisse deposit",
                "note": "C8 caisse deposit",
            },
        )
        assert caisse_deposit.status_code == 200, caisse_deposit.text
        caisse_withdraw = await client.post(
            "/master/caisse/withdraw",
            headers=headers,
            json={
                "caisse": caisse_a,
                "wallet": wallet_a,
                "amount": 4,
                "walletNote": "C8 caisse withdraw",
                "note": "C8 caisse withdraw",
            },
        )
        assert caisse_withdraw.status_code == 200, caisse_withdraw.text

        versement = await client.post(
            "/master/caisse/new_versement",
            headers=headers,
            json={
                "caisse": caisse_a,
                "customer": customer_a,
                "amount": 5,
                "note": "C8 versement",
            },
        )
        assert versement.status_code == 200, versement.text
        versement_id = versement.json()["caisse_transaction"]
        update_note = await client.post(
            "/master/caisse/updateNote",
            headers=headers,
            json={"transactionId": versement_id, "note": "C8 note updated"},
        )
        assert update_note.status_code == 200, update_note.text
        update_amount = await client.post(
            "/master/caisse/updateAmount",
            headers=headers,
            json={"caisseTransactionId": versement_id, "amount": 6},
        )
        assert update_amount.status_code == 200, update_amount.text
        change_customer = await client.post(
            "/master/caisse/changeTransactionCustomer",
            headers=headers,
            json={"transactionId": versement_id, "newCustomerId": customer_b},
        )
        assert change_customer.status_code == 200, change_customer.text

        expense = await client.post(
            "/master/caisse/expense",
            headers=headers,
            json={
                "caisse": caisse_a,
                "expenseType": expense_type_id,
                "amount": 1,
                "note": "C8 expense",
            },
        )
        assert expense.status_code == 200, expense.text

        bank = await client.post(
            "/master/caisse/bank_versement",
            headers=headers,
            json={
                "caisse": caisse_a,
                "walletId": wallet_a,
                "customer": customer_a,
                "amount": 2,
                "note": "C8 bank",
            },
        )
        assert bank.status_code == 200, bank.text
        bank_cancel = await client.post(
            "/master/caisse/cancel_bank_versement",
            headers=headers,
            json={"transactionId": bank.json()["caisse_transaction"]},
        )
        assert bank_cancel.status_code == 200, bank_cancel.text

        correction = await client.post(
            "/master/caisse/new_correction",
            headers=headers,
            json={
                "caisse": caisse_a,
                "transactionId": versement_id,
                "amount": 1,
                "deposit": True,
                "note": "C8 correction",
            },
        )
        assert correction.status_code == 200, correction.text

        debt = await client.post(
            "/master/caisse/employee_debt",
            headers=headers,
            json={"caisse": caisse_a, "employee": employee_id, "amount": 1},
        )
        assert debt.status_code == 200, debt.text
        repayment = await client.post(
            "/master/caisse/employee_debt_repayment",
            headers=headers,
            json={"caisse": caisse_a, "employee": employee_id, "amount": 1},
        )
        assert repayment.status_code == 200, repayment.text
        caisse_transfer = await client.post(
            "/master/caisse/transfer",
            headers=headers,
            json={
                "caisseFrom": caisse_a,
                "caisseTo": caisse_b,
                "amount": 2,
                "note": "C8 caisse transfer",
            },
        )
        assert caisse_transfer.status_code == 200, caisse_transfer.text

        async with engine.connect() as connection:
            caisse_tx = (
                await connection.execute(
                    text(
                        'SELECT id FROM "buma_caisse_transactions" '
                        "WHERE caisse=:caisse AND type='WITHDRAWAL' "
                        "ORDER BY created DESC LIMIT 1"
                    ),
                    {"caisse": caisse_a},
                )
            ).first()
        assert caisse_tx is not None
        caisse_cancel = await client.post(
            "/master/caisse/cancel",
            headers=headers,
            json={"transactionId": str(caisse_tx[0])},
        )
        assert caisse_cancel.status_code == 200, caisse_cancel.text

        accessible = await client.get("/master/caisse/accessible", headers=headers)
        assert accessible.status_code == 200, accessible.text
        assert len(accessible.json()["caisses"]) == 2

        async with engine.connect() as connection:
            session_id = (
                await connection.execute(
                    text('SELECT "currentSession" FROM "buma_caisse" WHERE id=:id'),
                    {"id": caisse_a},
                )
            ).scalar_one()
        verify = await client.get(f"/master/caisse/verifySession/{session_id}", headers=headers)
        assert verify.status_code == 200, verify.text
        assert "verification" in verify.json()
        archive = await client.post(
            "/master/caisse/expense_type/archive",
            headers=headers,
            json={"id": expense_type_id},
        )
        assert archive.status_code == 200, archive.text

        request_close = await client.post(
            "/master/caisse/requestClose", headers=headers, json={"caisse": caisse_a}
        )
        assert request_close.status_code == 200, request_close.text
        close_response = await client.post(
            "/master/caisse/closeCaisse", headers=headers, json={"caisse": caisse_a}
        )
        assert close_response.status_code == 200, close_response.text
        unallocate = await client.post(
            "/master/caisse/unallocate",
            headers=headers,
            json={"caisseTransactionId": versement_id},
        )
        assert unallocate.status_code == 200, unallocate.text
        async with engine.connect() as connection:
            debt_history = (
                (
                    await connection.execute(
                        text(
                            "SELECT caisse_transaction, type "
                            'FROM "buma_customer_solder_history" '
                            "WHERE caisse_transaction IS NOT NULL AND type='DEBT' "
                            "ORDER BY created DESC LIMIT 1"
                        ),
                    )
                )
                .mappings()
                .first()
            )
        assert debt_history is not None
        assert debt_history["caisse_transaction"]

        async def concurrent_deposit() -> int:
            response = await client.post(
                "/master/wallets/deposit",
                headers=headers,
                json={"wallet": wallet_a, "amount": 1},
            )
            return response.status_code

        assert sorted(await asyncio.gather(concurrent_deposit(), concurrent_deposit())) == [
            200,
            200,
        ]

        async with engine.begin() as connection:
            await connection.execute(
                text('UPDATE "roles" SET permission=CAST(:permission AS jsonb) WHERE id=:id'),
                {"permission": json.dumps({"isAdmin": False}), "id": role_id},
            )
        wallet_forbidden = await client.post(
            "/master/wallets/deposit",
            headers=headers,
            json={"wallet": wallet_a, "amount": 1},
        )
        assert wallet_forbidden.status_code == 403
        assert wallet_forbidden.json()["message"] == (
            "Vous n'avez pas les droits pour effectuez cette action"
        )
        caisse_forbidden = await client.post(
            "/master/caisse/withdraw",
            headers=headers,
            json={"caisse": caisse_b, "wallet": wallet_a, "amount": 1},
        )
        assert caisse_forbidden.status_code == 400
        assert caisse_forbidden.json()["message"] == "Vous n'avez pas les autorisations"

        async with engine.connect() as connection:
            balances = (
                await connection.execute(
                    text(
                        "SELECT "
                        '(SELECT balance FROM "buma_wallet" WHERE id=:wallet_a), '
                        '(SELECT balance FROM "buma_caisse" WHERE id=:caisse_a), '
                        '(SELECT balance FROM "buma_caisse" WHERE id=:caisse_b)'
                    ),
                    {"wallet_a": wallet_a, "caisse_a": caisse_a, "caisse_b": caisse_b},
                )
            ).one()
        assert balances[0] >= 0

    async with engine.begin() as connection:
        await connection.execute(
            text(
                'DELETE FROM "buma_customer_solder_history" '
                "WHERE caisse_transaction IN "
                '(SELECT id FROM "buma_caisse_transactions" WHERE caisse=ANY(:caisses))'
            ),
            {"caisses": [caisse_a, caisse_b]},
        )
        for table, column in (
            ("buma_wallet_transactions", "wallet"),
            ("buma_caisse_transactions", "caisse"),
            ("buma_caisse_open_close", "caisse"),
            ("buma_caisse_access", "caisse"),
        ):
            await connection.execute(
                text(f'DELETE FROM "{table}" WHERE "{column}" = ANY(:ids)'),
                {"ids": [wallet_a, wallet_b] if column == "wallet" else [caisse_a, caisse_b]},
            )
        await connection.execute(
            text('DELETE FROM "buma_caisse_transactions" WHERE caisse = ANY(:ids)'),
            {"ids": [caisse_a, caisse_b]},
        )
        await connection.execute(
            text('DELETE FROM "buma_wallet" WHERE id = ANY(:ids)'),
            {"ids": [wallet_a, wallet_b]},
        )
        await connection.execute(
            text('DELETE FROM "buma_caisse" WHERE id = ANY(:ids)'),
            {"ids": [caisse_a, caisse_b]},
        )
        await connection.execute(
            text('DELETE FROM "buma_customer" WHERE id = ANY(:ids)'),
            {"ids": [customer_a, customer_b]},
        )
        await connection.execute(
            text('DELETE FROM "buma_employee" WHERE id=:id'), {"id": employee_id}
        )
        await connection.execute(
            text('DELETE FROM "buma_caisse_expense_type" WHERE id=:id'),
            {"id": expense_type_id},
        )
        await connection.execute(text('DELETE FROM "users" WHERE id=:id'), {"id": user_id})
        await connection.execute(text('DELETE FROM "roles" WHERE id=:id'), {"id": role_id})
        if config_created:
            await connection.execute(text("DELETE FROM \"buma_config\" WHERE id='CONFIGURATION_1'"))
        else:
            await connection.execute(
                text(
                    'UPDATE "buma_config" SET transfer_wallet=:wallet, updated=NOW() '
                    "WHERE id='CONFIGURATION_1'"
                ),
                {"wallet": original_transfer_wallet},
            )
    await engine.dispose()
    print(
        "C8 finance runtime: auth=OK wallet_crud=OK wallet_cancel=OK "
        "caisse_session=OK caisse_crud=OK caisse_transfer=OK "
        "caisse_cancel=OK correction=OK versement=OK expense=OK bank=OK "
        "employee_debt=OK verify=OK accessible=OK permission=OK "
        "concurrency=OK cleanup=OK"
    )


if __name__ == "__main__":
    asyncio.run(main())
