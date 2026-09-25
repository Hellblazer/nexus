#!/usr/bin/env python3
# SPDX-License-Identifier: AGPL-3.0-or-later
"""RDR-215 interactive connection-race delay ladder (nexus-veh77).

Question: in INTERACTIVE Claude Code, can a prompt submitted the instant the
input box appears drive an ``mcp_tool`` hook before that hook's MCP server has
connected? Bead nexus-q02nx.6 answered this for headless ``claude -p`` only.

Method, reused from q02nx.6 with interactive entry as the one change:

* A probe MCP server (``probe_server.py``) stands in for ``nx-mcp``. Its start
  is delayed by ``server_delay_s`` so the spawned-but-unconnected window is
  seconds wide, far wider than a model turn. Without that widening the real
  server connects in under a second and a missing barrier would be invisible.
* Every hook event under test carries TWO hooks: a command-tier twin that
  logs the event from a subprocess (it needs no server, so it is ground truth
  that the event happened) and the ``mcp_tool`` hook that calls the probe.
  A miss is "twin fired, probe line absent"; the debug log's
  ``mcp_tool hook skipped`` line is recorded as corroboration.
* The real Claude Code TUI runs in tmux on a private socket. The pane is read
  ONLY to time the submit (the moment the input box is ready); every verdict
  comes from the twin log, the probe log and the ``--debug-file`` log.
* ``UserPromptSubmit`` is wired too although the shipped manifest keeps it on
  the command tier: it fires at submit, before any model turn, so it shows
  whether the server was still unconnected at the moment of submission. That
  is what makes a clean result on the later events non-vacuous.

Plan lines (``--plan FILE``, default ``full.plan`` beside this script):
    label  server_delay_s  submit  kind  reps  [sessionstart_sleep_s]
The optional last column adds a command-tier SessionStart hook that sleeps
that long, which tests whether turn 1 waits for SessionStart hooks.
``submit`` is milliseconds after the input box appears, or ``ta`` to type
ahead 300 ms after launch, before the box exists. ``kind`` is ``bash`` (one
Bash call then stop), ``long`` (four Bash calls over ~12 s), ``agent`` (one foreground subagent dispatch) or
``broken`` (the server exits before serving: the negative control).

Credentials (RDR-219): the ladder never handles credential material itself.
Launch it under ``python3 tests/e2e/lib/claude_credentials.py run -- ...``
(or ``run --remote HOST -- ...`` for qwentescence), which sets
``CLAUDE_CODE_OAUTH_TOKEN`` in this process's own environment before exec'ing
it. The ladder reads that variable and forwards it, per run, by starting a
brand-new private-socket tmux SERVER whose environment (``env=`` on the
Python ``subprocess`` call that spawns it, never an ``env -i`` shell scrub
and never a tmux ``-e`` flag) carries exactly the token plus a small
allowlist; the token never appears in any process's argv (nexus-wauo1.19 --
see ``run_one()``'s own comment for why ``env -i`` cannot do this). It
refuses to start at all when the variable is absent. No per-run credential
file is ever written under ``.claude``, and no OAuth-account seed block is
read (T2 ``nexus_rdr/219-research-14``: not needed -- a bare onboarding stub
authenticates from the token alone on every launch shape tested).

Output: ``<out>/results.jsonl`` (one record per run) and ``<out>/summary.txt``.

**Barrier re-test (nexus-veh77 round 2, ``--barrier``).** Injects a REAL
``SessionStart`` command hook running the actual ``nx-hook mcp-connect-wait``
verb (``nexus.hooks.mcp_connect_wait``, invoked in-process via
``nexus._hook_runtime.entry.main`` through ``args.python``, which must be an
interpreter with this checkout's ``nexus`` package importable -- the
worktree's own ``.venv`` python satisfies both that and the probe's ``mcp``
requirement). The probe (``probe_server.py``) is told the same
``NEXUS_CONFIG_DIR`` via ``PROBE_LEASE_CONFIG_DIR`` and publishes the real
lease-file readiness signal at the point in its own timeline that stands in
for "connected" (right after ``PROBE_START_DELAY`` elapses). Two twin
markers, ``BarrierBegin``/``BarrierEnd``, bracket the verb invocation so the
wait duration is measured the same way ``ssgate``'s synthetic SessionStart
sleep already is.
"""
from __future__ import annotations

import argparse
import json
import os
import re
import shlex
import shutil
import subprocess
import sys
import time
from datetime import datetime, timezone
from pathlib import Path

