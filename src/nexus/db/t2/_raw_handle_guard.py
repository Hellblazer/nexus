# SPDX-License-Identifier: AGPL-3.0-or-later
"""Fail-loud raw-handle guard for service-backed T2 stores (nexus-9613q.2).

The ``Http*Store`` classes delegate to the RDR-152 Java HTTP service and have
no SQLite ``.conn`` / ``._lock``. A consumer that reaches for one is the
silent / loud breakage class nexus-9613q closed — and since the RDR-158 P4
retirement deleted the SQLite stores, there is no raw handle ANYWHERE:
this mixin is the post-SQLite fail-loud tripwire. Native attribute lookup
would already raise ``AttributeError``; these guard properties make the
error ACTIONABLE — they name the missing handle and point at the fix —
while still raising ``AttributeError`` (never ``RuntimeError``) so
``hasattr`` probes keep returning ``False`` (a ``RuntimeError`` would
propagate through ``hasattr`` and break the guard contract; the
``has_raw_access`` helper that idiom served died with its last caller,
RDR-158 P3 nexus-7bomn).
"""
from __future__ import annotations

from typing import NoReturn


def _raise(cls_name: str, attr: str) -> NoReturn:
    raise AttributeError(
        f"{cls_name} is service-backed and has no raw SQLite '{attr}'. "
        f"The SQLite T2 stores were deleted (RDR-158 P4) — there is no raw "
        f"handle to fall back to. Route through a public store method, or "
        f"add the missing operation to the engine API (nexus-9613q)."
    )


class RawHandleGuardMixin:
    """Mixin giving ``Http*Store`` classes fail-loud ``.conn`` / ``._lock``."""

    @property
    def conn(self) -> NoReturn:
        _raise(type(self).__name__, "conn")

    @property
    def _lock(self) -> NoReturn:
        _raise(type(self).__name__, "_lock")
