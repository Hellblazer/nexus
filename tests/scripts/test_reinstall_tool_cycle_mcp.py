# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (c) 2026 Hal Hildebrand. All rights reserved.
"""``scripts/reinstall-tool.sh`` MCP-holder self-awareness + ``--cycle-mcp``
(nexus-hrqox).

Friction observed in a live 7.2.0->7.3.0 upgrade: a reinstall run from
INSIDE a live Claude session always hits the nexus-q3xrx live-holder guard,
because the session's own nx-mcp + nx-mcp-catalog spawn at session start
from the old venv. This module pins two behaviors added to close that gap:

1. Self-aware refusal — when every live holder is a claude-ancestored
   nx-mcp/nx-mcp-catalog process, the refusal names the PIDs and prints the
   kill/re-run//mcp sequence instead of the generic three-remedy list; a
   mixed holder set (or a standalone MCP server with no claude ancestor)
   still gets the generic list.
2. ``--cycle-mcp`` — kills exactly the claude-ancestored MCP holders and lets
   the install proceed; refuses instead of partially acting when a non-MCP
   holder is also present; refuses (never swaps the venv) if a killed holder
   survives the post-kill recheck — the accepted PID-reuse TOCTOU backstop
   between the ps snapshot and the kill; composes with ``--cycle-daemons``.

Classification is by ANCESTRY ONLY (a `claude` process anywhere in the ppid
chain), never session-scoped — ``--cycle-mcp`` can kill another live Claude
session's MCP servers, not just the invoking session's. All user-facing
messages say so; this module does not assert "this session" anywhere.

Follows the stubbed-PATH subprocess pattern of
``test_reinstall_tool_downgrade_guard.py``: this never touches the real
global ``uv``/``nx`` install, and never touches a real OS process — ``ps``
is a stubbed binary and ``kill`` is a shadowing shell function (bash's
builtin ``kill`` always wins over a same-named PATH binary, so the function
form is what actually intercepts the script's kill call), both driven
entirely by files under ``tmp_path``, so nothing here can kill anything
live on the box running the suite. The script's own live-holder classifier
functions
(``_is_mcp_server_cmd`` / ``_pid_has_claude_ancestor`` /
``_classify_live_holders``) are exercised through the full script
subprocess, not sourced directly — bash has no import isolation, and
duplicating the stubbed-PATH harness this module already needs to safely
drive them is cheaper than inventing a second sourcing mechanism.
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


def _stub_nx(path: Path) -> None:
    _make_executable(
        path,
        """#!/bin/bash
if [[ "$1" == "--version" ]]; then
    echo "nx, version 0.0.0"
    exit 0
