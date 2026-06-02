# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (c) 2026 Hal Hildebrand. All rights reserved.
"""RDR-126 §2 (nexus-3t1jg): library-level T2 autostart install/uninstall.

The OS-unit install/uninstall logic previously lived inside the Click
command bodies ``t2_install_cmd`` / ``t2_uninstall_cmd`` in
``nexus.commands.daemon``. RDR-126 lifts it here so it can be called
**in-process** with a structured return value by:

- ``nexus.mcp._first_run.ensure_installed_and_running`` — first-run on
  MCP startup, which needs to know whether it installed fresh
  (``NEWLY_INSTALLED``) or found an existing unit (``ALREADY_PRESENT``)
  to drive the first-run banner's two text variants and surface the
  unit path; and
- the ``daemon_uninstall`` MCP tool (RDR-126 §4); and
- the ``nx daemon t2 install/uninstall`` CLI, which becomes a thin
  wrapper that translates these results into ``click.echo`` / exit codes.

Design rules:

- **Pure library code.** No ``click``, no ``sys.exit``, no ``print``.
  Outcomes are returned (:class:`InstallResult` / :class:`UninstallResult`)
  or raised as typed :class:`InstallerError` subclasses.
- **Generic autostart helpers stay in ``nexus.commands.daemon``** (they
  are shared with the T3 install paths). This module delegates to them
  via a lazy import so there is no import cycle and so the existing test
  indirection points (``daemon._autostart_*``) keep working.
"""
from __future__ import annotations

import os
import subprocess
from dataclasses import dataclass, field
from enum import Enum
from pathlib import Path

import structlog

_log = structlog.get_logger(__name__)


class InstallStatus(Enum):
    """Outcome of :func:`install_autostart`."""

    NEWLY_INSTALLED = "newly_installed"
    ALREADY_PRESENT = "already_present"
    FAILED = "failed"


class UninstallStatus(Enum):
    """Outcome of :func:`uninstall_autostart`."""

    REMOVED = "removed"
    NOT_INSTALLED = "not_installed"


@dataclass(frozen=True)
class InstallResult:
    """Structured result of an autostart install attempt."""

    status: InstallStatus
    dest: Path
    detail: str = ""
    activated_cmd: list[str] | None = None
    warnings: tuple[str, ...] = ()


@dataclass(frozen=True)
class UninstallResult:
    """Structured result of an autostart uninstall attempt."""

    status: UninstallStatus
    dest: Path
    warnings: tuple[str, ...] = field(default_factory=tuple)


class InstallerError(Exception):
    """Base class for install failures the CLI translates to exit 1."""


class SymlinkRefusedError(InstallerError):
    """The destination unit path is a symlink; refuse to write through it."""


class ContentDiffersError(InstallerError):
    """The destination exists with differing content and ``force`` is off."""


class ActivationError(InstallerError):
    """``launchctl`` / ``systemctl`` activation failed and ``force`` is off."""


def _render_for_t2() -> tuple[Path, str]:
    """Resolve the destination path and the rendered unit body for T2.

    Delegates to the generic helpers in ``nexus.commands.daemon`` (lazy
    import to avoid an import cycle, since ``daemon`` imports this module
    to back its thin CLI wrappers).
    """
    from nexus.commands import daemon as _daemon

    install_dir = _daemon._autostart_install_dir()
    install_dir.mkdir(parents=True, exist_ok=True)
    log_dir = _daemon._autostart_log_dir()
    log_dir.mkdir(parents=True, exist_ok=True)

    template_name = _daemon._autostart_filename_t2()
    nx_bin = _daemon._resolve_nx_bin()
    rendered = _daemon._render_template(
        template_name,
        nx_bin=nx_bin,
        log_dir=str(log_dir),
        path_env=os.environ.get("PATH", "/usr/local/bin:/usr/bin:/bin"),
    )
    return install_dir / template_name, rendered


def _activate_cmd(dest: Path) -> list[str]:
    from nexus.commands import daemon as _daemon

    if _daemon._autostart_platform() == "darwin":
        uid = os.getuid()
        return ["launchctl", "bootstrap", f"gui/{uid}", str(dest)]
    return ["systemctl", "--user", "enable", "--now", dest.name]


def _deactivate_cmd(dest: Path) -> list[str]:
    from nexus.commands import daemon as _daemon

    if _daemon._autostart_platform() == "darwin":
        uid = os.getuid()
        return ["launchctl", "bootout", f"gui/{uid}/{_daemon._T2_LAUNCHD_LABEL}"]
    return ["systemctl", "--user", "disable", "--now", dest.name]


