# SPDX-License-Identifier: AGPL-3.0-or-later
"""nexus-f3yqd.2.23: engine convergence also refreshes the extracted PG bundle.

An install first set up on a conexus that shipped a bundle without pg_background
(engine-service-v0.1.189 and earlier) kept that bundle for good: ``nx upgrade``
installed the engine binary and never touched ``<config>/pg-bundle``. These tests drive
``converge_engine`` against REAL files under ``tmp_path`` (a tiny synthetic ``.txz`` for
the release asset, a real extracted tree for the install) and stub only the network
download (``_fetch_verified``, the one download-and-verify seam) and the stop/start of
the stack (``default_ops``), so the swap, the marker and the major-version guard all run.
"""
from __future__ import annotations

import contextlib
import hashlib
import inspect
import sys
import tarfile
from contextlib import ExitStack
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

import pytest
from click.testing import CliRunner

import nexus.commands.upgrade as up
from nexus.cli import main
from nexus.commands import daemon as daemon_cmds
from nexus.commands.daemon import service_install_binary_cmd
import nexus.bounded_subprocess as bs
from nexus.daemon import binary_install as b
from nexus.daemon import replace_quiesce as rq
from nexus.daemon import pg_bundle_refresh
from nexus.daemon.pg_bundle_refresh import bundle_has_pg_background, install_and_extract_pg_bundle
from nexus.db import pg_bundle
from nexus.db.pg_provision import _bundle_lib_env
from nexus.engine_version import REQUIRED_ENGINE_VERSION
from nexus.upgrade_finish import PoisonProbe, converge_engine
from nexus.upgrade_ladder.preconditions import PreconditionReport
from nexus.upgrade_finish import RunningEngine as _RunningEngine

_REQUIRED_STR = ".".join(str(p) for p in REQUIRED_ENGINE_VERSION)
_PINNED_TAG = "engine-service-v" + _REQUIRED_STR
_EXE = ".exe" if sys.platform == "win32" else ""  # the names PgBinaries looks for on this host
_FAILS = "raise SystemExit(127)\n"  # a pg_ctl that does not run
_CONTROL = Path("bundle") / "share" / "postgresql" / "extension" / "pg_background.control"


def _older_version_str() -> str:
    major, minor, patch = REQUIRED_ENGINE_VERSION
    return f"{major}.{minor}.{patch - 1}" if patch > 0 else f"{major}.{max(minor - 1, 0)}.999"


def _txz(
    dest_dir: Path, name: str, *, background: bool, pg_version: str = "17.5", variant: str = "",
    pg_ctl_body: str | None = None,
) -> Path:
    """A tiny bundle shaped like the real artifact: bin/ stubs, the relocation marker and,
    when *background* is set, the pg_background control file where the real bundle has it
    (``share/postgresql/extension/``)."""
    stage = dest_dir / f"_stage_{name}"
    bundle = stage / "bundle"
    (bundle / "bin").mkdir(parents=True)
    for tool in ("initdb", "pg_ctl", "psql", "createdb"):
        f = bundle / "bin" / (tool + _EXE)
        f.write_text("", encoding="utf-8")  # the binaries' presence is what extraction checks; pg_ctl's answer is below
    (bundle / "bin" / "pg_ctl.py").write_text(
        pg_ctl_body if pg_ctl_body is not None else f'print("pg_ctl (PostgreSQL) {pg_version}")\n', encoding="utf-8",
    )
    ext = bundle / "share" / "postgresql" / "extension"
    ext.mkdir(parents=True)
    (ext / "vector.control").write_text("default_version = '0.8.0'\n", encoding="utf-8")
    if background:
        (ext / "pg_background.control").write_text("default_version = '1.0'\n", encoding="utf-8")
    (bundle / "lib").mkdir()
    if variant:
        (bundle / "share" / "VARIANT").write_text(variant + "\n", encoding="utf-8")
    (bundle / ".build_prefix").write_text("/build/prefix/nexus-pg\n", encoding="utf-8")
    archive = dest_dir / name
    with tarfile.open(archive, "w:xz") as tf:
        tf.add(bundle, arcname="bundle")
    return archive


