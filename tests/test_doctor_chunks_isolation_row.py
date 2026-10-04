# SPDX-License-Identifier: AGPL-3.0-or-later
"""nx doctor's "Chunk tenant isolation" row (nexus-wbfpw.48).

The row reads ``chunks_tenant_isolation_intact`` from ``GET /v1/status``. The cases build the status body the
engine sends (field name from ``docs/wire-contract-pending.md``) and check what the row says, with the
not-applicable branches first because a virgin box and an engine that predates the field must stay green.
"""
from __future__ import annotations

from unittest.mock import patch

from nexus.health import _check_chunks_tenant_isolation

_FETCH = "nexus.db.http_engine_status.fetch_engine_status"


def _row(status):
    with patch(_FETCH, return_value=status):
        rows = _check_chunks_tenant_isolation()
    assert len(rows) == 1
    return rows[0]


def test_an_unreachable_engine_is_not_applicable_and_green() -> None:
    r = _row(None)
    assert r.ok is True and not r.warn and not r.fatal
    assert "not applicable" in r.detail


def test_a_probe_that_raises_is_not_applicable_and_green() -> None:
    with patch(_FETCH, side_effect=RuntimeError("boom")):
        (r,) = _check_chunks_tenant_isolation()
    assert r.ok is True and "not applicable" in r.detail


def test_an_engine_that_predates_the_field_is_not_applicable_and_green() -> None:
    r = _row({"embedding_mode": "local"})
    assert r.ok is True and not r.fatal
    assert "not applicable" in r.detail


def test_a_non_boolean_value_is_not_applicable_never_a_guess() -> None:
    for junk in ("false", 0, 1, None, [], {}):
        r = _row({"chunks_tenant_isolation_intact": junk})
        assert r.ok is True and "not applicable" in r.detail, junk


def test_true_is_green_and_says_what_was_examined() -> None:
    r = _row({"chunks_tenant_isolation_intact": True})
    assert r.ok is True and not r.warn and not r.fatal
    assert "tenant_isolation" in r.detail


def test_false_is_a_hard_failure_that_names_the_policy_and_both_remedies() -> None:
    r = _row({"chunks_tenant_isolation_intact": False})
    assert r.ok is False and r.fatal is True and not r.warn
    assert "chunks_gate_probe_owner_read" in r.detail
    assert "read or write every tenant's chunks" in r.detail
    assert "ENABLE ROW LEVEL SECURITY" in " ".join(r.fix_suggestions)
    fixes = " ".join(r.fix_suggestions)
    assert "NX_DB_ADMIN_URL" in fixes
    assert "DROP POLICY chunks_gate_probe_owner_read ON nexus.chunks" in fixes
    assert "chunks_isolation_check_failed" in fixes


def test_a_status_passed_in_is_used_and_nothing_is_fetched() -> None:
    """One `nx doctor` run fetches /v1/status once and hands it to every row that reads it."""
    with patch(_FETCH, side_effect=AssertionError("must not fetch")):
        (r,) = _check_chunks_tenant_isolation({"chunks_tenant_isolation_intact": False})
        assert r.fatal is True
        (r,) = _check_chunks_tenant_isolation(None)  # the caller's fetch ran and failed
        assert r.ok is True and "not applicable" in r.detail


def test_the_default_sweep_runs_the_row_from_the_one_status_fetch(monkeypatch) -> None:
    from click.testing import CliRunner

    from nexus.cli import main

    calls = {"n": 0}

    def _fake_fetch(**kwargs):
        calls["n"] += 1
        return {"embedding_mode": "local", "chunks_tenant_isolation_intact": False}

    monkeypatch.setattr("nexus.db.http_engine_status.fetch_engine_status", _fake_fetch)
    result = CliRunner().invoke(main, ["doctor"])
    assert calls["n"] == 1, result.output
    assert "Chunk tenant isolation" in result.output
    assert "chunks_gate_probe_owner_read" in result.output
