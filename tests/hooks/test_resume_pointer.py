# SPDX-License-Identifier: AGPL-3.0-or-later
"""RDR-253 Step 6 part 2: the hooks leave one ``resume-pointer`` entry in identity scratch.

Pins ``conexus/hooks/scripts/_resume_pointer.py`` and its two callers in ``compact_checkpoint.py`` (PreCompact, and
SessionEnd with reason ``clear``): which thread is chosen and what the line says, put-before-delete pruning that never
touches another entry, the single log line on a failure (with the error tail, not the echoed command) and no fallback
write, the time bounds, and that nothing here can cost the checkpoint. ``nx`` is a stub runner: no storage. The reader half is
``tests/hooks/test_resume_line.py``; the real ``nx scratch list`` text the writer parses is pinned here too.
"""
from __future__ import annotations

import ast
import importlib.util
import json
import sys
from datetime import datetime, timezone
from pathlib import Path

import pytest
from click.testing import CliRunner

from nexus.commands import scratch as scratch_cmd
from nexus.db.http_scratch_store import IDENTITY_RESUME_TAG
from nexus.hooks import resume_line
from tests._module_seam import setattr_in

SCRIPTS = Path(__file__).resolve().parents[2] / "conexus" / "hooks" / "scripts"
SID = "11111111-2222-3333-4444-555555555555"
NOW = datetime(2026, 11, 20, 9, 0, 0, tzinfo=timezone.utc)
HEAD = "resume 2026-11-19T10:00:00Z | thread: old"
BOARD = (
    f"session: {SID}\nidentity: i12\n"
    "## Decoy thread listed first\nref: nexus-zzzzz\nstate: other work\nworktree: /w/other\n\n"
    "## RDR-253 step 6 part 2\nref: nexus-abc12\nstate: tests written, hook next\nworktree: /w/elsinore\n"
    "waiting_on: review round\n\n"
    "## A later thread\nstate: should not be read\nworktree: /w/later\n"
)


@pytest.fixture()
def mod():
    spec = importlib.util.spec_from_file_location("compact_checkpoint_resume_under_test", SCRIPTS / "compact_checkpoint.py")
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    module._load_siblings()
    return module


@pytest.fixture()
def rp(mod):
    return mod._resume_pointer


class FakeNx:
    """A recording ``_run_nx``: ``entries`` is the identity scope's listing as (id8, tags, content), ``fail`` maps a
    verb to the error ``_run_nx`` would return."""

    def __init__(self, entries=(), fail=None):
        self.entries = list(entries)
        self.fail = fail or {}
        self.calls: list[tuple[list[str], float]] = []

    def __call__(self, args, cwd, timeout):
        self.calls.append((list(args), timeout))
        assert args[:3] == ["scratch", "--scope", "identity"], f"no write may leave the identity scope: {args}"
        verb = args[3]
        if verb in self.fail:
            return "", self.fail[verb]
        if verb == "list":
            if not self.entries:
                return "No scratch entries.\n", None
            return "".join(f"[{i}] {t}  flagged=False\n  {c[:120]}\n" for i, t, c in self.entries), None
        if verb == "delete":
            return f"Deleted: {args[4]}\n", None
        return "Stored: abc\n", None

    def verbs(self):
        return [a[3] for a, _t in self.calls]


def logs():
    got: list[tuple[str, str]] = []
    return got, lambda level, msg: got.append((level, msg))