fi
exit 0
""",
    )


def _stub_ps(bin_dir: Path, *, ax_template: Path, alive_dir: Path, ancestors: Path) -> None:
    """Replaces the real ``ps`` for both call shapes the script uses:

    - ``ps ax -o pid=,command=``           (live_venv_processes' bulk scan)
    - ``ps -o ppid=,comm= -p PID``         (_pid_has_claude_ancestor's walk)

    The bulk scan reads ``ax_template`` (one ``PID COMMAND...`` line per
    holder) and only emits a line whose ``alive_dir/PID`` marker file still
    exists — a stub ``kill`` (below) removes the marker instead of sending
    any real signal, so a killed holder disappears from subsequent scans
    exactly like a real process would, without ever touching a real PID.
    The ancestor walk reads ``ancestors`` (``PID PPID COMM`` lines).
    """
    alive_dir.mkdir(parents=True, exist_ok=True)
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
if [[ "$1" == "-o" && "$2" == "ppid=,comm=" && "$3" == "-p" ]]; then
    pid="$4"
    awk -v p="$pid" '$1==p {{print $2, $3}}' "{ancestors}"
    exit 0
fi
exit 1
""",
    )


class _Harness:
    """One reusable stubbed-environment fixture per test: a fake tool venv,
    a fake set of live holders (some MCP, some not), and stubbed
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
        self.ax_template = tmp_path / "ps_ax_template.txt"
        self.ancestors = tmp_path / "ps_ancestors.txt"

        self.stub_bin = tmp_path / "stubbin"
        self.stub_bin.mkdir()
        _stub_uv(self.stub_bin, tool_dir=self.tool_dir, marker=self.marker)
        _stub_nx(self.stub_bin / "nx")
        _stub_ps(
            self.stub_bin,
            ax_template=self.ax_template,
            alive_dir=self.alive_dir,
            ancestors=self.ancestors,
        )
        # `kill` is a bash BUILTIN, not an external binary — a stubbed
        # /kill on PATH never gets a chance to run (the builtin always
        # wins). Shadow it with a shell FUNCTION instead, exported into the
        # child bash process that runs the script (see run()). The function
        # removes the target PID's alive-marker instead of sending any
        # real signal — this is what makes it safe to exercise --cycle-mcp
        # end to end without ever touching a real OS process.

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

    def add_other_holder(self, pid: int, command: str = "index repo .") -> None:
        self._ax_lines.append(f"{pid} {self.venv_dir}/bin/nx {command}")
        (self.alive_dir / str(pid)).touch()
        # No ancestor entry needed — _is_mcp_server_cmd short-circuits
        # before _pid_has_claude_ancestor is ever called for a plain `nx`.

    def run(
        self, *extra_args: str, survivor_pid: int | None = None
    ) -> subprocess.CompletedProcess:
        """Run the script. ``survivor_pid``, if given, makes the exported
        ``kill`` function skip removing that one PID's alive-marker — i.e.
        it simulates a holder that does NOT actually go away when signaled,
        so the post-kill survivor recheck (nexus-hrqox CRITICAL fix) has
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


class TestSelfAwareRefusal:
    def test_all_holders_claude_ancestored_mcp_gets_targeted_message(
        self, tmp_path: Path
    ) -> None:
        h = _Harness(tmp_path)
        h.add_mcp_holder(501, claude_ancestor=True)
        h.add_mcp_holder(504, catalog=True, claude_ancestor=True)

        result = h.run()

        assert result.returncode == 3, result.stdout + result.stderr
        out = result.stdout
        assert "REFUSING to reinstall" in out
        assert "Claude-session MCP servers" in out
        assert "501" in out and "504" in out
        assert "kill 501 504" in out
        assert "/mcp" in out
        assert "--cycle-mcp" in out
        # The generic three-remedy list must NOT appear — this is the
        # targeted branch, not the fallback.
        assert "close other Claude sessions" not in out
        assert not h.marker.exists()  # never reached the install step

    def test_mixed_holders_get_the_generic_remedy_list(self, tmp_path: Path) -> None:
        h = _Harness(tmp_path)
        h.add_mcp_holder(501, claude_ancestor=True)
        h.add_other_holder(505)  # an in-flight `nx index` run

        result = h.run()

        assert result.returncode == 3, result.stdout + result.stderr
        out = result.stdout
        assert "Remedies:" in out
        assert "--cycle-daemons" in out
        assert "--cycle-mcp" in out
        assert "--force" in out
        # The targeted single-class message must NOT appear.
        assert "Claude-session MCP servers" not in out
        assert not h.marker.exists()

    def test_standalone_mcp_server_with_no_claude_ancestor_gets_generic_list(
        self, tmp_path: Path
    ) -> None:
        """An nx-mcp process is not automatically safe to name in the
        targeted message just because it matches the binary name — it must
        also have a `claude` ancestor. A launchd-parented one (e.g. a stray
        test harness) falls through to the generic remedies."""
        h = _Harness(tmp_path)
        h.add_mcp_holder(509, claude_ancestor=False)

        result = h.run()

        assert result.returncode == 3, result.stdout + result.stderr
        assert "Remedies:" in result.stdout
        assert "Claude-session MCP servers" not in result.stdout


class TestCycleMcpArm:
    def test_kills_claude_ancestored_mcp_holders_and_proceeds(self, tmp_path: Path) -> None:
        h = _Harness(tmp_path)
        h.add_mcp_holder(501, claude_ancestor=True)
        h.add_mcp_holder(504, catalog=True, claude_ancestor=True)

        result = h.run("--cycle-mcp")

        assert result.returncode == 0, result.stdout + result.stderr
        assert "Killing Claude session MCP server(s)" in result.stdout
        assert "REMINDER" in result.stdout
        assert "/mcp" in result.stdout
        # Messaging honesty: must not claim exclusivity to "this session" —
        # ancestry-only matching can kill another live session's servers too.
        assert "this session's MCP servers" not in result.stdout
        assert h.marker.exists()  # the (stubbed) install actually ran
        # Both markers were removed by the stub kill — holders are "gone".
        assert not (h.alive_dir / "501").exists()
        assert not (h.alive_dir / "504").exists()

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

        result = h.run("--cycle-mcp", survivor_pid=504)

        assert result.returncode == 3, result.stdout + result.stderr
        assert "REFUSING" in result.stdout
        assert "survived --cycle-mcp" in result.stdout
        assert "504" in result.stdout
        assert not h.marker.exists()  # never reached the install step
        # The non-survivor was still (correctly) killed by the attempt.
        assert not (h.alive_dir / "501").exists()
        assert (h.alive_dir / "504").exists()  # survivor's marker deliberately left

    def test_refuses_when_a_non_mcp_holder_is_also_present(self, tmp_path: Path) -> None:
        h = _Harness(tmp_path)
        h.add_mcp_holder(501, claude_ancestor=True)
        h.add_other_holder(505)

        result = h.run("--cycle-mcp")

        assert result.returncode == 3, result.stdout + result.stderr
        assert "REFUSING --cycle-mcp" in result.stdout
        assert not h.marker.exists()
        # Never touches ANY holder, MCP or not, when it must refuse.
        assert (h.alive_dir / "501").exists()
        assert (h.alive_dir / "505").exists()

    def test_force_bypasses_cycle_mcp_entirely(self, tmp_path: Path) -> None:
        """--force takes the existing force-warn path unchanged; --cycle-mcp
        must not fire (or kill anything) when --force is also given."""
        h = _Harness(tmp_path)
        h.add_mcp_holder(501, claude_ancestor=True)

        result = h.run("--cycle-mcp", "--force")

        assert result.returncode == 0, result.stdout + result.stderr
        assert "WARNING (--force)" in result.stdout
        assert "Killing Claude session MCP server(s)" not in result.stdout
        assert (h.alive_dir / "501").exists()  # never killed
        assert h.marker.exists()

    def test_no_holders_at_all_is_a_plain_successful_install(self, tmp_path: Path) -> None:
        h = _Harness(tmp_path)

        result = h.run("--cycle-mcp")

        assert result.returncode == 0, result.stdout + result.stderr
        assert "REFUSING" not in result.stdout
        assert "REMINDER" not in result.stdout
        assert h.marker.exists()

    def test_composes_with_cycle_daemons(self, tmp_path: Path) -> None:
        """Bead-named composition (nexus-hrqox item 2): both flags together.
        Daemons are stopped (the --cycle-daemons block runs, structurally,
        before the LIVE snapshot the MCP classification is based on — see
        scripts/reinstall-tool.sh line ordering), the MCP holder is killed,
        and the install proceeds with both reminders/markers present."""
        h = _Harness(tmp_path)
        h.add_mcp_holder(501, claude_ancestor=True)

        result = h.run("--cycle-daemons", "--cycle-mcp")

        assert result.returncode == 0, result.stdout + result.stderr
        assert "Stopping nx-owned daemons" in result.stdout
        assert "Killing Claude session MCP server(s)" in result.stdout
        assert "REMINDER" in result.stdout
        assert h.marker.exists()
        assert not (h.alive_dir / "501").exists()
