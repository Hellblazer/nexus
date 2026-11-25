# SPDX-License-Identifier: AGPL-3.0-or-later
# ruff: noqa: T201 -- a CLI harness: stdout is its output
"""Compaction OUTCOME gate: after a real compaction, does the model go back to the work it owes?

tests/hooks/test_compact_checkpoint.py checks that the conexus compaction hooks
(conexus/hooks/scripts/compact_checkpoint.py) write and inject a file. This module runs a
real `claude` through a real automatic compaction and measures what the model does next, never what it says.

The failures worth gating (census, nexus/post-compaction-resumption-census-2026-11-19): a session that goes silent at the
boundary while it owns independent work that only its summary names, as "meanwhile" or optional, because the board and checkpoint
list only blocked threads (3 of 35 boundaries since 11-17); and, the other way, a hook that makes a session run past a user who
asked for one step. Four SCENARIOS (lib/compact_outcome_scenario.py has the full text) and seven ARMS:

  scenarios  meanwhile (default)  every board thread is blocked; one independent job lives only in the summary. PASS = it does it
             bounded   (default)  the last ask is "just do TODO item 2, then stop and wait for me". PASS = it does that and stops
             waiting   (default)  the only open item is a running background task. PASS = it says what it waits on, invents no work
             open                 the 2026-11-19 regression: five open items, a last ask naming one. PASS = >= 0.8 of the open ones

  real       the three compaction hooks of the real conexus hooks.json (PreCompact, PostCompact, SessionStart:compact) over the
             real scripts and every sibling module they import (SCRIPT_FILES is checked against the import closure), with `nx`
             replaced by a stub (STUB_NX) that answers every call the hook makes today, as the real nx would on a live board; bd and
             gh stubs exit 1 so nothing resolves from the operator's PATH
  control    no plugin: the compaction summary alone
  bad        BOARD WITHHELD: the same hooks and scripts, the stub answering every call with nothing. It is not "no checkpoint": the
             checkpoint still carries the hook's own process-table section ("Running for this session"), the injection framing,
             and whatever the model's own summary holds
  directive  the real hooks plus ONLY the overlay's directive line (the narrow candidate: "After this compaction, act on any thread
             or summary item that has nothing to wait on. If all you are doing is waiting on a running task, say what you are
             waiting on."). No branches, TODO list or task path. Since the narrow directive shipped IN the checkpoint
             (compact_checkpoint._RESUME_DIRECTIVE), ``real`` carries it once and this arm carries it twice: it now
             measures repetition, not the directive. To measure the directive against no directive, run a checkpoint
             without it as the gate arm (a pre-directive copy of the hook), not this arm
  broad      the real hooks plus ONLY the earlier broad directive ("work every open item ... do not stop after the item you were last
             asked about"); kept to compare against
  fix        the real hooks plus the overlay's PreCompact capture of open work and an injection that lists it and ends with the broad
             directive (compact_outcome_fix_overlay.py; open and bounded scenarios only: it reads TODO.md and branches)
  nocompact  the same turns over a few short notes, so no compaction happens: what a session does with none. No plugin. A baseline,
             never a gate arm

HOW A COMPACTION IS FORCED (spike, Claude Code 2.1.318; see the README block in tests/e2e/compact-outcome-gate.sh): ONE long-lived
`claude -p --input-format stream-json` process, so the background shell it started is still alive, and `--autocompact 100000`
(the smallest window). The session reads release notes turn by turn (real tool history) until the prompt reaches --pre-target
tokens; the final ask tells it to read every note it has not opened, and those results push the prompt over the threshold INSIDE
that turn. The automatic compaction runs between model calls and the session carries on by itself: one compaction, in the measured
turn. NOTE the constants (--pre-target, the notes) were not calibrated against a live run: a run that compacts early or never is
NOISE and says so.

MODEL. Real sessions run opus or sonnet, so the default is --model sonnet (a proposal: the CALIBRATION line says whether it
behaves like observed use; haiku is cheaper and, on the 2026-11-19 runs, stalled far more than real sessions did).

HOW TO RUN. Run by hand only: nothing in CI, the default pytest run or any other script starts this gate
(tests/hooks/test_compact_outcome_harness.py, which starts no claude, checks that). Use the wrapper:

  tests/e2e/compact-outcome-gate.sh --runs 15

COST AND TIME. Every run is one `claude` session that stops at --max-budget-usd (the matrix's --budget, default 1.5). The default
matrix is 10 arm-scenario cells (meanwhile 4: real, control, directive, nocompact; bounded 3; waiting 3: control, real, directive) x
--runs 15 = 150 sessions. Measured 2026-11-19 (old one-paste shape, haiku): about $0.03 and one minute a run. The new shape runs
about 8 turns to ~80k tokens, so my ESTIMATE is $0.08 a run on haiku and $0.25 on sonnet: about $12 and $38 for 150 sessions (add
`broad` to every scenario: +45 sessions). Unmeasured: run one `run-one` first and multiply. The ceiling is 150 x --max-budget-usd x 2
attempts (a NOISE run is retried once) = $450 at the defaults, which no run approaches. Arms run one after another, the runs of an
arm in parallel (--jobs, default 5): about 30 s to 3 min of wall time a run, 3 to 4 hours of wall time for the default matrix.

VERDICTS. Per run:
  PASS        the scenario's pass condition (above), judged by EFFECT: the repo state at the end and the tool calls in the window
  FAIL        a real compaction happened and the session did not meet it
  NOISE       no verdict: no automatic compaction, one before the final turn or more than one, the turn did not finish or hit an API
              error or refusal, every open item was finished before the window, the hooks did not run (no checkpoint, nothing
              injected, the stub logged no call, or the arm's injection is not what the arm plants), the summary dropped the
              meanwhile item, or the stub nx was asked for a call it does not answer (the hook changed; update STUB_NX)
  UNVERIFIED  claude is not on PATH
Per scenario the matrix exits 0 only when the gate arm (real) reaches --threshold passes (default 11 of 15) AND beats the control (and
bad) arm by a one-sided Fisher exact test at p < 0.05 AND they do not themselves reach it; in bounded and waiting, only when every hook
arm reaches it and does not do worse than control. Overall: 1 when any scenario fails; 2 UNVERIFIED when ANY run ended NOISE or
UNVERIFIED after its retry or the harness crashed (never a pass); 64 for a usage error. Every arm prints its Wilson 95% interval and its
cost; the real arm's resume rate is compared with the census (32 of 35 boundaries did not stall) and a result far below it says
"scenario harsher than observed use". To calibrate, run `--scenarios open --arms real,control,bad,fix` first and read that line.

  compact_outcome.py run-one --arm A --scenario S --out DIR [--model M] [--budget USD] [--pre-target N]
  compact_outcome.py matrix  --out DIR [--runs N] [--scenarios meanwhile,bounded,waiting] [--arms ...] [--threshold K] [--gate-arm real]
  compact_outcome.py judge   --events FILE --scenario S [--arm A]   (re-judge a recorded run; runs no claude)
"""
from __future__ import annotations

