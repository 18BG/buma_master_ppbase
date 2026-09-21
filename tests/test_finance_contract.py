from pathlib import Path

from app.hooks.register import HOOK_MODULES


def test_wallet_and_caisse_core_routes_are_registered() -> None:
    source = (Path(__file__).parents[1] / "app/hooks/finance.py").read_text()
    for path in (
        "/master/wallets/deposit",
        "/master/wallets/withdraw",
        "/master/wallets/transfer",
        "/master/caisse/deposit",
        "/master/caisse/withdraw",
        "/master/caisse/transfer",
    ):
        assert path in source
    assert "app.hooks.finance" in HOOK_MODULES
