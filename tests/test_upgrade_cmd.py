# SPDX-License-Identifier: AGPL-3.0-or-later
"""Tests for ``nx upgrade`` CLI command (RDR-076, Phase 4).

RDR-158 P4 Stage 4 (nexus-i711w): the local-SQLite migration leg of
``_run_upgrade`` is DELETED with ``nexus/db/migrations.py``, so the classes
that drove it (TestUpgradeCommand, TestT3StepsThroughLadderLedger — already
deleted at Stage 3 — plus TestT3UpgradeStep and the T3-step short-circuit
pin) are gone with their subject. What survives is the whole verb surface on
this version: the service no-op message, the frozen-source non-mutation, and
the stranded ``=sqlite`` export redirect. The old ``local_t2_backend``
file-pin is gone with the leg it pinned.
"""
from __future__ import annotations

from pathlib import Path
from unittest.mock import patch

import pytest
from click.testing import CliRunner

from nexus.cli import main


@pytest.fixture()
def runner() -> CliRunner:
    return CliRunner()


@pytest.fixture(autouse=True)
def _no_real_daemon_nudge():
    """Patch the post-upgrade supervised-daemon cycle (nexus-5ldk1) AND the
    RDR-185 precondition stage for all upgrade tests so they never shell out to
    the real host daemons or cycle a live supervisor (the precondition stage's
    production defaults read the REAL lease and, on a version mismatch, would
    stop/start the box's live service — never from a unit test). Yields the
    cycle mock so tests can assert whether the nudge fired.
    """
    with (
        patch("nexus.commands.upgrade._cycle_supervised_daemons_to_current") as m,
        patch("nexus.commands.upgrade._converge_preconditions"),
    ):
        yield m


class TestSubstrateBridgeRetired:
    """RDR-185 P4.2 retired the nexus-0rwwv bridge; RDR-155 P4b deleted
    its machinery outright (guided_upgrade.py and the demoted verbs are
    gone). The surviving pin: `nx upgrade` output never names the deleted
    verb."""

    def test_upgrade_never_advertises_the_deleted_verb(
        self, runner: CliRunner, tmp_path: Path, monkeypatch,
    ) -> None:
        result = runner.invoke(main, ["upgrade"])
        assert result.exit_code == 0
        assert "guided-upgrade" not in result.output


