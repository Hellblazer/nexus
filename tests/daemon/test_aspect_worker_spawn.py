# SPDX-License-Identifier: AGPL-3.0-or-later
"""RDR-173 Phase 2 (beads nexus-gtdtc + nexus-x01oe) — spawn-if-absent from the
enqueue hook, with the spawn-as-child inherited-env credential model.

Approach item 2: the enqueue hook stops spawning an in-process daemon thread and
instead ensures the leased daemon is up (discover → spawn-if-absent via the
Phase-1 single-flight election). No extraction work happens in the storing
process; extraction completes for every store path because it no longer depends
on the storing process's lifetime.

CREDENTIAL MODEL (x01oe, the load-bearing Critical): the daemon is spawned as a
CHILD of the enqueue-triggering process so it INHERITS that process's
environment — the ``claude`` binary on ``PATH``, ``~/.claude``, and the
Anthropic credential context those store paths already use for ``claude -p``.
The spawn therefore must NOT pass an ``env=`` override (which would sever the
inherited context); it detaches (``start_new_session=True``) so the daemon
survives the short-lived storing process. A credential-bare spawn path is
forbidden — there is deliberately no autostart/launchd install for this tier.
"""
from __future__ import annotations

import os
from pathlib import Path
from typing import ClassVar

import pytest

import nexus.aspect_worker as aw
import nexus.daemon.aspect_worker_daemon as awd
from nexus.daemon.aspect_worker_daemon import (
    TIER,
    _daemon_version,
    ensure_aspect_worker_daemon,
)
from nexus.daemon.service_registry import (
    ServiceRegistry,
    ServiceSupervisor,
    ttl_for_tier,
)
from nexus.db import storage_mode


@pytest.fixture(autouse=True)
def _clear_spawn_dedup():
    """The intra-process spawn-suppression dict is a module global; clear it so
    one test's spawn does not suppress the next test's expected spawn."""
    awd._recent_spawn.clear()
    yield
    awd._recent_spawn.clear()


class _FakePopen:
    """Captures the spawn argv + kwargs instead of forking a process."""

    calls: ClassVar[list[dict]] = []

    def __init__(self, argv, **kwargs) -> None:
        type(self).calls.append({"argv": argv, "kwargs": kwargs})
        self.pid = 4242

    @classmethod
    def reset(cls) -> None:
        cls.calls = []


def _publish_live_lease(config_dir: Path, tenant: str, *, version: str | None = None) -> None:
    """Make discover(tenant) resolve a fresh lease (a daemon is 'already up').
    Defaults to the CURRENT daemon version so ensure_* reads it as up-to-date."""
    reg = ServiceRegistry(dir=config_dir, tier=TIER, ttl=ttl_for_tier(TIER))
    sup = ServiceSupervisor(
        reg, scope_key=tenant, version=version or _daemon_version(),
        endpoint_provider=lambda: {"pid": os.getpid()},
    )
    sup.publish_once()


def test_spawns_when_absent(tmp_path: Path) -> None:
    _FakePopen.reset()
    up = ensure_aspect_worker_daemon(config_dir=tmp_path, tenant="default", _popen=_FakePopen)
    assert up is True
    assert len(_FakePopen.calls) == 1
    argv = _FakePopen.calls[0]["argv"]
    assert argv[-4:] == ["--config-dir", str(tmp_path), "--tenant", "default"]
    assert "daemon" in argv and "aspect-worker" in argv and "start" in argv


def test_noop_when_already_running(tmp_path: Path) -> None:
    _FakePopen.reset()
    _publish_live_lease(tmp_path, "default")
    up = ensure_aspect_worker_daemon(config_dir=tmp_path, tenant="default", _popen=_FakePopen)
    assert up is True
    assert _FakePopen.calls == []   # discovered an existing daemon — no spawn


def test_spawn_inherits_env_not_overridden(tmp_path: Path) -> None:
    """x01oe: the spawn must NOT pass env= — the child inherits the parent's
    environment (PATH, ~/.claude, Anthropic creds) so claude -p works."""
    _FakePopen.reset()
    ensure_aspect_worker_daemon(config_dir=tmp_path, tenant="default", _popen=_FakePopen)
    kwargs = _FakePopen.calls[0]["kwargs"]
    # No env override at all (inherits os.environ), or an explicit pass-through.
    assert "env" not in kwargs or kwargs["env"] is os.environ


def test_spawn_is_detached_child(tmp_path: Path) -> None:
    """Detached (start_new_session) so the daemon survives the short-lived
    storing process, but still a child that inherited its env at fork."""
    _FakePopen.reset()
    ensure_aspect_worker_daemon(config_dir=tmp_path, tenant="default", _popen=_FakePopen)
    assert _FakePopen.calls[0]["kwargs"].get("start_new_session") is True


# ── enqueue-hook branch (gtdtc): SERVICE → daemon, LOCAL(sqlite) → in-process ──


