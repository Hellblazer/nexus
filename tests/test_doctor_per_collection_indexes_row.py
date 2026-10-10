# SPDX-License-Identifier: AGPL-3.0-or-later
"""nx doctor's "Per-collection indexes" row (nexus-43ulx.24, RDR-227 Day 2).

The engine reports the state of its per-collection partial HNSW indexes under ``per_collection_indexes`` in
``GET /v1/status`` (nexus-43ulx.23): the read half's global counts (``valid``, ``invalid``, ``unparsed``,
``last_read_at``) and, in ``this_engine``, what THIS engine's builder is doing (``builder_state``, ``building``,
``failing``, ``last_ddl_pass_at``). A downstream rotation check reads ``builder_state`` and ``last_ddl_pass_at`` by
name, so the cases build the body with exactly those names.

The row is read-only over the same status body as the Engine reaper row, so it works in local and managed mode alike.
Not-applicable cases come first: an engine that predates the object must stay green.
"""
from __future__ import annotations

import copy
import json
from datetime import UTC, datetime, timedelta
from pathlib import Path
from unittest.mock import patch

from click.testing import CliRunner

from nexus.cli import main
from nexus.health import _check_per_collection_indexes

_FETCH = "nexus.db.http_engine_status.fetch_engine_status"
NOW = datetime(2026, 10, 9, 12, 0, 0, tzinfo=UTC)


def _iso(delta: timedelta) -> str:
    return (NOW - delta).strftime("%Y-%m-%dT%H:%M:%SZ")


def _status(*, state: str = "ok", valid: int = 3, invalid: int = 0, unparsed: int = 0,
            building: int | None = 0, failing: int | None = 0,
            last_ddl: timedelta | None = timedelta(minutes=5), expired: bool = False,
            pass_in_progress: bool = False) -> dict:
    return {"embedding_mode": "onnx-local", "per_collection_indexes": {
        "valid": valid, "invalid": invalid, "unparsed": unparsed, "last_read_at": _iso(timedelta(minutes=1)),
        "expired": expired,
        "this_engine": {"builder_state": state, "building": building, "failing": failing,
                        "last_ddl_pass_at": None if last_ddl is None else _iso(last_ddl),
                        "pass_started_at": None, "pass_in_progress": pass_in_progress}}}


# The bodies the ENGINE emits, rendered by StatusHandlerTest through the real StatusHandler and committed as a golden
# fixture (the Java test asserts equality; this file feeds the same objects to the row). Hand-built bodies above can
# only prove the row reads the names the author remembered; these prove it reads the names the engine writes.
_GOLDEN = json.loads((Path(__file__).parent / "fixtures" / "pci_status_bodies.json").read_text())["cases"]


def _golden(name: str) -> dict:
    return {"embedding_mode": "onnx-local", "per_collection_indexes": copy.deepcopy(_GOLDEN[name])}


def _row(status, **kw):
    rows = _check_per_collection_indexes(status, now=NOW, **kw)
    assert len(rows) == 1
    assert rows[0].label == "Per-collection indexes"
    return rows[0]


# ── not applicable, and green ────────────────────────────────────────────────


def test_an_unreachable_engine_is_not_applicable_and_green() -> None:
    r = _row(None)
    assert r.ok is True and not r.warn and "not applicable" in r.detail


def test_a_probe_that_raises_is_not_applicable_and_green() -> None:
    with patch(_FETCH, side_effect=RuntimeError("boom")):
        (r,) = _check_per_collection_indexes()
    assert r.ok is True and not r.warn and "not applicable" in r.detail


def test_an_engine_that_predates_the_object_is_not_applicable_and_green() -> None:
    r = _row({"embedding_mode": "onnx-local", "reaper": {"enabled": True}})
    assert r.ok is True and not r.warn
    assert "not applicable" in r.detail and "predates" in r.detail


# ── a present object the row cannot read warns ───────────────────────────────
# The key being present means the engine claims to report the builder; a body this client cannot read, or a
# builder_state outside the closed vocabulary a rotation check reads, is what a doctor exists to surface.


def test_a_malformed_object_warns_rather_than_reading_green_or_crashing() -> None:
    for pci in ("on", [], 3, {}, {"valid": 1}, {"this_engine": "ok"}, {"this_engine": {}},
                {"this_engine": {"builder_state": 7}}):
        r = _row({"per_collection_indexes": pci})
        assert r.ok is False and r.warn is True, pci
        assert "not applicable" not in r.detail and "could not be read" in r.detail, pci