@pytest.fixture(autouse=True)
def _pg_ctl_runs_its_script(monkeypatch):
    """The stub ``pg_ctl`` of a synthetic bundle is a Python script beside the (empty) binary, run through
    ``sys.executable``: a shell script cannot be an ``.exe`` on Windows. Everything else about the probe is
    real: the child, its environment, its exit code and output."""
    real = bs.run_bounded

    def _run(argv, **kw):
        head = Path(argv[0])
        if head.stem == "pg_ctl" and head.with_name("pg_ctl.py").is_file():
            argv = [sys.executable, str(head.with_name("pg_ctl.py")), *argv[1:]]
        return real(argv, **kw)

    monkeypatch.setattr(bs, "run_bounded", _run)


class _Stack:
    """The stop/start effects of the stack, recorded."""

    def __init__(self, events: list[str] | None = None) -> None:
        self.events: list[str] = events if events is not None else []

    def ops(self, _config_dir: Path) -> rq.QuiesceOps:
        def stop_service():
            self.events.append("stop_service")
            return SimpleNamespace(pids=(4242,), refused=(), stubborn=(), unsignalled=())

        return rq.QuiesceOps(
            stop_service,
            lambda: True,
            lambda: self.events.append("stop_pg"),
            lambda: self.events.append("start_pg"),
            lambda: self.events.append("start_service"),
        )