class TestContent:
    def test_the_entry_names_the_thread_in_the_checkpoints_worktree_not_the_first_one(self, rp) -> None:
        line = rp.compose(BOARD, [], "/w/elsinore", now=NOW)
        assert line == ("resume 2026-11-20T09:00:00Z | thread: RDR-253 step 6 part 2 | worktree: /w/elsinore | "
                        "state: tests written, hook next | waiting_on: review round")

    def test_with_no_worktree_match_the_thread_naming_an_in_progress_bead_wins(self, rp) -> None:
        rows = ["◐ nexus-abc12 ● P2 the bead the board names"]
        line = rp.compose(BOARD, rows, "/w/nowhere-listed", now=NOW)
        assert "thread: RDR-253 step 6 part 2" in line and "worktree: /w/nowhere-listed" in line

    def test_with_neither_the_first_thread_is_used(self, rp) -> None:
        line = rp.compose(BOARD, [], None, now=NOW)
        assert "thread: Decoy thread listed first" in line and "state: other work" in line

    def test_a_bead_row_no_board_ref_names_is_never_used(self, rp) -> None:
        """The continuation lists every session's beads; only one the board's ref: lines name is this session's."""
        board = "## Work\nref: nexus-mine1\n"
        line = rp.compose(board, ["◐ nexus-peer9 ● P2 somebody else's bead"], None, now=NOW)
        assert "nexus-peer9" not in line and "bead:" not in line

    def test_a_named_bead_row_stands_in_for_a_missing_state_and_is_labelled_a_bead(self, rp) -> None:
        board = "## Work\nref: nexus-mine1\n"
        line = rp.compose(board, ["◐ nexus-peer9 ● P2 not ours", "◐ nexus-mine1 ● P2 ours"], None, now=NOW)
        assert "bead: ◐ nexus-mine1 ● P2 ours" in line and "next:" not in line and "state:" not in line

    def test_a_longer_bead_id_does_not_count_as_named(self, rp) -> None:
        board = "## Work\nref: nexus-mine1.5\n"
        assert "bead:" not in rp.compose(board, ["◐ nexus-mine1 ● P2 parent"], None, now=NOW)

    def test_nothing_to_point_at_writes_nothing(self, rp) -> None:
        assert rp.compose("", [], None) is None
        assert rp.compose("no heading here\nstate: x\n", [], None) is None

    def test_one_line_within_the_clamp_whatever_the_board_holds(self, rp) -> None:
        # 4-byte characters: each field fits its own character clamp, and together they are past the byte clamp
        emoji = "\U0001f600"
        board = f"## {emoji * 200}\nstate: {emoji * 1000}\nwaiting_on: {emoji * 400}\nworktree: /w/{emoji * 500}\n"
        line = rp.compose(board, [], None)
        assert "\n" not in line and len(line.encode("utf-8")) <= rp.MAX_BYTES
        assert line.startswith("resume ") and "thread: " in line, "the cut keeps the head of the line"

    def test_a_pipe_in_a_title_cannot_forge_a_field(self, rp) -> None:
        line = rp.compose("## a | worktree: /evil\nstate: x\n", [], None)
        assert line.count("| worktree:") == 0


