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
* the lockstep action, after it finds the CLI already satisfies the plugin.

``NX_LOCKSTEP_MARKER`` replaces the path, as it does for the hook and the action.
"""
from __future__ import annotations

import os
from pathlib import Path

from nexus import config as _config


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
