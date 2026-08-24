# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (c) 2026 Hal Hildebrand. All rights reserved.
"""nexus-h33x8.4/.5: SessionStart guidance imperative — Tier B delivery
and the content inversion.

Pins four things:

1. Byte budget + imperative-first (nexus-h33x8.5 VERIFICATION 1):
   ``GUIDANCE_IMPERATIVE`` is the SHORT, SessionStart-only form —
   imperative + trigger conditions, no routing menu — and stays well
   under its budget with the imperative sentence within the first 500
   bytes. This is the mechanical, cheap, non-negotiable check the bead
   calls for; ``TestGuidanceByteBudgetIntegration`` in test_hooks.py
   pins the same property end-to-end through ``session_start()``.
2. No content loss (nexus-h33x8.5 VERIFICATION 4): every routing
   destination (``/conexus:*`` command, MCP tool) that the guidance
   text named before this bead must still be reachable from the Skill
   body (``SKILL.md`` is unchanged in content by this bead — see
   ``test_all_destinations_still_reachable_from_skill_md``).
3. ``GUIDANCE_IMPERATIVE`` intentionally DIVERGES from ``SKILL.md`` as
   of this bead — the old byte-for-byte parity test pinned by
   nexus-h33x8.4 is retired below (that assertion existed exactly to
   be superseded here; see the .4/.5 module docstrings).
4. ``legacy_cat_channel_active`` / ``guidance_block``: the interim
   double-emission guard required by nexus-h33x8.4's item 4 — suppress
   the wheel-side emission whenever the installed (pinned) plugin's own
   ``hooks.json`` still carries the legacy ``cat .../SKILL.md`` entry,
   fail OPEN (emit) whenever the check cannot be completed. Unaffected
   by the .5 content inversion; re-pinned here unchanged.
"""
from __future__ import annotations

import json
import pathlib
import re

from nexus.session_start_guidance import (
    GUIDANCE_IMPERATIVE,
    guidance_block,
    legacy_cat_channel_active,
)

REPO_ROOT = pathlib.Path(__file__).parent.parent
SKILL_MD = REPO_ROOT / "conexus" / "skills" / "using-nx-skills" / "SKILL.md"

#: Budget for the SessionStart-only imperative (nexus-h33x8.5 VERIFICATION
#: 1: SessionStart total under 6,000 bytes). This constant carries only
#: the ``nx hook session-start`` emitter's own share — ``session_start()``
#: also prefixes "Nexus ready (session: ...)." (~40-70 bytes) and an
#: occasional best-effort stale-process NOTE — so 1,500 leaves headroom
#: for both while keeping this the dominant, hard-pinned share of the
#: budget the bead actually controls. The current text measures 1,140
#: bytes (2026-08-20); a regression toward the old ~7,500-byte routing-
#: menu copy would trip this long before the shared total budget did.
_IMPERATIVE_BUDGET_BYTES = 1500

#: The 39 routing destinations named by GUIDANCE_IMPERATIVE as it stood
#: at nexus-h33x8.4 landing (2026-08-08), before the .5 content
#: inversion — 36 ``/conexus:*`` commands + 3 ``mcp__...`` tool tokens.
#: Frozen here (not re-derived from git history) as the VERIFICATION 4
#: baseline: every one of these must still be reachable from SKILL.md
#: after the inversion, even though none of them survive in the short
#: SessionStart form any more.
_PRE_INVERSION_DESTINATIONS = frozenset(
    {
        "/conexus:analyze",
        "/conexus:analyze-code",
        "/conexus:architecture",
        "/conexus:brainstorming-gate",
        "/conexus:catalog",
        "/conexus:cli-controller",
        "/conexus:create-plan",
        "/conexus:debug",
        "/conexus:deep-analysis",
        "/conexus:document",
        "/conexus:enrich-plan",
        "/conexus:finishing-branch",
        "/conexus:git-worktrees",
        "/conexus:implement",
        "/conexus:knowledge-tidy",
        "/conexus:nexus",
        "/conexus:pdf-process",
        "/conexus:phase-review-gate",
        "/conexus:plan-audit",
        "/conexus:query",
        "/conexus:rdr-accept",
        "/conexus:rdr-audit",
        "/conexus:rdr-close",
        "/conexus:rdr-create",
        "/conexus:rdr-gate",
        "/conexus:rdr-list",
        "/conexus:rdr-research",
        "/conexus:rdr-show",
        "/conexus:receiving-review",
        "/conexus:research",
        "/conexus:review",
        "/conexus:review-code",
        "/conexus:serena-code-nav",
        "/conexus:substantive-critique",
        "/conexus:test-validate",
        "/conexus:writing-nx-skills",
        "mcp__plugin_conexus_nexus__plan_save",
        "mcp__plugin_conexus_nexus__plan_search",
        "mcp__plugin_conexus_sequential",
    }
)

