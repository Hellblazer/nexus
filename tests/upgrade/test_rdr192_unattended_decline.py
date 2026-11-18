# SPDX-License-Identifier: AGPL-3.0-or-later
"""The RDR-192 backfill rung against an unattended run on a managed service (observability look-back 2026-11-17).

Refused only in converge, the rung read pending at every unattended session start after a version bump and logged a
``ladder_rung_deferred`` warning each time (608 over 232 upgrades on one managed install). It now declines in detect,
as the sibling data rungs do: the walk skips it with no warning and still holds its hard-edge dependents, ``nx doctor``
names it with its reason, and the version-change notice names it once with the interactive command.
"""
from __future__ import annotations

from structlog.testing import capture_logs

from nexus import health, upgrade_finish
from nexus.upgrade_ladder.auto_guard import INTERACTIVE_REMEDY
from nexus.upgrade_ladder.protocol import RungStatus
from nexus.upgrade_ladder.registry import RUNG_MANIFEST_SECTION_LABELS, LadderRegistry
from nexus.upgrade_ladder.rungs.rdr192_manifest_backfill import RUNG_NAME
from nexus.upgrade_ladder.runner import LadderRunner, RungOutcome
from nexus.upgrade_ladder.unattended import unattended_run
from tests.upgrade.conftest import InMemoryCompletionLedger
from tests.upgrade.test_rdr192_manifest_backfill_rung import FakeTenant, _record

import pytest


@pytest.mark.parametrize("record", [None, "1.0.0"], ids=["no-record", "older-record"])
def test_an_unattended_detect_on_a_managed_service_is_not_applicable_and_names_the_interactive_command(record) -> None:
    """The observability look-back (2026-11-17): refused only in converge, the rung read pending at every unattended
    session start after a version bump and logged a ladder_rung_deferred warning each time (608 over 232 upgrades on
    one managed install). Refused in detect, as the sibling data rungs are: the walk skips it with no warning, and
    `nx doctor` lists it once under "not checked", whose fix is an interactive `nx upgrade`."""
    tenant = FakeTenant({"knowledge__a": 7})
    tenant.record = None if record is None else _record(record)
    with unattended_run():
        status = tenant.rung(managed_fn=lambda: True).detect()
    assert not status.applicable and not status.pending
    assert INTERACTIVE_REMEDY in status.pending_detail
    if record is not None:
        assert record in status.pending_detail, "an older record is named: the engine gate reads it at any version"
    assert tenant.censuses == 0


def test_an_unattended_managed_detect_still_converges_on_a_record_at_the_installed_version() -> None:
    tenant = FakeTenant({"knowledge__a": 7})
    tenant.record = _record()
    with unattended_run():
        status = tenant.rung(managed_fn=lambda: True).detect()
    assert status.applicable and status.converged


def test_an_interactive_managed_detect_and_an_unattended_local_one_still_read_pending() -> None:
    for managed, unattended in ((True, False), (False, True)):
        tenant = FakeTenant({"knowledge__a": 7})
        with unattended_run(unattended):
            status = tenant.rung(managed_fn=lambda m=managed: m).detect()
        assert status.pending, f"managed={managed} unattended={unattended}"


def test_an_unattended_walk_on_a_managed_service_skips_the_rung_without_a_deferral_warning() -> None:
    tenant = FakeTenant({"knowledge__a": 7})
    tenant.record = _record("1.0.0")
    ledger = InMemoryCompletionLedger()
    with unattended_run(), capture_logs() as logs:
        report = LadderRunner(LadderRegistry((tenant.rung(managed_fn=lambda: True),)), ledger).run()
    assert [r.outcome for r in report.runs] == [RungOutcome.SKIPPED_NOT_APPLICABLE]
    assert not [e for e in logs if e["event"] == "ladder_rung_deferred"]
    assert tenant.censuses == 0 and tenant.lock.entered == 0


# ── the declined rung holds its dependents and is told once ──


class _Dependent:
    """A rung with a hard edge from the backfill: it records when walked, which it must not do while the backfill
    has not finished (registry.HARD_EDGES)."""

    def __init__(self, name: str) -> None:
        self.name = name
        self.walked = 0

    def detect(self) -> RungStatus:
        self.walked += 1
        return RungStatus(applicable=True, converged=True)

    def converge(self, report):  # pragma: no cover - converged at detect
        raise AssertionError("not reached")

    def verify(self) -> bool:
        return True


