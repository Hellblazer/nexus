# SPDX-License-Identifier: AGPL-3.0-or-later
"""nexus-6pfgb.43 (RDR-236 Step 3): the ``Retired chunk keys`` doctor row, read from the retire-keys-sweep rung's own note
and its completion record, so ``nx doctor`` stays bounded (one T2 note, one ledger read, no dry run)."""
from __future__ import annotations

import inspect

import pytest

from nexus import health
from nexus.upgrade_ladder import retire_keys_state as rks


class Notes:
    def __init__(self, note) -> None:
        self.note = note

    def load(self, *, strict: bool = False):
        if isinstance(self.note, Exception):
            raise self.note
        return self.note


def _raise(exc: Exception):
    def boom():
        raise exc

    return boom


@pytest.fixture
def row_with(monkeypatch: pytest.MonkeyPatch):
    def make(note, recorded=None):
        monkeypatch.setattr(rks, "default_state_store", lambda: Notes(note))
        monkeypatch.setattr(rks, "_recorded_at", _raise(recorded) if isinstance(recorded, Exception) else (lambda: recorded))
        return rks.doctor_row()

    return make


def test_a_box_the_rung_never_measured_is_not_applicable(row_with) -> None:
    row = row_with(None)
    assert row.label == "Retired chunk keys" and row.ok and row.skipped
    assert "not applicable" in row.detail and "nx upgrade" in row.detail


def test_a_blocked_measurement_is_a_warning_with_the_full_text_the_count_and_the_verb(row_with) -> None:
    row = row_with({
        "outcome": "blocked", "retired_key_rows": 9, "tenant": "t1", "measured_at": "2026-11-16T01:00:00+00:00",
        "detail": "9 chunk row(s) hold retired metadata keys because keyless_notes=17: accept it with `--accept keyless_notes=17`",
    })
    assert row.warn and not row.ok and not row.skipped
    assert "9 chunk row(s) still carry retired metadata keys as of 2026-11-16T01:00:00+00:00" in row.detail
    assert "not a live count" in row.detail and "accept it with `--accept keyless_notes=17`" in row.detail
    assert any("nx t3 retire-keys --tenant t1" in f for f in row.fix_suggestions)
    assert any(f.startswith("Run: nx upgrade") for f in row.fix_suggestions)


def test_a_residue_measurement_warns_without_the_accept_verb(row_with) -> None:
    row = row_with({"outcome": "residue", "retired_key_rows": 2, "measured_at": "t", "detail": "2 with a wrong-width chash"})
    assert row.warn and not any("retire-keys --tenant" in f for f in row.fix_suggestions)


def test_a_clean_measurement_or_a_record_is_ok_and_the_record_says_it_is_not_a_live_count(row_with) -> None:
    clean = row_with({"outcome": "clean", "measured_at": "2026-11-16T01:00:00+00:00"})
    assert clean.ok and "no chunk row carried a retired key when last measured" in clean.detail
    swept = row_with({"outcome": "swept", "measured_at": "x"}, recorded="2026-11-16T02:00:00Z")
    assert swept.ok and "recorded 2026-11-16T02:00:00Z" in swept.detail and "not a live count" in swept.detail
    only_record = row_with(None, recorded="2026-11-16T02:00:00Z")
    assert only_record.ok and not only_record.skipped


def test_a_later_record_beats_an_older_blocked_note_and_a_later_note_beats_an_older_record(row_with) -> None:
    """The SWEPT save can fail silently, so a note still saying BLOCKED may be older than the sweep that verified."""
    older_note = {"outcome": "blocked", "retired_key_rows": 9, "measured_at": "2026-11-16T01:00:00+00:00", "detail": "held"}
    swept = row_with(older_note, recorded="2026-11-16T02:00:00Z")
    assert swept.ok and not swept.warn and "verified zero retired-key rows" in swept.detail
    residue = row_with({**older_note, "outcome": "residue"}, recorded="2026-11-16T02:00:00Z")
    assert residue.ok and not residue.warn
    newer_note = row_with({**older_note, "measured_at": "2026-11-16T03:00:00+00:00"}, recorded="2026-11-16T02:00:00Z")
    assert newer_note.warn and not newer_note.ok, "a measurement after the record says rows came back"
    unparsable = row_with({**older_note, "measured_at": "yesterday"}, recorded="2026-11-16T02:00:00Z")
    assert unparsable.warn, "a time that cannot be compared never hides a blocked measurement"
    same_instant = row_with({**older_note, "measured_at": "2026-11-16T02:00:00+00:00"}, recorded="2026-11-16T02:00:00Z")
    assert same_instant.ok, "the sweep that verified at the instant it measured wins"


def test_an_unreadable_note_or_ledger_is_never_a_crash_and_reads_as_not_measured(row_with) -> None:
    assert row_with(RuntimeError("t2 down")).skipped
    assert row_with(None, recorded=RuntimeError("ledger down")).skipped


def test_the_health_check_returns_the_one_row_and_is_wired_into_the_doctor_run() -> None:
    assert callable(health._check_retired_chunk_keys)
    assert '_step("_check_retired_chunk_keys"' in inspect.getsource(health)
