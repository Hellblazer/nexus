# SPDX-License-Identifier: AGPL-3.0-or-later
"""RDR-185 P0.4 (nexus-n7u38.4): the two user surfaces of the ladder.

``nx doctor`` gets a READ-ONLY pending-rungs check (zero writes, zero
work — the ``resolve_pending_steps`` dry-run-truth precedent), and
``nx upgrade`` gets the single-trigger walk hook. Both stay silent while
the production registry is empty (rungs land P1+); these tests drive them
with synthetic registries through the injectable seams.
"""
from __future__ import annotations

import inspect
import pathlib
from dataclasses import dataclass, field

import click
import pytest

from unittest.mock import patch

from click.testing import CliRunner

import nexus.db.migrations as migrations
import nexus.upgrade_ladder.registry as ladder_registry
from nexus.cli import main
from nexus.db.migrations import Migration, MigrationRetry
from nexus.health import _check_pending_rungs, run_health_checks
import nexus.commands.upgrade as upgrade_mod
from nexus.commands.upgrade import _run_ladder, upgrade
from nexus.upgrade_ladder.completion import CompletionStore
from nexus.upgrade_ladder.protocol import ConvergeOutcome, ConvergeResult, ProgressReporter, RungStatus
from nexus.upgrade_ladder.registry import LadderRegistry


@dataclass
class SurfaceRung:
    name: str
    pending: bool = True
    verify_result: bool = True
    converge_calls: int = 0
    verify_calls: int = 0

    def detect(self) -> RungStatus:
        return RungStatus(
            applicable=True,
            converged=not self.pending,
            pending_detail="3 collections behind" if self.pending else "",
        )

    def converge(self, report: ProgressReporter) -> ConvergeResult:
        self.converge_calls += 1
        self.pending = False
        return ConvergeResult(ConvergeOutcome.COMPLETED)

    def verify(self) -> bool:
        self.verify_calls += 1
        return self.verify_result


@dataclass
class _Reg:
    rungs: tuple[SurfaceRung, ...] = field(default_factory=tuple)

    def registry(self) -> LadderRegistry:
        return LadderRegistry(self.rungs)


# ── nexus-k1m2f: `nx upgrade --yes` must actually reach the rung's cost gate ──


