# SPDX-License-Identifier: AGPL-3.0-or-later
"""nexus-o8dil.15 (RDR-191 P4 repoint batch, risk R4): the
``_provision_diag_conformance_view`` DO block must not report ONE ambiguous
"attempted" line regardless of whether it actually created the view.

``_provision_diag_conformance_view`` issues a single anonymous
``DO $do$ ... END $do$;`` block whose ``IF`` branch either creates
``nexus.diag_chash_conformance`` (all chash-bearing tables already exist) or
does nothing at all (the normal state on a fresh provision, before Liquibase
has created them). Postgres gives the caller no return value from a DO
block, so the ORIGINAL code logged the exact same
``pg_diag_conformance_view_provision_attempted`` event on both outcomes —
"attempted" is not "succeeded", and a log scan (or an operator) could not
tell a genuine no-op from a genuine creation. That is the same
"reports success while converging nothing" shape RDR-182 forbids for the
chash-rekey rung's own diagnostics (see
``tests/upgrade/test_chash_rekey_verification_non_vacuous.py``).

RDR-194 P3c companion fix (nexus-v5lk3, 2026-08-17) added a THIRD outcome:
the view may already EXIST before this call, in which case the DDL is
skipped entirely (this function now defers to an existing view rather than
unconditionally re-CREATE-OR-REPLACE-ing it — see the function's own
docstring, "DEFERS TO AN EXISTING VIEW"). The three outcomes below —
already-present-deferred / freshly-created / tables-not-ready-yet — must
each emit a DIFFERENT, mutually exclusive structlog event.

These are PURE unit tests: ``_psql`` and ``_psql_tuples`` are monkeypatched
at the module level so nothing here touches a real Postgres cluster or the
Java engine — no bundled binaries, no jar, no network.
"""
from __future__ import annotations

import logging
from pathlib import Path

import structlog
import structlog.testing

import nexus.db.pg_provision as pg_provision
from nexus.db.pg_provision import PgBinaries, _provision_diag_conformance_view

_ALL_EVENTS = (
    "pg_diag_conformance_view_already_present_deferred",
    "pg_diag_conformance_view_provisioned",
    "pg_diag_conformance_view_provision_skipped_tables_absent",
    "pg_diag_view_best_effort_failed",
)


def _dummy_bins() -> PgBinaries:
    # Never actually exec'd — _psql is monkeypatched below — but PgBinaries
    # is a frozen dataclass of Paths, so it needs *something* path-shaped.
    d = Path("/nonexistent/pg/bin")
    return PgBinaries.from_dir(d)


def setup_function(_fn) -> None:
    # The suite runs structlog at WARNING by default (tests/conftest.py
    # pytest_configure), and make_filtering_bound_logger drops below-
    # threshold calls BEFORE any processor runs — so capture_logs() alone
    # would see nothing for this module's INFO-level events. Pin to INFO,
    # the level MCP/CLI mode actually runs at (logging_setup._resolve_level),
    # matching the pattern in tests/test_plan_match_binding_satisfiability.py.
    structlog.configure(wrapper_class=structlog.make_filtering_bound_logger(logging.INFO))


def _events_for(monkeypatch, tuples_responses: list[str]) -> set[str]:
    """Run the function once with a scripted sequence of _psql_tuples
    responses (one per call, in order) and return the structlog event
    names observed."""
    monkeypatch.setattr(pg_provision, "_psql", lambda *a, **k: None)
    responses = iter(tuples_responses)

    def _tuples(*_a, **_k):
        return next(responses)

    monkeypatch.setattr(pg_provision, "_psql_tuples", _tuples)
    with structlog.testing.capture_logs() as logs:
        _provision_diag_conformance_view(_dummy_bins(), 5432, "postgres")
    return {e["event"] for e in logs}


def test_view_already_present_is_deferred_not_recreated(monkeypatch):
    """Outcome 1 (nexus-v5lk3): the existence check (the FIRST
    ``_psql_tuples`` call) reports the view is already there — the DDL
    (``_psql``, the CREATE OR REPLACE + GRANT) must never even be attempted,
    and this must be its own, distinguishable event."""
    monkeypatch.setattr(pg_provision, "_psql_tuples", lambda *a, **k: "1")
    ddl_call_count = 0

    def _count_ddl(*_a, **_k):
        nonlocal ddl_call_count
        ddl_call_count += 1

    monkeypatch.setattr(pg_provision, "_psql", _count_ddl)
    with structlog.testing.capture_logs() as logs:
        _provision_diag_conformance_view(_dummy_bins(), 5432, "postgres")
    events = {e["event"] for e in logs}

    assert "pg_diag_conformance_view_already_present_deferred" in events
    assert "pg_diag_conformance_view_provisioned" not in events
    assert "pg_diag_conformance_view_provision_skipped_tables_absent" not in events
    assert "pg_diag_view_best_effort_failed" not in events
    assert ddl_call_count == 0, (
        "the DDL must never run when the view already exists — a second "
        "independent writer of the same object as taxonomy-011-8 is "
        "exactly the drift class nexus-v5lk3 closes"
    )


