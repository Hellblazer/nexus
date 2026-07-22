# SPDX-License-Identifier: AGPL-3.0-or-later
"""Stranded-install detector (nexus-gynt2, N+1 P4b prerequisite).

After P4b deletes Chroma + the migration tool (and RDR-158 P4 deletes
SQLite T2), a 4.x/5.x/early-6.x box that pip-upgrades DIRECTLY to N+1
would otherwise get a fresh empty PG install beside its unmigrated
``chroma/``, ``t2.db``, ``memory.db``, ``catalog/.catalog.db`` —
indistinguishable from data loss. The detector trips LOUD with the
literal two-hop instruction instead.

The detector ships DISARMED in every migration-capable release
(``LAST_MIGRATION_CAPABLE is None``): on those releases the migration
ladder exists in-place, ``memory.db``/``.catalog.db`` are
still LIVE stores, and tripping would false-positive every healthy box
(and the fresh-install MVV). Stamping the constant at N+1 cut time arms
every entry point at once — the same one-constant discipline as
``REQUIRED_ENGINE_VERSION``.
"""

from __future__ import annotations

import ast
import json
import os
import pathlib
import tomllib
from importlib.metadata import version as _dist_version
from pathlib import Path
from types import SimpleNamespace

import pytest
from click.testing import CliRunner

import nexus.commands.init as init_mod
import nexus.stranded_install as stranded_install
import nexus.upgrade_finish as upgrade_finish
from nexus.mcp._first_run import apply_stranded_notice
from nexus.cli import main
from nexus.commands.init import init_cmd
from nexus.config import detect_stranded_install_default
from nexus.health import _check_stranded_install
from nexus.stranded_install import (
    LAST_MIGRATION_CAPABLE,
    STAMP_FILENAME,
    StrandedInstall,
    detect_stranded_install,
)

_SRC_ROOT = Path(__file__).resolve().parent.parent / "src" / "nexus"

_PIN = "6.16.0"  # an arbitrary armed value for tests — NOT the real constant


@pytest.fixture()
def dirs(tmp_path: Path) -> tuple[Path, Path, Path]:
    """(config_dir, chroma_dir, catalog_dir) — all existing, all empty."""
    config = tmp_path / "config"
    chroma = tmp_path / "chroma"
    catalog = config / "catalog"
    for d in (config, chroma, catalog):
        d.mkdir(parents=True)
    return config, chroma, catalog


def _write_report(config_dir: Path, *, verification: str | None, total_failed: int = 0,
                  name: str = "migration-aaaa1111.json", body: str | None = None) -> Path:
    reports = config_dir / "migration-reports"
    reports.mkdir(exist_ok=True)
    path = reports / name
    if body is not None:
        path.write_text(body)
        return path
    report: dict = {"summary": {"total_failed": total_failed}}
    if verification is not None:
        report["verification"] = verification
    path.write_text(json.dumps(report))
    return path


class TestLeafContract:
    def test_module_is_stdlib_only_leaf(self) -> None:
        """stranded_install.py must import cleanly with zero ``nexus.*`` deps —
        it is imported by ``nexus.config`` (the default-path assembler) and,
        through it, by CLI/doctor/MCP startup; a nexus import here risks the
        circular-import class engine_version.py's leaf contract guards
        against."""
        src = pathlib.Path(stranded_install.__file__).read_text()
        tree = ast.parse(src)
        for node in ast.walk(tree):
            if isinstance(node, ast.ImportFrom) and node.module and node.module.split(".")[0] == "nexus":
                raise AssertionError(f"stranded_install.py imports from nexus: {node.module}")
            if isinstance(node, ast.Import):
                for alias in node.names:
                    if alias.name.split(".")[0] == "nexus":
                        raise AssertionError(f"stranded_install.py imports nexus: {alias.name}")

    def test_zero_chromadb_or_sqlite3_imports(self) -> None:
        """The bead's hard requirement: detection is PURE file stats. At N+1
        neither chromadb nor the sqlite3-backed stores exist any more — an
        import here would crash the very detector meant to explain their
        absence. Filenames like ``chroma.sqlite3`` are strings, not imports
        (the 19svb inverse-grep must not flag them)."""
        src = pathlib.Path(stranded_install.__file__).read_text()
        tree = ast.parse(src)
        banned = {"chromadb", "sqlite3"}
        for node in ast.walk(tree):
            if isinstance(node, ast.ImportFrom) and node.module and node.module.split(".")[0] in banned:
                raise AssertionError(f"stranded_install.py imports {node.module}")
            if isinstance(node, ast.Import):
                for alias in node.names:
                    if alias.name.split(".")[0] in banned:
                        raise AssertionError(f"stranded_install.py imports {alias.name}")

    def test_stamp_filename_matches_upgrade_finish(self) -> None:
        """The leaf module duplicates the ``last_seen_version`` literal (it
        cannot import ``nexus.upgrade_finish``); this tripwire keeps the two
        in sync."""
        assert STAMP_FILENAME == upgrade_finish.STAMP_FILENAME


