# SPDX-License-Identifier: AGPL-3.0-or-later
"""Pin the interpreter the plugin hook scripts resolve to, for every test here.

Several hook scripts call ``_interpreter.reexec_if_needed()`` at import time and
``os.execv`` into whatever ``resolve()`` picks. The dev-venv candidate matches an
active venv's ``nexus`` against ``CLAUDE_PROJECT_DIR`` (finding C, nexus-f9bgu.36;
it was the process cwd, which uv now sets to the plugin root), and a test run is
not a Claude Code hook, so the variable is unset and the match is None. The next
candidate is the installed conexus generation's python, and a test that merely
loads a script would exec the pytest process into it and end the run mid-file.

(Since nexus-sby49.2 ``reexec_if_needed`` acts only when its caller is the process's main script, so a test that
imports a script no longer execs; this pin still keeps ``resolve()`` quiet for the tests that run one as a script.)

``NX_HOOK_PYTHON`` is the first candidate and is the interpreter already running,
so ``resolve()`` returns it and the re-exec is a no-op, as tests/test_routing_*
already arrange for the scripts they spawn. Tests that exercise ``resolve()``
itself clear it with ``monkeypatch``.
"""
from __future__ import annotations

import os
import sys

import pytest

os.environ.setdefault("NX_HOOK_PYTHON", sys.executable)


@pytest.fixture(autouse=True)
def _no_live_t2_probe_at_session_start(monkeypatch):
    """``session_context.run`` probes T2 for its fire-only reachability line. A test that runs the hook has no engine, so the
    probe would print ``WARNING: T2 memory unreachable`` into every output a test compares. Tests of the probe line itself
    undo this (``monkeypatch.undo()``) or patch ``t2_prefix_scan``."""
    from nexus.hooks import session_context  # noqa: PLC0415 — deferred: a conftest import would load nexus.hooks for every collection

    monkeypatch.setattr(session_context, "_t2_reachability_lines", lambda project: [])
