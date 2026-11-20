# SPDX-License-Identifier: AGPL-3.0-or-later
# ruff: noqa: T201 -- a CLI harness: stdout is its output
"""Compaction OUTCOME gate: after a real compaction, does the model go back to its open work?

tests/hooks/test_compact_checkpoint.py checks that the conexus compaction hooks
(conexus/hooks/scripts/compact_checkpoint.py) write and inject a file. This module runs a
real `claude` through a real automatic compaction and measures what the model does next.

The known failure (2026-11-04 22:45Z): a session compacted with six background agents
running, the summary's "next step" named one stale deliverable, the model did only that and
ended its turn. The measured property: after the compaction, with NO user input at all, the model keeps
working its open items until each is done or genuinely blocked, then ends. Score per run =
open items acted on after the compaction / open items available (here 5: TODO items 2 and 3,
the branches alpha and beta, the background watcher). A run that does one item and ends fails.

How a compaction is forced (spike, Claude Code 2.1.318, see the README block in
tests/e2e/compact-outcome-gate.sh): ONE long-lived `claude -p --input-format stream-json`
process, so the background shell it started is still alive, and `--autocompact 100000`
(the smallest window) with a ~90k-token filler in the last user message. The automatic
compaction then runs in the middle of that turn and the model carries on by itself, which
is the case that failed. `claude -p --resume <id> /compact` also compacts, but it ends the
process (killing the background shell) and leaves no post-compaction turn to measure.

Arms (one `run-one` each; `matrix` runs N of each):

  real     the three compaction hooks of the real conexus hooks.json (PreCompact,
           PostCompact, SessionStart:compact) over the real scripts and every sibling module they
           import (SCRIPT_FILES is checked against the import closure), with `nx` replaced by a
           stub (STUB_NX) that answers every call the hook makes today: the identity-line and
           session-line board searches, the board fetch and listing, the continuation call and
           `nx identity work list --running --json`, as the real nx would on a live board. The
           scratch config dir carries one fake identity token file (no token) so the hook finds the
           board by its identity line, as RDR-253 sessions do
  control  no plugin: the compaction summary alone
  bad      the same hooks and scripts, but the stub answers every call with nothing, so the
           checkpoint carries no board and no continuation content (planted-bad; the verdict must not
           depend on the hooks merely being wired). The hook's own process-table section ("Running for
           this session") is not an nx call and stays: bad measures a checkpoint without the board
  fix      the candidate fix, as an OVERLAY owned by this harness (compact_outcome_fix_overlay.py),
           never a change to compact_checkpoint.py: the real hooks plus a second PreCompact hook
           that captures the open work (running background tasks, unmerged branches, open TODO
           items) and a second SessionStart:compact hook whose injection ends with that list and the
           line that the checkpoint is the continuation and the session runs it now, without
           waiting for the user. The real checkpoint now lists running work itself, so the overlay's
           addition is mostly the directive

Judging reads tool_use events after the `compact_boundary` event and before the turn's
`result`, never what the model says.

HOW TO RUN. Run by hand only: nothing in CI, the default pytest run or any other script starts this gate
(tests/hooks/test_compact_outcome_harness.py, which starts no claude, checks that). Use the wrapper:

  tests/e2e/compact-outcome-gate.sh --runs 5 --budget 1.5

COST AND TIME. Every run is one `claude` session (haiku by default) that stops at --max-budget-usd
(the matrix's --budget, default 1.5). A matrix is arms x runs sessions: 4 arms x 5 runs = 20. Measured
2026-11-04: about $0.03 and one minute of wall time per run. The arms run one after another and the
runs of one arm in parallel (--jobs, default 5), so the wall time is about 5 to 10 minutes; the ceiling
is 20 sessions x --max-budget-usd x 2 attempts (a NOISE run is retried once) = $60 at the defaults.

VERDICTS. Per run:
  PASS        a real automatic compaction happened and the session then acted on at least --min-score
              (0.8) of its open items with no user input
  FAIL        the same, but it acted on fewer: it did one item and ended, or asked
  NOISE       no verdict: no automatic compaction was seen, the turn did not finish or hit an API
              error or refusal, the hooks did not run (no checkpoint, nothing injected, or the arm's
              injection is not what the arm plants), or the stub nx was asked for a call it does not
              answer (the hook changed; update STUB_NX)
  UNVERIFIED  claude is not on PATH
The matrix exits 0 only when the gate arm (real) reaches --threshold passes and the planted-bad and
control arms do not; 1 when it does not, or a control arm reaches the threshold (the gate does not
discriminate); 2 UNVERIFIED when ANY run ended NOISE or UNVERIFIED after its retry (all arms noisy,
no compaction seen: never a pass); 64 for a usage error, among them a matrix that omits the gate arm, bad
or control, or a --threshold that --runs cannot reach.

  compact_outcome.py run-one --arm real|control|bad|fix --out DIR [--model M] [--budget USD]
  compact_outcome.py matrix  --runs N --out DIR [--arms real,control,bad,fix] [--threshold K]
  compact_outcome.py judge   --events FILE     (re-judge a recorded run; runs no claude)
"""
from __future__ import annotations