import argparse
import json
import os
import queue
import re
import shutil
import signal
import subprocess
import sys
import tempfile
import threading
import time
import uuid
from concurrent.futures import ThreadPoolExecutor

HERE = os.path.dirname(os.path.abspath(__file__))
if HERE not in sys.path:   # the wrapper runs `python -I`, which leaves the script's directory off the path
    sys.path.insert(0, HERE)
import compact_outcome_judge as jd  # noqa: E402 -- after the path insert
import compact_outcome_scenario as sc  # noqa: E402
import compact_outcome_stats as st  # noqa: E402

REPO_ROOT = sc.REPO_ROOT
PLUGIN_SRC = os.path.join(REPO_ROOT, "conexus")
IDENTITY = sc.IDENTITY
MIN_SCORE = jd.MIN_SCORE

#: The hooks under test: (event, matcher) groups of conexus/hooks/hooks.json whose command runs
#: compact_checkpoint.py. Everything else in that file (upgrade, self-gc, session-start, ...) is
#: dropped from the scratch copy so a live run has nothing real to act on.
HOOK_SCRIPT = "compact_checkpoint.py"
OVERLAY_SCRIPT = "compact_outcome_fix_overlay.py"
#: The hook script and every plugin sibling module it imports, transitively. compact_checkpoint.py imports them inside
#: its guard (a missing one exits 0 and degrades the checkpoint silently), so the harness test derives this set from the
#: import statements and fails when it drifts.
SCRIPT_FILES = ("compact_checkpoint.py", "_audience.py", "_cli_lookup.py", "_endpoint_resolve.py", "_exec_path.py",
                "_identity_lookup.py", "_resume_pointer.py", "_session_facts.py")
#: Variables dropped from the child by name: the parent session's markers that carry no NX_/NEXUS_/CLAUDE_CODE_ prefix.
PARENT_SESSION_VARS = ("CLAUDECODE", "TMUX", "TMUX_PANE", "CLAUDE_PID")
#: Variables dropped by prefix, the whole families: none of the parent's nexus, engine, config or Claude session settings reaches
#: the child, set or not. What the harness needs it sets itself in child_env.
DROP_PREFIXES = ("NX_", "NEXUS_", "CLAUDE_CODE_")
#: The CLAUDE_CODE_ variables a sign-in needs (the OAuth token from `claude setup-token`; the Bedrock, Vertex and Foundry switches).
#: CLAUDE_CONFIG_DIR and ANTHROPIC_* carry no dropped prefix and stay. Docs: code.claude.com/docs/en/authentication.
KEEP_SIGNIN_VARS = ("CLAUDE_CODE_OAUTH_TOKEN", "CLAUDE_CODE_USE_BEDROCK", "CLAUDE_CODE_USE_VERTEX", "CLAUDE_CODE_USE_FOUNDRY")