def test_ensure_aspect_worker_service_mode_uses_daemon(monkeypatch) -> None:
    monkeypatch.setattr(storage_mode, "storage_backend_for",
                        lambda _s: storage_mode.StorageBackend.SERVICE)
    daemon_calls: list = []
    inproc_calls: list = []
    monkeypatch.setattr("nexus.daemon.aspect_worker_daemon.ensure_aspect_worker_daemon",
                        lambda **k: daemon_calls.append(k) or True)
    monkeypatch.setattr(aw, "ensure_worker_started", lambda *a, **k: inproc_calls.append(1))

    aw._ensure_aspect_worker()
    assert len(daemon_calls) == 1          # leased daemon ensured
    assert daemon_calls[0]["tenant"] == "default"
    assert inproc_calls == []              # NO in-process thread in service mode


def test_ensure_aspect_worker_local_mode_uses_in_process(monkeypatch) -> None:
    monkeypatch.setattr(storage_mode, "storage_backend_for",
                        lambda _s: storage_mode.StorageBackend.SQLITE)
    daemon_calls: list = []
    inproc_calls: list = []
    monkeypatch.setattr("nexus.daemon.aspect_worker_daemon.ensure_aspect_worker_daemon",
                        lambda **k: daemon_calls.append(k) or True)
    monkeypatch.setattr(aw, "ensure_worker_started", lambda *a, **k: inproc_calls.append(1))

    aw._ensure_aspect_worker()
    assert inproc_calls == [1]             # in-process thread kept in local mode
    assert daemon_calls == []              # NO daemon spawn in local mode


def test_ensure_aspect_worker_spawn_failure_is_swallowed(monkeypatch) -> None:
    """The row is already enqueued; a daemon-spawn failure must not fail the store."""
    monkeypatch.setattr(storage_mode, "storage_backend_for",
                        lambda _s: storage_mode.StorageBackend.SERVICE)

    def _boom(**_k):
        raise RuntimeError("spawn blew up")

    monkeypatch.setattr("nexus.daemon.aspect_worker_daemon.ensure_aspect_worker_daemon", _boom)
    aw._ensure_aspect_worker()  # must not raise


def test_stale_version_lease_triggers_respawn(tmp_path: Path) -> None:
    """A live lease on a DIFFERENT (stale) version must trigger a spawn — the new
    daemon fences the stale predecessor. Closes the version_cycle carry-forward
    (review SIG-1)."""
    _FakePopen.reset()
    _publish_live_lease(tmp_path, "default", version="0.0.0-ancient")
    ensure_aspect_worker_daemon(config_dir=tmp_path, tenant="default", _popen=_FakePopen)
    assert len(_FakePopen.calls) == 1   # stale version → respawn (fences the old)


def test_intra_process_dedup_suppresses_repeat_spawn(tmp_path: Path) -> None:
    """A batch of enqueues in ONE process must not fire N forks before the daemon
    publishes: after the first spawn, subsequent calls within the suppression
    window are no-ops (review M2)."""
    _FakePopen.reset()
    clock = [1000.0]
    for _ in range(50):
        ensure_aspect_worker_daemon(
            config_dir=tmp_path, tenant="default",
            _popen=_FakePopen, _clock=lambda: clock[0],
        )
    assert len(_FakePopen.calls) == 1   # 50 calls, ONE spawn (window suppresses)


def test_enqueue_hook_service_mode_reaches_daemon_spawn(tmp_path, monkeypatch) -> None:
    """END-TO-END: aspect_extraction_enqueue_hook with AUTOSTART on + SERVICE mode
    must actually reach ensure_aspect_worker_daemon (not just _ensure_aspect_worker
    in isolation) — proving the full hook chain wires through (review SIG-3)."""
    _FakePopen.reset()
    monkeypatch.setenv("NX_ASPECT_WORKER_AUTOSTART", "1")
    monkeypatch.setattr(storage_mode, "storage_backend_for",
                        lambda _s: storage_mode.StorageBackend.SERVICE)
    # The collection must have a registered extractor or the hook early-returns.
    monkeypatch.setattr("nexus.aspect_extractor.select_config", lambda _c: object())
    # The enqueue itself is routed through t2_index_write — stub it to a no-op so
    # the test needs no live service queue.
    monkeypatch.setattr("nexus.mcp_infra.t2_index_write", lambda fn: None)
    monkeypatch.setattr(awd, "ensure_aspect_worker_daemon",
                        lambda **k: _FakePopen(["spawned"], tenant=k.get("tenant")))
    monkeypatch.setattr(aw, "nexus_config_dir", lambda: tmp_path)

    aw.aspect_extraction_enqueue_hook("/p/doc.pdf", "knowledge__o__m__v1", "content")
    assert len(_FakePopen.calls) == 1   # the hook chain reached the daemon-spawn path
