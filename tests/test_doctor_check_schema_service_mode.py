"""nx doctor --check-schema honesty (nexus-p0clh, PORTED at nexus-vl8lk).

HISTORY: nexus-p0clh made this check report N/A in service mode instead
of "T2 database not found — nothing to check" (the local SQLite reader's
failure mode). That was itself a vacuous stub: N/A ALWAYS printed and
ALWAYS exited 0, checking nothing (nexus-vl8lk). This suite now pins the
PORTED behavior: the check asks the engine's GET /version for the
Liquibase changelog fingerprint via
nexus.health.probe_t2_schema_fingerprint and renders an honest verdict.
"""

from __future__ import annotations

from unittest.mock import patch

import click
import pytest
from click.testing import CliRunner

from nexus.commands.doctor import _run_check_schema
from nexus.health import T2SchemaFingerprint


def _fp(**kwargs) -> T2SchemaFingerprint:
    defaults = dict(reachable=True, reported=True)
    defaults.update(kwargs)
    return T2SchemaFingerprint(**defaults)


def _run() -> tuple[str, str]:
    """Run ``_run_check_schema`` under a CliRunner isolation context so
    ``click.echo(..., err=True)`` is captured reliably — mirrors the
    established pattern for ``_run_check_*`` functions that can raise
    ``click.exceptions.Exit`` (tests/test_doctor_dangling_links.py)."""
    runner = CliRunner()
    with runner.isolation() as (out, err, _):
        exit_code: int | None = None
        try:
            _run_check_schema()
        except click.exceptions.Exit as exc:
            exit_code = exc.exit_code
        printed = out.getvalue().decode() + err.getvalue().decode()
    return printed, exit_code


def test_healthy_engine_exits_zero_reports_count():
    with patch(
        "nexus.health.probe_t2_schema_fingerprint",
        return_value=_fp(latest_id="vectors-014", changeset_count=209),
    ):
        printed, exit_code = _run()
    assert exit_code is None
    assert "OK" in printed
    assert "209 changeset" in printed
    assert "vectors-014" in printed


def test_unreachable_engine_exits_2_state_unknown():
    with patch(
        "nexus.health.probe_t2_schema_fingerprint",
        return_value=T2SchemaFingerprint(
            reachable=False, reported=False, unreachable_detail="connection refused",
        ),
    ):
        printed, exit_code = _run()
    assert exit_code == 2
    assert "UNKNOWN" in printed
    assert "connection refused" in printed


def test_schema_error_exits_1():
    with patch(
        "nexus.health.probe_t2_schema_fingerprint",
        return_value=_fp(schema_error="databasechangelog unreadable"),
    ):
        printed, exit_code = _run()
    assert exit_code == 1
    assert "databasechangelog unreadable" in printed


def test_zero_changesets_exits_1_non_vacuity():
    with patch(
        "nexus.health.probe_t2_schema_fingerprint",
        return_value=_fp(changeset_count=0),
    ):
        printed, exit_code = _run()
    assert exit_code == 1
    assert "applied nothing" in printed


def test_managed_endpoint_omits_fields_is_honest_na_exits_0():
    """The managed/cloud endpoint withholds the fingerprint by design — the
    check reports N/A and exits 0 (not a failure, not a silent pass over
    something it never asked)."""
    with patch(
        "nexus.health.probe_t2_schema_fingerprint",
        return_value=T2SchemaFingerprint(reachable=True, reported=False),
    ):
        printed, exit_code = _run()
    assert exit_code is None
    assert "N/A" in printed
    assert "not exposed" in printed
