# SPDX-License-Identifier: AGPL-3.0-or-later
"""Shared world for the PG-bundle refresh tests (nexus-f3yqd.2.23): real files under ``tmp_path`` (a tiny synthetic ``.txz``
for the release asset, a real extracted tree for the install), with only the network download (``_fetch_verified``) and
the stop and start of the stack stubbed. ``world`` and the autouse ``_pg_ctl_runs_its_script`` are fixtures a test file
imports by name."""
from __future__ import annotations

import hashlib
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
import nexus.bounded_subprocess as bs
from nexus.daemon import binary_install as b
from nexus.daemon import replace_quiesce as rq
from nexus.daemon import pg_bundle_refresh
from nexus.db import pg_bundle
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
        monkeypatch.setattr(pg_bundle_refresh, "engine_busy_reason", lambda _cfg, **_kw: self.busy)

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
    """The ``daemon service start`` calls; the argv head is ``nx`` or this host's launcher (``nx_argv``)."""
    verb = ["daemon", "service", "start"]
    return sum(
        1 for c in cycle.call_args_list
        if c.args and any(list(c.args[0][i:i + 3]) == verb for i in range(len(c.args[0])))
    )





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


