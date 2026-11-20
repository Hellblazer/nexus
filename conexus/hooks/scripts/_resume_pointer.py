# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (c) 2026 Hal Hildebrand. All rights reserved.
"""RDR-253 Step 6 part 2: the identity-scratch resume pointer the hooks leave behind.

One line, tagged ``resume-pointer``, in the identity T1 scope (``nx scratch --scope identity``), which crosses ``/clear``
and restart where the session's own T1 scope does not. SessionStart on ``clear`` prints the newest one
(``nexus.hooks.resume_line``). It holds the current thread (chosen below), that thread's ``state:`` and ``waiting_on:``
lines and the worktree, and nothing the board does not already hold: the board stays authoritative, this is only the
pointer that finds it again. ``compact_checkpoint.py`` writes it at PreCompact and at SessionEnd with reason ``clear``.

* **Which thread.** A seat has several, in no order of recency. The one whose ``worktree:`` equals the checkpoint's
  worktree; else the one whose ``ref:`` names an in-progress bead; else the first. The in-progress bead row stands in for
  a missing ``state:`` only when a ``ref:`` line on the board names that bead (the continuation lists every session's
  beads), and is then labelled ``bead:``, not ``state:``.
* **Put before delete.** List, put the new line, then delete the earlier pointers from that listing, so a failed put
  leaves the last good pointer. A pointer is an entry whose tag is exactly ``resume-pointer`` AND whose content opens
  ``resume <stamp> | ``; no other entry is ever deleted. The line carries its own UTC stamp, so the reader picks the
  newest when a delete failed and two remain.
* **Fails safe.** A list or put that errors or times out (no identity, a 401, a stalled engine, a missing ``nx``) writes
  nothing and logs ONE line ending in the error tail; there is no fallback write (the session scope is new after
  ``/clear``, so nothing would read it, and it is the bus the subagents share). The whole write holds the budget the
  caller passes (at most ``BUDGET_S``), every call is bounded by what is left of it, and nothing here raises.
* **Caps.** The identity scope's caps (2 KB an entry, 20 entries, a reserved slot for this entry) are held by the
  client's identity-scope put (``HttpScratchStore``), not the engine; the line stays well under 2 KB.

Standard library only: a plugin hook script runs where the ``nexus`` package may not import. The caller passes the
``nx`` runner (``compact_checkpoint._run_nx``); every call here is spelled ``run_nx([literal, ...], ...)`` so the
compaction-outcome harness's call scan reads it.
"""
from __future__ import annotations

import re
import time
from collections.abc import Callable
from datetime import datetime, timezone

TAG = "resume-pointer"
#: Bytes the stored line is clamped to. The reader prints a shorter line again (``nexus.hooks.resume_line``).
MAX_BYTES = 1024
#: Whole-write budget and the longest any one ``nx`` call may take.
BUDGET_S = 6.0
CALL_TIMEOUT_S = 3.0
_MIN_CALL_S = 0.5
_THREAD_CHARS = 80
_STATE_CHARS = 160
_WAITING_CHARS = 100
_WORKTREE_CHARS = 120
_HEAD = re.compile(r"^resume [0-9T:Z-]+ \| ")
#: ``nx scratch list`` prints ``[<id8>] <tags>  flagged=<bool>`` then the entry's first 120 characters on the next line.
_LIST_ROW = re.compile(r"^\[([0-9a-fA-F]{8,64})\] (.*?)\s+flagged=\S+\s*$")
_BEAD_ID = re.compile(r"[a-z][a-z0-9_]*(?:-[a-z0-9_]+)*-[0-9a-z]{2,}(?:\.\d+)*")

Runner = Callable[[list[str], "str | None", float], "tuple[str, str | None]"]


def _clean(value: str, limit: int) -> str:
    """*value* on one line, printable, at most *limit* characters; ``|`` is the field separator, so it never appears."""
    text = " ".join(value.replace("|", "/").split())
    text = "".join(c if c.isprintable() else "_" for c in text)
    return text[:limit].rstrip()


def board_threads(board: str) -> list[dict[str, list[str]]]:
    """The board entry's threads, in order: each ``{"title": [..], "state": [..], "waiting_on": [..], "worktree": [..],
    "ref": [..]}`` (every key present, values the field lines' text)."""
    threads: list[dict[str, list[str]]] = []
    for ln in board.splitlines():
        if ln.startswith("## "):
            threads.append({"title": [ln[3:].strip()], "state": [], "waiting_on": [], "worktree": [], "ref": []})
            continue
        if not threads:
            continue
        stripped = ln.strip()
        for key in ("state", "waiting_on", "worktree", "ref"):
            if stripped.startswith(f"{key}:"):
                threads[-1][key].append(stripped[len(key) + 1:].strip())
                break
    return threads


def _named(token: str, text: str) -> bool:
    """*token* occurs in *text* as a whole id: ``nexus-z1d0l.1`` is not named by ``nexus-z1d0l.1.5``."""
    return re.search(rf"(?<![\w.-]){re.escape(token)}(?![\w-]|\.\w)", text) is not None


def _same_path(a: str, b: str) -> bool:
    return bool(a) and bool(b) and a.rstrip("/\\").lower() == b.rstrip("/\\").lower()