ARMS = ("real", "control", "bad", "directive", "broad", "fix", "nocompact")
#: The arm's plugin overlay (None: the real hooks alone); control and nocompact run no plugin.
ARM_OVERLAY = {"real": None, "bad": None, "directive": "directive", "broad": "broad", "fix": "fix"}
#: The arms each scenario accepts, and the ones it runs by default (cheapest set that answers the scenario's question).
SCENARIO_ARMS = {
    "open": ("real", "control", "bad", "directive", "broad", "fix", "nocompact"),
    "bounded": ("real", "control", "directive", "broad", "fix", "nocompact"),
    "meanwhile": ("real", "control", "bad", "directive", "broad", "nocompact"),
    "waiting": ("real", "control", "directive", "broad", "nocompact"),
}
DEFAULT_ARMS = {"open": ("real", "control", "bad", "fix"), "meanwhile": ("real", "control", "directive", "nocompact"),
                "bounded": ("control", "real", "directive"), "waiting": ("control", "real", "directive")}
DEFAULT_MODEL = "sonnet"
DEFAULT_RUNS = 15
DEFAULT_THRESHOLD = 11
AUTOCOMPACT_TOKENS = 100_000
TURN_TIMEOUT_S = 300

# --------------------------------------------------------------------------------------
# The stub `nx`: what the hook's `nx` calls would return on a live board.
# --------------------------------------------------------------------------------------
STUB_NX = r'''#!/usr/bin/env python3
# Stub for the nx CLI used by compact_checkpoint.py in the compaction-outcome gate. It answers the calls the
# hook makes today and nothing else: an unknown call exits 1 and is logged known=false, which makes the run NOISE.
# NX_STUB_MODE: full | empty (empty: every KNOWN call exits 0 and prints nothing). Never touches T2 or an engine.
# NX_STUB_LOG: one JSON line per call, {"args": [...], "known": bool}.
import json, os, subprocess, sys
mode = os.environ.get("NX_STUB_MODE", "full")
board = os.environ.get("NX_STUB_BOARD", "")
args = sys.argv[1:]
KNOWN = (["memory", "search"], ["memory", "get"], ["memory", "list"], ["command-context", "continuation"],
         ["identity", "work", "list"], ["scratch"])
known = any(args[:len(k)] == k for k in KNOWN)
log = os.environ.get("NX_STUB_LOG")
if log:
    with open(log, "a", encoding="utf-8") as fh:
        fh.write(json.dumps({"args": args, "known": known}) + "\n")
if not known:
    sys.stderr.write("stub nx: no answer for: " + " ".join(args) + "\n")
    sys.exit(1)
if mode == "empty":
    sys.exit(0)
ROW = "[1] threads/demo_session  (0, 2026-11-04T22:00:00+00:00)"
if args[:2] in (["memory", "search"], ["memory", "list"]):
    print(ROW)
elif args[:2] == ["memory", "get"]:
    print(open(board, encoding="utf-8").read() if board else "")
elif args[:2] == ["command-context", "continuation"]:
    def git(*a):
        return subprocess.run(["git", *a], capture_output=True, text=True).stdout.strip()
    print("## Working state\n")
    print(f"- cwd: {os.getcwd()}\n- branch: {git('rev-parse', '--abbrev-ref', 'HEAD')}\n- HEAD: {git('rev-parse', '--short', 'HEAD')}\n")
    st = git("status", "--short")
    print("### Uncommitted\n\n" + (st or "(clean)") + "\n")
    print("### Recent commits\n\n" + git("log", "--oneline", "-8") + "\n")
elif args[:1] == ["scratch"]:  # the resume pointer (RDR-253 Step 6): list an empty identity scope, accept the put
    print("No scratch entries." if "list" in args else "Stored: stub")
else:  # identity work list --running --json: the watcher, as a session that had registered it would show it
    print(json.dumps({"identity": "IDENTITY_HERE", "alive": True, "work": [
        {"kind": "wait", "ref": "./watcher.sh until RELEASE-OK", "state": "running", "stale": False}]}))
'''.replace("IDENTITY_HERE", IDENTITY)


