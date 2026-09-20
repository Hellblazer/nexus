# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (c) 2026 Hal Hildebrand. All rights reserved.
"""The ported StopFailure observer (RDR-215 bead nexus-q02nx.21).

Driven against BOTH the plugin script and the port, so every assertion is
a differential against what production still runs. Drop the "bash" param
when ``conexus/hooks/scripts/stop_failure_hook.py`` goes.

**Why this file exists at all, given the script already has eleven tests.**
Every one of those asserts ``returncode == 0`` and nothing else. A script
whose entire body was replaced by ``sys.exit(0)`` passes all eleven. That
is fine as a crash guard and it is not evidence the hook does its job, so
the differential here is carried on the hook's ONE observable: the debug
trace under ``NX_HOOK_DEBUG=1``. Without that, a port that silently
dropped the type normalisation or the CLAUDECODE guard would be green.
"""
from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path

import pytest

SCRIPT = (
    Path(__file__).resolve().parents[2]
    / "conexus" / "hooks" / "scripts" / "stop_failure_hook.py"
)

FAILURE_TYPES = [
    "rate_limit",
    "authentication_failed",
    "billing_error",
    "invalid_request",
    "server_error",
    "max_output_tokens",
    "unknown",
]

_PY_DRIVER = """
import json, sys
from nexus._hook_runtime._io import never_fail
from nexus.hooks import stop_failure as _hook

raw = sys.stdin.read()
try:
    payload = json.loads(raw) if raw.strip() else None
except Exception:
    payload = None
if not isinstance(payload, dict):
    payload = None
result = never_fail(lambda: _hook.run(payload), "stop_failure")
if result.stdout is not None:
    sys.stdout.write(result.stdout + "\\n")
sys.exit(result.exit_code)
"""

_IMPL = "bash"


@pytest.fixture(params=["bash", "python"], autouse=True)
def impl(request):
    """Run every assertion against BOTH implementations."""
    global _IMPL
    _IMPL = request.param
    yield request.param
    _IMPL = "bash"


def _run_hook(stdin: str, *, env_overrides: dict[str, str] | None = None):
    env = {
        **os.environ,
        "PATH": os.environ.get("PATH", ""),
        "CLAUDECODE": "",
        **(env_overrides or {}),
    }
    argv = (
        [sys.executable, "-c", _PY_DRIVER]
        if _IMPL == "python"
        else [sys.executable, str(SCRIPT)]
    )
    return subprocess.run(
        argv, input=stdin, capture_output=True, text=True, timeout=15, env=env
    )


def _payload(error: str, details: str | None = "test details") -> str:
    return json.dumps({
        "session_id": "test-session",
        "hook_event_name": "StopFailure",
        "error": error,
        "error_details": details,
    })


class TestItNeverFails:
    """The script's original eleven, carried across unchanged in intent."""

    @pytest.mark.parametrize("error_type", FAILURE_TYPES)
    def test_exit_zero_for_every_failure_type(self, error_type):
        assert _run_hook(_payload(error_type)).returncode == 0

    @pytest.mark.parametrize(
        "stdin",
        [
            pytest.param("not valid json {{{", id="malformed"),
            pytest.param("", id="empty"),
            pytest.param("   \n  ", id="whitespace"),
            pytest.param("[]", id="a list, not an object"),
            pytest.param('{"session_id": "s"}', id="no error field"),
        ],
    )
    def test_exit_zero_on_any_stdin(self, stdin):
        assert _run_hook(stdin).returncode == 0

    def test_null_error_details_does_not_crash_the_slice(self):
        """``None[:200]`` would raise. The script coerces with ``or ''``."""
        assert _run_hook(_payload("rate_limit", None)).returncode == 0

    def test_graceful_without_bd_on_path(self):
        assert _run_hook(
            _payload("rate_limit"), env_overrides={"PATH": "/usr/bin:/bin"}
        ).returncode == 0


