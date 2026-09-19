# SPDX-License-Identifier: AGPL-3.0-or-later
"""The SessionStart behaviour census hook (conexus/hooks/scripts/behaviour_census.py).

Every case runs the REAL script as a subprocess against a real transcript
directory. Deliberately not a reimplementation of its counting: a gate whose
domain excludes the shipped artifact proves only that the gate's own copy
works (nexus-01, 2026-09-19, on
``test_a_stdlib_only_verb_dispatch_never_loads_structlog`` passing green while
every real verb regressed, because it dispatched a synthetic verb written into
tmp_path).

The census reports RAW COUNTS and explicitly refuses to emit a compliance
verdict (T3 "Design: restoring the nexus agent tool-guidance layer",
2026-07-31): a tool call absent from a transcript looks identical whether it
was forgotten or correctly rejected, so no percentage, ratio, or coverage
figure appears anywhere in its output. These tests pin that absence as hard
as they pin the presence of the raw counts.
"""
from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path

import pytest

SCRIPT = Path(__file__).resolve().parents[2] / "conexus" / "hooks" / "scripts" / "behaviour_census.py"


def _tool_use(name: str, command: str | None = None) -> str:
    block: dict = {"type": "tool_use", "name": name, "input": {}}
    if command is not None:
        block["input"]["command"] = command
    return json.dumps({"isSidechain": False, "message": {"role": "assistant", "content": [block]}})


def _thought() -> str:
    return _tool_use("mcp__plugin_conexus_sequential-thinking__sequentialthinking")


def _run(transcript_path: Path, session_id: str = "current") -> subprocess.CompletedProcess[str]:
    payload = json.dumps({"session_id": session_id, "transcript_path": str(transcript_path)})
    return subprocess.run(
        [sys.executable, str(SCRIPT)],
        input=payload, capture_output=True, text=True, timeout=60, check=False,
    )


def test_the_script_ships_where_the_hook_declares_it() -> None:
    assert SCRIPT.is_file(), f"hooks.json declares {SCRIPT.name}; it must exist"


def test_no_prior_session_is_silent(tmp_path: Path) -> None:
    """A first-ever session has nothing to report and must not say so loudly."""
    cur = tmp_path / "current.jsonl"
    cur.write_text("")
    r = _run(cur)
    assert r.returncode == 0
    assert r.stdout.strip() == ""


def test_a_prior_transcript_with_no_tool_calls_says_skipped_not_zero(tmp_path: Path) -> None:
    """The non-vacuity assert. A parse that saw nothing is not a session that
    did nothing, and a 0-valued count would read identically to one that was
    genuinely measured."""
    (tmp_path / "prior.jsonl").write_text(
        json.dumps({"type": "user", "message": {"role": "user", "content": "hi"}}) + "\n"
    )
    cur = tmp_path / "current.jsonl"
    cur.write_text("")
    r = _run(cur)
    assert r.returncode == 0
    assert "skipped, not zero" in r.stdout
    assert "%" not in r.stdout


def test_it_reports_raw_counts_and_distances_from_a_real_transcript(tmp_path: Path) -> None:
    lines = [
        _thought(),
        _tool_use("Edit"),                       # decision, distance 1 from the thought
        _tool_use("Agent"),                      # decision, distance 2
        _tool_use("Bash", "ls -la"),              # hands-on, NOT a decision
        _tool_use("Bash", "git commit -m x"),     # decision, distance 4 (ls -la counts too)
    ]
    (tmp_path / "prior.jsonl").write_text("\n".join(lines) + "\n")
    cur = tmp_path / "current.jsonl"
    cur.write_text("")
    r = _run(cur)
    assert r.returncode == 0, r.stderr
    # 1 thought, 3 decisions (Edit, Agent, git commit) -- "ls -la" is not one.
    assert "1 thought(s), 3 state-mutating action(s)" in r.stdout
    # distances [1, 2, 4]: median 2, p25 1.5, p75 3. Every decision here
    # followed the one thought, so 0 are ungrounded.
    assert "median 2, p25 1.5, p75 3" in r.stdout
    assert "3 of 3 action(s) had one" in r.stdout
    assert "had no prior thought" not in r.stdout


