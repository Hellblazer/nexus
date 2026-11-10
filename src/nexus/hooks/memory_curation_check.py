# SPDX-License-Identifier: AGPL-3.0-or-later
"""``nx-hook memory-curation-check``: the SessionStart memory curation check (RDR-238 TD3).

Prints at most one line about the repository's Claude Code memory directory, and nothing when
the directory is clean. It runs on every session start inside the ``session-start`` verb and
``nx hook session-start`` (both through ``session_start_verb.with_curation``, which wraps
:func:`with_curation_line`), so it adds no ``hooks.json``
entry and no process; this verb runs the same check by hand. Sharing that process means a
failure here must never cost the guidance it is appended to, so every entry point swallows.

* **Where.** :func:`nexus._hook_runtime.memory_dir.locate`, the mapping ``nx memory curate
  --files`` uses: from the git common directory, so a linked worktree checks its primary's
  directory.
* **Opt-in.** One listing of the directory first; silent unless ``MEMORY-ondemand.md`` is in it,
  so a directory that has not opted in is never read further.
* **What.** :func:`nexus._hook_runtime.memory_scan.scan`, the scan ``nx memory curate --files``
  uses, so the two cannot disagree about "indexed" (named in either index, or linked from an
  indexed memory file, one hub level), "replaced" or "dangling". The line is for the first of,
  in order: ``MEMORY.md``'s bytes / 4 above ``memory.index_budget_tokens``; an unindexed file; a
  file another file's ``replaces`` frontmatter names that still exists; an index link to a
  missing file. ``--files`` has no budget row, so the budget line points at the on-demand index.
* **Budget.** ``memory.index_budget_tokens`` from the PRIMARY checkout's ``.nexus.yml`` (the
  memory directory is the primary's, so one setting governs it whichever worktree starts the
  session), read by a line scan for that key at the first indent under a top-level ``memory:``
  line, because a hook cannot import a YAML parser. Flow style (``memory: {...}``), a quoted key
  and anything else the scan does not recognise mean :data:`DEFAULT_BUDGET_TOKENS`, as do a
  missing, non-integer or non-positive value.
* **Deadline.** :func:`with_curation_line` waits for the check at most :data:`DEADLINE_SECONDS` (a daemon
  thread and a join timeout); past it the guidance is returned as it is and the curation line is skipped.
  The scan skips the oversize ``stat`` (``scan(..., oversize=False)``): the line never names a size.
* **Repeats.** The line is printed on every SessionStart source (startup, resume, clear, compact): it is one
  line, silent when the directory is clean, and a resumed session is exactly where an unindexed file may still be
  unaddressed, so it is kept as it is (nexus-qo7ie.27 disposition of the code review's S5).
* **Never blocks.** A missing directory means silence. An unreadable directory or index, or any
  exception, means silence plus a ``hook.log`` line.

Standard library only at module scope (``tests/hooks/test_hook_runtime_thin.py``): it runs on
every session start.
"""
from __future__ import annotations

import math
import os
import threading
from collections.abc import Mapping
from pathlib import Path

from nexus._hook_runtime._io import HookResult, _emit  # noqa: PLC2701 — the shared never-stdout logging spine
from nexus._hook_runtime.memory_dir import locate
from nexus._hook_runtime.memory_scan import scan

DEFAULT_BUDGET_TOKENS = 1500
ONDEMAND_INDEX = "MEMORY-ondemand.md"
MAX_NAME_CHARS = 80
#: The wall-clock bound of the check inside the session-start process (nexus-qo7ie.27): a memory directory on a
#: stalled mount must not hold the guidance the check is appended to until the hook's own timeout kills the verb.
DEADLINE_SECONDS = 2.0
_REMEDY = "run `nx memory curate --files`"


def _listdir(directory: Path) -> list[str]:
    """The directory's entries. A module seam, so a test can make listing fail on a platform where
    a mode bit cannot (Windows, root) without patching ``os`` for the whole process."""
    return os.listdir(directory)


def _getcwd() -> str:
    """The process's working directory; a seam for the same reason (Windows cannot remove a cwd)."""
    return os.getcwd()


def _shown(name: str) -> str:
    """A file name safe to put in model context: printable, one line, bounded."""
    clean = "".join(c if c.isprintable() else "?" for c in name)
    return clean if len(clean) <= MAX_NAME_CHARS else clean[: MAX_NAME_CHARS - 3] + "..."


def _budget_from(text: str) -> int | None:
    """``index_budget_tokens`` at the first indent under a top-level ``memory:`` line, or None."""
    in_memory = False
    child_indent: int | None = None
    for raw in text.splitlines():
        line = raw.split("#", 1)[0].rstrip()
        if not line.strip():
            continue
        indent = len(line) - len(line.lstrip())
        if indent == 0:
            in_memory = line == "memory:"
            child_indent = None
            continue
        if not in_memory:
            continue
        if child_indent is None:
            child_indent = indent
        if indent != child_indent:
            continue  # deeper: a nested mapping or a block scalar's text, never this key
        key, sep, value = line.strip().partition(":")
        if sep and key == "index_budget_tokens":
            value = value.strip()
            if not value.isascii() or not value.isdigit():
                return None
            n = int(value)
            return n if n > 0 else None
    return None


