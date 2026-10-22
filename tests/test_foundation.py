from ppbase import PPBase

from app.hooks.register import HOOK_MODULES, register


def make_ppbase() -> PPBase:
    return PPBase(
        database_url="postgresql+asyncpg://ppbase:ppbase@localhost:55434/buma_master_ppbase"
    )


def test_register_entrypoint_is_callable() -> None:
    assert callable(register)


def test_foundation_modules_are_explicit_and_ordered() -> None:
    assert HOOK_MODULES == (
        "app.hooks.health",
        "app.hooks.auth",
        "app.hooks.customer",
        "app.hooks.supplier",
        "app.hooks.employee",
        "app.hooks.pdv",
        "app.hooks.finance",
        "app.hooks.stockage",
        "app.hooks.commerce",
        "app.hooks.legacy_sql",
        "app.hooks.legacy_stockage",
        "app.hooks.legacy_records",
        "app.hooks.legacy_caisse",
        "app.hooks.legacy_people",
        "app.hooks.legacy_commerce",
        "app.hooks.legacy_master",
        "app.hooks.poc",
    )


def test_register_loads_foundation_routes() -> None:
    pb = make_ppbase()
    register(pb)
    registered = {
        (route.path, method)
        for route in pb._extensions._routes
        if route.methods
        for method in route.methods
    }
    assert ("/", "GET") in registered
    assert ("/__foundation/poc", "GET") in registered
    assert ("/__foundation/transaction", "POST") in registered
    assert ("/__foundation/transaction-rollback", "POST") in registered

    api_health = next(
        route
        for route in pb.get_app().router.routes
        if getattr(route, "include_context", None) and route.include_context.prefix == "/api"
    )

    def nested_routes(routes):
        for route in routes:
            original = getattr(route, "original_router", None)
            if original is not None:
                prefix = getattr(route.include_context, "prefix", "")
                for nested in nested_routes(original.routes):
                    nested.path = prefix.rstrip("/") + nested.path
                    yield nested
            else:
                yield route

    assert any(
        route.path == "/health" and "GET" in route.methods
        for route in nested_routes(api_health.original_router.routes)
    )
