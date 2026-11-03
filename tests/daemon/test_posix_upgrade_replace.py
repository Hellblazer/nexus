# SPDX-License-Identifier: AGPL-3.0-or-later
"""nexus-86aaz: on macOS and Linux a replaced engine or PostgreSQL bundle goes in
with the service stopped, and the service is started again afterwards, as on
Windows (RDR-224 scope change, Sam 2026-11-03).

Before this a POSIX swap succeeded while the old process kept serving the old
file (``install-binary`` ended with "Restart the service to pick it up", and a
plain ``service start`` after it short-circuited onto the live lease). The
stop and start effects are the same seams the Windows tests use
(``QuiesceOps``), so every branch runs on every host; the host platform the
call is decided on is injected as ``linux`` / ``darwin``. One test at the end
runs real processes through the real stop function.
"""
from __future__ import annotations

import hashlib
import io
import json
import os
import subprocess
import sys
import tarfile
import time
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

import pytest

from nexus.daemon.storage_service_daemon import StorageServiceStartError
from tests._module_seam import setattr_in
from click.testing import CliRunner

from nexus import upgrade_finish as uf
from nexus.commands import daemon as dcmd
from nexus.daemon import binary_install as b
from nexus.daemon import replace_guard as rg
from nexus.daemon import replace_quiesce as rq
from nexus.daemon import service_registry as sr
from nexus.db import pg_bundle

_TAG = "engine-service-v0.1.301"
_POSIX = ("linux", "darwin")


class _Ops:
    """Fake effects recording the order they ran in."""

    def __init__(self, *, outcome=None, pg_up=False, start_error=None):
        self.events: list[str] = []
        self.outcome = outcome if outcome is not None else _stopped(100, 200)
        self.pg_up = pg_up
        self.start_error = start_error

    def ops(self) -> rq.QuiesceOps:
        def stop_service():
            self.events.append("stop_service")
            return self.outcome

        def pg_running():
            self.events.append("pg_running")
            return self.pg_up

        def stop_pg():
            self.events.append("stop_pg")

        def start_pg():
            self.events.append("start_pg")

        def start_service():
            self.events.append("start_service")
            if self.start_error:
                raise self.start_error

        return rq.QuiesceOps(stop_service, pg_running, stop_pg, start_pg, start_service)


def _stopped(*pids: int):
    return SimpleNamespace(pids=tuple(pids), stubborn=(), refused=(), source="lease")


def _nothing_running():
    return SimpleNamespace(pids=(), stubborn=(), refused=(), source="none")


# ── quiesced on a POSIX host ─────────────────────────────────────────────────


@pytest.mark.parametrize("platform", _POSIX)
def test_engine_replacement_stops_then_replaces_then_starts_the_service(tmp_path, platform):
    fake = _Ops(pg_up=True)
    with rq.quiesced(tmp_path, replacing="engine", platform=platform, ops=fake.ops()) as st:
        fake.events.append("body")
    assert fake.events == ["stop_service", "body", "start_service"]
    assert st.stopped_service and not st.stopped_pg
    assert st.restarted == ["service"]


def test_the_host_platform_is_the_default_and_acts_the_same(tmp_path):
    """No ``platform=``: whatever this host is, the stop and start happen."""
    fake = _Ops()
    with rq.quiesced(tmp_path, replacing="engine", ops=fake.ops()):
        fake.events.append("body")
    assert fake.events == ["stop_service", "body", "start_service"]


@pytest.mark.parametrize("platform", _POSIX)
def test_bundle_replacement_stops_service_then_pg_and_starts_pg_first(tmp_path, platform):
    fake = _Ops(pg_up=True)
    with rq.quiesced(tmp_path, replacing="pg_bundle", platform=platform, ops=fake.ops()):
        fake.events.append("body")
    assert fake.events == [
        "stop_service", "pg_running", "stop_pg", "body", "start_pg", "start_service",
    ]


@pytest.mark.parametrize("platform", _POSIX)
def test_bundle_replacement_with_no_cluster_running_stops_and_starts_no_pg(tmp_path, platform):
    fake = _Ops(pg_up=False)
    with rq.quiesced(tmp_path, replacing="pg_bundle", platform=platform, ops=fake.ops()):
        pass
    assert fake.events == ["stop_service", "pg_running", "start_service"]


@pytest.mark.parametrize("replacing", ["engine", "pg_bundle"])
def test_nothing_running_means_nothing_is_started(tmp_path, replacing):
    """A service that was not running stays down; the swap still happens."""
    fake = _Ops(outcome=_nothing_running(), pg_up=False)
    ran: list[str] = []
    with rq.quiesced(tmp_path, replacing=replacing, platform="linux", ops=fake.ops()) as st:
        ran.append("body")
    assert ran == ["body"]
    assert not st.stopped_service and not st.stopped_pg
    assert "start_service" not in fake.events and "start_pg" not in fake.events


def test_an_engine_replacement_never_touches_postgresql(tmp_path):
    fake = _Ops(pg_up=True)
    with rq.quiesced(tmp_path, replacing="engine", platform="linux", ops=fake.ops()):
        pass
    assert not {"pg_running", "stop_pg", "start_pg"} & set(fake.events)


