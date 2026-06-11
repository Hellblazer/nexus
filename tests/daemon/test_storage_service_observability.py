# SPDX-License-Identifier: AGPL-3.0-or-later
"""nexus-ovbr7: daemon observability — no daemon child is ever silent.

Four storage-service supervisor deaths (latest 2026-06-11) left zero
artifacts: ``run_storage_supervisor`` never configured file logging (its
structlog events went to a DEVNULL'd stderr), the JAR child was spawned
with stdout/stderr -> DEVNULL while its logback config is console-only,
and the t3 supervisor/chroma pair share both gaps. These tests pin the
class-wide fix, mirroring the nexus-n8sbw t2_daemon precedent:

1. ``run_storage_supervisor`` / ``run_t3_supervisor`` route structlog to
   ``<config_dir>/logs/storage_service.log`` / ``t3_daemon.log`` and
   leave start/exit breadcrumbs plus a crash backstop.
2. Child processes (jar, chroma) get their stdout/stderr redirected to
   ``<config_dir>/logs/storage_service_jar.log`` / ``t3_chroma.log``
   via ``nexus.logging_setup.open_child_log`` — never DEVNULL.
3. A jar/chroma exit is logged WITH its returncode.
4. The detached ``nx daemon service start`` spawn routes the child's
   stdout/stderr to the supervisor log so a crash BEFORE
   ``configure_logging`` runs (import error, bad argv) is captured.
5. ``nx daemon service status`` surfaces the log paths.
"""
from __future__ import annotations

import os
import signal
import subprocess
import threading
import time
from pathlib import Path
from typing import Any

import pytest
import structlog
from click.testing import CliRunner

import nexus.daemon.storage_service_daemon as ssd
import nexus.daemon.t3_daemon as t3d
from nexus.cli import main


@pytest.fixture(autouse=True)
def _restore_structlog_after_test():
    """configure_logging swaps structlog's logger_factory; restore so the
    rest of the suite keeps the default PrintLoggerFactory behaviour."""
    import logging
    import logging.handlers

    saved = structlog.get_config()
    yield
    structlog.configure(**saved)
    # Drop any file handlers the test added to the root logger.
    root = logging.getLogger()
    for h in list(root.handlers):
        if isinstance(h, logging.handlers.RotatingFileHandler):
            root.removeHandler(h)
            h.close()


@pytest.fixture
def config_dir(tmp_path: Path) -> Path:
    cd = tmp_path / "cfg"
    cd.mkdir(parents=True, exist_ok=True, mode=0o700)
    return cd


def _write_creds(config_dir: Path) -> Path:
    creds = config_dir / "pg_credentials"
    creds.write_text(
        "PG_PORT=15432\n"
        "PG_DATA=/tmp/testpgdata\n"
        "NX_DB_URL=jdbc:postgresql://127.0.0.1:15432/nexus\n"
        "NX_DB_USER=nexus_svc\n"
        "NX_DB_PASS=testsvcpass\n"
        "NX_DB_ADMIN_URL=jdbc:postgresql://127.0.0.1:15432/nexus\n"
        "NX_DB_ADMIN_USER=nexus_admin\n"
        "NX_DB_ADMIN_PASS=testadminpass\n"
        "NX_SERVICE_TOKEN=root-token-deadbeef\n"
    )
    creds.chmod(0o600)
    return creds


class _RecordingLog:
    """structlog stand-in recording (method, event, kwargs) tuples."""

    def __init__(self) -> None:
        self.calls: list[tuple[str, str, dict[str, Any]]] = []

    def _record(self, method: str):
        def _call(event: str, **kw: Any) -> None:
            self.calls.append((method, event, kw))

        return _call

    def __getattr__(self, name: str):
        return self._record(name)

    def events(self) -> list[str]:
        return [e for _, e, _ in self.calls]

    def kwargs_for(self, event: str) -> dict[str, Any]:
        for _, e, kw in self.calls:
            if e == event:
                return kw
        raise AssertionError(f"event {event!r} not logged; saw {self.events()}")


class _FakeProc:
    def __init__(self, pid: int = 42001, returncode: int | None = None) -> None:
        self.pid = pid
        self._returncode = returncode

    @property
    def returncode(self) -> int | None:
        return self._returncode

    def poll(self) -> int | None:
        return self._returncode


