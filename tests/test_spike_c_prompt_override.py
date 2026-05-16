# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (c) 2026 Hal Hildebrand. All rights reserved.
"""Smoke tests for the spike_c ``--prompt-override`` flag and the
``judge_aspect_diffs`` greedy bipartite matcher.

Scope: argument parsing + monkey-patch behavior of ``--prompt-override``
(without invoking any backend), and the pure-function ``_match_pairs``
matcher in ``judge_aspect_diffs`` with a stubbed judge. Live extraction
and live judge dispatch are NOT exercised — they need a real backend.
"""
from __future__ import annotations

import asyncio
import sys
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parent.parent
SPIKE_DIR = REPO_ROOT / "scripts" / "spikes"
sys.path.insert(0, str(SPIKE_DIR))

from nexus.aspect_extractor import _SCHOLARLY_PAPER_CONFIG  # noqa: E402

import spike_c_aspect_qwen_parity as spike  # noqa: E402
import judge_aspect_diffs as judge_mod  # noqa: E402


# ---------------------------------------------------------------------------
# --prompt-override
# ---------------------------------------------------------------------------


@pytest.fixture
def _restore_prompt():
    """Snapshot/restore _SCHOLARLY_PAPER_CONFIG.prompt_template so the
    monkey-patch does not leak across tests."""
    original = _SCHOLARLY_PAPER_CONFIG.prompt_template
    yield
    # ExtractorConfig is frozen; use object.__setattr__ to restore.
    object.__setattr__(_SCHOLARLY_PAPER_CONFIG, "prompt_template", original)


def test_prompt_override_rejects_missing_placeholder(
    tmp_path: Path,
    capsys: pytest.CaptureFixture,
    _restore_prompt,
) -> None:
    bad = tmp_path / "bad.txt"
    bad.write_text("this template has no placeholder at all")
    rc = spike.main([
        "--uri", "knowledge__test/x.pdf",
        "--collection", "knowledge__test",
        "--prompt-override", str(bad),
        "--out", str(tmp_path / "out.jsonl"),
    ])
    assert rc == 2
    err = capsys.readouterr().err
    assert "{content}" in err
    # Template must be untouched on rejection.
    assert _SCHOLARLY_PAPER_CONFIG.prompt_template != "this template has no placeholder at all"


def test_prompt_override_applies_valid_template(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    _restore_prompt,
) -> None:
    good = tmp_path / "good.txt"
    new_body = "OVERRIDE PROMPT v-test\n\n{content}\n\nend"
    good.write_text(new_body)

    captured: dict = {}

    # Short-circuit per-source extraction so we don't touch real backends.
    # _run_one returns (record_or_fail, elapsed_ms); we record the
    # patched prompt at call time then return a benign failure.
    from nexus.aspect_extractor import ExtractFail  # local to avoid top-level churn

    def _fake_run_one(uri, collection, backend):  # noqa: ANN001
        captured["prompt"] = _SCHOLARLY_PAPER_CONFIG.prompt_template
        return ExtractFail(uri=uri, reason="stubbed", detail="test stub"), 0.0

    monkeypatch.setattr(spike, "_run_one", _fake_run_one)
    monkeypatch.setattr(spike, "_render_md", lambda summary, out_path: "", raising=False)

    out_path = tmp_path / "out.jsonl"
    rc = spike.main([
        "--uri", "knowledge__test/x.pdf",
        "--collection", "knowledge__test",
        "--prompt-override", str(good),
        "--out", str(out_path),
        "--backends", "claude",
    ])
    # Patch took effect: prompt seen during run is the override body.
    assert captured.get("prompt") == new_body
    # And it remained set after main() returned.
    assert _SCHOLARLY_PAPER_CONFIG.prompt_template == new_body
    assert isinstance(rc, int)


# ---------------------------------------------------------------------------
# judge_aspect_diffs._match_pairs
# ---------------------------------------------------------------------------


def _make_stub_judge(equiv_map: dict[tuple[str, str], bool]):
    """Build an async stub for ``_judge`` that returns equivalent=True
    only for pairs explicitly listed in ``equiv_map``."""
    async def _stub(backend: str, a: str, b: str) -> dict:
        eq = equiv_map.get((a, b), False)
        return {"equivalent": eq, "reason": "stub"}
    return _stub


def test_match_pairs_records_match_and_keeps_unmatched(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    only_c = ["UCI Mushroom database", "Adult census"]
    only_q = ["Mushroom database", "totally unrelated thing"]
    stub = _make_stub_judge({
        ("UCI Mushroom database", "Mushroom database"): True,
        # all other pairs default False
    })
    monkeypatch.setattr(judge_mod, "_judge", stub)

    matched, trace = asyncio.run(judge_mod._match_pairs("qwen", only_c, only_q))

    assert matched == {("UCI Mushroom database", "Mushroom database")}
    # Trace contains every pair attempted up to (and including) the match,
    # plus the unmatched probes for "Adult census".
    assert any(
        t.get("a") == "UCI Mushroom database"
        and t.get("b") == "Mushroom database"
        and t.get("equivalent") is True
        for t in trace
    )
    # Every trace row has the documented shape.
    for t in trace:
        assert set(t).issuperset({"a", "b"})
        assert "equivalent" in t or "error" in t


def test_match_pairs_skips_already_matched_q(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # If two only-C items would both match the same only-Q item, only
    # the first should consume it (greedy bipartite).
    only_c = ["A1", "A2"]
    only_q = ["B"]
    stub = _make_stub_judge({("A1", "B"): True, ("A2", "B"): True})
    monkeypatch.setattr(judge_mod, "_judge", stub)

    matched, _trace = asyncio.run(judge_mod._match_pairs("qwen", only_c, only_q))
    assert matched == {("A1", "B")}


def test_match_pairs_records_judge_errors(monkeypatch: pytest.MonkeyPatch) -> None:
    async def _boom(backend: str, a: str, b: str) -> dict:
        raise RuntimeError("backend down")
    monkeypatch.setattr(judge_mod, "_judge", _boom)

    matched, trace = asyncio.run(judge_mod._match_pairs("qwen", ["a"], ["b"]))
    assert matched == set()
    assert trace and trace[0].get("error") == "backend down"
