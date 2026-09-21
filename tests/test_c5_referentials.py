from pathlib import Path

from app.settings.service import SettingsNotFoundError


def test_c5_defaults_migration_contains_source_reference_ids() -> None:
    migration = (
        Path(__file__).parents[1] / "pb_migrations/1789236846_c5_reference_defaults.py"
    ).read_text()
    for value in (
        "STOCK_PRINCIPAL",
        "PAYMENT_CASH_12",
        "PAYMENT_MOBILE_",
        "PAYMENT_CARTE_1",
        "CUSTOMER_DIVERS",
        "SUPPLIER_DIVERS",
        '"caisse"',
        '"wallet"',
    ):
        assert value in migration


def test_settings_missing_error_keeps_source_contract() -> None:
    assert issubclass(SettingsNotFoundError, LookupError)
