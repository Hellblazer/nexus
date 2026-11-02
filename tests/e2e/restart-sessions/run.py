# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (c) 2026 Hal Hildebrand. All rights reserved.
"""Sandbox gate for scripts/claude-restart-sessions --nx --fresh (nexus-nuyju, nexus-cmqe8, nexus-8m0ho, nexus-q4m6u).

Drives real Claude Code sessions through the coordinated restart and checks every outcome,
with nothing written to the operator's ~/.claude. Four scenarios, each in its own sandbox
(``--scenario``, default all, in this order):

- ``roll``: the full continuation roll. Six sessions; three restart, each told a code word:
  ``rs_alpha`` (the conexus channel, left at its prompt with whatever suggested prompt Claude
  Code draws in dim text; the caller, so it must go last), ``rs_draft`` (the channel, a real
  unsent draft in its prompt) and ``rs_nochan`` (no channel). Three are left running:
  ``rs_busy`` (mid-turn in a foreground command that waits on a hold file the harness
  removes after the roll), ``rs_shell`` (a background shell open, status ``shell``) and
  ``rs_paste`` (a bracketed paste shown as ``[Pasted text #N]``). The engine gate runs
  against a local ``/version`` stand-in that answers 502, then a version below the
  release's floor, then the floor: the gate must wait through both (30 s each), read three
  times, and pass before the roll starts. ``--fallback-resume`` is on.
- ``variant``: no ``--fallback-resume``. ``rs_alpha`` (the caller) and ``rs_late`` restart;
  ``rs_late`` is mid-turn when the roll starts and its hold is removed only once rs_alpha's
  and rs_image's handoffs are written. That is the proof a busy session holds up no other:
  a script that waited on rs_late before asking the others would never see it released and
  fail; the check that rs_late is asked after both handoffs then guards a script that asks
  a busy session early. ``rs_shell`` stays in ``shell`` and must be skipped at the handoff
  with no fallback line and no restart-phase SKIP. ``rs_image`` holds an ``[Image #N]``
  placeholder (a bracketed paste of a .png path) and is skipped at its restart.
- ``abort``: the release's reinstall-tool.sh stand-in installs and publishes X, then exits 1,
  so nx reports X and only the exit code can stop the roll. Both sessions write their
  handoff; the script must abort after the handoffs with nothing exited, no plugin update,
  X absent from the plugin cache, and the handoffs kept.
- ``gate``: the ``/version`` stand-in answers 502, then (a second run) a version below the
  floor, with ``--engine-wait 0``. Each run must exit 2 in the foreground with the gate's
  ABORT line for that case, after one read, before any handoff is asked or anything installed.

Shared by every scenario:

- a scratch HOME (onboarding done, the work folder trusted) and a scratch nexus config dir
  holding a copy of the operator's ``config.yml``, so the sessions' MCP servers and ``nx``
  reach the operator's own service. The copy, the data-token leases minted from it and the
  sessions' T1 session leases are deleted on every exit the harness sees (pass, fail, SIGTERM,
  SIGHUP, SIGINT; not SIGKILL, and not under ``--keep``); once cleanup starts it ignores those
  signals. Every process below the private tmux server is recorded while the run lasts, so an
  exited session's Serena and SessionEnd hooks, reparented with no HOME in their argv, are
  ended by checked pid with the other leftovers. Sessions run with ``NX_UPGRADE_AUTO=0``. The
  sessions write to that service as the operator would: the restart mail expires with its
  TTL, and nothing here asks a session to write anything permanent;
- the conexus and sn plugins from a git marketplace named nexus-plugins, served from the
  scratch HOME over smart HTTP on 127.0.0.1 (``git http-backend``; Claude Code 2.1.316
  accepts an http URL, refuses ``git://`` and ``file://``, and cannot shallow-clone the dumb
  transport). It holds copies of the operator's installed plugins (read only). Installing
  and updating fill the versioned cache ``plugins/cache/nexus-plugins/<plugin>/<version>``,
  as on the operator's box, so the cache folder a session's MCP servers run from names the
  version it loaded, and a marker bump_market writes into each published tree
  (``.sandbox-release``) proves that folder holds that release, for conexus and sn;
- a private tmux server (``-L``, ``-f /dev/null``, a bare bash in every pane);
- stand-ins for what --nx touches outside the sandbox: ``--repo`` is a scratch release repo
  whose tag vX holds this checkout's engine floor, and whose reinstall-tool.sh records its
  arguments and publishes X to the sandbox marketplace (or, in ``abort``, exits 1); an
  ``nx`` on PATH reports X once that ran (anything else goes to the real nx); a ``uv`` on
  PATH records every ``cache`` command and refuses ``tool install``, ``upgrade`` and
  ``uninstall`` (anything else goes to the real uv). X is the installed version's next
  patch that is not a tag in this checkout. The ``nx`` stand-in also records and refuses
  ``nx upgrade`` (the version-lockstep hook's action would upgrade the operator's install).
  The marketplace and plugin updates are the real
  claude CLI's.

Expected in ``roll``: alpha, draft and paste are asked by mail and nochan by typing; all four
write a handoff; the install (uv cache clean, reinstall-tool.sh with ``conexus==X
--allow-registry-over-dev``, nx at X, both plugins updated to X) runs after the last handoff
and before the first exit; alpha, draft and nochan restart with a new session id whose own
argv carries ``-- cat <handoff>`` and whose MCP servers run X from the versioned cache (all
three ran the old version's before); busy and shell are skipped at the handoff and again at
the restart, paste is skipped at its restart for its placeholder, and all three are the same
process on the same session afterwards (busy still busy when the roll ends); alpha's exit
comes after every other session's outcome; no mailed session's old transcript holds a typed
ask or the draft as a prompt; the draft is still in rs_draft's prompt from its complete
handoff until its exit, is saved at the restart, and is read by the fresh rs_draft with its
handoff; all three then answer their own code word and no other. Exiting Claude Code clears
its input box, so "the draft survives" means saved and handed on.

Continuity is proved from the handoff, not merely observed. Auto memory is off
(``CLAUDE_CODE_DISABLE_AUTO_MEMORY``, ``autoMemoryEnabled: false``) and the word is given as
task context, not "remember"; each fresh session's word must be in its handoff file, must
first appear in its transcript inside a tool result (the read of the handoff), and no memory
file in the scratch HOME may hold any word. A negative control in ``roll``, one more fresh
session in the same folder with no handoff, must not know any word.

Not exercised: a real cloud engine (the gate reads the stand-in), a real PyPI install, the
operator's own marketplace remote, a restart with ``--resume`` (no scenario resumes a
session), the ready-round check (the roll passes ``--no-ready-check``; unit tests cover it), the ``ABORT: plugins are`` branch (a plugin update that leaves an old version), and
restart()'s fallback for a handoff that went stale.

Positive controls: ``NX_RESTART_SANDBOX_SCRIPT=<copy>`` runs another copy of the script; the
pane checks always use this checkout's reader. Against the rewritten harness (nexus-q4m6u),
each one-change copy fails its scenario: every roll treated as --fallback-resume fails
``variant``; going on after a failed install, or ignoring the reinstall's exit code, fails
``abort``; an unreadable engine passing the gate fails ``gate``; a failed read ending the
wait fails ``roll``; and a copy whose post-handoff install never runs fails ``roll`` (fresh
sessions on the old release). The pre-cmqe8 copy (rs_draft, argv) and a caller-first,
install-early, shell-ready copy (caller-last, install window, rs_shell) failed the one-roll
harness these checks come from (nexus-8m0ho); they were not rerun here.

Run through run.sh (it supplies the automation token, RDR-219). Prints one verdict line,
``RESTART SANDBOX PASSED`` or ``RESTART SANDBOX FAILED``, and exits 0 or 1 (a precondition
that fails once a sandbox is up is a failure, exit 1); exit 2 when the token, claude or tmux
is missing. A failed scenario's scratch HOME is kept and named.

A pass of all four scenarios is recorded in ~/.local/state/claude-restart-sessions/sandbox-pass.json
(the script's sha256 and Claude Code's version), which the script's restart gate reads (nexus-8zdbu).
Each sandbox HOME gets its own record for the script under test, so every roll passes that gate.
"""
# ruff: noqa: T201 -- a gate harness: stdout carries its progress and verdict line
from __future__ import annotations

import argparse
import contextlib
import dataclasses
import http.server
import importlib.machinery
import importlib.util
import json
import os
import re
import shlex
import shutil
import signal
import struct
import subprocess
import sys
import tempfile
import threading
import time
import urllib.parse
import zlib
from pathlib import Path

REPO = Path(__file__).resolve().parents[3]
#: The script under test. NX_RESTART_SANDBOX_SCRIPT points the gate at another copy, which is
#: how its positive controls run.
SCRIPT = Path(os.environ.get("NX_RESTART_SANDBOX_SCRIPT") or REPO / "scripts" / "claude-restart-sessions")
CHANNEL_FLAGS = "--channels plugin:conexus@nexus-plugins"
MARKET = "nexus-plugins"
#: The branch the sandbox marketplace publishes on (its bare repo's HEAD).
MARKET_BRANCH = "plugins"
#: Every plugin the script's --nx install checks (its PLUGINS); the sandbox marketplace holds both.
MARKET_PLUGINS = ("conexus", "sn")
DRAFT_TEXT = "this is an unsent draft the restart must not clobber"
#: Pasted with bracketed paste: Claude Code shows a paste this long as a [Pasted text #N] placeholder.
PASTE_TEXT = "\n".join(f"pasted line {i} the restart cannot save from the pane" for i in range(1, 21))
OPERATOR_HOME = Path.home()
SANDBOX_PY = Path(__file__).resolve()
SCENARIOS = ("roll", "variant", "abort", "gate")
#: A file bump_market writes into each published plugin tree: which release a cache folder's content is.
RELEASE_MARK = ".sandbox-release"
#: The typed re-asks after a mail ask name no failure word of their own, so they are banned by phrase.
BANNED = ("SKIP", "WARN", "FAIL", "CRASH", "ABORT", "fallback", "by typed (the mail", "by typed (its mail")

