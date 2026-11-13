# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (c) 2026 Hal Hildebrand. All rights reserved.
"""The checkpoint checks the board against the facts (nexus-ct74m).

The bead's done-when: a checkpoint written in a session carrying each slip class from the
2026-11-13 continuity reflection (T2 nexus/continuity-reflection-2026-11-13) shows that slip.
``TestCheckpointShowsEachSlip`` writes a real checkpoint through ``pre`` for each class, with
the outside world (bd, git ls-remote, gh, ps) replaced at the module's resolver seams, and
``TestNoFalseClaim`` pins the review findings on 448ad2e1c (T2 nexus/review- and
critique-448ad2e1c-nexus-ct74m-2026-11-13-r1): each case there printed a claim the facts did not
support. The other classes pin the parsing those checks rest on.
"""
from __future__ import annotations

import importlib.util
import json
import os
import re
import subprocess
import sys
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

from tests._platform import IS_WINDOWS
from tests.hooks.test_compact_checkpoint import NAME, SID, env, mod, pre_payload, run  # noqa: F401 -- fixtures

FACTS = Path(__file__).resolve().parents[2] / "conexus" / "hooks" / "scripts" / "_session_facts.py"
POSIX_ONLY = pytest.mark.skipif(IS_WINDOWS, reason="process listing is POSIX-only; Windows says so (pinned below)")
SNAP = "/bin/zsh -c source /Users/x/.claude/shell-snapshots/snapshot-zsh-1.sh 2>/dev/null || true && eval '{}' < /dev/null"


@pytest.fixture()
def sf():
    spec = importlib.util.spec_from_file_location("session_facts_under_test", FACTS)
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module  # dataclasses resolve their module through sys.modules
    try:
        spec.loader.exec_module(module)
        yield module
    finally:
        sys.modules.pop(spec.name, None)


def _git(*args: str, cwd: Path) -> str:
    return subprocess.run(["git", *args], cwd=cwd, check=True, capture_output=True, text=True,
                          env={**os.environ, "GIT_AUTHOR_NAME": "t", "GIT_AUTHOR_EMAIL": "t@t",
                               "GIT_COMMITTER_NAME": "t", "GIT_COMMITTER_EMAIL": "t@t"}).stdout


def _commit(path: Path, name: str) -> None:
    (path / name).write_text(name, encoding="utf-8")
    _git("add", name, cwd=path)
    _git("commit", "-q", "-m", name, cwd=path)


def _repo(tmp_path: Path, worktrees: dict[str, bool]) -> Path:
    """A primary with origin/develop, and session worktrees cut from it; True ones get a commit of their own."""
    origin, primary = tmp_path / "origin.git", tmp_path / "primary"
    subprocess.run(["git", "init", "-q", "--bare", "-b", "develop", str(origin)], check=True)
    subprocess.run(["git", "clone", "-q", str(origin), str(primary)], check=True, capture_output=True)
    _commit(primary, "base")
    _git("push", "-q", "origin", "HEAD:develop", cwd=primary)
    _git("fetch", "-q", cwd=primary)
    for name, commit in worktrees.items():
        path = tmp_path / "wt" / name
        _git("worktree", "add", "-q", "-b", f"feature/x-{name}", str(path), "origin/develop", cwd=primary)
        if commit:
            _commit(path, f"f-{name}")
    return primary


def _chosen(text: str) -> str:
    """The directory name of the worktree the checkpoint header chose."""
    line = text.split("\n## ", 1)[0].split("- worktree: ", 1)[1].splitlines()[0]
    return Path(line.strip()).name


@pytest.fixture()
def world(mod, env, monkeypatch):  # noqa: F811 -- pytest fixtures
    """A board this test sets, and resolvers that answer from dicts this test fills."""
    sf = mod._session_facts
    state: dict = {"board": "", "beads": {}, "tags": {}, "runs": {}, "prs": {}, "table": [], "cwds": {}, "seen": []}
    monkeypatch.setattr(mod, "_board_entry", lambda *_a: (NAME, state["board"], "exact title", None))
    monkeypatch.setattr(sf, "resolve_beads", lambda ids, *_a: ({i: state["beads"][i] for i in ids if i in state["beads"]}, ""))
    monkeypatch.setattr(sf, "resolve_tags", lambda tags, *_a: ({t: t in state["tags"] for t in tags}, ""))
    monkeypatch.setattr(sf, "resolve_run", lambda repo, r, *_a: state["runs"].get(r))

    def pr(repo, number, *_a):
        state["seen"].append((repo, number))
        return state["prs"].get((repo, number))

    monkeypatch.setattr(sf, "resolve_pr", pr)
    monkeypatch.setattr(sf, "process_table", lambda *_a: state["table"])
    monkeypatch.setattr(sf, "process_cwds", lambda *_a: state["cwds"])
    state["write"] = lambda **kw: (run(mod, monkeypatch, "pre", pre_payload(**kw)),
                                   (env.checkpoints / f"{SID}.md").read_text(encoding="utf-8"))[1]
    return state


