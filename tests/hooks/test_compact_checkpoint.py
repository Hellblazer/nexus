# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (c) 2026 Hal Hildebrand. All rights reserved.
"""``compact_checkpoint.py``: auto-continuation across compaction (bead nexus-3lqxv).

PreCompact writes the session's working state to a file, PostCompact appends the
summary, and the SessionStart group whose matcher is ``compact`` puts the file back
in front of the model. The contract these tests pin:

- ``pre`` never vetoes (exit 0 on every path, nothing on stdout), survives a missing
  ``nx`` and a timeout, and writes the file atomically with mode 0600.
- ``post`` appends and logs a warning when the summary names none of the pointers; it
  never appends to, or refreshes, a checkpoint whose ``written:`` stamp is old.
- ``inject`` emits the capped ``additionalContext`` for THIS session only and nothing
  for a stale (by its ``written:`` stamp, not its mtime) or foreign one, and starts no
  process.
- the board entry comes first and is the one titled EXACTLY the session's name.
- the script imports only the standard library and the plugin's own ``_`` modules.
"""
from __future__ import annotations

import ast
import importlib.util
import io
import json
import os
import re
import shutil
import subprocess
import sys
import textwrap
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

from tests._module_seam import setattr_in

SCRIPT = Path(__file__).resolve().parents[2] / "conexus" / "hooks" / "scripts" / "compact_checkpoint.py"
HOOKS_JSON = Path(__file__).resolve().parents[2] / "conexus" / "hooks" / "hooks.json"

SID = "11111111-2222-3333-4444-555555555555"
OTHER_SID = "99999999-8888-7777-6666-555555555555"
NAME = "linda"

#: A stand-in for ``nx``. ``command-context continuation`` prints a block shaped like the
#: real one (measured 2026-10-31); ``memory get`` prints a board entry or fails. Every
#: argv is appended to FAKE_NX_LOG so a test can say what was and was not asked.
_STANDIN = textwrap.dedent(
    """
    import os, sys, time
    args = sys.argv[1:]
    with open(os.environ["FAKE_NX_LOG"], "a", encoding="utf-8") as log:
        log.write(" ".join(args) + "\\n")
    if os.environ.get("FAKE_NX_HANG"):
        print("## Working state\\npartial before the hang", flush=True)
        time.sleep(30)
    TITLES = {"exact": ["linda"], "prefix": ["linda_2"], "missing": []}[os.environ.get("FAKE_BOARD", "exact")]
    if args[:2] == ["command-context", "continuation"]:
        with open(os.environ["FAKE_NX_LOG"] + ".cwd", "w", encoding="utf-8") as log:
            log.write(os.getcwd())
        ready = "\\n".join(f"o nexus-r{i:04d} ready bead number {i} " + "x" * 80 for i in range(int(os.environ.get("FAKE_READY", "3"))))
        commits = "\\n".join(f"abc{i:04d} commit number {i} " + "x" * 80 for i in range(int(os.environ.get("FAKE_COMMITS", "3"))))
        print("**Target file:** `/tmp/handoff-target.md`\\n\\n**Topic:** current branch feature/x\\n\\n"
              "## Working state\\n\\n- **cwd:** `/w`\\n- **branch:** `feature/x`\\n- **HEAD:** `abc1 head`\\n\\n"
              "### Uncommitted\\n M a.py\\n\\n"
              "### Recent commits (last 10 on this branch)\\n" + commits + "\\n\\n"
              "### Open PRs from this branch\\n(none)\\n\\n"
              "### In-progress beads\\no nexus-3lqxv Auto-continuation\\n"
              "├── o nexus-3lqxv.2 its child\\no nexus-zz9yx another session's bead\\n\\n"
              "----\\nTotal: 3 issues\\n\\n"
              "### Ready beads (top 10)\\n" + ready + "\\n\\n### nx memory (x) titles\\nmemo-one\\nmemo-two\\n\\n"
              "### Feedback memories\\nfeedback-one\\n")
    elif args[:2] == ["memory", "list"]:
        for i, t in enumerate(TITLES):
            print(f"[{101 + i}] threads/{t}  (-, 2026-10-31T21:46:51Z)")
    elif args[:2] == ["memory", "get"]:
        if "--title" in args:  # the real CLI: exact, then a unique prefix
            want = args[args.index("--title") + 1]
            hits = [t for t in TITLES if t == want] or [t for t in TITLES if t.startswith(want)]
            if len(hits) != 1:
                print("not found", file=sys.stderr)
                sys.exit(1)
            title = hits[0]
        else:
            title = TITLES[int(args[2]) - 101]
        print(f"## board of {title}\\nref: nexus-3lqxv\\nstate: building")
    """
)


