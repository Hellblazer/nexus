# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (c) 2026 Hal Hildebrand. All rights reserved.
"""Project Management Infrastructure business logic (`nx pm`).

Active PM docs live in T2 under the bare ``{repo}`` project namespace
(tagged with ``pm``).  Archive synthesis lives in T3
``knowledge__pm__{repo}`` (permanent, ttl=0).
"""
from __future__ import annotations

import hashlib
import re
from datetime import UTC, datetime, timedelta
from typing import TYPE_CHECKING, Any

import structlog

from nexus.db import make_t3

if TYPE_CHECKING:
    from nexus.db.t2 import T2Database
    from nexus.db.t3 import T3Database

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

_ARCHIVE_MODEL = "claude-haiku-4-5-20251001"

# ~1200 tokens x 3.3 chars/token ~ 3960 chars. Keeps each archive chunk
# within a comfortable context window for downstream embedding / retrieval.
_SYNTHESIS_CHAR_LIMIT = 3960


def _build_archive_context(
    docs: list[dict[str, Any]],
) -> tuple[list[dict[str, Any]], str, str]:
    """Select and trim docs for archival; return (selected, started_at, archived_at)."""
    standard_titles = set(_STANDARD_DOCS.keys())
    standard = [d for d in docs if d["title"] in standard_titles]
    others = sorted(
        [d for d in docs if d["title"] not in standard_titles],
        key=lambda d: d.get("timestamp", ""),
        reverse=True,
    )
    selected = (standard + others)[:100]

    total_chars = sum(len(d.get("content", "")) for d in selected)
    if total_chars > 100_000:
        budget = 100_000 - sum(len(d.get("content", "")) for d in standard)
        trimmed: list[dict] = list(standard)
        for doc in others:
            c = doc.get("content", "")
            if budget >= len(c):
                trimmed.append(doc)
                budget -= len(c)
        selected = trimmed

    timestamps = [d.get("timestamp", "") for d in docs if d.get("timestamp")]
    started_at = min(timestamps) if timestamps else "unknown"
    archived_at = datetime.now(UTC).isoformat()
    return selected, started_at, archived_at


def _format_archive(docs: list[dict[str, Any]], project: str, status: str) -> str:
    """Format PM docs as a structured archive (no AI — plain concatenation fallback)."""
    selected, started_at, archived_at = _build_archive_context(docs)
    parts: list[str] = [
        f"# Project Archive: {project}",
        f"Status: {status}",
        f"Date Range: {started_at} → {archived_at}",
        "",
    ]
    for doc in selected:
        parts.append(f"## {doc['title']}")
        parts.append(doc.get("content", ""))
        parts.append("")
    return "\n".join(parts).strip()


def _synthesize_archive(docs: list[dict[str, Any]], project: str, status: str) -> str:
    """Synthesize PM docs via ``claude --print`` subprocess.

    Builds a structured synthesis prompt and pipes it to the ``claude`` CLI.
    Falls back to ``_format_archive`` (plain concatenation) if ``claude`` is
    not on PATH or the subprocess fails — so ``nx pm archive`` works outside
    of a Claude Code session without any API key.
    """
    import shutil
    import subprocess

    if not shutil.which("claude"):
        _log.debug("claude CLI not found; using plain archive format")
        return _format_archive(docs, project, status)

    selected, started_at, archived_at = _build_archive_context(docs)
    context_parts: list[str] = []
    for doc in selected:
        context_parts.append(f"## {doc['title']}\n{doc.get('content', '')}")
    context = "\n\n".join(context_parts)

    prompt = (
        f"You are archiving a software project named '{project}'.\n\n"
        f"Project documents:\n\n{context}\n\n"
        f"Produce a concise archive synthesis in this format:\n\n"
        f"# Project Archive: {project}\n"
        f"Status: {status}\n"
        f"Date Range: {started_at} → {archived_at}\n\n"
        f"## Key Decisions\n- [concise decision + rationale, one line each]\n\n"
        f"## Architecture Choices\n- [structural choices that future projects should know about]\n\n"
        f"## Challenges & Resolutions\n- [non-obvious problems encountered + how resolved]\n\n"
        f"## Outcome\n[2-3 sentences: what was built, current state, notable gaps]\n\n"
        f"## Lessons Learned\n- [concrete, reusable takeaways]\n\n"
        f"Use brief bullets (one line per item). Target 400-800 tokens, hard cap 1200 tokens."
    )

    _log.info("Synthesizing archive via claude CLI", project=project)
    try:
        result = subprocess.run(
            ["claude", "--print", "--model", _ARCHIVE_MODEL],
            input=prompt,
            capture_output=True,
            text=True,
            timeout=120,
        )
        if result.returncode == 0 and result.stdout.strip():
            return result.stdout.strip()
        _log.warning(
            "claude CLI returned empty or non-zero; using plain archive format",
            returncode=result.returncode,
            stderr=result.stderr[:200] if result.stderr else "",
        )
    except Exception as exc:
        _log.warning("claude CLI synthesis failed; using plain archive format", error=str(exc))
    return _format_archive(docs, project, status)