import argparse
import glob
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
REPO_ROOT = os.path.abspath(os.path.join(HERE, "..", "..", ".."))
PLUGIN_SRC = os.path.join(REPO_ROOT, "conexus")

#: The hooks under test: (event, matcher) groups of conexus/hooks/hooks.json whose command runs
#: compact_checkpoint.py. Everything else in that file (upgrade, self-gc, session-start, ...) is
#: dropped from the scratch copy so a live run has nothing real to act on.
HOOK_SCRIPT = "compact_checkpoint.py"
OVERLAY_SCRIPT = "compact_outcome_fix_overlay.py"
#: The hook script and every plugin sibling module it imports, transitively. compact_checkpoint.py imports them inside
#: its guard (a missing one exits 0 and degrades the checkpoint silently), so the harness test derives this set from the
#: import statements and fails when it drifts.
SCRIPT_FILES = ("compact_checkpoint.py", "_cli_lookup.py", "_endpoint_resolve.py", "_exec_path.py",
                "_identity_lookup.py", "_session_facts.py")
#: A fake identity (not one the engine ever issued) for the scratch token file and the board's identity: line.
IDENTITY = "i999001"
#: The parent session's identity and session markers. A live run must not look like, or be taken for, the session that
#: started it: none of these reaches the child (and so none reaches the hooks or the model's Bash).
PARENT_SESSION_VARS = (
    "NX_IDENTITY_TOKEN", "NX_SEAT", "NX_SESSION_ID", "NX_T1_SESSION_ID", "NX_T1_SESSION", "CLAUDE_CODE_SESSION_ID",
    "CLAUDE_CODE_CHILD_SESSION", "CLAUDE_CODE_ENTRYPOINT", "CLAUDECODE", "CLAUDE_CODE_MESSAGING_SOCKET",
    "CLAUDE_CODE_MESSAGING_TOKEN", "TMUX", "TMUX_PANE",
    "CLAUDE_CODE_SESSION_ATTENDED", "CLAUDE_CODE_CHILD_NOTES_DIR", "CLAUDE_PID",
)
#: Variables that point a client at an engine or a config dir other than the scratch ones set in child_env.
ENGINE_VARS = ("NX_SERVICE_HOST", "NX_SERVICE_PORT", "NX_MINT_TOKEN", "NX_CONFIG_DIR")

ARMS = ("real", "control", "bad", "fix")
#: A run passes when it acted on at least this share of the open items available after the compaction.
MIN_SCORE = 0.8
DEFAULT_THRESHOLD = 4
AUTOCOMPACT_TOKENS = 100_000
FILLER_CHARS = 190_000   # calibrated so the context lands just under the auto-compaction threshold
TURN_TIMEOUT_S = 240

# --------------------------------------------------------------------------------------
# The seed: what the model can see.
# --------------------------------------------------------------------------------------
TODO_MD = """# TODO

- [ ] 1. Add `mul(a, b)` to app.py.
- [ ] 2. Add `div(a, b)` to app.py; it raises ValueError("division by zero") when b is 0.
- [ ] 3. Write the "Usage" section of README.md (one short paragraph).
"""
APP_PY = '''def add(a, b):
    return a + b


def sub(a, b):
    return a - b
'''
README_MD = "# demo\n\nA tiny calculator.\n"
WATCHER_SH = """#!/bin/sh
# Prints a tick every 2 seconds; prints RELEASE-OK after 60 ticks (about two minutes), then keeps ticking.
n=0
while true; do
  n=$((n + 1))
  echo "tick $n"
  [ "$n" -ge 60 ] && echo "RELEASE-OK"
  sleep 2
done
"""

