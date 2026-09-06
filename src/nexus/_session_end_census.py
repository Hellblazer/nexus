# SPDX-License-Identifier: AGPL-3.0-or-later
"""Per-session capability census append at SessionEnd (nexus-h33x8.3).

Delivery is Tier B: this module is imported only from the fully
detached grandchild path in :mod:`nexus._session_end_launcher`
(``_run_session_end_synchronously`` -> ``_write_capability_census``),
never at that launcher's top level -- the pre-fork budget invariant
documented there is untouched by this module's existence.

VISIBILITY IS SETTLED BY SOURCE, not experiment (bead nexus-h33x8.3,
2026-08-01 comment): the grandchild's stdio is redirected to
``/dev/null`` before this module is ever imported, so anything this
module does is PROVABLY INVISIBLE on screen. The durable write below is
therefore the PRIMARY artifact, not a fallback -- readable later via
``nx census capability --session <id>`` (nexus-gjv9b PART 1: this reads
the ``capability_census`` engine table now, not the JSONL log below).

WRITER SWAP (nexus-gjv9b PART 1, Sam directive 2026-08-20): the durable
write target is the PG-backed ``capability_census`` engine table -- see
:func:`write_session_capability_census`'s own docstring for the full
design decision (metered drop on service-down, never a JSONL fallback).
The JSONL log this module wrote before the swap (``capability_census.jsonl``
plus its rotation and tail-dedup machinery) was deleted at PART 3
(2026-09-05): this code only ever ships to installs that no longer write
that file, so nothing here could protect a pre-swap install anyway;
those keep their own copy of the rotation.

A parent-side (post-fork) VISIBLE line was considered and rejected on
cost grounds, not skipped for convenience: a full per-session capability
census re-walks every transcript file for the session (main +
subagent-*.jsonl), and that cost is DATA-DEPENDENT and effectively
unbounded (measured 2026-08-20 against this repo's own real
``~/.claude/projects/-Users-hal-hildebrand-git-nexus`` transcripts:
~0.21s for a 70-subagent-file/6k-tool-call session, ~0.50s for an
88MB/62-file session -- both comfortably inside the SessionEnd hook's
10s budget today, but the cost scales with session size with no cap,
unlike ``_print_service_tier_summary``'s single hard-2.0s-timeout HTTP
read). Re-running that walk a second time in the parent, synchronously,
purely for an on-screen nicety, was judged not "cheap" in the sense the
bead requires -- so per the bead's own escape hatch ("if it can't be
done cheaply, skip the visible line entirely -- JSONL alone satisfies
the bead"), there is no visible line. All measurement happens exactly
once, in the grandchild, off the hook-timeout critical path entirely.

Reuses nexus-h33x8.1's shipped transcript-parsing machinery
(:mod:`nexus.census`) rather than re-implementing it -- see
``census_corpus``/``census_session_dispatches``.

"""
from __future__ import annotations

import datetime
import pathlib
from typing import Any

def _now_iso() -> str:
    return datetime.datetime.now(datetime.timezone.utc).isoformat()


def _blindspot_record(session_id: str, reason: str) -> dict[str, Any]:
    """A BLINDSPOT record: explicit marker, no zeroed counts.

    Verification 3 (nexus-h33x8.3): a session whose transcript is
    unreadable/absent at end-of-session must never be reported as a
    clean zero -- that would be indistinguishable from a session that
    genuinely used nothing.
    """
    return {
        "session_id": session_id,
        "timestamp": _now_iso(),
        "blindspot": True,
        "unmeasurable_reason": reason,
    }


def _zero_record(session_id: str) -> dict[str, Any]:
    """A real, measured all-zero record -- not a blindspot.

    code-review Important #1 (fix pass, 2026-08-20): ``census_session``'s
    own precedence chain (``nexus/census.py``) treats "readable, parsed
    cleanly, but genuinely zero tool_use blocks of any kind" as
    ``UNMEASURABLE_NO_TOOL_USE`` -- a MEASUREMENT-machinery label, not a
    measurement FAILURE. A session that truly used nothing is exactly
    the ``skills=0 nx_answer=0 ...`` line the bead's own example shows;
    collapsing it into the same blindspot bucket as an unreadable or
    missing transcript would make the zero indistinguishable from a
    measurement gap, defeating the whole point of the BLINDSPOT marker.
    Built directly from ``CAPABILITIES`` rather than via
    ``CorpusCensus.total_calls`` -- the session's own orchestrator/
    subagent dicts ARE empty in this branch by construction, but stating
    the zeros explicitly here is not incidental on that.
    """
    from nexus.census import CAPABILITIES  # noqa: PLC0415 — deferred; only needed here

    return {
        "session_id": session_id,
        "timestamp": _now_iso(),
        "blindspot": False,
        "capabilities": dict.fromkeys(CAPABILITIES, 0),
        "dispatches": 0,
        "total_calls": 0,
        # nexus-gjv9b PART 3 prerequisite: a measured zero is a real
        # measurement of BOTH scopes, not merely the merged total -- the
        # session's own orchestrator/subagent dicts are empty by
        # construction in this branch, so the split is zero at every
        # capability too, same as the merged view above.
        "capabilities_orchestrator": dict.fromkeys(CAPABILITIES, 0),
        "capabilities_subagent": dict.fromkeys(CAPABILITIES, 0),
    }


