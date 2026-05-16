# SPDX-License-Identifier: MIT
"""Tests for scripts/spikes/judge_parity_diffs.py — schema detection,
per-tool set-field selection, greedy bipartite match, and prose-judge
opt-in gating.
"""

from __future__ import annotations

import asyncio
import importlib.util
import sys
from pathlib import Path

import pytest

# Load the script as a module (it lives under scripts/, not in a package).
_SCRIPT = Path(__file__).resolve().parents[1] / "scripts" / "spikes" / "judge_parity_diffs.py"
_spec = importlib.util.spec_from_file_location("judge_parity_diffs", _SCRIPT)
jpd = importlib.util.module_from_spec(_spec)
sys.modules["judge_parity_diffs"] = jpd
assert _spec and _spec.loader
_spec.loader.exec_module(jpd)


# ── Schema detection ────────────────────────────────────────────────────────


def test_detect_schema_spike_c():
    row = {"claude_record": {"foo": [1]}, "qwen_record": {"foo": [2]}}
    assert jpd.detect_schema(row) == "spike-c"


def test_detect_schema_spike_d():
    row = {
        "tool": "nx_enrich_beads",
        "claude_agent": {"payload": {"key_files": ["a.py"]}},
        "qwen_agent": {"payload": {"key_files": ["b.py"]}},
    }
    assert jpd.detect_schema(row) == "spike-d"


def test_detect_schema_unknown_raises():
    with pytest.raises(ValueError, match="cannot detect schema"):
        jpd.detect_schema({"foo": "bar"})


# ── Per-tool set-field selection ────────────────────────────────────────────


@pytest.mark.parametrize(
    "tool, expected",
    [
        ("nx_enrich_beads", ("key_files", "test_commands", "constraints")),
        ("nx_tidy", ("actions",)),
        ("nx_plan_audit", ("findings",)),
        ("unknown_tool", ()),
    ],
)
def test_set_fields_spike_d_per_tool(tool, expected):
    row = {"tool": tool, "claude_agent": {"payload": {}}, "qwen_agent": {"payload": {}}}
    assert jpd._set_fields_for(row, "spike-d") == expected


def test_set_fields_spike_c_constant():
    row = {"claude_record": {}, "qwen_record": {}}
    assert jpd._set_fields_for(row, "spike-c") == (
        "experimental_datasets", "experimental_baselines",
    )


# ── Item canonicalisation ───────────────────────────────────────────────────


def test_canon_string_passthrough():
    assert jpd._canon("foo") == "foo"


def test_canon_dict_sorted_keys():
    a = jpd._canon({"b": 1, "a": 2})
    b = jpd._canon({"a": 2, "b": 1})
    assert a == b
    assert '"a"' in a and '"b"' in a


# ── Greedy bipartite match (mocked _judge) ──────────────────────────────────


def test_match_pairs_greedy(monkeypatch):
    """Each only-C item finds its first equivalent only-Q item, then
    that Q item is locked out."""
    pairs_seen = []

    async def fake_judge(backend, a, b, *, prose=False):
        pairs_seen.append((a, b))
        # "X" matches "x" (case-insensitive equivalence).
        return {"equivalent": a.lower() == b.lower(), "reason": "test"}

    monkeypatch.setattr(jpd, "_judge", fake_judge)

    matched, trace = asyncio.run(jpd._match_pairs(
        "qwen", ["Foo", "Bar"], ["bar", "foo"],
    ))
    assert ("Foo", "foo") in matched
    assert ("Bar", "bar") in matched
    assert len(matched) == 2
    # All trace entries with equivalent=True correspond to matches.
    eq_traces = [t for t in trace if t.get("equivalent")]
    assert len(eq_traces) == 2


def test_match_pairs_no_match(monkeypatch):
    async def fake_judge(backend, a, b, *, prose=False):
        return {"equivalent": False, "reason": "different"}

    monkeypatch.setattr(jpd, "_judge", fake_judge)
    matched, trace = asyncio.run(jpd._match_pairs(
        "qwen", ["x", "y"], ["p", "q"],
    ))
    assert matched == set()
    # 2 only_c * 2 only_q = 4 judge calls (since none match, no break).
    assert len(trace) == 4


