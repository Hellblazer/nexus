# SPDX-License-Identifier: AGPL-3.0-or-later
"""The in-process PostgreSQL stop at Windows session end (RDR-224, nexus-f9bgu.51 round 3).

Windows does not let a NEW process initialise in a session that is logging off. The round-2
guest run showed the window path working end to end and then ``pg_ctl`` dying in 29 ms with
``0xC000026B`` (``STATUS_DLL_INIT_FAILED``) at ``WM_ENDSESSION``, after which the postmaster
was killed with the session. So the stop spawns nothing: it reads the postmaster pid from
``postmaster.pid``, checks the image is ``postgres``, writes the fast-shutdown signal byte to
``\\\\.\\pipe\\pgsignal_<pid>`` (what ``pg_ctl`` itself does) and waits for the process to exit.

Every OS touchpoint (pid read, image identity, pipe write, process wait, clock) is a seam, so
all of it runs on any host. The real kernel is in ``tests/test_session_end_real_windows.py``.
"""
from __future__ import annotations

import sys
from pathlib import Path

import pytest

import nexus.db.pg_provision as pgp
from nexus.daemon import session_end as se
from nexus.daemon import storage_service_daemon as ssd
from tests.daemon._logs import info_logs


class _Clock:
    def __init__(self) -> None:
        self.now = 100.0

    def __call__(self) -> float:
        return self.now


class _FakePgApi:
    """A scripted kernel. ``calls`` records the order the stopper touched it in."""

    def __init__(
        self,
        clock: _Clock,
        *,
        image: str = "postgres",
        delivered: bool = True,
        error: int = 0,
        exits: bool = True,
        signal_s: float = 0.0,
        wait_s: float = 0.0,
    ) -> None:
        self.clock = clock
        self.image, self.delivered, self.error, self.exits = image, delivered, error, exits
        self.signal_s, self.wait_s = signal_s, wait_s
        self.calls: list[tuple[str, tuple]] = []

    def image_stem(self, pid: int) -> str:
        self.calls.append(("image", (pid,)))
        return self.image

    def send_signal(self, pid: int, signo: int, timeout_ms: int) -> tuple[bool, int]:
        self.calls.append(("signal", (pid, signo, timeout_ms)))
        self.clock.now += self.signal_s
        return self.delivered, self.error

    def wait_exit(self, pid: int, timeout_s: float) -> bool:
        self.calls.append(("wait", (pid, timeout_s)))
        self.clock.now += self.wait_s
        return self.exits

    def names(self) -> list[str]:
        return [n for n, _ in self.calls]


def _stopper(api: _FakePgApi, clock: _Clock, pid: int | None = 4321) -> tuple:
    return se.make_inprocess_pg_stopper(
        pgdata="unused", api=api, read_pid=lambda: pid, clock=clock,
    )


def test_the_fast_signal_is_sigint_two() -> None:
    # PostgreSQL maps SIGTERM=smart, SIGINT=fast, SIGQUIT=immediate; pg_ctl -m fast sends SIGINT.
    assert se.PG_SIGNAL_FAST == 2


def test_the_stop_checks_identity_then_signals_then_waits_and_spawns_nothing() -> None:
    clock = _Clock()
    api = _FakePgApi(clock)
    with info_logs() as logs:
        assert _stopper(api, clock)(2.5) is True
    assert api.names() == ["image", "signal", "wait"]
    assert api.calls[1][1][:2] == (4321, 2)
    events = [e["event"] for e in logs]
    assert events == ["session_end_pg_signal_sent", "session_end_pg_exited"]


def test_a_reused_pid_that_is_not_postgres_is_never_signalled() -> None:
    clock = _Clock()
    api = _FakePgApi(clock, image="notepad")
    with info_logs() as logs:
        assert _stopper(api, clock)(2.5) is False
    assert api.names() == ["image"], "no signal, no wait"
    assert [e["event"] for e in logs] == ["session_end_pg_pid_not_postgres"]


def test_a_missing_or_unreadable_postmaster_pid_means_nothing_is_running() -> None:
    clock = _Clock()
    api = _FakePgApi(clock)
    assert _stopper(api, clock, pid=None)(2.5) is True
    assert api.calls == []


def test_a_pid_with_no_process_means_a_stale_file_and_nothing_to_stop() -> None:
    clock = _Clock()
    api = _FakePgApi(clock, image="")
    assert _stopper(api, clock)(2.5) is True
    assert api.names() == ["image"]


