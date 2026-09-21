from pathlib import Path

from app.transactions.idempotency import (
    IdempotencyLookup,
    build_idempotency_key,
)


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
