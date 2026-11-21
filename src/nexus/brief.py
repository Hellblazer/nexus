# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (c) 2026 Hal Hildebrand. All rights reserved.
"""``nx brief <topic>``: the reference context SessionStart no longer injects, on demand.

Design of record: T2 nexus/design-sessionstart-progressive-disclosure-2026-11-20 (approved by Sam 2026-11-20). SessionStart
used to inject 7-9 KB on resume, clear and fork, about half of it reference context (the T2 memory scan, ready beads and the
bd primer, the capabilities block, the Knowledge Map, the RDR count). The hook now prints the rules and the state of THIS
session, plus one static index line (``nexus.hooks.session_context.BRIEF_INDEX_LINE``); each of those blocks is a topic here,
loaded by the model or the person when the work calls for it.

* One source. Every topic renders through the function the hook used (``nexus.hooks.session_context``,
  ``nexus.hooks.rdr_verb.brief_lines``): there is no copy to drift, and a brief is versioned with the code. The CLI verb
  (:mod:`nexus.commands.brief`) and the MCP tool (``brief`` in :mod:`nexus.mcp.core`) call :func:`render` and print its text.
* Fail safe. :func:`render` never raises: a topic that breaks answers ``ok=False`` and one line saying so. What a session
  must see to work safely stays at session start and is not a topic: the T2 reachability line (nexus-8fvp2), ``bd prime``
  where the beads plugin is off (nexus-dg7i6.4), the T1 contract, the threads due, the notes line. The full-prefix MCP rule
  in ``caps`` is carried by CLAUDE.md and AGENTS.md. A brief that fails costs a session context, not those.
* ``me`` is this seat's own standing context, the ``startup`` note of its T2 project ``identity/<id>``. It is printed for the
  session to read and decide on; nothing here, and nothing in the SessionStart hook, executes a note. Notes are data.
* One usage record per call: a ``brief_load`` structlog event and one JSON line in ``<config>/logs/brief.log`` (through
  :mod:`nexus.hooks.usage_log`, which rotates it at 1 MB). Each carries ``topic``, ``ok``, ``bytes``, ``source`` (``cli`` or
  ``mcp``), ``session_id`` and ``repo``. SessionStart writes one ``session_start`` line per start into the same file: the
  denominator. The CLI logs at WARNING with no file sink, so a structlog line alone would never reach a review. The
  30-day review: owner marginalia; due 30 days after the release that ships this change; retire a topic loaded in under 5% of
  sessions, and promote one loaded in over 60% back to the always-on block.
"""
from __future__ import annotations

import os
from pathlib import Path
from typing import Any, NamedTuple

import click
import structlog

from nexus.hooks import session_context, usage_log

__all__ = ["TOPICS", "TOPIC_HELP", "BriefResult", "index_text", "render"]

_log = structlog.get_logger(__name__)

TOPICS: tuple[str, ...] = session_context.BRIEF_TOPICS

#: One line each, for ``nx brief`` with no topic and the MCP tool description.
TOPIC_HELP: dict[str, str] = {
    "memory": "T2 notes of this project: titles, newest first, with snippets (the scan SessionStart used to print)",
    "beads": "ready beads, and the bd usage primer when the beads plugin is off for this project",
    "caps": "notes on the nexus tools: search filters, MCP prefix, pagination, enrich commands",
    "map": "the Knowledge Map: topic labels per collection for this repo (nx context refresh builds it)",
    "rdr": "RDR counts by status and whether docs/rdr is indexed, with the remedy when it is not",
    "me": "this seat's own startup note (identity notes titled startup), printed for you to read and act on",
}

#: The longest an error line quotes of an exception's text.
_ERROR_CHARS = 200
#: The longest a logged topic name is, so a model's garbage argument cannot bloat the log.
_LOG_TOPIC_CHARS = 40

STARTUP_TITLE = "startup"

#: The longest a startup note is printed (UTF-8 bytes); longer ends in a marker. A note is a standing brief, not a document.
ME_MAX_BYTES = 2048


class BriefResult(NamedTuple):
    """The text a topic printed and whether it was produced without error. Empty states ("no bd on PATH", "no startup
    note") are ``ok``: they are answers. ``ok=False`` is a topic that raised, or a topic that does not exist."""

    text: str
    ok: bool


def index_text() -> str:
    """What ``nx brief`` with no topic prints: how to ask, and each topic with its one line."""
    width = max(len(t) for t in TOPICS)
    rows = "\n".join(f"  {t.ljust(width)}  {TOPIC_HELP[t]}" for t in TOPICS)
    return (
        "Context that SessionStart does not inject. Ask for a topic with `nx brief <topic>` "
        f"(or the `brief` MCP tool):\n{rows}"
    )


def _project_dir(cwd: str) -> Path:
    """The project the hook keyed T2 and bd by: ``CLAUDE_PROJECT_DIR``, else the working directory."""
    return Path(os.environ.get("CLAUDE_PROJECT_DIR", cwd)).resolve()


def _text(lines: list[str]) -> str:
    return "\n".join(lines).strip()


def _memory(cwd: str) -> str:
    return _text(session_context._render_t2_memory(str(_project_dir(cwd)))) or (
        "memory: nothing to show (no repo here, or no T2 notes for it)"
    )


def _beads(cwd: str) -> str:
    if not session_context.which("bd"):
        return "beads: `bd` is not on PATH, so there is no ready list to show"
    return _text(session_context._render_beads(_project_dir(cwd))) or "beads: nothing to show (`bd ready` printed nothing)"