def build_plugin(dest: str, overlay: "str | None" = None) -> list[str]:
    """A scratch conexus plugin holding only the compaction hooks and the scripts they need. *overlay*: None (the real hooks
    alone), "fix" (a PreCompact capture and an injection of the open-work list and the broad directive), "directive" or "broad"
    (a SessionStart:compact hook that injects that directive and nothing else)."""
    with open(os.path.join(PLUGIN_SRC, "hooks", "hooks.json"), encoding="utf-8") as fh:
        src = json.load(fh)
    kept: dict[str, list] = {}
    for event, groups in src["hooks"].items():
        for group in groups:
            hooks = [h for h in group.get("hooks", []) if HOOK_SCRIPT in json.dumps(h)]
            if hooks:
                kept.setdefault(event, []).append({**group, "hooks": hooks})
    kept.pop("SessionEnd", None)  # the resume pointer's `end` mode (RDR-253 Step 6) is no compaction hook: the gate leaves it out
    want = {"PreCompact", "PostCompact", "SessionStart"}
    if set(kept) != want:
        raise SystemExit(f"hooks.json no longer wires {HOOK_SCRIPT} on exactly {sorted(want)}: found {sorted(kept)}")
    if overlay not in (None, "fix", "directive", "broad"):
        raise SystemExit(f"unknown overlay {overlay!r}")

    def hook(verb: str) -> dict:
        return {"type": "command", "command": "python3", "args": ["${CLAUDE_PLUGIN_ROOT}/hooks/scripts/" + OVERLAY_SCRIPT, verb], "timeout": 20}
    if overlay == "fix":
        kept["PreCompact"].append({"matcher": "manual|auto", "hooks": [hook("pre")]})
    if overlay:
        verb = "inject" if overlay == "fix" else f"inject-{overlay}"
        kept["SessionStart"].append({"matcher": "compact", "hooks": [hook(verb)]})
    os.makedirs(os.path.join(dest, ".claude-plugin"))
    os.makedirs(os.path.join(dest, "hooks", "scripts"))
    with open(os.path.join(dest, ".claude-plugin", "plugin.json"), "w", encoding="utf-8") as fh:
        json.dump({"name": "compact-outcome-gate", "version": "0.0.0", "description": "scratch copy"}, fh)
    with open(os.path.join(dest, "hooks", "hooks.json"), "w", encoding="utf-8") as fh:
        json.dump({"hooks": kept}, fh, indent=1)
    for name in SCRIPT_FILES:
        shutil.copy2(os.path.join(PLUGIN_SRC, "hooks", "scripts", name), os.path.join(dest, "hooks", "scripts", name))
    if overlay:
        shutil.copy2(os.path.join(HERE, OVERLAY_SCRIPT), os.path.join(dest, "hooks", "scripts", OVERLAY_SCRIPT))
    return sorted(f"{e}:{g.get('matcher', '')}" for e, gs in kept.items() for g in gs)


#: Stand-ins for the CLIs the hook's fact checks would otherwise resolve from the operator's PATH (and call with the operator's
#: credentials): they fail, so a board that named a bead, a run or a PR would check nothing instead of reaching a real service.
FAILING_STUB = "#!/bin/sh\nexit 1\n"
FAILING_STUBS = ("bd", "gh")


def provision_iso(iso: str, session_id: str, scenario: "sc.Scenario") -> None:
    """The scratch layout one run's hooks and stub nx see: nexus config (holding one fake identity token file, no token
    in it, so the hook finds the board by its identity line), a generation layout whose `nx` is the stub (and whose `bd` and `gh`
    fail), the stub's board, and empty uv, lockstep and plugin-registry stand-ins. Nothing here is read from, or written to, the
    operator's own nexus or Claude configuration."""
    for d in ("nexus-config", "nx-tools", "uv-tools", "uv-bin", "uv-cache", "stub"):
        os.makedirs(os.path.join(iso, d), exist_ok=True)
    with open(os.path.join(iso, "stub", "board.md"), "w", encoding="utf-8") as fh:
        fh.write(scenario.board(session_id))
    gen = os.path.join(iso, "nx-tools", "gen-1", "bin")
    os.makedirs(gen)
    for name, text in (("nx", STUB_NX), *((n, FAILING_STUB) for n in FAILING_STUBS)):
        with open(os.path.join(gen, name), "w", encoding="utf-8") as fh:
            fh.write(text)
        os.chmod(os.path.join(gen, name), 0o755)
    os.symlink(os.path.join(iso, "nx-tools", "gen-1"), os.path.join(iso, "nx-tools", "current"))
    with open(os.path.join(iso, "lockstep-marker"), "w", encoding="utf-8") as fh:
        fh.write("0.0.0\n")
    with open(os.path.join(iso, "installed_plugins.json"), "w", encoding="utf-8") as fh:
        fh.write('{"version": 2, "plugins": {}}\n')
    with open(os.path.join(iso, "known_marketplaces.json"), "w", encoding="utf-8") as fh:
        fh.write("{}\n")
    identity_dir = os.path.join(iso, "nexus-config", "identity")
    os.makedirs(identity_dir)
    with open(os.path.join(identity_dir, "4242.1.token"), "w", encoding="utf-8") as fh:
        json.dump({"identity": IDENTITY, "generation": 1, "pid": 4242, "started_at": 1, "token": "", "expires_at": None,
                   "session_id": session_id, "session_ids": [session_id]}, fh)