def _section(text: str, heading: str) -> str:
    return text.split(f"## {heading}", 1)[1].split("\n## ", 1)[0]


# ── each slip class, end to end through pre ─────────────────────────────────


class TestCheckpointShowsEachSlip:
    def test_a_wait_on_a_tag_that_exists(self, world) -> None:
        world["board"] = "## Engine cut\nref: nexus-ct74m\nwaiting_on: engine-service-v0.1.190\n"
        world["tags"] = {"engine-service-v0.1.190"}
        text = world["write"]()
        assert "waiting_on engine-service-v0.1.190: the tag exists on origin, so this wait may be over" in text

    def test_a_wait_on_a_bare_engine_version_and_a_client_release(self, world) -> None:
        world["board"] = "## Release\nwaiting_on: the 0.1.190 cut and conexus 8.2.2\n"
        world["tags"] = {"engine-service-v0.1.190", "v8.2.2"}
        text = world["write"]()
        # Two things on one line: each part is reported, no conclusion about the whole wait.
        assert "waiting_on engine-service-v0.1.190: the tag exists on origin\n" in text
        assert "waiting_on v8.2.2: the tag exists on origin\n" in text

    def test_a_wait_on_a_ci_run_that_finished(self, world) -> None:
        world["board"] = "## Push\nwaiting_on: CI run 43278647831 on develop\n"
        world["runs"] = {"43278647831": ("completed", "success")}
        assert "CI run 43278647831: completed, success, so this wait is over" in world["write"]()

    def test_a_wait_on_a_merged_pr_named_with_its_repository(self, world) -> None:
        world["board"] = "## Upstream\nwaiting_on: https://github.com/Hellblazer/conexus/pull/703\n"
        world["prs"] = {("Hellblazer/conexus", "703"): "MERGED"}
        assert "waiting_on Hellblazer/conexus#703: merged, so this wait is over" in world["write"]()
        assert world["seen"] == [("Hellblazer/conexus", "703")]

    def test_a_ref_to_a_bead_closed_days_ago(self, world) -> None:
        world["board"] = "## Old work\nref: nexus-abcde\nstate: building\n"
        world["beads"] = {"nexus-abcde": ("closed", "task", 0)}
        assert "ref nexus-abcde: closed, so this line is stale" in world["write"]()

    def test_an_epic_ref_with_every_child_closed(self, world) -> None:
        world["board"] = "## Epic\nref: nexus-ep1c0\n"
        world["beads"] = {"nexus-ep1c0": ("open", "epic", 0)}
        assert "ref nexus-ep1c0: open epic with no open child" in world["write"]()

    def test_a_wait_on_an_open_bead_says_the_wait_is_real(self, world) -> None:
        world["board"] = "## Blocked\nwaiting_on: nexus-b10ck\n"
        world["beads"] = {"nexus-b10ck": ("in_progress", "task", 0)}
        assert "waiting_on nexus-b10ck: still in_progress" in world["write"]()

    def test_a_board_that_holds_says_what_it_checked(self, world) -> None:
        world["board"] = "## Live\nref: nexus-11ve0\nwaiting_on: sam\n"
        world["beads"] = {"nexus-11ve0": ("in_progress", "task", 0)}
        text = _section(world["write"](), "Board checked against the facts")
        assert "- checked 1 ref/waiting_on line(s) against a bead, tag, CI run or PR; 1 line(s) name none" in text
        assert "stale" not in text

    def test_a_due_thread(self, world) -> None:
        world["board"] = "## Reminder\nref: nexus-ct74m\ndue: 2026-01-01\n"
        assert "due 2026-01-01: due now" in world["write"]()

    def test_a_worktree_line_naming_a_missing_path(self, world, tmp_path) -> None:
        gone = tmp_path / "no such worktree"
        world["board"] = f"## Lost\nworktree: {gone}\n"
        assert f"worktree {gone}: no such path" in world["write"]()

    def test_open_work_with_no_board_block(self, world, mod, env, monkeypatch, tmp_path) -> None:
        primary = _repo(tmp_path, {NAME: False, f"{NAME}-forgotten": True})
        monkeypatch.setattr(mod, "_git_worktrees", env.real_git_worktrees)
        world["board"] = f"## Main\nref: nexus-ct74m\nstate: in nexus-wt/{NAME}\n"
        text = world["write"](cwd=str(primary))
        assert re.search(rf"- no board thread names \S*{NAME}-forgotten \[feature/x-{NAME}-forgotten\]: "
                         r"1 commit\(s\) not on origin/develop \(as of its last fetch\)", text)

    def test_the_worktree_is_the_one_with_the_newest_commit_not_the_cwd(self, world, mod, env, monkeypatch, tmp_path) -> None:
        _repo(tmp_path, {NAME: True, f"{NAME}-live": False})
        time.sleep(1.1)  # the reflog stamps whole seconds
        _commit(tmp_path / "wt" / f"{NAME}-live", "newest")
        monkeypatch.setattr(mod, "_git_worktrees", env.real_git_worktrees)
        world["board"] = "## Main\nref: nexus-ct74m\n"
        # Launched inside the stale worktree: before nexus-ct74m the cwd won (tamsin, 2026-11-12).
        assert _chosen(world["write"](cwd=str(tmp_path / "wt" / NAME))) == f"{NAME}-live"

    def test_a_worktree_made_after_the_last_commit_wins_before_its_first_commit(self, world, mod, env, monkeypatch, tmp_path) -> None:
        primary = _repo(tmp_path, {NAME: True})
        time.sleep(1.1)
        _git("worktree", "add", "-q", "-b", "feature/x-fresh", str(tmp_path / "wt" / f"{NAME}-fresh"), "origin/develop", cwd=primary)
        monkeypatch.setattr(mod, "_git_worktrees", env.real_git_worktrees)
        world["board"] = "## Main\nref: nexus-ct74m\n"
        assert _chosen(world["write"](cwd=str(primary))) == f"{NAME}-fresh"

    def test_a_board_worktree_line_is_the_first_choice_whatever_its_name(self, world, mod, env, monkeypatch, tmp_path) -> None:
        primary = _repo(tmp_path, {NAME: True})
        other = tmp_path / "wt" / "release-v9"
        _git("worktree", "add", "-q", "--detach", str(other), "origin/develop", cwd=primary)
        monkeypatch.setattr(mod, "_git_worktrees", env.real_git_worktrees)
        world["board"] = f"## Main\nref: nexus-ct74m\nworktree: {other}\n"
        assert _chosen(world["write"](cwd=str(primary))) == "release-v9"

    @POSIX_ONLY
    def test_background_shells_are_listed_with_start_and_command(self, world, mod) -> None:
        sf = mod._session_facts
        world["table"] = [sf.Proc(4242, 1, "Fri Nov 13 01:00:00 2026", "claude"),
                          sf.Proc(5000, 4242, "Fri Nov 13 01:05:00 2026", SNAP.format("nx ci wait abc > /tmp/ci.log 2>&1")),
                          sf.Proc(5001, 5000, "Fri Nov 13 01:05:00 2026", "python nx ci wait abc"),
                          sf.Proc(5100, 4242, "Fri Nov 13 01:06:00 2026", SNAP.format("nx tuple wait mailbox/x")),
                          sf.Proc(6000, 4242, "Fri Nov 13 01:00:01 2026", "node mcp-server.js")]
        running = _section(world["write"](), "Running for this session")
        assert "- background task pid 5000, started Fri Nov 13 01:05:00 2026: `nx ci wait abc > /tmp/ci.log 2>&1`" in running
        assert "- background task pid 5100, started Fri Nov 13 01:06:00 2026 (standing mail wait)" in running
        assert "mcp-server" not in running

    @POSIX_ONLY
    def test_no_background_shell_says_none_only_on_proof(self, world, mod) -> None:
        world["table"] = [mod._session_facts.Proc(4242, 1, "Fri Nov 13 01:00:00 2026", "claude")]
        assert "- background tasks: none running" in world["write"]()

    @POSIX_ONLY
    def test_processes_in_the_sessions_worktrees(self, world, mod, env, monkeypatch, tmp_path) -> None:
        _repo(tmp_path, {NAME: True})
        wt = tmp_path / "wt" / NAME
        monkeypatch.setattr(mod, "_git_worktrees", env.real_git_worktrees)
        sf = mod._session_facts
        me = os.getpid()
        world["table"] = [sf.Proc(7000, 1, "Sat Nov 8 03:00:00 2026", "mineru-api --port 0"),
                          sf.Proc(me, 1, "Fri Nov 13 01:00:00 2026", "python compact_checkpoint.py pre"),
                          sf.Proc(7100, me, "Fri Nov 13 01:00:01 2026", "nx command-context continuation")]
        world["cwds"] = {7000: str(wt / "sub"), 7100: str(wt)}
        text = world["write"](cwd=str(tmp_path / "primary"))
        assert "- process pid 7000 in " in text and "`mineru-api --port 0`" in text
        assert "pid 7100" not in text  # the hook's own continuation child is not the session's work

    def test_crons_durable_ones_listed_and_the_session_ones_named_as_invisible(self, world, mod, env, monkeypatch, tmp_path) -> None:
        _repo(tmp_path, {NAME: True})
        wt = tmp_path / "wt" / NAME
        (wt / ".claude").mkdir()
        (wt / ".claude" / "scheduled_tasks.json").write_text(json.dumps(
            {"tasks": [{"cron": "*/30 * * * *", "prompt": "check the board", "sessionId": SID},
                       {"cron": "0 9 * * *", "prompt": "a peer's", "sessionId": "someone-else"}]}), encoding="utf-8")
        monkeypatch.setattr(mod, "_git_worktrees", env.real_git_worktrees)
        text = world["write"](cwd=str(tmp_path / "primary"))
        assert "- durable cron: `*/30 * * * *` check the board" in text and "a peer's" not in text
        assert "run CronList after this compaction" in text

    @pytest.mark.skipif(not IS_WINDOWS, reason="the Windows line")
    def test_windows_says_processes_are_not_listed(self, world) -> None:
        assert "- background tasks and processes: not listed on Windows" in world["write"]()

    def test_kept_output_is_stamped(self, world) -> None:
        world["board"] = "## X\nref: nexus-ct74m\n"
        text = world["write"]()
        stamp = r"\d{4}-\d\d-\d\dT\d\d:\d\d:\d\d\+00:00"
        assert re.search(rf"## Threads board entry \(threads/{NAME}\)\n\n\(read {stamp}; ", text)
        assert re.search(rf"## Board checked against the facts\n\n\(checked {stamp}\)", text)
        assert re.search(rf"## Running for this session\n\n\(listed {stamp}\)", text)
        assert re.search(rf"## Working state\n\n\(nx command-context continuation, run {stamp}; old tool output", text)

    def test_a_failing_check_still_writes_the_checkpoint(self, world, mod, monkeypatch) -> None:
        world["board"] = "## X\nref: nexus-ct74m\n"
        monkeypatch.setattr(mod._session_facts, "board_wants", lambda _b: 1 / 0)
        monkeypatch.setattr(mod._session_facts, "durable_crons", lambda *_a: 1 / 0)
        text = world["write"]()
        assert "board lines not checked" in text and "[task listing failed: ZeroDivisionError]" in text
        assert "## Working state" in text

    def test_a_hung_lookup_is_cut_off_inside_the_budget(self, world, mod, monkeypatch) -> None:
        world["board"] = "## X\nref: nexus-ct74m\n"
        monkeypatch.setattr(mod, "_FACTS_BUDGET_S", 0.5)
        monkeypatch.setattr(mod._session_facts, "resolve_beads", lambda *_a: (time.sleep(3), ({}, ""))[1])
        started = time.monotonic()
        text = world["write"]()
        assert time.monotonic() - started < 3.0
        assert "beads not checked: the lookup failed or ran out of time" in text