#: The harness's own pane reader: always this checkout's script, never the one under test,
#: so a control run of an older copy is judged by the same oracle.
ORACLE = REPO / "scripts" / "claude-restart-sessions"


@dataclasses.dataclass(frozen=True)
class Spec:
    name: str
    #: Sessions that must restart fresh, with the code word each is told ({} = told nothing).
    words: dict[str, str]
    #: Sessions that must be left running.
    held: tuple[str, ...]
    #: Sessions launched with the conexus channel; the rest are asked by typing.
    channel: frozenset[str]
    caller: str = "rs_alpha"
    wait: int = 120
    fallback: bool = False
    #: What the /version stand-in answers, read by read: a version, or "502". The last repeats.
    engine: tuple[str, ...] = ("floor",)
    engine_wait: int = 0
    #: Sessions mid-turn on a hold file when the roll starts.
    busy: tuple[str, ...] = ()
    #: Busy sessions whose hold is removed once these sessions' handoffs are written.
    release_after: tuple[str, ...] = ()
    install_fails: bool = False
    #: Sessions told nothing that still restart (abort: they would, were the install good).
    plain: tuple[str, ...] = ()

    @property
    def sessions(self) -> tuple[str, ...]:
        return (*self.words, *self.plain, *self.held)


SPECS = {
    "roll": Spec("roll", {"rs_alpha": "HELIOTROPE", "rs_draft": "MARZIPAN", "rs_nochan": "QUARTZITE"},
                 ("rs_busy", "rs_shell", "rs_paste"),
                 frozenset({"rs_alpha", "rs_draft", "rs_busy", "rs_shell", "rs_paste"}),
                 fallback=True, engine=("502", "behind", "floor"), engine_wait=150, busy=("rs_busy",)),
    "variant": Spec("variant", {"rs_alpha": "HELIOTROPE", "rs_late": "OBSIDIAN"}, ("rs_shell", "rs_image"),
                    frozenset({"rs_alpha", "rs_late", "rs_shell", "rs_image"}), wait=240,
                    busy=("rs_late",), release_after=("rs_alpha", "rs_image")),
    "abort": Spec("abort", {}, (), frozenset({"rs_alpha"}), plain=("rs_alpha", "rs_nochan"), install_fails=True),
    "gate": Spec("gate", {}, ("rs_alpha",), frozenset({"rs_alpha"}), engine=("502",)),
}


#: Serena's user config for a sandbox HOME. With none, Serena writes its default, which opens a
#: dashboard tab in the operator's browser for every sandbox session (Serena's macOS tray manager
#: is shared across HOMEs). Missing keys take Serena's defaults; the operator's own file is not
#: copied (it sets the JetBrains backend). ``projects`` is the one key Serena requires: its loader
#: raises SerenaConfigError without it, and the session's Serena MCP then fails to start.
SERENA_CONFIG = ("projects: []\nweb_dashboard: false\nweb_dashboard_open_on_launch: false\n"
                 "gui_log_window: false\n")


#: The signals main() turns into an exit. Once cleanup starts it ignores them: a second ^C or a
#: terminal closing mid-cleanup must not leave the tmux server (it holds the token) or the config copy.
CLEANUP_SIGNALS = (signal.SIGTERM, signal.SIGHUP, signal.SIGINT)


@contextlib.contextmanager
def signals_ignored():
    """Ignore CLEANUP_SIGNALS inside the block; restore the handlers that were set after it."""
    saved = {s: signal.getsignal(s) for s in CLEANUP_SIGNALS}
    try:
        for s in CLEANUP_SIGNALS:
            signal.signal(s, signal.SIG_IGN)
        yield
    finally:
        for s, h in saved.items():
            signal.signal(s, h)


def tree_of(rows: str, root: int) -> list[tuple[int, str]]:
    """Every process below *root* in ``ps -o pid=,ppid=,args=`` output, as (pid, argv)."""
    kids: dict[int, list[tuple[int, str]]] = {}
    for row in rows.splitlines():
        parts = row.split(None, 2)
        if len(parts) == 3 and parts[0].isdigit() and parts[1].isdigit():
            kids.setdefault(int(parts[1]), []).append((int(parts[0]), parts[2]))
    out, todo = [], [root]
    while todo:
        for child in kids.get(todo.pop(), []):
            out.append(child)
            todo.append(child[0])
    return out


class ProcWatch:
    """Record every process below the given roots, polled while the sandbox runs.

    A session that exits mid-roll leaves its Serena MCP server and SessionEnd hooks reparented
    to init, with argv that names nothing of the sandbox: the pre-kill descendant walk no longer
    reaches them and the HOME sweep cannot see them. A record taken while the tree was whole
    can; cleanup ends each recorded process still running the same argv."""

    def __init__(self, roots, interval: float = 2.0) -> None:
        self._roots, self._interval = roots, interval
        self._seen: dict[int, str] = {}
        self._lock = threading.Lock()
        self._stop = threading.Event()
        self._thread = threading.Thread(target=self._run, name="sandbox-proc-watch", daemon=True)

    def start(self) -> None:
        self._thread.start()

    def stop(self) -> None:
        self._stop.set()
        if self._thread.is_alive() and self._thread is not threading.current_thread():
            self._thread.join(timeout=10)

    def poll(self) -> None:
        roots = [r for r in self._roots() if r > 1]
        if not roots:
            return
        rows = subprocess.run(["ps", "-A", "-ww", "-o", "pid=,ppid=,args="], capture_output=True, text=True).stdout
        found = [c for r in roots for c in tree_of(rows, r) if c[0] > 1]
        with self._lock:
            for pid, args in found:
                self._seen[pid] = args  # a reused pid takes its newest argv; _end checks argv before it signals

    def _run(self) -> None:
        while not self._stop.is_set():
            try:
                self.poll()
            except Exception:  # noqa: BLE001,S110 -- a missed poll is covered by the next and by the HOME sweep
                pass
            self._stop.wait(self._interval)

    def seen(self) -> list[tuple[int, str]]:
        with self._lock:
            return sorted(self._seen.items())


#: Files in the sandbox's nexus config dir that hold a credential: the config copy, the data-token
#: leases minted from it, and the T1 session leases (each carries a T1 token; a kept HOME held them).
CREDENTIAL_PREFIXES = ("data_token_lease.", "t1_session_lease.")


def credentials_left(config_dir: Path) -> list[str]:
    return sorted(f.name for f in config_dir.glob("*")
                  if f.name == "config.yml" or f.name.startswith(CREDENTIAL_PREFIXES))


#: The exit code the sandbox's nx and uv shims refuse with.
REFUSED = 97


def nx_shim_text(real_nx: str, marker: Path, refused_log: Path) -> str:
    """The sandbox's nx: after the stand-in install, ``--version`` reports the installed release;
    ``nx upgrade`` (the version-lockstep hook's action, an ``--auto`` spawn) is recorded and
    refused, since it would upgrade the operator's install; anything else runs the real nx."""
    m, log = shlex.quote(str(marker)), shlex.quote(str(refused_log))
    return ("#!/bin/sh\n# restart sandbox: nx --version reports the stand-in install; nx upgrade is refused\n"
            f'if [ "$1" = "--version" ] && [ -f {m} ]; then echo "nx, version $(cat {m})"; exit 0; fi\n'
            'for a in "$@"; do case "$a" in\n'
            "  -*) ;;\n"
            f'  upgrade) echo "$*" >> {log}; exit {REFUSED} ;;\n'
            "  *) break ;;\n"
            "esac; done\n"
            f"exec {shlex.quote(real_nx)} \"$@\"\n")


def write_serena_config(home: Path) -> Path:
    """Write SERENA_CONFIG as *home*'s ~/.serena/serena_config.yml, before any session starts."""
    path = home / ".serena" / "serena_config.yml"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(SERENA_CONFIG, encoding="utf-8")
    return path


def _text_of(content) -> str:
    """The text of a transcript message's content: a plain string, or its text parts joined."""
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        return "".join(c.get("text", "") for c in content if isinstance(c, dict) and c.get("type") == "text")
    return ""


def reply_after(events: list[dict], prompt: str) -> str | None:
    """The first assistant text after the LAST user entry whose text is *prompt*; None while there
    is none yet (the turn is still running, or the prompt has not landed)."""
    at = None
    for i, d in enumerate(events):
        if d.get("type") == "user" and _text_of((d.get("message") or {}).get("content")).strip() == prompt.strip():
            at = i
    if at is None:
        return None
    for d in events[at + 1:]:
        if d.get("type") == "assistant":
            text = _text_of((d.get("message") or {}).get("content"))
            if text:
                return text
    return None


def load_script():
    """The oracle's module (its pane readers judge the preconditions)."""
    loader = importlib.machinery.SourceFileLoader("claude_restart_sessions", str(ORACLE))
    spec = importlib.util.spec_from_loader(loader.name, loader)
    assert spec is not None
    module = importlib.util.module_from_spec(spec)
    loader.exec_module(module)
    return module


def _git(repo: Path, *args: str) -> None:
    subprocess.run(["git", "-C", str(repo), "-c", "user.name=t", "-c", "user.email=t@example.invalid",
                    "-c", "commit.gpgsign=false", *args], check=True, capture_output=True)


