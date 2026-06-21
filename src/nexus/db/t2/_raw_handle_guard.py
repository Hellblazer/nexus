# SPDX-License-Identifier: AGPL-3.0-or-later
"""Fail-loud raw-handle guard for service-backed T2 stores (nexus-9613q.2).

The ``Http*Store`` classes delegate to the RDR-152 Java HTTP service and have
no SQLite ``.conn`` / ``._lock``. A consumer that reaches for one in service
mode (the 6.0 default) is the silent / loud breakage class nexus-9613q closed.
Native attribute lookup would already raise ``AttributeError``; these guard
properties make the error ACTIONABLE — they name the missing handle and point
at the fix — while still raising ``AttributeError`` (never ``RuntimeError``)
so ``hasattr`` and :func:`nexus.db.storage_mode.has_raw_access` keep returning
``False`` (a ``RuntimeError`` would propagate through ``hasattr`` and break the
guard contract).
"""
from __future__ import annotations

from typing import NoReturn


def _raise(cls_name: str, attr: str) -> NoReturn:
    raise AttributeError(
        f"{cls_name} is service-backed and has no raw SQLite '{attr}'. "
        f"Route through a public store method, or guard the access with "
        f"nexus.db.storage_mode.has_raw_access(store) and skip / degrade in "
        f"service mode (nexus-9613q)."
    )


class RawHandleGuardMixin:
    """Mixin giving ``Http*Store`` classes fail-loud ``.conn`` / ``._lock``."""

    @property
    def conn(self) -> NoReturn:
        _raise(type(self).__name__, "conn")

    @property
    def _lock(self) -> NoReturn:
        _raise(type(self).__name__, "_lock")
