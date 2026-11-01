# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (c) 2026 Hal Hildebrand. All rights reserved.
"""Sandbox gate for scripts/claude-restart-sessions --fresh (nexus-nuyju).

Drives real Claude Code sessions through the coordinated-restart continuation roll and
checks every outcome, with nothing read from or written to the operator's ~/.claude:

- a scratch HOME (``.claude.json`` with onboarding done and the work folder trusted);
- a private tmux server (``-L``, ``-f /dev/null``, a bare bash in every pane);
- three sessions in a scratch git repo, each told a code word: ``rs_alpha`` and
  ``rs_beta`` are left at their prompt (with whatever suggested prompt Claude Code
  draws, in dim text), ``rs_draft`` gets a real unsent draft typed into its prompt;
- the script under test runs from a fourth pane with ``--fresh --fallback-resume
  --no-update``, exactly as the operator runs it, minus the release install.

Expected: alpha and beta write a handoff, restart with a new session id and are fed
``cat <handoff>``; draft is skipped for its draft (no handoff asked) and resumed with
its old id; all three then answer their own code word and no other.

Continuity is proved from the handoff, not merely observed. Auto memory is off
(``CLAUDE_CODE_DISABLE_AUTO_MEMORY``, ``autoMemoryEnabled: false``) and the word is given
as task context, not "remember"; each fresh session's word must be in its handoff file,
must first appear in its transcript inside a tool result (the read of the handoff), and
no memory file in the scratch HOME may hold any word. A negative control, a fourth fresh
session in the same folder with no handoff, must not know any word.

Positive control: ``NX_RESTART_SANDBOX_SCRIPT=<copy>`` runs another copy of the script;
the pane checks always use this checkout's reader. The pre-fix copy (776e83008) needs its
``CLAUDE = str(Path.home() / ".local" / "bin" / "claude")`` line changed to
``CLAUDE = os.environ.get("NX_RESTART_CLAUDE", "claude")`` to relaunch under a scratch
HOME; it then fails in the 7.92.0 shape (alpha and beta skipped as drafts, resumed).

Run through run.sh (it supplies the automation token, RDR-219). Prints one verdict
line, ``RESTART SANDBOX PASSED`` or ``RESTART SANDBOX FAILED``, and exits 0 or 1;
exit 2 when a precondition is missing. On failure the scratch HOME is kept and named.
"""
# ruff: noqa: T201 -- a gate harness: stdout carries its progress and verdict line
from __future__ import annotations

import argparse
import importlib.machinery
import importlib.util
import json
import os
import re
import shlex
import shutil
import signal
import subprocess
import sys
import tempfile
import time
from pathlib import Path

REPO = Path(__file__).resolve().parents[3]
#: The script under test. NX_RESTART_SANDBOX_SCRIPT points the gate at another copy, which is
#: how its positive control runs: the pre-fix script must fail here on the ghost case.
SCRIPT = Path(os.environ.get("NX_RESTART_SANDBOX_SCRIPT") or REPO / "scripts" / "claude-restart-sessions")
WORDS = {"rs_alpha": "HELIOTROPE", "rs_beta": "QUARTZITE", "rs_draft": "MARZIPAN"}
DRAFT_TEXT = "this is an unsent draft the restart must not clobber"


#: The harness's own pane reader: always this checkout's script, never the one under test,
#: so a control run of an older copy is judged by the same oracle.
ORACLE = REPO / "scripts" / "claude-restart-sessions"


def load_script():
    """The oracle's module (its ``has_draft`` judges the preconditions)."""
    loader = importlib.machinery.SourceFileLoader("claude_restart_sessions", str(ORACLE))
    spec = importlib.util.spec_from_loader(loader.name, loader)
    assert spec is not None
    module = importlib.util.module_from_spec(spec)
    loader.exec_module(module)
    return module


