# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (c) 2026 Hal Hildebrand. All rights reserved.
"""SessionStart guidance-imperative delivery (nexus-h33x8.4).

TIER B, not Tier C. Before this module existed, the guidance imperative
reached every session as SessionStart hooks.json entry:

    cat $CLAUDE_PLUGIN_ROOT/skills/using-nx-skills/SKILL.md

``$CLAUDE_PLUGIN_ROOT`` resolves to the PINNED, currently-installed plugin
release tag, so every wording/ordering/trigger edit to that file was inert
until the next plugin release (Tier C). ``nx hook session-start`` is
already a SessionStart hooks.json entry AND already a PyPI binary (Tier
B) -- a wheel change reaches the maintainer at the next
``scripts/reinstall-tool.sh`` and every user at the next ordinary PyPI
release, bypassing the PINNED PLUGIN TAG specifically (see nexus-h33x8's
plan-audit correction: this is "PyPI-release-cadence", not
"no-release-needed" -- neither tier skips a release entirely).

GUIDANCE_IMPERATIVE was originally a VERBATIM copy of
``conexus/skills/using-nx-skills/SKILL.md`` as it existed when this
module was created (2026-08-08); nexus-h33x8.4 was channel-only and
made no content change. nexus-h33x8.5 is the content inversion the .4
docstring predicted: GUIDANCE_IMPERATIVE now DIVERGES from the Skill
file on purpose. SessionStart is a cold channel an agent reads long
before any routing decision exists -- volume there is pure cost, not
capability, so it carries ONLY the imperative and its trigger
CONDITIONS (the "when": what situations mean "route now"), full stop.
Every destination LIST (the "what": which skill/command handles which
situation, the MCP tool catalogue, the Common Mistakes and Red Flags
tables, the RDR lifecycle chain) moved to the Skill body ONLY --
``conexus/skills/using-nx-skills/SKILL.md``, read in full when
``Skill`` is explicitly invoked on ``using-nx-skills``, which is
exactly the progressive-disclosure contract every other conexus skill
already follows. An agent that already knows it should route can look
up WHERE by invoking the Skill; an agent that does not know it should
route never gets there regardless of how much menu SessionStart
carries, which is the whole rationale for cutting the menu from this
channel rather than trying to compress it in place.

The Skill file's own routing content is UNCHANGED by this bead (the
nx_answer cost narrowing landed at 3a04da95c and stays exactly as
narrowed) -- only ONE sentence was added there, pointing back at this
short form so a reader who lands on the Skill body directly still
knows a condensed version is what SessionStart actually emits.

INTERIM DOUBLE-EMISSION GUARD (bead's own requirement, item 4). This
Tier-B change ships to PyPI before the Tier-C hooks.json edit (which
drops the legacy ``cat`` entry) is activated by a plugin release. During
that window a session running under the OLD, still-pinned plugin would
otherwise receive the SAME content TWICE per session: once from the
legacy ``cat`` entry, once from this module -- doubling exactly the
SessionStart volume the epic (nexus-h33x8) is measuring against.
:func:`legacy_cat_channel_active` closes that gap: it reads the
INSTALLED plugin's own ``hooks/hooks.json`` (via ``$CLAUDE_PLUGIN_ROOT``,
NOT this repo's copy) and reports whether the legacy entry is still
registered there. :func:`guidance_block` suppresses this module's
emission whenever it is. The gate fails OPEN (emits) when it cannot be
completed -- no ``$CLAUDE_PLUGIN_ROOT`` (bare CLI/dev/test invocation, no
legacy channel to collide with), a missing pinned ``hooks.json``, or a
parse failure -- because going permanently dark would be worse than a
bounded, cosmetic double-emission window that self-closes at the next
plugin release regardless.
"""
from __future__ import annotations

import json
import os
from pathlib import Path

#: nexus-h33x8.5: the SessionStart form of the using-nx-skills imperative.
#: Deliberately NOT the Skill file's content -- no frontmatter (this is
#: never parsed as a skill, only printed as SessionStart text), no
#: routing table, no MCP tool catalogue, no Common Mistakes / Red Flags
#: tables, no RDR lifecycle chain. Those are destination LISTS ("what")
#: and live in conexus/skills/using-nx-skills/SKILL.md ONLY, read when
#: `Skill` is invoked. What stays here is the imperative plus its
#: trigger CONDITIONS ("when") -- the signal an agent needs to recognize
#: "route now", not the menu of where. Keep this short on purpose; every
#: byte here is paid by every session before any routing decision
#: exists, unlike the Skill body which is paid only by a session that
#: already decided to invoke it.
GUIDANCE_IMPERATIVE = (
    "Conexus skills carry this project's accumulated practice for "
    "specific situations — which tools apply, which storage tier already "
    "holds prior work, and the failure modes this project has already "
    "paid for. When a situation below matches, invoking `Skill` is "
    "usually cheaper than re-deriving the approach, and it is how a "
    "session inherits what earlier ones learned.\n"
    "\n"
    "Situations a conexus skill already covers:\n"
    "- something is broken: a failure, an exception, or two failed fix "
    "attempts\n"
    "- implementation is about to start with no design of record\n"
    "- work spans multiple steps, or needs cross-module design before "
    "code\n"
    "- code, a plan, or tests are ready and need a quality gate\n"
    "- an answer has to be reduced from many documents rather than "
    "looked up as a single fact\n"
    "- research or exploration is starting without checking T1/T2/T3 for "
    "prior work\n"
    "- a validated finding is about to go unstored, or a phase/RDR "
    "boundary is being crossed\n"
    "\n"
    "Which skill handles which situation, the MCP tool catalogue, and "
    "the known failure modes live in the Skill body — invoke `Skill` on "
    "`using-nx-skills` to read them. Skills change; check rather than "
    "recalling a prior version.\n"
)


#: Marker substring identifying the legacy Tier-C SessionStart entry in a
#: plugin's ``hooks/hooks.json``. Kept as one constant so the hooks.json
#: edit (this bead) and the detector below can never drift on what
#: "the legacy entry" means.
_LEGACY_ENTRY_MARKER = "using-nx-skills/SKILL.md"


def legacy_cat_channel_active(plugin_root: str | None = None) -> bool:
    """Whether the INSTALLED (pinned) plugin still delivers the guidance
    imperative via the legacy ``cat .../using-nx-skills/SKILL.md``
    SessionStart entry.

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
