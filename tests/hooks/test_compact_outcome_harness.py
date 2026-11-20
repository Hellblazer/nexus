# SPDX-License-Identifier: AGPL-3.0-or-later
"""The compaction-outcome gate's own machinery (tests/e2e/lib/compact_outcome.py), without `claude`.

The live part (a real session through a real automatic compaction) is tests/e2e/compact-outcome-gate.sh and
is run by hand only: it spends model calls. Nothing here starts `claude` (an autouse fixture makes the
session class raise and points NX_GATE_CLAUDE at nothing), so this module is safe in the default suite.

What is pinned:
- the judge on recorded-shape events, the scratch plugin's hook wiring and file list, and the fix overlay;
- the stub `nx` against the REAL compact_checkpoint.py: every nx call the hook can make (found by reading its
  source, not from a list kept here) must be answered by the stub, so a new hook call the stub does not
  answer fails here before it quietly changes what a live run measures;
- the child environment: the parent session's identity and session markers never reach the run, and the
  scratch plugin can bind no identity and reach no engine;
- the gate's non-vacuity: a run or a matrix with no verdict, no compaction, hooks that did not run, or no
  control arms can fail or be UNVERIFIED but never pass;
- nothing in CI or the default pytest run picks the live gate up.

Each pin is chosen so that reverting the code it names fails it.
"""
from __future__ import annotations

import ast
import fnmatch
import importlib.util
import json
import os
import shutil
import subprocess
import sys
import tomllib
from pathlib import Path

import pytest

from tests._platform import IS_WINDOWS

REPO = Path(__file__).resolve().parents[2]
LIB = REPO / "tests" / "e2e" / "lib"
HOOK_DIR = REPO / "conexus" / "hooks" / "scripts"
CHECKPOINT = HOOK_DIR / "compact_checkpoint.py"
GATE_SH = REPO / "tests" / "e2e" / "compact-outcome-gate.sh"
SID = "11111111-2222-3333-4444-555555555555"

#: The stub is a shebang script and the scratch plugin's hooks are POSIX commands: the tests that run them
#: skip on native Windows. The rest of the module is plain Python and runs everywhere.
posix_only = pytest.mark.skipif(IS_WINDOWS, reason="the gate is POSIX-only (shebang stub nx, live claude)")


def _load(name: str):
    spec = importlib.util.spec_from_file_location(name, LIB / f"{name}.py")
    mod = importlib.util.module_from_spec(spec)
    sys.modules[name] = mod
    spec.loader.exec_module(mod)
    return mod


co = _load("compact_outcome")
overlay = _load("compact_outcome_fix_overlay")


