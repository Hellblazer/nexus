# SPDX-License-Identifier: AGPL-3.0-or-later
"""``nx doctor``'s data-move rows (RDR-246 Day 2 Operations, nexus-f3yqd.2.17).

The engine's move worker reports each open or failed data move as a ``/v1/status`` ``boot_work`` entry whose id is
``data-move:<label>``. The shape has no state field, so ``what`` begins with the state:

* ``blocked:`` (the reason follows) and ``failed:`` (the failure follows): one red row per move, except a ``blocked:``
  entry whose ``reason_class`` is ``missing_prerequisite`` (the ``pg_background`` extension is not installed): that one
  waits for the install and an engine restart and nothing is wrong with the data, so it is a warning until the engine's
  retry cap (about four hours), when the engine drops the class and the row is red. The class is the engine's field,
  never read from the words; an engine that predates it sends none, and its blocked move stays red.
* A move listed in the status's ``data_moves_need_operator_moves`` (its worker has failed it transiently 20 times in a row,
  or hit a lock or statement timeout at ``on_complete`` 3 times in a row, which then stays latched until the move completes
  or its count resets; the engine decides, ``MoveRetryPolicy``) is a red
  row whatever state its entry shows, naming the label, the step and bound it missed (a bound only for a lock or statement
  timeout), its last attempt and its next retry (or that a retry is running now). The move keeps retrying; the row says a person should look. An engine without the field shows such a move as the plain
  ``blocked:`` row it always did.
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


def _old_bundle_pending() -> bool:
    """This install's extracted PostgreSQL bundle lacks pg_background (the doctor's PostgreSQL bundle row shows). Only
    then does the blocked-move hint send the user to ``nx upgrade`` for the bundle; with the bundle in place the
    advice would point at a fix already made (F33). Unknown reads as pending, which keeps the advice."""
    try:
        import nexus.config as _config  # noqa: PLC0415 - deferred, CLI startup cost
        from nexus.daemon.pg_bundle_refresh import bundle_lacks_pg_background  # noqa: PLC0415 - deferred, CLI startup cost

        return bundle_lacks_pg_background(_config.nexus_config_dir())
    except Exception:  # noqa: BLE001 - a hint's wording must never cost the doctor row
        return True


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


def _needs_operator(status: dict[str, Any]) -> dict[str, dict[str, Any]]:
    """The moves ``/v1/status`` lists as needing an operator, by label; empty when the engine predates the field."""
    listed = status.get("data_moves_need_operator_moves")
    if not isinstance(listed, list):
        return {}
    return {
        m["label"]: m for m in listed
        if isinstance(m, dict) and isinstance(m.get("label"), str) and m["label"]
    }


#: SQLSTATEs that mean a bound was missed: a lock timeout and a statement timeout. Any other SQLSTATE names no bound.
_TIMEOUT_SQLSTATES = ("55P03", "57014")


def _int(value: object) -> int | None:
    """``value`` when it is an int (a bool is not one), else ``None``."""
    return value if isinstance(value, int) and not isinstance(value, bool) else None


def _needs_operator_row(label: str, info: dict[str, Any], entry: dict[str, Any]) -> HealthResult:
    """The red row for a move in the needs-an-operator state: the label, the step and bound it missed, when it last ran and
    when it runs next (or that a retry is running now)."""
    retries, bound = _int(info.get("retries")), _int(info.get("bound_ms"))
    sqlstate = info.get("sqlstate") if isinstance(info.get("sqlstate"), str) else None
    step = info.get("step") if isinstance(info.get("step"), str) and info.get("step") else None
    builds = info.get("terminates_index_builds") is True
    parts = [f"needs an operator: {label} has failed {retries if retries is not None else 'many'} times in a row"]
    if step and sqlstate in _TIMEOUT_SQLSTATES and bound is not None:
        parts.append(f"step {step} missed its {bound} ms bound (SQLSTATE {sqlstate})")
    elif step and sqlstate:
        parts.append(f"step {step} failed with SQLSTATE {sqlstate}")
    elif step:
        parts.append(f"step {step} failed")
    elif sqlstate:
        parts.append(f"SQLSTATE {sqlstate}")
    last, nxt = info.get("last_attempt_at"), info.get("next_retry_at")
    parts.append(f"last attempt {last if isinstance(last, str) else 'unknown'}")
    if info.get("retry_in_progress") is True:
        parts.append("a retry is in progress now")
    else:
        parts.append(f"next retry {nxt if isinstance(nxt, str) else 'unknown'}")
    if builds:
        parts.append("each attempt ends in-flight index builds")
    reset = _RETRY_NOW.format(label=label)
    return HealthResult(
        label=f"Data move {label}", ok=False, detail="; ".join(parts) + _counts(entry),
        fix_suggestions=[
            "The engine keeps retrying this move by itself, and nothing committed is lost, but the retries are not "
            "getting anywhere. "
            + (
                "Each attempt re-runs the warm-up and ends the in-flight index builds (PCI, leaf, id-unique), so waiting "
                "costs those builds. " if builds else ""
            )
            + "The engine log has the page: event=data_move_needs_operator (label, step, SQLSTATE and the bound that "
            "step missed), and event=data_move_blocked has the full reason of each retry. Fix what it names (a lock "
            "holder, a bound too short for the table); the next retry time above says whether to wait or act",
            (
                "Resetting the retry count starts another attempt at once, and that attempt ends the in-flight index "
                "builds again, so fix the cause first (raise the bound, or let the builds finish); then: " + reset
                if builds else reset
            ),
        ],
    )


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
    paged = _needs_operator(status)
    listed = {e["id"][len(ID_PREFIX):] for e in moves}
    # a listed move whose entry is gone (it finished between the engine's two reads) is still the engine's page
    moves = moves + [{"id": ID_PREFIX + lbl, "what": "blocked: needs an operator"} for lbl in paged if lbl not in listed]
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
        if label in paged and state != "failed":
            rows.append(_needs_operator_row(label, paged[label], entry))
        elif state == "blocked" and entry.get("reason_class") == MISSING_PREREQUISITE:
            rows.append(HealthResult(
                label=row_label, ok=False, warn=True, detail=f"blocked: {text}{_counts(entry)}",
                fix_suggestions=[
                    "The move waits for the pg_background extension; nothing was changed and nothing is lost. The engine "
                    "looks for the extension only when it starts, so installing it is not enough: restart the engine "
                    "after. "
                    + (
                        "A local install's bundled PostgreSQL carries pg_background, but an install first set up on "
                        "conexus 8.2.0 or earlier keeps an older bundle without it: `nx upgrade` replaces that bundle and "
                        "restarts the engine (`nx doctor` has a PostgreSQL bundle row while it is pending). "
                        if _old_bundle_pending() else ""
                    )
                    + "A self-managed "
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
