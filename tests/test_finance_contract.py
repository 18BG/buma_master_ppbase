from pathlib import Path

from app.hooks.register import HOOK_MODULES


def test_wallet_and_caisse_core_routes_are_registered() -> None:
    source = (Path(__file__).parents[1] / "app/hooks/finance.py").read_text()
    for path in (
        "/master/wallets/deposit",
        "/master/wallets/withdraw",
        "/master/wallets/cancel_transaction",
        "/master/wallets/transfer",
        "/master/wallets/updateNote",
        "/master/caisse/requestOpen",
        "/master/caisse/requestClose",
        "/master/caisse/openCaisse",
        "/master/caisse/closeCaisse",
        "/master/caisse/new_correction",
        "/master/caisse/new_versement",
        "/master/caisse/bank_versement",
        "/master/caisse/cancel_bank_versement",
        "/master/caisse/expense",
        "/master/caisse/unallocate",
        "/master/caisse/updateAmount",
        "/master/caisse/changeTransactionCustomer",
        "/master/caisse/updateNote",
        "/master/caisse/changeCashier",
        "/master/caisse/expense_type/archive",
        "/master/caisse/verifySession/{sessionId}",
        "/master/caisse/employee_debt",
        "/master/caisse/employee_debt_repayment",
        "/master/caisse/accessible",
        "/master/caisse/deposit",
        "/master/caisse/withdraw",
        "/master/caisse/transfer",
    ):
        assert path in source
    assert "app.hooks.finance" in HOOK_MODULES


def test_c8_preserves_source_response_messages_and_atomicity() -> None:
    root = Path(__file__).parents[1]
    service = (root / "app/finance/service.py").read_text()
    for message in (
        "Transaction effectuée",
        "Transaction annulée",
        "Requete envoyer",
        "Transaction reussis",
        "Versement bancaire réussi",
        "Caissier changé avec succès",
    ):
        assert message in service
    assert "run_record_storage_transaction" in service
    assert "FOR UPDATE" in service
    assert "pg_advisory_xact_lock" in service


def test_c9_cross_domain_caisse_routes_are_not_claimed_by_c8() -> None:
    source = (Path(__file__).parents[1] / "app/hooks/finance.py").read_text()
    for path in (
        "/master/caisse/return",
        "/master/caisse/versement_uv",
        "/master/caisse/add_payment_to_sale",
        "/master/caisse/reportByExpenseType",
        "/master/caisse/reportByCaisseType",
    ):
        assert path not in source
