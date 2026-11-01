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
``cat <handoff>``; draft is skipped for its draft and resumed with its old id; all
three then answer their code word, which a fresh session can know only from the
handoff it was fed. A suggested prompt must not count as a draft.

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
import shutil
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


def load_script():
    loader = importlib.machinery.SourceFileLoader("claude_restart_sessions", str(SCRIPT))
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
                    if k not in ("CLAUDECODE", "CLAUDE_CODE_ENTRYPOINT", "ANTHROPIC_API_KEY", "TMUX", "TMUX_PANE",
                                 "CLAUDE_CONFIG_DIR", "NEXUS_CONFIG_DIR")}
        self.env.update(HOME=str(home), NX_NO_TELEMETRY="1", NX_RESTART_CLAUDE=claude, TERM="xterm-256color",
                        CLAUDE_CODE_ENABLE_PROMPT_SUGGESTION="1")  # the dim ghost prompt that broke 7.92.0

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
            json.dumps({"skipDangerousModePermissionPrompt": True, "promptSuggestionEnabled": True}),
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
            sb.prompt(name, f"Remember this code word for later: {word}. Reply with only: OK")
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
                if any("❯" in ln and "\x1b[2m" in ln for ln in sb.capture(name).splitlines()):
                    ghosts += 1
                    break
                time.sleep(1)
            if crs.has_draft(sb.capture(name)):
                failures.append(f"precondition: {name} reads as holding a draft with nothing typed")
        notes.append(f"suggested prompt showing at restart time: {ghosts} of 2 clean sessions")
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
                f"{sys.executable} -I {SCRIPT} --fresh --fallback-resume --no-update --handoff-timeout 600 --wait 300",
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

        for name, word in WORDS.items():
            old = before[name]["sessionId"]
            try:
                fresh = name != "rs_draft"
                d = sb.wait_status(name, {"idle"}, 300, not_sid=old if fresh else "")
            except TimeoutError as exc:
                failures.append(str(exc))
                continue
            if fresh and d["sessionId"] == old:
                failures.append(f"{name}: kept session id {old[:8]}; expected a fresh start")
            if not fresh and d["sessionId"] != old:
                failures.append(f"{name}: session id changed to {d['sessionId'][:8]}; expected a resume")
            try:
                sb.prompt(name, "What was the code word you were asked to remember? Reply with only the word.")
            except TimeoutError as exc:
                failures.append(str(exc))
                continue
            reply = sb.last_reply(d["sessionId"])
            if word not in reply.upper():
                failures.append(f"{name}: asked for its code word after the restart, replied {reply[:80]!r}")
            else:
                notes.append(f"{name}: {'fresh' if fresh else 'resumed'} as {d['sessionId'][:8]}, remembered {word}")
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
        print(f"{verdict} -- 3 sessions, {len(failures)} failure(s)", flush=True)
        if ok and not a.keep:
            sids = {str(d.get("sessionId", ""))[:8] for d in sb.entries()}
            sids |= {str(d.get("sessionId", ""))[:8] for d in before.values() if d}
            sb.tmux("kill-server")  # ours alone: the -L socket; its panes' claude processes end with it
            for sid in filter(None, sids):  # the script's /tmp files for these sessions
                for f in Path("/tmp").glob(f"rh-{sid}.*"):
                    f.unlink(missing_ok=True)
                for f in Path("/tmp").glob(f"nexus-continuation-restart-rs_*-{sid}-*.md"):
                    f.unlink(missing_ok=True)
            shutil.rmtree(home, ignore_errors=True)
        else:
            print(f"[sandbox] kept {home} and tmux server {sb.sock} (tmux -L {sb.sock} attach -t rs; "
                  f"tmux -L {sb.sock} kill-server when done)", flush=True)


if __name__ == "__main__":
    sys.exit(main())