def compose(board: str, bead_rows: list[str], worktree: str | None, now: datetime | None = None) -> str | None:
    """The one-line entry, or None when there is no thread, state or worktree to put in it. *bead_rows* are the
    continuation's in-progress bead rows; one is used only when a ``ref:`` line on the board names it."""
    threads = board_threads(board)
    named_rows = [row for row in bead_rows
                  if (m := _BEAD_ID.search(row)) and any(_named(m.group(0), " ".join(t["ref"])) for t in threads)]
    chosen: dict[str, list[str]] | None = None
    if threads:
        by_worktree = [t for t in threads if worktree and any(_same_path(w, worktree) for w in t["worktree"])]
        beads = {m.group(0) for row in named_rows if (m := _BEAD_ID.search(row))}
        by_bead = [t for t in threads if any(_named(b, " ".join(t["ref"])) for b in beads)]
        chosen = (by_worktree or by_bead or threads)[0]
    parts = []
    if chosen:
        parts.append(f"thread: {_clean(chosen['title'][0], _THREAD_CHARS)}")
    where = worktree or (chosen["worktree"][0] if chosen and chosen["worktree"] else "")
    if where:
        parts.append(f"worktree: {_clean(where, _WORKTREE_CHARS)}")
    if chosen and chosen["state"]:
        parts.append(f"state: {_clean(chosen['state'][0], _STATE_CHARS)}")
    elif named_rows:
        parts.append(f"bead: {_clean(named_rows[0], _STATE_CHARS)}")
    if chosen and chosen["waiting_on"]:
        parts.append(f"waiting_on: {_clean(chosen['waiting_on'][0], _WAITING_CHARS)}")
    if not parts:
        return None
    stamp = (now or datetime.now(timezone.utc)).strftime("%Y-%m-%dT%H:%M:%SZ")
    line = f"resume {stamp} | " + " | ".join(parts)
    data = line.encode("utf-8")
    return line if len(data) <= MAX_BYTES else data[:MAX_BYTES].decode("utf-8", "ignore")


def resume_ids(listing: str) -> list[str]:
    """The id prefixes of the pointers in an ``nx scratch list`` *listing*: the tag list names ``resume-pointer``
    exactly AND the entry's first line opens ``resume <stamp> | `` (the head is inside the 120 characters the listing
    shows). A row that is only tagged, or only looks like a pointer, is never returned."""
    rows = listing.splitlines()
    ids = []
    for i, ln in enumerate(rows):
        row = _LIST_ROW.match(ln)
        if not row or TAG not in {t.strip() for t in row.group(2).split(",")}:
            continue
        body = rows[i + 1].strip() if i + 1 < len(rows) else ""
        if _HEAD.match(body):
            ids.append(row.group(1))
    return ids


def error_tail(error: str) -> str:
    """The cause part of ``_run_nx``'s error (``exited 1: 401 unauthorized``, ``timed out after 3s``), without the
    echoed command line: that carries the entry's own text."""
    if error.startswith("`") and "` " in error:
        error = error.split("` ", 1)[1]
    return _clean(error, 160)


def _list(run_nx: Runner, cwd: str | None, timeout: float) -> tuple[str, str | None]:
    return run_nx(["scratch", "--scope", "identity", "list"], cwd, timeout)


def _put(run_nx: Runner, line: str, cwd: str | None, timeout: float) -> tuple[str, str | None]:
    return run_nx(["scratch", "--scope", "identity", "put", line, "--tags", TAG], cwd, timeout)


def _delete(run_nx: Runner, entry_id: str, cwd: str | None, timeout: float) -> tuple[str, str | None]:
    return run_nx(["scratch", "--scope", "identity", "delete", entry_id], cwd, timeout)


def write(line: str, run_nx: Runner, cwd: str | None, log: Callable[[str, str], None], budget: float = BUDGET_S,
          where: str = "pre") -> str:
    """Put *line* in the identity scope as the one pointer. Returns ``identity`` or ``none``. Logs ONE line either way
    (``outcome=`` and ``ms=``, and on a failure the error tail). Never raises."""
    started = time.monotonic()
    deadline = started + min(budget, BUDGET_S)
    outcome, reason = "none", ""

    def left() -> float:
        return deadline - time.monotonic()

    try:
        if left() < _MIN_CALL_S:
            reason = "the time budget ran out"
        else:
            listing, error = _list(run_nx, cwd, min(CALL_TIMEOUT_S, left()))
            if error:
                reason = error_tail(error)
            elif left() < _MIN_CALL_S:
                reason = "the time budget ran out after the list"
            else:
                _out, error = _put(run_nx, line, cwd, min(CALL_TIMEOUT_S, left()))
                if error:
                    reason = error_tail(error)
                else:
                    outcome = "identity"
                    for entry_id in resume_ids(listing):  # put first: a failed put above left the last good pointer
                        if left() < _MIN_CALL_S:
                            break
                        _delete(run_nx, entry_id, cwd, min(CALL_TIMEOUT_S, left()))  # a failed delete leaves two; the stamp picks
    except Exception as exc:  # noqa: BLE001 -- a resume pointer never costs the checkpoint
        reason = f"{type(exc).__name__}: {_clean(str(exc), 120)}"
    ms = int((time.monotonic() - started) * 1000)
    if outcome == "identity":
        log("INFO", f"{where}: resume pointer outcome=identity ms={ms}")
    else:
        log("WARNING", f"{where}: resume pointer outcome=none ms={ms}: not written to identity scratch ({reason})")
    return outcome
