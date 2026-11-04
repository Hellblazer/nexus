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

The walk now skips a symlink, and a stored document at a symlinked path is tombstoned in the
same run through the nexus-d6qmz path: on every full run, and on a ``--since-head`` run whose
delta is non-empty (an empty delta returns before the walk). A target inside the repository is
indexed under its own path when the walk admits it. A target outside the repository, or one the
walk filters (a hidden directory, an ignore pattern), is then indexed under neither path. That
is intended: following a link must not admit text the walk would refuse under its real path.
Before the tombstone, a ghost carrying hand-made links or an aspect row is named in a warning,
with its target's tumbler; nothing is moved.

Integration-marked and not an RDR-192 pin file (``tests/test_rdr192_pins_not_integration_marked_lint.py``).
Real git, real catalog; the embedder is the shared local fake.
"""
from __future__ import annotations

import os
from datetime import UTC, datetime
from pathlib import Path

import pytest

from nexus import indexer as idx
from nexus.catalog.store_hook import live_owner_entries
from nexus.db.t2.records import AspectRecord
from nexus.db.t3 import T3Database
from nexus.mcp_infra import t2_ctx
from tests._catalog_fixture_ops import active_reader, documents_by_file_path
from tests.integration.test_a3rl7_since_head_skips_unchanged_delta import _head, _index_delta
from tests.test_cp46b_runfence_repo_staleness import (  # noqa: F401 — fixtures and helpers shared with the fence tests
    _commit,
    _index,
    _init_repo,
    catalog_env,
    git_identity,
    local_t3,
    mock_voyage_client,
)

pytestmark = [pytest.mark.integration]

_TARGETS = {
    "AGENTS.md": b"# Agents\n\nRoot guidance for agents working in this repository.\n",
    "sub/AGENTS.md": b"# Sub agents\n\nGuidance scoped to the sub package.\n",
}
# link path -> link text (relative, as the nexus repo's own CLAUDE.md symlinks are)
_LINKS = {
    "CLAUDE.md": "AGENTS.md",
    "sub/CLAUDE.md": "AGENTS.md",
}
_OUTSIDE_TEXT = b"# Outside\n\nText that lives outside the repository.\n"

# Target classes the walk refuses under the target's own path. Each row: link path, link text,
# the target's repo-relative path (None outside the repository).
_UNINDEXABLE_TARGETS = sorted([
    ("outside", "outside.md", "../outside-target.md", None),
    ("hidden-dir", "hidden.md", ".notes/hidden.md", ".notes/hidden.md"),
    ("ignore-pattern", "vendored.md", "node_modules/pkg/README.md", "node_modules/pkg/README.md"),
])


def _write(repo: Path, rel: str, data: bytes) -> None:
    p = repo / rel
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_bytes(data)


def _seed(repo: Path) -> None:
    for rel, data in _TARGETS.items():
        _write(repo, rel, data)
    for rel, target in _LINKS.items():
        os.symlink(target, repo / rel)
        assert (repo / rel).is_symlink() and (repo / rel).is_file(), rel


def _register_symlinks_unguarded(
    repo: Path, local_t3: T3Database, monkeypatch: pytest.MonkeyPatch, links: list[str],
) -> None:
    """Run 1 with symlink detection off reproduces the pre-fix walk: the links register."""
    with monkeypatch.context() as m:
        m.setattr(Path, "is_symlink", lambda self: False)
        _index(repo, local_t3, monkeypatch)
    for rel in links:
        assert documents_by_file_path(rel), (
            f"{rel} must be registered by the unguarded run, or this test proves nothing"
        )


def _assert_links_absent_targets_present() -> None:
    for rel in _LINKS:
        assert documents_by_file_path(rel) == [], f"{rel} is a symlink and must hold no document"
    for rel in _TARGETS:
        assert documents_by_file_path(rel), f"{rel} is the real file and must stay registered"


class _RecordingLog:
    """Wraps ``nexus.indexer._log`` and records every call (event, level, fields). The
    configured wrapper drops info events before ``structlog.testing.capture_logs`` sees them."""

    def __init__(self, real: object) -> None:
        self._real = real
        self.events: list[dict] = []

    def __getattr__(self, level: str):
        real_method = getattr(self._real, level)

        def record(event: str, *args: object, **kw: object) -> object:
            self.events.append({"event": event, "level": level, **kw})
            return real_method(event, *args, **kw)

        return record

    def named(self, event: str) -> list[dict]:
        return [e for e in self.events if e["event"] == event]


@pytest.fixture
def indexer_log(monkeypatch: pytest.MonkeyPatch) -> _RecordingLog:
    rec = _RecordingLog(idx._log)
    monkeypatch.setattr(idx, "_log", rec)
    return rec


def _summary(log: _RecordingLog) -> list[tuple[int, int]]:
    return [(e["symlinks_skipped"], e["stored_symlink_entries_retired"])
            for e in log.named("symlink_walk_summary")]


def _register_ghosts_with_their_own_text(
    repo: Path, local_t3: T3Database, monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The link paths register as real files with their own text, then become links (the
    2026-04-28 shape of the nexus repo's CLAUDE.md). No live document shares a ghost's
    content_hash, so housekeeping's rename detection cannot retire it: whatever retires it
    in the next run is the same-run tombstone."""
    for rel, data in _TARGETS.items():
        _write(repo, rel, data)
    for rel in _LINKS:
        _write(repo, rel, f"# {rel}\n\nThe text this path held before it became a link.\n".encode())
    _commit(repo)
    _index(repo, local_t3, monkeypatch)
    for rel in _LINKS:
        assert documents_by_file_path(rel), f"{rel} must register as a real file first"
    for rel, target in _LINKS.items():
        (repo / rel).unlink()
        os.symlink(target, repo / rel)
    _commit(repo)


