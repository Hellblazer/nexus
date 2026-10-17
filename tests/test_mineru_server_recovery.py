# SPDX-License-Identifier: AGPL-3.0-or-later
"""nexus-x3m8e: what the extractor does when the MinerU server is up but broken,
and how ``stop_server_group`` ends a server group without leaking semaphores.

* ``FileNotFoundError`` reported by the REGISTERED server and about the server
  (its cwd was deleted, or the error names a path it owns): replace it ONCE from
  a stable cwd and retry; a MACHINE-wide cooldown keeps it from looping. A
  missing PDF, or a server that is not the registered one, restarts nothing.
* The replacement never signals a recycled pid: the live command is judged first.
* ``ENOSPC`` reported by the server: a machine fault only when a probe on this
  box cannot create a semaphore; a full disk also says ENOSPC.
* ``stop_server_group``: SIGTERM first, then a hard pass that spares the
  multiprocessing resource tracker.

Server error bodies are real ``httpx`` responses; the stop tests run real
process groups.
"""
from __future__ import annotations

import os
import signal
import subprocess
import sys
import time
from pathlib import Path
from unittest.mock import MagicMock, patch

import httpx
import pytest

from nexus import _mineru_spawn
from nexus import pdf_extractor as pe
from nexus._mineru_health import MineruMachineResourceError
from nexus.pdf_extractor import PDFExtractor

_RESULT = ("MD", [], [])
_PDF = Path("/tmp/x3m8e-does-not-matter.pdf")

#: The real stop, captured before any fixture replaces it on the module.
_REAL_STOP_SERVER_GROUP = _mineru_spawn.stop_server_group


def _status_error(status: int, body: object, port: int = 8010) -> httpx.HTTPStatusError:
    request = httpx.Request("POST", f"http://127.0.0.1:{port}/file_parse")
    response = httpx.Response(status, json=body, request=request)
    return httpx.HTTPStatusError(f"{status}", request=request, response=response)