@pytest.fixture(autouse=True)
def _no_claude(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    """Nothing in this module may start a real `claude`: it costs money and the operator runs the gate by hand."""
    def refuse(*_a, **_k):
        raise AssertionError("the harness unit tests start no claude")

    monkeypatch.setattr(co, "Session", refuse)
    monkeypatch.setenv("NX_GATE_CLAUDE", str(tmp_path / "no-such-claude"))


def _boundary(trigger: str = "auto") -> dict:
    return {"type": "system", "subtype": "compact_boundary", "compact_metadata": {"trigger": trigger, "pre_tokens": 80000}}


def _call(name: str, **tool_input) -> dict:
    return {"type": "assistant", "message": {"content": [{"type": "tool_use", "name": name, "input": tool_input}]}}


def _result(**kw) -> dict:
    return {"type": "result", "is_error": False, "stop_reason": "end_turn", **kw}


class TestJudge:
    def test_no_automatic_compaction_is_noise_not_a_pass_or_a_fail(self) -> None:
        assert co.judge([_call("Bash", command="git log alpha"), _result()])["verdict"] == "NOISE"
        assert co.judge([_boundary("manual"), _call("Bash", command="git log alpha"), _result()])["verdict"] == "NOISE"

    def test_one_item_and_end_fails(self) -> None:
        v = co.judge([_boundary(), _call("Edit", file_path="/r/app.py", new_string="def div(a, b):"), _result()])
        assert (v["verdict"], v["acted"], v["score"]) == ("FAIL", ["todo2"], 0.2)

    def test_all_items_pass_and_calls_before_the_boundary_do_not_count(self) -> None:
        before = [_call("Bash", command="git merge alpha beta README Usage")]
        after = [_boundary(), _call("Edit", new_string="def div(a, b)"), _call("Edit", new_string="## Usage"),
                 _call("Bash", command="git merge alpha"), _call("Bash", command="git merge beta"),
                 _call("TaskOutput", task_id="b1"), _result()]
        v = co.judge(before + after)
        assert (v["verdict"], v["score"]) == ("PASS", 1.0)
        assert co.judge(before + [_boundary(), _result()])["acted"] == []

    def test_the_last_automatic_boundary_opens_the_window_and_a_refusal_is_noise(self) -> None:
        events = [_boundary(), _call("Bash", command="git merge alpha"), _result(), _boundary(), _call("Bash", command="div"), _result()]
        assert co.judge(events)["acted"] == ["todo2"]
        assert co.judge([_boundary(), {"type": "result", "stop_reason": "refusal", "is_error": True}])["verdict"] == "NOISE"

    def test_min_score_is_a_parameter(self) -> None:
        events = [_boundary(), _call("Edit", new_string="def div(a, b)"), _call("Bash", command="git log alpha"), _result()]
        assert co.judge(events, 0.4)["verdict"] == "PASS" and co.judge(events, 0.8)["verdict"] == "FAIL"


def _sibling_closure(script: Path) -> set[str]:
    """``script`` and every ``_name`` sibling module it imports (anywhere in the file), transitively, as file names."""
    seen: set[str] = set()
    todo = [script]
    while todo:
        path = todo.pop()
        if path.name in seen:
            continue
        seen.add(path.name)
        for node in ast.walk(ast.parse(path.read_text(encoding="utf-8"))):
            names = [a.name for a in node.names] if isinstance(node, ast.Import) else \
                [node.module] if isinstance(node, ast.ImportFrom) and node.module and not node.level else []
            todo += [HOOK_DIR / f"{n}.py" for n in names if n.startswith("_") and (HOOK_DIR / f"{n}.py").is_file()]
    return seen


class TestPlugin:
    def test_scratch_plugin_wires_only_the_compaction_hooks(self, tmp_path: Path) -> None:
        wired = co.build_plugin(str(tmp_path / "p"))
        assert wired == ["PostCompact:manual|auto", "PreCompact:manual|auto", "SessionStart:compact"]
        text = (tmp_path / "p" / "hooks" / "hooks.json").read_text()
        for stray in ("nx_launch", "upgrade-auto", "self-gc", "version_lockstep", "mailbox_drain", "auto-approve",
                      "stop_wake_guard", "identity_bind", "nx-hook"):
            assert stray not in text

    def test_fix_arm_adds_the_overlay_beside_the_real_hooks(self, tmp_path: Path) -> None:
        wired = co.build_plugin(str(tmp_path / "p"), overlay=True)
        assert wired.count("PreCompact:manual|auto") == 2 and wired.count("SessionStart:compact") == 2
        assert (tmp_path / "p" / "hooks" / "scripts" / co.OVERLAY_SCRIPT).is_file()
        assert (tmp_path / "p" / "hooks" / "scripts" / "compact_checkpoint.py").read_bytes() == CHECKPOINT.read_bytes()

    def test_the_copied_scripts_are_the_hooks_whole_import_closure(self, tmp_path: Path) -> None:
        """compact_checkpoint.py imports plugin siblings inside its guard (an import failure exits 0 and logs, so a
        missing one degrades the checkpoint silently): the copy list must be the closure, not a list kept by hand."""
        closure = _sibling_closure(CHECKPOINT)
        assert {"_session_facts.py", "_identity_lookup.py", "_cli_lookup.py"} <= closure   # the scan sees today's imports
        assert set(co.SCRIPT_FILES) == closure
        co.build_plugin(str(tmp_path / "p"))
        for name in closure:
            assert (tmp_path / "p" / "hooks" / "scripts" / name).read_bytes() == (HOOK_DIR / name).read_bytes()

    def test_the_scratch_plugin_runs_nothing_but_the_hook_scripts_and_carries_no_mcp_server(self, tmp_path: Path) -> None:
        """What could bind an identity or reach an engine: a SessionStart bind hook, an MCP server's lease, a stray hook."""
        co.build_plugin(str(tmp_path / "p"), overlay=True)
        plugin = tmp_path / "p"
        hooks = json.loads((plugin / "hooks" / "hooks.json").read_text())["hooks"]
        assert sorted(hooks) == ["PostCompact", "PreCompact", "SessionStart"]
        for groups in hooks.values():
            for group in groups:
                for hook in group["hooks"]:
                    scripts = {Path(a).name for a in [hook["command"], *hook.get("args", [])] if a.endswith(".py")}
                    assert scripts and scripts <= {"compact_checkpoint.py", co.OVERLAY_SCRIPT}, hook
        assert not any(p.name in (".mcp.json", "mcp.json") for p in plugin.rglob("*"))
        assert "mcpServers" not in (plugin / ".claude-plugin" / "plugin.json").read_text()
        argv = co.claude_argv("claude", str(plugin), "haiku", 1.0, "sid")
        assert "--strict-mcp-config" in argv and argv[argv.index("--setting-sources") + 1] == ""


# --------------------------------------------------------------------------------------
# The stub nx against the real hook
# --------------------------------------------------------------------------------------
def nx_call_prefixes(source: str) -> set[tuple[str, ...]]:
    """Every nx call a hook source can make, as its leading literal words (``memory get``, ``identity work list``).

    Two spellings reach nx in compact_checkpoint.py: ``_run_nx([...])`` and ``subprocess.run([*base, ...])`` with
    ``base = _nx_command()``. The option words and variable arguments after the subcommand are not part of the prefix."""
    out: set[tuple[str, ...]] = set()

    def words(items: list[ast.expr]) -> tuple[str, ...]:
        prefix: list[str] = []
        for item in items:
            if not (isinstance(item, ast.Constant) and isinstance(item.value, str)) or item.value.startswith("-"):
                break
            prefix.append(item.value)
        return tuple(prefix)

    for node in ast.walk(ast.parse(source)):
        if not isinstance(node, ast.Call) or not node.args or not isinstance(node.args[0], ast.List):
            continue
        items = node.args[0].elts
        if isinstance(node.func, ast.Name) and node.func.id == "_run_nx":
            out.add(words(items))
        elif isinstance(node.func, ast.Attribute) and node.func.attr == "run" and items \
                and isinstance(items[0], ast.Starred) and isinstance(items[0].value, ast.Name) and items[0].value.id == "base":
            out.add(words(items[1:]))
    out.discard(())  # _run_nx's own ``subprocess.run([*base, *args])``: the dispatcher, whose callers are the calls
    return out


def unanswered(prefixes: set[tuple[str, ...]], tmp_path: Path, mode: str = "full") -> list[tuple[str, ...]]:
    """The prefixes the stub does not answer (exit non-zero, or logged as an unknown call)."""
    stub, board, log = tmp_path / "stub_nx.py", tmp_path / "board.md", tmp_path / "calls.jsonl"
    stub.write_text(co.STUB_NX, encoding="utf-8")
    board.write_text(co.board_text("sid-1"), encoding="utf-8")
    env = {**os.environ, "NX_STUB_MODE": mode, "NX_STUB_BOARD": str(board), "NX_STUB_LOG": str(log)}
    ordered = sorted(prefixes)
    bad: list[tuple[str, ...]] = []
    for prefix in ordered:
        done = subprocess.run([sys.executable, str(stub), *prefix, "x"], env=env, capture_output=True, text=True,
                              timeout=30, cwd=str(tmp_path))
        if done.returncode != 0:
            bad.append(prefix)
    logged = [json.loads(line) for line in log.read_text(encoding="utf-8").splitlines()]
    assert len(logged) == len(ordered), "the stub logs every call it is given"
    bad += [p for p, row in zip(ordered, logged) if not row["known"] and p not in bad]
    return sorted(bad)


class TestStubAnswersEveryHookCall:
    KNOWN = {("memory", "search"), ("memory", "get"), ("memory", "list"), ("command-context", "continuation"),
             ("identity", "work", "list")}

    def test_the_extractor_finds_the_calls_the_hook_makes_today(self) -> None:
        # a non-vacuity pin for the scan itself: if it finds nothing, "the stub answers all of it" proves nothing
        assert nx_call_prefixes(CHECKPOINT.read_text(encoding="utf-8")) >= self.KNOWN

    @posix_only
    @pytest.mark.parametrize("mode", ["full", "empty"])
    def test_the_stub_answers_every_nx_call_the_current_hook_source_can_make(self, tmp_path: Path, mode: str) -> None:
        prefixes = nx_call_prefixes(CHECKPOINT.read_text(encoding="utf-8"))
        assert unanswered(prefixes, tmp_path, mode) == []

    @posix_only
    def test_a_new_hook_call_the_stub_does_not_answer_fails(self, tmp_path: Path) -> None:
        source = CHECKPOINT.read_text(encoding="utf-8") + (
            "\n\ndef _new_call(cwd):\n    return _run_nx(['board', 'sync', '--all'], cwd, 1.0)\n"
            "\n\ndef _new_direct(base, cwd):\n    return subprocess.run([*base, 'identity', 'bind'], cwd=cwd)\n")
        prefixes = nx_call_prefixes(source)
        assert {("board", "sync"), ("identity", "bind")} <= prefixes
        assert unanswered(prefixes, tmp_path) == [("board", "sync"), ("identity", "bind")]

    def test_only_the_two_known_functions_start_nx_and_no_sibling_does(self) -> None:
        """A third caller of ``_nx_command`` is a call spelling the extractor above may not see: it must be read."""
        tree = ast.parse(CHECKPOINT.read_text(encoding="utf-8"))
        callers = {fn.name for fn in ast.walk(tree) if isinstance(fn, ast.FunctionDef)
                   and any(isinstance(c, ast.Call) and isinstance(c.func, ast.Name) and c.func.id == "_nx_command"
                           for c in ast.walk(fn))}
        assert callers == {"_run_nx", "_running_work_section"}
        for name in sorted(_sibling_closure(CHECKPOINT) - {CHECKPOINT.name}):
            assert 'resolve("nx")' not in (HOOK_DIR / name).read_text(encoding="utf-8"), name


def _provisioned(tmp_path: Path, arm: str = "real") -> tuple[dict, Path, Path]:
    """The live run's scratch layout and child env, with a fake Claude config dir naming the session."""
    iso, repo = tmp_path / "iso", tmp_path / "repo"
    co.seed_repo(str(repo))
    co.provision_iso(str(iso), SID)
    claude = tmp_path / "claude"
    (claude / "sessions").mkdir(parents=True)
    (claude / "sessions" / "4242.json").write_text(json.dumps({"pid": 4242, "sessionId": SID, "name": "demo_session"}))
    env = co.child_env(str(iso), arm, dict(os.environ))
    env["CLAUDE_CONFIG_DIR"] = str(claude)
    return env, iso, repo


def _run_hook(mode: str, env: dict, repo: Path, extra: dict | None = None) -> subprocess.CompletedProcess:
    payload = {"session_id": SID, "cwd": str(repo), "trigger": "auto", "hook_event_name": "PreCompact", **(extra or {})}
    return subprocess.run([sys.executable, str(CHECKPOINT), mode], input=json.dumps(payload), env=env,
                          capture_output=True, text=True, timeout=90)


def _stub_calls(iso: Path) -> list[dict]:
    return [json.loads(line) for line in (iso / "stub" / "calls.jsonl").read_text().splitlines()]


@posix_only
class TestStubDrivesTheRealHook:
    def test_full_stub_puts_the_board_the_checks_and_the_running_work_in_the_checkpoint(self, tmp_path: Path) -> None:
        env, iso, repo = _provisioned(tmp_path)
        assert _run_hook("pre", env, repo).returncode == 0
        ck = (iso / "nexus-config" / "compact-checkpoints" / f"{SID}.md").read_text()
        assert "Close out the demo repo" in ck and "branch: main" in ck
        assert "## Board checked against the facts" in ck and "## Running for this session" in ck
        assert "## Background work this identity still has running" in ck and "watcher.sh" in ck
        out = _run_hook("inject", env, repo, {"hook_event_name": "SessionStart", "source": "compact"})
        assert "Close out the demo repo" in json.loads(out.stdout)["hookSpecificOutput"]["additionalContext"]

    def test_the_board_is_found_by_its_identity_line_first_and_every_call_was_answered(self, tmp_path: Path) -> None:
        env, iso, repo = _provisioned(tmp_path)
        _run_hook("pre", env, repo)
        calls = _stub_calls(iso)
        argvs = [c["args"] for c in calls]
        assert all(c["known"] for c in calls)
        assert argvs[0][:3] == ["memory", "search", f"identity: {co.IDENTITY}"], argvs
        for want in (["memory", "get"], ["command-context", "continuation"], ["identity", "work", "list"]):
            assert any(a[:len(want)] == want for a in argvs), (want, argvs)

    def test_empty_stub_is_the_planted_bad_checkpoint(self, tmp_path: Path) -> None:
        env, iso, repo = _provisioned(tmp_path, "bad")
        assert _run_hook("pre", env, repo).returncode == 0
        ck = (iso / "nexus-config" / "compact-checkpoints" / f"{SID}.md").read_text()
        assert "Close out the demo repo" not in ck and "alpha" not in ck and "Background work" not in ck
        assert all(c["known"] for c in _stub_calls(iso))

    @pytest.mark.skipif(shutil.which("uv") is None, reason="needs uv, which the wired hook command runs through")
    def test_the_wired_hook_commands_run_end_to_end_under_the_isolated_env(self, tmp_path: Path) -> None:
        """hooks.json runs the script through `uv tool run python`: the scratch plugin's own commands, with the scratch
        UV directories and no network, must write the checkpoint and inject it."""
        env, _iso, repo = _provisioned(tmp_path)
        plugin = tmp_path / "plugin"
        co.build_plugin(str(plugin))
        hooks = json.loads((plugin / "hooks" / "hooks.json").read_text())["hooks"]

        def run(event: str, extra: dict) -> str:
            hook = hooks[event][0]["hooks"][0]
            argv = [hook["command"], *(a.replace("${CLAUDE_PLUGIN_ROOT}", str(plugin)) for a in hook["args"])]
            payload = {"session_id": SID, "cwd": str(repo), "trigger": "auto", "hook_event_name": event, **extra}
            done = subprocess.run(argv, input=json.dumps(payload), env=env, capture_output=True, text=True, timeout=120)
            assert done.returncode == 0, done.stderr
            return done.stdout

        assert run("PreCompact", {}) == ""
        out = run("SessionStart", {"source": "compact"})
        assert "Close out the demo repo" in json.loads(out)["hookSpecificOutput"]["additionalContext"]


class TestIsolation:
    #: The parent session's identity and session markers, written out so a shortened list in the code fails.
    PARENT = ("NX_IDENTITY_TOKEN", "NX_SEAT", "NX_SESSION_ID", "NX_T1_SESSION_ID", "NX_T1_SESSION", "CLAUDE_CODE_SESSION_ID",
              "CLAUDE_CODE_CHILD_SESSION", "CLAUDE_CODE_ENTRYPOINT", "CLAUDECODE", "CLAUDE_CODE_MESSAGING_SOCKET",
              "CLAUDE_CODE_MESSAGING_TOKEN", "TMUX", "TMUX_PANE")

    def test_the_parent_sessions_markers_never_reach_the_child(self, tmp_path: Path) -> None:
        base = {"PATH": os.environ.get("PATH", ""), "HOME": "/home/x", **{name: "leaked" for name in self.PARENT},
                "NX_SERVICE_HOST": "prod", "NX_SERVICE_PORT": "1", "NX_MINT_TOKEN": "t", "NX_CONFIG_DIR": "/prod",
                "NX_SERVICE_URL": "https://prod.example", "NX_SERVICE_TOKEN": "prod-token",
                "NX_IDENTITY_BIND": "1", "NX_IDENTITY_LEASE": "1", "NEXUS_CONFIG_DIR": "/prod"}
        env = co.child_env(str(tmp_path / "iso"), "real", base)
        assert not [name for name in self.PARENT if name in env]
        assert not [name for name in ("NX_SERVICE_HOST", "NX_SERVICE_PORT", "NX_MINT_TOKEN", "NX_CONFIG_DIR") if name in env]
        assert env["NX_IDENTITY_BIND"] == "0" and env["NX_IDENTITY_LEASE"] == "0"
        assert env["NX_SERVICE_URL"] == "http://127.0.0.1:9" and env["NX_SERVICE_TOKEN"] == "compact-outcome-unused"
        assert env["NEXUS_CONFIG_DIR"] == str(tmp_path / "iso" / "nexus-config")
        assert env["HOME"] == "/home/x", "the real HOME is kept: it holds the sign-in the live run needs"
        assert "leaked" not in json.dumps(env) and "prod" not in json.dumps(env)

    @posix_only
    def test_the_only_nx_the_session_can_run_is_the_stub_and_the_config_dir_holds_no_secret(self, tmp_path: Path) -> None:
        env, iso, _repo = _provisioned(tmp_path)
        assert shutil.which("nx", path=env["PATH"]) == str(iso / "nx-tools" / "gen-1" / "bin" / "nx")
        config = iso / "nexus-config"
        files = sorted(p.relative_to(config).as_posix() for p in config.rglob("*") if p.is_file())
        assert len(files) == 1 and files[0].startswith("identity/") and files[0].endswith(".token"), files
        record = json.loads((config / files[0]).read_text())
        assert record["identity"] == co.IDENTITY and record["token"] == "" and record["expires_at"] is None
        # an identity call the stub does not answer: nothing can bind
        done = subprocess.run(["nx", "identity", "bind"], env=env, capture_output=True, text=True, timeout=30)
        assert done.returncode != 0 and [c["known"] for c in _stub_calls(iso)] == [False]


class TestGateCannotPassVacuously:
    RATES = {"real": {"pass": 5, "fail": 0, "noise": 0}, "bad": {"pass": 0, "fail": 5, "noise": 0},
             "control": {"pass": 1, "fail": 4, "noise": 0}}

    def _decide(self, rates: dict, **kw) -> tuple[int, str]:
        code, lines = co.decide(rates, **{"runs": 5, "threshold": 4, "gate_arm": "real", **kw})
        return code, "\n".join(lines)

    def test_a_clean_discriminating_run_passes(self) -> None:
        assert self._decide(self.RATES)[0] == 0

    def test_every_arm_unverified_or_noise_is_unverified_never_a_pass(self) -> None:
        rates = {arm: {"pass": 0, "fail": 0, "noise": 5} for arm in self.RATES}
        code, text = self._decide(rates)
        assert code == 2 and "UNVERIFIED" in text

    def test_one_noise_run_anywhere_is_unverified_even_when_the_gate_arm_passed(self) -> None:
        assert self._decide({**self.RATES, "control": {"pass": 0, "fail": 4, "noise": 1}})[0] == 2

    def test_a_failing_gate_arm_fails(self) -> None:
        assert self._decide({**self.RATES, "real": {"pass": 3, "fail": 2, "noise": 0}})[0] == 1

    def test_a_control_arm_that_reaches_the_threshold_fails_non_vacuity(self) -> None:
        for arm in ("bad", "control"):
            code, text = self._decide({**self.RATES, arm: {"pass": 4, "fail": 1, "noise": 0}})
            assert code == 1 and "NON-VACUITY FAILED" in text and arm in text

    def test_a_matrix_without_the_gate_arm_or_its_control_arms_is_a_usage_error_before_any_run(self, tmp_path: Path) -> None:
        for arms in ("control", "real", "real,bad", "real,control", "bad,control"):
            argv = ["matrix", "--runs", "2", "--threshold", "1", "--arms", arms, "--out", str(tmp_path / "o")]
            assert co.main(argv) == 64, arms
        assert co.validate_matrix(runs=2, threshold=1, arms=["real", "bad", "control"], gate_arm="real") is None
        assert co.validate_matrix(runs=2, threshold=1, arms=["fix", "bad", "control"], gate_arm="fix") is None

    @pytest.mark.parametrize("runs,threshold", [(0, 0), (0, 4), (3, 0), (3, 4), (-1, 1)])
    def test_a_threshold_that_zero_runs_or_zero_passes_would_meet_is_a_usage_error(self, tmp_path: Path, runs: int, threshold: int) -> None:
        argv = ["matrix", "--runs", str(runs), "--threshold", str(threshold), "--out", str(tmp_path / "o")]
        assert co.main(argv) == 64

    def test_hooks_that_did_not_run_make_the_run_noise(self) -> None:
        good = {"hook_responses": 1, "names_branches": True, "has_directive": False}
        assert co.hook_evidence_problem("real", good, True, []) is None
        assert "no checkpoint" in co.hook_evidence_problem("real", good, False, [])
        assert "injected nothing" in co.hook_evidence_problem("real", {**good, "hook_responses": 0}, True, [])
        assert "does not carry the board" in co.hook_evidence_problem("real", {**good, "names_branches": False}, True, [])
        assert "planted-bad" in co.hook_evidence_problem("bad", good, True, [])
        assert co.hook_evidence_problem("bad", {**good, "names_branches": False}, True, []) is None
        assert "directive" in co.hook_evidence_problem("fix", good, True, [])
        assert co.hook_evidence_problem("fix", {**good, "has_directive": True}, True, []) is None

    def test_the_control_arm_must_see_no_hook_output_and_an_unanswered_nx_call_is_noise(self) -> None:
        assert co.hook_evidence_problem("control", {"hook_responses": 0}, False, []) is None
        assert "control" in co.hook_evidence_problem("control", {"hook_responses": 1}, False, [])
        problem = co.hook_evidence_problem("real", {"hook_responses": 1, "names_branches": True}, True, [["board", "sync"]])
        assert "board sync" in problem and "stub" in problem

    def test_run_one_without_claude_is_unverified(self, tmp_path: Path) -> None:
        assert co.run_one("real", str(tmp_path / "out"), "haiku", 1.0)["verdict"] == "UNVERIFIED"


class TestNothingRunsTheGateAutomatically:
    NAMES = ("compact-outcome-gate", "compact_outcome")
    OWN = {"tests/e2e/compact-outcome-gate.sh", "tests/e2e/lib/compact_outcome.py", "tests/e2e/lib/compact_outcome_fix_overlay.py",
           "tests/hooks/test_compact_outcome_harness.py"}

    def test_no_workflow_script_or_other_test_names_the_gate(self) -> None:
        scanned, hits = 0, []
        for root in (REPO / ".github", REPO / "tests", REPO / "scripts", REPO / "conexus" / "hooks"):
            for path in root.rglob("*"):
                if path.suffix not in (".yml", ".yaml", ".sh", ".py", ".toml", ".json", ".cmd", ".ps1") or not path.is_file():
                    continue
                rel = path.relative_to(REPO).as_posix()
                if "__pycache__" in rel or rel in self.OWN:
                    continue
                scanned += 1
                if any(n in path.read_text(encoding="utf-8", errors="replace") for n in self.NAMES):
                    hits.append(rel)
        assert scanned > 200, f"the scan examined {scanned} files: a vacuous scan proves nothing"
        assert hits == []

    def test_the_gate_files_are_not_collected_by_pytest(self) -> None:
        cfg = tomllib.loads((REPO / "pyproject.toml").read_text(encoding="utf-8"))["tool"]["pytest"]["ini_options"]
        patterns = cfg.get("python_files", ["test_*.py", "*_test.py"])
        for name in ("compact_outcome.py", "compact_outcome_fix_overlay.py", "compact-outcome-gate.sh"):
            assert not any(fnmatch.fnmatch(name, p) for p in patterns), name

    def test_headers_say_how_to_run_what_it_costs_and_what_each_verdict_means(self) -> None:
        for text in (co.__doc__ or "", GATE_SH.read_text(encoding="utf-8")):
            for word in ("--max-budget-usd", "PASS", "FAIL", "NOISE", "UNVERIFIED", "by hand", "wall"):
                assert word.lower() in text.lower(), word


class TestOverlay:
    def _repo(self, tmp_path: Path) -> Path:
        repo = tmp_path / "repo"
        co.seed_repo(str(repo))
        return repo

    def test_capture_finds_branches_todo_items_and_a_running_background_task(self, tmp_path: Path) -> None:
        repo = self._repo(tmp_path)
        tr = tmp_path / "t.jsonl"
        tr.write_text(json.dumps({"type": "user", "message": {"content": [{"type": "tool_result", "content":
                       "Command running in background with ID: bx1. Output is being written to: /tmp/x/bx1.output."}]}}) + "\n")
        work = overlay.capture({"cwd": str(repo), "transcript_path": str(tr)})
        assert work["branches"] == ["alpha", "beta"] and len(work["todo"]) == 3
        assert work["tasks"] == [{"id": "bx1", "output": "/tmp/x/bx1.output"}]
        text = overlay.render(work)
        assert "bx1" in text and "alpha, beta" in text and text.rstrip().endswith(overlay.DIRECTIVE)

    def test_a_finished_task_and_an_empty_repo_state_render_nothing(self, tmp_path: Path) -> None:
        tr = tmp_path / "t.jsonl"
        tr.write_text(
            json.dumps({"type": "user", "message": {"content": "Command running in background with ID: b2. Output is being written to: /tmp/b2.output."}}) + "\n"
            + json.dumps({"type": "user", "message": {"content": "<task-notification>b2 completed</task-notification>"}}) + "\n")
        assert overlay.background_tasks(str(tr)) == []
        assert overlay.render({"tasks": [], "branches": [], "todo": [], "head_branch": "main"}) == ""

    @posix_only
    def test_the_overlay_files_sit_beside_the_real_checkpoint_without_either_touching_the_other(self, tmp_path: Path) -> None:
        """The real hook's prune takes only *.md, *.injected and .tmp-* files from the checkpoint directory; the overlay's
        <session>.openwork.json is none of them, and the overlay's inject deletes only its own file."""
        env, iso, repo = _provisioned(tmp_path, "fix")
        directory = iso / "nexus-config" / "compact-checkpoints"
        script = LIB / "compact_outcome_fix_overlay.py"
        payload = json.dumps({"session_id": SID, "cwd": str(repo), "transcript_path": ""})

        def overlay_run(verb: str) -> subprocess.CompletedProcess:
            return subprocess.run([sys.executable, str(script), verb], input=payload, env=env, capture_output=True, text=True, timeout=30)

        overlay_run("pre")
        assert (directory / f"{SID}.openwork.json").is_file()
        assert _run_hook("pre", env, repo).returncode == 0
        assert (directory / f"{SID}.openwork.json").is_file() and (directory / f"{SID}.md").is_file()
        text = json.loads(overlay_run("inject").stdout)["hookSpecificOutput"]["additionalContext"]
        assert "alpha, beta" in text and text.rstrip().endswith(overlay.DIRECTIVE)
        assert (directory / f"{SID}.md").is_file() and not (directory / f"{SID}.openwork.json").exists()
