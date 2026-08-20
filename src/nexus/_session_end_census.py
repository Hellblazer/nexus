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
module does is PROVABLY INVISIBLE on screen. The durable JSONL append
below is therefore the PRIMARY artifact, not a fallback -- readable
later via ``nx census capability --session <id>`` or a direct read of
the log.

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
import json
import pathlib
from typing import Any

#: Filename for the durable per-session capability census log, alongside
#: ``routing_log.jsonl`` -- same directory in the common case, but NOT the
#: same precedent: ``nexus.routing_stats.default_log_path`` hardcodes
#: ``Path.home()`` and only honours ``NX_ROUTING_LOG_PATH``, while this
#: log resolves through :func:`nexus.config.nexus_config_dir` and honours
#: ``NEXUS_CONFIG_DIR`` (the bead's explicit requirement) -- the two
#: diverge whenever ``NEXUS_CONFIG_DIR`` is set (sandboxes, tests).
_LOG_FILENAME = "capability_census.jsonl"


def capability_census_log_path() -> pathlib.Path:
    """Durable JSONL path for the per-session capability census.

    Resolves through :func:`nexus.config.nexus_config_dir` (honours
    ``NEXUS_CONFIG_DIR``) rather than hardcoding ``Path.home()`` --
    the bead is explicit that the config dir must be resolved properly.
    """
    from nexus.config import nexus_config_dir  # noqa: PLC0415 — deferred; only needed here

    return nexus_config_dir() / _LOG_FILENAME


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

    capabilities = {cap: result.total_calls(cap) for cap in CAPABILITIES}

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
    }


def write_session_capability_census(session_id: str | None = None) -> dict[str, Any] | None:
    """Append one capability-census record to the durable JSONL log.

    Returns the record written, or ``None`` when no session id resolves
    (nothing meaningful to census -- mirrors
    ``_print_service_tier_summary``'s own silent no-op in that case).

    Does NOT swallow exceptions raised while measuring or writing --
    that is the caller's job
    (``_session_end_launcher._write_capability_census``), which wraps
    this call and logs failures via structlog so a census bug can never
    break SessionEnd cleanup.
    """
    from nexus.census import default_project_dir  # noqa: PLC0415 — deferred; only needed here
    from nexus.session import resolve_active_session_id  # noqa: PLC0415 — deferred; only needed here

    sid = session_id or resolve_active_session_id()
    if not sid:
        return None

    project_dir = default_project_dir()
    record = build_capability_census_record(project_dir, sid)

    log_path = capability_census_log_path()
    log_path.parent.mkdir(parents=True, exist_ok=True)
    with log_path.open("a", encoding="utf-8") as fh:
        # code-review Important #2 (fix pass, 2026-08-20): ONE fh.write()
        # call with the complete line (payload + newline), matching the
        # codebase's own precedent for this exact append-log problem
        # (conexus/hooks/scripts/routing/_lib.py::log_routing_event).
        # Concurrent SessionEnd appenders from multiple Claude Code
        # sessions can hit this file near-simultaneously; two separate
        # write() calls relied on CPython's buffering happening to
        # coalesce them into one OS-level write, an implementation
        # detail rather than a guarantee against interleaved partial
        # lines from another process.
        fh.write(json.dumps(record, sort_keys=True) + "\n")
    return record
