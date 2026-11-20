# SPDX-License-Identifier: AGPL-3.0-or-later
"""The compaction-outcome gate's own machinery (tests/e2e/lib/compact_outcome*.py), without `claude`. The judge, the statistics and the
hook-evidence rules are pinned in test_compact_outcome_judge.py.

The cases that need a shebang script, a symlink or bash are in test_compact_outcome_harness_posix.py, which the Windows test
set leaves out; everything here is plain Python and runs on native Windows too.

The live part (a real session through a real automatic compaction) is tests/e2e/compact-outcome-gate.sh and
is run by hand only: it spends model calls. Nothing here starts `claude` (an autouse fixture makes the
session class raise and points NX_GATE_CLAUDE at nothing), so this module is safe in the default suite.

What is pinned:
- the decision rule over per-arm counts (threshold, one-sided Fisher separation, guard scenarios, census calibration) and the turn script;
- the scratch plugin's hook wiring and file list, and the overlay's directive-only arms;
- the stub `nx` against the REAL compact_checkpoint.py: every nx call the hook can make (found by reading its
  source, never from a list kept here) must be answered by the stub, and every call site must be a spelling the scan can read;
- the child environment: the parent's NX_, NEXUS_ and CLAUDE_CODE_ variables and session markers never reach the run, the
  scratch plugin can bind no identity and reach no engine, and bd and gh are failing stubs;
- the gate's non-vacuity: a run or a matrix with no verdict, no compaction, hooks that did not run, or no control arms can
  fail or be UNVERIFIED but never pass, and a harness crash is UNVERIFIED, never a FAIL;
- nothing in CI or the default pytest run picks the live gate up.

Each pin is chosen so that reverting the code it names fails it.
"""
from __future__ import annotations

import ast
import fnmatch
import importlib.util
import json
import os
import re
import subprocess
import sys
import tomllib
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parents[2]
LIB = REPO / "tests" / "e2e" / "lib"
HOOK_DIR = REPO / "conexus" / "hooks" / "scripts"
CHECKPOINT = HOOK_DIR / "compact_checkpoint.py"
GATE_SH = REPO / "tests" / "e2e" / "compact-outcome-gate.sh"
SID = "11111111-2222-3333-4444-555555555555"


def _load(name: str):
    spec = importlib.util.spec_from_file_location(name, LIB / f"{name}.py")
    mod = importlib.util.module_from_spec(spec)
    sys.modules[name] = mod
    spec.loader.exec_module(mod)
    return mod


co = _load("compact_outcome")           # puts LIB on sys.path and imports the three siblings below
jd = sys.modules["compact_outcome_judge"]
sc = sys.modules["compact_outcome_scenario"]
st = sys.modules["compact_outcome_stats"]
overlay = _load("compact_outcome_fix_overlay")
REAL_SESSION = co.Session           # the autouse fixture replaces co.Session; the marker test needs the real class (it starts no process)
OPEN, BOUNDED, MEANWHILE, WAITING = (sc.SCENARIOS[n] for n in ("open", "bounded", "meanwhile", "waiting"))
NOTHING = {"todo2": False, "todo3": False, "alpha": False, "beta": False, "indep": False}


