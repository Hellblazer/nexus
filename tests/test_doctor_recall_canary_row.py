# SPDX-License-Identifier: AGPL-3.0-or-later
"""nx doctor's "Recall canary" row (nexus-4lnn1.1.10, RDR-230 Technical Design 7).

The engine reports its recall canary under ``per_collection_indexes.this_engine.maintenance.canary`` in
``GET /v1/status`` as counts only (``green``, ``red``, ``unmeasured``, ``last_run_at``): the route is unauthenticated,
so no ``pci_`` index is named (Sam's ruling of 2026-11-06, ask ``pci-names-on-status``, option b). The per-index list is
``canary_indexes`` on the operator-only ``GET /v1/admin/chunks-leaves``; the row names that route and never calls it.

The row warns on ``red`` > 0, reports the other counts informationally, is not applicable for an engine that predates the
keys, and says "could not read" for a value it cannot read: never a clean row it did not check. The bodies are the golden
fixture the engine's own test renders (``tests/fixtures/pci_status_bodies.json``), so the row reads the names the engine
writes; the cases it does not carry are the golden bodies with one key changed.
"""
from __future__ import annotations

import copy
import json
from pathlib import Path
from unittest.mock import patch

import pytest

import nexus.db.http_engine_status as http_status
import nexus.health as health
from nexus.health import _check_recall_canary

_LABEL = "Recall canary"
_FETCH = "nexus.db.http_engine_status.fetch_engine_status"
_ADMIN_ROUTE = "GET /v1/admin/chunks-leaves"

_GOLDEN = json.loads((Path(__file__).parent / "fixtures" / "pci_status_bodies.json").read_text())["cases"]


def _golden(name: str) -> dict:
    return {"embedding_mode": "onnx-local", "per_collection_indexes": copy.deepcopy(_GOLDEN[name])}


def _canary(status: dict) -> dict:
    return status["per_collection_indexes"]["this_engine"]["maintenance"]["canary"]


def _with_canary(**counts) -> dict:
    body = _golden("holder_with_canary")
    _canary(body).update(counts)
    return body


def _row(status):
    rows = _check_recall_canary(status)
    assert len(rows) == 1
    assert rows[0].label == _LABEL
    return rows[0]


def _is_not_applicable(r) -> bool:
    return r.ok is True and not r.warn and not r.fatal and r.detail.startswith("not applicable")


# ── not applicable ───────────────────────────────────────────────────────────


def test_an_unreachable_engine_is_not_applicable_and_green() -> None:
    assert _is_not_applicable(_row(None))


def test_a_probe_that_raises_is_not_applicable_and_green() -> None:
    with patch(_FETCH, side_effect=RuntimeError("boom")):
        (r,) = _check_recall_canary()
    assert _is_not_applicable(r)


def test_an_engine_without_the_per_collection_indexes_object_is_not_applicable() -> None:
    assert _is_not_applicable(_row({"embedding_mode": "onnx-local"}))


def test_an_engine_that_predates_the_maintenance_object_is_not_applicable() -> None:
    for name in ("holder_with_canary", "standby_with_canary_history", "never_held_lock", "holder", "off"):
        body = _golden(name)
        body["per_collection_indexes"]["this_engine"].pop("maintenance", None)
        assert _is_not_applicable(_row(body)), name


def test_an_engine_with_maintenance_but_no_canary_key_is_not_applicable() -> None:
    body = _golden("holder_with_canary")
    del body["per_collection_indexes"]["this_engine"]["maintenance"]["canary"]
    assert _is_not_applicable(_row(body))


def test_a_per_collection_indexes_object_without_this_engine_is_not_applicable() -> None:
    body = _golden("holder_with_canary")
    del body["per_collection_indexes"]["this_engine"]
    assert _is_not_applicable(_row(body))


# ── red warns; green and unmeasured are information ──────────────────────────


def test_the_golden_red_canary_warns_with_the_count_and_the_other_counts() -> None:
    r = _row(_golden("holder_with_canary"))
    assert r.ok is False and r.warn is True and not r.fatal
    assert "1 per-collection index" in r.detail and "red" in r.detail
    assert "1 green" in r.detail and "1 unmeasured" in r.detail
    assert "2026-10-20T08:01:10Z" in r.detail


def test_the_warning_points_at_the_admin_route_and_the_key_that_names_the_index() -> None:
    r = _row(_golden("holder_with_canary"))
    text = r.detail + " ".join(r.fix_suggestions)
    assert _ADMIN_ROUTE in text and "canary_indexes" in text
    assert "operator" in text
    assert "event=pci_canary" in text, "the engine log names the collection"


def test_a_standby_with_red_history_warns_too() -> None:
    # This engine's own history stays after it becomes a standby (RDR-230 Technical Design 7).
    r = _row(_golden("standby_with_canary_history"))
    assert r.warn is True and "red" in r.detail
    assert "this engine is a standby" in r.detail


def test_a_holder_with_red_does_not_say_standby() -> None:
    r = _row(_golden("holder_with_canary"))
    assert r.warn is True and "this engine's recall canary" in r.detail
    assert "standby" not in r.detail


def test_the_count_is_plural_when_more_than_one_index_is_red() -> None:
    one = _row(_with_canary(red=1)).detail
    three = _row(_with_canary(red=3)).detail
    assert "1 per-collection index " in one and "indexes" not in one.split(";")[0]
    assert "3 per-collection indexes" in three


def test_a_body_with_no_red_is_green_and_reports_the_counts() -> None:
    r = _row(_with_canary(green=4, red=0, unmeasured=1))
    assert r.ok is True and not r.warn and not r.fatal
    assert "4 green, 0 red, 1 unmeasured" in r.detail
    assert "2026-10-20T08:01:10Z" in r.detail


