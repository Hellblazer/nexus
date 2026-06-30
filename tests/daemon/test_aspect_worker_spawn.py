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

import nexus.aspect_worker as aw
from nexus.daemon.aspect_worker_daemon import (
    TIER,
    ensure_aspect_worker_daemon,
)
from nexus.db import storage_mode
from nexus.daemon.service_registry import (
    ServiceRegistry,
    ServiceSupervisor,
    ttl_for_tier,
)


class _FakePopen:
    """Captures the spawn argv + kwargs instead of forking a process."""

    calls: list[dict] = []

    def __init__(self, argv, **kwargs) -> None:
        type(self).calls.append({"argv": argv, "kwargs": kwargs})
        self.pid = 4242

    @classmethod
    def reset(cls) -> None:
        cls.calls = []


def _publish_live_lease(config_dir: Path, tenant: str) -> None:
    """Make discover(tenant) resolve a fresh lease (a daemon is 'already up')."""
    reg = ServiceRegistry(dir=config_dir, tier=TIER, ttl=ttl_for_tier(TIER))
    sup = ServiceSupervisor(reg, scope_key=tenant, version="t", endpoint_provider=lambda: {"pid": os.getpid()})
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