def test_a_declined_backfill_holds_its_hard_edge_dependents_without_a_warning() -> None:
    """A deferral held them before (nexus-dfemn); a decline must too: section labels and the span fill read converged
    for good once recorded, so recording them before the backfill adds its rows would leave those rows unfilled."""
    tenant = FakeTenant({"knowledge__a": 7})
    dependent = _Dependent(RUNG_MANIFEST_SECTION_LABELS)
    ledger = InMemoryCompletionLedger()
    with unattended_run(), capture_logs() as logs:
        report = LadderRunner(LadderRegistry((tenant.rung(managed_fn=lambda: True), dependent)), ledger).run()
    assert [(r.name, r.outcome) for r in report.runs] == [
        (RUNG_NAME, RungOutcome.SKIPPED_NOT_APPLICABLE), (RUNG_MANIFEST_SECTION_LABELS, RungOutcome.NOT_ATTEMPTED)]
    assert dependent.walked == 0 and ledger.verified_rungs() == frozenset()
    assert not [e for e in logs if e.get("log_level") == "warning"]


def test_an_interactive_walk_still_reaches_the_dependent_after_the_backfill() -> None:
    tenant = FakeTenant({})
    dependent = _Dependent(RUNG_MANIFEST_SECTION_LABELS)
    LadderRunner(LadderRegistry((tenant.rung(managed_fn=lambda: True), dependent)), InMemoryCompletionLedger()).run()
    assert dependent.walked == 1


def test_doctor_names_the_declined_backfill_with_its_reason_and_the_interactive_command(monkeypatch) -> None:
    tenant = FakeTenant({"knowledge__a": 7})
    with unattended_run():
        status = tenant.rung(managed_fn=lambda: True).detect()
    monkeypatch.setattr("nexus.upgrade_ladder.registry.default_registry", lambda: LadderRegistry(()))
    monkeypatch.setattr("nexus.upgrade_ladder.runner.pending_rungs", lambda _reg: [(RUNG_NAME, status)])
    monkeypatch.setattr("nexus.upgrade_ladder.auto_guard.managed_service", lambda: True)
    (row,) = health._check_pending_rungs()
    assert row.warn and not row.ok
    assert f"not checked: {RUNG_NAME} (no completion is recorded, so the engine's reaper stays refused)" in row.detail
    assert any("interactively" in s for s in row.fix_suggestions), row.fix_suggestions


def test_the_version_change_callout_names_the_declined_backfill_once_with_the_command(monkeypatch) -> None:
    tenant = FakeTenant({"knowledge__a": 7})
    tenant.record = _record("1.0.0")
    rung = tenant.rung(managed_fn=lambda: True)
    monkeypatch.setattr("nexus.upgrade_ladder.registry.default_registry", lambda: (rung,))
    monkeypatch.setattr("nexus.upgrade_ladder.auto_guard.managed_service", lambda: True)
    (line,) = upgrade_finish.pending_data_rung_callout()
    assert line.startswith(f"data rung '{RUNG_NAME}' not checked — the completion recorded at package version 1.0.0")
    assert line.endswith("run `nx upgrade` interactively")
    assert upgrade_finish.pending_data_rung_callout(upgrade_walks_ladder=True) == []


def test_on_a_local_engine_doctor_names_a_declined_rung_without_the_managed_reason_and_the_callout_is_silent(
    monkeypatch,
) -> None:
    """Under doctor's bounded reads a local engine declines too, but the session-start upgrade runs the rung there:
    no "run interactively" line, and no reason that speaks of a managed service."""
    reason = f"unattended `nx upgrade --auto` does not scan a managed catalog; {INTERACTIVE_REMEDY}"
    declined = RungStatus(applicable=False, converged=False, pending_detail=reason)
    monkeypatch.setattr("nexus.upgrade_ladder.auto_guard.managed_service", lambda: False)
    monkeypatch.setattr("nexus.upgrade_ladder.registry.default_registry", lambda: LadderRegistry(()))
    monkeypatch.setattr("nexus.upgrade_ladder.runner.pending_rungs", lambda _reg: [(RUNG_NAME, declined)])
    (row,) = health._check_pending_rungs()
    assert f"not checked: {RUNG_NAME} (each answers" in row.detail, row.detail

    class _Declining:
        name = RUNG_NAME

        def detect(self) -> RungStatus:
            return declined

    monkeypatch.setattr("nexus.upgrade_ladder.registry.default_registry", lambda: (_Declining(),))
    assert upgrade_finish.pending_data_rung_callout() == []


class _Failing(_Dependent):
    def detect(self) -> RungStatus:
        raise RuntimeError("boom")


def test_a_hard_failure_after_a_declined_rung_stays_a_failure() -> None:
    """Only a deferral earlier in the walk softens a later failure to a deferral (an engine outage); a decline is not
    an outage, so a failure after one is reported as the failure it is."""
    tenant = FakeTenant({"knowledge__a": 7})
    failing = _Failing("some-later-rung")
    with unattended_run():
        report = LadderRunner(LadderRegistry((tenant.rung(managed_fn=lambda: True), failing)),
                              InMemoryCompletionLedger()).run()
    assert [(r.name, r.outcome) for r in report.runs] == [
        (RUNG_NAME, RungOutcome.SKIPPED_NOT_APPLICABLE), ("some-later-rung", RungOutcome.FAILED)]
