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
