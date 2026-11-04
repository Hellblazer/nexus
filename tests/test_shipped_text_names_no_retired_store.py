# SPDX-License-Identifier: AGPL-3.0-or-later
"""Shipped text describes the storage nexus has, not the one it retired (nexus-pplwc).

T2 is PostgreSQL behind the engine and memory_search is PostgreSQL full-text search with no query grammar; SQLite
and ChromaDB are gone (RDR-158 P4, RDR-155 P4b). 7.95.0 still told users T2 was "per-project SQLite", that
memory_search takes "FTS5 syntax", and that a store failure was a "ChromaDB connectivity failure".

The sweep reads the plugin markdown (less the change records), ``conexus/registry.yaml``, both plugins'
``plugin.json``, the mcpb manifest, and every MCP tool's description and parameter descriptions as the server
registers them. Any ``SQLite``, ``Chroma`` or ``FTS5`` there fails unless :data:`_HISTORY` lists that file and a
fragment of that line with the reason it is history, not a claim about the running system.
"""
from __future__ import annotations

import importlib
import re
from pathlib import Path

import pytest

pytestmark = pytest.mark.lint

_REPO = Path(__file__).resolve().parents[1]
_RECORDS = frozenset({"CHANGELOG.md", "PENDING_RELEASE.md"})
_RETIRED_RE = re.compile(r"sqlite|chroma|fts5", re.IGNORECASE)

#: (source, fragment of the line) -> why the mention is history. A source is a repo path or "mcp:<tool>".
_HISTORY: dict[tuple[str, str], str] = {
    ("conexus/README.md", "`pdf-chromadb-processor`"): "the retired agent's name, in the list of what replaced it",
    ("conexus/README.md", "*(was → pdf-chromadb-processor agent)*"): "the same retired agent's name",
    ("conexus/README.md", "the SQLite-backed version retired at RDR-158 P4"): "says SQLite is retired",
    ("conexus/resources/agent-shared/ERROR_HANDLING.md", "(Historical: this section described"): "a history note",
    ("conexus/resources/agent-shared/ERROR_HANDLING.md", "There is no SQLite"): "says there is no SQLite",
    ("conexus/skills/upgrade/SKILL.md", "The ChromaDB → Postgres+pgvector move is NOT among"): (
        "the upgrade skill names the migration it no longer performs"
    ),
    ("conexus/skills/upgrade/SKILL.md", "unmigrated pre-PG data (`chroma.sqlite3`"): (
        "names the leftover files of a pre-PG install so a user can recognise one"
    ),
    ("conexus/skills/upgrade/SKILL.md", "a ChromaDB Cloud key with no mode record"): (
        "names a pre-PG config shape a user may still hold"
    ),
    ("conexus/skills/nexus/reference.md", "SQLite is retired (RDR-158 P4)"): "says SQLite is retired",
    ("conexus/skills/nexus/reference.md", "ChromaDB is not a live substrate in any mode"): "says ChromaDB is gone",
    ("mcp:daemon_uninstall", "any relic Chroma directory from a pre-PG"): (
        "the uninstaller removes a pre-PG leftover directory; naming it says what is deleted"
    ),
}


def _file_lines() -> list[tuple[str, int, str]]:
    paths = [p for pattern in ("conexus/**/*.md", "sn/**/*.md") for p in _REPO.glob(pattern) if p.name not in _RECORDS]
    paths += [_REPO / "conexus" / "registry.yaml", _REPO / "mcpb" / "manifest.json",
              _REPO / "conexus" / ".claude-plugin" / "plugin.json", _REPO / "sn" / ".claude-plugin" / "plugin.json"]
    return [(p.relative_to(_REPO).as_posix(), n, line)
            for p in sorted(paths) for n, line in enumerate(p.read_text(encoding="utf-8").splitlines(), 1)]


def _tool_lines() -> list[tuple[str, int, str]]:
    out: list[tuple[str, int, str]] = []
    for module in ("nexus.mcp.core", "nexus.mcp.catalog"):
        for name, tool in importlib.import_module(module).mcp._tool_manager._tools.items():
            texts = [tool.description or ""] + [
                str(p.get("description", "")) for p in (tool.parameters or {}).get("properties", {}).values()]
            out += [(f"mcp:{name}", n, line) for text in texts for n, line in enumerate(text.splitlines(), 1)]
    return out


def _offenders(lines: list[tuple[str, int, str]]) -> list[str]:
    return [f"{src}:{n}: {line.strip()[:110]}" for src, n, line in lines
            if _RETIRED_RE.search(line) and not any(s == src and frag in line for s, frag in _HISTORY)]


def test_no_shipped_text_claims_a_retired_store() -> None:
    lines = _file_lines() + _tool_lines()
    assert len({src for src, _, _ in lines}) > 80, "the sweep no longer sees the plugin text and the tools"
    offenders = _offenders(lines)
    assert not offenders, "\n".join(offenders)


def test_history_entries_still_match_a_line() -> None:
    lines = _file_lines() + _tool_lines()
    stale = [k for k in _HISTORY if not any(src == k[0] and k[1] in line for src, _, line in lines)]
    assert not stale, stale


def test_a_live_claim_on_a_line_that_also_says_retired_is_caught() -> None:
    """Review r1: the old word-list exemption passed any line with "retired" on it."""
    planted = [("conexus/x.md", 1, "T2 is SQLite (not retired)."), ("conexus/x.md", 2, "Chroma holds T3."),
               ("mcp:planted", 1, "FTS5 syntax.")]
    assert len(_offenders(planted)) == 3
