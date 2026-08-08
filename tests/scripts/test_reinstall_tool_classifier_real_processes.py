# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (c) 2026 Hal Hildebrand. All rights reserved.
"""``scripts/reinstall-tool.sh`` MCP-holder classifier — REAL-process
regression (nexus-e1m2v).

Proven live 2026-08-08 (7.4.0 reinstall): a live Claude session's own
nx-mcp/nx-mcp-catalog servers, one ancestry hop below the session's `claude`
process, were classified as "Non-MCP (or no MCP) holder(s)" and `--cycle-mcp`
refused in exactly the scenario it exists for.

Root cause: ``_is_mcp_server_cmd`` (scripts/reinstall-tool.sh) checked only
the FIRST whitespace-separated token of the holder's full command line.
``uv tool install`` writes console-script shims whose shebang line points
DIRECTLY at the venv's own python binary (``#!/…/conexus/bin/python3``, not
``/usr/bin/env``). On exec, the kernel rewrites the process's argv to
``[python_interpreter, script_path, ...original_args]`` — so `ps ax -o
command=` shows the interpreter as the first token and the script's own name
(``nx-mcp`` / ``nx-mcp-catalog``) as the SECOND. Checking only the first
token matches a hypothetical direct-exec shape that never actually occurs
for a `uv tool install`-managed venv, and silently misses the shape that
does.

The prior test module (``test_reinstall_tool_cycle_mcp.py``) stubs `ps`
entirely, feeding it single-token "``nx-mcp``" command lines built by hand —
which is exactly why it could not have caught this bug; a stub that already
assumes the (wrong) command shape validates the classifier against its own
wrong assumption, not against what a real shebang-exec'd process actually
looks like. This module instead spawns REAL OS processes shaped exactly
like a live `uv tool install` deployment:

  - a `claude`-argv0'd process (a `bash` binary invoked via `exec -a claude`,
    so `ps` reports its own command as literally starting with "claude" —
    no code-signing tricks needed since nothing is copied, only symlinked);
  - a `nx-mcp`-shaped DIRECT CHILD of it: a python script under a fixture
    "venv" whose shebang points straight at that venv's own `python3`
    symlink, exec'd so its real, kernel-rewritten argv is
    ``<venv>/bin/python3 <venv>/bin/nx-mcp`` — the exact shape observed live.

The full `reinstall-tool.sh` script runs against these real PIDs with the
REAL system `ps`/`kill`/`basename` (only `uv`/`nx` are stubbed, so nothing
here ever reaches the real global install — see `_stub_uv`/`_stub_nx`).
A second class of test drives the companion change made alongside this fix:
`_pid_has_ancestor_named` (the ancestor walk `_pid_has_claude_ancestor`
wraps) uses `ps -o command=`, not `comm=`. The ORIGINAL rationale — that
macOS truncates `comm=` at MAXCOMLEN (16 raw characters) for a long argv[0]
— was FALSIFIED (substantive-critic 2026-08-08, nexus-103v2): a real
93-char-argv0 repro on this box (Darwin 25.5.0 arm64) showed `ps -o comm=`
does NOT truncate here.
`command=` is still the right choice, just for the correct reason: Linux's
`ps -o comm=` (backed by `/proc/[pid]/comm`, kernel `TASK_COMM_LEN=16`,
i.e. 15 visible characters) DOES truncate a long argv[0] — a real,
well-documented, stable kernel ABI limit on the platform this repo's own
`tests/scripts/` CI job actually runs on (`ubuntu-latest`); `command=`
(the full, reconstructed argv) is never truncated on ANY platform, so it
is strictly never worse than `comm=` and closes a real gap on the platform
that matters for CI, even though this specific test (spawning real
processes) only runs — and could only ever assert the positive "still
detected" outcome — on the macOS box actually running this suite.
`test_long_argv0_claude_ancestor_still_detected` below proves the code
path handles a long argv[0] correctly; it does NOT claim to reproduce
truncation (which does not occur here) — see its own docstring.
"""
from __future__ import annotations

