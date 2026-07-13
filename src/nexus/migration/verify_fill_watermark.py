# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (c) 2026 Hal Hildebrand. All rights reserved.
"""nexus-te885.10 part 2: the verify-fill rowid watermark for the four
count-parity-UNSOUND telemetry tables.

``relevance_log`` / ``search_telemetry`` / ``tier_writes`` / ``frecency``
have no sound count-parity gate: ``DO NOTHING`` collapses source duplicates
(``target < source`` is ambiguous — collapse vs hole) and post-migration
live writes land target-side directly (``target > source`` proves nothing).
Their cheap no-op gate is a per-``(service_url, table)`` SOURCE-ROWID
watermark: after a breaker-clean fill verified every source row up to rowid
N present, the next run probes only rows with ``rowid > N`` — the frozen
post-cutover source cannot grow rows below it.

SOUNDNESS CONDITIONS (all load-bearing):

- **Trust gate**: a stored watermark is honoured ONLY when the engine
  returns a live row count for the table (proves a nexus-te885.10-era
  engine) AND that count is >= the count recorded when the watermark was
  written. A LOWER count means target rows were deleted (e.g. a rollback)
  — the watermark is invalidated and the full probe runs. On a
  pre-whitelist engine the count is absent, so the watermark is never
  trusted: fail-safe degradation to today's full-probe behavior.
- **Advance gate**: written only after a fill pass whose result status is
  ``parity``/``filled`` (never after a breaker abort), and only when a
  post-fill engine count is available to record.
- **Target-delete freedom**: a table qualifies ONLY while nothing deletes
  its target rows outside a full rollback — a rolling TTL sweep concurrent
  with live inserts keeps the count non-decreasing and blinds the trust
  gate. That is why ``relevance_log`` is excluded (see above); re-adding
  any table requires re-verifying this property FIRST.
- **Source invariants**: the SQLite source is frozen post-cutover and these
  tables are delete-free source-side, so rowids are stable and append-only.
  ``frecency`` mutates content in place, but verify-fill checks identity
  PRESENCE only, so a rowid watermark stays sound for it.
- Interaction with the ``telemetry_etl.read_rows_for_fill`` retention
  INVARIANT: the watermark narrows which SOURCE rows are probed; it cannot
  resurrect retention-pruned target rows any more than the full probe can.
"""
from __future__ import annotations

import json
import os
import tempfile
from pathlib import Path
from typing import Any

import structlog

_log = structlog.get_logger(__name__)

#: SQLite table -> PG relation, for the watermark's invalidation-count reads
#: ONLY. Deliberately DISJOINT from ``orchestrator._VERIFY_TABLES`` — putting
#: these there would let the outer loop skip them on an UNSOUND count
#: parity (see module docstring).
#:
#: ``relevance_log`` participates via the RETENTION MARKER (nexus-24p05): its
#: target-side 90-day TTL sweep (``expire_relevance_log``, fired by the
#: default-on SessionEnd hook via ``T2Database.expire()``) publishes a
#: monotonic cumulative-deletes counter. The sweep deletes ONLY rows older
#: than the retention horizon, and the fill probes ONLY rows inside it
#: (``RETENTION_HORIZON_TABLES``) — provably disjoint domains — so sweep
#: activity never invalidates fresh-row soundness; the marker's job is
#: ROLLBACK detection (a fresh schema resets it below the recorded value).
WATERMARK_TABLES: dict[str, str] = {
    "relevance_log": "nexus.relevance_log",
    "search_telemetry": "nexus.search_telemetry",
    "tier_writes": "nexus.tier_writes",
    "frecency": "nexus.frecency",
}

#: Tables whose watermark trust ADDITIONALLY requires the retention marker
#: (``marker_now >= recorded`` = no rollback). A missing marker read (old
#: engine, transport failure) distrusts — fail-safe full probe.
RETENTION_MARKED_TABLES: frozenset[str] = frozenset({"relevance_log"})

#: Days of source rows the fill PROBES for retention-swept tables — matching
#: the sweep's own horizon (``Telemetry.expire_relevance_log`` default). Rows
#: older than this are the sweep's legitimate domain: probing them would
#: RESURRECT expired rows (the pre-existing exposure nexus-24p05 closed).
RETENTION_HORIZON_TABLES: dict[str, int] = {"relevance_log": 90}