def bump_market(market: Path, version: str) -> None:
    """Publish *version* to the sandbox marketplace: set it in each plugin.json and the manifest,
    write RELEASE_MARK, commit, and push to the bare repo the smart-HTTP server serves (``srv/market.git``)."""
    for p in MARKET_PLUGINS:
        f = market / p / ".claude-plugin" / "plugin.json"
        d = json.loads(f.read_text(encoding="utf-8"))
        d["version"] = version
        f.write_text(json.dumps(d, indent=2) + "\n", encoding="utf-8")
        (market / p / RELEASE_MARK).write_text(version + "\n", encoding="utf-8")  # names the tree a cache folder holds
    (market / ".claude-plugin" / "marketplace.json").write_text(json.dumps(
        {"name": MARKET, "owner": {"name": "restart sandbox"},
         "plugins": [{"name": p, "source": f"./{p}", "version": version} for p in MARKET_PLUGINS]}),
        encoding="utf-8")
    _git(market, "add", "--all", ".")
    _git(market, "commit", "-q", "--allow-empty", "-m", f"publish {version}")
    _git(market, "push", "-q", str(market.parent / "srv" / "market.git"), f"HEAD:{MARKET_BRANCH}")


class _Server:
    """A ThreadingHTTPServer on 127.0.0.1, port 0, in a daemon thread."""

    def __init__(self, handler: type[http.server.BaseHTTPRequestHandler]) -> None:
        self.httpd = http.server.ThreadingHTTPServer(("127.0.0.1", 0), handler)
        self.url = f"http://127.0.0.1:{self.httpd.server_address[1]}"
        threading.Thread(target=self.httpd.serve_forever, daemon=True).start()

    def close(self) -> None:
        self.httpd.shutdown()
        self.httpd.server_close()


def git_http_server(root: Path) -> _Server:
    """Smart HTTP for the bare repos under *root*: each request runs ``git http-backend`` as CGI."""

    class Handler(http.server.BaseHTTPRequestHandler):
        def _run(self) -> None:
            u = urllib.parse.urlsplit(self.path)
            n = int(self.headers.get("Content-Length") or 0)
            body = self.rfile.read(n) if n else b""
            env = {"PATH": os.environ.get("PATH", "/usr/bin:/bin"), "GIT_PROJECT_ROOT": str(root),
                   "GIT_HTTP_EXPORT_ALL": "1", "REQUEST_METHOD": self.command,
                   "PATH_INFO": urllib.parse.unquote(u.path), "QUERY_STRING": u.query,
                   "CONTENT_TYPE": self.headers.get("Content-Type", ""), "CONTENT_LENGTH": str(len(body)),
                   "REMOTE_ADDR": "127.0.0.1"}
            if self.headers.get("Content-Encoding"):
                env["HTTP_CONTENT_ENCODING"] = self.headers["Content-Encoding"]
            if self.headers.get("Git-Protocol"):
                env["GIT_PROTOCOL"] = self.headers["Git-Protocol"]
            proc = subprocess.run(["git", "http-backend"], input=body, capture_output=True, env=env)
            if proc.returncode != 0:  # never an empty 200 for a backend that crashed
                self.send_response(500)
                self.send_header("Content-Length", "0")
                self.end_headers()
                return
            out = proc.stdout
            head, sep, rest = out.partition(b"\r\n\r\n")
            if not sep:
                head, _, rest = out.partition(b"\n\n")
            code, headers = 200, []
            for line in head.decode("latin-1").splitlines():
                k, _, v = line.partition(":")
                if k.lower() == "status":
                    code = int(v.strip().split()[0])
                elif k:
                    headers.append((k, v.strip()))
            self.send_response(code)
            for k, v in headers:
                self.send_header(k, v)
            self.send_header("Content-Length", str(len(rest)))
            self.end_headers()
            self.wfile.write(rest)

        do_GET = do_POST = _run

        def log_message(self, format: str, *args: object) -> None:  # noqa: A002 -- the base's name
            return

    return _Server(Handler)


class EngineStub:
    """The engine gate's ``/version`` read: answers in order (the last repeats), and counts reads."""

    def __init__(self, answers: list[str]) -> None:
        self.answers, self.reads = answers, 0
        stub = self

        class Handler(http.server.BaseHTTPRequestHandler):
            def do_GET(self) -> None:
                answer = stub.answers[min(stub.reads, len(stub.answers) - 1)]
                stub.reads += 1
                if self.path != "/version" or answer == "502":
                    self.send_response(404 if self.path != "/version" else 502)
                    self.send_header("Content-Length", "0")
                    self.end_headers()
                    return
                body = json.dumps({"release_version": answer}).encode()
                self.send_response(200)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)

            def log_message(self, format: str, *args: object) -> None:  # noqa: A002 -- the base's name
                return

        self.server = _Server(Handler)
        self.url = self.server.url


def write_png(path: Path) -> None:
    """A 1x1 PNG: a bracketed paste of its path becomes an [Image #N] placeholder (Claude Code 2.1.316)."""
    def chunk(tag: bytes, data: bytes) -> bytes:
        return struct.pack(">I", len(data)) + tag + data + struct.pack(">I", zlib.crc32(tag + data) & 0xFFFFFFFF)
    path.write_bytes(b"\x89PNG\r\n\x1a\n" + chunk(b"IHDR", struct.pack(">IIBBBBB", 1, 1, 8, 2, 0, 0, 0))
                     + chunk(b"IDAT", zlib.compress(b"\x00\xff\x00\x00")) + chunk(b"IEND", b""))


