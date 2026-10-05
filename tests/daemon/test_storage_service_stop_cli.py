# SPDX-License-Identifier: AGPL-3.0-or-later
"""The CLI's end of the Windows stop channel (RDR-224, nexus-f9bgu.17).

``nx daemon service start`` spawns the supervisor with its own process group
and a hidden console; ``nx daemon service stop`` attaches to that console and
sends ``CTRL_BREAK``, confirms the stop by the supervisor's EXIT, and falls
back to the hard kill. A stop from another Windows session is refused loudly
and never hard-kills (Sam, 2026-10-05).

Both platforms run on every host: the Windows arm through an injected console
API (``platform="win32"``), the POSIX arm against real children. The
supervisors in these tests are REAL child processes, so "confirmed by exit"
and "never killed" are observed, not assumed.
"""
from __future__ import annotations

import contextlib
import os
import signal
import subprocess
import sys
import time
from pathlib import Path
from typing import Any
from unittest.mock import patch

import pytest
import structlog
from click.testing import CliRunner

from nexus.commands import daemon as daemon_mod
from nexus.daemon import storage_service_daemon as ssd
from nexus.daemon.service_registry import GracefulStopSend, LeaseRecord

CREATE_NEW_PROCESS_GROUP = 0x00000200
CREATE_NO_WINDOW = 0x08000000
DETACHED_PROCESS = 0x00000008


# ── fixtures ─────────────────────────────────────────────────────────────────────


@pytest.fixture
def config_dir(tmp_path: Path) -> Path:
    cd = tmp_path / "cfg"
    cd.mkdir(parents=True, exist_ok=True, mode=0o700)
    return cd


def _write_lease(config_dir: Path, *, supervisor_pid: int | None, engine_pid: int | None) -> Path:
    scope = str(os.getuid())
    record = LeaseRecord(
        scope_key=scope,
        generation=1,
        owner_token="test-owner-token",
        heartbeat_epoch=time.time() - 1.0,
        ttl=15.0,
        endpoint={"pid": engine_pid} if engine_pid is not None else {},
        version="0.0.0-test",
        payload={"supervisor_pid": supervisor_pid} if supervisor_pid is not None else {},
    )
    path = config_dir / f"storage_service_addr.{scope}"
    path.write_text(record.to_json())
    return path


def _spawn(*, ignore_term: bool) -> subprocess.Popen[bytes]:
    code = "import signal,time\n"
    if ignore_term:
        code += "signal.signal(signal.SIGTERM, signal.SIG_IGN)\n"
    code += "print('up', flush=True)\ntime.sleep(120)\n"
    proc = subprocess.Popen(  # noqa: S603 — fixed argv, this interpreter
        [sys.executable, "-c", code], stdout=subprocess.PIPE,
    )
    assert proc.stdout is not None
    assert proc.stdout.readline().strip() == b"up"
    return proc


@contextlib.contextmanager
def _reaped(proc: subprocess.Popen[bytes]):
    try:
        yield proc
    finally:
        with contextlib.suppress(OSError):
            proc.kill()
        with contextlib.suppress(subprocess.TimeoutExpired, ChildProcessError):
            proc.wait(timeout=5)


class _ConsoleApi:
    """Scripted console calls. ``deliver`` turns a successful send into a
    SIGTERM on the target (the break arriving); ``refuse`` pids fail attach
    with access denied. The sequence is recorded for ordering assertions."""

    def __init__(self, *, deliver: bool = True, refuse: frozenset[int] = frozenset()) -> None:
        self.deliver = deliver
        self.refuse = refuse
        self.calls: list[tuple[str, int | None]] = []

    def free_console(self) -> bool:
        self.calls.append(("free", None))
        return True

    def attach_console(self, pid: int) -> tuple[bool, int]:
        self.calls.append(("attach", pid))
        return (False, 5) if pid in self.refuse else (True, 0)

    def generate_ctrl_break(self, pid: int) -> tuple[bool, int]:
        self.calls.append(("send", pid))
        if self.deliver:
            os.kill(pid, signal.SIGTERM)
        return True, 0

    def attach_parent_console(self) -> tuple[bool, int]:
        self.calls.append(("parent", None))
        return True, 0

    def session_of(self, pid: int) -> int | None:
        return 1 if pid in self.refuse else 0