HERE = Path(__file__).resolve().parent
EVENTS = ["UserPromptSubmit", "PreToolUse", "PostToolUse", "Stop",
          "SubagentStart", "SubagentStop"]
PROMPTS = {
    "bash": "Run exactly this bash command and nothing else: echo VEH77",
    "broken": "Run exactly this bash command and nothing else: echo VEH77",
    # Four separate Bash calls spread over ~12 s: the server connects part way
    # through turn 1, which separates "the event came before the connection"
    # from "the turn kept the view it had when it started".
    "long": ("Run these four bash commands one at a time, as four separate Bash tool "
             "calls, in order, and nothing else: sleep 3; echo A. Then sleep 3; echo B. "
             "Then sleep 3; echo C. Then sleep 3; echo D."),
    "agent": ("Use the Agent tool to dispatch one general-purpose subagent in the "
              "foreground whose only job is to reply with the word OK. Do nothing else."),
}
DEFAULT_PLAN = HERE / "full.plan"
TWIN = r'''#!/usr/bin/env python3
import json, sys, time
ev = sys.argv[1]
try:
    payload = json.load(sys.stdin)
except Exception:
    payload = {}
rec = {"event": ev, "ts": time.time(), "hook_event_name": payload.get("hook_event_name"),
       "tool_name": payload.get("tool_name")}
with open(%r, "a") as f:
    f.write(json.dumps(rec) + "\n")
'''


def tmux(sock: str, *args: str, check: bool = True,
         env: "dict[str, str] | None" = None) -> subprocess.CompletedProcess:
    # `env=None` (the default) inherits this process's own environment,
    # which is fine for every client command that talks to an ALREADY
    # RUNNING server over the socket (kill-session, send-keys,
    # capture-pane, kill-server) -- those never affect what a pane sees.
    # `env=<dict>` is for the one call that actually SPAWNS a server (a
    # `new-session` issued right after a `kill-server`, see run_one()):
    # subprocess's `env=` reaches the child through execve, never through
    # argv, so it is the one channel that can carry
    # CLAUDE_CODE_OAUTH_TOKEN without the token ever being visible to
    # `ps`.
    return subprocess.run(["tmux", "-L", sock, *args], capture_output=True, text=True,
                          check=check, env=env)


def read_jsonl(path: Path) -> list[dict]:
    if not path.exists():
        return []
    out = []
    for line in path.read_text().splitlines():
        try:
            out.append(json.loads(line))
        except ValueError:
            pass
    return out