class TestDisarmedUntilNPlusOne:
    def test_constant_is_none_on_every_migration_capable_release(self) -> None:
        """TRIPWIRE: ``LAST_MIGRATION_CAPABLE`` stays ``None`` until the N+1
        cut (the release that deletes Chroma + the migration tool, RDR-155
        P4b). Arming it earlier would trip every healthy 6.x box, where
        ``memory.db`` / ``.catalog.db`` are still LIVE stores. Whoever flips
        this intentionally at N+1 updates this test in the same commit."""
        assert LAST_MIGRATION_CAPABLE is None

    def test_disarmed_returns_none_even_with_artifacts(self, dirs: tuple[Path, Path, Path]) -> None:
        config, chroma, catalog = dirs
        (config / "memory.db").write_bytes(b"x")
        (config / "t2.db").write_bytes(b"x")
        (chroma / "chroma.sqlite3").write_bytes(b"x")
        (catalog / ".catalog.db").write_bytes(b"x")
        assert detect_stranded_install(config, chroma, catalog, last_migration_capable=None) is None
        # And via the real (unstamped) constant:
        assert detect_stranded_install(config, chroma, catalog) is None


class TestArmedDetection:
    def _detect(self, dirs: tuple[Path, Path, Path]) -> StrandedInstall | None:
        config, chroma, catalog = dirs
        return detect_stranded_install(config, chroma, catalog, last_migration_capable=_PIN)

    def test_fresh_box_negative(self, dirs: tuple[Path, Path, Path]) -> None:
        """A virgin install has none of the pre-PG files — the armed detector
        must stay silent (the fresh-install MVV stays green at N+1)."""
        assert self._detect(dirs) is None

    def test_fresh_box_negative_with_missing_dirs(self, tmp_path: Path) -> None:
        config = tmp_path / "nope-config"
        assert detect_stranded_install(
            config, tmp_path / "nope-chroma", config / "catalog",
            last_migration_capable=_PIN,
        ) is None

    @pytest.mark.parametrize("artifact", [
        ("chroma", "chroma.sqlite3"),
        ("config", "t2.db"),
        ("config", "memory.db"),
        ("catalog", ".catalog.db"),
    ])
    def test_each_artifact_alone_trips(self, dirs: tuple[Path, Path, Path],
                                       artifact: tuple[str, str]) -> None:
        config, chroma, catalog = dirs
        base = {"config": config, "chroma": chroma, "catalog": catalog}[artifact[0]]
        (base / artifact[1]).write_bytes(b"x")
        result = self._detect(dirs)
        assert result is not None
        assert result.artifacts == (str(base / artifact[1]),)

    def test_migrated_box_negative(self, dirs: tuple[Path, Path, Path]) -> None:
        """Artifacts remain on disk (copy-not-move rollback sources) but a
        VERIFIED migration report means the data made it to PG — no trip."""
        config, chroma, catalog = dirs
        (config / "memory.db").write_bytes(b"x")
        _write_report(config, verification="verified", total_failed=0)
        assert self._detect(dirs) is None

    @pytest.mark.parametrize("verification,total_failed", [
        ("verified", 3),       # verified verdict but recorded failures
        ("mismatch", 0),
        ("indeterminate", 0),
        (None, 0),             # pre-6.2 legacy report: no verdict recorded
    ])
    def test_unverified_report_still_trips(self, dirs: tuple[Path, Path, Path],
                                           verification: str | None,
                                           total_failed: int) -> None:
        """Anything short of ``verification=="verified" && total_failed==0``
        is NOT proof of migration — fail closed (the nexus-r0esi
        never-silently-pass rule). Re-running the ladder on an
        actually-migrated box is a near-no-op re-verify; silence on an
        unmigrated one is data loss."""
        config, chroma, catalog = dirs
        (config / "memory.db").write_bytes(b"x")
        _write_report(config, verification=verification, total_failed=total_failed)
        assert self._detect(dirs) is not None

    def test_unreadable_report_trips(self, dirs: tuple[Path, Path, Path]) -> None:
        config, chroma, catalog = dirs
        (config / "memory.db").write_bytes(b"x")
        _write_report(config, verification=None, body="{not json")
        assert self._detect(dirs) is not None

    def test_newest_report_wins(self, dirs: tuple[Path, Path, Path]) -> None:
        """Recency is mtime-based (migration ids are random UUIDs — same rule
        as doctor's ``_newest_migration_report_path``). An old failed run
        followed by a newer verified run = migrated."""
        config, chroma, catalog = dirs
        (config / "memory.db").write_bytes(b"x")
        old = _write_report(config, verification="mismatch", name="migration-old1.json")
        os.utime(old, (1_000_000, 1_000_000))
        _write_report(config, verification="verified", name="migration-new1.json")
        assert self._detect(dirs) is None

    def test_stranded_box_positive_exact_message(self, dirs: tuple[Path, Path, Path]) -> None:
        config, chroma, catalog = dirs
        (config / "t2.db").write_bytes(b"x")
        (config / STAMP_FILENAME).write_text("5.2.0\n")
        result = self._detect(dirs)
        assert result is not None
        assert result.era == "5.2.0"
        assert result.pinned_release == _PIN
        assert result.message == (
            f"This install carries unmigrated pre-PG data from conexus 5.2.0 "
            f"({config / 't2.db'}). This conexus version no longer ships the "
            f"migration tool, so it cannot read or migrate that data — proceeding "
            f"would look like an empty install, not data loss; nothing has been "
            f"touched. Two-hop upgrade: (1) install conexus=={_PIN} "
            f"(`uv tool install conexus=={_PIN}` or `pip install conexus=={_PIN}`), "
            f"(2) run `nx upgrade` there to migrate the data, "
            f"(3) upgrade back to this version."
        )

    def test_era_fallback_when_stamp_missing(self, dirs: tuple[Path, Path, Path]) -> None:
        """A box that never ran the stamped CLI (or predates the stamp) still
        gets the full redirect — era is advisory, never a gate."""
        config, chroma, catalog = dirs
        (config / "memory.db").write_bytes(b"x")
        result = self._detect(dirs)
        assert result is not None
        assert result.era is None
        assert "an earlier, pre-PG conexus release" in result.message
        assert f"conexus=={_PIN}" in result.message

    def test_artifact_dirs_do_not_trip(self, dirs: tuple[Path, Path, Path]) -> None:
        """Only FILES count — a stray empty directory named like an artifact
        is not pre-PG data."""
        config, chroma, catalog = dirs
        (config / "memory.db").mkdir()
        assert self._detect(dirs) is None

    def test_era_clobbered_by_own_version_stamp_is_unknown(self, dirs: tuple[Path, Path, Path]) -> None:
        """Critique 21029 Critical 1: check_version_transition rewrites the
        stamp to the RUNNING version on the first invocation after the direct
        hop onto N+1 — a stamp equal to this install's own version is that
        clobber's signature, and reporting it as the pre-PG era would make
        the message self-contradictory. It must degrade to the fallback
        clause instead."""
        config, chroma, catalog = dirs
        (config / "t2.db").write_bytes(b"x")
        (config / STAMP_FILENAME).write_text(_dist_version("conexus") + "\n")
        result = self._detect(dirs)
        assert result is not None
        assert result.era is None
        assert "an earlier, pre-PG conexus release" in result.message
        assert f"data from conexus {_dist_version('conexus')}" not in result.message

    def test_later_unverified_report_reverts_to_stranded(self, dirs: tuple[Path, Path, Path]) -> None:
        """DELIBERATE fail-closed choice (critique 21029, Significant 2): a
        box that once verified but whose NEWEST report is unverified (a
        later partial/interrupted rerun) trips again. The newest report is
        the current statement of migration state; the redirect's cost on a
        genuinely-migrated box is a near-no-op re-verify, while trusting a
        stale verified verdict over a newer failed rerun could silence a
        real gap. Newest-by-mtime, both directions."""
        config, chroma, catalog = dirs
        (config / "memory.db").write_bytes(b"x")
        old = _write_report(config, verification="verified", name="migration-old1.json")
        os.utime(old, (1_000_000, 1_000_000))
        _write_report(config, verification="mismatch", name="migration-new1.json")
        assert self._detect(dirs) is not None