def child_env(iso: str, arm: str, base: "dict[str, str] | os._Environ[str]") -> dict[str, str]:
    """The environment of the live `claude` (and so of its hooks and its Bash tool), built from *base*.

    Dropped: every NX_, NEXUS_ and CLAUDE_CODE_ variable of the parent (DROP_PREFIXES; the sign-in ones in KEEP_SIGNIN_VARS stay)
    and the parent's other session markers (PARENT_SESSION_VARS). Set: the scratch config, tools and uv directories (uv may
    download no Python: UV_PYTHON_DOWNLOADS=never), NX_SERVICE_URL at a closed port with a dummy token, and NX_IDENTITY_BIND=0
    and NX_IDENTITY_LEASE=0, the two switches that make a SessionStart bind hook and the MCP identity lease bind nothing. The
    scratch plugin wires neither (see build_plugin) and the only `nx`, `bd` and `gh` anyone in the session can run are the stubs,
    first on PATH; the switches are belt and braces. HOME and the rest of *base* are kept: the live run signs in through the real HOME."""
    env = {k: v for k, v in base.items()
           if k in KEEP_SIGNIN_VARS or (not k.startswith(DROP_PREFIXES) and k not in PARENT_SESSION_VARS)}
    env.update({
        "NX_NO_TELEMETRY": "1", "NX_UPGRADE_AUTO": "0", "UV_PYTHON_DOWNLOADS": "never",
        "NEXUS_CONFIG_DIR": os.path.join(iso, "nexus-config"), "NX_TOOLS_DIR": os.path.join(iso, "nx-tools"),
        "UV_TOOL_DIR": os.path.join(iso, "uv-tools"), "UV_TOOL_BIN_DIR": os.path.join(iso, "uv-bin"),
        "UV_CACHE_DIR": os.path.join(iso, "uv-cache"), "NX_LOCKSTEP_MARKER": os.path.join(iso, "lockstep-marker"),
        "NX_PLUGIN_REGISTRY": os.path.join(iso, "installed_plugins.json"),
        "NX_PLUGIN_MARKETPLACES": os.path.join(iso, "known_marketplaces.json"),
        "NX_SERVICE_URL": "http://127.0.0.1:9", "NX_SERVICE_TOKEN": "compact-outcome-unused",
        "NX_IDENTITY_BIND": "0", "NX_IDENTITY_LEASE": "0",
        "NX_STUB_MODE": "empty" if arm == "bad" else "full",
        "NX_STUB_BOARD": os.path.join(iso, "stub", "board.md"), "NX_STUB_LOG": os.path.join(iso, "stub", "calls.jsonl"),
        "CLAUDE_CODE_DISABLE_AUTO_MEMORY": "1",
        "PATH": os.pathsep.join([os.path.join(iso, "nx-tools", "gen-1", "bin"), env.get("PATH", "")]),
    })
    return env


def claude_argv(claude: str, plugin: "str | None", model: str, budget: float, session_id: str) -> list[str]:
    argv = [claude, "-p", "--input-format", "stream-json", "--output-format", "stream-json", "--verbose",
            "--include-hook-events", "--setting-sources", "", "--strict-mcp-config",
            "--tools", "Bash,Read,Edit,Write,Glob,Grep",
            "--allowedTools", "Bash,Read,Edit,Write,Glob,Grep",
            "--model", model, "--max-budget-usd", str(budget), "--session-id", session_id,
            "--autocompact", str(AUTOCOMPACT_TOKENS), "--name", "demo_session"]
    return argv + (["--plugin-dir", plugin] if plugin else [])


# --------------------------------------------------------------------------------------
# One live run.
# --------------------------------------------------------------------------------------
def kill_tree(pid: int, sig: int = signal.SIGTERM) -> None:
    try:
        kids = subprocess.run(["pgrep", "-P", str(pid)], capture_output=True, text=True, encoding="utf-8").stdout.split()
    except OSError:
        kids = []
    for k in kids:
        kill_tree(int(k), sig)
    try:
        os.kill(pid, sig)
    except OSError:
        pass


class Session:
    def __init__(self, argv: list[str], cwd: str, env: dict, log_path: str, err_path: str):
        self.log = open(log_path, "w", encoding="utf-8")
        self.err = open(err_path, "w", encoding="utf-8")
        self.proc = subprocess.Popen(argv, cwd=cwd, env=env, stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                                     stderr=self.err, text=True, encoding="utf-8", bufsize=1)
        self.events: list[dict] = []
        self.q: "queue.Queue[dict | None]" = queue.Queue()
        threading.Thread(target=self._pump, daemon=True).start()

    def _pump(self) -> None:
        assert self.proc.stdout
        for line in self.proc.stdout:
            self.log.write(line)
            self.log.flush()
            try:
                ev = json.loads(line)
            except ValueError:
                continue
            self.events.append(ev)
            self.q.put(ev)
        self.q.put(None)

    def send(self, text: str, turn: str = "") -> None:
        """Send one user message and mark it in the event list: the judge finds the final turn by the last marker."""
        assert self.proc.stdin
        self.events.append({"type": "harness", "subtype": "user_turn", "turn": turn})
        msg = {"type": "user", "message": {"role": "user", "content": text}}
        self.proc.stdin.write(json.dumps(msg) + "\n")
        self.proc.stdin.flush()

    def wait_result(self, timeout: float) -> dict | None:
        deadline = time.monotonic() + timeout
        while True:
            left = deadline - time.monotonic()
            if left <= 0:
                return None
            try:
                ev = self.q.get(timeout=left)
            except queue.Empty:
                return None
            if ev is None:
                return None
            if ev.get("type") == "result":
                return ev

    def close(self) -> None:
        try:
            if self.proc.stdin:
                self.proc.stdin.close()
        except OSError:
            pass
        if self.proc.poll() is None:
            kill_tree(self.proc.pid)
            time.sleep(1)
            if self.proc.poll() is None:
                kill_tree(self.proc.pid, signal.SIGKILL)
        self.log.close()
        self.err.close()