class TestUpgradeRetiredSqliteOptOut:
    """RDR-158 P3 (nexus-7bomn): the =sqlite opt-out is a clean CLI refusal.

    The resolver raises StorageModeFlagError inside _run_upgrade (the
    ``storage_backend_for`` call is retained by the Stage 4 collapse for
    exactly this); the _StorageBackendGuardGroup at the CLI boundary must
    render it as Click's "Error: <redirect>" + exit 2 — never a raw Python
    traceback (review Critical: the redirect message is the product here,
    and a traceback buries it).
    """

    def test_upgrade_under_sqlite_export_prints_redirect_and_exits_2(
        self, runner: CliRunner, tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        monkeypatch.setenv("NX_STORAGE_BACKEND", "sqlite")
        result = runner.invoke(main, ["upgrade"])
        assert result.exit_code == 2, result.output
        assert "Traceback" not in result.output
        assert "retired SQLite storage backend" in result.output
        assert "nx upgrade" in result.output  # the two-hop redirect verb
        assert "migration-capable" in result.output


class TestUpgradeServiceModeShortCircuit:
    """``nx upgrade`` in SERVICE mode — the whole verb since Stage 4.

    nexus-aqbrk established this coverage when the rest of the file was
    pinned to the local leg; RDR-158 P4 Stage 4 deleted that leg, so these
    are now the primary pins on what every user gets from ``nx upgrade``.
    RDR-176 Phase 1 Gap 2 depends on them: the local SQLite/Chroma tiers are
    a FROZEN migration source, so an upgrade that stamped ``_nexus_version``
    or flipped ``journal_mode=WAL`` would mutate the very thing a downgrade
    has to read back.
    """

    @pytest.fixture(autouse=True)
    def _service_backend(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv("NX_STORAGE_BACKEND", "service")

    def test_reports_immutable_source_and_exits_zero(
        self, runner: CliRunner, tmp_path: Path,
    ) -> None:
        result = runner.invoke(main, ["upgrade"])
        assert result.exit_code == 0, result.output
        assert "immutable" in result.output.lower(), result.output
        assert "no local schema migration to run" in result.output.lower()

    def test_never_creates_or_touches_the_local_db(
        self, runner: CliRunner, tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """RDR-176 Gap 2: the frozen migration source must not be written.

        Asserted as ABSENCE OF THE FILE, which is stronger than an mtime
        check — the deleted leg's ``bootstrap_version`` / ``apply_pending``
        would have CREATED it, and ``journal_mode=WAL`` is itself a header
        write. The config dir is pointed at a tmp path so the assertion
        covers wherever a regression might resolve the default db path.
        """
        monkeypatch.setenv("NEXUS_CONFIG_DIR", str(tmp_path))
        db_path = tmp_path / "memory.db"
        result = runner.invoke(main, ["upgrade"])
        assert result.exit_code == 0, result.output
        assert not db_path.exists(), (
            f"service-mode upgrade created {db_path.name} — the local tier is "
            f"an immutable migration source (RDR-176 P1 Gap 2) and a downgrade "
            f"has to read it back unmodified"
        )

    def test_auto_mode_is_silent_but_still_a_no_op(
        self, runner: CliRunner, tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """``--auto`` suppresses the message — not the guard.

        Pins both halves of that conditional: no chatter on the automatic
        path, and still no local file.
        """
        monkeypatch.setenv("NEXUS_CONFIG_DIR", str(tmp_path))
        db_path = tmp_path / "memory.db"
        result = runner.invoke(main, ["upgrade", "--auto"])
        assert result.exit_code == 0, result.output
        assert "immutable" not in result.output.lower(), (
            f"--auto must not emit the advisory: {result.output!r}"
        )
        assert not db_path.exists()


class TestUpgradeBackfillsInstallModeRecord:
    """nexus-g7ijj: a successful ``nx upgrade`` backfills a missing
    ``install.mode`` record (the ladder-succeeded path); ``--dry-run``
    must write nothing.

    The root ``nx`` group also runs its own ``check_version_transition``
    auto-trigger on EVERY invocation (nexus/cli.py), which — via the
    Part 2b hook — can perform the same backfill on an unrelated path (a
    version-transition / first-ever-run). ``CliRunner`` does not touch
    ``sys.argv``, so that root trigger's own preview detection cannot see
    this test's ``--dry-run`` subcommand flag; pre-stamping
    ``last_seen_version`` to the installed version makes it a same-version
    no-op so these tests isolate the ``upgrade.py``-level hook only.
    """

    def _cfg_dir(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
        from importlib.metadata import version as _pkg_version

        cfg = tmp_path / "cfg"
        cfg.mkdir()
        monkeypatch.setenv("NEXUS_CONFIG_DIR", str(cfg))
        for var in ("NX_LOCAL", "CHROMA_API_KEY", "VOYAGE_API_KEY", "NX_SERVICE_URL"):
            monkeypatch.delenv(var, raising=False)
        from nexus.db.pg_provision import CREDENTIALS_FILENAME
        (cfg / CREDENTIALS_FILENAME).write_text("PGHOST=127.0.0.1\n")
        (cfg / "last_seen_version").write_text(_pkg_version("conexus") + "\n")
        return cfg

    def test_successful_upgrade_stamps_the_record(
        self, runner: CliRunner, tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        cfg = self._cfg_dir(tmp_path, monkeypatch)
        result = runner.invoke(main, ["upgrade"])
        assert result.exit_code == 0, result.output
        import yaml
        data = yaml.safe_load((cfg / "config.yml").read_text())
        assert data["install"]["mode"] == "local"

    def test_dry_run_does_not_stamp(
        self, runner: CliRunner, tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        cfg = self._cfg_dir(tmp_path, monkeypatch)
        result = runner.invoke(main, ["upgrade", "--dry-run"])
        assert result.exit_code == 0, result.output
        assert not (cfg / "config.yml").exists()

    def test_malformed_config_yml_does_not_fail_the_upgrade(
        self, runner: CliRunner, tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """nexus-g7ijj fix round: a malformed config.yml must not turn a
        successful ladder run into a reported FAILURE. Before the fix,
        ``backfill_install_mode_record()``'s ``load_config()`` /
        ``get_credential()`` calls raised ``yaml.YAMLError`` uncaught, and
        this call site (unlike ``upgrade_finish.py``'s) had no try/except —
        the exception propagated out of ``upgrade()``'s outer try and the
        non-auto path re-raised, so plain ``nx upgrade`` reported failure
        even though the real upgrade had already succeeded.
        """
        cfg = self._cfg_dir(tmp_path, monkeypatch)
        # Unclosed flow sequence: yaml.safe_load raises yaml.parser.ParserError.
        (cfg / "config.yml").write_text("foo: [1, 2\n")
        result = runner.invoke(main, ["upgrade"])
        assert result.exit_code == 0, result.output
