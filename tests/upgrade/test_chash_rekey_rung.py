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

from nexus.db.t2.rekey_client import (
    RekeyJobFailedError,
    RekeyJobLostError,
    RekeyJobTimeoutError,
)
from nexus.db.chash_tables import ConformanceProbe
from nexus.upgrade_ladder.protocol import ConvergeOutcome
from nexus.upgrade_ladder.rungs.chash_rekey import (
    CONTENT_OCTET_CHECKS,
    OCTET_CHECKS,
    POINTER_OCTET_CHECKS,
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
        validate_fn=lambda checks=None: calls.__setitem__(
            'validated_statements',
            list(validate_statements(checks)) if checks is not None else [],
        ) or True,
        reprovision_fn=lambda: calls.setdefault("reprovisioned", True),
        freeze_fn=freeze,
        detect_probe_fn=lambda: ConformanceProbe.unavailable('no diag path in this fixture'),
    )
    kwargs.update(over)
    return ChashRekeyRung(**kwargs), calls


class TestDetect:
    def test_zero_probe_still_applies_pending_first_run(self):
        rung, _ = _rung(detect_probe_fn=lambda: ConformanceProbe.measured(0))
        st = rung.detect()
        assert st.applicable and not st.converged

    def test_unknown_probe_applies(self):
        """nexus-hdumg: an unmeasurable probe still APPLIES and stays pending,
        and the doctor-facing detail now names WHICH state and why (it used
        to say only 'unknowable here (no diag path)' regardless of whether
        there was no diag path or the diagnostic had blown up)."""
        rung, _ = _rung(detect_probe_fn=lambda: ConformanceProbe.unavailable('no diag path'))
        st = rung.detect()
        assert st.applicable and not st.converged
        assert "unavailable" in st.pending_detail and "no diag path" in st.pending_detail

    def test_nonzero_probe_reports_count(self):
        rung, _ = _rung(detect_probe_fn=lambda: ConformanceProbe.measured(7))
        assert "7" in rung.detect().pending_detail

    def test_validated_checks_report_converged(self):
        """nexus-p78a0 rehearsal catch: doctor / --dry-run / the transition
        callout are RAW detect() sweeps (never the completion ledger), so
        detect must see convergence from the DATA or a rekeyed store reads
        as pending forever. The convalidated octet CHECKs are that marker —
        only the rung's own VALIDATE (or the managed operator's) sets them."""
        rung, _ = _rung(detect_probe_fn=lambda: ConformanceProbe.measured(0), validated_probe_fn=lambda: True)
        st = rung.detect()
        assert st.applicable and st.converged

    def test_unvalidated_checks_stay_pending(self):
        for v in (False, None):
            rung, _ = _rung(detect_probe_fn=lambda: ConformanceProbe.measured(0), validated_probe_fn=lambda v=v: v)
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
        rung, calls = _rung(validate_fn=lambda checks=None: False)
        with pytest.raises(RuntimeError, match="VALIDATE failed"):
            rung.converge(_Report())
        assert calls["restored"]

    def test_managed_mode_validate_none_is_honest_not_fatal(self):
        rung, _ = _rung(validate_fn=lambda checks=None: None)
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


class TestRestartRetry:
    """nexus-sfgqi: the ONE could-not-tell with a correct automatic answer.

    HttpRemapStore.rekey distinguishes known-failed from two could-not-tell
    outcomes, but the ladder runner collapses every exception from converge()
    into RungOutcome.FAILED. For an engine restart specifically that is the
    wrong answer available: the rekey is idempotent, so re-running resolves
    it — all-zero counts over an already-rekeyed store, real work over a
    rolled-back one.
    """

    def test_lost_to_restart_retries_once_and_converges(self):
        attempts = []

        def flaky(policy):
            attempts.append(policy)
            if len(attempts) == 1:
                raise RekeyJobLostError("job abc was lost to an engine restart")
            return _counts()

        rung, calls = _rung(rekey_fn=flaky)
        result = rung.converge(_Report())
        assert result.outcome is ConvergeOutcome.COMPLETED
        assert len(attempts) == 2, "the idempotent rekey must be re-run exactly once"
        assert calls["restored"]

    def test_the_retry_is_bounded_at_one(self):
        """A store that keeps losing the job must fail, not spin."""
        attempts = []

        def always_lost(policy):
            attempts.append(policy)
            raise RekeyJobLostError("lost again")

        rung, calls = _rung(rekey_fn=always_lost)
        with pytest.raises(RekeyJobLostError):
            rung.converge(_Report())
        assert len(attempts) == 2, "one retry, then give up"
        assert calls["restored"]

    def test_timeout_is_NOT_retried(self):
        """A timeout means the transaction may still be running.

        Starting a second rekey against a live one would queue behind the
        per-tenant advisory lock rather than resolve anything, so this case
        propagates untouched — could-not-tell, for a human to settle.
        """
        attempts = []

        def timed_out(policy):
            attempts.append(policy)
            raise RekeyJobTimeoutError("still running after 3600s")

        rung, calls = _rung(rekey_fn=timed_out)
        with pytest.raises(RekeyJobTimeoutError):
            rung.converge(_Report())
        assert len(attempts) == 1, "a possibly-live transaction must not be re-driven"
        assert calls["restored"]

    def test_known_failure_is_NOT_retried(self):
        attempts = []

        def failed(policy):
            attempts.append(policy)
            raise RekeyJobFailedError("legacy id maps to two digests")

        rung, calls = _rung(rekey_fn=failed)
        with pytest.raises(RekeyJobFailedError):
            rung.converge(_Report())
        assert len(attempts) == 1, "a known failure is not made better by repetition"
        assert calls["restored"]