def test_match_pairs_judge_error(monkeypatch):
    async def fake_judge(backend, a, b, *, prose=False):
        raise RuntimeError("boom")

    monkeypatch.setattr(jpd, "_judge", fake_judge)
    matched, trace = asyncio.run(jpd._match_pairs(
        "qwen", ["x"], ["y"],
    ))
    assert matched == set()
    assert trace == [{"a": "x", "b": "y", "error": "boom"}]


# ── _rescore_row: spike-D set-field path with mocked judge ──────────────────


def test_rescore_spike_d_constraints_paraphrased(monkeypatch):
    """Strict Jaccard would be 0%; semantic judge collapses the pair."""
    async def fake_judge(backend, a, b, *, prose=False):
        return {"equivalent": True, "reason": "paraphrase"}
    monkeypatch.setattr(jpd, "_judge", fake_judge)

    row = {
        "tool": "nx_enrich_beads",
        "diff": {"both_ok": True},
        "claude_agent": {"payload": {
            "key_files": [], "test_commands": [],
            "constraints": ["Default posture is claude (cautious)"],
        }},
        "qwen_agent": {"payload": {
            "key_files": [], "test_commands": [],
            "constraints": ["Call-site names route to claude by default"],
        }},
    }
    out = asyncio.run(jpd._rescore_row("qwen", row, "spike-d", prose=False))
    judged = out["semantic_judged"]["constraints"]
    assert judged["agreement"] == 1.0
    assert len(judged["semantic_matches"]) == 1
    assert judged["unmatched_only_c"] == []
    assert judged["unmatched_only_q"] == []


def test_rescore_skips_when_not_both_ok(monkeypatch):
    called = False

    async def fake_judge(*a, **kw):
        nonlocal called
        called = True
        return {"equivalent": True, "reason": ""}

    monkeypatch.setattr(jpd, "_judge", fake_judge)
    row = {
        "tool": "nx_enrich_beads",
        "diff": {"both_ok": False},
        "claude_agent": {"payload": {}},
        "qwen_agent": {"payload": {}},
    }
    out = asyncio.run(jpd._rescore_row("qwen", row, "spike-d", prose=False))
    assert "semantic_judged" not in out
    assert called is False


def test_rescore_spike_c_unchanged_shape(monkeypatch):
    """spike-C path still produces a per-field judged block keyed by
    the spike-C set fields."""
    async def fake_judge(backend, a, b, *, prose=False):
        return {"equivalent": False, "reason": "no"}
    monkeypatch.setattr(jpd, "_judge", fake_judge)

    row = {
        "both_ok": True,
        "claude_record": {
            "experimental_datasets": ["MNIST"],
            "experimental_baselines": ["ResNet"],
        },
        "qwen_record": {
            "experimental_datasets": ["MNIST"],
            "experimental_baselines": ["ResNet"],
        },
    }
    out = asyncio.run(jpd._rescore_row("qwen", row, "spike-c", prose=False))
    j = out["semantic_judged"]
    assert set(j.keys()) == {"experimental_datasets", "experimental_baselines"}
    # Both sides identical → strict intersection covers everything; judge
    # not invoked at all.
    assert j["experimental_datasets"]["agreement"] == 1.0
    assert j["experimental_datasets"]["strict_intersection"] == ["MNIST"]


# ── Prose opt-in gating ─────────────────────────────────────────────────────


def test_prose_diverges_helper():
    # Identical → no divergence.
    assert jpd._prose_diverges("abc", "abc") is False
    # Both empty → no divergence.
    assert jpd._prose_diverges("", "") is False
    assert jpd._prose_diverges(None, None) is False
    # One empty → diverges.
    assert jpd._prose_diverges("abc", "") is True
    # Length ratio below 0.5 → diverges.
    assert jpd._prose_diverges("a" * 10, "a" * 4) is True
    # Length ratio above 0.5 → no divergence.
    assert jpd._prose_diverges("a" * 10, "a" * 8) is False