class Sandbox:
    def __init__(self, home: Path, sock: str, claude: str, model: str, spec: Spec) -> None:
        self.home, self.sock, self.claude, self.model, self.spec = home, sock, claude, model, spec
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
                        DISABLE_AUTOUPDATER="1",  # the dim ghost prompt that broke 7.92.0
                        # nexus-75w5d's kill switch: a session's SessionStart must not spawn a detached
                        # nx upgrade --auto, which would act on the operator's install and outlive the run
                        NX_UPGRADE_AUTO="0")
        # --nx installs a release: the sandbox's own stand-ins (setup_release) take its place.
        self.market = home / "market"
        self.cache = home / ".claude" / "plugins" / "cache" / MARKET
        self.relrepo = home / "release-repo"
        self.bin = home / "bin"
        self.installed_marker = home / ".nx-sandbox-installed"
        self.real_nx = shutil.which("nx") or ""
        self.real_uv = shutil.which("uv") or ""
        self.uv_cleans = home / "uv-cache-clean.log"
        self.uv_refused = home / "uv-refused.log"
        self.nx_refused = home / "nx-refused.log"
        self.reinstall_argv = home / "reinstall-argv.txt"
        self.holds = {name: home / f"{name}.hold" for name in spec.busy}
        self.env["PATH"] = f"{self.bin}{os.pathsep}{self.env.get('PATH', '')}"
        self.from_version = self.to_version = ""
        self.servers: list[_Server] = []
        self.engine: EngineStub | None = None
        self.floor = self.behind = ""
        #: Each session as it was before the roll (boot fills it; cleanup reads its session ids).
        self.before: dict[str, dict] = {}
        #: Every process below the private tmux server, recorded while it runs (setup starts it).
        self.watch = ProcWatch(self.server_pids)

    # tmux on the private server only
    def tmux(self, *args: str, check: bool = False) -> subprocess.CompletedProcess:
        return subprocess.run(["tmux", "-L", self.sock, "-f", "/dev/null", *args],
                              capture_output=True, text=True, env=self.env, check=check)

    def setup(self, crs) -> None:
        (self.home / ".claude").mkdir(parents=True)
        write_serena_config(self.home)
        self.work.mkdir()
        _git(self.work, "init", "-q")
        (self.work / "README.md").write_text("restart sandbox\n", encoding="utf-8")
        _git(self.work, "add", "README.md")
        _git(self.work, "commit", "-q", "-m", "root")
        projects = {key: {"hasTrustDialogAccepted": True, "hasCompletedProjectOnboarding": True}
                    for key in {str(self.work), os.path.realpath(self.work)}}
        (self.home / ".claude.json").write_text(
            json.dumps({"hasCompletedOnboarding": True, "projects": projects}), encoding="utf-8")
        (self.home / ".claude" / "settings.json").write_text(
            json.dumps({"skipDangerousModePermissionPrompt": True, "promptSuggestionEnabled": True,
                        "autoMemoryEnabled": False}), encoding="utf-8")
        self.setup_market()
        self.setup_release()
        # A pass record for the script under test, so each roll goes through the script's sandbox gate
        # (nexus-8zdbu) and is let by it; a copy without the gate ignores it.
        crs.record_sandbox_pass(self.home / crs.SANDBOX_PASS.relative_to(OPERATOR_HOME), crs.script_sha256(SCRIPT),
                                crs.claude_version(self.claude), [], 0)
        floor = crs.release_engine_floor(self.to_version, self.relrepo)
        self.floor = ".".join(map(str, floor))
        self.behind = ".".join(map(str, (floor[0], floor[1], floor[2] - 1) if floor[2] else (floor[0], max(floor[1] - 1, 0), 0)))
        self.engine = EngineStub([{"floor": self.floor, "behind": self.behind}.get(x, x) for x in self.spec.engine])
        self.servers.append(self.engine.server)
        # The operator's nexus config, copied (never printed) so nx and the MCP servers reach the
        # same service; delete_config() removes it in every outcome.
        self.nexus_config.mkdir(parents=True)
        shutil.copy2(OPERATOR_HOME / ".config" / "nexus" / "config.yml", self.nexus_config / "config.yml")
        os.chmod(self.nexus_config / "config.yml", 0o600)
        self.tmux("new-session", "-d", "-s", "rs", "-x", "220", "-y", "50", "-n", "ctl", "-c", str(self.work),
                  "bash --noprofile --norc", check=True)
        self.watch.start()

    def server_pids(self) -> list[int]:
        """The private tmux server's pid ([] when it is not running): every session runs below it."""
        out = self.tmux("display-message", "-p", "#{pid}").stdout.strip()
        return [int(out)] if out.isdigit() else []

    def claude_cli(self, *args: str) -> None:
        r = subprocess.run([self.claude, *args], capture_output=True, text=True, env=self.env)
        if r.returncode != 0:
            raise RuntimeError(f"claude {' '.join(args)}: exit {r.returncode}: {(r.stdout + r.stderr).strip()[-300:]}")

    def setup_market(self) -> None:
        """A git marketplace named nexus-plugins holding copies of the operator's installed conexus and
        sn (read only), served over smart HTTP and installed into the scratch HOME through the claude
        CLI, so the script's --nx install has a marketplace it can move to a new version and the
        sessions load the versioned cache."""
        plugins = json.loads((OPERATOR_HOME / ".claude" / "plugins" / "installed_plugins.json").read_text(encoding="utf-8"))
        plugins = plugins.get("plugins", plugins)
        (self.market / ".claude-plugin").mkdir(parents=True)
        versions = set()
        for p in MARKET_PLUGINS:
            entry = plugins[f"{p}@{MARKET}"]
            entry = entry[0] if isinstance(entry, list) else entry
            shutil.copytree(entry["installPath"], self.market / p, symlinks=True,
                            ignore=shutil.ignore_patterns(".in_use", "__pycache__", ".git"))
            versions.add(str(entry["version"]))
        if len(versions) != 1:
            raise RuntimeError(f"the operator's conexus and sn differ in version ({sorted(versions)})")
        self.from_version = versions.pop()
        major, minor, patch = (int(x) for x in self.from_version.split(".")[:3])
        patch += 1
        while subprocess.run(["git", "-C", str(REPO), "rev-parse", "-q", "--verify", f"refs/tags/v{major}.{minor}.{patch}"],
                             capture_output=True).returncode == 0:
            patch += 1  # a real tag: a script that ignored --repo would install a real release from it
        self.to_version = f"{major}.{minor}.{patch}"
        _git(self.market, "init", "-q", "-b", MARKET_BRANCH)
        bare = self.home / "srv" / "market.git"  # the only repo the server exports
        subprocess.run(["git", "init", "-q", "--bare", "-b", MARKET_BRANCH, str(bare)], check=True, capture_output=True)
        bump_market(self.market, self.from_version)
        git_srv = git_http_server(self.home / "srv")
        self.servers.append(git_srv)
        self.claude_cli("plugin", "marketplace", "add", f"{git_srv.url}/market.git")
        for p in MARKET_PLUGINS:
            self.claude_cli("plugin", "install", f"{p}@{MARKET}", "--scope", "user")
        if not (self.cache / "conexus" / self.from_version).is_dir():
            raise RuntimeError(f"plugin install did not fill the versioned cache {self.cache}/conexus/{self.from_version}")

    def setup_release(self) -> None:
        """Stand-ins for what --nx X touches outside the sandbox: a release repo whose tag vX holds the
        engine floor, a reinstall-tool.sh that records the install and publishes X to the sandbox
        marketplace (in ``abort``, records it and exits 1), and an nx on PATH that reports X once
        installed (anything else goes to the real nx)."""
        r = self.relrepo
        (r / "src" / "nexus").mkdir(parents=True)
        (r / "scripts").mkdir()
        shutil.copy2(REPO / "src" / "nexus" / "engine_version.py", r / "src" / "nexus" / "engine_version.py")
        stub = r / "scripts" / "reinstall-tool.sh"
        record = f'printf "%s\\n" "$@" > {shlex.quote(str(self.reinstall_argv))}\n'
        publish = ('v="${1#conexus==}"\n' + record
                   + f'printf "%s\\n" "$v" > {shlex.quote(str(self.installed_marker))}\n'
                   f'{shlex.quote(sys.executable)} -I {shlex.quote(str(SANDBOX_PY))} --bump-market '
                   f'{shlex.quote(str(self.market))} "$v" || exit $?\n')
        if self.spec.install_fails:
            # Installed and published, then a nonzero exit: nx reports X and the marketplace holds it, so
            # only the exit code stops the roll, and a script that went on would fill the cache with X.
            body = ("#!/bin/sh\n# restart sandbox stand-in: install and publish, then fail\n" + publish
                    + 'echo "sandbox: install failed after it ran" >&2\nexit 1\n')
        else:
            body = "#!/bin/sh\n# restart sandbox stand-in: record the install, publish the version to the sandbox marketplace\n" + publish
        stub.write_text(body, encoding="utf-8")
        stub.chmod(0o755)
        _git(r, "init", "-q")
        _git(r, "add", "--all", ".")
        _git(r, "commit", "-q", "-m", "release")
        _git(r, "tag", f"v{self.to_version}")
        _git(r, "remote", "add", "origin", str(r))  # the script fetches tags from origin first
        if not self.real_nx:
            raise RuntimeError("nx is not on PATH")
        self.bin.mkdir()
        shim = self.bin / "nx"
        shim.write_text(nx_shim_text(self.real_nx, self.installed_marker, self.nx_refused), encoding="utf-8")
        shim.chmod(0o755)
        # The script cleans uv's cached conexus before installing; in the detached child that runs
        # with the tmux server's environment, whose UV_CACHE_DIR is the operator's. Record it instead.
        if not self.real_uv:
            raise RuntimeError("uv is not on PATH")
        uv = self.bin / "uv"
        uv.write_text(
            "#!/bin/sh\n# restart sandbox: uv's cache and tool installs are the operator's; record, never run\n"
            f'case " $* " in *" cache "*) echo "$*" >> {shlex.quote(str(self.uv_cleans))}; exit 0 ;; esac\n'
            "tool=0\n"
            'for a in "$@"; do case "$a" in\n'
            "  tool) tool=1 ;;\n"
            f'  install|upgrade|uninstall) if [ "$tool" = 1 ]; then echo "$*" >> {shlex.quote(str(self.uv_refused))}; exit 97; fi ;;\n'
            "esac; done\n"
            f"exec {shlex.quote(self.real_uv)} \"$@\"\n", encoding="utf-8")
        uv.chmod(0o755)

    def close_servers(self) -> None:
        for s in self.servers:
            try:
                s.close()
            except OSError:
                pass

    def pane_id(self, name: str) -> str:
        return self.tmux("display-message", "-p", "-t", self.pane_of(name), "#{pane_id}", check=True).stdout.strip()

    def plugin_roots(self, pid: int, wait: float = 30) -> set[str]:
        """The conexus versions the process's descendants run from (its MCP servers' --directory, a
        versioned cache folder ``plugins/cache/nexus-plugins/conexus/<version>``), read until one
        shows or *wait* seconds pass; empty means none did."""
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

    @staticmethod
    def descendants(pid: int) -> list[tuple[int, str]]:
        """Every process below *pid*, as (pid, argv)."""
        rows = subprocess.run(["ps", "-A", "-ww", "-o", "pid=,ppid=,args="], capture_output=True, text=True).stdout
        return tree_of(rows, pid)

    def _plugin_roots(self, pid: int) -> set[str]:
        roots = "|".join(re.escape(r) for r in {str(self.cache), os.path.realpath(self.cache)})
        pat = re.compile(rf"(?:{roots})/conexus/([^/\s]+)(?:/|\s|$)")
        return {v for _, args in self.descendants(pid) for v in pat.findall(args)}

    def start_session(self, name: str) -> None:
        self.tmux("new-window", "-d", "-t", "rs:", "-n", name, "-c", str(self.work), "bash --noprofile --norc",
                  check=True)
        channel = f" {CHANNEL_FLAGS}" if name in self.spec.channel else ""
        cmd = f"{self.claude} --model {self.model} --dangerously-skip-permissions{channel} -n {name}"
        self.tmux("send-keys", "-t", f"rs:{name}", "-l", cmd, check=True)
        self.tmux("send-keys", "-t", f"rs:{name}", "Enter", check=True)

    def delete_config(self) -> None:
        """Remove the copied nexus config, the data-token leases minted from it and the sessions'
        T1 session leases (credentials)."""
        for name in credentials_left(self.nexus_config):
            (self.nexus_config / name).unlink(missing_ok=True)

    def entries(self) -> list[dict]:
        out = []
        for p in sorted(self.sessions_dir.glob("*.json")):
            try:
                d = json.loads(p.read_text(encoding="utf-8"))
            except (OSError, ValueError):
                continue
            try:
                pid = int(d.get("pid", 0) or 0) if isinstance(d, dict) else 0
            except (TypeError, ValueError):
                pid = 0
            if pid <= 1:  # os.kill(0, 0) succeeds on our own process group: no pid is no session
                continue
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

    def prompt(self, name: str, text: str, timeout: float = 240, done: frozenset[str] = frozenset({"idle"})) -> str:
        """Type a prompt, Enter, then wait until the session is idle with a text reply to THIS prompt;
        return that reply.

        Status alone is not the turn's end: with the conexus plugin loaded, hooks move a fresh
        session's status timestamp on their own, and an idle status read that way returned
        before the reply was written (nexus-cmqe8 sandbox, 2026-11-01). Nor is a reply count: a
        fresh session's first turn (reading its handoff) could still be writing its reply when the
        count was taken, so that reply raised the count and was read as the answer (the
        2026-11-02 roll: "THAT'S THE HANDOFF..." for a session that then answered HELIOTROPE).
        The reply is the first text the assistant wrote after the user entry carrying *text*.
        """
        d0 = self.wait_status(name, {"idle"}, 120)
        sid = str(d0.get("sessionId", ""))
        self.tmux("send-keys", "-t", self.pane_of(name), "-l", text, check=True)
        time.sleep(1)
        self.tmux("send-keys", "-t", self.pane_of(name), "Enter", check=True)
        deadline = time.time() + timeout
        while time.time() < deadline:
            d = self.session(name) or {}
            reply = reply_after(self.events(sid), text)
            if d.get("status") in done and reply is not None:
                return reply
            time.sleep(1)
        raise TimeoutError(f"{name}: prompt did not finish within {timeout:.0f}s")

    def hold_busy(self, name: str) -> None:
        """Mid-turn on a foreground command that waits while the session's hold file exists. Not a bare
        sleep: Claude Code's Bash tool refuses a standalone foreground sleep (2.1.316). The tool's own
        600 s ceiling bounds it."""
        hold = self.holds[name]
        hold.write_text("held\n", encoding="utf-8")
        self.tmux("send-keys", "-t", self.pane_of(name), "-l",
                  "Use the Bash tool, in the foreground with timeout 600000, to run exactly this command: "
                  f"python3 -c 'import os, time; [time.sleep(1) for _ in iter(lambda: os.path.exists({json.dumps(str(hold))}), False)]'. "
                  "Then reply with only: DONE", check=True)
        time.sleep(1)
        self.tmux("send-keys", "-t", self.pane_of(name), "Enter", check=True)
        self.wait_status(name, {"busy"}, 60)
        time.sleep(15)  # past the request: the wait itself, not the model's turn, holds it busy
        self.wait_status(name, {"busy"}, 5)

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
        """Files a later session could read a word from: anything in the scratch HOME except the
        transcripts, the session registry and the restart log themselves."""
        hits = []
        for f in self.home.rglob("*"):
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

    def read_log(self) -> str:
        return self.log.read_text(encoding="utf-8") if self.log.exists() else ""


