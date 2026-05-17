# SPDX-License-Identifier: Apache-2.0
"""Direct-mode `data_version` polling watcher for tuples.db (RDR-110 P1.4).

**Direct-mode only**: this module must NOT be used when
``NX_STORAGE_MODE=daemon``. In daemon mode the daemon owns the single
``tuples.db`` connection and exposes client-side wake-up via the blocking-take
RPC (RDR-112 §7). Instantiating ``_DataVersionWatcher`` under daemon mode
raises ``StorageModeError`` immediately.

The watcher opens its own read-only connection to ``tuples.db`` in a dedicated
daemon thread, polls ``PRAGMA data_version`` adaptively, and fires a
``threading.Event`` on any increment.  All blocking ``take`` calls in
direct-mode share the same ``wake_event``; spurious wakes (commits not relevant
to the caller's subspace) cost one extra CAS attempt that returns no row.

CPU cost (nexus-o5tc): one integer read per
``_POLL_INTERVAL_BASELINE_SECONDS`` (1 ms) when active. After about
100 consecutive idle ticks (~100 ms of dead air) the cadence ramps
exponentially up to ``_POLL_INTERVAL_MAX_SECONDS`` (1 s), so idle CPU
drops by ~1000x relative to the always-on 1 ms baseline. Activity
resets the cadence. CA-5 (RDR-110) reactive-take latency contract
(p50 <= 5 ms) holds because the watcher returns to baseline on the
first commit. Acceptable for the direct-mode development path; not
suitable for high-frequency production use (that is the daemon's
job).

RDR-110 §Mode-split note: the watcher is gated behind
``NX_STORAGE_MODE == "direct"`` (or unset, which is treated as direct).

nexus-zrk4: class renamed from ``_TupleSpaceWatcher`` to
``_DataVersionWatcher`` so its role (data_version polling for take()
wake-ups) is self-evident and does not collide with
``_BindingWatcher`` (cockpit binding dispatch over the events table).
"""

from __future__ import annotations

import os
import sqlite3
import threading
from pathlib import Path
from typing import Optional

import structlog

_log = structlog.get_logger(__name__)

# nexus-o5tc: adaptive polling cadence. The watcher fires at
# ``_POLL_INTERVAL_BASELINE_SECONDS`` (1 ms) when activity is recent;
# this preserves the CA-5 reactive-take latency contract (p50 <= 5 ms).
# After ``_POLL_IDLE_RAMP_THRESHOLD`` consecutive idle polls (about
# 100 ms of dead air at the baseline) the interval doubles each tick
# until it reaches ``_POLL_INTERVAL_MAX_SECONDS`` (1 s), cutting the
# steady-state CPU cost on quiet systems by ~1000x. Activity (any
# data_version increment) resets the interval back to the baseline.
_POLL_INTERVAL_BASELINE_SECONDS: float = 0.001  # 1 ms when active
_POLL_INTERVAL_MAX_SECONDS: float = 1.0  # 1 s when idle for a while
_POLL_IDLE_RAMP_THRESHOLD: int = 100  # idle ticks at baseline before ramping


def _next_poll_interval(*, idle_polls: int, current: Optional[float]) -> float:
    """Compute the next polling cadence given how long we have been idle.

    ``idle_polls == 0`` means the previous tick observed activity; return
    immediately to the baseline. Otherwise ramp from the current interval
    by doubling per tick, capped at :data:`_POLL_INTERVAL_MAX_SECONDS`.
    The first ``_POLL_IDLE_RAMP_THRESHOLD`` idle polls stay at baseline
    so a brief lull does not pessimise reactive take() latency.

    Pure function: deterministic, no side effects, no I/O. Tests exercise
    the cadence shape against this helper directly.
    """
    if idle_polls == 0:
        return _POLL_INTERVAL_BASELINE_SECONDS
    if idle_polls <= _POLL_IDLE_RAMP_THRESHOLD or current is None:
        return _POLL_INTERVAL_BASELINE_SECONDS
    doubled = min(current * 2.0, _POLL_INTERVAL_MAX_SECONDS)
    # Ensure we don't somehow drop below baseline if a caller passed in a
    # smaller current value (defensive; cadence should monotonically
    # increase under sustained idle).
    return max(doubled, _POLL_INTERVAL_BASELINE_SECONDS)


# nexus-vao3 (S360-dep S1): the prior alias-retention note (alias
# kept for transition, removed in next major bump) was superseded by
# nexus-cgul.1's hard removal; see the footer at ~line 234. Removed
# so the header no longer contradicts the footer.

# ---------------------------------------------------------------------------
# Errors
# ---------------------------------------------------------------------------


class StorageModeError(RuntimeError):
    """Raised when _DataVersionWatcher is constructed under daemon mode."""


# ---------------------------------------------------------------------------
# Watcher
# ---------------------------------------------------------------------------


