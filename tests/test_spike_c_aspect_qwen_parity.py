# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (c) 2026 Hal Hildebrand. All rights reserved.
"""Smoke tests for ``scripts/spikes/spike_c_aspect_qwen_parity.py``.

Scope: the pure-function diff layer (``diff_records`` + the per-bucket
agreement helpers) and the summary aggregator. The end-to-end
``_run_one`` path that toggles ``NEXUS_ASPECT_BACKEND`` and calls
``extract_aspects`` is NOT exercised here — it requires a live backend
and a real corpus. The intent is to lock down the diff contract so a
parity report cannot silently misclassify field agreements.
"""
from __future__ import annotations

import sys
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parent.parent
SPIKE_DIR = REPO_ROOT / "scripts" / "spikes"
sys.path.insert(0, str(SPIKE_DIR))

from nexus.aspect_extractor import AspectRecord, ExtractFail  # noqa: E402

import spike_c_aspect_qwen_parity as spike  # noqa: E402


def _rec(**overrides) -> AspectRecord:
    """Factory: an AspectRecord with sane defaults the test overrides."""
    base = dict(
        collection="knowledge__test",
        source_path="/p/x.pdf",
        problem_formulation="The paper addresses problem X in domain Y.",
        proposed_method="We propose a method using technique Z.",
        experimental_datasets=["MNIST", "CIFAR-10"],
        experimental_baselines=["ResNet", "VGG"],
        experimental_results="Achieves 95% accuracy, beating baselines.",
        extras={},
        confidence=0.9,
        extracted_at="2026-05-14T00:00:00+00:00",
        model_version="claude-haiku-4-5-20251001",
        extractor_name="scholarly-paper-v1",
    )
    base.update(overrides)
    return AspectRecord(**base)


class TestAgreeHelpers:
    def test_strict_normalises_whitespace_and_case(self) -> None:
        assert spike._agree_strict("  Foo  Bar ", "foo bar")
        assert not spike._agree_strict("foo", "bar")

    def test_set_is_order_insensitive(self) -> None:
        assert spike._agree_set(["a", "b"], ["b", "a"])
        assert not spike._agree_set(["a"], ["a", "b"])
        assert spike._agree_set([], [])

    def test_prose_both_none_agrees(self) -> None:
        assert spike._agree_prose(None, None)

    def test_prose_one_empty_disagrees(self) -> None:
        assert not spike._agree_prose("hello", "")
        assert not spike._agree_prose(None, "x")

    def test_prose_similar_length_agrees(self) -> None:
        a = "x" * 100
        b = "y" * 80
        assert spike._agree_prose(a, b)  # 80/100 = 0.8 >= 0.5

    def test_prose_wildly_different_length_disagrees(self) -> None:
        a = "x" * 10
        b = "y" * 100
        assert not spike._agree_prose(a, b)  # 10/100 = 0.1 < 0.5


class TestDiffRecords:
    def test_both_aspect_records_full_agreement(self) -> None:
        r = _rec()
        out = spike.diff_records(r, _rec())
        assert out["both_ok"] is True
        assert out["claude_ok"] and out["qwen_ok"]
        # All diffed fields agree.
        for f in spike.ALL_DIFFED_FIELDS:
            assert out["agreement"][f] is True, f

    def test_partial_disagreement(self) -> None:
        c = _rec()
        q = _rec(
            experimental_datasets=["MNIST"],  # set mismatch
            problem_formulation="x",  # prose length ratio fails
        )
        out = spike.diff_records(c, q)
        assert out["both_ok"]
        assert out["agreement"]["experimental_datasets"] is False
        assert out["agreement"]["problem_formulation"] is False
        # Untouched fields still agree.
        assert out["agreement"]["experimental_baselines"] is True
        assert out["agreement"]["proposed_method"] is True

    def test_one_side_extractfail_marks_all_disagree(self) -> None:
        c = _rec()
        q = ExtractFail(uri="chroma://x/y", reason="unreachable", detail="d")
        out = spike.diff_records(c, q)
        assert out["both_ok"] is False
        assert out["claude_ok"] is True
        assert out["qwen_ok"] is False
        assert out["claude_kind"] == "AspectRecord"
        assert out["qwen_kind"] == "ExtractFail"
        for f in spike.ALL_DIFFED_FIELDS:
            assert out["agreement"][f] is False

    def test_both_none_marks_all_disagree(self) -> None:
        # ``None`` means "no extractor registered" — symmetric but not
        # a parity agreement (no records to compare).
        out = spike.diff_records(None, None)
        assert out["both_ok"] is False
        for f in spike.ALL_DIFFED_FIELDS:
            assert out["agreement"][f] is False


class TestSummarize:
    def test_aggregates_field_rates_and_medians(self) -> None:
        records = [
            {
                "both_ok": True,
                "claude_ok": True,
                "qwen_ok": True,
                "claude_elapsed_ms": 1000.0,
                "qwen_elapsed_ms": 500.0,
                "agreement": {f: True for f in spike.ALL_DIFFED_FIELDS},
            },
            {
                "both_ok": True,
                "claude_ok": True,
                "qwen_ok": True,
                "claude_elapsed_ms": 2000.0,
                "qwen_elapsed_ms": 700.0,
                "agreement": {
                    **{f: True for f in spike.ALL_DIFFED_FIELDS},
                    "problem_formulation": False,
                },
            },
            {
                "both_ok": False,
                "claude_ok": True,
                "qwen_ok": False,
                "claude_elapsed_ms": 1500.0,
                "qwen_elapsed_ms": 100.0,
                "agreement": {f: False for f in spike.ALL_DIFFED_FIELDS},
            },
        ]
        s = spike._summarize(records)
        assert s["total"] == 3
        assert s["both_ok"] == 2
        assert s["claude_ok_rate"] == pytest.approx(1.0)
        assert s["qwen_ok_rate"] == pytest.approx(2 / 3)
        # problem_formulation: 1/2 agreement in both_ok subset.
        assert s["field_agreement"]["problem_formulation"]["rate"] == pytest.approx(0.5)
        assert s["field_agreement"]["problem_formulation"]["n"] == 2
        # Median of [1000, 2000, 1500] = 1500.
        assert s["claude_median_ms"] == 1500.0


class TestMarkdownRender:
    def test_renders_table_with_all_fields(self) -> None:
        records = [
            {
                "both_ok": True,
                "claude_ok": True,
                "qwen_ok": True,
                "claude_elapsed_ms": 1000.0,
                "qwen_elapsed_ms": 500.0,
                "agreement": {f: True for f in spike.ALL_DIFFED_FIELDS},
            },
        ]
        s = spike._summarize(records)
        md = spike._render_md(s, Path("/tmp/out.jsonl"))
        assert "Spike C" in md
        for f in spike.ALL_DIFFED_FIELDS:
            assert f"`{f}`" in md
