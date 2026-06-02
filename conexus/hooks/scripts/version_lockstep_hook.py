#!/usr/bin/env python3
# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (c) 2026 Hal Hildebrand. All rights reserved.
"""RDR-143 SessionStart hook: plugin<->CLI version lockstep (Shape B).

The blocking, stdlib-only SessionStart entry point. It detects skew
between the installed plugin version (the marketplace surface) and the
last-confirmed CLI version (a per-user marker), and when they diverge it
(a) emits an additionalContext nudge so the in-session model knows an
upgrade is in flight, and (b) dispatches a DETACHED action that performs
the extras-preserving two-command upgrade. The hook returns immediately
and NEVER wedges synchronous SessionStart (CA-4), NEVER writes the marker
(the action owns that, on confirmed upgrade only), and NEVER raises
(fail-safe exit 0).

Stdlib-only: this runs under whichever bare interpreter
``_run_python_hook.sh`` resolves, which on a ``uv tool install conexus``
deployment cannot import the ``conexus`` package (same constraint as
``t2_prefix_scan.py`` / ``preflight.py``).
"""
from __future__ import annotations

import sys

if sys.version_info < (3, 12):
    sys.stderr.write(
        f"ERROR: conexus plugin hook requires Python 3.12+, got {sys.version.split()[0]}\n"
        f"  Resolved: {sys.executable}\n"
        f"  Install: brew install python@3.13 (macOS) | apt install python3.12 (Ubuntu) | uv python install 3.12\n"
    )
    sys.exit(1)

import json
import os
import subprocess
from pathlib import Path

DEBUG = os.environ.get("NX_HOOK_DEBUG", "0") == "1"

# Detached action script lives beside this hook; launched via the same
# interpreter-selection wrapper so it picks a >=3.12 python.
_SCRIPTS_DIR = Path(__file__).resolve().parent
_LAUNCHER = _SCRIPTS_DIR / "_run_python_hook.sh"
_ACTION = _SCRIPTS_DIR / "version_lockstep_action.py"


def debug(msg: str) -> None:
    """Print a debug line to stderr when NX_HOOK_DEBUG=1."""
    if DEBUG:
        print(f"[version-lockstep-hook] {msg}", file=sys.stderr)


def marker_path() -> Path:
    """Per-user marker recording the last CLI version confirmed in lockstep.

    Lives under ``~/.config/nexus/`` so it survives ``/plugin update``
    (CLAUDE_PLUGIN_ROOT is replaced wholesale on update). ``NX_LOCKSTEP_MARKER``
    overrides the location for tests.
    """
    override = os.environ.get("NX_LOCKSTEP_MARKER")
    if override:
        return Path(override)
    return Path.home() / ".config" / "nexus" / "cli_lockstep_marker"


def read_plugin_version() -> str | None:
    """Read the plugin version from ``${CLAUDE_PLUGIN_ROOT}/.claude-plugin/plugin.json``.

    Returns None on any failure (missing env, missing file, malformed JSON,
    absent ``version`` key). The hook must never fail.
    """
    root = os.environ.get("CLAUDE_PLUGIN_ROOT")
    if not root:
        debug("CLAUDE_PLUGIN_ROOT unset")
        return None
    try:
        data = json.loads((Path(root) / ".claude-plugin" / "plugin.json").read_text())
        version = data.get("version")
        return version if isinstance(version, str) and version else None
    except (OSError, ValueError) as exc:
        debug(f"could not read plugin.json: {exc}")
        return None


def read_marker() -> str | None:
    """Return the marker's recorded version, or None when absent/unreadable."""
    try:
        return marker_path().read_text().strip() or None
    except OSError:
        return None


def build_context(target_version: str) -> str:
    """Build the SessionStart additionalContext JSON nudge (CA-1 contract)."""
    msg = (
        f"The conexus CLI (nx) may be out of lockstep with plugin "
        f"v{target_version}. An extras-preserving upgrade has been dispatched "
        f"in the background; it takes effect on your next session. No action "
        f"needed now."
    )
    return json.dumps(
        {
            "hookSpecificOutput": {
                "hookEventName": "SessionStart",
                "additionalContext": msg,
            }
        }
    )


def dispatch_action(target_version: str) -> None:
    """Fire the detached upgrade action and return immediately (CA-4).

    Uses Popen with detached stdio so synchronous SessionStart is never
    blocked. We deliberately do not wait()/communicate().
    """
    cmd = ["bash", str(_LAUNCHER), str(_ACTION), target_version]
    try:
        subprocess.Popen(
            cmd,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            stdin=subprocess.DEVNULL,
            start_new_session=True,
        )
        debug(f"dispatched detached action for v{target_version}")
    except OSError as exc:
        debug(f"failed to dispatch action: {exc}")


def main() -> None:
    """Detect skew, nudge + dispatch on mismatch. Always fail-safe."""
    try:
        plugin_version = read_plugin_version()
        if not plugin_version:
            return  # nothing to compare against; stay silent

        if read_marker() == plugin_version:
            debug("marker matches plugin version; in lockstep")
            return

        # Mismatch (or missing marker): nudge the model and kick off the
        # detached upgrade. The hook never writes the marker.
        print(build_context(plugin_version))
        dispatch_action(plugin_version)
    except Exception as exc:  # noqa: BLE001 - hook must never raise
        debug(f"swallowed unexpected error: {exc}")


if __name__ == "__main__":
    main()
    sys.exit(0)
