#!/usr/bin/env python3
# SPDX-License-Identifier: AGPL-3.0-or-later
"""T2 prefix-scan: surface all namespaces matching a project prefix.

Usage: t2_prefix_scan.py <project_name>

Queries T2 for every namespace whose name starts with *project_name*
(e.g. "nexus", "nexus_rdr", "nexus_pm") and prints a compact summary
formatted for session injection.

Cap algorithm:
  Per namespace (entries ranked by recency within that namespace):
    entries 1–5   : title + 1-line snippet (≤120 chars)
    entries 6–8   : title only
    beyond 8      : omitted; trailing count appended
  Cross-namespace hard cap: 15 rendered entries total (snippet or title).
  Namespaces are processed in recency order (most-recently-updated first).
"""
import sys

_HARD_CAP = 15       # max rendered entries across all namespaces combined
_SNIPPET_LIMIT = 5   # per-namespace: entries up to this rank get a snippet
_TITLE_LIMIT = 8     # per-namespace: entries up to this rank get title-only


def _snippet(content: str, max_chars: int = 120) -> str:
    """Return first meaningful line of content, truncated."""
    for line in content.splitlines():
        line = line.strip()
        if not line or line.startswith("#") or set(line) <= set("-="):
            continue
        return line[:max_chars] + ("…" if len(line) > max_chars else "")
    return ""


def main() -> None:
    if len(sys.argv) < 2:
        print("Usage: t2_prefix_scan.py <project_name>", file=sys.stderr)
        sys.exit(1)

    project_name = sys.argv[1]

    try:
        from nexus.commands._helpers import default_db_path
        from nexus.db.t2 import T2Database
    except ImportError as exc:
        print(f"T2 not available: {exc}", file=sys.stderr)
        return

    try:
        with T2Database(default_db_path()) as db:
            namespaces = db.get_projects_with_prefix(project_name)
            if not namespaces:
                return

            lines: list[str] = []
            total = 0  # rendered entries across all namespaces

            for ns_row in namespaces:
                if total >= _HARD_CAP:
                    break

                ns = ns_row["project"]
                entries = db.get_all(project=ns)
                if not entries:
                    continue

                suffix = ns[len(project_name):].lstrip("_") if ns != project_name else ""
                label = f"T2 Memory ({suffix})" if suffix else "T2 Memory"

                ns_lines: list[str] = []
                ns_remaining = 0
                ns_rank = 0  # per-namespace position (1-based)

                for entry in entries:
                    if total >= _HARD_CAP:
                        ns_remaining += 1
                        continue
                    ns_rank += 1
                    title = entry.get("title", "(untitled)")
                    if ns_rank <= _SNIPPET_LIMIT:
                        snip = _snippet(entry.get("content", ""))
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


if __name__ == "__main__":
    main()
