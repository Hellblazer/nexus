# SPDX-License-Identifier: AGPL-3.0-or-later
"""``nx memory curate`` covers the per-identity T2 projects ``identity/<id>`` (RDR-253 Step 6 part 3, nexus-hczdo).

The 30-day retire rule for the guidance reads transcript ``memory_get`` calls (RDR-238's own measure), not ``access_count``:
MCP ``memory_put`` re-reads each write, so every written note counts as read once. Two things make that measure possible:
``_repo_projects`` lists the ``identity/*`` projects beside the repo's own, and the cold section credits a transcript read
``identity/<id>/<title>`` to the project ``identity/<id>``, not to ``identity``.
"""
from __future__ import annotations

import contextlib
from collections.abc import Iterator
from types import SimpleNamespace

from nexus import memory_curate as mc
from nexus.memory_curate import cold

OLD = "2026-09-02T10:00:00+00:00"
SINCE = "2026-10-21T00:00:00Z"
READ = "2026-11-10T10:00:00Z"


class FakeT2:
    def __init__(self, projects: dict[str, list[dict]]) -> None:
        self.projects = projects

    def get_projects_with_prefix(self, prefix: str) -> list[dict]:
        return [{"project": p} for p in sorted(self.projects) if p.startswith(prefix)]

    def get_all(self, project: str) -> list[dict]:
        return list(self.projects[project])


def row(project: str, title: str, ts: str = OLD) -> dict:
    return {"id": f"{project}/{title}", "project": project, "title": title, "timestamp": ts, "content": "x", "last_accessed": ""}


PROJECTS = {
    "repo": [row("repo", "a")],
    "repo_rdr": [row("repo_rdr", "a")],
    "identity/i5": [row("identity/i5", "open-questions")],
    "identity/i7": [row("identity/i7", "commands")],
    "identityish/x": [row("identityish/x", "a")],       # not under identity/
    "other": [row("other", "a")],
}


def test_repo_projects_lists_the_identity_projects_beside_the_repos_own() -> None:
    assert mc._repo_projects(FakeT2(PROJECTS), "repo") == ["identity/i5", "identity/i7", "repo", "repo_rdr"]


def _cold(rows: list[dict], projects: dict[str, list[dict]] = PROJECTS) -> list[str]:
    q = SimpleNamespace(rows=lambda query: rows, window=SimpleNamespace(since=SINCE))

    @contextlib.contextmanager
    def open_db() -> Iterator[FakeT2]:
        yield FakeT2(projects)

    return cold._t2(q, "repo", open_db)


def _listed(lines: list[str]) -> list[str]:
    return [ln[4:].split(" ")[0] for ln in lines if ln.startswith("  - ")]


def test_a_transcript_read_of_an_identity_title_credits_that_identity_project() -> None:
    lines = _cold([{"kind": "t2_get", "name": "identity/i5/open-questions", "count": 1, "last_use": READ}])
    listed = _listed(lines)
    assert "identity/i5" not in listed, "read in the window: not cold"
    assert "identity/i7" in listed, "no read, no write in the window: cold"
    assert "identity" not in listed


def test_the_split_keeps_a_title_that_holds_slashes_and_the_old_rule_for_other_projects() -> None:
    lines = _cold([{"kind": "t2_get", "name": "identity/i7/a/b/c", "count": 2, "last_use": READ},
                   {"kind": "t2_get", "name": "repo/a", "count": 1, "last_use": READ}])
    listed = _listed(lines)
    assert "identity/i7" not in listed and "repo" not in listed
    assert "identity/i5" in listed and "repo_rdr" in listed


def test_the_listing_says_it_covers_the_identity_projects() -> None:
    head = _cold([{"kind": "t2_get", "name": "repo/a", "count": 1, "last_use": READ}])[0]
    assert head.startswith("T2 namespaces: ") and "identity/*" in head
