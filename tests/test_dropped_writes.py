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