@pytest.fixture(autouse=True)
def _no_claude(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    """Nothing in this module may start a real `claude`: it costs money and the operator runs the gate by hand."""
    def refuse(*_a, **_k):
        raise AssertionError("the harness unit tests start no claude")

    monkeypatch.setattr(co, "Session", refuse)
    monkeypatch.setenv("NX_GATE_CLAUDE", str(tmp_path / "no-such-claude"))


class TestGateCannotPassVacuously:
    WORK = {"real": {"pass": 15, "fail": 0, "noise": 0, "resumed": 15}, "bad": {"pass": 1, "fail": 14, "noise": 0},
            "control": {"pass": 3, "fail": 12, "noise": 0}}

    def _decide(self, rates: dict, scenario: str = "open", **kw) -> tuple[int, str]:
        code, lines = st.decide(rates, **{"runs": 15, "threshold": 11, "gate_arm": "real", "scenario": scenario, **kw})
        return code, "\n".join(lines)

    def test_a_clean_discriminating_run_passes(self) -> None:
        code, text = self._decide(self.WORK)
        assert code == 0 and "Fisher" in text and "separable" in text

    def test_every_arm_unverified_or_noise_is_unverified_never_a_pass(self) -> None:
        code, text = self._decide({arm: {"pass": 0, "fail": 0, "noise": 15} for arm in self.WORK})
        assert code == 2 and "UNVERIFIED" in text

    def test_one_noise_run_anywhere_is_unverified_even_when_the_gate_arm_passed(self) -> None:
        assert self._decide({**self.WORK, "control": {"pass": 0, "fail": 14, "noise": 1}})[0] == 2

    def test_a_failing_gate_arm_fails(self) -> None:
        assert self._decide({**self.WORK, "real": {"pass": 10, "fail": 5, "noise": 0}})[0] == 1

    def test_the_threshold_met_but_not_separable_from_control_fails(self) -> None:
        """11 of 15 against a control at 10 of 15: the threshold is met and nothing has been shown."""
        code, text = self._decide({**self.WORK, "real": {"pass": 11, "fail": 4, "noise": 0}, "control": {"pass": 10, "fail": 5, "noise": 0}})
        assert code == 1 and "NOT SEPARABLE" in text
        code, text = self._decide({**self.WORK, "real": {"pass": 12, "fail": 3, "noise": 0}, "bad": {"pass": 10, "fail": 5, "noise": 0}})
        assert code == 1 and "real vs bad" in text and "NOT SEPARABLE" in text

    def test_a_control_arm_that_reaches_the_threshold_fails_non_vacuity(self) -> None:
        for arm in ("bad", "control"):
            code, text = self._decide({**self.WORK, arm: {"pass": 12, "fail": 3, "noise": 0}})
            assert code == 1 and "NON-VACUITY FAILED" in text and arm in text

    def test_the_real_arm_is_calibrated_against_the_census_and_the_baseline_is_reported(self) -> None:
        rates = {"real": {"pass": 1, "fail": 14, "noise": 0, "resumed": 1}, "control": {"pass": 0, "fail": 15, "noise": 0},
                 "nocompact": {"pass": 4, "fail": 11, "noise": 0}}
        code, text = self._decide(rates, "meanwhile")
        assert code == 1 and "scenario harsher than observed use" in text and "nocompact baseline" in text and "4/15" in text

    def test_a_guard_scenario_passes_when_every_hook_arm_holds_and_is_not_worse_than_control(self) -> None:
        rates = {"control": {"pass": 14, "fail": 1, "noise": 0}, "directive": {"pass": 15, "fail": 0, "noise": 0},
                 "real": {"pass": 13, "fail": 2, "noise": 0}}
        assert self._decide(rates, "bounded")[0] == 0

    def test_a_directive_that_overrides_a_bounded_user_fails_the_guard(self) -> None:
        rates = {"control": {"pass": 14, "fail": 1, "noise": 0}, "directive": {"pass": 4, "fail": 11, "noise": 0}}
        code, text = self._decide(rates, "bounded")
        assert code == 1 and "GUARD FAILED" in text and "directive" in text
        # at the threshold but significantly worse than a perfect control is also a failure
        code, text = self._decide({"control": {"pass": 15, "fail": 0, "noise": 0}, "directive": {"pass": 11, "fail": 4, "noise": 0}}, "waiting")
        assert code == 1 and "does worse than control" in text

    def test_a_matrix_without_the_gate_arm_or_its_control_arms_is_a_usage_error_before_any_run(self, tmp_path: Path) -> None:
        for scenario, arms in (("open", "control"), ("open", "real"), ("open", "real,bad"), ("open", "real,control"), ("open", "bad,control"),
                               ("meanwhile", "real"), ("meanwhile", "control,directive"), ("bounded", "directive"), ("bounded", "control"),
                               ("waiting", "control,nocompact")):
            argv = ["matrix", "--runs", "2", "--threshold", "1", "--scenarios", scenario, "--arms", arms, "--out", str(tmp_path / "o")]
            assert co.main(argv) == 64, (scenario, arms)
        v = co.st.validate_matrix
        assert v(2, 1, ["real", "bad", "control"], "real", "open", co.ARMS, co.SCENARIO_ARMS) is None
        assert v(2, 1, ["real", "control"], "real", "meanwhile", co.ARMS, co.SCENARIO_ARMS) is None
        assert v(2, 1, ["control", "directive"], "real", "bounded", co.ARMS, co.SCENARIO_ARMS) is None
        assert "do not run" in v(2, 1, ["real", "control", "fix"], "real", "meanwhile", co.ARMS, co.SCENARIO_ARMS)

    @pytest.mark.parametrize("runs,threshold", [(0, 0), (0, 4), (3, 0), (3, 4), (-1, 1)])
    def test_a_threshold_that_zero_runs_or_zero_passes_would_meet_is_a_usage_error(self, tmp_path: Path, runs: int, threshold: int) -> None:
        argv = ["matrix", "--runs", str(runs), "--threshold", str(threshold), "--out", str(tmp_path / "o")]
        assert co.main(argv) == 64

    def test_the_defaults_are_fifteen_runs_and_eleven_passes(self) -> None:
        assert (co.DEFAULT_RUNS, co.DEFAULT_THRESHOLD) == (15, 11)
        p = subprocess.run([sys.executable, "-I", str(LIB / "compact_outcome.py"), "matrix", "--help"], capture_output=True, text=True,
                           encoding="utf-8", timeout=30)
        assert "--runs RUNS" in p.stdout and "--threshold THRESHOLD" in p.stdout

    def test_run_one_without_claude_is_unverified(self, tmp_path: Path) -> None:
        assert co.run_one("real", "open", str(tmp_path / "out"), "sonnet", 1.0)["verdict"] == "UNVERIFIED"

    def test_a_crash_in_a_pool_worker_is_unverified_exit_2_never_exit_1(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        def boom(*_a, **_k):
            raise RuntimeError("provisioning blew up")

        monkeypatch.setattr(co, "run_one", boom)
        argv = ["matrix", "--runs", "1", "--threshold", "1", "--scenarios", "open", "--arms", "real,control,bad", "--jobs", "1",
                "--noise-retries", "0", "--out", str(tmp_path / "o")]
        assert co.main(argv) == 2
        verdict = json.loads((tmp_path / "o" / "open" / "real-1" / "verdict.json").read_text(encoding="utf-8"))
        assert verdict["verdict"] == "NOISE" and "harness crash" in verdict["why"]
        monkeypatch.setattr(co, "cmd_matrix", boom)
        assert co.main(["matrix", "--out", str(tmp_path / "p")]) == 2
        assert co.main(["run-one", "--arm", "real", "--scenario", "open", "--out", str(tmp_path / "q")]) == 2

    def test_judge_needs_a_state_and_runs_no_claude(self, tmp_path: Path) -> None:
        events = tmp_path / "events.jsonl"
        run = [{"type": "harness", "subtype": "user_turn"}, {"type": "result"}, {"type": "harness", "subtype": "user_turn"},
               {"type": "system", "subtype": "compact_boundary", "compact_metadata": {"trigger": "auto"}},
               {"type": "assistant", "message": {"content": [{"type": "tool_use", "id": "a", "name": "Edit",
                                                              "input": {"file_path": "/r/app.py", "new_string": "def div(a, b)"}}]}},
               {"type": "result", "is_error": False}]
        events.write_text("\n".join(json.dumps(e) for e in run), encoding="utf-8")
        assert co.main(["judge", "--events", str(events), "--scenario", "open"]) == 64
        (tmp_path / "verdict.json").write_text(json.dumps({"state_done": {**NOTHING, "todo2": True}}), encoding="utf-8")
        assert co.main(["judge", "--events", str(events), "--scenario", "open"]) == 0


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
        text = (tmp_path / "p" / "hooks" / "hooks.json").read_text(encoding="utf-8")
        for stray in ("nx_launch", "upgrade-auto", "self-gc", "version_lockstep", "mailbox_drain", "auto-approve",
                      "stop_wake_guard", "identity_bind", "nx-hook"):
            assert stray not in text

    def test_fix_arm_adds_the_overlay_beside_the_real_hooks(self, tmp_path: Path) -> None:
        wired = co.build_plugin(str(tmp_path / "p"), overlay="fix")
        assert wired.count("PreCompact:manual|auto") == 2 and wired.count("SessionStart:compact") == 2
        assert (tmp_path / "p" / "hooks" / "scripts" / co.OVERLAY_SCRIPT).is_file()
        assert (tmp_path / "p" / "hooks" / "scripts" / "compact_checkpoint.py").read_bytes() == CHECKPOINT.read_bytes()

    @pytest.mark.parametrize("arm,verb", [("directive", "inject-directive"), ("broad", "inject-broad")])
    def test_the_directive_arms_add_one_session_start_hook_and_no_capture(self, tmp_path: Path, arm: str, verb: str) -> None:
        wired = co.build_plugin(str(tmp_path / "p"), overlay=arm)
        assert wired.count("PreCompact:manual|auto") == 1 and wired.count("SessionStart:compact") == 2
        hooks = json.loads((tmp_path / "p" / "hooks" / "hooks.json").read_text(encoding="utf-8"))["hooks"]
        added = [h for g in hooks["SessionStart"] for h in g["hooks"] if co.OVERLAY_SCRIPT in json.dumps(h)]
        assert [h["args"][1] for h in added] == [verb]
        assert not any(co.OVERLAY_SCRIPT in json.dumps(g) for g in hooks["PreCompact"] + hooks["PostCompact"])

    def test_every_arm_has_a_plugin_or_none_and_the_scenarios_name_only_known_arms(self) -> None:
        assert set(co.ARM_OVERLAY) | {"control", "nocompact"} == set(co.ARMS)
        for scenario, arms in {**co.SCENARIO_ARMS}.items():
            assert set(arms) <= set(co.ARMS) and set(co.DEFAULT_ARMS[scenario]) <= set(arms), scenario
        assert "fix" not in co.SCENARIO_ARMS["meanwhile"] and "fix" not in co.SCENARIO_ARMS["waiting"]

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
        co.build_plugin(str(tmp_path / "p"), overlay="fix")
        plugin = tmp_path / "p"
        hooks = json.loads((plugin / "hooks" / "hooks.json").read_text(encoding="utf-8"))["hooks"]
        assert sorted(hooks) == ["PostCompact", "PreCompact", "SessionStart"]
        for groups in hooks.values():
            for group in groups:
                for hook in group["hooks"]:
                    scripts = {Path(a).name for a in [hook["command"], *hook.get("args", [])] if a.endswith(".py")}
                    assert scripts and scripts <= {"compact_checkpoint.py", co.OVERLAY_SCRIPT}, hook
        assert not any(p.name in (".mcp.json", "mcp.json") for p in plugin.rglob("*"))
        assert "mcpServers" not in (plugin / ".claude-plugin" / "plugin.json").read_text(encoding="utf-8")
        argv = co.claude_argv("claude", str(plugin), "haiku", 1.0, "sid")
        assert "--strict-mcp-config" in argv and argv[argv.index("--setting-sources") + 1] == ""


# --------------------------------------------------------------------------------------
# The stub nx against the real hook
# --------------------------------------------------------------------------------------
#: A subprocess call in the hook may only be one of these spellings: the nx dispatcher's ``[*base, ...]`` or ``[git, ...]``.
OTHER_HEADS = {"git"}
SUBPROCESS_FUNCS = {"run", "Popen", "check_output", "check_call", "call", "getoutput", "getstatusoutput"}


def nx_scan(source: str) -> tuple[set[tuple[str, ...]], list[str]]:
    """Every nx call a hook source can make, as its leading literal words (``memory get``), and every call SITE whose spelling
    the scan cannot read, as violations.

    Two spellings reach nx in compact_checkpoint.py and the scan reads both: ``_run_nx([...], ...)`` (the first argument a LIST
    LITERAL) and ``subprocess.run([*base, ...])`` (a list literal whose first element is ``*base``). Anything else is a violation:
    a ``_run_nx`` whose first argument is a variable, a keyword or a built list; a subprocess call that is not a list literal
    headed ``*base`` or ``git``; ``os.system``, ``os.popen`` or an ``os.exec*``/``os.spawn*``. A violation fails the test, so a
    spelling the scan cannot see is never silently outside the stub's coverage."""
    prefixes: set[tuple[str, ...]] = set()
    violations: list[str] = []

    def words(items: list[ast.expr]) -> tuple[str, ...]:
        prefix: list[str] = []
        for item in items:
            if not (isinstance(item, ast.Constant) and isinstance(item.value, str)) or item.value.startswith("-"):
                break
            prefix.append(item.value)
        return tuple(prefix)

    for node in ast.walk(ast.parse(source)):
        if not isinstance(node, ast.Call):
            continue
        func = node.func
        first = node.args[0] if node.args else None
        where = f"line {node.lineno}"
        if isinstance(func, ast.Name) and func.id == "_run_nx":
            if isinstance(first, ast.List):
                prefixes.add(words(first.elts))
            else:
                violations.append(f"{where}: _run_nx without a list literal first argument")
        elif isinstance(func, ast.Attribute) and isinstance(func.value, ast.Name) and func.value.id == "subprocess" and func.attr in SUBPROCESS_FUNCS:
            head = first.elts[0] if isinstance(first, ast.List) and first.elts else None
            if isinstance(head, ast.Starred) and isinstance(head.value, ast.Name) and head.value.id == "base":
                prefixes.add(words(first.elts[1:]))
            elif isinstance(head, ast.Name) and head.id in OTHER_HEADS:
                continue
            else:
                violations.append(f"{where}: subprocess.{func.attr} is not a list literal headed *base or {sorted(OTHER_HEADS)}")
        elif isinstance(func, ast.Attribute) and isinstance(func.value, ast.Name) and func.value.id == "os" \
                and (func.attr in ("system", "popen") or func.attr.startswith(("exec", "spawn"))):
            violations.append(f"{where}: os.{func.attr}")
        elif isinstance(func, ast.Name) and func.id in ("Popen", "check_output"):
            violations.append(f"{where}: {func.id} imported by name")
    prefixes.discard(())  # _run_nx's own ``subprocess.run([*base, *args])``: the dispatcher, whose callers are the calls
    return prefixes, violations


def nx_call_prefixes(source: str) -> set[tuple[str, ...]]:
    prefixes, violations = nx_scan(source)
    assert violations == [], violations
    return prefixes


def unanswered(prefixes: set[tuple[str, ...]], tmp_path: Path, mode: str = "full") -> list[tuple[str, ...]]:
    """The prefixes the stub does not answer (exit non-zero, or logged as an unknown call)."""
    stub, board, log = tmp_path / "stub_nx.py", tmp_path / "board.md", tmp_path / "calls.jsonl"
    stub.write_text(co.STUB_NX, encoding="utf-8")
    board.write_text(OPEN.board("sid-1"), encoding="utf-8")
    env = {**os.environ, "NX_STUB_MODE": mode, "NX_STUB_BOARD": str(board), "NX_STUB_LOG": str(log)}
    ordered = sorted(prefixes)
    bad: list[tuple[str, ...]] = []
    for prefix in ordered:
        done = subprocess.run([sys.executable, str(stub), *prefix, "x"], env=env, capture_output=True, text=True, encoding="utf-8",
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

    def test_the_extractor_finds_the_calls_the_hook_makes_today_and_no_unreadable_site(self) -> None:
        # a non-vacuity pin for the scan itself: if it finds nothing, "the stub answers all of it" proves nothing
        prefixes, violations = nx_scan(CHECKPOINT.read_text(encoding="utf-8"))
        assert prefixes >= self.KNOWN and violations == []

    @pytest.mark.parametrize("spelling", [
        "a = ['memory', 'get']\n_run_nx(a, None, 1.0)",                                  # a list built elsewhere
        "_run_nx(args=['memory', 'get'], cwd=None, timeout=1.0)",                          # keyword form
        "_run_nx(['memory', 'get'] + extra, None, 1.0)",                                   # a built list
        "cmd = [*base, 'memory', 'get']\nsubprocess.run(cmd, capture_output=True)",        # a list in a variable
        "subprocess.run(['nx', 'memory', 'get'])",                                         # a literal nx
        "subprocess.run([base[0], 'memory', 'get'])",                                      # base by index
        "subprocess.Popen([*other, 'x'])",                                                 # another name for the base
        "os.system('nx memory get')",
        "os.execv(exe, [exe, 'memory'])",
        "from subprocess import check_output\ncheck_output(['nx'])",
    ])
    def test_a_spelling_the_scan_cannot_read_is_a_violation_not_a_silent_gap(self, spelling: str) -> None:
        _prefixes, violations = nx_scan(spelling)
        assert violations, spelling

    def test_the_two_readable_spellings_are_not_violations(self) -> None:
        prefixes, violations = nx_scan("_run_nx(['memory', 'get', x], None, 1.0)\nsubprocess.run([*base, 'identity', 'work', '--json'])\n"
                                       "subprocess.run([git, '-C', cwd, 'log'])")
        assert violations == [] and prefixes == {("memory", "get"), ("identity", "work")}

    @pytest.mark.parametrize("mode", ["full", "empty"])
    def test_the_stub_answers_every_nx_call_the_current_hook_source_can_make(self, tmp_path: Path, mode: str) -> None:
        prefixes = nx_call_prefixes(CHECKPOINT.read_text(encoding="utf-8"))
        assert unanswered(prefixes, tmp_path, mode) == []

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


class TestIsolation:
    #: The parent session's identity and session markers, written out so a shortened list in the code fails.
    PARENT = ("NX_IDENTITY_TOKEN", "NX_SEAT", "NX_SESSION_ID", "NX_T1_SESSION_ID", "NX_T1_SESSION", "CLAUDE_CODE_SESSION_ID",
              "CLAUDE_CODE_CHILD_SESSION", "CLAUDE_CODE_ENTRYPOINT", "CLAUDECODE", "CLAUDE_CODE_MESSAGING_SOCKET",
              "CLAUDE_CODE_MESSAGING_TOKEN", "TMUX", "TMUX_PANE")
    SENTINEL = "LEAK-SENTINEL-7f3a"

    def test_the_parent_sessions_markers_never_reach_the_child(self, tmp_path: Path) -> None:
        base = {"PATH": os.environ.get("PATH", ""), "HOME": "/home/x", **{name: self.SENTINEL for name in self.PARENT},
                "NX_SERVICE_HOST": self.SENTINEL, "NX_SERVICE_PORT": "1", "NX_MINT_TOKEN": self.SENTINEL, "NX_CONFIG_DIR": self.SENTINEL,
                "NX_SERVICE_URL": f"https://{self.SENTINEL}.example", "NX_SERVICE_TOKEN": self.SENTINEL,
                "NX_IDENTITY_BIND": "1", "NX_IDENTITY_LEASE": "1", "NEXUS_CONFIG_DIR": self.SENTINEL}
        env = co.child_env(str(tmp_path / "iso"), "real", base)
        assert not [name for name in self.PARENT if name in env]
        assert not [name for name in ("NX_SERVICE_HOST", "NX_SERVICE_PORT", "NX_MINT_TOKEN", "NX_CONFIG_DIR") if name in env]
        assert env["NX_IDENTITY_BIND"] == "0" and env["NX_IDENTITY_LEASE"] == "0"
        assert env["NX_SERVICE_URL"] == "http://127.0.0.1:9" and env["NX_SERVICE_TOKEN"] == "compact-outcome-unused"
        assert env["NEXUS_CONFIG_DIR"] == str(tmp_path / "iso" / "nexus-config")
        assert env["HOME"] == "/home/x", "the real HOME is kept: it holds the sign-in the live run needs"
        assert self.SENTINEL not in json.dumps(env), "no value of a dropped variable reaches the child"

    def test_whole_families_are_dropped_by_prefix_and_only_the_sign_in_survives(self, tmp_path: Path) -> None:
        """A variable nobody listed (a new NX_ switch, a CLAUDE_CODE_ flag, a NEXUS_ path) must not reach the child."""
        base = {"PATH": "/usr/bin", "HOME": "/home/x", "NX_BRAND_NEW_SWITCH": "1", "NEXUS_SOMETHING": "x", "CLAUDE_CODE_NEW_FLAG": "1",
                "CLAUDE_CODE_OAUTH_TOKEN": "tok", "CLAUDE_CODE_USE_BEDROCK": "1", "CLAUDE_CONFIG_DIR": "/home/x/.claude",
                "ANTHROPIC_API_KEY": "key", "NX_GATE_CLAUDE": "/bin/claude", "LANG": "C"}
        env = co.child_env(str(tmp_path / "iso"), "real", base)
        for gone in ("NX_BRAND_NEW_SWITCH", "NEXUS_SOMETHING", "CLAUDE_CODE_NEW_FLAG", "NX_GATE_CLAUDE"):
            assert gone not in env, gone
        for kept in ("CLAUDE_CODE_OAUTH_TOKEN", "CLAUDE_CODE_USE_BEDROCK", "CLAUDE_CONFIG_DIR", "ANTHROPIC_API_KEY", "LANG"):
            assert env[kept] == base[kept], kept
        assert env["CLAUDE_CODE_DISABLE_AUTO_MEMORY"] == "1" and env["NX_NO_TELEMETRY"] == "1"

    def test_uv_may_download_no_python(self, tmp_path: Path) -> None:
        assert co.child_env(str(tmp_path / "iso"), "real", {"UV_PYTHON_DOWNLOADS": "automatic"})["UV_PYTHON_DOWNLOADS"] == "never"


class TestNothingRunsTheGateAutomatically:
    NAMES = ("compact-outcome-gate", "compact_outcome")
    OWN = {"tests/e2e/compact-outcome-gate.sh", "tests/hooks/test_compact_outcome_judge.py", "tests/e2e/lib/compact_outcome.py", "tests/e2e/lib/compact_outcome_fix_overlay.py",
           "tests/e2e/lib/compact_outcome_judge.py", "tests/e2e/lib/compact_outcome_scenario.py", "tests/e2e/lib/compact_outcome_stats.py",
           "tests/hooks/test_compact_outcome_harness.py", "tests/hooks/test_compact_outcome_harness_posix.py"}

    #: The one workflow that names a gate module: its `--ignore` leaves the POSIX-only sibling out of the Windows test set.
    WINDOWS_WORKFLOW = ".github/workflows/windows-pg-bundle-rehearsal.yml"

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
                text = path.read_text(encoding="utf-8", errors="replace")
                if rel == self.WINDOWS_WORKFLOW:  # it must name the one module it leaves out of the Windows set
                    text = text.replace("tests/hooks/test_compact_outcome_harness_posix.py", "")
                if any(n in text for n in self.NAMES):
                    hits.append(rel)
        assert scanned > 200, f"the scan examined {scanned} files: a vacuous scan proves nothing"
        assert hits == []

    def test_every_gate_module_is_in_the_own_set(self) -> None:
        found = {p.relative_to(REPO).as_posix() for p in (REPO / "tests").rglob("*") if p.is_file() and "compact_outcome" in p.name
                 and "__pycache__" not in p.parts and p.suffix in (".py", ".sh")} | {"tests/e2e/compact-outcome-gate.sh"}
        assert found == self.OWN

    def test_the_gate_files_are_not_collected_by_pytest(self) -> None:
        cfg = tomllib.loads((REPO / "pyproject.toml").read_text(encoding="utf-8"))["tool"]["pytest"]["ini_options"]
        patterns = cfg.get("python_files", ["test_*.py", "*_test.py"])
        for name in ("compact_outcome.py", "compact_outcome_fix_overlay.py", "compact_outcome_judge.py", "compact_outcome_scenario.py",
                     "compact_outcome_stats.py", "compact-outcome-gate.sh"):
            assert not any(fnmatch.fnmatch(name, p) for p in patterns), name

    def test_headers_say_how_to_run_what_it_costs_and_what_each_verdict_means(self) -> None:
        for text in (co.__doc__ or "", GATE_SH.read_text(encoding="utf-8")):
            for word in ("--max-budget-usd", "PASS", "FAIL", "NOISE", "UNVERIFIED", "by hand", "wall", "bad", "board withheld"):
                assert word.lower() in text.lower(), word

    def test_the_cost_statement_counts_the_arms_and_gives_the_figure(self) -> None:
        """The default matrix is 10 arm-scenario cells x 15 runs = 150 sessions; the header, the wrapper and the changelog fragment
        must say so, with the dollar estimate, not the old per-run figure alone."""
        cells = sum(len(co.DEFAULT_ARMS[s]) for s in sc.DEFAULT_SCENARIOS)
        sessions = cells * co.DEFAULT_RUNS
        assert (cells, sessions) == (10, 150)
        fragment = (REPO / "changelog" / "unreleased" / "compact-outcome-gate.md").read_text(encoding="utf-8")
        for text in (co.__doc__ or "", GATE_SH.read_text(encoding="utf-8"), fragment):
            assert "150 sessions" in text and "$38" in text and "$12" in text, text[:80]


class TestOverlay:
    def _repo(self, tmp_path: Path) -> Path:
        repo = tmp_path / "repo"
        sc.seed_repo(str(repo), OPEN, sc.PLAN_SHORT)
        return repo

    def test_capture_finds_branches_todo_items_and_a_running_background_task(self, tmp_path: Path) -> None:
        repo = self._repo(tmp_path)
        tr = tmp_path / "t.jsonl"
        tr.write_text(json.dumps({"type": "user", "message": {"content": [{"type": "tool_result", "content":
                       "Command running in background with ID: bx1. Output is being written to: /tmp/x/bx1.output."}]}}) + "\n", encoding="utf-8")
        work = overlay.capture({"cwd": str(repo), "transcript_path": str(tr)})
        assert work["branches"] == ["alpha", "beta"] and len(work["todo"]) == 3
        assert work["tasks"] == [{"id": "bx1", "output": "/tmp/x/bx1.output"}]
        text = overlay.render(work)
        assert "bx1" in text and "alpha, beta" in text and text.rstrip().endswith(overlay.BROAD_DIRECTIVE)

    def test_the_two_directives_say_what_the_brief_says(self) -> None:
        assert overlay.DIRECTIVE == ("After this compaction, act on any thread or summary item that has nothing to wait on. "
                                     "If all you are doing is waiting on a running task, say what you are waiting on.")
        assert "do not stop after the item you were last asked about" in overlay.BROAD_DIRECTIVE.lower()
        assert jd.NARROW_PHRASE in overlay.DIRECTIVE and jd.BROAD_PHRASE in overlay.BROAD_DIRECTIVE
        assert jd.NARROW_PHRASE not in overlay.BROAD_DIRECTIVE and jd.BROAD_PHRASE not in overlay.DIRECTIVE

    @pytest.mark.parametrize("verb,expect", [("inject-directive", overlay.DIRECTIVE), ("inject-broad", overlay.BROAD_DIRECTIVE)])
    def test_the_directive_only_verbs_print_the_line_and_nothing_else(self, verb: str, expect: str) -> None:
        done = subprocess.run([sys.executable, str(LIB / "compact_outcome_fix_overlay.py"), verb], input=json.dumps({"session_id": SID}),
                              capture_output=True, text=True, encoding="utf-8", timeout=30)
        text = json.loads(done.stdout)["hookSpecificOutput"]["additionalContext"]
        assert text.strip() == expect
        for leak in ("alpha", "beta", "TODO", "branch", "OPEN WORK", "output:", "bx1"):
            assert leak not in text, leak

    def test_a_finished_task_and_an_empty_repo_state_render_nothing(self, tmp_path: Path) -> None:
        tr = tmp_path / "t.jsonl"
        tr.write_text(
            json.dumps({"type": "user", "message": {"content": "Command running in background with ID: b2. Output is being written to: /tmp/b2.output."}}) + "\n"
            + json.dumps({"type": "user", "message": {"content": "<task-notification>b2 completed</task-notification>"}}) + "\n", encoding="utf-8")
        assert overlay.background_tasks(str(tr)) == []
        assert overlay.render({"tasks": [], "branches": [], "todo": [], "head_branch": "main"}) == ""


class TestScenarioScript:
    """The turn script, driven with a fake session: no claude, the prompt size is whatever the fake reports."""

    def play(self, scenario, arm: str, sizes: list[int], compact_at: int | None = None):
        events: list[dict] = []
        sent: list[tuple[str, str]] = []
        plan = sc.plan_for(arm)

        def send(name: str, text: str):
            sent.append((name, text))
            events.append({"type": "harness", "subtype": "user_turn", "turn": name})
            size = sizes[min(len(sent) - 1, len(sizes) - 1)]
            events.append({"type": "assistant", "message": {"content": [{"type": "text", "text": "ok"}], "usage": {"input_tokens": size}}})
            if compact_at is not None and len(sent) == compact_at:
                events.append({"type": "system", "subtype": "compact_boundary", "compact_metadata": {"trigger": "auto"}})
            events.append({"type": "result"})
            return events[-1]
        return co.drive(send, events, scenario, plan), sent, plan

    def test_reading_turns_run_until_the_prompt_reaches_the_target_then_the_final_ask_goes(self) -> None:
        why, sent, plan = self.play(MEANWHILE, "real", [10_000, 12_000, 18_000, 24_000, 31_000, 5_000])
        assert why is None
        names = [n for n, _ in sent]
        assert names[:2] == ["t1", "t2"] and names[-1] == "final" and names[2:-1] == ["r1", "r2", "r3"]
        assert sent[-1][1] == MEANWHILE.final and "notes/00.md and notes/01.md" in sent[2][1]

    def test_an_already_large_prompt_skips_the_reading_turns(self) -> None:
        why, sent, _ = self.play(WAITING, "real", [60_000])
        assert why is None and [n for n, _ in sent] == ["t1", "t2", "final"]

    def test_the_reading_turns_are_capped_and_never_name_a_note_that_does_not_exist(self) -> None:
        why, sent, plan = self.play(OPEN, "real", [1_000])
        assert why is None and len([n for n, _ in sent if n.startswith("r")]) == plan.max_read_turns
        named = {int(m) for _n, t in sent if _n.startswith("r") for m in re.findall(r"notes/(\d\d)\.md", t)}
        assert named == set(range(plan.max_read_turns * plan.per_turn)) and max(named) < plan.notes

    def test_a_compaction_before_the_final_turn_ends_the_run_at_once(self) -> None:
        why, sent, _ = self.play(OPEN, "real", [20_000], compact_at=2)
        assert why and "before the final turn" in why and "--pre-target" in why and [n for n, _ in sent] == ["t1", "t2"]

    def test_a_turn_with_no_result_ends_the_run(self) -> None:
        events: list[dict] = []
        assert "no result" in co.drive(lambda n, t: None, events, OPEN, sc.plan_for("real"))

    def test_the_no_compaction_baseline_uses_short_notes_and_a_small_target(self) -> None:
        full, short = sc.plan_for("real"), sc.plan_for("nocompact", pre_target=99_999)
        assert short.note_chars < full.note_chars and short.pre_target < full.pre_target and short.notes * short.note_chars < 10_000
        assert sc.plan_for("real", 12_345).pre_target == 12_345

    def test_the_session_events_carry_a_marker_at_every_user_message(self) -> None:
        written: list[str] = []
        sess = object.__new__(REAL_SESSION)
        sess.events = []
        sess.proc = type("P", (), {"stdin": type("W", (), {"write": lambda _s, x: written.append(x), "flush": lambda _s: None})()})()
        sess.send("hello", "final")
        assert sess.events == [{"type": "harness", "subtype": "user_turn", "turn": "final"}]
        assert json.loads(written[0])["message"]["content"] == "hello"
        marks = [{"type": "harness", "subtype": "user_turn"}, {"type": "result"}, {"type": "harness", "subtype": "user_turn"}]
        assert jd.final_turn_start(marks) == 2

    def test_the_items_are_lettered_in_turn_one_and_numbered_only_in_todo(self) -> None:
        for turn in (sc.TURN1_OPEN, sc.TURN1_MEANWHILE):
            assert "(a)" in turn and "(b)" in turn and "(1)" not in turn and "(2)" not in turn
        assert "TODO item 1" in sc.TURN_ITEM1 and "- [ ] 2." in sc.TODO_MD

    def test_each_scenario_seeds_what_it_needs_and_nothing_that_would_invent_work(self, tmp_path: Path) -> None:
        for name, scenario in sc.SCENARIOS.items():
            repo = tmp_path / name
            sc.seed_repo(str(repo), scenario, sc.PLAN_SHORT)
            branches = subprocess.run(["git", "-C", str(repo), "branch", "--format=%(refname:short)"], capture_output=True, text=True,
                                      encoding="utf-8").stdout.split()
            assert sorted(branches) == (["alpha", "beta", "main"] if scenario.repo_extras else ["main"]), name
            assert (repo / "TODO.md").is_file() == scenario.repo_extras and (repo / "watcher.sh").is_file()
            assert sorted(p.name for p in (repo / "notes").iterdir()) == [f"{i:02d}.md" for i in range(sc.PLAN_SHORT.notes)]
        assert sc.repo_state(str(tmp_path / "meanwhile")) == {"todo2": False, "indep": False, "todo3": False, "alpha": False, "beta": False}

    def test_repo_state_reads_effects_by_running_the_code(self, tmp_path: Path) -> None:
        repo = tmp_path / "repo"
        sc.seed_repo(str(repo), OPEN, sc.PLAN_SHORT)
        subprocess.run(["git", "-C", str(repo), "merge", "-q", "--no-edit", "alpha"], check=True, capture_output=True,
                       env={**os.environ, "GIT_AUTHOR_NAME": "t", "GIT_AUTHOR_EMAIL": "t@x", "GIT_COMMITTER_NAME": "t", "GIT_COMMITTER_EMAIL": "t@x"})
        (repo / "app.py").write_text(sc.APP_PY + "\ndef div(a, b):\n    if b == 0:\n        raise ValueError('division by zero')\n    return a / b\n"
                                     "\ndef scheme_of(u):\n    return u.split(':')[0].lower() if ':' in u else ''\n", encoding="utf-8")
        (repo / "README.md").write_text(sc.README_MD + "\n## Usage\n\nImport add, sub, div.\n", encoding="utf-8")
        state = sc.repo_state(str(repo))
        assert state == {"todo2": True, "indep": True, "todo3": True, "alpha": True, "beta": False}
        (repo / "app.py").write_text(sc.APP_PY + "\ndef div(a, b):\n    return a / b\n", encoding="utf-8")     # no ValueError: the effect is missing
        assert sc.repo_state(str(repo))["todo2"] is False
        (repo / "app.py").write_text("def div(a, b):\n    while True: pass\n", encoding="utf-8")           # a hang is False, not a hang
        assert sc.repo_state(str(repo))["todo2"] is False

    def test_the_meanwhile_board_blocks_every_thread_and_names_no_independent_job(self) -> None:
        text = sc.board_meanwhile("sid")
        assert text.count("## ") == 2 and text.count("waiting_on:") == 2 and "scheme_of" not in text
        assert "scheme_of" not in sc.TURN_GIT and "scheme_of" in sc.TURN1_MEANWHILE
