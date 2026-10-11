# SPDX-License-Identifier: AGPL-3.0-or-later
"""The engine substrate proves a port belongs to ITS OWN boot (nexus-kmfz6).

On a host where several pytest sessions run at once as one user, two sessions can
draw the same port: ``_free_port`` probes by bind and releases, and the engine JVM
binds much later. A bare connect-check then passed against the sibling's listener,
the JVM died on its bind failure, and every later ``mint_test_tenant`` failed with
``Server disconnected without sending a response`` (1,197 setup errors on one worker,
all naming 127.0.0.1:20184). When the sibling's listener is another session's ENGINE
the failure is silent: the test bearer is a constant, so this worker's tests would
run against a foreign engine.

These tests drive ``_boot()`` against fake process machinery, with a REAL socket
listener (accepts and closes, as a sibling's postmaster does) squatting a port the
boot is handed. No real PG, no real JVM::

    NX_TEST_T2_SUBSTRATE=none uv run pytest tests/test_engine_substrate_port_ownership.py -q
"""
from __future__ import annotations

import contextlib
import socket
import subprocess
import tempfile
import threading
import time
import warnings
from collections.abc import Iterator
from pathlib import Path

import pytest

from nexus.daemon import service_registry
from tests import _engine_substrate as sub
from tests._module_seam import setattr_in


class _ForeignListener:
    """A real TCP listener on a kernel-assigned port that accepts and closes every
    connection: what a sibling session's postmaster looks like to a connect-check."""

    def __init__(self) -> None:
        self._sock = socket.socket()
        self._sock.bind(("127.0.0.1", 0))
        self._sock.listen(16)
        self.port: int = self._sock.getsockname()[1]
        self._stop = threading.Event()
        self._thread = threading.Thread(target=self._serve, daemon=True)
        self._thread.start()

    def _serve(self) -> None:
        while not self._stop.is_set():
            try:
                conn, _ = self._sock.accept()
            except OSError:
                return
            conn.close()

    def close(self) -> None:
        self._stop.set()
        # Unblock accept() with a throwaway connection, then release the port.
        with contextlib.suppress(OSError), socket.create_connection(("127.0.0.1", self.port), timeout=1):
            pass
        self._sock.close()
        self._thread.join(timeout=5)


#: Ports a test hands the boot. Every one is bound on port 0 by the test and the
#: socket stays open until the test ends, so the number is never a probe-and-release
#: guess that a sibling session (or this very process) could take in between.
_HELD: dict[int, socket.socket] = {}
#: Held sockets a fake engine has turned into listeners.
_ENGINE_SOCKETS: list[socket.socket] = []


def _reserve_port() -> int:
    """A port bound on port 0 and held open for the test's whole life."""
    sock = socket.socket()
    sock.bind(("127.0.0.1", 0))
    port: int = sock.getsockname()[1]
    _HELD[port] = sock
    return port


@pytest.fixture(autouse=True)
def _close_held_sockets() -> Iterator[None]:
    yield
    for sock in (*_ENGINE_SOCKETS, *_HELD.values()):
        sock.close()
    _ENGINE_SOCKETS.clear()
    _HELD.clear()


@pytest.fixture
def foreign_listener() -> Iterator[_ForeignListener]:
    listener = _ForeignListener()
    try:
        yield listener
    finally:
        listener.close()


