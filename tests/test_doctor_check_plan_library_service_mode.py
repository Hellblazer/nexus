"""nx doctor --check-plan-library honesty (RDR-092 Phase 0c.2, PORTED at
nexus-vl8lk).

HISTORY: this check originally queried the local SQLite ``plans`` table.
RDR-158 P3 (nexus-7bomn) killed the =sqlite opt-out and the check was
stubbed to an unconditional "N/A in service mode" — ALWAYS printed,
ALWAYS exited 0, checking nothing (nexus-vl8lk: "a stub that prints and
exits 0 is indistinguishable ... from a check that ran and found nothing
wrong"). This suite pins the PORTED behavior: the check reads the live
plan library via ``HttpPlanLibrary.list_plans`` (no new engine route)
and renders the original authored / backfilled / non-dimensional / global
-builtin census against real service data.
"""

from __future__ import annotations

import re
from unittest.mock import patch

import click
import httpx
from click.testing import CliRunner

from nexus.commands.doctor import _MIN_GLOBAL_BUILTIN_COUNT, _run_check_plan_library


def _row(*, dimensions=None, tags="", project=""):
    return {"dimensions": dimensions, "tags": tags, "project": project}


def _builtin_rows(n: int) -> list[dict]:
    return [
        _row(dimensions=f"d{i}", tags="builtin-template", project="")
        for i in range(n)
    ]


def _run() -> tuple[str, int | None]:
    """Drive the check with the disk-vs-live parity assert stubbed out.

    These rows are synthetic census fixtures (``dimensions="d0"``...), so
    the parity assert added at nexus-f1mbo would correctly report every
    shipped template as missing and drown the census this file is about.
    Parity has its own non-vacuity suite in
    ``tests/test_plan_seed_reconcile.py``; here it is deliberately silent.
    """
    from nexus.commands.doctor import _ParityReport

    runner = CliRunner()
    with runner.isolation() as (out, err, _), patch(
        "nexus.commands.doctor._plan_library_parity",
        return_value=_ParityReport([], [], []),
    ):
        exit_code: int | None = None
        try:
            _run_check_plan_library()
        except click.exceptions.Exit as exc:
            exit_code = exc.exit_code
        printed = out.getvalue().decode() + err.getvalue().decode()
    return printed, exit_code


def test_healthy_library_reports_counts_and_passes():
    """Buckets are NOT mutually exclusive with the global-builtin count:
    a builtin row counts as BOTH "authored" (dimensioned, non-backfill
    tags) AND "global-tier builtin" (project='' + builtin-template tag) —
    same overlapping-partition semantics the original SQLite census used
    (dimension-state buckets vs. the separate builtin-floor subset)."""
    n_builtins = _MIN_GLOBAL_BUILTIN_COUNT + 2
    rows = (
        _builtin_rows(n_builtins)
        + [_row(dimensions="grown1", tags="", project="personal")]  # +authored
        + [_row(dimensions="grown2", tags="backfill", project="personal")]  # backfilled
        + [_row(dimensions=None, tags="", project="personal")]  # non-dimensional
    )
    with patch("nexus.db.t2.http_plan_library.HttpPlanLibrary") as lib:
        lib.return_value.list_plans.return_value = rows
        printed, exit_code = _run()

    assert exit_code is None
    assert "All checks passed" in printed
    assert f"global-tier builtin count: {n_builtins}" in printed
    assert re.search(rf"authored:\s+{n_builtins + 1}\b", printed)
    assert re.search(r"backfilled:\s+1\b", printed)
    assert re.search(r"non-dimensional:\s+1\b", printed)


def test_below_floor_exits_1_names_reseed_fix():
    """Below the builtin floor is a genuine FAIL — the reseed verb named
    in the fix hint is the LIVE command (`nx plan reseed`), not the
    retired `nx catalog setup`."""
    with patch("nexus.db.t2.http_plan_library.HttpPlanLibrary") as lib:
        lib.return_value.list_plans.return_value = _builtin_rows(1)
        printed, exit_code = _run()

    assert exit_code == 1
    assert "FAIL" in printed
    assert "global-tier builtin count 1" in printed
    assert "nx plan reseed" in printed
    assert "nx catalog setup" not in printed


def test_unreachable_service_exits_2_state_unknown():
    """The false-clean class this bead exists to close: an unreachable
    service must never read as '0 builtins found', it must read UNKNOWN."""
    with patch("nexus.db.t2.http_plan_library.HttpPlanLibrary") as lib:
        lib.side_effect = httpx.ConnectError("refused")
        printed, exit_code = _run()

    assert exit_code == 2
    assert "UNKNOWN" in printed
    assert "global-tier builtin count: 0" not in printed


def test_unresolvable_endpoint_also_reports_unknown():
    """ServiceEndpointUnresolvableError is a RuntimeError, NOT an
    httpx.HTTPError — catching only httpx errors would let this escape as
    a traceback (the documented trap on every sibling _report_*_service)."""
    from nexus.db.service_endpoint import ServiceEndpointUnresolvableError

    with patch("nexus.db.t2.http_plan_library.HttpPlanLibrary") as lib:
        lib.side_effect = ServiceEndpointUnresolvableError("no lease, no token")
        printed, exit_code = _run()

    assert exit_code == 2
    assert "UNKNOWN" in printed


def test_page_cap_truncation_is_a_named_note_not_silent():
    """Non-vacuity: hitting the paging ceiling must not silently pass off
    an undercounted total as the whole library."""
    from nexus.db.limits import MAX_QUERY_RESULTS

    with patch("nexus.db.t2.http_plan_library.HttpPlanLibrary") as lib:
        lib.return_value.list_plans.return_value = _builtin_rows(MAX_QUERY_RESULTS)
        printed, _exit_code = _run()

    assert "page cap" in printed
