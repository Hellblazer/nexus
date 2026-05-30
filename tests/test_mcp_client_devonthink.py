# SPDX-License-Identifier: AGPL-3.0-or-later
"""P1.3 contracts for the DEVONthink per-call MCP client (RDR-139 Layer A).

Pins:
- ``dt_call`` running-loop guard fires + logs a DISTINCT
  ``dt_asyncio_context_error`` and returns ``None`` (CLI-path-only contract).
- ``available()`` gate: True only when ``is_running.running`` is truthy;
  False on unreachable DT; cached, refreshable.
- typed helpers map results correctly and degrade to ``[]`` / ``None`` /
  ``False`` when DT is unavailable (Gap 0 fail-soft).

No live DEVONthink server is touched: ``dt_call`` is monkeypatched for the
helper/gate pins; the loop-guard pin exercises the real ``dt_call`` (the guard
returns before any connection is attempted).
"""

from __future__ import annotations

from typing import Any

import pytest
import structlog

from nexus.mcp_client import devonthink as dt


@pytest.fixture(autouse=True)
def _reset_cache():
    dt.reset_availability_cache()
    yield
    dt.reset_availability_cache()


@pytest.mark.asyncio
async def test_dt_call_running_loop_guard_logs_distinctly() -> None:
    events: list[dict[str, Any]] = []

    def _capture(logger, method_name, event_dict):
        events.append(dict(event_dict))
        raise structlog.DropEvent

    structlog.configure(processors=[_capture])
    try:
        # We ARE inside a running loop (async test) — the guard must fire.
        out = dt.dt_call("is_running")
    finally:
        structlog.reset_defaults()

    assert out is None
    assert any(e.get("event") == "dt_asyncio_context_error" for e in events)


def test_available_true_when_running(monkeypatch) -> None:
    monkeypatch.setattr(dt, "dt_call", lambda tool, args=None: {"running": True})
    assert dt.available(refresh=True) is True


def test_available_false_when_not_running(monkeypatch) -> None:
    monkeypatch.setattr(dt, "dt_call", lambda tool, args=None: {"running": False})
    assert dt.available(refresh=True) is False


def test_available_false_when_unreachable(monkeypatch) -> None:
    monkeypatch.setattr(dt, "dt_call", lambda tool, args=None: None)
    assert dt.available(refresh=True) is False


def test_available_is_cached(monkeypatch) -> None:
    calls = {"n": 0}

    def _fake(tool, args=None):
        calls["n"] += 1
        return {"running": True}

    monkeypatch.setattr(dt, "dt_call", _fake)
    assert dt.available(refresh=True) is True
    assert dt.available() is True  # served from cache
    assert calls["n"] == 1


def test_dt_find_similar_maps_and_applies_floor(monkeypatch) -> None:
    payload = {
        "count": 3,
        "results": [
            {"uuid": "A", "score": 0.9, "name": "alpha"},
            {"uuid": "B", "score": 0.4, "name": "below-floor"},
            {"score": 0.95, "name": "no-uuid"},
        ],
    }
    monkeypatch.setattr(dt, "dt_call", lambda tool, args=None: payload)
    out = dt.dt_find_similar("Q", limit=10, floor=0.5)
    assert out == [{"uuid": "A", "score": 0.9, "name": "alpha"}]


def test_dt_find_similar_parses_bare_array_shape(monkeypatch) -> None:
    # Single-record mode returns a BARE neighbour array; core wraps a bare JSON
    # array as {"result": [...]}. dt_find_similar must read that shape, not only
    # {"results": [...]}. (Live MVV finding 2026-05-30 — the spike's assumed
    # {count, results} shape did not match single-uuid mode; the mismatch made
    # Layer B silently emit zero edges for every record.)
    payload = {"result": [
        {"uuid": "A", "score": 0.72, "name": "alpha"},
        {"uuid": "B", "score": 0.70, "name": "beta"},
    ]}
    monkeypatch.setattr(dt, "dt_call", lambda tool, args=None: payload)
    out = dt.dt_find_similar("Q", limit=10, floor=0.5)
    assert [n["uuid"] for n in out] == ["A", "B"]


