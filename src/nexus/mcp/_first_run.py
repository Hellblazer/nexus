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
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING, Any

import structlog

if TYPE_CHECKING:
    from nexus.daemon.installer import InstallStatus

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


def _installed_unit_path() -> Path | None:
    """Path of the installed T2 autostart unit for this platform, or
    ``None`` on an unsupported platform. Used to surface the unit
    location in the first-run banner's "already configured" variant."""
    platform = sys.platform
    if platform == "darwin":
        return _macos_launchagent_path()
    if platform.startswith("linux"):
        return _linux_systemd_unit_path()
    return None


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

    from nexus.daemon import installer

    status: InstallStatus
    dest: Path | None = None
    if not _os_unit_exists():
        # OS unit missing — install it in-process via the lifted
        # ``nexus.daemon.installer`` (RDR-126 §2). Calling the library
        # function rather than shelling out to ``nx daemon t2 install``
        # gives us a structured InstallResult (status + dest path) that
        # the first-run banner (§3) consumes to pick its text variant
        # and surface the installed unit path.
        try:
            result = installer.install_autostart()
            status = result.status
            dest = result.dest
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
            status = installer.InstallStatus.FAILED
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
    else:
        # OS unit already present — pre-installed user. Surface the
        # "already configured" banner variant.
        status = installer.InstallStatus.ALREADY_PRESENT
        dest = _installed_unit_path()

    # RDR-126 §3: queue the one-shot first-run banner (best-effort; never
    # blocks startup). Delivered on the first tool response by the
    # dispatch path (see deliver_pending_banner).
    try:
        spec = maybe_banner(status, dest)
        if spec is not None:
            queue_banner(spec)
    except Exception as exc:  # noqa: BLE001 — banner must never break boot
        _log.debug("first_run_banner_queue_failed", error=f"{type(exc).__name__}: {exc}")

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


# ── RDR-126 §3: first-run banner ──────────────────────────────────────────────


@dataclass(frozen=True)
class BannerSpec:
    """A one-shot first-run banner queued for the first tool response."""

    text: str


# Module-level pending banner. Single MCP process == single first-run, so a
# module global is the natural home. Delivered (and cleared) on the first tool
# response by ``deliver_pending_banner``.
_PENDING_BANNER: BannerSpec | None = None

_UNINSTALL_HINT = (
    "To remove the background daemon later, ask me to run the "
    "`daemon_uninstall` tool (it will confirm before removing anything)."
)


def _first_run_marker_path() -> Path:
    """Location of the one-shot first-run marker. Lives under
    ``nexus_config_dir()`` so it honours ``NEXUS_CONFIG_DIR`` for tests
    and multi-profile installs."""
    from nexus.config import nexus_config_dir

    return nexus_config_dir() / ".mcp_first_run_complete"


def maybe_banner(status: InstallStatus, dest: Path | None) -> BannerSpec | None:
    """Build the first-run banner for ``status``, or ``None`` when there is
    nothing to say (marker already written, or the install FAILED so there
    is no daemon to announce).

    Two variants (RDR-126 §3): NEWLY_INSTALLED -> "installed at <path>";
    ALREADY_PRESENT -> "already configured at <path>". Both carry the
    in-chat uninstall instruction.
    """
    from nexus.daemon.installer import InstallStatus

    if _first_run_marker_path().exists():
        return None

    where = f" at `{dest}`" if dest is not None else ""
    if status is InstallStatus.NEWLY_INSTALLED:
        body = (
            f"nexus: background knowledge daemon installed{where} and started. "
            "It runs at login so your notes and search index stay available."
        )
    elif status is InstallStatus.ALREADY_PRESENT:
        body = (
            f"nexus: background knowledge daemon already configured{where}. "
            "It runs at login so your notes and search index stay available."
        )
    else:  # FAILED or anything else — nothing to announce.
        return None

    return BannerSpec(text=f"{body} {_UNINSTALL_HINT}")


def mark_shown() -> None:
    """Write the first-run marker after the banner has been delivered on a
    channel. Idempotent; creates parent directories as needed."""
    marker = _first_run_marker_path()
    marker.parent.mkdir(parents=True, exist_ok=True)
    marker.touch(exist_ok=True)


def queue_banner(spec: BannerSpec) -> None:
    """Queue ``spec`` for delivery on the first tool response."""
    global _PENDING_BANNER
    _PENDING_BANNER = spec


def _clear_pending_banner() -> None:
    """Drop any pending banner without marking it shown (test helper / reset)."""
    global _PENDING_BANNER
    _PENDING_BANNER = None


def deliver_pending_banner(content_blocks: list[Any]) -> bool:
    """Prepend any pending first-run banner to ``content_blocks[0].text``.

    Returns ``True`` when the banner was delivered (and the marker written),
    ``False`` when there was nothing pending or delivery failed.

    Marker discipline (RDR-126 §3, load-bearing): the marker is written
    ONLY on a successful prepend. On a malformed result (empty list, a
    first block with no ``text`` attribute) the banner stays pending and
    retries on the next tool call — a failed delivery never burns the
    one-shot.
    """
    global _PENDING_BANNER
    spec = _PENDING_BANNER
    if spec is None:
        return False

    try:
        first = content_blocks[0]
        existing = first.text  # AttributeError if not a text block
        if not isinstance(existing, str):
            raise TypeError("content block .text is not a str")
        first.text = f"{spec.text}\n\n{existing}"
    except Exception as exc:  # noqa: BLE001 — best-effort; retry next call
        _log.debug(
            "first_run_banner_delivery_failed",
            error=f"{type(exc).__name__}: {exc}",
        )
        return False

    # Delivered — write the marker and clear the queue.
    try:
        mark_shown()
    except Exception as exc:  # noqa: BLE001 — marker write must not break the tool result
        _log.debug("first_run_marker_write_failed", error=f"{type(exc).__name__}: {exc}")
    _PENDING_BANNER = None
    return True


def install_banner_dispatch_hook(server: object) -> bool:
    """Wrap the MCP server's CallToolRequest handler so the first tool
    response carries the pending first-run banner (RDR-126 §3).

    The wrapper calls :func:`deliver_pending_banner` on the result's
    content blocks; once delivered (marker written) it is a no-op for the
    rest of the process's life. Best-effort: any failure to install the
    hook is logged at debug and returns ``False`` so MCP boot is never
    blocked. The notification channel is intentionally not used here
    (RDR-126 A2: ``notifications/message`` rendering is unverified; the
    tool-response content prepend is the primary, load-bearing channel).
    """
    try:
        from mcp import types  # type: ignore[import-not-found]

        low = server._mcp_server  # type: ignore[attr-defined]
        key = types.CallToolRequest
        original = low.request_handlers.get(key)
        if original is None:
            return False

        async def _wrapped(req: object) -> object:
            result = await original(req)
            try:
                inner = getattr(result, "root", result)
                content = getattr(inner, "content", None)
                if content:
                    deliver_pending_banner(content)
            except Exception as exc:  # noqa: BLE001 — never corrupt a tool result
                _log.debug(
                    "first_run_banner_hook_failed", error=f"{type(exc).__name__}: {exc}"
                )
            return result

        low.request_handlers[key] = _wrapped
        return True
    except Exception as exc:  # noqa: BLE001 — never block boot on the banner hook
        _log.debug("first_run_banner_hook_install_failed", error=f"{type(exc).__name__}: {exc}")
        return False
