# SPDX-License-Identifier: AGPL-3.0-or-later
"""nexus-hg2dw: unit coverage for ``nexus.indexer._reconcile_needs_fence``
(point 2, fail-stamp anything registered but unflushed at run exit).
Pure orchestration around the EXISTING ``doc_indexer`` fence primitives,
so these tests mock those primitives and assert the call shape rather
than standing up a live catalog.

Critique round 2 (T2 critique-nexus-hg2dw-36602c67f [24598] finding 1
CRITICAL) reverted the original design's whole-run-upfront
``_fence_begin_needs_fence`` helper — it enlarged an uncatchable kill's
blast radius from the incident's small in-flight batch to the entire
run. Per-file begin now lives in each producer (see
tests/test_indexer_modules.py's
``test_index_{prose,code}_file_begins_fence_before_chunking``); this
file covers only the exit-time reconciliation half that remains.
"""
from __future__ import annotations

from unittest.mock import MagicMock, patch

from nexus.indexer import _reconcile_needs_fence


class TestReconcileNeedsFence:
    def test_empty_needs_fence_is_a_no_op_no_reader_call(self) -> None:
        with patch("nexus.catalog.factory.make_catalog_reader") as make_reader:
            _reconcile_needs_fence({"needs_fence": {}, "owner": "1.1"})
        make_reader.assert_not_called()

    def test_fail_stamps_only_non_complete_docs(self) -> None:
        needs_fence = {
            "1.1.1": ("h1", "docs__repo__voyage-context-3__v1"),  # completed this run
            "1.1.2": ("h2", "docs__repo__voyage-context-3__v1"),  # still 'indexing'
            "1.1.3": ("h3", "docs__repo__voyage-context-3__v1"),  # never touched (missing)
        }

        entry_complete = MagicMock(tumbler="1.1.1", index_state="complete")
        entry_indexing = MagicMock(tumbler="1.1.2", index_state="indexing")
        # 1.1.3 deliberately absent from by_owner()'s result.

        reader = MagicMock()
        reader.by_owner.return_value = [entry_complete, entry_indexing]

        with patch("nexus.catalog.factory.make_catalog_reader", return_value=reader), \
             patch("nexus.doc_indexer._fence_fail") as fence_fail:
            _reconcile_needs_fence({"needs_fence": needs_fence, "owner": "1.1"})

        failed_doc_ids = {call.args[0] for call in fence_fail.call_args_list}
        assert failed_doc_ids == {"1.1.2", "1.1.3"}

    def test_all_complete_fails_nothing(self) -> None:
        needs_fence = {"1.1.1": ("h1", "docs__repo__voyage-context-3__v1")}
        entry = MagicMock(tumbler="1.1.1", index_state="complete")
        reader = MagicMock()
        reader.by_owner.return_value = [entry]

        with patch("nexus.catalog.factory.make_catalog_reader", return_value=reader), \
             patch("nexus.doc_indexer._fence_fail") as fence_fail:
            _reconcile_needs_fence({"needs_fence": needs_fence, "owner": "1.1"})

        fence_fail.assert_not_called()

    def test_already_failed_doc_is_not_re_failed(self) -> None:
        """A doc an in-loop handler (e.g. an upload-exception fence-fail)
        already resolved to 'failed' this run must not be re-failed —
        harmless state-wise, but a needless duplicate write, and it broke
        a call-count assertion in tests/db/test_bhlfy_runfence_fail_arms.py
        before this exclusion was added."""
        needs_fence = {"1.1.1": ("h1", "docs__repo__voyage-context-3__v1")}
        entry = MagicMock(tumbler="1.1.1", index_state="failed")
        reader = MagicMock()
        reader.by_owner.return_value = [entry]

        with patch("nexus.catalog.factory.make_catalog_reader", return_value=reader), \
             patch("nexus.doc_indexer._fence_fail") as fence_fail:
            _reconcile_needs_fence({"needs_fence": needs_fence, "owner": "1.1"})

        fence_fail.assert_not_called()