class TestItSaysNothingOnStdout:
    """StopFailure's output is ignored by Claude Code, which is exactly why
    this hook is safe on the tool tier — but a byte on stdout would still
    be a change, and on the tool tier it would become the tool's result."""

    @pytest.mark.parametrize("error_type", FAILURE_TYPES)
    def test_stdout_is_empty(self, error_type):
        assert _run_hook(_payload(error_type)).stdout == ""

    def test_stdout_is_empty_even_under_debug(self):
        """The trace goes to stderr. A debug line on stdout would be the
        classic hook bug: diagnostics landing in the decision channel."""
        proc = _run_hook(
            _payload("rate_limit"), env_overrides={"NX_HOOK_DEBUG": "1"}
        )
        assert proc.stdout == ""
        assert proc.stderr.strip(), "nothing traced under NX_HOOK_DEBUG=1"


class TestTheDebugTrace:
    """The hook's only observable, and so the only place a differential
    can discriminate a working port from ``sys.exit(0)``."""

    def test_silent_without_the_env_var(self):
        assert _run_hook(_payload("rate_limit")).stderr == ""

    @pytest.mark.parametrize("error_type", FAILURE_TYPES)
    def test_a_known_type_is_not_normalised(self, error_type):
        proc = _run_hook(
            _payload(error_type), env_overrides={"NX_HOOK_DEBUG": "1"}
        )
        assert "unknown error type" not in proc.stderr, (
            f"{error_type} is in KNOWN_TYPES but was normalised away"
        )

    def test_an_unknown_type_is_normalised_and_says_so(self):
        proc = _run_hook(
            _payload("some_future_error_type"),
            env_overrides={"NX_HOOK_DEBUG": "1"},
        )
        assert "unknown error type: some_future_error_type" in proc.stderr

    def test_empty_stdin_traces_that_it_was_empty(self):
        proc = _run_hook("", env_overrides={"NX_HOOK_DEBUG": "1"})
        assert "empty stdin" in proc.stderr

    def test_without_claudecode_it_stops_before_the_summary(self):
        """The CLAUDECODE guard, carried verbatim including its oddity: it
        is a test-shaped condition in production code guarding side
        effects that no longer exist. Pinned so that removing it is a
        visible decision rather than tidying."""
        proc = _run_hook(
            _payload("rate_limit"), env_overrides={"NX_HOOK_DEBUG": "1"}
        )
        assert "skipping side effects" in proc.stderr
        assert "observed:" not in proc.stderr

    def test_with_claudecode_it_reaches_the_summary(self):
        proc = _run_hook(
            _payload("rate_limit", "429 upstream"),
            env_overrides={"NX_HOOK_DEBUG": "1", "CLAUDECODE": "1"},
        )
        assert "observed: stop-failure-rate_limit" in proc.stderr
        assert "429 upstream" in proc.stderr

    def test_the_details_are_truncated_at_200_chars(self):
        proc = _run_hook(
            _payload("rate_limit", "x" * 500),
            env_overrides={"NX_HOOK_DEBUG": "1", "CLAUDECODE": "1"},
        )
        runs = max(
            (len(part) for part in proc.stderr.split("x" * 10)), default=0
        )
        assert "x" * 200 in proc.stderr
        assert "x" * 201 not in proc.stderr, (
            f"error_details was not truncated at 200 (longest run context {runs})"
        )


class TestNoSideEffects:
    """nexus-0dj7e: this hook used to file beads and `bd remember` per
    event, which polluted `bd ready` and injected a permanent key into
    every session through `bd prime`. It must never call out again."""

    def test_it_spawns_nothing(self, tmp_path):
        """A PATH holding only a recording shim for bd, nx and git. Any
        invocation leaves a file."""
        shim_dir = tmp_path / "bin"
        shim_dir.mkdir()
        log = tmp_path / "calls.log"
        for name in ("bd", "nx", "git", "claude"):
            p = shim_dir / name
            p.write_text(f'#!/bin/sh\necho "{name} $*" >> {log}\nexit 0\n')
            p.chmod(0o755)
        proc = _run_hook(
            _payload("rate_limit"),
            env_overrides={
                "PATH": f"{shim_dir}:{os.environ.get('PATH', '')}",
                "CLAUDECODE": "1",
                "NX_HOOK_DEBUG": "1",
            },
        )
        assert proc.returncode == 0
        assert not log.exists(), (
            f"the observer invoked something: {log.read_text()}"
        )
