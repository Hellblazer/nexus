# SPDX-License-Identifier: AGPL-3.0-or-later
"""nexus-vayt7: the repo-path staleness cache must take a document's
content hash from the catalog fence, not from chunk metadata.

Chunks are content-addressed (RDR-180): one T3 row per distinct chunk
text per collection. A chash shared by two files carries whichever
writer stamped it first, and a chash re-upserted for a changed file
keeps its original metadata (RDR-181 embed-skip never rewrites the
row). ``build_staleness_cache`` used to take the LAST chunk's
``content_hash`` per resolved doc, so on the nexus checkout 536 of 1818
code docs read as changed on every run while the catalog said
``index_state='complete'`` with the current hash for 2114 of 2122.
Measured 2026-09-04: ~650 of 2370 unchanged files re-embedded and
re-uploaded per post-commit run, ~10 minutes each.
"""
from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import MagicMock, patch

import pytest

from nexus.indexer_utils import (
    StalenessCache,
    apply_catalog_content_hashes,
    build_staleness_cache,
    check_staleness,
    complete_doc_hashes_for,
)


def _col(metadatas: list[dict]) -> MagicMock:
    col = MagicMock(spec=["get", "get_all_metadata", "name"])
    col.name = "code__x__model-a__v1"
    col.get_all_metadata.return_value = {
        "ids": [f"c{i}" for i in range(len(metadatas))],
        "metadatas": metadatas,
    }
    return col


class TestSharedChashPresence:
    def test_every_doc_a_shared_chash_maps_to_is_present(self) -> None:
        """A doc whose only chunks are shared with another doc must still
        be PRESENT in ``by_doc_id`` (it has chunks in T3), so the catalog
        overlay below can give it its own hash. Pre-fix the resolver kept
        ``sorted(doc_ids)[0]`` only, so the other doc was invisible: a
        cache miss, re-indexed on every run."""
        col = _col([
            {"chunk_text_hash": "shared", "content_hash": "hash-a", "embedding_model": "model-a"},
        ])
        fake_cat = MagicMock()
        fake_cat.docs_for_chashes.return_value = {"shared": ["1.1.1", "1.1.2"]}
        with patch("nexus.catalog.factory.make_catalog_reader", return_value=fake_cat):
            cache = build_staleness_cache(col)
        assert set(cache.by_doc_id) == {"1.1.1", "1.1.2"}
        assert cache.by_doc_id["1.1.1"] == ("hash-a", "model-a")

    @pytest.mark.parametrize("shared_first", [True, False])
    def test_unique_chunk_value_is_not_overwritten_by_shared_chunk(
        self, shared_first: bool,
    ) -> None:
        """A doc's own (unique) chunk value wins over a shared chunk's
        first-writer value, whatever order the sweep returns rows in."""
        rows = [
            {"chunk_text_hash": "shared", "content_hash": "hash-other", "embedding_model": "model-a"},
            {"chunk_text_hash": "own-2", "content_hash": "hash-2", "embedding_model": "model-a"},
        ]
        col = _col(rows if shared_first else rows[::-1])
        fake_cat = MagicMock()
        fake_cat.docs_for_chashes.return_value = {
            "shared": ["1.1.1", "1.1.2"], "own-2": ["1.1.2"],
        }
        with patch("nexus.catalog.factory.make_catalog_reader", return_value=fake_cat):
            cache = build_staleness_cache(col)
        assert cache.by_doc_id["1.1.2"] == ("hash-2", "model-a")


class TestCatalogOverlay:
    def test_complete_fence_hash_replaces_chunk_metadata_hash(self) -> None:
        cache = StalenessCache(by_doc_id={"1.1.1": ("stale-chunk-hash", "model-a")})
        applied = apply_catalog_content_hashes(cache, {"1.1.1": "current"})
        assert applied == 1
        assert cache.by_doc_id["1.1.1"] == ("current", "model-a")
        assert check_staleness(
            MagicMock(), "a.py", "current", "model-a", doc_id="1.1.1", cache=cache,
        ) is True

    def test_doc_absent_from_t3_stays_a_miss(self) -> None:
        """The catalog may say 'complete' for a doc whose chunks are gone
        (collection recreated, chunks reaped). The overlay corrects hashes
        for docs that HAVE chunks; it never invents presence, so the
        ghost-heal property of a cache miss survives."""
        cache = StalenessCache(by_doc_id={})
        applied = apply_catalog_content_hashes(cache, {"1.1.9": "current"})
        assert applied == 0
        assert "1.1.9" not in cache.by_doc_id
        assert check_staleness(
            MagicMock(), "a.py", "current", "model-a", doc_id="1.1.9", cache=cache,
        ) is False

    def test_never_fresh_still_wins_over_a_matching_fence_hash(self) -> None:
        cache = StalenessCache(
            by_doc_id={"1.1.1": ("x", "model-a")},
            never_fresh=frozenset({"1.1.1"}),
        )
        apply_catalog_content_hashes(cache, {"1.1.1": "current"})
        assert check_staleness(
            MagicMock(), "a.py", "current", "model-a", doc_id="1.1.1", cache=cache,
        ) is False

    def test_empty_overlay_is_a_no_op(self) -> None:
        cache = StalenessCache(by_doc_id={"1.1.1": ("h", "m")})
        assert apply_catalog_content_hashes(cache, {}) == 0
        assert apply_catalog_content_hashes(cache, None) == 0
        assert cache.by_doc_id == {"1.1.1": ("h", "m")}


