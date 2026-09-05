# SPDX-License-Identifier: AGPL-3.0-or-later
"""RDR-129 B4 (nexus-uq8a4): dropped-best-effort-write meter.

A "drop" is an *unrecovered* best-effort T2 write — a chash dual-write the
daemon could not commit because memory.db's writer slot was held, and which
exhausted any retry. This module turns that previously-invisible debug line
into a number `nx doctor` surfaces. Tests pin the append/aggregate contract
and the missing-file-is-zero semantics.
"""
from __future__ import annotations

from pathlib import Path

from nexus import dropped_writes


def test_default_log_path_honours_env_override(tmp_path, monkeypatch):
    target = tmp_path / "drops.jsonl"
    monkeypatch.setenv("NX_DROPPED_WRITES_LOG_PATH", str(target))
    assert dropped_writes.default_log_path() == target


def test_missing_file_counts_zero(tmp_path, monkeypatch):
    monkeypatch.setenv("NX_DROPPED_WRITES_LOG_PATH", str(tmp_path / "absent.jsonl"))
    summary = dropped_writes.count_drops()
    assert summary.total == 0
    assert summary.rows == 0
    assert summary.last_ts is None
    assert summary.last_collection == ""


def test_record_then_count(tmp_path, monkeypatch):
    monkeypatch.setenv("NX_DROPPED_WRITES_LOG_PATH", str(tmp_path / "drops.jsonl"))
    dropped_writes.record_drop(
        hook="chash_dual_write_batch_hook",
        collection="code__nexus",
        rows=3,
        error="database is locked",
    )
    dropped_writes.record_drop(
        hook="chash_dual_write_batch_hook",
        collection="docs__nexus",
        rows=2,
        error="database is locked",
    )
    summary = dropped_writes.count_drops()
    assert summary.total == 2
    assert summary.rows == 5
    assert summary.last_collection == "docs__nexus"
    assert summary.last_ts is not None


def test_record_drop_never_raises_on_bad_path(tmp_path, monkeypatch):
    # Point the log at a path whose parent is a file, not a directory, so a
    # write would fail. The meter must swallow it — it runs inside a
    # best-effort hook whose contract forbids propagating.
    not_a_dir = tmp_path / "blocker"
    not_a_dir.write_text("x")
    monkeypatch.setenv(
        "NX_DROPPED_WRITES_LOG_PATH", str(not_a_dir / "nested" / "drops.jsonl")
    )
    dropped_writes.record_drop(
        hook="h", collection="c", rows=1, error="database is locked"
    )  # must not raise


def test_malformed_lines_are_skipped(tmp_path, monkeypatch):
    log = tmp_path / "drops.jsonl"
    log.write_text(
        '{"ts": "2026-05-27T00:00:00Z", "collection": "code__x", "rows": 4}\n'
        "not json at all\n"
        "\n"
    )
    monkeypatch.setenv("NX_DROPPED_WRITES_LOG_PATH", str(log))
    summary = dropped_writes.count_drops()
    assert summary.total == 1
    assert summary.rows == 4
    assert summary.last_collection == "code__x"


# ── recency decay window (nexus-gjv9b review fold-in, critique CRITICAL 2) ──

def test_fresh_drop_counts_as_recent(tmp_path, monkeypatch):
    monkeypatch.setenv("NX_DROPPED_WRITES_LOG_PATH", str(tmp_path / "drops.jsonl"))
    dropped_writes.record_drop(
        hook="routing_events", collection="", rows=1, error="connection refused",
    )
    summary = dropped_writes.count_drops()
    assert summary.total == 1
    assert summary.recent_total == 1
    assert summary.recent_last_hook == "routing_events"


def test_old_drop_does_not_count_as_recent_but_stays_in_lifetime_total(
    tmp_path, monkeypatch,
):
    log = tmp_path / "drops.jsonl"
    monkeypatch.setenv("NX_DROPPED_WRITES_LOG_PATH", str(log))
    log.write_text(
        '{"ts": "2020-01-01T00:00:00Z", "hook": "routing_events", '
        '"collection": "", "rows": 1, "error": "old"}\n'
    )
    summary = dropped_writes.count_drops()
    assert summary.total == 1, "lifetime total must still count the old drop"
    assert summary.recent_total == 0, "an aged-out drop must not count as recent"
    assert summary.recent_last_hook == ""


def test_recent_hours_param_is_honoured(tmp_path, monkeypatch):
    """A drop from 2 hours ago is 'recent' under a 1-hour window's
    complement (i.e. NOT recent under a 1h window, but IS recent under a
    3h window) -- proves the parameter actually gates the boundary,
    not just the (default 24h) common case."""
    import time as _time

    log = tmp_path / "drops.jsonl"
    monkeypatch.setenv("NX_DROPPED_WRITES_LOG_PATH", str(log))
    two_hours_ago = _time.strftime(
        "%Y-%m-%dT%H:%M:%SZ", _time.gmtime(_time.time() - 2 * 3600)
    )
    log.write_text(
        f'{{"ts": "{two_hours_ago}", "hook": "capability_census", '
        f'"collection": "", "rows": 1, "error": "x"}}\n'
    )

    assert dropped_writes.count_drops(recent_hours=1.0).recent_total == 0
    assert dropped_writes.count_drops(recent_hours=3.0).recent_total == 1


def test_mixed_old_and_fresh_recent_reflects_only_the_fresh_one(tmp_path, monkeypatch):
    log = tmp_path / "drops.jsonl"
    monkeypatch.setenv("NX_DROPPED_WRITES_LOG_PATH", str(log))
    log.write_text(
        '{"ts": "2020-01-01T00:00:00Z", "hook": "chash_dual_write_batch_hook", '
        '"collection": "", "rows": 1, "error": "old"}\n'
    )
    dropped_writes.record_drop(
        hook="routing_events", collection="", rows=2, error="fresh",
    )
    summary = dropped_writes.count_drops()
    assert summary.total == 2
    assert summary.rows == 3
    assert summary.recent_total == 1
    assert summary.recent_last_hook == "routing_events"


def test_malformed_ts_never_counts_as_recent_and_never_raises(tmp_path, monkeypatch):
    log = tmp_path / "drops.jsonl"
    monkeypatch.setenv("NX_DROPPED_WRITES_LOG_PATH", str(log))
    log.write_text(
        '{"ts": "not-a-timestamp", "hook": "routing_events", '
        '"collection": "", "rows": 1, "error": "x"}\n'
    )
    summary = dropped_writes.count_drops()  # must not raise
    assert summary.total == 1
    assert summary.recent_total == 0