@pytest.mark.parametrize("platform", _POSIX)
def test_a_failed_body_still_starts_what_was_stopped(tmp_path, platform):
    fake = _Ops(pg_up=True)
    with pytest.raises(RuntimeError, match="boom"):
        with rq.quiesced(tmp_path, replacing="pg_bundle", platform=platform, ops=fake.ops()):
            raise RuntimeError("boom")
    assert fake.events[-2:] == ["start_pg", "start_service"]


def test_a_failed_body_starts_the_service_even_with_restart_after_false(tmp_path):
    fake = _Ops()
    with pytest.raises(RuntimeError):
        with rq.quiesced(tmp_path, replacing="engine", platform="linux",
                         restart_after=False, ops=fake.ops()):
            raise RuntimeError("install failed")
    assert fake.events == ["stop_service", "start_service"]


def test_restart_after_false_skips_the_start_after_a_good_body(tmp_path):
    fake = _Ops()
    with rq.quiesced(tmp_path, replacing="engine", platform="linux",
                     restart_after=False, ops=fake.ops()):
        pass
    assert fake.events == ["stop_service"]


def test_a_start_that_fails_after_a_good_replacement_is_reported(tmp_path):
    fake = _Ops(start_error=RuntimeError("port busy"))
    with pytest.raises(rq.RestartAfterReplaceError, match="storage service.*port busy"):
        with rq.quiesced(tmp_path, replacing="engine", platform="linux", ops=fake.ops()):
            pass


def test_a_start_that_fails_on_the_failure_path_does_not_mask_the_error(tmp_path):
    fake = _Ops(start_error=RuntimeError("port busy"))
    with pytest.raises(ValueError, match="real cause") as err:
        with rq.quiesced(tmp_path, replacing="engine", platform="linux", ops=fake.ops()):
            raise ValueError("real cause")
    assert any("port busy" in n for n in getattr(err.value, "__notes__", []))


def test_a_survivor_of_the_stop_blocks_the_replacement_without_a_start(tmp_path):
    fake = _Ops(outcome=SimpleNamespace(
        pids=(100,), stubborn=(100,), refused=(), source="lease"))
    body: list[str] = []
    with pytest.raises(rg.ReplaceBlockedError, match="survived"):
        with rq.quiesced(tmp_path, replacing="engine", platform="linux", ops=fake.ops()):
            body.append("ran")
    assert body == []
    assert "start_service" not in fake.events  # a start would short-circuit onto it


def test_the_windows_only_session_refusal_is_not_what_a_posix_stop_reports(tmp_path):
    """A POSIX stop never carries a refusal; the block message of a survivor
    names no Windows session."""
    fake = _Ops(outcome=SimpleNamespace(
        pids=(100,), stubborn=(100,), refused=(), source="lease"))
    with pytest.raises(rg.ReplaceBlockedError) as err:
        with rq.quiesced(tmp_path, replacing="engine", platform="darwin", ops=fake.ops()):
            pass
    assert "Windows session" not in str(err.value)


# ── the default effects stop this config dir's service with the stop verb's function ──


def test_the_default_stop_is_the_function_nx_daemon_service_stop_uses(tmp_path, monkeypatch):
    seen: list[Path] = []

    def fake_stop(*, config_dir=None, **_kw):
        seen.append(config_dir)
        return _nothing_running()

    monkeypatch.setattr(
        "nexus.daemon.storage_service_daemon.stop_storage_service", fake_stop,
    )
    rq.default_ops(tmp_path).stop_service()
    assert seen == [tmp_path]


# ── install_binary on a POSIX host ───────────────────────────────────────────

_BIN = b"\x7fELF-engine\n" * 32


def _txz(data: bytes) -> bytes:
    buf = io.BytesIO()
    with tarfile.open(fileobj=buf, mode="w:xz") as tf:
        info = tarfile.TarInfo("nexus-service")
        info.size = len(data)
        tf.addfile(info, io.BytesIO(data))
    return buf.getvalue()


class _OkChecker:
    def check(self, **_kw) -> None:
        return None


def _serve(monkeypatch, files: dict[str, bytes]) -> None:
    def _dl(url, dest, *, timeout=0):
        name = url.rsplit("/", 1)[1]
        for asset, data in files.items():
            if name == asset:
                dest.write_bytes(data)
                return
            if name == f"{asset}.sha256":
                dest.write_text(f"{hashlib.sha256(data).hexdigest()}  {asset}\n", encoding="utf-8")
                return
            if name == f"{asset}.sigstore.json":
                dest.write_text("{}", encoding="utf-8")
                return
        raise b.BinaryAssetAbsentError(f"404 {url}")

    monkeypatch.setattr(b, "_download", _dl)


def _install(tmp_path, **kw):
    return b.install_binary(
        _TAG, tmp_path, checker=_OkChecker(), download_dir=tmp_path,
        platform_tag="linux-amd64", host_platform="linux", **kw,
    )