# ── end to end: chunk metadata carries a stale hash, catalog says complete ──
#
# Fixtures are the cp46b runfence journey's (real engine catalog via the
# suite substrate, fake local T3 for chunk content). Imported names are
# picked up by pytest as fixtures in this module's namespace.
from pathlib import Path  # noqa: E402

from nexus.db.t3 import T3Database  # noqa: E402
from tests.test_cp46b_runfence_repo_staleness import (  # noqa: E402, F401
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


class TestStaleChunkMetadataDoesNotForceReindex:
    def test_complete_doc_skipped_when_chunk_metadata_hash_is_stale(
        self, tmp_path: Path, catalog_env: Path, local_t3: T3Database,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """The live shape: a doc fenced 'complete' with the current hash
        whose T3 chunks carry another hash in their metadata (a shared
        chash's first writer, or the doc's own pre-change row kept by
        embed-skip). A normal run must NOT re-embed it."""
        repo = tmp_path / "repo"
        _init_repo(repo)
        _prime_owner(repo, local_t3, monkeypatch)

        (repo / "healthy.md").write_text(
            "# Healthy\n\nStable content.\n", encoding="utf-8",
        )
        _commit(repo)
        _index(repo, local_t3, monkeypatch)
        entry = _entry_ending_with("healthy.md")
        assert entry.index_state == "complete"
        assert entry.index_content_hash

        # Rewrite the chunk-metadata hash the way the store leaves it
        # after a shared or embed-skipped upsert: not this doc's hash.
        col = local_t3.get_collection(entry.physical_collection)
        rows = col.get(include=["metadatas"])
        touched = [
            (cid, {**m, "content_hash": "first-writer-of-a-shared-chash"})
            for cid, m in zip(rows["ids"], rows["metadatas"])
            if m.get("content_hash") == entry.index_content_hash
        ]
        assert touched, "the doc must have at least one chunk carrying its hash"
        col.update(ids=[c for c, _ in touched], metadatas=[m for _, m in touched])

        call_order = _wrap_fence_helpers(monkeypatch)
        _index(repo, local_t3, monkeypatch)

        assert not [c for c in call_order if c[0] == "begin"], (
            "a 'complete' doc whose only disagreement is chunk-metadata "
            f"content_hash must be skipped by a normal run: {call_order}"
        )


class TestCompleteDocHashesFor:
    def test_sweep_fills_docs_outside_the_hook_map(self) -> None:
        cat = MagicMock()
        cat.resolve_many.return_value = {
            "1.2.7": SimpleNamespace(tumbler="1.2.7", index_state="complete", index_content_hash="h7", index_state_reported=True),
            "1.2.8": SimpleNamespace(tumbler="1.2.8", index_state="indexing", index_content_hash="h8", index_state_reported=True),
            "1.2.9": SimpleNamespace(tumbler="1.2.9", index_state="complete", index_content_hash="", index_state_reported=True),
        }
        out = complete_doc_hashes_for(
            cat, {"1.1.1", "1.2.7", "1.2.8", "1.2.9"}, known={"1.1.1": "hook"},
        )
        assert out == {"1.1.1": "hook", "1.2.7": "h7"}
        # O(wanted): only the ids outside the hook map are resolved.
        cat.resolve_many.assert_called_once_with(["1.2.7", "1.2.8", "1.2.9"])

    def test_nothing_wanted_means_no_sweep(self) -> None:
        cat = MagicMock()
        assert complete_doc_hashes_for(cat, {"1.1.1"}, known={"1.1.1": "hook"}) == {"1.1.1": "hook"}
        cat.resolve_many.assert_not_called()

    def test_sweep_failure_keeps_known_and_does_not_raise(self) -> None:
        cat = MagicMock()
        cat.resolve_many.side_effect = RuntimeError("engine away")
        assert complete_doc_hashes_for(cat, {"1.2.7"}, known={"1.1.1": "hook"}) == {"1.1.1": "hook"}
        assert complete_doc_hashes_for(None, {"1.2.7"}) == {}
