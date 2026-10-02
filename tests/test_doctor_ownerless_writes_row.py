# SPDX-License-Identifier: AGPL-3.0-or-later
"""nx doctor's "Ownerless writes" row (nexus-20onx, RDR-223 P3.2).

The row reads the engine's own counters from ``GET /v1/status``. The cases
build the status body the engine sends (field names from
``docs/wire-contract-pending.md``) and check what the row says for each, with
the not-applicable branches first because a virgin box and an engine that
predates the refusal must stay green.
"""
from __future__ import annotations

from unittest.mock import patch

from nexus.health import _check_ownerless_writes

_FETCH = "nexus.db.http_engine_status.fetch_engine_status"


def _row(status):
    with patch(_FETCH, return_value=status):
        rows = _check_ownerless_writes()
    assert len(rows) == 1
    return rows[0]


def test_an_unreachable_engine_is_not_applicable_and_green() -> None:
    r = _row(None)
    assert r.ok is True and not r.warn
    assert "not applicable" in r.detail


def test_a_probe_that_raises_is_not_applicable_and_green() -> None:
    with patch(_FETCH, side_effect=RuntimeError("boom")):
        (r,) = _check_ownerless_writes()
    assert r.ok is True and "not applicable" in r.detail


def test_an_engine_that_predates_the_refusal_is_not_applicable_and_green() -> None:
    r = _row({"embedding_mode": "local"})
    assert r.ok is True and not r.warn
    assert "predates" in r.detail


def test_zero_counters_are_green_and_say_what_was_examined() -> None:
    r = _row({
        "ownerless_write_mode": "log-only",
        "ownerless_writes_refused_total": 0,
        "ownerless_writes_would_refuse_total": 0,
    })
    assert r.ok is True and not r.warn
    assert "mode=log-only" in r.detail and "since the engine started" in r.detail


def test_would_refuse_counts_in_log_only_warn_and_say_to_restart() -> None:
    r = _row({
        "ownerless_write_mode": "log-only",
        "ownerless_writes_refused_total": 0,
        "ownerless_writes_would_refuse_total": 7,
    })
    assert r.ok is False and r.warn is True and not r.fatal
    assert "7 accepted that enforce mode would refuse" in r.detail
    fixes = " ".join(r.fix_suggestions)
    assert "RESTART" in fixes and "nx-mcp" in fixes
    assert "ownerless_chunk_write_would_refuse" in fixes
    assert "docs/operations/ownerless-write-cutover.md" in fixes


def test_refusals_in_enforce_warn() -> None:
    r = _row({
        "ownerless_write_mode": "enforce",
        "ownerless_writes_refused_total": 3,
        "ownerless_writes_would_refuse_total": 0,
    })
    assert r.ok is False and r.warn is True
    assert "3 refused" in r.detail and "mode=enforce" in r.detail


def test_both_counters_are_named_when_both_move() -> None:
    r = _row({
        "ownerless_write_mode": "enforce",
        "ownerless_writes_refused_total": 2,
        "ownerless_writes_would_refuse_total": 5,
    })
    assert "2 refused" in r.detail and "5 accepted" in r.detail


def test_a_status_passed_in_is_used_and_nothing_is_fetched() -> None:
    """One `nx doctor` run fetches /v1/status once and hands it to this row and the activity block."""
    with patch(_FETCH, side_effect=AssertionError("must not fetch")):
        (r,) = _check_ownerless_writes({
            "ownerless_write_mode": "enforce",
            "ownerless_writes_refused_total": 1,
        })
        assert r.warn is True
        (r,) = _check_ownerless_writes(None)  # the caller's fetch ran and failed
        assert r.ok is True and "not applicable" in r.detail


def test_the_default_sweep_makes_one_status_request_for_row_and_activity_block(monkeypatch) -> None:
    from click.testing import CliRunner

    from nexus.cli import main

    calls = {"n": 0}

    def _fake_fetch(**kwargs):
        calls["n"] += 1
        return {
            "ownerless_write_mode": "log-only",
            "ownerless_writes_refused_total": 0,
            "ownerless_writes_would_refuse_total": 2,
            "embedding_mode": "local",
        }

    monkeypatch.setattr("nexus.db.http_engine_status.fetch_engine_status", _fake_fetch)
    result = CliRunner().invoke(main, ["doctor"])
    assert calls["n"] == 1, result.output
    assert "Ownerless writes" in result.output
    assert "accepted that enforce mode would refuse" in result.output


def test_a_garbage_counter_is_not_a_warning() -> None:
    r = _row({
        "ownerless_write_mode": "log-only",
        "ownerless_writes_refused_total": "many",
        "ownerless_writes_would_refuse_total": True,
    })
    assert r.ok is True