def install_autostart(*, force: bool = False) -> InstallResult:
    """Install the T2 daemon OS autostart unit for the current user.

    The OS unit is the source of truth. If the destination already holds
    the freshly-rendered content, returns ``ALREADY_PRESENT`` without
    re-activating. Otherwise the unit is written and activated via
    ``launchctl bootstrap`` (macOS) / ``systemctl --user enable --now``
    (Linux), returning ``NEWLY_INSTALLED``.

    Raises:
        SymlinkRefusedError: ``dest`` is a symlink.
        ContentDiffersError: ``dest`` exists with differing content and
            ``force`` is False.
        ActivationError: activation shelled out non-zero / not found and
            ``force`` is False.

    Under ``force`` an activation failure is downgraded to a warning on
    the returned :class:`InstallResult` rather than raised.
    """
    dest, rendered = _render_for_t2()

    if dest.is_symlink():
        raise SymlinkRefusedError(
            f"{dest} is a symlink; refusing to install autostart through it. "
            "Remove the symlink first and re-run."
        )
    if dest.exists():
        try:
            existing: str | None = dest.read_text()
        except OSError:
            existing = None
        if existing == rendered:
            return InstallResult(
                status=InstallStatus.ALREADY_PRESENT,
                dest=dest,
                detail=f"{dest} already up to date; no changes",
            )
        if not force and existing is not None:
            raise ContentDiffersError(
                f"{dest} exists and its content differs from the rendered "
                "template; refusing to overwrite. Re-run with --force to "
                "replace the existing file (your customisations will be "
                "lost), or remove the file first."
            )

    dest.write_text(rendered)
    dest.chmod(0o644)

    cmd = _activate_cmd(dest)
    warnings: tuple[str, ...] = ()
    try:
        result = subprocess.run(cmd, capture_output=True, text=True, check=False)
    except FileNotFoundError as exc:
        msg = f"{cmd[0]} not found on PATH; file installed but not activated ({exc})."
        if not force:
            raise ActivationError(msg) from exc
        _log.warning("t2_install_activation_not_found", dest=str(dest), error=str(exc))
        return InstallResult(
            status=InstallStatus.NEWLY_INSTALLED, dest=dest, detail=msg, warnings=(msg,)
        )
    if result.returncode != 0:
        detail = (result.stderr or "").strip() or (result.stdout or "").strip()
        msg = f"{' '.join(cmd)} exited {result.returncode}: {detail}"
        if not force:
            raise ActivationError(msg)
        _log.warning("t2_install_activation_failed", dest=str(dest), returncode=result.returncode)
        warnings = (msg,)
        return InstallResult(
            status=InstallStatus.NEWLY_INSTALLED, dest=dest, detail=msg, warnings=warnings
        )

    return InstallResult(
        status=InstallStatus.NEWLY_INSTALLED,
        dest=dest,
        detail=f"Activated via: {' '.join(cmd)}",
        activated_cmd=cmd,
    )


def uninstall_autostart() -> UninstallResult:
    """Remove the T2 daemon OS autostart unit for the current user.

    A non-zero / missing ``launchctl bootout`` / ``systemctl disable``
    is downgraded to a warning and the file is removed anyway (mirrors
    the original CLI: the unit file is the durable artifact). Returns
    ``NOT_INSTALLED`` when nothing is present.
    """
    from nexus.commands import daemon as _daemon

    install_dir = _daemon._autostart_install_dir()
    dest = install_dir / _daemon._autostart_filename_t2()

    if not dest.exists():
        return UninstallResult(status=UninstallStatus.NOT_INSTALLED, dest=dest)

    warnings: list[str] = []
    cmd = _deactivate_cmd(dest)
    try:
        result = subprocess.run(cmd, capture_output=True, text=True, check=False)
        if result.returncode != 0:
            detail = (result.stderr or "").strip() or (result.stdout or "").strip()
            warnings.append(f"{' '.join(cmd)} exited {result.returncode}: {detail}")
    except FileNotFoundError as exc:
        warnings.append(f"{cmd[0]} not found ({exc}); removing file anyway.")

    dest.unlink()
    return UninstallResult(
        status=UninstallStatus.REMOVED, dest=dest, warnings=tuple(warnings)
    )
