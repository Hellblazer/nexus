# SPDX-License-Identifier: AGPL-3.0-or-later
"""``nx doctor``'s data-move rows (RDR-246 Day 2 Operations, nexus-f3yqd.2.17).

The engine's move worker reports each open or failed data move as a ``/v1/status`` ``boot_work`` entry whose id is
``data-move:<label>``. The shape has no state field, so ``what`` begins with the state:

* ``blocked:`` (the reason follows) and ``failed:`` (the failure follows): one red row per move, except a ``blocked:``
  entry whose ``reason_class`` is ``missing_prerequisite`` (the ``pg_background`` extension is not installed): that one
  waits for the install and an engine restart and nothing is wrong with the data, so it is a warning until the engine's
  retry cap (about four hours), when the engine drops the class and the row is red. The class is the engine's field,
  never read from the words; an engine that predates it sends none, and its blocked move stays red.
* ``stalled:``: a CALL that has completed no chunk for a long time; a warning, since a slow chunk looks the same.
* ``paused:``: an operator holds the move; a note, never red.
* ``working:``, ``queued:``, ``runs complete:``: no row of their own; the summary row counts them.

The id ``data-move-worker`` says the engine's move worker is not running: a red row, since every registered move then waits
forever. The id ``data-move-boot-work`` says the worker runs but nothing reports its moves: a red row, since a green summary
would hide whatever is blocked or failed. ``/v1/status`` is unauthenticated, so an entry carries the move's label, a class and an SQLSTATE and nothing a run, a
tenant or the server wrote; each row's hint says where the full text is (the engine log, and for a failed move
``nexus.data_moves.failure``).

Kept out of :mod:`nexus.health` (a file at its size cap). Stdlib and :class:`~nexus.health.HealthResult` only; the status
body is fetched by the caller, as for the other engine-status rows.
"""
from __future__ import annotations

from typing import Any

import structlog

from nexus.health import _ENGINE_STATUS_UNSET, HealthResult

_log = structlog.get_logger(__name__)

#: The engine's id prefix for a data move's ``boot_work`` entry (``MoveBootWork.ID_PREFIX``).
ID_PREFIX = "data-move:"
#: The id of the entry that says the move worker is not running (``MoveBootWork.WORKER_ID``).
WORKER_ID = "data-move-worker"
#: The id of the entry that says the worker's boot_work reporter is not running (``MoveBootWork.REPORTER_ID``).
REPORTER_ID = "data-move-boot-work"
#: The entries that are not moves, with the row each is shown as.
_ENGINE_STATE_ROWS: dict[str, tuple[str, str, str]] = {
    WORKER_ID: (
        "Data move worker", "the data-move worker is not running",
        "The engine log has event=data_move_worker_not_started with the cause; fix it and restart the engine. Until "
        "then no registered data move is worked",
    ),
    REPORTER_ID: (
        "Data move reporting", "the data-move boot_work reporter is not running",
        "The engine log has event=data_move_boot_work_not_started with the cause; fix it and restart the engine. "
        "Until then a blocked or failed data move is not reported here",
    ),
}

SUMMARY_LABEL = "Data moves"
#: ``reason_class`` of a ``blocked:`` entry whose move waits for the pg_background extension to be installed (``MoveReasons.MISSING_PREREQUISITE``).
MISSING_PREREQUISITE = "missing_prerequisite"

_RETRY_NOW = (
    "UPDATE nexus.data_moves SET transient_retries = 0, last_transient_at = NULL, last_transient = NULL "
    "WHERE label = '{label}'; (as nexus_admin) makes the next pass run at once"
)
_RESUME = "UPDATE nexus.data_moves SET paused_at = NULL, paused_by = NULL WHERE label = '{label}'; (as nexus_admin)"


def _entries(status: dict[str, Any]) -> list[dict[str, Any]] | None:
    """The data-move entries (moves and the worker's) of ``status['boot_work']``; ``None`` when the engine predates it."""
    work = status.get("boot_work")
    if not isinstance(work, list):
        return None
    return [
        e for e in work
        if isinstance(e, dict) and isinstance(e.get("id"), str)
        and (e["id"].startswith(ID_PREFIX) or e["id"] in _ENGINE_STATE_ROWS)
    ]


def _state(what: str) -> str:
    """The word before the first colon of ``what`` (``blocked``, ``failed``, ``paused``, ...), or ``''``."""
    head, sep, _ = what.partition(":")
    return head.strip().lower() if sep else ""


def _after_state(what: str) -> str:
    """``what`` without its state word and, when it repeats the label, the label: the text an operator reads."""
    _, _, rest = what.partition(":")
    return rest.strip()


def _counts(entry: dict[str, Any]) -> str:
    done, total = entry.get("done"), entry.get("total")
    if isinstance(done, int) and isinstance(total, int) and not isinstance(done, bool):
        return f" ({done:,} of {total:,} {entry.get('unit') or 'chunks'} done)"
    return ""