class TestVerify:
    def test_probe_zero_verifies(self):
        rung, _ = _rung(detect_probe_fn=lambda: ConformanceProbe.measured(0))
        assert rung.verify() is True

    def test_probe_nonzero_fails(self):
        rung, _ = _rung(detect_probe_fn=lambda: ConformanceProbe.measured(2))
        assert rung.verify() is False

    def test_an_unmeasurable_probe_never_verifies(self):
        """REPLACES ``test_no_probe_uses_recorded_counts``, which pinned the
        nexus-hdumg defect as intended behaviour: it asserted that after a
        clean converge, a rung with NO conformance measurement verifies True
        off the rekey endpoint's own envelope. That envelope proves the WORK
        ran; it is not an independent measurement of the store, and treating
        it as one is what let 'rung converged and verified' print seconds
        after ``event='diag_sql_failed'`` in the v0.1.69 rehearsal.

        Full contract coverage lives in
        ``test_chash_rekey_verification_non_vacuous.py``."""
        rung, _ = _rung(detect_probe_fn=lambda: ConformanceProbe.unavailable('no diag path'))
        assert rung.verify() is False  # nothing measured, nothing converged
        rung.converge(_Report())
        assert rung.verify() is False, (
            "the engine's own zero-residual echo stood in for a conformance "
            "measurement that never ran"
        )
        assert "not a clean-store verdict" in rung.verify_detail().lower()


class TestValidateStatements:
    def test_statements_shape(self):
        stmts = validate_statements()
        # 5 -> 4 (RDR-187 dropped chash_index) -> 2 (RDR-191 vectors-004
        # collapses chunks_384/768/1024 into the one unified nexus.chunks
        # check; catalog_document_chunks is untouched). See OCTET_CHECKS's
        # own derivation comment for the full chain — this assertion is
        # DERIVED from len(OCTET_CHECKS), never a bare literal, so it cannot
        # silently drift from the constant it is meant to pin.
        assert len(stmts) == len(OCTET_CHECKS) == 2
        for s in stmts:
            assert s.startswith("ALTER TABLE nexus.")
            assert "VALIDATE CONSTRAINT" in s

    def test_constraint_names_pinned_to_the_changeset(self):
        """The rung's VALIDATE set and the changeset that DEFINES each
        constraint must name the SAME table/constraint pair — drift here
        validates nothing, silently. RDR-191 split this pin across TWO
        changesets: the unified content check now lives in
        vectors-004-unify-chunks.xml, while the untouched pointer/manifest
        check remains in rdr180-001-bytea-chash.xml."""
        rdr180_xml = (
            _REPO / "service/src/main/resources/db/changelog/rdr180-001-bytea-chash.xml"
        ).read_text()
        vectors004_xml = (
            _REPO / "service/src/main/resources/db/changelog/vectors-004-unify-chunks.xml"
        ).read_text()
        sources = {
            ("nexus.chunks", "chunks_chash_octet_check"): vectors004_xml,
            (
                "nexus.catalog_document_chunks",
                "catalog_document_chunks_chash_octet_check",
            ): rdr180_xml,
        }
        assert set(OCTET_CHECKS) == set(sources), (
            "OCTET_CHECKS drifted from the pin table below — update both "
            "together"
        )
        for table, name in OCTET_CHECKS:
            xml = sources[(table, name)]
            assert name in xml, f"constraint {name} not found in its owning changeset"
            assert f"ALTER TABLE {table}" in xml


