# SPDX-License-Identifier: AGPL-3.0-or-later
"""nexus-f3yqd.2.17: ``nx doctor``'s data-move rows, from canned ``/v1/status`` bodies (RDR-246 Day 2 Operations).

One red row per ``blocked:`` or ``failed:`` move and none for a running one; a ``paused:`` move is a note, a ``stalled:``
one a warning.
"""
from __future__ import annotations

import nexus.db.http_engine_status as http_status
import nexus.health as health
from nexus.health import HealthResult
from nexus.data_moves_health import ID_PREFIX, REPORTER_ID, SUMMARY_LABEL, WORKER_ID, check_data_moves


def _entry(label: str, what: str, done: int | None = 3, total: int | None = 20) -> dict:
    return {
        "id": f"{ID_PREFIX}{label}", "what": what, "started_at": "2026-11-17T01:00:00Z", "done": done, "total": total,
        "unit": "chunks", "safe_to_stop": True, "on_stop": "resumes from the last committed batch", "step": None,
    }


def _red(rows: list[HealthResult]) -> list[HealthResult]:
    return [r for r in rows if not r.ok and not r.warn]


def _by_label(rows: list[HealthResult]) -> dict[str, HealthResult]:
    return {r.label: r for r in rows}


def test_a_blocked_move_is_one_red_row_with_its_reason():
    status = {"boot_work": [_entry("m1", "blocked: m1: waiting for a background worker slot (blocked since 2026-11-17T01:00:00Z)")]}

    rows = check_data_moves(status)

    red = _red(rows)
    assert [r.label for r in red] == ["Data move m1"]
    assert red[0].detail.startswith("blocked: m1: waiting for a background worker slot")
    assert "3 of 20 chunks done" in red[0].detail
    assert red[0].fatal is False
    assert any("transient_retries = 0" in s and "'m1'" in s for s in red[0].fix_suggestions)


def test_a_failed_move_is_one_red_row_with_its_failure_and_the_way_out():
    status = {"boot_work": [_entry("m2", "failed: m2: verification found 3 old-shape rows")]}

    red = _red(check_data_moves(status))

    assert [r.label for r in red] == ["Data move m2"]
    assert red[0].detail.startswith("failed: m2: verification found 3 old-shape rows")
    assert any("supersedes" in s and "'m2'" in s for s in red[0].fix_suggestions)


def test_a_running_move_has_no_red_row_and_no_row_of_its_own():
    status = {"boot_work": [
        _entry("w", "working: w"),
        _entry("q", "queued: q (the worker has not reached it yet)"),
        _entry("c", "runs complete: c (verification pending)", done=20),
    ]}

    rows = check_data_moves(status)

    assert _red(rows) == []
    assert [r.label for r in rows] == [SUMMARY_LABEL]
    assert rows[0].ok and "3 open or failed" in rows[0].detail


def test_one_red_row_per_blocked_or_failed_move_among_others():
    status = {"boot_work": [
        _entry("a", "blocked: a: dml_utils_owner is missing"),
        _entry("b", "working: b, retrying after being blocked since 2026-11-17T01:00:00Z"),
        _entry("c", "failed: c: run c/default raised sqlstate=42883"),
        _entry("d", "blocked: d: dml_utils_owner is missing"),
    ]}

    rows = check_data_moves(status)

    assert sorted(r.label for r in _red(rows)) == ["Data move a", "Data move c", "Data move d"]
    assert _by_label(rows)[SUMMARY_LABEL].ok


def test_a_paused_move_is_a_note_not_red():
    status = {"boot_work": [_entry("p", "paused: p by alice (an operator holds it; nothing is lost)")]}

    rows = check_data_moves(status)

    assert _red(rows) == []
    paused = _by_label(rows)["Data move p"]
    assert paused.ok and paused.warn is False
    assert paused.detail.startswith("paused: p by alice")
    assert any("paused_at = NULL" in s for s in paused.fix_suggestions)


def test_a_stalled_call_is_a_warning_not_red():
    status = {"boot_work": [_entry("s", "stalled: s: no chunk has completed for 25 m; its CALL (run s, backend pid 4242) is still open. Ending it with pg_cancel_backend(4242) makes the worker retry it")]}

    rows = check_data_moves(status)

    assert _red(rows) == []
    stalled = _by_label(rows)["Data move s"]
    assert stalled.ok is False and stalled.warn is True
    assert "pg_cancel_backend(4242)" in stalled.detail