def test_a_decision_far_past_the_old_six_call_window_is_reported_at_its_true_distance(
    tmp_path: Path,
) -> None:
    """The shipped bug: ``since_thought`` only advanced on a DECISION, so a
    single stub thought made an unbounded run of intervening non-decision
    calls invisible -- a decision 201 tool calls after the only thought in
    the transcript was reported as if the thought had just preceded it. The
    fix counts every tool call, so the true distance (201, not <= 6) is what
    a human reads."""
    lines = [_thought()] + [_tool_use("Read") for _ in range(200)] + [_tool_use("Edit")]
    (tmp_path / "prior.jsonl").write_text("\n".join(lines) + "\n")
    cur = tmp_path / "current.jsonl"
    cur.write_text("")
    r = _run(cur)
    assert r.returncode == 0, r.stderr
    assert "1 thought(s), 1 state-mutating action(s)" in r.stdout
    assert "median 201, p25 201, p75 201" in r.stdout
    assert "1 of 1 action(s) had one" in r.stdout


def test_a_decision_with_no_preceding_thought_at_all_is_reported_as_ungrounded(
    tmp_path: Path,
) -> None:
    lines = [_tool_use("Edit")]  # no thought anywhere in the transcript
    (tmp_path / "prior.jsonl").write_text("\n".join(lines) + "\n")
    cur = tmp_path / "current.jsonl"
    cur.write_text("")
    r = _run(cur)
    assert r.returncode == 0, r.stderr
    assert "0 thought(s), 1 state-mutating action(s)" in r.stdout
    assert "1 action(s) had no prior thought" in r.stdout
    # nothing to compute a distance distribution over
    assert "median" not in r.stdout


NEWLY_ADDED_DECISION_SHAPES = [
    pytest.param("Task", None, id="agent-dispatch-task"),
    pytest.param("SendMessage", None, id="cross-session-sendmessage"),
    pytest.param("mailbox_send", None, id="cross-session-mailbox-send"),
    pytest.param("memory_put", None, id="permanent-write-memory-put"),
    pytest.param("store_put", None, id="permanent-write-store-put"),
    pytest.param("AskUserQuestion", None, id="human-blocking-askuserquestion"),
    pytest.param("Bash", "git reset --hard HEAD~1", id="bash-git-reset"),
    pytest.param("Bash", "git rebase -i HEAD~3", id="bash-git-rebase"),
    pytest.param("Bash", "git checkout other-branch", id="bash-git-checkout"),
    pytest.param("Bash", "git merge --no-ff feature", id="bash-git-merge"),
    pytest.param("Bash", "bd update nexus-123 --status=in_progress", id="bash-bd-update"),
    pytest.param("Bash", "gh pr create --title x --body y", id="bash-gh-pr-create"),
    pytest.param("Bash", "gh pr merge 123 --merge", id="bash-gh-pr-merge"),
    pytest.param("Bash", "uv run pytest -q", id="bash-uv-run"),
    pytest.param("Bash", "sed -i '' 's/a/b/' file.py", id="bash-sed-in-place"),
    pytest.param("Bash", "cat > out.txt <<'EOF'\nhello\nEOF", id="bash-cat-redirect-heredoc-write"),
    pytest.param("Bash", "tee out.txt <<'EOF'\nhello\nEOF", id="bash-tee-heredoc-write"),
]


@pytest.mark.parametrize("tool_name,command", NEWLY_ADDED_DECISION_SHAPES)
def test_the_decision_set_includes_each_newly_added_shape(
    tmp_path: Path, tool_name: str, command: str | None
) -> None:
    """Each of these was previously invisible to DECISION / DECISION_BASH and
    would have been silently absent from the count (nexus-01, measured
    2026-09-19: the narrow set admitted ~436 of 4,653 real tool calls)."""
    lines = [_thought(), _tool_use(tool_name, command)]
    (tmp_path / "prior.jsonl").write_text("\n".join(lines) + "\n")
    cur = tmp_path / "current.jsonl"
    cur.write_text("")
    r = _run(cur)
    assert r.returncode == 0, r.stderr
    assert "1 thought(s), 1 state-mutating action(s)" in r.stdout, (
        f"{tool_name!r} / {command!r} was not recognized as a decision: {r.stdout!r}"
    )


