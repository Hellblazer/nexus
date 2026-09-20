# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (c) 2026 Hal Hildebrand. All rights reserved.
"""The PreToolUse(Agent|Task) EXPECT writer (RDR-215 bead nexus-q02nx.10).

Port of ``conexus/hooks/scripts/agent-dispatch-expect.sh`` (contract map
row 12). Writes one RDR-184 ``EXPECT`` row per agent dispatch, from the
dispatch's own ``subagent_type`` and ``run_in_background``, BEFORE the
dispatch happens -- write-before-dispatch is the load-bearing ordering,
because a row written afterwards races the subagent's own START.

**STDOUT-SILENT ON EVERY PATH.** This hook never writes to real stdout;
its diagnostics go to stderr, and :class:`HookResult`'s ``stdout`` stays
``None`` throughout. A stray byte on stdout is a malformed hook decision.

**Every path returns cleanly.** The bash exits 0 unconditionally, and the
four skip paths each emit one verbatim stderr line naming why no row was
written. Those lines are reproduced exactly: they are what an operator
reads when the ledger is missing a dispatch, and the whole RDR-184 guard
is built on the ledger being complete.

**A missing ``subagent_type`` is keyed ``general-purpose``, not a
placeholder** (nexus-a795d). The harness genuinely starts a
``general-purpose`` agent when the field is absent, so keying it anything
else would put a row in the ledger under a type no START will ever carry,
which reads later as an expected-but-never-started dispatch.
``docs/cli-reference.md`` documents this and must stay true.
"""

from __future__ import annotations

import os
import time
from pathlib import Path

from nexus._hook_runtime._config import stop_guard_mode
from nexus._hook_runtime._io import HookResult, _emit, structured_field
from nexus.hooks import expectations as _exp

__all__ = ["run"]

#: The dispatch tools this hook answers for. Anything else is skipped with
#: a named diagnostic rather than silently, because a hook wired to the
#: wrong matcher should say so.
_DISPATCH_TOOLS = ("Agent", "Task")

#: Field scrub: these characters would reshape the TSV row or the
#: bash reader's own \x1f-delimited decode.
_SCRUB = str.maketrans({"\x1f": " ", "\t": " ", "\n": " ", "\r": " "})

_LOCK_TRIES = 10


def _skip(reason: str) -> None:
    """One operator-facing line naming why no EXPECT row was written.

    Goes to stderr, never stdout: this hook is stdout-silent by contract
    and a stray byte there is a malformed hook decision. These are the
    lines someone reads when the ledger is missing a dispatch, and the
    whole RDR-184 guard rests on the ledger being complete, so they are
    carried verbatim from the bash rather than reworded.
    """
    _emit("warning", "agent_dispatch_expect_skipped", reason=reason)


def _as_background(value: object) -> bool:
    """Interpret ``run_in_background``, defaulting to True.

    Defaulting to background is deliberate: a dispatch whose mode cannot be
    read still deserves a row, and a background row is the one that can
    later require a report. The string forms are accepted because the
    field has arrived as a string from the harness.
    """
    if isinstance(value, str):
        return value.strip().lower() not in ("false", "0", "no", "")
    return bool(value)


def _already_written(file: str, dispatch_id: str) -> bool:
    """True iff this exact dispatch already has an EXPECT row.

    The hook takes a BOUNDED lock, so a double registration that outlasts
    the budget can append the same dispatch twice -- and a duplicate EXPECT
    is not the harmless nuisance a duplicate START is: it inflates the
    credit pool and MASKS an undeclared start. Checking by ``tool_use_id``
    settles it before the write rather than leaving the reader to dedupe.
    """
    if not dispatch_id:
        return False
    try:
        text = Path(file).read_text()
    except OSError:
        return False
    for line in text.split("\n"):
        row = line.split("\t")
        if len(row) > 4 and row[1] == "EXPECT" and row[4] == dispatch_id:
            return True
    return False


def _acquire(lockdir: str) -> bool:
    """Best-effort mutual exclusion around the read-then-append.

    Unlike the credit claim, correctness here does NOT rest on an atomic
    primitive -- the worst case is a duplicate row, which the reader
    dedupes by ``dispatch_id`` anyway. So a bounded budget that gives up is
    right: the row still gets written, and a missing row would be far worse
    than a duplicate one.
    """
    try:
        if os.path.isdir(lockdir) and (time.time() - os.stat(lockdir).st_mtime) > 60:
            os.rmdir(lockdir)
    except OSError:
        pass
    for _ in range(_LOCK_TRIES):
        try:
            os.mkdir(lockdir)
            return True
        except FileExistsError:
            time.sleep(0.1)
        except OSError:
            return False
    return False


def run(payload: dict | None) -> HookResult:
    """Write the EXPECT row for one agent dispatch.

    Always returns a silent, exit-0 result. Diagnostics are stderr lines
    carried verbatim from the bash.
    """
    if stop_guard_mode() not in ("observe", "block"):
        return HookResult()

    data = payload or {}
    # Contractually an object, but see structured_field: on the tool tier
    # it can arrive as JSON text, and reading that as "no subagent_type"
    # silently recorded every dispatch as general-purpose (bead
    # nexus-17i1n). A wrong type is worse here than a missing row.
    tool_input = structured_field(data, "tool_input")

    session_id = str(data.get("session_id") or "").translate(_SCRUB)
    tool_name = str(data.get("tool_name") or "").translate(_SCRUB)
    subagent_type = str(tool_input.get("subagent_type") or "general-purpose").translate(
        _SCRUB
    )
    dispatch_mode = (
        "background"
        if _as_background(tool_input.get("run_in_background", True))
        else "sync"
    )
    dispatch_id = str(data.get("tool_use_id") or "").translate(_SCRUB)
    shown_id = dispatch_id or "<none>"

    if tool_name not in _DISPATCH_TOOLS:
        _skip(
            f"agent-dispatch-expect: tool_name '{tool_name}' is not Agent/Task — "
            f"EXPECT row NOT written for this dispatch (tool_use_id={shown_id})"
        )
        return HookResult()
    if not session_id:
        _skip(
            "agent-dispatch-expect: empty/unparseable session_id — EXPECT row NOT "
            f"written for this dispatch (tool_use_id={shown_id})"
        )
        return HookResult()
    if not subagent_type:
        return HookResult()

    try:
        file = _exp.expectations_file(session_id)
    except _exp.ExpectationsUsageError:
        _skip(
            f"agent-dispatch-expect: expectations_file rejected session_id "
            f"'{session_id}' — EXPECT row NOT written for this dispatch "
            f"(tool_use_id={shown_id}, subagent_type={subagent_type})"
        )
        return HookResult()

    lockdir = f"{file}.expect.lock"
    held = _acquire(lockdir)
    try:
        if not _already_written(file, dispatch_id):
            try:
                _exp.expectations_expect(
                    session_id, subagent_type, dispatch_mode, dispatch_id
                )
            except _exp.ExpectationsUsageError as exc:
                _skip(
                    f"agent-dispatch-expect: refused to write an EXPECT row for "
                    f"subagent_type '{subagent_type}' (tool_use_id={shown_id}): {exc}"
                )
    finally:
        if held:
            try:
                os.rmdir(lockdir)
            except OSError:
                pass
    return HookResult()
