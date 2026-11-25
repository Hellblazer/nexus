# SPDX-License-Identifier: AGPL-3.0-or-later
"""T5 and the SubagentStart half of the audience split (nexus-sby49.3, plan section 7 Phase 2 change 2).

``_ORCH`` is one constant per row, and each row has an audience: Completion and Inbox are for everyone, the Git row
and the Search scope row are SAM-WIDE (``maintainer()``), and the brief check is SAM-WIDE except its local/cloud
question, which is NEXUS-REPO. A user's injection is the same text minus those rows.

Pure functions only, so none of this mints a T2 tenant (tests/hooks/test_subagent_start_hook.py does, per test).
"""
from __future__ import annotations

import re

import pytest

from nexus.hooks import subagent_start

#: The Orchestration table and the brief check as they read before nexus-sby49.3 (the parent of that commit).
#: A maintainer in a nexus checkout must still get exactly these bytes.
_OLD_ORCH = (
    "\n## Orchestration (Required)\n\n"
    "| Directive | Rule |\n"
    "|-----------|------|\n"
    "| Completion | Background: SubagentHandback (or SendMessage) full result to main "
    "before idling: success/failure/blocked + live task ids. Foreground: final message "
    "IS the hand-back. |\n"
    "| Inbox | Re-check inbox right before composing any hand-back; newest directive "
    "wins |\n"
    "| Git | Shared tree: NEVER git add/commit (hook-ENFORCED; linked worktrees "
    "exempt). Hand back diffs+paths; orchestrator commits pathspec-limited |\n"
    "| Search scope | Search only under the roots your brief names, or your working "
    "directory; never a whole-disk or whole-home search (`find /`, `find ~`, `grep -r /`, "
    "`mdfind`). A file you cannot find: hand back naming it (nexus-egv6q) |\n"
)

_OLD_BRIEF_CHECK = (
    "\n## Brief check (if you change code)\n\n"
    "Before your first edit, find your brief's answer to each question. Where "
    "it has none or says UNKNOWN, resolve it first (read the code, measure, or "
    "ask the dispatcher) and put the answers at the top of your hand-back. If "
    "you cannot resolve one, stop and report it; do not guess.\n"
    "1. Consumers: every caller and consumer of what you change, by name, and "
    "which of them filter, re-rank or page AFTER it.\n"
    "2. Final point: where the output's real final order or display is decided "
    "(after boosts, caps, paging), and that your change acts there.\n"
    "3. Modes: behaviour in local AND cloud mode; a mode nobody measured is "
    "measured first or left unchanged.\n"
    "4. Bounds: which pool, permit, memory or thread limit the change draws on, "
    "and its size against that limit on small and large boxes.\n"
    "5. State: every cache or shared state you touch, who invalidates it, the "
    "invalidate-vs-in-flight-fill race, the first-write and empty cases, and "
    "the test reset hooks.\n"
)


_OLD_PHASE_GATE = (
    "\n## Phase Boundary Gate (mandatory)\n\n"
    "If your task closes a phase-review bead, walk the `phase-review-gate` "
    "skill's checklist BEFORE close: list every numbered item of the phase in "
    "the RDR, and for each write `Item N = <closing bead id>` or `Item N = none` "
    "with a one-line reason. Do not close while an item has neither. Nothing "
    "enforces this; skipping it is the silent-scope-reduction failure mode: "
    "RDR-112 Phase 1 (nexus-52lb) lost days when the T3 daemon drop surfaced "
    "three phases later.\n"
)


def test_a_maintainer_gets_the_old_phase_gate_byte_for_byte(nexus_marked_repo) -> None:
    assert subagent_start._phase_gate({"cwd": str(nexus_marked_repo)}) == _OLD_PHASE_GATE


def test_the_account_flag_alone_gets_the_old_phase_gate_too(src_maintainer_flag, tmp_path) -> None:
    assert subagent_start._phase_gate({"cwd": str(tmp_path)}) == _OLD_PHASE_GATE


def test_a_user_gets_the_phase_gate_with_a_neutral_closing_sentence(tmp_path) -> None:
    text = subagent_start._phase_gate({"cwd": str(tmp_path)})
    assert text.startswith(subagent_start._PHASE_GATE) and text.endswith(subagent_start._PHASE_GATE_NEUTRAL)
    assert "RDR-112" not in text and "nexus-52lb" not in text and "dropped item" in text


def test_a_maintainer_in_nexus_gets_the_old_orchestration_table_byte_for_byte(nexus_marked_repo) -> None:
    assert subagent_start._orchestration({"cwd": str(nexus_marked_repo)}) == _OLD_ORCH


def test_a_maintainer_in_nexus_gets_the_old_brief_check_byte_for_byte(nexus_marked_repo) -> None:
    assert subagent_start._brief_check({"cwd": str(nexus_marked_repo)}) == _OLD_BRIEF_CHECK


def test_the_account_flag_alone_gets_the_table_too(src_maintainer_flag, tmp_path) -> None:
    assert subagent_start._orchestration({"cwd": str(tmp_path)}) == _OLD_ORCH


def test_a_user_gets_the_head_completion_and_inbox_only(tmp_path) -> None:
    table = subagent_start._orchestration({"cwd": str(tmp_path)})
    assert table == (
        subagent_start._ORCH_HEAD + subagent_start._ORCH_COMPLETION + subagent_start._ORCH_INBOX
    )
    assert "NEVER git" not in table and "Search scope" not in table and "orchestrator" not in table


