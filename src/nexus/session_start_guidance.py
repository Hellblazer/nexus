# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (c) 2026 Hal Hildebrand. All rights reserved.
"""SessionStart guidance-imperative delivery (nexus-h33x8.4, .5; nexus-dg7i6.6).

TIER B, not Tier C. The guidance imperative used to reach every session as a
SessionStart hooks.json entry that ran ``cat`` on a skill file inside
``$CLAUDE_PLUGIN_ROOT``, which resolves to the PINNED, currently-installed
plugin release tag, so every wording edit was inert until the next plugin
release (Tier C). ``nx hook session-start`` is already a SessionStart
hooks.json entry AND already a PyPI binary (Tier B): a wheel change reaches
the maintainer at the next ``scripts/reinstall-tool.sh`` and every user at the
next ordinary PyPI release.

SessionStart is a cold channel read long before any routing decision exists,
so volume here is pure cost (nexus-h33x8.5). GUIDANCE_IMPERATIVE carries the
imperative, the trigger CONDITIONS (the "when"), and one short destination per
condition: an agent or a skill name, no ``/conexus:`` command spelling and no
tool-name prefix. The full routing table, the review gate, the skill routing
menu and the working rules live in the ``orchestration`` skill.

nexus-dg7i6.6: the ``using-nx-skills`` skill this text used to point at was
retired. A 30-day census found zero model loads of it, and an eval with a true
premise loaded it 0 of 3 times: the model does the work inline or dispatches
the agent the rules name. Its routing content moved to the ``orchestration``
skill and to the destinations named here; the SessionStart text no longer sends
the reader to a skill it would have to load first.

INTERIM DOUBLE-EMISSION GUARD (nexus-h33x8.4, item 4). A session running under
an OLD, still-pinned plugin whose hooks.json still carries the legacy ``cat``
entry would otherwise receive guidance TWICE: once from that entry, once from
this module. :func:`legacy_cat_channel_active` reads the INSTALLED plugin's own
``hooks/hooks.json`` (via ``$CLAUDE_PLUGIN_ROOT``, NOT this repo's copy) and
reports whether the legacy entry is still registered there;
:func:`guidance_block` suppresses this module's emission whenever it is. The
gate fails OPEN (emits) when it cannot be completed -- no
``$CLAUDE_PLUGIN_ROOT``, a missing pinned ``hooks.json``, or a parse failure --
because going permanently dark would be worse than a bounded, cosmetic
double-emission window that self-closes at the next plugin release.
"""
from __future__ import annotations

import json
import os
from pathlib import Path

#: nexus-h33x8.5, nexus-dg7i6.6: the SessionStart routing imperative. The
#: imperative plus its trigger CONDITIONS ("when"), each with the one agent or
#: skill that answers it. Not a menu: the table, the review gate and the skill
#: routing live in the ``orchestration`` skill. Every byte here is paid by
#: every session before any routing decision exists; the byte budget is
#: pinned by tests/test_session_start_guidance.py and
#: tests/hooks/test_session_start_combined_budget.py.
GUIDANCE_IMPERATIVE = (
    "Conexus skills and agents carry this project's accumulated practice "
    "for specific situations: which tools apply, which storage tier holds "
    "prior work, and failure modes already paid for. When one matches, "
    "dispatch the agent or load the skill named; it is how a session "
    "inherits what earlier ones learned.\n"
    "\n"
    "- broken, or two failed fix attempts: `debugger` agent\n"
    "- implementing with no design of record: `brainstorming-gate`; "
    "multi-step or cross-module work: `strategic-planning`\n"
    "- quality gate for code: `code-review-expert` AND `substantive-critic`, "
    "after stating \"Ship when <X>\"; each finding is a blocker or a bead; "
    "one confirmation round, then the user. Tests: `test-validator` if "
    "coverage needs proof; a plan: `plan-validation`\n"
    "- an answer reduced from many documents, not a lookup: `nx_answer` "
    "(minutes); lookups: `search`, `query` (seconds)\n"
    "- research starting: check T1/T2/T3 first (`scratch`, "
    "`memory_search`, `search`)\n"
    "- a validated finding about to go unstored, or a phase/RDR boundary: "
    "`store_put`, `memory_put`; `phase-review-gate`\n"
    "\n"
    "Call sequential thinking before every decision. Routing table, review "
    "gate, skill routing, working rules: `orchestration` skill. Skills "
    "change; check, do not recall.\n"
)


#: Marker substring identifying the legacy Tier-C SessionStart entry in a
#: plugin's ``hooks/hooks.json``. Kept as one constant so the hooks.json
#: edit (this bead) and the detector below can never drift on what
#: "the legacy entry" means.
_LEGACY_ENTRY_MARKER = "using-nx-skills/SKILL.md"


def legacy_cat_channel_active(plugin_root: str | None = None) -> bool:
    """Whether the INSTALLED (pinned) plugin still delivers the guidance
    imperative via the legacy ``cat .../using-nx-skills/SKILL.md``
    SessionStart entry (a skill file the current plugin no longer ships).

    Reads ``<plugin_root>/hooks/hooks.json`` -- the CURRENTLY INSTALLED
    plugin's registration, not this repo's working copy, which is the
    whole point: this repo's hooks.json may have already dropped the
    entry while the pinned, released plugin has not yet picked that up.

    Args:
        plugin_root: Override for testing. Defaults to the
            ``CLAUDE_PLUGIN_ROOT`` environment variable, which Claude
            Code sets for every hook subprocess it spawns (including
            bare-command entries like ``nx hook session-start`` that
            do not reference the variable in their own command line).

    Returns:
        ``True`` when the legacy entry is present (this module must stay
        silent to avoid double emission). ``False`` -- fail OPEN -- when
        ``plugin_root`` is unset, the pinned ``hooks.json`` is missing,
        or it fails to parse; see the module docstring for why silence
        is the worse default.
    """
    root = plugin_root if plugin_root is not None else os.environ.get("CLAUDE_PLUGIN_ROOT")
    if not root:
        return False
    hooks_json_path = Path(root) / "hooks" / "hooks.json"
    try:
        data = json.loads(hooks_json_path.read_text(encoding="utf-8"))
    except (OSError, ValueError, UnicodeDecodeError):
        return False
    if not isinstance(data, dict):
        return False
    session_start_entries = data.get("hooks", {})
    if not isinstance(session_start_entries, dict):
        return False
    for entry in session_start_entries.get("SessionStart", []) or []:
        if not isinstance(entry, dict):
            continue
        for sub in entry.get("hooks", []) or []:
            if not isinstance(sub, dict):
                continue
            if _LEGACY_ENTRY_MARKER in sub.get("command", ""):
                return True
    return False


def guidance_block(plugin_root: str | None = None) -> str:
    """The SessionStart guidance imperative to emit, or ``""``.

    ``""`` when :func:`legacy_cat_channel_active` reports the legacy
    ``cat`` entry is still live for the installed plugin (avoid double
    emission during the Tier-C dormancy window); ``GUIDANCE_IMPERATIVE``
    otherwise.
    """
    if legacy_cat_channel_active(plugin_root):
        return ""
    return GUIDANCE_IMPERATIVE