def _default_guard_with(monkeypatch, fake: _Ops) -> None:
    """The default guard is the real one; swap its effects for the fakes."""
    monkeypatch.setattr(rq, "default_ops", lambda config_dir: fake.ops())


def _engine_path(tmp_path: Path) -> Path:
    return tmp_path / "service" / "nexus-service"


def _write_old_engine(tmp_path: Path) -> None:
    _engine_path(tmp_path).parent.mkdir(parents=True)
    _engine_path(tmp_path).write_bytes(b"OLD")


@pytest.mark.parametrize("layout", ["archive", "single-file"])
def test_install_stops_the_service_before_the_swap_and_starts_it_after(
    tmp_path, monkeypatch, layout,
):
    _write_old_engine(tmp_path)
    fake = _Ops()
    ops = fake.ops()
    observed: dict[str, bytes] = {}

    def stop_service():
        observed["at_stop"] = _engine_path(tmp_path).read_bytes()
        return _stopped(100, 200)

    def start_service():
        observed["at_start"] = _engine_path(tmp_path).read_bytes()
        # The supervisor reads the receipt on start: it must already describe the new
        # bytes (written inside the stop, before this start). Moving the receipt write
        # after the guard turns this red.
        observed["receipt_at_start"] = json.loads(
            b.binary_sidecar_path(tmp_path).read_text(encoding="utf-8")
        )

    ops.stop_service, ops.start_service = stop_service, start_service
    monkeypatch.setattr(rq, "default_ops", lambda config_dir: ops)
    if layout == "archive":
        asset = _txz(_BIN)
        _serve(monkeypatch, {"nexus-service-linux-amd64.txz": asset})
    else:
        asset = _BIN
        _serve(monkeypatch, {"nexus-service-linux-amd64": asset})
    _install(tmp_path)
    assert observed["at_stop"] == b"OLD", "nothing is replaced before the stop"
    assert observed["at_start"] == _BIN, "the service starts on the new file"
    receipt = observed["receipt_at_start"]
    assert receipt["sha256"] == hashlib.sha256(asset).hexdigest(), "the receipt names the NEW bytes"
    assert receipt["tag"] == _TAG
    if layout == "archive":
        assert receipt["installed_sha256"] == hashlib.sha256(_BIN).hexdigest()


@pytest.mark.parametrize("layout", ["archive", "single-file"])
def test_install_with_no_service_running_starts_nothing(tmp_path, monkeypatch, layout):
    fake = _Ops(outcome=_nothing_running())
    _default_guard_with(monkeypatch, fake)
    name = "nexus-service-linux-amd64.txz" if layout == "archive" else "nexus-service-linux-amd64"
    _serve(monkeypatch, {name: _txz(_BIN) if layout == "archive" else _BIN})
    _install(tmp_path)
    assert fake.events == ["stop_service"]
    assert _engine_path(tmp_path).read_bytes() == _BIN


@pytest.mark.parametrize("layout", ["archive", "single-file"])
def test_install_with_restart_after_false_leaves_the_start_to_the_caller(
    tmp_path, monkeypatch, layout,
):
    fake = _Ops()
    _default_guard_with(monkeypatch, fake)
    name = "nexus-service-linux-amd64.txz" if layout == "archive" else "nexus-service-linux-amd64"
    _serve(monkeypatch, {name: _txz(_BIN) if layout == "archive" else _BIN})
    _install(tmp_path, restart_after=False)
    assert fake.events == ["stop_service"]


def test_a_failed_single_file_copy_restarts_the_service_and_keeps_the_old_engine(
    tmp_path, monkeypatch,
):
    _write_old_engine(tmp_path)
    fake = _Ops()
    _default_guard_with(monkeypatch, fake)
    _serve(monkeypatch, {"nexus-service-linux-amd64": _BIN})

    def boom(*_a, **_kw):
        raise OSError(28, "No space left on device")

    setattr_in(monkeypatch, b, "os.replace", boom)
    with pytest.raises(OSError):
        _install(tmp_path)
    assert fake.events == ["stop_service", "start_service"]
    assert _engine_path(tmp_path).read_bytes() == b"OLD"


def test_a_download_that_fails_never_stops_the_service(tmp_path, monkeypatch):
    fake = _Ops()
    _default_guard_with(monkeypatch, fake)
    _serve(monkeypatch, {})  # every asset is a 404
    with pytest.raises(b.BinaryVerificationError):
        _install(tmp_path)
    assert fake.events == []


def test_install_refused_by_a_surviving_service_replaces_nothing(tmp_path, monkeypatch):
    _write_old_engine(tmp_path)
    fake = _Ops(outcome=SimpleNamespace(pids=(9,), stubborn=(9,), refused=(), source="lease"))
    _default_guard_with(monkeypatch, fake)
    _serve(monkeypatch, {"nexus-service-linux-amd64.txz": _txz(_BIN)})
    with pytest.raises(rg.ReplaceBlockedError, match="survived"):
        _install(tmp_path)
    assert _engine_path(tmp_path).read_bytes() == b"OLD"
    assert fake.events == ["stop_service"]