def test_a_symlink_is_never_registered_and_its_target_is(
    tmp_path: Path, catalog_env: Path, local_t3: T3Database, monkeypatch: pytest.MonkeyPatch,
    indexer_log: _RecordingLog,
) -> None:
    repo = tmp_path / "repo"
    _init_repo(repo)
    _seed(repo)
    _commit(repo)

    _index(repo, local_t3, monkeypatch)

    _assert_links_absent_targets_present()
    assert _summary(indexer_log) == [(len(_LINKS), 0)], indexer_log.named("symlink_walk_summary")
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
    indexer_log: _RecordingLog,
) -> None:
    """A full run used to re-walk the symlink, so housekeeping never counted it missing. The
    ghosts hold their own text, so rename detection cannot retire them and housekeeping's miss
    count reaches only 1 this run: the same-run tombstone is what this pins."""
    repo = tmp_path / "repo"
    _init_repo(repo)
    _register_ghosts_with_their_own_text(repo, local_t3, monkeypatch)
    indexer_log.events.clear()

    _index(repo, local_t3, monkeypatch)

    _assert_links_absent_targets_present()
    assert _summary(indexer_log) == [(len(_LINKS), len(_LINKS))], (
        indexer_log.named("symlink_walk_summary"))


def test_a_previously_registered_symlink_is_retired_by_a_since_head_run_that_does_not_name_it(
    tmp_path: Path, catalog_env: Path, local_t3: T3Database, monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The 1.1.589 shape: the target's text changes, the symlink's blob does not, so the delta
    names only the target. The ghost must still be retired in that run (the delta is non-empty;
    an empty one returns before the walk and retires nothing until the next run)."""
    repo = tmp_path / "repo"
    _init_repo(repo)
    _seed(repo)
    _commit(repo)
    _register_symlinks_unguarded(repo, local_t3, monkeypatch, list(_LINKS))
    base = _head(repo)
    (repo / "AGENTS.md").write_bytes(_TARGETS["AGENTS.md"] + b"\nA rule added later.\n")
    _commit(repo)

    phases = _index_delta(repo, local_t3, monkeypatch, base=base)

    assert any(p.startswith("--since-head: 1 changed") for p in phases), (
        f"the delta must name only AGENTS.md, or this test proves nothing: {phases}")
    _assert_links_absent_targets_present()


@pytest.mark.parametrize(
    ("link", "link_text", "target_rel"),
    [row[1:] for row in _UNINDEXABLE_TARGETS],
    ids=[row[0] for row in _UNINDEXABLE_TARGETS],
)
def test_a_symlink_to_a_target_the_walk_refuses_ends_indexed_under_neither_path(
    tmp_path: Path, catalog_env: Path, local_t3: T3Database, monkeypatch: pytest.MonkeyPatch,
    link: str, link_text: str, target_rel: str | None,
) -> None:
    """Intended outcome: the ghost is retired and its text is not indexed under the target's
    path either, because the walk refuses that path (outside the repository, hidden, ignored).
    Following a link must not admit text the walk would refuse under its real path."""
    repo = tmp_path / "repo"
    _init_repo(repo)
    _seed(repo)
    if target_rel is None:
        (tmp_path / "outside-target.md").write_bytes(_OUTSIDE_TEXT)
    else:
        _write(repo, target_rel, b"# Refused\n\nText the walk refuses under its own path.\n")
    os.symlink(link_text, repo / link)
    assert (repo / link).is_symlink() and (repo / link).is_file()
    _commit(repo)
    _register_symlinks_unguarded(repo, local_t3, monkeypatch, [link])
    (ghost,) = documents_by_file_path(link)
    ghost_chashes = [r.chash for r in active_reader().get_manifest(str(ghost.tumbler)) if r.chash]
    assert ghost_chashes, "the ghost must hold chunks, or the 'no longer indexed' check proves nothing"

    _index(repo, local_t3, monkeypatch)

    assert documents_by_file_path(link) == [], f"{link} is a symlink and must hold no document"
    if target_rel is not None:
        assert documents_by_file_path(target_rel) == [], f"{target_rel} is refused by the walk"
    owners = live_owner_entries(ghost.physical_collection, ghost_chashes)
    assert owners is not None
    assert all(held == [] for held in owners.values()), (
        f"the link's text must be indexed under no live document: {owners}")
    for rel in _TARGETS:
        assert documents_by_file_path(rel), f"{rel} is a real file and must stay registered"


def _give_ghost_a_hand_made_link(ghost, target) -> None:
    from nexus.catalog.factory import make_catalog_writer  # noqa: PLC0415 — test-local, mirrors production's deferred import

    make_catalog_writer().link(ghost.tumbler, target.tumbler, "relates", created_by="debugger")


def _give_ghost_an_aspect_row(ghost, target) -> None:
    with t2_ctx() as t2:
        assert t2.document_aspects.upsert(AspectRecord(
            collection=ghost.physical_collection,
            source_path=ghost.file_path,
            problem_formulation="P",
            proposed_method="M",
            confidence=0.9,
            extracted_at=datetime.now(UTC).isoformat(),
            model_version="test",
            extractor_name="test-extractor",
            doc_id=str(ghost.tumbler),
        ))
        assert t2.document_aspects.get_by_doc_id(str(ghost.tumbler)) is not None


@pytest.mark.parametrize("carries", ["aspect", "hand-made-link", "nothing"])
def test_a_ghost_that_carries_a_link_or_an_aspect_is_named_before_it_is_retired(
    tmp_path: Path, catalog_env: Path, local_t3: T3Database, monkeypatch: pytest.MonkeyPatch,
    indexer_log: _RecordingLog, carries: str,
) -> None:
    """The warning names the ghost's tumbler and the target's; the ghost is retired anyway and
    nothing moves to the target. Path links the indexer made itself (filepath_extractor) do not
    count, so a ghost carrying nothing else retires silently. The ghosts hold their own text:
    a ghost whose text equals its target's is retired by housekeeping's rename branch first,
    which DOES copy its links onto the target (pre-existing; reported on nexus-gnw42)."""
    repo = tmp_path / "repo"
    _init_repo(repo)
    _register_ghosts_with_their_own_text(repo, local_t3, monkeypatch)
    (ghost,) = documents_by_file_path("CLAUDE.md")
    (target,) = documents_by_file_path("AGENTS.md")
    if carries == "aspect":
        _give_ghost_an_aspect_row(ghost, target)
    elif carries == "hand-made-link":
        _give_ghost_a_hand_made_link(ghost, target)

    indexer_log.events.clear()

    _index(repo, local_t3, monkeypatch)

    _assert_links_absent_targets_present()
    warned = indexer_log.named("symlink_ghost_retired_with_dependents")
    if carries == "nothing":
        assert warned == [], warned
        return
    assert [(e["ghost_tumbler"], e["target_tumbler"]) for e in warned] == [
        (str(ghost.tumbler), str(target.tumbler))], warned
    assert warned[0]["has_aspects"] is (carries == "aspect")
    assert (warned[0]["links"] > 0) is (carries == "hand-made-link")
    moved = [lnk for lnk in active_reader().links_from(target.tumbler) if lnk.created_by == "debugger"]
    assert moved == [], f"nothing may be moved onto the target: {moved}"
    with t2_ctx() as t2:
        assert t2.document_aspects.get_by_doc_id(str(target.tumbler)) is None, (
            "nothing may be moved onto the target")
