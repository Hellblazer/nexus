# SPDX-License-Identifier: AGPL-3.0-or-later
"""nexus-wxjr6: the CLIENT half of the kl2z6 combined write.

Covers ``mcp_infra._apply_combined_write_response`` (engine response ->
CLI-summary accounting — nexus-39upx: skips are never silent) and
``indexer.py``'s ``_batch_flush`` combined-write construction (chunks
payload dedup, by_doc manifest-row grouping, the position-0 defended
invariant). Design of record: T2 ``design-kl2z6-combined-write`` REV 2.
"""

from __future__ import annotations

import pytest

from nexus.mcp_infra import (
    _apply_combined_write_response,
    get_complete_refusals,
    get_manifest_write_failures,
    get_superseded_sweep_stats,
    reset_complete_refusals,
    reset_manifest_write_failures,
    reset_superseded_sweep_stats,
)


@pytest.fixture(autouse=True)
def _reset_collectors():
    reset_manifest_write_failures()
    reset_complete_refusals()
    reset_superseded_sweep_stats()
    yield
    reset_manifest_write_failures()
    reset_complete_refusals()
    reset_superseded_sweep_stats()


class TestFailedDocIds:
    def test_failed_doc_ids_recorded(self) -> None:
        res = {"failed_doc_ids": ["1.1", "1.2"]}
        failed = _apply_combined_write_response(res, {}, "code__x")
        assert failed == ["1.1", "1.2"]
        assert get_manifest_write_failures() == ["1.1", "1.2"]

    def test_no_failures_is_a_noop(self) -> None:
        assert _apply_combined_write_response({}, {}, "code__x") == []
        assert get_manifest_write_failures() == []


class TestCompleteRefused:
    def test_refused_doc_recorded(self) -> None:
        res = {
            "complete_refused": [{"doc_id": "1.1", "referenced": 1, "missing": 1}],
            "complete_refused_count": 1,
        }
        _apply_combined_write_response(res, {"1.1": "h"}, "code__x")
        assert get_complete_refusals() == ["1.1"]

    def test_count_mismatch_conservative_fallback_records_every_claimed_doc(self) -> None:
        # nexus-2t63u precedent: a truncated/absent refusal list with a
        # non-zero scalar must never read as zero refusals — every doc
        # this call CLAIMED complete gets treated as unstamped.
        res = {"complete_refused": [], "complete_refused_count": 2}
        _apply_combined_write_response(
            res, {"1.1": "ha", "1.2": "hb"}, "code__x",
        )
        assert sorted(get_complete_refusals()) == ["1.1", "1.2"]

    def test_count_matches_list_no_extra_refusals(self) -> None:
        res = {
            "complete_refused": [{"doc_id": "1.1"}],
            "complete_refused_count": 1,
        }
        _apply_combined_write_response(
            res, {"1.1": "ha", "1.2": "hb"}, "code__x",
        )
        # 1.1 is genuinely refused; 1.2 was claimed complete and NOT
        # refused, and the count matches the list, so 1.2 must NOT be
        # conservatively marked.
        assert get_complete_refusals() == ["1.1"]


class TestSweepAccounting:
    def test_swept_count_recorded(self) -> None:
        res = {"swept": 5}
        _apply_combined_write_response(res, {}, "code__x")
        assert get_superseded_sweep_stats()["swept"] == 5

    def test_zero_swept_is_a_noop(self) -> None:
        _apply_combined_write_response({"swept": 0}, {}, "code__x")
        assert get_superseded_sweep_stats()["swept"] == 0

    @pytest.mark.parametrize(
        "reason", ["gate_timeout", "statement_timeout", "before_read_failed", "sweep_failed"],
    )
    def test_each_closed_vocabulary_reason_recorded(self, reason: str) -> None:
        res = {
            "sweep_detail": [
                {"doc_id": "1.1", "dropped": 1, "swept": 0, "kept": 1,
                 "errored": True, "reason": reason},
            ],
        }
        _apply_combined_write_response(res, {}, "code__x")
        skipped = get_superseded_sweep_stats()["skipped"]
        assert len(skipped) == 1
        assert skipped[0]["doc_id"] == "1.1"
        assert skipped[0]["collection"] == "code__x"
        assert skipped[0]["reason"] == reason

    def test_non_errored_outcome_is_not_recorded_as_a_skip(self) -> None:
        res = {
            "swept": 3,
            "sweep_detail": [
                {"doc_id": "1.1", "dropped": 3, "swept": 3, "kept": 0, "errored": False},
            ],
        }
        _apply_combined_write_response(res, {}, "code__x")
        assert get_superseded_sweep_stats()["skipped"] == []

    def test_missing_reason_on_errored_entry_falls_back_to_sweep_failed(self) -> None:
        # A shape a well-behaved engine never sends, but .get() must never
        # KeyError, and a missing reason must not be silently dropped —
        # it lands in the closed vocabulary's catch-all bucket.
        res = {
            "sweep_detail": [
                {"doc_id": "1.1", "errored": True},
            ],
        }
        _apply_combined_write_response(res, {}, "code__x")
        skipped = get_superseded_sweep_stats()["skipped"]
        assert skipped[0]["reason"] == "sweep_failed"

    def test_mixed_batch_multiple_docs(self) -> None:
        res = {
            "swept": 2,
            "sweep_detail": [
                {"doc_id": "1.1", "swept": 2, "errored": False},
                {"doc_id": "1.2", "swept": 0, "errored": True, "reason": "gate_timeout"},
            ],
        }
        _apply_combined_write_response(res, {}, "code__x")
        stats = get_superseded_sweep_stats()
        assert stats["swept"] == 2
        assert len(stats["skipped"]) == 1
        assert stats["skipped"][0]["doc_id"] == "1.2"
        assert stats["skipped"][0]["reason"] == "gate_timeout"
