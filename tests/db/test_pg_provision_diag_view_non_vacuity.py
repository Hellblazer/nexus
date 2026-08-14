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


def test_view_created_is_distinguishable_from_view_skipped(monkeypatch):
    """THE NON-VACUITY ASSERTION: the two outcomes must emit DIFFERENT,
    mutually exclusive structlog events — never the same line for both."""
    monkeypatch.setattr(pg_provision, "_psql", lambda *a, **k: None)

    # Outcome A: the follow-up existence probe reports the view IS there
    # (either freshly created by the DO block, or already present).
    monkeypatch.setattr(pg_provision, "_psql_tuples", lambda *a, **k: "1")
    with structlog.testing.capture_logs() as logs:
        _provision_diag_conformance_view(_dummy_bins(), 5432, "postgres")
    events_a = {e["event"] for e in logs}
    assert "pg_diag_conformance_view_provisioned" in events_a
    assert "pg_diag_conformance_view_provision_skipped_tables_absent" not in events_a
    assert "pg_diag_view_best_effort_failed" not in events_a

    # Outcome B: the follow-up existence probe reports the view is ABSENT
    # (the DO block's IF did not fire — chash-bearing tables not all present
    # yet, the normal state on a fresh provision).
    monkeypatch.setattr(pg_provision, "_psql_tuples", lambda *a, **k: "")
    with structlog.testing.capture_logs() as logs:
        _provision_diag_conformance_view(_dummy_bins(), 5432, "postgres")
    events_b = {e["event"] for e in logs}
    assert "pg_diag_conformance_view_provision_skipped_tables_absent" in events_b
    assert "pg_diag_conformance_view_provisioned" not in events_b
    assert "pg_diag_view_best_effort_failed" not in events_b

    # THE POINT: outcome A and outcome B must never share an event name —
    # a caller (or a log-scanning gate) can always tell them apart.
    assert events_a.isdisjoint(events_b) or (
        "pg_diag_conformance_view_provisioned" in events_a
        and "pg_diag_conformance_view_provisioned" not in events_b
    )


def test_a_genuine_psql_failure_still_degrades_best_effort(monkeypatch):
    """The existing best-effort contract (absent view = probe falls back to
    legacy statements) must survive the non-vacuity fix: a real psql failure
    is still swallowed into ``pg_diag_view_best_effort_failed``, not raised,
    and does not falsely claim either provisioned or skipped-tables-absent."""

    def _boom(*_a, **_k):
        raise RuntimeError("psql exit 1: could not connect to server")

    monkeypatch.setattr(pg_provision, "_psql", _boom)
    with structlog.testing.capture_logs() as logs:
        _provision_diag_conformance_view(_dummy_bins(), 5432, "postgres")
    events = {e["event"] for e in logs}
    assert "pg_diag_view_best_effort_failed" in events
    assert "pg_diag_conformance_view_provisioned" not in events
    assert "pg_diag_conformance_view_provision_skipped_tables_absent" not in events


def test_the_existence_probe_targets_the_real_view_identity(monkeypatch):
    """The follow-up probe must query DIAG_CONFORMANCE_VIEW's actual
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
