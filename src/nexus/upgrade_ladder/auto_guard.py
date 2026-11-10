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
cheaper to answer from (a completion record, a bounded engine count), does not read in an UNATTENDED run
(:mod:`nexus.upgrade_ladder.unattended`: ``--auto``, ``NX_UPGRADE_UNATTENDED=1``, the version-transition
callout) against a managed service. It reports itself NOT APPLICABLE, with a reason naming where the first scan
happens: an interactive ``nx upgrade``. A bounded engine count lets such a ``detect()`` answer (``manifest-section-labels``,
nexus-l4hds), but only as a REPORT: the same rung's ``converge()`` is the whole-catalog scan with writes, so it
checks :func:`refuses_unbounded_read` too and is DEFERRED unattended; an interactive ``nx upgrade`` fills. Interactive ``nx upgrade`` runs and local engines keep their
walk, except under ``nx doctor``, which runs its sweep as unattended (nexus-mq980) and with bounded reads only, on any
engine (nexus-mjmjl), and reports a refused rung as not checked.
"""
from __future__ import annotations

from collections.abc import Callable

import structlog

from nexus.upgrade_ladder.unattended import is_bounded, is_unattended

_log = structlog.get_logger(__name__)

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
    callout) against a managed service, or when it makes only bounded reads (``nx doctor``, any engine:
    nexus-mjmjl). Keyed on the explicit signal, never on a bound deadline."""
    return is_bounded() or (is_unattended() and managed_fn())


def refuses_managed_unattended(managed_fn: Callable[[], bool] = managed_service) -> bool:
    """The managed rule alone, without ``nx doctor``'s bounded-reads mode: for an answer that reads nothing
    (a completion record that predates the current rule says pending without a read), so doctor on a local
    engine still gives it (nexus-mjmjl review)."""
    return is_unattended() and managed_fn()


def default_completion_record(rung_name: str) -> object | None:
    """This tenant's completion record for *rung_name* from the engine's ledger (one bounded read), or ``None``.
    Raises when the ledger cannot be read."""
    from nexus.upgrade_ladder.http_store import HttpLadderStore  # noqa: PLC0415 — deferred: keeps the registry import cheap

    with HttpLadderStore() as store:
        return store.completions().get(rung_name)


def completion_recorded_at(record_fn: Callable[[], object | None], rung_name: str) -> str | None:
    """When *record_fn*'s completion for *rung_name* was verified (its ``verified_at``, or ``"an unknown time"`` if the
    record carries none), or ``None`` when no completion is on file. An unreadable ledger is not a record. This is the cheap
    answer a refusing ``detect()`` gives BEFORE it refuses (shakedown 7.94.0, nexus-mq980): an interactive
    ``nx upgrade`` records the rung once its scan verifies, and from then on every unattended run and every
    ``nx doctor`` answers from that record instead of reading the catalog."""
    try:
        record = record_fn()
    except Exception as exc:  # noqa: BLE001 — an unreadable ledger is 'not recorded', and the refusal decides
        _log.info("ladder_completion_ledger_unreadable", rung=rung_name, error=str(exc))
        return None
    if record is None:
        return None
    return str(getattr(record, "verified_at", "") or "") or "an unknown time"


def unattended_verify_refusal(what: str) -> str:
    """The ``verify_detail`` of a rung whose ``verify()`` was reached in an unattended run against a managed
    service (it would re-read *what*, which no unattended run does): a deferral, never a failure."""
    return f"unattended `nx upgrade --auto` does not re-read {what} of a managed service to confirm; {INTERACTIVE_REMEDY}"
