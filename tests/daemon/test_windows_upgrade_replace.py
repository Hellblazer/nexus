# SPDX-License-Identifier: AGPL-3.0-or-later
"""RDR-224 P3.2e (nexus-f9bgu.20): an upgrade on Windows stops what holds the
engine executable or the PostgreSQL bundle, replaces it as one unit, and starts
what it stopped.

The host platform is injected (``platform="win32"``) and the stop and start
effects are fakes, so every Windows branch runs on macOS and Linux as well and
nothing skips. A test that only asserted "no error" would pass with the Windows
branch deleted, so each asserts an ordering or a state the POSIX branch cannot
produce: a stop before the first replace, a start after the last, a file set
that is wholly old after a failure, a retry count.
"""
from __future__ import annotations

import hashlib
import io
import os
import tarfile
from pathlib import Path
from types import SimpleNamespace

import pytest

from nexus.daemon import binary_install as b
from nexus.daemon import replace_guard as rg
from nexus.daemon import replace_quiesce as rq
from nexus.daemon.binary_lifecycle import WINDOWS_ENGINE_EXE, WINDOWS_RUNTIME_DLLS
from nexus.daemon.service_registry import GracefulStopSend
from nexus.db import pg_bundle

_WIN = "win32"
_NAMES = (*WINDOWS_RUNTIME_DLLS, WINDOWS_ENGINE_EXE)


def _write_set(dir_: Path, tag: str) -> None:
    dir_.mkdir(parents=True, exist_ok=True)
    for name in _NAMES:
        (dir_ / name).write_bytes(f"{tag}:{name}".encode())


def _read_set(dir_: Path) -> dict[str, str]:
    return {n: (dir_ / n).read_text() for n in _NAMES if (dir_ / n).exists()}


def _sleeps() -> tuple[list[float], object]:
    log: list[float] = []
    return log, log.append


# ── replace_with_retry ────────────────────────────────────────────────────────


def test_windows_retries_a_sharing_violation_then_succeeds(tmp_path):
    calls: list[int] = []
    slept, sleep = _sleeps()

    def replace(src, dst):
        calls.append(1)
        if len(calls) < 3:
            raise PermissionError(13, "sharing violation")

    rg.replace_with_retry("a", "b", platform=_WIN, sleep=sleep, replace=replace)
    assert len(calls) == 3  # non-vacuity: it really retried
    assert slept == list(rg.RETRY_DELAYS_S[:2])


def test_windows_gives_up_after_the_bounded_retries_with_a_remedy():
    calls: list[int] = []
    slept, sleep = _sleeps()

    def replace(src, dst):
        calls.append(1)
        raise PermissionError(13, "held open")

    with pytest.raises(rg.ReplaceBlockedError) as err:
        rg.replace_with_retry("a", "C:/x/nexus-service.exe", platform=_WIN,
                              sleep=sleep, replace=replace)
    assert len(calls) == len(rg.RETRY_DELAYS_S) + 1
    assert slept == list(rg.RETRY_DELAYS_S)
    assert "nexus-service.exe" in str(err.value)
    assert "nx daemon service stop" in str(err.value)
    assert isinstance(err.value.__cause__, PermissionError)
    assert isinstance(err.value, OSError)


def test_posix_does_not_retry_or_wrap():
    calls: list[int] = []
    slept, sleep = _sleeps()

    def replace(src, dst):
        calls.append(1)
        raise PermissionError(13, "denied")

    with pytest.raises(PermissionError) as err:
        rg.replace_with_retry("a", "b", platform="linux", sleep=sleep, replace=replace)
    assert not isinstance(err.value, rg.ReplaceBlockedError)
    assert calls == [1] and slept == []


def test_windows_does_not_retry_an_error_that_is_not_a_sharing_violation():
    calls: list[int] = []

    def replace(src, dst):
        calls.append(1)
        raise FileNotFoundError("gone")

    with pytest.raises(FileNotFoundError):
        rg.replace_with_retry("a", "b", platform=_WIN, sleep=lambda s: None, replace=replace)
    assert calls == [1]


# ── place_set_with_rollback ───────────────────────────────────────────────────


