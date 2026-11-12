# SPDX-License-Identifier: AGPL-3.0-or-later
"""Daemon-suite fixtures (nexus-aqbrk, RDR-158/155 substrate port).

The ``_pin_daemon_suite_to_local_t2`` autouse fixture that pinned
``NX_STORAGE_BACKEND=sqlite`` for this whole tree is GONE (RDR-158 P3,
nexus-7bomn). Its subject — the T2 daemon, the SQLite single-writer — was
deleted in nexus-i711w Stage 2 sub-stage B, and after P3 retired the
=sqlite opt-out the pin stopped being inert: ``=sqlite`` now HARD-ERRORS
at every validation seam, so the pin turned every resolver-touching test
under ``tests/daemon/`` into a stranded-install repro (first trip:
``test_ensure_aspect_worker_spawn_failure_is_swallowed`` after the drain
path gained its fail-loud validation call). The surviving daemon tests
(aspect-worker daemon, service registry, lifecycle conformance) are
service-substrate tests and run under the suite's service default.
"""
from __future__ import annotations

import threading

import pytest

from tests.daemon import _watchdog

# The launchd uid stand-in lives with the other cross-platform test stand-ins
# (``_children.py``) so that no ``getuid`` appears in a module every run imports
# (tests/test_service_identity_lint.py sweeps conftest files).
from tests.daemon._children import launchd_uid  # noqa: F401


@pytest.fixture(autouse=True)
def _watched_tests_cannot_hang(request: pytest.FixtureRequest):
    """Per-test watchdog for the stop-channel and conformance files (see ``_watchdog``).

    Only the modules in ``WATCHED_MODULES``, and only on the main thread (a signal
    handler cannot be installed anywhere else)."""
    module = request.module.__name__.rsplit(".", 1)[-1]
    if module not in _watchdog.WATCHED_MODULES or threading.current_thread() is not threading.main_thread():
        yield
        return
    with _watchdog.watchdog(_watchdog.PER_TEST_TIMEOUT_S, request.node.nodeid):
        yield


@pytest.fixture(autouse=True)
def _no_walk_of_the_test_process_ancestry(monkeypatch: pytest.MonkeyPatch) -> None:
    """nexus-mqfu0: the process-table sweeps exclude the calling process's
    ancestors. These tests feed the sweeps fabricated rows with small pids
    (196, 214, ...), and in a container the test process's real ancestors
    can carry those same numbers, which would drop a row a test expects.
    The walk is left real for any pid other than this process."""
    import os  # noqa: PLC0415 — deferred: the fixture patches the module it names

    from nexus.daemon import service_registry  # noqa: PLC0415 — deferred: the fixture patches the module it names

    real = service_registry.ancestor_pids
    monkeypatch.setattr(
        service_registry, "ancestor_pids",
        lambda pid, **kw: frozenset() if pid == os.getpid() else real(pid, **kw),
    )


@pytest.fixture(autouse=True)
def _no_reembed_hint_read(monkeypatch: pytest.MonkeyPatch) -> None:
    """``nx daemon service start`` and the supervisor end with a courtesy catalog read for the re-embed hint
    (RDR-210 Phase 6 Step 3, nexus-cnxjk). The tests here fake the lease and the engine, so the read would reach
    storage from a thread no test provisions a tenant for; it is off by default. The hint's own tests
    (``test_service_start_reembed_hint.py``) override this fixture by name."""
    monkeypatch.setattr("nexus.reembed_job.off_profile_hint", lambda reader_factory=None: None)


@pytest.fixture(autouse=True)
def _legacy_chash_guard_finds_no_cluster_to_ask(request: pytest.FixtureRequest, monkeypatch: pytest.MonkeyPatch) -> None:
    """nexus-f3yqd: the supervisor's step 1.75 asks the bundled Postgres whether the engine's boot would delete legacy
    chash rows, and a start that cannot ask now waits instead of going on. The supervisor tests here fake Postgres and
    the engine, so the question would reach a port nobody listens on; it is answered "no changelog" (nothing to
    delete). The guard's own tests (``test_legacy_chash_guard*.py``) are left alone: they set their own."""
    if request.module.__name__.rsplit(".", 1)[-1].startswith("test_legacy_chash_guard"):
        return
    def no_changelog(sql: str) -> str:  # the role may see every row; some cluster; no databasechangelog
        return "t" if "rolsuper" in sql else "1" if "system_identifier" in sql else "f"

    monkeypatch.setattr("nexus.daemon.legacy_chash_guard.bundled_run_sql", lambda *_a, **_k: no_changelog)
