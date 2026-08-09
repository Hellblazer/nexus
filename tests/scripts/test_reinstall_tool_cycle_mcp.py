# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (c) 2026 Hal Hildebrand. All rights reserved.
"""``scripts/reinstall-tool.sh`` default-does-the-dance holder cycling
(nexus-otnvr, superseding the nexus-hrqox opt-in ``--cycle-mcp`` design;
storage-service folded into the default per nexus-103v2 substantive-critic
CRITICAL finding on the first cut of this rework).

Friction observed in a live 7.2.0->7.3.0 upgrade, and again worse in the
7.4.0 reinstall that prompted nexus-otnvr: a reinstall run from INSIDE a
live Claude session always hit the nexus-q3xrx live-holder guard (the
session's own nx-mcp + nx-mcp-catalog spawn at session start from the old
venv), and clearing it took three flags plus hand-killed PIDs. This module
pins the reworked DEFAULT behavior:

1. No flags needed — a bare invocation classifies every live holder into
   FOUR cyclable classes (Claude-session MCP servers by ancestry;
   aspect-worker; MinerU; the storage service) and cycles each with its own
   choreography, then proceeds. The storage service gets the EXACT
   stop-before/restart-after choreography that used to live behind the
   now-deprecated ``--cycle-daemons`` flag (nexus-103v2 CRITICAL: leaving
   it out unmet nexus-otnvr's own stated goal for the documented canonical
   scenario, since Hal's no-flag-ladder directive outranks the
   ``StaleProcess.restartable`` precedent that governs a DIFFERENT
   lifecycle — an already-booted process discovering it's stale — not this
   PLANNED reinstall journey).
2. aspect-worker gets a RESTART step symmetric with mineru/service — but
   ONLY when it was running STANDALONE (no MCP-server ancestor at classify
   time); an MCP-HOSTED aspect-worker is left alone (it respawns via the
   enqueue hook once its parent MCP server reconnects on /mcp), and the
   output says which case applied (nexus-103v2 Significant-1).
3. Every kill is preceded by a pid-recycle-safe RE-VERIFY — an immediate
   fresh ``ps -o command= -p pid`` re-check against the SAME classification
   predicate used at snapshot time, mirroring
   ``nexus.upgrade_finish.restart_stale``'s pre-kill re-check. A pid that
   no longer matches is skipped with a WARNING line, never killed
   (nexus-103v2 code-review Important-1/2 — this also tightened the
   aspect-worker match from a bare substring to the anchored CLI-verb
   shape "daemon aspect-worker start", closing a false-positive class the
   loose substring left open).
4. Any OTHER holder (an in-flight ``nx`` invocation, or anything
   unrecognized) still REFUSES — never partially acts — and the refusal
   names exactly ONE next command (a bare re-run), never a flag menu.
5. A killed/stopped holder surviving the post-cycle recheck still refuses
   (never swaps the venv under it) — the PID-reuse TOCTOU backstop.
6. ``--no-cycle`` reproduces the OLD pre-otnvr default: refuse on ANY live
   holder, cycle nothing, for scripted/cautious callers.
7. ``--cycle-mcp`` / ``--cycle-daemons`` are accepted as backward-compatible
   TRUE no-op aliases now (a deprecation NOTE prints; default cycling
   already covers everything either flag used to gate, storage service
   included).
8. A successful swap that cycled MCP servers prints exactly the one
   sanctioned next action ("MCP servers were cycled — run /mcp ...").

Classification is by ANCESTRY ONLY (a `claude` process anywhere in the ppid
chain) for MCP, and by an ANCHORED command-line substring for the daemon
classes (aspect-worker / mineru / service) — never session-scoped. All
user-facing messages say so; this module does not assert "this session"
anywhere.

Follows the stubbed-PATH subprocess pattern of
``test_reinstall_tool_downgrade_guard.py``: this never touches the real
global ``uv``/``nx`` install, and never touches a real OS process — ``ps``
is a stubbed binary and ``kill`` is a shadowing shell function (bash's
builtin ``kill`` always wins over a same-named PATH binary, so the function
form is what actually intercepts the script's kill call), both driven
entirely by files under ``tmp_path``, so nothing here can kill anything
live on the box running the suite. The script's own live-holder classifier
functions (``_is_mcp_server_cmd`` / ``_pid_has_ancestor_named`` /
``_daemon_kind`` / ``_kill_verified`` / ``_classify_live_holders``) are
exercised through the full script subprocess, not sourced directly — bash
has no import isolation, and duplicating the stubbed-PATH harness this
module already needs to safely drive them is cheaper than inventing a
second sourcing mechanism.

REAL-PROCESS coverage of the shebang-argv-rewrite root cause (nexus-e1m2v)
lives in ``test_reinstall_tool_classifier_real_processes.py`` — this module
stays a fast, deterministic, fully-stubbed suite (the classifier functions
here are fed hand-built command strings, which is exactly why that bug
needed a separate real-process module to catch).
"""
from __future__ import annotations