def check_data_moves(engine_status: object = _ENGINE_STATUS_UNSET) -> list[HealthResult]:
    """One red row per ``blocked:`` or ``failed:`` data move, a warning per ``stalled:`` one, a note per ``paused:`` one,
    and a summary row. Nothing for a move that is working.

    *engine_status* is the ``GET /v1/status`` body the doctor already fetched (``None`` when that failed); left unset,
    this fetches its own. Not applicable (ok) when the engine cannot be reached or predates ``boot_work``: a box with no
    engine to ask has no move to report.
    """
    status: dict[str, Any] | None
    if engine_status is _ENGINE_STATUS_UNSET:
        try:
            from nexus.db.http_engine_status import fetch_engine_status  # noqa: PLC0415 — deferred to keep CLI startup fast

            status = fetch_engine_status()
        except Exception as exc:  # noqa: BLE001 — best-effort: must not crash `nx doctor`
            _log.debug("doctor_data_moves_probe_failed", error=str(exc))
            status = None
    else:
        status = engine_status if isinstance(engine_status, dict) else None
    if status is None:
        return [HealthResult(
            label=SUMMARY_LABEL, ok=True, detail="not applicable: the engine's status endpoint could not be read",
        )]
    entries = _entries(status)
    moves = None if entries is None else [e for e in entries if e["id"] not in _ENGINE_STATE_ROWS]
    if moves is None:
        return [HealthResult(
            label=SUMMARY_LABEL, ok=True, detail="not applicable: this engine does not report its work (boot_work)",
        )]
    rows: list[HealthResult] = []
    for entry in entries or []:
        if entry["id"] in _ENGINE_STATE_ROWS:
            row_label, default, hint = _ENGINE_STATE_ROWS[entry["id"]]
            rows.append(HealthResult(
                label=row_label, ok=False, detail=_after_state(str(entry.get("what") or "")) or default,
                fix_suggestions=[hint],
            ))
    if not moves:
        if not rows:
            return [HealthResult(label=SUMMARY_LABEL, ok=True, detail="no data move is open or failed")]
        return rows
    tally: dict[str, int] = {}
    for entry in moves:
        label = entry["id"][len(ID_PREFIX):]
        what = str(entry.get("what") or "")
        state = _state(what)
        tally[state or "unknown"] = tally.get(state or "unknown", 0) + 1
        text = _after_state(what)
        row_label = f"Data move {label}"
        if state == "blocked" and entry.get("reason_class") == MISSING_PREREQUISITE:
            rows.append(HealthResult(
                label=row_label, ok=False, warn=True, detail=f"blocked: {text}{_counts(entry)}",
                fix_suggestions=[
                    "The move waits for the pg_background extension; nothing was changed and nothing is lost. The engine "
                    "looks for the extension only when it starts, so installing it is not enough: restart the engine "
                    "after. A local install's bundled PostgreSQL carries pg_background, but an install first set up on "
                    "conexus 8.2.0 or earlier keeps an older bundle without it: `nx upgrade` replaces that bundle and "
                    "restarts the engine (`nx doctor` has a PostgreSQL bundle row while it is pending). A self-managed "
                    "PostgreSQL needs pg_background installed, then the engine restarted. If the move is still blocked after about "
                    "four hours of retries the engine drops the class and this row turns red (the full reason is in the "
                    "engine log, event=data_move_blocked)",
                ],
            ))
        elif state == "blocked":
            rows.append(HealthResult(
                label=row_label, ok=False, detail=f"blocked: {text}{_counts(entry)}",
                fix_suggestions=[
                    "The engine retries a blocked move by itself; fix what the reason names. The status carries no run, "
                    "tenant or server text: the full reason is in the engine log (event=data_move_blocked)",
                    _RETRY_NOW.format(label=label),
                ],
            ))
        elif state == "failed":
            rows.append(HealthResult(
                label=row_label, ok=False, detail=f"failed: {text}{_counts(entry)}",
                fix_suggestions=[
                    "A failed move is not retried and the chunks it committed stay. The full failure is "
                    f"`SELECT failure FROM nexus.data_moves WHERE label = '{label}'` (as nexus_admin) and the engine log "
                    "(event=data_move_failed). Fix the cause, then register a "
                    f"move whose INSERT names '{label}' in supersedes; that closes this one",
                ],
            ))
        elif state == "stalled":
            rows.append(HealthResult(
                label=row_label, ok=False, warn=True, detail=f"stalled: {text}{_counts(entry)}",
                fix_suggestions=[
                    "A slow chunk looks the same. The CALL's backend pid is in the engine log (event=data_move_call); "
                    "if it is idle on a dead connection, end it as nexus_admin with pg_cancel_backend(<pid>); the "
                    "worker retries the run from its last committed batch, and nothing is lost. The bound is "
                    "NX_DATA_MOVE_STALL_MINUTES (default 15)",
                ],
            ))
        elif state == "paused":
            rows.append(HealthResult(
                label=row_label, ok=True, detail=f"paused: {text}",
                fix_suggestions=[f"To resume: {_RESUME.format(label=label)}"],
            ))
    summary = ", ".join(f"{n} {state}" for state, n in sorted(tally.items()))
    rows.append(HealthResult(label=SUMMARY_LABEL, ok=True, detail=f"{len(moves)} open or failed: {summary}"))
    return rows
