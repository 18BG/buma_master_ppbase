"""Ordered PPBase hook registration entrypoint.

Only foundation modules are registered in Checkpoint 1. Business modules are
added one domain at a time after their contract and tests are ready.
"""

from __future__ import annotations

import logging
from collections.abc import Callable
from importlib import import_module
from typing import Any

logger = logging.getLogger(__name__)

HOOK_MODULES: tuple[str, ...] = (
    "app.hooks.health",
    "app.hooks.poc",
)


def _load_register(module_name: str) -> Callable[[Any], None]:
    module = import_module(module_name)
    register_func = getattr(module, "register", None)
    if register_func is None or not callable(register_func):
        raise RuntimeError(f"Hook module {module_name!r} must expose register(pb)")
    return register_func


def register(pb: Any) -> None:
    """Register the currently implemented foundation modules in order."""
    logger.info("Loading BUMA Master PPBase foundation modules")
    for module_name in HOOK_MODULES:
        logger.info("Loading hook module %s", module_name)
        _load_register(module_name)(pb)
    logger.info("BUMA Master PPBase foundation modules loaded")