def build_home(run_dir: Path, args, server_delay: float, broken: bool,
               ss_sleep: float) -> dict:
    home = run_dir / "home"
    work = home / "work"
    (home / ".claude").mkdir(parents=True)
    work.mkdir()
    # RDR-219: no credential file is ever written here. CLAUDE_CODE_OAUTH_TOKEN
    # travels through run_one()'s per-run tmux server environment instead
    # (see the `keep` dict there, passed as subprocess `env=`) and a bare
    # onboarding stub authenticates from it alone (T2
    # nexus_rdr/219-research-14).
    seed: dict = {"hasCompletedOnboarding": True, "theme": "dark",
                  "projects": {str(work): {"hasTrustDialogAccepted": True,
                                           "hasCompletedProjectOnboarding": True}}}
    (home / ".claude.json").write_text(json.dumps(seed))

    twin_log = run_dir / "twin.jsonl"
    probe_log = run_dir / "probe.jsonl"
    twin = home / "twin.py"
    twin.write_text(TWIN % str(twin_log))
    hooks = {}
    for ev in EVENTS:
        hooks[ev] = [{"matcher": "", "hooks": [
            {"type": "command", "command": f"{args.hook_python} {twin} {ev}"},
            {"type": "mcp_tool", "server": "probe", "tool": "probe",
             "input": {"marker": ev, "session_id": "${session_id}"}},
        ]}]
    session_start_hooks: list[dict] = []
    if ss_sleep:
        # A slow command-tier SessionStart hook, standing in for conexus's own
        # SessionStart verbs: measures whether turn 1 waits for SessionStart.
        session_start_hooks.append(
            {"type": "command", "timeout": 120,
             "command": f"{args.hook_python} {twin} SessionStartBegin; sleep {ss_sleep}; "
                        f"{args.hook_python} {twin} SessionStartEnd"})
    lease_config_dir = run_dir / "nexus_config"
    if getattr(args, "barrier", False):
        # The REAL nx-hook mcp-connect-wait verb, invoked in-process through
        # the same interpreter the probe runs under (must have this
        # checkout's `nexus` package importable). Bracketed by twin markers
        # so the wait duration is measured the same way ss_sleep's synthetic
        # SessionStart hook already is.
        barrier_snippet = (
            "import sys; sys.argv=['nx-hook','mcp-connect-wait']; "
            "from nexus._hook_runtime.entry import main; main()"
        )
        # NEXUS_CONFIG_DIR is set INLINE in the command string, not via an
        # "env" key on the hook entry (hooks.json's command entries carry no
        # such key) -- Claude Code spawns "command" hooks through a shell,
        # which is what already lets ss_sleep's own entry use ";" above.
        #
        # `< /dev/null` on BOTH twin.py calls is load-bearing, not cosmetic:
        # all three `;`-chained commands share ONE stdin pipe (the real
        # SessionStart JSON payload Claude Code writes once), and twin.py
        # itself does `json.load(sys.stdin)` to log hook_event_name/
        # tool_name. Measured without the redirect: BarrierBegin drained the
        # payload and logged it fine (hook_event_name="SessionStart"), and
        # the real verb's own read_payload() then saw an already-EOF stdin,
        # read None, and took the source-is-None fast no-op path -- the
        # barrier never waited at all, elapsed ~50ms instead of up to 15s.
        # Starving twin.py of stdin here costs it nothing: these two calls
        # only need a timestamp, never the payload.
        session_start_hooks.append(
            {"type": "command", "timeout": 25,
             "command": (
                 f"{args.hook_python} {twin} BarrierBegin < /dev/null; "
                 f"NEXUS_CONFIG_DIR={shlex.quote(str(lease_config_dir))} "
                 f"{shlex.quote(args.python)} -c {shlex.quote(barrier_snippet)}; "
                 f"{args.hook_python} {twin} BarrierEnd < /dev/null"
             )})
    if session_start_hooks:
        hooks["SessionStart"] = [{"matcher": "", "hooks": session_start_hooks}]
    settings = {"skipDangerousModePermissionPrompt": True,
                "permissions": {"allow": ["Bash", "Agent", "Task", "mcp__probe__*"]},
                "hooks": hooks}
    (home / ".claude" / "settings.json").write_text(json.dumps(settings, indent=1))
    env = {"PROBE_LOG": str(probe_log), "PROBE_START_DELAY": str(server_delay)}
    if broken:
        env["PROBE_BROKEN"] = "1"
    if getattr(args, "barrier", False):
        env["PROBE_LEASE_CONFIG_DIR"] = str(lease_config_dir)
    mcp = {"mcpServers": {"probe": {"type": "stdio", "command": args.python,
                                    "args": [str(HERE / "probe_server.py"), str(run_dir)],
                                    "env": env}}}
    (home / "mcp.json").write_text(json.dumps(mcp))
    return {"home": home, "work": work, "twin_log": twin_log, "probe_log": probe_log,
            "debug_log": run_dir / "debug.log"}


def run_one(args, label: str, server_delay: float, submit: str, kind: str, rep: int,
            ss_sleep: float = 0.0) -> dict:
    # The per-run private server holds the automation token in its
    # environment, so it is killed on every exit path: a tmux call that
    # raises mid-run, or a Ctrl-C during a sleep, would otherwise leave it
    # running (RDR-219 Phase 2 review, round 2).
    try:
        return _run_one(args, label, server_delay, submit, kind, rep, ss_sleep)
    finally:
        tmux(args.sock, "kill-server", check=False)