def test_an_unknown_builder_state_warns_and_names_the_value() -> None:
    r = _row(_status(state="melting"))
    assert r.ok is False and r.warn is True
    assert "'melting'" in r.detail and "not applicable" not in r.detail


# ── pass ─────────────────────────────────────────────────────────────────────


def test_a_healthy_builder_with_no_invalid_or_failing_index_passes_and_says_so() -> None:
    r = _row(_status(state="ok", valid=3))
    assert r.ok is True and not r.warn and not r.fatal
    assert "3 valid" in r.detail and "0 invalid" in r.detail and "5 minutes ago" in r.detail


def test_a_standby_engine_passes_because_a_peer_holds_the_builder_lock() -> None:
    r = _row(_status(state="standby", building=None, failing=None, last_ddl=None))
    assert r.ok is True and not r.warn
    assert "standby" in r.detail and "peer" in r.detail


def test_a_builder_that_has_not_run_a_ddl_pass_yet_passes_and_says_so() -> None:
    r = _row(_status(state="ok", last_ddl=None))
    assert r.ok is True and not r.warn and "no DDL pass yet" in r.detail


def test_failing_null_on_a_non_holder_is_read_as_zero() -> None:
    assert _row(_status(state="standby", building=None, failing=None)).ok is True


def test_unparsed_indexes_are_counted_in_the_detail_but_never_warn() -> None:
    r = _row(_status(unparsed=2))
    assert r.ok is True and not r.warn and "2 unparsed" in r.detail


def test_a_disabled_builder_passes_with_a_note_naming_the_switch() -> None:
    r = _row(_status(state="off", building=None, failing=None, last_ddl=None))
    assert r.ok is True and not r.warn
    assert "builds disabled by NX_SEARCH_PCI=0" in r.detail


# ── warn ─────────────────────────────────────────────────────────────────────


def test_an_invalid_index_warns_and_names_the_count() -> None:
    r = _row(_status(invalid=2))
    assert r.ok is False and r.warn is True and not r.fatal
    assert "2 invalid" in r.detail
    assert "event=pci_sweep" in " ".join(r.fix_suggestions)


def test_a_failing_build_warns_and_names_the_count() -> None:
    r = _row(_status(failing=1))
    assert r.ok is False and r.warn is True and not r.fatal
    assert "1 failing" in r.detail


def test_a_disabled_builder_with_invalid_indexes_still_warns_and_keeps_the_note() -> None:
    r = _row(_status(state="off", invalid=1, building=None, failing=None))
    assert r.warn is True and "1 invalid" in r.detail and "NX_SEARCH_PCI=0" in r.detail


def test_a_standby_engine_warns_on_the_global_invalid_count() -> None:
    r = _row(_status(state="standby", invalid=4, building=None, failing=None))
    assert r.warn is True and "4 invalid" in r.detail


def test_unreadable_counts_are_read_as_zero_not_as_a_crash_or_a_warning() -> None:
    body = _status()
    body["per_collection_indexes"].update(invalid="many", valid=None, unparsed=True)
    body["per_collection_indexes"]["this_engine"]["failing"] = "lots"
    r = _row(body)
    assert r.ok is True and not r.warn


# ── fail ─────────────────────────────────────────────────────────────────────


def test_an_auth_failed_builder_fails_and_says_to_restart_after_a_rotation() -> None:
    r = _row(_status(state="auth_failed", failing=None, last_ddl=None))
    assert r.ok is False and not r.warn and not r.fatal
    assert "auth_failed" in r.detail
    fixes = " ".join(r.fix_suggestions)
    assert "restart the engine" in fixes and "nexus_admin" in fixes
    assert "pci_builder_auth_failed" in fixes


def test_a_no_privilege_builder_fails_and_says_the_admin_role_cannot_create_indexes() -> None:
    r = _row(_status(state="no_privilege"))
    assert r.ok is False and not r.warn and not r.fatal
    assert "no_privilege" in r.detail
    assert "cannot create indexes on the leaves" in " ".join(r.fix_suggestions)


def test_a_builder_failure_outranks_a_clean_count() -> None:
    # invalid 0 and failing 0 must not turn a dead builder green.
    for state in ("auth_failed", "no_privilege"):
        assert _row(_status(state=state, invalid=0, failing=0)).ok is False, state


def test_the_clock_the_row_reads_is_the_one_it_was_given() -> None:
    """A DDL pass stamped in the future (clock skew against a cloud engine) is a recent pass, not a crash."""
    r = _row(_status(last_ddl=timedelta(seconds=-30)))
    assert r.ok is True and not r.warn