class DataVersionWatcher:
    """Poll ``PRAGMA data_version`` and fire *wake_event* on any commit.

    Direct-mode only per RDR-110 §Mode-split note. nexus-zrk4: was
    ``_TupleSpaceWatcher``; renamed for clarity vs ``_BindingWatcher``.

    Args:
        db_path: Filesystem path to the ``tuples.db`` file.
        wake_event: A ``threading.Event`` that is set on each detected
            ``data_version`` increment.  Callers must ``clear()`` it before
            blocking; the watcher sets it but never clears it.
    """

    def __init__(self, db_path: Path, wake_event: threading.Event) -> None:
        # Guard: reject daemon mode immediately so the caller fails loudly
        # rather than silently opening a second writer against the daemon's db.
        # nexus-507q (RDR-112 P6.3 cutover, 2026-05-17): the default flipped
        # to daemon, so an unset env now triggers this guard. Callers that
        # genuinely want a direct-mode watcher must set NX_STORAGE_MODE=direct
        # explicitly.
        from nexus.db import is_daemon_mode  # noqa: PLC0415
        if is_daemon_mode():
            raise StorageModeError(
                "_DataVersionWatcher is direct-mode only; "
                "active storage mode is daemon (set NX_STORAGE_MODE=direct "
                "to construct the watcher, or use the daemon's blocking-take "
                "RPC, RDR-112 §7)."
            )

        self._db_path = db_path
        self._wake_event = wake_event
        self._stop_event = threading.Event()
        self._thread: Optional[threading.Thread] = None
        # nexus-26b7 (notables, dim-3 N-1 + dim-4 LD-2): observable
        # poll-loop health.  ``_connect_failed`` is set if the read
        # connection cannot be opened (silent-death symptom);
        # ``_poll_error_count`` throttles repeated per-tick warnings
        # under a persistent SQLite error.
        self._connect_failed: bool = False
        self._poll_error_count: int = 0

    def is_alive_and_healthy(self) -> bool:
        """Return True when the polling thread is running and connected.

        Direct-mode callers can use this to surface a silent watcher
        death (connect failure inside the poll loop). False means the
        watcher will never fire wake_event — callers should treat the
        wake mechanism as unavailable.
        """
        if self._thread is None or not self._thread.is_alive():
            return False
        return not self._connect_failed

    # ------------------------------------------------------------------
    # Lifecycle
    # ------------------------------------------------------------------

    def start(self) -> None:
        """Start the polling thread (idempotent: no-op if already running).

        nexus-fvww: idempotency is a stable contract, not an accident.
        Calling ``start()`` while the existing thread is alive returns
        without spawning a second polling thread (which would double
        the WAL read traffic). After ``stop()``, a follow-up ``start()``
        creates a fresh thread.
        """
        if self._thread is not None and self._thread.is_alive():
            _log.debug(
                "data_version_watcher_already_running", db=str(self._db_path)
            )
            return

        self._stop_event.clear()
        # Reset health flags so a restart can recover from a prior
        # connect failure (nexus-26b7 dim-3 N-1).
        self._connect_failed = False
        self._poll_error_count = 0
        self._thread = threading.Thread(
            target=self._poll_loop,
            name="tuplespace-data-version-watcher",
            daemon=True,
        )
        self._thread.start()
        _log.info("data_version_watcher_started", db=str(self._db_path))

    def stop(self) -> None:
        """Signal the polling thread to stop and join it."""
        self._stop_event.set()
        if self._thread is not None and self._thread.is_alive():
            self._thread.join(timeout=1.0)
        _log.info("data_version_watcher_stopped", db=str(self._db_path))

    # ------------------------------------------------------------------
    # Polling loop
    # ------------------------------------------------------------------

    def _poll_loop(self) -> None:
        """Run in the watcher thread: open a read connection and poll data_version."""
        try:
            # storage-boundary-allow: direct-mode-only-watcher (RDR-110 §Mode-split note)
            conn = sqlite3.connect(str(self._db_path), check_same_thread=False)
            conn.execute("PRAGMA journal_mode=WAL")
        except Exception as exc:
            # nexus-26b7 (notable, dim-3 N-1): record the failure so
            # ``is_alive_and_healthy()`` can report it. Previously the
            # thread returned silently and callers could not tell
            # whether the wake mechanism was up.
            self._connect_failed = True
            _log.error(
                "data_version_watcher_connect_failed",
                db=str(self._db_path),
                error=str(exc),
            )
            return

        try:
            last_version: Optional[int] = None
            idle_polls: int = 0
            interval: float = _POLL_INTERVAL_BASELINE_SECONDS

            while not self._stop_event.is_set():
                activity = False
                try:
                    row = conn.execute("PRAGMA data_version").fetchone()
                    version: int = row[0] if row else 0

                    if last_version is None:
                        last_version = version
                    elif version != last_version:
                        last_version = version
                        activity = True
                        self._wake_event.set()
                        _log.debug(
                            "data_version_watcher_commit_detected",
                            db=str(self._db_path),
                            data_version=version,
                        )
                except Exception as exc:
                    # nexus-26b7 (notable, dim-4 LD-2): the poll loop
                    # runs at 1ms baseline; logging a WARNING per tick
                    # under a persistent error (db locked, file
                    # removed) would spam at 1000 lines/sec. Throttle
                    # to first-occurrence + every 1000th repeat at
                    # the active cadence.
                    self._poll_error_count += 1
                    if self._poll_error_count == 1 or (
                        self._poll_error_count % 1000 == 0
                    ):
                        _log.warning(
                            "data_version_watcher_poll_error",
                            db=str(self._db_path),
                            error=str(exc),
                            occurrence=self._poll_error_count,
                        )

                if activity:
                    idle_polls = 0
                else:
                    idle_polls += 1
                interval = _next_poll_interval(
                    idle_polls=idle_polls, current=interval
                )
                self._stop_event.wait(timeout=interval)
        finally:
            try:
                conn.close()
            except Exception:
                pass


# nexus-cgul.1 (CR-1, 2026-05-17): the ``_TupleSpaceWatcher``
# backwards-compat alias introduced by nexus-zrk4 has been removed.
# All 4 in-tree importers (mcp/core.py + 3 spike tests) and 1 unit
# test file have been migrated to the canonical ``_DataVersionWatcher``
# name. The rename has now been the de jure spelling for ~24 hours;
# any out-of-tree consumer can ride this 24-hour deprecation window
# or pin to the prior release.