def _stage_new(tmp_path: Path) -> tuple[Path, Path]:
    dest = tmp_path / "service"
    stage = tmp_path / "stage"
    _write_set(dest, "old")
    _write_set(stage, "new")
    return stage, dest


def test_a_set_is_placed_whole_and_leaves_no_debris(tmp_path):
    stage, dest = _stage_new(tmp_path)
    rg.place_set_with_rollback(stage, dest, _NAMES, platform=_WIN, sleep=lambda s: None)
    assert _read_set(dest) == {n: f"new:{n}" for n in _NAMES}
    assert len(_NAMES) == 5  # non-vacuity: DLLs and the exe, not an empty set
    assert not [p for p in dest.iterdir() if p.name.startswith(".nx_old_")]


def test_a_failure_on_the_last_file_puts_the_whole_old_set_back(tmp_path):
    stage, dest = _stage_new(tmp_path)
    seen_new_dlls: list[bool] = []

    def replace(src, dst):
        if Path(dst).name == WINDOWS_ENGINE_EXE and Path(src).parent == stage:
            # the DLLs are already new at this moment: the set IS half replaced
            seen_new_dlls.append(
                all((dest / d).read_text().startswith("new:") for d in WINDOWS_RUNTIME_DLLS)
            )
            raise PermissionError(13, "exe held by antivirus")
        os.replace(src, dst)

    with pytest.raises(rg.ReplaceBlockedError):
        rg.place_set_with_rollback(
            stage, dest, _NAMES, platform=_WIN, sleep=lambda s: None, replace=replace,
        )
    assert seen_new_dlls and all(seen_new_dlls)  # non-vacuity: there was something to roll back
    assert _read_set(dest) == {n: f"old:{n}" for n in _NAMES}
    assert not [p for p in dest.iterdir() if p.name.startswith(".nx_old_")]


def test_a_transient_failure_on_the_exe_is_retried_and_the_set_completes(tmp_path):
    stage, dest = _stage_new(tmp_path)
    failures = {"left": 2}

    def replace(src, dst):
        if Path(dst).name == WINDOWS_ENGINE_EXE and failures["left"]:
            failures["left"] -= 1
            raise PermissionError(13, "scan in progress")
        os.replace(src, dst)

    rg.place_set_with_rollback(
        stage, dest, _NAMES, platform=_WIN, sleep=lambda s: None, replace=replace,
    )
    assert failures["left"] == 0
    assert _read_set(dest) == {n: f"new:{n}" for n in _NAMES}


def test_a_first_install_that_fails_leaves_nothing_behind(tmp_path):
    dest = tmp_path / "service"
    stage = tmp_path / "stage"
    dest.mkdir()
    _write_set(stage, "new")

    def replace(src, dst):
        if Path(dst).name == WINDOWS_ENGINE_EXE:
            raise PermissionError(13, "x")
        os.replace(src, dst)

    with pytest.raises(rg.ReplaceBlockedError):
        rg.place_set_with_rollback(
            stage, dest, _NAMES, platform=_WIN, sleep=lambda s: None, replace=replace,
        )
    assert list(dest.iterdir()) == []  # the DLLs placed before the failure are gone


def test_a_failed_rollback_keeps_the_old_content_and_names_where(tmp_path):
    stage, dest = _stage_new(tmp_path)
    first_dll = WINDOWS_RUNTIME_DLLS[0]

    def replace(src, dst):
        if Path(dst).name == WINDOWS_ENGINE_EXE and Path(src).parent == stage:
            raise PermissionError(13, "exe held")
        if Path(dst).name == first_dll and Path(src).parent != stage:
            raise PermissionError(13, "cannot restore")  # the rollback step
        os.replace(src, dst)

    with pytest.raises(rg.ReplaceBlockedError) as err:
        rg.place_set_with_rollback(
            stage, dest, _NAMES, platform=_WIN, sleep=lambda s: None, replace=replace,
        )
    text = str(err.value)
    assert first_dll in text and ".nx_old_" in text
    kept = [p for p in dest.iterdir() if p.name.startswith(".nx_old_")]
    assert len(kept) == 1
    assert (kept[0] / first_dll).read_text() == f"old:{first_dll}"