import os
import shutil
import signal
import stat
import subprocess
import sys
import time
from pathlib import Path

import pytest

_SCRIPT = Path(__file__).resolve().parents[2] / "scripts" / "reinstall-tool.sh"

# Real system PATH entries needed to resolve bash/ps/kill/basename/grep/sed —
# deliberately EXCLUDES ~/.local/bin (and any other uv-tool-managed bin dir),
# so the stub `uv`/`nx` placed ahead of this on PATH are the only ones ever
# reached; nothing here can touch the real global conexus install.
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

pytestmark = pytest.mark.skipif(
    sys.platform not in ("darwin", "linux"),
    reason="spawns real POSIX processes via `exec -a` and `ps`/`kill`",
)


def _make_executable(path: Path, content: str) -> None:
    path.write_text(content)
    path.chmod(path.stat().st_mode | stat.S_IEXEC | stat.S_IXGRP | stat.S_IXOTH)


def _stub_uv(bin_dir: Path, *, tool_dir: Path, marker: Path) -> None:
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


def _pid_alive(pid: int) -> bool:
    try:
        os.kill(pid, 0)
    except (ProcessLookupError, PermissionError):
        return False
    return True


def _wait_for(path: Path, *, timeout: float = 5.0) -> str:
    deadline = time.time() + timeout
    while time.time() < deadline:
        if path.exists() and path.stat().st_size > 0:
            return path.read_text().strip()
        time.sleep(0.05)
    raise TimeoutError(f"{path} never appeared")


class _RealMcpHolder:
    """Spawns a real `claude`-argv0'd process with a real, shebang-exec'd
    `nx-mcp` DIRECT CHILD, laid out under ``venv_dir`` (which must equal the
    script's computed ``$VENV_DIR`` so `live_venv_processes()`'s `grep -F`
    finds it). ``claude_argv0`` lets a caller drive a long, MAXCOMLEN-busting
    argv[0] for the ancestor-truncation regression test.
    """

    def __init__(
        self, tmp_path: Path, venv_dir: Path, *,
        server_name: str = "nx-mcp", claude_argv0: str = "claude",
    ) -> None:
        self.tmp_path = tmp_path
        self.venv_dir = venv_dir
        bin_dir = venv_dir / "bin"
        bin_dir.mkdir(parents=True, exist_ok=True)

        python_link = bin_dir / "python3"
        python_link.symlink_to(sys.executable)

        self.server_script = bin_dir / server_name
        self.server_script.write_text(
            f"#!{python_link}\nimport time\ntime.sleep(60)\n"
        )
        self.server_script.chmod(0o755)

        bash_link = tmp_path / f"claude_bin_{claude_argv0.replace('/', '_')}"
        bash_link.symlink_to(shutil.which("bash") or "/bin/bash")

        # The nx-mcp path must NEVER appear as a literal argv element of the
        # "claude" process itself — a real `claude` process's own command
        # line is just "claude", never carrying the venv path as an
        # argument, and `live_venv_processes()`'s `grep -F "$VENV_DIR"`
        # would otherwise (wrongly, as a TEST-HARNESS artifact) also match
        # the claude row. So the nx-mcp/child-pid-file paths are baked into
        # a RUNNER SCRIPT FILE's contents (ps only shows a process's argv,
        # never a script file's body) rather than passed as `bash -c`
        # arguments (which DO become part of the process's own argv/command
        # line). The runner script's own path lives outside the venv dir,
        # so it can't accidentally match the grep either. "wait" is a bash
        # BUILTIN (not externally exec'd), so bash never tail-call-replaces
        # itself away from the "claude" argv[0] while running this script.
        runner = tmp_path / f"runner_{claude_argv0.replace('/', '_')}.sh"
        self.child_pid_file = tmp_path / f"child_pid_{claude_argv0.replace('/', '_')}"
        runner.write_text(
            "#!/bin/bash\n"
            f'"{self.server_script}" &\n'
            f'echo $! > "{self.child_pid_file}"\n'
            "wait\n"
        )
        runner.chmod(0o755)

        wrapper = tmp_path / f"wrapper_{claude_argv0.replace('/', '_')}.sh"
        wrapper.write_text(
            "#!/bin/bash\n"
            '# $1=bash_symlink $2=argv0 $3=runner_script\n'
            'exec -a "$2" "$1" "$3"\n'
        )
        wrapper.chmod(0o755)

        self._claude_proc = subprocess.Popen(
            ["bash", str(wrapper), str(bash_link), claude_argv0, str(runner)],
        )
        self.claude_pid = self._claude_proc.pid
        self.server_pid = int(_wait_for(self.child_pid_file))
        # Give both processes a moment to settle so `ps` sees a stable row.
        time.sleep(0.2)
        assert _pid_alive(self.claude_pid), "fake claude process failed to start"
        assert _pid_alive(self.server_pid), "fake nx-mcp process failed to start"

    def close(self) -> None:
        for pid in (self.server_pid, self.claude_pid):
            try:
                os.kill(pid, signal.SIGKILL)
            except (ProcessLookupError, PermissionError):
                pass
        try:
            self._claude_proc.wait(timeout=5)
        except subprocess.TimeoutExpired:
            pass


