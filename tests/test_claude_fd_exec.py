# SPDX-License-Identifier: AGPL-3.0-or-later
"""RDR-219 (nexus-wauo1.36): harness Claude launches read the OAuth token from fd 3.

``CLAUDE_CODE_OAUTH_TOKEN_FILE_DESCRIPTOR=3`` with the token piped into fd 3
keeps ``CLAUDE_CODE_OAUTH_TOKEN`` out of Claude's exec-time environment, which a
same-user ``ps -E`` or ``/proc/<pid>/environ`` could otherwise read. Measured
2026-09-26 on Claude Code 2.1.283: an interactive tmux session and ``-p`` both
authenticate this way, and Claude removes the fd variable from its own children
as it does the token. These tests drive the real scripts against a FAKE
``claude`` and FAKE tokens only.
"""
from __future__ import annotations

import os
import pathlib
import re
import subprocess

REPO = pathlib.Path(__file__).resolve().parents[1]
FD_EXEC = REPO / "tests" / "e2e" / "lib" / "claude_fd_exec.sh"
LAUNCHER = REPO / "tests" / "e2e" / "lib" / "claude_mcp_grant.sh"
LIB_SH = REPO / "tests" / "e2e" / "lib.sh"
SCENARIOS = REPO / "tests" / "cc-validation" / "scenarios"
FAKE_TOKEN = 'sk-ant-oat01-FAKE"quote\\back-0000'


def _fake_claude(bin_dir: pathlib.Path, out: pathlib.Path) -> None:
    bin_dir.mkdir()
    fake = bin_dir / "claude"
    fake.write_text(
        "#!/usr/bin/env bash\n"
        f'out="{out}"\n'
        'if [ -n "${CLAUDE_CODE_OAUTH_TOKEN+x}" ]; then echo token_var=set > "$out"; else echo token_var=unset > "$out"; fi\n'
        'echo "fd_var=${CLAUDE_CODE_OAUTH_TOKEN_FILE_DESCRIPTOR:-}" >> "$out"\n'
        'fd="${CLAUDE_CODE_OAUTH_TOKEN_FILE_DESCRIPTOR:-}"\n'
        'if [ -n "$fd" ]; then IFS= read -r -d "" tok <&"$fd"; printf "%s" "$tok" > "$out.fd"; fi\n'
        'printf "%s\\n" "$@" > "$out.argv"\n'
    )
    fake.chmod(0o755)


def _env(bin_dir: pathlib.Path, token: "str | None") -> dict:
    env = {k: v for k, v in os.environ.items()
           if k not in ("CLAUDE_CODE_OAUTH_TOKEN", "NX_HARNESS_CLAUDE_OAUTH_TOKEN",
                        "CLAUDE_CODE_OAUTH_TOKEN_FILE_DESCRIPTOR")}
    env["PATH"] = f"{bin_dir}{os.pathsep}{env['PATH']}"
    if token is not None:
        env["CLAUDE_CODE_OAUTH_TOKEN"] = token
    return env


def _assert_fd_route(out: pathlib.Path) -> None:
    lines = out.read_text().splitlines()
    assert "token_var=unset" in lines, lines
    assert "fd_var=3" in lines, lines
    assert (out.parent / (out.name + ".fd")).read_text() == FAKE_TOKEN


def test_fd_exec_hands_claude_the_token_on_fd3_only(tmp_path: pathlib.Path) -> None:
    out = tmp_path / "rec"
    _fake_claude(tmp_path / "bin", out)
    proc = subprocess.run(["bash", str(FD_EXEC), "-p", "hi there"], env=_env(tmp_path / "bin", FAKE_TOKEN),
                          capture_output=True, text=True, timeout=20)
    assert proc.returncode == 0, proc.stderr
    _assert_fd_route(out)
    assert (tmp_path / "rec.argv").read_text().splitlines() == ["-p", "hi there"]
    assert FAKE_TOKEN not in proc.stdout + proc.stderr


def test_fd_exec_refuses_without_the_token(tmp_path: pathlib.Path) -> None:
    out = tmp_path / "rec"
    _fake_claude(tmp_path / "bin", out)
    proc = subprocess.run(["bash", str(FD_EXEC), "-p", "x"], env=_env(tmp_path / "bin", None),
                          capture_output=True, text=True, timeout=20)
    assert proc.returncode == 1
    assert "claude_credentials.py run" in proc.stderr
    assert not out.exists(), "claude ran without a token"


def test_grant_launcher_hands_claude_the_token_on_fd3_only(tmp_path: pathlib.Path) -> None:
    out = tmp_path / "rec"
    _fake_claude(tmp_path / "bin", out)
    driver = tmp_path / "driver.sh"
    driver.write_text(f"source '{LAUNCHER}'\n( claude_mcp_grant nx-mcp -- -p hi )\n")
    proc = subprocess.run(["bash", str(driver)], env=_env(tmp_path / "bin", FAKE_TOKEN),
                          capture_output=True, text=True, timeout=20)
    assert proc.returncode == 0, proc.stderr
    _assert_fd_route(out)


# Container-side harnesses are excluded by design: their init process holds
# the token via `docker run -e` either way (RDR-219 threat-model paragraph).
CONTAINER_DIRS = ("hook-surface-shakeout", "rdr208-mvv", "migration-rehearsal")


def _host_harness_scripts() -> list[pathlib.Path]:
    roots = [REPO / "tests" / "e2e", REPO / "tests" / "cc-validation"]
    return sorted(p for root in roots for p in root.rglob("*.sh")
                  if not any(d in p.parts for d in CONTAINER_DIRS))


def test_pane_launches_go_through_fd_exec() -> None:
    """Every host-side harness line that types `claude` into a tmux pane uses
    the fd launcher, so none leaves the token in Claude's exec environment."""
    sources = {p: p.read_text() for p in _host_harness_scripts()}
    assert LIB_SH in sources and (REPO / "tests/e2e/release-sandbox.sh") in sources
    bare = [f"{p.relative_to(REPO)}: {line.strip()}"
            for p, text in sources.items() for line in text.splitlines()
            if re.search(r'send[-_]keys\b.*"claude(\s|")', line)]
    assert not bare, "pane launches that bypass claude_fd_exec.sh:\n" + "\n".join(bare)
    via_fd = [line for text in sources.values() for line in text.splitlines()
              if re.search(r"send[-_]keys\b", line) and "claude_fd_exec" in line.replace("CLAUDE_FD_EXEC", "claude_fd_exec")]
    # lib.sh claude_start, scenarios 16 and 28, release-sandbox.sh's tmux mode
    assert len(via_fd) >= 4, via_fd
