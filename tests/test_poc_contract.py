from pathlib import Path

from app.hooks.register import HOOK_MODULES

ROOT = Path(__file__).resolve().parents[1]


def test_poc_is_not_an_official_schema_migration() -> None:
    migration = ROOT / "pb_migrations/0000000001_foundation_poc.py"
    assert not migration.exists()
    assert "transaction-rollback" in (ROOT / "app/hooks/poc.py").read_text(encoding="utf-8")
    assert '"rolled_back": True' in (ROOT / "app/hooks/poc.py").read_text(encoding="utf-8")


def test_poc_is_registered_after_health() -> None:
    assert HOOK_MODULES == (
        "app.hooks.health",
        "app.hooks.auth",
        "app.hooks.customer",
        "app.hooks.supplier",
        "app.hooks.employee",
        "app.hooks.pdv",
        "app.hooks.finance",
        "app.hooks.stockage",
        "app.hooks.poc",
    )
