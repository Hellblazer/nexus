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

import hashlib
import sys
import tarfile
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

import pytest

from nexus.daemon import binary_install as b
from nexus.daemon import replace_quiesce as rq
from nexus.db import pg_bundle
from nexus.engine_version import REQUIRED_ENGINE_VERSION
from nexus.upgrade_finish import PoisonProbe, converge_engine
from nexus.upgrade_finish import RunningEngine as _RunningEngine

pytestmark = pytest.mark.skipif(
    sys.platform == "win32", reason="the synthetic bundle's binaries are POSIX shell stubs",
)

_REQUIRED_STR = ".".join(str(p) for p in REQUIRED_ENGINE_VERSION)
_PINNED_TAG = "engine-service-v" + _REQUIRED_STR
_CONTROL = Path("bundle") / "share" / "postgresql" / "extension" / "pg_background.control"


def _older_version_str() -> str:
    major, minor, patch = REQUIRED_ENGINE_VERSION
    return f"{major}.{minor}.{patch - 1}" if patch > 0 else f"{major}.{max(minor - 1, 0)}.999"


def _txz(dest_dir: Path, name: str, *, background: bool, pg_version: str = "17.5", variant: str = "") -> Path:
    """A tiny bundle shaped like the real artifact: bin/ stubs, the relocation marker and,
    when *background* is set, the pg_background control file where the real bundle has it
    (``share/postgresql/extension/``)."""
    stage = dest_dir / f"_stage_{name}"
    bundle = stage / "bundle"
    (bundle / "bin").mkdir(parents=True)
    for tool in ("initdb", "pg_ctl", "psql", "createdb"):
        f = bundle / "bin" / tool
        f.write_text(f"#!/bin/sh\necho '{tool} (PostgreSQL) {pg_version}'\n")
        f.chmod(0o755)
    ext = bundle / "share" / "postgresql" / "extension"
    ext.mkdir(parents=True)
    (ext / "vector.control").write_text("default_version = '0.8.0'\n")
    if background:
        (ext / "pg_background.control").write_text("default_version = '1.0'\n")
    (bundle / "lib").mkdir()
    if variant:
        (bundle / "share" / "VARIANT").write_text(variant + "\n")
    (bundle / ".build_prefix").write_text("/build/prefix/nexus-pg\n")
    archive = dest_dir / name
    with tarfile.open(archive, "w:xz") as tf:
        tf.add(bundle, arcname="bundle")
    return archive


class _Stack:
    """The stop/start effects of the stack, recorded."""

    def __init__(self) -> None:
        self.events: list[str] = []

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
        (self.cfg / "pg_credentials").write_text("NX_DB_URL=postgresql://x/nexus\n")
        self.release = tmp_path / "release"
        self.release.mkdir()
        self.work = tmp_path / "work"
        self.work.mkdir()
        self.stack = _Stack()
        self.downloads: list[str] = []
        self.download_error: Exception | None = None
        self.shipped: Path | None = None
        monkeypatch.setattr(rq, "default_ops", self.stack.ops)
        monkeypatch.setattr(b, "_fetch_verified", self._fetch)

    def _fetch(self, tag, name, tmp, checker):
        self.downloads.append(name)
        if self.download_error is not None:
            raise self.download_error
        assert self.shipped is not None, "the test shipped no release bundle"
        asset = tmp / name
        asset.write_bytes(self.shipped.read_bytes())
        digest = hashlib.sha256(asset.read_bytes()).hexdigest()
        return asset, digest, f"https://example.invalid/{tag}/{name}"

    @property
    def root(self) -> Path:
        return pg_bundle.bundle_extract_root(self.cfg)

    def install_old(self, **kw) -> None:
        """Extract an already-installed bundle (a first extraction stops nothing)."""
        pg_bundle.extract_bundle(_txz(self.work, "old.txz", **kw), self.root)

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
    svc = cfg / "service"
    svc.mkdir(parents=True, exist_ok=True)
    payload = b"native-engine-image"
    (svc / "nexus-service").write_bytes(payload)
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
    from nexus.daemon.pg_bundle_refresh import bundle_has_pg_background

    world.install_old(background=False)
    assert bundle_has_pg_background(world.root) is False
    assert (world.root / "bundle/share/postgresql/extension/vector.control").is_file()
    (world.root / _CONTROL).write_text("default_version = '1.0'\n")
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
    assert world.stack.events == ["stop_service", "stop_pg", "start_pg", "start_service"]
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
    assert world.stack.events == ["stop_service", "stop_pg"]
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

    assert world.stack.events == []
    assert world.marker() == marker
    assert _service_starts(cycle) == 1  # only the engine convergence cycle


