# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (c) 2026 Hal Hildebrand. All rights reserved.
"""The explicit "this run is unattended" signal of the upgrade ladder (nexus-75w5d).

A ``detect()`` that reads the whole catalog (or a whole table) must not do so in a run nobody is
watching, against a managed service shared by every box of a tenant. Which runs are unattended used to
be inferred from a bound deadline, which only ``nx upgrade --auto`` builds: the lockstep action's
detached PLAIN ``nx upgrade`` bound none, took no lock, and walked the cloud catalog once per version
drift per box. The signal is now its own contextvar, independent of the clock:

* ``nx upgrade --auto`` (argv) and any ``nx`` run started with ``NX_UPGRADE_UNATTENDED=1`` set it at
  the top of the root group (:mod:`nexus.cli`) and in the ``upgrade`` command;
* the version-transition callout sets it around its ``detect()`` calls (a cheap "pending?" answer);
* an interactive ``nx upgrade`` and ``nx doctor`` never set it, and keep their walks.

The deadline stays a clock (:mod:`nexus.upgrade_ladder.deadline`); this module is the question "is
anyone watching?".
"""
from __future__ import annotations

import contextlib
import contextvars
from collections.abc import Iterator

from nexus.util.upgrade_auto_lock import UNATTENDED_ENV, env_unattended

__all__ = ["UNATTENDED_ENV", "env_unattended", "is_unattended", "unattended_run"]

_UNATTENDED: contextvars.ContextVar[bool] = contextvars.ContextVar("nexus_upgrade_unattended", default=False)


def is_unattended() -> bool:
    """True inside :func:`unattended_run`."""
    return _UNATTENDED.get()


@contextlib.contextmanager
def unattended_run(active: bool = True) -> Iterator[None]:
    """Mark the enclosed run unattended (or, with ``active=False``, leave the flag as it is)."""
    if not active:
        yield
        return
    token = _UNATTENDED.set(True)
    try:
        yield
    finally:
        _UNATTENDED.reset(token)
