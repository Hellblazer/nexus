# SPDX-License-Identifier: AGPL-3.0-or-later
"""Tests for ``nx upgrade`` CLI command (RDR-076, Phase 4)."""
from __future__ import annotations

from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest
from click.testing import CliRunner

from nexus.cli import main

# nexus-aqbrk: PINNED to the local T2 backend, whole-file, because every test
# here drives ``nx upgrade``'s LOCAL SQLite migration path. In service mode
# ``_run_upgrade`` early-returns at upgrade.py:548 — "the local SQLite/Chroma
# tiers are an immutable migration source" — and that return is BEFORE the T3
# step block at :608, so all four TestUpgradeCommand failures and all three
# TestT3StepsThroughLadderLedger failures share ONE root cause rather than
# seven.
#
# The pin also DE-VACUUMS several tests that were passing for the wrong
# reason: ``test_upgrade_default`` and ``test_upgrade_auto_exits_zero_on_error``
# both assert exit_code == 0, which the service-mode short-circuit satisfies
# without running (or failing) a single migration.
#
# SERVICE HALF: was owned by NOTHING before this commit. tests/test_upgrade_e2e
# .py mentions the same message only inside ``skipif`` REASONS (its dies-roster
# skips), never in an assertion. ``TestUpgradeServiceModeShortCircuit`` at the
# bottom of this file now owns it.
#
# The pin is applied PER CLASS rather than as a module-level ``pytestmark``.
# A module-level mark also lands on the service-mode class, and
# ``local_t2_backend`` (non-autouse) then resolves AFTER that class's own
# autouse env fixture and overwrites service with sqlite — the two classes
# want opposite backends, so expressing the pin per class removes the
# ordering question instead of answering it.


@pytest.fixture()
def runner() -> CliRunner:
    return CliRunner()


@pytest.fixture(autouse=True)
def _clear_upgrade_done() -> None:
    from nexus.db import migrations

    migrations._upgrade_done.clear()


@pytest.fixture()
def _tmp_db(tmp_path: Path) -> Path:
    """Return a temp DB path and patch default_db_path to use it."""
    return tmp_path / "memory.db"


@pytest.fixture(autouse=True)
def _no_real_daemon_nudge():
    """Patch the post-upgrade supervised-daemon cycle (nexus-5ldk1) AND the
    RDR-185 precondition stage for all upgrade tests so they never shell out to
    the real host daemons or cycle a live supervisor (the precondition stage's
    production defaults read the REAL lease and, on a version mismatch, would
    stop/start the box's live service — never from a unit test). Yields the
    cycle mock so tests can assert whether the nudge fired.

    Re-pointed from `_cycle_daemon_to_current` (the T2 leg, retired with the
    daemon in nexus-i711w Stage 2 sub-stage B) to the surviving orchestrator, so
    the suppression still covers the one supervised tier that remains."""
    with (
        patch("nexus.commands.upgrade._cycle_supervised_daemons_to_current") as m,
        patch("nexus.commands.upgrade._converge_preconditions"),
    ):
        yield m