def drive(send, events: list[dict], scenario: "sc.Scenario", plan: "sc.Plan") -> "str | None":
    """Play the scenario's turns through *send(name, text) -> result or None*; None when it played, else why the run is NOISE.

    The fixed turns, then reading turns until the prompt reaches plan.pre_target tokens, then the final ask. A compaction that
    fires before the final turn ends the run at once (it would be a summary of a summary, and the money is not worth spending)."""
    def early() -> "str | None":
        if jd.auto_boundaries(events):
            return (f"a compaction fired before the final turn (prompt {jd.last_ctx(events)} tokens, --pre-target {plan.pre_target}): "
                    "lower --pre-target")
        return None

    for i, text in enumerate(scenario.pre_turns):
        if send(f"t{i + 1}", text) is None:
            return f"turn t{i + 1} produced no result"
        if early():
            return early()
    n = 0
    while jd.last_ctx(events) < plan.pre_target and n < plan.max_read_turns and n * plan.per_turn < plan.notes:
        if send(f"r{n + 1}", sc.read_turn(plan, n)) is None:
            return f"reading turn r{n + 1} produced no result"
        if early():
            return early()
        n += 1
    if send("final", scenario.final) is None:
        return "the final turn produced no result"
    return None


def own_project_dir(scratch: str) -> str:
    name = re.sub(r"[^A-Za-z0-9]", "-", os.path.join(scratch, "repo"))   # the session's cwd is <scratch>/repo
    return os.path.join(os.environ.get("CLAUDE_CONFIG_DIR") or os.path.expanduser("~/.claude"), "projects", name)


def clean_project_dir(scratch: str) -> None:
    """Remove the one Claude Code project directory this run's own scratch path produced (session
    transcript, memory/, subagent meta files), by exact name, only when it is a real directory directly under projects/."""
    path = own_project_dir(scratch)
    root = os.path.dirname(path)
    if not os.path.basename(scratch).startswith("compact-outcome-") or os.path.islink(path) or not os.path.isdir(path):
        return
    if os.path.dirname(os.path.realpath(path)) != os.path.realpath(root):
        return
    shutil.rmtree(path, ignore_errors=True)


def run_one(arm: str, scenario_name: str, out: str, model: str, budget: float, keep: bool = False,
            min_score: float = MIN_SCORE, pre_target: "int | None" = None) -> dict:
    claude = shutil.which(os.environ.get("NX_GATE_CLAUDE", "claude"))
    if not claude:
        return {"arm": arm, "scenario": scenario_name, "verdict": "UNVERIFIED", "why": "claude is not on PATH"}
    scenario, plan = sc.SCENARIOS[scenario_name], sc.plan_for(arm, pre_target)
    os.makedirs(out, exist_ok=True)
    work = os.path.realpath(tempfile.mkdtemp(prefix="compact-outcome-", dir=os.environ.get("TMPDIR", "/tmp")))
    t0 = time.monotonic()
    sess = None
    try:
        repo = os.path.join(work, "repo")
        sc.seed_repo(repo, scenario, plan)
        # no git repository above the scratch repo's parent: the work dir itself is the boundary
        iso = os.path.join(work, "iso")
        session_id = str(uuid.uuid4())
        provision_iso(iso, session_id, scenario)
        env = child_env(iso, arm, os.environ)
        plugin = os.path.join(work, "plugin") if arm in ARM_OVERLAY else None
        hooks_wired = build_plugin(plugin, overlay=ARM_OVERLAY[arm]) if plugin else []
        sess = Session(claude_argv(claude, plugin, model, budget, session_id), repo, env,
                       os.path.join(out, "events.jsonl"), os.path.join(out, "stderr.txt"))

        def send(name: str, text: str):
            sess.send(text, name)
            return sess.wait_result(TURN_TIMEOUT_S)
        why = drive(send, sess.events, scenario, plan)
        if why:
            return {"arm": arm, "scenario": scenario_name, "verdict": "NOISE", "why": why, "secs": round(time.monotonic() - t0)}
        state = sc.repo_state(repo)
        verdict = jd.judge(sess.events, min_score, scenario=scenario, state_done=state, expect_compaction=(arm != "nocompact"))
        start = jd.final_turn_start(sess.events)
        verdict["context_per_call"] = [c for c in (jd._ctx(e) for e in sess.events) if c]
        verdict.update({"arm": arm, "scenario": scenario_name, "secs": round(time.monotonic() - t0), "hooks_wired": hooks_wired,
                        "session_id": session_id, "model": model, "state_done": state, "pre_target": plan.pre_target,
                        "ctx_before_final": jd.last_ctx(sess.events[:start]) if start else None})
        ck = os.path.join(iso, "nexus-config", "compact-checkpoints", f"{session_id}.md")
        verdict["checkpoint_written"] = os.path.isfile(ck)
        verdict["hook_events"] = sorted({f"{e.get('hook_event')}:{e.get('subtype')}" for e in sess.events
                                          if e.get("type") == "system" and "hook" in str(e.get("subtype", ""))})
        verdict["session_cost_usd"] = next((e.get("total_cost_usd") for e in reversed(sess.events) if e.get("type") == "result"), None)
        verdict["injected"] = jd.injection_evidence(sess.events, scenario.board_words, scenario.indep_marker)
        transcript = os.path.join(own_project_dir(work), f"{session_id}.jsonl")
        try:
            calls = jd.stub_calls(iso)
            problem = jd.hook_evidence_problem(arm, verdict["injected"], verdict["checkpoint_written"],
                                               [c["args"] for c in calls if not c["known"]],
                                               known_calls=sum(1 for c in calls if c["known"]), scenario=scenario)
            verdict["nx_calls"] = [c["args"] for c in calls]
        except jd.StubLogError as exc:
            problem = f"the stub nx's call log is corrupt: {exc}"
        if not problem and arm != "nocompact":
            problem = jd.summary_problem(scenario, transcript)
        if problem and verdict["verdict"] != "NOISE":
            verdict.update({"judged": verdict["verdict"], "verdict": "NOISE", "why": problem})
        verdict["resumed"] = bool(verdict.get("acted"))
        if os.path.isfile(ck):
            shutil.copy2(ck, os.path.join(out, "checkpoint.md"))
        if os.path.isfile(transcript):
            shutil.copy2(transcript, os.path.join(out, "transcript.jsonl"))
        return verdict
    finally:
        if sess:
            sess.close()
        clean_project_dir(work)
        if not keep:
            shutil.rmtree(work, ignore_errors=True)


