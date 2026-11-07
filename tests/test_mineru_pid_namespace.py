# SPDX-License-Identifier: AGPL-3.0-or-later
"""A MinerU pid file from another PID namespace is judged by its server, not its pid (nexus-dyfsw site 3).

A server started from another PID namespace that shares the config directory (a container sharing
HOME) has a pid this process cannot see. Unlinking its pid file let a second server start. The
first fix judged any ``/health`` 200 on the recorded port as alive and wedged ``nx mineru`` for an
ordinary user after a port reuse. The rule here: the file records ``pid_ns``; only a file from
ANOTHER namespace is judged on its port, by the MinerU counters in the body (or a young
``started_at``); a file from our own namespace, or with no namespace, is judged by pid as before.
"""
from __future__ import annotations

import json
import os
import socket
import threading
from collections.abc import Callable, Iterator
from datetime import datetime, timedelta, timezone
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest
from click.testing import CliRunner
from tests._module_seam import patch_in

import nexus._mineru_pid as mp
from nexus.commands.mineru import mineru_group
from nexus.daemon import service_registry as sr

_OURS = "pid:[4026531836]"
_THEIRS = "pid:[4026539999]"
_DEAD_PID = 999_999_002
_MINERU_BODY = {"processing_tasks": 0, "queued_tasks": 0, "completed_tasks": 3}


def _serve(body: bytes, status: int = 200) -> Iterator[int]:
    class _Handler(BaseHTTPRequestHandler):
        def do_GET(self) -> None:  # noqa: N802 — http.server's name
            self.send_response(status if self.path == "/health" else 404)
            self.send_header("Content-Type", "application/json")
            self.end_headers()
            self.wfile.write(body)

        def log_message(self, *a) -> None:
            pass

    server = HTTPServer(("127.0.0.1", 0), _Handler)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    try:
        yield server.server_address[1]
    finally:
        server.shutdown()
        server.server_close()


@pytest.fixture
def mineru_port() -> Iterator[int]:
    """A real server that answers /health the way MinerU does."""
    yield from _serve(json.dumps(_MINERU_BODY).encode())


@pytest.fixture
def other_service_port() -> Iterator[int]:
    """A real server that answers /health 200 and is not MinerU (a port reused by something else)."""
    yield from _serve(b'{"status": "ok"}')


@pytest.fixture
def silent_port() -> Iterator[int]:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        port = s.getsockname()[1]
    yield port  # released again: a connection is refused at once, not left to time out