import os
import stat
import subprocess
from pathlib import Path

import pytest

_SCRIPT = Path(__file__).resolve().parents[2] / "scripts" / "reinstall-tool.sh"

# Mirrors test_reinstall_tool_downgrade_guard.py's _SAFE_BASE_PATH: enough of
# the real system PATH for bash/sed/grep/awk/basename to resolve, but never
# ~/.local/bin or any other uv-tool-managed dir where the REAL nx/uv live.
_SAFE_BASE_PATH = ":".join(
    p for p in (
        "/opt/homebrew/bin",
        "/opt/homebrew/opt/python@3.13/libexec/bin",
        "/usr/local/bin",
        "/usr/bin",
        "/bin",
        "/usr/sbin",
        "/sbin",
    )
    if Path(p).is_dir()
)

# The real default (5s) bounds how long the script waits for a killed
# holder to actually exit. Tests never need that much wall-clock — the
# script honors NX_REINSTALL_CYCLE_POLL_SECONDS for exactly this reason.
_TEST_POLL_SECONDS = "1"


def _make_executable(path: Path, content: str) -> None:
    path.write_text(content)
    path.chmod(path.stat().st_mode | stat.S_IEXEC | stat.S_IXGRP | stat.S_IXOTH)


def _stub_uv(bin_dir: Path, *, tool_dir: Path, marker: Path) -> None:
    """``tool dir`` answers; ``tool install`` just drops a marker so tests
    can assert whether the real install step was ever reached."""
    _make_executable(
        bin_dir / "uv",
        f"""#!/bin/bash
if [[ "$1" == "tool" && "$2" == "dir" ]]; then
    echo "{tool_dir}"
    exit 0
fi
if [[ "$1" == "tool" && "$2" == "install" ]]; then
    touch "{marker}"
    exit 0
fi
exit 0
""",
    )


def _stub_nx(path: Path, *, alive_dir: Path, ax_template: Path) -> None:
    """``--version`` answers. ``mineru stop`` / ``daemon service stop``
    simulate the real verbs by clearing the alive-marker of whatever
    matching holder is currently "alive" per ``ax_template`` — the script's
    cycle logic shells out to these rather than a bare ``kill`` for mineru/
    service, so the stub has to actually make the holder "go away" the same
    way the stub ``kill`` function (below) does, or the post-cycle survivor
    recheck correctly (but, for these tests, unhelpfully) refuses. Every
    other subcommand (``daemon service start``, ``mineru start``, the
    aspect-worker restart's ``daemon aspect-worker start``) is a
    no-op success — nothing else needs to observe them."""
    _make_executable(
        path,
        f"""#!/bin/bash
if [[ "$1" == "--version" ]]; then
    echo "nx, version 0.0.0"
    exit 0
fi
if [[ "$1" == "mineru" && "$2" == "stop" ]]; then
    while read -r pid rest; do
        [[ -n "$pid" ]] || continue
        case "$rest" in
            *mineru-api*) rm -f "{alive_dir}/$pid" ;;
        esac
    done < "{ax_template}"
    exit 0
fi
if [[ "$1" == "daemon" && "$2" == "service" && "$3" == "stop" ]]; then
    while read -r pid rest; do
        [[ -n "$pid" ]] || continue
        case "$rest" in
            *"daemon service start"*) rm -f "{alive_dir}/$pid" ;;
        esac
    done < "{ax_template}"
    exit 0
fi
exit 0
""",
    )


