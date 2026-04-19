# SPDX-License-Identifier: AGPL-3.0-or-later
"""Tests for the per-stage intra-file timing accumulator (nexus-7niu)."""
from __future__ import annotations

import time
from unittest.mock import patch

import pytest

from nexus.retry import _add_voyage_retry, reset_retry_stats
from nexus.stage_timers import (
    StageTimers,
    aggregate,
    format_report,
)


@pytest.fixture(autouse=True)
def _reset_retry_stats_per_test():
    """Retry accumulators live in process-global state; isolate each
    test so leaked retries from an earlier case don't leak into
    ``StageTimers.retry_s`` assertions here."""
    reset_retry_stats()
    yield
    reset_retry_stats()


class TestStageTimersAccumulation:
    def test_stage_adds_elapsed_to_named_bucket(self) -> None:
        t = StageTimers()
        with t.stage("chunking"):
            time.sleep(0.02)
        s = t.snapshot()
        assert s["chunking_s"] >= 0.02
        assert s["embed_s"] == 0.0
        assert s["upload_s"] == 0.0
        assert s["retry_s"] == 0.0

    def test_each_bucket_receives_its_own_time(self) -> None:
        t = StageTimers()
        with t.stage("chunking"):
            time.sleep(0.01)
        with t.stage("embed"):
            time.sleep(0.01)
        with t.stage("upload"):
            time.sleep(0.01)
        s = t.snapshot()
        assert s["chunking_s"] >= 0.01
        assert s["embed_s"] >= 0.01
        assert s["upload_s"] >= 0.01
        assert s["retry_s"] == 0.0

    def test_retry_sleep_attributes_to_retry_not_stage(self) -> None:
        """The headline use case: voyage took 90 s but 60 s was retry
        backoff. Report must show embed_s=30, retry_s=60 — not
        embed_s=90. Otherwise the nexus-vatx stalls look like slow
        embed instead of rate-limited embed."""
        t = StageTimers()
        with t.stage("embed"):
            # Simulate 50 ms of actual work...
            time.sleep(0.05)
            # ...plus a 100 ms "backoff sleep" logged via the retry
            # accumulator. Real _voyage_with_retry calls
            # ``_add_voyage_retry(delay)`` right before ``time.sleep(delay)``.
            _add_voyage_retry(0.1)
            time.sleep(0.1)
        s = t.snapshot()
        # retry_s grabbed the 0.1 s backoff
        assert s["retry_s"] == pytest.approx(0.1, abs=0.01)
        # embed_s kept the ~0.05 s of net work, not the ~0.15 s total
        assert s["embed_s"] < 0.1, (
            f"retry time leaked into embed bucket: embed_s={s['embed_s']}"
        )
        assert s["embed_s"] >= 0.04

    def test_unknown_stage_name_raises(self) -> None:
        t = StageTimers()
        with pytest.raises(ValueError, match="unknown stage"):
            with t.stage("not-a-real-bucket"):
                pass

    def test_snapshot_is_thread_safe_copy(self) -> None:
        """``snapshot()`` returns a dict, not a live reference — caller
        mutating the dict must not corrupt the underlying counters."""
        t = StageTimers()
        with t.stage("chunking"):
            time.sleep(0.01)
        s1 = t.snapshot()
        s1["chunking_s"] = 999.0
        s2 = t.snapshot()
        assert s2["chunking_s"] < 1.0

    def test_total_s_equals_sum_of_buckets(self) -> None:
        t = StageTimers()
        with t.stage("chunking"):
            time.sleep(0.01)
        with t.stage("embed"):
            _add_voyage_retry(0.02)
            time.sleep(0.02)
        total = t.total_s()
        s = t.snapshot()
        assert total == pytest.approx(sum(s.values()))


class TestAggregate:
    def test_sums_across_multiple_files(self) -> None:
        a = StageTimers(chunking_s=1.0, embed_s=2.0, upload_s=0.5, retry_s=0.0)
        b = StageTimers(chunking_s=0.5, embed_s=3.0, upload_s=1.5, retry_s=1.0)
        totals = aggregate([a, b])
        assert totals["chunking_s"] == pytest.approx(1.5)
        assert totals["embed_s"] == pytest.approx(5.0)
        assert totals["upload_s"] == pytest.approx(2.0)
        assert totals["retry_s"] == pytest.approx(1.0)
        assert totals["total_s"] == pytest.approx(9.5)

    def test_empty_list_returns_zero_totals(self) -> None:
        totals = aggregate([])
        assert totals == {
            "chunking_s": 0.0,
            "embed_s": 0.0,
            "upload_s": 0.0,
            "retry_s": 0.0,
            "total_s": 0.0,
        }


class TestFormatReport:
    def test_empty_totals_emit_brief_message(self) -> None:
        line = format_report(aggregate([]), n_files=0)
        assert "no per-stage samples" in line

    def test_nonempty_totals_emit_breakdown_with_percentages(self) -> None:
        totals = {
            "chunking_s": 5.0, "embed_s": 30.0, "upload_s": 10.0,
            "retry_s": 5.0, "total_s": 50.0,
        }
        out = format_report(totals, n_files=12)
        assert "across 12 files" in out
        assert "chunking_s" in out and "5.0s" in out
        assert "embed_s" in out and "30.0s" in out
        assert "upload_s" in out and "10.0s" in out
        assert "retry_s" in out and "5.0s" in out
        # Percentages render
        assert "60.0%" in out   # embed = 30/50
        assert "20.0%" in out   # upload = 10/50
        assert "10.0%" in out   # chunking = 5/50 and retry = 5/50
        assert "total" in out


class TestRetrySnapshotNeverGoesNegative:
    """Retry counters are monotonic under normal use, but the clamp in
    ``stage()`` guards against any race where a snapshot appears to
    decrease. Verify the clamp doesn't accidentally double-count."""

    def test_retry_delta_clamped_at_zero(self) -> None:
        t = StageTimers()
        # Force the unusual case: pre > post (shouldn't happen but the
        # clamp exists for defense in depth)
        from nexus import stage_timers as _mod

        fake_vals = iter([{"total_seconds": 10.0}, {"total_seconds": 5.0}])
        with patch(
            "nexus.retry.get_retry_stats",
            side_effect=lambda: next(fake_vals),
        ):
            with t.stage("embed"):
                pass
        s = t.snapshot()
        assert s["retry_s"] == 0.0
        # elapsed here is ~0; embed may be 0 or a few micros — what
        # matters is retry_s never goes negative.
        assert s["embed_s"] >= 0.0
