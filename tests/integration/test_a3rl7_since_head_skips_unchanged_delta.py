# SPDX-License-Identifier: AGPL-3.0-or-later
"""nexus-a3rl7 on the real engine: ``index_repository(since_head=True)`` skips a delta file the
catalog already holds at its current hash, and still indexes one that changed.

The delta (``git diff`` since the owner's stored base) is the files whose content differs from
the BASE, not from the last index. A base that did not advance (an earlier run held it) offers
files a later run already indexed. The delta path used to hand ``check_staleness`` an empty
``StalenessCache``, a cached branch with nothing in it, so each of those was a miss and
re-indexed. The caches are now built from the catalog for the delta's documents
(``indexer_utils.seed_staleness_cache_from_catalog``), without a whole-collection sweep.

The base is pinned by patching ``_get_owner_head_hash`` to an earlier commit; everything else is
the real run: real git, the real catalog pass, the real staleness decision. The fence-begin
helper is wrapped to record which documents a run started real work on, the same observation the
fence staleness tests make (``tests/test_cp46b_runfence_repo_staleness.py``).

Integration-marked and not an RDR-192 pin file (``tests/test_rdr192_pins_not_integration_marked_lint.py``).
"""
from __future__ import annotations

import subprocess
from pathlib import Path
from unittest.mock import patch

import pytest

from nexus.db.t3 import T3Database
from nexus.registry import RepoRegistry
from tests.conftest import fake_credentials
from tests.test_cp46b_runfence_repo_staleness import (  # noqa: F401 — fixtures and helpers shared with the fence tests
    _commit,
    _entry_ending_with,
    _index,
    _init_repo,
    _prime_owner,
    _wrap_fence_helpers,
    catalog_env,
    git_identity,
    local_t3,
    mock_voyage_client,
)

pytestmark = [pytest.mark.integration]


def _head(repo: Path) -> str:
    return subprocess.run(
        ["git", "rev-parse", "HEAD"], cwd=repo, check=True, capture_output=True, text=True,
    ).stdout.strip()


def _index_delta(
    repo: Path, t3: T3Database, monkeypatch: pytest.MonkeyPatch, *, base: str,
) -> list[str]:
    """``index_repository(since_head=True)`` against *base* as the owner's stored head hash.
    Writes are FK-seeded exactly as ``_index`` does. Returns the run's phase lines, so a test
    can say the delta it ran was the one it meant (``--since-head: N changed, ...``)."""
    from nexus import indexer as idx
    from nexus.indexer import index_repository
    from tests._catalog_fixture_ops import seed_manifest_chunks

    monkeypatch.setenv("NX_LOCAL", "1")
    monkeypatch.setattr(idx, "_get_owner_head_hash", lambda r: base)
    reg = RepoRegistry(repo.parent / "repos.json")
    reg.add(repo)

    orig_write_batch = t3._write_batch

    def _seeding_write_batch(col, collection_name, ids, documents, metadatas,
                             embeddings=None, **kwargs):
        orig_write_batch(col, collection_name, ids, documents, metadatas, embeddings, **kwargs)
        seed_manifest_chunks(collection_name, ids)

    monkeypatch.setattr(t3, "_write_batch", _seeding_write_batch)

    phases: list[str] = []
    with patch("nexus.db.make_t3", return_value=t3), \
            patch("nexus.config.get_credential", side_effect=fake_credentials()):
        index_repository(repo, reg, force=False, since_head=True, on_phase=phases.append)
    return phases


def test_a_delta_file_the_catalog_holds_at_its_current_hash_is_skipped(
    tmp_path: Path, catalog_env: Path, local_t3: T3Database, monkeypatch: pytest.MonkeyPatch,
) -> None:
    repo = tmp_path / "repo"
    _init_repo(repo)
    _prime_owner(repo, local_t3, monkeypatch)
    (repo / "first.md").write_text("# First\n\nIndexed at the base.\n", encoding="utf-8")
    _commit(repo)
    base = _head(repo)
    (repo / "second.md").write_text("# Second\n\nCommitted after the base.\n", encoding="utf-8")
    _commit(repo)

    # A full run indexes both. The owner's stored base stays `base` (patched below): the delta
    # since it names second.md, which is already indexed at its current hash.
    _index(repo, local_t3, monkeypatch)
    entry = _entry_ending_with("second.md")
    assert entry.index_state == "complete"
    tumbler = str(entry.tumbler)

    call_order = _wrap_fence_helpers(monkeypatch)
    phases = _index_delta(repo, local_t3, monkeypatch, base=base)

    assert any(p.startswith("--since-head: 1 changed") for p in phases), (
        f"the delta must name second.md, or this test proves nothing: {phases}")
    assert call_order == [], (
        "second.md is in the delta but unchanged since it was indexed: the catalog holds it "
        f"'complete' at its current hash, so the run must not begin real work on it: {call_order}"
    )
    after = _entry_ending_with("second.md")
    assert after.index_state == "complete" and after.index_content_hash == entry.index_content_hash
    assert str(after.tumbler) == tumbler


def test_a_delta_file_that_changed_is_still_indexed(
    tmp_path: Path, catalog_env: Path, local_t3: T3Database, monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The delta's purpose is untouched: a file whose content changed after its last index is
    indexed and its fence moves to the new hash."""
    repo = tmp_path / "repo"
    _init_repo(repo)
    _prime_owner(repo, local_t3, monkeypatch)
    (repo / "moving.md").write_text("# Moving\n\nVersion one.\n", encoding="utf-8")
    _commit(repo)
    _index(repo, local_t3, monkeypatch)
    base = _head(repo)
    before = _entry_ending_with("moving.md")
    assert before.index_state == "complete"

    (repo / "moving.md").write_text("# Moving\n\nVersion two, different bytes.\n", encoding="utf-8")
    _commit(repo)
    call_order = _wrap_fence_helpers(monkeypatch)
    phases = _index_delta(repo, local_t3, monkeypatch, base=base)

    assert any(p.startswith("--since-head: 1 changed") for p in phases), phases
    assert [c for c in call_order if c[0] == "begin"], f"the changed file must be indexed: {call_order}"
    after = _entry_ending_with("moving.md")
    assert after.index_state == "complete"
    assert after.index_content_hash != before.index_content_hash


def test_a_delta_file_stranded_mid_run_is_re_indexed_even_at_an_unchanged_hash(
    tmp_path: Path, catalog_env: Path, local_t3: T3Database, monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The fence still outranks a seeded entry: a document fenced 'failed' is never skipped."""
    repo = tmp_path / "repo"
    _init_repo(repo)
    _prime_owner(repo, local_t3, monkeypatch)
    (repo / "first.md").write_text("# First\n\nAt the base.\n", encoding="utf-8")
    _commit(repo)
    base = _head(repo)
    (repo / "stranded.md").write_text("# Stranded\n\nAfter the base.\n", encoding="utf-8")
    _commit(repo)
    _index(repo, local_t3, monkeypatch)
    entry = _entry_ending_with("stranded.md")

    from nexus.catalog.factory import make_catalog_writer

    make_catalog_writer().fail_index_run(str(entry.tumbler), "a3rl7: simulated failure")
    assert _entry_ending_with("stranded.md").index_state == "failed"

    call_order = _wrap_fence_helpers(monkeypatch)
    phases = _index_delta(repo, local_t3, monkeypatch, base=base)

    assert any(p.startswith("--since-head: 1 changed") for p in phases), phases
    assert [c for c in call_order if c[0] == "begin" and c[1] == str(entry.tumbler)], call_order
    assert _entry_ending_with("stranded.md").index_state == "complete"
