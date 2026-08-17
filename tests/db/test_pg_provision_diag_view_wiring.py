# SPDX-License-Identifier: AGPL-3.0-or-later
"""RDR-194 P3c critical-fix round 4 (nexus-rkn3i, 2026-08-17): wiring-
completeness regression for the diag-view ownership reassignment.

Round 3 wired ``reassign_diag_view_owner_before_restart`` into ONLY
``converge_engine``'s two restart-triggering call sites — both round-3
reviewers (code-review-expert and substantive-critic, independently) found
this left ``nx daemon service install-binary``'s own documented restart
path, and OS-level launchd/systemd autostart, completely unwired, and gave
a wedged install no automated recovery (T2
``nexus/rdr194-p3c-round3-final-verification-2026-08-17`` [22766]). Round 4
moved the PRIMARY wiring to ``provision()``'s fast idempotency path — the
single choke point EVERY service start traverses
(``nexus.daemon.storage_service_daemon.StorageServiceSupervisor.
_backfill_provision_grants``, called from ``_ensure_pg_running`` at Step 1
of EVERY ``_start_locked``) — and kept the ``converge_engine`` wiring as
belt-and-braces.

This is a pure, PG-less STRUCTURAL test — ``tests/db/test_pg_provision.py``
(``TestReassignDiagViewOwnerBeforeRestart``,
``TestProvisionFastPathReassignsDiagView``) already proves the function and
the fast-path wiring BEHAVE correctly against a real cluster. This file
exists so a FUTURE refactor that accidentally drops (or never re-adds) the
call — e.g. while "cleaning up" ``provision()``'s fast path, splitting it
into a new function, or introducing a new restart-triggering call site
elsewhere — fails loud here instead of silently reintroducing round 3's
exact reachability gap. No PostgreSQL binaries required; runs under
``-n auto`` with the rest of the unit suite.
"""
from __future__ import annotations

import inspect
import re

import nexus.db.pg_provision as pg_provision
import nexus.upgrade_finish as upgrade_finish
from nexus.daemon import storage_service_daemon


def test_provision_fast_path_calls_the_reassignment() -> None:
    """THE PRIMARY wiring (round 4): ``provision()``'s own source must call
    ``reassign_diag_view_owner_before_restart`` somewhere in its body."""
    source = inspect.getsource(pg_provision.provision)
    assert "reassign_diag_view_owner_before_restart(" in source, (
        "provision()'s fast idempotency path must call "
        "reassign_diag_view_owner_before_restart — without this, the "
        "diag-view ownership crash-loop (RDR-194 P3c, nexus-v5lk3 / "
        "nexus-rkn3i) is unreachable from `nx daemon service "
        "install-binary`'s own documented restart path and OS autostart, "
        "and a wedged install has no automated recovery"
    )


def test_backfill_provision_grants_reaches_provision() -> None:
    """The chain's OTHER half: ``StorageServiceSupervisor.
    _backfill_provision_grants`` (called from ``_ensure_pg_running`` at
    Step 1 of EVERY ``_start_locked`` — every service start, any trigger)
    must actually call ``pg_provision.provision`` — otherwise the fast-path
    wiring above is correct but unreachable from any real service start."""
    source = inspect.getsource(
        storage_service_daemon.StorageServiceSupervisor._backfill_provision_grants
    )
    # Call-site-specific pattern (critic round-4 Sig finding): a bare
    # `"provision(" in source` is satisfied by the docstring's prose
    # mentions of provision() alone — match the actual call shape so a
    # refactor that deletes the call but keeps the docstring fails here.
    assert re.search(r"^\s*provision\(config_dir", source, re.MULTILINE), (
        "_backfill_provision_grants must call pg_provision.provision() — "
        "this is the documented nexus-hzhgl choke point EVERY service "
        "start traverses before the JVM (and therefore Liquibase) is "
        "spawned; if this call is ever removed, the diag-view "
        "reassignment (and every other fast-path backfill) becomes "
        "unreachable from a live service start"
    )


def test_converge_engine_secondary_wiring_survives() -> None:
    """The SECONDARY, belt-and-braces wiring (round 3, kept per explicit
    instruction as idempotent redundancy) must also survive — both
    restart-triggering call sites in ``converge_engine`` must still invoke
    the pre-restart reassignment wrapper."""
    source = inspect.getsource(upgrade_finish.converge_engine)
    call_count = source.count("_reassign_diag_view_before_restart(")
    assert call_count == 2, (
        "converge_engine must call _reassign_diag_view_before_restart at "
        "BOTH of its restart-triggering call sites (the 'on-disk right, "
        "process stale' branch and the post-install_binary branch) — "
        f"found {call_count} call(s). This is the secondary, redundant-"
        "but-harmless wiring kept alongside the round-4 primary "
        "(provision()'s fast path); losing it silently is not the "
        "reachability regression this file's other two tests guard "
        "against, but is still a real loss of the documented belt-and-"
        "braces guarantee."
    )