@pytest.mark.usefixtures("local_t2_backend")
class TestUpgradeCommand:
    """Tests for the ``nx upgrade`` CLI command."""

    def test_upgrade_default(self, runner: CliRunner, tmp_path: Path) -> None:
        """Default invocation runs pending migrations and reports results."""
        db_path = tmp_path / "memory.db"
        with (
            patch("nexus.commands.upgrade._db_path", return_value=db_path),
            patch("nexus.commands.upgrade.T3_UPGRADES", []),  # avoid cloud call
        ):
            result = runner.invoke(main, ["upgrade"])
        assert result.exit_code == 0

    def test_upgrade_dry_run(self, runner: CliRunner, tmp_path: Path) -> None:
        """--dry-run lists pending migrations without executing."""
        db_path = tmp_path / "memory.db"
        with (
            patch("nexus.commands.upgrade._db_path", return_value=db_path),
            patch("nexus.commands.upgrade.T3_UPGRADES", []),
        ):
            result = runner.invoke(main, ["upgrade", "--dry-run"])
        assert result.exit_code == 0
        assert "dry-run" in result.output.lower() or "pending" in result.output.lower()

    def test_upgrade_cycles_daemon_on_success(
        self, runner: CliRunner, tmp_path: Path, _no_real_daemon_nudge,
    ) -> None:
        """nexus-5ldk1: a successful (non-dry-run) upgrade brings a stale
        daemon to the just-installed version."""
        db_path = tmp_path / "memory.db"
        with (
            patch("nexus.commands.upgrade._db_path", return_value=db_path),
            patch("nexus.commands.upgrade.T3_UPGRADES", []),
        ):
            result = runner.invoke(main, ["upgrade"])
        assert result.exit_code == 0
        assert _no_real_daemon_nudge.called, "upgrade did not cycle the daemon"

    def test_upgrade_dry_run_does_not_cycle_daemon(
        self, runner: CliRunner, tmp_path: Path, _no_real_daemon_nudge,
    ) -> None:
        """--dry-run installs nothing, so it must not touch the daemon."""
        db_path = tmp_path / "memory.db"
        with (
            patch("nexus.commands.upgrade._db_path", return_value=db_path),
            patch("nexus.commands.upgrade.T3_UPGRADES", []),
        ):
            result = runner.invoke(main, ["upgrade", "--dry-run"])
        assert result.exit_code == 0
        assert not _no_real_daemon_nudge.called, "dry-run must not cycle the daemon"

    def test_upgrade_force(self, runner: CliRunner, tmp_path: Path) -> None:
        """--force resets version gate to 0.0.0 and re-runs."""
        db_path = tmp_path / "memory.db"
        with (
            patch("nexus.commands.upgrade._db_path", return_value=db_path),
            patch("nexus.commands.upgrade.T3_UPGRADES", []),
        ):
            # First run to set up version
            runner.invoke(main, ["upgrade"])

            from nexus.db import migrations

            migrations._upgrade_done.clear()

            # Force re-run
            result = runner.invoke(main, ["upgrade", "--force"])
        assert result.exit_code == 0

        # Verify stored version is current after --force
        import sqlite3

        conn = sqlite3.connect(str(db_path))
        row = conn.execute(
            "SELECT value FROM _nexus_version WHERE key='cli_version'"
        ).fetchone()
        assert row is not None
        conn.close()

    def test_force_dry_run_shows_all_pending(
        self, runner: CliRunner, tmp_path: Path
    ) -> None:
        """--force --dry-run shows all migrations as pending."""
        db_path = tmp_path / "memory.db"
        with (
            patch("nexus.commands.upgrade._db_path", return_value=db_path),
            patch("nexus.commands.upgrade.T3_UPGRADES", []),
        ):
            # First run to apply everything
            runner.invoke(main, ["upgrade"])

            from nexus.db import migrations

            migrations._upgrade_done.clear()

            # Force dry-run — should list migrations
            result = runner.invoke(main, ["upgrade", "--force", "--dry-run"])
        assert result.exit_code == 0
        assert "dry-run" in result.output.lower() or "pending" in result.output.lower()

    def test_upgrade_auto_exits_zero_on_error(
        self, runner: CliRunner, tmp_path: Path
    ) -> None:
        """--auto exits 0 even when apply_pending raises."""
        db_path = tmp_path / "memory.db"
        with (
            patch("nexus.commands.upgrade._db_path", return_value=db_path),
            patch(
                "nexus.commands.upgrade.apply_pending",
                side_effect=RuntimeError("boom"),
            ),
        ):
            result = runner.invoke(main, ["upgrade", "--auto"])
        assert result.exit_code == 0

    def test_upgrade_auto_skips_t3(self, runner: CliRunner, tmp_path: Path) -> None:
        """--auto mode skips T3 upgrade steps."""
        from nexus.db.migrations import T3UpgradeStep

        db_path = tmp_path / "memory.db"
        t3_called: list[bool] = []
        step = T3UpgradeStep(
            "0.0.1", "test step", lambda t3, tax: t3_called.append(True)
        )
        with (
            patch("nexus.commands.upgrade._db_path", return_value=db_path),
            patch("nexus.commands.upgrade.T3_UPGRADES", [step]),
        ):
            result = runner.invoke(main, ["upgrade", "--auto"])
        assert result.exit_code == 0
        assert len(t3_called) == 0

    def test_dry_run_unresolvable_version(
        self, runner: CliRunner, tmp_path: Path
    ) -> None:
        """--dry-run with unresolvable CLI version emits distinct message."""
        db_path = tmp_path / "memory.db"
        with (
            patch("nexus.commands.upgrade._db_path", return_value=db_path),
            patch("nexus.commands.upgrade._current_version", return_value="0.0.0"),
            patch("nexus.commands.upgrade.T3_UPGRADES", []),
        ):
            result = runner.invoke(main, ["upgrade", "--dry-run"])
        assert result.exit_code == 0
        assert "cannot determine" in result.output.lower()

    def test_upgrade_up_to_date(self, runner: CliRunner, tmp_path: Path) -> None:
        """When already current, reports up to date."""
        db_path = tmp_path / "memory.db"
        with (
            patch("nexus.commands.upgrade._db_path", return_value=db_path),
            patch("nexus.commands.upgrade.T3_UPGRADES", []),
        ):
            # First run applies everything
            runner.invoke(main, ["upgrade"])

            from nexus.db import migrations

            migrations._upgrade_done.clear()

            # Second run — already current
            result = runner.invoke(main, ["upgrade"])
        assert result.exit_code == 0