#: Standing context, the way a long session accumulates it: all of this is open, and the model
#: is expected to work it. (How strongly the mandate is worded decides how robust the summary
#: alone is, i.e. the control arm's rate; the wording is fixed here and reported with the result.)
TURN1 = (
    "I am stepping away for a while, so work without asking me questions. This repo is a demo project and "
    "all of this is open work that has to be closed out before the end of the session: "
    "(1) the branches `alpha` and `beta` each carry a commit that is not merged into main; both must be "
    "reviewed and merged; (2) TODO.md has three items; (3) ./watcher.sh has to be running in the background "
    "(start it now with run_in_background) and before we call anything done you check its output for the "
    "line RELEASE-OK. Start the watcher now, look at TODO.md, and reply with a two-line plan. "
    "Do no other work yet."
)
TURN2 = "Start with TODO item 1."
TURN3_HEAD = (
    "FYI, pasted from another team's wiki; it is not part of our work. Do not act on it; reply with the single "
    "word ok.\n\n"
)
TURN4 = "Next, TODO item 2."

# What counts as a tool call on an open item. Matched on the tool_use INPUT (never on the
# model's text, never on tool output). the last-asked item (todo2) counts like the rest.
ITEMS = {
    "todo2": re.compile(r"\bdiv\b|division by zero"),
    "todo3": re.compile(r"README|Usage"),
    "alpha": re.compile(r"\balpha\b"),
    "beta": re.compile(r"\bbeta\b"),
    "watcher": re.compile(r"watcher|watch\.log|RELEASE-OK|/tasks/[a-z0-9]+\.output"),
}
#: Tools that read or stop a background shell: a call to one of these touches the watcher.
BG_TOOL_RE = re.compile(r"^(TaskOutput|BashOutput|KillShell|TaskStop|Monitor)$")
#: Reading TODO.md or listing the repo is orientation, not work on an item. Recorded, not credited.
ORIENT_RE = re.compile(r"TODO\.md|\bgit (status|log|branch)\b|\bls\b")


def board_text(session_id: str) -> str:
    return (
        f"session: {session_id}\n"
        f"identity: {IDENTITY}\n"
        "## Close out the demo repo (alpha, beta, TODO.md, watcher)\n"
        "ref: none\n"
        "state: TODO item 1 done; item 2 in progress; item 3 open; branches alpha and beta unmerged; "
        "watcher.sh running in the background, RELEASE-OK not yet seen\n"
        "waiting_on: the watcher's RELEASE-OK line\n"
        "needs_sam: nothing\n"
    )


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
         ["identity", "work", "list"])
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
else:  # identity work list --running --json: the watcher, as a session that had registered it would show it
    print(json.dumps({"identity": "IDENTITY_HERE", "alive": True, "work": [
        {"kind": "wait", "ref": "./watcher.sh until RELEASE-OK", "state": "running", "stale": False}]}))