def _run_one(args, label: str, server_delay: float, submit: str, kind: str, rep: int,
             ss_sleep: float = 0.0) -> dict:
    barrier_tag = "-barrier" if getattr(args, "barrier", False) else ""
    run_dir = (Path(args.out) / "runs"
               / f"{label}-S{server_delay:g}-{submit}-{kind}-ss{ss_sleep:g}{barrier_tag}-{rep}")
    if run_dir.exists():
        shutil.rmtree(run_dir)
    run_dir.mkdir(parents=True)
    p = build_home(run_dir, args, server_delay, kind == "broken", ss_sleep)
    sess = "veh77"
    # RDR-219 (nexus-wauo1.19): the token must never appear on any
    # process's argv -- not as a literal `env -i CLAUDE_CODE_OAUTH_TOKEN=...`
    # argument to a transient `env` process, and not on tmux's own client
    # argv (a `new-session -e VAR=value` puts a literal value there too).
    # The one channel that never touches argv is execve's own env vector,
    # i.e. Python's subprocess `env=`. But a tmux SERVER's environment is
    # fixed at the moment it is spawned (the RDR-219 "tmux trap" -- see
    # tests/e2e/run.sh) and is never refreshed by a later `new-session` on
    # an already-running server. HOME differs on every run (`p["home"]` is
    # a fresh per-run directory), so per-run environments genuinely
    # differ, which rules out one long-lived server for the whole plan.
    # Chosen shape: start a brand-new PRIVATE-SOCKET server for every
    # single run. `kill-server` first (not just `kill-session`) so the
    # `new-session` call below is guaranteed to spawn a fresh server
    # rather than reuse one still holding a PRIOR run's HOME/token in its
    # environment; `new-session` then gets `env=keep`, an env dict built
    # entirely in Python, so the token reaches the server (and every pane
    # inside it) through execve and nowhere else. There is no `env -i`
    # shell scrub for the claude launch at all -- nothing needs scrubbing,
    # because the server was never given anything beyond this allowlist in
    # the first place; a parent Claude Code session's CLAUDECODE/
    # CLAUDE_CODE_*/session markers simply never reach `keep`.
    tmux(args.sock, "kill-server", check=False)
    keep = {k: os.environ[k]
            for k in ("PATH", "USER", "LOGNAME", "SHELL", "TMPDIR", "LANG")
            if k in os.environ}
    keep.update({"TERM": "tmux-256color", "HOME": str(p["home"]), "DISABLE_AUTOUPDATER": "1",
                 "CLAUDE_CODE_OAUTH_TOKEN": os.environ["CLAUDE_CODE_OAUTH_TOKEN"]})
    cmd = (f"{shlex.quote(args.claude)} --debug-file {shlex.quote(str(p['debug_log']))} "
           f"--mcp-config {shlex.quote(str(p['home'] / 'mcp.json'))} --strict-mcp-config "
           f"--dangerously-skip-permissions; sleep 600")
    rec: dict = {"label": label, "server_delay_s": server_delay, "submit": submit,
                 "ss_sleep_s": ss_sleep, "barrier": getattr(args, "barrier", False),
                 "kind": kind, "rep": rep, "run_dir": str(run_dir)}
    t_launch = time.time()
    tmux(args.sock, "new-session", "-d", "-s", sess, "-x", "200", "-y", "50",
         "-c", str(p["work"]), cmd, env=keep)
    rec["t_launch"] = t_launch
    ready = re.compile(args.ready_regex)
    t_ready = None
    typed_ahead = False
    deadline = t_launch + 60
    while time.time() < deadline:
        if submit == "ta" and not typed_ahead and time.time() - t_launch >= 0.3:
            tmux(args.sock, "send-keys", "-t", sess, "-l", PROMPTS[kind])
            tmux(args.sock, "send-keys", "-t", sess, "Enter")
            rec["t_submit_sent"] = time.time()
            typed_ahead = True
        pane = tmux(args.sock, "capture-pane", "-p", "-t", sess, check=False).stdout
        if ready.search(pane):
            t_ready = time.time()
            break
        time.sleep(0.02)
    rec["t_ready"] = t_ready
    if t_ready is None:
        rec["error"] = "input box never appeared"
        (run_dir / "pane.txt").write_text(
            tmux(args.sock, "capture-pane", "-p", "-t", sess, check=False).stdout)
        tmux(args.sock, "kill-session", "-t", sess, check=False)
        # Tear down the token-bearing server on this early-exit path too --
        # the next run's leading `kill-server` above would eventually catch
        # it, but a run that errors out and is also the LAST run in the
        # plan would otherwise leave the server (and the token in its
        # environment) running after this process exits.
        tmux(args.sock, "kill-server", check=False)
        return rec
    if submit != "ta":
        time.sleep(int(submit) / 1000.0)
        tmux(args.sock, "send-keys", "-t", sess, "-l", PROMPTS[kind])
        tmux(args.sock, "send-keys", "-t", sess, "Enter")
        rec["t_submit_sent"] = time.time()
    # Turn end: the command-tier Stop twin is the sentinel, never the pane.
    end_deadline = time.time() + args.turn_timeout
    while time.time() < end_deadline:
        if any(r.get("event") == "Stop" for r in read_jsonl(p["twin_log"])):
            break
        time.sleep(0.25)
    time.sleep(3)  # let a late mcp_tool hook land in the probe log
    (run_dir / "pane.txt").write_text(
        tmux(args.sock, "capture-pane", "-p", "-t", sess, check=False).stdout)
    tmux(args.sock, "send-keys", "-t", sess, "C-c", check=False)
    time.sleep(0.3)
    tmux(args.sock, "send-keys", "-t", sess, "C-c", check=False)
    time.sleep(1)
    tmux(args.sock, "kill-session", "-t", sess, check=False)
    # Same as the early-exit path above: this run's server (token in its
    # environment) is torn down here rather than left for the next run's
    # leading kill-server, so a plan whose LAST run is this one still exits
    # clean.
    tmux(args.sock, "kill-server", check=False)
    subprocess.run(["pkill", "-f", str(run_dir)], check=False)
    rec.update(analyse(p))
    return rec


