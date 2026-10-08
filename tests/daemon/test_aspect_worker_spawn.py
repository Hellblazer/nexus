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


#: The real predicate, captured before the autouse fixture stubs it.
_REAL_CLAUDE_AVAILABLE = awd._claude_available


@pytest.fixture(autouse=True)
def _clear_spawn_dedup(monkeypatch):
    """The intra-process spawn-suppression dict is a module global; clear it so
    one test's spawn does not suppress the next test's expected spawn.

    ``claude`` is present unless a test says otherwise: the spawner refuses to
    fork without it, so these tests would otherwise depend on the box running
    them having the binary installed."""
    awd._recent_spawn.clear()
    monkeypatch.setattr(awd, "_claude_absent_logged", False)
    monkeypatch.setattr(awd, "_claude_available", lambda: True)
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
    ensure_aspect_worker_daemon(
        config_dir=tmp_path, tenant="default", _popen=_FakePopen, _platform="linux",
    )
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


def test_ensure_aspect_worker_spawn_failure_is_swallowed(monkeypatch) -> None:
    """The row is already enqueued; a daemon-spawn failure must not fail the store."""
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
    monkeypatch.setenv("NEXUS_CONFIG_DIR", str(tmp_path))

    # hygiene-001 review round item B: this test is about daemon-spawn
    # wiring, not doc_id resolution -- t2_index_write is stubbed to a
    # no-op above, so no real engine FK check happens; supply an explicit
    # doc_id so the hook's new resolve-or-skip gate never short-circuits
    # before reaching the daemon-spawn path this test asserts on.
    aw.aspect_extraction_enqueue_hook(
        "/p/doc.pdf", "knowledge__o__m__v1", "content", doc_id="1.2.3",
    )
    assert len(_FakePopen.calls) == 1   # the hook chain reached the daemon-spawn path


# ── no spawn without `claude` ────────────────────────────────────────────────
#
# Measured 2026-10-08 on a local-mode box without the `claude` binary: every
# indexing step and every `nx store put` reached the spawner, which forked a
# daemon that refused at once (`aspect_worker_daemon.missing_claude_credentials`)
# 119 times and left a 400 KB crash log. The child's refusal stays as the
# backstop; the spawner now checks the same precondition first.


def test_no_spawn_without_claude(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setattr(awd, "_claude_available", lambda: False)
    _FakePopen.reset()

    up = ensure_aspect_worker_daemon(config_dir=tmp_path, tenant="default", _popen=_FakePopen)

    assert up is False, "no daemon is up and none was started"
    assert _FakePopen.calls == []


def test_the_absence_is_logged_once_per_process(tmp_path: Path, monkeypatch) -> None:
    from structlog.testing import capture_logs

    monkeypatch.setattr(awd, "_claude_available", lambda: False)
    _FakePopen.reset()

    with capture_logs() as logs:
        for _ in range(5):
            ensure_aspect_worker_daemon(config_dir=tmp_path, tenant="default", _popen=_FakePopen)

    skipped = [e for e in logs if e["event"] == "aspect_worker_daemon.spawn_skipped_claude_absent"]
    assert len(skipped) == 1, logs
    assert skipped[0]["log_level"] == "warning"
    assert "claude" in skipped[0]["hint"]
    assert _FakePopen.calls == []


def test_a_spawn_happens_when_claude_exists(tmp_path: Path, monkeypatch) -> None:
    """The same call, claude present: it spawns and logs no absence."""
    from structlog.testing import capture_logs

    monkeypatch.setattr(awd, "_claude_available", lambda: True)
    _FakePopen.reset()

    with capture_logs() as logs:
        up = ensure_aspect_worker_daemon(config_dir=tmp_path, tenant="default", _popen=_FakePopen)

    assert up is True
    assert len(_FakePopen.calls) == 1
    assert not [e for e in logs if e["event"] == "aspect_worker_daemon.spawn_skipped_claude_absent"]


def test_a_running_daemon_is_reported_up_even_when_claude_is_not_on_this_path(
    tmp_path: Path, monkeypatch,
) -> None:
    """A daemon some other process spawned is up; this process's PATH does not
    change that, and the spawner has nothing to refuse."""
    monkeypatch.setattr(awd, "_claude_available", lambda: False)
    _publish_live_lease(tmp_path, "default")
    _FakePopen.reset()

    assert ensure_aspect_worker_daemon(config_dir=tmp_path, tenant="default", _popen=_FakePopen) is True
    assert _FakePopen.calls == []


def test_the_availability_check_reads_path(monkeypatch) -> None:
    """The real predicate, not the stub: it is what both the spawner and the
    daemon's own guard ask."""
    monkeypatch.setattr("shutil.which", lambda name: None)
    assert _REAL_CLAUDE_AVAILABLE() is False
    monkeypatch.setattr("shutil.which", lambda name: "/usr/local/bin/claude" if name == "claude" else None)
    assert _REAL_CLAUDE_AVAILABLE() is True


def test_the_daemons_own_refusal_remains_the_backstop(monkeypatch) -> None:
    monkeypatch.setattr(awd, "_claude_available", lambda: False)
    with pytest.raises(RuntimeError, match="claude"):
        awd._require_extraction_credentials()


@pytest.mark.parametrize("claude_on_path", [False, True])
def test_the_spawner_and_the_daemon_guard_share_one_predicate(
    tmp_path: Path, monkeypatch, claude_on_path: bool,
) -> None:
    """The spawner and the daemon's own guard must ask the SAME question.

    The autouse stub is lifted and the real predicate restored, then the only
    input is ``shutil.which``: flipping it must flip BOTH the spawner (spawns
    or not) and the guard (raises or not). Two predicates that happened to
    agree on a stub would diverge here; a stub-only test could not see it.
    """
    monkeypatch.setattr(awd, "_claude_available", _REAL_CLAUDE_AVAILABLE)
    monkeypatch.setattr(
        "shutil.which", lambda name: "/usr/local/bin/claude" if claude_on_path else None,
    )
    _FakePopen.reset()

    up = ensure_aspect_worker_daemon(config_dir=tmp_path, tenant="default", _popen=_FakePopen)
    if claude_on_path:
        awd._require_extraction_credentials()  # does not raise
    else:
        with pytest.raises(RuntimeError, match="claude"):
            awd._require_extraction_credentials()

    assert up is claude_on_path
    assert len(_FakePopen.calls) == (1 if claude_on_path else 0)


def test_both_entry_points_call_through_the_module_level_predicate(
    tmp_path: Path, monkeypatch,
) -> None:
    """Replacing ``_claude_available`` moves both entry points together, in both
    directions, and each is observed refusing then allowing."""
    state = {"available": False}
    monkeypatch.setattr(awd, "_claude_available", lambda: state["available"])

    for available in (False, True):
        state["available"] = available
        awd._recent_spawn.clear()
        _FakePopen.reset()
        up = ensure_aspect_worker_daemon(config_dir=tmp_path, tenant="default", _popen=_FakePopen)
        guard_raised = False
        try:
            awd._require_extraction_credentials()
        except RuntimeError:
            guard_raised = True
        assert (up, guard_raised) == (available, not available)