# ── what review round 1 found printed falsely (448ad2e1c) ───────────────────


class TestNoFalseClaim:
    def test_work_already_on_develop_is_landed_whatever_the_upstream_says(self, world, mod, env, monkeypatch, tmp_path) -> None:
        # marlowe-runner: the branch's upstream lagged, HEAD was inside develop, and "16 not landed" printed.
        primary = _repo(tmp_path, {NAME: False})
        wt = tmp_path / "wt" / f"{NAME}-landed"
        _git("worktree", "add", "-q", "-b", "feature/x-landed", str(wt), "origin/develop", cwd=primary)
        _commit(wt, "a")
        _git("push", "-q", "-u", "origin", "HEAD:feature/x-landed", cwd=wt)
        _commit(wt, "b")
        _git("push", "-q", "origin", "HEAD:develop", cwd=wt)
        _git("fetch", "-q", cwd=wt)
        assert "+" in _git("cherry", "@{upstream}", "HEAD", cwd=wt)  # the old base would have counted it
        monkeypatch.setattr(mod, "_git_worktrees", env.real_git_worktrees)
        world["board"] = "## Main\nref: nexus-ct74m\n"
        assert "landed" not in _section(world["write"](cwd=str(primary)), "Board checked against the facts").replace(
            "not on origin/develop", "")
        assert f"{NAME}-landed" not in _section(world["write"](cwd=str(primary)), "Board checked against the facts")

    def test_one_closed_bead_on_a_mixed_line_does_not_make_the_line_stale(self, world) -> None:
        world["board"] = "## Mixed\nref: closed today nexus-nz8kl; open nexus-y3y8d (claimed)\n"
        world["beads"] = {"nexus-nz8kl": ("closed", "task", 0), "nexus-y3y8d": ("in_progress", "task", 0)}
        text = world["write"]()
        assert "ref: nexus-nz8kl is closed" in text and "stale" not in _section(text, "Board checked against the facts")

    @pytest.mark.parametrize(("line", "facts", "says", "never"), [
        # halcyon: a tag that exists is half of a two-step wait
        ("waiting_on: 0.1.188 then 8.1.3", {"tags": {"engine-service-v0.1.188"}},
         "waiting_on engine-service-v0.1.188: the tag exists on origin\n", "may be over"),
        # baton: a closed bead beside a sha and a window on the same line
        ("ref: nexus-qo7ie.31; develop 68bc53766 (window 24)", {"beads": {"nexus-qo7ie.31": ("closed", "task", 0)}},
         "ref: nexus-qo7ie.31 is closed", "stale"),
        ("waiting_on: CI run 43278647831 then the merge", {"runs": {"43278647831": ("completed", "success")}},
         "CI run 43278647831: completed, success\n", "over"),
    ], ids=["tag-then-another", "bead-beside-a-sha", "run-then-a-step"])
    def test_a_fact_on_a_line_with_another_wait_draws_no_conclusion(self, world, line, facts, says, never) -> None:
        world["board"] = f"## T\n{line}\n"
        for k, v in facts.items():
            world[k] = v
        section = _section(world["write"](), "Board checked against the facts")
        assert says in section and never not in section

    def test_a_line_whose_lookup_failed_is_not_a_line_that_names_nothing(self, world) -> None:
        world["board"] = "## T\nref: nexus-ct74m\nwaiting_on: sam\n"  # world["beads"] is empty: bd answered nothing
        section = _section(world["write"](), "Board checked against the facts")
        assert "- checked 0 ref/waiting_on line(s) against a bead, tag, CI run or PR; 1 line(s) name one that no " \
               "lookup answered; 1 line(s) name none" in section

    def test_a_deferred_epic_is_not_told_to_close(self, world) -> None:
        world["board"] = "## Held\nref: nexus-1d6pz\n"
        world["beads"] = {"nexus-1d6pz": ("deferred", "epic", 0)}
        assert "close it" not in world["write"]()

    def test_a_bare_pr_number_is_not_looked_up(self, world) -> None:
        # nexus #12 is merged; "PR #12" on a board can be any repository's.
        world["board"] = "## Up\nwaiting_on: PR #12 review in labrun\n"
        world["prs"] = {("", "12"): "MERGED"}
        text = world["write"]()
        assert world["seen"] == [] and "merged" not in text

    def test_a_file_path_worktree_line_is_not_a_missing_directory(self, world, tmp_path) -> None:
        page = tmp_path / "artifact.html"
        page.write_text("x", encoding="utf-8")
        world["board"] = f"## Page\nworktree: {page}\n"
        assert "no such" not in world["write"]()

    @POSIX_ONLY
    def test_a_lookup_that_failed_is_not_none_running(self, world, mod, env) -> None:
        (env.claude / "sessions" / "4242.json").unlink()  # the session file is gone (a /clear, a layout change)
        world["table"] = [mod._session_facts.Proc(1, 0, "Fri Nov 13 01:00:00 2026", "init")]
        text = world["write"]()
        assert "none running" not in text and "- background tasks: not listed (no " in text

    @POSIX_ONLY
    def test_unreadable_working_directories_are_not_no_processes(self, world, mod, env, monkeypatch, tmp_path) -> None:
        _repo(tmp_path, {NAME: True})
        monkeypatch.setattr(mod, "_git_worktrees", env.real_git_worktrees)
        world["table"] = [mod._session_facts.Proc(4242, 1, "Fri Nov 13 01:00:00 2026", "claude")]
        world["cwds"] = None
        assert "processes in this session's worktrees: not listed" in world["write"](cwd=str(tmp_path / "primary"))

    def test_without_the_board_no_worktree_is_called_unnamed(self, world, mod, env, monkeypatch, tmp_path) -> None:
        primary = _repo(tmp_path, {NAME: True})
        monkeypatch.setattr(mod, "_git_worktrees", env.real_git_worktrees)
        monkeypatch.setattr(mod, "_board_entry", lambda *_a: ("", "", "", "service unavailable"))
        text = world["write"](cwd=str(primary))
        assert "no board thread names" not in text
        assert "- not checked: the board entry was not read (service unavailable)" in text

    def test_a_child_bead_branch_is_named_by_its_parent_ref(self, sf) -> None:
        w = sf.WorktreeWork("/w/linda-b", "feature/nexus-z1d0l.1.3-x", 2, 0)
        assert sf.unboarded([w], "## T\nref: nexus-z1d0l.1\n") == ([], [])
        assert sf.unboarded([w], "## T\nref: nexus-z1d0l.10\n")[0]

    def test_a_worktree_git_could_not_read_is_unchecked_not_clean(self, sf) -> None:
        lines, unchecked = sf.unboarded([sf.WorktreeWork("/w/linda-x", "feature/y", -1, -1)], "## T\n")
        assert lines == [] and unchecked == ["/w/linda-x: git could not read its work"]