class TestWrite:
    def old(self, i="aaaaaaaa"):
        return (i, "resume-pointer", HEAD)

    def test_the_new_line_is_put_before_the_earlier_pointers_are_deleted(self, rp) -> None:
        nx = FakeNx(entries=[self.old("aaaaaaaa"), self.old("dddddddd")])
        got, log = logs()
        assert rp.write("resume X | thread: t", nx, None, log) == "identity"
        assert nx.verbs() == ["list", "put", "delete", "delete"], "list, put, then prune from that listing"
        put = next(a for a, _t in nx.calls if a[3] == "put")
        assert put[4] == "resume X | thread: t" and put[5:] == ["--tags", "resume-pointer"]
        assert [a[4] for a, _t in nx.calls if a[3] == "delete"] == ["aaaaaaaa", "dddddddd"]
        assert len(got) == 1 and got[0][0] == "INFO" and "outcome=identity" in got[0][1] and " ms=" in got[0][1]

    def test_a_failed_put_leaves_the_last_good_pointer(self, rp) -> None:
        nx = FakeNx(entries=[self.old()], fail={"put": "`nx scratch ...` exited 1: 401 unauthorized"})
        got, log = logs()
        assert rp.write("resume X | thread: t", nx, None, log) == "none"
        assert "delete" not in nx.verbs(), "the delete only follows a put that worked"

    def test_only_a_tagged_entry_with_the_pointer_head_is_deleted(self, rp) -> None:
        nx = FakeNx(entries=[
            ("aaaaaaaa", "resume-pointer", HEAD),
            ("bbbbbbbb", "working", HEAD),                                         # the head, the wrong tag
            ("cccccccc", "resume-pointer", "a note that merely wears the tag"),    # the tag, no head
            ("dddddddd", "resume", HEAD),                                          # the retired tag
            ("eeeeeeee", "working,resume-pointer", HEAD),                          # tag in a list: still the exact tag
            ("ffffffff", "-", "plain"),
        ])
        got, log = logs()
        rp.write("resume X | thread: t", nx, None, log)
        assert [a[4] for a, _t in nx.calls if a[3] == "delete"] == ["aaaaaaaa", "eeeeeeee"]

    def test_a_failure_logs_one_line_with_the_error_tail_and_never_the_entrys_text(self, rp) -> None:
        secret = "resume X | thread: SECRET-THREAD-TEXT"
        for failing in ("list", "put"):
            nx = FakeNx(entries=[self.old()],
                        fail={failing: f"`nx scratch --scope identity {failing} {secret}` exited 1: HTTP 401 unauthorized"})
            got, log = logs()
            assert rp.write(secret, nx, None, log) == "none"
            assert len(got) == 1 and got[0][0] == "WARNING"
            assert "401 unauthorized" in got[0][1] and "SECRET-THREAD-TEXT" not in got[0][1], got
            assert nx.verbs() == (["list"] if failing == "list" else ["list", "put"]), "no fallback write anywhere"

    def test_a_timeout_names_the_timeout(self, rp) -> None:
        nx = FakeNx(fail={"list": "`nx scratch --scope identity list` timed out after 3s"})
        got, log = logs()
        rp.write("resume X | thread: t", nx, None, log)
        assert "timed out after 3s" in got[0][1] and "--scope" not in got[0][1]

    def test_a_runner_that_raises_is_contained(self, rp) -> None:
        def boom(*_a):
            raise RuntimeError("kaboom")

        got, log = logs()
        assert rp.write("resume X | thread: t", boom, None, log) == "none"
        assert len(got) == 1 and "kaboom" in got[0][1]

    def test_every_call_is_bounded_by_what_is_left_of_the_budget_the_caller_gave(self, rp, monkeypatch) -> None:
        clock = {"t": 1000.0}
        setattr_in(monkeypatch, rp, "time.monotonic", lambda: clock["t"])

        class Slow(FakeNx):
            def __call__(self, args, cwd, timeout):
                clock["t"] += 1.4
                return super().__call__(args, cwd, timeout)

        nx = Slow(entries=[self.old()])
        got, log = logs()
        rp.write("resume X | thread: t", nx, None, log, budget=2.0)
        assert all(t <= 2.0 for _a, t in nx.calls) and nx.calls[0][1] == 2.0
        assert nx.verbs() == ["list", "put"], "0.6 s left after the list, then 0 after the put: no delete"
        assert nx.calls[1][1] == pytest.approx(0.6, abs=1e-6)

    def test_a_budget_the_caller_makes_larger_than_the_cap_is_clamped(self, rp) -> None:
        nx = FakeNx()
        got, log = logs()
        rp.write("resume X | thread: t", nx, None, log, budget=500.0)
        assert all(t <= rp.CALL_TIMEOUT_S for _a, t in nx.calls)

    def test_no_budget_left_writes_nothing_and_says_so_once(self, rp) -> None:
        nx = FakeNx()
        got, log = logs()
        assert rp.write("resume X | thread: t", nx, None, log, budget=0.1) == "none"
        assert nx.calls == [] and len(got) == 1 and "time budget" in got[0][1]