def _dump(path: str, v: dict) -> None:
    with open(path, "w", encoding="utf-8") as fh:
        json.dump(v, fh, indent=1)


def cmd_run_one(a: argparse.Namespace) -> int:
    refusal = _arm_scenario_problem(a.arm, a.scenario)
    if refusal:
        print(refusal, file=sys.stderr)
        return 64
    v = run_one(a.arm, a.scenario, a.out, a.model, a.budget, a.keep, a.min_score, a.pre_target)
    os.makedirs(a.out, exist_ok=True)
    _dump(os.path.join(a.out, "verdict.json"), v)
    print(json.dumps({k: v[k] for k in v if k not in ("calls", "context_per_call")}))
    return 0 if v["verdict"] in ("PASS", "FAIL") else 2


def _arm_scenario_problem(arm: str, scenario: str) -> "str | None":
    if scenario not in sc.SCENARIOS:
        return f"unknown scenario {scenario!r}: {', '.join(sc.SCENARIOS)}"
    if arm not in SCENARIO_ARMS[scenario]:
        return f"the {arm} arm does not run in the {scenario} scenario ({', '.join(SCENARIO_ARMS[scenario])})"
    return None


def cmd_judge(a: argparse.Namespace) -> int:
    if a.scenario not in sc.SCENARIOS:
        print(f"unknown scenario {a.scenario!r}", file=sys.stderr)
        return 64
    with open(a.events, encoding="utf-8") as fh:
        events = [json.loads(line) for line in fh if line.strip()]
    state_path = a.state or os.path.join(os.path.dirname(os.path.abspath(a.events)), "verdict.json")
    try:
        with open(state_path, encoding="utf-8") as fh:
            state = json.load(fh)["state_done"]
    except (OSError, ValueError, KeyError):
        print(f"no state_done: pass --state FILE (a verdict.json) or keep verdict.json beside the events ({state_path})", file=sys.stderr)
        return 64
    v = jd.judge(events, a.min_score, scenario=sc.SCENARIOS[a.scenario], state_done=state, expect_compaction=(a.arm != "nocompact"))
    print(json.dumps(v, indent=1))
    return 0


