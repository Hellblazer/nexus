# SPDX-License-Identifier: AGPL-3.0-or-later
"""nexus-te885.10 part 2: the verify-fill rowid watermark.

Unit layer: the trust gate (engine-count-validated) + advance/load
roundtrip. The orchestrator-level pass-3 shortcut is covered in
``test_rdr178_acceptance.py`` (same corpus harness as the drift tests).
"""
from __future__ import annotations

from nexus.migration.verify_fill_watermark import (
    WATERMARK_TABLES,
    advance_watermark,
    usable_min_rowid,
)

URL = "http://127.0.0.1:9999"


def _isolate(tmp_path, monkeypatch):
    monkeypatch.setenv("NEXUS_CONFIG_DIR", str(tmp_path / "cfg"))


def test_no_watermark_file_full_probe(tmp_path, monkeypatch):
    _isolate(tmp_path, monkeypatch)
    assert usable_min_rowid(URL, "tier_writes", engine_count=100) == 0


def test_trust_gate_requires_engine_count(tmp_path, monkeypatch):
    """A pre-whitelist engine returns no count for these tables: the stored
    watermark must NOT be trusted — fail-safe full probe."""
    _isolate(tmp_path, monkeypatch)
    advance_watermark(URL, "tier_writes", max_rowid=500, target_count=480)
    assert usable_min_rowid(URL, "tier_writes", engine_count=None) == 0


def test_trust_gate_invalidates_on_target_shrink(tmp_path, monkeypatch):
    """A LOWER live count than recorded means target rows were deleted (e.g.
    a rollback) — the watermark is invalid and the full probe runs."""
    _isolate(tmp_path, monkeypatch)
    advance_watermark(URL, "tier_writes", max_rowid=500, target_count=480)
    assert usable_min_rowid(URL, "tier_writes", engine_count=479) == 0


def test_trusted_watermark_returns_rowid_floor(tmp_path, monkeypatch):
    _isolate(tmp_path, monkeypatch)
    advance_watermark(URL, "tier_writes", max_rowid=500, target_count=480)
    assert usable_min_rowid(URL, "tier_writes", engine_count=480) == 500
    assert usable_min_rowid(URL, "tier_writes", engine_count=9000) == 500  # live growth OK


def test_watermarks_are_per_service_url_and_table(tmp_path, monkeypatch):
    """A different target (fresh service after rollback+re-init at a new URL)
    or a different table never inherits another watermark."""
    _isolate(tmp_path, monkeypatch)
    advance_watermark(URL, "tier_writes", max_rowid=500, target_count=480)
    assert usable_min_rowid("http://other:1", "tier_writes", engine_count=480) == 0
    assert usable_min_rowid(URL, "frecency", engine_count=480) == 0


def test_empty_service_url_never_trusts_or_advances(tmp_path, monkeypatch):
    _isolate(tmp_path, monkeypatch)
    advance_watermark("", "tier_writes", max_rowid=500, target_count=480)  # no-op
    assert usable_min_rowid("", "tier_writes", engine_count=480) == 0


def test_watermark_tables_disjoint_from_verify_tables():
    """Load-bearing separation: putting these four into _VERIFY_TABLES would
    let the outer loop skip them on an UNSOUND count parity (dup collapse +
    live writes). The watermark is their ONLY gate."""
    from nexus.migration.orchestrator import _VERIFY_TABLES

    mapped_telemetry = {t for (s, t) in _VERIFY_TABLES if s == "telemetry"}
    assert mapped_telemetry.isdisjoint(WATERMARK_TABLES.keys())