def _stub_ps(
    bin_dir: Path, *, ax_template: Path, alive_dir: Path, ancestors: Path,
    recycled_dir: Path,
) -> None:
    """Replaces the real ``ps`` for all three call shapes the script uses:

    - ``ps ax -o pid=,command=``        (live_venv_processes' bulk scan)
    - ``ps -o ppid=,command= -p PID``   (the ancestor walk —
      _pid_has_claude_ancestor / _pid_has_mcp_ancestor)
    - ``ps -o command= -p PID``         (_kill_verified's pre-kill re-check)

    The bulk scan reads ``ax_template`` (one ``PID COMMAND...`` line per
    holder) and only emits a line whose ``alive_dir/PID`` marker file still
    exists — a stub ``kill`` (below) removes the marker instead of sending
    any real signal, so a killed holder disappears from subsequent scans
    exactly like a real process would, without ever touching a real PID.
    The ancestor walk reads ``ancestors`` (``PID PPID COMM`` lines). The
    bare ``command=`` re-check first consults ``recycled_dir/PID`` (see
    ``_Harness.simulate_pid_recycled``) — simulating the pid-reuse TOCTOU
    window where the OS pid now belongs to an unrelated process — and only
    falls back to ``ax_template`` (the classify-time command, still
    accurate for the non-recycled case) when no override is staged.
    """
    alive_dir.mkdir(parents=True, exist_ok=True)
    recycled_dir.mkdir(parents=True, exist_ok=True)
    _make_executable(
        bin_dir / "ps",
        f"""#!/bin/bash
if [[ "$1" == "ax" ]]; then
    while read -r pid rest; do
        [[ -n "$pid" ]] || continue
        if [[ -f "{alive_dir}/$pid" ]]; then
            printf '  %s %s\\n' "$pid" "$rest"
        fi
    done < "{ax_template}"
    exit 0
fi
if [[ "$1" == "-o" && "$2" == "ppid=,command=" && "$3" == "-p" ]]; then
    pid="$4"
    awk -v p="$pid" '$1==p {{print $2, $3}}' "{ancestors}"
    exit 0
fi
if [[ "$1" == "-o" && "$2" == "command=" && "$3" == "-p" ]]; then
    pid="$4"
    if [[ -f "{recycled_dir}/$pid" ]]; then
        cat "{recycled_dir}/$pid"
        exit 0
    fi
    if [[ -f "{alive_dir}/$pid" ]]; then
        while read -r p rest; do
            if [[ "$p" == "$pid" ]]; then
                printf '%s\\n' "$rest"
                break
            fi
        done < "{ax_template}"
    fi
    exit 0
fi
exit 1
""",
    )


