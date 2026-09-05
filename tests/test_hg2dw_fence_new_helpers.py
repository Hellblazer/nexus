# SPDX-License-Identifier: AGPL-3.0-or-later
"""nexus-hg2dw: unit coverage for the two new registration-time fence
helpers in ``nexus.indexer`` — ``_fence_begin_needs_fence`` (point 1,
begin the fence for the run's registered set right after Pass 1) and
``_reconcile_needs_fence`` (point 2, fail-stamp anything registered but
unflushed at run exit). Both are pure orchestration around the EXISTING
``doc_indexer`` fence primitives, so these tests mock those primitives
and assert the call shape rather than standing up a live catalog.
"""
from __future__ import annotations

from unittest.mock import MagicMock, patch

from nexus.indexer import _fence_begin_needs_fence, _reconcile_needs_fence


class TestFenceBeginNeedsFence:
    def test_empty_needs_fence_is_a_no_op(self) -> None:
        with patch("nexus.doc_indexer._fence_begin_many") as begin_many:
            _fence_begin_needs_fence({})
        begin_many.assert_not_called()

    def test_groups_pairs_by_collection_one_call_per_collection(self) -> None:
        needs_fence = {
            "1.1.1": ("hash-a", "code__repo__voyage-code-3__v1"),
            "1.1.2": ("hash-b", "code__repo__voyage-code-3__v1"),
            "1.1.3": ("hash-c", "docs__repo__voyage-context-3__v1"),
        }
        with patch("nexus.doc_indexer._fence_begin_many") as begin_many:
            _fence_begin_needs_fence(needs_fence)

        assert begin_many.call_count == 2
        calls_by_collection = {
            call.args[1]: sorted(call.args[0]) for call in begin_many.call_args_list
        }
        assert calls_by_collection["code__repo__voyage-code-3__v1"] == sorted([
            ("1.1.1", "hash-a"), ("1.1.2", "hash-b"),
        ])
        assert calls_by_collection["docs__repo__voyage-context-3__v1"] == [
            ("1.1.3", "hash-c"),
        ]

    def test_relies_on_fence_begin_many_own_fail_open_contract(self) -> None:
        # _fence_begin_needs_fence adds no try/except of its own — it
        # relies entirely on _fence_begin_many's OWN documented fail-open
        # contract (never raises; logs and returns on a transport error),
        # exactly as _fire_flush_grain_begin's identical un-wrapped call
        # to the same function already does. Pinned here as a real
        # RuntimeError propagating through IS the current, honest
        # behavior of this thin wrapper — a future add of its own
        # try/except would be a deliberate decision, not a silent one.
        import pytest

        needs_fence = {"1.1.1": ("hash-a", "code__repo__voyage-code-3__v1")}
        with patch(
            "nexus.doc_indexer._fence_begin_many",
            side_effect=RuntimeError("boom"),
        ), pytest.raises(RuntimeError):
            _fence_begin_needs_fence(needs_fence)


class TestReconcileNeedsFence:
    def test_empty_needs_fence_is_a_no_op_no_reader_call(self) -> None:
        with patch("nexus.catalog.factory.make_catalog_reader") as make_reader:
            _reconcile_needs_fence({"needs_fence": {}, "owner": "1.1"})
        make_reader.assert_not_called()

    def test_missing_owner_is_a_no_op(self) -> None:
        with patch("nexus.catalog.factory.make_catalog_reader") as make_reader:
            _reconcile_needs_fence({
                "needs_fence": {"1.1.1": ("h", "docs__repo__voyage-context-3__v1")},
                "owner": None,
            })
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

    def test_reader_none_is_a_no_op(self) -> None:
        needs_fence = {"1.1.1": ("h1", "docs__repo__voyage-context-3__v1")}
        with patch("nexus.catalog.factory.make_catalog_reader", return_value=None), \
             patch("nexus.doc_indexer._fence_fail") as fence_fail:
            _reconcile_needs_fence({"needs_fence": needs_fence, "owner": "1.1"})
        fence_fail.assert_not_called()

    def test_reader_error_never_raises(self) -> None:
        needs_fence = {"1.1.1": ("h1", "docs__repo__voyage-context-3__v1")}
        with patch(
            "nexus.catalog.factory.make_catalog_reader",
            side_effect=RuntimeError("transport down"),
        ), patch("nexus.doc_indexer._fence_fail") as fence_fail:
            _reconcile_needs_fence({"needs_fence": needs_fence, "owner": "1.1"})
        fence_fail.assert_not_called()