def _classed(label: str, what: str, reason_class: str | None) -> dict:
    entry = _entry(label, what)
    if reason_class is not None:
        entry["reason_class"] = reason_class
    return entry


def test_a_move_blocked_on_a_missing_prerequisite_is_a_warning_not_red():
    status = {"boot_work": [_classed("m1", "blocked: m1: pg_background is not installed (blocked since 2026-11-17T01:00:00Z)",
                                     "missing_prerequisite")]}

    rows = check_data_moves(status)

    assert _red(rows) == []
    row = _by_label(rows)["Data move m1"]
    assert row.ok is False and row.warn is True
    assert row.detail.startswith("blocked: m1: pg_background is not installed")
    hint = " ".join(row.fix_suggestions)
    assert "`nx upgrade` replaces that bundle" in hint and "install-binary" not in hint
    assert "self-managed" in hint and "nothing was changed" in hint
    assert "restart the engine" in hint and "retries by itself" not in hint
    assert _by_label(rows)[SUMMARY_LABEL].ok


def test_a_move_blocked_for_another_reason_stays_red_even_beside_a_prerequisite_one():
    status = {"boot_work": [
        _classed("a", "blocked: a: pg_background is not installed", "missing_prerequisite"),
        _classed("b", "blocked: b: library role dml_utils_owner is missing", None),
        _classed("c", "blocked: c: waiting for a background worker slot", "some_other_class"),
    ]}

    rows = check_data_moves(status)

    assert sorted(r.label for r in _red(rows)) == ["Data move b", "Data move c"]
    assert _by_label(rows)["Data move a"].warn is True


def test_a_failed_move_stays_red_whatever_class_it_carries():
    status = {"boot_work": [_classed("f", "failed: f: pg_background is not installed", "missing_prerequisite")]}

    red = _red(check_data_moves(status))

    assert [r.label for r in red] == ["Data move f"]


def test_a_dml_utils_entry_point_miss_without_the_class_stays_red():
    status = {"boot_work": [_entry("d", "blocked: d: dml_utils.run_migration_chunks_committing is not installed")]}

    red = _red(check_data_moves(status))

    assert [r.label for r in red] == ["Data move d"]


def test_an_older_engine_entry_without_the_class_stays_red_as_before():
    status = {"boot_work": [_entry("old", "blocked: old: pg_background is not installed")]}

    red = _red(check_data_moves(status))

    assert [r.label for r in red] == ["Data move old"]


def test_a_checksum_drift_reason_reaches_the_row_with_its_recovery_hint():
    reason = (
        "blocked: m: a library changelog failed: Validation Failed: 1 changesets check sum "
        "db/vendor/ddl_utils/changes/changes.xml::001-create-schema::x was: 9:a but is now: 9:b | recovery: a library "
        "re-pin changed changeset 001-create-schema after the install had applied it (library changesets are not runOnChange)"
    )

    red = _red(check_data_moves({"boot_work": [_entry("m", reason)]}))

    assert len(red) == 1
    assert "001-create-schema" in red[0].detail and "not runOnChange" in red[0].detail


def test_other_boot_work_is_not_a_move():
    status = {"boot_work": [
        {"id": "pci-reconcile", "what": "blocked: something else", "done": 1, "total": 2, "unit": "indexes"},
        {"id": "migration", "what": "failed: not a move"},
    ]}

    rows = check_data_moves(status)

    assert _red(rows) == []
    assert rows[0].label == SUMMARY_LABEL and "no data move" in rows[0].detail


def test_no_moves_is_one_green_row():
    rows = check_data_moves({"boot_work": []})

    assert len(rows) == 1 and rows[0].ok and "no data move is open or failed" in rows[0].detail


def test_an_unreadable_status_or_an_engine_without_boot_work_is_not_applicable_and_not_red():
    for status in (None, {}, {"boot_work": "nope"}, {"other": 1}):
        rows = check_data_moves(status)
        assert len(rows) == 1 and rows[0].ok and rows[0].detail.startswith("not applicable")


def test_a_malformed_entry_costs_its_own_row_never_the_check():
    status = {"boot_work": [
        {"id": f"{ID_PREFIX}x"},                       # no what
        {"id": f"{ID_PREFIX}y", "what": 7},            # what is not text
        {"id": f"{ID_PREFIX}z", "what": "blocked: z: r", "done": "many", "total": None},
        "not even a dict",
    ]}

    rows = check_data_moves(status)

    assert [r.label for r in _red(rows)] == ["Data move z"]