def test_a_user_gets_no_brief_check(tmp_path) -> None:
    assert subagent_start._brief_check({"cwd": str(tmp_path)}) == ""


def test_a_maintainer_outside_nexus_gets_the_brief_check_without_the_modes_question(src_maintainer_flag, tmp_path) -> None:
    text = subagent_start._brief_check({"cwd": str(tmp_path)})
    assert text.startswith(subagent_start._BRIEF_CHECK_HEAD)
    assert "local AND cloud" not in text
    assert re.findall(r"^(\d)\. ", text, re.MULTILINE) == ["1", "2", "3", "4"]


def test_every_row_constant_is_a_whole_table_row() -> None:
    """A row that lost its newline would run into the next one in the maintainer's table."""
    for name in ("_ORCH_COMPLETION", "_ORCH_INBOX", "_ORCH_GIT", "_ORCH_SEARCH_SCOPE"):
        row = getattr(subagent_start, name)
        assert row.startswith("| ") and row.endswith(" |\n") and row.count("\n") == 1, name


# --- T8: a broken helper never thins the maintainer's text ---------------------------------------


def test_a_helper_that_cannot_import_gives_the_maintainer_text_to_everyone(tmp_path) -> None:
    """The module-level fallbacks are True: a broken ``_audience`` restores today's injection, never a thinner one."""
    from tests._audience_fixtures import broken_audience_import  # noqa: PLC0415

    with broken_audience_import(subagent_start) as reloaded:
        assert reloaded._maintainer({"cwd": str(tmp_path)}) is True
        assert reloaded._nexus_repo({"cwd": str(tmp_path)}) is True
        assert reloaded._orchestration({"cwd": str(tmp_path)}) == _OLD_ORCH


def test_a_predicate_that_raises_gives_the_maintainer_text(monkeypatch, tmp_path) -> None:
    from nexus.hooks import _audience

    def boom(*_a, **_k):
        raise RuntimeError("helper broke")

    monkeypatch.setattr(_audience, "account_home", boom)
    monkeypatch.setattr(_audience, "_payload_cwd", boom)
    assert subagent_start._orchestration({"cwd": str(tmp_path)}) == _OLD_ORCH


# --- D10: the relay's Bead field; I3: no bead or RDR ids in a user's text ----------------------------


def test_the_relay_bead_field_keeps_its_name_and_takes_a_tracking_id() -> None:
    relay = subagent_start._RELAY
    assert "| Bead | Bead ID with status, a tracking id, or 'none' |" in relay
    assert "| Task |" in relay and "| Input Artifacts |" in relay


#: What the plain-fixture leg of the behaviour leg forbids in any output a user sees (plan section 5 item 2).
_LEXICON = re.compile(
    r"\bnexus-[a-z0-9]{4,}\b|RDR-\d+|\bSam\b|\bHal\b|orchestrator|develop/main|lab-remote|lab-run"
    r"|\bchas\b|\bqwent\b|\bhellmini\b|Hellblazer/nexus|hook-ENFORCED|local AND cloud"
)

_EVERY_USER_CONSTANTS = [
    "_RELAY", "_ORCH_HEAD", "_ORCH_COMPLETION", "_ORCH_INBOX", "_NX_T1", "_NX_TIERS", "_NX_TOOLS", "_NX_T3",
    "_NX_ANSWER", "_NX_AUTOLINK", "_PHASE_GATE", "_PHASE_GATE_NEUTRAL", "_OPERATORS", "_CATALOG_TOOLS", "_CATALOG_LINKING",
]


@pytest.mark.parametrize("name", _EVERY_USER_CONSTANTS)
def test_an_every_user_constant_carries_no_internal_name(name: str) -> None:
    text = getattr(subagent_start, name)
    assert _LEXICON.search(text) is None, f"{name}: {_LEXICON.search(text).group(0)!r}"


def test_the_whole_user_injection_carries_no_internal_name(tmp_path, monkeypatch) -> None:
    """Every constant a user's dispatch can reach, assembled the way ``run`` assembles it, under every agent shape.

    The live sections (T2 scan, linked RDRs, Knowledge Map, T1 index) are stubbed: they carry the user's own data,
    and the test must reach no engine."""
    import json

    monkeypatch.setattr(subagent_start, "_t2_memory_section", lambda _root: "")
    monkeypatch.setattr(subagent_start, "_linked_rdrs_section", lambda *_a: "")
    monkeypatch.setattr(subagent_start, "_knowledge_map_section", lambda _root: "")
    monkeypatch.setattr(subagent_start, "_t1_scratch_section", lambda _env: "")
    monkeypatch.setattr(subagent_start.subagent_work, "register_start", lambda _payload: None)
    # The worktree Preflight row keys on the script's presence, a NEXUS-REPO fact, and the test process runs in a nexus checkout.
    monkeypatch.setattr(subagent_start, "_repo_root", lambda _env: "")
    monkeypatch.setattr(subagent_start, "_process_cwd", lambda: "")

    for agent_type in ("general-purpose", "conexus:developer", "Explore", "conexus:code-review-expert"):
        result = subagent_start.run({
            "session_id": "s", "agent_id": "a1", "agent_type": agent_type, "cwd": str(tmp_path),
            "task": "close phase review gate for the catalog and knowledge",
        })
        ctx = json.loads(result.stdout)["hookSpecificOutput"]["additionalContext"]
        hit = _LEXICON.search(ctx)
        assert hit is None, f"{agent_type}: {hit.group(0)!r} in a user's SubagentStart text"
