import importlib.util
import re
import sqlite3
from pathlib import Path

import pytest

from app.transactions.idempotency import (
    IdempotencyLookup,
    _find_on_connection,
    build_idempotency_key,
)
from scripts.verify_schema_baseline import SOURCE_INDEX_DELTAS

D0022 = Path(__file__).parents[1] / "pb_migrations/1789236849_c11_source_transaction_idempotency.py"


def _load_d0022():
    spec = importlib.util.spec_from_file_location("d0022", D0022)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_build_idempotency_key_matches_source_normalization() -> None:
    assert build_idempotency_key(" Recharge ", "  user-1:msg-9 ") == ("idem:recharge:user-1:msg-9")
    assert build_idempotency_key("return", "idem:return:abc") == "idem:return:abc"
    assert build_idempotency_key("return", "") == ""
    assert build_idempotency_key("return", None) == ""


def test_lookup_preserves_source_precedence_and_author_scope() -> None:
    lookup = IdempotencyLookup(
        type="RETURN",
        author="author-1",
        system_id="idem:return:author-1:key",
        legacy_system_id="legacy-key",
        mobile_message_id="mobile-9",
    )
    assert lookup.type == "RETURN"
    assert lookup.author == "author-1"
    assert lookup.system_id.startswith("idem:return:")
    assert lookup.legacy_system_id == "legacy-key"
    assert lookup.mobile_message_id == "mobile-9"


def test_c4_migration_declares_partial_unique_transaction_indexes() -> None:
    migration = (
        Path(__file__).parents[1] / "pb_migrations/1789236845_c4_transaction_idempotency.py"
    ).read_text()
    assert "idx_buma_transaction_idempotency_system" in migration
    assert "idx_buma_transaction_idempotency_mobile" in migration
    assert 'WHERE "system_id"' in migration
    assert 'WHERE "mobile_message_id"' in migration


def test_d0022_restores_only_the_frozen_source_unique_index() -> None:
    module = _load_d0022()
    assert module.COLLECTION == "buma_transaction"
    # Source 1785599959: unique on system_id alone, mobile_message_id untouched.
    assert module.SOURCE_INDEX == (
        'CREATE UNIQUE INDEX "idx_transaction_idempotency" ON "buma_transaction" ("system_id") '
        "WHERE translate(substr(\"system_id\", 1, 5), 'IDEM', 'idem') = 'idem:'"
    )
    assert dict(module.C4_INDEXES) == {
        "idx_buma_transaction_idempotency_system": "system_id",
        "idx_buma_transaction_idempotency_mobile": "mobile_message_id",
    }
    assert SOURCE_INDEX_DELTAS == {module.COLLECTION: [module.SOURCE_INDEX]}


@pytest.mark.parametrize(
    "value",
    [
        "idem:return:author:message:1",
        "idem:",
        "IDEM:x",
        "IdEm:x",
        "idem",
        "idem;x",
        " idem:x",
        "xidem:",
        "",
        "idem:%",
        "İdem:x",
        "ıdem:x",
        "ｉdem:x",
        "idém:x",
    ],
)
def test_d0022_predicate_has_sqlite_like_semantics(value: str) -> None:
    with sqlite3.connect(":memory:") as db:
        sqlite_like = bool(db.execute("SELECT ? LIKE 'idem:%'", (value,)).fetchone()[0])
    # translate(substr(value, 1, 5), 'IDEM', 'idem') = 'idem:' in PostgreSQL.
    predicate = value[:5].translate(str.maketrans("IDEM", "idem")) == "idem:"
    assert predicate is sqlite_like


class _MissingLookupConnection:
    """Records each lookup query; no candidate exists."""

    def __init__(self) -> None:
        self.calls: list[tuple[str, dict]] = []

    async def execute(self, statement, parameters):
        self.calls.append((str(statement), parameters))
        return self

    def mappings(self):
        return self

    def first(self):
        return None


async def test_d0023_lookup_keeps_created_order_and_source_precedence() -> None:
    connection = _MissingLookupConnection()
    lookup = IdempotencyLookup(
        type="RETURN",
        author="author-1",
        system_id="idem:return:author-1:key",
        legacy_system_id="legacy-key",
        mobile_message_id="mobile-9",
    )
    assert await _find_on_connection(connection, lookup) is None
    fields = [re.search(r'AND "(\w+)" = :value', sql).group(1) for sql, _ in connection.calls]
    assert fields == ["system_id", "system_id", "mobile_message_id"]
    assert [params["value"] for _, params in connection.calls] == [
        "idem:return:author-1:key",
        "legacy-key",
        "mobile-9",
    ]
    # Accepted divergence D-0023: SQLite returned the lowest rowid instead.
    assert all(sql.endswith('ORDER BY "created" ASC LIMIT 1') for sql, _ in connection.calls)
