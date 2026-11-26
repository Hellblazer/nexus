# SPDX-License-Identifier: AGPL-3.0-or-later
"""T5 and the SubagentStart half of the audience model (nexus-sby49.3; reworked in nexus-sby49.4).

``_ORCH`` is one constant per row. Completion, Inbox and Search scope are every dispatch's; the Git row goes where the project
or the user turned the strict subagent git guard on (``subagent_git``); the brief check is the four generic questions for every
implementer dispatch plus the project's own ``brief_questions``; the Preflight row is the project's ``preflight`` command, and
the Phase Boundary Gate block has one closing sentence for everyone. Nothing asks who the user is.

Pure functions only, so none of this mints a T2 tenant (tests/hooks/test_subagent_start_hook.py does, per test).
"""
from __future__ import annotations

import re

import pytest

from nexus.hooks import subagent_start

_ORCH_ROWS = (
    "\n## Orchestration (Required)\n\n"
    "| Directive | Rule |\n"
    "|-----------|------|\n"
    "| Completion | Background: SubagentHandback (or SendMessage) full result to main "
    "before idling: success/failure/blocked + live task ids. Foreground: final message "
    "IS the hand-back. |\n"
    "| Inbox | Re-check inbox right before composing any hand-back; newest directive "
    "wins |\n"
)
_GIT_ROW = (
    "| Git | Shared tree: NEVER git add/commit (a hook enforces this; linked worktrees "
    "exempt). Hand back diffs+paths; orchestrator commits pathspec-limited |\n"
)
_SEARCH_ROW = (
    "| Search scope | Search only under the roots your brief names, or your working "
    "directory; never a whole-disk or whole-home search (`find /`, `find ~`, `grep -r /`, "
    "`mdfind`). A file you cannot find: hand back naming it |\n"
)
_PLAIN_ORCH = _ORCH_ROWS + _SEARCH_ROW
_STRICT_ORCH = _ORCH_ROWS + _GIT_ROW + _SEARCH_ROW

_GENERIC_BRIEF_CHECK = (
    "\n## Brief check (if you change code)\n\n"
    "Before your first edit, find your brief's answer to each question. Where "
    "it has none or says UNKNOWN, resolve it first (read the code, measure, or "
    "ask the dispatcher) and put the answers at the top of your hand-back. If "
    "you cannot resolve one, stop and report it; do not guess.\n"
    "1. Consumers: every caller and consumer of what you change, by name, and "
    "which of them filter, re-rank or page AFTER it.\n"
    "2. Final point: where the output's real final order or display is decided "
    "(after boosts, caps, paging), and that your change acts there.\n"
    "3. Bounds: which pool, permit, memory or thread limit the change draws on, "
    "and its size against that limit on small and large boxes.\n"
    "4. State: every cache or shared state you touch, who invalidates it, the "
    "invalidate-vs-in-flight-fill race, the first-write and empty cases, and "
    "the test reset hooks.\n"
)


def test_the_phase_gate_has_one_closing_sentence_for_everyone(tmp_path, project_strict_repo, src_user_strict) -> None:
    for cwd in (tmp_path, project_strict_repo):
        text = subagent_start._phase_gate({"cwd": str(cwd)})
        assert text == subagent_start._PHASE_GATE + subagent_start._PHASE_GATE_NEUTRAL
        assert "RDR-112" not in text and "nexus-52lb" not in text and "dropped item" in text
    assert not hasattr(subagent_start, "_PHASE_GATE_INCIDENT")


def test_a_plain_session_gets_the_table_with_the_search_scope_row_and_no_git_row(tmp_path) -> None:
    table = subagent_start._orchestration({"cwd": str(tmp_path)})
    assert table == _PLAIN_ORCH
    assert "NEVER git" not in table and "Search scope" in table


def test_a_project_that_turns_the_strict_guard_on_gets_the_git_row(project_strict_repo) -> None:
    assert subagent_start._orchestration({"cwd": str(project_strict_repo)}) == _STRICT_ORCH


def test_a_user_who_turns_the_strict_guard_on_gets_the_git_row_in_any_project(src_user_strict, tmp_path) -> None:
    assert subagent_start._orchestration({"cwd": str(tmp_path)}) == _STRICT_ORCH


def test_a_project_that_asks_for_the_seatbelt_does_not_get_the_git_row(project_settings_repo) -> None:
    repo = project_settings_repo(subagent_git="seatbelt")
    assert subagent_start._orchestration({"cwd": str(repo)}) == _PLAIN_ORCH


def test_every_implementer_dispatch_gets_the_four_generic_questions(tmp_path) -> None:
    assert subagent_start._brief_check({"cwd": str(tmp_path)}) == _GENERIC_BRIEF_CHECK


def test_a_project_adds_its_own_questions_after_the_generic_ones(project_settings_repo) -> None:
    repo = project_settings_repo(brief_questions=["Does it hold on every platform we ship?", "Is the migration reversible?\n"])
    text = subagent_start._brief_check({"cwd": str(repo)})
    assert text.startswith(_GENERIC_BRIEF_CHECK)
    assert text.endswith("5. Does it hold on every platform we ship?\n6. Is the migration reversible?\n")
    assert re.findall(r"^(\d)\. ", text, re.MULTILINE) == ["1", "2", "3", "4", "5", "6"]


def test_no_session_is_told_about_local_and_cloud_unless_its_project_asks() -> None:
    assert not hasattr(subagent_start, "_BRIEF_Q_MODES")
    assert "local AND cloud" not in subagent_start._brief_check({"cwd": "/nonexistent"})


