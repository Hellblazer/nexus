# SPDX-License-Identifier: Apache-2.0
"""Direct-mode `data_version` polling watcher for tuples.db (RDR-110 P1.4).

**Direct-mode only** — this module must NOT be used when
``NX_STORAGE_MODE=daemon``. In daemon mode the daemon owns the single
``tuples.db`` connection and exposes client-side wake-up via the blocking-take
RPC (RDR-112 §7). Instantiating ``_TupleSpaceWatcher`` under daemon mode
raises ``StorageModeError`` immediately.

The watcher opens its own read-only connection to ``tuples.db`` in a dedicated
daemon thread, polls ``PRAGMA data_version`` every 1 ms, and fires a
``threading.Event`` on any increment.  All blocking ``take`` calls in
direct-mode share the same ``wake_event``; spurious wakes (commits not relevant
to the caller's subspace) cost one extra CAS attempt that returns no row.

CPU cost: one integer read per millisecond per database.  Acceptable for the
direct-mode development path; not suitable for high-frequency production use
(that is the daemon's job).

RDR-110 §Mode-split note: the watcher is gated behind
``NX_STORAGE_MODE == "direct"`` (or unset, which is treated as direct).
"""

from __future__ import annotations

import os
import sqlite3
import threading
from pathlib import Path
from typing import Optional

import structlog

_log = structlog.get_logger(__name__)

_POLL_INTERVAL: float = 0.001  # 1 ms


# ---------------------------------------------------------------------------
# Errors
# ---------------------------------------------------------------------------


class StorageModeError(RuntimeError):
    """Raised when _TupleSpaceWatcher is constructed under daemon mode."""


# ---------------------------------------------------------------------------
# Watcher
# ---------------------------------------------------------------------------


class _TupleSpaceWatcher:
    """Poll ``PRAGMA data_version`` and fire *wake_event* on any commit.

    Direct-mode only per RDR-110 §Mode-split note.

    Args:
        db_path: Filesystem path to the ``tuples.db`` file.
        wake_event: A ``threading.Event`` that is set on each detected
            ``data_version`` increment.  Callers must ``clear()`` it before
            blocking; the watcher sets it but never clears it.
    """

    def __init__(self, db_path: Path, wake_event: threading.Event) -> None:
        # Guard: reject daemon mode immediately so the caller fails loudly
        # rather than silently opening a second writer against the daemon's db.
        storage_mode = os.environ.get("NX_STORAGE_MODE", "").lower()
        if storage_mode == "daemon":
            raise StorageModeError(
                "_TupleSpaceWatcher is direct-mode only; "
                "NX_STORAGE_MODE=daemon detected. "
                "In daemon mode the daemon owns tuples.db. "
                "Use the blocking-take RPC instead (RDR-112 §7)."
            )

        self._db_path = db_path
        self._wake_event = wake_event
        self._stop_event = threading.Event()
        self._thread: Optional[threading.Thread] = None

    # ------------------------------------------------------------------
    # Lifecycle
    # ------------------------------------------------------------------

    def start(self) -> None:
        """Start the polling thread (idempotent — no-op if already running)."""
        if self._thread is not None and self._thread.is_alive():
            _log.debug("tuplespace_watcher_already_running", db=str(self._db_path))
            return

        self._stop_event.clear()
        self._thread = threading.Thread(
            target=self._poll_loop,
            name="tuplespace-watcher",
            daemon=True,
        )
        self._thread.start()
        _log.info("tuplespace_watcher_started", db=str(self._db_path))

    def stop(self) -> None:
        """Signal the polling thread to stop and join it."""
        self._stop_event.set()
        if self._thread is not None and self._thread.is_alive():
            self._thread.join(timeout=1.0)
        _log.info("tuplespace_watcher_stopped", db=str(self._db_path))

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
            _log.error(
                "tuplespace_watcher_connect_failed",
                db=str(self._db_path),
                error=str(exc),
            )
            return

        try:
            last_version: Optional[int] = None

            while not self._stop_event.is_set():
                try:
                    row = conn.execute("PRAGMA data_version").fetchone()
                    version: int = row[0] if row else 0

                    if last_version is None:
                        last_version = version
                    elif version != last_version:
                        last_version = version
                        self._wake_event.set()
                        _log.debug(
                            "tuplespace_watcher_commit_detected",
                            db=str(self._db_path),
                            data_version=version,
                        )
                except Exception as exc:
                    _log.warning(
                        "tuplespace_watcher_poll_error",
                        db=str(self._db_path),
                        error=str(exc),
                    )

                self._stop_event.wait(timeout=_POLL_INTERVAL)
        finally:
            try:
                conn.close()
            except Exception:
                pass