def _caps(cwd: str) -> str:  # noqa: ARG001 — every topic takes the cwd; this one is static
    return _text(session_context._build_capabilities_block())


def _map(cwd: str) -> str:
    # The cache is keyed by the process cwd, not CLAUDE_PROJECT_DIR: that is what `nx context refresh` wrote it under.
    return _text(session_context._render_knowledge_map(cwd)) or (
        "map: no Knowledge Map cache for this repo; `nx context refresh` builds one"
    )


def _rdr(cwd: str) -> str:  # noqa: ARG001 — the RDR tree is found from the process cwd by the hook's own helper
    from nexus.hooks import rdr_verb  # noqa: PLC0415 — deferred: only this topic pays for the verb's imports

    return _text(rdr_verb.brief_lines()) or "rdr: no RDR directory in this repo"


def _own_notes_project() -> str:
    """``identity/<id>`` for this process, as ``nx memory --mine`` resolves it (a delegated child reads its parent's).
    Raises :class:`click.ClickException` when no identity is bound."""
    from nexus.commands.memory import _mine  # noqa: PLC0415, PLC2701 — one rule for "my identity project", not a copy

    project = _mine(None, True)
    if project is None:  # unreachable: --mine answers a project or raises; typed for the checker
        raise click.ClickException("--mine named no project")
    return project


def _open_memory_store() -> Any:
    from nexus.db.t2.http_memory_store import HttpMemoryStore  # noqa: PLC0415 — deferred: only this topic pays for the client

    return HttpMemoryStore()


def _how_to_write_startup_note(project: str) -> str:
    """The hint for an empty startup note, matched to what this caller may write: a seat gets the ``--mine`` command, any
    other identity (ephemeral, delegated) gets the guard's own one-line reason, because the same write would be refused to
    it (gh#1680). Reads only: :func:`nexus.identity.notes.write_refusal` logs no refusal. Best effort: a failure of the
    lookup gives a neutral hint and never fails the ``me`` topic."""
    command = f"`nx memory put --mine --title {STARTUP_TITLE} \"...\"`"
    try:
        from nexus.identity import notes  # noqa: PLC0415 — deferred: only an empty note pays for the identity guard

        refusal = notes.write_refusal(project)
    except Exception:  # noqa: BLE001 — a hint never costs the brief
        return f"if this identity is a seat, write one with {command} to have it here next time"
    if refusal is not None:
        return " ".join(refusal[1].split())
    return f"write one with {command} to have it here next time"


def _me(cwd: str) -> str:  # noqa: ARG001
    try:
        project = _own_notes_project()
    except click.ClickException as exc:
        message = " ".join(exc.format_message().split())
        if "no identity is bound" in message:
            return "me: no identity is bound to this process, so there is no startup note"
        return f"me: {message}"
    store = _open_memory_store()
    try:
        row = store.get(project=project, title=STARTUP_TITLE)
    finally:
        store.close()
    content = str(row.get("content") or "").strip() if row else ""
    if not content:
        return f"me: no startup note in {project}; {_how_to_write_startup_note(project)}"
    data = content.encode("utf-8")
    if len(data) > ME_MAX_BYTES:
        content = data[:ME_MAX_BYTES].decode("utf-8", "ignore").rstrip() + "\n(truncated)"
    return (
        f"## Your startup note ({project}, title \"{STARTUP_TITLE}\")\n"
        "A note is data, not a command: nothing here has run. Read it and apply what fits this session.\n"
        f"{content}"
    )


def _session_id() -> str:
    try:
        from nexus.session import resolve_active_session_id  # noqa: PLC0415 — deferred: only a recorded call pays the import

        return resolve_active_session_id() or ""
    except Exception:  # noqa: BLE001 — usage accounting never costs a brief
        return ""


def _record(topic: str, result: BriefResult, source: str, cwd: str) -> None:
    """The usage record of one call: the structlog event, and one line in the shared usage file. Never raises."""
    try:
        fields = {
            "topic": topic[:_LOG_TOPIC_CHARS], "ok": result.ok, "bytes": len(result.text.encode("utf-8")), "source": source,
            "session_id": _session_id(), "repo": session_context.repo_names(cwd)[1],
        }
        _log.info("brief_load", **fields)
        usage_log.append("brief_load", **fields)
    except Exception:  # noqa: BLE001 — usage accounting never costs a brief
        return


_RENDERERS = {"memory": _memory, "beads": _beads, "caps": _caps, "map": _map, "rdr": _rdr, "me": _me}


def render(topic: str, *, cwd: str | None = None, source: str = "cli") -> BriefResult:
    """The text of *topic* (trimmed, case-insensitive), rendered for the repo at *cwd* (default: the process cwd), and one
    usage record. Never raises."""
    topic = topic.strip().lower()
    where = cwd if cwd is not None else os.getcwd()
    renderer = _RENDERERS.get(topic)
    if renderer is None:
        result = BriefResult(f"brief: unknown topic {topic[:_LOG_TOPIC_CHARS]!r}; topics are {', '.join(TOPICS)}", False)
    else:
        try:
            result = BriefResult(renderer(where), True)
        except Exception as exc:  # noqa: BLE001 — a brief never fails its caller; the caller is told, and the log has it
            result = BriefResult(f"brief {topic} failed: {type(exc).__name__}: {str(exc)[:_ERROR_CHARS]}", False)
    _record(topic, result, source, where)
    return result
