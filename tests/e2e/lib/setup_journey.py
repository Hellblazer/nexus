#!/usr/bin/env python3
# SPDX-License-Identifier: AGPL-3.0-or-later
"""The journey test for the plugin's setup path, with no network. RDR-228 P2.7 (nexus-sv2wx.20).

Driven by ``tests/e2e/setup-journey.sh``; run with the interpreter of a venv that holds the MCP
SDK (the wrapper builds one). Never run a ``conexus/hooks/hooks.json`` entry on a developer's
own machine: this file starts them, in a sandbox home, and is meant for a test host.

Three journeys, each in its own sandbox home with ``uv`` and no CLI (or, in C, a legacy one):

A  local        session start (state 6, the one question); the stand-in server answers through
                the entry in ``conexus/.mcp.json``; ``setup`` with mode ``local`` and autostart
                false is followed with ``setup_status`` to ``done``; a second connection through
                the same entry gets the real server and one storage call succeeds.
B  pruned       the generation A built, pruned the way ``uv`` prunes a managed CPython: session
                start gives state 2; the stand-in says "installed but cannot start; setup repairs
                it"; ``setup`` with mode ``cli-only`` builds a new generation beside the old one;
                after reconnect the real server answers; the session start is one quiet line.
C  legacy       a ``uv tool install conexus`` tree whose Python is pruned: the same, and the build
                runs (``nx self install --bootstrap`` no longer calls that tree a complete install).

The hook entries and the server entries are the files' own, ``${CLAUDE_PLUGIN_ROOT}`` replaced,
started as Claude Code starts them. The client is the MCP SDK's.

NO NETWORK. The journey sets ``UV_OFFLINE`` and points every proxy variable at a local listener that
refuses and records; it fails if the listener saw one request. What it needs comes from the seed
(``--seed-dir``): a ``uv`` cache that holds every package, the engine, the PostgreSQL bundle and
the two models. ``--online`` is the seed run: the same journeys, the network on, the seed filled.
It is the only run that fetches anything. A missing seed is a failure, never a skip.

The one request tolerated is ``nx doctor``'s refused best-effort PyPI version probe (``TOLERATED_REQUESTS``),
which the last step of ``setup`` makes and which reports "PyPI check skipped" when refused. Any other
request fails the journey.

A limit worth stating: the listener sees only clients that honour proxy variables. ``uv`` is also
told it is offline. The engine and PostgreSQL are local processes and fetch nothing.

OBSERVED while building this (not fixed here; see the bead comment): with a CLI present and no
``cli_lockstep_marker``, the SessionStart hook ``version_lockstep_hook.py`` nudges the model and dispatches an
upgrade action, also when the CLI cannot start. The first session after a fresh ``setup`` is such a session.
Journeys B and C write the marker (``NX_LOCKSTEP_MARKER``) so that the dispatched action cannot race the
repair they test.

Pure helpers (no process, no MCP) are at the top and are what ``tests/test_setup_journey_helpers.py``
exercises.
"""
from __future__ import annotations

import argparse
import asyncio
import contextlib
import hashlib
import http.server
import json
import os
import re
import shutil
import socketserver
import subprocess
import sys
import threading
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

REPO = Path(__file__).resolve().parents[3]
PLUGIN = REPO / "conexus"

#: What a poll of ``setup_status`` opens with when a run has ended, by outcome.
_DONE_HEADS = ("Setup is done.",)
_FAILED_HEADS = (
    "Setup finished, but nx doctor reported problems.",
    "Setup failed.",
    "Setup cannot run on this host.",
    "The last setup run was interrupted.",
)

#: Said by the stand-in (``nx_standin.py``) and the session-start hook (``nx_setup_notice.py``).
BROKEN_PHRASE = "Nexus is installed but cannot start; setup repairs it"
NOT_INSTALLED_PHRASE = "Nexus is not installed on this machine"
STAND_IN_NAMES = ("nexus-not-installed", "nexus-installed-cannot-start", "nexus-catalog-not-installed")

#: The setup tools, which are all a stand-in lists.
SETUP_TOOL_NAMES = frozenset({"setup", "setup_status"})
#: A few storage tools the real server must list.
REAL_TOOL_NAMES = frozenset({"search", "store_put", "memory_put", "memory_get", "scratch"})


class JourneyFailure(Exception):
    """A step failed. The message says which and why."""


def fail(message: str) -> None:
    raise JourneyFailure(message)


def expect(condition: bool, message: str) -> None:
    if not condition:
        fail(message)


# -- pure helpers -----------------------------------------------------------------------------


@dataclass(frozen=True)
class Entry:
    """A hook or a server entry with its placeholders replaced."""

    name: str
    command: str
    args: tuple[str, ...]
    timeout: float = 30.0


def substitute(text: str, plugin_root: str, project_dir: str) -> str:
    return text.replace("${CLAUDE_PLUGIN_ROOT}", plugin_root).replace("${CLAUDE_PROJECT_DIR}", project_dir)


