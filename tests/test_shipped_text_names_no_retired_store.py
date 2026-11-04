# SPDX-License-Identifier: AGPL-3.0-or-later
"""Shipped plugin text describes the storage nexus has, not the one it retired (nexus-pplwc).

T2 is PostgreSQL behind the engine and memory_search is PostgreSQL full-text search with no query grammar; SQLite
and ChromaDB are gone (RDR-158 P4, RDR-155 P4b). 7.95.0 still told users T2 was "per-project SQLite", that
memory_search takes "FTS5 syntax", and that a store failure was a "ChromaDB connectivity failure". This sweeps the
plugin markdown (less changelogs) and the mcpb manifest:

* ``FTS5`` never appears: no surface uses it.
* ``SQLite`` and ``ChromaDB`` appear only on a line that says they are retired or historical, or that names the
  migration away from them.
"""
from __future__ import annotations

import re
from pathlib import Path

import pytest

pytestmark = pytest.mark.lint

_REPO = Path(__file__).resolve().parents[1]
#: Words on the same line that mark a SQLite or ChromaDB mention as history or migration, not a current claim.
_HISTORY = re.compile(
    r"retired|historical|no sqlite|not a live|→ postgres|pre-pg|unmigrated|chroma\.sqlite3|chromadb cloud|"
    r"pdf-chromadb-processor|chromadb quotas",
    re.IGNORECASE,
)


def _files() -> list[Path]:
    md = [p for p in _REPO.glob("conexus/**/*.md") if p.name != "CHANGELOG.md"]
    md += [p for p in _REPO.glob("sn/**/*.md") if p.name != "CHANGELOG.md"]
    return sorted(md) + [_REPO / "mcpb" / "manifest.json"]


def test_no_shipped_text_claims_a_retired_store() -> None:
    files = _files()
    assert len(files) > 40, len(files)
    offenders: list[str] = []
    for path in files:
        for lineno, line in enumerate(path.read_text(encoding="utf-8").splitlines(), 1):
            where = f"{path.relative_to(_REPO)}:{lineno}"
            if "FTS5" in line:
                offenders.append(f"{where}: FTS5")
            elif re.search(r"sqlite|chromadb", line, re.IGNORECASE) and not _HISTORY.search(line):
                offenders.append(f"{where}: {line.strip()[:100]}")
    assert not offenders, "\n".join(offenders)