# ── the bundle swap on a POSIX host ──────────────────────────────────────────


def _two_archives(tmp_path, make_pg_bundle_txz):
    first = make_pg_bundle_txz(tmp_path, "nexus-pg-first.txz")
    second = make_pg_bundle_txz(tmp_path / "second", "nexus-pg-second.txz", variant="second")
    root = tmp_path / "pg-bundle"
    pg_bundle.extract_bundle(first, root, platform="linux")
    return first, second, root


def test_a_bundle_swap_stops_pg_and_the_service_and_starts_them_again(
    tmp_path, monkeypatch, make_pg_bundle_txz,
):
    _first, second, root = _two_archives(tmp_path, make_pg_bundle_txz)
    fake = _Ops(pg_up=True)
    ops = fake.ops()
    marked_at_start: list[bool] = []

    def start_pg():
        fake.events.append("start_pg")
        marked_at_start.append(
            (root / pg_bundle._EXTRACT_MARKER).read_text(encoding="utf-8")
            == pg_bundle._archive_identity(second)
        )

    ops.start_pg = start_pg
    monkeypatch.setattr(rq, "default_ops", lambda config_dir: ops)
    pg_bundle.extract_bundle(second, root, platform="linux")
    assert fake.events == ["stop_service", "pg_running", "stop_pg", "start_pg", "start_service"]
    assert marked_at_start == [True], "PostgreSQL starts on the new tree"


def test_a_first_extraction_and_a_repeat_of_the_same_archive_stop_nothing(
    tmp_path, monkeypatch, make_pg_bundle_txz,
):
    fake = _Ops(pg_up=True)
    _default_guard_with(monkeypatch, fake)
    first = make_pg_bundle_txz(tmp_path, "nexus-pg-first.txz")
    root = tmp_path / "pg-bundle"
    pg_bundle.extract_bundle(first, root, platform="linux")   # nothing to replace
    pg_bundle.extract_bundle(first, root, platform="linux")   # same archive: no-op
    assert fake.events == []


# ── no double bounce in the callers ──────────────────────────────────────────


def test_converge_engine_installs_without_the_restart_and_cycles_once(tmp_path, monkeypatch):
    """converge_engine verifies the version after its own cycle, so the install
    leaves the start to it (``restart_after=False``): with the service stopped by
    the install, its stop is a no-op and its start is the only start."""
    status = SimpleNamespace(
        applicable=True, converged=False, required_version=(9, 9, 9),
        installed_version=(9, 9, 8),
    )
    seen: dict[str, object] = {}

    def fake_install(tag, config_dir, **kw):
        seen["kw"] = kw
        return tmp_path / "service" / "nexus-service", {}

    cycles: list[str] = []
    monkeypatch.setattr(uf, "detect_engine_convergence", lambda cd: status)
    monkeypatch.setattr(uf, "_poison_probe", lambda cd: SimpleNamespace(
        playbook=None, unknown_reason=None))
    monkeypatch.setattr(uf, "_reassign_diag_view_before_restart", lambda cd: [])
    monkeypatch.setattr(
        uf, "_restart_and_verify", lambda cd, actions, req: cycles.append("cycle") or actions,
    )
    monkeypatch.setattr(b, "install_binary", fake_install)
    monkeypatch.setattr(b, "PINNED_SERVICE_TAG", _TAG)
    uf.converge_engine(tmp_path)
    assert seen["kw"].get("restart_after") is False
    assert cycles == ["cycle"]


def test_converge_engine_install_leaves_exactly_one_service_start(tmp_path, monkeypatch):
    """The real quiesce under converge_engine's install: the service is stopped once,
    and the quiesce does not start it (the cycle after it does)."""
    status = SimpleNamespace(
        applicable=True, converged=False, required_version=(9, 9, 9),
        installed_version=(9, 9, 8),
    )
    fake = _Ops()
    _default_guard_with(monkeypatch, fake)
    _serve(monkeypatch, {"nexus-service-linux-amd64.txz": _txz(_BIN)})
    monkeypatch.setattr(uf, "detect_engine_convergence", lambda cd: status)
    monkeypatch.setattr(uf, "_poison_probe", lambda cd: SimpleNamespace(
        playbook=None, unknown_reason=None))
    monkeypatch.setattr(uf, "_reassign_diag_view_before_restart", lambda cd: [])
    monkeypatch.setattr(uf, "_restart_and_verify", lambda cd, actions, req: actions)
    monkeypatch.setattr(b, "PINNED_SERVICE_TAG", _TAG)
    real = b.install_binary
    monkeypatch.setattr(
        b, "install_binary",
        lambda tag, cd, **kw: real(
            tag, cd, checker=_OkChecker(), download_dir=tmp_path,
            platform_tag="linux-amd64", host_platform="linux", **kw,
        ),
    )
    uf.converge_engine(tmp_path)
    assert fake.events == ["stop_service"], "stopped once; the cycle after it does the start"
    assert _engine_path(tmp_path).read_bytes() == _BIN


# ── the install-binary verb: one stop, one start ─────────────────────────────

