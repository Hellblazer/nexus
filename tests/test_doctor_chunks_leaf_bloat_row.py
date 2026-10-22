# SPDX-License-Identifier: AGPL-3.0-or-later
"""nx doctor's "Chunks leaf bloat" row (nexus-dxsjc, HNSW maintenance policy nexus-4lnn1 R4).

The engine reports the dead-tuple bloat of the leaves of ``nexus.chunks`` under ``chunks_leaf_bloat`` in
``GET /v1/status`` as AGGREGATES ONLY (Sam's ruling, 2026-10-19: the route is unauthenticated, so no leaf is named):
counts over every leaf (``leaves_total``, ``policy_unset``, ``autovacuum_disabled``, ``overdue``, ``pinned``,
``starved``), ``max_overdue_for_seconds`` and the sweep-health fields. A leaf is OVERDUE when its dead rows pass
``overdue_base_rows`` + ``overdue_scale_factor`` of its rows, not at a flat fraction: a small leaf at 30% dead is far
under autovacuum's 1000-row base and is left alone.

The row reads only that object, so it works against a local and a managed engine alike. It warns on ``pinned`` (a vacuum
finished and the dead rows remain) and ``starved`` (overdue for ``overdue_warn_after_seconds`` with no vacuum), and says
the leaf names are in the engine log (``event=chunks_leaf_bloat_leaf``). Not-applicable cases come first: an engine that
predates the object, and a box with no leaves to read, must stay green; reads that have failed since boot must not.
"""
from __future__ import annotations

import copy
import json
from pathlib import Path
from unittest.mock import patch

import pytest
from click.testing import CliRunner

from nexus.cli import main
from nexus.health import _check_chunks_leaf_bloat

_FETCH = "nexus.db.http_engine_status.fetch_engine_status"
_LABEL = "Chunks leaf bloat"

# The bodies the ENGINE emits, rendered by StatusHandlerChunksLeafBloatTest through the real StatusHandler and
# committed as a golden fixture (the Java test asserts equality; this file feeds the same objects to the row).
_GOLDEN = json.loads((Path(__file__).parent / "fixtures" / "leaf_bloat_status_bodies.json").read_text())["cases"]


def _golden(name: str) -> dict:
    return {"embedding_mode": "onnx-local", "chunks_leaf_bloat": copy.deepcopy(_GOLDEN[name])}


def _status(*, total: int = 1, unset: int = 0, disabled: int = 0, pinned: int = 0, starved: int = 0,
            overdue: int | None = None, longest: int = 0, expired: bool = False, failures: int = 0,
            ever_read: bool = True) -> dict:
    return {"embedding_mode": "onnx-local", "chunks_leaf_bloat": {
        "last_read_at": "2026-10-10T08:00:00Z", "expired": expired, "sweep_seconds": 600,
        "ever_read": ever_read, "consecutive_failures": failures,
        "overdue_base_rows": 1000, "overdue_scale_factor": 0.1, "overdue_warn_after_seconds": 1800,
        "leaves_total": total, "policy_unset": unset, "autovacuum_disabled": disabled,
        "overdue": pinned + starved if overdue is None else overdue,
        "pinned": pinned, "starved": starved, "max_overdue_for_seconds": longest}}


def _row(status):
    rows = _check_chunks_leaf_bloat(status)
    assert len(rows) == 1
    assert rows[0].label == _LABEL
    return rows[0]


# ── not applicable, and green ────────────────────────────────────────────────


def test_an_unreachable_engine_is_not_applicable_and_green() -> None:
    r = _row(None)
    assert r.ok is True and not r.warn and "not applicable" in r.detail


def test_a_probe_that_raises_is_not_applicable_and_green() -> None:
    with patch(_FETCH, side_effect=RuntimeError("boom")):
        (r,) = _check_chunks_leaf_bloat()
    assert r.ok is True and not r.warn and "not applicable" in r.detail


def test_an_engine_without_the_object_is_not_applicable_and_green() -> None:
    r = _row({"embedding_mode": "onnx-local", "reaper": {"enabled": True}})
    assert r.ok is True and not r.warn
    assert "not applicable" in r.detail and "does not include" in r.detail