def body_lines(log: str) -> list[str]:
    """The log's lines without their timestamp."""
    return [ln.split(" ", 1)[1] if " " in ln else ln for ln in log.splitlines()]


def roll_command(sb: Sandbox, *, rc_file: Path | None = None) -> str:
    spec = sb.spec
    assert sb.engine is not None
    cmd = (f"TMUX_PANE={shlex.quote(sb.pane_id(spec.caller))} "
           f"{shlex.quote(sys.executable)} -I {shlex.quote(str(SCRIPT))} --nx {sb.to_version} --fresh "
           f"--repo {shlex.quote(str(sb.relrepo))} --endpoint {sb.engine.url} --engine-wait {spec.engine_wait} "
           + ("--fallback-resume " if spec.fallback else "")
           + f"--no-update --no-ready-check --handoff-timeout 600 --wait {spec.wait}")
    if rc_file is not None:
        cmd += f"; echo $? > {shlex.quote(str(rc_file))}"
    return cmd


def check_lines(log: str, expect: list[str], held: set[str], failures: list[str]) -> list[str]:
    """Every expected line (a substring of some line) and held line (a whole line) is in the log, and
    no other line carries a failure word. Returns the log's body lines."""
    lines = body_lines(log)
    for want in expect:
        if want not in log:
            failures.append(f"log lacks: {want}")
    for want in sorted(held):
        if want not in lines:
            failures.append(f"log lacks: {want}")
    for ln in lines:
        if ln in held:
            continue
        for bad in BANNED:
            if bad in ln:
                failures.append(f"log has: {ln[:160]}")
                break
    return lines


def check_left_running(sb: Sandbox, names: tuple[str, ...], before: dict[str, dict], log: str,
                       failures: list[str]) -> None:
    """Each named session is the same process on the same session, and the script exited none."""
    for name in names:
        now = sb.session(name) or {}
        if now.get("pid") != before[name].get("pid") or now.get("sessionId") != before[name].get("sessionId"):
            failures.append(f"{name}: not left running as it was (before pid {before[name].get('pid')}, now {now.get('pid')})")
        if f"exit {name} " in log:
            failures.append(f"{name}: the script exited it")


def check_install(sb: Sandbox, lines: list[str], failures: list[str]) -> None:
    """The reinstall stand-in's arguments, uv's cache clean, no uv tool install, and the whole install
    between the last handoff and the first exit."""
    argv = sb.reinstall_argv.read_text(encoding="utf-8").split() if sb.reinstall_argv.exists() else []
    if argv != [f"conexus=={sb.to_version}", "--allow-registry-over-dev"]:
        failures.append(f"--nx: reinstall-tool.sh was called with {argv}")
    if sb.uv_refused.exists():
        failures.append(f"uv tool install refused in the sandbox: {sb.uv_refused.read_text(encoding='utf-8')[:200]!r}")
    cleans = sb.uv_cleans.read_text(encoding="utf-8").splitlines() if sb.uv_cleans.exists() else []
    if "cache clean conexus" not in cleans:
        failures.append(f"--nx: uv cache clean conexus was not run through the sandbox's uv ({cleans})")
    last_handoff = max((i for i, ln in enumerate(lines) if ln.startswith("handoff written by ")), default=-1)
    first_exit = min((i for i, ln in enumerate(lines) if ln.startswith("exit rs_")), default=len(lines))
    installs = [i for i, ln in enumerate(lines)
                if ln.startswith(("reinstall-tool ", "nx: ", "plugins: ", "plugin update ", "plugin marketplace update",
                                  "ABORT: nx reports", "ABORT: plugins are"))] or [-1]
    if not all(last_handoff < i < first_exit for i in installs):
        failures.append(f"--nx install (reinstall, nx, plugin update) at log line(s) {installs}, not only between the last handoff "
                        f"({last_handoff}) and the first exit ({first_exit})")


def check_caller_last(spec: Spec, lines: list[str], failures: list[str], notes: list[str]) -> None:
    caller_exit = next((i for i, ln in enumerate(lines) if ln.startswith(f"exit {spec.caller} ")), -1)
    last_other = max((i for i, ln in enumerate(lines)
                      if re.match(rf"(OK|SKIP) rs_(?!{spec.caller[3:]}\b)", ln) and "no handoff asked" not in ln), default=-1)
    if caller_exit < 0 or caller_exit < last_other:
        failures.append(f"{spec.caller} (the caller's pane) was not exited last (line {caller_exit}, last other {last_other})")
    else:
        notes.append(f"{spec.caller}, the caller's pane, exited after every other session's outcome")


def check_mailed_untyped(sb: Sandbox, before: dict[str, dict], failures: list[str]) -> None:
    """Nothing typed into a mailed session: its old transcript holds neither the draft nor the typed
    path's ask line as a prompt."""
    for name in sorted(sb.spec.channel & set(before)):
        typed = [d for d in sb.events(before[name].get("sessionId", "")) if d.get("type") == "user"
                 and (DRAFT_TEXT in json.dumps(d.get("message")) or "Do what /tmp/rh-" in json.dumps(d.get("message")))]
        if typed:
            failures.append(f"{name}: its old transcript has the draft or a typed ask as a prompt")


def check_fresh(sb: Sandbox, before: dict[str, dict], handoffs: dict[str, str], log: str,
                failures: list[str], notes: list[str]) -> None:
    """Each word session restarted fresh on its handoff, on the new release, and answers its own word."""
    words = sb.spec.words
    for name, word in words.items():
        others = [w for n, w in words.items() if n != name]
        old = before[name]["sessionId"]
        if f"OK {name}: fresh as pid" not in log:
            continue  # already failed on its log line; waiting for a start the log says never came proves nothing
        path = handoffs.get(name, "/nonexistent")
        body = Path(path).read_text(encoding="utf-8", errors="replace") if Path(path).exists() else ""
        if word not in body:
            failures.append(f"{name}: its handoff {handoffs.get(name)} does not carry {word}")
        if any(w in body for w in others):
            failures.append(f"{name}: its handoff carries another session's word")
        try:
            d = sb.wait_status(name, {"idle"}, 300, not_sid=old)
        except TimeoutError as exc:
            failures.append(str(exc))
            continue
        # The first prompt rode the command line: read the new process's own argv.
        argv = subprocess.run(["ps", "-o", "args=", "-p", str(d.get("pid", 0))], capture_output=True,
                              text=True).stdout.strip()
        want = f"-- cat {path}"
        if want not in argv:
            failures.append(f"{name}: the fresh process's argv lacks {want!r}")
        # The fresh process runs the release --nx installed, from the versioned cache.
        roots = sb.plugin_roots(int(d.get("pid", 0) or 0))
        if roots != {sb.to_version}:
            failures.append(f"{name}: the fresh process runs conexus {sorted(roots)} from the cache, not {sb.to_version}")
        else:
            notes.append(f"{name}: the fresh process runs conexus {sb.to_version} from the versioned cache (was {sb.from_version})")
        # The folder's content is the published release, for both plugins, not an old tree renamed.
        for p in MARKET_PLUGINS:
            mark = sb.cache / p / sb.to_version / RELEASE_MARK
            got = mark.read_text(encoding="utf-8").strip() if mark.is_file() else "absent"
            if got != sb.to_version:
                failures.append(f"{name}: the cache folder {p}/{sb.to_version} holds release {got}")
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
            reply = sb.prompt(name, "What is the project code word for this task? Reply with only the word.").upper()
        except TimeoutError as exc:
            failures.append(str(exc))
            continue
        source = sb.first_mention(d["sessionId"], word, path)
        if word not in reply:
            failures.append(f"{name}: asked for its code word after the restart, replied {reply[:80]!r}")
        elif any(w in reply for w in others):
            failures.append(f"{name}: replied with another session's word: {reply[:80]!r}")
        elif source != "handoff_read":
            failures.append(f"{name}: {word} first appears in its fresh transcript as {source or 'nothing'}, "
                            "not in the result of reading its handoff")
        else:
            notes.append(f"{name}: fresh as {d['sessionId'][:8]}, answered {word} (first seen in: {source})")
    leaks = sb.memory_files_with(list(words.values()))
    leaks = [h for h in leaks if not any(h.startswith(p) for p in handoffs.values())]
    if leaks:
        failures.append(f"a code word is on disk where a fresh session could read it: {leaks[:3]}")