def test_unset_status_fetches_its_own(monkeypatch):
    monkeypatch.setattr(http_status, "fetch_engine_status", lambda *a, **k: {"boot_work": [_entry("f", "failed: f: x")]})

    assert [r.label for r in _red(check_data_moves())] == ["Data move f"]


def test_the_doctor_runs_the_row_on_the_status_it_fetched(monkeypatch):
    """``run_health_checks`` is called for real with a canned status; every other check is stepped over (a stepper that runs
    only this one), so the call proves the row is in the sweep, under its name, handed the status the caller passed, and
    its red rows reach the sweep's result."""
    ran: list[str] = []

    def stepper(_progress):
        def step(name, fn):
            ran.append(name)
            return list(fn()) if name == "_check_data_moves" else []
        return step

    monkeypatch.setattr(health, "_row_stepper", stepper)
    status = {"boot_work": [_entry("m1", "blocked: m1: waiting for a background worker slot")]}

    results, _local = health.run_health_checks(engine_status=status)

    assert "_check_data_moves" in ran
    assert [r.label for r in _red(results)] == ["Data move m1"]
    assert any(r.label == SUMMARY_LABEL for r in results)


def test_a_worker_that_is_not_running_is_one_red_row_and_not_a_green_summary():
    status = {"boot_work": [{
        "id": WORKER_ID,
        "what": "blocked: the data-move worker is not running, so no data move is worked (the engine log has "
                "event=data_move_worker_not_started with the cause)",
    }]}

    rows = check_data_moves(status)

    assert [r.label for r in _red(rows)] == ["Data move worker"]
    assert "not running" in rows[0].detail
    assert any("data_move_worker_not_started" in s for s in rows[0].fix_suggestions)


def test_a_reporter_that_is_not_running_is_one_red_row_beside_the_moves_it_cannot_see():
    status = {"boot_work": [{
        "id": REPORTER_ID,
        "what": "blocked: the data-move boot_work reporter is not running, so no data move is reported (the engine log "
                "has event=data_move_boot_work_not_started with the cause)",
    }]}

    rows = check_data_moves(status)

    assert [r.label for r in _red(rows)] == ["Data move reporting"]
    assert any("data_move_boot_work_not_started" in s for s in rows[0].fix_suggestions)
    assert not any(r.label == SUMMARY_LABEL and "no data move is open" in r.detail for r in rows), "never a green summary"


def test_the_hints_say_where_the_text_the_status_leaves_out_is():
    status = {"boot_work": [
        _entry("b", "blocked: b: a CALL raised an error sqlstate=40001"),
        _entry("f", "failed: f: a CALL raised an error sqlstate=22023 (the failure text: SELECT failure FROM nexus.data_moves WHERE label = 'f')"),
        _entry("s", "stalled: s: no chunk has completed for 20 m; its CALL is still open"),
    ]}

    rows = _by_label(check_data_moves(status))

    assert any("event=data_move_blocked" in h for h in rows["Data move b"].fix_suggestions)
    assert any("SELECT failure FROM nexus.data_moves WHERE label = 'f'" in h for h in rows["Data move f"].fix_suggestions)
    assert any("event=data_move_call" in h and "NX_DATA_MOVE_STALL_MINUTES" in h for h in rows["Data move s"].fix_suggestions)


def test_the_sweeps_row_is_a_check_star_name_so_a_blanket_stub_covers_it(monkeypatch):
    """Doctor tests step over the sweep by replacing every ``health._check_*``; a data-move row outside that name would
    fetch the engine's status for real and trip the storage-touch guard (develop red 20b338640). With every ``_check_*``
    replaced, running the sweep's data-move step must call the replacement and never reach the engine."""
    called: list[str] = []
    for name in dir(health):
        if name.startswith("_check_") and callable(getattr(health, name)):
            monkeypatch.setattr(health, name, lambda *a, _n=name, **k: called.append(_n) or [])

    def boom(*a, **k):
        raise AssertionError("the sweep's data-move step reached past the stub")

    monkeypatch.setattr(http_status, "fetch_engine_status", boom)
    monkeypatch.setattr("nexus.data_moves_health.check_data_moves", boom)

    def stepper(_progress):
        def step(name, fn):
            return list(fn()) if name == "_check_data_moves" else []
        return step

    monkeypatch.setattr(health, "_row_stepper", stepper)
    health.run_health_checks(engine_status={"boot_work": []})

    assert "_check_data_moves" in called