def test_yes_flag_reaches_the_rungs_consent_channel(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The WIRE, not the gate. Every other NX_ASSUME_YES test calls
    _default_cost_gate directly with monkeypatch.setenv, so deleting the
    flag->env wiring in the command passed 387 tests (code review, 2026-07-17) —
    the consent channel RDR-185's amended Constraints make load-bearing had zero
    coverage of the only part a user touches. This arc already shipped a
    NameError in a production default no test executed; same class.

    Observes at `_run_ladder`, the ONLY consumer, reading exactly what the rung's
    `assume_yes()` would read."""
    from nexus.upgrade_ladder.rungs.substrate_etl import assume_yes

    seen: list[bool] = []
    monkeypatch.setattr(upgrade_mod, "_run_ladder", lambda **_kw: seen.append(assume_yes()))
    monkeypatch.setattr(upgrade_mod, "_quiesce_daemon", lambda: None)
    monkeypatch.setattr(upgrade_mod, "_run_upgrade", lambda **_kw: None)
    monkeypatch.setattr(upgrade_mod, "_converge_preconditions", lambda **_kw: None)
    monkeypatch.setattr(upgrade_mod, "_cycle_supervised_daemons_to_current", lambda **_kw: None)
    monkeypatch.delenv("NX_ASSUME_YES", raising=False)

    CliRunner().invoke(upgrade, ["--yes"], catch_exceptions=False)
    assert seen == [True], "`--yes` never reached the rung's consent channel"

    # Non-vacuity: the same command without the flag must NOT consent.
    seen.clear()
    CliRunner().invoke(upgrade, [], catch_exceptions=False)
    assert seen == [False]


def test_yes_flag_is_not_visible_to_the_daemons_the_upgrade_spawns(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Samples the env AT THE DAEMON-SPAWN INSTANT, which is the only moment
    that can observe the leak.

    The first draft of this pin asserted `"NX_ASSUME_YES" not in os.environ`
    AFTER the command returned. That was true, and true for a reason unrelated
    to the property: the daemon spawns happen inside the command's OWN finally,
    40 lines and one stack frame before any outer restore runs. The assertion
    could not fail for the reason it named, and passed under the real bug
    (substantive critic, 2026-07-17 — the fifth vacuous pin in this arc, and the
    first to short-circuit in TIME rather than control flow).

    `_cycle_supervised_daemons_to_current` and friends spawn subprocesses with
    no `env=`, so they inherit this process's environment. A flag typed once for
    one invocation must not hand a long-lived daemon standing consent to spend
    money."""
    import os

    at_spawn: dict[str, str | None] = {}
    monkeypatch.setattr(upgrade_mod, "_quiesce_daemon", lambda: None)
    monkeypatch.setattr(upgrade_mod, "_run_upgrade", lambda **_kw: None)
    monkeypatch.setattr(upgrade_mod, "_converge_preconditions", lambda **_kw: None)
    monkeypatch.setattr(upgrade_mod, "_run_ladder", lambda **_kw: None)
    monkeypatch.setattr(
        upgrade_mod,
        "_cycle_supervised_daemons_to_current",
        lambda **_kw: at_spawn.update(seen=os.environ.get("NX_ASSUME_YES")),
    )
    monkeypatch.delenv("NX_ASSUME_YES", raising=False)

    CliRunner().invoke(upgrade, ["--yes"], catch_exceptions=False)
    assert at_spawn == {"seen": None}, (
        "the daemons `nx upgrade --yes` spawns inherited standing consent"
    )


# ── nexus-fffey: a rung that CANNOT answer must not read as converged ────────


@dataclass
class _RefusingRung:
    """A rung whose detect() refuses the world — what `plan_substrate_legs`
    does on a target collision (`SubstrateTargetCollision`)."""

    name: str = "substrate-etl"

    def detect(self) -> RungStatus:
        raise RuntimeError("two DISTINCT source collections remap onto the same target")

    def converge(self, report: ProgressReporter) -> ConvergeResult:  # pragma: no cover
        raise AssertionError("converge must not run when detect refused")

    def verify(self) -> bool:  # pragma: no cover
        raise AssertionError("verify must not run when detect refused")


def test_a_rung_that_cannot_answer_is_pending_not_green(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """`pending_rungs` was a bare comprehension, so a raising detect() escaped
    the whole sweep into `_check_pending_rungs`' blanket handler and doctor
    printed `ok=True — "check failed (non-critical)"`. An install `nx upgrade`
    REFUSES then read as healthy, and the refusal's message — the only text
    naming the remedy — appeared nowhere. Gap-4 makes this row the authority on
    pending work; it must not answer "fine" when it cannot answer at all."""
    monkeypatch.setattr(
        ladder_registry, "default_registry", lambda **kw: LadderRegistry((_RefusingRung(),))
    )
    (result,) = _check_pending_rungs()
    assert result.ok is False
    assert result.warn is True
    assert "remap onto the same target" in result.detail  # the reason survives


def test_one_refusing_rung_does_not_blind_the_others(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Per-rung degradation: the healthy rung's verdict must still be reported.
    A sweep-wide except loses every other rung's answer along with the broken
    one's."""
    healthy = SurfaceRung("t2-schema", pending=True)
    monkeypatch.setattr(
        ladder_registry,
        "default_registry",
        lambda **kw: LadderRegistry((healthy, _RefusingRung())),
    )
    (result,) = _check_pending_rungs()
    assert "t2-schema" in result.detail          # the healthy rung still spoke
    assert "substrate-etl" in result.detail      # ...and so did the broken one


# ── nx doctor: _check_pending_rungs ──────────────────────────────────────────


def test_doctor_reports_pending_rungs_as_soft_warn(monkeypatch: pytest.MonkeyPatch) -> None:
    rung = SurfaceRung("substrate-etl")
    monkeypatch.setattr(ladder_registry, "default_registry", lambda **kw: LadderRegistry((rung,)))
    results = _check_pending_rungs()
    assert len(results) == 1
    result = results[0]
    assert result.ok is False
    assert result.warn is True  # soft warning, never fatal (RDR-129 B4)
    assert "substrate-etl" in result.detail
    assert "3 collections behind" in result.detail
    assert any("nx upgrade" in fix for fix in result.fix_suggestions)


def test_doctor_check_is_read_only(monkeypatch: pytest.MonkeyPatch) -> None:
    """The doctor surface reports from detect() only — zero work."""
    rung = SurfaceRung("substrate-etl")
    monkeypatch.setattr(ladder_registry, "default_registry", lambda **kw: LadderRegistry((rung,)))
    _check_pending_rungs()
    assert rung.converge_calls == 0
    assert rung.verify_calls == 0


def test_doctor_converged_registry_is_ok(monkeypatch: pytest.MonkeyPatch) -> None:
    rung = SurfaceRung("t2-schema", pending=False)
    monkeypatch.setattr(ladder_registry, "default_registry", lambda **kw: LadderRegistry((rung,)))
    results = _check_pending_rungs()
    assert results[0].ok is True
    assert "no pending rungs" in results[0].detail


def test_doctor_empty_registry_is_ok(monkeypatch: pytest.MonkeyPatch) -> None:
    """An empty registry passes quietly. (Monkeypatched empty since P1: the
    production registry now holds the t2-schema rung, whose real detect()
    reads whatever memory.db the environment resolves — not unit-test
    territory; the real rung is covered in test_t2_schema_rung.py.)"""
    monkeypatch.setattr(ladder_registry, "default_registry", lambda **kw: LadderRegistry(()))
    results = _check_pending_rungs()
    assert results[0].ok is True


def test_doctor_real_registry_on_service_mode_reports_no_pending(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """P1 review Critical companion: on a SERVICE-mode install the REAL
    registry's t2-schema rung is N/A (detect-and-skip before any path is
    touched), so doctor reports no pending rungs instead of a spurious
    'run nx upgrade' remedy against the immutable local source."""
    monkeypatch.setenv("NX_STORAGE_BACKEND", "service")
    results = _check_pending_rungs()  # real default_registry, real probe
    assert results[0].ok is True
    assert "no pending rungs" in results[0].detail


def test_doctor_check_is_crash_proof(monkeypatch: pytest.MonkeyPatch) -> None:
    """Every doctor check must degrade internally, never crash `nx doctor`."""
    def _boom() -> LadderRegistry:
        raise RuntimeError("registry exploded")

    monkeypatch.setattr(ladder_registry, "default_registry", _boom)
    results = _check_pending_rungs()
    assert results[0].ok is True
    assert "check failed" in results[0].detail


def test_doctor_check_is_wired_into_run_health_checks() -> None:
    """Wiring pin: run_health_checks() actually calls the ladder check (a
    defined-but-unregistered check is the silent-scope-reduction shape)."""
    source = inspect.getsource(run_health_checks)
    assert "_check_pending_rungs()" in source


# ── nx upgrade: _run_ladder ──────────────────────────────────────────────────


def test_walk_converges_and_records(tmp_path: pathlib.Path, monkeypatch: pytest.MonkeyPatch) -> None:
    rung = SurfaceRung("t2-schema")
    monkeypatch.setattr(ladder_registry, "default_registry", lambda **kw: LadderRegistry((rung,)))
    db = tmp_path / "ladder.db"
    _run_ladder(dry_run=False, auto_mode=True, _store_path_fn=lambda: db)
    assert rung.converge_calls == 1
    with CompletionStore(db) as store:
        assert store.verified_rungs() == frozenset({"t2-schema"})


def test_dry_run_reports_and_writes_nothing(
    tmp_path: pathlib.Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """Dry-run truth: pending rungs are reported from read-only detect();
    the completion store is never even opened (zero writes)."""
    rung = SurfaceRung("substrate-etl")
    monkeypatch.setattr(ladder_registry, "default_registry", lambda **kw: LadderRegistry((rung,)))
    db = tmp_path / "ladder.db"
    _run_ladder(dry_run=True, auto_mode=False, _store_path_fn=lambda: db)
    out = capsys.readouterr().out
    assert "substrate-etl" in out
    assert rung.converge_calls == 0
    assert not db.exists()


def test_empty_registry_walk_is_silent(
    tmp_path: pathlib.Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """An empty registry adds zero output to `nx upgrade`. (Monkeypatched
    empty since P1 — the production registry now holds the t2-schema rung,
    whose real walk would touch the environment's memory.db.)"""
    monkeypatch.setattr(ladder_registry, "default_registry", lambda **kw: LadderRegistry(()))
    _run_ladder(dry_run=False, auto_mode=False, _store_path_fn=lambda: tmp_path / "ladder.db")
    _run_ladder(dry_run=True, auto_mode=False, _store_path_fn=lambda: tmp_path / "ladder.db")
    assert capsys.readouterr().out == ""


def test_dry_run_survives_a_rung_whose_detect_raises(
    tmp_path: pathlib.Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """Critic P0.R2 finding 1: a real rung's detect() does live reads that can
    fail; --dry-run must report the broken rung and keep reporting the rest,
    never crash with a raw traceback."""

    @dataclass
    class BoomRung:
        name: str = "boom"

        def detect(self) -> RungStatus:
            raise RuntimeError("locked db")

        def converge(self, report: ProgressReporter) -> ConvergeResult:
            return ConvergeResult(ConvergeOutcome.COMPLETED)

        def verify(self) -> bool:
            return True

    healthy = SurfaceRung("substrate-etl")
    monkeypatch.setattr(
        ladder_registry, "default_registry", lambda **kw: LadderRegistry((BoomRung(), healthy))
    )
    db = tmp_path / "ladder.db"
    _run_ladder(dry_run=True, auto_mode=False, _store_path_fn=lambda: db)
    out = capsys.readouterr().out
    assert "boom" in out and "detect failed" in out
    assert "substrate-etl" in out  # the rest of the report still happened
    assert not db.exists()  # still zero writes


def test_failed_walk_raises_for_interactive(
    tmp_path: pathlib.Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A failed rung fails the upgrade loudly (no silent fallbacks for
    correctness problems); --auto swallowing happens in upgrade()'s existing
    handler, not here."""
    rung = SurfaceRung("t2-schema", verify_result=False)
    monkeypatch.setattr(ladder_registry, "default_registry", lambda **kw: LadderRegistry((rung,)))
    with pytest.raises(click.ClickException, match="t2-schema"):
        _run_ladder(dry_run=False, auto_mode=False, _store_path_fn=lambda: tmp_path / "ladder.db")
    with CompletionStore(tmp_path / "ladder.db") as store:
        assert store.verified_rungs() == frozenset()  # RDR-142: nothing recorded


def test_deferred_walk_notices_but_exits_cleanly(
    tmp_path: pathlib.Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """A deferred-only walk is NOT a failure (RDR-142 would-defer class):
    no ClickException, a notice line instead, nothing recorded."""

    @dataclass
    class DeferRung:
        name: str = "t2-schema"

        def detect(self) -> RungStatus:
            return RungStatus(applicable=True, converged=False, pending_detail="behind")

        def converge(self, report: ProgressReporter) -> ConvergeResult:
            return ConvergeResult(ConvergeOutcome.DEFERRED, detail="catalog absent")

        def verify(self) -> bool:
            return False

    monkeypatch.setattr(
        ladder_registry, "default_registry", lambda **kw: LadderRegistry((DeferRung(),))
    )
    db = tmp_path / "ladder.db"
    _run_ladder(dry_run=False, auto_mode=False, _store_path_fn=lambda: db)  # no raise
    out = capsys.readouterr().out
    assert "deferred" in out and "catalog absent" in out
    with CompletionStore(db) as store:
        assert store.verified_rungs() == frozenset()


def test_upgrade_invocation_executes_each_migration_step_exactly_once(
    tmp_path: pathlib.Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """P1 critique Critical: nx upgrade runs _run_upgrade (legacy leg) then
    the ladder walk in one invocation — in the DEFERRED case the rung must
    REPORT the prior attempt, never re-execute apply_pending (which re-ran
    every eligible step's body, incl. a 30s drain attempt, twice — also on
    the SessionStart --auto hot path). Pinned end to end through the CLI."""
    migrations._upgrade_done.clear()
    calls = {"n": 0}

    def _counting_defer(conn: object) -> None:
        calls["n"] += 1
        raise MigrationRetry("precondition blocked — retried next open")

    monkeypatch.setattr(
        migrations,
        "MIGRATIONS",
        [Migration(introduced="99.0.0", name="counting-defer", fn=_counting_defer)],
    )
    monkeypatch.setenv("NX_MIGRATION_NOTICE", "0")  # keep the bridge probe out
    db = tmp_path / "memory.db"
    with (
        patch("nexus.commands.upgrade._db_path", return_value=db),
        patch("nexus.commands.upgrade.T3_UPGRADES", []),
        patch("nexus.commands.upgrade._quiesce_daemon"),
        patch("nexus.commands.upgrade._cycle_supervised_daemons_to_current"),
    ):
        result = CliRunner().invoke(main, ["upgrade"])
    assert result.exit_code == 0, result.output  # deferral is NOT a failure
    assert calls["n"] == 1, (
        f"deferring step executed {calls['n']} times in one nx upgrade "
        "invocation — the ladder must report, not re-run"
    )
    assert "deferred" in result.output.lower()


def test_upgrade_command_is_wired_to_the_ladder() -> None:
    """Wiring pin: the single trigger (`nx upgrade`) walks the ladder.

    """
    assert "_run_ladder(" in inspect.getsource(upgrade.callback)