def _split_synthesis(text: str) -> list[str]:
    """Split synthesis into chunks if it exceeds _SYNTHESIS_CHAR_LIMIT."""
    if len(text) <= _SYNTHESIS_CHAR_LIMIT:
        return [text]

    # Split at any ## section boundary — robust against header name variations
    parts = re.split(r"(?=^## )", text, flags=re.MULTILINE)
    if len(parts) <= 1:
        return [text[:_SYNTHESIS_CHAR_LIMIT]]

    # Group parts into at most 3 evenly-distributed chunks
    n = len(parts)
    chunk_size = max(1, (n + 2) // 3)  # ceiling division by 3
    chunks: list[str] = []
    for i in range(0, n, chunk_size):
        chunk = "".join(parts[i : i + chunk_size]).strip()
        if chunk:
            chunks.append(chunk)

    return chunks[:3] if chunks else [text[:_SYNTHESIS_CHAR_LIMIT]]


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


# ── AC5: pm_archive ───────────────────────────────────────────────────────────

def pm_archive(
    db: "T2Database",
    project: str,
    status: str = "completed",
    archive_ttl: int = 90,
) -> None:
    """Two-phase archive: synthesize → T3, then decay T2.

    Idempotency: if T3 already has a synthesis matching current T2 state
    (by pm_doc_count + pm_latest_timestamp), skip re-synthesis and proceed
    directly to the T2 decay step.
    """
    collection = f"knowledge__pm__{project}"

    # Gather current T2 state for idempotency check
    all_docs = db.get_all(project)
    if not all_docs:
        raise ValueError(f"No PM docs found for project '{project}'")

    doc_count = len(all_docs)
    max_ts = max(d.get("timestamp", "") for d in all_docs)

    t3 = make_t3()

    # Idempotency: metadata-only check — no embedding API call (nexus-dqz)
    col = t3.get_or_create_collection(collection)
    existing = col.get(
        where={"store_type": {"$eq": "pm-archive"}},
        include=["metadatas"],
    )
    if existing["ids"]:
        prior_meta = existing["metadatas"][0]
        if (
            prior_meta.get("pm_doc_count") == doc_count
            and prior_meta.get("pm_latest_timestamp") == max_ts
            and len(existing["ids"]) >= prior_meta.get("chunk_total", 1)
        ):
            # Current and complete — skip synthesis, proceed to T2 decay
            db.decay_project(project, archive_ttl)
            return

    # Phase 1: Synthesize → T3
    synthesis_text = _synthesize_archive(all_docs, project, status)

    # Compute metadata
    archived_at = datetime.now(UTC).isoformat()
    phase_tags = set()
    for doc in all_docs:
        for tag in (doc.get("tags") or "").split(","):
            tag = tag.strip()
            if tag.startswith("phase:"):
                try:
                    phase_tags.add(int(tag[6:]))
                except ValueError:
                    pass
    phase_count = max(phase_tags) if phase_tags else 1

    chunks = _split_synthesis(synthesis_text)
    for i, chunk in enumerate(chunks):
        chunk_title = (
            f"Archive: {project}"
            if len(chunks) == 1
            else f"Archive: {project} (part {i + 1})"
        )
        # Build complete metadata up front so t3.put() writes the full set.
        # col.update() replaces ALL metadata (it's not a merge), so we must
        # include every required field in the initial put — no post-put update needed.
        extra_meta: dict[str, Any] = {
            "project": project,
            "status": status,
            "archived_at": archived_at,
            "phase_count": phase_count,
            "pm_doc_count": doc_count,
            "pm_latest_timestamp": max_ts,
            "chunk_total": len(chunks),
        }
        if len(chunks) > 1:
            extra_meta["chunk_index"] = i
        doc_id = hashlib.sha256(f"{collection}:{chunk_title}".encode()).hexdigest()[:16]
        now_iso = datetime.now(UTC).isoformat()
        full_meta: dict[str, Any] = {
            "title": chunk_title,
            "tags": f"pm-archive,{project}",
            "category": "pm-archive",
            "session_id": "",
            "source_agent": "nx-pm-archive",
            "store_type": "pm-archive",
            "indexed_at": now_iso,
            "expires_at": "",
            "ttl_days": 0,
            **extra_meta,
        }
        t3.upsert_chunks(collection=collection, ids=[doc_id], documents=[chunk], metadatas=[full_meta])

    # Phase 2: Decay T2 (only after T3 write succeeds)
    db.decay_project(project, archive_ttl)


# ── AC6: pm_restore ───────────────────────────────────────────────────────────

def pm_restore(db: "T2Database", project: str) -> None:
    """Reverse T2 decay for *project*.

    Raises if no docs remain. Logs a structlog warning if only some docs survived.
    """
    surviving = db.restore_project(project)

    if not surviving:
        raise RuntimeError(
            f"raw docs fully expired for project '{project}' — "
            f"use `nx pm reference {project}` to access the synthesis. "
            f"Re-run `nx pm init` to start a new project."
        )

    # Check for partial expiry: compare against the 4 standard docs
    standard_titles = set(_STANDARD_DOCS.keys())
    missing = standard_titles - set(surviving)
    if missing:
        _log.warning("docs expired before restore", count=len(missing), titles=", ".join(sorted(missing)))


# ── AC7: pm_reference ────────────────────────────────────────────────────────

def _is_semantic_query(query: str) -> bool:
    """Return True if query should use semantic search (vs project-name filter)."""
    q = query.strip()
    # Quoted string, contains spaces, or contains ? → semantic
    if q.startswith('"') or " " in q or "?" in q:
        return True
    return False


def _list_pm_collections(t3: "T3Database") -> list[str]:
    """Return names of all knowledge__pm__ collections that have documents."""
    return [
        c["name"]
        for c in t3.list_collections()
        if c["name"].startswith("knowledge__pm__") and c["count"] > 0
    ]


def pm_reference(db: "T2Database", query: str) -> list[dict[str, Any]]:
    """Dispatch reference query to T3 semantic search or metadata-only filter."""
    if _is_semantic_query(query):
        # Semantic path: fan out to all knowledge__pm__ collections
        t3 = make_t3()
        clean_query = query.strip('"')
        pm_collections = _list_pm_collections(t3)
        if not pm_collections:
            return []
        return t3.search(
            clean_query,
            pm_collections,
            n_results=10,
            where={"store_type": {"$eq": "pm-archive"}},
        )
    else:
        # Project-name path: metadata-only filter on collection for that project
        t3 = make_t3()
        collection = f"knowledge__pm__{query}"
        if not t3.collection_exists(collection):
            return []
        col = t3.get_or_create_collection(collection)
        result = col.get(
            where={"store_type": {"$eq": "pm-archive"}},
            include=["documents", "metadatas"],
        )
        items: list[dict[str, Any]] = []
        for doc_id, doc, meta in zip(
            result.get("ids", []),
            result.get("documents", []),
            result.get("metadatas", []),
        ):
            items.append({"id": doc_id, "content": doc, **meta})
        return items


# ── AC9: pm_promote ───────────────────────────────────────────────────────────

def pm_promote(
    db_t2: "T2Database",
    db_t3: "T3Database",
    project: str,
    title: str,
    collection: str,
    ttl_days: int = 0,
) -> str:
    """Promote a T2 PM document to T3 permanent knowledge storage.

    Fetches *title* from the ``{project}`` T2 namespace and writes it to
    *collection* in T3.  Returns the T3 document ID.

    TTL translation:
    - T2 ``ttl=None`` (permanent) → T3 ``ttl_days=0, expires_at=""``
    - T2 ``ttl=N`` with caller-supplied *ttl_days* > 0 → T3 ``ttl_days=N,
      expires_at=<computed from doc timestamp + ttl_days>``

    Raises ``KeyError`` if the document is not found in T2.
    """
    doc = db_t2.get(project=project, title=title)
    if doc is None:
        raise KeyError(f"Document '{title}' not found in project '{project}'.")

    # TTL translation: caller-supplied ttl_days takes precedence over T2 ttl.
    # When ttl_days=0 (permanent), expires_at is empty.
    if ttl_days > 0:
        base_ts = datetime.fromisoformat(doc["timestamp"].replace("Z", "+00:00"))
        expires_at = (base_ts + timedelta(days=ttl_days)).isoformat()
    else:
        expires_at = ""

    doc_id = db_t3.put(
        collection=collection,
        content=doc["content"],
        title=title,
        tags=doc.get("tags") or "",
        store_type="pm-promoted",
        ttl_days=ttl_days,
        expires_at=expires_at,
    )
    return doc_id


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
