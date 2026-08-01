# SPDX-License-Identifier: AGPL-3.0-or-later
"""nexus-39upx: sweep T3 rows a re-index leaves unreferenced.

A re-index that changes the extracted text writes chunks under NEW chashes —
content addressing working correctly — and atomic_manifest_replace repoints the
manifest at them. Nothing removed the OLD vector rows, so they persisted,
referenced by no manifest and still returned by vector search, which reads T3
directly rather than the manifest. Measured on tumbler 1.14.19 after the
nexus-gtltb normalizer fix: manifest 182 rows / 163 unique chashes, T3 180 rows,
17 of them referenced by nothing.
"""
from __future__ import annotations

from unittest.mock import MagicMock, patch

from nexus.mcp_infra import _sweep_superseded_vectors


def _chunks(*chashes: str) -> list[dict]:
    return [{"chash": h, "position": i} for i, h in enumerate(chashes)]


def _cat(refs: dict[str, list[str]]) -> MagicMock:
    cat = MagicMock()
    cat.docs_for_chashes.return_value = refs
    return cat


def test_superseded_chunks_are_deleted() -> None:
    cat = _cat({"old1": ["doc-A"], "old2": ["doc-A"]})
    col = MagicMock()
    with patch("nexus.db.make_t3", return_value=MagicMock(
            get_collection=MagicMock(return_value=col))):
        _sweep_superseded_vectors(cat, "doc-A", {"old1", "old2", "keep"},
                                  _chunks("keep", "new1"), "coll")
    col.delete.assert_called_once()
    assert sorted(col.delete.call_args.kwargs["ids"]) == ["old1", "old2"]


def test_chunk_shared_with_another_document_is_NOT_deleted() -> None:
    """THE guard. Identical chunk text collapses to ONE T3 row shared by every
    document containing it, so "not in THIS manifest" is not "unreferenced".
    Deleting on that basis removes chunks other documents still depend on."""
    cat = _cat({"shared": ["doc-A", "doc-B"], "mine": ["doc-A"]})
    col = MagicMock()
    with patch("nexus.db.make_t3", return_value=MagicMock(
            get_collection=MagicMock(return_value=col))):
        _sweep_superseded_vectors(cat, "doc-A", {"shared", "mine"},
                                  _chunks("new"), "coll")
    assert col.delete.call_args.kwargs["ids"] == ["mine"], "shared row must survive"


def test_nothing_dropped_means_no_delete_call() -> None:
    cat = _cat({})
    col = MagicMock()
    with patch("nexus.db.make_t3", return_value=MagicMock(
            get_collection=MagicMock(return_value=col))):
        _sweep_superseded_vectors(cat, "doc-A", {"a", "b"}, _chunks("a", "b"), "coll")
    col.delete.assert_not_called()


def test_reverse_lookup_failure_deletes_NOTHING() -> None:
    """Fail-open. A sweep that cannot prove orphanhood must not guess —
    over-retention is recoverable, over-deletion is not."""
    cat = MagicMock()
    cat.docs_for_chashes.side_effect = RuntimeError("engine down")
    col = MagicMock()
    with patch("nexus.db.make_t3", return_value=MagicMock(
            get_collection=MagicMock(return_value=col))):
        _sweep_superseded_vectors(cat, "doc-A", {"old"}, _chunks("new"), "coll")
    col.delete.assert_not_called()


def test_missing_collection_is_a_noop() -> None:
    cat = _cat({"old": ["doc-A"]})
    with patch("nexus.db.make_t3") as mk:
        _sweep_superseded_vectors(cat, "doc-A", {"old"}, _chunks("new"), None)
    mk.assert_not_called()


def test_delete_failure_does_not_raise_into_the_index() -> None:
    """Cleanup must never fail the indexing operation that triggered it."""
    cat = _cat({"old": ["doc-A"]})
    col = MagicMock()
    col.delete.side_effect = RuntimeError("t3 unreachable")
    with patch("nexus.db.make_t3", return_value=MagicMock(
            get_collection=MagicMock(return_value=col))):
        _sweep_superseded_vectors(cat, "doc-A", {"old"}, _chunks("new"), "coll")


def test_unreferenced_chash_with_empty_ref_list_is_deleted() -> None:
    """docs_for_chashes may omit a chash entirely, or map it to []."""
    cat = _cat({"gone": []})
    col = MagicMock()
    with patch("nexus.db.make_t3", return_value=MagicMock(
            get_collection=MagicMock(return_value=col))):
        _sweep_superseded_vectors(cat, "doc-A", {"gone", "absent"},
                                  _chunks("new"), "coll")
    assert sorted(col.delete.call_args.kwargs["ids"]) == ["absent", "gone"]