class _Harness:
    """Fake process machinery for one ``_boot()``; engine spawns follow ``engine_script``."""

    def __init__(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        self.engine_script: list[str] = []
        self.engine_ports: list[int] = []
        self.pg_ctl_script: list[str] = []
        self.pg_ports: list[int] = []
        self.spawned_ports: list[int] = []
        self.killed: list[int] = []
        self.pg_ctl_ports: list[int] = []
        self.pg_stops: list[str] = []
        harness = self

        real_mkdtemp = tempfile.mkdtemp

        def _fake_mkdtemp(*, prefix, dir=None):  # noqa: A002 — mirrors tempfile.mkdtemp
            return real_mkdtemp(prefix=prefix, dir=str(tmp_path))

        setattr_in(monkeypatch, sub, "tempfile.mkdtemp", _fake_mkdtemp)
        monkeypatch.setattr(sub, "sweep_stale_substrate_clusters", lambda **_kw: sub.SweepResult())
        monkeypatch.setattr(sub, "_boot_semaphore_slot", contextlib.nullcontext)
        monkeypatch.setattr(sub, "jar_freshness_skip_reason", lambda *_a, **_kw: None)
        monkeypatch.setattr(sub, "_pg_bin_resolved", tmp_path)
        monkeypatch.setattr(sub, "_read_postmaster_pid", lambda _pgdata: 313131)

        def _fake_run(args, **_kwargs):
            if args and "pg_ctl" in str(args[0]) and "stop" in args:
                harness.pg_stops.append(str(args[args.index("-D") + 1]))
            if args and "pg_ctl" in str(args[0]) and "start" in args:
                port = int(str(args[args.index("-o") + 1]).split("-p ")[1].split()[0])
                harness.pg_ctl_ports.append(port)
                verdict = harness.pg_ctl_script.pop(0) if harness.pg_ctl_script else "ok"
                if verdict == "port_in_use":
                    Path(args[args.index("-l") + 1]).write_text(
                        "FATAL:  could not bind IPv4 address \"127.0.0.1\": Address already in use\n",
                    )
                    return subprocess.CompletedProcess(args, 1, stdout="", stderr="could not start server")
            return subprocess.CompletedProcess(args, 0, stdout="", stderr="")

        setattr_in(monkeypatch, (sub, "tests._pg_ctl"), "subprocess.run", _fake_run)
        monkeypatch.setattr(service_registry, "run_bounded", _fake_run)

        class _FakeEngine:
            def __init__(self, _argv, *, env, stdout, **_kwargs) -> None:
                self.pid = 424242
                port = int(env["NX_SERVICE_PORT"])
                harness.spawned_ports.append(port)
                verdict = harness.engine_script.pop(0) if harness.engine_script else "ok"
                self.returncode: int | None = None
                self._polls_before_death: int | None = None
                if verdict in ("ok", "wrong_port", "longer_port", "dies_after_ready"):
                    # An engine that bound its port: the held socket starts listening.
                    listener = _HELD.pop(port)
                    listener.listen(16)
                    _ENGINE_SOCKETS.append(listener)
                    logged = {"wrong_port": port + 1, "longer_port": f"{port}4"}.get(verdict, port)
                    stdout.write(f"INFO  NexusService - event=service_started port={logged}\n".encode())
                    if verdict == "dies_after_ready":
                        # Alive for the first poll, found dead on the next.
                        self._polls_before_death = 1
                elif verdict == "bind_lost":
                    stdout.write(b"Exception in thread \"main\" java.net.BindException: Address already in use\n")
                    self.returncode = 1
                elif verdict == "crash":
                    stdout.write(b"Exception in thread \"main\" java.lang.IllegalStateException: boom\n")
                    self.returncode = 1
                # "silent": alive, says nothing (a JVM that has not bound its port)
                stdout.flush()

            def poll(self) -> int | None:
                if self._polls_before_death is not None:
                    if self._polls_before_death > 0:
                        self._polls_before_death -= 1
                        return None
                    self.returncode = 1
                return self.returncode

        setattr_in(monkeypatch, sub, "subprocess.Popen", _FakeEngine)
        monkeypatch.setattr(sub, "kill_group", lambda proc: harness.killed.append(proc.pid))

        ports = iter(self._port_feed)

        def _fake_free_port(**_kw) -> int:
            return next(ports)

        monkeypatch.setattr(sub, "_free_port", _fake_free_port)

    @property
    def _port_feed(self) -> Iterator[int]:
        # PG port first, then (on a PG retry) more PG ports, then engine ports; each list
        # is consumed lazily so a test can fill them after construction.
        yield from self._drain(self.pg_ports)
        yield from self._drain(self.engine_ports)

    @staticmethod
    def _drain(ports: list[int]) -> Iterator[int]:
        while ports:
            yield ports.pop(0)


def _harness(tmp_path, monkeypatch, *, pg_ports, engine_ports, engine_script, pg_ctl_script=()) -> _Harness:
    h = _Harness(tmp_path, monkeypatch)
    h.pg_ports.extend(pg_ports)
    h.engine_ports.extend(engine_ports)
    h.engine_script.extend(engine_script)
    h.pg_ctl_script.extend(pg_ctl_script)
    return h


class _AliveProc:
    """A process that never exits."""

    def poll(self) -> int | None:
        return None


class TestEngineListenerIdentity:
    def test_a_foreign_listener_on_the_engine_port_is_not_ready(
        self, tmp_path, monkeypatch, foreign_listener,
    ) -> None:
        """The JVM has not bound the port; a foreign listener answers. The connect
        succeeds, and the boot must still not report ready."""
        monkeypatch.setattr(sub, "_ENGINE_READY_TIMEOUT_S", 0.5, raising=False)
        monkeypatch.setattr(sub, "_ENGINE_POLL_S", 0.01, raising=False)
        _harness(
            tmp_path, monkeypatch,
            pg_ports=[_reserve_port()],
            engine_ports=[foreign_listener.port],
            engine_script=["silent"],
        )
        with pytest.raises(RuntimeError, match=str(foreign_listener.port)):
            sub._boot()

    def test_an_engine_that_lost_the_bind_race_is_retried_on_a_new_port(
        self, tmp_path, monkeypatch, foreign_listener,
    ) -> None:
        """The reported shape: the JVM dies on its bind failure while a foreign
        listener holds the port and the connect-check passes."""
        monkeypatch.setattr(sub, "_ENGINE_POLL_S", 0.01, raising=False)
        second = _reserve_port()
        h = _harness(
            tmp_path, monkeypatch,
            pg_ports=[_reserve_port()],
            engine_ports=[foreign_listener.port, second],
            engine_script=["bind_lost", "ok"],
        )
        with pytest.warns(UserWarning, match=str(foreign_listener.port)):
            state = sub._boot()
        assert state["base_url"] == f"http://127.0.0.1:{second}"
        assert h.spawned_ports == [foreign_listener.port, second]
        assert h.killed, "the engine that lost the race is torn down before the retry"

    def test_an_engine_crash_that_is_not_a_bind_failure_is_not_retried_and_fails_fast(
        self, tmp_path, monkeypatch,
    ) -> None:
        """A dead JVM is read from ``poll()``, not waited out: with a 30 s readiness
        timeout in force the failure must still land in seconds, carrying the log tail."""
        monkeypatch.setattr(sub, "_ENGINE_READY_TIMEOUT_S", 30.0, raising=False)
        monkeypatch.setattr(sub, "_ENGINE_POLL_S", 0.01, raising=False)
        h = _harness(
            tmp_path, monkeypatch,
            pg_ports=[_reserve_port()],
            engine_ports=[_reserve_port(), _reserve_port()],
            engine_script=["crash", "ok"],
        )
        started = time.monotonic()
        with pytest.raises(RuntimeError, match="boom") as err:
            sub._boot()
        assert time.monotonic() - started < 10.0, "a crash waited out the readiness timeout"
        assert "Tail of engine log" in str(err.value)
        assert "IllegalStateException" in str(err.value)
        assert len(h.spawned_ports) == 1

    @pytest.mark.parametrize("verdict", ["wrong_port", "longer_port"])
    def test_a_ready_line_for_another_port_is_not_ready(
        self, tmp_path, monkeypatch, verdict,
    ) -> None:
        """The right event for a DIFFERENT port (``port=N+1``), and a LONGER port that
        begins with this one's digits (``port=N4``), are not this engine's ready line,
        even though the listener answers a connect."""
        monkeypatch.setattr(sub, "_ENGINE_READY_TIMEOUT_S", 0.5, raising=False)
        monkeypatch.setattr(sub, "_ENGINE_POLL_S", 0.01, raising=False)
        _harness(
            tmp_path, monkeypatch,
            pg_ports=[_reserve_port()],
            engine_ports=[_reserve_port()],
            engine_script=[verdict],
        )
        with pytest.raises(RuntimeError, match="did not bind port"):
            sub._boot()

    def test_the_ready_line_ends_at_the_port_number_with_either_line_ending(
        self, tmp_path,
    ) -> None:
        port = _reserve_port()
        _HELD[port].listen(16)  # the readiness wait also connects to the port
        log = tmp_path / "engine.log"
        for ending in ("\n", "\r\n", ""):
            log.write_text(f"INFO  NexusService - {sub._ENGINE_READY_PREFIX}{port}{ending}")
            sub._wait_engine_ready(_AliveProc(), port, str(log), timeout=0.2)

    def test_an_engine_that_dies_after_the_ready_line_is_not_ready(
        self, tmp_path, monkeypatch,
    ) -> None:
        """Marker present and the port answers, then the process is found dead."""
        monkeypatch.setattr(sub, "_ENGINE_READY_TIMEOUT_S", 5.0, raising=False)
        monkeypatch.setattr(sub, "_ENGINE_POLL_S", 0.01, raising=False)
        h = _harness(
            tmp_path, monkeypatch,
            pg_ports=[_reserve_port()],
            engine_ports=[_reserve_port(), _reserve_port()],
            engine_script=["dies_after_ready", "ok"],
        )
        with pytest.raises(RuntimeError, match="exited with rc=1"):
            sub._boot()
        assert len(h.spawned_ports) == 1
        assert h.killed, "the dead engine's group is torn down"

    def test_an_engine_timeout_closes_the_log_handle_and_stops_postgres(
        self, tmp_path, monkeypatch,
    ) -> None:
        """All three failure branches end in the same state: group killed, log handle
        closed, Postgres stopped."""
        monkeypatch.setattr(sub, "_ENGINE_READY_TIMEOUT_S", 0.3, raising=False)
        monkeypatch.setattr(sub, "_ENGINE_POLL_S", 0.01, raising=False)
        opened: list = []
        real_open = open

        def _tracking_open(file, mode="r", *a, **kw):
            fh = real_open(file, mode, *a, **kw)
            if str(file).endswith("engine.log") and "w" in mode:
                opened.append(fh)
            return fh

        monkeypatch.setattr("builtins.open", _tracking_open)
        h = _harness(
            tmp_path, monkeypatch,
            pg_ports=[_reserve_port()],
            engine_ports=[_reserve_port()],
            engine_script=["silent"],
        )
        with pytest.raises(RuntimeError, match="did not bind port"):
            sub._boot()
        assert h.killed
        assert opened and all(fh.closed for fh in opened), "engine log handle left open"
        assert h.pg_stops, "Postgres is stopped on the way out"

    def test_the_retry_is_bounded_then_names_the_port_and_a_foreign_listener(
        self, tmp_path, monkeypatch, foreign_listener,
    ) -> None:
        monkeypatch.setattr(sub, "_ENGINE_POLL_S", 0.01, raising=False)
        attempts = sub._PORT_RACE_ATTEMPTS
        ports = [foreign_listener.port] + [_reserve_port() for _ in range(attempts - 1)]
        h = _harness(
            tmp_path, monkeypatch,
            pg_ports=[_reserve_port()],
            engine_ports=ports,
            engine_script=["bind_lost"] * attempts,
        )
        with warnings.catch_warnings(record=True) as caught:
            warnings.simplefilter("always")
            with pytest.raises(RuntimeError) as err:
                sub._boot()
        assert len(h.spawned_ports) == attempts
        message = str(err.value)
        assert str(ports[-1]) in message, "the error names the port of the last attempt"
        assert "foreign listener" in message
        warned = [str(w.message) for w in caught if "bind" in str(w.message).lower()]
        assert len(warned) == attempts - 1, "one warning per lost race that is retried"
        for port, text in zip(ports, warned, strict=False):
            assert str(port) in text


class TestPostgresPortRace:
    def test_a_postmaster_that_lost_the_bind_race_is_retried_on_a_new_port(
        self, tmp_path, monkeypatch, foreign_listener,
    ) -> None:
        second = _reserve_port()
        h = _harness(
            tmp_path, monkeypatch,
            pg_ports=[foreign_listener.port, second],
            engine_ports=[_reserve_port()],
            engine_script=["ok"],
            pg_ctl_script=["port_in_use", "ok"],
        )
        with pytest.warns(UserWarning, match=str(foreign_listener.port)):
            state = sub._boot()
        assert h.pg_ctl_ports == [foreign_listener.port, second]
        assert state["pg_port"] == second

    def test_the_postgres_retry_is_bounded_and_names_the_port(
        self, tmp_path, monkeypatch, foreign_listener,
    ) -> None:
        attempts = sub._PORT_RACE_ATTEMPTS
        ports = [foreign_listener.port] + [_reserve_port() for _ in range(attempts - 1)]
        _harness(
            tmp_path, monkeypatch,
            pg_ports=ports,
            engine_ports=[_reserve_port()],
            engine_script=["ok"],
            pg_ctl_script=["port_in_use"] * attempts,
        )
        with warnings.catch_warnings():
            warnings.simplefilter("ignore")
            with pytest.raises(RuntimeError) as err:
                sub._boot()
        assert str(ports[-1]) in str(err.value)
        assert "foreign listener" in str(err.value)


class TestSessionsDoNotShareASlice:
    """Two concurrent sessions' gw0 used to draw from the SAME slice (nexus-kmfz6)."""

    @pytest.fixture(autouse=True)
    def _clean_env(self, monkeypatch) -> None:
        monkeypatch.delenv("PYTEST_XDIST_WORKER_COUNT", raising=False)
        monkeypatch.delenv("PYTEST_XDIST_TESTRUNUID", raising=False)

    def _slice(self, monkeypatch, run_uid: str, worker: str, count: int = 4) -> range:
        monkeypatch.setenv("PYTEST_XDIST_TESTRUNUID", run_uid)
        monkeypatch.setenv("PYTEST_XDIST_WORKER", worker)
        monkeypatch.setenv("PYTEST_XDIST_WORKER_COUNT", str(count))
        return sub._worker_shard_range()

    def test_the_same_worker_index_in_different_sessions_gets_different_slices(
        self, monkeypatch,
    ) -> None:
        slices = {
            tuple(self._slice(monkeypatch, f"session-{n}", "gw0")[:1]) for n in range(12)
        }
        assert len(slices) > 1, "every session's gw0 drew the same slice"

    def test_one_sessions_workers_stay_disjoint_at_every_worker_count(
        self, monkeypatch,
    ) -> None:
        for run_uid in ("session-a", "session-b", "session-c"):
            for count in (4, 18, 19, 40):
                seen: set[int] = set()
                for index in range(count):
                    shard = self._slice(monkeypatch, run_uid, f"gw{index}", count)
                    assert shard != sub._LOW_PORT_RANGE
                    assert not (set(shard) & seen), (run_uid, count, index)
                    assert shard.start >= sub._LOW_PORT_RANGE.start
                    assert shard.stop <= sub._LOW_PORT_RANGE.stop
                    seen |= set(shard)

    def test_a_session_keeps_one_offset_across_its_workers_and_calls(
        self, monkeypatch,
    ) -> None:
        first = self._slice(monkeypatch, "session-a", "gw1")
        again = self._slice(monkeypatch, "session-a", "gw1")
        assert first == again

    def test_without_a_run_uid_the_offset_comes_from_the_controller_pid(
        self, monkeypatch,
    ) -> None:
        monkeypatch.setenv("PYTEST_XDIST_WORKER", "gw0")
        monkeypatch.setenv("PYTEST_XDIST_WORKER_COUNT", "4")
        starts = set()
        for pid in range(1000, 1012):
            setattr_in(monkeypatch, sub, "os.getppid", lambda pid=pid: pid)
            first = sub._worker_shard_range()
            assert sub._worker_shard_range() == first, "stable within one controller"
            starts.add(first.start)
        assert len(starts) > 1, "two controllers' gw0 drew the same slice"