def budget_tokens(primary: Path | None) -> int:
    """The token budget for ``MEMORY.md``; see the module docstring."""
    if primary is None:
        return DEFAULT_BUDGET_TOKENS
    try:
        text = (primary / ".nexus.yml").read_text(encoding="utf-8-sig", errors="replace")
    except OSError:
        return DEFAULT_BUDGET_TOKENS
    found = _budget_from(text)
    return found if found is not None else DEFAULT_BUDGET_TOKENS


def check(cwd: Path, *, env: Mapping[str, str] | None = None, home: Path | None = None) -> str | None:
    """The one curation line for the repository containing *cwd*, or None when there is nothing to say."""
    try:
        directory, primary = locate(Path(os.path.abspath(cwd)), env=env, home=home)
        if directory is None:
            return None
        try:
            if ONDEMAND_INDEX not in _listdir(directory):
                return None
            # Read both indexes first: one that cannot be read would scan as "nothing indexed".
            (directory / ONDEMAND_INDEX).read_bytes()
            index = directory / "MEMORY.md"
            index_bytes = len(index.read_bytes()) if index.exists() else 0
            result = scan(directory, oversize=False)  # the line never names a size: skip one stat per file
        except OSError as exc:
            _emit("warning", "memory_curation_check_unreadable", directory=str(directory), error=str(exc))
            return None
        budget = budget_tokens(primary)
        if index_bytes / 4 > budget:
            return (f"Memory curation: MEMORY.md is about {math.ceil(index_bytes / 4)} tokens, over "
                    f"the {budget}-token budget (memory.index_budget_tokens); move entries to "
                    f"{ONDEMAND_INDEX}.")
        if result.unindexed:
            more = f" (and {len(result.unindexed) - 1} more)" if len(result.unindexed) > 1 else ""
            return (f"Memory curation: {_shown(result.unindexed[0])}{more} is in neither MEMORY.md "
                    f"nor {ONDEMAND_INDEX}; index it or {_REMEDY}.")
        if result.replaced_present:
            source, _slug, target = result.replaced_present[0]
            return (f"Memory curation: {_shown(target)} is still present though {_shown(source)} "
                    f"replaces it; delete it once {_shown(source)} covers it, or {_REMEDY}.")
        if result.dangling:
            index_name, target = result.dangling[0]
            return (f"Memory curation: {index_name} links {_shown(target)}, which does not exist; "
                    f"fix the entry or {_REMEDY}.")
        return None
    except Exception as exc:  # noqa: BLE001 — a SessionStart check never fails a session
        _emit("warning", "memory_curation_check_failed", error=f"{type(exc).__name__}: {exc}")
        return None


def curation_line(payload: dict | None) -> str | None:
    """:func:`check` for the payload's ``cwd`` (a string), else the process's working directory."""
    try:
        cwd = payload.get("cwd") if isinstance(payload, dict) else None
        return check(Path(cwd) if isinstance(cwd, str) and cwd else Path(_getcwd()))
    except Exception as exc:  # noqa: BLE001 — a deleted cwd raises in _getcwd(); still never fails a session
        _emit("warning", "memory_curation_check_failed", error=f"{type(exc).__name__}: {exc}")
        return None


def _bounded_line(payload: dict | None) -> str | None:
    """:func:`curation_line` run in a daemon thread and waited for at most :data:`DEADLINE_SECONDS`; None on a
    timeout (the thread is abandoned, never waited for at exit) or on any failure.

    The bound covers Python-level stalls only (a slow or blocked read the interpreter can step away from). A
    thread stuck in a kernel syscall on a dead mount can still hold the process at exit until the hook's own
    40 s timeout ends it; the deadline keeps the guidance from waiting on the check, not the process from waiting
    on the thread."""
    box: list[str | None] = []

    def work() -> None:
        try:
            box.append(curation_line(payload))
        except Exception:  # noqa: BLE001 — curation_line swallows already; this guards a replaced one
            pass

    worker = threading.Thread(target=work, name="memory-curation-check", daemon=True)
    worker.start()
    worker.join(DEADLINE_SECONDS)
    if worker.is_alive():
        _emit("warning", "memory_curation_check_timeout", seconds=DEADLINE_SECONDS)
        return None
    return box[0] if box else None


def with_curation_line(output: str | None, payload: dict | None) -> str | None:
    """*output* with the curation line appended after a blank line: what both session-start tiers print.

    The check runs under a deadline (:data:`DEADLINE_SECONDS`): past it the guidance is still returned and the
    curation line is skipped."""
    line = _bounded_line(payload)
    if not line:
        return output
    return f"{output}\n\n{line}" if output else line


def run(payload: dict | None) -> HookResult:
    """Print the curation line, or nothing. See the module docstring."""
    return HookResult(stdout=curation_line(payload))
