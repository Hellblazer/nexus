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
