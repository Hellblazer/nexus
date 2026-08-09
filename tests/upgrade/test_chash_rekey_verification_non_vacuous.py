# SPDX-License-Identifier: AGPL-3.0-or-later
"""nexus-hdumg: a rung whose conformance diagnostic could NOT execute must
never declare itself verified.

Reported from the engine-service-v0.1.69 deploy rehearsal (2026-08-09):
``event='diag_sql_failed'`` — relation ``nexus.diag_chash_conformance`` does
not exist — and the NEXT line read ``Upgrade ladder: rung 'chash-rekey'
converged and verified``. That instance was the DESIGNED view→legacy
fallback (benign, see the root-cause note), but it exposed the structural
class underneath: ``verify()`` asserted on the ABSENCE of a non-conformance
signal, so an ERRORED, EMPTY, or UNAVAILABLE diagnostic was indistinguishable
from "all conformant".

RDR-182's own contract (``diag_connection.live_store_detail``,
``health._check_migration_state``): an unavailable diagnostic degrades to an
explicit unavailable note, NEVER a silent all-clean. The sibling surfaces
(the pre-upgrade poison probe, ``upgrade_finish._poison_probe``) already
carry that tri-state; this file pins it onto the ladder rung, which did not.

Every test here FAILS against the pre-fix rung.
"""
from __future__ import annotations

import pytest

from nexus.db.chash_tables import ConformanceProbe, ProbeState
from nexus.upgrade_ladder.protocol import ConvergeOutcome
from nexus.upgrade_ladder.registry import LadderRegistry
from nexus.upgrade_ladder.rungs.chash_rekey import (
    ChashRekeyRung,
    probe_from_diagnostic_output,
)
from nexus.upgrade_ladder.runner import LadderRunner, RungOutcome


class _Report:
    """ProgressReporter — ``emit(event, **fields)`` and nothing else."""

    def __init__(self) -> None:
        self.events: list[str] = []

    def emit(self, event: str, **fields: object) -> None:
        self.events.append(event)


class _Ledger:
    """CompletionLedger recorder — what the runner actually wrote down."""

    def __init__(self) -> None:
        self.recorded: dict[str, str] = {}

    def record_verified(self, rung_name: str, *, package_version: str, detail: str = "") -> None:
        self.recorded[rung_name] = detail

    def verified_rungs(self) -> frozenset[str]:
        return frozenset(self.recorded)

    def completions(self) -> dict:
        return {}


def _clean_counts() -> dict:
    """A rekey envelope reporting a perfectly clean cutover — the engine's
    OWN self-report, which is exactly what must NOT stand in for independent
    verification when the read-only diagnostic could not run."""
    return {
        "alias_rows": 0, "rehashed": 0, "collapsed_duplicates": 0,
        "reference_only_resolved": 0, "orphans_dropped": 0,
        "orphans_synthesized": 0, "residual_mismatched": 0,
        "dangling_manifest": 0,
    }


def _rung(probe: ConformanceProbe, **over) -> ChashRekeyRung:
    kwargs = dict(
        rekey_fn=lambda _policy: _clean_counts(),
        validate_fn=lambda _checks=None: True,
        pointer_debt_fn=lambda: {},
        reprovision_fn=lambda: None,
        freeze_fn=lambda: (lambda: None),
        detect_probe_fn=lambda: probe,
        validated_probe_fn=lambda: False,
        applicable_fn=lambda: True,
    )
    kwargs.update(over)
    return ChashRekeyRung(**kwargs)  # type: ignore[arg-type]


# ── the reproduction: the ladder walked end-to-end ───────────────────────────


@pytest.mark.parametrize(
    ("probe", "why"),
    [
        (
            ConformanceProbe.failed(
                'relation "nexus.diag_chash_conformance" does not exist'
            ),
            "the diagnostic ERRORED — the reported v0.1.69 symptom",
        ),
        (
            ConformanceProbe.unavailable(
                "no nexus_diag credentials (pre-P2.1 install)"
            ),
            "the diagnostic could not even be attempted",
        ),
    ],
)
def test_ladder_refuses_to_record_a_rung_it_could_not_verify(probe, why):
    """THE REPRODUCTION. Drive the real runner over the real rung with the
    conformance relation absent: pre-fix the walk ends RECORDED (the CLI
    prints "converged and verified"); post-fix it ends VERIFY_FAILED with
    nothing recorded and the ladder position unmoved."""
    ledger = _Ledger()
    runner = LadderRunner(
        LadderRegistry((_rung(probe),)),  # type: ignore[arg-type]
        ledger,  # type: ignore[arg-type]
        reporter=_Report(),
        package_version_fn=lambda: "test",
    )

    report = runner.run()

    assert [r.outcome for r in report.runs] == [RungOutcome.VERIFY_FAILED], (
        f"{why}: the ladder declared the rung verified off a diagnostic that "
        "never produced a measurement — the vacuous-verification class"
    )
    assert ledger.recorded == {}, "an unverifiable rung must leave NO completion fact"
    assert report.position == 0, "ladder position advanced past unverified work (RDR-142)"
    assert report.hard_failed, "an unverifiable rung must fail the upgrade LOUDLY"