def test_the_preflight_row_is_the_projects_command_and_only_then(project_settings_repo, tmp_path) -> None:
    assert subagent_start._worktree_preflight_row({"cwd": str(tmp_path)}) == ""
    repo = project_settings_repo(preflight="tools/preflight.sh")
    row = subagent_start._worktree_preflight_row({"cwd": str(repo)})
    assert row == (
        "| Preflight | If dispatched with isolation:worktree: run `tools/preflight.sh` as your FIRST action; "
        "stop and report if it prints PREFLIGHT_FAIL or exits non-zero |\n"
    )
    assert row.startswith("| ") and row.endswith(" |\n") and row.count("\n") == 1


def test_every_row_constant_is_a_whole_table_row() -> None:
    """A row that lost its newline would run into the next one in the table."""
    for name in ("_ORCH_COMPLETION", "_ORCH_INBOX", "_ORCH_GIT", "_ORCH_SEARCH_SCOPE"):
        row = getattr(subagent_start, name)
        assert row.startswith("| ") and row.endswith(" |\n") and row.count("\n") == 1, name


# --- T8: a broken helper never thins the strict text ---------------------------------------------------------


def test_a_helper_that_cannot_import_gives_the_strict_table_and_no_project_text(tmp_path) -> None:
    """The module-level fallbacks: strict mode (the Git row), no project text. A broken ``_audience`` restores the full guard's
    row, never a thinner one, and adds nothing the project did not write."""
    from tests._audience_fixtures import broken_audience_import  # noqa: PLC0415

    with broken_audience_import(subagent_start) as reloaded:
        assert reloaded._guard_mode({"cwd": str(tmp_path)}) == "strict"
        assert reloaded._project_text({"cwd": str(tmp_path)}, "preflight") is None
        assert reloaded._project_brief_questions({"cwd": str(tmp_path)}) == []
        assert reloaded._orchestration({"cwd": str(tmp_path)}) == _STRICT_ORCH


def test_a_reader_that_raises_gives_the_strict_table(monkeypatch, tmp_path) -> None:
    from nexus.hooks import _audience

    def boom(*_a, **_k):
        raise RuntimeError("helper broke")

    monkeypatch.setattr(_audience, "account_home", boom)
    monkeypatch.setattr(_audience, "_payload_cwd", boom)
    assert subagent_start._orchestration({"cwd": str(tmp_path)}) == _STRICT_ORCH


# --- D10: the relay's Bead field; I3: no bead or RDR ids in any text ------------------------------------------


def test_the_relay_bead_field_keeps_its_name_and_takes_a_tracking_id() -> None:
    relay = subagent_start._RELAY
    assert "| Bead | Bead ID with status, a tracking id, or 'none' |" in relay
    assert "| Task |" in relay and "| Input Artifacts |" in relay


#: What every fixture of the behaviour leg forbids in any output (plan section 5 item 2); ``orchestrator`` is process vocabulary.
_LEXICON = re.compile(
    r"\bnexus-[a-z0-9]{4,}\b|RDR-\d+|\bSam\b|\bHal\b|develop/main|lab-remote|lab-run"
    r"|\bchas\b|\bqwent\b|\bhellmini\b|Hellblazer/nexus|hook-ENFORCED|local AND cloud"
)

_EVERY_CONSTANT = [
    "_RELAY", "_ORCH_HEAD", "_ORCH_COMPLETION", "_ORCH_INBOX", "_ORCH_GIT", "_ORCH_SEARCH_SCOPE", "_NX_T1", "_NX_TIERS",
    "_NX_TOOLS", "_NX_T3", "_NX_ANSWER", "_NX_AUTOLINK", "_PHASE_GATE", "_PHASE_GATE_NEUTRAL", "_OPERATORS", "_CATALOG_TOOLS",
    "_CATALOG_LINKING", "_BRIEF_CHECK_HEAD", "_BRIEF_Q_CONSUMERS", "_BRIEF_Q_FINAL_POINT", "_BRIEF_Q_BOUNDS", "_BRIEF_Q_STATE",
]


@pytest.mark.parametrize("name", _EVERY_CONSTANT)
def test_a_constant_carries_no_internal_name(name: str) -> None:
    text = getattr(subagent_start, name)
    assert _LEXICON.search(text) is None, f"{name}: {_LEXICON.search(text).group(0)!r}"


def test_the_whole_injection_carries_no_internal_name_under_every_agent_shape(tmp_path, monkeypatch) -> None:
    """Every constant a dispatch can reach, assembled the way ``run`` assembles it, under every agent shape, in a plain project
    and in a project that sets every key.

    The live sections (T2 scan, linked RDRs, Knowledge Map, T1 index) are stubbed: they carry the user's own data,
    and the test must reach no engine."""
    import json

    monkeypatch.setattr(subagent_start, "_t2_memory_section", lambda _root: "")
    monkeypatch.setattr(subagent_start, "_linked_rdrs_section", lambda *_a: "")
    monkeypatch.setattr(subagent_start, "_knowledge_map_section", lambda _root: "")
    monkeypatch.setattr(subagent_start, "_t1_scratch_section", lambda _env: "")
    monkeypatch.setattr(subagent_start.subagent_work, "register_start", lambda _payload: None)
    monkeypatch.setattr(subagent_start, "_repo_root", lambda _env: "")

    for agent_type in ("general-purpose", "conexus:developer", "Explore", "conexus:code-review-expert"):
        result = subagent_start.run({
            "session_id": "s", "agent_id": "a1", "agent_type": agent_type, "cwd": str(tmp_path),
            "task": "close phase review gate for the catalog and knowledge",
        })
        ctx = json.loads(result.stdout)["hookSpecificOutput"]["additionalContext"]
        hit = _LEXICON.search(ctx)
        assert hit is None, f"{agent_type}: {hit.group(0)!r} in a SubagentStart text"
