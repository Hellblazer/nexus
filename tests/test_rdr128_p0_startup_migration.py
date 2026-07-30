# SPDX-License-Identifier: AGPL-3.0-or-later
"""RDR-128 P0a (RF-3): startup-migration lock tolerance.

The T2 daemon's startup migration (``T2Database.bootstrap_schema`` ->
``apply_pending``) must tolerate another process (typically ``nx index
repo``) holding memory.db's single WAL writer lock. Before this fix the
migration connection used a 5s ``busy_timeout`` with no retry, so a
concurrent indexer could push a migration step past the limit and crash
the freshly-spawned daemon on ``database is locked`` — and because
``ensure-running`` is one-shot, the daemon was then left down (the
post-5.0.4 crash-loop, RF-3 x RF-4).

Two layers of tolerance, both tested here:

* a >= 30s ``busy_timeout`` so each statement waits out the realistic
  intra-host contention window (mirrors aspect_extraction_queue,
  nexus-v4m7y);
* a bounded Python-level retry around ``apply_pending`` so a migration
  that still trips ``database is locked`` is re-attempted (idempotent)
  rather than crashing.
"""
from __future__ import annotations

import sqlite3
import threading
import time
from pathlib import Path

import pytest

from nexus.db.t2 import (
    _BOOTSTRAP_BUSY_TIMEOUT_MS,
    T2Database,
    _apply_pending_with_lock_retry,
)


def test_busy_timeout_constant_is_at_least_30s() -> None:
    """RDR-128 P0a spec: busy_timeout >= 30000 ms."""
    assert _BOOTSTRAP_BUSY_TIMEOUT_MS >= 30000


def test_retry_recovers_after_one_transient_lock() -> None:
    """A single ``database is locked`` is absorbed; the second attempt
    succeeds and ``apply_pending`` is not retried further."""
    calls = {"n": 0}

    def _fake_apply_pending(conn, current_version):  # noqa: ANN001
        calls["n"] += 1
        if calls["n"] == 1:
            raise sqlite3.OperationalError("database is locked")

    # Patch the symbol the helper imports lazily.
    import nexus.db.migrations as _mig

    orig = _mig.apply_pending
    _mig.apply_pending = _fake_apply_pending  # type: ignore[assignment]
    try:
        # No real sleep delay needed for correctness, but the helper does
        # sleep between attempts; keep it tiny by patching the sleep table.
        import nexus.db.t2 as _t2

        orig_sleeps = _t2._BOOTSTRAP_RETRY_SLEEPS_BETWEEN
        _t2._BOOTSTRAP_RETRY_SLEEPS_BETWEEN = (0.0, 0.0)  # type: ignore[attr-defined]
        try:
            _apply_pending_with_lock_retry(sqlite3.connect(":memory:"), "9.9.9")
        finally:
            _t2._BOOTSTRAP_RETRY_SLEEPS_BETWEEN = orig_sleeps  # type: ignore[attr-defined]
    finally:
        _mig.apply_pending = orig  # type: ignore[assignment]

    assert calls["n"] == 2, "expected exactly one retry then success"


def test_retry_exhausts_and_reraises_on_persistent_lock() -> None:
    """A lock that never clears re-raises after the bounded attempts —
    the helper must NOT hang indefinitely."""
    calls = {"n": 0}

    def _always_locked(conn, current_version):  # noqa: ANN001
        calls["n"] += 1
        raise sqlite3.OperationalError("database is locked")

    import nexus.db.migrations as _mig
    import nexus.db.t2 as _t2

    orig = _mig.apply_pending
    orig_sleeps = _t2._BOOTSTRAP_RETRY_SLEEPS_BETWEEN
    _mig.apply_pending = _always_locked  # type: ignore[assignment]
    _t2._BOOTSTRAP_RETRY_SLEEPS_BETWEEN = (0.0, 0.0)  # type: ignore[attr-defined]
    try:
        with pytest.raises(sqlite3.OperationalError, match="locked"):
            _apply_pending_with_lock_retry(sqlite3.connect(":memory:"), "9.9.9")
    finally:
        _mig.apply_pending = orig  # type: ignore[assignment]
        _t2._BOOTSTRAP_RETRY_SLEEPS_BETWEEN = orig_sleeps  # type: ignore[attr-defined]

    assert calls["n"] == len(orig_sleeps) + 1, "must try exactly max_attempts times"


def test_non_lock_operational_error_propagates_immediately() -> None:
    """A non-lock OperationalError (e.g. schema corruption) is not a
    contention signal — propagate on the first attempt, no retry."""
    calls = {"n": 0}

    def _schema_error(conn, current_version):  # noqa: ANN001
        calls["n"] += 1
        raise sqlite3.OperationalError("no such table: bogus")

    import nexus.db.migrations as _mig

    orig = _mig.apply_pending
    _mig.apply_pending = _schema_error  # type: ignore[assignment]
    try:
        with pytest.raises(sqlite3.OperationalError, match="no such table"):
            _apply_pending_with_lock_retry(sqlite3.connect(":memory:"), "9.9.9")
    finally:
        _mig.apply_pending = orig  # type: ignore[assignment]

    assert calls["n"] == 1, "non-lock errors must not be retried"