def test_the_failure_detail_names_the_diagnostic_reason():
    """RDR-182: 'an explicit unavailable note, never a silent all-clean'. The
    operator must be told WHICH diagnostic could not run — a bare
    'verify failed' sends them hunting for poison rows that were never
    counted."""
    ledger = _Ledger()
    report = LadderRunner(
        LadderRegistry((  # type: ignore[arg-type]
            _rung(ConformanceProbe.failed('relation "nexus.diag_chash_conformance" does not exist')),
        )),
        ledger,  # type: ignore[arg-type]
        reporter=_Report(),
        package_version_fn=lambda: "test",
    ).run()

    detail = report.runs[0].detail
    assert "diag_chash_conformance" in detail, (
        f"the reason never reached the operator-facing detail: {detail!r}"
    )
    assert "not" in detail.lower() and "clean" in detail.lower(), (
        "the detail must state that this is NOT a clean-store verdict, so an "
        f"operator cannot read the stop as cosmetic: {detail!r}"
    )


# ── unit-grain: verify() itself ──────────────────────────────────────────────


class TestVerifyIsNonVacuous:
    def test_measured_zero_is_the_only_true(self):
        assert _rung(ConformanceProbe.measured(0)).verify() is True
        assert _rung(ConformanceProbe.measured(3)).verify() is False

    def test_errored_diagnostic_is_not_verified_even_after_a_clean_converge(self):
        """The pre-fix path: converge stashes a zero-residual envelope, the
        probe returns no measurement, and verify() reads the engine's own
        echo as proof. The engine's in-transaction scan is evidence the WORK
        ran; it is not an independent measurement of the resulting store."""
        rung = _rung(ConformanceProbe.failed("psql exit 1: permission denied"))
        assert rung.converge(_Report()).outcome is ConvergeOutcome.COMPLETED
        assert rung.verify() is False

    def test_unavailable_diagnostic_is_not_verified_even_after_a_clean_converge(self):
        rung = _rung(ConformanceProbe.unavailable("no nexus_diag credentials"))
        assert rung.converge(_Report()).outcome is ConvergeOutcome.COMPLETED
        assert rung.verify() is False

    def test_verify_detail_is_empty_when_verification_actually_succeeded(self):
        rung = _rung(ConformanceProbe.measured(0))
        assert rung.verify() is True
        assert rung.verify_detail() == ""


# ── unit-grain: the probe can no longer manufacture a zero ───────────────────


class TestProbeCannotManufactureAZero:
    def test_measured_requires_a_real_count(self):
        assert ConformanceProbe.measured(0).state is ProbeState.MEASURED
        assert ConformanceProbe.failed("x").conformant is False
        assert ConformanceProbe.unavailable("x").conformant is False
        assert ConformanceProbe.measured(0).conformant is True
        assert ConformanceProbe.measured(1).conformant is False

    def test_a_probe_that_scanned_nothing_is_FAILED_not_zero(self):
        """``sum(int(c) for c in [])`` is 0 — the arithmetic identity that
        turns "I scanned no tables" into "no non-conformant rows". RDR-187
        already narrowed POISON_CHASH_TABLES from five entries to four; a
        further narrowing to zero must fail the probe, not pass it."""
        assert probe_from_diagnostic_output((), ()).state is ProbeState.FAILED
        assert probe_from_diagnostic_output(("SELECT 1",), ()).state is ProbeState.FAILED

    def test_a_null_aggregate_is_FAILED_not_zero(self):
        """psql renders a NULL ``sum()`` (a stale view generation with no row
        for the filtered table) as an empty line. Reading that as 0 is the
        same vacuity one layer down."""
        probe = probe_from_diagnostic_output(("SELECT sum(x) FROM v",), ("",))
        assert probe.state is ProbeState.FAILED

    def test_a_real_measurement_sums(self):
        probe = probe_from_diagnostic_output(("a", "b"), ("2", "3"))
        assert probe.state is ProbeState.MEASURED
        assert probe.count == 5


