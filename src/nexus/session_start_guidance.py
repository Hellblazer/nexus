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

GUIDANCE_IMPERATIVE below is a VERBATIM copy of
``conexus/skills/using-nx-skills/SKILL.md`` as it existed when this
module was created (2026-08-08) -- content is out of scope for this
change (nexus-h33x8.4 is channel-only; content restructuring is
nexus-h33x8.5's job, sequenced to land AFTER this so *that* iteration is
also Tier B). The Skill file itself is untouched and remains the body
read when ``/conexus:using-nx-skills`` is explicitly invoked; this is a
second, independent copy specifically so .5 can diverge the SessionStart
text (short imperative + triggers) from the Skill body (full routing
menu, behind progressive disclosure) without this module silently
tracking the Skill file forever.

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

#: Verbatim byte-for-byte copy of conexus/skills/using-nx-skills/SKILL.md,
#: frontmatter included, as it existed 2026-08-08 (nexus-h33x8.4 landing).
#: Do not reword here -- content iteration belongs to nexus-h33x8.5 and
#: every edit after it lands is picked up at the next reinstall/release
#: (that iteration-speed win is the entire point of this bead).
GUIDANCE_IMPERATIVE = '---\nname: using-nx-skills\ndescription: Use when starting any turn — you MUST scan the available conexus skill list and invoke `Skill` for any matching skill BEFORE producing any other response (clarifying questions, code, or prose included). Direct answers without first invoking a matching skill are a defect. False positives are cheap; misses cost real time.\neffort: low\n---\n\n# Using Conexus Skills\n\n**You MUST invoke `Skill` for any plausibly-matching conexus skill before producing any other response.** This is not a hint or a preference — it is a hard rule. Skipping a matching skill is a defect, not an optimization. False positives are cheap; misses cost real time. Skills evolve — read the current version, don\'t rely on memory.\n\n## Plan Reuse\n\nBefore any multi-agent pipeline:\n1. `mcp__plugin_conexus_nexus__plan_search(query="<task description>", limit=3)`\n2. If a match returns, present it as a starting structure\n3. If "No matching plans.", route normally\n\nAfter a successful pipeline:\n- Retrieval pipelines auto-grow the plan library via `nx_answer` — no manual save needed. Only `plan_save` a genuinely reusable **retrieval** plan, and it **requires a `verb`** (research / analyze / query / review / …): `mcp__plugin_conexus_nexus__plan_save(query="<question>", plan_json={...}, verb="<verb>", tags="<ops>")`. **Implementation / pipeline / phased-execution plans do NOT go here** — they live in beads + T2 memory. A verb-less save is refused (it pollutes the verb-dimensional plan-match library).\n\n## Routing\n\n**Before code:**\n- About to implement with NO design of record → `/conexus:brainstorming-gate` (mandatory; a locked T2 memo, accepted RDR, or reviewed bead IS the approved design — implement it without re-gating)\n- Multi-step → `/conexus:create-plan`\n- Needs design across modules → `/conexus:architecture` then `/conexus:create-plan`\n\n**Something broken:**\n- Failure / exception / unexpected behaviour → `/conexus:debug` immediately\n- 2 failed fix attempts without `/conexus:debug` → invoke now\n\n**Analyzing code:**\n- Structure / dependencies → `/conexus:analyze-code`\n- Why something behaves a certain way → `/conexus:deep-analysis`\n\n**Executing:**\n- Plan approved → `/conexus:implement`\n- Beads need enrichment → `/conexus:enrich-plan`\n\n**Quality gates:**\n- Code ready → `/conexus:review-code`\n- Plan ready → `/conexus:plan-audit` (validates against codebase)\n- Critique reasoning soundness → `/conexus:substantive-critique`\n- Tests written → `/conexus:test-validate`\n\n**ALL analytical questions go through `nx_answer`.** A verb-shaped question ("how does X work", "what tradeoffs in Y", "compare X across projects", "why was Z designed this way") routes to a skill that calls `nx_answer`. `nx_answer` composes search/query/operators under a plan-match-first gate — composed retrieval is strictly more useful than raw chunks. Raw `search` is for keyword lookup only ("find X in collection Y").\n\n- "how does…" / "tradeoffs…" / "compare…" / "why was this designed…" → `/conexus:query`\n- Design walks from concept to code → `/conexus:research`\n- Critique a change set → `/conexus:review`\n- Cross-corpus synthesis or ranking → `/conexus:analyze`\n- Why was this written this way → `/conexus:debug`\n- Documentation gaps → `/conexus:document`\n- 3+ validated findings to keep → `/conexus:knowledge-tidy`\n- PDF to index → `/conexus:pdf-process`\n\n**RDR lifecycle:** `/conexus:rdr-create` → `/conexus:rdr-research` → `/conexus:rdr-gate` → `/conexus:rdr-accept` → (implementation phases) → `/conexus:rdr-close`. List/show: `/conexus:rdr-list`, `/conexus:rdr-show NNN`. Audit: `/conexus:rdr-audit`.\n\n**Phase boundary inside an implementation arc:** every phase-review bead, before close, runs `/conexus:phase-review-gate <rdr-id> --phase N`. Pass 1 enumerates the RDR\'s numbered §Approach items; Pass 2 validates each has a closing-bead pointer (`ItemN=nexus-xxxx`) or explicit `none` deferral. BLOCKED on any unaccounted item. Not optional. Prevents the silent scope reduction class (RDR-112 Phase 1 / nexus-52lb, 2026-05-15: T3 daemon silently dropped from a 6-bead close, found three phases later, 2-3 days of replanning).\n\n**Git:** isolation → `/conexus:git-worktrees`. Done → `/conexus:finishing-branch`. Receiving review → `/conexus:receiving-review`.\n\n**Catalog/linking:** entries, links, tumblers, link-context seeding → `/conexus:catalog`.\n\n**Reference (no agent dispatch):** `/conexus:serena-code-nav`, `/conexus:nexus`, `/conexus:cli-controller`, `/conexus:writing-nx-skills`.\n\n## Essential MCP Tools (always available)\n\n**Sequential Thinking** (`mcp__plugin_conexus_sequential-thinking__sequentialthinking`) — use for any non-trivial decision: debugging hypotheses, design choices, plan evaluation, risk assessment. Workflow: hypothesis → evidence → evaluate → branch or proceed. `needsMoreThoughts: true` to continue, `isRevision: true` to correct, `branchFromThought: N` + `branchId` to explore alternatives.\n\n**Conexus Storage Tiers — check before any work, write your findings back.** Read widest → narrowest:\n- **T3** `nx search` / `nx_answer`: permanent knowledge across all sessions and projects — **check before researching from scratch**.\n- **T2** `nx memory`: project decisions, findings, session context — **check before project work**.\n- **T1** `nx scratch`: this session\'s discoveries, shared across all sibling agents — **check before duplicating sibling work**.\n\nWrite path: T1 (immediate, shared with siblings) → `--persist` flag to T2 (survives session) → `/conexus:knowledge-tidy` to T3 (permanent, cross-project). **Findings not stored are findings lost** — call `store_put` (T3) or `memory_put` (T2) before returning a result you\'d want a future session to know.\n\n## Common Mistakes\n\n| Mistake | Correction |\n|---------|------------|\n| `search(query="how does X work", …)` for an analytical question | `nx_answer(question="how does X work", …)` via `/conexus:query` or a verb skill |\n| `search(query="tradeoffs in Y")` | `nx_answer` via `/conexus:analyze` — `search` returns chunks, you need composition |\n| `search(query="compare X across projects")` | `nx_answer` via `/conexus:analyze` — cross-corpus compare is what plan operators do |\n| Researching from scratch without checking T3 | `nx search` / `nx_answer` first — prior sessions may have already answered |\n| Returning findings without storing them | `store_put` (T3) or `memory_put` (T2) before returning |\n| Test fails → try a different fix | `/conexus:debug` |\n| Implement undesigned work without brainstorming-gate | `brainstorming-gate` first (unless a design of record exists) |\n| Plan exists, start implementing | `/conexus:plan-audit` first |\n| Symbol callers via grep | `/conexus:serena-code-nav` |\n| Implement review feedback blindly | `/conexus:receiving-review` first |\n| Manual worktree setup | `isolation: "worktree"` on Agent tool, or `/conexus:git-worktrees` |\n\n## Red Flags\n\nThoughts that mean STOP — you are rationalizing past a tier check:\n\n| Thought | Reality |\n|---------|---------|\n| "Let me explore the codebase first" | T3 `nx search` first — prior research may already cover it. |\n| "I can just grep for it" | T2 `nx memory` first if it\'s a project decision; T3 if it\'s general. |\n| "I\'ll just answer this quickly" | Verb-shape question? → `nx_answer`. Even quick answers benefit from composed retrieval. |\n| "I know what that means" | Knowing the concept ≠ knowing this project\'s history with it. Check T2/T3. |\n| "This finding isn\'t worth storing" | Findings not stored are findings lost. The next session will redo your work. |\n'


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