_DESTINATION_RE = re.compile(r"/conexus:[a-zA-Z0-9-]+|mcp__[a-zA-Z0-9_]+")


def _destinations(text: str) -> set[str]:
    return set(_DESTINATION_RE.findall(text))


# ── nexus-h33x8.5: byte budget + imperative-first ───────────────────────────


def test_guidance_imperative_under_byte_budget():
    """VERIFICATION 1: the SessionStart-only imperative stays small.
    A regression that re-inlines the routing menu trips this long
    before it could reach the shared 6,000-byte SessionStart total."""
    n = len(GUIDANCE_IMPERATIVE.encode("utf-8"))
    assert n < _IMPERATIVE_BUDGET_BYTES, (
        f"GUIDANCE_IMPERATIVE grew to {n} bytes, budget is "
        f"{_IMPERATIVE_BUDGET_BYTES} — did the routing menu creep back in?"
    )


def test_routing_statement_within_first_500_bytes():
    """VERIFICATION 1: the routing statement, not a preamble, leads.

    TONE CHANGED 2026-08-23 (nexus-dkotg). This asserted the literal
    "You MUST invoke `Skill`". That sentence was measured, not assumed,
    and it did not work: across 6 sandboxed runs of the eval corpus case
    that exercises exactly this (p01 using-nx-skills-generic-turn), the
    SessionStart hook fired, this text reached the model verbatim, and
    a conexus skill was invoked 1 time in 6. Three of those runs made
    ZERO Skill calls and went straight to Bash. The instruction was
    already maximal -- "hard rule, not a hint or a preference",
    "skipping is a defect" -- so writing it harder was the one remedy
    known to fail, because that IS what shipped.

    What this test protects is unchanged: the lead must state why
    routing helps, not preamble around it. Only the sentence it pins
    changed."""
    head = GUIDANCE_IMPERATIVE.encode("utf-8")[:500].decode("utf-8", errors="ignore")
    assert "Conexus skills carry this project's accumulated practice" in head


def test_guidance_imperative_carries_no_routing_destinations():
    """The inversion's whole point: destinations move OFF this channel.
    None of the 39 pre-inversion destinations should appear verbatim in
    the short SessionStart form any more — they're reachable from
    SKILL.md instead (see test_all_destinations_still_reachable_from_skill_md)."""
    assert _destinations(GUIDANCE_IMPERATIVE) == set()


def test_guidance_imperative_retains_trigger_conditions():
    """The "when" stays even though the "what" (destinations) left.
    A handful of the routing table's trigger conditions, reworded as
    conditions rather than destinations, must still be present so an
    agent recognizes it should route even without the menu."""
    lowered = GUIDANCE_IMPERATIVE.lower()
    for phrase in (
        "no design of record",
        "failed fix attempt",
        "quality gate",
        "reduced from many documents",
        "t1/t2/t3",
    ):
        assert phrase in lowered, f"trigger condition {phrase!r} missing from GUIDANCE_IMPERATIVE"


# ── nexus-h33x8.5 VERIFICATION 4: no content loss ───────────────────────────


def test_all_destinations_still_reachable_from_skill_md():
    """Every destination the SessionStart channel used to name is still
    reachable from the Skill body — moved, not dropped. SKILL.md's
    routing content is unchanged by this bead (only a pointer sentence
    was added), so this is currently a tautology by construction; it
    stands as a regression guard against a FUTURE SKILL.md edit that
    silently drops a destination without updating the callers."""
    skill_destinations = _destinations(SKILL_MD.read_text(encoding="utf-8"))
    missing = _PRE_INVERSION_DESTINATIONS - skill_destinations
    assert not missing, f"destinations dropped from SKILL.md: {sorted(missing)}"


