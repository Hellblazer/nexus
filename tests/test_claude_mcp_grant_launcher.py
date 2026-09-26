# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (c) 2026 Hal Hildebrand. All rights reserved.
"""Unit coverage for ``tests/e2e/lib/claude_mcp_grant.sh`` (RDR-219
amendment, "The nx-mcp dispatch grant", Phase 3b Step 2, nexus-wauo1.39).

WHAT THIS COVERS. The shared launcher a harness sources to run `claude`
with the automation token granted to nx-mcp alone: it execs `claude
--strict-mcp-config --mcp-config <(builtin printf ...)`, where the piped
config's "nexus" server entry carries `NX_HARNESS_CLAUDE_OAUTH_TOKEN` set
to the calling shell's own `CLAUDE_CODE_OAUTH_TOKEN` (put there by
`claude_credentials.py run --` ahead of the call). These tests drive the
real launcher against a FAKE `claude` on PATH -- never a real Claude
session (the real-session proofs are nexus-wauo1.40) -- and assert:

1. The harness name reaches the piped config's `mcpServers.nexus.env`
   block with the right value.
2. The value never appears on ANY process's argv -- checked two ways: the
   fake `claude`'s own recorded argv, and a `ps` snapshot taken while the
   fake `claude` is still running (it sleeps briefly so the snapshot has
   something to catch).
3. The launcher fails loudly, before ever invoking `claude`, when
   `CLAUDE_CODE_OAUTH_TOKEN` is absent from the calling shell.

WHY A SEPARATE DRIVER SCRIPT FILE, NOT `bash -c "..."` INLINE. `claude_mcp_
grant` ends in `exec`, which REPLACES the calling shell's own process
image -- control never returns to whatever sourced it. So the fixture
script here does its own setup, sources the launcher, calls the function,
and stops; everything this test asserts is recovered from side-effect
files the FAKE `claude` writes (its own recorded argv, and the piped
config it read), never from output printed after the call returns (there
is no "after").

FAKE TOKENS ONLY. Every token value in this file is a synthetic
`sk-ant-oat01-...`-shaped string, never a real credential (RDR-219's own
TOKEN RULE).
"""
from __future__ import annotations

import json
import os
import pathlib
import stat
import subprocess
import time

REPO_ROOT = pathlib.Path(__file__).resolve().parent.parent
LAUNCHER = REPO_ROOT / "tests" / "e2e" / "lib" / "claude_mcp_grant.sh"

#: A synthetic, `sk-ant-oat...`-shaped token -- never a real credential.
FAKE_TOKEN = "sk-ant-oat01-FAKEFAKEFAKEFAKEFAKEFAKEFAKEFAKEFAKE0000"


def test_launcher_file_exists_and_is_executable() -> None:
    assert LAUNCHER.is_file(), f"expected the launcher at {LAUNCHER}"
    mode = LAUNCHER.stat().st_mode
    assert mode & stat.S_IXUSR, f"{LAUNCHER} is not marked executable"


def _bash_single_quote(s: str) -> str:
    """Bash-safe single-quoting: everything between `'...'` is 100%
    literal to bash (no backslash processing at all), so the only special
    case is a literal single quote in `s` itself, closed/escaped/reopened
    the standard way. Deliberately NOT `repr()`: Python's repr escapes a
    literal backslash as `\\\\` for ITS OWN re-parsing, which bash's
    single-quote literal semantics would then take at face value as TWO
    backslash characters instead of one -- exactly the class of bug this
    helper exists to avoid (caught while writing the tricky-token test
    below, which carries a real backslash)."""
    return "'" + s.replace("'", "'\\''") + "'"


def _write_fake_claude(
    bin_dir: pathlib.Path,
    argv_file: pathlib.Path,
    config_file: pathlib.Path,
    *,
    sleep_seconds: float = 1.2,
) -> pathlib.Path:
    """A fake `claude` on PATH: records its own argv, cats the file named
    right after `--mcp-config` (the process-substitution path `claude_mcp_
    grant` hands it), sleeps briefly so a `ps` snapshot taken from the test
    has something to catch, then exits 0."""
    fake = bin_dir / "claude"
    fake.write_text(
        "#!/usr/bin/env bash\n"
        "set -u\n"
        f'printf "%s\\n" "$@" > "{argv_file}"\n'
        'cfg=""\n'
        'prev=""\n'
        'for a in "$@"; do\n'
        '  if [ "$prev" = "--mcp-config" ]; then cfg="$a"; fi\n'
        '  prev="$a"\n'
        "done\n"
        f'if [ -n "$cfg" ]; then cat "$cfg" > "{config_file}"; fi\n'
        f"sleep {sleep_seconds}\n"
        "exit 0\n"
    )
    fake.chmod(0o755)
    return fake


