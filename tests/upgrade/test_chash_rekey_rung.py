# SPDX-License-Identifier: AGPL-3.0-or-later
"""RDR-180 Item6 (nexus-jxizy.6): the chash-rekey rung.

Seam-injected unit coverage: freeze/restore ordering, the idempotent rekey
drive, residual refusal (no dual-width window), VALIDATE outcomes, the
managed-mode honesty path, and the constraint-name pin against the
rdr180-001 changeset XML (a hand-typed drift between the rung's VALIDATE
set and the engine's constraint names would silently validate nothing).
"""
from __future__ import annotations

from pathlib import Path

import pytest

from nexus.upgrade_ladder.protocol import ConvergeOutcome
from nexus.upgrade_ladder.rungs.chash_rekey import (
    OCTET_CHECKS,
    ChashRekeyRung,
    validate_statements,
)

_REPO = Path(__file__).resolve().parents[2]


class _Report:
    """Conforms to ProgressReporter — ``emit(event, **fields)``, NOTHING
    else. The first fake here grew a ``step()`` method the real protocol
    never had, and the rung drifted onto it: every production ``nx
    upgrade`` then crashed with 'StructlogReporter has no attribute step'
    (caught live by the nexus-p78a0 rehearsal, run 2). Keep this fake
    interface-minimal so drift fails HERE first."""

    def __init__(self) -> None:
        self.events: list[str] = []

    def emit(self, event: str, **fields: object) -> None:
        self.events.append(event)


def _counts(residual: int = 0, **extra) -> dict:
    base = {
        "alias_rows": 3, "rehashed": 5, "collapsed_duplicates": 1,
        "reference_only_resolved": 0, "orphans_dropped": 0,
        "orphans_synthesized": 0, "residual_mismatched": residual,
        "dangling_manifest": 0,
    }
    base.update(extra)
    return base


def _rung(**over) -> tuple[ChashRekeyRung, dict]:
    calls: dict = {"restored": False, "frozen": False}

    def freeze():
        calls["frozen"] = True

        def restore():
            calls["restored"] = True
        return restore

    kwargs = dict(
        rekey_fn=lambda policy: calls.setdefault("policy", policy) and _counts() or _counts(),
        validate_fn=lambda: True,
        reprovision_fn=lambda: calls.setdefault("reprovisioned", True),
        freeze_fn=freeze,
        detect_probe_fn=lambda: None,
    )
    kwargs.update(over)
    return ChashRekeyRung(**kwargs), calls


class TestDetect:
    def test_zero_probe_still_applies_pending_first_run(self):
        rung, _ = _rung(detect_probe_fn=lambda: 0)
        st = rung.detect()
        assert st.applicable and not st.converged

    def test_unknown_probe_applies(self):
        rung, _ = _rung(detect_probe_fn=lambda: None)
        st = rung.detect()
        assert st.applicable and "unknowable" in st.pending_detail

    def test_nonzero_probe_reports_count(self):
        rung, _ = _rung(detect_probe_fn=lambda: 7)
        assert "7" in rung.detect().pending_detail

    def test_validated_checks_report_converged(self):
        """nexus-p78a0 rehearsal catch: doctor / --dry-run / the transition
        callout are RAW detect() sweeps (never the completion ledger), so
        detect must see convergence from the DATA or a rekeyed store reads
        as pending forever. The convalidated octet CHECKs are that marker —
        only the rung's own VALIDATE (or the managed operator's) sets them."""
        rung, _ = _rung(detect_probe_fn=lambda: 0, validated_probe_fn=lambda: True)
        st = rung.detect()
        assert st.applicable and st.converged

    def test_unvalidated_checks_stay_pending(self):
        for v in (False, None):
            rung, _ = _rung(detect_probe_fn=lambda: 0, validated_probe_fn=lambda v=v: v)
            st = rung.detect()
            assert st.applicable and not st.converged


class TestConverge:
    def test_happy_path_freezes_rekeys_validates_restores(self):
        rung, calls = _rung()
        report = _Report()
        result = rung.converge(report)
        assert result.outcome is ConvergeOutcome.COMPLETED
        assert calls["frozen"] and calls["restored"] and calls["reprovisioned"]
        assert calls["policy"] == "drop"
        assert "rekeyed=5" in result.detail
        assert "validated=yes" in result.detail

    def test_converge_conforms_to_the_real_runner_reporter(self):
        """The interface pin (nexus-p78a0 run-2 catch): converge must run
        against the runner's ACTUAL default reporter, not just this file's
        fake — structural drift between the two is a production crash on
        every `nx upgrade`."""
        from nexus.upgrade_ladder.runner import StructlogReporter

        rung, calls = _rung()
        result = rung.converge(StructlogReporter())
        assert result.outcome is ConvergeOutcome.COMPLETED
        assert calls["frozen"] and calls["restored"]

    def test_orphan_policy_flag_flows_through(self):
        rung, calls = _rung()
        rung_syn, calls_syn = _rung(orphan_policy="synthesize")
        rung_syn.converge(_Report())
        assert calls_syn["policy"] == "synthesize"

    def test_invalid_orphan_policy_rejected_at_construction(self):
        with pytest.raises(ValueError, match="orphan_policy"):
            _rung(orphan_policy="keep")

    def test_nonzero_residual_raises_and_still_restores(self):
        rung, calls = _rung(rekey_fn=lambda p: _counts(residual=4))
        with pytest.raises(RuntimeError, match="4 mismatched"):
            rung.converge(_Report())
        assert calls["restored"], "the freeze must be released on failure"

    def test_validate_failure_raises_after_clean_rekey(self):
        rung, calls = _rung(validate_fn=lambda: False)
        with pytest.raises(RuntimeError, match="VALIDATE failed"):
            rung.converge(_Report())
        assert calls["restored"]

    def test_managed_mode_validate_none_is_honest_not_fatal(self):
        rung, _ = _rung(validate_fn=lambda: None)
        result = rung.converge(_Report())
        assert result.outcome is ConvergeOutcome.COMPLETED
        assert "operator-step" in result.detail

    def test_rekey_exception_propagates_and_restores(self):
        def boom(policy):
            raise RuntimeError("engine 409: collision")
        rung, calls = _rung(rekey_fn=boom)
        with pytest.raises(RuntimeError, match="collision"):
            rung.converge(_Report())
        assert calls["restored"]