def _run_script(
    tmp_path: Path, *, venv_dir: Path, extra_args: tuple[str, ...] = (),
) -> tuple[subprocess.CompletedProcess, Path]:
    home = tmp_path / "home"
    home.mkdir(exist_ok=True)
    marker = tmp_path / "install-ran.marker"
    stub_bin = tmp_path / "stubbin"
    stub_bin.mkdir(exist_ok=True)
    _stub_uv(stub_bin, tool_dir=venv_dir.parent, marker=marker)
    _stub_nx(stub_bin / "nx")
    source = tmp_path / "checkout"
    source.mkdir(exist_ok=True)

    env = dict(os.environ)
    env["PATH"] = f"{stub_bin}:{_SAFE_BASE_PATH}"
    env["HOME"] = str(home)

    result = subprocess.run(
        ["bash", str(_SCRIPT), str(source), *extra_args],
        env=env, capture_output=True, text=True, timeout=30,
    )
    return result, marker


def _wait_dead(pid: int, *, timeout: float = 5.0) -> bool:
    deadline = time.time() + timeout
    while time.time() < deadline and _pid_alive(pid):
        time.sleep(0.1)
    return not _pid_alive(pid)


class TestRealShebangExecMisclassification:
    """The canonical shape: `uv tool install`-shimmed nx-mcp, one ancestry
    hop below a real `claude`-argv0'd process. Pre-fix, this always fell
    into OTHER_HOLDERS (the classifier only looked at the python
    interpreter, never the script name in the second argv token), so a
    BARE (no-flag) invocation — the default dance, nexus-otnvr — would have
    refused instead of cycling it. Correct classification is proven here by
    the bare invocation actually succeeding and killing the real process;
    an unclassified/OTHER holder would refuse instead (exit 3)."""

    def test_bare_invocation_classifies_and_kills_real_holder(self, tmp_path: Path) -> None:
        venv_dir = tmp_path / "tools" / "conexus"
        holder = _RealMcpHolder(tmp_path, venv_dir)
        try:
            result, marker = _run_script(tmp_path, venv_dir=venv_dir)
            out = result.stdout
            assert result.returncode == 0, out + result.stderr
            assert "Killing Claude session MCP server(s)" in out, out
            assert str(holder.server_pid) in out, out
            assert marker.exists(), "install must have proceeded"
            assert _wait_dead(holder.server_pid), (
                "the default dance must have actually killed the real "
                "nx-mcp process"
            )
        finally:
            holder.close()

    def test_catalog_variant_also_classified(self, tmp_path: Path) -> None:
        venv_dir = tmp_path / "tools" / "conexus"
        holder = _RealMcpHolder(tmp_path, venv_dir, server_name="nx-mcp-catalog")
        try:
            result, marker = _run_script(tmp_path, venv_dir=venv_dir)
            assert result.returncode == 0, result.stdout + result.stderr
            assert "Killing Claude session MCP server(s)" in result.stdout
            assert marker.exists()
            assert _wait_dead(holder.server_pid)
        finally:
            holder.close()

    def test_cycle_mcp_legacy_flag_still_kills_the_real_process(self, tmp_path: Path) -> None:
        """--cycle-mcp is now a deprecated no-op alias (nexus-otnvr) — this
        proves it still composes correctly (prints the deprecation note,
        default cycling still fires, nothing breaks for existing callers)."""
        venv_dir = tmp_path / "tools" / "conexus"
        holder = _RealMcpHolder(tmp_path, venv_dir)
        try:
            result, marker = _run_script(
                tmp_path, venv_dir=venv_dir, extra_args=("--cycle-mcp",),
            )
            assert result.returncode == 0, result.stdout + result.stderr
            assert "NOTE: --cycle-mcp / --cycle-daemons are no longer required" in result.stdout
            assert "Killing Claude session MCP server(s)" in result.stdout
            assert marker.exists(), "install must have proceeded"
            assert _wait_dead(holder.server_pid), (
                "--cycle-mcp must still result in the real nx-mcp process dying"
            )
        finally:
            holder.close()

    def test_no_cycle_still_refuses_on_the_real_holder(self, tmp_path: Path) -> None:
        """--no-cycle opts out of the default dance entirely — even a
        correctly-classifiable MCP holder is left untouched and the swap
        refuses."""
        venv_dir = tmp_path / "tools" / "conexus"
        holder = _RealMcpHolder(tmp_path, venv_dir)
        try:
            result, marker = _run_script(
                tmp_path, venv_dir=venv_dir, extra_args=("--no-cycle",),
            )
            assert result.returncode == 3, result.stdout + result.stderr
            assert "--no-cycle was passed" in result.stdout
            assert not marker.exists()
            assert _pid_alive(holder.server_pid), "--no-cycle must never kill anything"
        finally:
            holder.close()