# ---------------------------------------------------------------------------
# 2: jar child stdout/stderr go to a log file, never DEVNULL
# ---------------------------------------------------------------------------


class TestJarChildLogging:
    def _spawn_with_captured_popen(
        self, config_dir: Path, monkeypatch: pytest.MonkeyPatch,
    ) -> dict[str, Any]:
        captured: dict[str, Any] = {}

        def _fake_popen(argv, **kwargs):
            captured["argv"] = argv
            captured.update(kwargs)
            return _FakeProc()

        monkeypatch.setattr(ssd.subprocess, "Popen", _fake_popen)
        monkeypatch.setattr(
            ssd.StorageServiceSupervisor, "_find_java", lambda self: "/usr/bin/true",
        )
        # Skip the credential-chain lookup for the voyage key.
        monkeypatch.setenv("NX_VOYAGE_API_KEY", "test-key")

        sup = ssd.StorageServiceSupervisor(
            config_dir=config_dir,
            jar_path=Path("/fake/nexus-service.jar"),
            pg_port=15432,
            service_port=18080,
            creds={
                "NX_DB_URL": "jdbc:...", "NX_DB_USER": "svc",
                "NX_DB_PASS": "p", "NX_DB_ADMIN_URL": "jdbc:...",
                "NX_DB_ADMIN_USER": "a", "NX_DB_ADMIN_PASS": "ap",
                "PG_PORT": "15432", "PG_DATA": "/tmp/pgdata",
                "NX_SERVICE_TOKEN": "tok-deadbeef",
            },
        )
        sup._spawn_service()
        return captured

    def test_jar_stdout_routed_to_log_file_not_devnull(
        self, config_dir: Path, monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        captured = self._spawn_with_captured_popen(config_dir, monkeypatch)
        assert captured["stdout"] is not subprocess.DEVNULL, (
            "jar stdout is still DEVNULL — the silent-death class survives"
        )
        assert captured["stderr"] is not subprocess.DEVNULL
        # Both streams land in the SAME file so interleaved JVM banners,
        # logback output, and stack traces keep their relative order.
        name = getattr(captured["stdout"], "name", "")
        assert str(name).endswith("logs/storage_service_jar.log"), name
        assert captured["stderr"] is captured["stdout"]

    def test_jar_log_lives_under_config_dir(
        self, config_dir: Path, monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        captured = self._spawn_with_captured_popen(config_dir, monkeypatch)
        name = Path(getattr(captured["stdout"], "name"))
        assert name == config_dir / "logs" / "storage_service_jar.log"
        assert name.parent.is_dir()


# ---------------------------------------------------------------------------
# 3: jar exit is logged with its returncode
# ---------------------------------------------------------------------------


class TestJarExitCodeLogged:
    def test_heartbeat_logs_jar_exit_with_returncode(
        self, config_dir: Path, monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        rec = _RecordingLog()
        monkeypatch.setattr(ssd, "_log", rec)
        sup = ssd.StorageServiceSupervisor(
            config_dir=config_dir,
            jar_path=Path("/fake/nexus-service.jar"),
            pg_port=15432,
            service_port=18080,
            creds={"NX_SERVICE_TOKEN": "tok-deadbeef", "PG_PORT": "15432"},
        )
        sup._proc = _FakeProc(pid=777, returncode=137)
        sup._supervisor = object()  # truthy; heartbeat returns before using it

        jar_running, _pg = sup.heartbeat_once()
        assert jar_running is False
        kw = rec.kwargs_for("storage_service_jar_exit_detected")
        assert kw.get("returncode") == 137
        assert kw.get("pid") == 777


class TestChromaExitCodeLogged:
    def test_t3_heartbeat_logs_chroma_exit_with_returncode(
        self, config_dir: Path, monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        rec = _RecordingLog()
        monkeypatch.setattr(t3d, "_log", rec)
        sup = t3d.T3Supervisor(
            config_dir=config_dir, local_path=config_dir / "chroma",
        )
        sup._proc = _FakeProc(pid=888, returncode=9)
        sup._supervisor = object()

        assert sup.heartbeat_once() is False
        kw = rec.kwargs_for("t3_chroma_exit_detected")
        assert kw.get("returncode") == 9
        assert kw.get("pid") == 888


# ---------------------------------------------------------------------------
# 2 (t3 mirror): chroma child stdout/stderr go to a log file
# ---------------------------------------------------------------------------


class TestChromaChildLogging:
    def test_chroma_stdout_routed_to_log_file_not_devnull(
        self, config_dir: Path, monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        captured: dict[str, Any] = {}

        def _fake_popen(argv, **kwargs):
            captured["argv"] = argv
            captured.update(kwargs)
            return _FakeProc()

        monkeypatch.setattr(t3d.subprocess, "Popen", _fake_popen)
        monkeypatch.setattr(t3d, "_find_chroma", lambda: "/usr/bin/true")
        monkeypatch.setattr(t3d, "_wait_for_ready", lambda *a, **k: None)

        sup = t3d.T3Supervisor(
            config_dir=config_dir, local_path=config_dir / "chroma",
        )
        sup._spawn_chroma()

        assert captured["stdout"] is not subprocess.DEVNULL
        name = Path(getattr(captured["stdout"], "name"))
        assert name == config_dir / "logs" / "t3_chroma.log"
        assert captured["stderr"] is captured["stdout"]


# ---------------------------------------------------------------------------
# 1: supervisor entry points configure file logging + breadcrumbs
# ---------------------------------------------------------------------------


class _FakeStorageSupervisor:
    """Stands in for StorageServiceSupervisor inside run_storage_supervisor."""

    instances: list["_FakeStorageSupervisor"] = []
    start_raises: Exception | None = None

    def __init__(self, **kwargs: Any) -> None:
        self.kwargs = kwargs
        self.started = False
        self.stopped = False
        type(self).instances.append(self)

    def start(self) -> None:
        if type(self).start_raises is not None:
            raise type(self).start_raises
        self.started = True

    def heartbeat_once(self) -> tuple[bool, bool]:
        return True, True

    def stop(self) -> None:
        self.stopped = True


@pytest.fixture
def fake_storage_sup(monkeypatch: pytest.MonkeyPatch) -> type[_FakeStorageSupervisor]:
    _FakeStorageSupervisor.instances = []
    _FakeStorageSupervisor.start_raises = None
    monkeypatch.setattr(ssd, "StorageServiceSupervisor", _FakeStorageSupervisor)
    monkeypatch.setattr(ssd, "DEFAULT_HEARTBEAT_INTERVAL", 0.01)
    return _FakeStorageSupervisor


def _sigterm_after(delay: float) -> threading.Timer:
    t = threading.Timer(delay, lambda: os.kill(os.getpid(), signal.SIGTERM))
    t.start()
    return t


class TestSupervisorLifecycleLog:
    def test_run_storage_supervisor_writes_breadcrumbs(
        self, config_dir: Path, fake_storage_sup, monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        _write_creds(config_dir)
        timer = _sigterm_after(0.15)
        try:
            code = ssd.run_storage_supervisor(
                config_dir=config_dir, jar_path=Path("/fake/nexus-service.jar"),
            )
        finally:
            timer.cancel()
        assert code == 0

        log_path = config_dir / "logs" / "storage_service.log"
        assert log_path.exists(), (
            "supervisor produced no log file; it is still silent"
        )
        text = log_path.read_text()
        assert "storage_service_supervisor_started" in text
        assert "storage_service_supervisor_exit" in text

    def test_run_storage_supervisor_crash_backstop_logs_exception(
        self, config_dir: Path, fake_storage_sup, monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        _write_creds(config_dir)
        fake_storage_sup.start_raises = RuntimeError("boom at startup")
        with pytest.raises(RuntimeError, match="boom at startup"):
            ssd.run_storage_supervisor(
                config_dir=config_dir, jar_path=Path("/fake/nexus-service.jar"),
            )
        text = (config_dir / "logs" / "storage_service.log").read_text()
        assert "storage_service_supervisor_crashed" in text
        assert "boom at startup" in text


class _FakeT3Supervisor:
    instances: list["_FakeT3Supervisor"] = []

    def __init__(self, **kwargs: Any) -> None:
        self.kwargs = kwargs
        type(self).instances.append(self)

    def start(self) -> None:
        pass

    def heartbeat_once(self) -> bool:
        return True

    def stop(self) -> None:
        pass


class TestT3SupervisorLifecycleLog:
    def test_run_t3_supervisor_writes_breadcrumbs(
        self, config_dir: Path, monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        _FakeT3Supervisor.instances = []
        monkeypatch.setattr(t3d, "T3Supervisor", _FakeT3Supervisor)
        monkeypatch.setattr(t3d, "DEFAULT_HEARTBEAT_INTERVAL", 0.01)
        timer = _sigterm_after(0.15)
        try:
            code = t3d.run_t3_supervisor(
                config_dir=config_dir, local_path=config_dir / "chroma",
            )
        finally:
            timer.cancel()
        assert code == 0
        log_path = config_dir / "logs" / "t3_daemon.log"
        assert log_path.exists()
        text = log_path.read_text()
        assert "t3_supervisor_started" in text
        assert "t3_supervisor_exit" in text


# ---------------------------------------------------------------------------
# 4: the detached spawn captures pre-configure_logging crashes
# ---------------------------------------------------------------------------


class _FakeLeaseRecord:
    def __init__(self) -> None:
        self.endpoint = {"host": "127.0.0.1", "port": 1234, "pid": 4321}
        self.generation = 7
        self.version = "5.10.6"
        self.heartbeat_epoch = time.time()
        self.status = "live"
        self.payload = {"supervisor_pid": 4320}


class TestDetachedSpawnCapture:
    def test_service_start_spawn_routes_output_to_supervisor_log(
        self, config_dir: Path, monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        import nexus.commands.daemon as dm
        from nexus.daemon import service_registry as sr

        captured: dict[str, Any] = {}
        discover_calls = {"n": 0}

        class _FakeRegistry:
            def __init__(self, **kwargs: Any) -> None:
                pass

            def discover(self, scope: str):
                discover_calls["n"] += 1
                # None before the spawn, a live record right after — so the
                # command exits without burning its 60s readiness budget.
                return None if "argv" not in captured else _FakeLeaseRecord()

        def _fake_popen(argv, **kwargs):
            captured["argv"] = argv
            captured.update(kwargs)
            return _FakeProc()

        monkeypatch.setattr(sr, "ServiceRegistry", _FakeRegistry)
        monkeypatch.setattr(dm.subprocess, "Popen", _fake_popen)

        result = CliRunner().invoke(
            main, ["daemon", "service", "start", "--config-dir", str(config_dir)],
        )
        assert result.exit_code == 0, result.output
        assert captured["stdout"] is not subprocess.DEVNULL, (
            "detached spawn still DEVNULLs — a crash before configure_logging "
            "(import error, bad argv) remains invisible"
        )
        # The crash channel is a SEPARATE file from the structlog file: the
        # daemon drops its stderr handler post-configure (non-tty), so this
        # file holds only pre-configure failures and fatal tracebacks.
        name = Path(getattr(captured["stdout"], "name"))
        assert name == config_dir / "logs" / "storage_service.crash.log"
        assert captured["stderr"] is captured["stdout"]


# ---------------------------------------------------------------------------
# 5: status surfaces the log paths
# ---------------------------------------------------------------------------


class TestStatusLogPaths:
    def test_status_surfaces_log_paths(
        self, config_dir: Path, monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        import nexus.commands.daemon as dm
        from nexus.daemon import service_registry as sr
        from nexus.daemon import jar_lifecycle as jl

        class _FakeRegistry:
            def __init__(self, **kwargs: Any) -> None:
                pass

            def discover(self, scope: str):
                return _FakeLeaseRecord()

        monkeypatch.setattr(sr, "ServiceRegistry", _FakeRegistry)
        monkeypatch.setattr(dm, "_probe_health", lambda host, port: "ok")
        monkeypatch.setattr(
            dm, "_probe_pg",
            lambda creds_path: {"pg": "up", "pg_data": "/tmp/testpgdata"},
        )
        monkeypatch.setattr(jl, "fetch_service_version", lambda host, port: None)

        result = CliRunner().invoke(
            main, ["daemon", "service", "status", "--config-dir", str(config_dir)],
        )
        assert result.exit_code == 0, result.output
        out = result.output
        assert str(config_dir / "logs" / "storage_service.log") in out
        assert str(config_dir / "logs" / "storage_service_jar.log") in out
        assert str(config_dir / "logs" / "storage_service.crash.log") in out
        # pg_log derives from the probed pg_data.
        assert "/tmp/testpgdata/pg.log" in out
