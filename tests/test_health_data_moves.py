# SPDX-License-Identifier: AGPL-3.0-or-later
"""nexus-f3yqd.2.17: ``nx doctor``'s data-move rows, from canned ``/v1/status`` bodies (RDR-246 Day 2 Operations).

One red row per ``blocked:`` or ``failed:`` move and none for a running one; a ``paused:`` move is a note, a ``stalled:``
one a warning.
"""
from __future__ import annotations

import json
from pathlib import Path

import nexus.data_moves_health as dmh
import nexus.db.http_engine_status as http_status
import nexus.health as health
from nexus.health import HealthResult
from nexus.data_moves_health import ID_PREFIX, REPORTER_ID, SUMMARY_LABEL, WORKER_ID, check_data_moves


_FIXTURES = Path(__file__).resolve().parent / "fixtures" / "data_moves_need_operator_status"


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


def test_a_move_blocked_on_a_missing_prerequisite_is_a_warning_not_red(monkeypatch):
    monkeypatch.setattr(dmh, "_old_bundle_pending", lambda: True)  # the hint's bundle sentence: never this box's bundle
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


def test_the_prerequisite_hint_names_the_bundle_upgrade_only_while_the_bundle_lacks_pg_background(monkeypatch):
    """F33: after the swap the bundle has pg_background; the hint no longer sends the user to nx upgrade for it."""
    status = {"boot_work": [_classed("m1", "blocked: m1: pg_background is not installed", "missing_prerequisite")]}
    for pending, named in ((True, True), (False, False)):
        monkeypatch.setattr(dmh, "_old_bundle_pending", lambda p=pending: p)
        (row,) = [r for r in check_data_moves(status) if r.label == "Data move m1"]
        assert ("`nx upgrade` replaces that bundle" in " ".join(row.fix_suggestions)) is named
        assert "self-managed" in " ".join(row.fix_suggestions)


# ── a move the engine says needs an operator (RDR-246, nexus-p2m1x.22) ───────────────────────────────────────


def _paged(label: str, retries: int = 3, builds: bool = True, sqlstate: str | None = "57014",
           step: str | None = "on_complete", bound: int | None = 1400, working: bool = False) -> dict:
    return {
        "label": label, "retries": retries, "last_attempt_at": "2026-11-23T10:00:00Z",
        "next_retry_at": "2026-11-23T10:02:00Z", "sqlstate": sqlstate, "statement_timeout_ms": 1400,
        "terminates_index_builds": builds, "step": step, "bound_ms": bound, "retry_in_progress": working,
    }


def _needs_operator_status(*paged: dict, boot_work: list[dict] | None = None) -> dict:
    return {
        "boot_work": boot_work if boot_work is not None else [
            _classed(p["label"], f"blocked: {p['label']}: on_complete did not complete sqlstate=57014; needs an operator",
                     None) for p in paged
        ],
        "data_moves_need_operator": bool(paged),
        "data_moves_need_operator_labels": [p["label"] for p in paged],
        "data_moves_need_operator_moves": list(paged),
    }


def test_a_move_that_needs_an_operator_is_a_red_row_naming_the_label_the_bound_and_both_times():
    rows = check_data_moves(_needs_operator_status(_paged("rdr229-swap-chunks")))

    row = _by_label(rows)["Data move rdr229-swap-chunks"]
    assert row.ok is False and row.warn is False, "red, not a warning"
    assert "needs an operator: rdr229-swap-chunks" in row.detail
    assert "step on_complete missed its 1400 ms bound (SQLSTATE 57014)" in row.detail
    assert "last attempt 2026-11-23T10:00:00Z" in row.detail
    assert "next retry 2026-11-23T10:02:00Z" in row.detail
    hint = " ".join(row.fix_suggestions)
    assert "keeps retrying" in hint
    assert "event=data_move_needs_operator" in hint
    assert "ends the in-flight index builds" in hint
    assert any("transient_retries = 0" in s and "'rdr229-swap-chunks'" in s for s in row.fix_suggestions)
    assert len(_red(rows)) == 1, "one row for the move, not the plain blocked row as well"
    assert _by_label(rows)[SUMMARY_LABEL].ok


def test_a_lock_timeout_shows_its_own_bound_not_the_statement_timeout():
    row = _by_label(check_data_moves(_needs_operator_status(_paged("m", sqlstate="55P03", bound=700))))["Data move m"]

    assert "step on_complete missed its 700 ms bound (SQLSTATE 55P03)" in row.detail
    assert "1400" not in row.detail


def test_a_non_timeout_sqlstate_names_no_bound():
    for sqlstate in ("40001", "08006"):
        row = _by_label(check_data_moves(_needs_operator_status(_paged("m", sqlstate=sqlstate, bound=None))))["Data move m"]

        assert f"step on_complete failed with SQLSTATE {sqlstate}" in row.detail
        assert "bound" not in row.detail and " ms" not in row.detail


def test_a_bound_is_not_named_when_the_engine_sends_none_even_for_a_timeout():
    row = _by_label(check_data_moves(_needs_operator_status(_paged("m", bound=None, step=None))))["Data move m"]

    assert "bound" not in row.detail and "SQLSTATE 57014" in row.detail


def test_a_retry_in_progress_is_said_not_an_overdue_next_retry():
    row = _by_label(check_data_moves(_needs_operator_status(_paged("m", working=True))))["Data move m"]

    assert "a retry is in progress now" in row.detail
    assert "next retry" not in row.detail