class TestOctetCheckPartition:
    """nexus-o8dil.15 CRITICAL fix: CONTENT_OCTET_CHECKS / POINTER_OCTET_CHECKS
    are explicit membership, never a positional slice of OCTET_CHECKS. At 4
    entries `[:3]`/`[3:]` happened to align with CONTENT/POINTER; at 2 they
    would have silently gone (BOTH, EMPTY) with neither slice raising. These
    tests fail if the arms are swapped, if either is empty, or if they
    overlap or drop an entry relative to OCTET_CHECKS."""

    def test_content_is_the_unified_chunks_table_only(self):
        assert CONTENT_OCTET_CHECKS == (("nexus.chunks", "chunks_chash_octet_check"),)

    def test_pointer_is_the_manifest_table_only(self):
        assert POINTER_OCTET_CHECKS == (
            ("nexus.catalog_document_chunks", "catalog_document_chunks_chash_octet_check"),
        )

    def test_neither_arm_is_empty(self):
        assert CONTENT_OCTET_CHECKS, "an empty CONTENT arm silently VALIDATEs nothing"
        assert POINTER_OCTET_CHECKS, "an empty POINTER arm silently VALIDATEs nothing"

    def test_arms_do_not_overlap(self):
        assert not (set(CONTENT_OCTET_CHECKS) & set(POINTER_OCTET_CHECKS))

    def test_arms_partition_octet_checks_exactly(self):
        """Every OCTET_CHECKS entry lands in EXACTLY one arm — this is what
        a swapped or a dropped-entry regression would break."""
        assert set(CONTENT_OCTET_CHECKS) | set(POINTER_OCTET_CHECKS) == set(OCTET_CHECKS)
        assert len(CONTENT_OCTET_CHECKS) + len(POINTER_OCTET_CHECKS) == len(OCTET_CHECKS)


class TestExistenceGateIntegration:
    """F14a (nexus-o8dil.1) at the RUNG level: converge() must complete
    without raising when a VALIDATE target relation is absent, driving the
    REAL ``run_admin_sql`` + ``validate_statements`` wiring (not a
    rung-level fake) — and the store must read as NOT converged afterward
    (fewer than ``len(OCTET_CHECKS)`` constraints ever got VALIDATEd)."""

    def test_absent_relation_does_not_crash_converge_and_stays_unconverged(
        self, tmp_path, monkeypatch,
    ):
        from subprocess import CompletedProcess

        from nexus.db.admin_sql import AdminCredentials, run_admin_sql

        monkeypatch.setattr(
            "nexus.db.admin_sql.resolve_admin_credentials",
            lambda creds_path=None: AdminCredentials(port=1, user="a", password="p"),
        )
        (tmp_path / "bundle" / "bin").mkdir(parents=True)
        (tmp_path / "bundle" / "lib").mkdir(parents=True)
        psql = tmp_path / "bundle" / "bin" / "psql"
        psql.write_text("")

        # One of the four real octet-check tables is gone — the chash_index
        # shape, generalized: ANY tracked relation can vanish out from under
        # this rung, not just the one already hard-removed from OCTET_CHECKS.
        absent_table = "nexus.catalog_document_chunks"
        validated_statements: list[str] = []

        def runner(argv, env):
            stmt = argv[-1]
            if "to_regclass" in stmt:
                table = stmt.split("'")[1]
                stdout = "" if table == absent_table else "t"
                return CompletedProcess(argv, 0, stdout=stdout, stderr="")
            validated_statements.append(stmt)
            return CompletedProcess(argv, 0, stdout="", stderr="")

        def _validate(checks=None):
            return run_admin_sql(
                validate_statements(checks if checks is not None else OCTET_CHECKS),
                psql_bin=psql, psql_runner=runner,
            )

        rung, _ = _rung(validate_fn=_validate)
        result = rung.converge(_Report())

        assert result.outcome is ConvergeOutcome.COMPLETED, (
            "an absent relation must not raise — it must be skipped"
        )
        assert not any(absent_table in s for s in validated_statements), (
            f"the absent relation must never be VALIDATEd: {validated_statements!r}"
        )
        assert len(validated_statements) == len(OCTET_CHECKS) - 1

        # The overall store must NOT read as converged: only 3 of the 4
        # constraints were ever VALIDATEd, so a validated_probe-style count
        # (which requires len(OCTET_CHECKS)) reports pending, never crashed.
        rung_pending, _ = _rung(
            detect_probe_fn=lambda: ConformanceProbe.measured(0),
            validated_probe_fn=lambda: len(validated_statements) == len(OCTET_CHECKS),
        )
        st = rung_pending.detect()
        assert st.applicable and not st.converged, (
            "a store missing one VALIDATEd constraint must detect as pending, "
            "not converged"
        )

    def test_present_relations_unchanged_full_convergence(self, tmp_path, monkeypatch):
        """Regression: nothing missing means every statement still runs and
        the store still converges — the existence gate is a no-op on a
        clean store."""
        from subprocess import CompletedProcess

        from nexus.db.admin_sql import AdminCredentials, run_admin_sql

        monkeypatch.setattr(
            "nexus.db.admin_sql.resolve_admin_credentials",
            lambda creds_path=None: AdminCredentials(port=1, user="a", password="p"),
        )
        (tmp_path / "bundle" / "bin").mkdir(parents=True)
        (tmp_path / "bundle" / "lib").mkdir(parents=True)
        psql = tmp_path / "bundle" / "bin" / "psql"
        psql.write_text("")

        validated_statements: list[str] = []

        def runner(argv, env):
            stmt = argv[-1]
            if "to_regclass" in stmt:
                return CompletedProcess(argv, 0, stdout="t", stderr="")
            validated_statements.append(stmt)
            return CompletedProcess(argv, 0, stdout="", stderr="")

        def _validate(checks=None):
            return run_admin_sql(
                validate_statements(checks if checks is not None else OCTET_CHECKS),
                psql_bin=psql, psql_runner=runner,
            )

        rung, _ = _rung(validate_fn=_validate)
        result = rung.converge(_Report())
        assert result.outcome is ConvergeOutcome.COMPLETED
        assert len(validated_statements) == len(OCTET_CHECKS)


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


