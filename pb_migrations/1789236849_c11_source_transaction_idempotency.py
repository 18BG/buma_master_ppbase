"""Restore the frozen source idempotency index on buma_transaction (D-0022).

Source migration migrationsDir/1785599959_updated_buma_transaction.js (commit
530018c) adds one unique index, on system_id only, for values matching SQLite's
LIKE 'idem:%'; mobile_message_id stays non-unique. Production never applied it.
C4 had instead created two PPBase-only unique indexes scoped by type/author; the
mobile one rejected 2,391 historical RETURN rows that PocketBase had accepted.

SQLite's LIKE folds ASCII letters only, so the predicate folds exactly the four
prefix letters instead of relying on PostgreSQL's locale-aware ILIKE.
"""

COLLECTION = "buma_transaction"
SOURCE_INDEX = (
    'CREATE UNIQUE INDEX "idx_transaction_idempotency" ON "buma_transaction" ("system_id") '
    "WHERE translate(substr(\"system_id\", 1, 5), 'IDEM', 'idem') = 'idem:'"
)
C4_INDEXES = (
    ("idx_buma_transaction_idempotency_system", "system_id"),
    ("idx_buma_transaction_idempotency_mobile", "mobile_message_id"),
)


async def up(app):
    for name, _ in C4_INDEXES:
        await app.execute_sql(f'DROP INDEX IF EXISTS "{name}"')
    collection = await app.find_collection(COLLECTION)
    indexes = list(collection.indexes or [])
    if SOURCE_INDEX not in indexes:
        indexes.append(SOURCE_INDEX)
    # Like the source collection.indexes + saveCollection: metadata and the
    # physical index change together, inside this migration's transaction.
    await app.update_collection(COLLECTION, {"indexes": indexes})


async def down(app):
    collection = await app.find_collection(COLLECTION)
    indexes = [index for index in collection.indexes or [] if index != SOURCE_INDEX]
    await app.update_collection(COLLECTION, {"indexes": indexes})
    # Fails, rolling this migration back, once imported history contains the
    # duplicate RETURN groups that the stricter C4 index cannot represent.
    for name, column in C4_INDEXES:
        await app.execute_sql(
            f'CREATE UNIQUE INDEX IF NOT EXISTS "{name}" '
            f'ON "buma_transaction" ("type", "author", "{column}") '
            f"WHERE \"{column}\" <> ''"
        )