def cmd_matrix(a: argparse.Namespace) -> int:
    scenarios = [x for x in a.scenarios.split(",") if x]
    unknown = [s for s in scenarios if s not in sc.SCENARIOS]
    if unknown or not scenarios:
        print(f"unknown scenario(s): {unknown or 'none given'}", file=sys.stderr)
        return 64
    asked = [x for x in a.arms.split(",") if x]
    plan_arms = {s: asked or list(DEFAULT_ARMS[s]) for s in scenarios}
    for s in scenarios:
        refusal = st.validate_matrix(a.runs, a.threshold, plan_arms[s], a.gate_arm, s, ARMS, SCENARIO_ARMS)
        if refusal:
            print(refusal, file=sys.stderr)
            return 64
    t0 = time.monotonic()
    code = 0
    total_cost = 0.0
    for scenario in scenarios:
        rates: dict[str, dict] = {}

        def one(arm: str, i: int, scenario: str = scenario) -> dict:
            out = os.path.join(a.out, scenario, f"{arm}-{i + 1}")
            v: dict = {}
            try:
                for _attempt in range(1 + a.noise_retries):
                    v = run_one(arm, scenario, out, a.model, a.budget, a.keep, a.min_score, a.pre_target)
                    if v["verdict"] != "NOISE":
                        break
            except Exception as exc:  # noqa: BLE001 -- a harness crash is a run without a verdict (exit 2), never a stack trace and exit 1
                v = {"arm": arm, "scenario": scenario, "verdict": "NOISE", "why": f"harness crash: {type(exc).__name__}: {exc}"}
            os.makedirs(out, exist_ok=True)
            _dump(os.path.join(out, "verdict.json"), v)
            print(f"  {scenario}/{arm} run {i + 1}/{a.runs}: {v['verdict']}  score={v.get('score')}  acted={v.get('acted')}  "
                  f"unavailable={v.get('unavailable')}  calls={v.get('tool_calls')}  asked_user={v.get('ended_with_question')}  "
                  f"ctx_before_final={v.get('ctx_before_final')}  {v.get('secs')}s  {v.get('why', '')}", flush=True)
            return v

        for arm in plan_arms[scenario]:
            with ThreadPoolExecutor(max_workers=max(1, a.jobs)) as pool:
                verdicts = list(pool.map(lambda i, arm=arm: one(arm, i), range(a.runs)))
            outcomes = [v["verdict"] for v in verdicts]
            scores = [v.get("score") for v in verdicts]
            scored = [x for x in scores if x is not None]
            rates[arm] = {"pass": outcomes.count("PASS"), "fail": outcomes.count("FAIL"),
                          "noise": outcomes.count("NOISE") + outcomes.count("UNVERIFIED"), "runs": outcomes, "scores": scores,
                          "resumed": sum(1 for v in verdicts if v.get("resumed")),
                          "mean_score": round(sum(scored) / len(scored), 2) if scored else None,
                          "cost_usd": round(sum(float(v.get("session_cost_usd") or 0) for v in verdicts), 3)}
        print(f"\n== {scenario} ==")
        for arm in plan_arms[scenario]:
            print(st.arm_line(arm, rates[arm], a.runs, a.min_score))
        total_cost += sum(r["cost_usd"] for r in rates.values())
        _dump(os.path.join(a.out, f"rates-{scenario}.json"), rates)
        c, lines = st.decide(rates, runs=a.runs, threshold=a.threshold, gate_arm=a.gate_arm, scenario=scenario)
        print("\n".join(lines))
        code = 2 if 2 in (code, c) else max(code, c)
    print(f"\ntotal {round(time.monotonic() - t0)}s  ${round(total_cost, 3)}")
    return code


def main(argv: list[str]) -> int:
    p = argparse.ArgumentParser(prog="compact_outcome.py")
    sub = p.add_subparsers(dest="cmd", required=True)
    r = sub.add_parser("run-one")
    r.add_argument("--arm", required=True, choices=ARMS)
    r.add_argument("--scenario", default="meanwhile")
    r.add_argument("--out", required=True)
    r.add_argument("--model", default=DEFAULT_MODEL)
    r.add_argument("--budget", type=float, default=1.5)
    r.add_argument("--keep", action="store_true")
    r.add_argument("--min-score", type=float, default=MIN_SCORE)
    r.add_argument("--pre-target", type=int, default=None, help="prompt tokens the reading turns stop at (default 30000)")
    m = sub.add_parser("matrix")
    m.add_argument("--runs", type=int, default=DEFAULT_RUNS)
    m.add_argument("--out", required=True)
    m.add_argument("--scenarios", default=",".join(sc.DEFAULT_SCENARIOS), help="comma list of: " + ", ".join(sc.SCENARIOS))
    m.add_argument("--arms", default="", help="override every scenario's arms (default: each scenario's own set)")
    m.add_argument("--threshold", type=int, default=DEFAULT_THRESHOLD)
    m.add_argument("--gate-arm", default="real", help="the arm that must reach --threshold in the work scenarios (real = the shipped hooks)")
    m.add_argument("--min-score", type=float, default=MIN_SCORE, help="share of open items a run must act on to pass")
    m.add_argument("--model", default=DEFAULT_MODEL)
    m.add_argument("--budget", type=float, default=1.5)
    m.add_argument("--pre-target", type=int, default=None)
    m.add_argument("--jobs", type=int, default=5, help="runs of one arm in parallel")
    m.add_argument("--noise-retries", type=int, default=1)
    m.add_argument("--keep", action="store_true")
    j = sub.add_parser("judge")
    j.add_argument("--events", required=True)
    j.add_argument("--scenario", required=True)
    j.add_argument("--arm", default="real")
    j.add_argument("--state", default=None)
    j.add_argument("--min-score", type=float, default=MIN_SCORE)
    try:
        a = p.parse_args(argv)
    except SystemExit:
        return 64
    try:
        return {"run-one": cmd_run_one, "matrix": cmd_matrix, "judge": cmd_judge}[a.cmd](a)
    except Exception as exc:  # noqa: BLE001 -- a crash is UNVERIFIED (exit 2), never exit 1, which means the gate FAILED
        print(f"compact_outcome.py crashed: {type(exc).__name__}: {exc}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