def hook_entries(hooks: dict[str, Any], event: str, source: str, plugin_root: str, project_dir: str) -> list[Entry]:
    """The entries of *event* whose matcher selects *source*, in the file's order, as written."""
    found: list[Entry] = []
    for group in hooks["hooks"].get(event, []):
        matcher = group.get("matcher", "")
        if matcher and not re.fullmatch(matcher, source):
            continue
        for item in group["hooks"]:
            if item.get("type") != "command":
                continue
            args = tuple(substitute(a, plugin_root, project_dir) for a in item.get("args", []))
            script = max((i for i, a in enumerate(args) if a.endswith(".py")), default=-1)
            name = " ".join([os.path.basename(args[script]), *args[script + 1:]]) if script >= 0 else item["command"]
            found.append(Entry(name, item["command"], args, float(item.get("timeout", 30))))
    return found


def server_entry(mcp_json: dict[str, Any], name: str, plugin_root: str, project_dir: str) -> Entry:
    servers = mcp_json.get("mcpServers", mcp_json)
    spec = servers[name]
    args = tuple(substitute(a, plugin_root, project_dir) for a in spec.get("args", []))
    return Entry(name, spec["command"], args)


def setup_outcome(text: str) -> str | None:
    """``done`` or ``failed`` when a ``setup_status`` answer reports a run that has ended, else ``None``."""
    if text.startswith(_DONE_HEADS):
        return "done"
    if text.startswith(_FAILED_HEADS):
        return "failed"
    return None


def run_has_ended(record: dict[str, Any] | None, mode: str, previous_pid: Any) -> str | None:
    """The terminal state in a ``status.json`` record that belongs to the run just started
    (this mode, and not the previous run's process), else ``None``."""
    if not record or record.get("mode") != mode:
        return None
    if previous_pid is not None and record.get("pid") == previous_pid:
        return None
    state = record.get("state")
    return state if state in ("done", "doctor-failed", "failed", "unsupported") else None


def sandbox_env(
    *, home: Path, project: Path, bin_dir: Path, tmp: Path, uv_cache: Path, uv_python_dir: str,
    plugin_root: Path, find_links: Path | None, proxy: str | None, offline: bool,
    lockstep_marker: Path | None = None,
) -> dict[str, str]:
    """The environment of every process the journey starts: an allowlist, built from nothing.

    Deliberately absent: every ``NX_*`` and ``NEXUS_*`` steering variable, every credential, ``XDG_*``,
    ``UV_TOOL_DIR`` and the rest of ``UV_*`` (the install lands under *home*), and the caller's ``PATH``
    (a CLI the host already has would be found).
    """
    env = {
        "HOME": str(home),
        "PATH": os.pathsep.join([str(bin_dir), str(home / ".local" / "bin"), "/usr/bin", "/bin"]),
        "TERM": "dumb",
        "LANG": "C.UTF-8",
        "TMPDIR": str(tmp),
        "NX_NO_TELEMETRY": "1",
        "CLAUDE_PLUGIN_ROOT": str(plugin_root),
        "CLAUDE_PROJECT_DIR": str(project),
        "UV_CACHE_DIR": str(uv_cache),
        "UV_PYTHON_INSTALL_DIR": uv_python_dir,
        "UV_PYTHON_DOWNLOADS": "never",
        "UV_NO_PROGRESS": "1",
    }
    if find_links is not None:
        env["UV_FIND_LINKS"] = str(find_links)
    if lockstep_marker is not None:
        # The version-lockstep hook's own seam. Without a marker it takes every session that has a CLI for a
        # skewed one and dispatches an upgrade (see the module docstring, "Observed").
        env["NX_LOCKSTEP_MARKER"] = str(lockstep_marker)
    if offline:
        env["UV_OFFLINE"] = "1"
    if proxy:
        for name in ("HTTP_PROXY", "HTTPS_PROXY", "ALL_PROXY", "http_proxy", "https_proxy", "all_proxy"):
            env[name] = proxy
        env["NO_PROXY"] = env["no_proxy"] = "127.0.0.1,localhost,::1"
    return env


def prune_python(venv: Path, gone: Path) -> Path:
    """Leave *venv* as ``uv`` leaves one whose managed CPython it pruned (the same as
    ``tests/_platform.prune_python``, restated because that module needs pytest): ``pyvenv.cfg``
    names *gone* as ``home`` and, on POSIX, ``bin/python`` and ``bin/python3`` dangle. A gone
    ``home`` alone proves nothing on POSIX; the launcher and the hook want both."""
    cfg = venv / "pyvenv.cfg"
    kept = [
        line for line in (cfg.read_text(encoding="utf-8").splitlines() if cfg.exists() else [])
        if line.partition("=")[0].strip() != "home"
    ]
    cfg.write_text("\n".join([f"home = {gone}", *kept]) + "\n", encoding="utf-8")
    if os.name != "nt":
        for name in ("python", "python3"):
            link = venv / "bin" / name
            if link.is_symlink() or link.exists():
                link.unlink()
            link.symlink_to(gone / "bin" / name)
    return gone


def generations(tools: Path) -> list[Path]:
    """The ``gen-*`` directories under *tools* that carry a receipt, oldest first."""
    if not tools.is_dir():
        return []
    return sorted(p for p in tools.glob("gen-*") if (p / "nexus-install.json").is_file())


def stand_in_claim(name: str | None) -> bool:
    return name in STAND_IN_NAMES


# -- the recording proxy: the journey's no-network witness ------------------------------------


