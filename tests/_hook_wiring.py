# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (c) 2026 Hal Hildebrand. All rights reserved.
"""Read ``hooks.json`` by hook IDENTITY, not by declaration form (nexus-17i1n).

A conexus hook is declared one of two ways, and which one is a wiring
decision that changes over time:

* tool tier -- ``{"type": "mcp_tool", "tool": "hook_<name>", ...}``
* command tier -- ``{"type": "command", "command": "nx-hook",
  "args": ["<name-with-dashes>"], ...}``, or the same verb through the
  stdlib shim (nexus-rcoze, launched through ``uv`` since nexus-efk2h):
  ``{"type": "command", "command": "uv", "args": [*UV_LAUNCHER_ARGV,
  "${CLAUDE_PLUGIN_ROOT}/hooks/scripts/nx_hook_shim.py", "<name-with-dashes>"]}``, which is how a verb that an older CLI lacks
  is wired so it cannot block. It is still the command tier.

Tests that assert WHICH EVENT a hook fires on, or that it is wired
exactly once, are making a claim about the hook. Writing those against
one form means every tier move breaks assertions that were never about
the tier. Measured: the pre-close gate's matcher assertion had been
rewritten three times by 2026-09-20 -- positional index, then bash
command string, then mcp_tool name -- and bead nexus-17i1n moved it a
fourth time, when three verdict-returning hooks came back to the command
tier because an ``mcp_tool`` hook cannot return a verdict.

Worse than the churn: two of those rewrites were made by someone
reading a RED test and making it green, which is the same gesture
whether the wiring is right or wrong. Keying on identity means a tier
move is silent here and a hook genuinely going missing is loud, which
is the way round it should have been all along.
"""
from __future__ import annotations

import json
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
HOOKS_JSON = REPO_ROOT / "conexus" / "hooks" / "hooks.json"
NX_HOOK_SHIM = "${CLAUDE_PLUGIN_ROOT}/hooks/scripts/nx_hook_shim.py"


def command_verb(entry: dict) -> str | None:
    """The nx-hook verb a command-tier entry runs, directly or via the shim."""
    if entry.get("type") != "command":
        return None
    args = [a for a in (entry.get("args") or []) if isinstance(a, str)]
    if entry.get("command") == "nx-hook" and args:
        return args[0]
    shim_args = launcher_script_args(entry)
    if len(shim_args) == 2 and shim_args[0] == NX_HOOK_SHIM:
        return shim_args[1]
    return None


#: Everything `uv` is given before the script path (nexus-efk2h): sn's argv.
UV_LAUNCHER_ARGV = ("tool", "run", "--directory", "${CLAUDE_PLUGIN_ROOT}", "--no-config", "--quiet", "--python", ">=3.12", "python")


def launcher_script_args(entry: dict) -> list[str]:
    """The args after the launcher: ``[script, *script_args]``.

    Plugin-resident scripts and the shim launch through ``uv`` with
    :data:`UV_LAUNCHER_ARGV` first (nexus-efk2h: ``python3`` is not on PATH on
    stock Windows). Anything else, a direct ``nx-hook`` entry included,
    returns ``[]``.
    """
    args = [a for a in (entry.get("args") or []) if isinstance(a, str)]
    n = len(UV_LAUNCHER_ARGV)
    if entry.get("command") == "uv" and tuple(args[:n]) == UV_LAUNCHER_ARGV:
        return args[n:]
    return []


def is_plugin_launcher(entry: dict) -> bool:
    """Is *entry* a plugin-resident script or shim launched through ``uv``?"""
    return bool(launcher_script_args(entry))


def names_hook(entry: dict, hook_name: str) -> bool:
    """Does one ``hooks[].hooks[]`` entry declare *hook_name*, on either tier?

    *hook_name* is the underscore form (``auto_approve``); the
    command tier's dashed verb is derived, so callers name a hook one way.
    """
    if entry.get("type") == "mcp_tool":
        return entry.get("tool") == f"hook_{hook_name}"
    return command_verb(entry) == hook_name.replace("_", "-")


def events_for(hook_name: str, hooks_json: Path | None = None) -> list[str]:
    """Every event *hook_name* is wired on, one entry per wiring.

    A list rather than a set on purpose: ``auto_approve`` is wired twice,
    deliberately, and an exactly-once check needs to be able to see a
    duplicate rather than have it collapse.
    """
    data = json.loads((hooks_json or HOOKS_JSON).read_text())
    return [
        event
        for event, groups in data.get("hooks", {}).items()
        for group in groups
        for hook in group.get("hooks", [])
        if names_hook(hook, hook_name)
    ]


def matchers_for(hook_name: str, event: str, hooks_json: Path | None = None) -> list[str]:
    """The matchers of every *event* group that declares *hook_name*."""
    data = json.loads((hooks_json or HOOKS_JSON).read_text())
    return [
        group.get("matcher", "")
        for group in data.get("hooks", {}).get(event, [])
        if any(names_hook(hook, hook_name) for hook in group.get("hooks", []))
    ]