class TestRealListing:
    """The writer parses ``nx scratch --scope identity list``; feed it the real command's output, not a hand-typed copy."""

    def test_resume_ids_reads_what_the_real_list_command_prints(self, rp, monkeypatch) -> None:
        class Store:
            def list_entries(self):
                return [
                    {"id": "11111111-aaaa", "tags": "resume-pointer", "flagged": False,
                     "content": "resume 2026-11-19T10:00:00Z | thread: " + "long " * 80},
                    {"id": "22222222-bbbb", "tags": "working", "flagged": False, "content": "resume 2026-11-19T10:00:00Z | x"},
                    {"id": "33333333-cccc", "tags": "resume-pointer", "flagged": False, "content": "wears the tag only"},
                    {"id": "44444444-dddd", "tags": "", "flagged": False, "content": "line one\nline two"},
                ]

        monkeypatch.setattr(scratch_cmd, "_t1", lambda: Store())
        result = CliRunner().invoke(scratch_cmd.scratch, ["--scope", "identity", "list"])
        assert result.exit_code == 0, result.output
        assert rp.resume_ids(result.output) == ["11111111"]

    def test_the_commands_the_writer_runs_exist_with_these_spellings(self, rp) -> None:
        runner = CliRunner()
        for argv in (["--scope", "identity", "list", "--help"], ["--scope", "identity", "put", "--help"],
                     ["--scope", "identity", "delete", "--help"]):
            assert runner.invoke(scratch_cmd.scratch, argv).exit_code == 0, argv
        put = runner.invoke(scratch_cmd.scratch, ["put", "--help"]).output
        assert "--tags" in put


class TestRunPre:
    def _drive(self, mod, monkeypatch, tmp_path, nx, board=BOARD, worktree="/w/elsinore", clock=None):
        monkeypatch.setattr(mod, "_checkpoint_dir", lambda: tmp_path / "cp")
        monkeypatch.setattr(mod, "_config_dir", lambda: tmp_path)
        monkeypatch.setattr(mod, "_session_name", lambda sid: "linda")
        monkeypatch.setattr(mod, "_board_entry", lambda *a, **k: ("linda", board, "exact title", None))
        monkeypatch.setattr(mod, "_session_worktrees", lambda name, cwd, b: (worktree, []))
        monkeypatch.setattr(mod, "_facts_sections", lambda *a, **k: ["## Board checked against the facts\n"])
        monkeypatch.setattr(mod, "_running_work_section", lambda cwd: [])
        if clock is not None:
            setattr_in(monkeypatch, mod, "time.monotonic", clock)

        def runner(args, cwd, timeout):
            if args[:2] == ["command-context", "continuation"]:
                return ("## Working state\n\n- **branch:** `f`\n\n### In-progress beads\n◐ nexus-abc12 ● P2 the bead\n\n", None)
            return nx(args, cwd, timeout)

        monkeypatch.setattr(mod, "_run_nx", runner)
        return mod.run_pre({"session_id": SID, "trigger": "auto", "cwd": "/w"})

    def test_pre_writes_the_checkpoint_then_one_pointer_from_the_thread_in_the_checkpoints_worktree(self, mod, monkeypatch, tmp_path) -> None:
        nx = FakeNx(entries=[("aaaaaaaa", "resume-pointer", HEAD)])
        outcome = self._drive(mod, monkeypatch, tmp_path, nx)
        assert (tmp_path / "cp" / f"{SID}.md").exists()
        put = next(a for a, _t in nx.calls if a[3] == "put")
        assert "thread: RDR-253 step 6 part 2" in put[4] and "worktree: /w/elsinore" in put[4] and "waiting_on: review round" in put[4]
        assert "resume pointer: identity" in outcome

    def test_a_pointer_that_blows_up_never_costs_the_checkpoint(self, mod, monkeypatch, tmp_path) -> None:
        monkeypatch.setattr(mod._resume_pointer, "compose", lambda *a, **k: (_ for _ in ()).throw(RuntimeError("x")))
        outcome = self._drive(mod, monkeypatch, tmp_path, FakeNx())
        assert (tmp_path / "cp" / f"{SID}.md").read_text(encoding="utf-8").startswith("# Compaction checkpoint")
        assert outcome.startswith("wrote checkpoint") and "resume pointer: failed" in outcome

    def test_an_identity_failure_in_pre_writes_nothing_else_and_the_checkpoint_stands(self, mod, monkeypatch, tmp_path) -> None:
        nx = FakeNx(fail={"list": "`nx scratch` exited 1: 401"})
        outcome = self._drive(mod, monkeypatch, tmp_path, nx)
        assert nx.verbs() == ["list"] and "resume pointer: none" in outcome
        assert (tmp_path / "cp" / f"{SID}.md").exists()

    def test_a_board_with_nothing_to_point_at_writes_no_entry(self, mod, monkeypatch, tmp_path) -> None:
        nx = FakeNx()
        monkeypatch.setattr(mod, "_filter_in_progress", lambda shaped, board: "")
        outcome = self._drive(mod, monkeypatch, tmp_path, nx, board="", worktree=None)
        assert nx.calls == [] and "resume pointer: none" in outcome

    def test_the_pointer_gets_the_time_the_hook_has_left_not_its_full_budget(self, mod, monkeypatch, tmp_path) -> None:
        # the hook has run 34 s when the pointer starts: 38 - 34 = 4 s of budget, not 6
        nx = FakeNx()
        seen = []
        monkeypatch.setattr(mod._resume_pointer, "write", lambda line, run, cwd, log, budget=0, where="pre": seen.append(budget) or "identity")
        ticks = iter([0.0] + [34.0] * 200)
        self._drive(mod, monkeypatch, tmp_path, nx, clock=lambda: next(ticks))
        assert seen and seen[0] == pytest.approx(4.0)

    def test_past_36_seconds_into_the_hook_the_pointer_is_skipped(self, mod, monkeypatch, tmp_path) -> None:
        ticks = iter([0.0] + [37.0] * 200)
        nx = FakeNx()
        outcome = self._drive(mod, monkeypatch, tmp_path, nx, clock=lambda: next(ticks))
        assert nx.calls == [] and "resume pointer: skipped" in outcome
        assert (tmp_path / "cp" / f"{SID}.md").exists()