@pytest.fixture
def config_dir(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    monkeypatch.setenv("NEXUS_CONFIG_DIR", str(tmp_path))
    monkeypatch.setattr(sr, "own_pid_namespace", lambda: _OURS)
    monkeypatch.setattr(mp, "is_process_alive", lambda pid: False)  # the recorded pid is invisible here
    return tmp_path


def _info(port: int, *, ns: str | None = _THEIRS, age_s: float = 3600.0, pid: int = _DEAD_PID) -> dict:
    started = datetime.now(timezone.utc) - timedelta(seconds=age_s)
    info: dict = {"pid": pid, "port": port, "started_at": started.isoformat()}
    if ns is not None:
        info["pid_ns"] = ns
    return info


def _write(config_dir: Path, info: dict) -> Path:
    path = config_dir / "mineru.pid"
    path.write_text(json.dumps(info))
    return path


# ── the namespace rule is the leases' rule ───────────────────────────────────


def test_namespace_rule_is_the_leases_rule(config_dir: Path) -> None:
    assert mp.pid_file_from_other_namespace({"pid": 1, "pid_ns": _THEIRS})
    assert not mp.pid_file_from_other_namespace({"pid": 1, "pid_ns": _OURS})
    assert not mp.pid_file_from_other_namespace({"pid": 1})  # an older client records none
    # The same answer the lease reads give for the same payload.
    for payload in ({"pid_ns": _THEIRS}, {"pid_ns": _OURS}, {}):
        record = sr.LeaseRecord(
            scope_key="s", generation=1, owner_token="t", heartbeat_epoch=0.0, ttl=1.0,
            endpoint={}, version="v", payload=payload,
        )
        assert sr.lease_from_other_pid_namespace(record) == mp.pid_file_from_other_namespace(payload)


# ── recorded_server_alive ────────────────────────────────────────────────────


def test_a_foreign_file_whose_port_serves_mineru_is_alive(config_dir: Path, mineru_port: int) -> None:
    assert mp.recorded_server_alive(_info(mineru_port))


def test_a_foreign_file_whose_port_was_reused_by_another_service_is_stale(
    config_dir: Path, other_service_port: int,
) -> None:
    """The first attempt's wedge: a bare 200 is not identity."""
    assert not mp.recorded_server_alive(_info(other_service_port))


def test_a_foreign_file_whose_port_serves_text_is_stale(config_dir: Path) -> None:
    for port in _serve(b"OK"):
        assert not mp.recorded_server_alive(_info(port))


def test_a_foreign_file_with_a_silent_port_is_stale_once_it_is_old(config_dir: Path, silent_port: int) -> None:
    assert not mp.recorded_server_alive(_info(silent_port, age_s=3600))


def test_a_young_foreign_file_may_still_be_loading_models(config_dir: Path, silent_port: int) -> None:
    assert mp.recorded_server_alive(_info(silent_port, age_s=10))
    # ...for the warm-up grace only; ``now`` is the seam, so no clock is patched.
    info = _info(silent_port, age_s=0)
    started = datetime.fromisoformat(info["started_at"]).timestamp()
    assert mp.recorded_server_alive(info, now=started + mp.FOREIGN_WARMUP_GRACE_S - 1)
    assert not mp.recorded_server_alive(info, now=started + mp.FOREIGN_WARMUP_GRACE_S + 1)


def test_a_local_process_with_the_recorded_number_proves_nothing_for_a_foreign_file(
    config_dir: Path, silent_port: int, monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(mp, "is_process_alive", lambda pid: True)
    assert not mp.recorded_server_alive(_info(silent_port, pid=os.getpid()))


@pytest.mark.parametrize("ns", [_OURS, None], ids=["own namespace", "no namespace recorded"])
def test_an_ordinary_file_is_judged_by_pid_and_never_by_a_port(
    config_dir: Path, mineru_port: int, ns: str | None, monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Port reuse must not wedge an ordinary user: a dead pid is stale although the port answers."""
    info = _info(mineru_port, ns=ns)
    assert not mp.recorded_server_alive(info)
    monkeypatch.setattr(mp, "is_process_alive", lambda pid: True)
    assert mp.recorded_server_alive(info)


def test_a_malformed_file_is_stale(config_dir: Path) -> None:
    assert not mp.recorded_server_alive({"port": 1})
    assert not mp.recorded_server_alive({"pid": True, "port": 1})
    assert not mp.server_identified({"port": True})
    assert not mp.server_identified({"port": 70000})


# ── the spawn records the namespace ──────────────────────────────────────────


def test_spawn_records_the_pid_namespace(config_dir: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    from nexus._mineru_spawn import spawn_server_process

    proc = MagicMock(pid=4321)
    with patch("nexus._mineru_spawn._resolve_mineru_api_bin", return_value="/fake/bin/mineru-api"), \
            patch("nexus._mineru_spawn._mineru_output_root", return_value=config_dir), \
            patch("nexus.logging_setup.open_child_log_or_devnull", return_value=MagicMock()), \
            patch_in("nexus._mineru_spawn", "subprocess.Popen", return_value=proc):
        assert spawn_server_process(8011) is proc
    assert json.loads((config_dir / "mineru.pid").read_text())["pid_ns"] == _OURS


# ── nx mineru start / stop / status ──────────────────────────────────────────


@pytest.fixture
def no_signal(monkeypatch: pytest.MonkeyPatch) -> Callable[[], None]:
    """Fail the test if anything tries to signal the recorded server's group."""
    import nexus._mineru_spawn as spawn

    def boom(*a, **k):
        raise AssertionError("a server in another PID namespace must never be signalled")

    monkeypatch.setattr(spawn, "stop_server_group", boom)
    monkeypatch.setattr(spawn, "classify_server_pid", boom)
    return lambda: None


def test_stop_keeps_the_file_of_a_foreign_server_that_is_up(
    config_dir: Path, mineru_port: int, no_signal,
) -> None:
    path = _write(config_dir, _info(mineru_port))
    result = CliRunner().invoke(mineru_group, ["stop"])
    assert result.exit_code == 1
    assert "another PID namespace" in result.output
    assert "stop --force" in result.output
    assert path.exists()


def test_stop_force_clears_a_foreign_file_without_signalling(
    config_dir: Path, mineru_port: int, no_signal,
) -> None:
    path = _write(config_dir, _info(mineru_port))
    result = CliRunner().invoke(mineru_group, ["stop", "--force"])
    assert result.exit_code == 0, result.output
    assert "nothing was signalled" in result.output
    assert not path.exists()


def test_stop_clears_a_foreign_file_whose_port_was_reused(
    config_dir: Path, other_service_port: int, no_signal,
) -> None:
    path = _write(config_dir, _info(other_service_port))
    result = CliRunner().invoke(mineru_group, ["stop"])
    assert result.exit_code == 0, result.output
    assert "stale PID" in result.output
    assert not path.exists()


def test_stop_clears_an_ordinary_stale_file_although_the_port_answers(
    config_dir: Path, mineru_port: int,
) -> None:
    """The wedge the first attempt caused: dead pid, own namespace, port reused by a MinerU-shaped server."""
    path = _write(config_dir, _info(mineru_port, ns=_OURS))
    result = CliRunner().invoke(mineru_group, ["stop"])
    assert result.exit_code == 0, result.output
    assert "stale PID" in result.output
    assert not path.exists()


def test_stop_force_clears_an_unidentifiable_local_server_without_signalling(
    config_dir: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    import nexus._mineru_spawn as spawn

    monkeypatch.setattr("nexus.commands.mineru._is_process_alive", lambda pid: True)
    monkeypatch.setattr(spawn, "classify_server_pid", lambda pid: "unreadable")
    monkeypatch.setattr(spawn, "stop_server_group", lambda *a, **k: pytest.fail("signalled"))
    path = _write(config_dir, _info(1, ns=_OURS))
    plain = CliRunner().invoke(mineru_group, ["stop"])
    assert "could not" in plain.output and path.exists()  # without --force: kept, as before
    forced = CliRunner().invoke(mineru_group, ["stop", "--force"])
    assert forced.exit_code == 0, forced.output
    assert not path.exists()


def test_status_keeps_a_foreign_file_of_a_server_that_is_up_and_skips_local_diagnosis(
    config_dir: Path, mineru_port: int, monkeypatch: pytest.MonkeyPatch,
) -> None:
    import nexus._mineru_health as health

    monkeypatch.setattr(health, "diagnose_server", lambda *a, **k: pytest.fail("diagnosed a pid from another namespace"))
    path = _write(config_dir, _info(mineru_port))
    result = CliRunner().invoke(mineru_group, ["status"])
    assert result.exit_code == 0, result.output
    assert "running and healthy" in result.output
    assert path.exists()


def test_status_clears_a_foreign_file_whose_port_was_reused(config_dir: Path, other_service_port: int) -> None:
    path = _write(config_dir, _info(other_service_port))
    result = CliRunner().invoke(mineru_group, ["status"])
    assert "stale PID" in result.output
    assert not path.exists()


def test_start_reports_a_foreign_server_running_and_spawns_nothing(
    config_dir: Path, mineru_port: int, monkeypatch: pytest.MonkeyPatch,
) -> None:
    spawn = MagicMock()
    monkeypatch.setattr("nexus.commands.mineru.spawn_server_process", spawn)
    monkeypatch.setattr("nexus.pdf_stack.ensure_or_exit", lambda *a, **k: None)
    _write(config_dir, _info(mineru_port))
    result = CliRunner().invoke(mineru_group, ["start", "--port", "8011"])
    assert result.exit_code == 0, result.output
    assert "already running" in result.output
    spawn.assert_not_called()


def test_start_clears_only_its_own_record_when_the_child_dies(
    config_dir: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Another process started a server after the election was released: its record is not ours to unlink."""
    other = _write(config_dir, _info(1, ns=_OURS, pid=777))
    proc = MagicMock(pid=4242)
    proc.poll.return_value = 1  # died at once
    monkeypatch.setattr("nexus.commands.mineru.spawn_server_process", lambda port: proc)
    monkeypatch.setattr("nexus.pdf_stack.ensure_or_exit", lambda *a, **k: None)
    monkeypatch.setattr(mp, "is_process_alive", lambda pid: False)
    result = CliRunner().invoke(mineru_group, ["start", "--port", "8011"])
    assert result.exit_code == 1
    assert other.exists(), "start unlinked a pid file that names another server"


# ── the callers that judged by pid alone ─────────────────────────────────────


def test_the_registered_port_follows_the_server_not_the_pid(
    config_dir: Path, mineru_port: int, other_service_port: int,
) -> None:
    from nexus.config import _read_live_mineru_port

    _write(config_dir, _info(mineru_port))
    assert _read_live_mineru_port() == mineru_port
    _write(config_dir, _info(other_service_port))
    assert _read_live_mineru_port() is None
    _write(config_dir, _info(mineru_port, ns=_OURS))  # an ordinary dead pid
    assert _read_live_mineru_port() is None


def test_the_extractor_restart_leaves_a_foreign_server_alone(
    config_dir: Path, mineru_port: int, monkeypatch: pytest.MonkeyPatch,
) -> None:
    from nexus.pdf_extractor import PDFExtractor

    monkeypatch.setenv("NX_MINERU_AUTOSTART", "1")
    import nexus._mineru_spawn as spawn

    monkeypatch.setattr(spawn, "spawn_server_process", lambda port: pytest.fail("spawned a second server"))
    monkeypatch.setattr(spawn, "stop_server_group", lambda *a, **k: pytest.fail("signalled"))
    monkeypatch.setattr(spawn, "classify_server_pid", lambda pid: pytest.fail("classified a foreign pid"))
    path = _write(config_dir, _info(mineru_port))
    ex = PDFExtractor()
    assert ex._restart_mineru_server(replace_pid=_DEAD_PID) is False
    assert ex._restart_skipped == "other-namespace"
    assert path.exists()


def test_the_extractor_restart_replaces_a_stale_foreign_record(
    config_dir: Path, other_service_port: int, monkeypatch: pytest.MonkeyPatch,
) -> None:
    from nexus.pdf_extractor import PDFExtractor

    monkeypatch.setenv("NX_MINERU_AUTOSTART", "1")
    import nexus._mineru_spawn as spawn

    spawned: list[int] = []
    monkeypatch.setattr(spawn, "spawn_server_process", lambda port: spawned.append(port))  # returns None: "not found"
    monkeypatch.setattr(spawn, "classify_server_pid", lambda pid: pytest.fail("classified a foreign pid"))
    path = _write(config_dir, _info(other_service_port))
    assert PDFExtractor()._restart_mineru_server(replace_pid=_DEAD_PID) is False
    assert len(spawned) == 1, "a record whose server is gone must not block the replacement"
    assert not path.exists()


# ── ensure_mineru_running's wait (120 s budget) ──────────────────────────────


class _FakeClock:
    """Advances only when the code under test sleeps: the 120 s budget is counted, not waited."""

    def __init__(self) -> None:
        self.t = 1_700_000_000.0
        self.slept = 0.0

    def monotonic(self) -> float:
        return self.t

    def time(self) -> float:
        return self.t

    def sleep(self, seconds: float) -> None:
        self.slept += seconds
        self.t += seconds


@pytest.fixture
def lifecycle(config_dir: Path, monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setenv("NX_MINERU_AUTOSTART", "1")
    cfg = MagicMock(mineru_autostart=True, mineru_server_url="http://127.0.0.1:8010")
    spawn = MagicMock(return_value=MagicMock(pid=4242, **{"poll.return_value": None}))
    with patch("nexus.config.get_pdf_config", return_value=cfg), \
            patch("nexus.config.get_mineru_configured_fixed_port", return_value=None), \
            patch("nexus._mineru_spawn._find_free_port", return_value=8010), \
            patch("nexus._mineru_spawn.spawn_server_process", spawn):
        yield spawn


def test_ensure_waits_out_its_budget_for_a_foreign_server_that_is_loading_and_spawns_nothing(
    config_dir: Path, silent_port: int, lifecycle,
) -> None:
    from nexus.daemon.mineru_lifecycle import _ENSURE_HEALTH_WAIT_S, ensure_mineru_running

    _write(config_dir, _info(silent_port, age_s=5))
    clock = _FakeClock()
    with patch("nexus.daemon.mineru_lifecycle._healthy", return_value=False):
        result = ensure_mineru_running(
            _monotonic=clock.monotonic, _wall_time=clock.time, _sleep=clock.sleep,
        )
    assert result is None
    lifecycle.assert_not_called()
    assert _ENSURE_HEALTH_WAIT_S == 120.0
    assert 118.0 <= clock.slept <= 124.0, f"waited {clock.slept}s of fake time, not the 120 s budget"


def test_ensure_returns_the_url_when_the_foreign_server_finishes_loading(
    config_dir: Path, silent_port: int, lifecycle,
) -> None:
    from nexus.daemon.mineru_lifecycle import ensure_mineru_running

    _write(config_dir, _info(silent_port, age_s=5))
    clock = _FakeClock()
    probes = {"n": 0}

    def healthy(url: str) -> bool:
        probes["n"] += 1
        return probes["n"] > 5  # up on the sixth probe, ten seconds in

    with patch("nexus.daemon.mineru_lifecycle._healthy", side_effect=healthy):
        result = ensure_mineru_running(
            _monotonic=clock.monotonic, _wall_time=clock.time, _sleep=clock.sleep,
        )
    assert result == f"http://127.0.0.1:{silent_port}"
    lifecycle.assert_not_called()
    assert clock.slept < 30


def test_ensure_replaces_a_foreign_record_whose_server_is_gone(
    config_dir: Path, silent_port: int, lifecycle,
) -> None:
    """Control: the same file, old and silent, is stale, so the lifecycle spawns (and the wait test above is not vacuous)."""
    from nexus.daemon.mineru_lifecycle import ensure_mineru_running

    _write(config_dir, _info(silent_port, age_s=3600))
    clock = _FakeClock()
    with patch("nexus.daemon.mineru_lifecycle._healthy", return_value=False):
        ensure_mineru_running(
            wait_healthy_s=4.0, _monotonic=clock.monotonic, _wall_time=clock.time, _sleep=clock.sleep,
        )
    lifecycle.assert_called_once()


# ── fix round: review findings ───────────────────────────────────────────────


def test_a_busy_foreign_server_that_answers_after_two_seconds_is_alive(config_dir: Path) -> None:
    """The probe waits as long as `nx mineru status` does (5 s): a server mid-parse is slow, not dead."""
    import time

    class _Slow(BaseHTTPRequestHandler):
        def do_GET(self) -> None:  # noqa: N802 — http.server's name
            time.sleep(2.5)
            self.send_response(200)
            self.end_headers()
            self.wfile.write(json.dumps(_MINERU_BODY).encode())

        def log_message(self, *a) -> None:
            pass

    server = HTTPServer(("127.0.0.1", 0), _Slow)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    try:
        assert mp.server_identified(_info(server.server_address[1]))
    finally:
        server.shutdown()
        server.server_close()


def test_server_is_broken_never_judges_a_foreign_server(config_dir: Path) -> None:
    from nexus._mineru_health import server_is_broken

    foreign = _info(8123, pid=os.getpid())
    foreign["output_root"] = "/srv/mineru-out/x"
    url = "http://127.0.0.1:8123"
    # Control: the same record from our own namespace is judged by its error text.
    own = {**foreign, "pid_ns": _OURS}
    assert server_is_broken(url, "FileNotFoundError: /srv/mineru-out/x/a.pdf", own)
    assert not server_is_broken(url, "FileNotFoundError: /srv/mineru-out/x/a.pdf", foreign)


def test_a_restart_that_cannot_happen_here_does_not_burn_the_machine_cooldown(
    config_dir: Path, mineru_port: int, other_service_port: int, monkeypatch: pytest.MonkeyPatch,
) -> None:
    from nexus.pdf_extractor import PDFExtractor
    import nexus._mineru_health as health

    claims: list[int] = []
    monkeypatch.setattr(health, "claim_broken_restart", lambda *a, **k: claims.append(1) or True)
    ex = PDFExtractor()
    monkeypatch.setattr(ex, "_restart_refused", lambda: False)
    assert ex._restart_broken_mineru_server(_info(mineru_port)) is False
    assert ex._restart_skipped == "other-namespace"
    assert claims == [], "the cooldown was claimed for a restart that cannot happen"
    # Control: a foreign record whose server is gone is replaceable, so it does claim.
    monkeypatch.setattr(ex, "_restart_mineru_server", lambda replace_pid=None: True)
    assert ex._restart_broken_mineru_server(_info(other_service_port)) is True
    assert claims == [1]


def test_the_stale_server_row_ignores_a_foreign_file_even_when_its_pid_is_a_live_local_process(
    config_dir: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    """upgrade_finish feeds an automatic SIGTERM: a pid from another namespace names nothing here."""
    from nexus.upgrade_finish import _registered_mineru_outside_generation

    monkeypatch.setattr(mp, "is_process_alive", lambda pid: True)
    monkeypatch.setattr("nexus.upgrade_finish._classify_pid", lambda pid, cmd: "mineru")
    monkeypatch.setattr("nexus.upgrade_finish.process_command", lambda pid: "mineru-api")
    elsewhere = config_dir / "elsewhere"
    now = datetime.now(timezone.utc).timestamp()
    info = {**_info(1, pid=os.getpid()), "python": str(config_dir / "other" / "bin" / "python")}
    _write(config_dir, info)
    assert _registered_mineru_outside_generation(elsewhere, now=now) is None
    # Control: the same record from our own namespace IS a stale row, so the None above is the guard's.
    _write(config_dir, {**info, "pid_ns": _OURS})
    assert _registered_mineru_outside_generation(elsewhere, now=now) is not None


def test_the_console_card_follows_the_server_not_the_pid(
    config_dir: Path, mineru_port: int, other_service_port: int,
) -> None:
    from nexus.console.routes.health import _mineru_card

    _write(config_dir, _info(mineru_port))
    assert _mineru_card(config_dir)["running"] is True
    _write(config_dir, _info(other_service_port))
    assert _mineru_card(config_dir) == {"running": False, "stale_pid": True}
    _write(config_dir, _info(mineru_port, ns=_OURS))  # an ordinary dead pid
    assert _mineru_card(config_dir)["running"] is False
    (config_dir / "mineru.pid").unlink()
    assert _mineru_card(config_dir) == {"running": False}
