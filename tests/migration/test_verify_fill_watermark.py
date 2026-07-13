# SPDX-License-Identifier: AGPL-3.0-or-later
"""nexus-te885.10 part 2: the verify-fill rowid watermark.

Unit layer: the trust gate (engine-count-validated) + advance/load
roundtrip. The orchestrator-level pass-3 shortcut is covered in
``test_rdr178_acceptance.py`` (same corpus harness as the drift tests).
"""
from __future__ import annotations

from nexus.migration.orchestrator import _VERIFY_TABLES
from nexus.migration.verify_fill_watermark import (
    WATERMARK_TABLES,
    advance_watermark,
    usable_min_rowid,
)

URL = "http://127.0.0.1:9999"
TEN = "default"


def _isolate(tmp_path, monkeypatch):
    monkeypatch.setenv("NEXUS_CONFIG_DIR", str(tmp_path / "cfg"))


def test_no_watermark_file_full_probe(tmp_path, monkeypatch):
    _isolate(tmp_path, monkeypatch)
    assert usable_min_rowid(URL, TEN, "tier_writes", engine_count=100) == 0


def test_trust_gate_requires_engine_count(tmp_path, monkeypatch):
    """A pre-whitelist engine returns no count for these tables: the stored
    watermark must NOT be trusted — fail-safe full probe."""
    _isolate(tmp_path, monkeypatch)
    advance_watermark(URL, TEN, "tier_writes", max_rowid=500, target_count=480)
    assert usable_min_rowid(URL, TEN, "tier_writes", engine_count=None) == 0


def test_trust_gate_invalidates_on_target_shrink(tmp_path, monkeypatch):
    """A LOWER live count than recorded means target rows were deleted (e.g.
    a rollback) — the watermark is invalid and the full probe runs."""
    _isolate(tmp_path, monkeypatch)
    advance_watermark(URL, TEN, "tier_writes", max_rowid=500, target_count=480)
    assert usable_min_rowid(URL, TEN, "tier_writes", engine_count=479) == 0


def test_trusted_watermark_returns_rowid_floor(tmp_path, monkeypatch):
    _isolate(tmp_path, monkeypatch)
    advance_watermark(URL, TEN, "tier_writes", max_rowid=500, target_count=480)
    assert usable_min_rowid(URL, TEN, "tier_writes", engine_count=480) == 500
    assert usable_min_rowid(URL, TEN, "tier_writes", engine_count=9000) == 500  # live growth OK


def test_watermarks_are_per_service_url_and_table(tmp_path, monkeypatch):
    """A different target (fresh service after rollback+re-init at a new URL)
    or a different table never inherits another watermark."""
    _isolate(tmp_path, monkeypatch)
    advance_watermark(URL, TEN, "tier_writes", max_rowid=500, target_count=480)
    assert usable_min_rowid("http://other:1", TEN, "tier_writes", engine_count=480) == 0
    assert usable_min_rowid(URL, TEN, "frecency", engine_count=480) == 0


def test_empty_service_url_never_trusts_or_advances(tmp_path, monkeypatch):
    _isolate(tmp_path, monkeypatch)
    advance_watermark("", TEN, "tier_writes", max_rowid=500, target_count=480)  # no-op
    assert usable_min_rowid("", TEN, "tier_writes", engine_count=480) == 0


def test_watermark_tables_disjoint_from_verify_tables():
    """Load-bearing separation: putting these four into _VERIFY_TABLES would
    let the outer loop skip them on an UNSOUND count parity (dup collapse +
    live writes). The watermark is their ONLY gate."""
    mapped_telemetry = {t for (s, t) in _VERIFY_TABLES if s == "telemetry"}
    assert mapped_telemetry.isdisjoint(WATERMARK_TABLES.keys())


# ── nexus-24p05: retention-marked tables ──────────────────────────────────────


def test_marked_table_distrusts_without_live_marker(tmp_path, monkeypatch):
    """Old engine (no marker route) or transport failure -> marker None ->
    full probe, even with a healthy count."""
    _isolate(tmp_path, monkeypatch)
    advance_watermark(URL, TEN, "relevance_log", max_rowid=300, target_count=100,
                      retention_marker=0)
    assert usable_min_rowid(URL, TEN, "relevance_log", engine_count=100,
                            retention_marker=None) == 0


