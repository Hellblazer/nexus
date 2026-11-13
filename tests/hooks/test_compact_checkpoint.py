# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (c) 2026 Hal Hildebrand. All rights reserved.
"""``compact_checkpoint.py``: auto-continuation across compaction (bead nexus-3lqxv).

PreCompact writes the session's working state to a file, PostCompact appends the
summary, and the SessionStart group whose matcher is ``compact`` puts the file back
in front of the model. The contract these tests pin:

- ``pre`` never vetoes (exit 0 on every path, nothing on stdout), survives a missing
  ``nx`` and a timeout, and writes the file atomically with mode 0600.
- ``post`` appends and logs a warning when the summary names none of the pointers; it
  never appends to, or refreshes, a checkpoint an earlier compaction already used.
- ``inject`` emits the capped ``additionalContext`` for THIS session only, once, and
  nothing for a used, foreign or past-``_MAX_AGE_S`` (7 days) one (by its ``written:`` stamp,
  not its mtime), and starts no process.
- pairing is by use, not age: an idle compaction's checkpoint, written when the
  session went idle and used later (the deferred case), is appended to and injected with
  its real age, and inject and post work in either order; in the live runs
  measured, inject ran first (nexus-702gy).
- the board entry comes first and is the one titled EXACTLY the session's name.
- the script imports only the standard library and the plugin's own ``_`` modules.
"""
from __future__ import annotations

import ast
import importlib.util
import io
import json
import ntpath
import os
import re
import shutil
import subprocess
import sys
import textwrap
import time
import types
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