class _Refuse(http.server.BaseHTTPRequestHandler):
    seen: list[str]

    def _refuse(self) -> None:
        self.server.seen.append(f"{self.command} {self.path}")  # type: ignore[attr-defined]
        self.send_response(403)
        self.send_header("Content-Length", "0")
        self.end_headers()

    do_GET = do_POST = do_PUT = do_HEAD = do_DELETE = do_CONNECT = _refuse

    def log_message(self, *_: Any) -> None:  # silent
        pass


class RefusingProxy:
    """A local HTTP proxy that refuses every request and remembers what was asked for."""

    def __init__(self) -> None:
        class Server(socketserver.ThreadingMixIn, http.server.HTTPServer):
            daemon_threads = True

        self._server = Server(("127.0.0.1", 0), _Refuse)
        self._server.seen = []  # type: ignore[attr-defined]
        self._thread = threading.Thread(target=self._server.serve_forever, daemon=True)

    @property
    def url(self) -> str:
        return f"http://127.0.0.1:{self._server.server_address[1]}"

    @property
    def seen(self) -> list[str]:
        return list(self._server.seen)  # type: ignore[attr-defined]

    def __enter__(self) -> RefusingProxy:
        self._thread.start()
        return self

    def __exit__(self, *_: Any) -> None:
        self._server.shutdown()
        self._server.server_close()


# -- logging -----------------------------------------------------------------------------------

_T0 = time.monotonic()
TIMINGS: list[tuple[str, float]] = []


def say(message: str) -> None:
    print(f"[{time.monotonic() - _T0:7.1f}s] {message}", flush=True)  # noqa: T201 -- the harness's own output


class Step:
    def __init__(self, label: str) -> None:
        self.label = label

    def __enter__(self) -> None:
        self.t = time.monotonic()
        say(f"-- {self.label}")

    def __exit__(self, exc_type: Any, exc: Any, tb: Any) -> None:
        took = time.monotonic() - self.t
        TIMINGS.append((self.label, took))
        say(f"   {'ok' if exc is None else 'FAILED'} ({took:.1f}s)")


# -- the sandbox -------------------------------------------------------------------------------