# ── quiesced ──────────────────────────────────────────────────────────────────


class _Ops:
    """Fake effects recording the order they ran in."""

    def __init__(self, *, outcome=None, pg_up=False, stop_pg_error=None, start_error=None):
        self.events: list[str] = []
        self.outcome = outcome if outcome is not None else _stopped(100, 200)
        self.pg_up = pg_up
        self.stop_pg_error = stop_pg_error
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
            if self.stop_pg_error:
                raise self.stop_pg_error

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


def _refusal(pid=4242, target=3, own=1):
    return GracefulStopSend(
        pid=pid, sent=False, refused=True, error=5, target_session=target, own_session=own,
    )


def test_posix_stops_and_starts_nothing(tmp_path):
    fake = _Ops()
    ran: list[str] = []
    with rq.quiesced(tmp_path, replacing="engine", platform="linux", ops=fake.ops()):
        ran.append("body")
    assert ran == ["body"] and fake.events == []


def test_engine_replacement_stops_then_replaces_then_starts_the_service(tmp_path):
    fake = _Ops(pg_up=True)
    with rq.quiesced(tmp_path, replacing="engine", platform=_WIN, ops=fake.ops()) as st:
        fake.events.append("body")
    assert fake.events == ["stop_service", "body", "start_service"]
    assert st.stopped_service and not st.stopped_pg
    assert "stop_pg" not in fake.events  # an engine replacement leaves PostgreSQL up


def test_bundle_replacement_stops_service_then_pg_and_starts_pg_first(tmp_path):
    fake = _Ops(pg_up=True)
    with rq.quiesced(tmp_path, replacing="pg_bundle", platform=_WIN, ops=fake.ops()):
        fake.events.append("body")
    assert fake.events == [
        "stop_service", "pg_running", "stop_pg", "body", "start_pg", "start_service",
    ]


def test_bundle_replacement_with_pg_down_stops_and_starts_no_pg(tmp_path):
    fake = _Ops(pg_up=False)
    with rq.quiesced(tmp_path, replacing="pg_bundle", platform=_WIN, ops=fake.ops()):
        pass
    assert fake.events == ["stop_service", "pg_running", "start_service"]


def test_only_what_was_stopped_is_started(tmp_path):
    fake = _Ops(outcome=_nothing_running(), pg_up=True)
    with rq.quiesced(tmp_path, replacing="pg_bundle", platform=_WIN, ops=fake.ops()):
        pass
    # service was not running: it stays down; PostgreSQL was, so it comes back
    assert fake.events == ["stop_service", "pg_running", "stop_pg", "start_pg"]


def test_a_service_in_another_session_is_refused_and_nothing_runs(tmp_path):
    refused = _refusal()
    fake = _Ops(outcome=SimpleNamespace(
        pids=(), stubborn=(4242,), refused=(refused,), source="refused"))
    body: list[str] = []
    with pytest.raises(rg.ReplaceBlockedError) as err:
        with rq.quiesced(tmp_path, replacing="engine", platform=_WIN, ops=fake.ops()):
            body.append("ran")
    msg = str(err.value)
    assert "session 3" in msg and "session 1" in msg
    assert "Run this upgrade from session 3" in msg
    assert "hard-killed" in msg and "no file was replaced" in msg
    assert body == []
    assert fake.events == ["stop_service"]  # no PG stop, no start


def test_a_refusal_without_a_session_difference_says_elevation(tmp_path):
    fake = _Ops(outcome=SimpleNamespace(
        pids=(), stubborn=(7,), refused=(_refusal(7, target=1, own=1),), source="refused"))
    with pytest.raises(rg.ReplaceBlockedError, match="elevation"):
        with rq.quiesced(tmp_path, replacing="engine", platform=_WIN, ops=fake.ops()):
            pass


def test_a_survivor_of_the_stop_blocks_the_replacement_without_a_start(tmp_path):
    fake = _Ops(outcome=SimpleNamespace(
        pids=(100,), stubborn=(100,), refused=(), source="lease"))
    with pytest.raises(rg.ReplaceBlockedError, match="survived"):
        with rq.quiesced(tmp_path, replacing="engine", platform=_WIN, ops=fake.ops()):
            pass
    assert "start_service" not in fake.events  # a start would short-circuit onto it