_TS = re.compile(r"^(\d{4}-\d\d-\d\dT[\d:.]+Z)")


def _debug_ts(line: str) -> float | None:
    m = _TS.match(line)
    if not m:
        return None
    return datetime.strptime(m.group(1), "%Y-%m-%dT%H:%M:%S.%fZ").replace(
        tzinfo=timezone.utc).timestamp()


def analyse(p: dict) -> dict:
    twin = read_jsonl(p["twin_log"])
    probe = read_jsonl(p["probe_log"])
    debug = p["debug_log"].read_text(errors="replace").splitlines() \
        if p["debug_log"].exists() else []
    out: dict = {"events": {}}
    for ev in EVENTS:
        tw = [r["ts"] for r in twin if r.get("event") == ev]
        pr = [r["ts"] for r in probe if r.get("event") == "probe_called"
              and r.get("marker") == ev]
        skipped = sum(1 for ln in debug if f"Hook {ev}" in ln and "not connected" in ln)
        # Verdicts are about the FIRST occurrence of the event: a later
        # occurrence (a second turn) firing must not mask a first-turn miss.
        # An occurrence is matched when a probe call lands within 1 s of it.
        matched = [t for t in tw if any(abs(x - t) <= 1.0 for x in pr)]
        if not tw:
            verdict = "not_reached"
        elif any(abs(x - min(tw)) <= 1.0 for x in pr):
            verdict = "fired"
        else:
            verdict = "missed"
        out["events"][ev] = {"verdict": verdict, "twin_n": len(tw), "probe_n": len(pr),
                             "matched_n": len(matched),
                             "twin_first": min(tw) if tw else None,
                             "probe_first": min(pr) if pr else None,
                             "twin_all": sorted(tw), "probe_all": sorted(pr),
                             "skipped_lines": skipped}
    out["submitted"] = bool([r for r in twin if r.get("event") == "UserPromptSubmit"])
    for tag in ("SessionStartBegin", "SessionStartEnd", "BarrierBegin", "BarrierEnd"):
        ts = [r["ts"] for r in twin if r.get("event") == tag]
        out[tag] = min(ts) if ts else None
    if out["BarrierBegin"] is not None and out["BarrierEnd"] is not None:
        out["barrier_wait_s"] = out["BarrierEnd"] - out["BarrierBegin"]
    serving = [r["ts"] for r in probe if r.get("event") == "serving"]
    out["probe_serving"] = min(serving) if serving else None
    lease_pub = [r["ts"] for r in probe if r.get("event") == "lease_published"]
    out["lease_published"] = min(lease_pub) if lease_pub else None
    for ln in debug:
        if "[engine] turn 1 start" in ln and "turn1_ts" not in out:
            out["turn1_ts"] = _debug_ts(ln)
        if "probe" in ln and ("Successfully connected" in ln or "connected in" in ln) \
                and "connect_ts" not in out:
            out["connect_ts"] = _debug_ts(ln)
            out["connect_line"] = ln[:300]
        if ("CONNECTION_CLOSED" in ln or "Connection failed" in ln
                or "failed to connect" in ln.lower()) and "connfail_line" not in out:
            out["connfail_line"] = ln[:300]
    out["skipped_total"] = sum(1 for ln in debug if "mcp_tool hook skipped" in ln)
    return out


def _out(text: str) -> None:
    sys.stdout.write(text + "\n")
    sys.stdout.flush()