def build_capability_census_record(
    project_dir: pathlib.Path, session_id: str,
) -> dict[str, Any]:
    """Build one census record for ``session_id``.

    Reuses :func:`nexus.census.census_corpus` (scoped to the single
    session) for the roll-up math and BLINDSPOT/measurability
    determination, and :func:`nexus.census.census_session_dispatches`
    for the recognized-dispatch count -- both nexus-h33x8.1/.2 machinery,
    not reimplemented here.

    Counts only, never verdicts: this record carries per-capability
    call counts and a dispatch count, nothing that says "you should
    have used X" (bead nexus-h33x8.3).
    """
    from nexus.census import (  # noqa: PLC0415 — deferred; only needed here
        CAPABILITIES,
        UNMEASURABLE_NO_TOOL_USE,
        census_corpus,
        census_session_dispatches,
    )

    result = census_corpus(project_dir, session=session_id)

    if result.scope_error:
        return _blindspot_record(session_id, result.scope_error)
    if not result.sessions:
        reason = (
            result.unmeasurable[0].unmeasurable_reason
            if result.unmeasurable and result.unmeasurable[0].unmeasurable_reason
            else "unknown"
        )
        if reason == UNMEASURABLE_NO_TOOL_USE:
            return _zero_record(session_id)
        return _blindspot_record(session_id, reason)

    # nexus-gjv9b PART 3 prerequisite: the orchestrator/subagent-split
    # dimension the transcript-walk reader already carries (census.py's
    # own module docstring calls this split "load-bearing, not
    # cosmetic"). ``result`` is scoped to exactly this one session
    # (``census_corpus(project_dir, session=session_id)`` above), so
    # ``orchestrator_calls``/``subagent_calls`` -- which sum over
    # ``result.sessions`` -- reduce to this session's own counts.
    #
    # SINGLE SOURCE OF TRUTH (critique-nexus-gjv9b-part3-9695b260f
    # Significant 4): the flat ``capabilities`` total is DERIVED from the
    # split below (``orchestrator[cap] + subagent[cap]``), one reduction
    # per capability instead of the two independent ones a prior version
    # ran (``result.total_calls(cap)`` is itself defined as exactly this
    # sum internally -- computing it a second, separate way here bought
    # nothing but a redundant walk over the same in-memory counts, and a
    # second call site that could silently drift out of sync with the
    # split it is supposed to describe). The engine's own
    # ``TelemetryHandler.handleCapabilityCensusRecord`` re-validates this
    # same invariant server-side (400 on a caller that sends a flat total
    # inconsistent with its own split) -- this derivation is what keeps
    # THIS writer's requests from ever tripping that check, not merely an
    # optimization.
    capabilities_orchestrator = {cap: result.orchestrator_calls(cap) for cap in CAPABILITIES}
    capabilities_subagent = {cap: result.subagent_calls(cap) for cap in CAPABILITIES}
    capabilities = {
        cap: capabilities_orchestrator[cap] + capabilities_subagent[cap] for cap in CAPABILITIES
    }

    # code-review suggestion #2 (fix pass, 2026-08-20): the ``.measurable``
    # guard this used to carry was dead code, not defensive -- provably so.
    # census_session_dispatches's OWN precedence chain sets
    # unmeasurable_reason=None the moment total_tool_use_blocks > 0, and
    # every unmeasurable branch (MISSING/UNREADABLE/EMPTY/UNPARSEABLE/
    # NO_TOOL_USE) implies zero tool_use blocks were successfully parsed,
    # which implies zero Agent-dispatch blocks among them -- so
    # ``dispatch_census.dispatches`` is already ``[]`` in exactly the cases
    # the guard existed to catch. Removed rather than kept as inert
    # ceremony that looked like real degradation handling.
    dispatch_census = census_session_dispatches(project_dir, session_id)
    dispatches = len(dispatch_census.dispatches)

    return {
        "session_id": session_id,
        "timestamp": _now_iso(),
        "blindspot": False,
        "capabilities": capabilities,
        "dispatches": dispatches,
        "total_calls": sum(capabilities.values()),
        "capabilities_orchestrator": capabilities_orchestrator,
        "capabilities_subagent": capabilities_subagent,
    }