class TestReconcileNeedsFenceDegradedFallback:
    """T2 critique-nexus-hg2dw-36602c67f [24598] finding 5 (Significant):
    an unresolved owner or a failed bulk read must NOT silently disable
    reconciliation for the whole run — it degrades to a per-doc fallback
    (``doc_indexer._index_fence_state``), loudly logged, still fail-
    stamping what it can."""

    def test_missing_owner_falls_back_to_per_doc_reads(self) -> None:
        needs_fence = {
            "1.1.1": ("h1", "docs__repo__voyage-context-3__v1"),
            "1.1.2": ("h2", "docs__repo__voyage-context-3__v1"),
        }

        def _fake_fence_state(doc_id):
            return {"1.1.1": ("complete", "h1"), "1.1.2": ("indexing", "h2")}[doc_id]

        with patch("nexus.catalog.factory.make_catalog_reader") as make_reader, \
             patch("nexus.doc_indexer._index_fence_state", side_effect=_fake_fence_state), \
             patch("nexus.doc_indexer._fence_fail") as fence_fail:
            _reconcile_needs_fence({"needs_fence": needs_fence, "owner": None})

        make_reader.assert_not_called()  # no owner -> never even tries the bulk path
        failed_doc_ids = {call.args[0] for call in fence_fail.call_args_list}
        assert failed_doc_ids == {"1.1.2"}

    def test_bulk_read_failure_falls_back_to_per_doc_reads(self) -> None:
        needs_fence = {"1.1.1": ("h1", "docs__repo__voyage-context-3__v1")}

        with patch(
            "nexus.catalog.factory.make_catalog_reader",
            side_effect=RuntimeError("transport down"),
        ), patch(
            "nexus.doc_indexer._index_fence_state",
            return_value=("indexing", "h1"),
        ) as fence_state, patch("nexus.doc_indexer._fence_fail") as fence_fail:
            _reconcile_needs_fence({"needs_fence": needs_fence, "owner": "1.1"})

        fence_state.assert_called_once_with("1.1.1")
        fence_fail.assert_called_once()
        assert fence_fail.call_args[0][0] == "1.1.1"

    def test_reader_none_falls_back_to_per_doc_reads(self) -> None:
        needs_fence = {"1.1.1": ("h1", "docs__repo__voyage-context-3__v1")}
        with patch("nexus.catalog.factory.make_catalog_reader", return_value=None), \
             patch("nexus.doc_indexer._index_fence_state", return_value=(None, "")), \
             patch("nexus.doc_indexer._fence_fail") as fence_fail:
            _reconcile_needs_fence({"needs_fence": needs_fence, "owner": "1.1"})
        fence_fail.assert_called_once_with("1.1.1", "index run exited without completing this document")

    def test_per_doc_read_failure_is_skipped_not_fatal(self) -> None:
        """One doc's individual read failing must not abort the rest of
        the fallback loop, and must never raise out of reconciliation."""
        needs_fence = {
            "1.1.1": ("h1", "docs__repo__voyage-context-3__v1"),
            "1.1.2": ("h2", "docs__repo__voyage-context-3__v1"),
        }

        def _fake_fence_state(doc_id):
            if doc_id == "1.1.1":
                raise RuntimeError("read failed for 1.1.1")
            return ("indexing", "h2")

        with patch("nexus.catalog.factory.make_catalog_reader", return_value=None), \
             patch("nexus.doc_indexer._index_fence_state", side_effect=_fake_fence_state), \
             patch("nexus.doc_indexer._fence_fail") as fence_fail:
            _reconcile_needs_fence({"needs_fence": needs_fence, "owner": "1.1"})

        failed_doc_ids = {call.args[0] for call in fence_fail.call_args_list}
        assert failed_doc_ids == {"1.1.2"}

    def test_degraded_fallback_never_raises(self) -> None:
        needs_fence = {"1.1.1": ("h1", "docs__repo__voyage-context-3__v1")}
        with patch("nexus.catalog.factory.make_catalog_reader", return_value=None), \
             patch(
                 "nexus.doc_indexer._index_fence_state",
                 side_effect=RuntimeError("boom"),
             ):
            _reconcile_needs_fence({"needs_fence": needs_fence, "owner": None})  # must not raise


class TestFenceBeginFailureCounter:
    """T2 code-review-nexus-hg2dw-52d06c8c5 [24626] finding 2: a
    fail-open fence-begin failure is counted (not just per-doc logged) so
    a run-level summary line can name a burst of these instead of only a
    WARNING per file, which is easy to miss on a large run."""

    def setup_method(self) -> None:
        import nexus.doc_indexer as doc_indexer_mod
        doc_indexer_mod.reset_fence_begin_failure_count()

    def teardown_method(self) -> None:
        import nexus.doc_indexer as doc_indexer_mod
        doc_indexer_mod.reset_fence_begin_failure_count()

    def test_reset_then_zero(self) -> None:
        from nexus.doc_indexer import fence_begin_failure_count
        assert fence_begin_failure_count() == 0

    def test_fence_begin_failure_increments_by_one(self) -> None:
        from nexus.doc_indexer import _fence_begin, fence_begin_failure_count

        with patch("nexus.catalog.factory.make_catalog_writer", side_effect=RuntimeError("catalog down")):
            _fence_begin("1.1.1", "hash", "docs__repo__voyage-context-3__v1")  # must not raise

        assert fence_begin_failure_count() == 1

    def test_fence_begin_success_does_not_increment(self) -> None:
        from nexus.doc_indexer import _fence_begin, fence_begin_failure_count

        writer = MagicMock()
        with patch("nexus.catalog.factory.make_catalog_writer", return_value=writer):
            _fence_begin("1.1.1", "hash", "docs__repo__voyage-context-3__v1")

        assert fence_begin_failure_count() == 0

    def test_fence_begin_many_failure_increments_by_pair_count(self) -> None:
        from nexus.doc_indexer import _fence_begin_many, fence_begin_failure_count

        pairs = [("1.1.1", "h1"), ("1.1.2", "h2"), ("1.1.3", "h3")]
        with patch("nexus.catalog.factory.make_catalog_writer", side_effect=RuntimeError("catalog down")):
            _fence_begin_many(pairs, "docs__repo__voyage-context-3__v1")  # must not raise

        assert fence_begin_failure_count() == 3

    def test_counter_accumulates_across_multiple_failures(self) -> None:
        from nexus.doc_indexer import _fence_begin, fence_begin_failure_count

        with patch("nexus.catalog.factory.make_catalog_writer", side_effect=RuntimeError("catalog down")):
            _fence_begin("1.1.1", "hash", "docs__repo__voyage-context-3__v1")
            _fence_begin("1.1.2", "hash", "docs__repo__voyage-context-3__v1")

        assert fence_begin_failure_count() == 2

    def test_reset_zeroes_a_nonzero_counter(self) -> None:
        from nexus.doc_indexer import (
            _fence_begin,
            fence_begin_failure_count,
            reset_fence_begin_failure_count,
        )

        with patch("nexus.catalog.factory.make_catalog_writer", side_effect=RuntimeError("catalog down")):
            _fence_begin("1.1.1", "hash", "docs__repo__voyage-context-3__v1")
        assert fence_begin_failure_count() == 1

        reset_fence_begin_failure_count()
        assert fence_begin_failure_count() == 0