def test_a_view_the_engine_has_not_read_yet_is_not_applicable_and_green() -> None:
    r = _row(_golden("not_read_yet"))
    assert r.ok is True and not r.warn
    assert "not applicable" in r.detail and "no leaves" in r.detail


def test_a_box_with_no_leaves_and_no_failed_read_is_not_applicable_even_when_a_read_time_is_set() -> None:
    r = _row(_status(total=0))
    assert r.ok is True and not r.warn and "not applicable" in r.detail


# ── reads that have failed since boot warn ───────────────────────────────────


def test_reads_failing_since_boot_warn_instead_of_reading_not_applicable() -> None:
    r = _row(_golden("reads_failing_since_boot"))
    assert r.ok is False and r.warn is True and not r.fatal
    assert "failed 3 times" in r.detail and "none has succeeded" in r.detail
    assert "not applicable" not in r.detail
    assert any("chunks_leaf_bloat_read_failed" in f for f in r.fix_suggestions)


def test_a_single_failed_read_before_the_first_success_warns_in_the_singular() -> None:
    r = _row(_status(total=0, ever_read=False, failures=1))
    assert r.warn is True and "failed 1 time since" in r.detail


def test_no_leaves_and_no_failures_before_the_first_read_stays_green() -> None:
    r = _row(_status(total=0, ever_read=False, failures=0))
    assert r.ok is True and "not applicable" in r.detail


def test_failures_after_a_good_read_do_not_warn_but_say_the_figures_are_old() -> None:
    r = _row(_status(total=1, failures=2))
    assert r.ok is True and not r.warn
    assert "last 2 reads failed" in r.detail


# ── a present object the row cannot read warns ──────────────────────────────


def test_a_malformed_object_warns_rather_than_reading_green_or_crashing() -> None:
    for body in ("on", [], 3, {}, {"leaves": []}, {"leaves_total": "2"}, {"leaves_total": True}, {"leaves_total": -1}):
        r = _row({"chunks_leaf_bloat": body})
        assert r.ok is False and r.warn is True, body
        assert "could not be read" in r.detail and "not applicable" not in r.detail, body


# ── pass ─────────────────────────────────────────────────────────────────────


def test_the_golden_healthy_body_passes_and_says_nothing_is_overdue() -> None:
    r = _row(_golden("healthy"))
    assert r.ok is True and not r.warn and not r.fatal
    assert "2 leaves, none overdue" in r.detail


def test_a_small_leaf_at_thirty_percent_dead_is_green() -> None:
    # 70 live, 30 dead: 30% dead, but 30 rows is far under autovacuum's 1000-row base, so autovacuum correctly leaves it.
    r = _row(_golden("small_leaf_thirty_percent_dead"))
    assert r.ok is True and not r.warn
    assert "1 leaf, none overdue" in r.detail


def test_an_overdue_leaf_that_is_still_waiting_passes_with_a_note_and_no_hard_coded_minutes() -> None:
    r = _row(_golden("overdue_waiting"))
    assert r.ok is True and not r.warn
    assert "1 leaf overdue and waiting for a vacuum" in r.detail
    assert "after 30m without one" in r.detail, "the wait comes from the engine's overdue_warn_after_seconds"
    body = _golden("overdue_waiting")
    body["chunks_leaf_bloat"]["overdue_warn_after_seconds"] = 7200
    assert "after 2h without one" in _row(body).detail


# ── warn ─────────────────────────────────────────────────────────────────────


def test_the_golden_pinned_body_warns_from_the_aggregates_and_points_at_the_engine_log() -> None:
    r = _row(_golden("overdue_pinned"))
    assert r.ok is False and r.warn is True and not r.fatal
    assert "dead rows have not come down on 1 leaf" in r.detail
    assert "1 pinned (a vacuum finished" in r.detail and "starved" not in r.detail
    assert "not keeping up" not in r.detail, "the row states readings, not a diagnosis it cannot make"
    assert "the leaf names are in the engine log (event=chunks_leaf_bloat_leaf)" in r.detail
    assert "chunks_minilm" not in r.detail, "the engine names no leaf, so the row cannot"
    assert any("event=chunks_leaf_bloat_leaf" in f for f in r.fix_suggestions)
    assert any("holds the dead rows back" in f and "pg_replication_slots" in f for f in r.fix_suggestions)
    assert not any("autovacuum_max_workers" in f for f in r.fix_suggestions), "advice for the other case stays out"


