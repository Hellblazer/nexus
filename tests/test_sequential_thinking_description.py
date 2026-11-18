# SPDX-License-Identifier: AGPL-3.0-or-later
"""The conexus sequential-thinking server's tool text and its upstream-2.2.0 sync (GH #1661).

The server loads into every session (``alwaysLoad``), so its tool and field descriptions ride on every request.
The API safety classifier stops requests with ``[reasoning_extraction]``, and upstream's text frames the tool as a
way to write out the model's own thinking. These tests keep that framing out, keep the tool and field names a
caller depends on, keep the description inside the 1024-character cap upstream 2.2.0 adopted, and pin the two
upstream 2.2.0 behaviours the port took: the annotations and the bounded history.
"""

from __future__ import annotations

import importlib.util
import re
from pathlib import Path

_PATH = Path(__file__).resolve().parent.parent / "conexus" / "mcp" / "sequential_thinking.py"
_spec = importlib.util.spec_from_file_location("conexus_sequential_thinking_under_test", _PATH)
assert _spec is not None and _spec.loader is not None
st = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(st)

#: Wording that asks the model to expose its reasoning. "thought" stays allowed: it is upstream's field name.
_TRACE = re.compile(r"think|reason|reflect|chain of thought|thought process|your current", re.IGNORECASE)


def _texts() -> list[tuple[str, str]]:
    out = [("description", st.TOOL["description"]), ("title", st.TOOL["title"])]
    out += [(f"field {k}", v["description"]) for k, v in st.INPUT_SCHEMA["properties"].items()]
    return out


def test_no_tool_or_field_text_frames_the_tool_as_the_models_own_thinking() -> None:
    hits = [(where, m.group(0)) for where, text in _texts() for m in _TRACE.finditer(text)]
    assert hits == [], hits


def test_the_trace_pattern_would_catch_upstreams_own_text() -> None:
    # Non-vacuity: upstream 2.2.0's opening line and its thought field description both trip the pattern.
    assert _TRACE.search("A detailed tool for dynamic and reflective problem-solving through thoughts.")
    assert _TRACE.search("Your current thinking step")


def test_the_names_a_caller_depends_on_are_upstreams() -> None:
    assert st.TOOL["name"] == "sequentialthinking"
    assert set(st.INPUT_SCHEMA["properties"]) == {
        "thought", "nextThoughtNeeded", "thoughtNumber", "totalThoughts", "isRevision", "revisesThought",
        "branchFromThought", "branchId", "needsMoreThoughts",
    }
    assert st.INPUT_SCHEMA["required"] == ["thought", "nextThoughtNeeded", "thoughtNumber", "totalThoughts"]


def test_the_description_fits_the_1024_character_cap() -> None:
    assert len(st.TOOL_DESCRIPTION) <= 1024


def test_annotations_match_upstream_2_2_0() -> None:
    assert st.TOOL["annotations"] == {
        "readOnlyHint": False, "destructiveHint": False, "idempotentHint": False, "openWorldHint": False,
    }


def _step(state, n: int, branch: str | None = None) -> dict:
    args = {"thought": f"step {n}", "thoughtNumber": n, "totalThoughts": n, "nextThoughtNeeded": True}
    if branch:
        args |= {"branchFromThought": 1, "branchId": branch}
    return state.process(args)


def test_the_history_is_bounded_and_an_evicted_step_leaves_its_branch() -> None:
    state = st.SequentialThinking(max_history=3)
    _step(state, 1, "a")
    _step(state, 2, "b")
    _step(state, 3)
    _step(state, 4)
    result = _step(state, 5)
    assert result["thoughtHistoryLength"] == 3
    assert [s["thoughtNumber"] for s in state.thought_history] == [3, 4, 5]
    assert result["branches"] == [] and state.branches == {}


def test_max_thought_history_reads_the_environment_and_falls_back_on_a_bad_value() -> None:
    assert st.max_thought_history({}) == st.DEFAULT_MAX_THOUGHT_HISTORY == 1000
    assert st.max_thought_history({"MAX_THOUGHT_HISTORY": " 7 "}) == 7
    assert st.max_thought_history({"MAX_THOUGHT_HISTORY": "05"}) == 5
    for bad in ("0", "-3", "+5", "1.5", "many", "", "\u00b2", "9" * 16, "9" * 5000):
        assert st.max_thought_history({"MAX_THOUGHT_HISTORY": bad}) == 1000, bad


def test_a_bound_of_one_keeps_only_the_newest_step() -> None:
    state = st.SequentialThinking(max_history=1)
    _step(state, 1, "a")
    result = _step(state, 2)
    assert result["thoughtHistoryLength"] == 1 and result["branches"] == []


def test_eviction_keeps_a_branch_that_still_has_steps() -> None:
    state = st.SequentialThinking(max_history=2)
    _step(state, 1, "a")
    _step(state, 2, "a")
    result = _step(state, 3)
    assert result["branches"] == ["a"] and len(state.branches["a"]) == 1


def test_a_branch_id_without_a_branch_point_makes_no_branch() -> None:
    state = st.SequentialThinking(max_history=5)
    result = state.process({"thought": "x", "thoughtNumber": 1, "totalThoughts": 1, "nextThoughtNeeded": False,
                            "branchId": "lonely"})
    assert result["branches"] == []