def _write_driver(
    tmp_path: pathlib.Path,
    *,
    token: "str | None",
    nexus_argv: "list[str]",
    claude_args: "list[str]",
    extra_servers_json: "str | None" = None,
) -> pathlib.Path:
    """A standalone driver script: sets/unsets `CLAUDE_CODE_OAUTH_TOKEN`,
    sources the real launcher, and calls `claude_mcp_grant`. Never `bash -c
    "..."` inline -- a real file, so quoting is unambiguous and the
    `exec`-terminated control flow (see module docstring) is exactly what
    a real harness would run."""
    lines = ["#!/usr/bin/env bash", "set -u"]
    if token is None:
        lines.append("unset CLAUDE_CODE_OAUTH_TOKEN")
    else:
        lines.append(f"export CLAUDE_CODE_OAUTH_TOKEN={_bash_single_quote(token)}")
    if extra_servers_json is not None:
        lines.append(
            f"export CLAUDE_MCP_GRANT_EXTRA_SERVERS_JSON={_bash_single_quote(extra_servers_json)}"
        )
    lines.append(f"source {_bash_single_quote(str(LAUNCHER))}")
    args = " ".join(_bash_single_quote(a) for a in nexus_argv)
    if claude_args:
        args += " -- " + " ".join(_bash_single_quote(a) for a in claude_args)
    lines.append(f"claude_mcp_grant {args}")
    script = tmp_path / "driver.sh"
    script.write_text("\n".join(lines) + "\n")
    script.chmod(0o755)
    return script


def _bin_env(bin_dir: pathlib.Path) -> dict:
    env = dict(os.environ)
    env.pop("CLAUDE_CODE_OAUTH_TOKEN", None)
    env.pop("NX_HARNESS_CLAUDE_OAUTH_TOKEN", None)
    env["PATH"] = f"{bin_dir}{os.pathsep}{env['PATH']}"
    return env


# ---------------------------------------------------------------------------
# The success path: the token reaches the piped config's nexus.env block,
# and never reaches any argv.
# ---------------------------------------------------------------------------


def test_launcher_puts_token_in_nexus_env_block_never_on_any_argv(tmp_path) -> None:
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    argv_file = tmp_path / "claude_argv.txt"
    config_file = tmp_path / "claude_config.json"
    _write_fake_claude(bin_dir, argv_file, config_file)
    driver = _write_driver(
        tmp_path, token=FAKE_TOKEN, nexus_argv=["nx-mcp"], claude_args=["-p", "hello"],
    )
    env = _bin_env(bin_dir)

    proc = subprocess.Popen(
        ["bash", str(driver)], env=env, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True,
    )
    # A ps snapshot taken WHILE the fake claude is still running (it
    # sleeps) -- checked below for the fake token, using a "[s]k-ant-..."
    # style anchor is unnecessary here since we grep the CAPTURED text in
    # Python rather than piping through a live `ps | grep`, which is what
    # that idiom exists to avoid matching its own grep process.
    time.sleep(0.4)
    ps_snapshot = subprocess.run(
        ["ps", "-eo", "command"], capture_output=True, text=True,
    ).stdout
    stdout, stderr = proc.communicate(timeout=10)

    assert proc.returncode == 0, f"driver failed: rc={proc.returncode} stderr={stderr!r}"
    assert FAKE_TOKEN not in ps_snapshot, (
        "the fake token appeared in a live ps snapshot's command column -- "
        "it leaked onto some process's argv"
    )
    assert argv_file.is_file(), "the fake claude was never invoked"
    argv_text = argv_file.read_text()
    assert FAKE_TOKEN not in argv_text, (
        f"the fake token appeared on claude's own argv: {argv_text!r}"
    )
    assert config_file.is_file(), "claude was not given a --mcp-config path it could read"
    config = json.loads(config_file.read_text())
    nexus_entry = config["mcpServers"]["nexus"]
    assert nexus_entry["command"] == "nx-mcp"
    assert nexus_entry["args"] == []
    assert nexus_entry["env"]["NX_HARNESS_CLAUDE_OAUTH_TOKEN"] == FAKE_TOKEN