def test_the_golden_starved_body_warns_with_advice_for_a_leaf_autovacuum_never_reached() -> None:
    r = _row(_golden("overdue_starved"))
    assert r.ok is False and r.warn is True
    assert "1 starved (overdue for 30m or more with no vacuum finished)" in r.detail
    assert "the longest has been overdue 1h" in r.detail
    assert any("autovacuum_max_workers" in f and "30m or more" in f for f in r.fix_suggestions)
    assert not any("pg_replication_slots" in f for f in r.fix_suggestions), "advice for the other case stays out"


def test_the_golden_policy_unset_body_warns_counting_the_leaf() -> None:
    r = _row(_golden("policy_unset"))
    assert r.ok is False and r.warn is True
    assert "1 leaf without all four autovacuum settings (vectors-034)" in r.detail
    assert "event=chunks_leaf_bloat_leaf" in r.detail
    assert any("restart" in f.lower() for f in r.fix_suggestions)


def test_the_golden_autovacuum_disabled_body_warns_counting_the_leaf() -> None:
    r = _row(_golden("autovacuum_disabled"))
    assert r.ok is False and r.warn is True
    assert "1 leaf with autovacuum_enabled off" in r.detail
    assert any("RESET (autovacuum_enabled)" in f for f in r.fix_suggestions)


def test_the_golden_expired_body_warns_that_the_view_is_stale() -> None:
    r = _row(_golden("expired"))
    assert r.ok is False and r.warn is True
    assert "expired" in r.detail


def test_the_golden_fixture_is_not_vacuous() -> None:
    # Every case is read, the green ones pass and the red ones warn; a fixture reduced to green cases, or a row that
    # stopped reading it, fails here rather than passing quietly.
    assert set(_GOLDEN) == {"healthy", "small_leaf_thirty_percent_dead", "overdue_waiting", "overdue_pinned",
                            "overdue_starved", "policy_unset", "autovacuum_disabled", "expired", "not_read_yet",
                            "reads_failing_since_boot"}
    warned = {name for name in _GOLDEN if _row(_golden(name)).warn}
    assert warned == {"overdue_pinned", "overdue_starved", "policy_unset", "autovacuum_disabled", "expired",
                      "reads_failing_since_boot"}


def test_the_golden_fixture_carries_no_per_leaf_datum() -> None:
    # Sam's ruling, 2026-10-19: the route is unauthenticated. No fixture body, which is what the engine emits, holds a
    # list, a nested object, a leaf name or a per-leaf key.
    per_leaf = {"leaves", "name", "pci_indexes", "pci_indexes_total", "live_rows", "dead_rows", "dead_fraction",
                "last_autovacuum", "vacuum_count", "overdue_state"}
    for case, body in _GOLDEN.items():
        assert not per_leaf & set(body), case
        assert not any(isinstance(v, (list, dict)) for v in body.values()), case
        assert "chunks_" not in json.dumps(body) and "pci_" not in json.dumps(body), case


def test_every_problem_is_counted_in_one_row() -> None:
    r = _row(_status(total=9, pinned=2, starved=1, unset=3, disabled=4, longest=7200))
    assert r.warn is True
    assert "on 3 leaves" in r.detail and "2 pinned" in r.detail and "1 starved" in r.detail
    assert "the longest has been overdue 2h" in r.detail
    assert "3 leaves without all four autovacuum settings" in r.detail
    assert "4 leaves with autovacuum_enabled off" in r.detail
    assert "9 leaves, 3 overdue" in r.detail


def test_the_engines_rule_is_what_the_text_quotes() -> None:
    body = _status(pinned=1)
    body["chunks_leaf_bloat"]["overdue_base_rows"] = 2000
    body["chunks_leaf_bloat"]["overdue_scale_factor"] = 0.15
    r = _row(body)
    assert r.warn is True and "2,000 + 15% of the leaf's rows" in r.detail


