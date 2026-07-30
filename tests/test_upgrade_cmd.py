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


# TestUpgradeCommand + TestT3StepsThroughLadderLedger DELETED (RDR-158 P3,
# nexus-7bomn): their subject was _run_upgrade's LOCAL-SQLITE leg
# (bootstrap_version / apply_pending / dry-run resolver / T3 ladder-ledger
# carry against a local memory.db), reached only via the =sqlite selector
# those tests pinned. The selector is retired — =sqlite now hard-errors with
# the stranded-install redirect (pinned below by
# TestUpgradeRetiredSqliteOptOut) — and the leg itself is Stage 4's deletion
# target (db/migrations.py + bootstrap plumbing). On THIS version the local
# migration story is the two-hop redirect: install the last
# migration-capable 6.x release, migrate there, upgrade back.


class TestUpgradeRetiredSqliteOptOut:
    """RDR-158 P3 (nexus-7bomn): the =sqlite opt-out is a clean CLI refusal.

    The resolver raises StorageModeFlagError deep inside _run_upgrade's
    service gate; the _StorageBackendGuardGroup at the CLI boundary must
    render it as Click's "Error: <redirect>" + exit 2 — never a raw Python
    traceback (review Critical: the redirect message is the product here,
    and a traceback buries it).
    """

    def test_upgrade_under_sqlite_export_prints_redirect_and_exits_2(
        self, runner: CliRunner, tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        monkeypatch.setenv("NX_STORAGE_BACKEND", "sqlite")
        with (
            patch("nexus.commands.upgrade._db_path", return_value=tmp_path / "memory.db"),
            patch("nexus.commands.upgrade.T3_UPGRADES", []),
        ):
            result = runner.invoke(main, ["upgrade"])
        assert result.exit_code == 2, result.output
        assert "Traceback" not in result.output
        assert "retired SQLite storage backend" in result.output
        assert "nx upgrade" in result.output  # the two-hop redirect verb
        assert "migration-capable" in result.output


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
