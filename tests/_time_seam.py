# SPDX-License-Identifier: AGPL-3.0-or-later
"""Fake a module's clock or sleep without touching the global ``time`` module.

``patch("nexus.retry.time.sleep")`` and ``monkeypatch.setattr(mod.time, "sleep", f)``
do not patch ``nexus.retry``: ``mod.time`` IS the stdlib ``time`` module, so they
replace ``time.sleep`` for the whole process. Anything else that sleeps while the
fake is live gets it too. ``subprocess.run(timeout=...)`` waits in a loop around
``time.sleep``, so a substrate teardown (``drop_test_tenant``) that ran under a
test's blocking fake sleep hung its xdist worker for 50 minutes (nexus-hkafl).

The helpers here install a :class:`TimeProxy` as the module's own ``time`` global
instead. The proxy forwards every attribute to the real ``time`` module except the
ones a test sets on it, so the code under test sees the fake and nothing else does.
``tests/test_time_seam.py`` pins both halves and lints ``tests/`` for the global form.
"""
from __future__ import annotations

import importlib
import time as _real_time
from collections.abc import Iterator
from contextlib import ExitStack, contextmanager
from types import ModuleType
from typing import Any
from unittest.mock import DEFAULT, MagicMock, patch

import pytest

#: A retry test faking ``nexus.retry``'s sleep has always faked two more layers
#: through the global module, and keeps doing so by sharing the proxy: the default
#: ``RateLimitBrake``, whose sleep and clock are looked up in ``nexus.rate_brake``'s
#: own ``time`` global when used (nexus-q81g7), and the vector client's gateway
#: retry under the wrapper (``nexus.db.http_vector_client``'s 502/503/504 sleeps).
_COUPLED: dict[str, tuple[str, ...]] = {
    "nexus.retry": ("nexus.rate_brake", "nexus.db.http_vector_client"),
}


class TimeProxy:
    """Stands in for the ``time`` module inside one or more modules.

    Attributes set on an instance override; everything else is the real
    ``time`` module's."""

    def __getattr__(self, name: str) -> Any:
        return getattr(_real_time, name)


def _resolve(module: ModuleType | str) -> ModuleType:
    return importlib.import_module(module) if isinstance(module, str) else module


def _targets(module: ModuleType | str, also: tuple[ModuleType | str, ...]) -> list[ModuleType]:
    first = _resolve(module)
    mods = [first, *(_resolve(m) for m in _COUPLED.get(first.__name__, ())), *map(_resolve, also)]
    unique: list[ModuleType] = []
    for mod in mods:
        if mod not in unique:
            unique.append(mod)
    return unique


def _proxy_for(mods: list[ModuleType]) -> TimeProxy:
    for mod in mods:
        if not hasattr(mod, "time"):
            raise AttributeError(f"{mod.__name__} has no module-level 'time' to replace")
        current = mod.time
        if isinstance(current, TimeProxy):
            return current
    return TimeProxy()


def module_time(
    monkeypatch: pytest.MonkeyPatch, module: ModuleType | str, *also: ModuleType | str,
) -> TimeProxy:
    """Give ``module`` (and ``also``) a private ``time`` for the rest of the test.

    Returns the proxy; set what to fake on it::

        module_time(monkeypatch, retry_mod).sleep = fake.sleep

    Calling it again for a module that already has a proxy returns that proxy.
    ``monkeypatch`` restores each module's ``time`` binding at teardown, and the
    global ``time`` module is never modified."""
    mods = _targets(module, also)
    proxy = _proxy_for(mods)
    for mod in mods:
        if mod.time is not proxy:
            monkeypatch.setattr(mod, "time", proxy)
    return proxy


@contextmanager
def patch_time(
    module: ModuleType | str,
    attr: str,
    new: Any = DEFAULT,
    *,
    also: tuple[ModuleType | str, ...] = (),
    **mock_kwargs: Any,
) -> Iterator[Any]:
    """Context-manager twin of ``patch("<module>.time.<attr>")`` that stays local.

    Yields the replacement: a ``MagicMock(**mock_kwargs)`` unless ``new`` is
    given, exactly as ``unittest.mock.patch`` would."""
    mods = _targets(module, also)
    proxy = _proxy_for(mods)
    value = MagicMock(**mock_kwargs) if new is DEFAULT else new
    with ExitStack() as stack:
        for mod in mods:
            if mod.time is not proxy:
                stack.enter_context(patch.object(mod, "time", proxy))
        stack.enter_context(patch.object(proxy, attr, value))
        yield value