def test_guidance_imperative_diverges_from_skill_md():
    """The old nexus-h33x8.4 byte-for-byte parity assertion is
    RETIRED here, by design (see module docstring item 3) — this bead
    is exactly the intentional divergence that assertion's own comment
    predicted. Pin the divergence instead of the old equality."""
    assert GUIDANCE_IMPERATIVE != SKILL_MD.read_text(encoding="utf-8")
    assert len(GUIDANCE_IMPERATIVE) < len(SKILL_MD.read_text(encoding="utf-8")) / 3


def test_guidance_imperative_is_nonempty():
    assert 200 < len(GUIDANCE_IMPERATIVE) < _IMPERATIVE_BUDGET_BYTES


# ── nexus-h33x8.5 fix-pass: bidirectional drift guard ───────────────────────
#
# The critic's Significant (T2 nexus/substantive-critique-h33x8.3-.5-2026-08-20,
# [22935]): once the byte-parity pin was retired, GUIDANCE_IMPERATIVE (wheel)
# and SKILL.md (plugin) can drift independently, and only ONE direction was
# guarded (test_all_destinations_still_reachable_from_skill_md checks SKILL.md
# doesn't silently drop a destination; nothing forced GUIDANCE_IMPERATIVE's
# trigger-condition bullets to stay anchored to SKILL.md's routing content, or
# vice versa). This section closes both directions for the imperative
# sentence itself and for each trigger-condition bullet's anchor phrase(s) in
# SKILL.md's routing content -- a content edit on EITHER side that breaks the
# pairing fails here, naming the OTHER file in the assertion message.

#: (GUIDANCE_IMPERATIVE trigger-bullet phrase, SKILL.md anchor phrase(s) that
#: back it). Each anchor is a verbatim substring of the CURRENT SKILL.md
#: (verified at authoring time); a future edit to either side that breaks
#: the pairing is exactly what this test exists to catch, so do not "fix" a
#: red run here by deleting a row -- fix the drift the row is reporting.
_TRIGGER_ANCHOR_PAIRS: tuple[tuple[str, tuple[str, ...]], ...] = (
    ("no design of record", ("brainstorming-gate",)),
    ("failed fix attempt", ("/conexus:debug",)),
    ("cross-module design", ("/conexus:architecture", "/conexus:create-plan")),
    ("quality gate", ("Quality gates",)),
    ("reduced from many documents", ("REDUCED FROM MANY DOCUMENTS",)),
    ("t1/t2/t3", ("Conexus Storage Tiers",)),
    ("finding is about to go unstored", ("Findings not stored are findings lost",)),
    ("phase/rdr boundary", ("RDR lifecycle", "phase-review-gate")),
)


def test_imperative_sentence_present_in_both_files():
    """The one sentence that must never drift apart: GUIDANCE_IMPERATIVE's
    hard-rule imperative and SKILL.md's own copy of it (SKILL.md line 9,
    unchanged by this bead)."""
    imperative = (
        "Conexus skills carry this project's accumulated practice for "
        "specific situations"
    )
    skill_text = SKILL_MD.read_text(encoding="utf-8")
    assert imperative in GUIDANCE_IMPERATIVE, (
        "the imperative sentence is missing from GUIDANCE_IMPERATIVE "
        "(src/nexus/session_start_guidance.py) -- SKILL.md still has it, "
        "the wheel-delivered SessionStart text has drifted"
    )
    assert imperative in skill_text, (
        "the imperative sentence is missing from "
        "conexus/skills/using-nx-skills/SKILL.md -- GUIDANCE_IMPERATIVE "
        "still has it, the Skill body has drifted"
    )


def test_trigger_condition_anchors_stay_paired_with_skill_md():
    """For each GUIDANCE_IMPERATIVE trigger-condition bullet, its SKILL.md
    anchor phrase(s) must still exist -- catches a FUTURE SKILL.md edit
    that silently drops or renames the routing content a SessionStart
    trigger bullet refers agents onward to."""
    skill_text = SKILL_MD.read_text(encoding="utf-8")
    lowered_guidance = GUIDANCE_IMPERATIVE.lower()
    for phrase, anchors in _TRIGGER_ANCHOR_PAIRS:
        assert phrase in lowered_guidance, (
            f"trigger-condition phrase {phrase!r} is missing from "
            "GUIDANCE_IMPERATIVE (src/nexus/session_start_guidance.py) -- "
            "either it was reworded (update this row's phrase to match) or "
            "dropped (SKILL.md's matching routing content is now unreferenced "
            "at SessionStart)"
        )
        assert any(anchor in skill_text for anchor in anchors), (
            f"none of SKILL.md's anchor phrase(s) {anchors!r} for "
            f"GUIDANCE_IMPERATIVE trigger {phrase!r} were found in "
            "conexus/skills/using-nx-skills/SKILL.md -- the routing content "
            "that trigger points agents toward has moved or been dropped; "
            "update either SKILL.md or this row's anchor list"
        )


