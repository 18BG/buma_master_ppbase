from ppbase import PPBase

from app.hooks.register import HOOK_MODULES, register


def make_ppbase() -> PPBase:
    return PPBase(
        database_url="postgresql+asyncpg://ppbase:ppbase@localhost:55434/buma_master_ppbase"
    )


def test_register_entrypoint_is_callable() -> None:
    assert callable(register)


def test_foundation_modules_are_explicit_and_ordered() -> None:
    assert HOOK_MODULES == ("app.hooks.health", "app.hooks.poc")


def test_register_loads_foundation_routes() -> None:
    pb = make_ppbase()
    register(pb)
    registered = {
        (route.path, method)
        for route in pb.get_app().routes
        if getattr(route, "methods", None)
        for method in route.methods
    }
    assert ("/", "GET") in registered
    assert ("/api/health", "GET") in registered
    assert ("/__foundation/poc", "GET") in registered
    assert ("/__foundation/transaction", "POST") in registered
    assert ("/__foundation/transaction-rollback", "POST") in registered