def test_a_failed_body_still_starts_what_was_stopped(tmp_path):
    fake = _Ops(pg_up=True)
    with pytest.raises(RuntimeError, match="boom"):
        with rq.quiesced(tmp_path, replacing="pg_bundle", platform=_WIN, ops=fake.ops()):
            raise RuntimeError("boom")
    assert fake.events[-2:] == ["start_pg", "start_service"]


def test_restart_after_false_skips_the_start_on_success_only(tmp_path):
    fake = _Ops()
    with rq.quiesced(tmp_path, replacing="engine", platform=_WIN,
                     restart_after=False, ops=fake.ops()):
        pass
    assert fake.events == ["stop_service"]

    fake = _Ops()
    with pytest.raises(RuntimeError):
        with rq.quiesced(tmp_path, replacing="engine", platform=_WIN,
                         restart_after=False, ops=fake.ops()):
            raise RuntimeError("install failed")
    assert fake.events == ["stop_service", "start_service"]


def test_a_start_that_fails_after_a_good_replacement_is_reported(tmp_path):
    fake = _Ops(start_error=RuntimeError("port busy"))
    with pytest.raises(rq.RestartAfterReplaceError, match="storage service.*port busy"):
        with rq.quiesced(tmp_path, replacing="engine", platform=_WIN, ops=fake.ops()):
            pass


def test_a_start_that_fails_on_the_failure_path_does_not_mask_the_error(tmp_path):
    fake = _Ops(start_error=RuntimeError("port busy"))
    with pytest.raises(ValueError, match="real cause") as err:
        with rq.quiesced(tmp_path, replacing="engine", platform=_WIN, ops=fake.ops()):
            raise ValueError("real cause")
    assert any("port busy" in n for n in getattr(err.value, "__notes__", []))


def test_a_pg_that_will_not_stop_restarts_the_service_and_refuses(tmp_path):
    fake = _Ops(pg_up=True, stop_pg_error=RuntimeError("pg_ctl timed out"))
    with pytest.raises(rg.ReplaceBlockedError, match="pg_ctl timed out"):
        with rq.quiesced(tmp_path, replacing="pg_bundle", platform=_WIN, ops=fake.ops()):
            pytest.fail("the body must not run")
    assert fake.events[-1] == "start_service"


# ── install_binary through the guard ──────────────────────────────────────────

_TAG = "engine-service-v0.1.200"
_ASSET = "nexus-service-windows-x64.txz"


def _archive(tag: str) -> bytes:
    buf = io.BytesIO()
    with tarfile.open(fileobj=buf, mode="w:xz") as tf:
        for name in _NAMES:
            data = f"{tag}:{name}".encode()
            info = tarfile.TarInfo(name)
            info.size = len(data)
            tf.addfile(info, io.BytesIO(data))
    return buf.getvalue()


class _OkChecker:
    def check(self, **_kw) -> None:
        return None


def _serve(monkeypatch, archive: bytes) -> None:
    def _dl(url, dest, *, timeout=0):
        if url.endswith(".sha256"):
            dest.write_text(f"{hashlib.sha256(archive).hexdigest()}  {_ASSET}\n")
        elif url.endswith(".sigstore.json"):
            dest.write_text("{}")
        else:
            dest.write_bytes(archive)

    monkeypatch.setattr(b, "_download", _dl)


def _install(tmp_path, monkeypatch, fake: _Ops, **kw):
    _serve(monkeypatch, _archive("new"))
    guard = rq.quiesced(tmp_path, replacing="engine", platform=_WIN, ops=fake.ops())
    return b.install_binary(
        _TAG, tmp_path, checker=_OkChecker(), download_dir=tmp_path,
        platform_tag="windows-x64", quiesce=guard, host_platform=_WIN, **kw,
    )


