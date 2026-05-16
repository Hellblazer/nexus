# SPDX-License-Identifier: AGPL-3.0-or-later
"""RDR-112 first-run autostart nudge (nexus-mf91).

The T2 daemon's autostart unit (launchd plist on macOS, systemd user
unit on Linux) ships under ``nx daemon t2 install --autostart``. There
is no post-install hook (pip wheels don't run them), no plugin-side
SessionStart prompt, and no doctor auto-remediation. First-time users
silently get direct-mode SQLite opens with no cross-process state
sharing, and bridge writes during the manual-start window contend with
the daemon when one is eventually started.

This module is the human-visible nudge layer. On a TTY, when:

  - the autostart unit is not installed,
  - the operator has not opted out (``NX_STORAGE_MODE=direct`` or the
    ``no_autostart_nudge`` marker file),
  - this is not a CI / subagent / non-interactive context,

``maybe_emit_autostart_prompt()`` prints a single actionable warning to
stderr naming ``nx daemon t2 install --autostart`` and writes the
marker file so subsequent invocations stay quiet. The same diagnostic
is also available on demand via ``nx doctor --check-autostart`` for
ongoing visibility.

Auto-install with consent prompt was considered (bead Option A) and
declined: modifying ``~/Library/LaunchAgents`` or
``~/.config/systemd/user`` from an arbitrary CLI invocation is too
invasive. The strongly-worded warning + explicit command-name route
respects operator agency.
"""
from __future__ import annotations

import os
import sys
from pathlib import Path

import click

# Module-scoped sentinel — fire the prompt at most once per process.
_PROMPTED: bool = False

# Operator escape hatches (any non-empty value disables; "0"/"false" do
# not). NEXUS_NO_PROMPTS matches the existing bootstrap prompt.
_NO_PROMPTS_ENV = "NEXUS_NO_PROMPTS"
_NUDGE_MARKER_NAME = "no_autostart_nudge"


def _stderr_is_tty() -> bool:
    """Wrapper that swallows AttributeError/ValueError from edge cases.

    Factored out of ``maybe_emit_autostart_prompt`` so tests can mock
    the TTY check cleanly without wrestling with pytest's ``capsys``
    redirection of ``sys.stderr``.
    """
    try:
        return bool(sys.stderr.isatty())
    except (AttributeError, ValueError):
        return False


def _is_supported_platform() -> bool:
    return sys.platform == "darwin" or sys.platform.startswith("linux")


def _autostart_unit_path() -> Path | None:
    """Return the per-OS autostart unit path, or ``None`` on unsupported OS."""
    if sys.platform == "darwin":
        return Path.home() / "Library" / "LaunchAgents" / "com.nexus.t2.plist"
    if sys.platform.startswith("linux"):
        return (
            Path.home()
            / ".config"
            / "systemd"
            / "user"
            / "nexus-t2.service"
        )
    return None


def _is_autostart_installed() -> bool:
    """True if the per-OS autostart unit file is present on disk."""
    path = _autostart_unit_path()
    return path is not None and path.exists()


def _nudge_marker_path() -> Path:
    """Marker that records 'operator has been nudged once'."""
    from nexus.config import nexus_config_dir

    return nexus_config_dir() / _NUDGE_MARKER_NAME


def _looks_like_subagent() -> bool:
    """Heuristic: a subagent / MCP-spawned ``nx`` invocation.

    Per the mf91 enrichment, prompting from a Claude Code subagent's
    tool call is noise — the operator is not at the terminal even
    though stderr may technically be a TTY. ``CLAUDECODE`` is set by
    Claude Code's harness; ``NX_T1_HOST`` is set when a parent process
    has already addressed a per-session T1 chroma server (subagents
    inherit it).
    """
    if os.environ.get("CLAUDECODE", "").strip():
        return True
    if os.environ.get("NX_T1_HOST", "").strip():
        return True
    return False


def _looks_like_ci() -> bool:
    """Standard CI-environment heuristic.

    ``CI=true`` is set by GitHub Actions, CircleCI, GitLab, Travis,
    Buildkite, and most other runners. We accept any non-empty
    ``CI`` value as a CI signal.
    """
    return bool(os.environ.get("CI", "").strip())


def autostart_status() -> dict[str, object]:
    """Structured status of the autostart subsystem.

    Used by ``nx doctor --check-autostart`` to render the same
    information the prompt surfaces, on demand. Safe to call from any
    context (no TTY / env / marker gating).
    """
    path = _autostart_unit_path()
    return {
        "platform_supported": _is_supported_platform(),
        "unit_path": str(path) if path is not None else None,
        "installed": _is_autostart_installed(),
        "marker_present": _nudge_marker_path().exists()
        if _is_supported_platform()
        else False,
        "storage_mode": os.environ.get("NX_STORAGE_MODE", "").strip().lower()
        or None,
    }


def maybe_emit_autostart_prompt() -> None:
    """Emit the autostart nudge to stderr if every gate clears.

    Best-effort: any exception during the gate logic suppresses the
    prompt rather than disrupting the CLI invocation. The prompt is
    advisory; the underlying state is available through
    ``nx doctor --check-autostart``.

    Gate order is cheap-to-expensive: the in-process ``_PROMPTED``
    sentinel runs first, then env-only checks, then the
    filesystem-touching ones.
    """
    global _PROMPTED
    if _PROMPTED:
        return

    if not _is_supported_platform():
        return

    # TTY gate: stderr must be a TTY (operator at a real terminal).
    if not _stderr_is_tty():
        return

    # NEXUS_NO_PROMPTS escape hatch (shared with the bootstrap prompt).
    raw = os.environ.get(_NO_PROMPTS_ENV, "").strip().lower()
    if raw in ("1", "true", "yes", "on"):
        return

    # NX_STORAGE_MODE=direct is the documented opt-out: the operator
    # explicitly chose direct mode and does not want a daemon at all.
    storage_mode = os.environ.get("NX_STORAGE_MODE", "").strip().lower()
    if storage_mode == "direct":
        return

    # CI / subagent contexts: humans are not watching stderr.
    if _looks_like_ci() or _looks_like_subagent():
        return

    # Filesystem gates last. Marker present means we already nudged on
    # this machine; autostart installed means there is nothing to nudge.
    try:
        marker = _nudge_marker_path()
        if marker.exists():
            return
        if _is_autostart_installed():
            return
    except OSError:
        return

    _PROMPTED = True
    click.echo(
        "WARNING: T2 daemon autostart not installed.\n"
        "  Cross-process state sharing (cockpit panels, bindings, daemon-\n"
        "  routed tuplespace) is degraded to direct-mode SQLite opens.\n"
        "  Run `nx daemon t2 install --autostart` to install the launchd\n"
        "  (macOS) or systemd user (Linux) unit so the daemon comes up at\n"
        "  login. Suppress this nudge with NEXUS_NO_PROMPTS=1, or set\n"
        "  NX_STORAGE_MODE=direct to opt out of the daemon entirely.",
        err=True,
    )

    # Best-effort marker write so we only nudge once per machine. A
    # write failure (read-only home, full disk) is logged and ignored;
    # the worst-case repeat is one more nudge next invocation.
    try:
        marker.parent.mkdir(parents=True, exist_ok=True)
        marker.write_text("nudged\n")
    except OSError:
        pass