_ENGINE_ASSET = "nexus-service-linux-amd64.txz"
_PG_ASSET = "nexus-pg-linux-amd64.txz"


def _verb_world(tmp_path, monkeypatch, fake: _Ops) -> None:
    """The verb with the network, the signature check and the stop effects faked,
    on a pretend linux-amd64 host (the asset names are the host's own otherwise)."""
    monkeypatch.setattr(dcmd, "_emit_chash_poison_gate", lambda cd, force=False: None)
    monkeypatch.setattr(b, "current_platform_tag", lambda **_kw: "linux-amd64")
    monkeypatch.setattr(b, "verify_signature", lambda *a, **k: None)
    _default_guard_with(monkeypatch, fake)


def _pg_bytes(tmp_path, make_pg_bundle_txz, variant: str) -> bytes:
    return make_pg_bundle_txz(tmp_path / f"pgsrc-{variant}", f"x-{variant}.txz", variant=variant).read_bytes()


def _run_verb(tmp_path, *extra: str):
    return CliRunner().invoke(
        dcmd.daemon_group,
        ["service", "install-binary", _TAG, "--config-dir", str(tmp_path), *extra],
    )


def _prepare_sources(tmp_path) -> None:
    for variant in ("v1", "v2"):
        (tmp_path / f"pgsrc-{variant}").mkdir(exist_ok=True)


def test_the_verb_run_twice_with_identical_assets_stops_and_swaps_nothing_the_second_time(
    tmp_path, monkeypatch, make_pg_bundle_txz,
):
    _prepare_sources(tmp_path)
    fake = _Ops(pg_up=True)
    _verb_world(tmp_path, monkeypatch, fake)
    pg = _pg_bytes(tmp_path, make_pg_bundle_txz, "v1")
    _serve(monkeypatch, {_ENGINE_ASSET: _txz(_BIN), _PG_ASSET: pg})

    first = _run_verb(tmp_path)
    assert first.exit_code == 0, first.output
    assert fake.events == ["stop_service", "start_service"], "the engine went in under one stop, one start"
    engine = _engine_path(tmp_path)
    archive = b.pg_bundle_dest(tmp_path, platform_tag="linux-amd64")
    marker = tmp_path / "pg-bundle" / pg_bundle._EXTRACT_MARKER
    before = (
        engine.stat().st_mtime_ns, archive.stat().st_mtime_ns, marker.stat().st_mtime_ns,
        marker.read_text(encoding="utf-8"),
    )
    fake.events.clear()

    second = _run_verb(tmp_path)
    assert second.exit_code == 0, second.output
    assert fake.events == [], "nothing stopped, nothing started"
    after = (
        engine.stat().st_mtime_ns, archive.stat().st_mtime_ns, marker.stat().st_mtime_ns,
        marker.read_text(encoding="utf-8"),
    )
    assert after == before, "neither the engine, nor the placed archive, nor the extracted bundle was touched"
    assert "Already installed" in second.output
    assert "Nothing was replaced" in second.output


def test_the_verb_with_only_the_bundle_changed_stops_and_starts_once(
    tmp_path, monkeypatch, make_pg_bundle_txz,
):
    _prepare_sources(tmp_path)
    fake = _Ops(pg_up=True)
    _verb_world(tmp_path, monkeypatch, fake)
    _serve(monkeypatch, {_ENGINE_ASSET: _txz(_BIN), _PG_ASSET: _pg_bytes(tmp_path, make_pg_bundle_txz, "v1")})
    assert _run_verb(tmp_path).exit_code == 0
    engine_mtime = _engine_path(tmp_path).stat().st_mtime_ns
    fake.events.clear()

    pg_v2 = _pg_bytes(tmp_path, make_pg_bundle_txz, "v2")
    _serve(monkeypatch, {_ENGINE_ASSET: _txz(_BIN), _PG_ASSET: pg_v2})
    out = _run_verb(tmp_path)
    assert out.exit_code == 0, out.output
    assert fake.events == ["stop_service", "pg_running", "stop_pg", "start_pg", "start_service"]
    assert _engine_path(tmp_path).stat().st_mtime_ns == engine_mtime, "the unchanged engine was not re-placed"
    marker = (tmp_path / "pg-bundle" / pg_bundle._EXTRACT_MARKER).read_text(encoding="utf-8")
    assert marker == f"sha256={hashlib.sha256(pg_v2).hexdigest()}\n"