def boot(sb: Sandbox, failures: list[str]) -> dict[str, dict]:
    """Start every session, tell each word session its word, and record each session as it was."""
    spec = sb.spec
    for name in spec.sessions:
        sb.start_session(name)
    for name in spec.sessions:
        sb.wait_status(name, {"idle"}, 120)
    for name, word in spec.words.items():
        sb.prompt(name, f"For this task, the project code word is {word}. Reply with only: OK")
    before: dict[str, dict] = {}
    for name in spec.sessions:
        d = sb.session(name)
        if not d or not d.get("tmux"):
            failures.append(f"{name}: its session file has no tmux pane; the script cannot see it")
        before[name] = d or {}
    # Non-vacuity for the --nx check: before the roll the restarting sessions run the version it
    # moves from, from the versioned cache.
    for name in (*spec.words, *spec.plain):
        pid0 = int(before[name].get("pid", 0) or 0)
        roots0 = sb.plugin_roots(pid0)
        if roots0 != {sb.from_version}:
            failures.append(f"precondition: {name} runs conexus {sorted(roots0)}, not {sb.from_version} from the "
                            f"versioned cache (pid {pid0}, children {sb.children(pid0)[:12]})")
    return before


def hold_shell(sb: Sandbox, failures: list[str]) -> None:
    """At its prompt with a background shell open: status 'shell'."""
    sb.prompt("rs_shell", "Use the Bash tool with run_in_background set to true to run exactly this command: sleep 1500. "
              "Then reply with only: STARTED", done=frozenset({"idle", "shell"}))
    try:
        sb.wait_status("rs_shell", {"shell"}, 60)
    except TimeoutError as exc:
        failures.append(f"precondition: {exc}")


def hold_placeholder(sb: Sandbox, crs, name: str, text: str, mark: str, failures: list[str]) -> None:
    """A bracketed paste Claude Code collapses to a placeholder (*mark*): a draft (not dim suggestion
    text) that the pane cannot hand on."""
    sb.tmux("set-buffer", "-b", "rs-paste", text, check=True)
    sb.tmux("paste-buffer", "-p", "-d", "-b", "rs-paste", "-t", sb.pane_of(name), check=True)
    time.sleep(3)
    cap = sb.capture(name)
    if not crs.has_draft(cap) or mark not in crs.draft_text(cap):
        failures.append(f"precondition: {name}'s prompt does not read as a {mark}] draft")


def watch_roll(sb: Sandbox, timeout: float, on_tick=None) -> str:
    """Poll the restart log until the roll ends (done, ABORT or CRASH) or *timeout*; return it."""
    t0 = time.time()
    while time.time() - t0 < timeout:
        text = sb.read_log()
        if on_tick:
            on_tick(text)
        if "=== done:" in text or "=== ABORT" in text or "=== CRASH" in text:
            break
        time.sleep(1)
    return sb.read_log()


def placeholder_line() -> str:
    return "its unsent draft holds pasted text or an image, which the pane shows only as a placeholder; left running, not restarted"


def scenario_roll(sb: Sandbox, crs, a, failures: list[str], notes: list[str]) -> None:
    spec = sb.spec
    before = boot(sb, failures)
    sb.before = before
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
            shown = re.sub(r"\x1b\[[0-9;:]*m", "", line).replace("\xa0", " ").split("❯", 1)[-1].strip()
            if "\x1b[2m" in line and shown and not crs.typed_text(line).replace("\xa0", " ").split("❯", 1)[-1].strip():
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
    hold_placeholder(sb, crs, "rs_paste", PASTE_TEXT, "[Pasted text #", failures)
    hold_shell(sb, failures)
    try:
        sb.hold_busy("rs_busy")
    except TimeoutError as exc:
        failures.append(f"precondition: {exc}")
    if failures:
        return

    t0 = time.time()
    sb.tmux("send-keys", "-t", "rs:ctl", "-l", roll_command(sb), check=True)
    sb.tmux("send-keys", "-t", "rs:ctl", "Enter", check=True)
    # The draft must survive the ask: from the moment rs_draft's handoff file is complete until
    # the script exits that session, its prompt must still hold the draft. Sampled while the roll
    # runs; the last sample before the exit is the one judged.
    draft_seen: list[bool] = []
    draft_marker = f"HANDOFF-COMPLETE {before['rs_draft'].get('sessionId', '')}"
    draft_glob = f"nexus-continuation-restart-rs_draft-{before['rs_draft'].get('sessionId', '')[:8]}-*.md"

    def tick(text: str) -> None:
        if "exit rs_draft" in text:
            return
        for f in Path("/tmp").glob(draft_glob):
            if f.read_text(encoding="utf-8", errors="replace").strip().endswith(draft_marker):
                seen = DRAFT_TEXT in crs.draft_text(sb.capture("rs_draft"))
                # The exit may have started during the capture: such a sample proves nothing.
                if "exit rs_draft" not in sb.read_log():
                    draft_seen[:] = [seen]

    log = watch_roll(sb, a.timeout, tick)
    print(f"[sandbox:{spec.name}] restart log:\n" + "\n".join("    " + ln for ln in log.splitlines()), flush=True)
    assert sb.engine is not None
    expect = [
        "handoff asked of rs_alpha by mail", "handoff asked of rs_draft by mail",
        "handoff asked of rs_nochan by typed (launched without the conexus channel)",
        "handoff written by rs_alpha", "handoff written by rs_draft", "handoff written by rs_nochan",
        "OK rs_alpha: fresh as pid", "OK rs_draft: fresh as pid", "OK rs_nochan: fresh as pid",
        "rs_draft: unsent draft saved to",
        "handoff asked of rs_paste by mail", "handoff written by rs_paste",
        f"engine gate: read of {sb.engine.url}/version failed (",
        f"engine gate: v{sb.to_version} requires engine {sb.floor}; {sb.engine.url} serves {sb.behind}",
        f"engine gate: v{sb.to_version} requires engine {sb.floor}; {sb.engine.url} serves {sb.floor}",
        f"engine gate skipped (v{sb.to_version} requires engine {sb.floor})",
        f"reinstall-tool conexus=={sb.to_version}: exit 0", f"nx: nx, version {sb.to_version}",
        "plugins: {" + ", ".join(f"'{p}@{MARKET}': '{sb.to_version}'" for p in MARKET_PLUGINS) + "}",
        "=== done: 3 of 6 restarted (3 fresh, 0 resumed)",
    ]
    if sb.engine.reads != 3:
        failures.append(f"engine gate: /version was read {sb.engine.reads} time(s), not three (502, behind, then at the floor)")
    for name in ("rs_alpha", "rs_nochan"):
        if f"{name}: unsent draft saved to" in log:
            failures.append(f"{name}: a draft was saved for a session with nothing typed")
    w = spec.wait
    held = {
        f"SKIP rs_busy: still busy after {w}s; no handoff asked, not restarted",
        f"SKIP rs_shell: still shell after {w}s; no handoff asked, not restarted",
        "rs_busy: no handoff; will restart with --resume (fallback)",
        "rs_shell: no handoff; will restart with --resume (fallback)",
        f"SKIP rs_busy: still busy after {w}s; not restarted",
        f"SKIP rs_shell: still shell after {w}s; not restarted",
        f"SKIP rs_paste: {placeholder_line()}",
    }
    lines = check_lines(log, expect, held, failures)
    # The gate waits through a failed and a lagging read, and only then does the roll begin.
    reads = [i for i, ln in enumerate(lines) if ln.startswith("engine gate: ")]
    start = next((i for i, ln in enumerate(lines) if ln.startswith("=== restart ")), -1)
    if len(reads) != 3 or start < 0 or max(reads) > start:
        failures.append(f"engine gate: its three reads (lines {reads}) do not all come before the roll starts (line {start})")
    busy_now = (sb.session("rs_busy") or {}).get("status")
    if busy_now != "busy":
        failures.append(f"rs_busy: its hold ended before the roll did (status {busy_now}); its checks prove nothing")
    sb.holds["rs_busy"].unlink(missing_ok=True)
    check_install(sb, lines, failures)
    check_left_running(sb, spec.held, before, log, failures)
    if "[Pasted text #" not in crs.draft_text(sb.capture("rs_paste")):
        failures.append("rs_paste: its pasted draft is gone from its prompt")
    check_caller_last(spec, lines, failures, notes)
    if not draft_seen:
        failures.append("rs_draft: its complete handoff was never seen before it was exited; draft not checked")
    elif not draft_seen[0]:
        failures.append("rs_draft: its draft was gone from the prompt once its handoff was written")
    else:
        notes.append("rs_draft: draft still in its prompt after the mail ask and its handoff")
    check_mailed_untyped(sb, before, failures)
    handoffs = dict(re.findall(r"handoff written by (\S+): (\S+) \(", log))
    check_fresh(sb, before, handoffs, log, failures, notes)
    # Negative control: a fresh session in the same folder with no handoff must not know any word.
    sb.start_session("rs_blank")
    sb.wait_status("rs_blank", {"idle"}, 120)
    blank_reply = sb.prompt("rs_blank", "What is the project code word for this task? If you do not know it, "
                                        "reply with only: UNKNOWN").upper()
    if any(w in blank_reply for w in spec.words.values()):
        failures.append(f"negative control: a fresh session with no handoff knew a word: {blank_reply[:80]!r}")
    elif "UNKNOWN" not in blank_reply:
        failures.append(f"negative control: no usable reply from the no-handoff session ({blank_reply[:80]!r})")
    else:
        notes.append(f"negative control: a fresh session with no handoff replied {blank_reply[:40]!r}")
    notes.append(f"roll took {time.time() - t0:.0f}s")


