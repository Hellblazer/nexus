"""Regression tests for two coupled daemon bugs (2026-06-05, conexus 5.10.0).

nexus-we61e — ``reclaim_stale`` is a GLOBAL janitor op but used to run inside
every per-process aspect worker's poll loop, so N nx-mcp processes RPC'd N
redundant reclaim UPDATEs into the one T2 daemon — the WAL-lock contention
that pegged a core on ``database is locked`` after a restart. Reclaim now runs
once, on the daemon's own periodic loop.

nexus-64w50 — a spawn-lock loser that polled and found no reachable winner
(``attached=False``) used to quit, which left ZERO daemons when the incumbent
was mid-exit in the RDR-129 defer-release-to-exit drain window (lock still
held, discovery file already unlinked). ``run_t2_daemon`` now retries the
spawn so the freed lock is re-acquired rather than orphaning the service.
"""

from __future__ import annotations

import asyncio
import logging
import time
from types import SimpleNamespace
from unittest.mock import MagicMock

import pytest
import structlog
from structlog.testing import capture_logs

import nexus.daemon.t2_daemon as t2_daemon
from nexus.daemon.t2_daemon import T2Daemon, T2SpawnLockLost, run_t2_daemon


# ── nexus-we61e: worker no longer reclaims; daemon owns reclaim ───────────────


def test_worker_poll_does_not_reclaim_stale(monkeypatch: pytest.MonkeyPatch) -> None:
    """The per-process aspect worker's poll claims rows but never calls
    ``reclaim_stale`` — that op is the daemon's responsibility now."""
    from nexus.aspect_worker import AspectExtractionWorker

    reclaim = MagicMock(name="reclaim_stale")

    worker = AspectExtractionWorker(poll_interval=0.0)

    def _claim_batch(_n):  # noqa: ANN001
        # Stop the loop after exactly one claim so the test is bounded.
        worker._stop_event.set()
        return []

    fake_t2 = SimpleNamespace(
        aspect_queue=SimpleNamespace(
            reclaim_stale=reclaim, claim_batch=_claim_batch,
        )
    )

    import nexus.mcp_infra as infra

    monkeypatch.setattr(infra, "t2_index_write", lambda fn: fn(fake_t2))

    worker._run_loop()

    reclaim.assert_not_called()