def test_dt_record_links_merges_and_dedups(monkeypatch) -> None:
    payload = {
        "incoming": [{"uuid": "A", "name": "a"}],
        "outgoing": [{"uuid": "A", "name": "a"}, {"uuid": "B", "name": "b"}],
    }
    monkeypatch.setattr(dt, "dt_call", lambda tool, args=None: payload)
    out = dt.dt_record_links("Q")
    assert [n["uuid"] for n in out] == ["A", "B"]
    assert all(n["score"] == 1.0 for n in out)


def test_helpers_fail_soft_when_unavailable(monkeypatch) -> None:
    monkeypatch.setattr(dt, "dt_call", lambda tool, args=None: None)
    assert dt.dt_find_similar("Q") == []
    assert dt.dt_record_links("Q") == []
    assert dt.dt_resolve_doi("10.1/x") is None
    assert dt.dt_extract_content("Q") is None
    assert dt.dt_set_tags("Q", ["nx-related"]) is False
    assert dt.dt_set_custom_metadata("Q", {"x": "y"}) is False
    assert dt.dt_set_annotation("Q", "note") is False


def test_write_helpers_true_on_success_and_pass_no_clobber_mode(monkeypatch) -> None:
    seen: list[tuple[str, dict]] = []

    def _capture(tool, args=None):
        seen.append((tool, dict(args or {})))
        return {"uuid": "Q"}

    monkeypatch.setattr(dt, "dt_call", _capture)
    assert dt.dt_set_tags("Q", ["nx-related"]) is True
    assert dt.dt_set_custom_metadata("Q", {"mddoi": "10.1/x"}) is True
    assert dt.dt_set_annotation("Q", "see nx tumbler 1.2.3") is True

    by_tool = {tool: args for tool, args in seen}
    # CA5 no-clobber: writes must be additive/merge, never replace the user's data.
    assert by_tool["set_record_tags"]["mode"] == "add"
    assert by_tool["set_record_custom_metadata"]["mode"] == "merge"
    assert by_tool["set_record_annotation"]["mode"] == "append"
    assert by_tool["set_record_annotation"]["text"] == "see nx tumbler 1.2.3"


def test_dt_set_custom_metadata_false_when_all_dropped(monkeypatch) -> None:
    # DT drops unknown (not pre-defined) fields; helper must report the no-op
    # as False rather than a false success.
    monkeypatch.setattr(
        dt, "dt_call",
        lambda tool, args=None: {"metadata": {}, "dropped_fields": ["nxtumbler", "nxindexed"]},
    )
    assert dt.dt_set_custom_metadata("Q", {"nxtumbler": "1.2.3", "nxindexed": "true"}) is False


def test_dt_set_custom_metadata_true_on_partial_write(monkeypatch) -> None:
    monkeypatch.setattr(
        dt, "dt_call",
        lambda tool, args=None: {"metadata": {"nxtumbler": "1.2.3"}, "dropped_fields": ["nxindexed"]},
    )
    assert dt.dt_set_custom_metadata("Q", {"nxtumbler": "1.2.3", "nxindexed": "true"}) is True


def test_dt_annotation_text_two_hop(monkeypatch) -> None:
    # get_record_annotation → annotation_uuid, then get_record_text → body.
    def _fake(tool, args=None):
        if tool == "get_record_annotation":
            return {"annotation_uuid": "ANN"}
        if tool == "get_record_text":
            assert args == {"uuid": "ANN"}
            return {"text": "existing body"}
        raise AssertionError(f"unexpected tool {tool}")

    monkeypatch.setattr(dt, "dt_call", _fake)
    assert dt.dt_annotation_text("Q") == "existing body"


def test_dt_annotation_text_none_when_no_annotation(monkeypatch) -> None:
    monkeypatch.setattr(dt, "dt_call", lambda tool, args=None: {"annotation_uuid": None})
    assert dt.dt_annotation_text("Q") is None


def test_dt_annotation_text_none_when_unreachable(monkeypatch) -> None:
    monkeypatch.setattr(dt, "dt_call", lambda tool, args=None: None)
    assert dt.dt_annotation_text("Q") is None


def test_write_helpers_reject_empty_input(monkeypatch) -> None:
    # Empty tags/fields short-circuit without a call.
    monkeypatch.setattr(dt, "dt_call", lambda tool, args=None: pytest.fail("should not call"))
    assert dt.dt_set_tags("Q", []) is False
    assert dt.dt_set_custom_metadata("Q", {}) is False
    assert dt.dt_set_annotation("Q", "") is False
    assert dt.dt_resolve_doi("") is None
