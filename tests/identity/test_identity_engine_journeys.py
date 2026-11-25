# SPDX-License-Identifier: AGPL-3.0-or-later
"""Every engine-backed identity journey of RDR-253 Step 3, run from ONE test on ONE tenant.

Sam's ruling (2026-11-18, T2 nexus/sam-ruling-rdr253-step3-tenants-2026-11-18): Step 3 raises the tenant ceiling
(tests/storage_touch_ceiling.toml) by one test, not by seven. Each module below keeps its row functions and its
``ROWS`` table and holds no test; this test takes the one tenant and hands each table to ``run_journeys``
(tests/identity/conftest.py), which runs it with its own ``Rows`` and tag prefix (so no two rows of the run share a host,
seat name or session id) and its own check that no ``row_*`` function was left out of its table.

A failure is reported once, naming ``module::row`` with its traceback. A module that cannot run on the engine (the
token rows, on a jar without Step 2) is a failure and the other modules still run; only
``NX_IDENTITY_REQUIRE_STEP2=0`` records it as skipped instead. ``NX_ROWS=<substring>`` keeps the rows whose ``module::row`` name
contains it (``NX_ROWS=session_start_bind::lapsed``, ``NX_ROWS=hook_leg``).

To add engine-backed rows (RDR-253 Steps 4 to 6): write ``row_*`` functions taking a ``World`` and a ``ROWS`` tuple in a
module under tests/identity, give it no test function and no tenant mark, and add one ``_journey(module, "<tag>")``
entry to ``JOURNEYS`` with a tag no other entry uses (``run_journeys`` refuses a repeated tag or module). Never add a
second tenant test: the ceiling is one. ``test_rows_collector.py`` fails when a module with a ``ROWS`` table is
missing from ``JOURNEYS``.
"""
from __future__ import annotations

import sys
import warnings
from collections.abc import Callable

import pytest

from nexus.identity import token_header

#: Opt out of ``tests/conftest.py``'s autouse ``_no_real_resume_pointer_store``: the work-engine rows read the resume
#: pointer through the real opener, against this tenant's engine.
REAL_RESUME_POINTER_STORE = True
from tests.identity import (
    test_group_journeys,
    test_identity_step4,
    test_hook_leg,
    test_identity_token_engine,
    test_lease_wiring,
    test_mcp_identity_lease,
    test_owner_path_journeys,
    test_pending_bind,
    test_session_start_bind,
    test_sweep,
    test_work_engine_rows,
)
from tests.identity.conftest import Journey, World, run_journeys


def _journey(module, tag: str, **extra) -> Journey:
    return Journey(module.__name__.rsplit(".", 1)[-1].removeprefix("test_"), tag, module.ROWS, vars(module), **extra)


JOURNEYS = (
    _journey(test_session_start_bind, "ssb"),
    _journey(test_mcp_identity_lease, "mil"),
    _journey(test_sweep, "swp"),
    _journey(test_hook_leg, "hkl"),
    _journey(test_lease_wiring, "lwr"),
    _journey(test_pending_bind, "pnb"),
    # the token rows need the Step 2 engine; the rest run on any engine. The rows leave token_header's token-file and
    # ancestor caches process-global, so they are reset after the module, a failing row included.
    _journey(test_identity_token_engine, "ite", precheck=test_identity_token_engine.step2_skip_reason,
             require_env=test_identity_token_engine.REQUIRE_ENV, cleanup=token_header.reset_for_tests),
    # Step 4 (nexus-hczdo.1.5): take-over, release, promote, rename, purge and delegated identities. The delegated-child
    # fence row sends identity tokens, so the module needs Step 2 and resets the same caches.
    _journey(test_identity_step4, "st4", precheck=test_identity_step4.step2_skip_reason, require_env=test_identity_step4.REQUIRE_ENV,
             cleanup=token_header.reset_for_tests),
    # RDR-252 Step 4 part B1 (nexus-igea9.7): `nx group` and nexus.groups. The rows send identity tokens and reset the same
    # caches; they assert the engine issued one, so an engine without Step 2 fails them (no precheck is needed).
    _journey(test_group_journeys, "grp", cleanup=token_header.reset_for_tests),
    # RDR-252 post-close audit lane A5 (nexus-igea9): the owner paths never seen live (no_record, chain_gap, owner_not_live,
    # owner_unleased, stamper_error, escalated). The rows send identity tokens and reset the same caches.
    _journey(test_owner_path_journeys, "own", cleanup=token_header.reset_for_tests),
    # Step 6 (nexus-hczdo.1.9): identity scratch, memberships, per-identity T2 and background work. The rows send identity
    # tokens, so the module needs Step 2 and resets the same caches.
    _journey(test_work_engine_rows, "wrk", precheck=test_work_engine_rows.step2_skip_reason,
             require_env=test_work_engine_rows.REQUIRE_ENV, cleanup=token_header.reset_for_tests),
)


@pytest.mark.t2_tenant
def test_the_engine_backed_identity_journeys(world: World, record_property: Callable[[str, object], None]) -> None:
    """Every module's rows against one engine tenant; a failure names its module and row, and the rest still run."""
    try:
        report = run_journeys(world, JOURNEYS)
    finally:
        token_header.reset_for_tests()
    sys.stdout.write(report.text() + "\n")                                     # shown with -s / -rP; a failure carries the same text
    for name, secs in report.seconds.items():
        record_property(f"row_seconds[{name}]", f"{secs:.2f}")
    for module, why in report.skipped.items():
        record_property(f"skipped[{module}]", why)
        warnings.warn(f"identity journeys: module {module!r} skipped: {why}", stacklevel=1)
