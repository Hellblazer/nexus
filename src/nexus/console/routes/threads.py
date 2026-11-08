# SPDX-License-Identifier: AGPL-3.0-or-later
"""Panel 1: Threads -- every Claude session's open threads on one board.

Reads two sources: T2 project ``threads`` (one entry per session, see
``nexus.console.threads`` for the format) and the beads labelled ``needs-sam``
(``bd list``). Both readers, and the clock, are seams on ``app.state`` so a
test injects a stand-in without patching ``subprocess`` or T2 globally.
"""
from __future__ import annotations

import os
import subprocess
from collections.abc import Callable
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import structlog
from fastapi import APIRouter, Request

from nexus.bounded_subprocess import run_bounded
from nexus.console.threads import (
    BeadsResult,
    build_board,
    format_age,
    has_value,
    headline,
    merge_asks,
    parse_beads_json,
)

_log = structlog.get_logger(__name__)

router = APIRouter(tags=["threads"])

THREADS_PROJECT = "threads"
ASKS_PROJECT = "asks"
BD_TIMEOUT_SECONDS = 10.0
_BD_ARGV = ["bd", "list", "--label", "needs-sam", "--status", "open,in_progress,blocked", "--limit", "0", "--json"]

#: bd's own error text (compared lower case) when the working directory has no beads database.
_NO_BEADS_DATABASE = "no beads database found"

BdRunner = Callable[[], BeadsResult]
ThreadsReader = Callable[[], list[dict[str, Any]]]
AnswersReader = Callable[[], list[dict[str, Any]]]
Clock = Callable[[], datetime]


def _has_beads_database() -> bool:
    """True when the beads dir variable is set or a ``.beads`` directory sits at or above the working directory."""
    if os.environ.get("BEADS_DIR"):
        return True
    cwd = Path.cwd()
    return any((d / ".beads").is_dir() for d in (cwd, *cwd.parents))


def run_bd_needs_sam(timeout: float = BD_TIMEOUT_SECONDS) -> BeadsResult:
    """Live beads (open, in progress or blocked) labelled ``needs-sam``, from the repo the console started in.

    Never raises. The source is ``skipped`` (a user without beads has none to read) only when there is no beads
    database for the working directory: ``bd`` is missing from PATH and no ``.beads`` directory sits at or above
    it, or ``bd`` exits with its "no beads database found" error. A missing ``bd`` where a ``.beads`` directory
    exists, a broken ``bd``, a timeout, any other non-zero exit or unparseable output comes back as a one-line
    ``error``, so a real failure never reads as "nothing needs you".
    """
    try:
        proc = run_bounded(_BD_ARGV, timeout=timeout)
    except FileNotFoundError:
        if _has_beads_database():
            return BeadsResult((), "bd is not on PATH")
        return BeadsResult((), skipped="bd is not installed")
    except subprocess.TimeoutExpired:
        return BeadsResult((), f"bd list timed out after {timeout:g}s")
    except OSError as exc:
        return BeadsResult((), f"bd could not run: {exc}")
    if proc.returncode != 0:
        if _NO_BEADS_DATABASE in f"{proc.stderr}\n{proc.stdout}".lower():
            return BeadsResult((), skipped="there is no beads database here")
        detail = (proc.stderr or proc.stdout).strip().splitlines()
        return BeadsResult((), f"bd list failed (exit {proc.returncode})" + (f": {detail[0][:120]}" if detail else ""))
    try:
        return BeadsResult(parse_beads_json(proc.stdout))
    except ValueError as exc:
        return BeadsResult((), str(exc))


def read_threads_entries() -> list[dict[str, Any]]:
    """Every entry in T2 project ``threads``, through the CLI's own T2 handle."""
    from nexus.commands._helpers import t2_handle  # noqa: PLC0415 -- deferred: keeps console import light

    with t2_handle() as db:
        return list(db.memory.get_all(project=THREADS_PROJECT))


def read_answer_records() -> list[dict[str, Any]]:
    """Every row in T2 project ``asks`` (answer records and batches; ``merge_asks`` reads the ``sent`` answers)."""
    from nexus.commands._helpers import t2_handle  # noqa: PLC0415 -- deferred: keeps console import light

    with t2_handle() as db:
        return list(db.memory.get_all(project=ASKS_PROJECT))


def _collect(request: Request) -> dict[str, Any]:
    state = request.app.state
    bd_runner: BdRunner = getattr(state, "bd_runner", run_bd_needs_sam)
    reader: ThreadsReader = getattr(state, "threads_reader", read_threads_entries)

    t2_error = ""
    entries: list[dict[str, Any]] = []
    try:
        entries = reader()
    except Exception as exc:  # noqa: BLE001 -- the panel degrades to one line naming the failure
        _log.warning("console_threads_t2_failed", error=str(exc))
        t2_error = f"T2 project '{THREADS_PROJECT}' could not be read: {str(exc)[:200]}"
    clock: Clock = getattr(state, "clock", lambda: datetime.now(timezone.utc))
    now = clock()
    board = build_board(entries, now)
    beads = bd_runner()
    records: list[dict[str, Any]] = []
    if board.structured:  # the answers are only needed to retire a structured ask
        answers: AnswersReader = getattr(state, "answers_reader", read_answer_records)
        try:
            records = answers()
        except Exception as exc:  # noqa: BLE001 -- unread answers leave every ask open: over-counts, never hides
            _log.warning("console_threads_answers_failed", error=str(exc))
    asks = merge_asks(board.needs_you, beads.beads, board.structured, records)
    # False when a source failed or an entry could not be read: "nothing needs you" would then be a guess.
    checked_everything = beads.ok and not t2_error and not board.unreadable
    return {
        "board": board,
        "beads": beads,
        "t2_error": t2_error,
        "asks": asks,
        "headline": headline(board, asks, checked_everything),
        "as_of": now.astimezone().strftime("%H:%M"),
        "checked_everything": checked_everything,
        "has_value": has_value,
        "age_of": lambda dt: format_age(now - dt) if dt else "",
    }


@router.get("/threads")
def threads_index(request: Request):
    templates = request.app.state.templates
    return templates.TemplateResponse(
        request, "threads/index.html", {"active_panel": "threads", **_collect(request)},
    )


@router.get("/threads/refresh")
def threads_refresh(request: Request):
    """HTMX partial: the board without the page chrome."""
    templates = request.app.state.templates
    return templates.TemplateResponse(request, "threads/_board.html", _collect(request))
