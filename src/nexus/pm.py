# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (c) 2026 Hal Hildebrand. All rights reserved.
"""Project Management Infrastructure business logic (`nx pm`).

Active PM docs live in T2 under the bare ``{repo}`` project namespace
(tagged with ``pm``).
"""
from __future__ import annotations

import re
from datetime import UTC, datetime
from typing import TYPE_CHECKING, Any

import structlog

if TYPE_CHECKING:
    from nexus.db.t2 import T2Database

# ── Constants ─────────────────────────────────────────────────────────────────

_STANDARD_DOCS: dict[str, str] = {
    "METHODOLOGY.md": (
        "# Methodology\n\nEngineering discipline and workflow for this project."
    ),
    "BLOCKERS.md": (
        "# Blockers\n"
    ),
    "CONTEXT_PROTOCOL.md": (
        "# Context Protocol\n\nContext management rules and relay format."
    ),
    "phases/phase-1/context.md": (
        "# Phase 1 Context\n\n(Describe phase goals and current state here.)"
    ),
}

_log = structlog.get_logger()




# ── AC1: pm_init ──────────────────────────────────────────────────────────────

def pm_init(db: "T2Database", project: str) -> None:
    """Create the 4 standard PM docs in T2 under ``{project}``."""
    date = datetime.now(UTC).strftime("%Y-%m-%d")
    for title, template in _STANDARD_DOCS.items():
        content = template.format(project=project, date=date)
        db.put(project, title, content, tags="pm,phase:1,context", ttl=None)


# ── AC2: pm_resume ────────────────────────────────────────────────────────────

def pm_resume(db: "T2Database", project: str) -> str | None:
    """Assemble computed continuation from ground truth, capped at 2000 chars.

    Returns structured markdown built from pm_status(), current phase
    context, and recent activity.  Returns None if no PM docs exist for
    *project*.
    """
    all_rows = db.get_all(project)
    if not all_rows:
        return None

    status = pm_status(db, project)
    parts: list[str] = []

    # Header
    parts.append(f"## PM Resume: {project}")
    parts.append(f"Phase: {status['phase']}  |  Agent: {status['agent'] or '(none)'}")
    if status["blockers"]:
        parts.append("Blockers: " + "; ".join(status["blockers"]))

    # Current phase context
    phase_title = f"phases/phase-{status['phase']}/context.md"
    phase_row = db.get(project=project, title=phase_title)
    if phase_row and phase_row.get("content"):
        parts.append("")
        parts.append(phase_row["content"][:600])

    # Recent activity
    entries = db.list_entries(project=project)[:5]
    if entries:
        parts.append("")
        parts.append("### Recent Activity")
        for e in entries:
            parts.append(f"- {e['title']} ({e.get('agent') or '-'}, {e.get('timestamp', '')[:10]})")

    return "\n".join(parts)[:2000]


# ── AC3: pm_status / pm_block / pm_unblock ────────────────────────────────────

def pm_status(db: "T2Database", project: str) -> dict[str, Any]:
    """Return status dict with phase, agent, and blockers."""
    all_rows = db.get_all(project)  # single query replaces N+1 list_entries + get pattern

    # Determine current phase: MAX phase tag across all docs
    phase = 1
    last_agent = None
    latest_ts = ""
    blockers_row = None
    for row in all_rows:
        tags = row.get("tags") or ""
        for tag in tags.split(","):
            tag = tag.strip()
            if tag.startswith("phase:"):
                try:
                    n = int(tag[6:])
                    if n > phase:
                        phase = n
                except ValueError:
                    pass
        ts = row.get("timestamp", "")
        if row.get("agent") and ts > latest_ts:
            latest_ts = ts
            last_agent = row["agent"]
        if row["title"] == "BLOCKERS.md":
            blockers_row = row

    # Blockers from BLOCKERS.md
    if blockers_row and blockers_row.get("content"):
        blocker_lines = [
            line.lstrip("- ").strip()
            for line in blockers_row["content"].splitlines()
            if line.strip().startswith("-")
        ]
    else:
        blocker_lines = []

    return {"phase": phase, "agent": last_agent, "blockers": blocker_lines}


def pm_block(db: "T2Database", project: str, blocker: str) -> None:
    """Append a blocker bullet to BLOCKERS.md (create if absent)."""
    row = db.get(project=project, title="BLOCKERS.md")
    existing = row["content"] if row and row.get("content") else "# Blockers\n"
    if not existing.endswith("\n"):
        existing += "\n"
    new_content = existing + f"- {blocker}\n"
    db.put(project, "BLOCKERS.md", new_content, tags="pm,blockers", ttl=None)


def pm_unblock(db: "T2Database", project: str, line: int) -> None:
    """Remove blocker at 1-based *line* number (as shown by pm_status)."""
    row = db.get(project=project, title="BLOCKERS.md")
    if row is None or not row.get("content"):
        return
    bullets = [
        ln for ln in row["content"].splitlines() if ln.strip().startswith("-")
    ]
    idx = line - 1
    if idx < 0 or idx >= len(bullets):
        raise IndexError(
            f"No blocker at line {line}; only {len(bullets)} blocker(s) exist."
        )
    bullets.pop(idx)
    non_bullets = [
        ln for ln in row["content"].splitlines() if not ln.strip().startswith("-")
    ]
    new_content = "\n".join(non_bullets) + "\n" + "\n".join(bullets)
    if bullets:
        new_content += "\n"
    db.put(project, "BLOCKERS.md", new_content.strip() + "\n", tags="pm,blockers", ttl=None)


# ── AC4: pm_phase_next ────────────────────────────────────────────────────────

def pm_phase_next(db: "T2Database", project: str) -> int:
    """Transition to the next phase.

    1. Reads current phase N as MAX(phase tag) across all docs.
    2. Creates phases/phase-{N+1}/context.md with initial content.

    Returns the new phase number.
    """
    status = pm_status(db, project)
    n = status["phase"]
    new_phase = n + 1

    content = (
        f"# Phase {new_phase} Context\n\n"
        "(Describe phase goals and current state here.)\n\n"
        f"Previous phase: {n}"
    )
    db.put(
        project,
        f"phases/phase-{new_phase}/context.md",
        content,
        tags=f"pm,phase:{new_phase},context",
        ttl=None,
    )

    return new_phase



# ── AC8: pm_search ────────────────────────────────────────────────────────────

def pm_search(
    db: "T2Database",
    query: str,
    project: str | None = None,
) -> list[dict[str, Any]]:
    """FTS5 search scoped to PM-tagged entries.

    Without *project*: searches all T2 entries tagged with ``pm``.
    With *project*: searches only ``{project}``.
    """
    if project is not None:
        return db.search(query, project=project)
    return db.search_by_tag(query, "pm")
