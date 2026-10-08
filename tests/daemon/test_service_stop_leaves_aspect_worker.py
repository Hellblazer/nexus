# SPDX-License-Identifier: AGPL-3.0-or-later
"""``nx daemon service stop`` leaves the aspect-worker daemon running, on every
platform (nexus-g5rz5).

RDR-224's recorded decision (Phase 3 fix round B; Revision History: "its
decision that `service stop` leaves it running as on POSIX"): the worker
belongs to the store path, not to the service. It respawns on the next
enqueue (``ensure_aspect_worker_daemon``), holds neither the engine nor
PostgreSQL files, and idles at a bounded cadence with the stack down
(``next_reclaim_wait``). The stop's process sweep is
``storage_service_stack_matcher``; this pins that its predicate never matches
an aspect-worker command line, so a future widening of the matcher cannot turn
the by-design survivor into a hard-kill target (and, on Windows, into a
cross-session refusal for a process that was never part of the stack).
"""
from __future__ import annotations

from pathlib import Path

import pytest

from nexus.daemon.service_registry import storage_service_stack_matcher

_WIN_CFG = Path("C:/Users/sam/AppData/Roaming/nexus")
_POSIX_CFG = Path("/home/sam/.config/nexus-g5rz5")


@pytest.mark.parametrize(
    ("platform", "cfg", "tag", "worker", "supervisor"),
    [
        (
            "linux", _POSIX_CFG, "linux-amd64",
            f"/usr/bin/python3 -m nexus.cli daemon aspect-worker start --config-dir {_POSIX_CFG} --tenant default",
            f"/usr/bin/python3 -m nexus.cli daemon service start --foreground --config-dir {_POSIX_CFG}",
        ),
        (
            "win32", _WIN_CFG, "windows-x64",
            f'"C:\\tool\\python.exe" -m nexus.cli daemon aspect-worker start --config-dir {_WIN_CFG} --tenant default',
            f'"C:\\tool\\python.exe" -m nexus.cli daemon service start --foreground --config-dir {_WIN_CFG}',
        ),
    ],
)
def test_stack_matcher_matches_the_supervisor_and_never_the_aspect_worker(
    platform: str, cfg: Path, tag: str, worker: str, supervisor: str,
) -> None:
    match = storage_service_stack_matcher(cfg, platform_tag=tag, platform=platform)
    assert match(supervisor), "non-vacuity: the matcher must still see the supervisor"
    assert not match(worker)
