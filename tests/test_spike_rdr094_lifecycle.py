# SPDX-License-Identifier: AGPL-3.0-or-later
"""Deterministic tests for the RDR-094 Spike A lifecycle harness.

The harness itself spawns real subprocesses, so end-to-end exercise is
out of scope for unit tests (10-run evidence is collected by Hal on
demand and appended to T2 ``nexus_rdr/094-spike-a-lifecycle``). What we
*can* pin here are the pure-python pieces that classify cleanup paths
and the phase dispatch wiring -- the parts that decide whether a real
run counts as ``mcp_owned_lifespan`` vs ``watchdog_mcp`` vs
``watchdog_unknown`` for the new fifth-mode probe (nexus-sawb).
"""
from __future__ import annotations

import importlib.util
import sys
from pathlib import Path

import pytest

_SPIKE_PATH = (
    Path(__file__).resolve().parent.parent
    / "scripts"
    / "spikes"
    / "spike_rdr094_lifecycle.py"
)


@pytest.fixture(scope="module")
def harness():
    spec = importlib.util.spec_from_file_location("spike_rdr094_a", _SPIKE_PATH)
    assert spec is not None and spec.loader is not None
    mod = importlib.util.module_from_spec(spec)
    sys.modules["spike_rdr094_a"] = mod
    spec.loader.exec_module(mod)
    return mod


# ── _classify_cleanup_path: stdin_close branch (Phase 5 / nexus-sawb) ────────


class TestClassifyStdinClose:
    """The fifth-mode probe needs three discriminations:

    * ``mcp_owned_lifespan`` -- stdin EOF cleanly drove the lifespan
      finally; the wild silent-death observation has another root cause.
    * ``watchdog_mcp`` -- nx-mcp crashed, watchdog cleaned chroma; the
      crash event signature confirms FastMCP's reader raised on EOF.
    * ``watchdog_unknown`` -- chroma is dead but no mcp lifecycle event
      ever fired. This is the fifth-mode silent-death signature.
    """

    def test_clean_exit_event_classifies_as_lifespan(self, harness):
        log_lines = [
            "2026-04-25 17:31:00 nx INFO event='mcp_server_stopping' "
            "reason='exit' pid=42",
        ]
        path = harness._classify_cleanup_path(
            log_lines, chroma_alive=False, signal_sent="stdin_close",
        )
        assert path == "mcp_owned_lifespan"

    def test_crash_event_classifies_as_watchdog_mcp(self, harness):
        log_lines = [
            "2026-04-25 17:31:00 nx ERROR event='mcp_server_crashed' pid=42",
        ]
        path = harness._classify_cleanup_path(
            log_lines, chroma_alive=False, signal_sent="stdin_close",
        )
        assert path == "watchdog_mcp"

    def test_no_event_with_chroma_dead_is_silent_fifth_mode(self, harness):
        """The fifth-mode signature: chroma dead, no mcp lifecycle event."""
        path = harness._classify_cleanup_path(
            [], chroma_alive=False, signal_sent="stdin_close",
        )
        assert path == "watchdog_unknown"

    def test_chroma_alive_means_none_path(self, harness):
        """Regression sentinel: chroma still alive => cleanup didn't run."""
        path = harness._classify_cleanup_path(
            [], chroma_alive=True, signal_sent="stdin_close",
        )
        assert path == "none"

    def test_signal_stop_event_is_impossible_for_stdin_close(self, harness):
        """No signal was delivered, so any 'reason=signal' event in the
        log must be from a prior run that didn't get cleaned up. Our
        branch does not consider it; saw_clean_exit / saw_crash drive
        the verdict."""
        log_lines = [
            "old: event='mcp_server_stopping' reason='signal' pid=999",
            "event='mcp_server_stopping' reason='exit' pid=42",
        ]
        # Clean-exit event in the same window classifies as lifespan
        # despite the stale signal-stop entry.
        path = harness._classify_cleanup_path(
            log_lines, chroma_alive=False, signal_sent="stdin_close",
        )
        assert path == "mcp_owned_lifespan"


# ── Existing classify branches: regression sentinel ──────────────────────────


class TestClassifyExistingBranches:
    """Pin the classify_cleanup_path behaviour for the four pre-existing
    phases so the new branch addition does not perturb them."""

    def test_sigterm_with_signal_stop_event(self, harness):
        log_lines = [
            "event='mcp_server_stopping' reason='signal' pid=42",
        ]
        path = harness._classify_cleanup_path(
            log_lines, chroma_alive=False, signal_sent="SIGTERM",
        )
        assert path == "mcp_owned_signal"

    def test_sigkill_classifies_as_watchdog_mcp(self, harness):
        path = harness._classify_cleanup_path(
            [], chroma_alive=False, signal_sent="SIGKILL",
        )
        assert path == "watchdog_mcp"

    def test_harness_sigkill_classifies_as_watchdog_claude(self, harness):
        path = harness._classify_cleanup_path(
            [], chroma_alive=False, signal_sent="harness_SIGKILL",
        )
        assert path == "watchdog_claude"


# ── Phase dispatcher wiring ──────────────────────────────────────────────────


class TestPhaseRegistration:
    """Verify the new phase is wired into both the default --phases list
    and the main() dispatcher branch."""

    def test_stdio_pipe_break_in_default_phase_list(self, harness):
        """Default --phases must include stdio_pipe_break so a no-arg
        invocation runs the fifth-mode probe alongside the other four."""
        source = _SPIKE_PATH.read_text()
        assert 'default="clean,mcp_sigkill,mcp_oom,claude_crash,stdio_pipe_break"' in source

    def test_run_phase_function_exists(self, harness):
        assert hasattr(harness, "_run_phase_stdio_pipe_break")
        assert callable(harness._run_phase_stdio_pipe_break)

    def test_dispatcher_routes_phase_name(self, harness):
        """main()'s dispatcher must have a branch for stdio_pipe_break."""
        source = _SPIKE_PATH.read_text()
        assert 'phase == "stdio_pipe_break"' in source
        assert "_run_phase_stdio_pipe_break(run_id)" in source