def test_launcher_passes_nexus_args_and_extra_claude_args(tmp_path) -> None:
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    argv_file = tmp_path / "claude_argv.txt"
    config_file = tmp_path / "claude_config.json"
    _write_fake_claude(bin_dir, argv_file, config_file, sleep_seconds=0.1)
    driver = _write_driver(
        tmp_path,
        token=FAKE_TOKEN,
        nexus_argv=["nx-mcp", "--stdio", "--tenant", "default"],
        claude_args=["-p", "hi there", "--allowedTools", "mcp__nexus__search"],
    )
    env = _bin_env(bin_dir)
    proc = subprocess.run(["bash", str(driver)], env=env, capture_output=True, text=True, timeout=10)
    assert proc.returncode == 0, proc.stderr

    config = json.loads(config_file.read_text())
    nexus_entry = config["mcpServers"]["nexus"]
    assert nexus_entry["command"] == "nx-mcp"
    assert nexus_entry["args"] == ["--stdio", "--tenant", "default"]

    argv = argv_file.read_text().splitlines()
    assert argv[-4:] == ["-p", "hi there", "--allowedTools", "mcp__nexus__search"]
    assert "--strict-mcp-config" in argv


def test_launcher_json_escapes_a_token_containing_quotes_and_backslashes(tmp_path) -> None:
    """A pathological (but still fake) token, to prove the JSON produced is
    actually valid rather than merely looking right on the easy case."""
    tricky_token = 'sk-ant-oat01-fake"with\\quote-and-backslash'
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    argv_file = tmp_path / "claude_argv.txt"
    config_file = tmp_path / "claude_config.json"
    _write_fake_claude(bin_dir, argv_file, config_file, sleep_seconds=0.1)
    driver = _write_driver(
        tmp_path, token=tricky_token, nexus_argv=["nx-mcp"], claude_args=[],
    )
    env = _bin_env(bin_dir)
    proc = subprocess.run(["bash", str(driver)], env=env, capture_output=True, text=True, timeout=10)
    assert proc.returncode == 0, proc.stderr
    config = json.loads(config_file.read_text())
    assert config["mcpServers"]["nexus"]["env"]["NX_HARNESS_CLAUDE_OAUTH_TOKEN"] == tricky_token


def test_launcher_splices_extra_servers_json_alongside_nexus(tmp_path) -> None:
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    argv_file = tmp_path / "claude_argv.txt"
    config_file = tmp_path / "claude_config.json"
    _write_fake_claude(bin_dir, argv_file, config_file, sleep_seconds=0.1)
    extra = '"other":{"command":"echo","args":["hi"]}'
    driver = _write_driver(
        tmp_path, token=FAKE_TOKEN, nexus_argv=["nx-mcp"], claude_args=[], extra_servers_json=extra,
    )
    env = _bin_env(bin_dir)
    proc = subprocess.run(["bash", str(driver)], env=env, capture_output=True, text=True, timeout=10)
    assert proc.returncode == 0, proc.stderr
    config = json.loads(config_file.read_text())
    assert config["mcpServers"]["nexus"]["command"] == "nx-mcp"
    assert config["mcpServers"]["other"]["command"] == "echo"


# ---------------------------------------------------------------------------
# The failure path: fails loudly, before ever invoking claude, with no
# CLAUDE_CODE_OAUTH_TOKEN.
# ---------------------------------------------------------------------------


def test_launcher_fails_loudly_without_the_token_and_never_invokes_claude(tmp_path) -> None:
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    argv_file = tmp_path / "claude_argv.txt"
    config_file = tmp_path / "claude_config.json"
    _write_fake_claude(bin_dir, argv_file, config_file, sleep_seconds=0.1)
    driver = _write_driver(tmp_path, token=None, nexus_argv=["nx-mcp"], claude_args=["-p", "hello"])
    env = _bin_env(bin_dir)

    proc = subprocess.run(["bash", str(driver)], env=env, capture_output=True, text=True, timeout=10)
    assert proc.returncode != 0
    assert not argv_file.exists(), "claude must never be invoked when the token is absent"
    assert not config_file.exists()
    assert "CLAUDE_CODE_OAUTH_TOKEN" in proc.stderr
    assert "claude_credentials.py run --" in proc.stderr


