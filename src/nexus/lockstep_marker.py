# SPDX-License-Identifier: AGPL-3.0-or-later
"""The RDR-143 version-lockstep marker, as the CLI reads and writes it.

The marker (``<config dir>/cli_lockstep_marker``) holds the CLI version last confirmed to match
the plugin. The SessionStart hook compares it with the plugin's version; a missing or stale marker
makes the hook fall back to the CLI's own `nx --version`, and only a CLI older than the
plugin, or one that prints no version, makes the session announce an upgrade and dispatch one.

Who writes it:

* the plugin's setup (``conexus/launch/nx_setup.py``), after a build it has checked. It runs
  before any CLI exists and cannot import this module, so it keeps its own copy of the path
  rule;
* ``nx self install --bootstrap`` (nexus-sv2wx.28), for the installer scripts
  (``web/install.sh``, ``web/install.ps1``), which build the CLI without the plugin's setup;
* the lockstep action, after it finds the CLI already satisfies the plugin;
* ``nx upgrade`` itself, :func:`mark_upgrade_retry`, when it is the lockstep action's detached
  child and loses the single-instance lock (exit 75): the action wrote the plugin's version at
  spawn, so without this the work the child owed (plugin convergence, the generated agents and
  hooks refresh, the engine install, the dimension advisory) waits for the next version or ref
  drift. The sentinel is :data:`UPGRADE_RETRY_MARKER`. Deleting the marker would not do: a marker
  that is missing or stale reads as lockstep whenever the CLI is not older than the plugin
  (nexus-ehg1y), and the binary is current by then.
  The sentinel is written only over a marker no newer than the running CLI, and a completed
  ``nx upgrade`` that does the owed work replaces it with its version (:func:`settle_upgrade_retry`).

``NX_LOCKSTEP_MARKER`` replaces the path, as it does for the hook and the action.
"""
from __future__ import annotations

import os
from pathlib import Path

from nexus import config as _config


#: What :func:`mark_upgrade_retry` writes: "the binary is current, the rest of ``nx upgrade`` is owed". Not a
#: version, so no version comparison can mistake it for one. The hook and the action are stdlib scripts that
#: import nothing from ``nexus`` and keep a copy of this string; ``tests/hooks/test_version_lockstep_hook.py``
#: keeps the three equal.
UPGRADE_RETRY_MARKER = "retry-nx-upgrade"


def marker_path() -> Path:
    """The marker file: ``NX_LOCKSTEP_MARKER`` when set, else ``cli_lockstep_marker`` in the
    config directory the CLI uses (``NEXUS_CONFIG_DIR``, else ``~/.config/nexus``)."""
    override = os.environ.get("NX_LOCKSTEP_MARKER")
    if override:
        return Path(override)
    return _config.nexus_config_dir() / "cli_lockstep_marker"


def read_marker() -> str | None:
    """The recorded version; ``None`` when there is no marker, it is empty or it cannot be read."""
    try:
        text = marker_path().read_text(encoding="utf-8").strip()
    except OSError:
        return None
    return text or None


def write_marker(version: str) -> None:
    """Record *version* as the CLI version in lockstep with the plugin.

    Raises ``OSError`` when the file cannot be written; the caller decides whether that matters.
    """
    path = marker_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(version, encoding="utf-8")


def running_version() -> str | None:
    """The version of the conexus this process runs; ``None`` when its distribution metadata is unreadable."""
    from importlib.metadata import PackageNotFoundError, version  # noqa: PLC0415 — stdlib, deferred

    try:
        return version("conexus")
    except PackageNotFoundError:
        return None


def _newer_than(held: str, running: str) -> bool | None:
    """Whether *held* is a version newer than *running*; ``None`` when either is not a version."""
    from packaging.version import InvalidVersion, Version  # noqa: PLC0415 — deferred

    try:
        return Version(held) > Version(running)
    except InvalidVersion:
        return None


def mark_upgrade_retry(running: str | None) -> None:
    """Leave :data:`UPGRADE_RETRY_MARKER` in the marker, so the next session's lockstep hook dispatches
    the upgrade again (nexus-emiky). Best effort: an unwritable path changes nothing, because the run is
    already exiting with a refusal and the marker was only ever a retry signal.

    The sentinel goes in only over what this run may speak for: a missing marker, the sentinel itself, or a
    version no newer than *running* (the action wrote the plugin's pin at spawn, and the child runs that pin
    or a later build). A run that waited 20 minutes for the lock must not put a stale sentinel over a newer
    version a later session has confirmed since. The comparison is with the running CLI, not with the marker
    as the run saw it when its wait began: the action writes its version about two seconds after spawning,
    so that snapshot would be the version before it. An unknown *running* or a marker that is not a version
    changes nothing: the claim cannot be checked.
    """
    held = read_marker()
    if held is not None and held != UPGRADE_RETRY_MARKER:
        if running is None or _newer_than(held, running) is not False:
            return
    try:
        write_marker(UPGRADE_RETRY_MARKER)
    except OSError:
        pass


def settle_upgrade_retry(running: str | None) -> None:
    """A completed ``nx upgrade`` has done the work :data:`UPGRADE_RETRY_MARKER` says is owed: when the marker
    still holds the sentinel, put *running* there (nexus-emiky). Any other content is left as it is, and so is
    the sentinel when *running* is unknown. Best effort."""
    if running is None or read_marker() != UPGRADE_RETRY_MARKER:
        return
    try:
        write_marker(running)
    except OSError:
        pass