# ── wiring ───────────────────────────────────────────────────────────────────


def test_a_status_passed_in_is_used_and_nothing_is_fetched() -> None:
    with patch(_FETCH, side_effect=AssertionError("must not fetch")):
        (r,) = _check_per_collection_indexes(_status(invalid=1), now=NOW)
        assert r.warn is True
        (r,) = _check_per_collection_indexes(None, now=NOW)  # the caller's fetch ran and failed
        assert r.ok is True and "not applicable" in r.detail


def test_the_default_sweep_makes_one_status_request_and_prints_the_row(monkeypatch) -> None:
    calls = {"n": 0}

    def _fake_fetch(**kwargs):
        calls["n"] += 1
        body = _status(state="no_privilege")
        body["reaper"] = {"enabled": False}
        return body

    monkeypatch.setattr("nexus.db.http_engine_status.fetch_engine_status", _fake_fetch)
    result = CliRunner().invoke(main, ["doctor"])
    assert calls["n"] == 1, result.output
    assert "Per-collection indexes" in result.output
    assert "no_privilege" in result.output


# ── the engine's own bodies (golden fixture shared with StatusHandlerTest) ──────────────────────────────────


def test_the_golden_holder_body_mid_build_reads_as_a_build_in_progress_not_as_an_invalid_index() -> None:
    r = _row(_golden("holder"))
    assert r.ok is True and not r.warn
    assert "build in progress" in r.detail and "1 invalid" in r.detail


def test_the_golden_first_pass_body_reports_the_first_pass_instead_of_warning() -> None:
    r = _row(_golden("first_pass_in_flight"))
    assert r.ok is True and not r.warn
    assert "first pass in progress" in r.detail and "no DDL pass yet" not in r.detail


def test_the_golden_standby_body_passes() -> None:
    r = _row(_golden("standby"))
    assert r.ok is True and not r.warn and "peer" in r.detail


def test_the_golden_auth_failed_and_no_privilege_bodies_fail() -> None:
    for name, word in (("auth_failed", "auth_failed"), ("no_privilege", "no_privilege")):
        r = _row(_golden(name))
        assert r.ok is False and not r.warn and word in r.detail, name


def test_the_golden_expired_body_warns_that_the_router_set_is_frozen() -> None:
    r = _row(_golden("expired"))
    assert r.ok is False and r.warn is True and "expired" in r.detail
    assert "ef_search" in r.detail


def test_the_golden_off_body_passes_with_the_switch_note() -> None:
    r = _row(_golden("off"))
    assert r.ok is True and not r.warn and "NX_SEARCH_PCI=0" in r.detail


def test_every_golden_case_has_an_expectation_here() -> None:
    """A case added to the fixture must be read by this file; otherwise the shared fixture guards nothing for it."""
    covered = {"holder", "first_pass_in_flight", "standby", "auth_failed", "no_privilege", "expired", "off"}
    assert set(_GOLDEN) == covered


# ── expired, and a build in flight ───────────────────────────────────────────


def test_an_expired_router_set_warns_even_when_the_builder_is_healthy() -> None:
    r = _row(_status(expired=True, invalid=0, failing=0))
    assert r.ok is False and r.warn is True and "expired" in r.detail
    assert "event=pci_sweep_set_expired" in " ".join(r.fix_suggestions)


def test_an_expired_set_still_warns_while_a_build_is_in_flight() -> None:
    r = _row(_status(expired=True, building=1, invalid=1))
    assert r.warn is True and "expired" in r.detail and "invalid" not in r.detail.split("(")[0]


def test_an_invalid_index_during_a_build_is_not_a_warning() -> None:
    r = _row(_status(building=1, invalid=1))
    assert r.ok is True and not r.warn and "build in progress" in r.detail


def test_an_invalid_index_during_a_pass_is_not_a_warning_and_keeps_the_last_pass_time() -> None:
    r = _row(_status(pass_in_progress=True, building=0, invalid=2, last_ddl=timedelta(minutes=5)))
    assert r.ok is True and not r.warn
    assert "pass in progress" in r.detail and "5 minutes ago" in r.detail


def test_failing_builds_still_warn_while_another_build_is_in_flight() -> None:
    r = _row(_status(building=1, invalid=1, failing=2))
    assert r.warn is True and "2 failing" in r.detail


def test_an_invalid_index_with_no_build_in_flight_still_warns() -> None:
    r = _row(_status(building=0, pass_in_progress=False, invalid=1))
    assert r.warn is True and "1 invalid" in r.detail