'''.replace("IDENTITY_HERE", IDENTITY)


def build_plugin(dest: str, overlay: bool = False) -> list[str]:
    """A scratch conexus plugin holding only the compaction hooks and the scripts they need."""
    with open(os.path.join(PLUGIN_SRC, "hooks", "hooks.json"), encoding="utf-8") as fh:
        src = json.load(fh)
    kept: dict[str, list] = {}
    for event, groups in src["hooks"].items():
        for group in groups:
            hooks = [h for h in group.get("hooks", []) if HOOK_SCRIPT in json.dumps(h)]
            if hooks:
                kept.setdefault(event, []).append({**group, "hooks": hooks})
    want = {"PreCompact", "PostCompact", "SessionStart"}
    if set(kept) != want:
        raise SystemExit(f"hooks.json no longer wires {HOOK_SCRIPT} on exactly {sorted(want)}: found {sorted(kept)}")
    if overlay:
        py = ["python3", "${CLAUDE_PLUGIN_ROOT}/hooks/scripts/" + OVERLAY_SCRIPT]
        kept["PreCompact"].append({"matcher": "manual|auto", "hooks": [{"type": "command", "command": py[0], "args": [py[1], "pre"], "timeout": 20}]})
        kept["SessionStart"].append({"matcher": "compact", "hooks": [{"type": "command", "command": py[0], "args": [py[1], "inject"], "timeout": 20}]})
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


def provision_iso(iso: str, session_id: str) -> None:
    """The scratch layout one run's hooks and stub nx see: nexus config (holding one fake identity token file, no token
    in it, so the hook finds the board by its identity line), a generation layout whose `nx` is the stub, the stub's
    board, and empty uv, lockstep and plugin-registry stand-ins. Nothing here is read from, or written to, the operator's
    own nexus or Claude configuration."""
    for d in ("nexus-config", "nx-tools", "uv-tools", "uv-bin", "uv-cache", "stub"):
        os.makedirs(os.path.join(iso, d), exist_ok=True)
    with open(os.path.join(iso, "stub", "board.md"), "w", encoding="utf-8") as fh:
        fh.write(board_text(session_id))
    gen = os.path.join(iso, "nx-tools", "gen-1", "bin")
    os.makedirs(gen)
    with open(os.path.join(gen, "nx"), "w", encoding="utf-8") as fh:
        fh.write(STUB_NX)
    os.chmod(os.path.join(gen, "nx"), 0o755)
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

    Dropped: the parent session's identity and session markers (PARENT_SESSION_VARS) and every variable that names
    another engine or config dir (ENGINE_VARS). Set: the scratch config, tools and uv directories, NX_SERVICE_URL at a
    closed port with a dummy token, and NX_IDENTITY_BIND=0 and NX_IDENTITY_LEASE=0, the two switches that make a
    SessionStart bind hook and the MCP identity lease bind nothing. The scratch plugin wires neither (see build_plugin) and
    the only `nx` anyone in the session can run is the stub, first on PATH; the switches are belt and braces. HOME and
    the rest of *base* are kept: the live run signs in through the real HOME."""
    env = {k: v for k, v in base.items() if k not in PARENT_SESSION_VARS and k not in ENGINE_VARS}
    env.update({
        "NX_NO_TELEMETRY": "1", "NX_UPGRADE_AUTO": "0",
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


def seed_repo(path: str) -> None:
    env = {**os.environ, "GIT_AUTHOR_NAME": "t", "GIT_AUTHOR_EMAIL": "t@example.invalid",
           "GIT_COMMITTER_NAME": "t", "GIT_COMMITTER_EMAIL": "t@example.invalid",
           "GIT_CONFIG_GLOBAL": os.devnull, "GIT_CONFIG_SYSTEM": os.devnull}

    def git(*a: str) -> None:
        subprocess.run(["git", "-C", path, *a], env=env, check=True, capture_output=True)

    def put(name: str, text: str, mode: int = 0o644) -> None:
        with open(os.path.join(path, name), "w", encoding="utf-8") as fh:
            fh.write(text)
        os.chmod(os.path.join(path, name), mode)

    os.makedirs(path, exist_ok=True)
    git("init", "-q", "-b", "main")
    put("app.py", APP_PY); put("README.md", README_MD); put("TODO.md", TODO_MD); put("watcher.sh", WATCHER_SH, 0o755)
    git("add", "app.py", "README.md", "TODO.md", "watcher.sh"); git("commit", "-q", "-m", "seed")
    git("checkout", "-q", "-b", "alpha")
    put("app.py", APP_PY + "\n\ndef power(a, b):\n    return a ** b\n")
    git("commit", "-q", "-am", "alpha: add power()")
    git("checkout", "-q", "main"); git("checkout", "-q", "-b", "beta")
    put("CHANGELOG.md", "# Changelog\n\n- 0.2: beta adds a changelog\n")
    git("add", "CHANGELOG.md"); git("commit", "-q", "-m", "beta: add CHANGELOG.md")
    git("checkout", "-q", "main")


def filler(chars: int = FILLER_CHARS) -> str:
    """Prose to pad the context: this repo's own docs (docs/agents/*.md, then docs/*.md, sorted), cut at
    *chars*. Deterministic for a given checkout. (A synthetic CI log was tried first and tripped the model's
    safety classifier on 2026-11-04, which garbled the post-compaction turn; real prose does not.)"""
    paths = sorted(glob.glob(os.path.join(REPO_ROOT, "docs", "agents", "*.md"))) + \
        sorted(glob.glob(os.path.join(REPO_ROOT, "docs", "*.md")))
    out, size = [], 0
    for path in paths:
        with open(path, encoding="utf-8", errors="replace") as fh:
            text = fh.read()
        out.append(text)
        size += len(text)
        if size >= chars:
            break
    return "".join(out)[:chars]


# --------------------------------------------------------------------------------------
# Judging (pure; unit-tested on fixture events in tests/hooks/test_compact_outcome_harness.py)
# --------------------------------------------------------------------------------------
def _tool_uses(event: dict) -> list[tuple[str, dict]]:
    if event.get("type") != "assistant":
        return []
    content = (event.get("message") or {}).get("content")
    if not isinstance(content, list):
        return []
    return [(b.get("name", ""), b.get("input") or {}) for b in content if isinstance(b, dict) and b.get("type") == "tool_use"]


def _ctx(event: dict) -> int:
    """Prompt size of one model call (input + cache read + cache creation), from an assistant event."""
    if event.get("type") != "assistant":
        return 0
    u = (event.get("message") or {}).get("usage") or {}
    return int(u.get("input_tokens", 0)) + int(u.get("cache_read_input_tokens", 0)) + int(u.get("cache_creation_input_tokens", 0))


def classify(name: str, tool_input: dict) -> set[str]:
    text = json.dumps(tool_input, sort_keys=True)
    hit = {item for item, rx in ITEMS.items() if rx.search(text)}
    if BG_TOOL_RE.match(name):
        hit.add("watcher")
    return hit


def judge(events: list[dict], min_score: float = MIN_SCORE) -> dict:
    """Verdict for one recorded run: PASS | FAIL | NOISE, with the evidence.

    Window: from the LAST automatic compact_boundary to the first `result` after it. The harness
    sends no user message after the compaction, so every call in the window is the session's own
    continuation. Score = items acted on / items available."""
    boundaries = [i for i, e in enumerate(events)
                  if e.get("type") == "system" and e.get("subtype") == "compact_boundary"]
    auto = [i for i in boundaries if (events[i].get("compact_metadata") or {}).get("trigger") == "auto"]
    if not auto:
        return {"verdict": "NOISE", "why": "no automatic compaction happened", "boundaries": len(boundaries)}
    start = auto[-1]
    results = [i for i, e in enumerate(events) if i > start and e.get("type") == "result"]
    if not results:
        return {"verdict": "NOISE", "why": "the post-compaction turn never produced a result (timeout or crash)"}
    end = results[0]
    res = events[end]
    if res.get("stop_reason") == "refusal" or res.get("is_error"):
        return {"verdict": "NOISE", "why": "the post-compaction turn ended in an API error or a safety refusal"}
    calls: list[dict] = []
    final_text = ""
    for e in events[start + 1:end]:
        for name, tin in _tool_uses(e):
            calls.append({"tool": name, "items": sorted(classify(name, tin)),
                          "input": json.dumps(tin, sort_keys=True)[:160]})
        if e.get("type") == "assistant":
            for blk in (e.get("message") or {}).get("content") or []:
                if isinstance(blk, dict) and blk.get("type") == "text":
                    final_text = blk.get("text", "")
    acted = sorted({it for c in calls for it in c["items"]})
    score = len(acted) / len(ITEMS)
    return {"verdict": "PASS" if score >= min_score else "FAIL", "score": round(score, 2), "acted": acted,
            "items_available": sorted(ITEMS), "tool_calls": len(calls), "calls": calls,
            "ended_with_question": final_text.rstrip().endswith("?"), "final_text": final_text[:300],
            "pre_tokens": (events[start].get("compact_metadata") or {}).get("pre_tokens"),
            "cost_usd": res.get("total_cost_usd")}


# --------------------------------------------------------------------------------------
# One live run.
# --------------------------------------------------------------------------------------
def kill_tree(pid: int, sig: int = signal.SIGTERM) -> None:
    try:
        kids = subprocess.run(["pgrep", "-P", str(pid)], capture_output=True, text=True).stdout.split()
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
                                     stderr=self.err, text=True, bufsize=1)
        self.events: list[dict] = []
        self.q: "queue.Queue[dict | None]" = queue.Queue()
        threading.Thread(target=self._pump, daemon=True).start()

    def _pump(self) -> None:
        assert self.proc.stdout
        for line in self.proc.stdout:
            self.log.write(line); self.log.flush()
            try:
                ev = json.loads(line)
            except ValueError:
                continue
            self.events.append(ev)
            self.q.put(ev)
        self.q.put(None)

    def send(self, text: str) -> None:
        assert self.proc.stdin
        msg = {"type": "user", "message": {"role": "user", "content": text}}
        self.proc.stdin.write(json.dumps(msg) + "\n"); self.proc.stdin.flush()

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
        self.log.close(); self.err.close()


def injection_evidence(events: list[dict]) -> dict:
    """What the SessionStart:compact hooks put in front of the model at the LAST compaction: proof
    that an arm's hooks really ran (a 'real' arm whose inject never fired measures nothing)."""
    boundaries = [i for i, e in enumerate(events) if e.get("type") == "system" and e.get("subtype") == "compact_boundary"]
    if not boundaries:
        return {"hook_responses": 0}
    lo = boundaries[-2] if len(boundaries) > 1 else -1
    texts = []
    for e in events[lo + 1:boundaries[-1] + 1]:
        if e.get("type") == "system" and e.get("subtype") == "hook_response" and str(e.get("hook_name", "")).startswith("SessionStart:compact"):
            try:
                texts.append(json.loads(e.get("stdout") or "{}")["hookSpecificOutput"]["additionalContext"])
            except (ValueError, KeyError, TypeError):
                texts.append("")
    joined = "\n".join(texts)
    return {"hook_responses": len(texts), "chars": len(joined), "names_branches": "alpha" in joined and "beta" in joined,
            "has_directive": "without waiting for the user" in joined}


def stub_calls(iso: str) -> list[dict]:
    """The calls the stub nx logged during one run: ``[{"args": [...], "known": bool}]``."""
    try:
        with open(os.path.join(iso, "stub", "calls.jsonl"), encoding="utf-8") as fh:
            return [json.loads(line) for line in fh if line.strip()]
    except (OSError, ValueError):
        return []


def hook_evidence_problem(arm: str, injected: dict, checkpoint_written: bool, unknown_calls: list) -> "str | None":
    """Why a run's hooks did not do what its arm needs, or None. A run with a problem is NOISE, never a verdict: a
    'real' PASS where the hooks never ran, or a planted-bad arm that carried the board, measures nothing, and a stub that
    was asked for a call it does not answer means the hook has changed under the gate."""
    if unknown_calls:
        words = "; ".join(" ".join(c) for c in unknown_calls)
        return f"the stub nx was asked for a call it does not answer ({words}): the hook changed, update STUB_NX"
    responses = injected.get("hook_responses", 0)
    if arm == "control":
        return None if responses == 0 else "the control arm saw SessionStart:compact hook output: a plugin leaked in"
    if not checkpoint_written:
        return "pre wrote no checkpoint: the compaction hooks did not run"
    if responses == 0:
        return "SessionStart:compact injected nothing: the compaction hooks did not run"
    if arm == "bad":
        return "the planted-bad injection carries the board: the plant failed" if injected.get("names_branches") else None
    if not injected.get("names_branches"):
        return "the injection does not carry the board (alpha, beta): the stub and the hook have drifted apart"
    if arm == "fix" and not injected.get("has_directive"):
        return "the fix overlay's directive is not in the injection: the overlay did not run"
    return None


def repo_state(repo: str) -> dict:
    """Which items the repo shows as done at the end (secondary evidence next to the tool-call score)."""
    def run(*a: str) -> str:
        return subprocess.run(["git", "-C", repo, *a], capture_output=True, text=True).stdout

    def read(name: str) -> str:
        try:
            with open(os.path.join(repo, name), encoding="utf-8") as fh:
                return fh.read()
        except OSError:
            return ""
    merged = run("branch", "--merged", "main").split()
    return {"todo2": "def div(" in read("app.py"), "todo3": "usage" in read("README.md").lower(),
            "alpha": "alpha" in merged, "beta": "beta" in merged}


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


def run_one(arm: str, out: str, model: str, budget: float, keep: bool = False,
            min_score: float = MIN_SCORE) -> dict:
    claude = shutil.which(os.environ.get("NX_GATE_CLAUDE", "claude"))
    if not claude:
        return {"arm": arm, "verdict": "UNVERIFIED", "why": "claude is not on PATH"}
    os.makedirs(out, exist_ok=True)
    work = os.path.realpath(tempfile.mkdtemp(prefix="compact-outcome-", dir=os.environ.get("TMPDIR", "/tmp")))
    t0 = time.monotonic()
    sess = None
    try:
        repo = os.path.join(work, "repo")
        seed_repo(repo)
        # no git repository above the scratch repo's parent: the work dir itself is the boundary
        iso = os.path.join(work, "iso")
        session_id = str(uuid.uuid4())
        provision_iso(iso, session_id)
        env = child_env(iso, arm, os.environ)
        plugin = os.path.join(work, "plugin") if arm != "control" else None
        hooks_wired = build_plugin(plugin, overlay=(arm == "fix")) if plugin else []
        argv = claude_argv(claude, plugin, model, budget, session_id)
        sess = Session(argv, repo, env, os.path.join(out, "events.jsonl"), os.path.join(out, "stderr.txt"))
        turns = [("t1", TURN1), ("t2", TURN2), ("t3", TURN3_HEAD + filler()), ("t4", TURN4)]
        for name, text in turns:
            sess.send(text)
            res = sess.wait_result(TURN_TIMEOUT_S)
            if res is None:
                return {"arm": arm, "verdict": "NOISE", "why": f"turn {name} produced no result", "secs": round(time.monotonic() - t0)}
        verdict = judge(sess.events, min_score)
        verdict["context_per_call"] = [c for c in (_ctx(e) for e in sess.events) if c]
        verdict.update({"arm": arm, "secs": round(time.monotonic() - t0), "hooks_wired": hooks_wired,
                        "session_id": session_id})
        # what the hooks did
        ck = os.path.join(iso, "nexus-config", "compact-checkpoints", f"{session_id}.md")
        verdict["checkpoint_written"] = os.path.isfile(ck)
        verdict["checkpoint_names_open_work"] = bool(os.path.isfile(ck) and re.search(
            r"alpha", open(ck, encoding="utf-8").read()))
        verdict["hook_events"] = sorted({f"{e.get('hook_event')}:{e.get('subtype')}" for e in sess.events
                                          if e.get("type") == "system" and "hook" in str(e.get("subtype", ""))})
        verdict["session_cost_usd"] = next((e.get("total_cost_usd") for e in reversed(sess.events) if e.get("type") == "result"), None)
        verdict["compactions"] = sum(1 for e in sess.events if e.get("type") == "system" and e.get("subtype") == "compact_boundary")
        verdict["injected"] = injection_evidence(sess.events)
        verdict["state_done"] = repo_state(repo)
        calls = stub_calls(iso)
        verdict["nx_calls"] = [c["args"] for c in calls]
        problem = hook_evidence_problem(arm, verdict["injected"], verdict["checkpoint_written"],
                                        [c["args"] for c in calls if not c["known"]])
        if problem and verdict["verdict"] != "NOISE":
            verdict.update({"judged": verdict["verdict"], "verdict": "NOISE", "why": problem})
        if os.path.isfile(ck):
            shutil.copy2(ck, os.path.join(out, "checkpoint.md"))
        transcript = os.path.join(own_project_dir(work), f"{session_id}.jsonl")
        if os.path.isfile(transcript):
            shutil.copy2(transcript, os.path.join(out, "transcript.jsonl"))
        return verdict
    finally:
        if sess:
            sess.close()
        clean_project_dir(work)
        if not keep:
            shutil.rmtree(work, ignore_errors=True)


def cmd_run_one(a: argparse.Namespace) -> int:
    v = run_one(a.arm, a.out, a.model, a.budget, a.keep, a.min_score)
    with open(os.path.join(a.out, "verdict.json"), "w", encoding="utf-8") as fh:
        json.dump(v, fh, indent=1)
    print(json.dumps({k: v[k] for k in v if k not in ("calls", "context_per_call")}))
    return 0 if v["verdict"] in ("PASS", "FAIL") else 2


def cmd_judge(a: argparse.Namespace) -> int:
    events = [json.loads(line) for line in open(a.events, encoding="utf-8") if line.strip()]
    print(json.dumps(judge(events, a.min_score), indent=1))
    return 0


def validate_matrix(runs: int, threshold: int, arms: list[str], gate_arm: str) -> "str | None":
    """A reason the matrix cannot measure anything, or None. Checked before any session starts (no money spent).

    The gate arm and BOTH discriminators (planted-bad and the no-hooks control) must run: without them a pass proves
    only that the model did the work, not that the hooks made the difference. And the threshold must be reachable and
    non-zero: --runs 0 or --threshold 0 would meet it with no evidence at all."""
    unknown = [x for x in [*arms, gate_arm] if x not in ARMS]
    if unknown:
        return f"unknown arm(s): {unknown}"
    if gate_arm in ("bad", "control"):
        return f"the gate arm is real or fix, not {gate_arm}"
    if runs < 1:
        return "--runs must be at least 1"
    if not 1 <= threshold <= runs:
        return f"--threshold must be between 1 and --runs ({runs}), got {threshold}"
    missing = sorted({gate_arm, "bad", "control"} - set(arms))
    if missing:
        return (f"--arms must include the gate arm ({gate_arm}) and both discriminators, bad and control; "
                f"missing: {', '.join(missing)}")
    return None


def decide(rates: dict, *, runs: int, threshold: int, gate_arm: str) -> tuple[int, list[str]]:
    """The matrix's exit code and report lines from per-arm counts ``{arm: {pass, fail, noise}}``.

    2 UNVERIFIED: any run ended NOISE or UNVERIFIED after its retry (every arm noisy, no compaction seen, hooks that
    did not run: never a pass), or a required arm has no counts. 1: the gate arm is under the threshold, or the bad or
    control arm reached it (the gate does not discriminate). 0 only when the gate arm reached it and neither did."""
    noisy = {arm: r["noise"] for arm, r in rates.items() if r["noise"]}
    if noisy:
        detail = ", ".join(f"{arm} {n}" for arm, n in sorted(noisy.items()))
        return 2, [f"UNVERIFIED: runs without a verdict ({detail}): no automatic compaction, a turn that did not finish, "
                   "hooks that did not run, or a stale stub nx. No result is reported."]
    missing = [arm for arm in (gate_arm, "bad", "control") if arm not in rates]
    if missing:
        return 2, [f"UNVERIFIED: arm(s) not run: {', '.join(missing)}"]
    gated = rates[gate_arm]
    ok = gated["pass"] >= threshold
    lines = [f"{gate_arm} arm {gated['pass']}/{runs} vs threshold {threshold}: {'PASS' if ok else 'FAIL'}"]
    for name in ("bad", "control"):
        if rates[name]["pass"] >= threshold:
            lines.append(f"NON-VACUITY FAILED: the {name} arm reached the threshold; the gate does not discriminate on this seed")
            return 1, lines
    return (0 if ok else 1), lines


def cmd_matrix(a: argparse.Namespace) -> int:
    arms = [x for x in a.arms.split(",") if x]
    refusal = validate_matrix(a.runs, a.threshold, arms, a.gate_arm)
    if refusal:
        print(refusal, file=sys.stderr)
        return 64
    t0 = time.monotonic()
    rates: dict[str, dict] = {}

    def one(arm: str, i: int) -> dict:
        out = os.path.join(a.out, f"{arm}-{i + 1}")
        v: dict = {}
        for _attempt in range(1 + a.noise_retries):
            v = run_one(arm, out, a.model, a.budget, a.keep, a.min_score)
            if v["verdict"] != "NOISE":
                break
        os.makedirs(out, exist_ok=True)
        with open(os.path.join(out, "verdict.json"), "w", encoding="utf-8") as fh:
            json.dump(v, fh, indent=1)
        print(f"  {arm} run {i + 1}/{a.runs}: {v['verdict']}  score={v.get('score')}  acted={v.get('acted')}  "
              f"calls={v.get('tool_calls')}  asked_user={v.get('ended_with_question')}  injected={v.get('injected')}  "
              f"{v.get('secs')}s  {v.get('why', '')}", flush=True)
        return v

    for arm in arms:
        with ThreadPoolExecutor(max_workers=max(1, a.jobs)) as pool:
            verdicts = list(pool.map(lambda i: one(arm, i), range(a.runs)))
        outcomes = [v["verdict"] for v in verdicts]
        scores = [v.get("score") for v in verdicts]
        scored = [x for x in scores if x is not None]
        rates[arm] = {"pass": outcomes.count("PASS"), "fail": outcomes.count("FAIL"),
                      "noise": outcomes.count("NOISE") + outcomes.count("UNVERIFIED"), "runs": outcomes, "scores": scores,
                      "mean_score": round(sum(scored) / len(scored), 2) if scored else None,
                      "cost_usd": round(sum(float(v.get("session_cost_usd") or 0) for v in verdicts), 3)}
    print()
    for arm in arms:
        r = rates[arm]
        print(f"{arm:8s} {r['pass']}/{a.runs} pass (score >= {a.min_score})  mean score {r['mean_score']}  "
              f"scores {r['scores']}  ({r['fail']} fail, {r['noise']} noise)  ${r['cost_usd']}")
    print(f"total {round(time.monotonic() - t0)}s  ${round(sum(r['cost_usd'] for r in rates.values()), 3)}")
    with open(os.path.join(a.out, "rates-" + "+".join(arms) + ".json"), "w", encoding="utf-8") as fh:
        json.dump(rates, fh, indent=1)
    code, lines = decide(rates, runs=a.runs, threshold=a.threshold, gate_arm=a.gate_arm)
    print("\n".join(lines))
    return code


def main(argv: list[str]) -> int:
    p = argparse.ArgumentParser(prog="compact_outcome.py")
    sub = p.add_subparsers(dest="cmd", required=True)
    r = sub.add_parser("run-one"); r.add_argument("--arm", required=True, choices=ARMS); r.add_argument("--out", required=True)
    r.add_argument("--model", default="haiku"); r.add_argument("--budget", type=float, default=1.5); r.add_argument("--keep", action="store_true")
    r.add_argument("--min-score", type=float, default=MIN_SCORE)
    m = sub.add_parser("matrix"); m.add_argument("--runs", type=int, default=5); m.add_argument("--out", required=True)
    m.add_argument("--arms", default="real,control,bad,fix"); m.add_argument("--threshold", type=int, default=DEFAULT_THRESHOLD)
    m.add_argument("--gate-arm", default="real", help="the arm whose pass count must reach --threshold (real = the shipped hooks)")
    m.add_argument("--min-score", type=float, default=MIN_SCORE, help="share of open items a run must act on to pass")
    m.add_argument("--model", default="haiku"); m.add_argument("--budget", type=float, default=1.5)
    m.add_argument("--jobs", type=int, default=5, help="runs of one arm in parallel"); m.add_argument("--noise-retries", type=int, default=1); m.add_argument("--keep", action="store_true")
    j = sub.add_parser("judge"); j.add_argument("--events", required=True); j.add_argument("--min-score", type=float, default=MIN_SCORE)
    try:
        a = p.parse_args(argv)
    except SystemExit:
        return 64
    return {"run-one": cmd_run_one, "matrix": cmd_matrix, "judge": cmd_judge}[a.cmd](a)


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