class Sandbox:
    def __init__(self, home: Path, sock: str, claude: str, model: str) -> None:
        self.home, self.sock, self.claude, self.model = home, sock, claude, model
        self.work = home / "work"
        self.sessions_dir = home / ".claude" / "sessions"
        self.log = home / ".claude" / "restart-sessions.log"
        self.env = {k: v for k, v in os.environ.items()
                    if k not in ("CLAUDECODE", "CLAUDE_CODE_ENTRYPOINT", "ANTHROPIC_API_KEY", "ANTHROPIC_AUTH_TOKEN",
                                 "ANTHROPIC_BASE_URL", "CLAUDE_CODE_USE_BEDROCK", "CLAUDE_CODE_USE_VERTEX",
                                 "CLAUDE_CODE_SSE_PORT", "TMUX", "TMUX_PANE", "CLAUDE_CONFIG_DIR", "NEXUS_CONFIG_DIR")}
        self.env.update(HOME=str(home), NX_NO_TELEMETRY="1", NX_RESTART_CLAUDE=claude, TERM="xterm-256color",
                        CLAUDE_CODE_ENABLE_PROMPT_SUGGESTION="1", CLAUDE_CODE_DISABLE_AUTO_MEMORY="1",
                        DISABLE_AUTOUPDATER="1")  # the dim ghost prompt that broke 7.92.0

    # tmux on the private server only
    def tmux(self, *args: str, check: bool = False) -> subprocess.CompletedProcess:
        return subprocess.run(["tmux", "-L", self.sock, "-f", "/dev/null", *args],
                              capture_output=True, text=True, env=self.env, check=check)

    def setup(self) -> None:
        (self.home / ".claude").mkdir(parents=True)
        self.work.mkdir()
        git = ["git", "-C", str(self.work), "-c", "user.name=t", "-c", "user.email=t@example.invalid",
               "-c", "commit.gpgsign=false"]
        subprocess.run([*git, "init", "-q"], check=True)
        (self.work / "README.md").write_text("restart sandbox\n", encoding="utf-8")
        subprocess.run([*git, "add", "README.md"], check=True)
        subprocess.run([*git, "commit", "-q", "-m", "root"], check=True)
        projects = {key: {"hasTrustDialogAccepted": True, "hasCompletedProjectOnboarding": True}
                    for key in {str(self.work), os.path.realpath(self.work)}}
        (self.home / ".claude.json").write_text(
            json.dumps({"hasCompletedOnboarding": True, "projects": projects}), encoding="utf-8")
        (self.home / ".claude" / "settings.json").write_text(
            json.dumps({"skipDangerousModePermissionPrompt": True, "promptSuggestionEnabled": True,
                        "autoMemoryEnabled": False}),
            encoding="utf-8")
        self.tmux("new-session", "-d", "-s", "rs", "-x", "220", "-y", "50", "-n", "ctl", "-c", str(self.work),
                  "bash --noprofile --norc", check=True)

    def start_session(self, name: str) -> None:
        self.tmux("new-window", "-d", "-t", "rs:", "-n", name, "-c", str(self.work), "bash --noprofile --norc",
                  check=True)
        cmd = f"{self.claude} --model {self.model} --dangerously-skip-permissions -n {name}"
        self.tmux("send-keys", "-t", f"rs:{name}", "-l", cmd, check=True)
        self.tmux("send-keys", "-t", f"rs:{name}", "Enter", check=True)

    def entries(self) -> list[dict]:
        out = []
        for p in sorted(self.sessions_dir.glob("*.json")):
            try:
                d = json.loads(p.read_text(encoding="utf-8"))
            except (OSError, ValueError):
                continue
            pid = int(d.get("pid", 0) or 0)
            try:
                os.kill(pid, 0)
            except (ProcessLookupError, ValueError):
                continue
            except PermissionError:
                pass
            out.append(d)
        return out

    def session(self, name: str) -> dict | None:
        live = [d for d in self.entries() if d.get("name") == name]
        return max(live, key=lambda d: d.get("statusUpdatedAt") or 0) if live else None

    def wait_status(self, name: str, want: set[str], timeout: float, not_sid: str = "") -> dict:
        deadline = time.time() + timeout
        while time.time() < deadline:
            d = self.session(name)
            if d and d.get("status") in want and d.get("sessionId") != not_sid:
                return d
            time.sleep(1)
        raise TimeoutError(f"{name}: not {sorted(want)} within {timeout:.0f}s (last {self.session(name)})")

    def pane_of(self, name: str) -> str:
        return f"rs:{name}"

    def prompt(self, name: str, text: str, timeout: float = 240) -> None:
        """Type a prompt, Enter, then wait for the turn to start and end."""
        d0 = self.wait_status(name, {"idle"}, 120)
        self.tmux("send-keys", "-t", self.pane_of(name), "-l", text, check=True)
        time.sleep(1)
        self.tmux("send-keys", "-t", self.pane_of(name), "Enter", check=True)
        deadline = time.time() + timeout
        seen_busy = False
        while time.time() < deadline:
            d = self.session(name) or {}
            if d.get("status") == "busy":
                seen_busy = True
            elif seen_busy and d.get("status") == "idle":
                return
            elif not seen_busy and d.get("statusUpdatedAt") != d0.get("statusUpdatedAt") and d.get("status") == "idle":
                return
            time.sleep(1)
        raise TimeoutError(f"{name}: prompt did not finish within {timeout:.0f}s")

    def capture(self, name: str) -> str:
        return self.tmux("capture-pane", "-e", "-p", "-t", self.pane_of(name)).stdout

    def events(self, session_id: str) -> list[dict]:
        out: list[dict] = []
        for f in (self.home / ".claude" / "projects").glob(f"*/{session_id}.jsonl"):
            for line in f.read_text(encoding="utf-8", errors="replace").splitlines():
                try:
                    out.append(json.loads(line))
                except ValueError:
                    continue
        return out

    def first_mention(self, session_id: str, word: str) -> str:
        """Where *word* first appears in a session's transcript: ``tool_result``, ``user``,
        ``assistant``, another event type, or ``""`` when it never does."""
        for d in self.events(session_id):
            if word not in json.dumps(d):
                continue
            content = (d.get("message") or {}).get("content")
            if isinstance(content, list) and any(isinstance(c, dict) and c.get("type") == "tool_result"
                                                 and word in json.dumps(c) for c in content):
                return "tool_result"
            return str(d.get("type") or "unknown")
        return ""

    def memory_files_with(self, words: list[str]) -> list[str]:
        """Files a later session could read a word from: anything in the scratch HOME or the
        work folder except the transcripts and session registry themselves."""
        hits = []
        for root in (self.home,):
            for f in root.rglob("*"):
                rel = f.relative_to(self.home).as_posix()
                if not f.is_file() or rel.endswith(".jsonl") or rel.startswith(".claude/sessions/") \
                        or rel.startswith(".claude/restart-sessions.log") or f.stat().st_size > 2_000_000:
                    continue
                try:
                    text = f.read_text(encoding="utf-8", errors="ignore")
                except OSError:
                    continue
                hits += [f"{rel}: {w}" for w in words if w in text]
        return hits

    def last_reply(self, session_id: str) -> str:
        for f in (self.home / ".claude" / "projects").glob(f"*/{session_id}.jsonl"):
            text = ""
            for line in f.read_text(encoding="utf-8", errors="replace").splitlines():
                try:
                    d = json.loads(line)
                except ValueError:
                    continue
                msg = d.get("message") or {}
                if d.get("type") == "assistant" and isinstance(msg.get("content"), list):
                    parts = [c.get("text", "") for c in msg["content"] if isinstance(c, dict) and c.get("type") == "text"]
                    if any(parts):
                        text = "".join(parts)
            return text
        return ""


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--model", default="sonnet")
    ap.add_argument("--keep", action="store_true", help="keep the scratch HOME and tmux server even on a pass")
    ap.add_argument("--timeout", type=int, default=1200, help="seconds for the whole restart to finish")
    a = ap.parse_args()

    if not os.environ.get("CLAUDE_CODE_OAUTH_TOKEN"):
        print("RESTART SANDBOX UNVERIFIED: no automation token in the environment; run through run.sh", flush=True)
        return 2
    claude = shutil.which("claude")
    if not claude or not shutil.which("tmux"):
        print("RESTART SANDBOX UNVERIFIED: claude or tmux not on PATH", flush=True)
        return 2

    # A SIGTERM must still reach the finally below: the private tmux server holds the token in
    # its environment and must not outlive the run.
    signal.signal(signal.SIGTERM, lambda *_: sys.exit(143))
    home = Path(tempfile.mkdtemp(prefix="nx-restart-sandbox-"))
    sb = Sandbox(home, f"nx-rs-{os.getpid()}", claude, a.model)
    failures: list[str] = []
    notes: list[str] = []
    before: dict[str, dict] = {}
    ok = False
    try:
        sb.setup()
        print(f"[sandbox] HOME {home}; watch: tmux -L {sb.sock} attach -t rs", flush=True)
        for name in WORDS:
            sb.start_session(name)
        for name in WORDS:
            sb.wait_status(name, {"idle"}, 120)
        for name, word in WORDS.items():
            sb.prompt(name, f"For this task, the project code word is {word}. Reply with only: OK")
            d = sb.session(name)
            if not d or not d.get("tmux"):
                failures.append(f"{name}: its session file has no tmux pane; the script cannot see it")
            before[name] = d or {}
        crs = load_script()
        # A turn that invites a follow-up, so Claude Code draws a suggested next prompt (dim text):
        # the case the old check read as a draft. Poll for it rather than sleep and hope.
        ghosts = 0
        for name in ("rs_alpha", "rs_beta"):
            sb.prompt(name, "List two small improvements you could make to README.md in this repo. Do not make them.")
        for name in ("rs_alpha", "rs_beta"):
            deadline = time.time() + 30
            while time.time() < deadline:
                line = crs.prompt_line(sb.capture(name)) or ""
                shown = re.sub(r"\x1b\[[0-9;:]*m", "", line).replace("\xa0", " ").split("\u276f", 1)[-1].strip()
                if "\x1b[2m" in line and shown and not crs.typed_text(line).replace("\xa0", " ").split("\u276f", 1)[-1].strip():
                    ghosts += 1  # dim text after the mark on the input line, and nothing typed
                    break
                time.sleep(1)
            if crs.has_draft(sb.capture(name)):
                failures.append(f"precondition: {name} reads as holding a draft with nothing typed")
        notes.append(f"suggested prompt showing just before the restart: {ghosts} of 2 clean sessions")
        if ghosts == 0:
            failures.append("non-vacuity: no clean session showed a suggested prompt, so the ghost case was not exercised")
        sb.tmux("send-keys", "-t", sb.pane_of("rs_draft"), "-l", DRAFT_TEXT, check=True)
        time.sleep(2)
        if not crs.has_draft(sb.capture("rs_draft")):
            failures.append("precondition: rs_draft's typed draft is not read as a draft")
        if failures:
            return 1

        t0 = time.time()
        sb.tmux("send-keys", "-t", "rs:ctl", "-l",
                f"{shlex.quote(sys.executable)} -I {shlex.quote(str(SCRIPT))} --fresh --fallback-resume --no-update "
                "--handoff-timeout 600 --wait 300",
                check=True)
        sb.tmux("send-keys", "-t", "rs:ctl", "Enter", check=True)
        while time.time() - t0 < a.timeout:
            text = sb.log.read_text(encoding="utf-8") if sb.log.exists() else ""
            if "=== done:" in text or "=== ABORT" in text or "=== CRASH" in text:
                break
            time.sleep(5)
        log = sb.log.read_text(encoding="utf-8") if sb.log.exists() else ""
        print("[sandbox] restart log:\n" + "\n".join("    " + ln for ln in log.splitlines()), flush=True)
        expect = [
            "handoff written by rs_alpha", "handoff written by rs_beta",
            "SKIP rs_draft: unsent text in its prompt",
            "rs_draft: no handoff; will restart with --resume (fallback)",
            "OK rs_alpha: fresh as pid", "OK rs_beta: fresh as pid", "OK rs_draft: resumed as pid",
            "=== done: 3 of 3 resumed",
        ]
        for line in expect:
            if line not in log:
                failures.append(f"log lacks: {line}")
        for bad in ("SKIP rs_alpha", "SKIP rs_beta", "WARN", "FAIL", "CRASH", "ABORT"):
            if bad in log:
                failures.append(f"log has: {bad}")

        handoffs = dict(re.findall(r"handoff written by (\S+): (\S+) \(", log))
        if "rs_draft" in handoffs or Path(f"/tmp/rh-{before['rs_draft'].get('sessionId', '')[:8]}.txt").exists():
            failures.append("rs_draft: a handoff was asked of a session holding a draft")
        for name, word in WORDS.items():
            others = [w for n, w in WORDS.items() if n != name]
            old = before[name]["sessionId"]
            fresh = name != "rs_draft"
            if fresh and f"OK {name}: fresh as pid" not in log:
                continue  # already failed above; waiting for a fresh start the log says never came is time spent for nothing
            if fresh:
                body = Path(handoffs.get(name, "/nonexistent")).read_text(encoding="utf-8", errors="replace") \
                    if Path(handoffs.get(name, "/nonexistent")).exists() else ""
                if word not in body:
                    failures.append(f"{name}: its handoff {handoffs.get(name)} does not carry {word}")
                if any(w in body for w in others):
                    failures.append(f"{name}: its handoff carries another session's word")
            try:
                d = sb.wait_status(name, {"idle"}, 300, not_sid=old if fresh else "")
            except TimeoutError as exc:
                failures.append(str(exc))
                continue
            if fresh and d["sessionId"] == old:
                failures.append(f"{name}: kept session id {old[:8]}; expected a fresh start")
            if not fresh and d["sessionId"] != old:
                failures.append(f"{name}: session id changed to {d['sessionId'][:8]}; expected a resume")
            try:
                sb.prompt(name, "What is the project code word for this task? Reply with only the word.")
            except TimeoutError as exc:
                failures.append(str(exc))
                continue
            reply = sb.last_reply(d["sessionId"]).upper()
            source = sb.first_mention(d["sessionId"], word) if fresh else "resumed context"
            if word not in reply:
                failures.append(f"{name}: asked for its code word after the restart, replied {reply[:80]!r}")
            elif any(w in reply for w in others):
                failures.append(f"{name}: replied with another session's word: {reply[:80]!r}")
            elif fresh and source != "tool_result":
                failures.append(f"{name}: {word} first appears in its fresh transcript as {source or 'nothing'}, "
                                "not in the result of reading its handoff")
            else:
                notes.append(f"{name}: {'fresh' if fresh else 'resumed'} as {d['sessionId'][:8]}, "
                             f"answered {word} (first seen in: {source})")
        leaks = sb.memory_files_with(list(WORDS.values()))
        handoff_paths = set(handoffs.values())
        leaks = [h for h in leaks if not any(h.startswith(p) for p in handoff_paths)]
        if leaks:
            failures.append(f"a code word is on disk where a fresh session could read it: {leaks[:3]}")
        # Negative control: a fresh session in the same folder with no handoff must not know any word.
        sb.start_session("rs_blank")
        sb.wait_status("rs_blank", {"idle"}, 120)
        sb.prompt("rs_blank", "What is the project code word for this task? If you do not know it, reply with only: UNKNOWN")
        blank = sb.session("rs_blank") or {}
        blank_reply = sb.last_reply(str(blank.get("sessionId", ""))).upper()
        if any(w in blank_reply for w in WORDS.values()):
            failures.append(f"negative control: a fresh session with no handoff knew a word: {blank_reply[:80]!r}")
        else:
            notes.append(f"negative control: a fresh session with no handoff replied {blank_reply[:40]!r}")
        notes.append(f"restart took {time.time() - t0:.0f}s")
        ok = not failures
        return 0 if ok else 1
    except Exception as exc:  # noqa: BLE001 -- a harness fault is a failed run with its reason
        failures.append(f"harness: {type(exc).__name__}: {exc}")
        return 1
    finally:
        for n in notes:
            print(f"[sandbox] {n}", flush=True)
        for f in failures:
            print(f"[sandbox] FAIL {f}", flush=True)
        verdict = "RESTART SANDBOX PASSED" if ok else "RESTART SANDBOX FAILED"
        print(f"{verdict} -- 3 sessions and a no-handoff control, {len(failures)} failure(s)", flush=True)
        sids = {str(d.get("sessionId", ""))[:8] for d in sb.entries()}
        sids |= {str(d.get("sessionId", ""))[:8] for d in before.values() if d}
        if not a.keep:
            sb.tmux("kill-server")  # always: ours alone (-L), and it holds the token; its claude processes end with it
        if ok and not a.keep:
            for sid in filter(None, sids):  # the script's /tmp files for these sessions
                for f in Path("/tmp").glob(f"rh-{sid}.*"):
                    f.unlink(missing_ok=True)
                for f in Path("/tmp").glob(f"nexus-continuation-restart-rs_*-{sid}-*.md"):
                    f.unlink(missing_ok=True)
            shutil.rmtree(home, ignore_errors=True)
        else:
            print(f"[sandbox] kept {home} (transcripts, restart log, handoffs under /tmp)"
                  + (f"; tmux server {sb.sock} left running: tmux -L {sb.sock} kill-server when done" if a.keep else ""),
                  flush=True)


if __name__ == "__main__":
    sys.exit(main())
