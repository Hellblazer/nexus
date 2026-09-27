# SPDX-License-Identifier: AGPL-3.0-or-later
"""Derive the plugin set ``.claude-plugin/marketplace.json`` currently
lists (nexus-smsau).

Adding a plugin to marketplace.json used to silently fall out of three
hand-kept sites: the ``PLUGINS`` tuple in :mod:`nexus.plugin_lockstep`
(consumed with ``if plugin_short not in PLUGINS: continue``), an
identical tuple in ``conexus/hooks/scripts/version_lockstep_hook.py``
(that file keeps its own copy -- stdlib-only, see its module docstring,
so it cannot import this module), and a fixed loop in
``nexus.routing_stats``. This module is the ONE wheel-side derivation
:mod:`nexus.plugin_lockstep` and :mod:`nexus.routing_stats` share.

Resolution order, each a strict improvement over the hardcoded fallback:

1. ``NX_MARKETPLACE_JSON`` env override -- test-isolation seam, and an
   explicit escape hatch on a box where neither of the following applies.
2. The dev checkout's own ``.claude-plugin/marketplace.json``, found by
   walking up from this file (this repo IS the marketplace source, so a
   dev checkout always has the freshest copy on disk).
3. The installed marketplace clone's ``.claude-plugin/marketplace.json``,
   located via ``~/.claude/plugins/known_marketplaces.json``'s
   ``MARKETPLACE_NAME`` entry -- the one marketplace this project's own
   plugins ship from. ``MARKETPLACE_NAME`` is this repo's OWN marketplace
   name (marketplace.json's own ``"name"`` field, and the literal already
   spelled out in every registry key comment such as
   ``"conexus@nexus-plugins"`` throughout this codebase) -- a fixed
   string, not something read out of the file this function is trying to
   locate, so there is no bootstrapping problem.

``.claude-plugin/marketplace.json`` is NOT force-included into the wheel
(see ``pyproject.toml``'s ``[tool.hatch.build.targets.wheel]`` --
only ``conexus/plans``, ``dt/scripts`` and ``conexus/daemon`` travel), so
an installed, non-dev-checkout box that also has no local marketplace
clone (route 3 needs one) has no marketplace.json to read at ALL. FAIL
OPEN, never fail loud: a stale, logged fallback to the set this module
shipped with is preferable to breaking ``nx upgrade``,
``nx hook routing-stats``, or anything else built on this -- but the
fallback is ALWAYS logged (never a silent substitution), so genuine
drift (this module's fallback disagreeing with a live marketplace.json)
is discoverable rather than swallowed.
"""
from __future__ import annotations

import json
import os
from pathlib import Path

import structlog

_log = structlog.get_logger(__name__)

#: Test-isolation seam / explicit override: point this straight at a
#: marketplace.json (real or fixture), skipping every location probe
#: below. Read fresh on every call (never cached at import time), so a
#: test setting this mid-session takes effect immediately.
MARKETPLACE_JSON_ENV = "NX_MARKETPLACE_JSON"

#: Test-isolation seam for the installed-clone lookup (route 3); same
#: override name :mod:`nexus.plugin_lockstep` uses for its own
#: ``known_marketplaces.json`` reads, so one env var isolates both.
#: Duplicated here (not imported) so this module carries NO import-time
#: dependency on ``nexus.plugin_lockstep`` -- that module imports THIS
#: one to build its own derived ``PLUGINS``-equivalent, and a two-way
#: module-level import would be a cycle.
MARKETPLACES_ENV = "NX_PLUGIN_MARKETPLACES"

#: This repo's own marketplace name -- see the module docstring for why
#: this is a fixed literal rather than something resolved at runtime.
MARKETPLACE_NAME = "nexus-plugins"

#: The set this module ships with, used ONLY when no marketplace.json is
#: reachable by any route above -- see the module docstring.
FALLBACK_PLUGINS: tuple[str, ...] = ("conexus", "sn")


def _plugin_names(data: object) -> tuple[str, ...] | None:
    """Plugin short names from parsed marketplace.json content, or
    ``None`` for any unexpected shape (missing/non-list ``plugins``, no
    usable names)."""
    if not isinstance(data, dict):
        return None
    plugins = data.get("plugins")
    if not isinstance(plugins, list):
        return None
    names = tuple(
        p["name"]
        for p in plugins
        if isinstance(p, dict) and isinstance(p.get("name"), str) and p["name"]
    )
    return names or None


def _read(path: Path) -> tuple[str, ...] | None:
    try:
        data = json.loads(path.read_text())
    except (OSError, ValueError):
        return None
    return _plugin_names(data)


def _dev_checkout_path() -> Path:
    # src/nexus/plugin_registry.py -> parents[2] is the repo root.
    return Path(__file__).resolve().parents[2] / ".claude-plugin" / "marketplace.json"


def _default_marketplaces_path() -> Path:
    override = os.environ.get(MARKETPLACES_ENV, "").strip()
    if override:
        return Path(override)
    return Path.home() / ".claude" / "plugins" / "known_marketplaces.json"


def _installed_clone_path(marketplaces_path: Path | None = None) -> Path | None:
    """The installed marketplace clone's marketplace.json, via
    ``known_marketplaces.json``'s own :data:`MARKETPLACE_NAME` entry --
    the only route from an installed (non-dev-checkout) wheel, since
    marketplace.json itself is not force-included into the wheel. ``None``
    on any unreadable/unexpected shape -- refuse, never guess."""
    path = marketplaces_path or _default_marketplaces_path()
    try:
        data = json.loads(path.read_text())
    except (OSError, ValueError):
        return None
    if not isinstance(data, dict):
        return None
    entry = data.get(MARKETPLACE_NAME)
    if not isinstance(entry, dict):
        return None
    loc = entry.get("installLocation")
    if not isinstance(loc, str) or not loc:
        return None
    return Path(loc) / ".claude-plugin" / "marketplace.json"


def known_plugins(marketplaces_path: Path | None = None) -> tuple[str, ...]:
    """The plugin short names ``.claude-plugin/marketplace.json`` lists
    right now, resolved by the routes named in the module docstring.
    Never raises; a fully unreachable marketplace.json falls back to
    :data:`FALLBACK_PLUGINS`, logged via ``plugin_registry_marketplace_
    unreachable`` -- never a silent skip.
    """
    override = os.environ.get(MARKETPLACE_JSON_ENV, "").strip()
    if override:
        names = _read(Path(override))
        if names:
            return names
    names = _read(_dev_checkout_path())
    if names:
        return names
    clone_path = _installed_clone_path(marketplaces_path)
    if clone_path is not None:
        names = _read(clone_path)
        if names:
            return names
    _log.warning("plugin_registry_marketplace_unreachable", fallback=FALLBACK_PLUGINS)
    return FALLBACK_PLUGINS