class TestProductionProbeWiring:
    """The real ``default_chash_rekey_rung()`` probe, seams monkeypatched at
    the diag choke point. These pin the LEG SELECTION — which is what decides
    whether the reported v0.1.69 symptom is a benign fallback or a genuine
    unmeasurable store."""

    @staticmethod
    def _probe(monkeypatch, run_impl):
        import nexus.db.diag_connection as diag

        from nexus.db.diag_connection import DiagCredentials

        monkeypatch.setattr(
            diag, "resolve_diag_credentials",
            lambda *_a, **_k: DiagCredentials(port=1, user="d", password="p"),
        )
        monkeypatch.setattr(diag, "run_diagnostic_sql", run_impl)
        from nexus.upgrade_ladder.rungs.chash_rekey import default_chash_rekey_rung

        return default_chash_rekey_rung()._detect_probe_fn()  # noqa: SLF001 — the seam under test

    def test_absent_view_falls_back_to_legacy_and_MEASURES(self, monkeypatch):
        """The reported v0.1.69 shape. The view leg raises exactly as psql
        does for a missing relation; the legacy direct-table leg still counts
        (grants-nexus-diag-1 is runAlways and restores legacy-era table SELECT
        whenever the view is absent). Result: a REAL measurement, so this
        instance was benign — but it must be MEASURED, not assumed."""
        def run(statements, _creds, **_kw):
            if any("diag_chash_conformance" in s for s in statements):
                raise RuntimeError(
                    'diagnostic statement failed (psql exit 1): ERROR: relation '
                    '"nexus.diag_chash_conformance" does not exist'
                )
            return ["0"] * len(statements)

        probe = self._probe(monkeypatch, run)
        assert probe.state is ProbeState.MEASURED
        assert probe.count == 0

    def test_stale_view_generation_NULL_also_falls_back(self, monkeypatch):
        """A deployed view generation with no row for a filtered table_name
        yields ``sum() = NULL``, which psql prints as a blank line. Reading
        that as 0 is the vacuity one layer down; failing outright would strand
        a store the legacy leg can still count. Fall back, then measure."""
        def run(statements, _creds, **_kw):
            if any("diag_chash_conformance" in s for s in statements):
                return [""] * len(statements)
            return ["0"] * len(statements)

        probe = self._probe(monkeypatch, run)
        assert probe.state is ProbeState.MEASURED

    def test_BOTH_legs_dead_is_FAILED_and_names_both(self, monkeypatch):
        """The genuinely unmeasurable store — the case that used to collapse
        to None and verify True off the rekey envelope."""
        def run(_statements, _creds, **_kw):
            raise RuntimeError("permission denied")

        probe = self._probe(monkeypatch, run)
        assert probe.state is ProbeState.FAILED
        assert "view path" in probe.reason and "legacy" in probe.reason

    def test_a_lint_refusal_does_NOT_retry_the_legacy_leg(self, monkeypatch):
        """A DiagnosticSqlViolation is a PRODUCT defect, not engine-generation
        skew. health.py re-raises it rather than falling back; the rung's old
        bare ``except Exception`` retried the legacy statements and mislabeled
        it as a stale-view fallback."""
        from nexus.remediation.sql_lint import DiagnosticSqlViolation

        seen: list[str] = []

        def run(statements, _creds, **_kw):
            seen.append(statements[0])
            raise DiagnosticSqlViolation("not read-only")

        probe = self._probe(monkeypatch, run)
        assert probe.state is ProbeState.FAILED
        assert "lint refused" in probe.reason
        assert all("diag_chash_conformance" in s for s in seen), (
            "the lint refusal was retried against the legacy direct-table leg"
        )

    def test_absent_credentials_are_UNAVAILABLE_not_FAILED(self, monkeypatch):
        """The two non-measurement arms stay distinguishable: 'this box has no
        diagnostic role' is a different operator action from 'the diagnostic
        blew up'. Both refuse verification; only the wording differs."""
        import nexus.db.diag_connection as diag

        monkeypatch.setattr(diag, "resolve_diag_credentials", lambda *_a, **_k: None)
        from nexus.upgrade_ladder.rungs.chash_rekey import default_chash_rekey_rung

        probe = default_chash_rekey_rung()._detect_probe_fn()  # noqa: SLF001 — the seam under test
        assert probe.state is ProbeState.UNAVAILABLE
        assert "nx init --service" in probe.reason


# ── sibling: the pointer-debt amnesty must not ride an absent baseline ───────


def test_amnesty_is_refused_when_the_pre_rekey_debt_baseline_is_unknOWABLE():
    """Same class, same rung (nexus-hdumg sibling sweep). The pointer-CHECK
    amnesty narrows VALIDATE to the three CONTENT tables on the strength of
    "this debt PRE-EXISTED". When the BEFORE probe could not run, that claim
    has no evidence — and the growth check (``debt_before is not None``) is
    skipped, so a rekey that CREATED the orphans is waved through as
    pre-existing. Absent evidence must not grant the amnesty."""
    calls: dict = {}
    seen = iter([None, {"nexus.catalog_document_chunks": 7}])
    rung = _rung(
        ConformanceProbe.measured(0),
        pointer_debt_fn=lambda: next(seen),
        validate_fn=lambda checks=None: calls.__setitem__("checks", checks) or True,
    )

    rung.converge(_Report())

    assert calls["checks"] is not None
    validated = {t for t, _n in calls["checks"]}
    assert "nexus.catalog_document_chunks" in validated, (
        "the amnesty was granted on an unmeasured baseline — the rung cannot "
        "know whether those 7 orphans pre-existed or were created by this run"
    )
