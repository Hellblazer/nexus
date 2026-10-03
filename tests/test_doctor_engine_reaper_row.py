# SPDX-License-Identifier: AGPL-3.0-or-later
"""nx doctor's "Engine reaper" row (nexus-wbfpw.56, RDR-192 Phase 3 gate S5).

The engine reaper is the one safety net for manifest-less chunks, and a dead one is invisible: a pass with nothing
to move writes no ``gc_audit`` row, and a cloud operator has no engine log. The engine reports the time of its last
completed pass under ``reaper`` in ``GET /v1/status``; this row flags one older than a few intervals.

The cases build the status body the engine sends (field names from ``docs/wire-contract-pending.md``) and check
what the row says for each, with the not-applicable branches first: a virgin box, an engine that predates the
field, and a reaper switched off must all stay green.
"""
from __future__ import annotations

from datetime import UTC, datetime, timedelta
from unittest.mock import patch

from nexus.health import _check_engine_reaper

_FETCH = "nexus.db.http_engine_status.fetch_engine_status"
NOW = datetime(2026, 10, 2, 12, 0, 0, tzinfo=UTC)
HOUR = 3600


def _iso(delta: timedelta) -> str:
    return (NOW - delta).strftime("%Y-%m-%dT%H:%M:%SZ")


def _status(*, last: timedelta | None, interval: int = HOUR, started: timedelta | None = timedelta(days=3),
            failed: int = 0, enabled: bool = True, budget: int | None = None) -> dict:
    body: dict = {"embedding_mode": "onnx-local", "reaper": {
        "enabled": enabled, "interval_seconds": interval,
        "last_completed_pass_at": None if last is None else _iso(last), "failed_passes_total": failed}}
    if budget is not None:
        body["reaper"]["wall_clock_budget_seconds"] = budget
    if started is not None:
        body["process_start_time"] = _iso(started)
    return body


def _row(status, **kw):
    rows = _check_engine_reaper(status, now=NOW, **kw)
    assert len(rows) == 1
    return rows[0]


# ── not applicable, and green ────────────────────────────────────────────────


def test_an_unreachable_engine_is_not_applicable_and_green() -> None:
    r = _row(None)
    assert r.ok is True and not r.warn and "not applicable" in r.detail


def test_a_probe_that_raises_is_not_applicable_and_green() -> None:
    with patch(_FETCH, side_effect=RuntimeError("boom")):
        (r,) = _check_engine_reaper()
    assert r.ok is True and not r.warn and "not applicable" in r.detail


def test_an_engine_that_predates_the_field_is_not_applicable_and_green() -> None:
    r = _row({"embedding_mode": "onnx-local", "ownerless_write_mode": "enforce"})
    assert r.ok is True and not r.warn
    assert "not applicable" in r.detail and "predates" in r.detail


def test_a_disabled_reaper_is_not_applicable_and_green() -> None:
    r = _row({"reaper": {"enabled": False}})
    assert r.ok is True and not r.warn
    assert "not applicable" in r.detail and "not running" in r.detail


def test_a_body_the_row_cannot_read_is_not_applicable_rather_than_a_crash() -> None:
    for reaper in ({"enabled": True}, {"enabled": True, "interval_seconds": "soon"},
                   {"enabled": True, "interval_seconds": 0}, "running", []):
        r = _row({"reaper": reaper})
        assert r.ok is True and not r.warn and "not applicable" in r.detail, reaper


def test_a_last_pass_time_that_is_not_a_timestamp_is_not_applicable_rather_than_a_crash() -> None:
    r = _row({"reaper": {"enabled": True, "interval_seconds": HOUR, "last_completed_pass_at": "yesterday"}})
    assert r.ok is True and not r.warn and "not applicable" in r.detail


# ── applicable ───────────────────────────────────────────────────────────────


def test_a_recent_pass_is_green_and_says_when() -> None:
    r = _row(_status(last=timedelta(minutes=20)))
    assert r.ok is True and not r.warn
    assert "20 minutes ago" in r.detail and "interval 60 minutes" in r.detail


def test_a_pass_just_inside_three_intervals_is_green_and_just_outside_is_a_warning() -> None:
    limit = 3 * HOUR + 120
    assert _row(_status(last=timedelta(seconds=limit))).ok is True
    stale = _row(_status(last=timedelta(seconds=limit + 1)))
    assert stale.ok is False and stale.warn is True and not stale.fatal