def test_install_stops_before_the_first_replace_and_starts_after_the_last(tmp_path, monkeypatch):
    svc = tmp_path / "service"
    _write_set(svc, "old")
    fake = _Ops()
    ops = fake.ops()
    observed: dict[str, dict[str, str]] = {}

    def stop_service():
        observed["at_stop"] = _read_set(svc)
        return _stopped(100, 200)

    def start_service():
        observed["at_start"] = _read_set(svc)

    ops.stop_service, ops.start_service = stop_service, start_service
    _serve(monkeypatch, _archive("new"))
    guard = rq.quiesced(tmp_path, replacing="engine", platform=_WIN, ops=ops)
    b.install_binary(
        _TAG, tmp_path, checker=_OkChecker(), download_dir=tmp_path,
        platform_tag="windows-x64", quiesce=guard, host_platform=_WIN,
    )
    assert observed["at_stop"] == {n: f"old:{n}" for n in _NAMES}  # nothing replaced yet
    assert observed["at_start"] == {n: f"new:{n}" for n in _NAMES}  # all replaced
    assert len(observed["at_start"]) == 5


def test_install_refused_across_sessions_replaces_nothing(tmp_path, monkeypatch):
    svc = tmp_path / "service"
    _write_set(svc, "old")
    fake = _Ops(outcome=SimpleNamespace(
        pids=(), stubborn=(9,), refused=(_refusal(9, target=2, own=1),), source="refused"))
    with pytest.raises(rg.ReplaceBlockedError, match="session 2"):
        _install(tmp_path, monkeypatch, fake)
    assert _read_set(svc) == {n: f"old:{n}" for n in _NAMES}
    assert fake.events == ["stop_service"]


def test_install_that_fails_midway_restores_the_old_engine_and_restarts(tmp_path, monkeypatch):
    svc = tmp_path / "service"
    _write_set(svc, "old")
    fake = _Ops()
    real = b.place_set_with_rollback

    def failing(stage, dest_dir, names, *, platform=None, **kw):
        def replace(src, dst):
            if Path(dst).name == WINDOWS_ENGINE_EXE and Path(src).parent == stage:
                raise PermissionError(13, "exe held")
            os.replace(src, dst)
        return real(stage, dest_dir, names, platform=platform,
                    sleep=lambda s: None, replace=replace)

    monkeypatch.setattr(b, "place_set_with_rollback", failing)
    with pytest.raises(rg.ReplaceBlockedError):
        _install(tmp_path, monkeypatch, fake)
    assert _read_set(svc) == {n: f"old:{n}" for n in _NAMES}
    assert fake.events == ["stop_service", "start_service"]
    assert not [p for p in svc.iterdir() if p.name.startswith((".nx_old_", ".nx_stage_"))]


def test_install_with_restart_after_false_leaves_the_start_to_the_caller(tmp_path, monkeypatch):
    fake = _Ops()
    _serve(monkeypatch, _archive("new"))
    guard = b._engine_quiesce(tmp_path, restart_after=False, host_platform=_WIN)
    # the default guard is the real one: swap its effects for the fakes
    monkeypatch.setattr(rq, "default_ops", lambda config_dir: fake.ops())
    b.install_binary(
        _TAG, tmp_path, checker=_OkChecker(), download_dir=tmp_path,
        platform_tag="windows-x64", quiesce=guard, host_platform=_WIN,
    )
    assert fake.events == ["stop_service"]


def test_install_on_a_posix_host_does_not_stop_anything(tmp_path, monkeypatch):
    fake = _Ops()
    monkeypatch.setattr(rq, "default_ops", lambda config_dir: fake.ops())
    _serve(monkeypatch, _archive("new"))
    b.install_binary(
        _TAG, tmp_path, checker=_OkChecker(), download_dir=tmp_path,
        platform_tag="windows-x64", host_platform="linux",
    )
    assert fake.events == []
    assert (tmp_path / "service" / WINDOWS_ENGINE_EXE).read_text() == f"new:{WINDOWS_ENGINE_EXE}"


# ── pg_bundle swap ────────────────────────────────────────────────────────────

_MARKER = pg_bundle._EXTRACT_MARKER


def _two_archives(tmp_path, make_pg_bundle_txz):
    first = make_pg_bundle_txz(tmp_path, "nexus-pg-first.txz")
    second = make_pg_bundle_txz(tmp_path / "second", "nexus-pg-second.txz")
    root = tmp_path / "pg-bundle"
    bin_dir = pg_bundle.extract_bundle(first, root, platform=_WIN)
    (bin_dir / "initdb").write_text("FROM-FIRST-ARCHIVE\n")
    return first, second, root, bin_dir