class TestPointerDebtValidatePolicy:
    """nexus-noa8d: a lived-in store carrying PRE-EXISTING orphan pointers
    cannot VALIDATE the two pointer-table octet CHECKs — the constraint is
    table-grain, so 292,656 dangling rows (production, 2026-07-20) make it
    arithmetically impossible. The rung must still VALIDATE the three CONTENT
    tables (the RDR-180 contract) and report the debt, instead of failing the
    whole upgrade.

    The amnesty is a CEILING, never a table exemption (conexus correction,
    Hal-caught): 'these two tables do not gate' would wave a FUTURE rekey's
    brand-new orphans through as `observed`. The ceiling here is measured
    within the run — pointer debt before the rekey versus after — so growth
    caused by this rekey FAILS loud while pre-existing debt is grandfathered.
    """

    def test_pre_existing_debt_validates_content_and_reports_pointers(self):
        # Post-RDR-187 the debt probe iterates only the manifest (the router
        # died with its 292,230); the amnesty semantics are unchanged.
        rung, calls = _rung(
            pointer_debt_fn=lambda: {"nexus.catalog_document_chunks": 426},
        )
        result = rung.converge(_Report())
        assert result.outcome is ConvergeOutcome.COMPLETED, (
            "pre-existing pointer debt must NOT fail the upgrade"
        )
        detail = result.detail or ""
        assert "426" in detail, (
            f"the skipped debt must be REPORTED with counts, not silent: {detail!r}"
        )
        stmts = calls.get("validated_statements") or []
        joined = " ".join(stmts)
        # RDR-191: the three per-dim CONTENT checks collapsed into the one
        # unified nexus.chunks check.
        assert "ALTER TABLE nexus.chunks VALIDATE CONSTRAINT chunks_chash_octet_check" in joined, (
            f"the unified CONTENT octet CHECK must still be VALIDATEd: {stmts!r}"
        )
        assert "chash_index" not in joined and "catalog_document_chunks" not in joined, (
            f"pointer-table CHECKs must be SKIPPED while debt exists: {stmts!r}"
        )

    def test_zero_debt_validates_all_checks(self):
        rung, calls = _rung(pointer_debt_fn=lambda: {})
        rung.converge(_Report())
        joined = " ".join(calls.get("validated_statements") or [])
        for name in ("chunks_chash_octet_check", "catalog_document_chunks"):
            assert name in joined, f"a clean store must VALIDATE every check; missing {name}"
        # RDR-187 (nexus-piwya.9): the router's VALIDATE must NEVER be issued
        # again — against a dropped table it is a guaranteed RuntimeError on
        # every upgrade (the .9 critique Critical 1).
        assert "chash_index" not in joined

    def test_debt_GROWN_by_this_rekey_fails_loud(self):
        """THE CEILING. Debt measured before the rekey is grandfathered; any
        INCREASE is damage this run caused and must never be waved through."""
        seq = iter([{"nexus.catalog_document_chunks": 100}, {"nexus.catalog_document_chunks": 150}])
        rung, _ = _rung(pointer_debt_fn=lambda: next(seq))
        with pytest.raises(RuntimeError, match="NEW"):
            rung.converge(_Report())