def test_prose_off_skips_prose_judge(monkeypatch):
    calls = {"set": 0, "prose": 0}

    async def fake_judge(backend, a, b, *, prose=False):
        calls["prose" if prose else "set"] += 1
        return {"equivalent": True, "reason": ""}
    monkeypatch.setattr(jpd, "_judge", fake_judge)

    row = {
        "tool": "nx_enrich_beads",
        "diff": {"both_ok": True},
        "claude_agent": {"payload": {
            "key_files": [], "test_commands": [], "constraints": [],
            "enriched_description": "short",
        }},
        "qwen_agent": {"payload": {
            "key_files": [], "test_commands": [], "constraints": [],
            "enriched_description": "this is a much much much longer description that diverges in length",
        }},
    }
    asyncio.run(jpd._rescore_row("qwen", row, "spike-d", prose=False))
    assert calls["prose"] == 0


def test_prose_on_invokes_prose_judge_when_diverged(monkeypatch):
    calls = {"set": 0, "prose": 0}

    async def fake_judge(backend, a, b, *, prose=False):
        calls["prose" if prose else "set"] += 1
        return {"equivalent": True, "reason": "semantically same"}
    monkeypatch.setattr(jpd, "_judge", fake_judge)

    row = {
        "tool": "nx_enrich_beads",
        "diff": {"both_ok": True},
        "claude_agent": {"payload": {
            "key_files": [], "test_commands": [], "constraints": [],
            "enriched_description": "short",
        }},
        "qwen_agent": {"payload": {
            "key_files": [], "test_commands": [], "constraints": [],
            "enriched_description": "this is a much much much longer description that diverges in length substantially",
        }},
    }
    out = asyncio.run(jpd._rescore_row("qwen", row, "spike-d", prose=True))
    assert calls["prose"] == 1
    block = out["semantic_judged"]["enriched_description"]
    assert block["kind"] == "prose"
    assert block["equivalent"] is True


def test_prose_on_skips_when_within_tolerance(monkeypatch):
    calls = {"prose": 0}

    async def fake_judge(backend, a, b, *, prose=False):
        if prose:
            calls["prose"] += 1
        return {"equivalent": True, "reason": ""}
    monkeypatch.setattr(jpd, "_judge", fake_judge)

    row = {
        "tool": "nx_enrich_beads",
        "diff": {"both_ok": True},
        "claude_agent": {"payload": {
            "key_files": [], "test_commands": [], "constraints": [],
            "enriched_description": "abc",
        }},
        "qwen_agent": {"payload": {
            "key_files": [], "test_commands": [], "constraints": [],
            "enriched_description": "abc",
        }},
    }
    out = asyncio.run(jpd._rescore_row("qwen", row, "spike-d", prose=True))
    assert calls["prose"] == 0
    assert out["semantic_judged"]["enriched_description"]["skipped"] is True


# ── Dict-shaped set items (nx_tidy / nx_plan_audit) ─────────────────────────


def test_dict_items_canonicalised_for_judge(monkeypatch):
    """nx_tidy actions are dicts. They should be JSON-canonicalised
    before being passed to the judge."""
    seen = []

    async def fake_judge(backend, a, b, *, prose=False):
        seen.append((a, b))
        return {"equivalent": False, "reason": ""}
    monkeypatch.setattr(jpd, "_judge", fake_judge)

    row = {
        "tool": "nx_tidy",
        "diff": {"both_ok": True},
        "claude_agent": {"payload": {
            "actions": [{"type": "move", "target": "a"}],
        }},
        "qwen_agent": {"payload": {
            "actions": [{"type": "delete", "target": "b"}],
        }},
    }
    asyncio.run(jpd._rescore_row("qwen", row, "spike-d", prose=False))
    # Exactly one judge call comparing JSON-serialised dicts.
    assert len(seen) == 1
    a, b = seen[0]
    assert a.startswith("{") and b.startswith("{")
    assert '"type"' in a and '"target"' in a


# ── Backward-compat shim ────────────────────────────────────────────────────


def test_shim_imports_and_reexports():
    """judge_aspect_diffs.py should still import and re-export the
    public names spike-C callers depended on."""
    shim_path = Path(__file__).resolve().parents[1] / "scripts" / "spikes" / "judge_aspect_diffs.py"
    spec = importlib.util.spec_from_file_location("judge_aspect_diffs_shim", shim_path)
    mod = importlib.util.module_from_spec(spec)
    assert spec and spec.loader
    spec.loader.exec_module(mod)

    assert mod.SET_FIELDS == ("experimental_datasets", "experimental_baselines")
    assert callable(mod.main)
    assert callable(mod._judge)
    assert callable(mod._match_pairs)
    assert callable(mod._rescore_row)