def test_overdue_below_the_persistent_count_is_raised_to_it() -> None:
    r = _row(_status(total=4, starved=2, overdue=0))
    assert r.warn is True and "4 leaves, 2 overdue" in r.detail


def test_a_count_that_is_not_a_non_negative_int_reads_as_zero() -> None:
    for bad in (None, "2", -1, True, 1.5):
        body = _status(total=2)
        for key in ("pinned", "starved", "policy_unset", "autovacuum_disabled", "overdue", "max_overdue_for_seconds"):
            body["chunks_leaf_bloat"][key] = bad
        assert _row(body).warn is False, bad


# ── wiring ───────────────────────────────────────────────────────────────────


def test_a_status_passed_in_is_used_and_nothing_is_fetched() -> None:
    with patch(_FETCH, side_effect=AssertionError("must not fetch")):
        (r,) = _check_chunks_leaf_bloat(_golden("policy_unset"))
        assert r.warn is True
        (r,) = _check_chunks_leaf_bloat(None)  # the caller's fetch ran and failed
        assert r.ok is True and "not applicable" in r.detail


@pytest.mark.t2_tenant
def test_the_default_sweep_makes_one_status_request_and_prints_the_row(monkeypatch) -> None:
    calls = {"n": 0}

    def _fake_fetch(**kwargs):
        calls["n"] += 1
        body = _golden("policy_unset")
        body["reaper"] = {"enabled": False}
        return body

    monkeypatch.setattr("nexus.db.http_engine_status.fetch_engine_status", _fake_fetch)
    result = CliRunner().invoke(main, ["doctor"])
    assert calls["n"] == 1, result.output
    assert _LABEL in result.output
    assert "chunks_leaf_bloat_leaf" in result.output


def test_counts_are_pluralised_in_plain_english() -> None:
    one = _row(_status(pinned=1)).detail
    two = _row(_status(pinned=2)).detail
    assert "on 1 leaf " in one and "1 leaves" not in one
    assert "on 2 leaves " in two and "leafves" not in two


# ── nx doctor --verbose: name the leaves from the operator-only admin route (nexus-i827c) ──────────────────────


_ADMIN_ROUTE = "GET /v1/admin/chunks-leaves"
_SENTINEL_TOKEN = "tok-sentinel-never-printed-0123456789"


class _FakeStore:
    """An ``HttpTokenStore`` stand-in: ``outcome`` is what ``chunks_leaves()`` returns or raises."""

    outcome: object = None
    constructed = 0

    def __init__(self, *a, **kw) -> None:
        type(self).constructed += 1

    def __enter__(self):
        return self

    def __exit__(self, *exc) -> None:
        return None

    def chunks_leaves(self):
        if isinstance(self.outcome, BaseException):
            raise self.outcome
        return self.outcome


@pytest.fixture
def store(monkeypatch):
    """Patch the store the verbose read builds; the test sets ``store.outcome``."""
    _FakeStore.outcome, _FakeStore.constructed = None, 0
    monkeypatch.setattr("nexus.db.t2.http_token_store.HttpTokenStore", _FakeStore)
    monkeypatch.setenv("NX_SERVICE_TOKEN", _SENTINEL_TOKEN)
    return _FakeStore


def _leaf(name: str, problems: list[str], **kw) -> dict:
    return {"leaf": name, "problems": problems, "rows": 130000, "dead_rows": 30000,
            "last_autovacuum": "2026-10-22T09:00:00Z", "overdue_state": "pinned", "pci_index_count": 1,
            "pci_index_bytes": 4096000, **kw}


def _verbose_row(status):
    (r,) = _check_chunks_leaf_bloat(status, verbose=True)
    return r


