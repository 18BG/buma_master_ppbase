"""Ordered PPBase hook registration entrypoint.

Authentication compatibility is registered after the foundation routes. The
validated business modules follow it in checkpoint order; unfinished domains
are intentionally not registered.
"""

from __future__ import annotations

import logging
from collections.abc import Callable
from importlib import import_module
from typing import Any

logger = logging.getLogger(__name__)

HOOK_MODULES: tuple[str, ...] = (
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


def _load_register(module_name: str) -> Callable[[Any], None]:
    module = import_module(module_name)
    register_func = getattr(module, "register", None)
    if register_func is None or not callable(register_func):
        raise RuntimeError(f"Hook module {module_name!r} must expose register(pb)")
    return register_func


def register(pb: Any) -> None:
    """Register foundation, validated auth and business checkpoint modules."""
    logger.info("Loading BUMA Master PPBase foundation/auth/business modules")
    for module_name in HOOK_MODULES:
        logger.info("Loading hook module %s", module_name)
        _load_register(module_name)(pb)
    logger.info("BUMA Master PPBase foundation/auth/business modules loaded")