def _no_sweep(monkeypatch: pytest.MonkeyPatch) -> list[str]:
    """Replace the process-table sweep with an empty, recorded one."""
    from nexus.daemon import service_registry as sr

    calls: list[str] = []

    def fake_sweep(matcher: Any, **_k: Any) -> sr.ProcessSweepResult:
        calls.append("sweep")
        return sr.ProcessSweepResult(available=True, error=None, found=(), stubborn=())

    monkeypatch.setattr(sr, "sweep_matching_processes", fake_sweep)
    return calls


# ── step 3: how the CLI spawns the supervisor ────────────────────────────────────


def test_windows_supervisor_gets_its_own_group_and_a_hidden_console_never_detached() -> None:
    kw = daemon_mod._supervisor_popen_kwargs(platform="win32")
    assert kw == {"creationflags": CREATE_NEW_PROCESS_GROUP | CREATE_NO_WINDOW}
    # DETACHED_PROCESS leaves no console to attach to, so CTRL_BREAK could not
    # reach the supervisor (T2 nexus_rdr/224-research-20).
    assert kw["creationflags"] & DETACHED_PROCESS == 0


def test_posix_supervisor_spawn_is_unchanged() -> None:
    assert daemon_mod._supervisor_popen_kwargs(platform="linux") == {"start_new_session": True}