def test_the_verb_with_the_engine_and_the_bundle_both_changed_stops_and_starts_once(
    tmp_path, monkeypatch, make_pg_bundle_txz,
):
    _prepare_sources(tmp_path)
    fake = _Ops(pg_up=True)
    ops = fake.ops()
    root = tmp_path / "pg-bundle"
    seen: dict[str, object] = {}

    def start_pg():
        fake.events.append("start_pg")
        seen["engine_at_start_pg"] = _engine_path(tmp_path).read_bytes()
        seen["tree_new_at_start_pg"] = (root / "bundle" / "share" / "VARIANT").is_file()

    ops.start_pg = start_pg
    monkeypatch.setattr(rq, "default_ops", lambda config_dir: ops)
    monkeypatch.setattr(dcmd, "_emit_chash_poison_gate", lambda cd, force=False: None)
    monkeypatch.setattr(b, "current_platform_tag", lambda **_kw: "linux-amd64")
    monkeypatch.setattr(b, "verify_signature", lambda *a, **k: None)
    _serve(monkeypatch, {_ENGINE_ASSET: _txz(_BIN), _PG_ASSET: _pg_bytes(tmp_path, make_pg_bundle_txz, "v1")})
    assert _run_verb(tmp_path).exit_code == 0
    fake.events.clear()

    new_bin = b"\x7fELF-engine-two\n" * 32
    _serve(monkeypatch, {
        _ENGINE_ASSET: _txz(new_bin), _PG_ASSET: _pg_bytes(tmp_path, make_pg_bundle_txz, "v2"),
    })
    out = _run_verb(tmp_path)
    assert out.exit_code == 0, out.output
    assert fake.events.count("stop_service") == 1 and fake.events.count("start_service") == 1
    assert fake.events == ["stop_service", "pg_running", "stop_pg", "start_pg", "start_service"]
    assert seen == {"engine_at_start_pg": new_bin, "tree_new_at_start_pg": True}
    assert "started again" in out.output


def test_the_verb_replaces_the_engine_alone_when_the_bundle_is_not_asked_for(tmp_path, monkeypatch):
    fake = _Ops(pg_up=True)
    _verb_world(tmp_path, monkeypatch, fake)
    _serve(monkeypatch, {_ENGINE_ASSET: _txz(_BIN)})
    out = _run_verb(tmp_path, "--no-pg-bundle")
    assert out.exit_code == 0, out.output
    assert fake.events == ["stop_service", "start_service"]
    assert "Restart the service to pick it up" not in out.output


def test_the_verb_with_the_engine_current_and_no_bundle_extracted_yet_stops_nothing(
    tmp_path, monkeypatch, make_pg_bundle_txz,
):
    """A first extraction replaces nothing, so it needs no stop of its own."""
    _prepare_sources(tmp_path)
    fake = _Ops(pg_up=True)
    _verb_world(tmp_path, monkeypatch, fake)
    _serve(monkeypatch, {_ENGINE_ASSET: _txz(_BIN), _PG_ASSET: _pg_bytes(tmp_path, make_pg_bundle_txz, "v1")})
    assert _run_verb(tmp_path, "--no-pg-bundle").exit_code == 0
    fake.events.clear()
    out = _run_verb(tmp_path)
    assert out.exit_code == 0, out.output
    assert fake.events == []
    assert pg_bundle.extracted_bin_dir(tmp_path) is not None


def test_a_service_that_was_not_running_stays_down_and_the_verb_says_how_to_start_it(
    tmp_path, monkeypatch,
):
    fake = _Ops(outcome=_nothing_running())
    _verb_world(tmp_path, monkeypatch, fake)
    _serve(monkeypatch, {_ENGINE_ASSET: _txz(_BIN)})
    out = _run_verb(tmp_path, "--no-pg-bundle")
    assert out.exit_code == 0, out.output
    assert fake.events == ["stop_service"]
    assert "was not running" in out.output and "nx daemon service start" in out.output


def test_a_bundle_that_fails_after_the_engine_went_in_still_starts_the_service_once(
    tmp_path, monkeypatch,
):
    fake = _Ops()
    _verb_world(tmp_path, monkeypatch, fake)
    _serve(monkeypatch, {_ENGINE_ASSET: _txz(_BIN)})  # the bundle is a 404
    out = _run_verb(tmp_path)
    assert out.exit_code == 2
    assert "binary installed OK" in out.output
    assert fake.events == ["stop_service", "start_service"]


# ── a slow start is a warning, a failed start is an error ────────────────────


@pytest.mark.parametrize("message", [
    "Storage service supervisor did not become ready within 60s. Check the log.",
    "The autostart unit x was started but the storage service did not become ready within 60s; retrying.",
])
def test_a_start_that_outlasts_the_wait_is_a_warning_not_an_error(tmp_path, monkeypatch, message):
    fake = _Ops(start_error=StorageServiceStartError(message, still_starting=True))
    _verb_world(tmp_path, monkeypatch, fake)
    _serve(monkeypatch, {_ENGINE_ASSET: _txz(_BIN)})
    out = _run_verb(tmp_path, "--no-pg-bundle")
    assert out.exit_code == 0, out.output
    assert "may still be migrating" in out.output
    assert "nx daemon service status" in out.output and "nx doctor" in out.output
    assert _engine_path(tmp_path).read_bytes() == _BIN


@pytest.mark.parametrize("message", [
    "Storage service supervisor exited with code 1 before the service became ready.",
    "port busy",
    # nexus-86aaz r2: the wording alone no longer makes a warning; only the raiser's flag does.
    "The autostart unit x was started but the storage service did not become ready within 60s; retrying.",
])
def test_a_start_that_really_failed_is_still_an_error(tmp_path, monkeypatch, message):
    fake = _Ops(start_error=RuntimeError(message))
    _verb_world(tmp_path, monkeypatch, fake)
    _serve(monkeypatch, {_ENGINE_ASSET: _txz(_BIN)})
    out = _run_verb(tmp_path, "--no-pg-bundle")
    assert out.exit_code == 2
    assert "could not be started again" in out.output
    assert "may still be migrating" not in out.output