class _Harness:
    """One reusable stubbed-environment fixture per test: a fake tool venv,
    a fake set of live holders (MCP, daemon, or other), and stubbed
    uv/nx/ps/kill binaries that never reach the real OS or the real global
    install."""

    def __init__(self, tmp_path: Path) -> None:
        self.tmp_path = tmp_path
        self.home = tmp_path / "home"
        self.home.mkdir()
        self.tool_dir = self.home / "tools"
        self.tool_dir.mkdir()
        self.venv_dir = self.tool_dir / "conexus"
        # Deliberately NOT populated (no bin/nx, no uv-receipt.toml) so the
        # downgrade guard's two pre-LIVE-check blocks both skip cleanly and
        # execution reaches the live-holder logic under test directly.
        self.marker = tmp_path / "install-ran.marker"

        self.alive_dir = tmp_path / "alive"
        self.recycled_dir = tmp_path / "recycled"
        self.ax_template = tmp_path / "ps_ax_template.txt"
        self.ancestors = tmp_path / "ps_ancestors.txt"

        self.stub_bin = tmp_path / "stubbin"
        self.stub_bin.mkdir()
        _stub_uv(self.stub_bin, tool_dir=self.tool_dir, marker=self.marker)
        _stub_nx(self.stub_bin / "nx", alive_dir=self.alive_dir, ax_template=self.ax_template)
        _stub_ps(
            self.stub_bin,
            ax_template=self.ax_template,
            alive_dir=self.alive_dir,
            ancestors=self.ancestors,
            recycled_dir=self.recycled_dir,
        )
        # `kill` is a bash BUILTIN, not an external binary — a stubbed
        # /kill on PATH never gets a chance to run (the builtin always
        # wins). Shadow it with a shell FUNCTION instead, exported into the
        # child bash process that runs the script (see run()). The function
        # removes the target PID's alive-marker instead of sending any
        # real signal — this is what makes it safe to exercise the default
        # cycling end to end without ever touching a real OS process.

        self.source = tmp_path / "checkout"
        self.source.mkdir()

        self._ax_lines: list[str] = []
        self._ancestor_lines: list[str] = []

    def add_mcp_holder(self, pid: int, *, catalog: bool = False, claude_ancestor: bool) -> None:
        name = "nx-mcp-catalog" if catalog else "nx-mcp"
        self._ax_lines.append(f"{pid} {self.venv_dir}/bin/{name}")
        (self.alive_dir / str(pid)).touch()
        if claude_ancestor:
            self._ancestor_lines.append(f"{pid} 1 claude")
        else:
            self._ancestor_lines.append(f"{pid} 1 launchd")

    def add_mcp_holder_shebang_wrapped(self, pid: int, *, catalog: bool = False) -> None:
        """The REAL shape a `uv tool install` shim produces on exec
        (nexus-e1m2v): the shebang points directly at the venv's own
        python, so the kernel rewrites argv to [python, script, ...args] —
        the script name lands in the SECOND token. Always claude-ancestored
        (that's the scenario this shape matters for)."""
        name = "nx-mcp-catalog" if catalog else "nx-mcp"
        self._ax_lines.append(
            f"{pid} {self.venv_dir}/bin/python3 {self.venv_dir}/bin/{name}"
        )
        (self.alive_dir / str(pid)).touch()
        self._ancestor_lines.append(f"{pid} 1 claude")

    def add_daemon_holder(self, pid: int, kind: str, *, ancestor: str = "launchd") -> None:
        """kind: "aspect-worker", "mineru", or "service". ``ancestor`` is
        the immediate parent's recorded comm — "launchd" (default,
        standalone/detached) or "nx-mcp"/"nx-mcp-catalog" (MCP-hosted;
        only meaningful for aspect-worker's hosted/standalone restart
        choice, nexus-103v2 item 2)."""
        if kind == "aspect-worker":
            cmd = f"{self.venv_dir}/bin/nx daemon aspect-worker start --tenant default"
        elif kind == "mineru":
            cmd = f"{self.venv_dir}/bin/mineru-api --port 8899"
        elif kind == "service":
            cmd = f"{self.venv_dir}/bin/nx daemon service start --foreground"
        else:
            raise ValueError(kind)
        self._ax_lines.append(f"{pid} {cmd}")
        (self.alive_dir / str(pid)).touch()
        self._ancestor_lines.append(f"{pid} 1 {ancestor}")

    def add_other_holder(self, pid: int, command: str = "index repo .") -> None:
        self._ax_lines.append(f"{pid} {self.venv_dir}/bin/nx {command}")
        (self.alive_dir / str(pid)).touch()
        # No ancestor entry needed — _is_mcp_server_cmd short-circuits
        # before _pid_has_claude_ancestor is ever called for a plain `nx`.

    def simulate_pid_recycled(self, pid: int, new_command: str) -> None:
        """The pid-reuse TOCTOU window: after classification, the OS pid
        gets reaped and reused for an unrelated process before the kill
        fires. `_kill_verified`'s pre-kill `ps -o command= -p pid`
        re-check must see THIS command — not the one recorded at classify
        time — and refuse to kill it."""
        (self.recycled_dir / str(pid)).write_text(new_command + "\n")

    def run(
        self, *extra_args: str, survivor_pid: int | None = None
    ) -> subprocess.CompletedProcess:
        """Run the script. ``survivor_pid``, if given, makes the exported
        ``kill`` function skip removing that one PID's alive-marker — i.e.
        it simulates a holder that does NOT actually go away when signaled,
        so the post-kill survivor recheck (nexus-q3xrx TOCTOU backstop) has
        something real to catch."""
        self.ax_template.write_text("\n".join(self._ax_lines) + "\n" if self._ax_lines else "")
        self.ancestors.write_text(
            "\n".join(self._ancestor_lines) + "\n" if self._ancestor_lines else ""
        )
        env = dict(os.environ)
        env["PATH"] = f"{self.stub_bin}:{_SAFE_BASE_PATH}"
        env["HOME"] = str(self.home)
        env["ALIVE_DIR"] = str(self.alive_dir)
        env["SURVIVOR_PID"] = str(survivor_pid) if survivor_pid is not None else ""
        env["NX_REINSTALL_CYCLE_POLL_SECONDS"] = _TEST_POLL_SECONDS
        # Define + export a `kill` shell function that shadows the builtin
        # in the child bash process that actually runs the script (bash
        # functions propagate to child bash processes via `export -f`,
        # same mechanism as exported env vars). A PID matching
        # $SURVIVOR_PID is deliberately left "alive" (its marker kept) to
        # simulate a kill that did not actually take.
        wrapper = (
            'kill() { local pid; for pid in "$@"; do '
            '[[ -n "${SURVIVOR_PID:-}" && "$pid" == "$SURVIVOR_PID" ]] && continue; '
            'rm -f "$ALIVE_DIR/$pid"; done; return 0; }; '
            "export -f kill; "
            'exec bash "$0" "$@"'
        )
        return subprocess.run(
            ["bash", "-c", wrapper, str(_SCRIPT), str(self.source), *extra_args],
            env=env,
            capture_output=True,
            text=True,
            timeout=30,
        )


