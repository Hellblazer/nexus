# SPDX-License-Identifier: AGPL-3.0-or-later
"""Smoke tests for ``scripts/spikes/spike_d_tier_b_parity.py``.

Scope: pure-function diff + summary layer, manifest validation,
backend env toggling, per-case routing to the correct tool function,
and the ``qwen_agent_skipped`` behaviour for tools currently unwired
to ``NEXUS_TIER_B_DISPATCHER``. The live-backend leg is NOT exercised
— the dispatchers are patched.
"""
from __future__ import annotations

import json
import os
import sys
from pathlib import Path
from unittest.mock import AsyncMock, patch

import pytest

REPO_ROOT = Path(__file__).resolve().parent.parent
SPIKE_DIR = REPO_ROOT / "scripts" / "spikes"
sys.path.insert(0, str(SPIKE_DIR))

import spike_d_tier_b_parity as spike  # noqa: E402


# ── Manifest validation ──────────────────────────────────────────────────────


class TestLoadCases:
    def test_missing_manifest_raises(self, tmp_path: Path) -> None:
        with pytest.raises(FileNotFoundError):
            spike._load_cases(tmp_path / "nope.json")

    def test_malformed_case_missing_tool_raises(self, tmp_path: Path) -> None:
        manifest = tmp_path / "m.json"
        manifest.write_text(json.dumps([{"name": "x", "input": {}}]))
        with pytest.raises(ValueError, match="unsupported / missing tool"):
            spike._load_cases(manifest)

    def test_malformed_case_missing_input_raises(self, tmp_path: Path) -> None:
        manifest = tmp_path / "m.json"
        manifest.write_text(json.dumps([
            {"name": "x", "tool": "nx_enrich_beads"}
        ]))
        with pytest.raises(ValueError, match="missing 'input' dict"):
            spike._load_cases(manifest)

    def test_non_array_manifest_raises(self, tmp_path: Path) -> None:
        manifest = tmp_path / "m.json"
        manifest.write_text(json.dumps({"not": "a list"}))
        with pytest.raises(ValueError, match="must be a JSON array"):
            spike._load_cases(manifest)

    def test_well_formed_manifest_loads(self, tmp_path: Path) -> None:
        manifest = tmp_path / "m.json"
        manifest.write_text(json.dumps([
            {
                "name": "c1",
                "tool": "nx_enrich_beads",
                "input": {"bead_description": "x"},
            },
        ]))
        cases = spike._load_cases(manifest)
        assert len(cases) == 1
        assert cases[0]["name"] == "c1"


# ── Diff math ────────────────────────────────────────────────────────────────


class TestJaccard:
    def test_identical_lists(self) -> None:
        assert spike._jaccard(["a", "b"], ["b", "a"]) == 1.0

    def test_partial_overlap(self) -> None:
        # {a,b} ∩ {b,c} = {b}; union = {a,b,c}; 1/3.
        assert spike._jaccard(["a", "b"], ["b", "c"]) == pytest.approx(1 / 3)

    def test_disjoint(self) -> None:
        assert spike._jaccard(["a"], ["b"]) == 0.0

    def test_both_empty_vacuously_agrees(self) -> None:
        assert spike._jaccard([], []) == 1.0

    def test_dicts_canonicalised(self) -> None:
        # Same content, different key order → still equal in the set.
        a = [{"type": "merge", "id": 1}]
        b = [{"id": 1, "type": "merge"}]
        assert spike._jaccard(a, b) == 1.0


class TestDiffPayloads:
    def test_both_ok_full_agreement(self) -> None:
        c = {"key_files": ["a", "b"], "test_commands": ["pytest"],
             "constraints": [], "enriched_description": "x" * 50}
        q = {"key_files": ["b", "a"], "test_commands": ["pytest"],
             "constraints": [], "enriched_description": "y" * 50}
        out = spike.diff_payloads("nx_enrich_beads", c, q)
        assert out["both_ok"]
        assert out["structural"]["key_files"] == 1.0
        assert out["structural"]["test_commands"] == 1.0
        assert out["prose"]["enriched_description"] is True

    def test_one_side_none_marks_disagree(self) -> None:
        out = spike.diff_payloads("nx_enrich_beads", None, {"x": 1})
        assert not out["both_ok"]
        assert out["structural"]["key_files"] == 0.0