def test_the_restart_error_carries_the_slow_start_split(tmp_path):
    slow = _Ops(start_error=StorageServiceStartError("supervisor did not become ready within 60s", still_starting=True))
    with pytest.raises(rq.RestartAfterReplaceError) as err:
        with rq.quiesced(tmp_path, replacing="engine", ops=slow.ops()):
            pass
    assert err.value.slow_start is True
    exited = _Ops(start_error=StorageServiceStartError("supervisor exited with code 3", still_starting=False))
    with pytest.raises(rq.RestartAfterReplaceError) as err:
        with rq.quiesced(tmp_path, replacing="engine", ops=exited.ops()):
            pass
    assert err.value.slow_start is False
    pg_fail = _Ops(pg_up=True, start_error=None)
    ops = pg_fail.ops()

    def boom():
        raise RuntimeError("did not become ready within 60s")

    ops.start_pg = boom
    with pytest.raises(rq.RestartAfterReplaceError) as err:
        with rq.quiesced(tmp_path, replacing="pg_bundle", ops=ops):
            pass
    assert err.value.slow_start is False, "only the service's slow start is a warning"


# ── a stop that cannot rule out a running service says so, and does not block ──


@pytest.mark.parametrize("outcome", [
    SimpleNamespace(pids=(100,), stubborn=(), refused=(), source="lease", sweep_verified=False),
    SimpleNamespace(pids=(), stubborn=(), refused=(), source="process_table_unavailable", sweep_verified=False),
    SimpleNamespace(pids=(), stubborn=(), refused=(), source="none", unreadable=(77,), sweep_verified=False),
], ids=["sweep-not-run", "no-process-table", "unreadable-pid"])
def test_an_unverified_stop_warns_and_does_not_block(tmp_path, outcome):
    fake = _Ops(outcome=outcome)
    ran: list[str] = []
    with rq.quiesced(tmp_path, replacing="engine", ops=fake.ops()) as st:
        ran.append("body")
    assert ran == ["body"], "the swap goes ahead"
    assert len(st.warnings) == 1
    assert "could not be ruled out" in st.warnings[0] and "nx daemon service stop" in st.warnings[0]


def test_a_verified_stop_carries_no_warning(tmp_path):
    with rq.quiesced(tmp_path, replacing="engine", ops=_Ops().ops()) as st:
        pass
    assert st.warnings == []


def test_the_verb_prints_the_unverified_stop_warning_and_still_installs(tmp_path, monkeypatch):
    fake = _Ops(outcome=SimpleNamespace(
        pids=(100,), stubborn=(), refused=(), source="lease", sweep_verified=False))
    _verb_world(tmp_path, monkeypatch, fake)
    _serve(monkeypatch, {_ENGINE_ASSET: _txz(_BIN)})
    out = _run_verb(tmp_path, "--no-pg-bundle")
    assert out.exit_code == 0, out.output
    assert "could not be ruled out" in out.output
    assert "restart" in out.output.lower() and "by hand" in out.output
    assert _engine_path(tmp_path).read_bytes() == _BIN


# ── SharedQuiesce ────────────────────────────────────────────────────────────


def test_a_shared_quiesce_that_nothing_joins_stops_nothing(tmp_path):
    fake = _Ops()
    with rq.SharedQuiesce(tmp_path, ops=fake.ops()) as shared:
        pass
    assert fake.events == [] and shared.state is None


def test_a_shared_quiesce_stops_once_for_the_engine_and_again_for_nothing(tmp_path):
    fake = _Ops(pg_up=True)
    with rq.SharedQuiesce(tmp_path, ops=fake.ops()) as shared:
        with shared.guard("engine"):
            fake.events.append("engine")
        with shared.guard("engine"):
            fake.events.append("engine again")
    assert fake.events == ["stop_service", "engine", "engine again", "start_service"]


def test_a_bundle_joining_an_engine_stop_stops_postgresql_too_and_starts_it_first(tmp_path):
    fake = _Ops(pg_up=True)
    with rq.SharedQuiesce(tmp_path, ops=fake.ops()) as shared:
        with shared.guard("engine"):
            fake.events.append("engine")
        with shared.guard("pg_bundle"):
            fake.events.append("bundle")
        with shared.guard("pg_bundle"):
            fake.events.append("bundle again")
    assert fake.events == [
        "stop_service", "engine", "pg_running", "stop_pg", "bundle", "bundle again",
        "start_pg", "start_service",
    ]


def test_a_failure_in_a_shared_run_starts_what_was_stopped_once(tmp_path):
    fake = _Ops(pg_up=True)
    with pytest.raises(RuntimeError, match="boom"):
        with rq.SharedQuiesce(tmp_path, ops=fake.ops()) as shared:
            with shared.guard("engine"):
                pass
            with shared.guard("pg_bundle"):
                raise RuntimeError("boom")
    assert fake.events.count("start_service") == 1 and fake.events.count("start_pg") == 1