class TestDefaultDanceMcp:
    """Bare invocation (no flags) — the whole point of nexus-otnvr."""

    def test_bare_invocation_cycles_claude_ancestored_mcp_holders(
        self, tmp_path: Path
    ) -> None:
        h = _Harness(tmp_path)
        h.add_mcp_holder(501, claude_ancestor=True)
        h.add_mcp_holder(504, catalog=True, claude_ancestor=True)

        result = h.run()

        assert result.returncode == 0, result.stdout + result.stderr
        assert "Killing Claude session MCP server(s)" in result.stdout
        assert "MCP servers were cycled — run /mcp" in result.stdout
        assert h.marker.exists()  # the (stubbed) install actually ran
        assert not (h.alive_dir / "501").exists()
        assert not (h.alive_dir / "504").exists()

    def test_bare_invocation_cycles_shebang_wrapped_mcp_holder(
        self, tmp_path: Path
    ) -> None:
        """The real shape (nexus-e1m2v): a shebang-rewritten `python3 nx-mcp`
        command line. This is what would have failed pre-fix."""
        h = _Harness(tmp_path)
        h.add_mcp_holder_shebang_wrapped(501)

        result = h.run()

        assert result.returncode == 0, result.stdout + result.stderr
        assert "Killing Claude session MCP server(s)" in result.stdout
        assert not (h.alive_dir / "501").exists()

    def test_refuses_when_a_killed_holder_survives(self, tmp_path: Path) -> None:
        """The post-kill survivor recheck (scripts/reinstall-tool.sh
        ``STILL="$(live_venv_processes)"`` -> refuse/exit 3) is the
        PID-reuse TOCTOU backstop between the ps snapshot and the kill —
        proved by mutation in review: deleting that branch left every other
        test in this module green, because the stub kill unconditionally
        cleared every marker. This drives one PID's marker to deliberately
        survive the kill attempt and asserts the script refuses instead of
        proceeding to install."""
        h = _Harness(tmp_path)
        h.add_mcp_holder(501, claude_ancestor=True)
        h.add_mcp_holder(504, catalog=True, claude_ancestor=True)

        result = h.run(survivor_pid=504)

        assert result.returncode == 3, result.stdout + result.stderr
        assert "REFUSING" in result.stdout
        assert "survived the cycle attempt" in result.stdout
        assert "504" in result.stdout
        assert not h.marker.exists()  # never reached the install step
        # The non-survivor was still (correctly) killed by the attempt.
        assert not (h.alive_dir / "501").exists()
        assert (h.alive_dir / "504").exists()  # survivor's marker deliberately left

    def test_no_holders_at_all_is_a_plain_successful_install(self, tmp_path: Path) -> None:
        h = _Harness(tmp_path)

        result = h.run()

        assert result.returncode == 0, result.stdout + result.stderr
        assert "REFUSING" not in result.stdout
        assert "MCP servers were cycled" not in result.stdout
        assert h.marker.exists()


class TestKillSafetyReVerify:
    """nexus-103v2 code-review Important-1/2: every kill is preceded by a
    fresh re-classification of the CURRENT command — the pid-recycle TOCTOU
    backstop between the classify-time snapshot and the kill signal,
    mirroring nexus.upgrade_finish.restart_stale's pre-kill re-check. A pid
    that no longer matches is skipped with a WARNING, never killed."""

    def test_recycled_mcp_pid_is_skipped_not_killed(self, tmp_path: Path) -> None:
        h = _Harness(tmp_path)
        h.add_mcp_holder(501, claude_ancestor=True)
        # Simulate: between classify and kill, this pid was reaped and
        # reused for an unrelated `nx index` invocation.
        h.simulate_pid_recycled(501, f"{h.venv_dir}/bin/nx index repo .")

        result = h.run()

        assert "WARNING: pid 501 no longer matches its classified class" in result.stdout
        # Never killed — the recycled pid's marker survives the attempt,
        # which correctly trips the post-cycle survivor refusal (nothing
        # else marked it dead either).
        assert (h.alive_dir / "501").exists()
        assert result.returncode == 3, result.stdout + result.stderr
        assert not h.marker.exists()

    def test_recycled_aspect_worker_pid_is_skipped_not_killed(self, tmp_path: Path) -> None:
        h = _Harness(tmp_path)
        h.add_daemon_holder(601, "aspect-worker")
        h.simulate_pid_recycled(601, f"{h.venv_dir}/bin/nx index repo .")

        result = h.run()

        assert "WARNING: pid 601 no longer matches its classified class" in result.stdout
        assert (h.alive_dir / "601").exists()
        assert result.returncode == 3, result.stdout + result.stderr

    def test_still_matching_pid_is_killed_normally(self, tmp_path: Path) -> None:
        """Sanity: the re-verify machinery does not break the ordinary,
        non-recycled case — a pid whose command is unchanged is still
        killed exactly as before."""
        h = _Harness(tmp_path)
        h.add_mcp_holder(501, claude_ancestor=True)

        result = h.run()

        assert "WARNING" not in result.stdout
        assert result.returncode == 0, result.stdout + result.stderr
        assert not (h.alive_dir / "501").exists()

    def test_tightened_aspect_worker_match_excludes_bare_substring_false_positive(
        self, tmp_path: Path
    ) -> None:
        """code-review Important-2: a bare "*aspect-worker*" substring
        would have matched an unrelated `nx` invocation over a path
        literally containing that text — e.g. an operator indexing a repo
        checked out under a directory named "aspect-worker-notes". The
        tightened match requires the anchored CLI-verb shape "daemon
        aspect-worker start", so this classifies as an OTHER holder (in-
        flight nx run) and refuses, rather than getting silently killed."""
        h = _Harness(tmp_path)
        h.add_other_holder(801, command="index repo ~/proj/aspect-worker-notes")

        result = h.run()

        assert result.returncode == 3, result.stdout + result.stderr
        assert "Stop or close the holder(s) above, then re-run:" in result.stdout
        assert (h.alive_dir / "801").exists()  # never touched


