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