class TestRunEnd:
    """SessionEnd with reason ``clear``: a /clear in a session that never compacted still leaves a pointer."""

    def _drive(self, mod, monkeypatch, tmp_path, nx, payload=None, board=BOARD):
        monkeypatch.setattr(mod, "_config_dir", lambda: tmp_path)
        monkeypatch.setattr(mod, "_session_name", lambda sid: "linda")
        monkeypatch.setattr(mod, "_board_entry", lambda *a, **k: ("linda", board, "exact title", None))
        monkeypatch.setattr(mod, "_session_worktrees", lambda name, cwd, b: ("/w/elsinore", []))
        monkeypatch.setattr(mod, "_run_nx", nx)
        return mod.run_end(payload or {"session_id": SID, "reason": "clear", "cwd": "/w"})

    def test_a_clear_writes_the_pointer_from_the_board_and_the_worktree(self, mod, monkeypatch, tmp_path) -> None:
        nx = FakeNx()
        outcome = self._drive(mod, monkeypatch, tmp_path, nx)
        put = next(a for a, _t in nx.calls if a[3] == "put")
        assert "thread: RDR-253 step 6 part 2" in put[4] and "worktree: /w/elsinore" in put[4]
        assert "identity" in outcome and not (tmp_path / "compact-checkpoints").exists(), "no checkpoint file at SessionEnd"

    @pytest.mark.parametrize("reason", ["logout", "prompt_input_exit", "resume", "other", "", None])
    def test_any_other_reason_writes_nothing(self, mod, monkeypatch, tmp_path, reason) -> None:
        nx = FakeNx()
        outcome = self._drive(mod, monkeypatch, tmp_path, nx, payload={"session_id": SID, "reason": reason})
        assert nx.calls == [] and outcome.startswith("skipped")

    def test_an_end_with_no_session_id_writes_nothing(self, mod, monkeypatch, tmp_path) -> None:
        nx = FakeNx()
        assert self._drive(mod, monkeypatch, tmp_path, nx, payload={"reason": "clear"}).startswith("skipped")
        assert nx.calls == []

    def test_the_end_pointer_uses_no_continuation_call(self, mod, monkeypatch, tmp_path) -> None:
        """SessionEnd has seconds, not a compaction's half minute: no ``command-context continuation``."""
        seen: list[list[str]] = []

        def nx(args, cwd, timeout):
            seen.append(list(args))
            return "No scratch entries.\n", None

        self._drive(mod, monkeypatch, tmp_path, nx)
        assert seen and all(a[0] == "scratch" for a in seen)

    def test_an_identity_failure_at_the_end_is_logged_once_and_exits_cleanly(self, mod, monkeypatch, tmp_path) -> None:
        nx = FakeNx(fail={"list": "`nx scratch` exited 1: no identity is bound to this process"})
        assert "none" in self._drive(mod, monkeypatch, tmp_path, nx)
        assert nx.verbs() == ["list"]