class _World:
    """A local-mode config dir with an extracted bundle, a release carrying another, the
    stack's effects recorded and the download counted."""

    def __init__(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        self.cfg = tmp_path / "cfg"
        self.cfg.mkdir()
        (self.cfg / "pg_credentials").write_text("NX_DB_URL=postgresql://x/nexus\n", encoding="utf-8")
        self.release = tmp_path / "release"
        self.release.mkdir()
        self.work = tmp_path / "work"
        self.work.mkdir()
        self.stack = _Stack()
        self.events = self.stack.events  # the stack's effects and the downloads, in order
        self.downloads: list[str] = []
        self.download_error: Exception | None = None
        self.shipped: Path | None = None
        monkeypatch.setattr(rq, "default_ops", self.stack.ops)
        monkeypatch.setattr(b, "_fetch_verified", self._fetch)
        monkeypatch.setattr(pg_bundle_refresh, "_REFRESH_FAILURES", {})
        self.busy: str | None = None  # why the engine is busy, for the converged leg's idle gate
        monkeypatch.setattr(pg_bundle_refresh, "engine_busy_reason", lambda _cfg: self.busy)

    def _fetch(self, tag, name, tmp, checker):
        self.downloads.append(name)
        self.events.append("download")
        if self.download_error is not None:
            raise self.download_error
        assert self.shipped is not None, "the test shipped no release bundle"
        asset = tmp / name
        asset.write_bytes(self.shipped.read_bytes())
        digest = hashlib.sha256(asset.read_bytes()).hexdigest()
        return asset, digest, f"https://example.invalid/{tag}/{name}"

    @property
    def stops(self) -> list[str]:
        """The stack's stop and start effects, without the download and extraction markers."""
        return [e for e in self.events if e not in ("download", "extract")]

    @property
    def root(self) -> Path:
        return pg_bundle.bundle_extract_root(self.cfg)

    def install_old(self, cluster: str | None = "17", **kw) -> None:
        """Extract an already-installed bundle (a first extraction stops nothing) and, by default,
        a data directory of PostgreSQL *cluster* (what the major guard reads)."""
        pg_bundle.extract_bundle(_txz(self.work, f"old{sum(1 for _ in self.work.iterdir())}.txz", **kw), self.root)
        if cluster is not None:
            (self.cfg / "postgres").mkdir(exist_ok=True)
            (self.cfg / "postgres" / "PG_VERSION").write_text(cluster + "\n", encoding="utf-8")

    def ship(self, **kw) -> None:
        self.shipped = _txz(self.release, "nexus-pg-release.txz", **kw)

    def has_background(self) -> bool:
        return (self.root / _CONTROL).is_file()

    def marker(self) -> str:
        return (self.root / pg_bundle._EXTRACT_MARKER).read_text(encoding="utf-8")


@pytest.fixture
def world(tmp_path, monkeypatch) -> _World:
    return _World(tmp_path, monkeypatch)


def _converged(cfg: Path, *, version: str = _REQUIRED_STR) -> dict:
    """A receipt backed by a real binary on disk: the box is converged."""
    binary = b.well_known_binary_path(cfg)  # nexus-service.exe on Windows
    binary.parent.mkdir(parents=True, exist_ok=True)
    payload = b"native-engine-image"
    binary.write_bytes(payload)
    return {"version": version, "tag": "engine-service-v" + version, "sha256": hashlib.sha256(payload).hexdigest()}


def _converge(world: _World, *, engine: str = "converged", local: bool = True, **kw):
    """converge_engine with the engine facts stubbed; returns (actions, install_binary mock, run_bounded mock)."""
    prov = _converged(world.cfg) if engine == "converged" else {"version": _older_version_str()}
    install = MagicMock(return_value=(world.cfg / "service" / "nexus-service", {"version": _REQUIRED_STR}))
    cycle = MagicMock(return_value=MagicMock(returncode=0))
    with patch("nexus.config.is_local_mode", return_value=local), \
            patch("nexus.daemon.binary_lifecycle.read_installed_provenance", return_value=prov), \
            patch("nexus.upgrade_finish._poison_probe", return_value=PoisonProbe()), \
            patch("nexus.daemon.binary_install.install_binary", install), \
            patch("nexus.upgrade_finish.run_bounded", cycle), \
            patch("nexus.upgrade_finish._running_engine", return_value=_RunningEngine(up=True, version=REQUIRED_ENGINE_VERSION)):
        actions = converge_engine(world.cfg, **kw)
    return actions, install, cycle


def _service_starts(cycle: MagicMock) -> int:
    return sum(1 for c in cycle.call_args_list if c.args and c.args[0][:4] == ["nx", "daemon", "service", "start"])


# -- the control file the check reads ---------------------------------------------------


def test_the_real_pg_background_control_path_is_under_share_postgresql_extension(world):
    """Verified against the shipped archive layout: the control files sit at
    ``bundle/share/postgresql/extension/`` (``vector.control`` beside it), not
    ``bundle/share/extension/``."""
    world.install_old(background=False)
    assert bundle_has_pg_background(world.root) is False
    assert (world.root / "bundle/share/postgresql/extension/vector.control").is_file()
    (world.root / _CONTROL).write_text("default_version = '1.0'\n", encoding="utf-8")
    assert bundle_has_pg_background(world.root) is True


# -- the gap: an old bundle gets replaced -----------------------------------------------


def test_converged_engine_with_an_old_bundle_gets_the_new_bundle_extracted(world):
    world.install_old(background=False, variant="old")
    world.ship(background=True, variant="new")
    assert not world.has_background()

    actions, install, cycle = _converge(world, engine="converged")

    install.assert_not_called()  # the engine is already the pinned one
    assert world.has_background()
    assert world.downloads == [b.pg_bundle_asset_name()]
    assert any("PostgreSQL bundle" in a for a in actions), actions
    # one stop, one start, PostgreSQL first, done by the shared guard
    assert world.stops == ["stop_service", "stop_pg", "start_pg", "start_service"]
    assert _service_starts(cycle) == 0


def test_an_engine_change_refreshes_the_bundle_under_the_one_stop(world):
    world.install_old(background=False, variant="old")
    world.ship(background=True, variant="new")

    actions, install, cycle = _converge(world, engine="older")

    install.assert_called_once()
    kwargs = install.call_args.kwargs
    assert kwargs["restart_after"] is False
    assert kwargs["quiesce"] is not None  # the engine joins the shared stop
    assert world.has_background()
    # the stack stops once; nothing starts it until the convergence cycle does, once
    assert world.stops == ["stop_service", "stop_pg"]
    assert _service_starts(cycle) == 1
    assert any("converged engine" in a for a in actions), actions


# -- no-ops ------------------------------------------------------------------------------


def test_an_unchanged_bundle_swaps_nothing_and_stops_nothing(world):
    world.install_old(background=True, variant="same")
    world.ship(background=True, variant="same")
    # the extracted tree IS the shipped archive's content: record the shipped bytes as its identity
    pg_bundle._write_marker(world.root, pg_bundle._archive_identity(world.shipped))
    marker = world.marker()

    actions, install, cycle = _converge(world, engine="older")  # engine change => a refresh is tried

    assert world.stops == []
    assert world.marker() == marker
    assert _service_starts(cycle) == 1  # only the engine convergence cycle


def test_a_converged_engine_with_pg_background_present_does_not_download(world):
    world.install_old(background=True)
    world.ship(background=True, variant="would-differ")

    actions, install, cycle = _converge(world, engine="converged")

    assert world.downloads == []
    assert world.stops == []
    assert actions == []


def test_cloud_mode_does_nothing(world):
    world.install_old(background=False)
    world.ship(background=True)

    actions, install, cycle = _converge(world, engine="older", local=False)

    assert actions == []
    assert world.downloads == [] and world.stops == []
    assert not world.has_background()


def test_a_self_managed_postgresql_install_does_nothing(world):
    """No extracted bundle on disk: the install runs its own PostgreSQL."""
    world.ship(background=True)
    assert not world.root.exists()

    actions, install, cycle = _converge(world, engine="converged")
    assert actions == [] and world.downloads == []

    actions, install, cycle = _converge(world, engine="older")
    install.assert_called_once()  # the engine converges as it always did
    assert world.downloads == [] and world.stops == []
    assert not world.root.exists()


def test_report_only_and_dry_run_never_download_or_stop(world):
    world.install_old(background=False)
    world.ship(background=True)

    actions, _install, _cycle = _converge(world, engine="converged", acquire=False)
    assert any("pg_background" in a for a in actions), actions
    assert world.downloads == [] and world.stops == []

    actions, _install, _cycle = _converge(world, engine="converged", dry_run=True)
    assert any("pending" in a and "PostgreSQL bundle" in a for a in actions), actions
    assert world.downloads == [] and world.stops == []
    assert not world.has_background()


# -- failures leave the old bundle and the engine convergence completes -------------------


def test_a_failed_download_leaves_the_old_bundle_and_the_engine_still_converges(world):
    world.install_old(background=False, variant="old")
    world.download_error = b.BinaryVerificationError("sha256 mismatch")
    marker = world.marker()

    actions, install, cycle = _converge(world, engine="older")

    install.assert_called_once()
    assert _service_starts(cycle) == 1
    assert any("converged engine" in a for a in actions), actions
    assert any("PostgreSQL bundle" in a and "sha256 mismatch" in a for a in actions), actions
    assert world.stops == []  # the download runs before anything is stopped
    assert not world.has_background() and world.marker() == marker
    assert pg_bundle.is_bundle_extracted(world.root)


def test_a_failed_download_on_a_converged_engine_is_reported_and_leaves_the_box_alone(world):
    world.install_old(background=False)
    world.download_error = b.BinaryDownloadError("connection reset")

    actions, install, cycle = _converge(world, engine="converged")

    install.assert_not_called()
    assert any("PostgreSQL bundle" in a and "connection reset" in a for a in actions), actions
    assert world.stops == [] and not world.has_background()


def test_a_failed_swap_restores_the_old_bundle_and_starts_the_stack_again(world, monkeypatch):
    world.install_old(background=False, variant="old")
    world.ship(background=True, variant="new")
    marker = world.marker()

    real = pg_bundle.replace_with_retry
    calls = {"n": 0}

    def _second_rename_fails(src, dst, **kw):
        calls["n"] += 1
        if calls["n"] == 2:  # staging -> extract_root, after the old tree moved aside
            raise OSError("disk went away")
        return real(src, dst, **kw)

    monkeypatch.setattr(pg_bundle, "replace_with_retry", _second_rename_fails)

    actions, _install, _cycle = _converge(world, engine="converged")

    assert any("PostgreSQL bundle" in a and "disk went away" in a for a in actions), actions
    assert not world.has_background() and world.marker() == marker
    assert pg_bundle.is_bundle_extracted(world.root)
    assert world.stops[-2:] == ["start_pg", "start_service"]  # a failed run never leaves the box down


# -- the major-version guard --------------------------------------------------------------


def test_a_swap_across_a_major_version_is_refused_before_anything_is_stopped(world):
    world.install_old(background=False, pg_version="17.5", variant="old")
    world.ship(background=True, pg_version="18.1", variant="new")
    marker = world.marker()

    actions, _install, _cycle = _converge(world, engine="converged")

    assert any("PostgreSQL bundle" in a and "major" in a for a in actions), actions
    assert world.stops == []
    assert not world.has_background() and world.marker() == marker
    assert not (world.root.parent / (world.root.name + ".incoming")).exists()  # the staged tree is gone


def test_extract_bundle_refuses_a_major_change_and_allows_the_same_major(tmp_path):
    root = tmp_path / "pg-bundle"
    pg_bundle.extract_bundle(_txz(tmp_path, "a.txz", background=False, pg_version="17.5", variant="a"), root)

    class _MustNotEnter:
        def __enter__(self):
            raise AssertionError("the swap was refused, so nothing may be stopped")

        def __exit__(self, *exc):
            return None

    with pytest.raises(pg_bundle.PgBundleMajorChangeError, match="17.*18"):
        pg_bundle.extract_bundle(
            _txz(tmp_path, "b.txz", background=True, pg_version="18.1", variant="b"), root, quiesce=_MustNotEnter(),
        )
    assert not (root / _CONTROL).exists()

    entered: list[str] = []

    class _Records:
        def __enter__(self):
            entered.append("in")

        def __exit__(self, *exc):
            return None

    pg_bundle.extract_bundle(
        _txz(tmp_path, "c.txz", background=True, pg_version="17.6", variant="c"), root, quiesce=_Records(),
    )
    assert entered == ["in"] and (root / _CONTROL).is_file()


# -- the shared function, as install-binary uses it ---------------------------------------


def test_the_shared_function_installs_then_extracts_and_a_repeat_swaps_nothing(world):
    world.ship(background=True)
    seen: list[Path] = []

    dest, prov, bin_dir = install_and_extract_pg_bundle(
        _PINNED_TAG, world.cfg, installed_by="test", quiesce=None,
        on_installed=lambda archive, _prov: seen.append(archive),
    )
    assert seen == [dest] == [b.pg_bundle_dest(world.cfg)]
    assert prov["asset"] == b.pg_bundle_asset_name()
    assert bin_dir == pg_bundle.bundle_bin_dir(world.root) and world.has_background()

    marker = world.marker()
    install_and_extract_pg_bundle(_PINNED_TAG, world.cfg, installed_by="test", quiesce=None)
    assert world.marker() == marker and world.stops == []


def test_install_binary_and_converge_share_the_one_function():
    """No copy: both call sites go through ``install_and_extract_pg_bundle``."""
    assert "install_and_extract_pg_bundle" in inspect.getsource(daemon_cmds._install_binary_steps)
    assert "install_and_extract_pg_bundle" in inspect.getsource(pg_bundle_refresh)
    assert "extract_bundle(" not in inspect.getsource(daemon_cmds._install_binary_steps)


# -- review round 1 (nexus-f3yqd.2.23): reachability, posture, ordering, guards ----------------------------


def _nx_upgrade(world: _World, monkeypatch, *args: str, engine: str = "converged"):
    """Run ``nx upgrade`` through the CLI entry point on *world*'s config dir. Everything that is not the
    engine/bundle axis is stubbed; the precondition stage, ``converge_preconditions`` and ``converge_engine`` are real."""
    monkeypatch.setenv("NEXUS_CONFIG_DIR", str(world.cfg))
    prov = _converged(world.cfg) if engine == "converged" else {"version": _older_version_str()}
    install = MagicMock(return_value=(world.cfg / "service" / "nexus-service", {"version": _REQUIRED_STR}))
    cycle = MagicMock(return_value=MagicMock(returncode=0))
    with ExitStack() as stack:
        for target, kw in (
            ("nexus.config.is_local_mode", {"return_value": True}),
            ("nexus.daemon.binary_lifecycle.read_installed_provenance", {"return_value": prov}),
            ("nexus.upgrade_finish._poison_probe", {"return_value": PoisonProbe()}),
            ("nexus.daemon.binary_install.install_binary", {"new": install}),
            ("nexus.upgrade_finish.run_bounded", {"new": cycle}),
            ("nexus.upgrade_finish._running_engine", {"return_value": _RunningEngine(up=True, version=REQUIRED_ENGINE_VERSION)}),
            ("nexus.commands.self_cmd.repair_uv_takeover", {"return_value": []}),
            ("nexus.upgrade_ladder.preconditions._plan_library_report", {"return_value": PreconditionReport(
                name="plan-library", applicable=False, current=True, detail="stubbed")}),
        ):
            stack.enter_context(patch(target, **kw))
        for name in (
            "_run_upgrade", "_run_ladder", "_converge_vc_runtime", "_converge_pdf_stack", "_converge_installer_rebuild",
            "_converge_plugins", "_refresh_generated_agents", "_migrate_repos_json_to_catalog",
            "_emit_name_vs_embed_dim_advisory", "_refresh_all_git_hooks", "_install_beads_prime_best_effort",
            "_cycle_supervised_daemons_to_current",
        ):
            stack.enter_context(patch.object(up, name))
        result = CliRunner().invoke(main, ["upgrade", *args])
    return result, install, cycle


def test_a_plain_nx_upgrade_refreshes_an_old_bundle_on_a_converged_engine(world, monkeypatch):
    """The review's blocker: the pinned-engine leg was reachable only from restart-stale, because the precondition
    stage called converge_engine only for an engine that was NOT converged."""
    world.install_old(background=False, variant="old")
    world.ship(background=True, variant="new")

    result, install, _cycle = _nx_upgrade(world, monkeypatch)

    assert result.exit_code == 0, result.output
    install.assert_not_called()
    assert world.has_background()
    assert "pg-bundle" in result.output and "PostgreSQL bundle" in result.output
    assert world.events == ["download", "stop_service", "stop_pg", "start_pg", "start_service"]


def test_nx_upgrade_dry_run_shows_the_bundle_pending_and_changes_nothing(world, monkeypatch):
    world.install_old(background=False)
    world.ship(background=True)

    result, _install, _cycle = _nx_upgrade(world, monkeypatch, "--dry-run")

    assert result.exit_code == 0, result.output
    assert "pg_background" in result.output and "nx upgrade" in result.output
    assert world.downloads == [] and world.events == [] and not world.has_background()


def test_nx_upgrade_auto_only_reports_and_never_swaps_the_bundle(world, monkeypatch):
    world.install_old(background=False)
    world.ship(background=True)

    result, _install, _cycle = _nx_upgrade(world, monkeypatch, "--auto")

    assert result.exit_code == 0
    assert world.downloads == [] and world.events == [] and not world.has_background()


# -- the converged leg never bounces PostgreSQL unattended or under work ---------------------------------


def test_the_converged_leg_does_not_run_unattended(world):
    world.install_old(background=False)
    world.ship(background=True)

    actions, _install, _cycle = _converge(world, engine="converged", unattended=True)

    assert any("nx upgrade" in a and "pg_background" in a for a in actions), actions
    assert world.downloads == [] and world.events == [] and not world.has_background()


def test_the_converged_leg_defers_while_the_engine_is_busy_and_says_so(world):
    world.install_old(background=False)
    world.ship(background=True)
    world.busy = "the engine reports background work (index build)"

    actions, _install, _cycle = _converge(world, engine="converged")

    assert any("index build" in a and "nx upgrade" in a for a in actions), actions
    assert world.downloads == [] and world.events == [] and not world.has_background()


def test_the_engine_change_path_does_not_wait_for_an_idle_engine(world):
    """The engine swap restarts the stack anyway; it keeps today's posture."""
    world.install_old(background=False)
    world.ship(background=True)
    world.busy = "the engine reports background work (index build)"

    _converge(world, engine="older")

    assert world.has_background()


def test_the_busy_check_reads_boot_work_and_index_builds(tmp_path, monkeypatch):
    """The idle rule of scripts/upgrade_boot_writes.py: boot_work empty and no index build or vacuum running."""
    status: dict | None = {"boot_work": []}
    up = {"v": True}
    rows: dict[str, int | None] = {"n": 0}
    monkeypatch.setattr(pg_bundle_refresh, "_engine_status", lambda: status)
    monkeypatch.setattr(pg_bundle_refresh, "_service_up", lambda _cfg: up["v"])
    monkeypatch.setattr(pg_bundle_refresh, "_progress_rows", lambda _cfg: rows["n"])

    assert pg_bundle_refresh.engine_busy_reason(tmp_path) is None
    status = {"boot_work": [{"id": "pci-build", "what": "building indexes"}]}
    assert "background work" in pg_bundle_refresh.engine_busy_reason(tmp_path)
    status, rows["n"] = {"boot_work": []}, 2
    assert "index build" in pg_bundle_refresh.engine_busy_reason(tmp_path)
    rows["n"] = None  # the progress views cannot be read while the engine is up: unknown is not idle
    assert "cannot" in pg_bundle_refresh.engine_busy_reason(tmp_path)
    status = None
    assert "cannot" in pg_bundle_refresh.engine_busy_reason(tmp_path)
    up["v"] = False  # no service running: nothing can be in flight
    assert pg_bundle_refresh.engine_busy_reason(tmp_path) is None


# -- the swap is staged before anything stops ---------------------------------------------------------------


def test_the_engine_change_path_downloads_and_stages_the_bundle_before_the_stack_stops(world, monkeypatch):
    world.install_old(background=False, variant="old")
    world.ship(background=True, variant="new")
    real = pg_bundle._extract_and_validate

    def _logged(archive, dest):
        world.events.append("extract")
        return real(archive, dest)

    monkeypatch.setattr(pg_bundle, "_extract_and_validate", _logged)

    _converge(world, engine="older")

    assert world.events[:2] == ["download", "extract"], world.events
    assert world.events[2:] == ["stop_service", "stop_pg"], world.events  # no download, no extraction while stopped
    assert world.has_background()


def test_a_staged_tree_carries_its_marker_before_the_swap(world):
    """A marker written after the swap leaves an unmarked tree when the write fails (disk full)."""
    world.install_old(background=False, variant="old")
    world.ship(background=True, variant="new")

    assert pg_bundle.prestage_bundle(world.shipped, world.root) is True

    staging = pg_bundle.staging_dir(world.root)
    marker = staging / pg_bundle._EXTRACT_MARKER
    assert marker.read_text(encoding="utf-8") == pg_bundle._archive_identity(world.shipped)
    assert pg_bundle.prestage_bundle(world.shipped, world.root) is True  # a repeat reuses the staged tree
    pg_bundle.extract_bundle(world.shipped, world.root, quiesce=pg_bundle_refresh_noop())
    assert world.has_background() and not staging.exists()
    assert world.marker() == pg_bundle._archive_identity(world.shipped)


def pg_bundle_refresh_noop():
    return contextlib.nullcontext()


# -- the major guard fails closed -----------------------------------------------------------------------


def test_the_guard_reads_the_data_directory_when_the_bundle_in_service_cannot_run(world):
    """Review: an in-service pg_ctl that does not run read as 'no major known' and the guard let a 17 -> 18 swap by."""
    world.install_old(background=False, cluster="17", variant="old", pg_ctl_body=_FAILS)
    world.ship(background=True, pg_version="18.1", variant="new")

    actions, _install, _cycle = _converge(world, engine="converged")

    assert any("major" in a for a in actions), actions
    assert world.stops == [] and not world.has_background()


def test_a_staged_tree_whose_major_cannot_be_read_is_refused(world):
    world.install_old(background=False, cluster="17", variant="old")
    world.ship(background=True, variant="new", pg_ctl_body=_FAILS)

    actions, _install, _cycle = _converge(world, engine="converged")

    assert any("PostgreSQL bundle" in a for a in actions), actions
    assert world.stops == [] and not world.has_background()


def test_the_major_probe_gives_the_new_tree_its_own_libraries(tmp_path):
    """The published bundles carry no RPATH: a bare pg_ctl --version exits 127 on a minimal Linux base. The stub
    answers only when the child's loader path names the tree's own lib/ (Windows loads DLLs from the exe's own
    directory and sets no loader variable, so there the stub answers and ``_bundle_lib_env`` is the identity)."""
    root = tmp_path / "tree"
    (root / "bin").mkdir(parents=True)
    (root / "lib").mkdir()
    for tool in ("initdb", "pg_ctl", "psql", "createdb"):
        (root / "bin" / (tool + _EXE)).write_text("", encoding="utf-8")
    (root / "bin" / "pg_ctl.py").write_text(
        "import os, sys\n"
        "from pathlib import Path\n"
        "lib = Path(sys.argv[0]).resolve().parent.parent / 'lib'\n"
        "ok = os.name == 'nt' or str(lib) in os.environ.get('LD_LIBRARY_PATH', '').split(os.pathsep)\n"
        "print('pg_ctl (PostgreSQL) 17.5') if ok else sys.exit(127)\n", encoding="utf-8",
    )
    assert pg_bundle.bundle_major(root / "bin") == 17
    if sys.platform == "win32":
        assert _bundle_lib_env([str(root / "bin" / "pg_ctl.exe")], {"A": "1"}) == {"A": "1"}


def test_a_major_refusal_has_its_own_message_and_no_retry_advice(world, tmp_path):
    world.install_old(background=False, cluster="17", variant="old")
    world.ship(background=True, pg_version="18.1", variant="new")
    with patch("nexus.daemon.binary_install.install_binary", MagicMock(return_value=(
            world.cfg / "service" / "nexus-service",
            {"asset": "x", "version": "0", "sha256": "a" * 64, "source_url": "u"}))):
        result = CliRunner().invoke(service_install_binary_cmd, [_PINNED_TAG, "--config-dir", str(world.cfg)])

    assert result.exit_code == 2
    out = result.output.lower()
    assert "major" in out and "pg_upgrade" in out
    assert "re-run" not in out and "retry" not in out and "tries again" not in out
    assert not world.has_background()
    actions, _i, _c = _converge(world, engine="converged")
    assert all("tries again" not in a and "retry" not in a.lower() for a in actions if "major" in a), actions


# -- verify, once, that the refresh brought pg_background ------------------------------------------------


def test_a_pinned_bundle_without_pg_background_is_reported_and_not_downloaded_again(world, monkeypatch):
    world.install_old(background=False, variant="old")
    world.ship(background=False, variant="new")  # the release's own bundle lacks it

    first, _i, _c = _converge(world, engine="converged")
    assert any("pg_background" in a and "does not contain" in a for a in first), first
    assert len(world.downloads) == 1

    monkeypatch.setattr(pg_bundle_refresh, "_REFRESH_FAILURES", {})  # a new process: only the disk remembers
    second, _i, _c = _converge(world, engine="converged")
    assert len(world.downloads) == 1, "the bundle of the pinned tag is already installed: no second download"
    assert any("pg_background" in a and "does not contain" in a for a in second), second
    assert all("tries again" not in a for a in first + second)


def test_a_swap_that_drops_pg_background_is_refused(world):
    world.install_old(background=True, variant="old")
    world.ship(background=False, variant="downgrade")

    actions, _i, _c = _converge(world, engine="older")

    assert any("pg_background" in a and "not swapped" in a for a in actions), actions
    assert world.has_background()  # the old tree stays
    assert "stop_pg" not in world.events


# -- install-binary: a download-stage OSError propagates as it did ------------------------------------------


def test_install_binary_lets_a_download_stage_oserror_propagate(world, monkeypatch):
    def _disk_full(tag, config_dir, *, installed_by=""):
        raise OSError(28, "No space left on device")

    monkeypatch.setattr(b, "install_pg_bundle", _disk_full)
    with patch("nexus.daemon.binary_install.install_binary", MagicMock(return_value=(
            world.cfg / "service" / "nexus-service",
            {"asset": "x", "version": "0", "sha256": "a" * 64, "source_url": "u"}))):
        result = CliRunner().invoke(service_install_binary_cmd, [_PINNED_TAG, "--config-dir", str(world.cfg)])

    assert isinstance(result.exception, OSError), (result.exception, result.output)
    assert "extracting it failed" not in result.output


# -- the doctor row --------------------------------------------------------------------------------------


def test_doctor_warns_while_the_extracted_bundle_lacks_pg_background(world):
    world.install_old(background=False)
    with patch("nexus.config.is_local_mode", return_value=True):
        rows = pg_bundle_refresh.check_pg_bundle(world.cfg)
    assert len(rows) == 1
    row = rows[0]
    assert row.ok is False and row.warn is True and "pg_background" in row.detail
    assert any("nx upgrade" in f for f in row.fix_suggestions)
    assert all("install-binary" not in f for f in row.fix_suggestions)


def test_doctor_says_nothing_when_the_bundle_has_it_or_is_not_ours(world):
    with patch("nexus.config.is_local_mode", return_value=True):
        assert pg_bundle_refresh.check_pg_bundle(world.cfg) == []  # self-managed PostgreSQL: no extracted bundle
        world.install_old(background=True)
        assert pg_bundle_refresh.check_pg_bundle(world.cfg) == []


def test_doctor_says_nothing_in_cloud_mode(world):
    world.install_old(background=False)
    with patch("nexus.config.is_local_mode", return_value=False):
        assert pg_bundle_refresh.check_pg_bundle(world.cfg) == []