class TestWiring:
    """The four entry points from the bead spec: nx init, first CLI run,
    MCP startup, nx doctor. Behavioral tests where cheap; a source census
    holds the full set (same pattern as test_kmo9h_catalog_gate_census)."""

    #: Wiring markers: an entry point counts as wired when it calls either
    #: the config assembler directly (CLI/init/doctor surfaces) or the MCP
    #: instructions-channel wrapper (which calls the assembler internally).
    _WIRING_MARKERS = ("detect_stranded_install_default", "apply_stranded_notice")

    #: Console-script modules with a rationaled exemption from wiring.
    #: nexus._session_end_launcher: fire-and-forget SessionEnd daemonizer
    #: with a hard pre-fork budget invariant (near-zero cost before
    #: os.fork(), see its module docstring) — it is a shutdown path, not a
    #: user-facing entry surface; the CLI/MCP/doctor wirings cover the box.
    _CENSUS_EXEMPT = {"nexus._session_end_launcher"}

    def test_every_console_script_entry_is_wired(self) -> None:
        """Census derived from ``pyproject.toml`` ``[project.scripts]`` — the
        ground truth for entry points — so a forgotten console script fails
        here instead of shipping unwired (critique 21029 Critical 2/3: the
        original hand-typed list silently omitted nx-mcp-catalog, repeating
        the nexus-4xgfy dual-MCP-entrypoint bug class)."""
        pyproject = tomllib.loads((_SRC_ROOT.parent.parent / "pyproject.toml").read_text())
        scripts: dict[str, str] = pyproject["project"]["scripts"]
        assert scripts, "pyproject [project.scripts] disappeared?"
        for name, target in scripts.items():
            module = target.split(":")[0]
            if module in self._CENSUS_EXEMPT:
                continue
            rel = module.removeprefix("nexus.").replace(".", "/") + ".py"
            src = (_SRC_ROOT / rel).read_text()
            assert any(m in src for m in self._WIRING_MARKERS), (
                f"console script `{name}` ({rel}) has no stranded-install "
                f"wiring (nexus-gynt2): every user-facing entry point must "
                f"call one of {self._WIRING_MARKERS}, or be added to "
                f"_CENSUS_EXEMPT with a rationale"
            )

    @pytest.mark.parametrize("rel", ["commands/init.py", "health.py", "mcp/catalog.py"])
    def test_non_script_surfaces_wire_detector(self, rel: str) -> None:
        """Surfaces not enumerable from [project.scripts]: the init command
        body, the doctor check, and the catalog MCP server (reached via its
        console script, but pinned here too for symmetry with core)."""
        src = (_SRC_ROOT / rel).read_text()
        assert any(m in src for m in self._WIRING_MARKERS), (
            f"{rel} lost its stranded-install wiring (nexus-gynt2)"
        )

    def test_config_default_assembler(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        """``nexus.config.detect_stranded_install_default`` resolves the three
        real path roots (config dir, chroma dir, catalog dir) and delegates."""
        config = tmp_path / "cfg"
        chroma = tmp_path / "chroma-data"
        config.mkdir()
        chroma.mkdir()
        monkeypatch.setenv("NEXUS_CONFIG_DIR", str(config))
        monkeypatch.setenv("NX_LOCAL_CHROMA_PATH", str(chroma))
        monkeypatch.delenv("NEXUS_CATALOG_PATH", raising=False)
        (chroma / "chroma.sqlite3").write_bytes(b"x")
        monkeypatch.setattr(stranded_install, "LAST_MIGRATION_CAPABLE", _PIN)
        result = detect_stranded_install_default()
        assert result is not None
        assert result.artifacts == (str(chroma / "chroma.sqlite3"),)
        # Disarmed (the real constant) → None even with the artifact present.
        monkeypatch.setattr(stranded_install, "LAST_MIGRATION_CAPABLE", None)
        assert detect_stranded_install_default() is None

    def test_doctor_check_trips_when_stranded(self, tmp_path: Path,
                                              monkeypatch: pytest.MonkeyPatch) -> None:
        config = tmp_path / "cfg"
        config.mkdir()
        monkeypatch.setenv("NEXUS_CONFIG_DIR", str(config))
        monkeypatch.setenv("NX_LOCAL_CHROMA_PATH", str(tmp_path / "chroma-none"))
        monkeypatch.delenv("NEXUS_CATALOG_PATH", raising=False)
        (config / "t2.db").write_bytes(b"x")
        monkeypatch.setattr(stranded_install, "LAST_MIGRATION_CAPABLE", _PIN)
        results = _check_stranded_install()
        assert len(results) == 1
        assert results[0].ok is False
        assert results[0].fatal is True
        assert "unmigrated pre-PG data" in results[0].detail
        assert any(f"conexus=={_PIN}" in s for s in results[0].fix_suggestions)

    def test_doctor_check_disarmed_is_ok(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setattr(stranded_install, "LAST_MIGRATION_CAPABLE", None)
        results = _check_stranded_install()
        assert len(results) == 1
        assert results[0].ok is True
        assert "disarmed" in results[0].detail

    def test_init_refuses_when_stranded(self, tmp_path: Path,
                                        monkeypatch: pytest.MonkeyPatch) -> None:
        """``nx init`` on a stranded box must exit non-zero with the two-hop
        message BEFORE provisioning anything — never a fresh empty install
        beside unmigrated data."""
        config = tmp_path / "cfg"
        config.mkdir()
        monkeypatch.setenv("NEXUS_CONFIG_DIR", str(config))
        monkeypatch.setenv("NX_LOCAL_CHROMA_PATH", str(tmp_path / "chroma-none"))
        monkeypatch.delenv("NEXUS_CATALOG_PATH", raising=False)
        (config / "memory.db").write_bytes(b"x")
        monkeypatch.setattr(stranded_install, "LAST_MIGRATION_CAPABLE", _PIN)
        # Regression guard: if the refusal is ever lost, fail FAST here
        # instead of falling through into real provisioning (the mutation
        # run showed that path attempts actual PG/service setup).

        def _must_not_reach() -> str:
            raise AssertionError("init proceeded past the stranded-install refusal")

        monkeypatch.setattr(init_mod, "_resolve_init_mode", _must_not_reach)
        runner = CliRunner()
        result = runner.invoke(init_cmd, ["--yes"], obj={})
        assert result.exit_code == 1
        assert "unmigrated pre-PG data" in result.output
        assert f"conexus=={_PIN}" in result.output

    def test_cli_startup_banner_when_stranded(self, tmp_path: Path,
                                              monkeypatch: pytest.MonkeyPatch) -> None:
        """First (and every) CLI invocation on a stranded box prints the loud
        banner to stderr — correctness class, no stamp-once suppression."""
        config = tmp_path / "cfg"
        config.mkdir()
        monkeypatch.setenv("NEXUS_CONFIG_DIR", str(config))
        monkeypatch.setenv("NX_LOCAL_CHROMA_PATH", str(tmp_path / "chroma-none"))
        monkeypatch.delenv("NEXUS_CATALOG_PATH", raising=False)
        (config / "t2.db").write_bytes(b"x")
        monkeypatch.setattr(stranded_install, "LAST_MIGRATION_CAPABLE", _PIN)
        # Neutralize the unrelated startup hooks (finish-pass would otherwise
        # see a fresh stamp dir and run its restart choreography).
        monkeypatch.setattr(upgrade_finish, "check_version_transition", lambda _dir: None)
        runner = CliRunner()
        result = runner.invoke(main, ["doctor", "--help"], obj={})
        assert "[stranded-install]" in result.output
        assert f"conexus=={_PIN}" in result.output

    def test_cli_banner_era_survives_real_transition_order(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """Critique 21029 Critical 1, exercised WITHOUT neutralizing
        check_version_transition: the group callback runs the detector
        BEFORE the upgrade-finish trigger, so the first invocation after a
        direct hop reports the TRUE pre-PG era even though the trigger
        clobbers the stamp in the same invocation; the second invocation
        (stamp now = running version) degrades to the fallback clause and
        never claims the running version as the era. Safe to run for real:
        in a dev checkout check_version_transition stamps and returns
        before any restart logic (running_from_tool_install() is False)."""
        config = tmp_path / "cfg"
        config.mkdir()
        monkeypatch.setenv("NEXUS_CONFIG_DIR", str(config))
        monkeypatch.setenv("NX_LOCAL_CHROMA_PATH", str(tmp_path / "chroma-none"))
        monkeypatch.delenv("NEXUS_CATALOG_PATH", raising=False)
        (config / "t2.db").write_bytes(b"x")
        (config / STAMP_FILENAME).write_text("5.2.0\n")
        monkeypatch.setattr(stranded_install, "LAST_MIGRATION_CAPABLE", _PIN)
        runner = CliRunner()

        first = runner.invoke(main, ["doctor", "--help"], obj={})
        assert "unmigrated pre-PG data from conexus 5.2.0" in first.output

        # The un-neutralized trigger has now rewritten the stamp to the
        # running version — the exact clobber the era guard exists for.
        running = _dist_version("conexus")
        assert (config / STAMP_FILENAME).read_text().strip() == running

        second = runner.invoke(main, ["doctor", "--help"], obj={})
        assert "[stranded-install]" in second.output
        assert "an earlier, pre-PG conexus release" in second.output
        assert f"unmigrated pre-PG data from conexus {running}" not in second.output

    def test_apply_stranded_notice_armed(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """The MCP LOUD surface: the notice lands in the server
        `instructions` string (the channel MCP-only users actually see —
        structlog alone is invisible to them), preserving any existing
        instructions."""
        config = tmp_path / "cfg"
        config.mkdir()
        monkeypatch.setenv("NEXUS_CONFIG_DIR", str(config))
        monkeypatch.setenv("NX_LOCAL_CHROMA_PATH", str(tmp_path / "chroma-none"))
        monkeypatch.delenv("NEXUS_CATALOG_PATH", raising=False)
        (config / "memory.db").write_bytes(b"x")
        monkeypatch.setattr(stranded_install, "LAST_MIGRATION_CAPABLE", _PIN)

        server = SimpleNamespace(_mcp_server=SimpleNamespace(instructions=None))
        assert apply_stranded_notice(server) is True
        assert "STRANDED INSTALL" in server._mcp_server.instructions
        assert f"conexus=={_PIN}" in server._mcp_server.instructions

        prior = SimpleNamespace(_mcp_server=SimpleNamespace(instructions="keep me"))
        assert apply_stranded_notice(prior) is True
        assert prior._mcp_server.instructions.startswith("keep me\n\n")

    def test_apply_stranded_notice_disarmed(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        config = tmp_path / "cfg"
        config.mkdir()
        monkeypatch.setenv("NEXUS_CONFIG_DIR", str(config))
        monkeypatch.setenv("NX_LOCAL_CHROMA_PATH", str(tmp_path / "chroma-none"))
        monkeypatch.delenv("NEXUS_CATALOG_PATH", raising=False)
        (config / "memory.db").write_bytes(b"x")
        monkeypatch.setattr(stranded_install, "LAST_MIGRATION_CAPABLE", None)
        server = SimpleNamespace(_mcp_server=SimpleNamespace(instructions="untouched"))
        assert apply_stranded_notice(server) is False
        assert server._mcp_server.instructions == "untouched"