@pytest.fixture()
def mod():
    spec = importlib.util.spec_from_file_location("compact_checkpoint_under_test", SCRIPT)
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


@pytest.fixture()
def env(tmp_path: Path, monkeypatch, mod):
    """Isolated config and Claude dirs, a recording ``nx`` stand-in, a registered session."""
    config = tmp_path / "config"
    claude = tmp_path / "claude"
    (claude / "sessions").mkdir(parents=True)
    (claude / "sessions" / "4242.json").write_text(json.dumps({"pid": 4242, "sessionId": SID, "name": NAME}), encoding="utf-8")
    (claude / "sessions" / "4243.json").write_text(json.dumps({"pid": 4243, "sessionId": OTHER_SID, "name": "other"}), encoding="utf-8")
    standin = tmp_path / "fake_nx.py"
    standin.write_text(_STANDIN, encoding="utf-8")
    nx_log = tmp_path / "nx.log"
    monkeypatch.setenv("NEXUS_CONFIG_DIR", str(config))
    monkeypatch.setenv("CLAUDE_CONFIG_DIR", str(claude))
    monkeypatch.setenv("FAKE_NX_LOG", str(nx_log))
    monkeypatch.delenv("FAKE_NX_HANG", raising=False)
    monkeypatch.delenv("FAKE_BOARD", raising=False)
    monkeypatch.delenv("FAKE_READY", raising=False)
    monkeypatch.delenv("FAKE_COMMITS", raising=False)
    monkeypatch.setattr(mod, "_nx_command", lambda: [sys.executable, str(standin)])
    # The test checkout's own worktrees must not decide where continuation runs.
    real_git_worktrees = mod._git_worktrees
    monkeypatch.setattr(mod, "_git_worktrees", lambda _cwd, _timeout: [])

    class Env:
        pass

    e = Env()
    e.real_git_worktrees = real_git_worktrees
    e.config, e.claude, e.nx_log = config, claude, nx_log
    e.checkpoints = config / "compact-checkpoints"
    e.hook_log = config / "logs" / "hook.log"
    return e


def run(mod, monkeypatch, mode: str, payload: object) -> tuple[int, str]:
    stdin = payload if isinstance(payload, str) else json.dumps(payload)
    monkeypatch.setattr(sys, "stdin", io.StringIO(stdin))
    out = io.StringIO()
    monkeypatch.setattr(sys, "stdout", out)
    code = mod.main([mode])
    return code, out.getvalue()


def pre_payload(**kw) -> dict:
    return {"session_id": SID, "hook_event_name": "PreCompact", "trigger": "auto", "cwd": os.getcwd(), **kw}


def restamp(path: Path, seconds_ago: float) -> None:
    """Rewrite the checkpoint's own ``written:`` stamp, leaving its mtime alone (now)."""
    stamp = (datetime.now(timezone.utc) - timedelta(seconds=seconds_ago)).isoformat(timespec="seconds")
    text = path.read_text(encoding="utf-8")
    text, n = re.subn(r"(?m)^- written: .*$", f"- written: {stamp}", text, count=1)
    assert n == 1
    path.write_text(text, encoding="utf-8")


def nx_calls(env) -> list[str]:
    return env.nx_log.read_text(encoding="utf-8").splitlines() if env.nx_log.exists() else []