def test_ensure_storage_supervisor_spawns_with_those_kwargs(
    config_dir: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    captured: dict[str, Any] = {}

    class _Stop(Exception):
        pass

    def fake_popen(argv: list[str], **kw: Any) -> None:
        captured.update(kw)
        raise _Stop

    monkeypatch.setattr(daemon_mod, "_popen", fake_popen)
    with pytest.raises(_Stop):
        daemon_mod.ensure_storage_supervisor(config_dir, platform="win32")
    assert captured["creationflags"] == CREATE_NEW_PROCESS_GROUP | CREATE_NO_WINDOW
    assert "start_new_session" not in captured
    assert captured["stdin"] is subprocess.DEVNULL


# ── steps 5 and 6: the CLI stop, confirmed by exit ───────────────────────────────


def test_windows_stop_attaches_sends_and_confirms_by_the_supervisors_exit(
    config_dir: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    sweep = _no_sweep(monkeypatch)
    api = _ConsoleApi(deliver=True)
    with _reaped(_spawn(ignore_term=False)) as sup:
        _write_lease(config_dir, supervisor_pid=sup.pid, engine_pid=None)
        with patch("os.kill", wraps=os.kill) as spy:
            outcome = ssd.stop_storage_service(
                config_dir=config_dir, platform="win32", console_api=api,
            )
        assert sup.wait(timeout=30) == -signal.SIGTERM  # it received the delivered break and exited
    assert api.calls == [("free", None), ("attach", sup.pid), ("send", sup.pid), ("parent", None)]
    assert outcome.pids == (sup.pid,) and outcome.stubborn == () and outcome.refused == ()
    assert outcome.source == "lease"
    # No hard kill: the only os.kill was the scripted delivery of the break.
    sent = [c.args[1] for c in spy.call_args_list if c.args[0] == sup.pid and c.args[1] != 0]
    assert sent == [signal.SIGTERM]
    assert sweep == ["sweep"], "the tree-completion sweep still runs after the lease branch"


def test_a_supervisor_that_ignores_the_break_is_hard_killed_after_the_grace_and_it_is_logged(
    config_dir: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    _no_sweep(monkeypatch)
    monkeypatch.setattr(ssd, "_SUPERVISOR_STOP_GRACE", 0.6)
    api = _ConsoleApi(deliver=False)  # TRUE returned, nothing delivered
    with _reaped(_spawn(ignore_term=True)) as sup:
        _write_lease(config_dir, supervisor_pid=sup.pid, engine_pid=None)
        t0 = time.monotonic()
        with structlog.testing.capture_logs() as logs:
            outcome = ssd.stop_storage_service(
                config_dir=config_dir, platform="win32", console_api=api,
            )
        elapsed = time.monotonic() - t0
        assert sup.wait(timeout=30) == -signal.SIGKILL
    assert ("send", sup.pid) in api.calls, "non-vacuity: the break WAS sent and ignored"
    assert elapsed >= 0.6
    assert outcome.stubborn == ()
    unclean = [e for e in logs if e["event"] == "storage_service_supervisor_unclean_stop"]
    assert len(unclean) == 1 and unclean[0]["pid"] == sup.pid


def test_a_stop_from_another_session_is_refused_and_kills_nothing(
    config_dir: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Sam DECIDED 2026-10-05: cross-session stop fails loud, never hard-kills."""
    sweep = _no_sweep(monkeypatch)
    monkeypatch.setattr(ssd, "_SUPERVISOR_STOP_GRACE", 0.3)
    with _reaped(_spawn(ignore_term=False)) as sup:
        api = _ConsoleApi(refuse=frozenset({sup.pid}))
        lease = _write_lease(config_dir, supervisor_pid=sup.pid, engine_pid=None)
        real_kill = os.kill

        def only_probes(pid: int, sig: int) -> None:
            assert sig == 0, f"a refused stop must not signal (sent {sig})"
            real_kill(pid, sig)  # the liveness probe is signal 0

        with patch("os.kill", side_effect=only_probes):
            outcome = ssd.stop_storage_service(
                config_dir=config_dir, platform="win32", console_api=api,
            )
        assert sup.poll() is None, "the supervisor must still be running"
        assert lease.exists(), "a refused stop must not touch the lease"
    assert [r.pid for r in outcome.refused] == [sup.pid]
    assert outcome.refused[0].refused is True
    assert (outcome.refused[0].target_session, outcome.refused[0].own_session) == (1, 0)
    assert outcome.pids == () and outcome.stubborn == (sup.pid,)
    assert outcome.source == "refused"
    assert not outcome.already_stopped
    assert sweep == [], "the sweep would try the same attach; nothing more to do"
    assert ("send", sup.pid) not in api.calls


def test_lease_with_no_supervisor_pid_signals_the_engine_through_the_console_on_windows(
    config_dir: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    _no_sweep(monkeypatch)
    api = _ConsoleApi(deliver=True)
    monkeypatch.setattr(
        "nexus.util.process_group.safe_killpg",
        lambda *a, **k: pytest.fail("Windows must not take the TerminateProcess path first"),
    )
    with _reaped(_spawn(ignore_term=False)) as engine:
        lease = _write_lease(config_dir, supervisor_pid=None, engine_pid=engine.pid)
        outcome = ssd.stop_storage_service(
            config_dir=config_dir, platform="win32", console_api=api,
        )
        assert engine.wait(timeout=30) == -signal.SIGTERM
    assert api.calls[:3] == [("free", None), ("attach", engine.pid), ("send", engine.pid)]
    assert outcome.pids == (engine.pid,) and outcome.refused == ()
    assert not lease.exists(), "the lease is relinquished once the engine is gone"


def test_an_engine_that_ignores_the_break_is_hard_killed_on_the_no_supervisor_branch(
    config_dir: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    _no_sweep(monkeypatch)
    monkeypatch.setattr(ssd, "_GRACEFUL_STOP_TIMEOUT", 0.5)
    api = _ConsoleApi(deliver=False)
    with _reaped(_spawn(ignore_term=True)) as engine:
        _write_lease(config_dir, supervisor_pid=None, engine_pid=engine.pid)
        with structlog.testing.capture_logs() as logs:
            ssd.stop_storage_service(config_dir=config_dir, platform="win32", console_api=api)
        assert engine.wait(timeout=30) == -signal.SIGKILL
    assert any(e["event"] == "storage_service_engine_unclean_stop" for e in logs)


def test_a_refused_engine_signal_on_the_no_supervisor_branch_kills_nothing(
    config_dir: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    _no_sweep(monkeypatch)
    with _reaped(_spawn(ignore_term=False)) as engine:
        api = _ConsoleApi(refuse=frozenset({engine.pid}))
        lease = _write_lease(config_dir, supervisor_pid=None, engine_pid=engine.pid)
        outcome = ssd.stop_storage_service(
            config_dir=config_dir, platform="win32", console_api=api,
        )
        assert engine.poll() is None
        assert lease.exists()
    assert [r.pid for r in outcome.refused] == [engine.pid]
    assert outcome.source == "refused"


def test_posix_stop_never_touches_a_console(
    config_dir: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    _no_sweep(monkeypatch)
    api = _ConsoleApi()
    with _reaped(_spawn(ignore_term=False)) as sup:
        _write_lease(config_dir, supervisor_pid=sup.pid, engine_pid=None)
        outcome = ssd.stop_storage_service(
            config_dir=config_dir, platform="linux", console_api=api,
        )
        assert sup.wait(timeout=30) == -signal.SIGTERM
    assert api.calls == []
    assert outcome.pids == (sup.pid,) and outcome.refused == ()


def test_the_sweep_refusal_reaches_the_outcome(
    config_dir: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A lease MISS whose process-table sweep finds a process in another
    session: the outcome carries the refusal rather than reading 'stopped'."""
    from nexus.daemon import service_registry as sr

    refusal = GracefulStopSend(pid=999001, sent=False, refused=True, error=5,
                               target_session=1, own_session=0)

    def fake_sweep(matcher: Any, **_k: Any) -> sr.ProcessSweepResult:
        return sr.ProcessSweepResult(
            available=True, error=None, found=((999001, "nexus-service"),),
            stubborn=(999001,), refused=(refusal,),
        )

    monkeypatch.setattr(sr, "sweep_matching_processes", fake_sweep)
    outcome = ssd.stop_storage_service(config_dir=config_dir)
    assert outcome.refused == (refusal,)
    assert outcome.source == "refused"
    assert not outcome.already_stopped


# ── step 8: what the CLI says ────────────────────────────────────────────────────


def _invoke_stop(outcome: ssd.StopOutcome, config_dir: Path):
    with patch.object(ssd, "stop_storage_service", return_value=outcome):
        return CliRunner().invoke(
            daemon_mod.service_stop_cmd, ["--config-dir", str(config_dir)],
        )


def test_refusal_message_names_the_owning_session_and_the_remedy_and_exits_nonzero(
    config_dir: Path,
) -> None:
    refusal = GracefulStopSend(pid=4242, sent=False, refused=True, error=5,
                               target_session=1, own_session=0)
    outcome = ssd.StopOutcome(
        pids=(), stubborn=(4242,), source="refused", lease_seen=True, refused=(refusal,),
    )
    result = _invoke_stop(outcome, config_dir)
    text = result.output
    assert result.exit_code == 1
    assert "REFUSED" in text
    assert "pid 4242" in text
    assert "Windows session 1" in text and "session 0" in text
    assert "nx daemon service stop" in text and "from session 1" in text
    assert "nothing was signalled or killed" in text.lower()
    # The two lines that would be false here must be absent.
    assert "already stopped" not in text
    assert "survived the stop escalation" not in text
    assert "Storage service stopped" not in text


def test_refusal_without_session_ids_still_says_what_to_do(config_dir: Path) -> None:
    refusal = GracefulStopSend(pid=4242, sent=False, refused=True, error=5)
    outcome = ssd.StopOutcome(
        pids=(), stubborn=(4242,), source="refused", lease_seen=True, refused=(refusal,),
    )
    result = _invoke_stop(outcome, config_dir)
    assert result.exit_code == 1
    assert "pid 4242" in result.output
    assert "access was denied" in result.output
    assert "nothing was signalled or killed" in result.output.lower()


def test_a_normal_stop_message_is_unchanged(config_dir: Path) -> None:
    outcome = ssd.StopOutcome(pids=(4242,), stubborn=(), source="lease", lease_seen=True)
    result = _invoke_stop(outcome, config_dir)
    assert "Storage service stopped (pid(s)=4242)." in result.output
    assert "REFUSED" not in result.output
