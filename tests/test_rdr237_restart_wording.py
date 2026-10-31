# SPDX-License-Identifier: AGPL-3.0-or-later
"""RDR-237 (nexus-tr1rj.26): what the timed-out-server restart says when ANOTHER process got there first.

``_restart_mineru_server`` takes the machine-wide election and reads the pid file again. When the file
now names a different live server (a concurrent trigger already restarted), it stops and spawns
nothing and probes that server. The progress line used to say ``replaced it (old pid P)`` as if this
process had done it. It now says another process did, and names both pids.

The restart runs for real, under the real election and a real loopback ``/health`` server; only the
pid-file reads are scripted (the first names the server that timed out, the second the peer's).
"""
from __future__ import annotations

import os
from unittest.mock import MagicMock

import pytest

from nexus.pdf_extractor import PDFExtractor
from tests.test_rdr237_slots_from_health import _health, health_server

TIMED_OUT = {"pid": 31337, "port": 8010, "output_root": "/var/rdr237/out", "python": "/opt/rdr237/python"}


@pytest.fixture(autouse=True)
def _world(tmp_path, monkeypatch) -> None:
    cd = tmp_path / "nexus-config"
    cd.mkdir()
    monkeypatch.setenv("NEXUS_CONFIG_DIR", str(cd))
    monkeypatch.setenv("NX_MINERU_AUTOSTART", "1")
    monkeypatch.delenv("NEXUS_PDF_PROGRESS_QUIET", raising=False)
    monkeypatch.setattr("nexus.config.get_mineru_server_url", lambda: "http://127.0.0.1:8010")
    monkeypatch.setattr("nexus.daemon.mineru_lifecycle.spawn_policy_allows", lambda *a, **k: True)


def _peer(port: int) -> dict:
    # This test process is a live pid that is not 31337.
    return {"pid": os.getpid(), "port": port, "output_root": "/var/rdr237/out", "python": "/opt/rdr237/python"}


def test_a_server_another_process_already_replaced_is_not_called_replaced_by_this_one(
    monkeypatch, capsys,
) -> None:
    ext = PDFExtractor()
    ext._server_timeout_pages.append("3")
    spawned = MagicMock(name="spawn_server_process")
    stopped = MagicMock(name="stop_server_group")
    monkeypatch.setattr("nexus._mineru_spawn.spawn_server_process", spawned)
    monkeypatch.setattr("nexus._mineru_spawn.stop_server_group", stopped)

    with health_server(_health(max_concurrent_requests=1)) as url:
        port = int(url.rsplit(":", 1)[1])
        reads = iter([dict(TIMED_OUT), _peer(port)])
        monkeypatch.setattr("nexus._mineru_pid.read_pid_file", lambda: next(reads))
        ext._restart_timed_out_server()

    err = capsys.readouterr().err
    pid = os.getpid()
    assert f"another process already replaced it (old pid 31337, now pid {pid})" in err
    assert "— replacing it" not in err, "this process replaced nothing"
    assert "— replaced it" not in err
    spawned.assert_not_called()
    stopped.assert_not_called()
    assert ext._mineru_server_up is True, "the peer's server is in use"


def test_a_peers_server_that_does_not_answer_is_said_so(monkeypatch, capsys) -> None:
    ext = PDFExtractor()
    ext._server_timeout_pages.append("3")
    monkeypatch.setattr("nexus._mineru_spawn.spawn_server_process", MagicMock())
    # Nothing listens on this port (the server above is shut down) and the health wait is cut short.
    with health_server(_health()) as url:
        port = int(url.rsplit(":", 1)[1])
    reads = iter([dict(TIMED_OUT), _peer(port)])
    monkeypatch.setattr("nexus._mineru_pid.read_pid_file", lambda: next(reads))
    monkeypatch.setattr("nexus.pdf_extractor._RESTART_HEALTH_WAIT_S", 0.3)  # the real wait is 60 s

    ext._restart_timed_out_server()

    err = capsys.readouterr().err
    assert f"another process replaced it (now pid {os.getpid()}) and that server did not come up" in err
    assert "replaced it (old pid" not in err and "tried to replace it" not in err


def test_a_restart_this_process_made_still_says_so(monkeypatch, capsys) -> None:
    """The control: the pid file still names the server that timed out, so the stop and the spawn are
    this process's and the line is unchanged."""
    ext = PDFExtractor()
    ext._server_timeout_pages.append("3")
    monkeypatch.setattr("nexus._mineru_pid.read_pid_file", lambda: dict(TIMED_OUT))
    monkeypatch.setattr(ext, "_restart_mineru_server", MagicMock(return_value=True))

    ext._restart_timed_out_server()

    err = capsys.readouterr().err
    assert "replaced it (old pid 31337)" in err and "another process" not in err


def test_a_takeover_is_not_remembered_by_the_next_restart_on_the_same_extractor(monkeypatch, capsys) -> None:
    """The takeover pid belongs to ONE restart. A second timed-out restart on the same extractor,
    whose pid file names the timed-out server again, is this process's own replacement and must say
    so; a stale takeover pid from the first would print the other-process line for it."""
    ext = PDFExtractor()
    ext._server_timeout_pages.append("3")
    monkeypatch.setattr("nexus._mineru_spawn.spawn_server_process", MagicMock())
    monkeypatch.setattr("nexus._mineru_spawn.stop_server_group", MagicMock())
    with health_server(_health(max_concurrent_requests=1)) as url:
        port = int(url.rsplit(":", 1)[1])
        reads = iter([dict(TIMED_OUT), _peer(port)])
        monkeypatch.setattr("nexus._mineru_pid.read_pid_file", lambda: next(reads))
        ext._restart_timed_out_server()
    assert ext._restart_taken_over_by == os.getpid(), "the setup: the first restart was a takeover"
    capsys.readouterr()

    # The second restart: the pid file names the timed-out server again; this process replaces it.
    ext._server_timeout_pages.append("9")
    monkeypatch.setattr("nexus._mineru_pid.read_pid_file", lambda: dict(TIMED_OUT))
    monkeypatch.setattr("nexus._mineru_health.claim_broken_restart", lambda: True)
    monkeypatch.setattr(ext, "_restart_mineru_server", MagicMock(return_value=True))
    ext._restart_timed_out_server()

    err = capsys.readouterr().err
    assert "replaced it (old pid 31337)" in err
    assert "another process" not in err
    assert ext._restart_taken_over_by is None