class TestPre:
    def test_writes_the_working_state_and_the_board_entry(self, mod, env, monkeypatch) -> None:
        code, out = run(mod, monkeypatch, "pre", pre_payload())
        assert code == 0 and out == ""
        text = (env.checkpoints / f"{SID}.md").read_text(encoding="utf-8")
        assert f"- session: {SID}" in text
        assert "- trigger: auto" in text
        assert "- written: 20" in text and text.count("+00:00") >= 1  # a UTC stamp
        assert "### In-progress beads" in text and "nexus-3lqxv Auto-continuation" in text
        assert f"## Threads board entry (threads/{NAME})" in text and "state: building" in text
        assert nx_calls(env) == [
            "command-context continuation", "memory list --project threads", "memory get 101 --project threads"
        ]

    def test_the_board_entry_comes_first_and_the_kept_sections_follow_in_order(self, mod, env, monkeypatch) -> None:
        run(mod, monkeypatch, "pre", pre_payload())
        text = (env.checkpoints / f"{SID}.md").read_text(encoding="utf-8")
        order = ["## Threads board entry", "## Working state", "- **branch:**", "### In-progress beads", "### Uncommitted",
                 "### Open PRs", "### Recent commits"]
        positions = [text.index(h) for h in order]
        assert positions == sorted(positions), dict(zip(order, positions))
        assert text.count("## Working state") == 1

    def test_ready_beads_memory_titles_and_the_handoff_cues_are_not_kept(self, mod, env, monkeypatch) -> None:
        run(mod, monkeypatch, "pre", pre_payload())
        text = (env.checkpoints / f"{SID}.md").read_text(encoding="utf-8")
        for gone in ("Ready beads", "nexus-r0000", "nx memory", "memo-one", "Feedback memories", "feedback-one",
                     "Target file", "handoff-target", "Topic:"):
            assert gone not in text, gone

    def test_a_prefix_match_is_not_this_sessions_board(self, mod, env, monkeypatch) -> None:
        """``memory get --title linda`` resolves to a unique-prefix ``linda_2``; its board is not ours."""
        monkeypatch.setenv("FAKE_BOARD", "prefix")
        run(mod, monkeypatch, "pre", pre_payload())
        text = (env.checkpoints / f"{SID}.md").read_text(encoding="utf-8")
        assert "board of linda_2" not in text and "[no board entry:" in text and "titled exactly" in text
        assert not any(c.startswith("memory get") for c in nx_calls(env))

    def test_a_key_file_is_not_a_session_source(self, mod, env, monkeypatch) -> None:
        third = "33333333-3333-3333-3333-333333333333"
        (env.claude / "sessions" / "9.key").write_text(json.dumps({"sessionId": third, "name": "from_the_key"}), encoding="utf-8")
        run(mod, monkeypatch, "pre", pre_payload(session_id=third))
        text = (env.checkpoints / f"{third}.md").read_text(encoding="utf-8")
        assert "from_the_key" not in text and "- session name:" not in text
        assert nx_calls(env) == ["command-context continuation"]

    def test_no_matching_sessions_file_is_logged(self, mod, env, monkeypatch) -> None:
        (env.claude / "sessions" / "4242.json").unlink()
        run(mod, monkeypatch, "pre", pre_payload())
        assert f"carries session id {SID}" in env.hook_log.read_text(encoding="utf-8")

    @pytest.mark.skipif(sys.platform == "win32", reason="POSIX file modes")
    def test_the_file_is_mode_0600_and_no_temp_file_is_left(self, mod, env, monkeypatch) -> None:
        run(mod, monkeypatch, "pre", pre_payload())
        assert (env.checkpoints / f"{SID}.md").stat().st_mode & 0o777 == 0o600
        assert [p.name for p in env.checkpoints.iterdir()] == [f"{SID}.md"]

    def test_an_unresolvable_session_name_skips_the_board(self, mod, env, monkeypatch) -> None:
        (env.claude / "sessions" / "4242.json").unlink()
        run(mod, monkeypatch, "pre", pre_payload())
        text = (env.checkpoints / f"{SID}.md").read_text(encoding="utf-8")
        assert "Threads board entry" not in text and "- session name:" not in text
        assert nx_calls(env) == ["command-context continuation"]

    def test_a_missing_board_entry_is_recorded_not_fatal(self, mod, env, monkeypatch) -> None:
        monkeypatch.setenv("FAKE_BOARD", "missing")
        code, _ = run(mod, monkeypatch, "pre", pre_payload())
        text = (env.checkpoints / f"{SID}.md").read_text(encoding="utf-8")
        assert code == 0 and "[no board entry:" in text and "### In-progress beads" in text

    def test_in_progress_keeps_only_the_beads_the_board_names(self, mod, env, monkeypatch) -> None:
        """Every session claims under one assignee; the board's ref is this session's own list."""
        run(mod, monkeypatch, "pre", pre_payload())
        section = mod._in_progress_section((env.checkpoints / f"{SID}.md").read_text(encoding="utf-8"))
        assert "nexus-3lqxv Auto-continuation" in section and "nexus-3lqxv.2 its child" in section
        assert "nexus-zz9yx" not in section and "Total:" not in section
        assert "(2 of 3 in-progress beads" in section

    def test_without_a_board_the_in_progress_list_is_whole(self, mod, env, monkeypatch) -> None:
        monkeypatch.setenv("FAKE_BOARD", "missing")
        run(mod, monkeypatch, "pre", pre_payload())
        section = mod._in_progress_section((env.checkpoints / f"{SID}.md").read_text(encoding="utf-8"))
        assert "nexus-zz9yx" in section and "in-progress beads: the ones" not in section

    def test_continuation_runs_in_the_worktree_named_after_the_session(self, mod, env, monkeypatch, tmp_path) -> None:
        """The payload cwd is the launch directory (the shared primary); the session works in its worktree."""
        primary = tmp_path / "primary"
        primary.mkdir()
        git = ["git", "-c", "user.name=t", "-c", "user.email=t@example.invalid", "-C", str(primary)]
        subprocess.run([*git, "init", "-q"], check=True)
        subprocess.run([*git, "commit", "-q", "--allow-empty", "-m", "root"], check=True)
        for wt in ("linda_2", NAME):  # a prefix-named worktree is not the session's
            subprocess.run([*git, "worktree", "add", "-q", "-b", f"feature/{wt}", str(tmp_path / "wt" / wt)], check=True)
        monkeypatch.setattr(mod, "_git_worktrees", env.real_git_worktrees)
        run(mod, monkeypatch, "pre", pre_payload(cwd=str(primary)))
        expected = os.path.realpath(tmp_path / "wt" / NAME)
        assert os.path.realpath(Path(str(env.nx_log) + ".cwd").read_text(encoding="utf-8")) == expected
        text = (env.checkpoints / f"{SID}.md").read_text(encoding="utf-8")
        header = text.split("\n## ", 1)[0]
        assert "- worktree: " in header and header.rstrip().endswith(f"wt/{NAME}")

    def test_no_session_worktree_keeps_the_payload_cwd(self, mod, env, monkeypatch, tmp_path) -> None:
        monkeypatch.setattr(mod, "_git_worktrees", env.real_git_worktrees)
        run(mod, monkeypatch, "pre", pre_payload(cwd=str(tmp_path)))  # not a repository
        assert os.path.realpath(Path(str(env.nx_log) + ".cwd").read_text(encoding="utf-8")) == os.path.realpath(tmp_path)
        assert "- worktree:" not in (env.checkpoints / f"{SID}.md").read_text(encoding="utf-8")

    def test_survives_a_missing_nx(self, mod, env, monkeypatch) -> None:
        monkeypatch.setattr(mod, "_nx_command", lambda: None)
        code, out = run(mod, monkeypatch, "pre", pre_payload())
        text = (env.checkpoints / f"{SID}.md").read_text(encoding="utf-8")
        assert code == 0 and out == ""
        assert "[working state incomplete: the nx CLI was not found]" in text
        assert f"- session: {SID}" in text
        assert "the nx CLI was not found" in env.hook_log.read_text(encoding="utf-8")

    def test_survives_a_timeout_and_writes_an_error_line(self, mod, env, monkeypatch) -> None:
        monkeypatch.setenv("FAKE_NX_HANG", "1")
        monkeypatch.setattr(mod, "_NX_TIMEOUT_S", 1.0)
        monkeypatch.setattr(mod, "_BOARD_TIMEOUT_S", 1.0)
        started = time.monotonic()
        code, out = run(mod, monkeypatch, "pre", pre_payload())
        assert time.monotonic() - started < 15
        text = (env.checkpoints / f"{SID}.md").read_text(encoding="utf-8")
        assert code == 0 and out == ""
        assert "timed out after 1s" in text and "[working state incomplete:" in text

    @pytest.mark.parametrize(
        "payload", ["", "not json", "[1, 2]", {}, {"session_id": "../../etc/passwd"}, {"session_id": ""}]
    )
    def test_a_bad_payload_exits_zero_and_writes_nothing(self, mod, env, monkeypatch, payload) -> None:
        code, out = run(mod, monkeypatch, "pre", payload)
        assert code == 0 and out == ""
        assert not env.checkpoints.exists() or not any(env.checkpoints.iterdir())

    def test_an_unwritable_config_dir_still_exits_zero(self, mod, env, monkeypatch, tmp_path) -> None:
        blocker = tmp_path / "blocker"
        blocker.write_text("a file where the config dir should be", encoding="utf-8")
        monkeypatch.setenv("NEXUS_CONFIG_DIR", str(blocker))
        code, out = run(mod, monkeypatch, "pre", pre_payload())
        assert code == 0 and out == ""

    def test_a_path_shaped_session_id_creates_nothing_outside_the_checkpoint_directory(
        self, mod, env, monkeypatch
    ) -> None:
        for bad in ("../escape", "..", "a/../../escape", "/abs"):
            run(mod, monkeypatch, "pre", pre_payload(session_id=bad))
            run(mod, monkeypatch, "post", {"session_id": bad, "compact_summary": "x"})
        outside = [
            p for p in env.config.rglob("*")
            if p.is_file() and env.checkpoints not in p.parents and "logs" not in p.relative_to(env.config).parts
        ]
        assert outside == [], outside
        assert not env.checkpoints.exists() or not any(env.checkpoints.iterdir())

    def test_an_unexpected_error_in_the_handler_exits_zero(self, mod, env, monkeypatch) -> None:
        def boom(*_a, **_k):
            raise RuntimeError("boom")

        monkeypatch.setattr(mod, "_run_nx", boom)
        code, out = run(mod, monkeypatch, "pre", pre_payload())
        assert code == 0 and out == ""
        assert "unexpected RuntimeError: boom" in env.hook_log.read_text(encoding="utf-8")

    def test_unknown_mode_exits_zero(self, mod, env, monkeypatch) -> None:
        code, out = run(mod, monkeypatch, "bogus", pre_payload())
        assert code == 0 and out == ""

    def test_keeps_the_last_three_superseded_checkpoints_and_prunes_old_files(self, mod, env, monkeypatch) -> None:
        env.checkpoints.mkdir(parents=True)
        old = env.checkpoints / f"{OTHER_SID}.md"
        old.write_text("someone else, three days old", encoding="utf-8")
        three_days = time.time() - 3 * 86400
        os.utime(old, (three_days, three_days))
        fresh_other = env.checkpoints / f"{OTHER_SID}.20260101T000000Z.md"
        fresh_other.write_text("someone else's archive, fresh", encoding="utf-8")
        for n in range(6):
            run(mod, monkeypatch, "pre", pre_payload(trigger=f"t{n}"))
            # distinct mtimes so each supersession gets its own archive stamp
            current = env.checkpoints / f"{SID}.md"
            stamp = time.time() - (60 - n)
            os.utime(current, (stamp, stamp))
        names = sorted(p.name for p in env.checkpoints.iterdir())
        mine = [n for n in names if n.startswith(SID)]
        assert f"{SID}.md" in mine and len(mine) == 4, names  # current + 3 archives
        assert "- trigger: t5" in (env.checkpoints / f"{SID}.md").read_text(encoding="utf-8")
        assert not old.exists(), "a file older than two days survived the prune"
        assert fresh_other.exists(), "another session's recent file must be left alone"