@pytest.fixture(autouse=True)
def cfg(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """An isolated config dir: the pid file and the machine-wide restart
    cooldown stamp live next to each other here."""
    cd = tmp_path / "nexus-config"
    cd.mkdir()
    monkeypatch.setenv("NEXUS_CONFIG_DIR", str(cd))
    return cd


_SERVER = {
    "pid": 31337, "port": 8010,
    "output_root": "/var/nexus-x3m8e/mineru-output", "python": "/opt/tool/bin/python",
}


@pytest.fixture
def registered_server(monkeypatch: pytest.MonkeyPatch) -> dict:
    monkeypatch.setattr("nexus._mineru_pid.read_pid_file", lambda: dict(_SERVER))
    return dict(_SERVER)


@pytest.fixture
def ext() -> PDFExtractor:
    return PDFExtractor()


# ── _http_error_text ─────────────────────────────────────────────────────────


@pytest.mark.parametrize("body,needle", [
    ({"error": "FileNotFoundError: [Errno 2] No such file or directory"}, "FileNotFoundError"),
    ({"detail": "boom"}, "boom"),
    ({"message": "kaboom"}, "kaboom"),
    ({"other": 1}, "other"),
])
def test_the_servers_account_of_a_failed_task_is_read_from_its_json(body: dict, needle: str) -> None:
    assert needle in pe._http_error_text(_status_error(409, body))


def test_a_non_json_body_is_read_as_text() -> None:
    request = httpx.Request("POST", "http://x/")
    response = httpx.Response(500, text="plain ENOSPC text", request=request)
    exc = httpx.HTTPStatusError("500", request=request, response=response)
    assert pe._http_error_text(exc) == "plain ENOSPC text"


# ── _mineru_after_server_error ───────────────────────────────────────────────


def test_enospc_with_no_semaphores_left_is_a_machine_error_not_a_runtime_error(
    ext: PDFExtractor, monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The bisect ladder catches RuntimeError and would retry every half of a
    failure no page split can fix; the machine error must not be one."""
    monkeypatch.setattr("nexus._mineru_health.semaphores_exhausted", lambda: "no semaphores left")
    sub = MagicMock(return_value=_RESULT)
    monkeypatch.setattr(ext, "_mineru_run_subprocess", sub)
    exc = _status_error(409, {"error": "OSError: [Errno 28] No space left on device"})
    with pytest.raises(MineruMachineResourceError, match="no semaphores left") as info:
        ext._mineru_after_server_error(_PDF, 0, 1, exc)
    assert not isinstance(info.value, RuntimeError)
    sub.assert_not_called()


def test_enospc_on_a_box_that_can_still_make_a_semaphore_is_a_full_disk(
    ext: PDFExtractor, monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr("nexus._mineru_health.semaphores_exhausted", lambda: None)
    monkeypatch.setattr(ext, "_mineru_run_subprocess", MagicMock(return_value=_RESULT))
    exc = _status_error(409, {"error": "[Errno 28] No space left on device"})
    assert ext._mineru_after_server_error(_PDF, 0, 1, exc) == _RESULT


def test_a_server_side_file_not_found_with_the_cwd_gone_restarts_the_server_once_and_retries(
    ext: PDFExtractor, registered_server: dict, monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr("nexus._mineru_health.process_cwd_state", lambda pid: "gone")
    restart = MagicMock(return_value=True)
    monkeypatch.setattr(ext, "_restart_broken_mineru_server", restart)
    via_server = MagicMock(return_value=_RESULT)
    monkeypatch.setattr(ext, "_mineru_run_via_server", via_server)
    sub = MagicMock()
    monkeypatch.setattr(ext, "_mineru_run_subprocess", sub)
    exc = _status_error(409, {"error": "FileNotFoundError: [Errno 2] No such file or directory"})
    assert ext._mineru_after_server_error(_PDF, 3, 4, exc) == _RESULT
    restart.assert_called_once_with(registered_server)
    via_server.assert_called_once_with(_PDF, 3, 4)
    sub.assert_not_called()


def test_a_missing_pdf_does_not_restart_the_shared_server(
    ext: PDFExtractor, registered_server: dict, monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The needless-kill finding: any ``No such file or directory`` used to kill
    the machine-shared server. A server whose cwd is fine and whose error names
    none of its own paths is reporting about the REQUEST."""
    monkeypatch.setattr("nexus._mineru_health.process_cwd_state", lambda pid: "ok")
    restart = MagicMock(return_value=True)
    monkeypatch.setattr(ext, "_restart_broken_mineru_server", restart)
    monkeypatch.setattr(ext, "_mineru_run_subprocess", MagicMock(return_value=_RESULT))
    exc = _status_error(
        409, {"error": "FileNotFoundError: [Errno 2] No such file or directory: '/home/u/papers/gone.pdf'"},
    )
    assert ext._mineru_after_server_error(_PDF, 0, 1, exc) == _RESULT
    restart.assert_not_called()


def test_an_error_that_names_a_path_the_server_owns_restarts_it_even_with_a_healthy_cwd(
    ext: PDFExtractor, registered_server: dict, monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr("nexus._mineru_health.process_cwd_state", lambda pid: "ok")
    restart = MagicMock(return_value=True)
    monkeypatch.setattr(ext, "_restart_broken_mineru_server", restart)
    monkeypatch.setattr(ext, "_mineru_run_via_server", MagicMock(return_value=_RESULT))
    owned = registered_server["output_root"]
    exc = _status_error(
        409, {"error": f"FileNotFoundError: [Errno 2] No such file or directory: '{owned}/abc/auto'"},
    )
    assert ext._mineru_after_server_error(_PDF, 0, 1, exc) == _RESULT
    restart.assert_called_once()


def test_the_interpreter_prefix_counts_as_a_path_the_server_owns(
    ext: PDFExtractor, registered_server: dict, monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr("nexus._mineru_health.process_cwd_state", lambda pid: "ok")
    restart = MagicMock(return_value=True)
    monkeypatch.setattr(ext, "_restart_broken_mineru_server", restart)
    monkeypatch.setattr(ext, "_mineru_run_via_server", MagicMock(return_value=_RESULT))
    exc = _status_error(
        409, {"error": "FileNotFoundError: No such file or directory: '/opt/tool/lib/python3.12/site-packages/x.py'"},
    )
    assert ext._mineru_after_server_error(_PDF, 0, 1, exc) == _RESULT
    restart.assert_called_once()


def test_a_server_that_is_not_the_registered_one_is_never_restarted(
    ext: PDFExtractor, registered_server: dict, monkeypatch: pytest.MonkeyPatch,
) -> None:
    """An operator-pinned or remote URL: its working directory is not the pid
    file's, and the registered server must not die for another server's error."""
    monkeypatch.setattr("nexus._mineru_health.process_cwd_state", lambda pid: "gone")
    restart = MagicMock(return_value=True)
    monkeypatch.setattr(ext, "_restart_broken_mineru_server", restart)
    monkeypatch.setattr(ext, "_mineru_run_subprocess", MagicMock(return_value=_RESULT))
    exc = _status_error(409, {"error": "FileNotFoundError"}, port=9999)
    assert ext._mineru_after_server_error(_PDF, 0, 1, exc) == _RESULT
    restart.assert_not_called()


def test_a_retry_that_fails_again_falls_back_to_the_subprocess(
    ext: PDFExtractor, registered_server: dict, monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr("nexus._mineru_health.process_cwd_state", lambda pid: "gone")
    monkeypatch.setattr(ext, "_restart_broken_mineru_server", MagicMock(return_value=True))
    monkeypatch.setattr(
        ext, "_mineru_run_via_server",
        MagicMock(side_effect=_status_error(409, {"error": "FileNotFoundError again"})),
    )
    monkeypatch.setattr(ext, "_mineru_run_subprocess", MagicMock(return_value=_RESULT))
    exc = _status_error(409, {"error": "FileNotFoundError"})
    assert ext._mineru_after_server_error(_PDF, 0, 1, exc) == _RESULT


def test_a_restart_that_does_not_happen_goes_straight_to_the_subprocess(
    ext: PDFExtractor, registered_server: dict, monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr("nexus._mineru_health.process_cwd_state", lambda pid: "gone")
    monkeypatch.setattr(ext, "_restart_broken_mineru_server", MagicMock(return_value=False))
    via_server = MagicMock()
    monkeypatch.setattr(ext, "_mineru_run_via_server", via_server)
    monkeypatch.setattr(ext, "_mineru_run_subprocess", MagicMock(return_value=_RESULT))
    exc = _status_error(409, {"error": "FileNotFoundError"})
    assert ext._mineru_after_server_error(_PDF, 0, 1, exc) == _RESULT
    via_server.assert_not_called()


def test_any_other_server_error_degrades_to_the_subprocess_as_before(
    ext: PDFExtractor, monkeypatch: pytest.MonkeyPatch,
) -> None:
    restart = MagicMock()
    monkeypatch.setattr(ext, "_restart_broken_mineru_server", restart)
    monkeypatch.setattr(ext, "_mineru_run_subprocess", MagicMock(return_value=_RESULT))
    exc = _status_error(500, {"error": "CUDA out of memory"})
    assert ext._mineru_after_server_error(_PDF, 0, 1, exc) == _RESULT
    restart.assert_not_called()


def test_the_isolated_dispatch_routes_a_409_through_the_decision(
    ext: PDFExtractor, registered_server: dict, monkeypatch: pytest.MonkeyPatch,
) -> None:
    """End to end through ``_mineru_run_isolated``: a real 409 response from the
    server restarts it and the retry's result is what the caller gets."""
    monkeypatch.setattr("nexus._mineru_health.process_cwd_state", lambda pid: "gone")
    monkeypatch.setattr(ext, "_mineru_server_available", lambda: True)
    calls = {"n": 0}

    def via_server(pdf, start, end):
        calls["n"] += 1
        if calls["n"] == 1:
            raise _status_error(409, {"error": "FileNotFoundError: [Errno 2] No such file or directory"})
        return _RESULT

    monkeypatch.setattr(ext, "_mineru_run_via_server", via_server)
    monkeypatch.setattr(ext, "_restart_broken_mineru_server", MagicMock(return_value=True))
    assert ext._mineru_run_isolated(_PDF, 0, 1) == _RESULT
    assert calls["n"] == 2


# ── _restart_broken_mineru_server: the machine-wide cooldown ─────────────────


def test_the_broken_server_is_replaced_once_per_cooldown(
    ext: PDFExtractor, monkeypatch: pytest.MonkeyPatch,
) -> None:
    restart = MagicMock(return_value=True)
    monkeypatch.setattr(ext, "_restart_mineru_server", restart)
    assert ext._restart_broken_mineru_server(dict(_SERVER)) is True
    restart.assert_called_once_with(replace_pid=31337)
    # a second FileNotFoundError inside the cooldown does not restart again
    assert ext._restart_broken_mineru_server(dict(_SERVER)) is False
    restart.assert_called_once()


def test_there_is_nothing_to_replace_without_a_registered_server(
    ext: PDFExtractor, monkeypatch: pytest.MonkeyPatch,
) -> None:
    restart = MagicMock(return_value=True)
    monkeypatch.setattr(ext, "_restart_mineru_server", restart)
    assert ext._restart_broken_mineru_server(None) is False
    restart.assert_not_called()


def test_the_cooldown_is_machine_wide_not_per_process(cfg: Path) -> None:
    """A module global gave every process its own clock, so N parallel indexers
    restarted the one shared server N times. The claim is a stamp file next to
    mineru.pid: a SECOND PROCESS on the same config dir sees it."""
    from nexus._mineru_health import claim_broken_restart

    assert claim_broken_restart() is True
    assert (cfg / "mineru_broken_restart.ts").exists()
    other = subprocess.run(  # noqa: S603 — fixed argv, test-owned
        [sys.executable, "-c",
         "from nexus._mineru_health import claim_broken_restart; print(claim_broken_restart())"],
        capture_output=True, text=True, timeout=120, env={**os.environ, "NEXUS_CONFIG_DIR": str(cfg)},
    )
    assert other.stdout.strip() == "False", other.stderr


def test_the_cooldown_expires_and_the_stamp_is_replaced_atomically(cfg: Path) -> None:
    from nexus._mineru_health import BROKEN_RESTART_COOLDOWN_S, claim_broken_restart

    assert claim_broken_restart(_now=1_000.0) is True
    assert claim_broken_restart(_now=1_000.0 + BROKEN_RESTART_COOLDOWN_S - 1) is False
    assert claim_broken_restart(_now=1_000.0 + BROKEN_RESTART_COOLDOWN_S + 1) is True
    assert claim_broken_restart(_now=500.0) is True, "a stamp from the future (clock change) must not block"
    assert [p.name for p in cfg.iterdir()] == ["mineru_broken_restart.ts"], "no temp file left behind"


# ── _restart_mineru_server(replace_pid): what it signals ─────────────────────


@pytest.fixture
def restart_world(monkeypatch: pytest.MonkeyPatch, cfg: Path) -> dict:
    """Everything ``_restart_mineru_server`` touches outside the election:
    policy, spawn, health, the pid file. Its REAL election runs on ``cfg``."""
    world: dict = {"stopped": [], "spawned": []}
    pid_file = cfg / "mineru.pid"
    pid_file.write_text("{}")
    monkeypatch.setattr("nexus.daemon.mineru_lifecycle.spawn_policy_allows", lambda *a, **k: True)
    monkeypatch.setattr("nexus._mineru_pid.read_pid_file", lambda: {"pid": 31337, "port": 8010})
    monkeypatch.setattr("nexus._mineru_pid.is_process_alive", lambda pid: pid == 31337)

    class _NewServer:
        pid = 777

        def poll(self) -> None:
            return None

    def spawn(port: int) -> _NewServer:
        world["spawned"].append(port)
        return _NewServer()

    monkeypatch.setattr(_mineru_spawn, "spawn_server_process", spawn)
    monkeypatch.setattr(
        _mineru_spawn, "stop_server_group",
        lambda pid, **k: world["stopped"].append(pid) or "exited",
    )
    ok = MagicMock(status_code=200)
    monkeypatch.setattr(pe.httpx, "get", lambda *a, **k: ok)
    world["pid_file"] = pid_file
    return world


def test_replacing_a_broken_server_stops_its_group_then_spawns_a_new_one(
    ext: PDFExtractor, restart_world: dict, monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(_mineru_spawn, "classify_server_pid", lambda pid: "mineru")
    assert ext._restart_mineru_server(replace_pid=31337) is True
    assert restart_world["stopped"] == [31337]
    assert len(restart_world["spawned"]) == 1
    assert not restart_world["pid_file"].exists(), "the stale pid file is removed before the spawn"


def test_a_replace_for_a_pid_that_is_no_longer_the_registered_one_stops_nothing(
    ext: PDFExtractor, restart_world: dict, monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A concurrent trigger already replaced the server: the pid file now names a
    different pid, so this caller probes that one and signals nobody."""
    monkeypatch.setattr(_mineru_spawn, "classify_server_pid", lambda pid: pytest.fail("classified"))
    monkeypatch.setattr("nexus._mineru_pid.is_process_alive", lambda pid: True)
    assert ext._restart_mineru_server(replace_pid=11111) is True
    assert restart_world["stopped"] == [] and restart_world["spawned"] == []


@pytest.mark.skipif(sys.platform == "win32", reason="process groups are POSIX")
def test_a_recycled_pid_is_never_signalled_and_a_real_innocent_process_survives(
    ext: PDFExtractor, restart_world: dict, monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The pid file names a pid the OS has given to an unrelated process; the
    stop signals that pid's whole process GROUP. A REAL such process (a plain
    sleeper leading its own group, not a mineru-api) must be left alone, the
    stale file removed, and a new server spawned. The REAL stop and the REAL
    identity check are in play: without the guard this kills the sleeper."""
    innocent = subprocess.Popen(  # noqa: S603 — fixed argv, test-owned
        [sys.executable, "-c", "import time; time.sleep(120)"], start_new_session=True,
    )
    try:
        monkeypatch.setattr("nexus._mineru_pid.read_pid_file", lambda: {"pid": innocent.pid, "port": 8010})
        monkeypatch.setattr("nexus._mineru_pid.is_process_alive", lambda pid: pid == innocent.pid)
        monkeypatch.setattr(_mineru_spawn, "stop_server_group", _REAL_STOP_SERVER_GROUP)
        assert ext._restart_mineru_server(replace_pid=innocent.pid) is True
        time.sleep(0.5)
        assert innocent.poll() is None, "an unrelated process was signalled through a recycled pid"
        assert len(restart_world["spawned"]) == 1
        assert not restart_world["pid_file"].exists()
    finally:
        innocent.kill()
        innocent.wait(timeout=10)


def test_a_pid_whose_command_cannot_be_read_is_not_signalled_and_nothing_is_spawned(
    ext: PDFExtractor, restart_world: dict, monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(_mineru_spawn, "classify_server_pid", lambda pid: "unreadable")
    assert ext._restart_mineru_server(replace_pid=31337) is False
    assert restart_world["stopped"] == [] and restart_world["spawned"] == []
    assert restart_world["pid_file"].exists(), "an inconclusive read keeps the pid file"


def test_classify_server_pid_reads_the_live_command(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr("nexus.upgrade_finish.process_command", lambda pid: "")
    assert _mineru_spawn.classify_server_pid(1) == "unreadable"
    monkeypatch.setattr("nexus.upgrade_finish.process_command", lambda pid: "/usr/bin/vim notes.txt")
    assert _mineru_spawn.classify_server_pid(1) == "other"
    monkeypatch.setattr(
        "nexus.upgrade_finish.process_command", lambda pid: "/opt/tool/bin/mineru-api --host 127.0.0.1 --port 8010",
    )
    assert _mineru_spawn.classify_server_pid(1) == "mineru"


# ── the pre-dispatch replacement call site ───────────────────────────────────


def test_availability_replaces_a_broken_registered_server_before_any_task(
    ext: PDFExtractor, monkeypatch: pytest.MonkeyPatch,
) -> None:
    """``_mineru_server_available`` is the production door to the pre-dispatch
    check: a ``/health`` 200 must be followed by the cwd-gone replacement."""
    monkeypatch.setattr("nexus.config.get_mineru_server_url", lambda *a, **k: "http://127.0.0.1:8010")
    monkeypatch.setattr(ext, "_probe_mineru_health", lambda url: (True, ""))
    replace = MagicMock()
    monkeypatch.setattr(ext, "_replace_server_with_deleted_cwd", replace)
    assert ext._mineru_server_available() is True
    replace.assert_called_once_with("http://127.0.0.1:8010")


# ── the machine error is not bisected, not wrapped ───────────────────────────


def test_a_machine_resource_error_is_not_bisected_and_reaches_the_caller_unwrapped() -> None:
    ext = PDFExtractor()
    mock_doc = MagicMock()
    mock_doc.__len__.return_value = 2
    mock_ctx = MagicMock()
    mock_ctx.__enter__.return_value = mock_doc
    mock_ctx.__exit__.return_value = False
    isolated = MagicMock(side_effect=MineruMachineResourceError("no semaphores left"))
    with patch("nexus.pdf_extractor.do_parse", object()), patch(
        "pymupdf.open", return_value=mock_ctx,
    ), patch(
        "nexus.config.get_mineru_page_batch", return_value=2,
    ), patch.object(ext, "_mineru_run_isolated", isolated):
        with pytest.raises(MineruMachineResourceError, match="no semaphores left"):
            ext._extract_with_mineru(Path("/tmp/math.pdf"), formula_count=9, on_formula_oom="fail")
    assert isolated.call_count == 1, "a split of the batch cannot fix a machine fault"


def test_route_to_mineru_does_not_wrap_the_machine_error() -> None:
    ext = PDFExtractor()
    with patch.object(
        ext, "_extract_with_mineru",
        side_effect=MineruMachineResourceError("no semaphores left"),
    ):
        with pytest.raises(MineruMachineResourceError):
            ext._route_to_mineru(Path("/tmp/m.pdf"), 9, on_page=None, on_formula_oom="fail")


# ── _replace_server_with_deleted_cwd ─────────────────────────────────────────


def test_a_registered_server_with_a_deleted_cwd_is_replaced_before_any_task(
    ext: PDFExtractor, registered_server: dict, monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr("nexus._mineru_health.process_cwd_state", lambda pid: "gone")
    restart = MagicMock(return_value=True)
    monkeypatch.setattr(ext, "_restart_broken_mineru_server", restart)
    ext._replace_server_with_deleted_cwd("http://127.0.0.1:8010")
    restart.assert_called_once_with(registered_server)


@pytest.mark.parametrize("state,url", [
    ("ok", "http://127.0.0.1:8010"),       # healthy cwd
    (None, "http://127.0.0.1:8010"),       # cannot tell
    ("gone", "http://127.0.0.1:9999"),     # a server that is not the registered one
    ("gone", "http://127.0.0.1:801"),      # a port that merely prefixes the registered one
    ("gone", "http://example.com:8010"),   # not loopback
])
def test_a_server_that_is_fine_or_not_ours_is_left_alone(
    state: str | None, url: str, ext: PDFExtractor, registered_server: dict, monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr("nexus._mineru_health.process_cwd_state", lambda pid: state)
    restart = MagicMock()
    monkeypatch.setattr(ext, "_restart_broken_mineru_server", restart)
    ext._replace_server_with_deleted_cwd(url)
    restart.assert_not_called()


# ── stop_server_group: real process groups ───────────────────────────────────

pytestmark_posix = pytest.mark.skipif(
    sys.platform == "win32", reason="process groups and SIGTERM semantics are POSIX",
)

#: Launcher: starts the group leader (own session) and exits, so the leader is
#: reparented to init and is reaped when it dies, like a real server started by
#: an earlier ``nx mineru start``. The leader ignores SIGTERM when asked.
_LAUNCHER = r"""
import subprocess, sys
ignore_term = sys.argv[1] == "ignore"
leader_src = (
    "import signal, subprocess, sys, time\n"
    + ("signal.signal(signal.SIGTERM, signal.SIG_IGN)\n" if ignore_term else "")
    + "m = 'multiprocessing' + '.resource_tracker'\n"
    + "t = subprocess.Popen([sys.executable, '-c', 'import time; time.sleep(120)  # ' + m], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)\n"
    + "print(t.pid, flush=True)\n"
    + "time.sleep(120)\n"
)
leader = subprocess.Popen([sys.executable, "-c", leader_src], start_new_session=True,
                          stdout=subprocess.PIPE, stderr=subprocess.DEVNULL, text=True)
tracker_like = leader.stdout.readline().strip()
print(leader.pid, tracker_like, flush=True)
"""


def _start_group(mode: str) -> tuple[int, int]:
    out = subprocess.run(  # noqa: S603 — fixed argv, test-owned
        [sys.executable, "-c", _LAUNCHER, mode], capture_output=True, text=True, timeout=30, check=True,
    ).stdout.split()
    return int(out[0]), int(out[1])


def _alive(pid: int) -> bool:
    from nexus._mineru_pid import is_process_alive

    return is_process_alive(pid)


def _wait_dead(pid: int, timeout: float = 10.0) -> bool:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if not _alive(pid):
            return True
        time.sleep(0.05)
    return False


def _cleanup(*pids: int) -> None:
    for pid in pids:
        try:
            os.kill(pid, signal.SIGKILL)
        except OSError:
            pass


@pytestmark_posix
def test_a_server_that_exits_on_sigterm_is_stopped_without_escalation() -> None:
    leader, tracker_like = _start_group("obey")
    try:
        assert _mineru_spawn.stop_server_group(leader, grace=10.0) == "exited"
        assert _wait_dead(leader)
    finally:
        _cleanup(leader, tracker_like)


@pytestmark_posix
def test_a_server_that_ignores_sigterm_is_escalated_and_the_tracker_is_spared() -> None:
    leader, tracker_like = _start_group("ignore")
    try:
        assert _mineru_spawn.stop_server_group(leader, grace=0.6) == "escalated"
        assert _wait_dead(leader), "the hard pass must kill the server"
        assert _alive(tracker_like), "the hard pass must spare the resource tracker"
    finally:
        _cleanup(leader, tracker_like)


@pytestmark_posix
def test_stopping_a_group_that_is_already_gone_says_so() -> None:
    leader, tracker_like = _start_group("obey")
    _cleanup(leader, tracker_like)
    assert _wait_dead(leader)
    assert _mineru_spawn.stop_server_group(leader, grace=0.2) == "gone"


@pytestmark_posix
def test_a_group_left_with_only_its_tracker_is_stopped_with_no_second_whole_group_kill(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The reviewed defect: when the hard pass found only the tracker left, it
    reported False and the caller fell back to a WHOLE-group kill, which killed
    the tracker. There is no fallback now: the tracker-only group is success."""
    from nexus.util import process_group as pgmod

    sent: list[tuple[int, int]] = []
    killed_group: list[tuple[int, int]] = []
    monkeypatch.setattr(pgmod, "safe_killpg", lambda pid, sig=pgmod.KILL_SIGNAL: sent.append((pid, sig)) or True)
    monkeypatch.setattr(pgmod, "safe_killpg_group", lambda pgid, sig=pgmod.KILL_SIGNAL: killed_group.append((pgid, sig)) or True)
    monkeypatch.setattr("nexus._mineru_pid.is_process_alive", lambda pid: True)
    outcome = _mineru_spawn.stop_server_group(777, grace=0.0, _sleep=lambda s: None)
    assert outcome == "escalated"
    assert sent == [(777, signal.SIGTERM)], "no whole-group safe_killpg after the polite pass"
    assert killed_group == [(777, pgmod.KILL_SIGNAL)]


def test_without_process_groups_the_escalation_terminates_the_one_process(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Windows shape (no ``os.killpg``): the pid is the one process to terminate."""
    from nexus.util import process_group as pgmod
    from tests._module_seam import delattr_in

    sent: list[tuple[int, int]] = []
    delattr_in(monkeypatch, _mineru_spawn, "os.killpg", raising=False)
    monkeypatch.setattr(pgmod, "safe_killpg", lambda pid, sig=pgmod.KILL_SIGNAL: sent.append((pid, sig)) or True)
    monkeypatch.setattr(pgmod, "safe_killpg_group", lambda *a, **k: pytest.fail("group kill without groups"))
    monkeypatch.setattr("nexus._mineru_pid.is_process_alive", lambda pid: True)
    assert _mineru_spawn.stop_server_group(777, grace=0.0, _sleep=lambda s: None) == "escalated"
    assert sent == [(777, signal.SIGTERM), (777, pgmod.KILL_SIGNAL)]