def test_verbose_lists_each_leaf_with_rows_last_autovacuum_state_and_pci_bytes(store) -> None:
    store.outcome = {"leaves_total": 3, "needing_attention": 2, "leaves": [
        _leaf("chunks_m0a90d9bc_t_aaaa", ["pinned"]),
        _leaf("chunks_mad318272_t_bbbb", ["policy_unset", "autovacuum_disabled"], overdue_state="none",
              last_autovacuum=None, pci_index_bytes=0, pci_index_count=0, rows=5000, dead_rows=10)]}
    r = _verbose_row(_golden("overdue_pinned"))
    assert r.ok is False and r.warn is True, "the row still warns on the counts"
    assert "chunks_m0a90d9bc_t_aaaa: pinned; rows 130000, dead 30000, last autovacuum 2026-10-22T09:00:00Z, " \
        "overdue pinned, 3.9 MiB in 1 pci_ index(es)" in r.detail
    assert "chunks_mad318272_t_bbbb: policy_unset,autovacuum_disabled; rows 5000, dead 10, last autovacuum never, " \
        "overdue none, 0 B in 0 pci_ index(es)" in r.detail
    assert store.constructed == 1


def test_the_default_sweep_reads_no_admin_route(store) -> None:
    store.outcome = AssertionError("the default sweep must not read the admin route")
    r = _row(_golden("overdue_pinned"))
    assert r.warn is True and "chunks_m" not in r.detail
    assert store.constructed == 0
    assert "nx doctor --verbose" in r.detail, "the default row says where the names are"


def test_verbose_reads_nothing_when_the_row_does_not_warn(store) -> None:
    store.outcome = AssertionError("a healthy row needs no leaf list")
    r = _verbose_row(_status(total=2))
    assert r.ok is True and not r.warn
    assert store.constructed == 0


def test_an_engine_that_predates_the_route_is_one_line_and_never_an_error(store) -> None:
    store.outcome = None  # chunks_leaves() returns None for a 404
    r = _verbose_row(_golden("overdue_pinned"))
    assert r.ok is False and r.warn is True and not r.fatal
    assert f"this engine predates the leaf listing ({_ADMIN_ROUTE} answered 404)" in r.detail
    assert "event=chunks_leaf_bloat_leaf" in r.detail
    assert r.detail.count("predates the leaf listing") == 1


@pytest.mark.parametrize("status_code", [401, 403])
def test_a_refused_credential_names_the_operator_token_and_leaks_nothing(store, status_code) -> None:
    import httpx

    from nexus.db.t2.http_token_store import TokenAdminAuthError

    request = httpx.Request("GET", "http://127.0.0.1:9/v1/admin/chunks-leaves")
    response = httpx.Response(status_code, request=request, json={"error": "operator privilege required"})
    store.outcome = TokenAdminAuthError([f"{status_code} from engine", f"  sent: {_SENTINEL_TOKEN}"],
                                        request=request, response=response)
    r = _verbose_row(_golden("overdue_pinned"))
    assert r.ok is False and r.warn is True and not r.fatal
    assert f"leaf listing refused ({_ADMIN_ROUTE} answered {status_code})" in r.detail
    assert "operator (root) token" in r.detail
    assert _SENTINEL_TOKEN not in r.detail and not any(_SENTINEL_TOKEN in f for f in r.fix_suggestions)


@pytest.mark.parametrize("error", [ConnectionError("refused"), OSError("boom"), ValueError("not an object"),
                                   RuntimeError("no service configured")])
def test_any_other_failure_is_one_line_with_the_error_type_only(store, error) -> None:
    store.outcome = error
    r = _verbose_row(_golden("overdue_pinned"))
    assert r.ok is False and r.warn is True
    assert f"leaf listing unavailable: {type(error).__name__}" in r.detail
    assert str(error) not in r.detail, "the message may carry an endpoint or credential; only the type is printed"


def test_a_leaf_whose_pci_sizes_could_not_be_read_prints_unknown_bytes_never_zero(store) -> None:
    # The engine answers pci_index_bytes null when a lock on a pci_ index cost the size read (nexus-i827c).
    store.outcome = {"leaves": [_leaf("chunks_m0a90d9bc_t_aaaa", ["pinned"], pci_index_bytes=None)]}
    r = _verbose_row(_golden("overdue_pinned"))
    assert "? in 1 pci_ index(es)" in r.detail
    assert "0 B in 1 pci_ index(es)" not in r.detail