def summarise(records: list[dict]) -> str:
    rows: dict = {}
    for r in records:
        key = (r["label"], r["server_delay_s"], r["submit"], r["kind"], r.get("ss_sleep_s", 0),
               r.get("barrier", False))
        row = rows.setdefault(key, {"runs": 0, "errors": 0, "nosub": 0,
                                    **{ev: [0, 0, 0] for ev in EVENTS}})
        row["runs"] += 1
        if "error" in r:
            row["errors"] += 1
            continue
        if not r.get("submitted", True):
            # Enter was consumed before the input box took it: no turn ran.
            row["nosub"] += 1
            continue
        for ev in EVENTS:
            v = r["events"][ev]["verdict"]
            idx = {"fired": 0, "missed": 1, "not_reached": 2}[v]
            row[ev][idx] += 1
    lines = ["label | S | submit | kind | ss | barrier | runs | err | nosub | " +
             " | ".join(f"{ev} f/m/nr" for ev in EVENTS)]
    for key, row in rows.items():
        lines.append(" | ".join(str(x) for x in key) + f" | {row['runs']} | {row['errors']} | {row['nosub']} | "
                     + " | ".join("/".join(map(str, row[ev])) for ev in EVENTS))
    return "\n".join(lines)


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    ap.add_argument("--out", required=True)
    ap.add_argument("--python", required=True, help="interpreter with mcp<2 for the probe")
    ap.add_argument("--hook-python", default="python3")
    ap.add_argument("--claude", default=shutil.which("claude") or "claude")
    ap.add_argument("--sock", default="veh77-ladder")
    ap.add_argument("--plan")
    ap.add_argument("--ready-regex", default=r"bypass permissions on")
    ap.add_argument("--turn-timeout", type=float, default=120)
    ap.add_argument("--barrier", action="store_true",
                    help="inject the real nx-hook mcp-connect-wait verb as a SessionStart "
                         "hook and have the probe publish the same lease-file readiness "
                         "signal it waits for (nexus-veh77 round 2)")
    ap.add_argument("--reanalyse", action="store_true",
                    help="recompute every record in <out>/results.jsonl from its run dir")
    args = ap.parse_args()
    if not os.environ.get("CLAUDE_CODE_OAUTH_TOKEN"):
        ap.error(
            "CLAUDE_CODE_OAUTH_TOKEN is not set (RDR-219) -- launch the ladder under "
            "`python3 tests/e2e/lib/claude_credentials.py run -- ...` (or "
            "`run --remote HOST -- ...`), never with a credential file or command"
        )
    # Claude runs with cwd inside each run's HOME, so every path it is handed
    # must be absolute.
    for name in ("out", "plan"):
        if getattr(args, name):
            setattr(args, name, str(Path(getattr(args, name)).resolve()))
    if "/" in args.python:
        args.python = os.path.abspath(args.python)  # a venv python: keep the symlink
    if args.reanalyse:
        res = Path(args.out) / "results.jsonl"
        recs = read_jsonl(res)
        for r in recs:
            if "error" in r:
                continue
            d = Path(r["run_dir"])
            r.update(analyse({"twin_log": d / "twin.jsonl", "probe_log": d / "probe.jsonl",
                              "debug_log": d / "debug.log"}))
        res.write_text("".join(json.dumps(r) + "\n" for r in recs))
        summary = summarise(recs)
        (Path(args.out) / "summary.txt").write_text(summary + "\n")
        _out(summary)
        return 0
    # Plan-audit round-1 residual (bead notes): a server left running on
    # this socket from an earlier invocation carries THAT invocation's own
    # environment, token included or not, regardless of what THIS process's
    # os.environ holds. Reset it before the first run so every session this
    # invocation starts is born from a fresh server, using the `keep`
    # environment run_one() builds per launch.
    tmux(args.sock, "kill-server", check=False)
    plan_text = Path(args.plan or DEFAULT_PLAN).read_text()
    Path(args.out).mkdir(parents=True, exist_ok=True)
    results = Path(args.out) / "results.jsonl"
    records = read_jsonl(results)
    for line in plan_text.splitlines():
        line = line.split("#", 1)[0].strip()
        if not line:
            continue
        label, sdelay, submit, kind, reps, *rest = line.split()
        ss = float(rest[0]) if rest else 0.0
        for rep in range(int(reps)):
            rec = run_one(args, label, float(sdelay), submit, kind, rep, ss)
            records.append(rec)
            with open(results, "a") as f:
                f.write(json.dumps(rec) + "\n")
            evs = rec.get("events", {})
            _out(f"{label} S={sdelay} submit={submit} {kind} rep={rep}: "
                  + (rec.get("error") or " ".join(f"{k}={v['verdict']}" for k, v in evs.items())))
    summary = summarise(records)
    (Path(args.out) / "summary.txt").write_text(summary + "\n")
    _out(summary)
    return 0


if __name__ == "__main__":
    sys.exit(main())