class TestPost:
    def _pre(self, mod, env, monkeypatch) -> Path:
        run(mod, monkeypatch, "pre", pre_payload())
        return env.checkpoints / f"{SID}.md"

    def test_appends_the_summary_under_a_heading(self, mod, env, monkeypatch) -> None:
        path = self._pre(mod, env, monkeypatch)
        before = path.read_text(encoding="utf-8")
        code, out = run(mod, monkeypatch, "post", {"session_id": SID, "trigger": "auto",
                                                   "compact_summary": "We were fixing nexus-3lqxv."})
        text = path.read_text(encoding="utf-8")
        assert code == 0 and out == ""
        assert text.startswith(before.rstrip())
        assert "## Compact summary (" in text and text.rstrip().endswith("We were fixing nexus-3lqxv.")

    def test_warns_when_the_summary_names_no_pointer(self, mod, env, monkeypatch) -> None:
        self._pre(mod, env, monkeypatch)
        run(mod, monkeypatch, "post", {"session_id": SID, "compact_summary": "We discussed things at length."})
        log = env.hook_log.read_text(encoding="utf-8")
        assert log.count("names none of") == 1 and SID in log

    @pytest.mark.parametrize(
        "summary", ["see nexus-3lqxv", f"the board entry {NAME} has it", "file: {path}"], ids=["bead", "board", "path"]
    )
    def test_no_warning_when_a_pointer_is_named(self, mod, env, monkeypatch, summary) -> None:
        path = self._pre(mod, env, monkeypatch)
        run(mod, monkeypatch, "post", {"session_id": SID, "compact_summary": summary.format(path=path)})
        assert not env.hook_log.exists() or "names none of" not in env.hook_log.read_text(encoding="utf-8")

    def test_without_a_precompact_checkpoint_it_still_records_the_summary(self, mod, env, monkeypatch) -> None:
        code, _ = run(mod, monkeypatch, "post", {"session_id": SID, "compact_summary": "orphan summary"})
        text = (env.checkpoints / f"{SID}.md").read_text(encoding="utf-8")
        assert code == 0 and "no pre-compaction checkpoint for this compaction" in text and "orphan summary" in text

    def test_an_old_checkpoint_is_archived_and_replaced_by_a_stub_not_appended_to(self, mod, env, monkeypatch) -> None:
        path = self._pre(mod, env, monkeypatch)
        restamp(path, 3 * 3600)
        run(mod, monkeypatch, "post", {"session_id": SID, "compact_summary": "this compaction's summary"})
        text = path.read_text(encoding="utf-8")
        assert "state: building" not in text and "- written:" not in text
        assert "no pre-compaction checkpoint for this compaction" in text and "this compaction's summary" in text
        archives = [p for p in env.checkpoints.iterdir() if p.name != path.name]
        assert len(archives) == 1 and "state: building" in archives[0].read_text(encoding="utf-8")
        assert "no fresh pre-compaction checkpoint" in env.hook_log.read_text(encoding="utf-8")

    def test_has_no_decision_output_and_exits_zero_on_garbage(self, mod, env, monkeypatch) -> None:
        for payload in ("", "nope", {"session_id": "bad id"}):
            code, out = run(mod, monkeypatch, "post", payload)
            assert code == 0 and out == ""


