# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (c) 2026 Hal Hildebrand. All rights reserved.
"""The managed-service rule for unattended ``detect()`` walks (nexus-75w5d).

``nx upgrade --auto`` runs at every session start, and the plugin's lockstep action starts a plain
``nx upgrade`` on a version drift. On 2026-10-31 one rung's ``detect()`` walked the
whole cloud catalog (43,574 documents: paged listing, ``chash_positions``, a chunk read per document)
in every one of those runs, nineteen at once, and the managed edge ran out of admission permits. A
local engine's walk is cheap and the ``--auto`` deadline bounds it; a managed service's is a 15-minute
walk of shared capacity per session start, and no deadline makes that acceptable.

The rule: a ``detect()`` whose read cost grows with the catalog or a table, and that has nothing
cheaper to answer from (a completion record, a remembered clean scan), does not read in an UNATTENDED run
(:mod:`nexus.upgrade_ladder.unattended`: ``--auto``, ``NX_UPGRADE_UNATTENDED=1``, the version-transition
callout) against a managed service. It reports itself NOT APPLICABLE, with a reason naming where the first scan
happens: an interactive ``nx upgrade``. Interactive runs, ``nx doctor`` and local engines keep their
walk.
"""
from __future__ import annotations

from collections.abc import Callable

from nexus.upgrade_ladder.unattended import is_unattended

#: Said in every refusal's ``pending_detail``, so the reason reads the same on every rung.
INTERACTIVE_REMEDY = "an interactive `nx upgrade` performs the first scan"


def managed_service() -> bool:
    """True when ``not is_local_mode()``: a ``service_url`` is configured and ``NX_LOCAL=0`` or
    ``install.mode`` is ``managed``. A service on a LAN host counts as managed: the rule is about
    capacity shared by other boxes, not about the vendor."""
    from nexus.config import is_local_mode  # noqa: PLC0415 — deferred: keeps the registry import cheap

    return not is_local_mode()


def refuses_unbounded_read(managed_fn: Callable[[], bool] = managed_service) -> bool:
    """True when this run is unattended (``--auto``, ``NX_UPGRADE_UNATTENDED=1``, the version-transition
    callout) against a managed service. Keyed on the explicit signal, never on a bound deadline."""
    return is_unattended() and managed_fn()
