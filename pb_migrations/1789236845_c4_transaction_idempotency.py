"""Add PostgreSQL uniqueness gates for transaction idempotency keys."""


async def up(app):
    await app.execute_sql(
        'CREATE UNIQUE INDEX IF NOT EXISTS '
        '"idx_buma_transaction_idempotency_system" '
        'ON "buma_transaction" ("type", "author", "system_id") '
        'WHERE "system_id" <> \'\''
    )
    await app.execute_sql(
        'CREATE UNIQUE INDEX IF NOT EXISTS '
        '"idx_buma_transaction_idempotency_mobile" '
        'ON "buma_transaction" ("type", "author", "mobile_message_id") '
        'WHERE "mobile_message_id" <> \'\''
    )


async def down(app):
    await app.execute_sql(
        'DROP INDEX IF EXISTS "idx_buma_transaction_idempotency_mobile"'
    )
    await app.execute_sql(
        'DROP INDEX IF EXISTS "idx_buma_transaction_idempotency_system"'
    )