def test_marked_table_distrusts_on_marker_reset(tmp_path, monkeypatch):
    """A live marker BELOW the recorded baseline = fresh schema (rollback) —
    even when live inserts pushed the count back up past the recorded value
    (the exact offset scenario that blinds the count-only gate)."""
    _isolate(tmp_path, monkeypatch)
    advance_watermark(URL, TEN, "relevance_log", max_rowid=300, target_count=100,
                      retention_marker=7)
    assert usable_min_rowid(URL, TEN, "relevance_log", engine_count=250,
                            retention_marker=0) == 0


def test_marked_table_trusts_when_marker_monotonic(tmp_path, monkeypatch):
    """Ordinary sweep activity bumps the marker ABOVE the baseline — the
    sweep's domain (expired rows) is disjoint from the fill's fresh window,
    so a higher marker never invalidates."""
    _isolate(tmp_path, monkeypatch)
    advance_watermark(URL, TEN, "relevance_log", max_rowid=300, target_count=100,
                      retention_marker=7)
    assert usable_min_rowid(URL, TEN, "relevance_log", engine_count=100,
                            retention_marker=7) == 300
    assert usable_min_rowid(URL, TEN, "relevance_log", engine_count=140,
                            retention_marker=42) == 300


def test_marked_table_never_advances_without_marker(tmp_path, monkeypatch):
    """No live marker at advance time -> no baseline can be recorded -> no
    watermark is written at all (rather than one that could never distrust)."""
    _isolate(tmp_path, monkeypatch)
    advance_watermark(URL, TEN, "relevance_log", max_rowid=300, target_count=100,
                      retention_marker=None)
    assert usable_min_rowid(URL, TEN, "relevance_log", engine_count=100,
                            retention_marker=0) == 0


def test_unmarked_tables_ignore_the_marker_argument(tmp_path, monkeypatch):
    _isolate(tmp_path, monkeypatch)
    advance_watermark(URL, TEN, "tier_writes", max_rowid=500, target_count=480)
    assert usable_min_rowid(URL, TEN, "tier_writes", engine_count=480,
                            retention_marker=None) == 500


def test_bootstrap_zero_marker_is_backstopped_by_count(tmp_path, monkeypatch):
    """Review 68509ac8 High-1: with recorded_marker=0 (advanced before any
    sweep ever fired) the marker check is vacuous (0 >= 0) — the COUNT check
    is what carries that regime: a rollback to a point before a zero-delete
    advance necessarily regressed the row count too (no deletes had occurred,
    so inserts were the only movement). Pin both halves."""
    _isolate(tmp_path, monkeypatch)
    advance_watermark(URL, TEN, "relevance_log", max_rowid=300, target_count=100,
                      retention_marker=0)
    # Marker check vacuous BUT count regressed -> the count gate distrusts.
    assert usable_min_rowid(URL, TEN, "relevance_log", engine_count=99,
                            retention_marker=0) == 0
    # Count intact + marker vacuous -> trusted (correct: nothing was deleted
    # as of the advance, so count-non-decrease IS the full soundness story).
    assert usable_min_rowid(URL, TEN, "relevance_log", engine_count=100,
                            retention_marker=0) == 300


def test_horizon_constant_is_single_sourced():
    """Review 68509ac8 Medium-3: the sweep default and the fill window are
    the SAME object — a tightened sweep with a stale fill window silently
    reintroduces the resurrect bug. Signature-default introspection so a
    hand-typed 90 anywhere breaks this."""
    import inspect

    from nexus.db.t2.telemetry import RELEVANCE_LOG_RETENTION_DAYS, Telemetry
    from nexus.migration.verify_fill_watermark import RETENTION_HORIZON_TABLES

    assert RETENTION_HORIZON_TABLES["relevance_log"] is RELEVANCE_LOG_RETENTION_DAYS
    sweep_default = inspect.signature(
        Telemetry.expire_relevance_log
    ).parameters["days"].default
    assert sweep_default == RELEVANCE_LOG_RETENTION_DAYS


def test_watermarks_are_tenant_scoped(tmp_path, monkeypatch):
    """Critique 68509ac8: one cloud engine URL serves many RLS tenants — the
    counts/markers the trust gate compares are tenant-scoped, so tenant B
    must never trust a floor recorded against tenant A's values."""
    _isolate(tmp_path, monkeypatch)
    advance_watermark(URL, "tenant-a", "tier_writes", max_rowid=500, target_count=480)
    assert usable_min_rowid(URL, "tenant-a", "tier_writes", engine_count=480) == 500
    assert usable_min_rowid(URL, "tenant-b", "tier_writes", engine_count=480) == 0
