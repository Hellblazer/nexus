# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (c) 2026 Hal Hildebrand. All rights reserved.
"""User-level ``beads`` PRIME.md management (nexus-cnzei.8).

``bd`` (https://github.com/BeadsProject/beads) resolves the workflow-priming
text ``bd prime`` prints, in order: a repo-level ``.beads/PRIME.md``, then
``<beads-dir>/PRIME.md``, then a machine-wide file at the OS user config
directory (Go's ``os.UserConfigDir()``: macOS
``~/Library/Application Support/beads/PRIME.md``, Linux
``$XDG_CONFIG_HOME/beads/PRIME.md`` or ``~/.config/beads/PRIME.md``,
Windows ``%AppData%/beads/PRIME.md``). A repo-level file (this repo's own
``.beads/PRIME.md``, nexus-cnzei.2) fixes only that one repo; every OTHER
beads repo on the machine, for every conexus user, still gets ``bd``'s own
bundled PRIME text -- several KB, and on this project's own reading it
contradicts this project's workflow (a conservative no-commit/no-push
default, "do NOT use MEMORY.md", ``bd remember``, ``git add .``).

This module installs a GENERIC, conexus-managed, marker-versioned
``PRIME.md`` at that user-level path when beads is detected, so every beads
repo on the machine gets consistent guidance without a per-repo file.
Confirmed by direct experiment (not merely inspection): ``bd prime`` prints
a PRIME.md file's content byte-for-byte, HTML comment lines included -- the
first-line marker survives as ordinary literal text, harmless in model
context.

Never touches a user-authored ``PRIME.md`` -- one with no recognizable
marker, or one that cannot even be read back -- see :func:`status`.

LIMITS, stated rather than left implicit:

* This is a MACHINE-WIDE file. Installing it affects every beads repo on
  this box, not only the current project.
* beads 1.2.x still APPENDS ``bd remember`` memories after the PRIME.md
  text (``--no-memories`` suppresses it; ``bd prime`` is not invoked with
  that flag by this module or by the beads Claude Code plugin's own hook).
  Installing this file does not stop that.
* A repo-level ``.beads/PRIME.md`` always wins over this file -- ``bd``
  reads repo-level first.
"""
from __future__ import annotations

import contextlib
import os
import shutil
import sys
import tempfile
from collections.abc import Callable, Mapping
from enum import Enum
from pathlib import Path

import structlog

_log = structlog.get_logger()

__all__ = [
    "PrimeStatus",
    "beads_detected",
    "install",
    "install_and_describe",
    "load_template",
    "status",
    "user_prime_path",
]

#: First-line marker every conexus-managed PRIME.md carries. Anything else
#: (no marker at all, or a line that merely starts similarly but does not
#: parse) is treated as user-authored and never touched.
_MARKER_PREFIX = "<!-- conexus-managed beads PRIME v"
_MARKER_SUFFIX = " -->"
_TEMPLATE_RESOURCE = "beads_prime_template.md"


class PrimeStatus(str, Enum):
    """The four states a user-level ``PRIME.md`` can be in."""

    ABSENT = "absent"
    MANAGED_CURRENT = "managed-current"
    MANAGED_STALE = "managed-stale"
    USER_AUTHORED = "user-authored"


def user_prime_path(
    *,
    platform: str | None = None,
    home: Path | None = None,
    environ: Mapping[str, str] | None = None,
) -> Path:
    """The OS user-config path ``bd``'s final fallback tier resolves,
    matching Go's ``os.UserConfigDir()``:

    * macOS (``darwin``) -- ``~/Library/Application Support/beads/PRIME.md``
    * Windows (``win32``) -- ``%AppData%/beads/PRIME.md``; when ``APPDATA``
      is unset (Go's function would error there -- a caller here needs a
      path, not an exception) falls back to ``~/AppData/Roaming``.
    * everything else (Linux and other Unix-likes) --
      ``$XDG_CONFIG_HOME/beads/PRIME.md``, or ``~/.config/beads/PRIME.md``
      when that variable is unset.

    *platform*, *home*, and *environ* are each injectable for tests and
    default to the real ambient values (:data:`sys.platform`,
    :meth:`Path.home`, :data:`os.environ`) when omitted.
    """
    plat = platform if platform is not None else sys.platform
    env = environ if environ is not None else os.environ
    base_home = home if home is not None else Path.home()

    if plat == "darwin":
        base = base_home / "Library" / "Application Support"
    elif plat == "win32":
        appdata = env.get("APPDATA")
        base = Path(appdata) if appdata else base_home / "AppData" / "Roaming"
    else:
        xdg = env.get("XDG_CONFIG_HOME")
        base = Path(xdg) if xdg else base_home / ".config"
    return base / "beads" / "PRIME.md"


def _default_claude_config_dir() -> Path:
    override = os.environ.get("CLAUDE_CONFIG_DIR")
    return Path(override).expanduser() if override else Path.home() / ".claude"