def test_the_retry_now_hint_warns_when_the_attempt_ends_index_builds_and_not_otherwise():
    ends = _by_label(check_data_moves(_needs_operator_status(_paged("m", builds=True))))["Data move m"]
    plain = _by_label(check_data_moves(_needs_operator_status(_paged("m", builds=False))))["Data move m"]

    reset_ends = [s for s in ends.fix_suggestions if "transient_retries = 0" in s]
    reset_plain = [s for s in plain.fix_suggestions if "transient_retries = 0" in s]
    assert len(reset_ends) == 1 and len(reset_plain) == 1
    assert "fix the cause first" in reset_ends[0] and "ends the in-flight index builds again" in reset_ends[0]
    assert "fix the cause first" not in reset_plain[0]


def test_a_need_operator_move_without_the_index_build_claim_does_not_make_it():
    row = _by_label(check_data_moves(_needs_operator_status(_paged("m", retries=20, builds=False))))["Data move m"]

    assert row.ok is False and row.warn is False
    assert "index builds" not in row.detail
    assert "index builds" not in " ".join(row.fix_suggestions)
    assert "failed 20 times in a row" in row.detail
    assert "fix the cause first" not in " ".join(row.fix_suggestions)


def test_the_move_is_red_whatever_state_its_entry_shows_while_a_retry_runs():
    status = _needs_operator_status(_paged("m"), boot_work=[_entry("m", "working: m, retrying after being blocked since x")])

    row = _by_label(check_data_moves(status))["Data move m"]

    assert row.ok is False and row.warn is False and row.detail.startswith("needs an operator: m")


def test_a_listed_move_whose_entry_has_gone_is_still_a_red_row():
    status = _needs_operator_status(_paged("gone"), boot_work=[])

    assert [r.label for r in _red(check_data_moves(status))] == ["Data move gone"]


def test_a_control_move_not_in_the_list_keeps_its_plain_blocked_row():
    status = _needs_operator_status(_paged("paged"), boot_work=[
        _entry("paged", "blocked: paged: x; needs an operator"),
        _entry("control", "blocked: control: waiting for a background worker slot"),
    ])

    rows = _by_label(check_data_moves(status))

    assert rows["Data move paged"].detail.startswith("needs an operator: paged")
    assert rows["Data move control"].detail.startswith("blocked: control: waiting for a background worker slot")
    assert "needs an operator" not in rows["Data move control"].detail


def test_an_empty_list_changes_nothing_and_an_engine_without_the_field_keeps_the_plain_row():
    plain = {"boot_work": [_entry("m", "blocked: m: waiting for a background worker slot; needs an operator")]}
    empty = dict(plain, data_moves_need_operator=False, data_moves_need_operator_labels=[],
                 data_moves_need_operator_moves=[])

    for status in (plain, empty):
        row = _by_label(check_data_moves(status))["Data move m"]
        assert row.ok is False and row.warn is False
        assert row.detail.startswith("blocked: m: waiting for a background worker slot")


def test_the_pg_background_warning_is_untouched_beside_a_paged_move(monkeypatch):
    monkeypatch.setattr(dmh, "_old_bundle_pending", lambda: True)
    status = _needs_operator_status(_paged("paged"), boot_work=[
        _entry("paged", "blocked: paged: x; needs an operator"),
        _classed("waiting", "blocked: waiting: pg_background is not installed", "missing_prerequisite"),
    ])

    rows = _by_label(check_data_moves(status))

    assert rows["Data move waiting"].ok is False and rows["Data move waiting"].warn is True
    assert [r.label for r in _red(check_data_moves(status))] == ["Data move paged"]


def test_malformed_need_operator_fields_cost_nothing():
    status = {"boot_work": [_entry("m", "blocked: m: x")], "data_moves_need_operator_moves": [
        "junk", {"label": 3}, {"label": "m", "retries": "many", "last_attempt_at": None, "statement_timeout_ms": True},
    ]}

    row = _by_label(check_data_moves(status))["Data move m"]

    assert row.ok is False and row.warn is False
    assert "last attempt unknown" in row.detail and "next retry unknown" in row.detail
    assert "statement_timeout_ms" not in row.detail


def test_a_bool_is_not_a_retry_count():
    status = {"boot_work": [_entry("m", "blocked: m: x")], "data_moves_need_operator_moves": [
        {"label": "m", "retries": True, "bound_ms": True, "sqlstate": "57014", "step": "on_complete"},
    ]}

    row = _by_label(check_data_moves(status))["Data move m"]

    assert "failed many times in a row" in row.detail
    assert "bound" not in row.detail


def test_the_engines_own_status_shape_is_read():
    """The fixture is the key set and types the engine's ``/v1/status`` renders (``MoveNeedsOperatorIntegrationTest`` fails
    when the engine's body and this file differ), so the keys the doctor reads cannot drift from the Java."""
    fixture = json.loads((_FIXTURES / "paged.json").read_text())
    status = dict(fixture, boot_work=[_entry("rdr229-swap-chunks", "blocked: rdr229-swap-chunks: x; needs an operator")])

    rows = check_data_moves(status)

    row = _by_label(rows)["Data move rdr229-swap-chunks"]
    assert row.ok is False and row.warn is False
    assert "step on_complete missed its 700 ms bound (SQLSTATE 55P03)" in row.detail
    assert "last attempt 2026-11-23T10:00:00Z" in row.detail and "next retry 2026-11-23T10:02:00Z" in row.detail
    assert "each attempt ends in-flight index builds" in row.detail
    assert sorted(fixture["data_moves_need_operator_moves"][0]) == sorted(
        ["label", "retries", "last_attempt_at", "next_retry_at", "sqlstate", "statement_timeout_ms",
         "terminates_index_builds", "step", "bound_ms", "retry_in_progress"])