def test_a_stale_pass_warns_names_the_age_and_says_where_to_look() -> None:
    r = _row(_status(last=timedelta(hours=9)))
    assert r.ok is False and r.warn is True
    assert "9 hours ago" in r.detail and "3 intervals" in r.detail
    fixes = " ".join(r.fix_suggestions)
    assert "reaper_scheduled_run_failed" in fixes and "reaper_pass_failed" in fixes
    assert "NX_REAPER_ENABLED" in fixes
    assert "docs/operations/engine-reaper.md" in fixes


def test_the_threshold_includes_the_time_a_pass_may_take() -> None:
    # Completions are at most interval + the pass's wall-clock budget apart: a one-minute interval with the default
    # ten minute budget must not read a pass finished eleven minutes ago as a dead reaper.
    eleven_minutes = timedelta(minutes=11)
    assert _row(_status(last=eleven_minutes, interval=60)).warn is True, "no budget reported: interval alone"
    assert _row(_status(last=eleven_minutes, interval=60, budget=600)).ok is True
    assert _row(_status(last=timedelta(minutes=16), interval=60, budget=600)).warn is True


def test_the_threshold_follows_the_engines_own_interval() -> None:
    # A pass 90 minutes old is fine at an hourly interval and long dead at a one-minute one.
    assert _row(_status(last=timedelta(minutes=90))).ok is True
    assert _row(_status(last=timedelta(minutes=90), interval=60)).warn is True


def test_no_pass_yet_on_a_young_engine_is_green_and_says_how_young() -> None:
    r = _row(_status(last=None, started=timedelta(minutes=5)))
    assert r.ok is True and not r.warn
    assert "no completed pass yet" in r.detail and "5 minutes ago" in r.detail


def test_no_pass_yet_on_an_engine_older_than_three_intervals_warns() -> None:
    r = _row(_status(last=None, started=timedelta(hours=5)))
    assert r.ok is False and r.warn is True
    assert "no completed pass" in r.detail and "5 hours ago" in r.detail


def test_no_pass_yet_and_no_start_time_is_not_applicable() -> None:
    r = _row(_status(last=None, started=None))
    assert r.ok is True and not r.warn and "not applicable" in r.detail


def test_failed_passes_are_named_even_when_the_last_pass_is_recent() -> None:
    r = _row(_status(last=timedelta(minutes=10), failed=2))
    assert r.ok is True
    assert "2 passes failed since the engine started" in r.detail


def test_the_clock_the_row_reads_is_the_one_it_was_given() -> None:
    """A pass stamped in the future (clock skew between this box and a cloud engine) is a recent pass, not a crash."""
    r = _row(_status(last=timedelta(seconds=-30)))
    assert r.ok is True and not r.warn


# ── alive but doing nothing (nexus-wbfpw.55 round 2, critique S4a) ─────────────────────────────────────────


def _with_pass(visited: int, errored: int, refused: int, *, empty: int | None = None,
               last: timedelta = timedelta(minutes=10)) -> dict:
    """``empty=None`` leaves ``tenants_empty`` out, which is what an engine that predates nexus-wbfpw.73 sends."""
    body = _status(last=last)
    body["reaper"]["last_pass"] = {"tenants_visited": visited, "tenants_errored": errored,
                                   "tenants_refused": refused}
    if empty is not None:
        body["reaper"]["last_pass"]["tenants_empty"] = empty
    return body


def test_a_recent_pass_where_every_tenant_was_refused_warns_that_the_reaper_is_alive_but_doing_nothing() -> None:
    r = _row(_with_pass(2, 0, 2))
    assert r.ok is False and r.warn is True and not r.fatal
    assert "alive" in r.detail and "every tenant" in r.detail and "2 refused" in r.detail
    assert "10 minutes ago" in r.detail
    fixes = " ".join(r.fix_suggestions)
    assert "nx upgrade" in fixes, "a refused tenant has no verified backfill record: the remedy is named"
    assert "docs/operations/engine-reaper.md" in fixes


def test_a_recent_pass_where_every_tenant_errored_warns_and_points_at_the_engine_log() -> None:
    r = _row(_with_pass(1, 1, 0))
    assert r.ok is False and r.warn is True
    assert "1 errored" in r.detail
    assert "reaper_tenant_failed" in " ".join(r.fix_suggestions)
    assert "reaper_collection_failed" in " ".join(r.fix_suggestions)


def test_a_mix_of_refused_and_errored_tenants_with_no_success_warns_and_counts_both() -> None:
    r = _row(_with_pass(3, 1, 2))
    assert r.warn is True and "1 errored" in r.detail and "2 refused" in r.detail


def test_one_tenant_that_worked_keeps_the_row_green_even_beside_refused_ones() -> None:
    r = _row(_with_pass(3, 1, 1))
    assert r.ok is True and not r.warn
    assert "last completed pass" in r.detail