def test_a_signal_that_is_not_delivered_is_not_stopped_and_is_logged_with_the_error() -> None:
    clock = _Clock()
    api = _FakePgApi(clock, delivered=False, error=2)  # ERROR_FILE_NOT_FOUND: no pipe
    with info_logs() as logs:
        assert _stopper(api, clock)(2.5) is False
    assert api.names() == ["image", "signal"], "no wait for a process that was not told to stop"
    (entry,) = logs
    assert entry["event"] == "session_end_pg_signal_failed" and entry["error"] == 2


def test_a_postmaster_that_outlives_the_budget_is_logged_as_a_timeout() -> None:
    clock = _Clock()
    api = _FakePgApi(clock, exits=False)
    with info_logs() as logs:
        assert _stopper(api, clock)(2.5) is False
    assert [e["event"] for e in logs] == ["session_end_pg_signal_sent", "session_end_pg_exit_timeout"]


def test_the_pipe_timeout_is_one_second_and_the_wait_gets_what_the_signal_left() -> None:
    clock = _Clock()
    api = _FakePgApi(clock, signal_s=0.4)
    _stopper(api, clock)(3.0)
    pipe_timeout_ms = api.calls[1][1][2]
    wait_budget = api.calls[2][1][1]
    assert pipe_timeout_ms == 1000
    assert wait_budget == pytest.approx(3.0 - 0.4)


def test_a_small_budget_caps_the_pipe_timeout_but_never_below_a_floor() -> None:
    clock = _Clock()
    api = _FakePgApi(clock)
    _stopper(api, clock)(0.5)
    assert api.calls[1][1][2] == 500
    api2 = _FakePgApi(clock)
    _stopper(api2, clock)(0.0)
    assert api2.calls[1][1][2] == 100
    assert api2.calls[2][1][1] >= 0.1


# ── postmaster.pid ───────────────────────────────────────────────────────────────


def test_the_postmaster_pid_is_the_first_line_of_the_file(tmp_path: Path) -> None:
    (tmp_path / "postmaster.pid").write_text("4242\nC:/pgdata\n1700000000\n5432\n", encoding="utf-8")
    assert se.read_postmaster_pid(str(tmp_path)) == 4242


@pytest.mark.parametrize("content", ["", "\n", "abc\n", "0\n", "-5\n"])
def test_a_postmaster_pid_file_without_a_positive_pid_reads_as_none(
    tmp_path: Path, content: str,
) -> None:
    (tmp_path / "postmaster.pid").write_text(content, encoding="utf-8")
    assert se.read_postmaster_pid(str(tmp_path)) is None


def test_a_missing_postmaster_pid_file_reads_as_none(tmp_path: Path) -> None:
    assert se.read_postmaster_pid(str(tmp_path)) is None


def test_the_pid_is_read_when_the_handler_runs_not_when_it_is_installed(tmp_path: Path) -> None:
    clock = _Clock()
    api = _FakePgApi(clock)
    stop = se.make_inprocess_pg_stopper(pgdata=str(tmp_path), api=api, clock=clock)
    (tmp_path / "postmaster.pid").write_text("777\n", encoding="utf-8")  # written AFTER install
    assert stop(2.0) is True
    assert api.calls[0] == ("image", (777,))


# ── wiring ───────────────────────────────────────────────────────────────────────


def test_the_supervisors_session_end_stopper_never_discovers_or_runs_pg_ctl(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def boom(*_a: object, **_k: object) -> object:
        raise AssertionError("pg_ctl must not be found or spawned on the session-end path")

    monkeypatch.setattr(pgp, "discover_pg_binaries", boom)
    monkeypatch.setattr(pgp, "_run", boom)
    built: list[str] = []
    monkeypatch.setattr(
        se, "make_inprocess_pg_stopper",
        lambda *, pgdata, **kw: (built.append(pgdata), lambda b: True)[1],
    )
    stop = ssd._session_end_pg_stopper({"PG_DATA": "C:/pgdata"})
    assert stop(1.0) is True
    assert built == ["C:/pgdata"]


def test_the_supervisors_session_end_stopper_needs_pg_data() -> None:
    with pytest.raises(ssd.StorageServiceStartError):
        ssd._session_end_pg_stopper({})


def test_building_the_real_binding_off_windows_fails_loudly_at_install_time() -> None:
    # The wiring builds it inside a try at install, so a failure degrades to "no handler"
    # there; it must fail HERE (install), never later inside the handler.
    if sys.platform == "win32":
        pytest.skip("this host has the Windows kernel")
    with pytest.raises(AttributeError):
        se.make_inprocess_pg_stopper(pgdata="x")