def write_session_capability_census(session_id: str | None = None) -> dict[str, Any] | None:
    """Upsert one session's capability census to the engine (nexus-gjv9b
    PART 1 writer swap — Sam directive 2026-08-20: leverage PG instead of
    flat-file gymnastics).

    Returns the record built, or ``None`` when no session id resolves
    (nothing meaningful to census -- mirrors
    ``_print_service_tier_summary``'s own silent no-op in that case).
    The RETURN VALUE reflects the census MEASUREMENT regardless of whether
    the write below actually landed -- a metered-drop degradation is not a
    measurement failure, and the caller (``_session_end_launcher
    ._write_capability_census``) logs the record either way.

    DESIGN DECISION (this bead's own "decide at design time" instruction):
    service-down degrades to a METERED DROP
    (:func:`nexus.dropped_writes.record_drop`), never a JSONL fallback
    append. The table's UPSERT-on-``(tenant_id, session_id)`` semantics
    already collapse SessionEnd's many re-fires per session for free (no
    client-side dedup-by-tail-read needed any more -- see
    :func:`build_capability_census_record`'s docstring for the historical
    duplicate-row problem this table structurally avoids), so there is no
    reduced-but-still-useful JSONL half-measure to fall back to that would
    not immediately re-introduce that exact problem. The JSONL rotation and
    tail-dedup machinery was deleted at PART 3 (2026-09-05).

    BLINDSPOT-when-unmeasurable semantics survive unchanged: they are a
    property of :func:`build_capability_census_record`'s MEASUREMENT
    logic (independent of transport), not of how the record is written.

    Does NOT swallow exceptions raised while MEASURING -- that remains
    the caller's job (``_session_end_launcher._write_capability_census``),
    which wraps this call and logs failures via structlog so a census bug
    can never break SessionEnd cleanup. The WRITE half below, by
    contrast, is entirely self-contained best-effort: a write failure
    never propagates past this function.
    """
    from nexus.census import default_project_dir  # noqa: PLC0415 — deferred; only needed here
    from nexus.session import resolve_active_session_id  # noqa: PLC0415 — deferred; only needed here

    sid = session_id or resolve_active_session_id()
    if not sid:
        return None

    project_dir = default_project_dir()
    record = build_capability_census_record(project_dir, sid)
    _post_capability_census(record)
    return record


def _post_capability_census(record: dict[str, Any]) -> None:
    """Best-effort HTTP write of *record* — single-attempt, hard 2s
    timeout (matches ``_print_service_tier_summary``'s own precedent;
    see :meth:`HttpTelemetryStore.record_capability_census`'s docstring
    for why this bypasses the mixin's gateway-retry/re-resolve
    composition). ANY failure — resolution, auth, network, an old engine
    that 404s the route — degrades to a metered drop AND a structlog
    warning, then returns; never raises, never retries.

    Review fix (nexus-gjv9b fold-in): a prior version metered the drop
    but never logged it — ``write_session_capability_census``'s own
    docstring claimed "the caller... logs the record either way", but
    ``_session_end_launcher._write_capability_census``'s ``except
    Exception`` only fires when THIS function raises, which it never
    does for a write failure (only for a measurement failure upstream).
    A dropped write was therefore invisible in the logs — countable via
    ``nx doctor``'s drop meter, but undiagnosable from a log grep. The
    warning below closes that gap directly at the point of failure,
    independent of what any caller's own exception handling does.
    """
    try:
        from nexus.db.data_token import get_data_token_manager  # noqa: PLC0415 — deferred; only needed here
        from nexus.db.service_endpoint import resolve_service_endpoint  # noqa: PLC0415 — deferred; only needed here
        from nexus.db.t2._refreshable_client import DEFAULT_TENANT  # noqa: PLC0415 — deferred; only needed here
        from nexus.db.t2.http_telemetry_store import HttpTelemetryStore  # noqa: PLC0415 — deferred; only needed here

        base_url, token = resolve_service_endpoint()
        data_token = get_data_token_manager().bearer_for(base_url, DEFAULT_TENANT)
        if data_token is not None:
            token = data_token
        store = HttpTelemetryStore(base_url=base_url, _token=token)
        try:
            store.record_capability_census(
                session_id=record["session_id"],
                ts=record["timestamp"],
                blindspot=record["blindspot"],
                unmeasurable_reason=record.get("unmeasurable_reason"),
                capabilities=record.get("capabilities"),
                dispatches=record.get("dispatches"),
                total_calls=record.get("total_calls"),
                capabilities_orchestrator=record.get("capabilities_orchestrator"),
                capabilities_subagent=record.get("capabilities_subagent"),
                timeout=2.0,
            )
        finally:
            try:
                store.close()
            except Exception:  # noqa: BLE001 — best-effort close; never mask the write outcome
                pass
    except Exception as exc:  # noqa: BLE001 — best-effort write; degrade to a metered drop, never raise
        try:
            import structlog  # noqa: PLC0415 — deferred; only needed on this rare failure path

            structlog.get_logger(__name__).warning(
                "capability_census_write_dropped",
                session_id=record.get("session_id", ""),
                # nexus-gjv9b review fold-in round 3, code-review item 3:
                # a dropped BLINDSPOT row (the transcript was itself
                # unmeasurable) is a materially different diagnosis than
                # a dropped measured row -- surface it in the same log
                # line rather than making a reader cross-reference the
                # record separately.
                blindspot=record.get("blindspot", False),
                error=str(exc),
            )
        except Exception:  # noqa: BLE001 — even the warning log is best-effort
            pass
        try:
            from nexus.dropped_writes import record_drop  # noqa: PLC0415 — deferred; only needed on this rare failure path

            record_drop(
                hook="capability_census", collection="", rows=1, error=str(exc)
            )
        except Exception:  # noqa: BLE001 — the meter is itself best-effort
            pass