def _watermark_file() -> Path:
    from nexus.config import nexus_config_dir  # noqa: PLC0415 — deferred, avoids import cycle at module load

    return nexus_config_dir() / "migration" / "verify_fill_watermarks.json"


def _load_all() -> dict[str, Any]:
    try:
        data = json.loads(_watermark_file().read_text())
    except (OSError, ValueError):
        return {}
    # "Never raises" contract: syntactically-valid-but-non-dict JSON (manual
    # edit, format drift) must read as empty, not AttributeError downstream.
    return data if isinstance(data, dict) else {}


def _key(service_url: str, table: str) -> str:
    return f"{service_url}|{table}"


def usable_min_rowid(
    service_url: str, table: str, engine_count: int | None,
    retention_marker: int | None = None,
) -> int:
    """The rowid floor the probe may start ABOVE, or 0 for a full probe.

    Returns 0 (no restriction) unless every trust condition holds — see the
    module docstring. For ``RETENTION_MARKED_TABLES``, *retention_marker*
    (the live cumulative-deletes counter) must additionally be >= the value
    recorded at advance time: a LOWER value means a fresh schema (rollback)
    — distrust; ordinary sweep bumps (higher) are fine (disjoint domains).
    Never raises.
    """
    if not service_url or engine_count is None:
        return 0
    mark = _load_all().get(_key(service_url, table))
    if not isinstance(mark, dict):
        return 0
    recorded = mark.get("target_count")
    max_rowid = mark.get("max_rowid")
    if not isinstance(recorded, int) or not isinstance(max_rowid, int):
        return 0
    if table in RETENTION_MARKED_TABLES:
        recorded_marker = mark.get("retention_marker")
        if (
            retention_marker is None
            or not isinstance(recorded_marker, int)
            or retention_marker < recorded_marker
        ):
            _log.warning(
                "verify_fill.watermark_distrusted_retention_marker",
                table=table,
                recorded_marker=mark.get("retention_marker"),
                live_marker=retention_marker,
                note="marker absent or below recorded (old engine / rollback) "
                     "— full probe runs",
            )
            return 0
    if engine_count < recorded:
        _log.warning(
            "verify_fill.watermark_invalidated_target_shrank",
            table=table,
            recorded_count=recorded,
            engine_count=engine_count,
            note="target rows were deleted (rollback?) — full probe runs",
        )
        return 0
    return max_rowid


def advance_watermark(
    service_url: str, table: str, max_rowid: int, target_count: int,
    retention_marker: int | None = None,
) -> None:
    """Record that every source row up to *max_rowid* is verified present.

    Atomic write (tmp + rename); best-effort — a persist failure only costs
    the next run its shortcut, never correctness.
    """
    if not service_url:
        return
    import fcntl  # noqa: PLC0415 — POSIX-only (darwin/linux, the supported platforms), deferred

    path = _watermark_file()
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        # flock around load+write: two concurrent verify-fill runs would
        # otherwise lose each other's OTHER-table updates (read-modify-write
        # race; safe — a lost update only costs the next run its shortcut —
        # but silent, review c0e4493e finding 4).
        lock_path = path.parent / ".wm.lock"
        with open(lock_path, "w") as lock_fh:
            fcntl.flock(lock_fh, fcntl.LOCK_EX)
            marks = _load_all()
            mark: dict[str, Any] = {
                "max_rowid": max_rowid,
                "target_count": target_count,
            }
            if table in RETENTION_MARKED_TABLES:
                if retention_marker is None:
                    return  # cannot record a rollback baseline -> no advance
                mark["retention_marker"] = retention_marker
            marks[_key(service_url, table)] = mark
            fd, tmp = tempfile.mkstemp(dir=path.parent, prefix=".wm_")
            try:
                with os.fdopen(fd, "w") as fh:
                    json.dump(marks, fh, indent=2)
                os.replace(tmp, path)
            except BaseException:
                Path(tmp).unlink(missing_ok=True)
                raise
    except OSError:
        _log.warning("verify_fill.watermark_persist_failed", table=table, exc_info=True)
