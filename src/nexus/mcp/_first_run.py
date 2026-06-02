# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (c) 2026 Hal Hildebrand. All rights reserved.
"""RDR-126 P2 (nexus-bsjro): first-run daemon install for MCP startup.

When ``nx-mcp`` / ``nx-mcp-catalog`` boots, ensure the host T2 daemon
unit (LaunchAgent on macOS, systemd user-unit on Linux) is installed.
Without this, a Claude-Desktop-only user who installs the .mcpb bundle
has nx-mcp running but no daemon for it to talk to — every MCP tool
that touches T2 fails opaquely.

Idempotency model (per RDR-126 §Approach §2):

- The OS unit (LaunchAgent / systemd file) is the source of truth.
  If it exists, skip install. We do NOT compare contents or
  overwrite — that's the realm of ``nx daemon t2 install --autostart
  --force`` which the user can invoke deliberately.
- We always call ``daemon t2 ensure-running`` so the current MCP
  session has a daemon to talk to even if install just happened or
  the previously-installed unit was stopped.

Side effects are silent on success; failures log a structured warning
and continue (the MCP server still starts; tool calls that need the
daemon will fail with their own error path).
"""
from __future__ import annotations

import os
import shutil
import subprocess
import sys
from pathlib import Path

import structlog

_log = structlog.get_logger(__name__)


def _macos_launchagent_path() -> Path:
    return Path.home() / "Library" / "LaunchAgents" / "com.nexus.t2.plist"


def _linux_systemd_unit_path() -> Path:
    xdg_config = Path(os.environ.get("XDG_CONFIG_HOME", str(Path.home() / ".config")))
    return xdg_config / "systemd" / "user" / "nexus-t2.service"


def _os_unit_exists() -> bool:
    """Return True if the T2 daemon's OS-level autostart unit is
    already installed on this host. macOS = LaunchAgent .plist,
    Linux = systemd user .service. Other platforms (Windows): always
    return True so the install step skips (no install support yet)."""
    platform = sys.platform
    if platform == "darwin":
        return _macos_launchagent_path().exists()
    if platform.startswith("linux"):
        return _linux_systemd_unit_path().exists()
    return True


def _find_nx_binary() -> str | None:
    """Locate the ``nx`` CLI binary the MCP server can shell out to.

    Strategy:
    1. ``shutil.which`` on the current PATH (typical CLI-spawned case).
    2. Sibling of ``sys.argv[0]`` (the ``nx-mcp`` entry point lives in
       the same bin dir as ``nx`` when conexus is installed via uv tool
       or pip).
    3. Standard uv-tool location (~/.local/bin/nx) as last-resort
       fallback for GUI-spawned subprocesses that may have a sparse PATH.
    """
    found = shutil.which("nx")
    if found:
        return found

    if sys.argv and sys.argv[0]:
        sibling = Path(sys.argv[0]).resolve().parent / "nx"
        if sibling.exists():
            return str(sibling)

    fallback = Path.home() / ".local" / "bin" / "nx"
    if fallback.exists():
        return str(fallback)

    return None


def ensure_installed_and_running() -> None:
    """Best-effort: install the T2 daemon autostart unit if missing,
    then ensure-running for this session. Never raises; logs warnings.

    Called from each MCP server's ``main()`` once at startup, after
    logging setup but before serving tools.
    """
    nx_bin = _find_nx_binary()
    if not nx_bin:
        _log.warning(
            "first_run_no_nx_binary",
            hint=(
                "nx CLI binary not found on PATH; cannot auto-install "
                "the T2 daemon. Install via 'uv tool install conexus' "
                "and ensure ~/.local/bin (or your uv tool bin dir) is "
                "on PATH for GUI-spawned subprocesses."
            ),
        )
        return

    if not _os_unit_exists():
        # OS unit missing — install it in-process via the lifted
        # ``nexus.daemon.installer`` (RDR-126 §2). Calling the library
        # function rather than shelling out to ``nx daemon t2 install``
        # gives us a structured InstallResult (status + dest path) that
        # the first-run banner (§3) consumes to pick its text variant
        # and surface the installed unit path.
        try:
            from nexus.daemon import installer

            result = installer.install_autostart()
            _log.info(
                "first_run_install_ok",
                status=result.status.value,
                dest=str(result.dest),
                warnings=list(result.warnings),
            )
        except Exception as exc:
            # installer raises typed InstallerError on symlink / content
            # diff / activation failure; all are best-effort here — the
            # session still works via ensure-running below.
            _log.warning(
                "first_run_install_failed",
                error=f"{type(exc).__name__}: {exc}",
                hint=(
                    "T2 daemon autostart install failed; current MCP "
                    "session will work via ensure-running but the "
                    "daemon will not survive reboots. Re-run "
                    "'nx daemon t2 install --autostart' manually."
                ),
            )

    # Always ensure-running so the current session has a daemon.
    try:
        subprocess.run(
            [nx_bin, "daemon", "t2", "ensure-running", "--quiet"],
            capture_output=True, text=True, timeout=15, check=False,
        )
    except Exception as exc:
        _log.warning(
            "first_run_ensure_running_exception",
            error=f"{type(exc).__name__}: {exc}",
        )


# ── RDR-144 P5b: user-visible embedder notice ─────────────────────────────────


def embedder_startup_notice() -> str | None:
    """Return a one-line notice for a local-mode user whose active embedder is
    not what they (would) want, or ``None`` when nothing needs saying.

    Plugin / Claude-Desktop / Cowork-first users never run the Claude Code
    SessionStart hook, so the embedder advisory that ``nx doctor`` shows (P5a)
    never reaches them. The MCP server is their only channel, and it cannot
    print (stdout is JSON-RPC). The notice is delivered via the server
    ``instructions`` string instead (see :func:`apply_embedder_notice`).

    Reuses the single source of truth for the two states
    (:func:`nexus.health.local_embedder_advisory`): State 1 (default 384, no
    ``nx init`` choice) and State 2 (chose bge-768 but the ``[local]`` extra
    is missing, so the resolver silently fell back to 384). Cloud mode and a
    correctly-active bge return ``None``.
    """
    from nexus.config import is_local_mode, local_embed_model_choice

    if not is_local_mode():
        return None

    from nexus.db.local_ef import _resolve_local_model
    from nexus.health import local_embedder_advisory

    active = _resolve_local_model(warn=False)
    advisory = local_embedder_advisory(local_embed_model_choice(), active)
    if advisory is None:
        return None

    fix = advisory.fix_suggestions[0] if advisory.fix_suggestions else "run `nx init`"
    # Collapse to a single line — server instructions should stay compact.
    return f"nexus embedder: {advisory.detail}. {fix}".replace("\n", " ")


def apply_embedder_notice(server: object) -> bool:
    """Write the embedder notice (if any) into ``server``'s low-level MCP
    ``instructions`` so it reaches the client at ``initialize``.

    ``FastMCP.instructions`` is a read-only property; the writable surface is
    the low-level ``server._mcp_server.instructions`` (P5b spike). An existing
    instructions string is preserved (notice appended), never clobbered.

    Best-effort: a startup advisory must never break MCP boot, so any failure
    is logged at debug and returns ``False``.
    """
    try:
        notice = embedder_startup_notice()
        if notice is None:
            return False
        low = server._mcp_server  # type: ignore[attr-defined]
        existing = getattr(low, "instructions", None)
        low.instructions = f"{existing}\n\n{notice}" if existing else notice
        return True
    except Exception as exc:  # noqa: BLE001 — never block startup on an advisory
        _log.debug("embedder_notice_apply_failed", error=f"{type(exc).__name__}: {exc}")
        return False