# ── Backend leg execution ────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_claude_agent_leg_unsets_env_and_calls_tool(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("NEXUS_TIER_B_DISPATCHER", "qwen_agent")  # stale
    fake = AsyncMock(return_value={"enriched_description": "out",
                                   "key_files": ["a.py"]})
    # The claude_agent leg must clear the env so production code-path
    # falls through to claude_dispatch.
    seen_env: dict[str, str | None] = {}

    async def _claude_dispatch(prompt, schema, timeout=600.0):
        seen_env["v"] = os.environ.get("NEXUS_TIER_B_DISPATCHER")
        return await fake(prompt, schema, timeout=timeout)

    with patch("nexus.operators.dispatch.claude_dispatch", _claude_dispatch):
        leg = await spike._run_one_leg(
            "nx_enrich_beads",
            {"bead_description": "a bead"},
            "claude_agent",
        )

    assert seen_env["v"] is None  # env was popped for the claude leg
    assert leg["payload"] == {"enriched_description": "out",
                              "key_files": ["a.py"]}
    assert leg["error"] is None
    assert leg["elapsed_ms"] > 0


@pytest.mark.asyncio
async def test_qwen_agent_leg_sets_env(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.delenv("NEXUS_TIER_B_DISPATCHER", raising=False)
    seen_env: dict[str, str | None] = {}

    async def _qwen_dispatch(prompt, schema, **kwargs):
        seen_env["v"] = os.environ.get("NEXUS_TIER_B_DISPATCHER")
        return {"enriched_description": "qwen-out", "key_files": []}

    with patch(
        "nexus.operators.qwen_agent_dispatch.qwen_agent_dispatch",
        _qwen_dispatch,
    ):
        leg = await spike._run_one_leg(
            "nx_enrich_beads",
            {"bead_description": "a bead"},
            "qwen_agent",
        )

    assert seen_env["v"] == "qwen_agent"
    assert leg["payload"] == {"enriched_description": "qwen-out",
                              "key_files": []}


@pytest.mark.asyncio
async def test_run_case_skips_qwen_for_unwired_tools(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Historical skip path — opt-in via ``--skip-unwired`` (which the
    driver translates into a non-default ``unwired`` set). Verifies the
    skip mechanism still works for replaying pre-completion benches.
    """
    monkeypatch.delenv("NEXUS_TIER_B_DISPATCHER", raising=False)
    fake_claude = AsyncMock(return_value={"summary": "s", "actions": []})
    fake_qwen = AsyncMock(side_effect=AssertionError(
        "qwen_agent must not run when caller opts into --skip-unwired"
    ))
    with (
        patch("nexus.operators.dispatch.claude_dispatch", fake_claude),
        patch(
            "nexus.operators.qwen_agent_dispatch.qwen_agent_dispatch",
            fake_qwen,
        ),
    ):
        row = await spike._run_case(
            {"name": "tidy-1", "tool": "nx_tidy",
             "input": {"topic": "x", "collection": "knowledge"}},
            ["claude_agent", "qwen_agent"],
            unwired=frozenset({"nx_tidy", "nx_plan_audit"}),
        )
    assert row["qwen_agent_skipped"] is True
    assert row["qwen_agent"] is None
    assert row["claude_agent"]["payload"] == {"summary": "s", "actions": []}
    fake_qwen.assert_not_awaited()


@pytest.mark.asyncio
async def test_run_case_default_runs_qwen_for_nx_tidy(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Tier-B completion: default ``unwired`` set is empty, so
    ``nx_tidy`` now runs the qwen leg like ``nx_enrich_beads``.
    """
    monkeypatch.delenv("NEXUS_TIER_B_DISPATCHER", raising=False)
    fake_claude = AsyncMock(return_value={"summary": "s", "actions": []})
    fake_qwen = AsyncMock(return_value={"summary": "qs", "actions": []})
    with (
        patch("nexus.operators.dispatch.claude_dispatch", fake_claude),
        patch(
            "nexus.operators.qwen_agent_dispatch.qwen_agent_dispatch",
            fake_qwen,
        ),
    ):
        row = await spike._run_case(
            {"name": "tidy-1", "tool": "nx_tidy",
             "input": {"topic": "x", "collection": "knowledge"}},
            ["claude_agent", "qwen_agent"],
        )
    assert not row.get("qwen_agent_skipped")
    assert fake_claude.await_count == 1
    assert fake_qwen.await_count == 1


@pytest.mark.asyncio
async def test_run_case_routes_enrich_beads_through_both_dispatchers(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.delenv("NEXUS_TIER_B_DISPATCHER", raising=False)
    fake_claude = AsyncMock(return_value={
        "enriched_description": "c" * 40, "key_files": ["x.py"],
        "test_commands": [], "constraints": [],
    })
    fake_qwen = AsyncMock(return_value={
        "enriched_description": "q" * 40, "key_files": ["x.py"],
        "test_commands": [], "constraints": [],
    })
    with (
        patch("nexus.operators.dispatch.claude_dispatch", fake_claude),
        patch(
            "nexus.operators.qwen_agent_dispatch.qwen_agent_dispatch",
            fake_qwen,
        ),
    ):
        row = await spike._run_case(
            {"name": "e1", "tool": "nx_enrich_beads",
             "input": {"bead_description": "fix the widget"}},
            ["claude_agent", "qwen_agent"],
        )
    assert fake_claude.await_count == 1
    assert fake_qwen.await_count == 1
    # Bead description forwarded into the prompt (first positional arg).
    claude_args, _ = fake_claude.call_args
    assert "fix the widget" in claude_args[0]
    qwen_args, _ = fake_qwen.call_args
    assert "fix the widget" in qwen_args[0]
    # Structural diff computed; identical key_files → 1.0 Jaccard.
    assert row["diff"]["both_ok"] is True
    assert row["diff"]["structural"]["key_files"] == 1.0


# ── Aggregation ──────────────────────────────────────────────────────────────


class TestSummarize:
    def test_per_tool_aggregates(self) -> None:
        records = [
            {
                "tool": "nx_enrich_beads",
                "claude_agent": {"elapsed_ms": 1000.0, "error": None,
                                 "tool_calls": None},
                "qwen_agent": {"elapsed_ms": 500.0, "error": None,
                               "tool_calls": 7},
                "diff": {
                    "both_ok": True,
                    "structural": {"key_files": 1.0, "test_commands": 0.5,
                                   "constraints": 1.0},
                    "prose": {"enriched_description": True},
                },
            },
            {
                "tool": "nx_enrich_beads",
                "claude_agent": {"elapsed_ms": 2000.0, "error": None,
                                 "tool_calls": None},
                "qwen_agent": {"elapsed_ms": 700.0, "error": None,
                               "tool_calls": 12},
                "diff": {
                    "both_ok": True,
                    "structural": {"key_files": 0.0, "test_commands": 0.5,
                                   "constraints": 1.0},
                    "prose": {"enriched_description": False},
                },
            },
            {
                "tool": "nx_tidy",
                "claude_agent": {"elapsed_ms": 3000.0, "error": None,
                                 "tool_calls": None},
                "qwen_agent": None,
                "qwen_agent_skipped": True,
                "diff": {"both_ok": False, "structural": {}, "prose": {}},
            },
        ]
        s = spike._summarize(records)
        assert s["total"] == 3
        # nx_enrich_beads aggregates.
        eb = s["by_tool"]["nx_enrich_beads"]
        assert eb["n"] == 2
        assert eb["skipped_qwen_agent"] == 0
        assert eb["claude_ok_rate"] == 1.0
        assert eb["qwen_ok_rate"] == 1.0
        assert eb["claude_median_ms"] == 1500.0
        assert eb["qwen_median_ms"] == 600.0
        assert eb["tool_calls_median"] == 9.5
        assert eb["tool_calls_max"] == 12
        # Mean Jaccard over key_files = (1.0 + 0.0) / 2 = 0.5.
        assert eb["structural"]["key_files"]["mean_jaccard"] == 0.5
        # Prose: 1 of 2 agree.
        assert eb["prose"]["enriched_description"]["agree_rate"] == 0.5
        # nx_tidy skipped on qwen leg.
        td = s["by_tool"]["nx_tidy"]
        assert td["skipped_qwen_agent"] == 1