class TestDefaultDanceDaemons:
    def test_bare_invocation_cycles_aspect_worker_holder(self, tmp_path: Path) -> None:
        h = _Harness(tmp_path)
        h.add_daemon_holder(601, "aspect-worker")

        result = h.run()

        assert result.returncode == 0, result.stdout + result.stderr
        assert "Stopping aspect-worker daemon(s)" in result.stdout
        assert "601" in result.stdout
        assert not (h.alive_dir / "601").exists()
        assert h.marker.exists()
        # Daemon-only cycles never print the MCP-specific /mcp reminder.
        assert "MCP servers were cycled" not in result.stdout

    def test_bare_invocation_cycles_mineru_holder(self, tmp_path: Path) -> None:
        h = _Harness(tmp_path)
        h.add_daemon_holder(602, "mineru")

        result = h.run()

        assert result.returncode == 0, result.stdout + result.stderr
        assert "Stopping MinerU server" in result.stdout
        assert not (h.alive_dir / "602").exists()
        assert h.marker.exists()

    def test_bare_invocation_cycles_storage_service_holder(self, tmp_path: Path) -> None:
        """nexus-103v2 CRITICAL: the storage service is now auto-cycled by
        default too — the exact stop-before/restart-after choreography that
        used to require --cycle-daemons, for the exact scenario the
        nexus-otnvr bead documents."""
        h = _Harness(tmp_path)
        h.add_daemon_holder(701, "service")

        result = h.run()

        assert result.returncode == 0, result.stdout + result.stderr
        assert "Stopping the storage service" in result.stdout
        assert not (h.alive_dir / "701").exists()
        assert h.marker.exists()
        assert "MCP servers were cycled" not in result.stdout

    def test_bare_invocation_cycles_mixed_mcp_and_daemon_holders(
        self, tmp_path: Path
    ) -> None:
        h = _Harness(tmp_path)
        h.add_mcp_holder(501, claude_ancestor=True)
        h.add_daemon_holder(601, "aspect-worker")
        h.add_daemon_holder(602, "mineru")
        h.add_daemon_holder(701, "service")

        result = h.run()

        assert result.returncode == 0, result.stdout + result.stderr
        assert not (h.alive_dir / "501").exists()
        assert not (h.alive_dir / "601").exists()
        assert not (h.alive_dir / "602").exists()
        assert not (h.alive_dir / "701").exists()
        assert "MCP servers were cycled — run /mcp" in result.stdout
        assert h.marker.exists()


class TestAspectWorkerRestartSymmetry:
    """nexus-103v2 Significant-1: aspect-worker gets a RESTART step
    post-install, symmetric with mineru/storage-service — but only for a
    STANDALONE holder (no MCP-server ancestor at classify time). A
    HOSTED holder (spawned as a child of an MCP server, which the default
    dance also just killed) is left alone; the output says which case
    applied either way."""

    def test_standalone_aspect_worker_is_restarted_with_same_args(
        self, tmp_path: Path
    ) -> None:
        h = _Harness(tmp_path)
        h.add_daemon_holder(601, "aspect-worker", ancestor="launchd")

        result = h.run()

        assert result.returncode == 0, result.stdout + result.stderr
        assert "aspect-worker (was standalone) restarting:" in result.stdout
        assert "nx daemon aspect-worker start --tenant default" in result.stdout
        assert "MCP-hosted" not in result.stdout

    def test_mcp_hosted_aspect_worker_is_not_restarted(self, tmp_path: Path) -> None:
        h = _Harness(tmp_path)
        h.add_daemon_holder(601, "aspect-worker", ancestor="nx-mcp")

        result = h.run()

        assert result.returncode == 0, result.stdout + result.stderr
        assert "was MCP-hosted — not" in result.stdout
        assert "601" in result.stdout
        assert "restarting:" not in result.stdout

    def test_mcp_hosted_via_catalog_ancestor_is_also_not_restarted(
        self, tmp_path: Path
    ) -> None:
        h = _Harness(tmp_path)
        h.add_daemon_holder(601, "aspect-worker", ancestor="nx-mcp-catalog")

        result = h.run()

        assert result.returncode == 0, result.stdout + result.stderr
        assert "was MCP-hosted — not" in result.stdout

    def test_no_aspect_worker_holder_prints_no_restart_disposition(
        self, tmp_path: Path
    ) -> None:
        h = _Harness(tmp_path)
        h.add_mcp_holder(501, claude_ancestor=True)

        result = h.run()

        assert result.returncode == 0, result.stdout + result.stderr
        assert "aspect-worker" not in result.stdout