def test_postgresql_that_cannot_be_stopped_for_a_joining_bundle_blocks_it_and_restarts_once(tmp_path):
    fake = _Ops(pg_up=True)
    ops = fake.ops()

    def stop_pg():
        fake.events.append("stop_pg")
        raise RuntimeError("pg_ctl timed out")

    ops.stop_pg = stop_pg
    with pytest.raises(rg.ReplaceBlockedError, match="pg_ctl timed out"):
        with rq.SharedQuiesce(tmp_path, ops=ops) as shared:
            with shared.guard("engine"):
                pass
            with shared.guard("pg_bundle"):
                fake.events.append("not reached")
    assert "not reached" not in fake.events
    assert fake.events.count("start_service") == 1


def test_a_blocked_first_join_holds_nothing_and_starts_nothing(tmp_path):
    fake = _Ops(outcome=SimpleNamespace(pids=(9,), stubborn=(9,), refused=(), source="lease"))
    with pytest.raises(rg.ReplaceBlockedError, match="survived"):
        with rq.SharedQuiesce(tmp_path, ops=fake.ops()) as shared:
            with shared.guard("engine"):
                pass
    assert fake.events == ["stop_service"] and shared.state is None


def test_a_shared_run_with_restart_after_false_leaves_the_start_to_the_caller(tmp_path):
    fake = _Ops()
    with rq.SharedQuiesce(tmp_path, restart_after=False, ops=fake.ops()) as shared:
        with shared.guard("engine"):
            pass
    assert fake.events == ["stop_service"]


# ── real processes ───────────────────────────────────────────────────────────

_posix_only = pytest.mark.skipif(
    sys.platform == "win32", reason="real fake-supervisor processes are POSIX-only here",
)


def _fake_nx(bin_dir: Path) -> Path:
    bin_dir.mkdir(parents=True, exist_ok=True)
    script = bin_dir / "nx"
    script.write_text(f"#!{sys.executable}\nimport time\ntime.sleep(120)\n", encoding="utf-8")
    script.chmod(0o755)
    return script


def _spawn(nx: Path, config_dir: Path) -> subprocess.Popen:
    proc = subprocess.Popen(
        [str(nx), "daemon", "service", "start", "--foreground", "--config-dir", str(config_dir)],
        env={"PATH": os.environ.get("PATH", "")},
        stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, start_new_session=True,
    )
    deadline = time.monotonic() + 10
    while time.monotonic() < deadline:
        if "daemon service" in sr.process_command(proc.pid):
            return proc
        time.sleep(0.02)
    proc.kill()
    raise AssertionError("child never showed its command line")


@_posix_only
def test_real_processes_the_swap_stops_this_dirs_service_only_and_starts_it_again(tmp_path):
    """Two real supervisor-shaped processes, one for this config dir and one for
    another; the process table is cut down to them. The real stop function stops
    this dir's, leaves the other alive, and the file is swapped between the two."""
    nx = _fake_nx(tmp_path / "bin")
    mine_dir, other_dir = tmp_path / "mine", tmp_path / "other"
    mine_dir.mkdir()
    other_dir.mkdir()
    mine, other = _spawn(nx, mine_dir), _spawn(nx, other_dir)
    rows = [(p.pid, 5, sr.process_command(p.pid)) for p in (mine, other)]
    started: list[str] = []
    try:
        ops = rq.default_ops(mine_dir)
        ops.start_service = lambda: started.append("start")
        engine = tmp_path / "engine"
        engine.write_bytes(b"OLD")
        with patch.object(sr, "all_process_rows", return_value=rows):
            with rq.quiesced(mine_dir, replacing="engine", platform="linux", ops=ops) as st:
                assert mine.wait(timeout=15) is not None, "this dir's service is stopped first"
                assert other.poll() is None, "another config dir's service is not touched"
                engine.write_bytes(b"NEW")
        assert st.stopped_service and started == ["start"]
        assert other.poll() is None
    finally:
        for proc in (mine, other):
            if proc.poll() is None:
                proc.kill()
            proc.wait(timeout=10)


@_posix_only
def test_real_processes_a_service_that_is_not_running_is_not_started(tmp_path):
    nx = _fake_nx(tmp_path / "bin")
    mine_dir, other_dir = tmp_path / "mine", tmp_path / "other"
    mine_dir.mkdir()
    other_dir.mkdir()
    other = _spawn(nx, other_dir)
    rows = [(other.pid, 5, sr.process_command(other.pid))]
    started: list[str] = []
    try:
        ops = rq.default_ops(mine_dir)
        ops.start_service = lambda: started.append("start")
        with patch.object(sr, "all_process_rows", return_value=rows):
            with rq.quiesced(mine_dir, replacing="engine", platform="linux", ops=ops) as st:
                pass
        assert not st.stopped_service and started == []
        assert other.poll() is None
    finally:
        other.kill()
        other.wait(timeout=10)