class TestInject:
    def _ctx(self, out: str) -> str:
        data = json.loads(out)
        assert data["hookSpecificOutput"]["hookEventName"] == "SessionStart"
        assert set(data) == {"hookSpecificOutput"}, "inject must carry no decision or other control key"
        return data["hookSpecificOutput"]["additionalContext"]

    def _no_process(self, mod, monkeypatch) -> None:
        def refuse(*_a, **_k):
            raise AssertionError("inject must not start a process")

        setattr_in(monkeypatch, mod, "subprocess.run", refuse)
        setattr_in(monkeypatch, mod, "subprocess.Popen", refuse)
        monkeypatch.setattr(mod, "_nx_command", refuse)

    def test_emits_this_sessions_checkpoint_and_the_path(self, mod, env, monkeypatch) -> None:
        run(mod, monkeypatch, "pre", pre_payload())
        run(mod, monkeypatch, "post", {"session_id": SID, "compact_summary": "SUMMARY-SENTINEL"})
        self._no_process(mod, monkeypatch)
        code, out = run(mod, monkeypatch, "inject", {"session_id": SID, "source": "compact"})
        ctx = self._ctx(out)
        assert code == 0
        assert "nexus-3lqxv Auto-continuation" in ctx and "state: building" in ctx
        assert str(env.checkpoints / f"{SID}.md") in ctx.splitlines()[-1]
        assert "SUMMARY-SENTINEL" not in ctx, "the summary is already in the new context; do not repeat it"

    def test_a_foreign_session_gets_nothing(self, mod, env, monkeypatch) -> None:
        run(mod, monkeypatch, "pre", pre_payload())
        code, out = run(mod, monkeypatch, "inject", {"session_id": OTHER_SID})
        assert code == 0 and out == ""
        # and the path-shaped ids cannot reach another file
        for bad in ("../" + SID, SID + "/../x", "", None):
            assert run(mod, monkeypatch, "inject", {"session_id": bad})[1] == ""

    def test_a_stale_stamp_gets_nothing_whatever_the_mtime_says(self, mod, env, monkeypatch) -> None:
        run(mod, monkeypatch, "pre", pre_payload())
        path = env.checkpoints / f"{SID}.md"
        restamp(path, 2 * 3600)  # the mtime stays fresh, as after a rewrite
        code, out = run(mod, monkeypatch, "inject", {"session_id": SID})
        assert code == 0 and out == ""

    def test_a_missing_or_unparseable_stamp_gets_nothing(self, mod, env, monkeypatch) -> None:
        run(mod, monkeypatch, "pre", pre_payload())
        path = env.checkpoints / f"{SID}.md"
        good = path.read_text(encoding="utf-8")
        for broken in (re.sub(r"(?m)^- written: .*\n", "", good), re.sub(r"(?m)^- written: .*$", "- written: yesterday", good)):
            path.write_text(broken, encoding="utf-8")
            assert run(mod, monkeypatch, "inject", {"session_id": SID}) == (0, "")

    def test_a_checkpoint_from_before_this_compaction_is_never_injected_after_a_post_without_a_pre(
        self, mod, env, monkeypatch
    ) -> None:
        """The ship-blocker: pre did not run for this compaction, post must not refresh the old file."""
        run(mod, monkeypatch, "pre", pre_payload())
        restamp(env.checkpoints / f"{SID}.md", 3 * 3600)  # the previous compaction's checkpoint
        run(mod, monkeypatch, "post", {"session_id": SID, "compact_summary": "the summary of this compaction"})
        code, out = run(mod, monkeypatch, "inject", {"session_id": SID, "source": "compact"})
        assert code == 0 and out == ""

    def test_the_intro_states_the_checkpoints_age_and_trigger(self, mod, env, monkeypatch) -> None:
        run(mod, monkeypatch, "pre", pre_payload(trigger="auto"))
        restamp(env.checkpoints / f"{SID}.md", 3 * 60)
        ctx = self._ctx(run(mod, monkeypatch, "inject", {"session_id": SID})[1])
        assert "3 min ago (trigger auto)" in ctx.splitlines()[0]

    def test_a_path_shaped_session_id_cannot_reach_a_file_outside_the_checkpoint_directory(
        self, mod, env, monkeypatch
    ) -> None:
        planted = env.config / f"{SID}.md"  # what ``"../" + SID`` resolves to
        env.config.mkdir(parents=True, exist_ok=True)
        stamp = datetime.now(timezone.utc).isoformat(timespec="seconds")
        planted.write_text(f"# Compaction checkpoint\n\n- session: x\n- trigger: auto\n- written: {stamp}\n\n"
                           "## Working state\n\nPLANTED-SENTINEL\n", encoding="utf-8")
        (env.config / "compact-checkpoints").mkdir(parents=True, exist_ok=True)
        for bad in ("../" + SID, "..", SID + "/../" + SID):
            code, out = run(mod, monkeypatch, "inject", {"session_id": bad})
            assert (code, out) == (0, ""), bad

    def test_no_checkpoint_gets_nothing(self, mod, env, monkeypatch) -> None:
        assert run(mod, monkeypatch, "inject", {"session_id": SID}) == (0, "")

    def test_over_the_cap_the_recent_commits_go_first_and_the_board_entry_stays(self, mod, env, monkeypatch) -> None:
        monkeypatch.setenv("FAKE_COMMITS", "200")
        run(mod, monkeypatch, "pre", pre_payload())
        full = (env.checkpoints / f"{SID}.md").read_text(encoding="utf-8")
        assert len(full.encode()) > mod._INJECT_CAP_BYTES * 2
        ctx = self._ctx(run(mod, monkeypatch, "inject", {"session_id": SID})[1])
        assert len(ctx.encode()) <= 8192
        assert "nexus-3lqxv Auto-continuation" in ctx and "state: building" in ctx
        assert "commit number 100" not in ctx and "[omitted for size" in ctx
        assert ctx.splitlines()[-1].startswith("Full checkpoint")

    def test_a_working_state_that_alone_exceeds_the_cap_is_truncated_with_the_path_kept(
        self, mod, env, monkeypatch
    ) -> None:
        env.checkpoints.mkdir(parents=True)
        stamp = datetime.now(timezone.utc).isoformat(timespec="seconds")
        (env.checkpoints / f"{SID}.md").write_text(
            f"# Compaction checkpoint\n\n- written: {stamp}\n\n## Working state\n\n" + "é" * 20000 + "\n", encoding="utf-8"
        )
        ctx = self._ctx(run(mod, monkeypatch, "inject", {"session_id": SID})[1])
        assert len(ctx.encode()) <= 8192
        assert "[truncated for size" in ctx and ctx.splitlines()[-1].startswith("Full checkpoint")

    def test_a_small_checkpoint_is_not_cut(self, mod, env, monkeypatch) -> None:
        run(mod, monkeypatch, "pre", pre_payload())
        ctx = self._ctx(run(mod, monkeypatch, "inject", {"session_id": SID})[1])
        assert "omitted for size" not in ctx and "truncated" not in ctx and "commit number 0" in ctx