def test_daemon_reclaim_loop_calls_reclaim_stale(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The daemon's periodic loop is the sole caller of ``reclaim_stale``,
    invoked with the stale-row timeout window."""
    monkeypatch.setattr(t2_daemon, "_ASPECT_RECLAIM_INTERVAL", 0.0)

    reclaim = MagicMock(name="reclaim_stale", return_value=3)
    daemon = T2Daemon(config_dir=None, db_path=None)  # type: ignore[arg-type]
    daemon._t2db = SimpleNamespace(
        aspect_queue=SimpleNamespace(reclaim_stale=reclaim)
    )

    async def _drive() -> None:
        task = asyncio.create_task(daemon._reclaim_stale_loop())
        # Let the zero-interval loop tick a few times, then cancel.
        await asyncio.sleep(0.05)
        task.cancel()
        try:
            await task
        except asyncio.CancelledError:
            pass

    asyncio.run(_drive())

    assert reclaim.call_count >= 1
    # Called positionally with the stale-row timeout window.
    assert reclaim.call_args[0][0] == t2_daemon._ASPECT_RECLAIM_STALE_TIMEOUT_S


def test_daemon_reclaim_loop_survives_reclaim_error(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A reclaim failure is logged best-effort and never crashes the loop."""
    monkeypatch.setattr(t2_daemon, "_ASPECT_RECLAIM_INTERVAL", 0.0)

    reclaim = MagicMock(side_effect=RuntimeError("locked"))
    daemon = T2Daemon(config_dir=None, db_path=None)  # type: ignore[arg-type]
    daemon._t2db = SimpleNamespace(
        aspect_queue=SimpleNamespace(reclaim_stale=reclaim)
    )

    async def _drive() -> None:
        task = asyncio.create_task(daemon._reclaim_stale_loop())
        await asyncio.sleep(0.02)
        task.cancel()
        try:
            await task
        except asyncio.CancelledError:
            pass

    # Must not raise out of the loop.
    asyncio.run(_drive())
    assert reclaim.call_count >= 1


# ── nexus-64w50: spawn-loser retries instead of orphaning the service ─────────


@pytest.fixture
def _info_logs(monkeypatch: pytest.MonkeyPatch):
    """Capture structlog at INFO and stub the side-effecting seams of
    ``run_t2_daemon`` (logging config + sleep), yielding the captured list."""
    import nexus.logging_setup as logging_setup

    structlog.configure(
        wrapper_class=structlog.make_filtering_bound_logger(logging.INFO),
    )
    monkeypatch.setattr(logging_setup, "configure_logging", lambda *a, **k: None)
    monkeypatch.setattr(t2_daemon, "_SPAWN_LOST_RETRY_BACKOFF", 0.0)
    monkeypatch.setattr(time, "sleep", lambda _s: None)


def _stub_asyncio_run(monkeypatch: pytest.MonkeyPatch, outcomes: list):
    """Replace ``asyncio.run`` with a scripted sequence. Each outcome is
    either an exception instance (raised) or a sentinel for clean return.
    The passed coroutine is closed to avoid 'never awaited' warnings."""
    calls: list[int] = []

    def _fake_run(coro):  # noqa: ANN001
        coro.close()
        idx = len(calls)
        calls.append(1)
        outcome = outcomes[idx]
        if isinstance(outcome, BaseException):
            raise outcome
        return outcome

    monkeypatch.setattr(asyncio, "run", _fake_run)
    return calls


def test_spawn_loss_retries_then_acquires(
    monkeypatch: pytest.MonkeyPatch, _info_logs,
) -> None:
    """No reachable winner + lock collisions that clear → the loser retries
    and eventually wins the freed lock (clean daemon run), never orphaning."""
    monkeypatch.setattr(t2_daemon, "_poll_for_winner", lambda *a, **k: False)
    calls = _stub_asyncio_run(
        monkeypatch,
        [T2SpawnLockLost("held"), T2SpawnLockLost("held"), None],
    )

    with capture_logs() as logs:
        result = run_t2_daemon(config_dir=None, db_path=None)  # type: ignore[arg-type]

    assert result is None
    assert len(calls) == 3  # two losses, then the winning run
    retries = [e for e in logs if e.get("event") == "t2_daemon_spawn_lost_retry"]
    assert len(retries) == 2
    # Won the lock on attempt 3 → no terminal spawn_lost, no crash.
    assert not [e for e in logs if e.get("event") == "t2_daemon_spawn_lost"]
    assert not [e for e in logs if e.get("event") == "t2_daemon_crashed"]


def test_spawn_loss_exhausts_retries_without_crash(
    monkeypatch: pytest.MonkeyPatch, _info_logs,
) -> None:
    """Persistent collision + no winner → bounded retries, then a clean
    terminal ``spawn_lost`` (attached=False). Never an unbounded spin or a
    crash."""
    monkeypatch.setattr(t2_daemon, "_poll_for_winner", lambda *a, **k: False)
    calls = _stub_asyncio_run(
        monkeypatch,
        [T2SpawnLockLost("held")] * t2_daemon._SPAWN_LOST_RETRY_MAX,
    )

    with capture_logs() as logs:
        result = run_t2_daemon(config_dir=None, db_path=None)  # type: ignore[arg-type]

    assert result is None
    assert len(calls) == t2_daemon._SPAWN_LOST_RETRY_MAX
    retries = [e for e in logs if e.get("event") == "t2_daemon_spawn_lost_retry"]
    assert len(retries) == t2_daemon._SPAWN_LOST_RETRY_MAX - 1
    terminal = [e for e in logs if e.get("event") == "t2_daemon_spawn_lost"]
    assert len(terminal) == 1
    assert terminal[0].get("attached") is False
    assert not [e for e in logs if e.get("event") == "t2_daemon_crashed"]


def test_spawn_loss_to_live_winner_does_not_retry(
    monkeypatch: pytest.MonkeyPatch, _info_logs,
) -> None:
    """A reachable serving winner (attached=True) is never disturbed: the
    loser exits immediately on the first loss, no retries."""
    monkeypatch.setattr(t2_daemon, "_poll_for_winner", lambda *a, **k: True)
    calls = _stub_asyncio_run(monkeypatch, [T2SpawnLockLost("held")])

    with capture_logs() as logs:
        result = run_t2_daemon(config_dir=None, db_path=None)  # type: ignore[arg-type]

    assert result is None
    assert len(calls) == 1
    assert not [e for e in logs if e.get("event") == "t2_daemon_spawn_lost_retry"]
    terminal = [e for e in logs if e.get("event") == "t2_daemon_spawn_lost"]
    assert len(terminal) == 1
    assert terminal[0].get("attached") is True
