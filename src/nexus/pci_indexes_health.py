# SPDX-License-Identifier: AGPL-3.0-or-later
"""``nx doctor``'s "Per-collection indexes" row (nexus-43ulx.24, RDR-227 Day 2 Operations; the id-predicate counts RDR-229 P3.4).

Reads the ``per_collection_indexes`` object of ``GET /v1/status``: the read half's global ``valid`` / ``invalid`` / ``unparsed`` /
``reindex_leftover`` counts (and ``valid_id`` / ``invalid_id``, the id-predicate indexes that serve once gate 3 latches), and in
``this_engine`` what this engine's builder is doing. See :func:`check_per_collection_indexes` for the verdicts.

Kept out of :mod:`nexus.health` (a file over its size cap), like :mod:`nexus.recall_canary_health`; ``nexus.health._check_per_collection_indexes``
calls it and the doctor registers that, so the row's name, order and output are as before. The status body is fetched by the caller, as for
the other engine-status rows.
"""
from __future__ import annotations

from datetime import UTC, datetime

import structlog

from nexus.health import _ENGINE_STATUS_UNSET, HealthResult, _instant, _span, _status_int

_log = structlog.get_logger(__name__)

_PCI_LABEL = "Per-collection indexes"

#: ``per_collection_indexes.this_engine.builder_state`` values the row knows. Anything else is an engine newer than
#: this client and reads not applicable rather than a guess.
_PCI_BUILDER_STATES = frozenset({"ok", "auth_failed", "no_privilege", "off", "standby"})

#: A pass that has been in flight this long is stuck or unusually long. There is no pass deadline (serial
#: ``CREATE INDEX CONCURRENTLY`` builds run up to 30 minutes each), so an hour is the point at which an operator should
#: look; a very long first pass over many collections warns too, and the detail says so.
_PCI_PASS_STUCK_SECONDS = 3600

#: The DDL sweep period when the engine does not report ``sweep_seconds`` (the setting's default, 600 s).
_PCI_DEFAULT_SWEEP_SECONDS = 600

#: Margin added to three sweep periods before a holder with no new completed pass reads as stalled: the read bound
#: (the engine's catalog statement bound, 5 minutes by default) with room for one slow counting transaction.
_PCI_STALL_MARGIN_SECONDS = 900
_PCI_STALL_SECONDS_PERIODS = 3