class TestVerify:
    def test_probe_zero_verifies(self):
        rung, _ = _rung(detect_probe_fn=lambda: 0)
        assert rung.verify() is True

    def test_probe_nonzero_fails(self):
        rung, _ = _rung(detect_probe_fn=lambda: 2)
        assert rung.verify() is False

    def test_no_probe_uses_recorded_counts(self):
        rung, _ = _rung(detect_probe_fn=lambda: None)
        assert rung.verify() is False  # nothing recorded yet
        rung.converge(_Report())
        assert rung.verify() is True


class TestValidateStatements:
    def test_statements_shape(self):
        stmts = validate_statements()
        assert len(stmts) == 5
        for s in stmts:
            assert s.startswith("ALTER TABLE nexus.")
            assert "VALIDATE CONSTRAINT" in s

    def test_constraint_names_pinned_to_the_changeset(self):
        """The rung's VALIDATE set and the rdr180-001 changeset must name the
        SAME constraints — drift here validates nothing, silently."""
        xml = (_REPO / "service/src/main/resources/db/changelog/rdr180-001-bytea-chash.xml").read_text()
        for table, name in OCTET_CHECKS:
            assert name in xml, f"constraint {name} not found in rdr180-001"
            assert f"ALTER TABLE {table}" in xml


class TestSentinelFreeze:
    def test_snapshot_and_restore_prior_state(self, tmp_path, monkeypatch):
        monkeypatch.setenv("NEXUS_CONFIG_DIR", str(tmp_path))
        from nexus.migration import state as mig
        from nexus.upgrade_ladder.rungs.chash_rekey import _sentinel_freeze

        prior = mig.MigrationState(
            phase=mig.MIGRATED, started_at="2026-07-01T00:00:00+00:00",
            collections_total=3, collections_done=3,
        )
        mig.write_state(prior)
        restore = _sentinel_freeze()
        assert mig.current_phase() == mig.MIGRATING
        restore()
        assert mig.current_phase() == mig.MIGRATED
        assert mig.read_state() == prior

    def test_no_prior_state_clears_on_restore(self, tmp_path, monkeypatch):
        monkeypatch.setenv("NEXUS_CONFIG_DIR", str(tmp_path))
        from nexus.migration import state as mig
        from nexus.upgrade_ladder.rungs.chash_rekey import _sentinel_freeze

        assert mig.read_state() is None
        restore = _sentinel_freeze()
        assert mig.is_migrating()
        restore()
        assert mig.read_state() is None


class TestRegistry:
    def test_chash_rekey_is_last_and_ordered(self):
        from nexus.upgrade_ladder.registry import (
            RUNG_CHASH_REKEY,
            RUNG_ORDER,
            default_registry,
        )

        assert RUNG_ORDER[-1] == RUNG_CHASH_REKEY
        assert [r.name for r in default_registry()] == list(RUNG_ORDER)


def test_unrefreshed_alias_stats_are_surfaced_not_swallowed():
    """rdr180-17 / F2: the engine reports whether its in-transaction ANALYZE of
    chash_alias actually took effect — Postgres SILENTLY skips it for a role
    without MAINTAIN (PG17+). A False must reach the operator: the rekey is
    still correct, but a multi-tenant store just planned it blind, which
    measured 101 minutes versus 461 seconds in production."""
    rung, _ = _rung(
        rekey_fn=lambda policy: _counts(alias_stats_refreshed=False),
    )
    result = rung.converge(_Report())
    assert "alias planner statistics were NOT refreshed" in (result.detail or ""), (
        "an unrefreshed-stats engine response must be surfaced in the converge "
        f"detail, not swallowed; got: {result.detail!r}"
    )


def test_refreshed_alias_stats_stay_quiet():
    """The happy path must not add noise — the NOTE appears only on False."""
    rung, _ = _rung(rekey_fn=lambda policy: _counts(alias_stats_refreshed=True))
    result = rung.converge(_Report())
    assert "NOT refreshed" not in (result.detail or "")
