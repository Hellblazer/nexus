# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (c) 2026 Hal Hildebrand. All rights reserved.
"""The checkpoint checks the board against the facts (nexus-ct74m).

The bead's done-when: a checkpoint written in a session carrying each slip class from the
2026-11-13 continuity reflection (T2 nexus/continuity-reflection-2026-11-13) shows that slip.
``TestCheckpointShowsEachSlip`` writes a real checkpoint through ``pre`` for each class, with
the outside world (bd, git ls-remote, gh, ps) replaced at the module's resolver seams. The
other classes pin the parsing those checks rest on.
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

from tests.hooks.test_compact_checkpoint import NAME, SID, env, mod, pre_payload, run  # noqa: F401 -- fixtures

FACTS = Path(__file__).resolve().parents[2] / "conexus" / "hooks" / "scripts" / "_session_facts.py"


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


def _git(*args: str, cwd: Path) -> None:
    subprocess.run(["git", *args], cwd=cwd, check=True, capture_output=True,
                   env={**os.environ, "GIT_AUTHOR_NAME": "t", "GIT_AUTHOR_EMAIL": "t@t", "GIT_COMMITTER_NAME": "t",
                        "GIT_COMMITTER_EMAIL": "t@t"})


# ── each slip class, end to end through pre ─────────────────────────────────


class TestCheckpointShowsEachSlip:
    @pytest.fixture()
    def world(self, mod, env, monkeypatch):  # noqa: F811 -- pytest fixtures
        """A board this test sets, and resolvers that answer from dicts this test fills."""
        sf = mod._session_facts
        state = {"board": "", "beads": {}, "tags": {}, "runs": {}, "prs": {}, "table": [], "cwds": {}}
        monkeypatch.setattr(mod, "_board_entry", lambda *_a: (NAME, state["board"], "exact title", None))
        monkeypatch.setattr(sf, "resolve_beads", lambda ids, *_a: ({i: state["beads"][i] for i in ids if i in state["beads"]}, ""))
        monkeypatch.setattr(sf, "resolve_tags", lambda tags, *_a: ({t: t in state["tags"] for t in tags}, ""))
        monkeypatch.setattr(sf, "resolve_run", lambda r, *_a: state["runs"].get(r))
        monkeypatch.setattr(sf, "resolve_pr", lambda n, *_a: state["prs"].get(n))
        monkeypatch.setattr(sf, "process_table", lambda *_a: state["table"])
        monkeypatch.setattr(sf, "process_cwds", lambda *_a: state["cwds"])
        state["write"] = lambda **kw: (run(mod, monkeypatch, "pre", pre_payload(**kw)),
                                       (env.checkpoints / f"{SID}.md").read_text(encoding="utf-8"))[1]
        return state

    def test_a_wait_on_a_tag_that_exists(self, world) -> None:
        world["board"] = "## Engine cut\nref: nexus-ct74m\nwaiting_on: bob's engine-service-v0.1.190 cut\n"
        world["tags"] = {"engine-service-v0.1.190"}
        text = world["write"]()
        assert "waiting_on engine-service-v0.1.190: the tag exists on origin, so this wait may be over" in text

    def test_a_wait_on_a_bare_engine_version_and_a_client_release(self, world) -> None:
        world["board"] = "## Release\nwaiting_on: the 0.1.190 cut and conexus 8.2.2\n"
        world["tags"] = {"engine-service-v0.1.190", "v8.2.2"}
        text = world["write"]()
        assert "engine-service-v0.1.190: the tag exists" in text and "v8.2.2: the tag exists" in text

    def test_a_wait_on_a_ci_run_that_finished(self, world) -> None:
        world["board"] = "## Push\nwaiting_on: CI run 43278647831 on develop\n"
        world["runs"] = {"43278647831": ("completed", "success")}
        assert "CI run 43278647831: completed, success, so this wait is over" in world["write"]()

    def test_a_wait_on_a_merged_pr(self, world) -> None:
        world["board"] = "## Upstream\nwaiting_on: PR #1656 review\n"
        world["prs"] = {"1656": "MERGED"}
        assert "waiting_on PR #1656: merged, so this wait is over" in world["write"]()

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

    def test_a_board_that_holds_says_so(self, world) -> None:
        world["board"] = "## Live\nref: nexus-11ve0\nwaiting_on: sam\n"
        world["beads"] = {"nexus-11ve0": ("in_progress", "task", 0)}
        assert "- nothing found stale" in world["write"]()

    def test_an_overdue_thread(self, world) -> None:
        world["board"] = "## Reminder\nref: nexus-ct74m\ndue: 2026-01-01\n"
        assert "due 2026-01-01: overdue" in world["write"]()

    def test_a_worktree_line_naming_a_missing_directory(self, world, tmp_path) -> None:
        gone = tmp_path / "no such worktree"
        world["board"] = f"## Lost\nworktree: {gone}\n"
        assert f"worktree {gone}: no such directory" in world["write"]()

    def _repo(self, tmp_path: Path, worktrees: dict[str, bool]) -> Path:
        """A primary with origin/develop, and session worktrees; True ones get a commit of their own."""
        origin, primary = tmp_path / "origin.git", tmp_path / "primary"
        subprocess.run(["git", "init", "-q", "--bare", "-b", "develop", str(origin)], check=True)
        subprocess.run(["git", "clone", "-q", str(origin), str(primary)], check=True, capture_output=True)
        (primary / "a").write_text("a", encoding="utf-8")
        _git("add", "a", cwd=primary)
        _git("commit", "-q", "-m", "base", cwd=primary)
        _git("push", "-q", "origin", "HEAD:develop", cwd=primary)
        _git("fetch", "-q", cwd=primary)
        for i, (name, commit) in enumerate(worktrees.items()):
            path = tmp_path / "wt" / name
            _git("worktree", "add", "-q", "-b", f"feature/x-{name}", str(path), "origin/develop", cwd=primary)
            if commit:
                (path / f"f{i}").write_text(name, encoding="utf-8")
                _git("add", f"f{i}", cwd=path)
                _git("commit", "-q", "-m", name, cwd=path)
        return primary

    def test_open_work_with_no_board_block(self, world, mod, env, monkeypatch, tmp_path) -> None:
        primary = self._repo(tmp_path, {NAME: False, f"{NAME}-forgotten": True})
        monkeypatch.setattr(mod, "_git_worktrees", env.real_git_worktrees)
        world["board"] = f"## Main\nref: nexus-ct74m\nstate: in nexus-wt/{NAME}\n"
        text = world["write"](cwd=str(primary))
        assert f"no board thread names {tmp_path / 'wt' / (NAME + '-forgotten')}" in text.replace(os.path.realpath(tmp_path), str(tmp_path))
        assert "1 commit(s) not landed" in text

    def test_the_worktree_is_the_one_with_the_newest_commit_not_the_cwd(self, world, mod, env, monkeypatch, tmp_path) -> None:
        primary = self._repo(tmp_path, {NAME: True, f"{NAME}-live": False})
        live = tmp_path / "wt" / f"{NAME}-live"
        time.sleep(1.1)  # the reflog stamps whole seconds
        (live / "g").write_text("g", encoding="utf-8")
        _git("add", "g", cwd=live)
        _git("commit", "-q", "-m", "newest", cwd=live)
        monkeypatch.setattr(mod, "_git_worktrees", env.real_git_worktrees)
        world["board"] = "## Main\nref: nexus-ct74m\n"
        # Launched inside the stale worktree: before nexus-ct74m the cwd won (tamsin, 2026-11-12).
        text = world["write"](cwd=str(tmp_path / "wt" / NAME))
        header = text.split("\n## ", 1)[0]
        assert f"- worktree: {os.path.realpath(live)}" in header.replace(str(live), os.path.realpath(live))

    def test_a_board_worktree_line_is_the_first_choice(self, world, mod, env, monkeypatch, tmp_path) -> None:
        primary = self._repo(tmp_path, {NAME: True, f"{NAME}-declared": False})
        declared = tmp_path / "wt" / f"{NAME}-declared"
        monkeypatch.setattr(mod, "_git_worktrees", env.real_git_worktrees)
        world["board"] = f"## Main\nref: nexus-ct74m\nworktree: {declared}\n"
        header = world["write"](cwd=str(primary)).split("\n## ", 1)[0]
        assert "-declared" in header.split("- worktree: ", 1)[1].splitlines()[0]

    def test_background_shells_are_listed_with_start_and_command(self, world, mod, env) -> None:
        sf = mod._session_facts
        shell = ("/bin/zsh -c source /Users/x/.claude/shell-snapshots/snapshot-zsh-1.sh 2>/dev/null || true && "
                 "eval 'nx ci wait abc --branch develop > /tmp/ci.log 2>&1' < /dev/null && pwd -P >| /tmp/c")
        world["table"] = [sf.Proc(4242, 1, "Fri Nov 13 01:00:00 2026", "claude"),
                          sf.Proc(5000, 4242, "Fri Nov 13 01:05:00 2026", shell),
                          sf.Proc(5001, 5000, "Fri Nov 13 01:05:00 2026", "python nx ci wait abc"),
                          sf.Proc(6000, 4242, "Fri Nov 13 01:00:01 2026", "node mcp-server.js")]
        text = world["write"]()
        assert ("- background task pid 5000, started Fri Nov 13 01:05:00 2026: "
                "`nx ci wait abc --branch develop > /tmp/ci.log 2>&1`") in text
        assert "mcp-server" not in text.split("## Running for this session", 1)[1].split("## Working state")[0]

    def test_no_background_shell_says_none(self, world) -> None:
        assert "- background tasks: none running" in world["write"]()

    def test_processes_in_the_sessions_worktrees(self, world, mod, env, monkeypatch, tmp_path) -> None:
        self._repo(tmp_path, {NAME: True})
        wt = tmp_path / "wt" / NAME
        monkeypatch.setattr(mod, "_git_worktrees", env.real_git_worktrees)
        sf = mod._session_facts
        me = os.getpid()
        world["table"] = [sf.Proc(7000, 1, "Sat Nov  8 03:00:00 2026", "mineru-api --port 0"),
                          sf.Proc(me, 1, "Fri Nov 13 01:00:00 2026", "python compact_checkpoint.py pre"),
                          sf.Proc(7100, me, "Fri Nov 13 01:00:01 2026", "nx command-context continuation")]
        world["cwds"] = {7000: str(wt / "sub"), 7100: str(wt)}
        text = world["write"](cwd=str(tmp_path / "primary"))
        assert "- process pid 7000 in " in text and "`mineru-api --port 0`" in text
        assert "pid 7100" not in text  # the hook's own continuation child is not the session's work

    def test_crons_durable_ones_listed_and_the_session_ones_named_as_invisible(self, world, mod, env, monkeypatch, tmp_path) -> None:
        self._repo(tmp_path, {NAME: True})
        wt = tmp_path / "wt" / NAME
        (wt / ".claude").mkdir()
        (wt / ".claude" / "scheduled_tasks.json").write_text(json.dumps(
            {"tasks": [{"cron": "*/30 * * * *", "prompt": "check the board", "sessionId": SID},
                       {"cron": "0 9 * * *", "prompt": "a peer's", "sessionId": "someone-else"}]}), encoding="utf-8")
        monkeypatch.setattr(mod, "_git_worktrees", env.real_git_worktrees)
        text = world["write"](cwd=str(tmp_path / "primary"))
        assert "- durable cron: `*/30 * * * *` check the board" in text and "a peer's" not in text
        assert "run CronList after this compaction" in text

    def test_kept_output_is_stamped(self, world) -> None:
        text = world["write"]()
        stamp = r"\d{4}-\d\d-\d\dT\d\d:\d\d:\d\d\+00:00"
        assert re.search(rf"## Board checked against the facts\n\n\(checked {stamp}\)", text)
        assert re.search(rf"## Running for this session\n\n\(listed {stamp}\)", text)
        assert re.search(rf"## Working state\n\n\(nx command-context continuation, run {stamp}; old tool output", text)

    def test_a_failing_check_still_writes_the_checkpoint(self, world, mod, monkeypatch) -> None:
        world["board"] = "## X\nref: nexus-ct74m\n"
        monkeypatch.setattr(mod._session_facts, "board_wants", lambda _b: 1 / 0)
        monkeypatch.setattr(mod._session_facts, "process_table", lambda *_a: 1 / 0)
        text = world["write"]()
        assert "[board check failed: ZeroDivisionError]" in text and "[task listing failed: ZeroDivisionError]" in text
        assert "## Working state" in text


# ── parsing the facts rest on ────────────────────────────────────────────────


class TestParsing:
    def test_threads_read_the_fields_as_the_board_parser_does(self, sf) -> None:
        board = ("session: x\n## One\nref: nexus-aaaaa\nWaiting-On : nexus-bbbbb\nDue: none\nworktree: -\n"
                 "## Two\nworktree: /a path/with spaces\ndue: 2026-11-20\n")
        got = [(t.title, t.fields) for t in sf.threads(board)]
        assert got == [("One", [("ref", "nexus-aaaaa"), ("waiting_on", "nexus-bbbbb")]),
                       ("Two", [("worktree", "/a path/with spaces"), ("due", "2026-11-20")])]
        assert sf.board_worktree(board) == ["/a path/with spaces"]

    @pytest.mark.parametrize(("text", "beads", "engine", "client", "runs", "prs"), [
        ("nexus-f3yqd.2 and nexus-ct74m", {"nexus-f3yqd.2", "nexus-ct74m"}, set(), set(), set(), set()),
        ("bob's 0.1.190 cut", set(), {"engine-service-v0.1.190"}, set(), set(), set()),
        ("engine 0.1.191 and engine-service-v0.1.192", set(), {"engine-service-v0.1.191", "engine-service-v0.1.192"}, set(), set(), set()),
        ("conexus 8.2.2 then v8.3.0", set(), set(), {"v8.2.2", "v8.3.0"}, set(), set()),
        ("run 43278647831, https://github.com/o/r/actions/runs/43266578276", set(), set(), set(), {"43278647831", "43266578276"}, set()),
        ("PR #12 and https://github.com/o/r/pull/34", set(), set(), set(), set(), {"12", "34"}),
        ("nexus-wt/tamsin-cancel is a path, not a bead", set(), set(), set(), set(), set()),
    ])
    def test_facts_named(self, sf, text, beads, engine, client, runs, prs) -> None:
        w = sf.facts_named(text)
        assert set(w.beads) == beads and set(w.engine_tags) == engine
        assert set(w.client_tags) == client and set(w.runs) == runs and set(w.prs) == prs

    def test_a_due_date_is_due_from_local_midnight(self, sf) -> None:
        day = datetime(2026, 11, 20).astimezone()
        assert sf.parse_date("2026-11-20") == day
        assert sf.parse_date("2026-11-20T09:30:00Z") == datetime(2026, 11, 20, 9, 30, tzinfo=timezone.utc)
        assert sf.parse_date("2026-11-20T09:30") == datetime(2026, 11, 20, 9, 30).astimezone()
        for bad in ("next week", "2026-11-20 09:30", "20261120", "2026-11-20T09"):
            assert sf.parse_date(bad) is None, bad

    def test_a_due_line_that_is_not_a_date_is_no_due_date(self, sf) -> None:
        board = "## T\ndue: soon\n"
        assert sf.verify(board, sf.Facts(), datetime.now(timezone.utc)) == []

    def test_given_twice_the_last_due_counts(self, sf) -> None:
        now = datetime.now(timezone.utc)
        assert sf.verify("## T\ndue: 2026-01-01\ndue: 2999-01-01\n", sf.Facts(), now) == []
        assert sf.verify("## T\ndue: 2999-01-01\ndue: 2026-01-01\n", sf.Facts(), now) == ["- T", "  - due 2026-01-01: overdue"]

    def test_the_eval_body_is_the_task_command(self, sf) -> None:
        line = "zsh -c source /h/.claude/shell-snapshots/snapshot-zsh-1.sh && eval 'echo '\\''hi'\\'' > /x' < /dev/null"
        assert sf.task_command(line) == "echo 'hi' > /x"
        assert sf.task_command("plain  command") == "plain command"

    def test_ps_rows_parse(self, sf) -> None:
        out = ("  501   1 Fri Nov 13 19:33:02 2026     /usr/bin/claude --resume\n"
               "  777 501 Sat Nov  8 03:00:00 2026 /bin/zsh -c x\nnot a row\n")
        rows = sf.process_table(lambda _a, _t: (0, out))
        if sys.platform == "win32":
            assert rows == []
        else:
            assert rows == [sf.Proc(501, 1, "Fri Nov 13 19:33:02 2026", "/usr/bin/claude --resume"),
                            sf.Proc(777, 501, "Sat Nov 8 03:00:00 2026", "/bin/zsh -c x")]

    def test_background_tasks_are_snapshot_shells_below_the_sessions_claude(self, sf, tmp_path) -> None:
        (tmp_path / "4242.json").write_text(json.dumps({"pid": 4242, "sessionId": "s1"}), encoding="utf-8")
        snap = "zsh -c source /h/.claude/shell-snapshots/snapshot-zsh-1.sh && eval 'sleep 9'"
        table = [sf.Proc(4242, 1, "t", "claude"), sf.Proc(10, 4242, "t", snap), sf.Proc(11, 4242, "t", "mcp"),
                 sf.Proc(12, 4242, "t", snap), sf.Proc(20, 1, "t", snap)]
        assert [p.pid for p in sf.background_tasks("s1", tmp_path, table, exclude={12})] == [10]
        assert sf.background_tasks("unknown", tmp_path, table) == []

    def test_a_pipeline_in_a_worktree_shows_once(self, sf, tmp_path) -> None:
        table = [sf.Proc(1, 0, "t", "sh -c a | b"), sf.Proc(2, 1, "t", "a"), sf.Proc(3, 0, "t", "elsewhere")]
        cwds = {1: str(tmp_path), 2: str(tmp_path / "x"), 3: "/"}
        assert [p.pid for p, _r in sf.worktree_processes(table, cwds, [str(tmp_path)])] == [1]

    def test_last_commit_time_reads_commit_entries_not_checkouts(self, sf, tmp_path) -> None:
        gitdir = tmp_path / "g"
        (gitdir / "logs").mkdir(parents=True)
        (gitdir / "logs" / "HEAD").write_text(
            "0 1 t <t@t> 1000 +0000\tcommit: one\n"
            "1 2 t <t@t> 3000 +0000\tcheckout: moving from a to b\n"
            "2 3 t <t@t> 2000 +0000\tcherry-pick: two\n", encoding="utf-8")
        wt = tmp_path / "wt"
        wt.mkdir()
        (wt / ".git").write_text(f"gitdir: {gitdir}\n", encoding="utf-8")
        assert sf.last_commit_time(str(wt)) == 2000.0
        assert sf.last_commit_time(str(tmp_path / "missing")) == 0.0

    def test_unboarded_names_by_path_branch_or_bead(self, sf) -> None:
        w = sf.WorktreeWork("/w/tamsin-x", "feature/nexus-abcde-x", 2, 0)
        assert sf.unboarded([w], "## T\nref: nexus-abcde\n") == []
        assert sf.unboarded([w], "## T\nstate: in nexus-wt/tamsin-x\n") == []
        assert sf.unboarded([w], "## T\nref: nexus-zzzzz\n")[0].startswith("- no board thread names /w/tamsin-x")
        assert sf.unboarded([sf.WorktreeWork("/w/a", "b", 0, 0)], "") == []

    def test_resolve_beads_reads_open_then_the_rest_by_id(self, sf, monkeypatch) -> None:
        calls = []

        def fake(argv, timeout, cwd=None):
            calls.append(argv)
            if "--status" in argv:
                return 0, json.dumps({"issues": [{"id": "nexus-ep1c0", "status": "open", "issue_type": "epic"},
                                                 {"id": "nexus-ep1c0.1", "status": "open", "issue_type": "task",
                                                  "parent": "nexus-ep1c0"}]})
            return 0, json.dumps({"issues": [{"id": "nexus-c1osd", "status": "closed", "issue_type": "task"}]})

        monkeypatch.setattr(sf, "run", fake)
        got, why = sf.resolve_beads({"nexus-ep1c0", "nexus-c1osd", "fresh-start"}, "bd", None, 1.0)
        assert why == "" and got == {"nexus-ep1c0": ("open", "epic", 1), "nexus-c1osd": ("closed", "task", 0)}
        assert calls[1][-1] == "fresh-start,nexus-c1osd"

    def test_resolve_tags_one_ls_remote(self, sf, monkeypatch) -> None:
        monkeypatch.setattr(sf, "run", lambda argv, t, cwd=None: (0, "abc\trefs/tags/v8.2.2\nabd\trefs/tags/v8.2.2^{}\n"))
        assert sf.resolve_tags({"v8.2.2", "v9.0.0"}, "git", None, 1.0) == ({"v8.2.2": True, "v9.0.0": False}, "")
        monkeypatch.setattr(sf, "run", lambda *_a, **_k: (-1, ""))
        assert sf.resolve_tags({"v8.2.2"}, "git", None, 1.0)[1].startswith("tags not checked")

    def test_durable_crons_tolerate_shapes(self, sf, tmp_path) -> None:
        (tmp_path / ".claude").mkdir()
        (tmp_path / ".claude" / "scheduled_tasks.json").write_text(json.dumps([{"cron": "@hourly", "prompt": "p"}]), encoding="utf-8")
        assert sf.durable_crons([str(tmp_path), str(tmp_path)]) == [f"`@hourly` p ({tmp_path / '.claude' / 'scheduled_tasks.json'})"]
        (tmp_path / ".claude" / "scheduled_tasks.json").write_text("{not json", encoding="utf-8")
        assert sf.durable_crons([str(tmp_path)]) == []


def test_verify_lists_only_threads_with_findings(sf) -> None:
    facts = sf.Facts(beads={"nexus-c1osd": ("closed", "task", 0), "nexus-0pen0": ("open", "task", 0)})
    board = "## Stale\nref: nexus-c1osd\n## Fine\nref: nexus-0pen0\n"
    assert sf.verify(board, facts, datetime.now(timezone.utc) - timedelta(0)) == [
        "- Stale", "  - ref nexus-c1osd: closed, so this line is stale"]
