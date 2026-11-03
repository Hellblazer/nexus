# SPDX-License-Identifier: AGPL-3.0-or-later
"""nexus-gnw42 on the real engine: a tracked symlink gets no catalog document of its own.

``git ls-files`` lists a symlink as a path, and ``Path.is_file()`` follows it, so the walk
indexed ``CLAUDE.md -> AGENTS.md`` as a second document holding the target's text. Nothing
retired it afterwards. A ``--since-head`` run (the post-commit hook) sees only the symlink's
own blob, which never changes when the target's text does, and skips housekeeping. A full run
re-walks the symlink, so housekeeping never counts it missing. Measured on the nexus tenant
2026-11-03: 1.1.589 ``CLAUDE.md`` (58 chunks, indexed 2026-09-28) was still served beside
1.1.620 ``AGENTS.md`` (17 chunks, current). The second live owner of a chunk is also what made
``owner_titles`` label ``tests/AGENTS.md`` chunks "AGENTS.md · CLAUDE.md".

The walk now skips a symlink (its target, when tracked, is indexed under its own path), and a
stored document at a symlinked path is tombstoned in the same run through the nexus-d6qmz path,
full and ``--since-head``.

Integration-marked and not an RDR-192 pin file (``tests/test_rdr192_pins_not_integration_marked_lint.py``).
Real git, real catalog; the embedder is the shared local fake.
"""
from __future__ import annotations

import os
from pathlib import Path

import pytest

from nexus.catalog.store_hook import live_owner_entries
from nexus.db.t3 import T3Database
from tests._catalog_fixture_ops import active_reader, documents_by_file_path
from tests.test_cp46b_runfence_repo_staleness import (  # noqa: F401 — fixtures and helpers shared with the fence tests
    _commit,
    _index,
    _init_repo,
    catalog_env,
    git_identity,
    local_t3,
    mock_voyage_client,
)
from tests.integration.test_a3rl7_since_head_skips_unchanged_delta import _head, _index_delta

pytestmark = [pytest.mark.integration]

_TARGETS = {
    "AGENTS.md": b"# Agents\n\nRoot guidance for agents working in this repository.\n",
    "sub/AGENTS.md": b"# Sub agents\n\nGuidance scoped to the sub package.\n",
}
# link path -> link text (relative, as the nexus repo's own CLAUDE.md symlinks are)
_LINKS = {
    "CLAUDE.md": "AGENTS.md",
    "sub/CLAUDE.md": "AGENTS.md",
    "outside.md": "../outside-target.md",  # points out of the repository
}


def _seed(repo: Path) -> None:
    for rel, data in _TARGETS.items():
        p = repo / rel
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_bytes(data)
    (repo.parent / "outside-target.md").write_bytes(
        b"# Outside\n\nText that lives outside the repository.\n")
    for rel, target in _LINKS.items():
        os.symlink(target, repo / rel)
    for rel in _LINKS:
        assert (repo / rel).is_symlink() and (repo / rel).is_file(), rel


def _register_symlinks_unguarded(
    repo: Path, local_t3: T3Database, monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Run 1 with symlink detection off reproduces the pre-fix walk: the links register."""
    with monkeypatch.context() as m:
        m.setattr(Path, "is_symlink", lambda self: False)
        _index(repo, local_t3, monkeypatch)
    for rel in _LINKS:
        assert documents_by_file_path(rel), (
            f"{rel} must be registered by the unguarded run, or this test proves nothing"
        )


def _assert_links_absent_targets_present() -> None:
    for rel in _LINKS:
        assert documents_by_file_path(rel) == [], f"{rel} is a symlink and must hold no document"
    for rel in _TARGETS:
        assert documents_by_file_path(rel), f"{rel} is the real file and must stay registered"


def test_a_symlink_is_never_registered_and_its_target_is(
    tmp_path: Path, catalog_env: Path, local_t3: T3Database, monkeypatch: pytest.MonkeyPatch,
) -> None:
    repo = tmp_path / "repo"
    _init_repo(repo)
    _seed(repo)
    _commit(repo)

    _index(repo, local_t3, monkeypatch)

    _assert_links_absent_targets_present()
    # The title label joins the live owners of a chunk: the target's chunks must have one.
    (entry,) = documents_by_file_path("AGENTS.md")
    chashes = [r.chash for r in active_reader().get_manifest(str(entry.tumbler)) if r.chash]
    assert chashes, "AGENTS.md must have manifest rows, or the owner check proves nothing"
    owners = live_owner_entries(entry.physical_collection, chashes)
    assert owners is not None
    for chash, held in owners.items():
        assert [e.file_path for e in held] == ["AGENTS.md"], (chash, [e.file_path for e in held])


def test_a_previously_registered_symlink_is_retired_in_the_same_full_run(
    tmp_path: Path, catalog_env: Path, local_t3: T3Database, monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A full run used to re-walk the symlink, so housekeeping never counted it missing."""
    repo = tmp_path / "repo"
    _init_repo(repo)
    _seed(repo)
    _commit(repo)
    _register_symlinks_unguarded(repo, local_t3, monkeypatch)

    _index(repo, local_t3, monkeypatch)

    _assert_links_absent_targets_present()


def test_a_previously_registered_symlink_is_retired_by_a_since_head_run_that_does_not_name_it(
    tmp_path: Path, catalog_env: Path, local_t3: T3Database, monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The 1.1.589 shape: the target's text changes, the symlink's blob does not, so the delta
    names only the target. The ghost must still be retired in that run."""
    repo = tmp_path / "repo"
    _init_repo(repo)
    _seed(repo)
    _commit(repo)
    _register_symlinks_unguarded(repo, local_t3, monkeypatch)
    base = _head(repo)
    (repo / "AGENTS.md").write_bytes(_TARGETS["AGENTS.md"] + b"\nA rule added later.\n")
    _commit(repo)

    phases = _index_delta(repo, local_t3, monkeypatch, base=base)

    assert any(p.startswith("--since-head: 1 changed") for p in phases), (
        f"the delta must name only AGENTS.md, or this test proves nothing: {phases}")
    _assert_links_absent_targets_present()
