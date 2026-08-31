#!/usr/bin/env python3
"""Resolve-with-retry bootstrap for the Claude Desktop .mcpb bundle (nexus-r433b).

Claude Desktop launches this file via ``uv run --no-project`` (see
manifest.json's mcp_config). The bundle's real dependency resolution — the
step that pulls ``conexus[local]>=X.Y.Z`` from PyPI — used to happen inside
the ``uv run src/server.py`` invocation itself, which meant a resolver
failure killed the extension before any of our code ran. PyPI's simple
index lags the upload by ~10-25 minutes after every release (measured on
four consecutive releases), so a Desktop install or update inside that
window died with a bare "no matching version" resolver error.

This bootstrap runs ``uv sync`` explicitly, retries the
propagation-window failure class with bounded backoff (naming the cause on
stderr each time), and then execs ``uv run src/server.py`` — the real MCP
entry point — so stdio passes straight through to Claude Desktop. Any
``uv sync`` failure OUTSIDE that class (network down, permissions, a
genuinely missing package) fails immediately with uv's own output:
behavior unchanged from before this file existed.

Set ``NX_MCPB_SKIP_RESOLVE_RETRY=1`` to skip the sync-with-retry and exec
the server directly (the pre-r433b behavior).

Deliberately conservative syntax: under ``--no-project`` uv runs this on
whatever Python it discovers, which need not satisfy the bundle's own
``requires-python`` (that constraint governs the project venv ``uv sync``
creates, not this file).
"""
from __future__ import annotations

import os
import subprocess
import sys
import time

# Backoff spans ~15 min after the first failure — sized against the measured
# 10-25 min propagation window (the user typically lands mid-window, so the
# remaining lag is shorter than the full window).
_RETRY_SLEEPS = (60, 120, 240, 480)

_PROPAGATION_MSG = (
    "[conexus-mcpb] PyPI has not finished propagating the pinned conexus "
    "version to its download index yet (this lags a new release by ~10-25 "
    "minutes)."
)


def _bundle_dir() -> str:
    """The mcpb/ bundle root: parent of the src/ directory holding this file."""
    return os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def _is_resolution_unavailable(output: str) -> bool:
    """True when uv's failure output is the version-not-yet-served resolver
    class (the PyPI propagation window), as opposed to any other failure.

    Deliberately broad within that class: a genuine (non-propagation)
    resolver conflict that mentions conexus also matches and rides the
    ~15-min retry schedule before surfacing — bounded, and preferred over
    a narrower match that misses a real propagation shape and kills the
    extension with a bare resolver error. Kept in parity with the shell
    grep in tests/e2e/fresh-install-mvv.sh's retry branch (pinned by
    test_retry_signature_parity_with_mcpb_bootstrap).
    """
    low = output.lower()
    if "conexus" not in low:
        return False
    return (
        "no solution found" in low
        or "no version of conexus" in low
        or "not found in the package registry" in low
    )


def _sync_with_retry(bundle_dir, run=subprocess.run, sleep=time.sleep, sleeps=_RETRY_SLEEPS):
    """``uv sync`` the bundle env, retrying only the propagation-window
    failure class. Raises SystemExit on terminal failure."""
    attempts = len(sleeps) + 1
    output = ""
    for i in range(attempts):
        proc = run(
            ["uv", "sync", "--directory", bundle_dir],
            capture_output=True,
            text=True,
        )
        if proc.returncode == 0:
            return
        output = (proc.stdout or "") + (proc.stderr or "")
        if not _is_resolution_unavailable(output):
            sys.stderr.write(output)
            raise SystemExit(proc.returncode or 1)
        if i == attempts - 1:
            break
        wait = sleeps[i]
        print(
            "%s Retrying in %ds (attempt %d/%d)." % (_PROPAGATION_MSG, wait, i + 1, len(sleeps)),
            file=sys.stderr,
            flush=True,
        )
        sleep(wait)
    print(
        "%s All retries exhausted — try again in a few minutes." % _PROPAGATION_MSG,
        file=sys.stderr,
        flush=True,
    )
    sys.stderr.write(output)
    raise SystemExit(1)


def main() -> None:
    bundle_dir = _bundle_dir()
    if not os.environ.get("NX_MCPB_SKIP_RESOLVE_RETRY"):
        _sync_with_retry(bundle_dir)
    # exec, not subprocess: Claude Desktop's stdio pipes must land on the
    # server process itself for the MCP handshake.
    os.execvp("uv", ["uv", "run", "--directory", bundle_dir, "src/server.py"])


if __name__ == "__main__":
    main()
