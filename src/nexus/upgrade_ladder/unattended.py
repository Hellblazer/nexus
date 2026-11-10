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
* ``nx doctor`` sets it around its pending-rungs sweep (nexus-mq980): a managed catalog is not walked for a
  doctor row either, and the row reports those rungs as not checked;
* an interactive ``nx upgrade`` never sets it, and keeps its walks.

The deadline stays a clock (:mod:`nexus.upgrade_ladder.deadline`); this module is the question "is
anyone watching?".
"""
from __future__ import annotations

import contextlib
import contextvars
from collections.abc import Iterator

from nexus.util.upgrade_auto_lock import UNATTENDED_ENV, env_unattended

__all__ = [
    "UNATTENDED_ENV", "bounded_reads", "cheap_detect", "env_unattended", "is_bounded", "is_cheap_detect",
    "is_unattended", "unattended_run",
]

_UNATTENDED: contextvars.ContextVar[bool] = contextvars.ContextVar("nexus_upgrade_unattended", default=False)
#: nexus-mjmjl: ``nx doctor`` decides whether a rung is pending without any read whose cost grows with the store,
#: against a local engine too (marginalia's py-spy: ~210 s of a 207-239 s doctor in one rung's dry-run scan).
_BOUNDED: contextvars.ContextVar[bool] = contextvars.ContextVar("nexus_upgrade_bounded_reads", default=False)
#: nexus-mjmjl: an interactive ``nx upgrade`` (not ``--dry-run``) asks each rung a cheap "is it pending?": a rung that
#: cannot answer from its record or a bounded count says PENDING, and its converge makes the one scan (with writes and
#: per-batch progress) instead of a dry-run scan in detect followed by the same scan in converge.
_CHEAP_DETECT: contextvars.ContextVar[bool] = contextvars.ContextVar("nexus_upgrade_cheap_detect", default=False)


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


def is_bounded() -> bool:
    """True inside :func:`bounded_reads`."""
    return _BOUNDED.get()


@contextlib.contextmanager
def bounded_reads() -> Iterator[None]:
    """Mark the enclosed run as one that makes no read whose cost grows with the store (``nx doctor``): a rung that
    cannot answer from its completion record or a bounded engine count reports itself not checked, whatever the
    engine (nexus-mjmjl)."""
    token = _BOUNDED.set(True)
    try:
        yield
    finally:
        _BOUNDED.reset(token)


def is_cheap_detect() -> bool:
    """True inside :func:`cheap_detect`."""
    return _CHEAP_DETECT.get()


@contextlib.contextmanager
def cheap_detect() -> Iterator[None]:
    """Mark the enclosed run as an interactive ``nx upgrade`` whose rungs answer detect() without a scan: unknown
    reads as pending and the converge scans once (nexus-mjmjl)."""
    token = _CHEAP_DETECT.set(True)
    try:
        yield
    finally:
        _CHEAP_DETECT.reset(token)
