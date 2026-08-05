# SPDX-License-Identifier: AGPL-3.0-or-later
"""nexus-tp8yk D3 substantive-critic SIGNIFICANT (nexus-tp8yk-substantive-
critique-2026-08-04): the union guard on all four T3-deleting prune sites
used to gate ``orphaned_chashes`` on ``if doc_id:`` — falling back to an
UNCONDITIONAL delete whenever ``_register_or_lookup_doc_id`` returned "".
That function's own docstring is explicit that "" means "only when an
unexpected error occurs" (best-effort), not only "the catalog is genuinely
absent" — so a TRANSIENT registration failure on an otherwise-healthy
catalog silently degraded to the exact unguarded delete this bead exists
to close (the P2 incident class: a chash another live document still
depends on gets removed because THIS run couldn't identify itself).

``nexus.indexer_utils.prune_orphan_candidates`` closes the gap by making
the "no catalog to guard against" decision on the READER's own
availability, never on the doc_id string alone. These tests pin both
branches directly (fast, no real engine needed) — the real-engine wiring
lives in the four production call sites' own tests (doc_indexer.py /
pipeline_stages.py / tests/db/test_http_catalog_integration.py).
"""
from __future__ import annotations

from unittest.mock import MagicMock, patch

from nexus.indexer_utils import prune_orphan_candidates


def test_empty_candidates_short_circuits_without_touching_the_catalog():
    with patch("nexus.catalog.factory.make_catalog_reader") as mk:
        assert prune_orphan_candidates("1.2.3", []) == []
    mk.assert_not_called()


def test_no_reader_at_all_preserves_legacy_unconditional_delete():
    """Genuinely no catalog (uninitialized/absent) — nothing tracks
    cross-document manifest sharing in that state, so there is no "other
    document" a delete could be wrong about. Pre-tp8yk behaviour."""
    with patch("nexus.catalog.factory.make_catalog_reader", return_value=None):
        result = prune_orphan_candidates("", ["a" * 64, "b" * 64])
    assert sorted(result) == sorted(["a" * 64, "b" * 64])


def test_reader_construction_failure_is_treated_as_no_reader():
    """A raising factory (e.g. a transient connection error building the
    reader itself) must degrade the SAME way as `reader is None` — fail
    toward the only behaviour that has no "other document" to protect."""
    with patch(
        "nexus.catalog.factory.make_catalog_reader",
        side_effect=RuntimeError("engine unreachable"),
    ):
        result = prune_orphan_candidates("", ["a" * 64])
    assert result == ["a" * 64]


def test_reader_available_but_empty_doc_id_still_runs_the_union_guard():
    """THE fix (substantive-critic SIGNIFICANT): a TRANSIENT
    _register_or_lookup_doc_id failure on an otherwise-healthy catalog
    must NOT silently degrade to unconditional delete. A candidate
    referenced by some OTHER live document is kept even with doc_id="";
    a candidate referenced by nothing is still correctly deleted.
    """
    shared = "a" * 64
    exclusive = "b" * 64
    reader = MagicMock()
    reader.docs_for_chashes.return_value = {shared: ["1.9.9"]}  # exclusive: unreferenced
    with patch("nexus.catalog.factory.make_catalog_reader", return_value=reader):
        result = prune_orphan_candidates("", [shared, exclusive])
    assert exclusive in result, (
        "an unreferenced candidate must still be identified as orphaned "
        f"when the catalog is healthy but doc_id is empty — got {result}"
    )
    assert shared not in result, (
        "a candidate another live document references must survive even "
        f"with doc_id='' — deleting it would violate the union guard's "
        f"whole purpose — got {result}"
    )
    reader.docs_for_chashes.assert_called_once()


def test_reader_available_with_real_doc_id_excludes_self():
    """Sanity: the normal (non-empty doc_id) path still self-excludes —
    finding *doc_id* itself in the reverse lookup is not evidence of
    sharing."""
    chash = "c" * 64
    reader = MagicMock()
    reader.docs_for_chashes.return_value = {chash: ["1.2.3"]}
    with patch("nexus.catalog.factory.make_catalog_reader", return_value=reader):
        result = prune_orphan_candidates("1.2.3", [chash])
    assert result == [chash]


def test_reader_read_failure_keeps_everything():
    """Fail-open, not fail-delete: docs_for_chashes raising must keep
    every candidate, matching orphaned_chashes' own contract."""
    reader = MagicMock()
    reader.docs_for_chashes.side_effect = RuntimeError("truncated page")
    with patch("nexus.catalog.factory.make_catalog_reader", return_value=reader):
        result = prune_orphan_candidates("1.2.3", ["d" * 64])
    assert result == []