class TestScript:
    def test_imports_only_the_standard_library_and_the_plugins_own_modules(self) -> None:
        tree = ast.parse(SCRIPT.read_text(encoding="utf-8"))
        roots: set[str] = set()
        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                roots |= {a.name.split(".")[0] for a in node.names}
            elif isinstance(node, ast.ImportFrom) and node.level == 0 and node.module:
                roots.add(node.module.split(".")[0])
        assert roots, "the walk found no imports; it went blind"
        outside = {r for r in roots if r not in sys.stdlib_module_names and not r.startswith("_")}
        assert not outside, outside
        assert {r for r in roots if r.startswith("_")} <= {"__future__", "_cli_lookup", "_endpoint_resolve"}
        assert (SCRIPT.parent / "_cli_lookup.py").is_file() and (SCRIPT.parent / "_endpoint_resolve.py").is_file()

    def test_the_sibling_imports_sit_inside_main_so_an_import_error_exits_zero(self, tmp_path: Path) -> None:
        tree = ast.parse(SCRIPT.read_text(encoding="utf-8"))
        top = {a.name for n in tree.body if isinstance(n, ast.Import) for a in n.names}
        assert not top & {"_cli_lookup", "_endpoint_resolve"}, "a module-level import of a sibling bypasses main's guard"
        lone = tmp_path / "lone"
        lone.mkdir()
        shutil.copy(SCRIPT, lone / SCRIPT.name)  # no _cli_lookup / _endpoint_resolve beside it
        for mode in ("pre", "post", "inject"):
            done = subprocess.run([sys.executable, str(lone / SCRIPT.name), mode], input=json.dumps({"session_id": SID}),
                                  capture_output=True, text=True, encoding="utf-8", timeout=60, check=False)
            assert done.returncode == 0 and done.stdout == "", (mode, done.stderr)
            assert "ModuleNotFoundError" in done.stderr or "ImportError" in done.stderr, done.stderr

    @pytest.mark.parametrize("text", ["nexus-3lqxv", "nexus-6pfgb.27", "see nexus-0abcd, then", "(nexus-zz9yx)"])
    def test_a_bead_id_matches(self, mod, text) -> None:
        assert mod._BEAD_ID_RE.findall(text), text

    @pytest.mark.parametrize("text", ["ready-beads", "auto-memory", "nexus-abcdef", "nexus-abcd", "pre-compact", "nx-memory"])
    def test_an_ordinary_hyphenated_word_is_not_a_bead_id(self, mod, text) -> None:
        assert mod._BEAD_ID_RE.findall(text) == [], text

    def test_never_exits_with_a_veto_code_or_prints_a_decision(self) -> None:
        text = SCRIPT.read_text(encoding="utf-8")
        assert "sys.exit(2" not in text and "exit(2" not in text and '"decision"' not in text and "'block'" not in text

    @pytest.mark.skipif(sys.platform == "win32", reason="the stand-in is a POSIX executable")
    def test_end_to_end_as_hooks_json_launches_it(self, tmp_path: Path) -> None:
        """A real subprocess: nx found through PATH, config through the env, exit 0, three modes."""
        bindir = tmp_path / "bin"
        bindir.mkdir()
        nx = bindir / "nx"
        nx.write_text(f"#!{sys.executable}\n{_STANDIN}", encoding="utf-8")
        nx.chmod(0o755)
        claude = tmp_path / "claude"
        (claude / "sessions").mkdir(parents=True)
        (claude / "sessions" / "1.json").write_text(json.dumps({"sessionId": SID, "name": NAME}), encoding="utf-8")
        env = {
            "PATH": f"{bindir}{os.pathsep}/usr/bin{os.pathsep}/bin",
            "HOME": str(tmp_path / "home"),
            "NEXUS_CONFIG_DIR": str(tmp_path / "config"),
            "CLAUDE_CONFIG_DIR": str(claude),
            "NX_TOOLS_DIR": str(tmp_path / "no-tools"),
            "FAKE_NX_LOG": str(tmp_path / "nx.log"),
        }

        def launch(mode: str, payload: dict | str) -> subprocess.CompletedProcess:
            return subprocess.run(
                [sys.executable, str(SCRIPT), mode], input=payload if isinstance(payload, str) else json.dumps(payload),
                capture_output=True, text=True, encoding="utf-8", timeout=60, env=env, check=False,
            )

        pre = launch("pre", pre_payload())
        assert pre.returncode == 0 and pre.stdout == "", pre.stderr
        assert (tmp_path / "config" / "compact-checkpoints" / f"{SID}.md").is_file()
        post = launch("post", {"session_id": SID, "compact_summary": "nexus-3lqxv"})
        assert post.returncode == 0 and post.stdout == ""
        inject = launch("inject", {"session_id": SID, "source": "compact"})
        assert inject.returncode == 0
        assert "additionalContext" in json.loads(inject.stdout)["hookSpecificOutput"]
        assert launch("inject", {"session_id": OTHER_SID}).stdout == ""
        assert launch("pre", "garbage").returncode == 0
        assert launch("nonsense", {}).returncode == 0


