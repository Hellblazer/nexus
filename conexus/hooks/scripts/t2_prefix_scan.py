#!/usr/bin/env python3
# SPDX-License-Identifier: AGPL-3.0-or-later
"""T2 prefix-scan: surface all namespaces matching a project prefix.

Usage: t2_prefix_scan.py <project_name>

Queries T2 for every namespace whose name starts with *project_name*
(e.g. "nexus", "nexus_rdr", "nexus_knowledge") and prints a compact summary
formatted for session injection.

**Stdlib-only** (nexus-vg6d4): this script must run under whatever bare
Python interpreter ``_run_python_hook.sh`` resolves (probes
``python3.13`` → ``python3.12`` → ``python3``), which on a
``uv tool install conexus`` deployment is the system Python that
cannot import the ``nexus`` package (it lives in conexus's own venv).
Importing ``nexus.db.t2`` would silently fail and the entire
``## T2 Memory (Active Project)`` section would be omitted from the
session-start context. Using ``sqlite3`` directly keeps the hook
portable across the wrapper's interpreter probe.

The SQL mirrors ``MemoryStore.get_projects_with_prefix`` and
``MemoryStore.get_all`` verbatim (including the ``ESCAPE '\\'`` clause
that prevents ``LIKE`` metacharacters in the prefix from matching
unintended namespaces).

Cap algorithm:
  Per namespace (entries ranked by recency within that namespace):
    entries 1–5   : title + 1-line snippet (≤120 chars)
    entries 6–8   : title only
    beyond 8      : omitted; trailing count appended
  Cross-namespace hard cap: 15 rendered entries total (snippet or title).
  Namespaces are processed in recency order (most-recently-updated first).
"""
from __future__ import annotations

import os
import sqlite3
import sys
from pathlib import Path

if sys.version_info < (3, 12):
    sys.stderr.write(
        f"ERROR: conexus plugin hook requires Python 3.12+, got {sys.version.split()[0]}\n"
        f"  Resolved: {sys.executable}\n"
        f"  Install: brew install python@3.13 (macOS) | apt install python3.12 (Ubuntu) | uv python install 3.12\n"
    )
    sys.exit(1)

_HARD_CAP = 15       # max rendered entries across all namespaces combined
_SNIPPET_LIMIT = 5   # per-namespace: entries up to this rank get a snippet
_TITLE_LIMIT = 8     # per-namespace: entries up to this rank get title-only


def _default_db_path() -> Path:
    """Stdlib-only mirror of ``nexus.config.default_db_path``.

    Honours ``NEXUS_CONFIG_DIR`` / ``NX_CONFIG_DIR`` env overrides for
    parity with the test sandbox and the release-sandbox harness, then
    falls back to the canonical ``~/.config/nexus/memory.db``. Kept in
    sync with the resolver in ``src/nexus/config.py``; if that resolver
    ever grows additional precedence rules, mirror them here.
    """
    config_dir = (
        os.environ.get("NEXUS_CONFIG_DIR")
        or os.environ.get("NX_CONFIG_DIR")
    )
    if config_dir:
        return Path(config_dir) / "memory.db"
    return Path.home() / ".config" / "nexus" / "memory.db"


def _snippet(content: str, max_chars: int = 120) -> str:
    """Return first meaningful line of content, truncated."""
    for line in content.splitlines():
        line = line.strip()
        if not line or line.startswith("#") or set(line) <= set("-="):
            continue
        return line[:max_chars] + ("…" if len(line) > max_chars else "")
    return ""


def _get_namespaces(conn: sqlite3.Connection, prefix: str) -> list[str]:
    """Return project namespaces matching ``prefix``, recency-ordered (DESC).

    Mirrors ``MemoryStore.get_projects_with_prefix``: ``LIKE`` metacharacters
    ``\\``, ``%``, ``_`` in *prefix* are escaped so they match literally
    (a repo named ``my_project`` will not match ``myXproject``).
    """
    if not prefix:
        return []
    escaped = (
        prefix.replace("\\", "\\\\")
              .replace("%", "\\%")
              .replace("_", "\\_")
    )
    rows = conn.execute(
        "SELECT project, MAX(timestamp) AS last_updated "
        "FROM memory WHERE project LIKE ? ESCAPE '\\' "
        "GROUP BY project ORDER BY MAX(timestamp) DESC",
        (f"{escaped}%",),
    ).fetchall()
    return [row[0] for row in rows]


def _get_entries(conn: sqlite3.Connection, project: str) -> list[tuple[str, str]]:
    """Return ``[(title, content), ...]`` for *project*, recency-ordered."""
    rows = conn.execute(
        "SELECT title, content FROM memory WHERE project = ? "
        "ORDER BY timestamp DESC",
        (project,),
    ).fetchall()
    return [(row[0], row[1] or "") for row in rows]


def main() -> None:
    if len(sys.argv) < 2:
        print("Usage: t2_prefix_scan.py <project_name>", file=sys.stderr)
        sys.exit(1)

    project_name = sys.argv[1]
    db_path = _default_db_path()
    if not db_path.exists():
        # No T2 yet (fresh install). Silently no-op so the hook stays
        # invisible until the user actually writes a memory entry.
        return

    try:
        conn = sqlite3.connect(
            f"file:{db_path}?mode=ro",
            uri=True,
            timeout=5.0,
        )
    except sqlite3.OperationalError as exc:
        print(f"T2 read error: {exc}", file=sys.stderr)
        return

    try:
        namespaces = _get_namespaces(conn, project_name)
        if not namespaces:
            return

        lines: list[str] = []
        total = 0  # rendered entries across all namespaces

        for ns in namespaces:
            if total >= _HARD_CAP:
                break

            entries = _get_entries(conn, ns)
            if not entries:
                continue

            suffix = ns[len(project_name):].lstrip("_") if ns != project_name else ""
            label = f"T2 Memory ({suffix})" if suffix else "T2 Memory"

            ns_lines: list[str] = []
            ns_remaining = 0
            ns_rank = 0  # per-namespace position (1-based)

            for title, content in entries:
                if total >= _HARD_CAP:
                    ns_remaining += 1
                    continue
                ns_rank += 1
                if ns_rank <= _SNIPPET_LIMIT:
                    snip = _snippet(content)
                    ns_lines.append(f"  {title}" + (f" — {snip}" if snip else ""))
                    total += 1
                elif ns_rank <= _TITLE_LIMIT:
                    ns_lines.append(f"  {title}")
                    total += 1
                else:
                    ns_remaining += 1

            if ns_lines:
                lines.append(f"### {label}")
                lines.extend(ns_lines)
                if ns_remaining:
                    lines.append(f"  … ({ns_remaining} more)")
                lines.append("")

        if lines:
            print("\n".join(lines), end="")
    except Exception as exc:
        print(f"T2 read error: {exc}", file=sys.stderr)
    finally:
        conn.close()


if __name__ == "__main__":
    main()