def scenario_variant(sb: Sandbox, crs, a, failures: list[str], notes: list[str]) -> None:
    spec = sb.spec
    before = boot(sb, failures)
    sb.before = before
    img = sb.home / "dot.png"
    write_png(img)
    hold_placeholder(sb, crs, "rs_image", str(img), "[Image #", failures)
    hold_shell(sb, failures)
    try:
        sb.hold_busy("rs_late")
    except TimeoutError as exc:
        failures.append(f"precondition: {exc}")
    if failures:
        return
    t0 = time.time()
    sb.tmux("send-keys", "-t", "rs:ctl", "-l", roll_command(sb), check=True)
    sb.tmux("send-keys", "-t", "rs:ctl", "Enter", check=True)
    released: list[float] = []

    def tick(text: str) -> None:
        # rs_late stays busy until every other asked session's handoff is written, then goes idle
        # inside the wait: it must be asked then, and nothing before it may have waited on it.
        if not released and all(f"handoff written by {n}:" in text for n in spec.release_after):
            if (sb.session("rs_late") or {}).get("status") != "busy":
                failures.append("rs_late: no longer busy when the other handoffs were written; its checks prove nothing")
            sb.holds["rs_late"].unlink(missing_ok=True)
            released.append(time.time() - t0)

    log = watch_roll(sb, a.timeout, tick)
    print(f"[sandbox:{spec.name}] restart log:\n" + "\n".join("    " + ln for ln in log.splitlines()), flush=True)
    if not released:
        failures.append("rs_late: never released (the other handoffs were not all written)")
    else:
        notes.append(f"rs_late: released {released[0]:.0f}s into the roll, after the other handoffs")
    expect = [
        "handoff asked of rs_alpha by mail", "handoff written by rs_alpha",
        "handoff asked of rs_image by mail", "handoff written by rs_image",
        "handoff asked of rs_late by mail", "handoff written by rs_late",
        "OK rs_alpha: fresh as pid", "OK rs_late: fresh as pid",
        f"engine gate: v{sb.to_version} requires engine {sb.floor}; {sb.engine.url if sb.engine else ''} serves {sb.floor}",
        f"reinstall-tool conexus=={sb.to_version}: exit 0", f"nx: nx, version {sb.to_version}",
        "=== done: 2 of 4 restarted (2 fresh, 0 resumed)",
    ]
    # Without --fallback-resume a session with no handoff is dropped before the restart phase: its
    # handoff-phase SKIP is its only line (no fallback line, no second SKIP after another wait).
    held = {
        f"SKIP rs_shell: still shell after {spec.wait}s; no handoff asked, not restarted",
        f"SKIP rs_image: {placeholder_line()}",
    }
    lines = check_lines(log, expect, held, failures)
    if sum(1 for ln in lines if ln.startswith("SKIP rs_shell")) != 1:
        failures.append("rs_shell: not exactly one SKIP line; without --fallback-resume it is dropped after the handoffs")
    late_ask = next((i for i, ln in enumerate(lines) if ln.startswith("handoff asked of rs_late ")), -1)
    for n in spec.release_after:
        written = next((i for i, ln in enumerate(lines) if ln.startswith(f"handoff written by {n}:")), -1)
        if late_ask < 0 or written < 0 or written > late_ask:
            failures.append(f"rs_late: asked at line {late_ask}, not after {n}'s handoff (line {written}); "
                            "a busy session held up another")
    check_install(sb, lines, failures)
    check_left_running(sb, spec.held, before, log, failures)
    if "[Image #" not in crs.draft_text(sb.capture("rs_image")):
        failures.append("rs_image: its image draft is gone from its prompt")
    check_caller_last(spec, lines, failures, notes)
    check_mailed_untyped(sb, before, failures)
    handoffs = dict(re.findall(r"handoff written by (\S+): (\S+) \(", log))
    check_fresh(sb, before, handoffs, log, failures, notes)
    notes.append(f"roll took {time.time() - t0:.0f}s")


def scenario_abort(sb: Sandbox, crs, a, failures: list[str], notes: list[str]) -> None:
    spec = sb.spec
    before = boot(sb, failures)
    sb.before = before
    if failures:
        return
    t0 = time.time()
    sb.tmux("send-keys", "-t", "rs:ctl", "-l", roll_command(sb), check=True)
    sb.tmux("send-keys", "-t", "rs:ctl", "Enter", check=True)
    log = watch_roll(sb, a.timeout)
    print(f"[sandbox:{spec.name}] restart log:\n" + "\n".join("    " + ln for ln in log.splitlines()), flush=True)
    expect = [
        "handoff asked of rs_alpha by mail", "handoff written by rs_alpha",
        "handoff asked of rs_nochan by typed (launched without the conexus channel)", "handoff written by rs_nochan",
        f"reinstall-tool conexus=={sb.to_version}: exit 1",
    ]
    aborts = {
        f"ABORT: reinstall-tool exited 1 (nx reports 'nx, version {sb.to_version}'). Nothing restarted.",
        "=== ABORT: install failed after the handoffs; no session restarted (handoffs kept in /tmp)",
    }
    lines = check_lines(log, expect, aborts, failures)
    for ln in lines:
        if ln.startswith(("exit rs_", "OK rs_", "plugin update", "plugin marketplace update", "plugins: ", "nx: ", "=== done")):
            failures.append(f"abort: the roll went on past the failed install: {ln[:160]}")
    check_install(sb, lines, failures)
    check_left_running(sb, spec.plain, before, log, failures)
    handoffs = dict(re.findall(r"handoff written by (\S+): (\S+) \(", log))
    for name in spec.plain:
        if not Path(handoffs.get(name, "/nonexistent")).is_file():
            failures.append(f"{name}: its handoff {handoffs.get(name)} was not kept after the ABORT")
        roots = sb.plugin_roots(int(before[name].get("pid", 0) or 0))
        if roots != {sb.from_version}:
            failures.append(f"{name}: runs conexus {sorted(roots)} after the ABORT, not {sb.from_version}")
    if (sb.cache / "conexus" / sb.to_version).exists():
        failures.append(f"abort: {sb.to_version} reached the plugin cache although the install failed")
    notes.append(f"abort took {time.time() - t0:.0f}s; handoffs kept: {sorted(handoffs)}")


def scenario_gate(sb: Sandbox, crs, a, failures: list[str], notes: list[str]) -> None:
    spec = sb.spec
    before = boot(sb, failures)
    sb.before = before
    if failures:
        return
    assert sb.engine is not None
    runs = (("502", f"engine gate: read of {sb.engine.url}/version failed (",
             f"ABORT: could not read the cloud engine's version from {sb.engine.url}; nothing installed, nothing restarted."),
            (sb.behind, f"engine gate: v{sb.to_version} requires engine {sb.floor}; {sb.engine.url} serves {sb.behind}",
             f"ABORT: the cloud engine is behind v{sb.to_version}'s floor; deploy it first. Nothing installed, nothing restarted."))
    for i, (answer, gate_line, abort) in enumerate(runs, 1):
        sb.engine.answers, sb.engine.reads = [answer], 0
        rc_file = sb.home / f"gate-rc-{i}"
        n0 = len(sb.read_log().splitlines())
        sb.tmux("send-keys", "-t", "rs:ctl", "-l", roll_command(sb, rc_file=rc_file), check=True)
        sb.tmux("send-keys", "-t", "rs:ctl", "Enter", check=True)
        deadline = time.time() + 120
        while not rc_file.exists() and time.time() < deadline:
            time.sleep(1)
        time.sleep(1)
        rc = rc_file.read_text(encoding="utf-8").strip() if rc_file.exists() else "none"
        log = "\n".join(sb.read_log().splitlines()[n0:])
        print(f"[sandbox:{spec.name}] gate run {i} ({answer}) log:\n" + "\n".join("    " + ln for ln in log.splitlines()), flush=True)
        if rc != "2":
            failures.append(f"gate run {i} ({answer}): the script exited {rc}, not 2")
        lines = body_lines(log)
        if not any(ln.startswith(gate_line) for ln in lines):
            failures.append(f"gate run {i}: log lacks: {gate_line}")
        if abort not in lines:
            failures.append(f"gate run {i}: log lacks: {abort}")
        if sb.engine.reads != 1:
            failures.append(f"gate run {i}: /version read {sb.engine.reads} time(s) with --engine-wait 0, not once")
        for ln in lines:
            if not (ln.startswith(gate_line) or ln == abort):
                failures.append(f"gate run {i}: the script went past the gate: {ln[:160]}")
    for f, what in ((sb.reinstall_argv, "reinstall-tool.sh ran"), (sb.uv_cleans, "uv cache clean ran")):
        if f.exists():
            failures.append(f"gate: {what} although the gate refused")
    check_left_running(sb, spec.held, before, sb.read_log(), failures)
    if sb.events(before["rs_alpha"].get("sessionId", "")) and any(
            "nexus-continuation-restart" in json.dumps(e) for e in sb.events(before["rs_alpha"].get("sessionId", ""))):
        failures.append("gate: rs_alpha was asked for a handoff although the gate refused")


RUNNERS = {"roll": scenario_roll, "variant": scenario_variant, "abort": scenario_abort, "gate": scenario_gate}


def _args_of(pid: int) -> str:
    return subprocess.run(["ps", "-ww", "-o", "args=", "-p", str(pid)], capture_output=True, text=True).stdout.strip()


def _naming(home: Path) -> list[tuple[int, str]]:
    """Live processes whose argv names the scratch HOME (its mkdtemp path is unique to this run)."""
    # The mkdtemp path holds only [A-Za-z0-9_/.-]: as a pgrep pattern its "." alone is not literal, harmlessly.
    out = subprocess.run(["pgrep", "-f", str(home)], capture_output=True, text=True).stdout.split()
    return [(int(p), _args_of(int(p))) for p in out if p.isdigit() and int(p) not in (os.getpid(), 1)]


