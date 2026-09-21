from pathlib import Path

from app.hooks.register import HOOK_MODULES

ROOT = Path(__file__).parents[1]


def test_c7_routes_and_hook_are_registered() -> None:
    source = (ROOT / "app/hooks/stockage.py").read_text()
    for path in (
        "/master/article/creates",
        "/master/stockage/transfer",
        "/master/stockage/cancel_transfer",
        "/master/stockage/convert",
        "/master/stockage/cancel_conversion",
        "/master/stockage/movement",
        "/master/stockage/batch-movement",
        "/master/stockage/wallet_uv",
        "/master/stockage/uv_wallet",
    ):
        assert path in source
    assert "app.hooks.stockage" in HOOK_MODULES


def test_c7_service_uses_atomic_coordinator_and_row_locks() -> None:
    source = (ROOT / "app/stockage/service.py").read_text()
    assert "run_record_storage_transaction" in source
    assert "FOR UPDATE" in source
    assert "pg_advisory_xact_lock" in source
    assert 'StockageValidationError("Stock insuffisant' in source


def test_c7_source_enum_divergence_is_explicitly_localized() -> None:
    source = (ROOT / "app/stockage/service.py").read_text()
    assert "WALLET_TO_UV" in source
    assert "CONVERSION_CANCEL_IN" in source
    assert "corrected export has no cancel_author/cancel_date columns" in source