NOT_DECISIONS = [
    pytest.param("Bash", "ls -la", id="bash-plain-list"),
    pytest.param("Bash", "git status", id="bash-git-status-readonly"),
    pytest.param("Bash", "git log --oneline -5", id="bash-git-log-readonly"),
    pytest.param("Bash", "git diff", id="bash-git-diff-readonly"),
    pytest.param("Bash", "cat file.txt", id="bash-plain-cat-no-redirect"),
    pytest.param("Read", None, id="read-tool"),
    pytest.param("Grep", None, id="grep-tool"),
]


@pytest.mark.parametrize("tool_name,command", NOT_DECISIONS)
def test_read_only_calls_are_never_counted_as_decisions(
    tmp_path: Path, tool_name: str, command: str | None
) -> None:
    lines = [_thought(), _tool_use(tool_name, command)]
    (tmp_path / "prior.jsonl").write_text("\n".join(lines) + "\n")
    cur = tmp_path / "current.jsonl"
    cur.write_text("")
    r = _run(cur)
    assert r.returncode == 0, r.stderr
    assert "1 thought(s), 0 state-mutating action(s)" in r.stdout, (
        f"{tool_name!r} / {command!r} was wrongly counted as a decision: {r.stdout!r}"
    )


def test_subagent_calls_are_excluded(tmp_path: Path) -> None:
    """A subagent's own calls are not the orchestrator's refusal to delegate;
    counting them would make the raw decision count look worse the more you
    delegate. (The isSidechain filter matches zero records in real
    transcripts as of 2026-09-19 -- subagent work lives in separate
    transcript files -- but it is exercised here because a synthetic record
    shaped this way is constructible, and retained as defense-in-depth
    against a future transcript format where it might not be.)"""
    own = json.loads(_tool_use("Edit"))
    sub = json.loads(_tool_use("Edit"))
    sub["isSidechain"] = True
    (tmp_path / "prior.jsonl").write_text(
        json.dumps(own) + "\n" + (json.dumps(sub) + "\n") * 20
    )
    cur = tmp_path / "current.jsonl"
    cur.write_text("")
    r = _run(cur)
    assert r.returncode == 0, r.stderr
    assert "0 thought(s), 1 state-mutating action(s)" in r.stdout, "sidechain entries must not be counted"


def test_the_current_session_is_never_its_own_previous(tmp_path: Path) -> None:
    """Excluded by session_id, not by mtime: the current transcript is the
    newest file on disk precisely because it is being written now."""
    cur = tmp_path / "mysession.jsonl"
    cur.write_text("\n".join(_tool_use("Edit") for _ in range(3)) + "\n")
    r = _run(cur, session_id="mysession")
    assert r.stdout.strip() == "", "the session must not report itself"


def test_output_never_contains_a_percentage_and_always_contains_the_refusal(
    tmp_path: Path,
) -> None:
    """The governing constraint (T3 "Design: restoring the nexus agent
    tool-guidance layer", 2026-07-31): the census can never tell a forgotten
    call apart from a correctly rejected one, so it must not compute or print
    a coverage ratio, a delegation rate, or any other percentage -- and must
    say so explicitly rather than let raw counts imply a verdict by
    omission."""
    lines = [_thought(), _tool_use("Edit"), _tool_use("Agent")]
    (tmp_path / "prior.jsonl").write_text("\n".join(lines) + "\n")
    cur = tmp_path / "current.jsonl"
    cur.write_text("")
    r = _run(cur)
    assert r.returncode == 0, r.stderr
    assert "%" not in r.stdout
    assert "not a compliance measure" in r.stdout
    assert "forgotten" in r.stdout and "correctly rejected" in r.stdout


@pytest.mark.parametrize("payload", ["", "not json", "[]", "null"])
def test_a_malformed_payload_never_blocks_a_session(payload: str) -> None:
    r = subprocess.run(
        [sys.executable, str(SCRIPT)],
        input=payload, capture_output=True, text=True, timeout=60, check=False,
    )
    assert r.returncode == 0, "a census must never fail a session start"