def test_bundle_swap_stops_pg_after_staging_and_starts_it_on_the_new_tree(
    tmp_path, make_pg_bundle_txz,
):
    _first, second, root, bin_dir = _two_archives(tmp_path, make_pg_bundle_txz)
    fake = _Ops(pg_up=True)
    ops = fake.ops()
    seen: dict[str, object] = {}

    def stop_pg():
        fake.events.append("stop_pg")
        seen["staging_ready"] = (tmp_path / "pg-bundle.incoming").is_dir()
        seen["tree_old_at_stop"] = (bin_dir / "initdb").read_text() == "FROM-FIRST-ARCHIVE\n"

    def start_pg():
        fake.events.append("start_pg")
        seen["tree_new_at_start"] = (bin_dir / "initdb").read_text() != "FROM-FIRST-ARCHIVE\n"
        seen["marked_new_at_start"] = (
            pg_bundle.is_bundle_extracted(root)
            and (root / _MARKER).read_text() == pg_bundle._archive_identity(second)
        )

    ops.stop_pg, ops.start_pg = stop_pg, start_pg
    pg_bundle.extract_bundle(second, root, platform=_WIN, quiesce=rq.quiesced(
        tmp_path, replacing="pg_bundle", platform=_WIN, ops=ops))

    assert fake.events == ["stop_service", "pg_running", "stop_pg", "start_pg", "start_service"]
    assert seen == {
        "staging_ready": True,  # the slow extraction ran BEFORE the stop
        "tree_old_at_stop": True,  # nothing swapped yet at the stop
        "tree_new_at_start": True,  # the swap is complete at the start
        "marked_new_at_start": True,  # and so is the marker the start finds binaries through
    }
    assert not (tmp_path / "pg-bundle.incoming").exists()
    assert not (tmp_path / "pg-bundle.replaced").exists()


def test_extracting_the_same_archive_again_stops_nothing(tmp_path, make_pg_bundle_txz):
    archive = make_pg_bundle_txz(tmp_path)
    root = tmp_path / "pg-bundle"
    pg_bundle.extract_bundle(archive, root, platform=_WIN)
    fake = _Ops(pg_up=True)
    pg_bundle.extract_bundle(archive, root, platform=_WIN, quiesce=rq.quiesced(
        tmp_path, replacing="pg_bundle", platform=_WIN, ops=fake.ops()))
    assert fake.events == []


def test_a_first_extraction_stops_nothing(tmp_path, make_pg_bundle_txz):
    archive = make_pg_bundle_txz(tmp_path)
    fake = _Ops(pg_up=True)
    pg_bundle.extract_bundle(archive, tmp_path / "pg-bundle", platform=_WIN,
                             quiesce=rq.quiesced(tmp_path, replacing="pg_bundle",
                                                 platform=_WIN, ops=fake.ops()))
    assert fake.events == []


def test_bundle_swap_refused_across_sessions_keeps_the_old_tree(tmp_path, make_pg_bundle_txz):
    _first, second, root, bin_dir = _two_archives(tmp_path, make_pg_bundle_txz)
    marker_before = (root / _MARKER).read_text()
    fake = _Ops(outcome=SimpleNamespace(
        pids=(), stubborn=(5,), refused=(_refusal(5, target=2, own=1),), source="refused"))
    with pytest.raises(rg.ReplaceBlockedError, match="session 2"):
        pg_bundle.extract_bundle(second, root, platform=_WIN, quiesce=rq.quiesced(
            tmp_path, replacing="pg_bundle", platform=_WIN, ops=fake.ops()))
    assert (bin_dir / "initdb").read_text() == "FROM-FIRST-ARCHIVE\n"
    assert (root / _MARKER).read_text() == marker_before
    assert pg_bundle.is_bundle_extracted(root)
    assert not (tmp_path / "pg-bundle.incoming").exists()  # staging cleaned up
    assert not (tmp_path / "pg-bundle.replaced").exists()
    assert fake.events == ["stop_service"]