class TestHooksJson:
    @staticmethod
    def _groups(event: str) -> list[dict]:
        return json.loads(HOOKS_JSON.read_text(encoding="utf-8"))["hooks"].get(event, [])

    @staticmethod
    def _cmd(entry: dict) -> str:
        return " ".join(entry["args"])

    def test_precompact_and_postcompact_entries(self) -> None:
        for event, mode, timeout in (("PreCompact", "pre", 45), ("PostCompact", "post", 10)):
            groups = self._groups(event)
            assert len(groups) == 1 and groups[0]["matcher"] == "manual|auto"
            (entry,) = groups[0]["hooks"]
            assert entry["type"] == "command" and entry["command"] == "uv" and entry["timeout"] == timeout
            assert self._cmd(entry).endswith(f"/hooks/scripts/compact_checkpoint.py {mode}")

    def test_the_compact_session_start_group_is_separate_and_light(self) -> None:
        groups = self._groups("SessionStart")
        compact_only = [g for g in groups if g["matcher"] == "compact"]
        assert len(compact_only) == 1
        (entry,) = compact_only[0]["hooks"]
        assert self._cmd(entry).endswith("/hooks/scripts/compact_checkpoint.py inject") and entry["timeout"] == 35
        for group in groups:
            if group is not compact_only[0]:
                assert not any("compact_checkpoint" in self._cmd(h) for h in group["hooks"]), (
                    "the existing SessionStart groups must not carry the inject verb"
                )
        shared = [g for g in groups if "compact" in g["matcher"].split("|") and g["matcher"] != "compact"]
        assert len(shared) == 1 and len(shared[0]["hooks"]) == 7, "the seven-verb group is out of scope and unchanged"