def test_main_runs_the_end_mode_and_exits_zero(mod, monkeypatch, tmp_path) -> None:
    monkeypatch.setattr(mod, "_read_payload", lambda: {"session_id": SID, "reason": "clear", "hook_event_name": "SessionEnd"})
    monkeypatch.setattr(mod, "run_end", lambda payload: "wrote pointer")
    monkeypatch.setitem(mod._MODES, "end", mod.run_end)
    monkeypatch.setattr(mod, "_config_dir", lambda: tmp_path)
    assert mod.main(["end"]) == 0


def test_the_script_stays_standard_library_plus_siblings() -> None:
    tree = ast.parse((SCRIPTS / "_resume_pointer.py").read_text(encoding="utf-8"))
    names = {a.name.split(".")[0] for n in ast.walk(tree) if isinstance(n, ast.Import) for a in n.names}
    names |= {n.module.split(".")[0] for n in ast.walk(tree) if isinstance(n, ast.ImportFrom) and n.module}
    assert names <= set(sys.stdlib_module_names) | {"__future__"}, names


def test_the_tag_constant_is_the_one_the_store_and_the_reader_use() -> None:
    spec = importlib.util.spec_from_file_location("resume_pointer_parity", SCRIPTS / "_resume_pointer.py")
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    assert module.TAG == IDENTITY_RESUME_TAG == resume_line.TAG == "resume-pointer"


def test_hooks_json_runs_the_end_mode_on_a_clear_session_end_inside_its_time_limit(mod) -> None:
    """The SessionEnd group is wired with matcher ``clear`` (no other end reason writes a pointer), runs ``end``, and its
    timeout covers the board read plus the pointer's deadline with margin."""
    hooks = json.loads((SCRIPTS.parent / "hooks.json").read_text(encoding="utf-8"))["hooks"]["SessionEnd"]
    ours = [(g, h) for g in hooks for h in g["hooks"] if any("compact_checkpoint.py" in a for a in h.get("args", []))]
    assert len(ours) == 1, "exactly one SessionEnd hook runs the checkpoint script"
    group, hook = ours[0]
    assert group["matcher"] == "clear" and hook["args"][-1] == "end"
    assert hook["timeout"] >= mod._END_DEADLINE_S + 2, hook["timeout"]
    assert any("nx-session-end-launcher" in a for g in hooks for h in g["hooks"] for a in h.get("args", [])), "the existing end hook stays"


def test_an_end_that_is_not_a_clear_leaves_no_trace_at_all(mod, monkeypatch, tmp_path) -> None:
    """The matcher keeps other reasons away, but a run that gets one (or a test that runs every SessionEnd entry on a machine
    with no CLI) must write nothing: no pointer, no probe line, nothing under the config directory."""
    monkeypatch.setattr(mod, "_config_dir", lambda: tmp_path)
    monkeypatch.setattr(mod, "_read_payload", lambda: {"session_id": SID, "reason": "logout", "hook_event_name": "SessionEnd"})
    assert mod.main(["end"]) == 0
    assert list(tmp_path.rglob("*")) == []
    monkeypatch.setattr(mod, "_read_payload", lambda: {"session_id": SID, "reason": "clear", "hook_event_name": "SessionEnd"})
    monkeypatch.setattr(mod, "run_end", lambda payload: "wrote pointer")
    monkeypatch.setitem(mod._MODES, "end", mod.run_end)
    assert mod.main(["end"]) == 0
    assert "probe verb=end" in (tmp_path / "logs" / "hook.log").read_text(encoding="utf-8"), "a clear is probed like the other verbs"