# ── parsing the facts rest on ────────────────────────────────────────────────


class TestParsing:
    def test_threads_read_the_fields_as_the_board_parser_does(self, sf, tmp_path) -> None:
        spaced = str(tmp_path / "a path" / "with spaces")
        board = (f"session: x\n## One\nref: nexus-aaaaa\nWaiting-On : nexus-bbbbb\nDue: none\nworktree: -\n"
                 f"### Two\nworktree: {spaced}\ndue: 2026-11-20\n")
        got = [(t.title, t.fields) for t in sf.threads(board)]
        assert got == [("One", [("ref", "nexus-aaaaa"), ("waiting_on", "nexus-bbbbb")]),
                       ("Two", [("worktree", spaced), ("due", "2026-11-20")])]
        assert sf.board_worktree(board) == [spaced]

    @pytest.mark.parametrize(("text", "beads", "engine", "client", "runs", "prs"), [
        ("nexus-f3yqd.2 and nexus-ct74m.", {"nexus-f3yqd.2", "nexus-ct74m"}, set(), set(), set(), set()),
        ("feature/nexus-scmcs.3-markup-standin", set(), set(), set(), set(), set()),
        ("bob's 0.1.190 cut", set(), {"engine-service-v0.1.190"}, set(), set(), set()),
        ("engine 0.1.191 and engine-service-v0.1.192", set(), {"engine-service-v0.1.191", "engine-service-v0.1.192"}, set(), set(), set()),
        ("conexus 8.2.2, tag v8.3.0; a bare v2.8.0 is anyone's", set(), set(), {"v8.2.2", "v8.3.0"}, set(), set()),
        ("host 10.0.1.9", set(), set(), set(), set(), set()),
        ("run 43278647831, https://github.com/o/r/actions/runs/43266578276", set(), set(), set(),
         {("", "43278647831"), ("o/r", "43266578276")}, set()),
        ("PR #12, Hellblazer/conexus#703 and https://github.com/o/r/pull/34", set(), set(), set(), set(),
         {("Hellblazer/conexus", "703"), ("o/r", "34")}),
        ("nexus-wt/tamsin-cancel is a path, not a bead", set(), set(), set(), set(), set()),
    ])
    def test_facts_named(self, sf, text, beads, engine, client, runs, prs) -> None:
        w = sf.facts_named(text)
        assert set(w.beads) == beads and set(w.engine_tags) == engine
        assert set(w.client_tags) == client and set(w.runs) == runs and set(w.prs) == prs

    def test_a_due_date_is_due_from_local_midnight(self, sf) -> None:
        assert sf.parse_date("2026-11-20") == datetime(2026, 11, 20).astimezone()
        assert sf.parse_date("2026-11-20T09:30:00Z") == datetime(2026, 11, 20, 9, 30, tzinfo=timezone.utc)
        assert sf.parse_date("2026-11-20T09:30") == datetime(2026, 11, 20, 9, 30).astimezone()
        for bad in ("next week", "2026-11-20 09:30", "20261120", "2026-11-20T09"):
            assert sf.parse_date(bad) is None, bad

    def test_a_due_line_that_is_not_a_date_is_no_due_date(self, sf) -> None:
        assert sf.verify("## T\ndue: soon\n", sf.Facts(), datetime.now(timezone.utc)).lines == []

    def test_given_twice_the_last_due_counts(self, sf) -> None:
        now = datetime.now(timezone.utc)
        assert sf.verify("## T\ndue: 2026-01-01\ndue: 2999-01-01\n", sf.Facts(), now).lines == []
        assert sf.verify("## T\ndue: 2999-01-01\ndue: 2026-01-01\n", sf.Facts(), now).lines == ["- T", "  - due 2026-01-01: due now"]

    @pytest.mark.parametrize(("body", "is_standing"), [
        ("nx tuple wait mailbox/tamsin", True),
        ("nx tuple wait mailbox/tamsin --timeout 600 > /tmp/w.log 2>&1", True),
        ("nx tuple wait mailbox/x; rm -rf build", False),
        ("bd comments add x \"nx tuple wait\"", False),
        ("nx tuple wait x && make", False),
    ])
    def test_only_the_bare_wake_is_a_standing_wait(self, sf, body, is_standing) -> None:
        assert sf.standing(SNAP.format(body)) is is_standing

    @pytest.mark.parametrize(("line", "want"), [
        ("zsh -c source /h/.claude/shell-snapshots/snapshot-zsh-1.sh && eval 'echo '\\''hi'\\'' > /x' < /dev/null", "echo 'hi' > /x"),
        # macOS ps: '"'"' for a quote, \012 for a newline (live, review of 448ad2e1c)
        ("zsh -c source /h/.claude/shell-snapshots/snapshot-zsh-1.sh && eval 'ENC=$(printf '\"'\"'%s'\"'\"' x)\\012echo $ENC' < /dev/null",
         "ENC=$(printf '%s' x) echo $ENC"),
        ("plain  command", "plain command"),
    ])
    def test_the_eval_body_is_the_task_command(self, sf, line, want) -> None:
        assert sf.task_command(line) == want

    def test_ps_rows_parse(self, sf) -> None:
        out = ("  501   1 Fri Nov 13 19:33:02 2026     /usr/bin/claude --resume\n"
               "  777 501 Sat Nov  8 03:00:00 2026 /bin/zsh -c x\nnot a row\n")
        rows = sf.process_table(lambda _a, _t: (0, out))
        if IS_WINDOWS:
            assert rows == []
        else:
            assert rows == [sf.Proc(501, 1, "Fri Nov 13 19:33:02 2026", "/usr/bin/claude --resume"),
                            sf.Proc(777, 501, "Sat Nov 8 03:00:00 2026", "/bin/zsh -c x")]

    def test_background_tasks_are_snapshot_shells_below_the_sessions_claude(self, sf, tmp_path) -> None:
        (tmp_path / "4242.json").write_text(json.dumps({"pid": 4242, "sessionId": "s1"}), encoding="utf-8")
        snap = SNAP.format("sleep 9")
        table = [sf.Proc(4242, 1, "t", "claude"), sf.Proc(10, 4242, "t", snap), sf.Proc(11, 4242, "t", "mcp"),
                 sf.Proc(12, 4242, "t", snap), sf.Proc(20, 1, "t", snap)]
        assert [p.pid for p in sf.background_tasks("s1", tmp_path, table, exclude={12})] == [10]
        assert sf.background_tasks("unknown", tmp_path, table) is None
        assert sf.background_tasks("s1", tmp_path, []) is None

    def test_a_pipeline_in_a_worktree_shows_once(self, sf, tmp_path) -> None:
        table = [sf.Proc(1, 0, "t", "sh -c a | b"), sf.Proc(2, 1, "t", "a"), sf.Proc(3, 0, "t", "elsewhere")]
        cwds = {1: str(tmp_path), 2: str(tmp_path / "x"), 3: "/"}
        assert [p.pid for p, _r in sf.worktree_processes(table, cwds, [str(tmp_path)])] == [1]

    def test_last_work_time_reads_commits_and_creation_not_checkouts_or_syncs(self, sf, tmp_path) -> None:
        gitdir = tmp_path / "g"
        (gitdir / "logs").mkdir(parents=True)
        (gitdir / "logs" / "HEAD").write_text(
            "0 1 t <t@t> 500 +0000\treset: moving to HEAD\n"
            "1 2 t <t@t> 1000 +0000\tcommit: one\n"
            "2 3 t <t@t> 3000 +0000\tcheckout: moving from a to b\n"
            "3 4 t <t@t> 3500 +0000\tmerge origin/develop: Fast-forward\n"
            "4 5 t <t@t> 3600 +0000\trebase (pick): old\n"
            "5 6 t <t@t> 2000 +0000\tcherry-pick: two\n", encoding="utf-8")
        wt = tmp_path / "wt"
        wt.mkdir()
        (wt / ".git").write_text(f"gitdir: {gitdir}\n", encoding="utf-8")
        assert sf.last_work_time(str(wt)) == 2000.0
        (gitdir / "logs" / "HEAD").write_text("0 1 t <t@t> 900 +0000\treset: moving to HEAD\n", encoding="utf-8")
        assert sf.last_work_time(str(wt)) == 900.0  # a fresh worktree: its creation
        assert sf.last_work_time(str(tmp_path / "missing")) == 0.0

    def test_unboarded_names_by_path_branch_or_bead(self, sf) -> None:
        w = sf.WorktreeWork("/w/tamsin-x", "feature/nexus-abcde-x", 2, 0)
        assert sf.unboarded([w], "## T\nref: nexus-abcde\n") == ([], [])
        assert sf.unboarded([w], "## T\nstate: in nexus-wt/tamsin-x\n") == ([], [])
        assert sf.unboarded([w], "## T\nref: nexus-zzzzz\n")[0][0].startswith("- no board thread names /w/tamsin-x")
        assert sf.unboarded([w], "## T\nstate: in /w/tamsin-x\n") == ([], [])
        # A longer sibling's path does not name this one (/w/tamsin-x-old is not /w/tamsin-x).
        assert sf.unboarded([w], "## T\nstate: in /w/tamsin-x-old\n")[0]
        # A session's name elsewhere on its board (mailbox/tamsin-x) is not the worktree.
        assert sf.unboarded([w], "## T\n(mailbox/tamsin-x)\nref: nexus-zzzzz\n")[0]
        assert sf.unboarded([sf.WorktreeWork("/w/a", "b", 0, 0)], "") == ([], [])

    def test_resolve_beads_reads_open_then_the_rest_by_id(self, sf, monkeypatch) -> None:
        calls = []

        def fake(argv, timeout, cwd=None):
            calls.append(argv)
            if "--status" in argv:
                return 0, json.dumps({"issues": [
                    {"id": "nexus-ep1c0", "status": "open", "issue_type": "epic"},
                    {"id": "nexus-ep1c0.1", "status": "open", "issue_type": "task", "parent": "nexus-ep1c0"},
                    {"id": "nexus-ep2c0", "status": "open", "issue_type": "epic"},
                    {"id": "nexus-b1ock", "status": "open", "issue_type": "task",
                     "dependencies": [{"depends_on_id": "nexus-ep2c0"}]}]})
            return 0, json.dumps({"issues": [{"id": "nexus-c1osd", "status": "closed", "issue_type": "task"}]})

        monkeypatch.setattr(sf, "run", fake)
        got, why = sf.resolve_beads({"nexus-ep1c0", "nexus-ep2c0", "nexus-c1osd", "fresh-start"}, "bd", None, 1.0)
        assert why == "" and got == {"nexus-ep1c0": ("open", "epic", 1), "nexus-ep2c0": ("open", "epic", 1),
                                     "nexus-c1osd": ("closed", "task", -1)}
        assert calls[1][-1] == "fresh-start,nexus-c1osd"

    def test_resolve_tags_one_ls_remote(self, sf, monkeypatch) -> None:
        monkeypatch.setattr(sf, "run", lambda argv, t, cwd=None: (0, "abc\trefs/tags/v8.2.2\nabd\trefs/tags/v8.2.2^{}\n"))
        assert sf.resolve_tags({"v8.2.2", "v9.0.0"}, "git", None, 1.0) == ({"v8.2.2": True, "v9.0.0": False}, "")
        monkeypatch.setattr(sf, "run", lambda *_a, **_k: (-1, ""))
        assert sf.resolve_tags({"v8.2.2"}, "git", None, 1.0)[1].startswith("tags not checked")

    def test_resolve_pr_names_the_repository(self, sf, monkeypatch) -> None:
        seen = []
        monkeypatch.setattr(sf, "run", lambda argv, t, cwd=None: (seen.append(argv), (0, '{"state": "OPEN"}'))[1])
        assert sf.resolve_pr("Hellblazer/conexus", "703", "gh", None, 1.0) == "OPEN"
        assert seen[0][-4:-2] == ["-R", "Hellblazer/conexus"]
        assert sf.resolve_pr("", "12", "gh", None, 1.0) is None

    def test_durable_crons_tolerate_shapes_and_name_an_unknown_owner(self, sf, tmp_path) -> None:
        (tmp_path / ".claude").mkdir()
        (tmp_path / ".claude" / "scheduled_tasks.json").write_text(json.dumps([{"cron": "@hourly", "prompt": "p"}]), encoding="utf-8")
        assert sf.durable_crons([str(tmp_path), str(tmp_path)]) == [
            f"`@hourly` p ({tmp_path / '.claude' / 'scheduled_tasks.json'}, owner unknown: the file names no session)"]
        (tmp_path / ".claude" / "scheduled_tasks.json").write_text("{not json", encoding="utf-8")
        assert sf.durable_crons([str(tmp_path)]) == []


def test_verify_lists_only_threads_with_findings(sf) -> None:
    facts = sf.Facts(beads={"nexus-c1osd": ("closed", "task", 0), "nexus-0pen0": ("open", "task", 0)})
    board = "## Stale\nref: nexus-c1osd\n## Fine\nref: nexus-0pen0\n"
    got = sf.verify(board, facts, datetime.now(timezone.utc) - timedelta(0))
    assert got.lines == ["- Stale", "  - ref nexus-c1osd: closed, so this line is stale"] and got.checked == 2
