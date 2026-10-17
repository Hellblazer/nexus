# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (c) 2026 Hal Hildebrand. All rights reserved.
"""nexus-h33x8.4/.5, nexus-dg7i6.6: SessionStart guidance imperative -- Tier B
delivery, the byte budget, and the destinations it names.

Pins:

1. Byte budget + imperative-first (nexus-h33x8.5 VERIFICATION 1):
   ``GUIDANCE_IMPERATIVE`` is the SHORT SessionStart-only form -- imperative,
   trigger conditions, and one short destination per condition -- under its
   budget with the imperative sentence within the first 500 bytes.
   ``TestGuidanceByteBudgetIntegration`` in test_hooks.py pins the same
   property end-to-end through ``session_start()``.
2. Destinations (nexus-dg7i6.6): ``using-nx-skills`` was retired, so the text
   no longer points at a skill the reader must load first. Every skill or
   agent it names must exist, and every trigger condition must still carry its
   destination. The routing menu it used to hold lives in
   ``conexus/skills/orchestration/reference.md``; every destination the
   SessionStart channel ever named stays reachable from there
   (``test_all_destinations_still_reachable_from_the_routing_reference``).
3. ``legacy_cat_channel_active`` / ``guidance_block``: the interim
   double-emission guard required by nexus-h33x8.4's item 4 -- suppress the
   wheel-side emission whenever the installed (pinned) plugin's own
   ``hooks.json`` still carries the legacy ``cat`` entry, fail OPEN (emit)
   whenever the check cannot be completed.
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
SKILLS_DIR = REPO_ROOT / "conexus" / "skills"
AGENTS_DIR = REPO_ROOT / "conexus" / "agents"
ROUTING_MD = SKILLS_DIR / "orchestration" / "reference.md"

#: Budget for the SessionStart-only imperative (nexus-h33x8.5 VERIFICATION
#: 1: SessionStart total under 6,000 bytes). This constant carries only
#: the ``nx hook session-start`` emitter's own share — ``session_start()``
#: also prefixes "Nexus ready (session: ...)." (~40-70 bytes) and an
#: occasional best-effort stale-process NOTE — so 1,500 leaves headroom
#: for both while keeping this the dominant, hard-pinned share of the
#: budget the bead actually controls. The current text measures 1,135
#: bytes (2026-10-17, nexus-dg7i6.6); a regression toward the old ~7,500-byte routing-
#: menu copy would trip this long before the shared total budget did.
_IMPERATIVE_BUDGET_BYTES = 1500

#: The routing destinations the SessionStart channel named at nexus-h33x8.4
#: landing (2026-08-08), before the .5 content inversion: 36 ``/conexus:*``
#: commands + 3 ``mcp__...`` tool tokens. Frozen here (not re-derived from the
#: commit history) as the VERIFICATION 4 baseline: every one of them must stay
#: reachable from the routing reference even though none survive in the short
#: SessionStart form.
#:
#: nexus-dg7i6.2: nine command destinations became user-only
#: (``disable-model-invocation: true``) and the routing named the skill each
#: wrapped; ``/conexus:rdr-list`` became the CLI line ``nx rdr preamble
#: rdr-list`` (pinned by ``TestModelHiddenCommands``).
#: nexus-dg7i6.6: five of those skills (code-review, debugging, development,
#: query, test-validation) were retired as wrappers the model never loaded. Their
#: rows are gone: the agents they wrapped are dispatched directly (named in the
#: reference's routing tables and in ``GUIDANCE_IMPERATIVE``), and ``nx_answer``
#: replaced ``/conexus:query``. 33 rows remain.
_PRE_INVERSION_DESTINATIONS = frozenset(
    {
        "/conexus:analyze",
        "/conexus:codebase-analysis",  # was /conexus:analyze-code (nexus-dg7i6.2)
        "/conexus:architecture",
        "/conexus:brainstorming-gate",
        "/conexus:catalog",
        "/conexus:cli-controller",
        "/conexus:strategic-planning",  # was /conexus:create-plan (nexus-dg7i6.2)
        "/conexus:deep-analysis",
        "/conexus:document",
        "/conexus:enrich-plan",
        "/conexus:finishing-branch",
        "/conexus:git-worktrees",
        # nexus-cnzei.4: /conexus:knowledge-tidy (command) was merged into the
        # /conexus:knowledge-tidying skill, the sole entry point now.
        "/conexus:knowledge-tidying",
        "/conexus:nexus",
        "/conexus:pdf-process",
        "/conexus:phase-review-gate",
        "/conexus:plan-validation",  # was /conexus:plan-audit (nexus-dg7i6.2)
        "/conexus:rdr-accept",
        "/conexus:rdr-audit",
        "/conexus:rdr-close",
        "/conexus:rdr-create",
        "/conexus:rdr-gate",
        "/conexus:rdr-research",
        "/conexus:rdr-show",
        "/conexus:receiving-review",
        "/conexus:research-synthesis",  # was /conexus:research (nexus-dg7i6.2)
        # nexus-cnzei.4: the bare-verb "review" skill was renamed to
        # decision-drift-review. Destination moved, not dropped.
        "/conexus:decision-drift-review",
        "/conexus:serena-code-nav",
        "/conexus:substantive-critique",
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
    assert "Conexus skills and agents carry this project's accumulated practice" in head


def test_guidance_imperative_carries_no_routing_destinations():
    """The inversion's whole point: the command-spelled destination LIST
    stays off this channel. No ``/conexus:*`` spelling and no ``mcp__`` tool
    token appears in the short SessionStart form; it names a handful of
    agents and skills in bare form. The full list is in the routing
    reference (see test_all_destinations_still_reachable_from_the_routing_reference)."""
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


# ── nexus-dg7i6.6: destinations ─────────────────────────────────────────────

#: (trigger-condition phrase, destination token that must sit in the same text).
#: Each destination is a skill directory, an agent file, or an MCP tool the
#: routing reference names. Do not "fix" a red run by deleting a row: either the
#: text lost a destination or a destination was renamed.
_TRIGGER_DESTINATIONS: tuple[tuple[str, str], ...] = (
    ("failed fix attempt", "debugger"),
    ("no design of record", "brainstorming-gate"),
    ("cross-module", "strategic-planning"),
    ("quality gate", "code-review-expert"),
    ("quality gate", "substantive-critic"),
    ("quality gate", "test-validator"),
    ("quality gate", "plan-validation"),
    ("reduced from many documents", "nx_answer"),
    ("t1/t2/t3", "memory_search"),
    ("go unstored", "store_put"),
    ("phase/rdr boundary", "phase-review-gate"),
)


def _destination_exists(token: str) -> bool:
    return (
        (SKILLS_DIR / token / "SKILL.md").is_file()
        or (AGENTS_DIR / f"{token}.md").is_file()
        or token in ROUTING_MD.read_text(encoding="utf-8")
    )


def test_every_trigger_condition_still_carries_its_destination():
    lowered = GUIDANCE_IMPERATIVE.lower()
    for phrase, token in _TRIGGER_DESTINATIONS:
        assert phrase in lowered, (
            f"trigger-condition phrase {phrase!r} is missing from GUIDANCE_IMPERATIVE "
            "(src/nexus/session_start_guidance.py)"
        )
        assert f"`{token}`" in GUIDANCE_IMPERATIVE, (
            f"GUIDANCE_IMPERATIVE no longer names `{token}` for {phrase!r}"
        )
        assert _destination_exists(token), (
            f"`{token}` (named for {phrase!r}) is neither a conexus skill, a conexus agent, "
            "nor a term in orchestration/reference.md: the destination was renamed or retired"
        )


def test_the_routing_skill_it_points_at_exists():
    assert "`orchestration`" in GUIDANCE_IMPERATIVE
    assert (SKILLS_DIR / "orchestration" / "SKILL.md").is_file()
    assert ROUTING_MD.is_file()


def test_guidance_does_not_point_at_a_retired_skill():
    """nexus-dg7i6.6: a 30-day census found zero model loads of these, and an
    eval with a true premise loaded them 0 of 3. They are agents or rules now."""
    for retired in ("using-nx-skills", "`code-review`", "`debugging`", "`development`", "`test-validation`"):
        assert retired not in GUIDANCE_IMPERATIVE, f"{retired} is a retired skill (nexus-dg7i6.6)"
        if retired.startswith("`"):
            assert not (SKILLS_DIR / retired.strip("`")).exists(), f"skills/{retired} came back"
    assert not (SKILLS_DIR / "using-nx-skills").exists()
    assert not (SKILLS_DIR / "query").exists()


def test_all_destinations_still_reachable_from_the_routing_reference():
    """Every destination the SessionStart channel used to name is still
    reachable from the routing reference -- moved, not dropped. A guard against
    a future edit that drops a destination without updating its callers."""
    reachable = _destinations(ROUTING_MD.read_text(encoding="utf-8"))
    missing = _PRE_INVERSION_DESTINATIONS - reachable
    assert not missing, f"destinations dropped from orchestration/reference.md: {sorted(missing)}"
    assert len(_PRE_INVERSION_DESTINATIONS) == 33, "the frozen baseline changed size"


def test_guidance_imperative_is_nonempty():
    assert 200 < len(GUIDANCE_IMPERATIVE) < _IMPERATIVE_BUDGET_BYTES


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