class TestOtherHoldersRefuseWithSingleCommand:
    """Anything outside the four cyclable classes always refuses, and the
    refusal names exactly ONE next command — never a flag menu."""

    def test_mixed_mcp_and_other_holder_refuses(self, tmp_path: Path) -> None:
        h = _Harness(tmp_path)
        h.add_mcp_holder(501, claude_ancestor=True)
        h.add_other_holder(505)  # an in-flight `nx index` run

        result = h.run()

        assert result.returncode == 3, result.stdout + result.stderr
        out = result.stdout
        assert "REFUSING to reinstall" in out
        assert "Stop or close the holder(s) above, then re-run:" in out
        assert "scripts/reinstall-tool.sh" in out
        # Never a flag menu.
        assert "--cycle-daemons" not in out
        assert "--cycle-mcp" not in out
        assert "--force" not in out
        assert not h.marker.exists()
        # Never partially acts — the MCP holder is untouched too.
        assert (h.alive_dir / "501").exists()
        assert (h.alive_dir / "505").exists()

    def test_standalone_mcp_server_with_no_claude_ancestor_refuses(
        self, tmp_path: Path
    ) -> None:
        """An nx-mcp process is not automatically safe to cycle just because
        it matches the binary name — it must also have a `claude` ancestor.
        A launchd-parented one (e.g. a stray test harness) is an OTHER
        holder and refuses."""
        h = _Harness(tmp_path)
        h.add_mcp_holder(509, claude_ancestor=False)

        result = h.run()

        assert result.returncode == 3, result.stdout + result.stderr
        assert "Stop or close the holder(s) above, then re-run:" in result.stdout
        assert (h.alive_dir / "509").exists()

    def test_daemon_holder_mixed_with_other_holder_refuses(self, tmp_path: Path) -> None:
        h = _Harness(tmp_path)
        h.add_daemon_holder(601, "aspect-worker")
        h.add_other_holder(505)

        result = h.run()

        assert result.returncode == 3, result.stdout + result.stderr
        assert not h.marker.exists()
        assert (h.alive_dir / "601").exists()
        assert (h.alive_dir / "505").exists()

    def test_storage_service_mixed_with_other_holder_refuses_without_partial_cycle(
        self, tmp_path: Path
    ) -> None:
        """Never partially acts: an OTHER holder alongside a storage-
        service holder refuses BOTH, rather than stopping the service and
        then discovering the other holder."""
        h = _Harness(tmp_path)
        h.add_daemon_holder(701, "service")
        h.add_other_holder(505)

        result = h.run()

        assert result.returncode == 3, result.stdout + result.stderr
        assert not h.marker.exists()
        assert (h.alive_dir / "701").exists()
        assert (h.alive_dir / "505").exists()


class TestNoCycleOptOut:
    def test_no_cycle_refuses_without_killing_anything(self, tmp_path: Path) -> None:
        h = _Harness(tmp_path)
        h.add_mcp_holder(501, claude_ancestor=True)

        result = h.run("--no-cycle")

        assert result.returncode == 3, result.stdout + result.stderr
        assert "REFUSING to reinstall" in result.stdout
        assert "--no-cycle was passed" in result.stdout
        assert not h.marker.exists()
        assert (h.alive_dir / "501").exists()  # never killed

    def test_no_cycle_with_daemon_holder_also_refuses(self, tmp_path: Path) -> None:
        h = _Harness(tmp_path)
        h.add_daemon_holder(601, "aspect-worker")

        result = h.run("--no-cycle")

        assert result.returncode == 3, result.stdout + result.stderr
        assert (h.alive_dir / "601").exists()

    def test_no_cycle_with_storage_service_holder_also_refuses(self, tmp_path: Path) -> None:
        h = _Harness(tmp_path)
        h.add_daemon_holder(701, "service")

        result = h.run("--no-cycle")

        assert result.returncode == 3, result.stdout + result.stderr
        assert (h.alive_dir / "701").exists()

    def test_no_cycle_with_no_holders_is_a_plain_successful_install(
        self, tmp_path: Path
    ) -> None:
        h = _Harness(tmp_path)

        result = h.run("--no-cycle")

        assert result.returncode == 0, result.stdout + result.stderr
        assert h.marker.exists()