@dataclass
class Sandbox:
    """One machine: a home with ``uv`` and nothing of Nexus's."""

    name: str
    root: Path
    uv: str
    uv_cache: Path
    uv_python_dir: str
    wheels: Path
    proxy: str | None
    offline: bool
    lockstep_marker: Path | None = None
    notes: list[str] = field(default_factory=list)

    def __post_init__(self) -> None:
        self.home = self.root / "home"
        self.project = self.root / "project"
        self.bin = self.root / "bin"
        self.tmp = self.root / "tmp"
        for d in (self.home, self.project, self.bin, self.tmp):
            d.mkdir(parents=True, exist_ok=True)
        link = self.bin / "uv"
        if not link.exists():
            link.symlink_to(self.uv)

    @property
    def config(self) -> Path:
        return self.home / ".config" / "nexus"

    @property
    def tools(self) -> Path:
        return self.home / ".local" / "share" / "nexus" / "tools"

    @property
    def uv_tools(self) -> Path:
        return self.home / ".local" / "share" / "uv" / "tools"

    def env(self) -> dict[str, str]:
        return sandbox_env(
            home=self.home, project=self.project, bin_dir=self.bin, tmp=self.tmp, uv_cache=self.uv_cache,
            uv_python_dir=self.uv_python_dir, plugin_root=PLUGIN, find_links=self.wheels,
            proxy=self.proxy, offline=self.offline, lockstep_marker=self.lockstep_marker,
        )

    def confirm_lockstep(self) -> None:
        """Record, as an earlier session's upgrade action would, that the CLI matches the plugin."""
        version = json.loads((PLUGIN / ".claude-plugin" / "plugin.json").read_text(encoding="utf-8"))["version"]
        self.lockstep_marker = self.tmp / "cli_lockstep_marker"
        self.lockstep_marker.write_text(version + "\n", encoding="utf-8")

    def current(self) -> Path | None:
        link = self.tools / "current"
        try:
            return Path(os.readlink(link))
        except OSError:
            return None

    def nx(self) -> str:
        current = self.current()
        expect(current is not None, f"{self.name}: no generation is current under {self.tools}")
        return str(current / "bin" / "nx")  # type: ignore[operator]

    def status_record(self) -> dict[str, Any] | None:
        try:
            return json.loads((self.config / "setup" / "status.json").read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return None

    def setup_log_tail(self, lines: int = 60) -> str:
        try:
            return "\n".join((self.config / "setup" / "setup.log").read_text(errors="replace").splitlines()[-lines:])
        except OSError:
            return "(no setup.log)"

    # -- processes ------------------------------------------------------------------------

    def run(self, argv: list[str], *, timeout: float, stdin: str = "", cwd: Path | None = None) -> subprocess.CompletedProcess[str]:
        return subprocess.run(  # noqa: S603 -- argv from the plugin's own files
            argv, input=stdin, capture_output=True, text=True, encoding="utf-8", errors="replace",
            env=self.env(), cwd=str(cwd or self.project), timeout=timeout, check=False,
        )

    def stop_service(self) -> None:
        """Stop the sandbox's service and PostgreSQL, whatever state the journey ended in."""
        current = self.current()
        if current is None:
            return
        nx = current / "bin" / "nx"
        if not nx.exists():
            return
        try:
            done = self.run([str(nx), "daemon", "service", "stop", "--with-pg"], timeout=120)
            say(f"   {self.name}: service stop exit {done.returncode}")
        except (OSError, subprocess.SubprocessError) as exc:
            say(f"   {self.name}: service stop could not run: {exc}")


# -- session start -----------------------------------------------------------------------------


@dataclass
class HookResult:
    entry: Entry
    code: int
    stdout: str
    stderr: str
    seconds: float


def run_session_start(box: Sandbox) -> list[HookResult]:
    """Every ``SessionStart`` entry of ``hooks.json`` for a ``startup``, in order, as written, with the
    payload Claude Code gives and the entry's own timeout. A hook past its timeout is a failure."""
    hooks = json.loads((PLUGIN / "hooks" / "hooks.json").read_text(encoding="utf-8"))
    entries = hook_entries(hooks, "SessionStart", "startup", str(PLUGIN), str(box.project))
    expect(len(entries) >= 9, f"hooks.json lists {len(entries)} SessionStart entries; the journey expected at least 9")
    payload = json.dumps({
        "hook_event_name": "SessionStart", "source": "startup", "session_id": "setup-journey",
        "cwd": str(box.project), "transcript_path": str(box.tmp / "transcript.jsonl"),
    })
    results: list[HookResult] = []
    for entry in entries:
        t = time.monotonic()
        try:
            done = box.run([_command(entry, box), *entry.args], timeout=entry.timeout, stdin=payload)
        except subprocess.TimeoutExpired:
            fail(f"{box.name}: the hook {entry.name} ran past its {entry.timeout:.0f} s timeout")
        results.append(HookResult(entry, done.returncode, done.stdout, done.stderr, time.monotonic() - t))
    return results


def _command(entry: Entry, box: Sandbox) -> str:
    return str(box.bin / "uv") if entry.command == "uv" else entry.command


def notice_text(results: list[HookResult]) -> str | None:
    """What the setup hook (``nx_setup_notice.py``) told the model, or ``None`` for silence."""
    notice = next(r for r in results if r.entry.args and r.entry.args[-1].endswith("nx_setup_notice.py"))
    if not notice.stdout.strip():
        return None
    try:
        return json.loads(notice.stdout)["hookSpecificOutput"]["additionalContext"]
    except (ValueError, KeyError, TypeError):
        fail(f"the setup hook's output is not the hook JSON: {notice.stdout[:300]!r}")
        return None


def check_hooks_are_quiet_without_a_cli(box: Sandbox, results: list[HookResult], *, reason: str) -> None:
    """Every entry but the setup hook: exit 0, nothing on standard output, and for a launcher entry
    one line on standard error that names why its program is skipped."""
    for r in results:
        expect(r.code == 0, f"{box.name}: {r.entry.name} exited {r.code}: {r.stderr[:300]!r}")
        if r.entry.args and r.entry.args[-1].endswith("nx_setup_notice.py"):
            continue
        expect(not r.stdout.strip(), f"{box.name}: {r.entry.name} wrote to standard output: {r.stdout[:200]!r}")
        if any(a.endswith("nx_launch.py") for a in r.entry.args):
            lines = [line for line in r.stderr.splitlines() if line.strip()]
            expect(len(lines) == 1 and reason in lines[0], f"{box.name}: {r.entry.name} should give one line saying {reason!r}, gave {lines!r}")


def quiet_hook_pass(box: Sandbox) -> str | None:
    """Run the setup hook alone (the other entries are the CLI's own hooks, which a CLI that starts runs for real)."""
    entry = next(e for e in hook_entries(
        json.loads((PLUGIN / "hooks" / "hooks.json").read_text(encoding="utf-8")), "SessionStart", "startup",
        str(PLUGIN), str(box.project)) if e.args[-1].endswith("nx_setup_notice.py"))
    done = box.run([_command(entry, box), *entry.args], timeout=entry.timeout, stdin="{}")
    expect(done.returncode == 0, f"{box.name}: the setup hook exited {done.returncode}")
    if not done.stdout.strip():
        return None
    return json.loads(done.stdout)["hookSpecificOutput"]["additionalContext"]


# -- the MCP client ----------------------------------------------------------------------------


@dataclass
class Connection:
    server_name: str
    instructions: str
    tools: frozenset[str]
    calls: list[tuple[str, str]] = field(default_factory=list)


@contextlib.asynccontextmanager
async def _session(box: Sandbox, server: str):
    """One connection to *server* through its entry in ``.mcp.json``, started as Claude Code starts it."""
    from mcp import ClientSession, StdioServerParameters  # noqa: PLC0415 -- the wrapper's venv has it; helpers do not need it
    from mcp.client.stdio import stdio_client  # noqa: PLC0415

    mcp_json = json.loads((PLUGIN / ".mcp.json").read_text(encoding="utf-8"))
    entry = server_entry(mcp_json, server, str(PLUGIN), str(box.project))
    params = StdioServerParameters(command=_command(entry, box), args=list(entry.args), env=box.env(), cwd=str(box.project))
    with open(box.tmp / f"mcp-{server}.stderr", "a", encoding="utf-8") as errlog:
        async with stdio_client(params, errlog=errlog) as (read, write):
            async with ClientSession(read, write) as session:
                init = await asyncio.wait_for(session.initialize(), timeout=120)
                listed = await asyncio.wait_for(session.list_tools(), timeout=120)
                conn = Connection(init.serverInfo.name, init.instructions or "", frozenset(t.name for t in listed.tools))
                yield session, conn


async def _call(box: Sandbox, session: Any, server: str, name: str, arguments: dict[str, Any], timeout: float) -> str:
    result = await asyncio.wait_for(session.call_tool(name, arguments), timeout=timeout)
    text = "\n".join(getattr(c, "text", "") for c in result.content)
    if result.isError:
        fail(f"{box.name}: {server}.{name} returned an error: {text[:400]}")
    return text


async def _talk(box: Sandbox, server: str, calls: list[tuple[str, dict[str, Any]]], *, call_timeout: float) -> tuple[Connection, list[str]]:
    answers: list[str] = []
    async with _session(box, server) as (session, conn):
        for name, arguments in calls:
            answers.append(await _call(box, session, server, name, arguments, call_timeout))
    return conn, answers


def talk(box: Sandbox, server: str, calls: list[tuple[str, dict[str, Any]]] | None = None, *, call_timeout: float = 180) -> tuple[Connection, list[str]]:
    return asyncio.run(_talk(box, server, calls or [], call_timeout=call_timeout))


async def _follow(box: Sandbox, mode: str, args: dict[str, Any], budget_s: float, stand_in: str) -> str:
    """The way a model uses the stand-in: ONE connection, ``setup``, then ``setup_status`` until the run ends.
    The connection stays the stand-in for its whole life, however the machine changes under it."""
    previous = box.status_record()
    previous_pid = previous.get("pid") if previous else None
    async with _session(box, "nexus") as (session, conn):
        expect(conn.server_name == stand_in, f"{box.name}: expected the stand-in {stand_in!r} to take the call, got {conn.server_name!r}")
        started = await _call(box, session, "nexus", "setup", args, 120)
        expect(started.startswith(("Setup started in mode", "A setup run is in progress")),
               f"{box.name}: setup did not start a run: {started[:300]!r}")
        deadline = time.monotonic() + budget_s
        last = started
        while time.monotonic() < deadline:
            await asyncio.sleep(10)
            last = await _call(box, session, "nexus", "setup_status", {}, 120)
            ended = run_has_ended(box.status_record(), mode, previous_pid)
            if ended is None:
                continue
            if ended == "done" and setup_outcome(last) == "done":
                return last
            fail(f"{box.name}: setup mode {mode} ended {ended}.\n--- setup_status said:\n{last[:800]}\n--- end of setup.log:\n{box.setup_log_tail()}")
    fail(f"{box.name}: setup mode {mode} did not end within {budget_s:.0f} s. Last answer:\n{last[:600]}\n--- end of setup.log:\n{box.setup_log_tail()}")
    return last


def follow_setup(box: Sandbox, mode: str, *, autostart: bool, budget_s: float, stand_in: str) -> str:
    """Call ``setup`` through the stand-in and follow ``setup_status`` on that connection to the run's end."""
    args: dict[str, Any] = {"mode": mode}
    if mode == "local":
        args["autostart"] = autostart
    return asyncio.run(_follow(box, mode, args, budget_s, stand_in))


# -- the journeys ------------------------------------------------------------------------------


def precondition_no_cli(box: Sandbox) -> None:
    """The box has ``uv`` and none of Nexus's: no CLI on its PATH or in its layout, nothing under its config."""
    path = box.env()["PATH"]
    for program in ("nx", "nx-mcp", "nx-hook"):
        found = shutil.which(program, path=path)
        expect(found is None, f"{box.name}: {program} is already on the sandbox PATH at {found}")
    expect(shutil.which("uv", path=path) is not None, f"{box.name}: uv is not on the sandbox PATH")
    expect(not box.config.exists(), f"{box.name}: {box.config} exists before the journey began")
    expect(not box.tools.exists(), f"{box.name}: {box.tools} exists before the journey began")


#: The one request the journey tolerates, refused: ``nx doctor`` (the last step of ``setup``) asks
#: pypi.org for the newest version of conexus (``nexus.health._check_cli_version``), a best-effort probe
#: with a 3 second timeout that reports "PyPI check skipped" when refused. Nothing is fetched or needed
#: from it. Anything else the listener sees fails the journey.
TOLERATED_REQUESTS = frozenset({"CONNECT pypi.org:443"})
TOLERATED_EVIDENCE = "PyPI check skipped"


def unexpected_requests(seen: list[str]) -> list[str]:
    return [request for request in seen if request not in TOLERATED_REQUESTS]


def no_network_so_far(proxy: RefusingProxy | None) -> None:
    if proxy is not None:
        expect(not unexpected_requests(proxy.seen), f"the journey tried to reach the network: {unexpected_requests(proxy.seen)[:10]}")


def tree_wheel_is_installed(generation: Path) -> None:
    """The generation was built from THIS tree's wheel, not a published one of the same version."""
    mine = (REPO / "src" / "nexus" / "engine_version.py").read_bytes()
    py = generation / "bin" / "python"
    done = subprocess.run(  # noqa: S603 -- the generation's own interpreter
        [str(py), "-I", "-c", "import nexus.engine_version as m, sys; sys.stdout.buffer.write(open(m.__file__,'rb').read())"],
        capture_output=True, timeout=60, check=False,
    )
    expect(done.returncode == 0, f"cannot import nexus from {generation}: {done.stderr[:200]!r}")
    expect(hashlib.sha256(done.stdout).digest() == hashlib.sha256(mine).digest(),
           f"{generation.name} holds a nexus that is not this tree's (a published wheel of the same version?)")


def journey_a_local(box: Sandbox, proxy: RefusingProxy | None, *, seed: Path, online: bool) -> None:
    with Step("A1 the box: uv, no CLI, nothing under the config or tools directory"):
        precondition_no_cli(box)
    with Step("A2 session start: nine entries as written; the setup hook asks the one question (state 6)"):
        results = run_session_start(box)
        check_hooks_are_quiet_without_a_cli(box, results, reason="is not installed")
        text = notice_text(results)
        expect(text is not None and NOT_INSTALLED_PHRASE in text, f"A: state 6 text missing: {text!r}")
        expect("ASK THE USER FIRST" in text and "| Local |" in text, "A: the one question or its table is missing from the hook's text")  # type: ignore[operator]
        expect(not box.config.exists() and not box.tools.exists(), "A: session start wrote under the config or tools directory")
    with Step("A3 the stand-in: the nexus entry answers a handshake and lists exactly the two setup tools"):
        conn, _ = talk(box, "nexus")
        expect(conn.server_name == "nexus-not-installed", f"A: server name {conn.server_name!r}")
        expect(conn.tools == SETUP_TOOL_NAMES, f"A: stand-in tools {sorted(conn.tools)}")
        expect("not installed" in conn.instructions, "A: the stand-in's instructions do not say Nexus is not installed")
        cat, _ = talk(box, "nexus-catalog")
        expect(cat.tools == frozenset(), f"A: the catalog stand-in lists tools {sorted(cat.tools)}")
    if not online:
        with Step("A4 place the seeded engine, PostgreSQL bundle and models where nx init looks (it then downloads nothing)"):
            place_service_files(box, seed)
    with Step("A5 setup mode local, autostart false: follow setup_status to done (build, then nx init, then nx doctor)"):
        last = follow_setup(box, "local", autostart=False, budget_s=2400, stand_in="nexus-not-installed")
        say("   " + last.splitlines()[0])
    with Step("A6 what the run left: a generation built from this tree, current, shims; no login unit; no network"):
        gens = generations(box.tools)
        expect(len(gens) == 1, f"A: expected one generation, found {[g.name for g in gens]}")
        expect(box.current() == gens[0] or (box.current() and box.current().resolve() == gens[0].resolve()), "A: current does not name the generation")  # type: ignore[union-attr]
        tree_wheel_is_installed(gens[0])
        units = [p for base in (box.home / ".config" / "systemd" / "user", box.home / "Library" / "LaunchAgents")
                 if base.is_dir() for p in base.iterdir() if "nexus" in p.name.lower()]
        expect(not units, f"A: a login unit was registered although autostart was false: {units}")
        no_network_so_far(proxy)
    with Step("A7 reconnect through the same entry: the real server answers, and a storage call succeeds"):
        conn, answers = talk(box, "nexus", [
            ("memory_put", {"project": "setup-journey", "title": "probe", "content": "journey-a"}),
            ("memory_get", {"project": "setup-journey", "title": "probe"}),
        ])
        expect(not stand_in_claim(conn.server_name), f"A: after reconnect the server is still the stand-in {conn.server_name!r}")
        expect(REAL_TOOL_NAMES <= conn.tools and SETUP_TOOL_NAMES <= conn.tools, f"A: real server tools incomplete: missing {sorted((REAL_TOOL_NAMES | SETUP_TOOL_NAMES) - conn.tools)}")
        expect("journey-a" in answers[1], f"A: memory_get did not return what memory_put stored: {answers[1][:200]!r}")
    with Step("A8 session start with a usable service: the setup hook says nothing (state 3)"):
        expect(quiet_hook_pass(box) is None, "A: the setup hook spoke although a CLI and a service are usable")


def journey_b_pruned_generation(box: Sandbox, proxy: RefusingProxy | None) -> None:
    """Continues A's box: its generation, then pruned."""
    with Step("B1 stop the service, prune the generation's Python as uv does (home gone, python links dangling)"):
        box.confirm_lockstep()
        box.stop_service()
        current = box.current()
        expect(current is not None, "B: A left no current generation")
        old = Path(os.path.realpath(current))  # type: ignore[arg-type]
        prune_python(old, box.root / "pruned-python")
        before = {g.name for g in generations(box.tools)}
    with Step("B2 session start (state 2): every launcher entry gives one line and exit 0; the setup hook says cannot start"):
        results = run_session_start(box)
        check_hooks_are_quiet_without_a_cli(box, results, reason="could not start")
        text = notice_text(results)
        expect(text is not None and BROKEN_PHRASE in text, f"B: state 2 text missing: {text!r}")
    with Step("B3 the stand-in says installed but cannot start; setup_status keeps the offer over the old done"):
        conn, answers = talk(box, "nexus", [("setup_status", {})])
        expect(conn.server_name == "nexus-installed-cannot-start", f"B: server name {conn.server_name!r}")
        expect(conn.tools == SETUP_TOOL_NAMES, f"B: stand-in tools {sorted(conn.tools)}")
        expect(BROKEN_PHRASE in conn.instructions, "B: the instructions lack the statement")
        expect(BROKEN_PHRASE in answers[0] and "did not repair" in answers[0], f"B: setup_status over a stale done: {answers[0][:300]!r}")
    with Step("B4 setup mode cli-only: builds a new generation beside the old one"):
        follow_setup(box, "cli-only", autostart=False, budget_s=1800, stand_in="nexus-installed-cannot-start")
        after = generations(box.tools)
        new = [g for g in after if g.name not in before]
        expect(len(new) == 1, f"B: expected one new generation beside {sorted(before)}, found {[g.name for g in after]}")
        expect(old.is_dir(), "B: the pruned generation was removed instead of left beside the new one")
        expect(Path(os.path.realpath(box.current())) == new[0].resolve(), "B: current does not name the new generation")  # type: ignore[arg-type]
        tree_wheel_is_installed(new[0])
        no_network_so_far(proxy)
    with Step("B5 reconnect: the real server answers"):
        conn, answers = talk(box, "nexus", [("setup_status", {})])
        expect(not stand_in_claim(conn.server_name) and REAL_TOOL_NAMES <= conn.tools, f"B: server {conn.server_name!r} tools {sorted(conn.tools)[:8]}")
        expect(answers[0].startswith("No usable Nexus service was found") and "Setup is done." in answers[0],
               f"B: the real server's setup_status after the repair: {answers[0][:300]!r}")
    with Step("B6 session start after a cli-only run: one quiet line, no question (state 7 with the choice recorded)"):
        text = quiet_hook_pass(box)
        expect(text is not None and "the user chose to leave it so" in text, f"B: expected the quiet service-down line, got {text!r}")
        expect("ASK THE USER FIRST" not in text, "B: the quiet line asks")  # type: ignore[operator]


def journey_c_legacy_tree(box: Sandbox, proxy: RefusingProxy | None) -> None:
    with Step("C1 the box: uv, no CLI"):
        precondition_no_cli(box)
        box.confirm_lockstep()
    with Step("C2 a legacy `uv tool install conexus` tree from this tree's wheel, then its Python pruned"):
        version = json.loads((PLUGIN / ".claude-plugin" / "plugin.json").read_text())["version"]
        torch = ["--torch-backend", "cpu"] if sys.platform.startswith("linux") else []
        done = box.run([str(box.bin / "uv"), "tool", "install", "--python", "3.12", f"conexus=={version}", *torch], timeout=1800)
        expect(done.returncode == 0, f"C: uv tool install failed ({done.returncode}): {done.stderr[-800:]}")
        tree = box.uv_tools / "conexus"
        expect((tree / "pyvenv.cfg").is_file() and (box.home / ".local" / "bin" / "nx-mcp").exists(), "C: the legacy tree or its links are missing")
        prune_python(tree, box.root / "pruned-python")
        expect(not (box.tools / "current").exists(), "C: a generation exists before the repair")
    with Step("C3 session start (state 2) through the PATH link; the stand-in says installed but cannot start"):
        results = run_session_start(box)
        check_hooks_are_quiet_without_a_cli(box, results, reason="could not start")
        text = notice_text(results)
        expect(text is not None and BROKEN_PHRASE in text, f"C: state 2 text missing: {text!r}")
        conn, _ = talk(box, "nexus")
        expect(conn.server_name == "nexus-installed-cannot-start" and BROKEN_PHRASE in conn.instructions, f"C: stand-in {conn.server_name!r}")
    with Step("C4 setup mode cli-only: the build runs and makes a generation (the legacy tree is not a complete install)"):
        follow_setup(box, "cli-only", autostart=False, budget_s=1800, stand_in="nexus-installed-cannot-start")
        gens = generations(box.tools)
        expect(len(gens) == 1, f"C: expected one generation, found {[g.name for g in gens]}")
        tree_wheel_is_installed(gens[0])
        no_network_so_far(proxy)
    with Step("C5 reconnect: the generation's server answers (the layout is searched before PATH)"):
        conn, answers = talk(box, "nexus", [("setup_status", {})])
        expect(not stand_in_claim(conn.server_name) and REAL_TOOL_NAMES <= conn.tools, f"C: server {conn.server_name!r}")
        expect(answers[0].startswith("No usable Nexus service was found") and "Setup is done." in answers[0],
               f"C: the real server's setup_status after the repair: {answers[0][:300]!r}")
    with Step("C6 session start after a cli-only run with no service: one quiet line (state 8 with the choice recorded)"):
        text = quiet_hook_pass(box)
        expect(text is not None and "no Nexus service is set up, as the user chose" in text, f"C: expected the quiet no-service line, got {text!r}")


# -- seed --------------------------------------------------------------------------------------

SEED_MARK = "seed.json"


def seed_problem(seed: Path) -> str | None:
    """Why *seed* cannot serve an offline run, or ``None``."""
    needed = {
        "the uv cache": seed / "uvcache" / "CACHEDIR.TAG",
        "the engine": seed / "service" / ("nexus-service.exe" if os.name == "nt" else "nexus-service"),
        "the seed record": seed / SEED_MARK,
    }
    missing = [what for what, path in needed.items() if not path.exists()]
    if not (seed / "service").is_dir() or not list((seed / "service").glob("nexus-pg-*.txz")):
        missing.append("the PostgreSQL bundle")
    models = seed / "models"
    if not (models / "bge-base-en-v1.5" / "onnx").is_dir() or not (models / "ms-marco-minilm-l6-v2" / "onnx").is_dir():
        missing.append("the two models")
    return None if not missing else f"the seed at {seed} lacks {', '.join(missing)}; run the wrapper with --seed once, with a network"


def place_service_files(box: Sandbox, seed: Path) -> None:
    shutil.copytree(seed / "service", box.config / "service")
    target = box.home / ".cache" / "nexus" / "onnx_models"
    shutil.copytree(seed / "models", target)


def harvest(box: Sandbox, seed: Path) -> None:
    """After the online run: keep what ``nx init`` downloaded, so an offline run can place it."""
    for sub in ("service", "models"):
        if (seed / sub).exists():
            shutil.rmtree(seed / sub)
    source = box.config / "service"
    (seed / "service").mkdir(parents=True)
    for item in source.iterdir():
        if item.is_file() and (item.name.startswith(("nexus-service", "nexus-pg-")) or item.name.endswith((".meta.json", ".provenance.json"))):
            shutil.copy2(item, seed / "service" / item.name)
    shutil.copytree(box.home / ".cache" / "nexus" / "onnx_models", seed / "models")
    record = {
        "tree_version": json.loads((PLUGIN / ".claude-plugin" / "plugin.json").read_text())["version"],
        "seeded_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
        "service_files": sorted(p.name for p in (seed / "service").iterdir()),
    }
    (seed / SEED_MARK).write_text(json.dumps(record, indent=2) + "\n", encoding="utf-8")
    say(f"   seed written: {record['service_files']}")


# -- main --------------------------------------------------------------------------------------


def main(argv: list[str]) -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--seed-dir", required=True, type=Path)
    ap.add_argument("--work", required=True, type=Path)
    ap.add_argument("--uv", required=True)
    ap.add_argument("--uv-python-dir", required=True)
    ap.add_argument("--wheels", required=True, type=Path, help="a directory holding this tree's wheel")
    ap.add_argument("--online", action="store_true", help="the seed run: the network is on and the seed is filled")
    ap.add_argument("--only", default="ABC", help="journeys to run, in order (B needs A)")
    ap.add_argument("--keep", action="store_true")
    args = ap.parse_args(argv)

    seed: Path = args.seed_dir
    if not args.online:
        problem = seed_problem(seed)
        if problem:
            say(f"SETUP JOURNEY FAILED: {problem}")
            return 1
    (seed / "uvcache").mkdir(parents=True, exist_ok=True)
    if "B" in args.only and "A" not in args.only:
        say("SETUP JOURNEY FAILED: journey B continues A's box; run A with it")
        return 2

    proxy_cm = RefusingProxy() if not args.online else None
    boxes: list[Sandbox] = []
    outcome = 1
    try:
        with (proxy_cm if proxy_cm is not None else _Nothing()) as proxy:
            def new_box(name: str) -> Sandbox:
                box = Sandbox(
                    name=name, root=args.work / name, uv=args.uv, uv_cache=seed / "uvcache",
                    uv_python_dir=args.uv_python_dir, wheels=args.wheels,
                    proxy=proxy.url if proxy is not None else None, offline=not args.online,
                )
                boxes.append(box)
                return box

            box_a = None
            if "A" in args.only:
                box_a = new_box("a-local")
                journey_a_local(box_a, proxy, seed=seed, online=args.online)
                if args.online:
                    with Step("A9 harvest the seed: the engine, the PostgreSQL bundle and the models nx init downloaded"):
                        harvest(box_a, seed)
            if "B" in args.only:
                assert box_a is not None
                journey_b_pruned_generation(box_a, proxy)
            if "C" in args.only:
                journey_c_legacy_tree(new_box("c-legacy"), proxy)
            if proxy is not None:
                stray = unexpected_requests(proxy.seen)
                expect(not stray, f"the network was asked for {len(stray)} request(s): {stray[:10]}")
                if proxy.seen:
                    logs = " ".join(box.setup_log_tail(400) for box in boxes)
                    expect(TOLERATED_EVIDENCE in logs,
                           f"the listener saw {proxy.seen[:5]} and no setup.log shows nx doctor skipping its PyPI check")
                say(f"   the refusing proxy saw {len(proxy.seen)} request(s), all the tolerated doctor probe: {sorted(set(proxy.seen))}")
        outcome = 0
    except JourneyFailure as exc:
        say(f"SETUP JOURNEY FAILED: {exc}")
    except Exception as exc:  # noqa: BLE001 -- a failed journey names where
        say(f"SETUP JOURNEY FAILED: unexpected {type(exc).__name__}: {exc}")
    finally:
        for box in boxes:
            box.stop_service()
    total = time.monotonic() - _T0
    for label, took in TIMINGS:
        say(f"   {took:7.1f}s  {label}")
    if outcome == 0:
        say(f"SETUP JOURNEY PASSED ({'seed run, network on' if args.online else 'offline'}; journeys {args.only}; {total:.0f} s)")
        if not args.keep:
            shutil.rmtree(args.work, ignore_errors=True)
    else:
        say(f"failure evidence kept under {args.work}")
    return outcome


class _Nothing:
    def __enter__(self) -> None:
        return None

    def __exit__(self, *_: Any) -> None:
        return None


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