from tests._module_seam import setattr_in
from tests._platform import IS_WINDOWS, WINDOWS_SHEBANG, install_windows_exe_fake, scrubbed_env

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
    # As src/nexus/cli.py does on Windows: a cp1252 pipe cannot encode the bead glyphs.
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
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
              "### In-progress beads\\n◐ nexus-3lqxv ● P2 Auto-continuation\\n"
              "├── ◐ nexus-3lqxv.2 ● P2 its child\\n◐ nexus-zz9yx ● P2 another session's bead\\n\\n"
              "----\\nTotal: 3 issues\\n\\n"
              "### Ready beads (top 10)\\n" + ready + "\\n\\n### nx memory (x) titles\\nmemo-one\\nmemo-two\\n\\n"
              "### Feedback memories\\nfeedback-one\\n")
    elif args[:2] == ["memory", "list"]:
        for i, t in enumerate(TITLES):
            print(f"[{101 + i}] threads/{t}  (-, 2026-10-31T21:46:51Z)")
    elif args[:2] == ["memory", "search"]:
        # nexus-3lcda: FTS for the session id. "hit" finds the entry whose body carries a session: line;
        # "decoy_first" lists an entry that only mentions the id before it; "decoy_only" lists the decoy alone.
        # "fail" exits 1, "hang" never answers; "tie_*" list two entries that both carry the line, 208
        # written 2026-11-01 and 207 written 2026-11-03, in the order the name says.
        mode, sid = os.environ.get("FAKE_SEARCH", "none"), args[2]
        if mode == "fail":
            print("service unavailable", file=sys.stderr)
            sys.exit(1)
        if mode == "hang":
            time.sleep(30)
        rows = {"hit": [205], "decoy_first": [206, 205], "decoy_only": [206],
                "tie_old_first": [208, 207], "tie_new_first": [207, 208]}.get(mode, [])
        if not rows:
            print("No results found.")
        titles = {205: "bullock", 206: "decoy", 207: "newer", 208: "older"}
        stamps = {207: "2026-11-03T00:00:00Z", 208: "2026-11-01T00:00:00Z"}
        for row in rows:
            print(f"[{row}] threads/{titles[row]}  (-, {stamps.get(row, '2026-11-02T04:37:32Z')})")
            print(f"  {sid} preview")
    elif args[:2] == ["memory", "get"] and args[2].isdigit() and int(args[2]) >= 200:
        sid = os.environ["FAKE_SID"]
        if int(args[2]) in (207, 208):
            print(f"session: {sid}\\n## board of {'newer' if int(args[2]) == 207 else 'older'}\\nref: nexus-3lqxv")
        elif int(args[2]) == 205:
            print(f"ref: nexus-3lqxv\\nsession: {sid}\\n## board of bullock\\nstate: building")
        else:  # mentions the id but is not this session's entry
            print(f"session: someone-else\\n## decoy board\\nwaiting_on: session: {sid}\\nref: nexus-zz9yx")
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
    module._load_siblings()  # main() does this inside its guard; the unit tests call functions directly
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
    monkeypatch.setenv("FAKE_SID", SID)
    monkeypatch.delenv("FAKE_SEARCH", raising=False)
    # A Windows pipe's encoding, on every platform, so the stand-in's glyphs fail here as they do there.
    monkeypatch.setenv("PYTHONIOENCODING", "cp1252")
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
        assert "### In-progress beads" in text and "P2 Auto-continuation" in text
        assert f"## Threads board entry (threads/{NAME})" in text and "state: building" in text
        assert nx_calls(env) == [  # the board first: its ref lines choose the worktree
            f"memory search {SID} --project threads",  # by session id; no hit, so the exact title
            "memory list --project threads", "memory get 101 --project threads", "command-context continuation"
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

    def test_the_board_is_found_by_its_session_line_when_the_name_is_not_its_title(self, mod, env, monkeypatch) -> None:
        """Desktop seats keep their board under a handle (``bullock``) that is not the session's name
        (nexus-3lcda). The entry whose body carries ``session: <id>`` is found by full-text search."""
        monkeypatch.setenv("FAKE_SEARCH", "hit")
        monkeypatch.setenv("FAKE_BOARD", "missing")
        run(mod, monkeypatch, "pre", pre_payload())
        text = (env.checkpoints / f"{SID}.md").read_text(encoding="utf-8")
        assert "## Threads board entry (threads/bullock)" in text and "## board of bullock" in text
        assert "[no board entry" not in text
        assert nx_calls(env) == [
            f"memory search {SID} --project threads", "memory get 205 --project threads", "command-context continuation"
        ], "one search and one get: no listing of the project"
        # the board's ref: line still filters the in-progress beads
        assert "nexus-zz9yx" not in mod._in_progress_section(text)

    def test_the_session_line_is_found_without_a_session_name(self, mod, env, monkeypatch) -> None:
        (env.claude / "sessions" / "4242.json").unlink()
        monkeypatch.setenv("FAKE_SEARCH", "hit")
        run(mod, monkeypatch, "pre", pre_payload())
        text = (env.checkpoints / f"{SID}.md").read_text(encoding="utf-8")
        assert "## Threads board entry (threads/bullock)" in text and "- session name:" not in text

    def test_a_hit_that_only_mentions_the_id_is_not_this_sessions_board(self, mod, env, monkeypatch) -> None:
        monkeypatch.setenv("FAKE_SEARCH", "decoy_first")
        run(mod, monkeypatch, "pre", pre_payload())
        text = (env.checkpoints / f"{SID}.md").read_text(encoding="utf-8")
        assert "board of bullock" in text and "decoy" not in text
        assert nx_calls(env)[:3] == [
            f"memory search {SID} --project threads", "memory get 206 --project threads", "memory get 205 --project threads"
        ]

    def test_without_a_session_line_the_exact_title_is_the_fallback(self, mod, env, monkeypatch) -> None:
        monkeypatch.setenv("FAKE_SEARCH", "decoy_only")  # a hit that is not ours
        run(mod, monkeypatch, "pre", pre_payload())
        text = (env.checkpoints / f"{SID}.md").read_text(encoding="utf-8")
        assert f"## Threads board entry (threads/{NAME})" in text and f"board of {NAME}" in text and "decoy" not in text
        assert nx_calls(env) == [
            f"memory search {SID} --project threads", "memory get 206 --project threads",
            "memory list --project threads", "memory get 101 --project threads", "command-context continuation",
        ]

    @pytest.mark.parametrize("order", ["tie_old_first", "tie_new_first"])
    def test_when_two_entries_carry_the_session_line_the_newest_wins(self, mod, env, monkeypatch, order) -> None:
        """Both bodies pass the line check; the listing's timestamp decides, not the search rank."""
        monkeypatch.setenv("FAKE_SEARCH", order)
        run(mod, monkeypatch, "pre", pre_payload())
        text = (env.checkpoints / f"{SID}.md").read_text(encoding="utf-8")
        assert "## Threads board entry (threads/newer)" in text and "board of older" not in text
        assert nx_calls(env)[:2] == [f"memory search {SID} --project threads", "memory get 207 --project threads"]

    def test_a_failing_search_falls_back_to_the_exact_title(self, mod, env, monkeypatch) -> None:
        monkeypatch.setenv("FAKE_SEARCH", "fail")
        run(mod, monkeypatch, "pre", pre_payload())
        text = (env.checkpoints / f"{SID}.md").read_text(encoding="utf-8")
        assert f"## Threads board entry (threads/{NAME})" in text and f"board of {NAME}" in text
        assert nx_calls(env)[:3] == [
            f"memory search {SID} --project threads", "memory list --project threads", "memory get 101 --project threads"
        ]

    def test_a_search_that_times_out_falls_back_to_the_exact_title_inside_the_budget(self, mod, env, monkeypatch) -> None:
        monkeypatch.setenv("FAKE_SEARCH", "hang")
        monkeypatch.setattr(mod, "_SEARCH_TIMEOUT_S", 0.8)
        started = time.monotonic()
        run(mod, monkeypatch, "pre", pre_payload())
        assert time.monotonic() - started < mod._BOARD_TIMEOUT_S + 10, "the 30 s hang was cut"
        text = (env.checkpoints / f"{SID}.md").read_text(encoding="utf-8")
        assert f"## Threads board entry (threads/{NAME})" in text and f"board of {NAME}" in text
        assert [c.split()[1] for c in nx_calls(env)[:3]] == ["search", "list", "get"]

    @pytest.mark.parametrize("search", ["fail", "hang"])
    def test_a_failed_search_and_no_entry_names_both_lookups_in_the_warning(self, mod, env, monkeypatch, search) -> None:
        monkeypatch.setenv("FAKE_SEARCH", search)
        monkeypatch.setenv("FAKE_BOARD", "missing")
        monkeypatch.setattr(mod, "_SEARCH_TIMEOUT_S", 0.8)
        run(mod, monkeypatch, "pre", pre_payload())
        log = env.hook_log.read_text(encoding="utf-8")
        assert "also failed" in log and "titled exactly 'linda'" in log

    def test_no_entry_at_all_names_the_session_line_and_the_title_in_the_warning(self, mod, env, monkeypatch) -> None:
        monkeypatch.setenv("FAKE_BOARD", "missing")
        run(mod, monkeypatch, "pre", pre_payload())
        warning = next(ln for ln in env.hook_log.read_text(encoding="utf-8").splitlines() if " WARNING " in ln)
        assert f"no threads entry carries `session: {SID}` and none is titled exactly 'linda'" in warning

    @pytest.mark.parametrize(
        ("body", "found"),
        [
            (f"session: {SID}\n## A\nstate: x", True),  # the entry's first line
            (f"## A\nstate: x\nsession: {SID}", True),  # a block-level line
            (f"  Session:   {SID}  \n", True),
            (f"- session: {SID}\n", True),
            (f"session: {SID}-2\n", False),  # a longer id
            (f"session: x{SID}\n", False),
            (f"waiting_on: session: {SID}\n", False),  # mentions it in another field
            (f"session: {OTHER_SID}\nref: {SID}\n", False),
            ("", False),
        ],
    )
    def test_the_session_line_matcher(self, mod, body, found) -> None:
        assert mod._has_session_line(body, SID) is found

    def test_a_key_file_is_not_a_session_source(self, mod, env, monkeypatch) -> None:
        third = "33333333-3333-3333-3333-333333333333"
        (env.claude / "sessions" / "9.key").write_text(json.dumps({"sessionId": third, "name": "from_the_key"}), encoding="utf-8")
        run(mod, monkeypatch, "pre", pre_payload(session_id=third))
        text = (env.checkpoints / f"{third}.md").read_text(encoding="utf-8")
        assert "from_the_key" not in text and "- session name:" not in text
        assert nx_calls(env) == [f"memory search {third} --project threads", "command-context continuation"]

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
        assert nx_calls(env) == [f"memory search {SID} --project threads", "command-context continuation"]

    def test_a_missing_board_entry_is_recorded_not_fatal(self, mod, env, monkeypatch) -> None:
        monkeypatch.setenv("FAKE_BOARD", "missing")
        code, _ = run(mod, monkeypatch, "pre", pre_payload())
        text = (env.checkpoints / f"{SID}.md").read_text(encoding="utf-8")
        assert code == 0 and "[no board entry:" in text and "### In-progress beads" in text

    def test_in_progress_keeps_only_the_beads_the_board_names(self, mod, env, monkeypatch) -> None:
        """Every session claims under one assignee; the board's ref is this session's own list."""
        run(mod, monkeypatch, "pre", pre_payload())
        section = mod._in_progress_section((env.checkpoints / f"{SID}.md").read_text(encoding="utf-8"))
        assert "nexus-3lqxv ● P2 Auto-continuation" in section and "nexus-3lqxv.2 ● P2 its child" in section
        assert "nexus-zz9yx" not in section and "Total:" not in section
        assert "(2 of the 3 in-progress beads listed" in section

    def test_without_a_board_the_in_progress_list_is_whole(self, mod, env, monkeypatch) -> None:
        monkeypatch.setenv("FAKE_BOARD", "missing")
        run(mod, monkeypatch, "pre", pre_payload())
        section = mod._in_progress_section((env.checkpoints / f"{SID}.md").read_text(encoding="utf-8"))
        assert "nexus-zz9yx" in section and "beads listed" not in section

    def test_continuation_runs_in_the_worktree_named_after_the_session(self, mod, env, monkeypatch, tmp_path) -> None:
        """The payload cwd is the launch directory (the shared primary); the session works in its worktree."""
        primary = tmp_path / "primary"
        primary.mkdir()
        git = ["git", "-c", "user.name=t", "-c", "user.email=t@example.invalid", "-c", "commit.gpgsign=false", "-C", str(primary)]
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

    @staticmethod
    def _repo_with_worktrees(tmp_path: Path, worktrees: dict[str, str]) -> Path:
        """A primary checkout plus one worktree per ``{dir name: branch}``."""
        primary = tmp_path / "primary"
        primary.mkdir()
        git = ["git", "-c", "user.name=t", "-c", "user.email=t@example.invalid", "-c", "commit.gpgsign=false", "-C", str(primary)]
        subprocess.run([*git, "init", "-q"], check=True)
        subprocess.run([*git, "commit", "-q", "--allow-empty", "-m", "root"], check=True)
        for wt, branch in worktrees.items():
            subprocess.run([*git, "worktree", "add", "-q", "-b", branch, str(tmp_path / "wt" / wt)], check=True)
        return primary

    @staticmethod
    def _age(tmp_path: Path, wt: str, seconds_ago: float) -> None:
        """Set a worktree's git-dir activity stamps (logs/HEAD, HEAD, index) to *seconds_ago*."""
        pointer = (tmp_path / "wt" / wt / ".git").read_text(encoding="utf-8").split(":", 1)[1].strip()
        stamp = time.time() - seconds_ago
        for leaf in ("logs/HEAD", "HEAD", "index"):
            target = Path(pointer) / leaf
            if target.exists():
                os.utime(target, (stamp, stamp))

    def test_a_topic_named_worktree_is_the_sessions(self, mod, env, monkeypatch, tmp_path) -> None:
        """Sessions name worktrees ``<name>-<topic>`` (``marlowe-owner-onestmt``); 7.92.0 matched only ``<name>``."""
        primary = self._repo_with_worktrees(tmp_path, {"linda_2": "feature/x", f"{NAME}-3mdmp": "feature/nexus-3mdmp-lease"})
        monkeypatch.setattr(mod, "_git_worktrees", env.real_git_worktrees)
        run(mod, monkeypatch, "pre", pre_payload(cwd=str(primary)))
        expected = os.path.realpath(tmp_path / "wt" / f"{NAME}-3mdmp")
        assert os.path.realpath(Path(str(env.nx_log) + ".cwd").read_text(encoding="utf-8")) == expected

    def test_the_board_ref_chooses_among_several_worktrees(self, mod, env, monkeypatch, tmp_path) -> None:
        """The board names nexus-3lqxv; its worktree wins over a more recently active one."""
        primary = self._repo_with_worktrees(tmp_path, {
            f"{NAME}-old": "feature/nexus-3lqxv-auto-continuation", f"{NAME}-new": "feature/nexus-aaaaa-other",
        })
        self._age(tmp_path, f"{NAME}-old", 3600)
        monkeypatch.setattr(mod, "_git_worktrees", env.real_git_worktrees)
        run(mod, monkeypatch, "pre", pre_payload(cwd=str(primary)))
        assert os.path.realpath(Path(str(env.nx_log) + ".cwd").read_text(encoding="utf-8")) == os.path.realpath(
            tmp_path / "wt" / f"{NAME}-old"
        )
        header = (env.checkpoints / f"{SID}.md").read_text(encoding="utf-8").split("\n## ", 1)[0]
        assert f"- other worktree: " in header and f"wt/{NAME}-new [feature/nexus-aaaaa-other]" in header

    @pytest.mark.parametrize(
        ("rows", "active", "board", "cwd", "chosen"),
        [
            # a child ref does not name its parent's branch; recency then decides
            ([("/w/linda-a", "feature/nexus-3lqxv-y"), ("/w/linda-b", "feature/nexus-aaaaa-z")],
             {"/w/linda-a": 2.0, "/w/linda-b": 1.0}, "ref: nexus-3lqxv.2", "/p", "/w/linda-a"),
            # a parent ref names its child's branch (marlowe's board: ref nexus-z1d0l.1, branch .1.3)
            ([("/w/linda-a", "feature/nexus-zzzzz-y"), ("/w/linda-b", "feature/nexus-z1d0l.1.3-x")],
             {"/w/linda-a": 2.0, "/w/linda-b": 1.0}, "ref: nexus-z1d0l.1", "/p", "/w/linda-b"),
            # .1 is not .10
            ([("/w/linda-a", "feature/nexus-z1d0l.10-y"), ("/w/linda-b", "feature/nexus-qqqqq-x")],
             {"/w/linda-a": 1.0, "/w/linda-b": 2.0}, "ref: nexus-z1d0l.1", "/p", "/w/linda-b"),
            # the board naming a worktree's path beats a ref match and recency
            ([("/w/linda-a", "feature/nexus-3lqxv-y"), ("/w/linda-b", "release/v1")],
             {"/w/linda-a": 2.0, "/w/linda-b": 1.0}, "ref: nexus-3lqxv\nstate: testing in nexus-wt/linda-b", "/p", "/w/linda-b"),
            # a directory name in prose, not after a path separator, is not a path mention
            ([("/w/linda-a", "feature/x"), ("/w/linda-b", "feature/y")],
             {"/w/linda-a": 1.0, "/w/linda-b": 2.0}, "linda-a is waiting", "/p", "/w/linda-b"),
            # with no board signal, the exact name beats a more recent topic worktree
            ([("/w/linda", "feature/x"), ("/w/linda-b", "feature/y")],
             {"/w/linda": 1.0, "/w/linda-b": 2.0}, "", "/p", "/w/linda"),
            # a session launched inside a worktree keeps it, whatever the board says
            ([("/w/linda", "feature/nexus-3lqxv-x"), ("/w/linda-b", "feature/y")],
             {"/w/linda": 2.0, "/w/linda-b": 1.0}, "ref: nexus-3lqxv", "/w/linda-b/src", "/w/linda-b"),
        ],
        ids=["child-ref-not-parent", "parent-ref-names-child", "dot1-not-dot10", "path-mention-wins",
             "bare-name-not-a-path", "exact-beats-recent-topic", "launch-cwd-wins"],
    )
    def test_the_worktree_ranking(self, mod, monkeypatch, rows, active, board, cwd, chosen) -> None:
        monkeypatch.setattr(mod, "_git_worktrees", lambda _cwd, _timeout: rows)
        monkeypatch.setattr(mod, "_last_active", lambda path: active[path])
        setattr_in(monkeypatch, mod, "os.path.isdir", lambda _p: True)
        setattr_in(monkeypatch, mod, "os.path.realpath", lambda p: p)
        got, others = mod._session_worktrees(NAME, cwd, board)
        assert got == chosen and [o[0] for o in others] == [r[0] for r in rows if r[0] != chosen]

    def test_the_launch_cwd_test_holds_on_windows_path_spellings(self, mod, monkeypatch) -> None:
        # realpath is faked as the identity, as in test_the_worktree_ranking; with Windows
        # path rules a bare os.sep prefix test over git's forward slashes missed the
        # launch worktree there (Windows set red on b05101286, nexus-qowvl).
        rows = [("C:/w/linda", "feature/nexus-3lqxv-x"), ("C:/w/linda-b", "feature/y")]
        winpath = types.SimpleNamespace(
            **{k: getattr(ntpath, k) for k in ("normcase", "normpath", "join", "basename")},
            realpath=lambda p: p, isdir=lambda _p: True,
        )
        setattr_in(monkeypatch, mod, "os.path", winpath)
        setattr_in(monkeypatch, mod, "os.sep", "\\")
        monkeypatch.setattr(mod, "_git_worktrees", lambda _cwd, _timeout: rows)
        monkeypatch.setattr(mod, "_last_active", lambda path: {"C:/w/linda": 2.0, "C:/w/linda-b": 1.0}[path])
        got, _others = mod._session_worktrees(NAME, "c:\\w\\linda-b\\src", "ref: nexus-3lqxv")
        assert got == "C:/w/linda-b"

    def test_the_others_list_is_capped_and_dated(self, mod) -> None:
        others = [(f"/w/linda-{i}", f"feature/b{i}", 1_000_000.0 + i) for i in range(8)]
        lines = mod._other_worktree_lines(others)
        assert len([ln for ln in lines if ln.startswith("- other worktree: ")]) == mod._OTHER_WORKTREES_SHOWN
        assert lines[0].startswith("- other worktree: /w/linda-7 [feature/b7], last active 1970-01-")
        assert "- other worktrees: 3 more not listed" in lines and lines[-1].startswith("- worktree choice:")
        assert mod._other_worktree_lines([]) == []

    def test_a_ranking_failure_keeps_the_launch_directory(self, mod, monkeypatch) -> None:
        monkeypatch.setattr(mod, "_git_worktrees", lambda _cwd, _timeout: [("/w/linda-a", "x"), ("/w/linda-b", "y")])
        setattr_in(monkeypatch, mod, "os.path.isdir", lambda _p: True)
        monkeypatch.setattr(mod, "_last_active", lambda _p: 1 / 0)
        assert mod._session_worktrees(NAME, "/p", "") == (None, [])

    def test_activity_reads_a_relative_gitdir_pointer_and_survives_a_bad_one(self, mod, tmp_path) -> None:
        gitdir = tmp_path / "gitdirs" / "wt"
        (gitdir / "logs").mkdir(parents=True)
        (gitdir / "logs" / "HEAD").write_text("x", encoding="utf-8")
        os.utime(gitdir / "logs" / "HEAD", (1_000_000.0, 1_000_000.0))
        wt = tmp_path / "wt"
        wt.mkdir()
        (wt / ".git").write_text("gitdir: ../gitdirs/wt\n", encoding="utf-8")
        assert mod._last_active(str(wt)) == 1_000_000.0
        (wt / ".git").write_text("gitdir: bad\x00path\n", encoding="utf-8")
        assert mod._last_active(str(wt)) == 0.0

    def test_without_a_board_match_the_most_recent_worktree_wins(self, mod, env, monkeypatch, tmp_path) -> None:
        monkeypatch.setenv("FAKE_BOARD", "missing")
        primary = self._repo_with_worktrees(tmp_path, {f"{NAME}-old": "feature/nexus-v1929-old", f"{NAME}-now": "feature/nexus-bbbbb-now"})
        self._age(tmp_path, f"{NAME}-old", 7200)
        monkeypatch.setattr(mod, "_git_worktrees", env.real_git_worktrees)
        run(mod, monkeypatch, "pre", pre_payload(cwd=str(primary)))
        assert os.path.realpath(Path(str(env.nx_log) + ".cwd").read_text(encoding="utf-8")) == os.path.realpath(
            tmp_path / "wt" / f"{NAME}-now"
        )

    def test_no_session_worktree_keeps_the_payload_cwd(self, mod, env, monkeypatch, tmp_path) -> None:
        monkeypatch.setattr(mod, "_git_worktrees", env.real_git_worktrees)
        run(mod, monkeypatch, "pre", pre_payload(cwd=str(tmp_path)))  # not a repository
        assert os.path.realpath(Path(str(env.nx_log) + ".cwd").read_text(encoding="utf-8")) == os.path.realpath(tmp_path)
        assert f"- worktree: none named {NAME} or {NAME}-*;" in (env.checkpoints / f"{SID}.md").read_text(encoding="utf-8")

    @pytest.mark.parametrize(
        ("rows", "board", "kept", "dropped"),
        [
            # another repo's prefix and a 3-char hash
            ("◐ labrun-56v.1.6 ● P2 x\n◐ labrun-a1b ● P2 y", "ref: labrun-56v.1", ["labrun-56v.1.6"], ["labrun-a1b"]),
            # a legacy 4-char nexus id
            ("◐ nexus-9clx ● P2 x\n◐ nexus-zz9yx ● P2 y", "ref: nexus-9clx", ["nexus-9clx"], ["nexus-zz9yx"]),
            # naming a child does not name its siblings
            ("◐ nexus-z1d0l.1.1 ● P2 x\n└── ◐ nexus-z1d0l.1.5 ● P2 y", "ref: nexus-z1d0l.1.5", ["nexus-z1d0l.1.5"], ["nexus-z1d0l.1.1"]),
            # a peer's bead in waiting_on: is not the session's
            ("◐ nexus-aaaaa ● P2 x\n◐ nexus-peerx ● P2 y", "ref: nexus-aaaaa\nwaiting_on: nexus-peerx", ["nexus-aaaaa"], ["nexus-peerx"]),
        ],
        ids=["other-prefix", "legacy-id", "child-exact", "waiting-on-excluded"],
    )
    def test_the_filter_reads_any_bd_row(self, mod, rows, board, kept, dropped) -> None:
        out = mod._in_progress_section(mod._filter_in_progress(f"### In-progress beads\n{rows}\n", board))
        assert all(k in out for k in kept) and not any(d in out for d in dropped), out

    @pytest.mark.parametrize(
        ("rows", "board"),
        [
            ("◐ nexus-aaaaa ● P2 claimed after the board was written", "ref: nexus-closd"),  # nothing kept
            ("- (bd unavailable or no results)", "ref: nexus-3lqxv"),  # a bd outage
            ("◐ nexus-aaaaa ● P2 mine", "ref: (none yet)\nwaiting_on: nexus-peerx"),  # only a peer's bead
            ("◐ nexus-aaaaa ● P2 mine", "state: no ref line"),
        ],
        ids=["zero-kept", "bd-outage", "waiting-on-only", "no-ref-line"],
    )
    def test_the_filter_fails_open(self, mod, rows, board) -> None:
        shaped = f"### In-progress beads\n{rows}\n\n### Uncommitted\n M a.py\n"
        assert mod._filter_in_progress(shaped, board) == shaped

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
        # A USED checkpoint (a post appended a summary): an unused one is kept for seven days (nexus-ct9sv).
        old.write_text("# Compaction checkpoint\n\n- written: 2026-01-01T00:00:00+00:00\n\n"
                       "## Compact summary (2026-01-01T00:00:01+00:00)\n\nsomeone else, three days old\n",
                       encoding="utf-8")
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
        assert not old.exists(), "a used checkpoint older than two days survived the prune"
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
        line = next(ln for ln in log.splitlines() if "names none of" in ln)
        # Desktop's summaries never name them and nothing consumes the line: a count, not a warning
        # (nexus-3lcda); the warning level stays for what needs action.
        assert " INFO compact-checkpoint: post:" in line and "WARNING" not in line

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

    def test_a_used_checkpoint_is_archived_and_replaced_by_a_stub_not_appended_to(self, mod, env, monkeypatch) -> None:
        path = self._pre(mod, env, monkeypatch)
        run(mod, monkeypatch, "post", {"session_id": SID, "compact_summary": "the earlier compaction's summary"})
        # this compaction's pre did not run; post finds the earlier compaction's file
        run(mod, monkeypatch, "post", {"session_id": SID, "compact_summary": "this compaction's summary"})
        text = path.read_text(encoding="utf-8")
        assert "state: building" not in text and "- written:" not in text
        assert "no pre-compaction checkpoint for this compaction" in text and "this compaction's summary" in text
        assert "the earlier compaction's summary" not in text
        archives = [p for p in env.checkpoints.iterdir() if p.name != path.name]
        assert len(archives) == 1 and "state: building" in archives[0].read_text(encoding="utf-8")
        assert "no unused pre-compaction checkpoint" in env.hook_log.read_text(encoding="utf-8")

    def test_the_live_order_inject_then_post_appends_to_this_compactions_checkpoint(
        self, mod, env, monkeypatch
    ) -> None:
        """Live (dcb9aae4, 2026-11-01): pre, then SessionStart inject, then post about 6 s later.
        An inject trace that post read as "used" archived every real checkpoint (round-1 blocker)."""
        path = self._pre(mod, env, monkeypatch)
        before = path.read_text(encoding="utf-8")
        assert run(mod, monkeypatch, "inject", {"session_id": SID, "source": "compact"})[1] != ""
        assert path.read_text(encoding="utf-8") == before, "inject never rewrites the checkpoint"
        run(mod, monkeypatch, "post", {"session_id": SID, "compact_summary": "this compaction's summary"})
        text = path.read_text(encoding="utf-8")
        assert text.startswith(before.rstrip()) and text.rstrip().endswith("this compaction's summary")
        assert sorted(p.name for p in env.checkpoints.iterdir()) == sorted([path.name, f"{SID}.injected"])
        assert not env.hook_log.exists() or "no unused pre-compaction" not in env.hook_log.read_text(encoding="utf-8")

    def test_an_idle_compactions_hours_old_unused_checkpoint_is_appended_to(self, mod, env, monkeypatch) -> None:
        """Deferred idle case: pre ran when the session went idle, post runs hours later."""
        path = self._pre(mod, env, monkeypatch)
        restamp(path, 3 * 3600)
        run(mod, monkeypatch, "post", {"session_id": SID, "compact_summary": "the idle compaction's summary"})
        text = path.read_text(encoding="utf-8")
        assert "state: building" in text and "- written:" in text
        assert text.rstrip().endswith("the idle compaction's summary")
        assert [p.name for p in env.checkpoints.iterdir()] == [path.name], "nothing is archived"

    def test_a_checkpoint_past_the_prune_window_is_archived(self, mod, env, monkeypatch) -> None:
        path = self._pre(mod, env, monkeypatch)
        restamp(path, mod._MAX_AGE_S + 60)
        run(mod, monkeypatch, "post", {"session_id": SID, "compact_summary": "this compaction's summary"})
        assert "no pre-compaction checkpoint for this compaction" in path.read_text(encoding="utf-8")

    def test_has_no_decision_output_and_exits_zero_on_garbage(self, mod, env, monkeypatch) -> None:
        for payload in ("", "nope", {"session_id": "bad id"}):
            code, out = run(mod, monkeypatch, "post", payload)
            assert code == 0 and out == ""


class TestSubagentPayload:
    """A hook fired inside a subagent carries the parent's session_id plus agent_id. Whether the
    compaction hooks fire there is undocumented; if they do, all three modes do nothing
    (nexus-mtl98, Sam 2026-11-01)."""

    @pytest.mark.parametrize(
        "agent", ["a0123456789abcdef", "name@team", "../x", 7, True, ["a"], {"a": 1}],
        ids=["hex", "teammate", "odd", "int", "bool", "list", "object"],
    )
    def test_a_subagent_payload_touches_nothing(self, mod, env, monkeypatch, agent) -> None:
        run(mod, monkeypatch, "pre", pre_payload())
        parent = env.checkpoints / f"{SID}.md"
        before = parent.read_text(encoding="utf-8")
        names = sorted(p.name for p in env.checkpoints.iterdir())
        calls = len(nx_calls(env))
        for mode, extra in (("pre", pre_payload()), ("inject", {"source": "compact"}), ("post", {"compact_summary": "agent"})):
            code, out = run(mod, monkeypatch, mode, {"session_id": SID, **extra, "agent_id": agent})
            assert (code, out) == (0, ""), mode
        assert parent.read_text(encoding="utf-8") == before, "the parent's checkpoint was touched"
        assert sorted(p.name for p in env.checkpoints.iterdir()) == names, "a file was written or removed"
        assert len(nx_calls(env)) == calls, "a subagent's pre ran nx"
        assert env.hook_log.read_text(encoding="utf-8").count("carries agent_id") == 3
        # the parent's own restore still works afterwards
        ctx = json.loads(run(mod, monkeypatch, "inject", {"session_id": SID, "source": "compact"})[1])
        assert str(parent) in ctx["hookSpecificOutput"]["additionalContext"]

    @pytest.mark.parametrize(
        "extra",
        [{"agent_id": None}, {"agent_id": ""}, {"agent_id": "   "}, {"agent_type": "Explore"}],
        ids=["none", "empty", "blank", "main-session-with---agent"],
    )
    def test_a_main_session_still_checkpoints_and_restores(self, mod, env, monkeypatch, extra) -> None:
        """A main session started with --agent carries agent_type and no agent_id (measured on
        Claude Code 2.1.316); a guard keyed on agent_type would silently disable its restore."""
        run(mod, monkeypatch, "pre", {**pre_payload(), **extra})
        path = env.checkpoints / f"{SID}.md"
        assert path.exists()
        ctx = json.loads(run(mod, monkeypatch, "inject", {"session_id": SID, "source": "compact", **extra})[1])
        assert str(path) in ctx["hookSpecificOutput"]["additionalContext"]
        run(mod, monkeypatch, "post", {"session_id": SID, "compact_summary": "main summary", **extra})
        assert path.read_text(encoding="utf-8").rstrip().endswith("main summary")


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
        assert "P2 Auto-continuation" in ctx and "state: building" in ctx
        assert str(env.checkpoints / f"{SID}.md") in ctx.splitlines()[-1]
        assert "SUMMARY-SENTINEL" not in ctx, "the summary is already in the new context; do not repeat it"

    def test_a_stub_checkpoint_tells_the_resumed_session_no_restore_is_available(self, mod, env, monkeypatch) -> None:
        """nexus-2v7da: post wrote a stub (no unused pre-compaction checkpoint), so inject has nothing to
        restore. It said nothing, and the resumed session could not tell "nothing was lost" from "nothing
        was saved". It now gets a short notice that names where to look instead."""
        run(mod, monkeypatch, "post", {"session_id": SID, "compact_summary": "SUMMARY-SENTINEL"})
        stub = (env.checkpoints / f"{SID}.md").read_text(encoding="utf-8")
        assert mod._STUB_BODY in stub and "- written:" not in stub
        self._no_process(mod, monkeypatch)
        code, out = run(mod, monkeypatch, "inject", {"session_id": SID, "source": "compact"})
        assert code == 0
        ctx = self._ctx(out)
        assert "no working-state checkpoint" in ctx and "nothing is restored" in ctx
        assert "threads" in ctx and SID in ctx and "git" in ctx
        assert "SUMMARY-SENTINEL" not in ctx, "the summary is already in the new context"
        assert len(ctx.encode("utf-8")) < 1024, "a notice, not a checkpoint"

    def test_a_missing_checkpoint_on_a_compact_start_gets_the_same_notice(self, mod, env, monkeypatch) -> None:
        """Live, inject runs BEFORE post (nexus-702gy), so when pre did not run there is no stub yet at
        inject time: the file is simply absent. A start that says it is a compact one and finds no file
        is the same fact."""
        code, out = run(mod, monkeypatch, "inject", {"session_id": SID, "source": "compact"})
        assert code == 0 and "no working-state checkpoint" in self._ctx(out)

    def test_a_missing_checkpoint_without_a_compact_source_stays_silent(self, mod, env, monkeypatch) -> None:
        assert run(mod, monkeypatch, "inject", {"session_id": SID}) == (0, "")
        assert run(mod, monkeypatch, "inject", {"session_id": SID, "source": "startup"}) == (0, "")

    def test_a_stub_notice_is_not_a_restore_and_repeats_on_the_next_compaction(self, mod, env, monkeypatch) -> None:
        """No sidecar is written, so a later compaction whose pre also failed is owed the same notice.
        Fails if inject records the stub as used (the second inject would then be silent)."""
        run(mod, monkeypatch, "post", {"session_id": SID, "compact_summary": "s"})
        first = run(mod, monkeypatch, "inject", {"session_id": SID, "source": "compact"})
        assert "no working-state checkpoint" in self._ctx(first[1])
        assert not (env.checkpoints / f"{SID}.injected").exists()
        second = run(mod, monkeypatch, "inject", {"session_id": SID, "source": "compact"})
        assert "no working-state checkpoint" in self._ctx(second[1]), "the notice repeats"

    @pytest.mark.parametrize("source", [None, "startup", "resume"])
    def test_a_stub_without_a_compact_source_stays_silent(self, mod, env, monkeypatch, source) -> None:
        """Only a start that says it is a compact one is told (the absent-file branch already works so)."""
        run(mod, monkeypatch, "post", {"session_id": SID, "compact_summary": "s"})
        payload = {"session_id": SID} if source is None else {"session_id": SID, "source": source}
        assert run(mod, monkeypatch, "inject", payload) == (0, "")

    def test_a_checkpoint_that_exists_but_cannot_be_read_is_not_reported_as_missing(
        self, mod, env, monkeypatch
    ) -> None:
        """A directory where the file belongs raises IsADirectoryError (PermissionError on Windows), both
        OSError but neither FileNotFoundError. The notice says there is no checkpoint, which would be false."""
        (env.checkpoints / f"{SID}.md").mkdir(parents=True)
        code, out = run(mod, monkeypatch, "inject", {"session_id": SID, "source": "compact"})
        assert (code, out) == (0, "")
        assert "could not be read" in env.hook_log.read_text(encoding="utf-8")

    def test_a_compact_start_with_a_checkpoint_past_the_seven_day_limit_gets_the_notice(
        self, mod, env, monkeypatch
    ) -> None:
        run(mod, monkeypatch, "pre", pre_payload())
        restamp(env.checkpoints / f"{SID}.md", 8 * 24 * 3600)  # was 3 days while _MAX_AGE_S was 48 h (nexus-ct9sv)
        code, out = run(mod, monkeypatch, "inject", {"session_id": SID, "source": "compact"})
        ctx = self._ctx(out)
        assert code == 0 and "no working-state checkpoint" in ctx and "state: building" not in ctx
        assert not (env.checkpoints / f"{SID}.injected").exists()

    def test_a_compact_start_with_an_unstamped_checkpoint_gets_the_notice(self, mod, env, monkeypatch) -> None:
        run(mod, monkeypatch, "pre", pre_payload())
        path = env.checkpoints / f"{SID}.md"
        path.write_text(re.sub(r"(?m)^- written: .*\n", "", path.read_text(encoding="utf-8")), encoding="utf-8")
        ctx = self._ctx(run(mod, monkeypatch, "inject", {"session_id": SID, "source": "compact"})[1])
        assert "no working-state checkpoint" in ctx and "state: building" not in ctx

    def test_an_already_injected_checkpoint_gets_the_notice_on_a_later_compaction(
        self, mod, env, monkeypatch
    ) -> None:
        """Live order: inject before post, so a compaction whose pre failed finds the previous compaction's
        checkpoint, already used. The sidecar was written by an EARLIER compaction (minutes ago), so the
        session is told there is nothing to restore, and the old state is not repeated."""
        run(mod, monkeypatch, "pre", pre_payload())
        first = run(mod, monkeypatch, "inject", {"session_id": SID, "source": "compact"})
        assert "state: building" in self._ctx(first[1])
        self._age_sidecar(env, 5 * 60)
        code, out = run(mod, monkeypatch, "inject", {"session_id": SID, "source": "compact"})
        ctx = self._ctx(out)
        assert code == 0 and "no working-state checkpoint" in ctx and "state: building" not in ctx

    def test_a_duplicated_compact_fire_for_the_same_compaction_stays_silent(self, mod, env, monkeypatch) -> None:
        """A second SessionStart(compact) fire for a HEALTHY compaction reaches the already-injected branch
        (the first fire just wrote the sidecar). Telling the session "no restore" there would contradict the
        restore it was just given. None was seen in 20 live compactions, but Desktop's were never observed."""
        run(mod, monkeypatch, "pre", pre_payload())
        first = run(mod, monkeypatch, "inject", {"session_id": SID, "source": "compact"})
        assert "state: building" in self._ctx(first[1])
        assert run(mod, monkeypatch, "inject", {"session_id": SID, "source": "compact"}) == (0, "")
        self._age_sidecar(env, 25)  # still inside the window
        assert run(mod, monkeypatch, "inject", {"session_id": SID, "source": "compact"}) == (0, "")

    def test_the_duplicate_fire_window_ends_before_compactions_can_recur(self, mod, env, monkeypatch) -> None:
        """The closest live pair was 115 s apart: a sidecar 60 s old belongs to an earlier compaction."""
        assert mod._DUPLICATE_FIRE_S < 115
        run(mod, monkeypatch, "pre", pre_payload())
        run(mod, monkeypatch, "inject", {"session_id": SID, "source": "compact"})
        self._age_sidecar(env, 60)
        out = run(mod, monkeypatch, "inject", {"session_id": SID, "source": "compact"})[1]
        assert "no working-state checkpoint" in self._ctx(out)

    def test_an_already_injected_checkpoint_without_a_compact_source_stays_silent(self, mod, env, monkeypatch) -> None:
        run(mod, monkeypatch, "pre", pre_payload())
        run(mod, monkeypatch, "inject", {"session_id": SID, "source": "compact"})
        self._age_sidecar(env, 5 * 60)
        assert run(mod, monkeypatch, "inject", {"session_id": SID}) == (0, "")
        assert run(mod, monkeypatch, "inject", {"session_id": SID, "source": "startup"}) == (0, "")

    @staticmethod
    def _age_sidecar(env, seconds: float) -> None:
        sidecar = env.checkpoints / f"{SID}.injected"
        then = time.time() - seconds
        os.utime(sidecar, (then, then))

    def test_a_foreign_session_gets_nothing(self, mod, env, monkeypatch) -> None:
        run(mod, monkeypatch, "pre", pre_payload())
        code, out = run(mod, monkeypatch, "inject", {"session_id": OTHER_SID})
        assert code == 0 and out == ""
        # and the path-shaped ids cannot reach another file
        for bad in ("../" + SID, SID + "/../x", "", None):
            assert run(mod, monkeypatch, "inject", {"session_id": bad})[1] == ""

    def test_a_stamp_past_the_prune_window_gets_nothing_whatever_the_mtime_says(self, mod, env, monkeypatch) -> None:
        run(mod, monkeypatch, "pre", pre_payload())
        path = env.checkpoints / f"{SID}.md"
        restamp(path, mod._MAX_AGE_S + 60)  # the mtime stays fresh, as after a rewrite
        code, out = run(mod, monkeypatch, "inject", {"session_id": SID})
        assert code == 0 and out == ""

    @pytest.mark.parametrize(
        "order",
        [("post", "age", "inject"), ("age", "inject", "post"), ("age", "post", "inject")],
        ids=["post-at-idle", "inject-then-post-at-next-message", "post-then-inject-at-next-message"],
    )
    def test_an_idle_compactions_checkpoint_is_injected_hours_later_with_its_real_age(
        self, mod, env, monkeypatch, order
    ) -> None:
        """Deferred idle case: pre at idle, the summary used hours later (nexus-702gy).
        The docs do not say when post and inject run, so every order is pinned."""
        run(mod, monkeypatch, "pre", pre_payload(trigger="auto"))
        path = env.checkpoints / f"{SID}.md"
        ctx = None
        for step in order:
            if step == "post":
                run(mod, monkeypatch, "post", {"session_id": SID, "compact_summary": "idle summary"})
            elif step == "age":
                restamp(path, 3 * 3600 + 5 * 60)
            else:
                self._no_process(mod, monkeypatch)
                ctx = self._ctx(run(mod, monkeypatch, "inject", {"session_id": SID, "source": "compact"})[1])
        first = ctx.splitlines()[0]
        assert "3 h 5 min ago (trigger auto)" in first
        assert "may predate this compaction" in first and "The state may have changed since it was written" in first
        assert "check git, beads and the board before acting on it." in first
        assert "just before" not in first and "failed" not in first, "no failure is claimed from an age alone"
        assert "state: building" in ctx
        text = path.read_text(encoding="utf-8")
        assert "state: building" in text and text.rstrip().endswith("idle summary"), "post kept the real checkpoint"

    def test_precomputed_says_the_summary_was_prepared_while_the_session_was_idle(self, mod, env, monkeypatch) -> None:
        """A payload with ``precomputed: true`` (nexus-3lcda): the intro says what the payload marked and gives
        the checkpoint's age, and claims neither a failed pre nor how the client times its compactions."""
        run(mod, monkeypatch, "pre", pre_payload())
        restamp(env.checkpoints / f"{SID}.md", 69 * 60)
        first = self._ctx(run(mod, monkeypatch, "inject",
                              {"session_id": SID, "source": "compact", "precomputed": True})[1]).splitlines()[0]
        assert "69 min ago (trigger auto)" in first
        assert "summary was prepared while the session was idle" in first
        assert "failed" not in first and "predate" not in first and "away" not in first
        assert "check git, beads and the board before acting on it" in first

    @pytest.mark.parametrize("value", [False, None, "true", 1], ids=["false", "null", "string", "int"])
    def test_only_a_true_precomputed_changes_the_intro(self, mod, env, monkeypatch, value) -> None:
        run(mod, monkeypatch, "pre", pre_payload())
        restamp(env.checkpoints / f"{SID}.md", 69 * 60)
        first = self._ctx(run(mod, monkeypatch, "inject",
                              {"session_id": SID, "source": "compact", "precomputed": value})[1]).splitlines()[0]
        assert "while the session was idle" not in first and "may predate this compaction" in first and "failed" not in first

    def test_a_checkpoint_is_injected_once_and_a_new_pre_is_injected_again(self, mod, env, monkeypatch) -> None:
        run(mod, monkeypatch, "pre", pre_payload())
        path = env.checkpoints / f"{SID}.md"
        written = re.search(r"(?m)^- written: (\S+)$", path.read_text(encoding="utf-8")).group(1)
        assert run(mod, monkeypatch, "inject", {"session_id": SID})[1] != ""
        assert (env.checkpoints / f"{SID}.injected").read_text(encoding="utf-8").strip() == written
        assert run(mod, monkeypatch, "inject", {"session_id": SID}) == (0, "")
        restamp(path, -60)  # the next compaction's pre: a new written: stamp
        assert "state: building" in self._ctx(run(mod, monkeypatch, "inject", {"session_id": SID})[1])

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
        run(mod, monkeypatch, "pre", pre_payload())  # the previous compaction, used in full
        run(mod, monkeypatch, "post", {"session_id": SID, "compact_summary": "the previous summary"})
        run(mod, monkeypatch, "inject", {"session_id": SID, "source": "compact"})
        run(mod, monkeypatch, "post", {"session_id": SID, "compact_summary": "the summary of this compaction"})
        code, out = run(mod, monkeypatch, "inject", {"session_id": SID, "source": "compact"})
        # The old state is not injected; the session is told there is none (nexus-2v7da).
        ctx = self._ctx(out)
        assert code == 0 and "no working-state checkpoint" in ctx
        assert "state: building" not in ctx and "the previous summary" not in ctx

    def test_the_intro_states_the_checkpoints_age_and_trigger(self, mod, env, monkeypatch) -> None:
        run(mod, monkeypatch, "pre", pre_payload(trigger="auto"))
        restamp(env.checkpoints / f"{SID}.md", 3 * 60)
        ctx = self._ctx(run(mod, monkeypatch, "inject", {"session_id": SID})[1])
        assert "3 min ago (trigger auto), just before this session was compacted." in ctx.splitlines()[0]

    @pytest.mark.parametrize(("minutes", "stale"), [(9, False), (11, True)])
    def test_the_stale_note_starts_at_ten_minutes(self, mod, env, monkeypatch, minutes, stale) -> None:
        run(mod, monkeypatch, "pre", pre_payload())
        restamp(env.checkpoints / f"{SID}.md", minutes * 60)
        first = self._ctx(run(mod, monkeypatch, "inject", {"session_id": SID})[1]).splitlines()[0]
        assert ("may predate this compaction" in first) is stale
        assert ("just before this session was compacted" in first) is not stale

    @pytest.mark.parametrize("garbage", [b"\xff\xfe\x80", b"not a stamp\n", b""], ids=["non-utf8", "text", "empty"])
    def test_a_garbage_sidecar_reads_as_not_injected(self, mod, env, monkeypatch, garbage) -> None:
        """A non-UTF-8 sidecar raised UnicodeDecodeError, and inject printed nothing for two days."""
        run(mod, monkeypatch, "pre", pre_payload())
        (env.checkpoints / f"{SID}.injected").write_bytes(garbage)
        assert "state: building" in self._ctx(run(mod, monkeypatch, "inject", {"session_id": SID})[1])
        if garbage == b"\xff\xfe\x80":
            assert "treating" in env.hook_log.read_text(encoding="utf-8"), "a garbled sidecar is logged"
        assert run(mod, monkeypatch, "inject", {"session_id": SID}) == (0, ""), "the rewritten sidecar suppresses"

    def test_an_undecodable_checkpoint_is_not_injected_and_post_archives_it(self, mod, env, monkeypatch) -> None:
        env.checkpoints.mkdir(parents=True)
        path = env.checkpoints / f"{SID}.md"
        path.write_bytes(b"# Compaction checkpoint\n\xff\xfe\n")
        assert run(mod, monkeypatch, "inject", {"session_id": SID}) == (0, "")
        assert "inject: " in env.hook_log.read_text(encoding="utf-8") and "is not valid UTF-8" in env.hook_log.read_text(encoding="utf-8")
        assert run(mod, monkeypatch, "post", {"session_id": SID, "compact_summary": "s"}) == (0, "")
        assert re.search(r"post: .* is not valid UTF-8 .*archiving it", env.hook_log.read_text(encoding="utf-8")), \
            "post's own read-error line"
        assert "no pre-compaction checkpoint for this compaction" in path.read_text(encoding="utf-8")
        assert len([p for p in env.checkpoints.iterdir() if p.name != path.name]) == 1, "the bad file is archived"

    @pytest.mark.parametrize("inject_first", [True, False], ids=["inject-then-post", "post-then-inject"])
    def test_after_a_complete_compaction_a_compaction_whose_pre_failed_restores_nothing_and_says_so(
        self, mod, env, monkeypatch, inject_first
    ) -> None:
        path = env.checkpoints / f"{SID}.md"
        run(mod, monkeypatch, "pre", pre_payload())  # compaction 1, complete, in the live order
        run(mod, monkeypatch, "inject", {"session_id": SID, "source": "compact"})
        run(mod, monkeypatch, "post", {"session_id": SID, "compact_summary": "summary one"})
        # compaction 2: pre failed
        if inject_first:
            self._age_sidecar(env, 5 * 60)  # compaction 1's inject was minutes ago, not this compaction's
            code, out = run(mod, monkeypatch, "inject", {"session_id": SID, "source": "compact"})
            ctx = self._ctx(out)  # compaction 1's checkpoint, already used: told so, never its state
            assert code == 0 and "no working-state checkpoint" in ctx and "state: building" not in ctx
        run(mod, monkeypatch, "post", {"session_id": SID, "compact_summary": "summary two"})
        if not inject_first:
            code, out = run(mod, monkeypatch, "inject", {"session_id": SID, "source": "compact"})
            ctx = self._ctx(out)  # the stub's notice (nexus-2v7da), never compaction 1's state
            assert code == 0 and "no working-state checkpoint" in ctx and "state: building" not in ctx
        text = path.read_text(encoding="utf-8")
        assert "no pre-compaction checkpoint for this compaction" in text and "summary two" in text
        assert "summary one" not in text

    @pytest.mark.parametrize(
        "title",
        ["Compact summary pairing", "Compact summary (2026-11-01 notes)", "Compact summary (2026-11-01T handoff)",
         "Compact summary (2026-11-01T21:27:09+00:00) and more", "Compact summary (2026-11-01T21:27:09Z)",
         "Compact summary (2026-11-01T21:27:09-07:00)"],
        ids=["prefix", "date-only", "date-T-text", "stamp-then-text", "zulu-stamp", "offset-stamp"],
    )
    def test_a_board_block_titled_compact_summary_is_not_taken_for_posts_heading(
        self, mod, env, monkeypatch, title
    ) -> None:
        run(mod, monkeypatch, "pre", pre_payload())
        path = env.checkpoints / f"{SID}.md"
        text = path.read_text(encoding="utf-8")
        assert text.count("\n## Working state") == 1
        path.write_text(text.replace("\n## Working state", f"\n## {title}\nref: nexus-mtl98\n\n## Working state"),
                        encoding="utf-8")
        ctx = self._ctx(run(mod, monkeypatch, "inject", {"session_id": SID, "source": "compact"})[1])
        assert f"## {title}" in ctx and "P2 Auto-continuation" in ctx, "inject cut at the board block"
        run(mod, monkeypatch, "post", {"session_id": SID, "compact_summary": "this compaction's summary"})
        after = path.read_text(encoding="utf-8")
        assert "P2 Auto-continuation" in after and after.rstrip().endswith("this compaction's summary"), \
            "post took the board block for its own heading and archived the checkpoint"

    def test_posts_heading_is_matched_as_post_writes_it(self, mod, env, monkeypatch) -> None:
        run(mod, monkeypatch, "pre", pre_payload())
        run(mod, monkeypatch, "post", {"session_id": SID, "compact_summary": "s"})
        text = (env.checkpoints / f"{SID}.md").read_text(encoding="utf-8")
        assert mod._SUMMARY_RE.search(text), "the pattern must match post's own heading"

    def test_a_summary_with_a_lone_surrogate_is_written_not_lost(self, mod, env, monkeypatch) -> None:
        run(mod, monkeypatch, "pre", pre_payload())
        assert run(mod, monkeypatch, "post", {"session_id": SID, "compact_summary": "before \ud83d after"}) == (0, "")
        text = (env.checkpoints / f"{SID}.md").read_text(encoding="utf-8")
        assert "## Compact summary (" in text and "before ? after" in text

    def test_a_lone_surrogate_in_the_trigger_still_writes_the_checkpoint(self, mod, env, monkeypatch) -> None:
        run(mod, monkeypatch, "pre", pre_payload(trigger="auto\ud83d"))
        assert "- trigger: auto?" in (env.checkpoints / f"{SID}.md").read_text(encoding="utf-8")

    def test_the_sidecar_is_pruned_with_the_checkpoints(self, mod, env, monkeypatch) -> None:
        env.checkpoints.mkdir(parents=True)
        old = env.checkpoints / f"{OTHER_SID}.injected"
        old.write_text("2026-01-01T00:00:00+00:00\n", encoding="utf-8")
        three_days = time.time() - 3 * 24 * 3600
        os.utime(old, (three_days, three_days))
        run(mod, monkeypatch, "pre", pre_payload())
        assert not old.exists()

    @pytest.mark.parametrize(
        ("seconds", "label"),
        [(30, "under a minute"), (89, "under a minute"), (180, "3 min"), (89 * 60, "89 min"),
         (90 * 60, "1 h 30 min"), (5 * 3600 + 59, "5 h 1 min"), (25 * 3600, "25 h 0 min")],
    )
    def test_the_age_label(self, mod, seconds, label) -> None:
        assert mod._age_label(seconds) == label

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
        assert "P2 Auto-continuation" in ctx and "state: building" in ctx
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


class TestDeferredApply:
    """A compaction applied long after its pre (nexus-3lcda, nexus-702gy): measured on Desktop 2.1.309 at 7 to 69
    minutes. All three verbs, on a fixed clock, in the live order (inject about 6 s before post)."""

    T0 = datetime(2026, 11, 1, 22, 18, 1, tzinfo=timezone.utc)

    @pytest.mark.parametrize("precomputed", [True, None], ids=["precomputed", "no-precomputed-key"])
    def test_pre_then_inject_and_post_69_minutes_later(self, mod, env, monkeypatch, precomputed) -> None:
        clock = [self.T0]
        monkeypatch.setattr(mod, "_now", lambda: clock[0])
        extra = {} if precomputed is None else {"precomputed": precomputed}
        run(mod, monkeypatch, "pre", pre_payload(hook_event_name="PreCompact"))
        path = env.checkpoints / f"{SID}.md"
        before = path.read_text(encoding="utf-8")
        assert "- written: 2026-11-01T22:18:01+00:00" in before

        clock[0] = self.T0 + timedelta(minutes=69)
        out = run(mod, monkeypatch, "inject", {"session_id": SID, "hook_event_name": "SessionStart",
                                               "source": "compact", **extra})[1]
        ctx = json.loads(out)["hookSpecificOutput"]["additionalContext"]
        first = ctx.splitlines()[0]
        assert "69 min ago (trigger auto)" in first and "state: building" in ctx
        assert "failed" not in first, "an old checkpoint is not a failed pre"
        assert ("while the session was idle" in first) is bool(precomputed)

        clock[0] += timedelta(seconds=6)
        run(mod, monkeypatch, "post", {"session_id": SID, "hook_event_name": "PostCompact", "trigger": "auto",
                                       "compact_summary": "the deferred summary", **extra})
        text = path.read_text(encoding="utf-8")
        assert text.startswith(before.rstrip()) and text.rstrip().endswith("the deferred summary")
        assert "no pre-compaction checkpoint for this compaction" not in text, "post wrote a stub"
        assert [p.name for p in env.checkpoints.iterdir() if p.suffix == ".md"] == [path.name], "nothing archived"
        log = env.hook_log.read_text(encoding="utf-8")
        assert "writing a stub" not in log and "WARNING" not in log


class TestBoardBudget:
    """The whole board lookup (search, then the title fallback's list and get) fits ``_BOARD_TIMEOUT_S`` end
    to end (nexus-3lcda review). ``_run_nx`` is replaced by a stand-in that spends a fake clock: it is handed
    a timeout and burns ``min(duration, timeout)``, so the arithmetic is exact and nothing sleeps."""

    @staticmethod
    def drive(mod, monkeypatch, behave):
        """Run ``_board_entry`` on a fake clock. *behave(kind, args)* gives ``(seconds, stdout)``; ``None``
        seconds never answers. Returns ``(result, calls, elapsed)``; *calls* is ``(kind, timeout, started_at)``."""
        clock = [0.0]
        calls: list[tuple[str, float, float]] = []
        monkeypatch.setattr(mod, "_mono", lambda: clock[0], raising=False)

        def fake(args, cwd, timeout):
            kind = args[1]
            calls.append((kind, timeout, clock[0]))
            assert timeout <= mod._BOARD_TIMEOUT_S - clock[0] + 1e-9, f"{kind} was given more than the budget has left"
            seconds, out = behave(kind, args)
            if seconds is None or seconds > timeout:
                clock[0] += timeout
                return "", f"`nx memory {kind}` timed out after {timeout:.0f}s"
            clock[0] += seconds
            return out, None

        monkeypatch.setattr(mod, "_run_nx", fake)
        result = mod._board_entry(SID, NAME, None, mod._BOARD_TIMEOUT_S)
        return result, calls, clock[0]

    TITLE_ROW = "[101] threads/linda  (-, 2026-10-31T21:46:51Z)"

    def test_a_hung_search_is_capped_and_the_title_fallback_is_reached(self, mod, monkeypatch) -> None:
        def behave(kind, args):
            return {"search": (None, ""), "list": (0.3, self.TITLE_ROW), "get": (0.3, "## board of linda")}[kind]

        (title, body, how, error), calls, elapsed = self.drive(mod, monkeypatch, behave)
        assert [c[0] for c in calls] == ["search", "list", "get"]
        assert calls[0][1] <= 2.0, "the search is capped at 2 s"
        assert (title, how, error) == (NAME, "exact title", None) and "board of linda" in body
        assert elapsed <= mod._BOARD_TIMEOUT_S

    def test_a_slow_search_and_a_slow_list_never_exceed_the_budget(self, mod, monkeypatch) -> None:
        def behave(kind, args):
            return {"search": (1.9, "No results found."), "list": (3.9, self.TITLE_ROW), "get": (3.0, "## board")}[kind]

        (_title, body, _how, error), calls, elapsed = self.drive(mod, monkeypatch, behave)
        assert [c[0] for c in calls] == ["search", "list"], "no get is started on the 0.2 s left"
        assert body == "" and error and "time budget" in error
        assert elapsed <= mod._BOARD_TIMEOUT_S

    def test_hits_whose_fetches_hang_leave_the_fallback_its_share(self, mod, monkeypatch) -> None:
        hits = "\n".join(f"[{n}] threads/h{n}  (-, 2026-11-0{n - 200}T00:00:00Z)" for n in (201, 202, 203, 204))

        def behave(kind, args):
            if kind == "search":
                return 0.2, hits
            if kind == "get" and args[2].isdigit() and int(args[2]) >= 200:
                return None, ""
            return {"list": (0.3, self.TITLE_ROW), "get": (0.3, "## board of linda")}[kind]

        (title, body, how, error), calls, elapsed = self.drive(mod, monkeypatch, behave)
        assert [c[0] for c in calls] == ["search", "get", "list", "get"], "the first hung fetch ends the session-line phase"
        assert (title, how, error) == (NAME, "exact title", None) and "board of linda" in body
        assert elapsed <= mod._BOARD_TIMEOUT_S

    def test_hits_that_could_not_be_fetched_are_named_not_called_absent(self, mod, monkeypatch) -> None:
        """A search that found hits whose fetches failed must not report that no entry carries the line
        (review r2 L2); the warning names the failed fetch instead."""
        hits = "[201] threads/h201  (-, 2026-11-01T00:00:00Z)"

        def behave(kind, args):
            if kind == "search":
                return 0.2, hits
            if kind == "get" and args[2] == "201":
                return None, ""
            return {"list": (0.3, "No entries."), "get": (0.3, "")}[kind]

        (_title, body, _how, error), _calls, elapsed = self.drive(mod, monkeypatch, behave)
        assert body == "" and error
        assert "carries `session:" not in error, error
        assert "could not be fetched" in error and "timed out" in error, error
        assert elapsed <= mod._BOARD_TIMEOUT_S

    def test_a_call_is_not_started_on_less_than_the_minimum(self, mod, monkeypatch) -> None:
        def behave(kind, args):
            return {"search": (2.0, ""), "list": (None, ""), "get": (1.0, "x")}[kind]

        _result, calls, elapsed = self.drive(mod, monkeypatch, behave)
        assert all(c[1] >= mod._MIN_CALL_S for c in calls) and elapsed <= mod._BOARD_TIMEOUT_S


class TestProbe:
    """One INFO line per verb run: the probe that shows what Desktop's payload carries (nexus-3lcda)."""

    @staticmethod
    def lines(env) -> list[str]:
        return [ln for ln in env.hook_log.read_text(encoding="utf-8").splitlines() if "probe verb=" in ln]

    def test_each_verb_logs_one_info_line_with_event_source_keys_and_outcome(self, mod, env, monkeypatch) -> None:
        run(mod, monkeypatch, "pre", pre_payload())
        run(mod, monkeypatch, "inject", {"session_id": SID, "hook_event_name": "SessionStart", "source": "compact",
                                         "cwd": "/w", "precomputed": True})
        run(mod, monkeypatch, "post", {"session_id": SID, "hook_event_name": "PostCompact", "trigger": "auto",
                                       "compact_summary": "SECRET-SUMMARY-TEXT"})
        pre, inject, post = self.lines(env)
        for line in (pre, inject, post):
            assert " INFO compact-checkpoint: probe verb=" in line
        assert "verb=pre event=PreCompact source=- trigger=auto" in pre
        assert "keys=[cwd,hook_event_name,session_id,trigger]" in pre
        assert "outcome=wrote checkpoint" in pre and "board: exact title" in pre
        assert "verb=inject event=SessionStart source=compact trigger=-" in inject
        assert "keys=[cwd,hook_event_name,precomputed,session_id,source]" in inject and "precomputed=True" in inject
        assert "outcome=emitted" in inject
        assert "verb=post event=PostCompact source=- trigger=auto" in post
        assert "keys=[compact_summary,hook_event_name,session_id,trigger]" in post and "precomputed=" not in post
        assert "outcome=appended summary" in post
        assert "SECRET-SUMMARY-TEXT" not in env.hook_log.read_text(encoding="utf-8"), "values are never logged"

    @pytest.mark.parametrize("source", ["startup", "resume", "clear", None])
    def test_inject_on_a_non_compact_start_writes_nothing(self, mod, env, monkeypatch, source) -> None:
        """inject is a no-op unless SessionStart's source is compact; a no-op writes no probe line and so no
        hook.log (a session with no CLI writes nothing; tests/hooks/test_nx_launch.py pins it end to end)."""
        payload = {"session_id": SID, "hook_event_name": "SessionStart", "cwd": "/w"}
        if source is not None:
            payload["source"] = source
        run(mod, monkeypatch, "inject", payload)
        assert not env.hook_log.exists() or not self.lines(env), env.hook_log.read_text(encoding="utf-8")

    def test_the_outcome_names_a_skip_and_a_stub(self, mod, env, monkeypatch) -> None:
        run(mod, monkeypatch, "inject", {"session_id": SID, "source": "compact"})
        run(mod, monkeypatch, "post", {"session_id": SID, "compact_summary": "orphan"})
        run(mod, monkeypatch, "inject", {"session_id": SID, "source": "compact"})  # a stub: nothing to emit
        inject_none, post_stub, inject_stub = self.lines(env)
        assert "outcome=emitted: no restore (no checkpoint file)" in inject_none
        assert "outcome=wrote stub" in post_stub
        assert "outcome=emitted: no restore (stub)" in inject_stub
        assert "event=-" in inject_none, "a payload without hook_event_name logs a dash"

    def test_the_board_route_and_a_missing_session_id_are_in_the_outcome(self, mod, env, monkeypatch) -> None:
        monkeypatch.setenv("FAKE_SEARCH", "hit")
        run(mod, monkeypatch, "pre", pre_payload())
        run(mod, monkeypatch, "pre", {"hook_event_name": "PreCompact", "trigger": "manual"})
        by_id, bad = self.lines(env)
        assert "board: session id" in by_id
        assert "outcome=skipped: no usable session id" in bad

    def test_only_four_values_are_logged_and_only_when_str_or_bool(self, mod, env, monkeypatch) -> None:
        """cwd, an unknown key, compact_summary and non-scalar values of the allowed keys never reach the log."""
        base = {"cwd": "/CANARY-CWD", "unknown_key": "CANARY-EXTRA",
                "compact_summary": "CANARY-SUMMARY", "agent_name": "CANARY-AGENT"}
        run(mod, monkeypatch, "pre", pre_payload(**base))
        run(mod, monkeypatch, "inject", {"session_id": SID, **base, "hook_event_name": "SessionStart",
                                         "source": "compact", "trigger": {"nested": "CANARY-NESTED"},
                                         "precomputed": ["CANARY-LIST"]})
        run(mod, monkeypatch, "post", {"session_id": SID, **base, "hook_event_name": "PostCompact",
                                       "trigger": "auto", "precomputed": True})
        probes = "\n".join(self.lines(env))
        assert len(self.lines(env)) == 3
        for canary in ("CANARY-CWD", "CANARY-EXTRA", "CANARY-SUMMARY", "CANARY-AGENT", "CANARY-NESTED", "CANARY-LIST"):
            assert canary not in probes, f"{canary} was logged"
        for key in ("cwd", "unknown_key", "compact_summary", "agent_name"):
            assert key in probes, "keys are logged by name"
        assert "event=SessionStart source=compact trigger=-" in probes, "a dict value reads as absent"
        assert "precomputed=True" in probes and "precomputed=-" in probes, "a list value reads as absent"

    def test_a_subagent_payload_logs_no_probe(self, mod, env, monkeypatch) -> None:
        run(mod, monkeypatch, "pre", {**pre_payload(), "agent_id": "a1"})
        assert self.lines(env) == []

    def test_hostile_keys_and_values_cannot_forge_a_log_line(self, mod, env, monkeypatch) -> None:
        run(mod, monkeypatch, "post", {"session_id": SID, "trigger": "x\nFORGED ERROR line", "compact_summary": "s",
                                       "k\ney": 1, "precomputed": "yes\nFORGED"})
        log = env.hook_log.read_text(encoding="utf-8")
        assert not any(ln.startswith("FORGED") for ln in log.splitlines())
        assert len(self.lines(env)) == 1


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
        own = {"_cli_lookup", "_endpoint_resolve", "_session_facts"}
        assert {r for r in roots if r.startswith("_")} <= {"__future__", *own}
        assert all((SCRIPT.parent / f"{m}.py").is_file() for m in own)
        # The sibling the checkpoint shares with the restart script holds to the same rule.
        facts = ast.parse((SCRIPT.parent / "_session_facts.py").read_text(encoding="utf-8"))
        theirs = {a.name.split(".")[0] for n in ast.walk(facts) if isinstance(n, ast.Import) for a in n.names}
        theirs |= {n.module.split(".")[0] for n in ast.walk(facts) if isinstance(n, ast.ImportFrom) and n.module}
        assert theirs and all(r in sys.stdlib_module_names or r == "__future__" for r in theirs), theirs

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

    def test_end_to_end_as_hooks_json_launches_it(self, tmp_path: Path) -> None:
        """A real subprocess: nx found through PATH, config through the env, exit 0, three modes.
        On Windows the stand-in is a real nx.exe (the plugin's PATH lookup refuses .cmd) and the
        env keeps SYSTEMROOT, without which the child cannot import asyncio (nexus-qowvl)."""
        bindir = tmp_path / "bin"
        bindir.mkdir()
        if IS_WINDOWS:
            install_windows_exe_fake(bindir, "nx", f"{WINDOWS_SHEBANG}\n{_STANDIN}")
        else:
            nx = bindir / "nx"
            nx.write_text(f"#!{sys.executable}\n{_STANDIN}", encoding="utf-8")
            nx.chmod(0o755)
        claude = tmp_path / "claude"
        (claude / "sessions").mkdir(parents=True)
        (claude / "sessions" / "1.json").write_text(json.dumps({"sessionId": SID, "name": NAME}), encoding="utf-8")
        env = scrubbed_env(**{
            "PATH": f"{bindir}{os.pathsep}/usr/bin{os.pathsep}/bin",
            "HOME": str(tmp_path / "home"),
            "NEXUS_CONFIG_DIR": str(tmp_path / "config"),
            "CLAUDE_CONFIG_DIR": str(claude),
            "NX_TOOLS_DIR": str(tmp_path / "no-tools"),
            "FAKE_NX_LOG": str(tmp_path / "nx.log"),
        })

        def launch(mode: str, payload: dict | str) -> subprocess.CompletedProcess:
            return subprocess.run(
                [sys.executable, str(SCRIPT), mode], input=payload if isinstance(payload, str) else json.dumps(payload),
                capture_output=True, text=True, encoding="utf-8", timeout=60, env=env, check=False,
            )

        pre = launch("pre", pre_payload())
        assert pre.returncode == 0 and pre.stdout == "", pre.stderr
        assert (tmp_path / "config" / "compact-checkpoints" / f"{SID}.md").is_file()
        # The script exits 0 whatever happens, so prove the PATH lookup reached the stand-in.
        assert (tmp_path / "nx.log").read_text(encoding="utf-8").strip(), "nx was never launched"
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