class TestForceBypass:
    def test_force_bypasses_everything(self, tmp_path: Path) -> None:
        """--force takes the existing force-warn path unchanged; nothing is
        classified or killed when --force is given."""
        h = _Harness(tmp_path)
        h.add_mcp_holder(501, claude_ancestor=True)
        h.add_daemon_holder(601, "aspect-worker")
        h.add_daemon_holder(701, "service")

        result = h.run("--force")

        assert result.returncode == 0, result.stdout + result.stderr
        assert "WARNING (--force)" in result.stdout
        assert "Killing Claude session MCP server(s)" not in result.stdout
        assert (h.alive_dir / "501").exists()  # never killed
        assert (h.alive_dir / "601").exists()  # never killed
        assert (h.alive_dir / "701").exists()  # never killed
        assert h.marker.exists()


class TestLegacyFlagsAreDeprecatedNoOps:
    """--cycle-mcp / --cycle-daemons are accepted for backward compatibility
    but no longer change the classify/refuse/cycle behavior on their own —
    default cycling already covers everything either flag used to gate,
    storage service included (nexus-103v2: --cycle-daemons's OLD unique
    side effect — proactively stopping/restarting the storage service — is
    now just what the default dance does for ANY storage-service holder,
    flag or no flag)."""

    def test_cycle_mcp_prints_deprecation_note_but_behaves_like_default(
        self, tmp_path: Path
    ) -> None:
        h = _Harness(tmp_path)
        h.add_mcp_holder(501, claude_ancestor=True)

        result = h.run("--cycle-mcp")

        assert result.returncode == 0, result.stdout + result.stderr
        assert "NOTE: --cycle-mcp / --cycle-daemons are no longer required" in result.stdout
        assert "Killing Claude session MCP server(s)" in result.stdout
        assert not (h.alive_dir / "501").exists()
        assert h.marker.exists()

    def test_cycle_mcp_still_refuses_on_other_holder(self, tmp_path: Path) -> None:
        """The flag no longer owns a separate refusal branch — the default
        OTHER_HOLDERS refusal fires exactly as it would with no flag."""
        h = _Harness(tmp_path)
        h.add_mcp_holder(501, claude_ancestor=True)
        h.add_other_holder(505)

        result = h.run("--cycle-mcp")

        assert result.returncode == 3, result.stdout + result.stderr
        assert "Stop or close the holder(s) above, then re-run:" in result.stdout
        assert not h.marker.exists()
        assert (h.alive_dir / "501").exists()
        assert (h.alive_dir / "505").exists()

    def test_force_bypasses_cycle_mcp_entirely(self, tmp_path: Path) -> None:
        h = _Harness(tmp_path)
        h.add_mcp_holder(501, claude_ancestor=True)

        result = h.run("--cycle-mcp", "--force")

        assert result.returncode == 0, result.stdout + result.stderr
        assert "WARNING (--force)" in result.stdout
        assert "Killing Claude session MCP server(s)" not in result.stdout
        assert (h.alive_dir / "501").exists()  # never killed
        assert h.marker.exists()

    def test_cycle_daemons_is_harmless_alongside_default_storage_service_cycle(
        self, tmp_path: Path
    ) -> None:
        """--cycle-daemons no longer owns a unique code path — passing it
        alongside a live storage-service holder produces IDENTICAL
        cycling behavior to a bare invocation (just with the extra
        deprecation note)."""
        h = _Harness(tmp_path)
        h.add_mcp_holder(501, claude_ancestor=True)
        h.add_daemon_holder(701, "service")

        result = h.run("--cycle-daemons", "--cycle-mcp")

        assert result.returncode == 0, result.stdout + result.stderr
        assert "NOTE: --cycle-mcp / --cycle-daemons are no longer required" in result.stdout
        assert "Stopping the storage service" in result.stdout
        assert "Killing Claude session MCP server(s)" in result.stdout
        assert "MCP servers were cycled — run /mcp" in result.stdout
        assert h.marker.exists()
        assert not (h.alive_dir / "501").exists()
        assert not (h.alive_dir / "701").exists()

    def test_no_cycle_overrides_legacy_flags(self, tmp_path: Path) -> None:
        """--no-cycle is the authoritative opt-out even when a legacy flag
        is also passed — nothing gets auto-cycled."""
        h = _Harness(tmp_path)
        h.add_mcp_holder(501, claude_ancestor=True)

        result = h.run("--cycle-mcp", "--no-cycle")

        assert result.returncode == 3, result.stdout + result.stderr
        assert "--no-cycle was passed" in result.stdout
        assert (h.alive_dir / "501").exists()  # never killed
        assert not h.marker.exists()