class TestT3UpgradeStep:
    """T3UpgradeStep dataclass and T3_UPGRADES list."""

    def test_dataclass_fields(self) -> None:
        from nexus.db.migrations import T3UpgradeStep

        fn = MagicMock()
        step = T3UpgradeStep(introduced="4.2.0", name="test", fn=fn)
        assert step.introduced == "4.2.0"
        assert step.name == "test"
        assert step.fn is fn

class TestSubstrateBridgeRetired:
    """RDR-185 P4.2 retired the nexus-0rwwv bridge; RDR-155 P4b deleted
    its machinery outright (guided_upgrade.py and the demoted verbs are
    gone). The surviving pin: `nx upgrade` output never names the deleted
    verb."""

    def test_upgrade_never_advertises_the_deleted_verb(
        self, runner: CliRunner, tmp_path: Path, monkeypatch,
    ) -> None:
        with (
            patch("nexus.commands.upgrade._db_path", return_value=tmp_path / "memory.db"),
            patch("nexus.commands.upgrade.T3_UPGRADES", []),
        ):
            result = runner.invoke(main, ["upgrade"])
        assert result.exit_code == 0
        assert "guided-upgrade" not in result.output


@pytest.mark.usefixtures("local_t2_backend")
class TestT3StepsThroughLadderLedger:
    """RDR-186 .15: T3-step completion flows through the ladder's own
    CompletionLedger (namespaced ``t3-step:`` records in PG
    ladder_completions) — the inline ``_nexus_t3_steps`` table is retired.
    Position derivation ignores the namespaced records by construction
    (derive_ladder_position only consults the canonical rung ORDER)."""

    @pytest.fixture
    def runner(self) -> CliRunner:
        return CliRunner()

    @staticmethod
    def _ledger():
        from tests.upgrade.conftest import InMemoryCompletionLedger

        return InMemoryCompletionLedger()

    def _run(self, runner, tmp_path, step, holder_backend, force=False):
        import os

        from nexus.upgrade_ladder.holder import InProcessCompletionHolder

        db_path = tmp_path / "memory.db"
        with (
            patch("nexus.commands.upgrade._db_path", return_value=db_path),
            patch("nexus.commands.upgrade.T3_UPGRADES", [step]),
            patch("nexus.commands.upgrade._cycle_supervised_daemons_to_current"),
            patch("nexus.commands.upgrade._converge_preconditions"),
            patch("nexus.commands.upgrade._run_ladder"),
            patch("nexus.db.make_t3", return_value=object()),
            patch(
                "nexus.commands._helpers.default_db_path",
                return_value=tmp_path / "memory.db",
            ),
            patch(
                "nexus.commands.upgrade._t3_completion_holder",
                return_value=InProcessCompletionHolder(holder_backend),
            ),
            # Skip the real T2 migration chain (slow) and its confirm prompt —
            # the T3 block is what this class tests (the counting-defer test's
            # established pattern).
            patch("nexus.db.migrations.MIGRATIONS", []),
        ):
            from nexus.db import migrations

            migrations._upgrade_done.clear()
            args = ["upgrade", "--force"] if force else ["upgrade"]
            return runner.invoke(main, args, input="y\n")

    def test_step_completion_recorded_and_skipped_on_rerun(
        self, runner: CliRunner, tmp_path: Path
    ) -> None:
        from nexus.db.migrations import T3UpgradeStep

        calls: list[bool] = []
        step = T3UpgradeStep("0.0.1", "test step", lambda t3, tax: calls.append(True))
        backend = self._ledger()

        result = self._run(runner, tmp_path, step, backend)
        assert result.exit_code == 0, result.output
        assert calls == [True], "the step ran once"
        assert "t3-step:0.0.1:test step" in backend.verified_rungs(), (
            "completion recorded through the ladder ledger, namespaced"
        )

        result2 = self._run(runner, tmp_path, step, backend)
        assert result2.exit_code == 0, result2.output
        assert calls == [True], "second run skipped the already-recorded step"

    def test_local_t3_table_rows_carry_then_table_drops(
        self, runner: CliRunner, tmp_path: Path
    ) -> None:
        """Data-carry: pre-.15 _nexus_t3_steps rows are recorded through the
        ledger ONCE, then the local table is DROPPED (the retirement; the
        table is debt, not a rollback source — unlike chash_remap)."""
        import sqlite3

        from nexus.db.migrations import T3UpgradeStep

        db_path = tmp_path / "memory.db"
        conn = sqlite3.connect(db_path)
        conn.execute(
            "CREATE TABLE _nexus_t3_steps ("
            " introduced TEXT NOT NULL, name TEXT NOT NULL,"
            " applied_at TEXT NOT NULL, PRIMARY KEY (introduced, name))"
        )
        conn.execute(
            "INSERT INTO _nexus_t3_steps VALUES ('0.0.1', 'test step', 't0')"
        )
        conn.commit()
        conn.close()

        calls: list[bool] = []
        step = T3UpgradeStep("0.0.1", "test step", lambda t3, tax: calls.append(True))
        backend = self._ledger()

        result = self._run(runner, tmp_path, step, backend)
        assert result.exit_code == 0, result.output
        assert calls == [], "the carried record made the step already-done"
        assert "t3-step:0.0.1:test step" in backend.verified_rungs()

        conn = sqlite3.connect(db_path)
        try:
            tables = {
                r[0] for r in conn.execute(
                    "SELECT name FROM sqlite_master WHERE type='table'"
                )
            }
        finally:
            conn.close()
        assert "_nexus_t3_steps" not in tables, "the local table dropped after carry"

    def test_failed_step_records_nothing_and_force_retries_it(
        self, runner: CliRunner, tmp_path: Path
    ) -> None:
        """The retry contract over the NEW mechanism (reviewer-146xx-15 gap),
        stated PRECISELY: a step that raises records NO ledger key and the
        upgrade exits non-zero. The retry VEHICLE is ``--force`` (which
        reopens the version gate) — a plain re-run's pending_t3 is empty
        once apply_pending stamped the version, before AND after .15 (the
        version-gate limitation is pre-existing; the ledger's done-tracking
        is what makes --force skip succeeded steps while retrying failed
        ones)."""
        from nexus.db.migrations import T3UpgradeStep

        calls: list[bool] = []

        def _boom(t3, tax):
            calls.append(True)
            if len(calls) == 1:
                raise RuntimeError("step exploded")

        step = T3UpgradeStep("0.0.1", "test step", _boom)
        backend = self._ledger()

        result = self._run(runner, tmp_path, step, backend)
        assert result.exit_code != 0, "a failed step must exit non-zero"
        assert calls == [True]
        assert "t3-step:0.0.1:test step" not in backend.verified_rungs(), (
            "a failed step records nothing — the retry contract"
        )

        result2 = self._run(runner, tmp_path, step, backend, force=True)
        assert result2.exit_code == 0, result2.output
        assert calls == [True, True], "--force retried the failed step"
        assert "t3-step:0.0.1:test step" in backend.verified_rungs()

        # ...and a SUCCEEDED step under --force is skipped via the ledger
        # (the done-tracking's whole purpose).
        result3 = self._run(runner, tmp_path, step, backend, force=True)
        assert result3.exit_code == 0, result3.output
        assert calls == [True, True], "--force skipped the recorded step"

    def test_engine_down_carry_leaves_table_for_recarry(
        self, runner: CliRunner, tmp_path: Path
    ) -> None:
        """The gated DROP (critic-146xx-15): a carry whose facts cannot reach
        the engine must NOT delete the local table — it stays for the next
        invocation's idempotent re-carry, and only a durably-confirmed carry
        drops it."""
        import sqlite3

        from nexus.db.migrations import T3UpgradeStep

        db_path = tmp_path / "memory.db"
        conn = sqlite3.connect(db_path)
        conn.execute(
            "CREATE TABLE _nexus_t3_steps ("
            " introduced TEXT NOT NULL, name TEXT NOT NULL,"
            " applied_at TEXT NOT NULL, PRIMARY KEY (introduced, name))"
        )
        conn.execute("INSERT INTO _nexus_t3_steps VALUES ('0.0.1', 'test step', 't0')")
        conn.commit()
        conn.close()

        class DownBackend:
            def record_verified(self, rung_name, *, package_version, detail=""):
                raise ConnectionError("engine down")

            def verified_rungs(self):
                raise ConnectionError("engine down")

            def completions(self):
                raise ConnectionError("engine down")

        step = T3UpgradeStep("0.0.1", "test step", lambda t3, tax: None)
        result = self._run(runner, tmp_path, step, DownBackend())
        assert result.exit_code == 0, result.output  # holder degrades, never crashes

        conn = sqlite3.connect(db_path)
        try:
            tables = {
                r[0] for r in conn.execute(
                    "SELECT name FROM sqlite_master WHERE type='table'"
                )
            }
        finally:
            conn.close()
        assert "_nexus_t3_steps" in tables, (
            "an unconfirmed carry must LEAVE the local table (the gated DROP)"
        )


