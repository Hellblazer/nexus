# SPDX-License-Identifier: AGPL-3.0-or-later
"""``nx doctor``'s "Recall canary" row (nexus-4lnn1.1.10, RDR-230 Technical Design 7 and Day 2 Operations).

The engine reports its recall canary as counts under ``per_collection_indexes.this_engine.maintenance.canary`` in
``GET /v1/status`` (``green``, ``red``, ``unmeasured``, ``last_run_at``): this engine's own history, kept after it
becomes a standby, and zeros for an engine that has never held the builder lock. The route is unauthenticated, so no
``pci_`` index is named (Sam, 2026-11-06, ask ``pci-names-on-status``, option b); the per-index list is ``canary_indexes``
on the operator-only ``GET /v1/admin/chunks-leaves``. This row names that route and never calls it.

* warn: ``red`` > 0, with the count, the other counts and where the names are.
* pass: no red; the counts are shown (``unmeasured`` is information, not a warning).
* not applicable (ok, no warning): the engine cannot be reached, or does not report ``per_collection_indexes``,
  ``this_engine``, ``maintenance`` or ``canary`` (an older engine, or one still starting).
* warn "could not read": a key that is present and cannot be read (not an object, or a count that is not a non-negative
  int). The row never reads such a value as 0, which would be a clean row it did not check.

Kept out of :mod:`nexus.health` (a file over its size cap), like :mod:`nexus.data_moves_health`. The status body is
fetched by the caller, as for the other engine-status rows.
"""
from __future__ import annotations

import structlog

from nexus.health import _ENGINE_STATUS_UNSET, HealthResult

_log = structlog.get_logger(__name__)

LABEL = "Recall canary"

#: The authenticated route whose ``canary_indexes`` names each index the status counts.
ADMIN_ROUTE = "GET /v1/admin/chunks-leaves"

_RDR = "https://github.com/Hellblazer/nexus/blob/main/docs/rdr/rdr-230-event-keyed-reindex-and-recall-canary.md (Day 2 Operations)"


def _count(value: object) -> int | None:
    """A canary count, or None when it is not a non-negative int (a bool is not a count)."""
    return value if isinstance(value, int) and not isinstance(value, bool) and value >= 0 else None


def check_recall_canary(engine_status: object = _ENGINE_STATUS_UNSET) -> list[HealthResult]:
    """The recall canary row; see the module docstring. *engine_status* is the ``GET /v1/status`` body the doctor already
    fetched (``None`` when that failed); left unset, this fetches its own."""
    status: dict | None
    if engine_status is _ENGINE_STATUS_UNSET:
        try:
            from nexus.db.http_engine_status import fetch_engine_status  # noqa: PLC0415 — deferred to keep CLI startup fast

            status = fetch_engine_status()
        except Exception as exc:  # noqa: BLE001 — best-effort: must not crash `nx doctor`
            _log.debug("doctor_recall_canary_probe_failed", error=str(exc))
            status = None
    else:
        status = engine_status if isinstance(engine_status, dict) else None

    def _na(why: str) -> list[HealthResult]:
        return [HealthResult(label=LABEL, ok=True, detail=f"not applicable: {why}")]

    def _unreadable(what: str) -> list[HealthResult]:
        return [HealthResult(
            label=LABEL, ok=False, warn=True,
            detail=f"could not read the engine's recall canary: {what}",
            fix_suggestions=["The engine reports a canary object this client does not understand: check that the "
                             "client and engine versions match (`nx doctor` reports both)", _RDR],
        )]

    if status is None:
        return _na("the engine's status endpoint could not be read")
    pci = status.get("per_collection_indexes")
    if pci is None:
        return _na("this engine's status does not include the per-collection index object (an older engine, or one "
                   "that is still starting)")
    if not isinstance(pci, dict):
        return _unreadable(f"per_collection_indexes is not an object (got {repr(pci)[:60]})")
    this_engine = pci.get("this_engine")
    maintenance = this_engine.get("maintenance") if isinstance(this_engine, dict) else None
    if maintenance is None:
        return _na("this engine's status does not report its maintenance state (an older engine, or one that is "
                   "still starting)")
    if not isinstance(maintenance, dict):
        return _unreadable(f"maintenance is not an object (got {repr(maintenance)[:60]})")
    canary = maintenance.get("canary")
    if canary is None:
        return _na("this engine's status does not report a recall canary (an older engine)")
    if not isinstance(canary, dict):
        return _unreadable(f"maintenance.canary is not an object (got {repr(canary)[:60]})")

    red, green, unmeasured = _count(canary.get("red")), _count(canary.get("green")), _count(canary.get("unmeasured"))
    if red is None:
        return _unreadable(f"maintenance.canary.red is not a non-negative count (got {repr(canary.get('red'))[:40]})")
    last_run = canary.get("last_run_at")
    when = (f"last run {last_run}" if isinstance(last_run, str) and last_run
            else "no run yet" if last_run is None else "no run time reported")
    counts = f"{'?' if green is None else green} green, {red} red, {'?' if unmeasured is None else unmeasured} unmeasured"

    if red > 0:
        return [HealthResult(
            label=LABEL, ok=False, warn=True,
            detail=(f"{red} per-collection {'index reads' if red == 1 else 'indexes read'} red on the recall canary "
                    f"({counts}; {when}); /v1/status carries counts only, so {ADMIN_ROUTE} (operator token) lists "
                    "them under canary_indexes"),
            fix_suggestions=[
                f"{ADMIN_ROUTE} needs the operator (root) token (`nx config set service_token <root token>` or "
                "NX_SERVICE_TOKEN): its canary_indexes entry names each red index with its min_recall, trigger and "
                "red_after_reindex",
                "The engine log has event=pci_canary lines for each run, with the index, collection and tenant",
                _RDR,
            ],
        )]
    if green is None or unmeasured is None:
        bad = "green" if green is None else "unmeasured"
        return _unreadable(f"maintenance.canary.{bad} is not a non-negative count (got {repr(canary.get(bad))[:40]})")
    return [HealthResult(label=LABEL, ok=True, detail=f"{counts}; {when}")]