def test_unmeasured_alone_is_information_not_a_warning() -> None:
    r = _row(_with_canary(green=0, red=0, unmeasured=7))
    assert r.ok is True and not r.warn
    assert "7 unmeasured" in r.detail


def test_an_engine_that_never_held_the_lock_says_no_run_yet_and_stays_green() -> None:
    r = _row(_golden("never_held_lock"))
    assert r.ok is True and not r.warn
    assert "0 green, 0 red, 0 unmeasured" in r.detail and "no run yet" in r.detail


def test_the_green_row_does_not_name_the_admin_route() -> None:
    assert _ADMIN_ROUTE not in _row(_with_canary(red=0)).detail


# ── a value that cannot be read says so, and never reads clean ───────────────


_BAD_COUNTS = [None, "2", -1, True, 1.5, [1], {}]


@pytest.mark.parametrize("bad", _BAD_COUNTS, ids=repr)
def test_a_red_count_that_is_not_a_non_negative_int_could_not_be_read(bad) -> None:
    r = _row(_with_canary(red=bad))
    assert r.ok is False and r.warn is True and not r.fatal
    assert "could not read" in r.detail


@pytest.mark.parametrize("key", ["green", "unmeasured"])
@pytest.mark.parametrize("bad", _BAD_COUNTS, ids=repr)
def test_an_unreadable_other_count_with_no_red_could_not_be_read_not_green(key, bad) -> None:
    r = _row(_with_canary(red=0, **{key: bad}))
    assert r.ok is False and r.warn is True and not r.fatal
    assert "could not read" in r.detail


def test_a_red_count_still_warns_when_another_count_is_unreadable() -> None:
    r = _row(_with_canary(red=2, green="x"))
    assert r.warn is True and "2 per-collection indexes" in r.detail
    assert "? green" in r.detail


@pytest.mark.parametrize("bad", ["x", [], 3, True], ids=repr)
def test_a_canary_object_that_is_not_an_object_could_not_be_read(bad) -> None:
    body = _golden("holder_with_canary")
    body["per_collection_indexes"]["this_engine"]["maintenance"]["canary"] = bad
    r = _row(body)
    assert r.ok is False and r.warn is True and "could not read" in r.detail


@pytest.mark.parametrize("bad", ["x", [], 3, None], ids=repr)
def test_a_maintenance_object_that_is_not_an_object_could_not_be_read(bad) -> None:
    body = _golden("holder_with_canary")
    body["per_collection_indexes"]["this_engine"]["maintenance"] = bad
    r = _row(body)
    if bad is None:
        assert _is_not_applicable(r), "a null maintenance is the key absent, as for the other rows"
    else:
        assert r.ok is False and r.warn is True and "could not read" in r.detail


def test_a_per_collection_indexes_object_that_is_not_an_object_could_not_be_read() -> None:
    r = _row({"per_collection_indexes": ["x"]})
    assert r.ok is False and r.warn is True and "could not read" in r.detail


def test_a_run_time_that_is_not_a_string_is_not_shown_and_does_not_fail_the_row() -> None:
    r = _row(_with_canary(red=0, last_run_at=12345))
    assert r.ok is True and "12345" not in r.detail


# ── fixture: every case is read, only the red ones warn ──────────────────────


def test_the_golden_fixture_is_not_vacuous() -> None:
    # Every case is read: those with a canary object say so, the rest are not applicable. A fixture reduced to bodies
    # the row ignores, or a row that stopped reading them, fails here rather than passing quietly.
    seen = {name: _row(_golden(name)) for name in _GOLDEN}
    with_canary = {n for n, r in seen.items() if not _is_not_applicable(r)}
    assert with_canary == {"holder_with_canary", "standby_with_canary_history", "never_held_lock"}
    assert {n for n, r in seen.items() if r.warn} == {"holder_with_canary", "standby_with_canary_history"}


# ── wiring ───────────────────────────────────────────────────────────────────


def test_a_status_passed_in_is_used_and_nothing_is_fetched() -> None:
    with patch(_FETCH, side_effect=AssertionError("must not fetch")):
        assert _row(_golden("holder_with_canary")).warn is True
        assert _is_not_applicable(_row(None))  # the caller's fetch ran and failed


def test_left_unset_the_row_fetches_the_status_once() -> None:
    calls = {"n": 0}

    def fake(**kwargs):
        calls["n"] += 1
        return _golden("holder_with_canary")

    with patch(_FETCH, fake):
        (r,) = _check_recall_canary()
    assert calls["n"] == 1 and r.warn is True


def test_the_row_never_calls_the_operator_admin_route() -> None:
    # nx doctor reads /v1/status only; the admin route needs the operator token and is named, never called.
    with patch("nexus.db.t2.http_token_store.HttpTokenStore", side_effect=AssertionError("must not call admin")):
        assert _row(_golden("holder_with_canary")).warn is True


def test_the_sweep_runs_the_row_on_the_status_it_was_handed(monkeypatch) -> None:
    """``run_health_checks`` is called for real with a canned status and every other check stepped over, so this proves
    the row is in the sweep under its ``_check_*`` name, handed the caller's status, and its warning reaches the result."""
    ran: list[str] = []

    def stepper(_progress):
        def step(name, fn):
            ran.append(name)
            return list(fn()) if name == "_check_recall_canary" else []
        return step

    monkeypatch.setattr(health, "_row_stepper", stepper)
    monkeypatch.setattr(http_status, "fetch_engine_status", lambda *a, **k: pytest.fail("the sweep fetched the status"))

    results, _local = health.run_health_checks(engine_status=_golden("holder_with_canary"))

    assert "_check_recall_canary" in ran
    assert [r.label for r in results if r.warn] == [_LABEL]