class TestUpgradeServiceModeShortCircuit:
    """``nx upgrade`` in SERVICE mode — the half the file-level pin excludes.

    nexus-aqbrk. The rest of this file is pinned to the local T2 backend
    because it drives the SQLite migration path. That pin is only honest if
    something still covers what the verb does in service mode, and before
    this class NOTHING did: ``tests/test_upgrade_e2e.py`` names the same
    message exclusively inside ``skipif`` reasons, so the branch at
    ``upgrade.py:548`` had no assertion anywhere in the suite.

    That branch is not obscure — it is what every migrated user gets from
    ``nx upgrade``, and RDR-176 Phase 1 Gap 2 depends on it: the local
    SQLite/Chroma tiers are a FROZEN migration source, so an upgrade that
    stamped ``_nexus_version`` or flipped ``journal_mode=WAL`` would mutate
    the very thing a downgrade has to read back.

    These deliberately do NOT request ``local_t2_backend`` — they override
    the file-level pin by setting the backend to service themselves, which
    wins because it is the later ``setenv`` on the same monkeypatch.
    """

    @pytest.fixture(autouse=True)
    def _service_backend(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv("NX_STORAGE_BACKEND", "service")

    def test_reports_immutable_source_and_exits_zero(
        self, runner: CliRunner, tmp_path: Path,
    ) -> None:
        db_path = tmp_path / "memory.db"
        with (
            patch("nexus.commands.upgrade._db_path", return_value=db_path),
            patch("nexus.commands.upgrade.T3_UPGRADES", []),
        ):
            result = runner.invoke(main, ["upgrade"])
        assert result.exit_code == 0, result.output
        assert "immutable" in result.output.lower(), result.output
        assert "no local schema migration to run" in result.output.lower()

    def test_never_creates_or_touches_the_local_db(
        self, runner: CliRunner, tmp_path: Path,
    ) -> None:
        """RDR-176 Gap 2: the frozen migration source must not be written.

        Asserted as ABSENCE OF THE FILE, which is stronger than an mtime
        check — ``bootstrap_version`` / ``apply_pending`` would CREATE it,
        and ``journal_mode=WAL`` is itself a header write.
        """
        db_path = tmp_path / "memory.db"
        with (
            patch("nexus.commands.upgrade._db_path", return_value=db_path),
            patch("nexus.commands.upgrade.T3_UPGRADES", []),
        ):
            result = runner.invoke(main, ["upgrade"])
        assert result.exit_code == 0, result.output
        assert not db_path.exists(), (
            f"service-mode upgrade created {db_path.name} — the local tier is "
            f"an immutable migration source (RDR-176 P1 Gap 2) and a downgrade "
            f"has to read it back unmodified"
        )

    def test_t3_steps_do_not_run(self, runner: CliRunner, tmp_path: Path) -> None:
        """The early return sits BEFORE the T3 block, so steps must not fire.

        This is the invariant that produced three of this file's seven
        failures (TestT3StepsThroughLadderLedger); stated directly here it
        is the intended behaviour rather than a symptom.
        """
        from nexus.db.migrations import T3UpgradeStep

        calls: list[bool] = []
        step = T3UpgradeStep("0.0.1", "test step", lambda t3, tax: calls.append(True))
        with (
            patch("nexus.commands.upgrade._db_path", return_value=tmp_path / "memory.db"),
            patch("nexus.commands.upgrade.T3_UPGRADES", [step]),
        ):
            result = runner.invoke(main, ["upgrade"])
        assert result.exit_code == 0, result.output
        assert calls == [], "T3 upgrade steps ran despite the service-mode return"

    def test_auto_mode_is_silent_but_still_a_no_op(
        self, runner: CliRunner, tmp_path: Path,
    ) -> None:
        """``--auto`` suppresses the message (upgrade.py:549) — not the guard.

        Pins both halves of that conditional: no chatter on the automatic
        path, and still no local file.
        """
        db_path = tmp_path / "memory.db"
        with (
            patch("nexus.commands.upgrade._db_path", return_value=db_path),
            patch("nexus.commands.upgrade.T3_UPGRADES", []),
        ):
            result = runner.invoke(main, ["upgrade", "--auto"])
        assert result.exit_code == 0, result.output
        assert "immutable" not in result.output.lower(), (
            f"--auto must not emit the advisory: {result.output!r}"
        )
        assert not db_path.exists()