def check_per_collection_indexes(
    engine_status: object = _ENGINE_STATUS_UNSET, *, now: datetime | None = None,
) -> list[HealthResult]:
    """nexus-43ulx.24 (RDR-227 Day 2): are the per-collection partial HNSW indexes being built and usable?

    Reads the ``per_collection_indexes`` object of ``GET /v1/status`` (nexus-43ulx.23): the read half's global
    ``valid`` / ``invalid`` / ``unparsed`` / ``reindex_leftover`` counts (and ``valid_id`` / ``invalid_id``, the id-predicate
    indexes that serve once gate 3 latches, shown as ``+ N by id`` and counted with ``invalid`` for the warning), and in ``this_engine`` what this engine's
    builder is doing
    (``builder_state``, ``failing``, ``last_ddl_pass_at``). The same status body serves the Engine reaper row, so a
    local and a managed engine are judged the same way (HTTP only).

    * fail (hard ✗): ``builder_state`` ``auth_failed`` (the engine's admin credential was rejected, typically after a
      ``nexus_admin`` rotation) or ``no_privilege`` (the admin role cannot create indexes on the leaves). Checked
      before the counts, so a dead builder cannot read green on ``invalid=0``.
    * warn: ``expired`` (the router's index set is frozen and answering as empty, so every statement walks at the
      serving ``ef_search``), ``invalid`` > 0 or ``failing`` > 0. While this engine's builder is mid-build
      (``this_engine.building`` is 1, or ``pass_in_progress`` is true) ``invalid`` does NOT warn: the index being built is
      invalid by design until its ``CREATE INDEX CONCURRENTLY`` finishes, and the row says "first pass in progress" or
      "build in progress" instead. ``expired`` and ``failing`` still warn then. On a ``standby`` engine whose last
      catalog read is fresh (not ``expired``), ``invalid`` does not warn either: a peer holds the builder lock and the
      invalid index may be its build in flight (this engine cannot tell a build from a failed one), so the row passes
      with a note.
    * warn, too: a pass in flight for more than an hour (``pass_started_at``; there is no pass deadline, so a hung
      or very long pass is otherwise green), and a holder (``builder_state`` ``ok``) with no completed pass for three
      sweep periods plus 15 minutes (``last_ddl_pass_at``; a holder whose every leaf is skipped by count timeouts
      completes no pass and would otherwise read green). The period is the engine's ``sweep_seconds`` (600 when an
      older engine does not report it). A holder that has never completed a pass warns on the same threshold, read
      from the engine's ``process_start_time``.
    * warn, too: ``reindex_leftover`` > 0 (nexus-rn1yd: an invalid ``_ccnew``/``_ccold`` index a failed REINDEX
      CONCURRENTLY left behind) while ``builder_state`` is ``off``, because nothing will drop it. ``auth_failed`` and
      ``no_privilege`` already fail and carry the count. On ``ok`` the next pass drops it and on ``standby`` a peer
      does, so the count is shown and never a warning there.
    * pass: ``ok`` or ``standby`` (a peer holds the builder lock) with none of those; ``off`` passes with a note that
      builds are disabled by ``NX_SEARCH_PCI=0``.

    Not applicable (ok, no warning) when the engine cannot be reached or does not report the object. A present object
    that cannot be read, or a ``builder_state`` outside the closed vocabulary above, warns and names what it got:
    a rotation check reads that vocabulary, and a green row over an unrecognised state would hide the one case the
    row exists to show. A count that is not a non-negative int is read as 0, as the reaper row reads its counters.
    """
    label = _PCI_LABEL
    status: dict | None
    if engine_status is _ENGINE_STATUS_UNSET:
        try:
            from nexus.db.http_engine_status import fetch_engine_status  # noqa: PLC0415 — deferred to keep CLI startup fast
            status = fetch_engine_status()
        except Exception as exc:  # noqa: BLE001 — best-effort: must not crash `nx doctor`
            _log.debug("doctor_per_collection_indexes_probe_failed", error=str(exc))
            status = None
    else:
        status = engine_status if isinstance(engine_status, dict) else None

    def _na(why: str) -> list[HealthResult]:
        return [HealthResult(label=label, ok=True, detail=f"not applicable: {why}")]

    if status is None:
        return _na("the engine's status endpoint could not be read")
    pci = status.get("per_collection_indexes")
    if pci is None:
        return _na("this engine's status does not include the per-collection index object (an older engine, or "
                   "one that is still starting)")
    this_engine = pci.get("this_engine") if isinstance(pci, dict) else None
    state = this_engine.get("builder_state") if isinstance(this_engine, dict) else None

    def _unreadable(why: str) -> list[HealthResult]:
        return [HealthResult(
            label=label, ok=False, warn=True, detail=why,
            fix_suggestions=[
                "The engine reports a per_collection_indexes object this client does not understand: check that "
                "the client and engine versions match (`nx doctor` reports both)",
                "https://github.com/Hellblazer/nexus/blob/main/docs/rdr/rdr-227-per-collection-hnsw-above-the-router-threshold.md (Day 2 Operations)",
            ],
        )]

    if not isinstance(state, str):
        return _unreadable("the engine's per_collection_indexes object could not be read "
                           f"(this_engine.builder_state missing or not a string; got {repr(pci)[:80]})")
    if state not in _PCI_BUILDER_STATES:
        return _unreadable(f"unknown builder_state {state!r}; this client knows "
                           f"{', '.join(sorted(_PCI_BUILDER_STATES))}")

    now = now or datetime.now(UTC)
    valid, invalid = _status_int(pci.get("valid")), _status_int(pci.get("invalid"))
    unparsed, failing = _status_int(pci.get("unparsed")), _status_int(this_engine.get("failing"))
    leftover = _status_int(pci.get("reindex_leftover"))
    # RDR-229 P3.4: once gate 3 latches the id-predicate indexes serve and the name ones retire, so ``valid`` alone reads 0.
    valid_id, invalid_id = _status_int(pci.get("valid_id")), _status_int(pci.get("invalid_id"))
    last_ddl = _instant(this_engine.get("last_ddl_pass_at"))
    ddl_note = ("no DDL pass yet" if last_ddl is None
                else f"last DDL pass {_span(max(0.0, (now - last_ddl).total_seconds()))} ago")
    counts = (f"{valid} valid" + (f" + {valid_id} by id" if valid_id else "")
              + f", {invalid} invalid" + (f" + {invalid_id} by id" if invalid_id else "")
              + (f", {unparsed} unparsed" if unparsed else "")
              + (f", {leftover} reindex leftover" if leftover else ""))
    expired = pci.get("expired") is True
    building_now = _status_int(this_engine.get("building")) == 1
    pass_in_flight = this_engine.get("pass_in_progress") is True
    in_flight = building_now or pass_in_flight
    pass_started = _instant(this_engine.get("pass_started_at"))
    pass_age = None if pass_started is None else max(0.0, (now - pass_started).total_seconds())
    if in_flight:
        progress = ("first pass in progress" if pass_in_flight and last_ddl is None
                    else "build in progress" if building_now else "pass in progress")
        if pass_age is not None:
            progress = f"{progress} for {_span(pass_age)}"
        ddl_note = progress if last_ddl is None else f"{progress}, {ddl_note}"

    if state in ("auth_failed", "no_privilege"):
        why = ("the engine's admin credential was rejected" if state == "auth_failed"
               else "the engine's admin role has no privilege to create indexes on the leaves")
        return [HealthResult(
            label=label, ok=False,
            detail=f"builder_state {state}: {why}, so no per-collection index is being built ({counts})",
            fix_suggestions=[
                *(["After a nexus_admin credential rotation, restart the engine so it reads the new credential "
                   "(`nx daemon service stop`, then `nx daemon service start` for a local engine); the engine logs "
                   "event=pci_builder_auth_failed"] if state == "auth_failed" else
                  ["The engine's admin role cannot create indexes on the leaves of nexus.chunks: grant it the "
                   "privilege (it must own or be able to CREATE INDEX on the partitions), then restart the engine"]),
                "NX_SEARCH_PCI=0 switches the builder off on purpose, and then this row passes with a note",
                "https://github.com/Hellblazer/nexus/blob/main/docs/rdr/rdr-227-per-collection-hnsw-above-the-router-threshold.md (Day 2 Operations)",
            ],
        )]
    off_note = "; builds disabled by NX_SEARCH_PCI=0" if state == "off" else ""
    peer_may_be_building = (state == "standby" and not expired and _instant(pci.get("last_read_at")) is not None
                            and invalid + invalid_id > 0)
    # The index being built is invalid until its build finishes; on a standby with a fresh read it may be a peer's.
    warn_invalid = 0 if in_flight or peer_may_be_building else invalid + invalid_id

    sweep_seconds = _status_int(pci.get("sweep_seconds")) or _PCI_DEFAULT_SWEEP_SECONDS
    stall_seconds = _PCI_STALL_SECONDS_PERIODS * sweep_seconds + _PCI_STALL_MARGIN_SECONDS
    stuck = pass_in_flight and pass_age is not None and pass_age > _PCI_PASS_STUCK_SECONDS
    stalled = False
    stall_note = ""
    if state == "ok" and not in_flight:
        if last_ddl is not None:
            idle = max(0.0, (now - last_ddl).total_seconds())
            stalled = idle > stall_seconds
            stall_note = f"no completed DDL pass for {_span(idle)}"
        else:
            started = _instant(status.get("process_start_time"))
            uptime = None if started is None else max(0.0, (now - started).total_seconds())
            stalled = uptime is not None and uptime > stall_seconds
            stall_note = ("no DDL pass has completed" if uptime is None
                          else f"no DDL pass has completed in the {_span(uptime)} since the engine started")
    # A leftover only matters here when nothing will drop it: the builder is off (auth_failed/no_privilege returned
    # above with the count in `counts`; ok and standby drop it on a later pass).
    warn_leftover = leftover if state == "off" else 0
    if expired or warn_invalid or failing or stuck or stalled or warn_leftover:
        parts = ["router index set expired (no successful catalog read for three sweep periods; every statement "
                 "walks at the serving ef_search until a read succeeds)"] if expired else []
        parts += [f"{n} {what}" for n, what in ((warn_invalid, "invalid"), (failing, "failing")) if n]
        if warn_leftover:
            parts.append(f"{warn_leftover} reindex leftover (an invalid _ccnew/_ccold index a failed REINDEX "
                         "CONCURRENTLY left behind; the builder is off, so nothing drops it)")
        if stuck:
            parts.append(f"a DDL pass has been in flight for {_span(pass_age)}, more than "
                         f"{_span(_PCI_PASS_STUCK_SECONDS)} (hung, or a very long first pass)")
        if stalled:
            parts.append(f"{stall_note}, more than {_PCI_STALL_SECONDS_PERIODS} sweep periods of "
                         f"{_span(sweep_seconds)} plus {_span(_PCI_STALL_MARGIN_SECONDS)} (every leaf skipped, or the "
                         "pass task is not running)")
        return [HealthResult(
            label=label, ok=False, warn=True,
            detail=(f"{', '.join(parts)} ({counts}); builder_state {state}, {ddl_note}{off_note}"),
            fix_suggestions=[
                "Read the engine log for event=pci_sweep (the read half's counts, one line per sweep; "
                "event=pci_sweep_set_expired marks a frozen set) and the builder's own lines for the collection "
                "that fails; event=pci_reconcile_pass shows whether passes complete (ran_to_end, skipped_leaves, "
                "max_count_ms against the 30 s count timeout)",
                *(["A reindex leftover is dropped by the builder on its next pass: set NX_SEARCH_PCI=1, or "
                   "DROP INDEX CONCURRENTLY it by hand (it is the invalid pci_<hash>_ccnew/_ccold index)"]
                  if warn_leftover else []),
                "https://github.com/Hellblazer/nexus/blob/main/docs/rdr/rdr-227-per-collection-hnsw-above-the-router-threshold.md (Day 2 Operations)",
            ],
        )]
    if state == "standby":
        note = "; the invalid index may be a peer's build in flight" if peer_may_be_building else ""
        return [HealthResult(label=label, ok=True,
                             detail=f"{counts}; a peer engine holds the builder lock (standby){note}")]
    return [HealthResult(label=label, ok=True, detail=f"{counts}; builder_state {state}, {ddl_note}{off_note}")]
