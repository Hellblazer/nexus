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


# ---------------------------------------------------------------------------
# Cause classification (nexus-gjv9b review fold-in round 3, critique
# CRITICAL 2 / code-review item 1): record_drop's error field already
# carries the distinguishing text for most producers -- classify_drop_cause
# turns it into a short, stable vocabulary so count_drops can report the
# DOMINANT failure mode in its recency window.
# ---------------------------------------------------------------------------


def test_classify_drop_cause_guard_refused():
    msg = (
        "STOP: refusing a WRITE to 'https://api.example.test'. This "
        "process's nexus package resolves from a dev checkout"
    )
    assert dropped_writes.classify_drop_cause(msg) == "guard_refused"


def test_classify_drop_cause_401():
    assert dropped_writes.classify_drop_cause(
        "HttpTelemetryStore.record_capability_census failed: HTTP 401: unauthorized"
    ) == "401"


def test_classify_drop_cause_403():
    assert dropped_writes.classify_drop_cause(
        "HttpTelemetryStore.record_routing_event failed: HTTP 403: forbidden"
    ) == "403"


def test_classify_drop_cause_5xx():
    assert dropped_writes.classify_drop_cause(
        "HttpTelemetryStore.record_capability_census failed: HTTP 503: unavailable"
    ) == "5xx"


def test_classify_drop_cause_timeout():
    assert dropped_writes.classify_drop_cause("ReadTimeout: timed out") == "timeout"


def test_classify_drop_cause_connect():
    assert dropped_writes.classify_drop_cause(
        "ConnectError: [Errno 61] Connection refused"
    ) == "connect"


def test_classify_drop_cause_unresolvable():
    assert dropped_writes.classify_drop_cause(
        "service_url is set but no service_token is resolvable"
    ) == "unresolvable"


def test_classify_drop_cause_unrecognized_is_other():
    assert dropped_writes.classify_drop_cause("something completely unexpected") == "other"


def test_classify_drop_cause_empty_is_unclassified():
    assert dropped_writes.classify_drop_cause("") == ""
    assert dropped_writes.classify_drop_cause("   ") == ""


def test_record_drop_auto_classifies_cause_from_error(tmp_path, monkeypatch):
    log = tmp_path / "drops.jsonl"
    monkeypatch.setenv("NX_DROPPED_WRITES_LOG_PATH", str(log))
    dropped_writes.record_drop(
        hook="capability_census", collection="", rows=1,
        error="STOP: refusing a WRITE to 'https://x'.",
    )
    line = log.read_text().splitlines()[0]
    import json as _json
    rec = _json.loads(line)
    assert rec["cause"] == "guard_refused"


def test_record_drop_explicit_cause_wins_over_auto_classification(tmp_path, monkeypatch):
    """The routing hook's stdlib urllib layer already knows its failure
    mode precisely from the transport itself -- more reliably than any
    text match on an error string could -- so an explicit cause is never
    overridden by classify_drop_cause."""
    log = tmp_path / "drops.jsonl"
    monkeypatch.setenv("NX_DROPPED_WRITES_LOG_PATH", str(log))
    dropped_writes.record_drop(
        hook="routing_events", collection="", rows=1,
        error="routing_events POST failed: 401", cause="401",
    )
    import json as _json
    rec = _json.loads(log.read_text().splitlines()[0])
    assert rec["cause"] == "401"


# ---------------------------------------------------------------------------
# Dominant cause + guard-refused-only window (nexus-gjv9b review fold-in
# round 3, critique CRITICAL 2 / code-review item 1)
# ---------------------------------------------------------------------------


def test_count_drops_reports_dominant_cause_in_window(tmp_path, monkeypatch):
    monkeypatch.setenv("NX_DROPPED_WRITES_LOG_PATH", str(tmp_path / "drops.jsonl"))
    for _ in range(3):
        dropped_writes.record_drop(
            hook="routing_events", collection="", rows=1, error="x", cause="401",
        )
    dropped_writes.record_drop(
        hook="routing_events", collection="", rows=1, error="x", cause="timeout",
    )
    summary = dropped_writes.count_drops()
    assert summary.recent_total == 4
    assert summary.recent_dominant_cause == "401"
    assert summary.recent_dominant_cause_count == 3


def test_count_drops_dominant_cause_tiebreak_is_most_recent(tmp_path, monkeypatch):
    log = tmp_path / "drops.jsonl"
    monkeypatch.setenv("NX_DROPPED_WRITES_LOG_PATH", str(log))
    import json as _json
    import time as _time
    now = _time.time()
    lines = [
        _json.dumps({
            "ts": _time.strftime("%Y-%m-%dT%H:%M:%SZ", _time.gmtime(now - 100)),
            "hook": "routing_events", "collection": "", "rows": 1,
            "error": "x", "cause": "connect",
        }),
        _json.dumps({
            "ts": _time.strftime("%Y-%m-%dT%H:%M:%SZ", _time.gmtime(now - 10)),
            "hook": "routing_events", "collection": "", "rows": 1,
            "error": "x", "cause": "timeout",
        }),
    ]
    log.write_text("\n".join(lines) + "\n")
    summary = dropped_writes.count_drops()
    assert summary.recent_dominant_cause == "timeout", (
        "a 1-1 tie between two causes must break on whichever was seen "
        "MOST RECENTLY, mirroring recent_last_hook's own framing"
    )


def test_count_drops_recent_all_guard_refused_true_when_every_in_window_drop_is(
    tmp_path, monkeypatch,
):
    monkeypatch.setenv("NX_DROPPED_WRITES_LOG_PATH", str(tmp_path / "drops.jsonl"))
    for _ in range(3):
        dropped_writes.record_drop(
            hook="capability_census", collection="", rows=1,
            error="STOP: refusing a WRITE", cause="guard_refused",
        )
    summary = dropped_writes.count_drops()
    assert summary.recent_all_guard_refused is True


def test_count_drops_recent_all_guard_refused_false_with_one_other_cause_mixed_in(
    tmp_path, monkeypatch,
):
    monkeypatch.setenv("NX_DROPPED_WRITES_LOG_PATH", str(tmp_path / "drops.jsonl"))
    for _ in range(3):
        dropped_writes.record_drop(
            hook="capability_census", collection="", rows=1,
            error="STOP: refusing a WRITE", cause="guard_refused",
        )
    dropped_writes.record_drop(
        hook="capability_census", collection="", rows=1, error="x", cause="401",
    )
    summary = dropped_writes.count_drops()
    assert summary.recent_all_guard_refused is False, (
        "a single non-guard-refused drop in the window must keep the WARN "
        "path live -- this flag is all-or-nothing, not 'mostly'"
    )


def test_count_drops_recent_all_guard_refused_false_when_cause_unclassified(
    tmp_path, monkeypatch,
):
    """An unclassified (empty) cause must NOT be treated as confirmed
    guard_refused -- only an AFFIRMATIVE classification may excuse the
    window."""
    monkeypatch.setenv("NX_DROPPED_WRITES_LOG_PATH", str(tmp_path / "drops.jsonl"))
    dropped_writes.record_drop(
        hook="capability_census", collection="", rows=1, error="",
    )
    summary = dropped_writes.count_drops()
    assert summary.recent_all_guard_refused is False


def test_count_drops_recent_all_guard_refused_false_with_no_drops():
    summary = dropped_writes.DropSummary()
    assert summary.recent_all_guard_refused is False