# ── tenants_empty (nexus-wbfpw.73): the default tenant is always visited and empty in cloud ──────────────────


def test_every_visited_tenant_empty_reads_healthy_so_a_fresh_install_stays_green() -> None:
    r = _row(_with_pass(1, 0, 0, empty=1))
    assert r.ok is True and not r.warn
    assert "last completed pass" in r.detail


def test_the_empty_default_tenant_does_not_hide_every_real_tenant_being_refused() -> None:
    # visited = default (empty) + two real tenants, both refused: errored + refused == nonempty, so it warns,
    # where comparing against visited (3) would stay green forever.
    r = _row(_with_pass(3, 0, 2, empty=1))
    assert r.ok is False and r.warn is True
    assert "2 refused" in r.detail and "visited 2 tenants" in r.detail


def test_the_empty_default_tenant_does_not_hide_every_real_tenant_erroring() -> None:
    r = _row(_with_pass(2, 1, 0, empty=1))
    assert r.warn is True and "1 errored" in r.detail and "visited 1 tenant " in r.detail


def test_one_real_tenant_that_worked_beside_an_empty_one_and_a_refused_one_stays_green() -> None:
    r = _row(_with_pass(3, 0, 1, empty=1))
    assert r.ok is True and not r.warn


def test_an_old_engine_with_no_tenants_empty_reads_exactly_as_before() -> None:
    assert _row(_with_pass(2, 0, 2)).warn is True          # every tenant refused: warns, as it always did
    assert _row(_with_pass(3, 1, 1)).warn is False          # one worked: green
    assert _row(_with_pass(1, 0, 1)).warn is True


def test_an_unreadable_tenants_empty_is_read_as_zero_not_as_a_crash_or_a_free_pass() -> None:
    for bad in ("1", True, -1, 5, None, 1.0):
        body = _with_pass(2, 0, 2)
        body["reaper"]["last_pass"]["tenants_empty"] = bad
        r = _row(body)
        assert r.warn is True and "2 refused" in r.detail, bad


def test_a_pass_that_visited_no_tenant_is_not_alive_but_refusing_because_there_was_nothing_to_refuse() -> None:
    r = _row(_with_pass(0, 0, 0))
    assert r.ok is True and not r.warn


def test_an_engine_that_sends_no_pass_summary_is_judged_on_the_time_alone() -> None:
    r = _row(_status(last=timedelta(minutes=10)))
    assert r.ok is True and not r.warn


def test_a_pass_summary_the_row_cannot_read_is_ignored_rather_than_a_crash() -> None:
    for summary in (None, "refused", [], {"tenants_visited": "many"}, {"tenants_visited": 2},
                    {"tenants_visited": True, "tenants_errored": 1, "tenants_refused": 1}):
        body = _status(last=timedelta(minutes=10))
        body["reaper"]["last_pass"] = summary
        r = _row(body)
        assert r.ok is True and not r.warn, summary


def test_a_stale_pass_is_reported_as_stale_before_any_summary_is_read() -> None:
    body = _with_pass(2, 0, 2, last=timedelta(hours=9))
    r = _row(body)
    assert r.warn is True and "9 hours ago" in r.detail and "may be dead" in r.detail


# ── wiring ───────────────────────────────────────────────────────────────────


def test_a_status_passed_in_is_used_and_nothing_is_fetched() -> None:
    with patch(_FETCH, side_effect=AssertionError("must not fetch")):
        (r,) = _check_engine_reaper(_status(last=timedelta(hours=9)), now=NOW)
        assert r.warn is True
        (r,) = _check_engine_reaper(None, now=NOW)  # the caller's fetch ran and failed
        assert r.ok is True and "not applicable" in r.detail


def test_the_default_sweep_makes_one_status_request_for_every_row_that_reads_it(monkeypatch) -> None:
    from click.testing import CliRunner

    from nexus.cli import main

    calls = {"n": 0}

    def _fake_fetch(**kwargs):
        calls["n"] += 1
        started = (datetime.now(UTC) - timedelta(days=2)).strftime("%Y-%m-%dT%H:%M:%SZ")
        return {"embedding_mode": "local", "process_start_time": started,
                "reaper": {"enabled": True, "interval_seconds": HOUR, "last_completed_pass_at": None,
                           "failed_passes_total": 0}}

    monkeypatch.setattr("nexus.db.http_engine_status.fetch_engine_status", _fake_fetch)
    result = CliRunner().invoke(main, ["doctor"])
    assert calls["n"] == 1, result.output
    assert "Engine reaper" in result.output
    assert "no completed pass" in result.output