def test_a_converged_engine_with_pg_background_present_does_not_download(world):
    world.install_old(background=True)
    world.ship(background=True, variant="would-differ")

    actions, install, cycle = _converge(world, engine="converged")

    assert world.downloads == []
    assert world.stack.events == []
    assert actions == []


def test_cloud_mode_does_nothing(world):
    world.install_old(background=False)
    world.ship(background=True)

    actions, install, cycle = _converge(world, engine="older", local=False)

    assert actions == []
    assert world.downloads == [] and world.stack.events == []
    assert not world.has_background()


def test_a_self_managed_postgresql_install_does_nothing(world):
    """No extracted bundle on disk: the install runs its own PostgreSQL."""
    world.ship(background=True)
    assert not world.root.exists()

    actions, install, cycle = _converge(world, engine="converged")
    assert actions == [] and world.downloads == []

    actions, install, cycle = _converge(world, engine="older")
    install.assert_called_once()  # the engine converges as it always did
    assert world.downloads == [] and world.stack.events == []
    assert not world.root.exists()


def test_report_only_and_dry_run_never_download_or_stop(world):
    world.install_old(background=False)
    world.ship(background=True)

    actions, _install, _cycle = _converge(world, engine="converged", acquire=False)
    assert any("pg_background" in a for a in actions), actions
    assert world.downloads == [] and world.stack.events == []

    actions, _install, _cycle = _converge(world, engine="converged", dry_run=True)
    assert any("would" in a and "PostgreSQL bundle" in a for a in actions), actions
    assert world.downloads == [] and world.stack.events == []
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
    assert world.stack.events == []  # the download runs before anything is stopped
    assert not world.has_background() and world.marker() == marker
    assert pg_bundle.is_bundle_extracted(world.root)


def test_a_failed_download_on_a_converged_engine_is_reported_and_leaves_the_box_alone(world):
    world.install_old(background=False)
    world.download_error = b.BinaryDownloadError("connection reset")

    actions, install, cycle = _converge(world, engine="converged")

    install.assert_not_called()
    assert any("PostgreSQL bundle" in a and "connection reset" in a for a in actions), actions
    assert world.stack.events == [] and not world.has_background()


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
    assert world.stack.events[-2:] == ["start_pg", "start_service"]  # a failed run never leaves the box down


# -- the major-version guard --------------------------------------------------------------


def test_a_swap_across_a_major_version_is_refused_before_anything_is_stopped(world):
    world.install_old(background=False, pg_version="17.5", variant="old")
    world.ship(background=True, pg_version="18.1", variant="new")
    marker = world.marker()

    actions, _install, _cycle = _converge(world, engine="converged")

    assert any("PostgreSQL bundle" in a and "major" in a for a in actions), actions
    assert world.stack.events == []
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
    from nexus.daemon.pg_bundle_refresh import install_and_extract_pg_bundle

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
    assert world.marker() == marker and world.stack.events == []


def test_install_binary_and_converge_share_the_one_function():
    """No copy: both call sites go through ``install_and_extract_pg_bundle``."""
    import inspect

    from nexus.commands import daemon as daemon_cmds
    from nexus.daemon import pg_bundle_refresh

    assert "install_and_extract_pg_bundle" in inspect.getsource(daemon_cmds._install_binary_steps)
    assert "install_and_extract_pg_bundle" in inspect.getsource(pg_bundle_refresh)
    assert "extract_bundle(" not in inspect.getsource(daemon_cmds._install_binary_steps)
