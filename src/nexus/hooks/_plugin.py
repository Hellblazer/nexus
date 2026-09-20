# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (c) 2026 Hal Hildebrand. All rights reserved.
"""Where the conexus plugin's own files are, from inside the wheel.

RDR-215 moves hooks from the plugin into the wheel, but several of them
still reach a plugin-resident sibling that this epic does not port --
``t2_prefix_scan.py``, ``read_verification_config.py``,
``divergence-language-scan.py``, ``tuple_ledger_project.py``. That is a
NEW dependency direction: previously plugin reached plugin, and a
``dirname "${BASH_SOURCE[0]}"`` was always right.

It is no longer always right, and the failure is silent. A ported module
at ``<repo>/src/nexus/hooks/x.py`` has the repo root at ``parents[3]``,
so a checkout-relative anchor works and every test passes. The same
module installed is at ``<...>/site-packages/nexus/hooks/x.py``, where
``parents[3]`` is the ``python3.N`` directory -- the sibling is not
there, the caller's "a missing script is an advisory no-op" branch
fires, and the hook reports nothing wrong forever. Measured on
``divergence_language_guard`` (nexus-q02nx.21 critique, critical 1): the
RDR-065 Gap 2 scan was dead for every non-checkout user.

So the resolution lives in one place. ``CLAUDE_PLUGIN_ROOT`` is what
Claude Code sets for every plugin hook invocation, real or dispatched
through ``nx-hook``; the checkout layout is the fallback for a
from-source run with no plugin host.

NOT every caller should use this. ``subagent_start`` resolves off
``CLAUDE_PLUGIN_ROOT`` ALONE, with no fallback, deliberately -- the bash
it ports had no ``:-`` default either, and adding one would change what
an unset variable does in a case the original never covered. Its
docstring carries that reasoning. A helper is for callers that want the
fallback, not a rule that everyone must.
"""

from __future__ import annotations

import os
from pathlib import Path

__all__ = ["checkout_plugin_root", "plugin_root", "plugin_script"]


def checkout_plugin_root() -> Path:
    """The plugin root as it sits in a dev CHECKOUT, ignoring the env var.

    The one place that knows the ``parents[N]`` walk. Callers that want
    the ordinary resolution want :func:`plugin_root`; this exists for
    ``tuple_projection``, which tries the env path and the checkout path
    as two CANDIDATES and takes whichever exists, so it needs the second
    one spelled separately.
    """
    return Path(__file__).resolve().parents[3] / "conexus"


def _is_unexpanded(value: str) -> bool:
    """Is *value* a shell/JSON placeholder nobody substituted?

    ``conexus/.mcp.json`` declares the MCP servers' env as
    ``{"CLAUDE_PLUGIN_ROOT": "${CLAUDE_PLUGIN_ROOT}"}``, and Claude Code
    does NOT expand ``${...}`` inside an MCP server's ``env`` block -- it
    passes the literal through. Measured 2026-09-20 across every
    ``nx-mcp`` on this box and in three different repositories, so this
    is the configuration every conexus user runs, not a local accident.

    That is strictly worse than the variable being unset. A non-empty
    literal is TRUTHY, so the "env wins" branch below took it and
    returned ``Path("${CLAUDE_PLUGIN_ROOT}")`` -- a relative path that
    can never exist -- and the documented unset fallback never ran. Every
    tool-tier hook that reaches a plugin-resident script then failed
    silently: the session-end verification config read ``{}`` and so
    never verified, subagent context lost its whole T2 Memory section,
    and the RDR-205 ledger projection wrote nothing. Bead nexus-b5ugt.

    Treating it as unset does not FIX those -- the real fix is that
    nothing in the wheel should need the plugin root at all -- but it
    turns a silent wrong answer into the branch every caller already
    documents and handles.
    """
    return "${" in value or value.startswith("$")


def plugin_root() -> Path:
    """The conexus plugin's root directory.

    ``CLAUDE_PLUGIN_ROOT`` when set to a real value, else the dev-checkout
    layout (``<repo>/conexus``, this file being
    ``<repo>/src/nexus/hooks/_plugin.py``). The path is not checked for
    existence: callers differ on what an absent plugin means, and several
    of them treat it as a no-op rather than an error.

    An UNEXPANDED placeholder counts as unset -- see :func:`_is_unexpanded`
    for why that case is real and what it cost.
    """
    root = os.environ.get("CLAUDE_PLUGIN_ROOT")
    if root and not _is_unexpanded(root):
        return Path(root)
    return checkout_plugin_root()


def plugin_script(name: str) -> Path:
    """A script under the plugin's ``hooks/scripts/``, by file name.

    *name* may include a subdirectory, e.g. ``"routing/_lib.py"``.
    """
    return plugin_root() / "hooks" / "scripts" / name