# ── legacy_cat_channel_active: the interim double-emission guard ───────────


def _write_hooks_json(tmp_path: pathlib.Path, *, session_start_commands: list[str]) -> pathlib.Path:
    hooks_dir = tmp_path / "hooks"
    hooks_dir.mkdir(parents=True, exist_ok=True)
    payload = {
        "hooks": {
            "SessionStart": [
                {
                    "matcher": "startup|resume|clear|compact",
                    "hooks": [
                        {"type": "command", "command": cmd, "timeout": 10}
                        for cmd in session_start_commands
                    ],
                }
            ]
        }
    }
    (hooks_dir / "hooks.json").write_text(json.dumps(payload))
    return tmp_path


class TestLegacyCatChannelActive:
    def test_no_plugin_root_fails_open(self, monkeypatch):
        monkeypatch.delenv("CLAUDE_PLUGIN_ROOT", raising=False)
        assert legacy_cat_channel_active() is False

    def test_explicit_none_root_and_no_env_fails_open(self, monkeypatch):
        monkeypatch.delenv("CLAUDE_PLUGIN_ROOT", raising=False)
        assert legacy_cat_channel_active(None) is False

    def test_legacy_entry_present_reports_active(self, tmp_path):
        root = _write_hooks_json(
            tmp_path,
            session_start_commands=[
                "nx upgrade --auto",
                "nx hook session-start",
                "cat $CLAUDE_PLUGIN_ROOT/skills/using-nx-skills/SKILL.md",
            ],
        )
        assert legacy_cat_channel_active(str(root)) is True

    def test_legacy_entry_absent_reports_inactive(self, tmp_path):
        root = _write_hooks_json(
            tmp_path,
            session_start_commands=[
                "nx upgrade --auto",
                "nx hook session-start",
            ],
        )
        assert legacy_cat_channel_active(str(root)) is False

    def test_missing_hooks_json_fails_open(self, tmp_path):
        # tmp_path has no hooks/hooks.json at all.
        assert legacy_cat_channel_active(str(tmp_path)) is False

    def test_malformed_hooks_json_fails_open(self, tmp_path):
        hooks_dir = tmp_path / "hooks"
        hooks_dir.mkdir(parents=True)
        (hooks_dir / "hooks.json").write_text("not json {{")
        assert legacy_cat_channel_active(str(tmp_path)) is False

    def test_hooks_json_not_a_dict_fails_open(self, tmp_path):
        hooks_dir = tmp_path / "hooks"
        hooks_dir.mkdir(parents=True)
        (hooks_dir / "hooks.json").write_text("[1, 2, 3]")
        assert legacy_cat_channel_active(str(tmp_path)) is False

    def test_env_var_used_when_no_explicit_root(self, tmp_path, monkeypatch):
        root = _write_hooks_json(
            tmp_path,
            session_start_commands=[
                "cat $CLAUDE_PLUGIN_ROOT/skills/using-nx-skills/SKILL.md",
            ],
        )
        monkeypatch.setenv("CLAUDE_PLUGIN_ROOT", str(root))
        assert legacy_cat_channel_active() is True


# ── guidance_block: the actual emission gate ────────────────────────────────


class TestGuidanceBlock:
    def test_emits_full_text_when_legacy_channel_absent(self, tmp_path):
        root = _write_hooks_json(tmp_path, session_start_commands=["nx hook session-start"])
        assert guidance_block(str(root)) == GUIDANCE_IMPERATIVE

    def test_suppressed_when_legacy_channel_present(self, tmp_path):
        root = _write_hooks_json(
            tmp_path,
            session_start_commands=[
                "cat $CLAUDE_PLUGIN_ROOT/skills/using-nx-skills/SKILL.md",
            ],
        )
        assert guidance_block(str(root)) == ""

    def test_emits_when_no_plugin_root_at_all(self, monkeypatch):
        """Bare CLI / dev / test invocation: no legacy channel could
        possibly be running, so fail-open means emit."""
        monkeypatch.delenv("CLAUDE_PLUGIN_ROOT", raising=False)
        assert guidance_block() == GUIDANCE_IMPERATIVE
