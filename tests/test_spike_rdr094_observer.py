# SPDX-License-Identifier: AGPL-3.0-or-later
"""Deterministic unit tests for the RDR-094 Spike C observer.

The observer (``scripts/spikes/spike_rdr094_mid_session_observer.py``) tails
``mcp.log`` for the lifecycle events emitted by ``nexus.mcp.core``. The
original implementation seeked from byte 0 on startup, replaying every
historical entry as if it had just occurred. This test pins the
timestamp-filter fix per nexus-5rea acceptance criteria.
"""
from __future__ import annotations

import datetime as _dt
import importlib.util
import sys
from pathlib import Path

import pytest

_SPIKE_PATH = (
    Path(__file__).resolve().parent.parent
    / "scripts"
    / "spikes"
    / "spike_rdr094_mid_session_observer.py"
)


@pytest.fixture(scope="module")
def observer():
    spec = importlib.util.spec_from_file_location("spike_rdr094_obs", _SPIKE_PATH)
    assert spec is not None and spec.loader is not None
    mod = importlib.util.module_from_spec(spec)
    sys.modules["spike_rdr094_obs"] = mod
    spec.loader.exec_module(mod)
    return mod


def _epoch(year: int, month: int, day: int, hour: int = 0) -> float:
    return _dt.datetime(year, month, day, hour, tzinfo=_dt.timezone.utc).timestamp()


def test_extract_event_timestamp_iso_z(observer):
    line = (
        "2026-04-25 17:31:00 nexus INFO event='mcp_server_starting' "
        "timestamp='2026-04-25T17:31:00.500000Z' level='info' pid=42"
    )
    expected = _dt.datetime(
        2026, 4, 25, 17, 31, 0, 500000, tzinfo=_dt.timezone.utc
    ).timestamp()
    assert observer._extract_event_timestamp(line) == pytest.approx(expected)


def test_extract_event_timestamp_iso_offset(observer):
    line = "event='x' timestamp='2026-04-25T17:31:00.123456+00:00' pid=1"
    expected = _dt.datetime(
        2026, 4, 25, 17, 31, 0, 123456, tzinfo=_dt.timezone.utc
    ).timestamp()
    assert observer._extract_event_timestamp(line) == pytest.approx(expected)


def test_extract_event_timestamp_missing_returns_none(observer):
    assert observer._extract_event_timestamp("no timestamp anywhere") is None


def test_extract_event_timestamp_malformed_returns_none(observer):
    assert observer._extract_event_timestamp("timestamp='not-a-date'") is None


def test_parse_log_line_includes_event_ts(observer):
    line = (
        "2026-04-25 17:31:00 nexus INFO event='mcp_server_starting' "
        "timestamp='2026-04-25T17:31:00Z' level='info' pid=42"
    )
    parsed = observer._parse_log_line(line)
    assert parsed is not None
    assert parsed["event_type"] == "starting"
    assert parsed["pid"] == 42
    assert parsed["event_ts"] is not None


def test_parse_log_line_no_event_marker_returns_none(observer):
    assert observer._parse_log_line("plain stdlib log line, no event field") is None


def test_parse_log_line_stopping_extracts_reason(observer):
    line = (
        "event='mcp_server_stopping' timestamp='2026-04-25T17:31:00Z' "
        "level='info' reason='signal' pid=42"
    )
    parsed = observer._parse_log_line(line)
    assert parsed is not None
    assert parsed["event_type"] == "stopping_signal"


def test_is_historical_skips_pre_observer_entries(observer):
    started = _epoch(2026, 4, 25, 12)
    parsed = {"event_ts": _epoch(2020, 1, 1)}
    assert observer._is_historical(parsed, started) is True


def test_is_historical_passes_post_observer_entries(observer):
    started = _epoch(2026, 4, 25, 12)
    parsed = {"event_ts": _epoch(2026, 4, 25, 13)}
    assert observer._is_historical(parsed, started) is False


def test_is_historical_passes_when_timestamp_unparseable(observer):
    """No timestamp -> err on the side of processing rather than dropping."""
    started = _epoch(2026, 4, 25, 12)
    assert observer._is_historical({"event_ts": None}, started) is False


def test_historical_entries_are_filtered_in_a_seeded_log(observer):
    """End-to-end on the parse+filter path without spawning the tail loop.

    Seeds two raw log lines: one historical, one current. Verifies the
    main-loop predicate (`_is_historical`) correctly partitions them.
    """
    started = _epoch(2026, 4, 25, 12)

    historical = (
        "2020-01-01 00:00:00 nexus INFO event='mcp_server_starting' "
        "timestamp='2020-01-01T00:00:00Z' level='info' pid=111"
    )
    current = (
        "2026-04-25 12:01:00 nexus INFO event='mcp_server_starting' "
        "timestamp='2026-04-25T12:01:00Z' level='info' pid=222"
    )

    parsed_old = observer._parse_log_line(historical)
    parsed_new = observer._parse_log_line(current)
    assert parsed_old is not None and parsed_new is not None
    assert observer._is_historical(parsed_old, started) is True
    assert observer._is_historical(parsed_new, started) is False