def _end(procs: list[tuple[int, str]]) -> int:
    """SIGTERM each process still running the same argv, SIGKILL any still there 5 s later; return
    how many were signalled."""
    sent = 0
    for sig, pause in ((signal.SIGTERM, 5), (signal.SIGKILL, 1)):
        live = [(p, args) for p, args in procs if args and _args_of(p) == args]  # not a reused pid
        for p, _ in live:
            try:
                os.kill(p, sig)
                sent += sig == signal.SIGTERM
            except (ProcessLookupError, PermissionError):
                pass
        if live:
            time.sleep(pause)
    return sent


def cleanup(sb: Sandbox, notes: list[str]) -> list[str]:
    """End the sandbox and prove it ended; return what is still dirty (each one fails the run).

    First our tmux server alone (-L): it holds the token, and its claude processes end with it;
    then the config copy they were reading. The sessions' tool processes (rs_shell's background
    sleep, a busy session's hold) and the killed sessions' MCP servers and hooks can outlive their
    claude and carry its environment, token included: the descendants collected before the kill, and
    anything whose argv still names the scratch HOME after 60 s, are ended by checked pid; so is every
    process the watch recorded below the tmux server during the run (an exited session's Serena and
    SessionEnd hooks, reparented with no HOME in their argv). The config is deleted again at the
    end, for a lease minted by a process that lingered. The caller runs it under signals_ignored()."""
    leftovers: list[tuple[int, str]] = []
    try:  # never at the cost of the kill-server and the config deletion below
        sb.watch.poll()
        sb.watch.stop()
        roots = [int(d.get("pid", 0) or 0) for d in sb.entries()]
        leftovers = [c for r in roots if r > 1 for c in sb.descendants(r) if c[0] > 1]
        leftovers = sorted(set(leftovers) | set(sb.watch.seen()))
    except Exception as exc:  # noqa: BLE001 -- reported, and the token cleanup still runs
        notes.append(f"leftover tool processes not collected: {type(exc).__name__}: {exc}")
    sb.tmux("kill-server")
    sb.delete_config()
    time.sleep(3)
    ended = _end(leftovers)
    for _ in range(30):
        if not _naming(sb.home):
            break
        time.sleep(2)
    late = _naming(sb.home)
    ended_late = _end(late)
    sb.delete_config()
    notes.append(f"cleanup: {len(leftovers)} session descendant(s) collected, {ended} still running and signalled; "
                 f"{ended_late} more still naming the HOME after 60 s")
    try:
        if sb.nx_refused.exists():
            refused = sb.nx_refused.read_text(encoding="utf-8").splitlines()
            notes.append(f"the nx shim refused {len(refused)} nx upgrade call(s): {refused[:5]}")
    except OSError as exc:
        notes.append(f"nx refusals not read: {exc}")
    dirty = []
    if sb.tmux("has-session", "-t", "rs").returncode == 0:
        dirty.append(f"tmux server {sb.sock} still running")
    left = credentials_left(sb.nexus_config)
    if left:
        dirty.append(f"credentials left in {sb.nexus_config}: {left}")
    if still := _naming(sb.home):
        dirty.append(f"{len(still)} process(es) still name the HOME: {[p for p, _ in still][:10]}")
    return dirty


def run_scenario(name: str, claude: str, a, crs) -> tuple[bool, list[str], list[str]]:
    spec = SPECS[name]
    home = Path(tempfile.mkdtemp(prefix=f"nx-restart-sandbox-{name}-"))
    sb = Sandbox(home, f"nx-rs-{os.getpid()}-{name}", claude, a.model, spec)
    failures: list[str] = []
    notes: list[str] = []
    ok = False
    try:
        sb.setup(crs)
        print(f"[sandbox:{name}] HOME {home}; watch: tmux -L {sb.sock} attach -t rs", flush=True)
        RUNNERS[name](sb, crs, a, failures, notes)
        ok = not failures
    except Exception as exc:  # noqa: BLE001 -- a harness fault is a failed run with its reason
        failures.append(f"harness: {type(exc).__name__}: {exc}")
    finally:
        # Nothing before the kill-server and the config deletion may raise past them: each step
        # that reads the sandbox's files is guarded and only reported, and a signal now is ignored.
        with signals_ignored():
            sids: set[str] = set()
            try:
                for hold in sb.holds.values():
                    hold.unlink(missing_ok=True)
                sids = {str(d.get("sessionId", ""))[:8] for d in sb.entries()}
                sids |= {str(d.get("sessionId", ""))[:8] for d in sb.before.values() if d}
            except Exception as exc:  # noqa: BLE001 -- reported, and the token cleanup still runs
                notes.append(f"session ids not collected: {type(exc).__name__}: {exc}")
            if not a.keep:
                dirty = cleanup(sb, notes)
                if dirty:
                    failures.extend(f"cleanup: {d}" for d in dirty)
                    ok = False
            else:
                sb.watch.stop()
        sb.close_servers()
        for n in notes:
            print(f"[sandbox:{name}] {n}", flush=True)
        for f in failures:
            print(f"[sandbox:{name}] FAIL {f}", flush=True)
        print(f"[sandbox:{name}] {'passed' if ok else 'FAILED'}: {len(spec.sessions)} session(s), {len(failures)} failure(s)",
              flush=True)
        if a.keep:
            print(f"[sandbox:{name}] --keep: the sessions still run on a copy of your nexus config at "
                  f"{sb.nexus_config}/config.yml; delete it when done; tmux -L {sb.sock} kill-server", flush=True)
        if ok and not a.keep:
            for sid in filter(None, sids):  # the script's /tmp files for these sessions
                for f in Path("/tmp").glob(f"rh-{sid}.*"):
                    f.unlink(missing_ok=True)
                for f in Path("/tmp").glob(f"nexus-continuation-restart-rs_*-{sid}-*"):
                    f.unlink(missing_ok=True)
            shutil.rmtree(home, ignore_errors=True)
            if home.exists():
                print(f"[sandbox:{name}] could not remove {home}; it holds no credentials (deleted above)", flush=True)
        else:
            print(f"[sandbox:{name}] kept {home} (transcripts, restart log, handoffs under /tmp)", flush=True)
    return ok, failures, notes


def records_a_pass(chosen: list[str], bad: list[str]) -> bool:
    """Only a run of every scenario, in order, with none failed, is a pass the restart gate honours."""
    return not bad and chosen == list(SCENARIOS)


def record_full_pass(crs, claude: str, before: tuple[str, str], chosen: list[str], elapsed: float) -> str:
    """Record a full pass for the script's restart gate, if what ran is still what is installed.

    *before* is (script sha256, Claude Code version) read before the first scenario. Returns the
    line to print; records nothing when the version is unreadable, either value changed during
    the run, or the write fails.
    """
    sha, version = before
    if not version:
        return "pass NOT recorded: could not read Claude Code's version"
    after = (crs.script_sha256(SCRIPT), crs.claude_version(claude))
    if after != before:
        return (f"pass NOT recorded: the script or Claude Code changed during the run "
                f"(sha {sha[:12]} -> {after[0][:12]}, version {version} -> {after[1]})")
    try:
        crs.record_sandbox_pass(crs.SANDBOX_PASS, sha, version, chosen, elapsed)
    except OSError as e:
        return f"pass NOT recorded: could not write {crs.SANDBOX_PASS} ({e})"
    return f"recorded the pass in {crs.SANDBOX_PASS} (Claude Code {version})"


def main() -> int:
    if sys.argv[1:2] == ["--bump-market"]:  # the stand-in reinstall-tool.sh's publish step
        bump_market(Path(sys.argv[2]), sys.argv[3])
        return 0
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--model", default="sonnet")
    ap.add_argument("--keep", action="store_true", help="keep each scratch HOME and tmux server even on a pass")
    ap.add_argument("--timeout", type=int, default=1200, help="seconds for each roll to finish")
    ap.add_argument("--scenario", action="append", choices=SCENARIOS,
                    help="run only this scenario (repeatable; default: all, in order)")
    a = ap.parse_args()

    if not os.environ.get("CLAUDE_CODE_OAUTH_TOKEN"):
        print("RESTART SANDBOX UNVERIFIED: no automation token in the environment; run through run.sh", flush=True)
        return 2
    claude = shutil.which("claude")
    if not claude or not shutil.which("tmux"):
        print("RESTART SANDBOX UNVERIFIED: claude or tmux not on PATH", flush=True)
        return 2
    # A SIGTERM must still reach each scenario's finally: the private tmux server holds the token in
    # its environment and must not outlive the run.
    signal.signal(signal.SIGTERM, lambda *_: sys.exit(143))
    signal.signal(signal.SIGHUP, lambda *_: sys.exit(129))  # a closed terminal: same cleanup
    crs = load_script()
    chosen = [s for s in SCENARIOS if s in (a.scenario or SCENARIOS)]
    results = {}
    t0 = time.time()
    # The pass covers what the scenarios ran: the script's bytes and Claude Code's version as they
    # were before the first scenario, and only if neither changed by the end (a fast-forward of the
    # primary or a Claude Code auto-update mid-run would otherwise be recorded as tested).
    tested = (crs.script_sha256(SCRIPT), crs.claude_version(claude))
    for name in chosen:
        ok, failures, _ = run_scenario(name, claude, a, crs)
        results[name] = (ok, len(failures))
    bad = [n for n, (ok, _) in results.items() if not ok]
    total = sum(n for _, n in results.values())
    verdict = "RESTART SANDBOX FAILED" if bad else "RESTART SANDBOX PASSED"
    print(f"{verdict} -- {len(chosen)} scenario(s) ({', '.join(chosen)}), {total} failure(s)"
          + (f" in {', '.join(bad)}" if bad else "") + f", {time.time() - t0:.0f}s", flush=True)
    if records_a_pass(chosen, bad):
        print(record_full_pass(crs, claude, tested, chosen, time.time() - t0), flush=True)
    return 1 if bad else 0


if __name__ == "__main__":
    sys.exit(main())
