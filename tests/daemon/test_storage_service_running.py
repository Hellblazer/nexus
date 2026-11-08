# SPDX-License-Identifier: AGPL-3.0-or-later
"""``storage_service_running``: the read-only twin of ``stop_storage_service`` (nexus-j738q).

``nx daemon service pg-upgrade --apply`` refuses while the service runs. It must find the service the way
``nx daemon service stop`` does (a live lease, then the process table), and must signal nothing while it looks.
"""
from __future__ import annotations

import subprocess
import sys
from pathlib import Path

import pytest

import nexus.daemon.storage_service_daemon as ssd
from nexus.daemon.service_registry import ServiceRegistry, service_identity, ttl_for_tier

pytestmark = pytest.mark.skipif(sys.platform == "win32", reason="POSIX process table and pid probes")


def _registry(cfg: Path) -> ServiceRegistry:
    return ServiceRegistry(dir=cfg, tier=ssd._REGISTRY_TIER, ttl=ttl_for_tier(ssd._REGISTRY_TIER))


def test_nothing_running_finds_nothing(tmp_path: Path) -> None:
    assert ssd.storage_service_running(tmp_path) == []


def test_a_live_lease_is_found_and_left_in_place(tmp_path: Path) -> None:
    registry = _registry(tmp_path)
    registry.publish(service_identity(), endpoint={"pid": 1, "port": 1}, version="t", owner_token="tok",
                     payload={"supervisor_pid": 424242})
    found = ssd.storage_service_running(tmp_path)
    assert len(found) == 1 and "lease" in found[0] and "424242" in found[0], found
    assert len(registry.records()) == 1, "looking must not release the lease"


def test_a_supervisor_process_without_a_lease_is_found_and_not_signalled(tmp_path: Path) -> None:
    child = subprocess.Popen(
        [sys.executable, "-c", "import time; time.sleep(60)", "daemon service start", "--foreground",
         "--config-dir", str(tmp_path)],
    )
    try:
        found = ssd.storage_service_running(tmp_path)
        assert found == [f"process {child.pid}"], found
        assert child.poll() is None, "the read-only check signalled the service"
    finally:
        child.kill()
        child.wait(timeout=10)


def test_a_supervisor_of_another_config_dir_is_not_ours(tmp_path: Path) -> None:
    mine, other = tmp_path / "mine", tmp_path / "other"
    mine.mkdir()
    other.mkdir()
    child = subprocess.Popen(
        [sys.executable, "-c", "import time; time.sleep(60)", "daemon service start", "--foreground",
         "--config-dir", str(other)],
    )
    try:
        assert ssd.storage_service_running(mine) == []
    finally:
        child.kill()
        child.wait(timeout=10)


def _supervisor_child(cfg: Path) -> subprocess.Popen:
    return subprocess.Popen(
        [sys.executable, "-c", "import time; time.sleep(60)", "daemon service start", "--foreground",
         "--config-dir", str(cfg)],
    )


def test_a_candidate_the_member_check_does_not_place_is_not_reported(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """The stop's second stage applies: a flagless start of another directory (or an unreadable one) is not ours."""
    import nexus.daemon.service_registry as sr  # noqa: PLC0415

    monkeypatch.setattr(sr, "storage_service_member_check", lambda _cfg, **_kw: (lambda _pid, _cmd: "unreadable"))
    child = _supervisor_child(tmp_path)
    try:
        assert ssd.storage_service_running(tmp_path) == []
    finally:
        child.kill()
        child.wait(timeout=10)


def test_an_ancestor_is_never_reported(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """A wrapper that carries the stack's own command line and is this process's ancestor is skipped, as in the stop."""
    import nexus.daemon.service_registry as sr  # noqa: PLC0415

    child = _supervisor_child(tmp_path)
    try:
        monkeypatch.setattr(sr, "ancestor_pids", lambda _pid, **_kw: frozenset({child.pid}))
        assert ssd.storage_service_running(tmp_path) == []
    finally:
        child.kill()
        child.wait(timeout=10)
