# SPDX-License-Identifier: AGPL-3.0-or-later
"""``nx-hook memory-curation-check``: the SessionStart memory curation check (RDR-238 TD3).

Prints at most one line about the repository's Claude Code memory directory, and nothing when
the directory is clean. It runs on every session start through the ``session-start`` verb
(``nexus.hooks.session_start_verb`` appends the line), so it adds no ``hooks.json`` entry and
no process; this verb runs the same check by hand.

* **Where.** :func:`nexus._hook_runtime.memory_dir.memory_dir`, the mapping ``nx memory curate
  --files`` uses: from the git common directory, so a linked worktree checks its primary's
  directory.
* **Opt-in.** Silent unless ``MEMORY-ondemand.md`` exists in that directory.
* **What.** :func:`nexus._hook_runtime.memory_scan.scan`, the scan ``nx memory curate --files``
  uses, so the two cannot disagree about "indexed", "replaced" or "dangling". The line is for
  the first of, in order: ``MEMORY.md``'s bytes / 4 above ``memory.index_budget_tokens``; an
  unindexed file; a file another file's ``replaces`` frontmatter names that still exists; an
  index link to a missing file.
* **Budget.** ``memory.index_budget_tokens`` from the repository's ``.nexus.yml`` (the worktree's
  own, else the primary's), read by a line scan for that one key under a top-level ``memory:``
  block, because a hook cannot import a YAML parser. A missing, non-integer or non-positive
  value means :data:`DEFAULT_BUDGET_TOKENS`.
* **Never blocks.** A missing or unreadable directory, or any exception, means silence; an
  unreadable directory or an exception is logged to ``hook.log``.

Standard library only at module scope (``tests/hooks/test_hook_runtime_thin.py``): it runs on
every session start.
"""
from __future__ import annotations

import os
from collections.abc import Mapping
from pathlib import Path

from nexus._hook_runtime._io import HookResult, _emit  # noqa: PLC2701 — the shared never-stdout logging spine
from nexus._hook_runtime.memory_dir import memory_dir, primary_checkout_root
from nexus._hook_runtime.memory_scan import scan

DEFAULT_BUDGET_TOKENS = 1500
ONDEMAND_INDEX = "MEMORY-ondemand.md"
_REMEDY = "run `nx memory curate --files`"


def _worktree_root(cwd: Path) -> Path | None:
    for d in (cwd, *cwd.parents):
        if (d / ".git").exists():
            return d
    return None


def _budget_from(text: str) -> int | None:
    """``index_budget_tokens`` directly under a top-level ``memory:`` block, or None."""
    in_memory = False
    for raw in text.splitlines():
        line = raw.split("#", 1)[0].rstrip()
        if not line.strip():
            continue
        if not line[0].isspace():
            in_memory = line.strip() == "memory:"
            continue
        if in_memory:
            key, sep, value = line.strip().partition(":")
            if sep and key == "index_budget_tokens":
                try:
                    n = int(value.strip())
                except ValueError:
                    return None
                return n if n > 0 else None
    return None


def budget_tokens(cwd: Path, env: Mapping[str, str] | None = None) -> int:
    """The token budget for ``MEMORY.md``; see the module docstring."""
    roots = [_worktree_root(cwd), primary_checkout_root(cwd, env)]
    for root in roots:
        if root is None:
            continue
        try:
            text = (root / ".nexus.yml").read_text(encoding="utf-8", errors="replace")
        except OSError:
            continue
        found = _budget_from(text)
        return found if found is not None else DEFAULT_BUDGET_TOKENS
    return DEFAULT_BUDGET_TOKENS


def check(cwd: Path, *, env: Mapping[str, str] | None = None, home: Path | None = None) -> str | None:
    """The one curation line for the repository containing *cwd*, or None when there is nothing to say."""
    try:
        directory = memory_dir(cwd, env=env, home=home)
        if directory is None:
            return None
        try:
            # Scan before the opt-in test: in a directory that cannot be listed, a stat of the
            # on-demand index fails too and would read as "not opted in", with nothing logged.
            result = scan(directory)
        except OSError as exc:
            _emit("warning", "memory_curation_check_unreadable", directory=str(directory), error=str(exc))
            return None
        if ONDEMAND_INDEX not in result.index_files:
            return None
        budget = budget_tokens(cwd, env)
        index_bytes = (directory / "MEMORY.md").stat().st_size if (directory / "MEMORY.md").is_file() else 0
        if index_bytes / 4 > budget:
            return (f"Memory curation: MEMORY.md is about {index_bytes // 4} tokens, over the "
                    f"{budget}-token budget (memory.index_budget_tokens); move entries to "
                    f"{ONDEMAND_INDEX} or {_REMEDY}.")
        if result.unindexed:
            more = f" (and {len(result.unindexed) - 1} more)" if len(result.unindexed) > 1 else ""
            return (f"Memory curation: {result.unindexed[0]}{more} is in neither MEMORY.md nor "
                    f"{ONDEMAND_INDEX}; index it or {_REMEDY}.")
        if result.replaced_present:
            source, _slug, target = result.replaced_present[0]
            return (f"Memory curation: {target} is still present though {source} replaces it; "
                    f"delete {target} or {_REMEDY}.")
        if result.dangling:
            index, target = result.dangling[0]
            return (f"Memory curation: {index} links {target}, which does not exist; fix the "
                    f"entry or {_REMEDY}.")
        return None
    except Exception as exc:  # noqa: BLE001 — a SessionStart check never fails a session
        _emit("warning", "memory_curation_check_failed", error=f"{type(exc).__name__}: {exc}")
        return None


def curation_line(payload: dict | None) -> str | None:
    """:func:`check` for the payload's ``cwd`` (a string), else the process's working directory."""
    cwd = payload.get("cwd") if isinstance(payload, dict) else None
    return check(Path(cwd) if isinstance(cwd, str) and cwd else Path(os.getcwd()))


def run(payload: dict | None) -> HookResult:
    """Print the curation line, or nothing. See the module docstring."""
    return HookResult(stdout=curation_line(payload))