def beads_detected(
    *,
    which: Callable[[str], str | None] = shutil.which,
    claude_config_dir: Path | None = None,
) -> tuple[bool, str]:
    """Is beads present on this machine? Returns ``(present, reason)``.

    Two independent signals, either sufficient:

    1. ``bd`` on ``PATH`` -- the CLI itself.
    2. The beads Claude Code plugin, installed under
       ``<claude-config-dir>/plugins/marketplaces/*/beads/`` or
       ``.../plugins/cache/*/beads/*/`` (the marketplace-cache layout ships
       one version-numbered directory per install). Honours
       ``CLAUDE_CONFIG_DIR`` the same way :mod:`nexus.health`'s Claude
       settings resolution does, when *claude_config_dir* is not passed
       explicitly.

    A user who only has the plugin (no local ``bd`` binary yet) still
    benefits once they install ``bd`` -- the file will already be there.
    A user who only has the binary still gets the file ``bd`` itself reads.
    """
    bd_path = which("bd")
    if bd_path:
        return True, f"bd on PATH ({bd_path})"

    base = claude_config_dir if claude_config_dir is not None else _default_claude_config_dir()
    plugins_root = base / "plugins"
    for sub in ("marketplaces", "cache"):
        root = plugins_root / sub
        if not root.is_dir():
            continue
        if any(root.glob("*/beads/.claude-plugin/plugin.json")):
            return True, f"beads plugin found under {root}"
        if any(root.glob("*/beads/*/.claude-plugin/plugin.json")):
            return True, f"beads plugin found under {root}"
    return False, "bd not on PATH and no beads Claude Code plugin found"


def load_template() -> str:
    """The packaged, generic ``PRIME.md`` text this module installs.

    Resolved through :mod:`importlib.resources` so it works from a wheel
    install and from an editable checkout alike (the same approach
    :func:`nexus.commands.self_cmd.packaged_install_dir` and
    :func:`nexus.tables.load` use for their own packaged data).
    """
    from importlib.resources import files  # noqa: PLC0415 — deferred, stdlib

    return (files("nexus") / _TEMPLATE_RESOURCE).read_text(encoding="utf-8")


def _has_marker(first_line: str) -> bool:
    line = first_line.rstrip("\n")
    return line.startswith(_MARKER_PREFIX) and line.endswith(_MARKER_SUFFIX)


def status(path: Path) -> PrimeStatus:
    """Classify *path* into one of the four :class:`PrimeStatus` states.

    Never raises. A path that does not exist at all is :data:`ABSENT`. Any
    OTHER failure to read it (a directory, permission denied, non-UTF-8
    bytes) is reported :data:`USER_AUTHORED` -- the safe default, since it
    guarantees :func:`install` never writes over something it could not
    first inspect.

    Distinguishing :data:`MANAGED_CURRENT` from :data:`MANAGED_STALE` is a
    byte-for-byte compare against :func:`load_template` -- covers both a
    content edit at the same marker version and a marker-version bump,
    with no separate version-arithmetic path to drift out of sync with the
    packaged text.
    """
    if not path.exists():
        return PrimeStatus.ABSENT
    try:
        text = path.read_text(encoding="utf-8")
    except (OSError, UnicodeDecodeError):
        return PrimeStatus.USER_AUTHORED

    first_line = text.split("\n", 1)[0]
    if not _has_marker(first_line):
        return PrimeStatus.USER_AUTHORED

    return (
        PrimeStatus.MANAGED_CURRENT
        if text == load_template()
        else PrimeStatus.MANAGED_STALE
    )


def _atomic_write(path: Path, content: str) -> None:
    """Write *content* to *path*, replacing it atomically (write-to-temp +
    ``os.replace``) so a crash mid-write never leaves a truncated
    ``PRIME.md`` for ``bd`` to read.
    """
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp_name = tempfile.mkstemp(
        dir=str(path.parent), prefix=".prime-", suffix=".tmp"
    )
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            f.write(content)
        os.replace(tmp_name, path)
    except BaseException:
        with contextlib.suppress(OSError):
            os.unlink(tmp_name)
        raise


def install(path: Path | None = None) -> tuple[str, Path]:
    """Install or refresh the user-level ``PRIME.md`` at *path* (default
    :func:`user_prime_path`).

    Returns ``(action, path)``, *action* being one of ``"installed"``,
    ``"updated"``, ``"up to date"``, or ``"left alone (user-authored)"``.
    Idempotent: a second call once the file is current is a no-op. Never
    overwrites a user-authored file, in either direction.
    """
    target = path if path is not None else user_prime_path()
    current = status(target)
    if current is PrimeStatus.USER_AUTHORED:
        return "left alone (user-authored)", target
    if current is PrimeStatus.MANAGED_CURRENT:
        return "up to date", target
    template = load_template()
    _atomic_write(target, template)
    action = "installed" if current is PrimeStatus.ABSENT else "updated"
    _log.info("beads_prime_install", action=action, path=str(target))
    return action, target


def install_and_describe() -> str | None:
    """CLI-facing convenience: detect beads, install/refresh the user-level
    ``PRIME.md`` when detected, and return one human-readable line
    describing what happened -- or ``None`` when beads was not detected at
    all (nothing to report).

    Callers (``nx init``, ``nx upgrade``) wrap this in their own
    best-effort ``try``/``except`` per their established convention
    (mirroring ``_seed_builtin_plans_best_effort`` et al.) -- this function
    itself does not swallow errors, so a caller's `except` block sees the
    real exception.
    """
    detected, _reason = beads_detected()
    if not detected:
        return None
    action, path = install()
    return f"Beads PRIME.md: {action} ({path})"