def test_an_empty_listing_says_the_engine_names_no_leaf(store) -> None:
    store.outcome = {"leaves_total": 3, "needing_attention": 0, "leaves": []}
    r = _verbose_row(_golden("overdue_pinned"))
    assert "names no leaf that needs attention" in r.detail


def test_a_long_listing_names_twenty_and_counts_the_rest(store) -> None:
    store.outcome = {"leaves": [_leaf(f"chunks_leaf_{i:03d}", ["starved"]) for i in range(23)]}
    r = _verbose_row(_golden("overdue_pinned"))
    assert "chunks_leaf_019" in r.detail and "chunks_leaf_020" not in r.detail
    assert "and 3 more" in r.detail


def test_a_body_without_a_leaves_list_is_unreadable_not_a_crash(store) -> None:
    store.outcome = {"leaves_total": 3}
    r = _verbose_row(_golden("overdue_pinned"))
    assert "leaf listing unreadable" in r.detail


def test_the_doctor_verbose_flag_reaches_the_sweep(monkeypatch) -> None:
    seen: dict = {}

    def fake_run(git_hooks_scope=None, engine_status=None, *, verbose=False):
        seen["verbose"] = verbose
        return [], True

    monkeypatch.setattr("nexus.health.run_health_checks", fake_run)
    monkeypatch.setattr(_FETCH, lambda **kw: None)
    monkeypatch.setattr("nexus.commands.doctor._run_supplementary_checks", lambda status: None)
    runner = CliRunner()
    assert runner.invoke(main, ["doctor"]).exit_code == 0
    assert seen == {"verbose": False}
    assert runner.invoke(main, ["doctor", "--verbose"]).exit_code == 0
    assert seen == {"verbose": True}


# ── a stale listing says so (nexus-i827c review): the body carries expired / last_read_at / consecutive_failures ──


def test_an_expired_view_marks_the_listing_stale_and_names_the_last_read(store) -> None:
    store.outcome = {"expired": True, "last_read_at": "2026-10-20T08:00:00Z", "consecutive_failures": 0,
                     "leaves": [_leaf("chunks_m0a90d9bc_t_aaaa", ["pinned"])]}
    r = _verbose_row(_golden("overdue_pinned"))
    assert "STALE" in r.detail and "2026-10-20T08:00:00Z" in r.detail
    assert "chunks_m0a90d9bc_t_aaaa: pinned" in r.detail, "the stale names are still listed"


def test_an_empty_listing_from_an_expired_view_does_not_say_the_leaves_recovered(store) -> None:
    store.outcome = {"expired": True, "last_read_at": "2026-10-20T08:00:00Z", "leaves": []}
    r = _verbose_row(_status(total=2, expired=True))
    assert "STALE" in r.detail and "2026-10-20T08:00:00Z" in r.detail
    assert "recovered" not in r.detail, "an expired view cannot say a leaf recovered"


def test_the_row_s_own_expired_flag_marks_the_listing_stale_even_if_the_body_does_not(store) -> None:
    store.outcome = {"expired": False, "last_read_at": "2026-10-20T08:00:00Z", "leaves": []}
    r = _verbose_row(_status(total=2, expired=True))
    assert "STALE" in r.detail and "recovered" not in r.detail


def test_failed_reads_after_a_good_one_are_named_on_a_fresh_listing(store) -> None:
    store.outcome = {"expired": False, "last_read_at": "2026-10-22T09:00:00Z", "consecutive_failures": 2,
                     "leaves": [_leaf("chunks_leaf_a", ["starved"])]}
    r = _verbose_row(_golden("overdue_pinned"))
    assert "the last 2 reads failed" in r.detail and "2026-10-22T09:00:00Z" in r.detail
    assert "STALE" not in r.detail


def test_a_fresh_empty_listing_keeps_the_recovered_wording(store) -> None:
    store.outcome = {"expired": False, "last_read_at": "2026-10-22T09:00:00Z", "consecutive_failures": 0, "leaves": []}
    r = _verbose_row(_golden("overdue_pinned"))
    assert "names no leaf that needs attention" in r.detail and "may have recovered" in r.detail
    assert "STALE" not in r.detail