class TestAncestorTruncationRegression:
    """`_pid_has_ancestor_named` (which `_pid_has_claude_ancestor` wraps)
    reads `ps -o command=`, not `comm=`. This test proves a `claude`
    process with a long argv[0] is still correctly detected as an
    ancestor — it does NOT prove macOS truncates `comm=` for such an
    argv[0], because it doesn't, on this box (substantive-critic
    2026-08-08, nexus-103v2 Significant-2: falsified via a real 93-char
    repro — reverting to `comm=` left this test GREEN). `command=` is kept
    regardless as correct, platform-general hardening: Linux's `ps -o
    comm=` (backed by `/proc/[pid]/comm`, kernel `TASK_COMM_LEN=16`) DOES
    truncate a long argv[0] — a real, stable kernel limit on the platform
    this repo's `tests/scripts/` CI job actually runs on (`ubuntu-latest`)
    — and `command=` is never worse on any platform. This test exercises
    the code path honestly (long argv0 -> still detected) without
    overclaiming what it reproduces on macOS."""

    def test_long_argv0_claude_ancestor_still_detected(self, tmp_path: Path) -> None:
        long_argv0 = "/some/very/long/absolute/path/to/claude"
        assert len(long_argv0) > 16, (
            "long enough to matter on Linux's real TASK_COMM_LEN=16 limit, "
            "even though this box's ps -o comm= does not truncate it"
        )
        venv_dir = tmp_path / "tools" / "conexus"
        holder = _RealMcpHolder(tmp_path, venv_dir, claude_argv0=long_argv0)
        try:
            result, marker = _run_script(tmp_path, venv_dir=venv_dir)
            assert result.returncode == 0, result.stdout + result.stderr
            assert "Killing Claude session MCP server(s)" in result.stdout, (
                "a long argv[0] must not defeat the claude-ancestor match; "
                f"got:\n{result.stdout}"
            )
            assert marker.exists()
            assert _wait_dead(holder.server_pid)
        finally:
            holder.close()