def test_launcher_fails_loudly_with_no_nexus_command(tmp_path) -> None:
    """A usage error -- an empty command list -- fails distinctly from the
    missing-token case, and still never invokes claude."""
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    argv_file = tmp_path / "claude_argv.txt"
    config_file = tmp_path / "claude_config.json"
    _write_fake_claude(bin_dir, argv_file, config_file, sleep_seconds=0.1)
    driver = _write_driver(tmp_path, token=FAKE_TOKEN, nexus_argv=[], claude_args=["-p", "hello"])
    env = _bin_env(bin_dir)

    proc = subprocess.run(["bash", str(driver)], env=env, capture_output=True, text=True, timeout=10)
    assert proc.returncode != 0
    assert not argv_file.exists()
    assert "usage" in proc.stderr


# ---------------------------------------------------------------------------
# Code review of 7d534f0cc: control characters, shell-option leak, and the
# plugin-loading limit enforced rather than only documented.
# ---------------------------------------------------------------------------


def _run_driver(tmp_path, driver) -> "tuple[subprocess.CompletedProcess, pathlib.Path, pathlib.Path]":
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir(exist_ok=True)
    argv_file = tmp_path / "claude_argv.txt"
    config_file = tmp_path / "claude_config.json"
    _write_fake_claude(bin_dir, argv_file, config_file, sleep_seconds=0.1)
    proc = subprocess.run(
        ["bash", str(driver)], env=_bin_env(bin_dir), capture_output=True, text=True, timeout=10,
    )
    return proc, argv_file, config_file


def test_launcher_refuses_a_token_containing_a_control_character(tmp_path) -> None:
    """A newline in the value would make the piped JSON invalid; refuse it
    loudly instead of handing claude an unparseable config."""
    driver = _write_driver(
        tmp_path, token="sk-ant-oat01-fake\nsecond-line", nexus_argv=["nx-mcp"], claude_args=[],
    )
    proc, argv_file, _ = _run_driver(tmp_path, driver)
    assert proc.returncode != 0
    assert not argv_file.exists()
    assert "control character" in proc.stderr


def test_launcher_escapes_newline_and_tab_in_nexus_args(tmp_path) -> None:
    driver = _write_driver(
        tmp_path, token=FAKE_TOKEN, nexus_argv=["nx-mcp", "a\nb", "c\td"], claude_args=[],
    )
    proc, _, config_file = _run_driver(tmp_path, driver)
    assert proc.returncode == 0, proc.stderr
    assert json.loads(config_file.read_text())["mcpServers"]["nexus"]["args"] == ["a\nb", "c\td"]


def test_launcher_refuses_to_run_with_the_conexus_plugin_loaded(tmp_path) -> None:
    """RDR-219: plugin-loaded harnesses are not supported by the grant yet
    (nexus-wauo1.37), so the launcher enforces it rather than documenting it."""
    for i, flag in enumerate((["--plugin-dir", "/x/conexus"], ["--plugin-dir=/x/conexus"])):
        case = tmp_path / f"case{i}"
        case.mkdir()
        driver = _write_driver(case, token=FAKE_TOKEN, nexus_argv=["nx-mcp"], claude_args=flag)
        proc, argv_file, _ = _run_driver(case, driver)
        assert proc.returncode != 0, flag
        assert not argv_file.exists(), flag
        assert "plugin" in proc.stderr, flag


def test_sourcing_the_launcher_does_not_turn_on_nounset_in_the_caller(tmp_path) -> None:
    script = tmp_path / "caller.sh"
    script.write_text(
        "#!/usr/bin/env bash\n"
        f"source {_bash_single_quote(str(LAUNCHER))}\n"
        'case $- in *u*) echo NOUNSET_ON ;; *) echo NOUNSET_OFF ;; esac\n'
    )
    proc = subprocess.run(["bash", str(script)], capture_output=True, text=True, timeout=10)
    assert proc.stdout.strip() == "NOUNSET_OFF", proc.stdout + proc.stderr
