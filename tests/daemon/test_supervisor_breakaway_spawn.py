# SPDX-License-Identifier: AGPL-3.0-or-later
"""The storage supervisor's detached spawn asks Windows to break away from the
caller's Job Object, and retries inside it when the job refuses (nexus-9pc2f).

Defence in depth behind ``contained=False`` on the service-start call sites: a
caller that does sit in a kill-on-close job whose policy allows breakaway no longer
takes the supervisor and its engine with it when the job closes. A job without
breakaway refuses ``CreateProcess`` with access denied; the spawn is retried once
without the flag, the aspect worker's rule, now shared in ``service_registry``.

Every test here drives the spawn with a fake ``Popen``; no real job object is made.
Only a Windows run can show the kernel honouring the flag; that is the guest check
named in the bead.
"""
from __future__ import annotations

from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

from nexus.daemon.service_registry import popen_outliving_host_job
from nexus.util import win_job

_BASE = win_job.CREATE_NEW_PROCESS_GROUP | win_job.CREATE_NO_WINDOW


def test_windows_spawn_asks_for_breakaway_first() -> None:
    calls: list[dict] = []

    def popen(argv, **kw):
        calls.append(kw)
        return "child"

    assert popen_outliving_host_job(popen, ["x"], creationflags=_BASE, cwd="/") == "child"
    assert [c["creationflags"] for c in calls] == [_BASE | win_job.CREATE_BREAKAWAY_FROM_JOB]
    assert calls[0]["cwd"] == "/", "other kwargs pass through untouched"


def test_a_refused_breakaway_is_retried_once_without_the_flag() -> None:
    flags: list[int] = []

    def popen(argv, **kw):
        flags.append(kw["creationflags"])
        if kw["creationflags"] & win_job.CREATE_BREAKAWAY_FROM_JOB:
            raise PermissionError(5, "Access is denied")
        return "child"

    assert popen_outliving_host_job(popen, ["x"], creationflags=_BASE) == "child"
    assert flags == [_BASE | win_job.CREATE_BREAKAWAY_FROM_JOB, _BASE]


def test_a_failure_that_is_not_about_breakaway_propagates_from_the_retry() -> None:
    def popen(argv, **kw):
        raise FileNotFoundError(2, "no such file")

    with pytest.raises(FileNotFoundError):
        popen_outliving_host_job(popen, ["x"], creationflags=_BASE)


def test_off_windows_there_is_no_creationflags_and_nothing_changes() -> None:
    seen: list[dict] = []
    popen_outliving_host_job(lambda argv, **kw: seen.append(kw), ["x"], start_new_session=True)
    assert seen == [{"start_new_session": True}]


def test_ensure_storage_supervisor_spawns_through_the_breakaway_rule(tmp_path: Path) -> None:
    """The supervisor spawn itself, with the real kwargs builder for win32."""
    from nexus.commands import daemon as daemon_mod

    flags: list[int] = []

    def _popen(argv, **kw):
        flags.append(kw["creationflags"])
        if kw["creationflags"] & win_job.CREATE_BREAKAWAY_FROM_JOB:
            raise PermissionError(5, "Access is denied")  # host job refuses breakaway
        raise _Spawned  # reached the retry: stop before the lease wait

    class _Spawned(Exception):
        pass

    with patch.object(daemon_mod, "_resolve_nx_bin", return_value=["nx"]), \
         patch.object(daemon_mod, "_popen", side_effect=_popen), \
         patch.object(daemon_mod, "_start_via_autostart_unit", return_value=None), \
         pytest.raises(_Spawned):
        daemon_mod.ensure_storage_supervisor(tmp_path, platform="win32")
    assert flags == [_BASE | win_job.CREATE_BREAKAWAY_FROM_JOB, _BASE]


def test_aspect_worker_keeps_its_name_for_the_shared_rule() -> None:
    from nexus.daemon.aspect_worker_daemon import _popen_outliving_host_job

    popen = MagicMock(return_value="child")
    assert _popen_outliving_host_job(popen, ["x"], creationflags=_BASE) == "child"
    assert popen.call_args.kwargs["creationflags"] & win_job.CREATE_BREAKAWAY_FROM_JOB