def test_view_freshly_created_when_absent(monkeypatch):
    """Outcome 2: absent before the call (existence check returns falsy),
    the DO block runs, and the FOLLOW-UP existence probe reports the view
    IS there afterward (freshly created)."""
    events = _events_for(monkeypatch, ["", "1"])
    assert "pg_diag_conformance_view_provisioned" in events
    assert "pg_diag_conformance_view_already_present_deferred" not in events
    assert "pg_diag_conformance_view_provision_skipped_tables_absent" not in events
    assert "pg_diag_view_best_effort_failed" not in events


def test_view_still_absent_when_chash_tables_not_ready(monkeypatch):
    """Outcome 3: absent before the call, the DO block's IF branch does not
    fire (chash-bearing tables not all present yet — the normal state on a
    fresh provision before Liquibase has created them), so the view is
    STILL absent afterward."""
    events = _events_for(monkeypatch, ["", ""])
    assert "pg_diag_conformance_view_provision_skipped_tables_absent" in events
    assert "pg_diag_conformance_view_provisioned" not in events
    assert "pg_diag_conformance_view_already_present_deferred" not in events
    assert "pg_diag_view_best_effort_failed" not in events


def test_the_three_outcomes_are_pairwise_disjoint(monkeypatch):
    """THE NON-VACUITY ASSERTION, extended to three outcomes: no two of the
    three scenarios above may ever share an event name."""
    already_present = _events_for(monkeypatch, ["1"])
    freshly_created = _events_for(monkeypatch, ["", "1"])
    tables_not_ready = _events_for(monkeypatch, ["", ""])

    assert already_present.isdisjoint(freshly_created)
    assert already_present.isdisjoint(tables_not_ready)
    assert freshly_created.isdisjoint(tables_not_ready)
    # And each is a genuine subset of the known event vocabulary — a typo'd
    # event name would otherwise pass the disjointness checks vacuously.
    for events in (already_present, freshly_created, tables_not_ready):
        assert events <= set(_ALL_EVENTS)
        assert events, "each scenario must emit at least one event"


def test_a_genuine_psql_failure_still_degrades_best_effort(monkeypatch):
    """The existing best-effort contract (absent view = probe falls back to
    legacy statements) must survive the non-vacuity fix: a real psql failure
    is still swallowed into ``pg_diag_view_best_effort_failed``, not raised,
    and does not falsely claim provisioned, deferred, or skipped-tables-absent."""

    def _boom(*_a, **_k):
        raise RuntimeError("psql exit 1: could not connect to server")

    monkeypatch.setattr(pg_provision, "_psql_tuples", _boom)
    with structlog.testing.capture_logs() as logs:
        _provision_diag_conformance_view(_dummy_bins(), 5432, "postgres")
    events = {e["event"] for e in logs}
    assert "pg_diag_view_best_effort_failed" in events
    assert "pg_diag_conformance_view_provisioned" not in events
    assert "pg_diag_conformance_view_already_present_deferred" not in events
    assert "pg_diag_conformance_view_provision_skipped_tables_absent" not in events


def test_the_existence_probe_targets_the_real_view_identity(monkeypatch):
    """The existence check(s) must query DIAG_CONFORMANCE_VIEW's actual
    schema-qualified identity, not a hand-typed duplicate that could drift
    from ``nexus.db.chash_tables.DIAG_CONFORMANCE_VIEW``."""
    from nexus.db.chash_tables import DIAG_CONFORMANCE_VIEW

    monkeypatch.setattr(pg_provision, "_psql", lambda *a, **k: None)
    seen_sql: list[str] = []

    def _tuples(bins, port, db, user, sql):  # noqa: ARG001 — signature match
        seen_sql.append(sql)
        return "1"

    monkeypatch.setattr(pg_provision, "_psql_tuples", _tuples)
    _provision_diag_conformance_view(_dummy_bins(), 5432, "postgres")

    schema, relname = DIAG_CONFORMANCE_VIEW.split(".", 1)
    assert any(
        f"n.nspname = '{schema}'" in sql and f"c.relname = '{relname}'" in sql
        for sql in seen_sql
    ), f"existence probe did not target {DIAG_CONFORMANCE_VIEW}: {seen_sql!r}"
