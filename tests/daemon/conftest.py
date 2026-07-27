# SPDX-License-Identifier: AGPL-3.0-or-later
"""Daemon-suite fixtures (nexus-aqbrk, RDR-158/155 substrate port)."""
from __future__ import annotations

import pytest


@pytest.fixture(autouse=True)
def _pin_daemon_suite_to_local_t2(monkeypatch: pytest.MonkeyPatch) -> None:
    """Pin the T2 backend to SQLite for everything under ``tests/daemon/``.

    THE T2 DAEMON IS THE SQLITE SINGLE-WRITER. That is its entire purpose:
    it serialises writes to ``memory.db`` so concurrent CLI processes and the
    MCP server do not contend on the WAL writer lock. In service mode there is
    nothing for it to do, and ``_t2_ensure_running_inner`` says exactly that —
    it short-circuits to ``T2EnsureOutcome.SERVICE_MODE_SKIP`` with "T2 daemon
    not needed: memory store is in service mode" before any of the lifecycle
    state machine runs (commands/daemon.py:775). So under the engine substrate
    these tests were asserting against a state machine that never executed.

    Three distinct failure shapes, all the same root:

    - ``SERVICE_MODE_SKIP`` where a real outcome (REACHABLE, SPAWN_FAILED,
      CRASHLOOP_SUPPRESSED, DEFERRED_WRITE_LOCK, DEFERRED_SIGTERM) was
      expected — the guard above, working.
    - "daemon did not start" — the daemon genuinely cannot start when any
      store is service-backed: ``_build_dispatch_table`` enumerates stores
      with ``dir()`` + a bare ``getattr`` and trips the fail-loud ``.conn``
      guard (nexus-uqiyo). This suite is the bulk of that bead's reach.
    - ``HttpMemoryStore ... has no raw SQLite 'conn'`` — the same guard,
      reached directly.

    NOTHING IS LOST BY PINNING. No file under ``tests/daemon/`` opts into the
    engine substrate, and the four that test service-mode behaviour
    (test_t2_ensure_running_inner, test_t2_daemon_observability,
    test_aspect_worker_observability, test_aspect_worker_spawn) do it by
    monkeypatching ``storage_backend_for`` to ``StorageBackend.SERVICE``
    directly — an attribute patch, which overrides this env pin and is
    substrate-independent by construction. The SERVICE_MODE_SKIP contract in
    particular has two dedicated tests that continue to run here.

    Retirement note: the daemon retires with the SQLite substrate it guards
    (nexus-i711w) — not before, and not silently.
    """
    monkeypatch.setenv("NX_STORAGE_BACKEND", "sqlite")
