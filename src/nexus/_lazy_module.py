# SPDX-License-Identifier: AGPL-3.0-or-later
"""A module reference that imports on first attribute access.

``T2Database.__init__`` imports every T2 store module, and two of them
(``http_taxonomy_store``, ``taxonomy_compute``) used numpy at module scope,
which dragged numpy in on every process's first T2 handle whether or not it
ever touched taxonomy. On native Windows that import blocked indefinitely
inside the MCP server (nexus-fd3zf). ``np = lazy_module("numpy")`` keeps every
``np.<attr>`` call site unchanged and moves the import to the first call
that actually needs it.

Pair it with a ``TYPE_CHECKING`` import so the type checker still sees the
real module::

    if TYPE_CHECKING:
        import numpy as np
    else:
        np = lazy_module("numpy")
"""

from __future__ import annotations

import importlib
from types import ModuleType
from typing import Any


class LazyModule:
    """Proxy that resolves ``importlib.import_module(name)`` on first use."""

    __slots__ = ("_name", "_module")

    def __init__(self, name: str) -> None:
        self._name = name
        self._module: ModuleType | None = None

    def __getattr__(self, attr: str) -> Any:
        module = self._module
        if module is None:
            module = importlib.import_module(self._name)
            self._module = module
        return getattr(module, attr)

    def __repr__(self) -> str:
        state = "loaded" if self._module is not None else "not loaded"
        return f"<LazyModule {self._name!r} ({state})>"


def lazy_module(name: str) -> Any:
    """Return a proxy for module ``name`` that imports it on first access."""
    return LazyModule(name)
