# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (c) 2026 Hal Hildebrand. All rights reserved.
"""Sandbox gate for scripts/claude-restart-sessions --fresh (nexus-nuyju, nexus-cmqe8).

Drives real Claude Code sessions through the coordinated-restart continuation roll and
checks every outcome, with nothing written to the operator's ~/.claude:

- a scratch HOME (``.claude.json`` with onboarding done and the work folder trusted),
  with the conexus plugin enabled from the operator's installed plugin cache (read only)
  and a scratch nexus config dir holding a copy of the operator's ``config.yml``, so the
  sessions' MCP servers and ``nx`` reach the operator's own service. The copy and the
  data-token leases minted from it are deleted on every exit the harness sees (pass, fail,
  SIGTERM, SIGHUP; not SIGKILL, and not under ``--keep``). The sessions write to that
  service as the operator would: the restart mail expires with its TTL, and nothing here
  asks a session to write anything permanent, but the harness does not police it;
- a private tmux server (``-L``, ``-f /dev/null``, a bare bash in every pane);
- six sessions in a scratch git repo. Three restart, each told a code word:
  ``rs_alpha`` (launched with the conexus channel, left at its prompt with whatever
  suggested prompt Claude Code draws in dim text), ``rs_draft`` (the channel, with a
  real unsent draft typed into its prompt) and ``rs_nochan`` (no channel, left at its
  prompt). Three must be left running (nexus-8m0ho): ``rs_busy`` (mid-turn in a
  foreground sleep past both of the script's waits), ``rs_shell`` (at its prompt with a
  background shell open, status ``shell``) and ``rs_paste`` (a bracketed paste in its
  prompt, shown as a ``[Pasted text #N]`` placeholder, which is a draft and not dim text);
- the conexus and sn plugins come from a local-directory marketplace named
  nexus-plugins, copied from the operator's installed cache (read only) and installed
  with the claude CLI;
- the script under test runs from a fourth pane as ``--nx X --fresh --skip-engine-gate
  --fallback-resume --no-update`` with ``TMUX_PANE`` set to rs_alpha's pane, so rs_alpha is
  the caller and must go last. X is the installed version's next patch; ``--repo`` names a
  scratch release repo whose tag vX holds the engine floor and whose reinstall-tool.sh is
  a stand-in that publishes X to the sandbox marketplace and makes the nx on PATH report X
  (everything else goes to the real nx). The release install is the one step not real.

Expected: alpha, draft and paste are asked by mail and nochan by typing; all four write a
handoff; the install runs after the last handoff and before the first exit, and moves
both plugins to X; alpha, draft and nochan restart with a new session id whose own argv
carries ``-- cat <handoff>`` and whose MCP servers run conexus X (they ran the old
version before); busy and shell are skipped at the handoff and again at the restart,
paste is skipped at its restart for its placeholder, and all three are the same process
on the same session afterwards; alpha's exit comes after every other session's outcome;
neither mailed session's old transcript holds a typed ask or the draft as a prompt; the
draft is still in rs_draft's prompt from its complete handoff until its exit, is saved at
the restart, and is read by the fresh rs_draft with its handoff; all three then answer
their own code word and no other. The draft cannot outlive the exit in the input box
itself: exiting Claude Code clears it, so "survives" means saved and handed on.

Continuity is proved from the handoff, not merely observed. Auto memory is off
(``CLAUDE_CODE_DISABLE_AUTO_MEMORY``, ``autoMemoryEnabled: false``) and the word is given
as task context, not "remember"; each fresh session's word must be in its handoff file,
must first appear in its transcript inside a tool result (the read of the handoff), and
no memory file in the scratch HOME may hold any word. A negative control, a fourth fresh
session in the same folder with no handoff, must not know any word.

Positive control: ``NX_RESTART_SANDBOX_SCRIPT=<copy>`` runs another copy of the script;
the pane checks always use this checkout's reader. The pre-cmqe8 copy (develop before
nexus-cmqe8) fails here. The discriminating checks are rs_draft's (skipped for its
draft and resumed instead of a fresh start) and the argv check (it typed ``cat`` into the
fresh sessions); its missing "by mail" log lines would fail any older copy and prove
nothing on their own.

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
WORDS = {"rs_alpha": "HELIOTROPE", "rs_draft": "MARZIPAN", "rs_nochan": "QUARTZITE"}
#: Sessions the roll must leave running: mid-turn (busy), at the prompt with a background shell
#: open (shell), and holding a pasted-text placeholder as its draft (paste) (nexus-8m0ho).
HELD = ("rs_busy", "rs_shell", "rs_paste")
SESSIONS = (*WORDS, *HELD)
#: Sessions launched with the conexus channel; the rest are asked by typing.
CHANNEL = {"rs_alpha", "rs_draft", *HELD}
#: The session the script is started from (its TMUX_PANE): it must go last.
CALLER = "rs_alpha"
CHANNEL_FLAGS = "--channels plugin:conexus@nexus-plugins"
MARKET = "nexus-plugins"
#: Every plugin the script's --nx install checks (its PLUGINS); the sandbox marketplace holds both.
MARKET_PLUGINS = ("conexus", "sn")
DRAFT_TEXT = "this is an unsent draft the restart must not clobber"
#: Pasted with bracketed paste: Claude Code shows a paste this long as a [Pasted text #N] placeholder.
PASTE_TEXT = "\n".join(f"pasted line {i} the restart cannot save from the pane" for i in range(1, 21))
#: --wait for the roll: how long rs_busy and rs_shell are waited for at each phase.
WAIT = 120
#: rs_busy's foreground sleep: past both of its waits (handoff and restart) and the install.
BUSY_SLEEP = 560
OPERATOR_HOME = Path.home()
SANDBOX_PY = Path(__file__).resolve()


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


def bump_market(market: Path, version: str) -> None:
    """Publish *version* to the sandbox marketplace: each plugin as its own folder ``<plugin>-<version>``
    copied from ``base/<plugin>`` with that version in its plugin.json, and the manifest pointing there.

    A plugin from a directory marketplace runs from its source folder, not the versioned cache
    (Claude Code 2.1.316), so one folder per version is what lets the folder a session's MCP servers
    run from name the version it loaded; the old folder stays for the sessions still on it."""
    for p in MARKET_PLUGINS:
        dest = market / f"{p}-{version}"
        if not dest.exists():
            shutil.copytree(market / "base" / p, dest, symlinks=True)
        f = dest / ".claude-plugin" / "plugin.json"
        d = json.loads(f.read_text(encoding="utf-8"))
        d["version"] = version
        f.write_text(json.dumps(d, indent=2) + "\n", encoding="utf-8")
    (market / ".claude-plugin" / "marketplace.json").write_text(json.dumps(
        {"name": MARKET, "owner": {"name": "restart sandbox"},
         "plugins": [{"name": p, "source": f"./{p}-{version}", "version": version} for p in MARKET_PLUGINS]}),
        encoding="utf-8")


class Sandbox:
    def __init__(self, home: Path, sock: str, claude: str, model: str) -> None:
        self.home, self.sock, self.claude, self.model = home, sock, claude, model
        self.work = home / "work"
        self.sessions_dir = home / ".claude" / "sessions"
        self.log = home / ".claude" / "restart-sessions.log"
        self.nexus_config = home / ".config" / "nexus"
        # The operator's session identity must not leak in: a launching session's markers make
        # Claude Code treat the sandbox sessions as its children (no session file, no transcript),
        # and NX_SESSION_ID / NX_T1_SESSION_ID would point their nx at the operator's session.
        drop = ("CLAUDECODE", "CLAUDE_CODE_ENTRYPOINT", "ANTHROPIC_API_KEY", "ANTHROPIC_AUTH_TOKEN",
                "ANTHROPIC_BASE_URL", "CLAUDE_CODE_USE_BEDROCK", "CLAUDE_CODE_USE_VERTEX",
                "CLAUDE_CODE_SSE_PORT", "TMUX", "TMUX_PANE", "CLAUDE_CONFIG_DIR", "NEXUS_CONFIG_DIR",
                "CLAUDE_CODE_CHILD_SESSION", "CLAUDE_CODE_SESSION_ID", "CLAUDE_CODE_SESSION_ATTENDED",
                "CLAUDE_CODE_MESSAGING_SOCKET", "CLAUDE_CODE_MESSAGING_TOKEN", "CLAUDE_PID",
                "NX_SESSION_ID", "NX_T1_SESSION_ID")
        self.env = {k: v for k, v in os.environ.items() if k not in drop}
        uv_dirs = {"UV_CACHE_DIR": OPERATOR_HOME / ".cache" / "uv",
                   "UV_PYTHON_INSTALL_DIR": OPERATOR_HOME / ".local" / "share" / "uv" / "python",
                   "UV_TOOL_DIR": OPERATOR_HOME / ".local" / "share" / "uv" / "tools"}
        for k, v in uv_dirs.items():  # the plugin's MCP servers start through uv; do not refetch Python
            if v.is_dir():
                self.env.setdefault(k, str(v))
        self.env.update(HOME=str(home), NX_NO_TELEMETRY="1", NX_RESTART_CLAUDE=claude, TERM="xterm-256color",
                        CLAUDE_CODE_ENABLE_PROMPT_SUGGESTION="1", CLAUDE_CODE_DISABLE_AUTO_MEMORY="1",
                        DISABLE_AUTOUPDATER="1")  # the dim ghost prompt that broke 7.92.0
        # --nx installs a release: the sandbox's own stand-ins (setup_release) take its place.
        self.market = home / "market"
        self.relrepo = home / "release-repo"
        self.bin = home / "bin"
        self.installed_marker = home / ".nx-sandbox-installed"
        self.real_nx = shutil.which("nx") or ""
        self.real_uv = shutil.which("uv") or ""
        self.uv_cleans = home / "uv-cache-clean.log"
        self.env["PATH"] = f"{self.bin}{os.pathsep}{self.env.get('PATH', '')}"
        self.from_version = self.to_version = ""

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
                        "autoMemoryEnabled": False}), encoding="utf-8")
        self.setup_market()
        self.setup_release()
        # The operator's nexus config, copied (never printed) so nx and the MCP servers reach the
        # same service; delete_config() removes it in every outcome.
        self.nexus_config.mkdir(parents=True)
        shutil.copy2(OPERATOR_HOME / ".config" / "nexus" / "config.yml", self.nexus_config / "config.yml")
        os.chmod(self.nexus_config / "config.yml", 0o600)
        self.tmux("new-session", "-d", "-s", "rs", "-x", "220", "-y", "50", "-n", "ctl", "-c", str(self.work),
                  "bash --noprofile --norc", check=True)

    def claude_cli(self, *args: str) -> None:
        r = subprocess.run([self.claude, *args], capture_output=True, text=True, env=self.env)
        if r.returncode != 0:
            raise RuntimeError(f"claude {' '.join(args)}: exit {r.returncode}: {(r.stdout + r.stderr).strip()[-300:]}")

    def setup_market(self) -> None:
        """A local-directory marketplace named nexus-plugins holding copies of the operator's installed
        conexus and sn (read only), installed into the scratch HOME through the claude CLI, so the
        script's --nx install has a marketplace it can move to a new version."""
        plugins = json.loads((OPERATOR_HOME / ".claude" / "plugins" / "installed_plugins.json").read_text(encoding="utf-8"))
        plugins = plugins.get("plugins", plugins)
        (self.market / ".claude-plugin").mkdir(parents=True)
        versions = set()
        for p in MARKET_PLUGINS:
            entry = plugins[f"{p}@{MARKET}"]
            entry = entry[0] if isinstance(entry, list) else entry
            shutil.copytree(entry["installPath"], self.market / "base" / p, symlinks=True)
            versions.add(str(entry["version"]))
        if len(versions) != 1:
            raise RuntimeError(f"the operator's conexus and sn differ in version ({sorted(versions)})")
        self.from_version = versions.pop()
        major, minor, patch = (int(x) for x in self.from_version.split(".")[:3])
        self.to_version = f"{major}.{minor}.{patch + 1}"
        bump_market(self.market, self.from_version)
        self.claude_cli("plugin", "marketplace", "add", str(self.market))
        for p in MARKET_PLUGINS:
            self.claude_cli("plugin", "install", f"{p}@{MARKET}", "--scope", "user")

    def setup_release(self) -> None:
        """Stand-ins for what --nx X touches outside the sandbox: a release repo whose tag vX holds the
        engine floor, a reinstall-tool.sh that records the install and publishes X to the sandbox
        marketplace, and an nx on PATH that reports X once installed (anything else goes to the real nx)."""
        r = self.relrepo
        (r / "src" / "nexus").mkdir(parents=True)
        (r / "scripts").mkdir()
        shutil.copy2(REPO / "src" / "nexus" / "engine_version.py", r / "src" / "nexus" / "engine_version.py")
        stub = r / "scripts" / "reinstall-tool.sh"
        stub.write_text(
            "#!/bin/sh\n# restart sandbox stand-in: record the install, publish the version to the sandbox marketplace\n"
            'v="${1#conexus==}"\n'
            f'printf "%s\\n" "$v" > {shlex.quote(str(self.installed_marker))}\n'
            f'exec {shlex.quote(sys.executable)} -I {shlex.quote(str(SANDBOX_PY))} --bump-market '
            f'{shlex.quote(str(self.market))} "$v"\n', encoding="utf-8")
        stub.chmod(0o755)
        git = ["git", "-C", str(r), "-c", "user.name=t", "-c", "user.email=t@example.invalid", "-c", "commit.gpgsign=false"]
        subprocess.run([*git, "init", "-q"], check=True)
        subprocess.run([*git, "add", "."], check=True)
        subprocess.run([*git, "commit", "-q", "-m", "release"], check=True)
        subprocess.run([*git, "tag", f"v{self.to_version}"], check=True)
        if not self.real_nx:
            raise RuntimeError("nx is not on PATH")
        self.bin.mkdir()
        shim = self.bin / "nx"
        marker = shlex.quote(str(self.installed_marker))
        shim.write_text(
            "#!/bin/sh\n# restart sandbox: after the stand-in install, --version reports the installed release\n"
            f'if [ "$1" = "--version" ] && [ -f {marker} ]; then echo "nx, version $(cat {marker})"; exit 0; fi\n'
            f"exec {shlex.quote(self.real_nx)} \"$@\"\n", encoding="utf-8")
        shim.chmod(0o755)
        # The script cleans uv's cached conexus before installing; in the detached child that runs
        # with the tmux server's environment, whose UV_CACHE_DIR is the operator's. Record it instead.
        if not self.real_uv:
            raise RuntimeError("uv is not on PATH")
        uv = self.bin / "uv"
        uv.write_text(
            "#!/bin/sh\n# restart sandbox: uv cache clean is recorded, never run on the operator's cache\n"
            f'if [ "$1" = "cache" ] && [ "$2" = "clean" ]; then echo "$*" >> {shlex.quote(str(self.uv_cleans))}; exit 0; fi\n'
            f"exec {shlex.quote(self.real_uv)} \"$@\"\n", encoding="utf-8")
        uv.chmod(0o755)

    def pane_id(self, name: str) -> str:
        return self.tmux("display-message", "-p", "-t", self.pane_of(name), "#{pane_id}", check=True).stdout.strip()

    def plugin_roots(self, pid: int, wait: float = 30) -> set[str]:
        """The conexus versions the process's descendants run from (its MCP servers' --directory, a
        ``market/conexus-<version>`` folder),
        read until one shows or *wait* seconds pass; empty means none did."""
        deadline = time.time() + wait
        while True:
            found = self._plugin_roots(pid)
            if found or time.time() >= deadline:
                return found
            time.sleep(3)

    def children(self, pid: int) -> list[str]:
        """The process's direct children, by command name only: a diagnostic line for a failure."""
        rows = subprocess.run(["ps", "-A", "-o", "ppid=,comm="], capture_output=True, text=True).stdout
        return [r.split(None, 1)[1] for r in rows.splitlines()
                if len(r.split(None, 1)) == 2 and r.split(None, 1)[0] == str(pid)]

    def _plugin_roots(self, pid: int) -> set[str]:
        rows = subprocess.run(["ps", "-A", "-ww", "-o", "pid=,ppid=,args="], capture_output=True, text=True).stdout
        kids: dict[int, list[tuple[int, str]]] = {}
        for row in rows.splitlines():
            parts = row.split(None, 2)
            if len(parts) == 3 and parts[0].isdigit() and parts[1].isdigit():
                kids.setdefault(int(parts[1]), []).append((int(parts[0]), parts[2]))
        found, todo = set(), [pid]
        pat = re.compile(rf"{re.escape(str(self.market))}/conexus-([^/\s]+)(?:/|\s|$)")
        while todo:
            for child, args in kids.get(todo.pop(), []):
                found |= set(pat.findall(args))
                todo.append(child)
        return found

    def start_session(self, name: str) -> None:
        self.tmux("new-window", "-d", "-t", "rs:", "-n", name, "-c", str(self.work), "bash --noprofile --norc",
                  check=True)
        channel = f" {CHANNEL_FLAGS}" if name in CHANNEL else ""
        cmd = f"{self.claude} --model {self.model} --dangerously-skip-permissions{channel} -n {name}"
        self.tmux("send-keys", "-t", f"rs:{name}", "-l", cmd, check=True)
        self.tmux("send-keys", "-t", f"rs:{name}", "Enter", check=True)

    def delete_config(self) -> None:
        """Remove the copied nexus config and the data-token leases minted from it (credentials)."""
        (self.nexus_config / "config.yml").unlink(missing_ok=True)
        for f in self.nexus_config.glob("data_token_lease.*"):
            f.unlink(missing_ok=True)

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

    def prompt(self, name: str, text: str, timeout: float = 240, done: frozenset[str] = frozenset({"idle"})) -> None:
        """Type a prompt, Enter, then wait until the session is idle with a new text reply.

        Status alone is not the turn's end: with the conexus plugin loaded, hooks move a fresh
        session's status timestamp on their own, and an idle status read that way returned
        before the reply was written (nexus-cmqe8 sandbox, 2026-11-01).
        """
        d0 = self.wait_status(name, {"idle"}, 120)
        sid = str(d0.get("sessionId", ""))
        replies0 = self.reply_count(sid)
        self.tmux("send-keys", "-t", self.pane_of(name), "-l", text, check=True)
        time.sleep(1)
        self.tmux("send-keys", "-t", self.pane_of(name), "Enter", check=True)
        deadline = time.time() + timeout
        while time.time() < deadline:
            d = self.session(name) or {}
            if d.get("status") in done and self.reply_count(sid) > replies0:
                return
            time.sleep(1)
        raise TimeoutError(f"{name}: prompt did not finish within {timeout:.0f}s")

    def reply_count(self, session_id: str) -> int:
        """Assistant transcript entries that carry text, for the session."""
        n = 0
        for d in self.events(session_id):
            content = (d.get("message") or {}).get("content")
            if d.get("type") == "assistant" and isinstance(content, list) and any(
                    isinstance(c, dict) and c.get("type") == "text" and c.get("text") for c in content):
                n += 1
        return n

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

    def first_mention(self, session_id: str, word: str, handoff: str = "") -> str:
        """Where *word* first appears in a session's transcript: ``handoff_read`` (the result of
        a tool call whose input names *handoff* itself; the retired /tmp/rh-*.md short copy does
        not count), another ``tool_result``, ``user``, ``assistant``, or ``""`` when it never does."""
        reads: set[str] = set()
        for d in self.events(session_id):
            content = (d.get("message") or {}).get("content")
            items = content if isinstance(content, list) else []
            for c in items:
                if isinstance(c, dict) and c.get("type") == "tool_use":
                    if handoff and handoff in json.dumps(c.get("input")):
                        reads.add(str(c.get("id")))
            if word not in json.dumps(d):
                continue
            for c in items:
                if isinstance(c, dict) and c.get("type") == "tool_result" and word in json.dumps(c):
                    return "handoff_read" if str(c.get("tool_use_id")) in reads else "tool_result"
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
    if sys.argv[1:2] == ["--bump-market"]:  # the stand-in reinstall-tool.sh's publish step
        bump_market(Path(sys.argv[2]), sys.argv[3])
        return 0
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
    signal.signal(signal.SIGHUP, lambda *_: sys.exit(129))  # a closed terminal: same cleanup
    home = Path(tempfile.mkdtemp(prefix="nx-restart-sandbox-"))
    sb = Sandbox(home, f"nx-rs-{os.getpid()}", claude, a.model)
    failures: list[str] = []
    notes: list[str] = []
    before: dict[str, dict] = {}
    ok = False
    try:
        sb.setup()
        print(f"[sandbox] HOME {home}; watch: tmux -L {sb.sock} attach -t rs", flush=True)
        for name in SESSIONS:
            sb.start_session(name)
        for name in SESSIONS:
            sb.wait_status(name, {"idle"}, 120)
        for name, word in WORDS.items():
            sb.prompt(name, f"For this task, the project code word is {word}. Reply with only: OK")
        for name in SESSIONS:
            d = sb.session(name)
            if not d or not d.get("tmux"):
                failures.append(f"{name}: its session file has no tmux pane; the script cannot see it")
            before[name] = d or {}
        # Non-vacuity for the --nx check: before the roll the sessions run the version it moves from.
        roots0 = sb.plugin_roots(int(before[CALLER].get("pid", 0) or 0))
        if roots0 != {sb.from_version}:
            failures.append(f"precondition: {CALLER} runs conexus {sorted(roots0)}, not {sb.from_version} from the "
                            f"sandbox marketplace (pid {before[CALLER].get('pid')}, children "
                            f"{sb.children(int(before[CALLER].get('pid', 0) or 0))[:12]})")
        crs = load_script()
        # A turn that invites a follow-up, so Claude Code draws a suggested next prompt (dim text):
        # the case the old check read as a draft. Poll for it rather than sleep and hope.
        ghosts = 0
        clean = ("rs_alpha", "rs_nochan")
        for name in clean:
            sb.prompt(name, "List two small improvements you could make to README.md in this repo. Do not make them.")
        for name in clean:
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
        # A bracketed paste: Claude Code collapses it to a [Pasted text #N] placeholder, which is
        # a draft (not dim suggestion text) that the pane cannot hand on.
        sb.tmux("set-buffer", "-b", "rs-paste", PASTE_TEXT, check=True)
        sb.tmux("paste-buffer", "-p", "-d", "-b", "rs-paste", "-t", sb.pane_of("rs_paste"), check=True)
        time.sleep(2)
        paste_cap = sb.capture("rs_paste")
        if not crs.has_draft(paste_cap) or "[Pasted text #" not in crs.draft_text(paste_cap):
            failures.append("precondition: rs_paste's prompt does not read as a [Pasted text] draft")
        # A background shell left open: the session is at its prompt with status 'shell'.
        sb.prompt("rs_shell", "Use the Bash tool with run_in_background set to true to run exactly this command: sleep 1500. "
                  "Then reply with only: STARTED", done=frozenset({"idle", "shell"}))
        try:
            sb.wait_status("rs_shell", {"shell"}, 60)
        except TimeoutError as exc:
            failures.append(f"precondition: {exc}")
        # Mid-turn for the whole roll: a foreground command outlasting both of the script's waits.
        # Not a bare sleep: Claude Code's Bash tool refuses a standalone foreground sleep (2.1.316).
        sb.tmux("send-keys", "-t", sb.pane_of("rs_busy"), "-l",
                f"Use the Bash tool, in the foreground with timeout 600000, to run exactly this command: python3 -c 'import time; time.sleep({BUSY_SLEEP})'. "
                "Then reply with only: DONE", check=True)
        time.sleep(1)
        sb.tmux("send-keys", "-t", sb.pane_of("rs_busy"), "Enter", check=True)
        try:
            sb.wait_status("rs_busy", {"busy"}, 60)
            time.sleep(15)  # past the request: the sleep itself, not the model's turn, holds it busy
            sb.wait_status("rs_busy", {"busy"}, 5)
        except TimeoutError as exc:
            failures.append(f"precondition: {exc}")
        if failures:
            return 1

        t0 = time.time()
        # Started from rs_alpha's pane, as a session runs it: that session must go last.
        sb.tmux("send-keys", "-t", "rs:ctl", "-l",
                f"TMUX_PANE={shlex.quote(sb.pane_id(CALLER))} "
                f"{shlex.quote(sys.executable)} -I {shlex.quote(str(SCRIPT))} --nx {sb.to_version} --fresh "
                f"--skip-engine-gate --repo {shlex.quote(str(sb.relrepo))} --fallback-resume --no-update "
                f"--handoff-timeout 600 --wait {WAIT}",
                check=True)
        sb.tmux("send-keys", "-t", "rs:ctl", "Enter", check=True)
        # The draft must survive the ask: from the moment rs_draft's handoff file is complete until
        # the script exits that session, its prompt must still hold the draft. Sampled here while
        # the roll runs; the last sample before the exit is the one judged.
        draft_seen: bool | None = None
        draft_marker = f"HANDOFF-COMPLETE {before['rs_draft'].get('sessionId', '')}"
        draft_glob = f"nexus-continuation-restart-rs_draft-{before['rs_draft'].get('sessionId', '')[:8]}-*.md"
        while time.time() - t0 < a.timeout:
            text = sb.log.read_text(encoding="utf-8") if sb.log.exists() else ""
            if "exit rs_draft" not in text:
                for f in Path("/tmp").glob(draft_glob):
                    body = f.read_text(encoding="utf-8", errors="replace").strip()
                    if body.endswith(draft_marker):
                        seen = DRAFT_TEXT in crs.draft_text(sb.capture("rs_draft"))
                        # The exit may have started during the capture: such a sample proves nothing.
                        if "exit rs_draft" not in (sb.log.read_text(encoding="utf-8") if sb.log.exists() else ""):
                            draft_seen = seen
            if "=== done:" in text or "=== ABORT" in text or "=== CRASH" in text:
                break
            time.sleep(1)
        log = sb.log.read_text(encoding="utf-8") if sb.log.exists() else ""
        print("[sandbox] restart log:\n" + "\n".join("    " + ln for ln in log.splitlines()), flush=True)
        expect = [
            "handoff asked of rs_alpha by mail", "handoff asked of rs_draft by mail",
            "handoff asked of rs_nochan by typed (launched without the conexus channel)",
            "handoff written by rs_alpha", "handoff written by rs_draft", "handoff written by rs_nochan",
            "OK rs_alpha: fresh as pid", "OK rs_draft: fresh as pid", "OK rs_nochan: fresh as pid",
            "rs_draft: unsent draft saved to",
            "handoff asked of rs_paste by mail", "handoff written by rs_paste",
            f"engine gate skipped (v{sb.to_version} requires engine",
            f"reinstall-tool conexus=={sb.to_version}: exit 0", f"nx: nx, version {sb.to_version}",
            "plugins: {" + ", ".join(f"'{p}@{MARKET}': '{sb.to_version}'" for p in MARKET_PLUGINS) + "}",
            "=== done: 3 of 6 restarted (3 fresh, 0 resumed)",
        ]
        for name in ("rs_alpha", "rs_nochan"):
            if f"{name}: unsent draft saved to" in log:
                failures.append(f"{name}: a draft was saved for a session with nothing typed")
        for line in expect:
            if line not in log:
                failures.append(f"log lacks: {line}")
        # The held sessions' lines, exactly; no other line may name a failure word. The typed
        # re-asks after a mail ask name no failure word of their own, so they are banned by phrase.
        held_lines = {
            f"SKIP rs_busy: still busy after {WAIT}s; no handoff asked, not restarted",
            f"SKIP rs_shell: still shell after {WAIT}s; no handoff asked, not restarted",
            "rs_busy: no handoff; will restart with --resume (fallback)",
            "rs_shell: no handoff; will restart with --resume (fallback)",
            f"SKIP rs_busy: still busy after {WAIT}s; not restarted",
            f"SKIP rs_shell: still shell after {WAIT}s; not restarted",
            "SKIP rs_paste: its unsent draft holds pasted text or an image, which the pane shows only as a "
            "placeholder; left running, not restarted",
        }
        body_lines = [ln.split(" ", 1)[1] if " " in ln else ln for ln in log.splitlines()]
        for want in sorted(held_lines):
            if want not in body_lines:
                failures.append(f"log lacks: {want}")
        for ln in body_lines:
            if ln in held_lines:
                continue
            for bad in ("SKIP", "WARN", "FAIL", "CRASH", "ABORT", "fallback", "by typed (the mail", "by typed (its mail"):
                if bad in ln:
                    failures.append(f"log has: {ln[:160]}")
                    break
        # Each held session is left running: the same process on the same session, nothing exited.
        for name in HELD:
            now = sb.session(name) or {}
            if now.get("pid") != before[name].get("pid") or now.get("sessionId") != before[name].get("sessionId"):
                failures.append(f"{name}: not left running as it was (before pid {before[name].get('pid')}, now {now.get('pid')})")
            if f"exit {name} " in log:
                failures.append(f"{name}: the script exited it")
        if "[Pasted text #" not in crs.draft_text(sb.capture("rs_paste")):
            failures.append("rs_paste: its pasted draft is gone from its prompt")
        # The caller's pane goes last: after every other session's restart or final skip.
        caller_exit = next((i for i, ln in enumerate(body_lines) if ln.startswith(f"exit {CALLER} ")), -1)
        last_other = max((i for i, ln in enumerate(body_lines)
                          if re.match(r"(OK|SKIP) rs_(?!alpha)", ln) and "no handoff asked" not in ln), default=-1)
        if caller_exit < 0 or caller_exit < last_other:
            failures.append(f"{CALLER} (the caller's pane) was not exited last (line {caller_exit}, last other {last_other})")
        else:
            notes.append(f"{CALLER}, the caller's pane, exited after every other session's outcome")
        # --nx installs after the last handoff and before the first exit.
        last_handoff = max((i for i, ln in enumerate(body_lines) if ln.startswith("handoff written by ")), default=-1)
        first_exit = min((i for i, ln in enumerate(body_lines) if ln.startswith("exit rs_")), default=len(body_lines))
        installs = [i for i, ln in enumerate(body_lines) if ln.startswith("reinstall-tool ")] or [-1]
        cleans = sb.uv_cleans.read_text(encoding="utf-8").split() if sb.uv_cleans.exists() else []
        if cleans[:3] != ["cache", "clean", "conexus"]:
            failures.append(f"--nx: uv cache clean conexus was not run through the sandbox's uv ({cleans})")
        if not all(last_handoff < i < first_exit for i in installs):
            failures.append(f"--nx install at log line(s) {installs}, not only between the last handoff "
                            f"({last_handoff}) and the first exit ({first_exit})")
        if draft_seen is None:
            failures.append("rs_draft: its complete handoff was never seen before it was exited; draft not checked")
        elif not draft_seen:
            failures.append("rs_draft: its draft was gone from the prompt once its handoff was written")
        else:
            notes.append("rs_draft: draft still in its prompt after the mail ask and its handoff")
        # Nothing typed into a mailed session: its old transcript holds neither the draft nor the
        # typed path's ask line as a prompt.
        for name in sorted(CHANNEL):
            typed = [d for d in sb.events(before[name].get("sessionId", "")) if d.get("type") == "user"
                     and (DRAFT_TEXT in json.dumps(d.get("message")) or "Do what /tmp/rh-" in json.dumps(d.get("message")))]
            if typed:
                failures.append(f"{name}: its old transcript has the draft or a typed ask as a prompt")

        handoffs = dict(re.findall(r"handoff written by (\S+): (\S+) \(", log))
        for name, word in WORDS.items():
            others = [w for n, w in WORDS.items() if n != name]
            old = before[name]["sessionId"]
            fresh = True
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
            # The first prompt rode the command line: read the new process's own argv.
            argv = subprocess.run(["ps", "-o", "args=", "-p", str(d.get("pid", 0))], capture_output=True,
                                  text=True).stdout.strip()
            want = f"-- cat {handoffs.get(name, '?')}"
            if fresh and want not in argv:
                failures.append(f"{name}: the fresh process's argv lacks {want!r}")
            # The fresh process runs the release --nx installed, not the one it was asked on.
            roots = sb.plugin_roots(int(d.get("pid", 0) or 0))
            if roots != {sb.to_version}:
                failures.append(f"{name}: the fresh process runs conexus {sorted(roots)}, not {sb.to_version}")
            else:
                notes.append(f"{name}: the fresh process runs conexus {sb.to_version} (was {sb.from_version})")
            if name == "rs_draft":
                saw = any(isinstance(c, dict) and c.get("type") == "tool_result" and DRAFT_TEXT in json.dumps(c)
                          for e in sb.events(d["sessionId"])
                          for c in ((e.get("message") or {}).get("content") or [] if isinstance(
                              (e.get("message") or {}).get("content"), list) else []))
                if not saw:
                    failures.append("rs_draft: the fresh session never read its saved draft")
                else:
                    notes.append("rs_draft: the fresh session read its saved draft with its handoff")
            try:
                sb.prompt(name, "What is the project code word for this task? Reply with only the word.")
            except TimeoutError as exc:
                failures.append(str(exc))
                continue
            reply = sb.last_reply(d["sessionId"]).upper()
            source = sb.first_mention(d["sessionId"], word, handoffs.get(name, "")) if fresh else "resumed context"
            if word not in reply:
                failures.append(f"{name}: asked for its code word after the restart, replied {reply[:80]!r}")
            elif any(w in reply for w in others):
                failures.append(f"{name}: replied with another session's word: {reply[:80]!r}")
            elif fresh and source != "handoff_read":
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
        elif "UNKNOWN" not in blank_reply:
            failures.append(f"negative control: no usable reply from the no-handoff session ({blank_reply[:80]!r})")
        else:
            notes.append(f"negative control: a fresh session with no handoff replied {blank_reply[:40]!r}")
        notes.append(f"restart took {time.time() - t0:.0f}s")
        ok = not failures
        return 0 if ok else 1
    except Exception as exc:  # noqa: BLE001 -- a harness fault is a failed run with its reason
        failures.append(f"harness: {type(exc).__name__}: {exc}")
        return 1
    finally:
        sids = {str(d.get("sessionId", ""))[:8] for d in sb.entries()}
        sids |= {str(d.get("sessionId", ""))[:8] for d in before.values() if d}
        if not a.keep:
            # First: ours alone (-L), it holds the token, and its claude processes end with it;
            # then the config copy they were reading.
            sb.tmux("kill-server")
            sb.delete_config()
        for n in notes:
            print(f"[sandbox] {n}", flush=True)
        for f in failures:
            print(f"[sandbox] FAIL {f}", flush=True)
        verdict = "RESTART SANDBOX PASSED" if ok else "RESTART SANDBOX FAILED"
        print(f"{verdict} -- 6 sessions and a no-handoff control, {len(failures)} failure(s)", flush=True)
        if a.keep:
            print(f"[sandbox] --keep: the sessions still run on a copy of your nexus config at "
                  f"{sb.nexus_config}/config.yml; delete it when done", flush=True)
        if ok and not a.keep:
            for sid in filter(None, sids):  # the script's /tmp files for these sessions
                for f in Path("/tmp").glob(f"rh-{sid}.*"):
                    f.unlink(missing_ok=True)
                for f in Path("/tmp").glob(f"nexus-continuation-restart-rs_*-{sid}-*"):
                    f.unlink(missing_ok=True)
            # The killed sessions' MCP servers and hooks outlive the tmux server for a while and
            # keep writing logs under the scratch HOME: wait until no process names it (60 s at most).
            for _ in range(30):
                if subprocess.run(["pgrep", "-f", str(home)], capture_output=True).returncode != 0:
                    break
                time.sleep(2)
            shutil.rmtree(home, ignore_errors=True)
            if home.exists():
                print(f"[sandbox] could not remove {home}; it holds no credentials (deleted above)", flush=True)
        else:
            print(f"[sandbox] kept {home} (transcripts, restart log, handoffs under /tmp)"
                  + (f"; tmux server {sb.sock} left running: tmux -L {sb.sock} kill-server when done" if a.keep else ""),
                  flush=True)


if __name__ == "__main__":
    sys.exit(main())
