# SPDX-License-Identifier: AGPL-3.0-or-later
"""conexus/hooks/scripts/nx_hook_shim.py: an older CLI cannot block a session.

The shim changes exactly one outcome, `nx-hook` exiting 2 with the fail-closed
releases' unknown-verb line, into exit 0. Everything else passes through, so a
real verb that denies with exit 2 still denies. These cases drive the real
script as a subprocess, the way hooks.json runs it, against a fake `nx-hook`
first on PATH. tests/e2e/hook-cli-skew-gate.sh drives it against the real
published CLIs.
"""
from __future__ import annotations

import ast
import importlib.util
import os
import re
import signal
import subprocess
import sys
import time
from pathlib import Path

import pytest

_ROOT = Path(__file__).resolve().parents[2]
_SHIM = _ROOT / "conexus" / "hooks" / "scripts" / "nx_hook_shim.py"

_FAKE = """#!{python}
import os, sys
data = sys.stdin.buffer.read()
mode = os.environ["FAKE_MODE"]
verb = sys.argv[1]
if mode == "unknown":
    sys.stderr.write(f"nx-hook: unknown verb {{verb!r}} -- no hook is registered under that name\\n")
    sys.exit(2)
if mode == "deny":
    sys.stderr.write("blocked: not in an orchestrated session\\n")
    sys.exit(2)
if mode == "echo":
    sys.stdout.buffer.write(data)
    sys.exit(0)
if mode == "ledger":
    sys.exit(70)
if mode == "sleep":
    import time
    open(os.environ["FAKE_PIDFILE"], "w").write(str(os.getpid()))
    time.sleep(60)
"""


def _run(tmp_path: Path, mode: str | None, verb: str = "mcp-connect-check", payload: bytes = b"{}"):
    bindir = tmp_path / "bin"
    bindir.mkdir(exist_ok=True)
    if mode is not None:
        fake = bindir / "nx-hook"
        fake.write_text(_FAKE.format(python=sys.executable))
        fake.chmod(0o755)
    env = {"PATH": str(bindir), "FAKE_MODE": mode or "", "FAKE_PIDFILE": str(tmp_path / "child.pid")}
    return subprocess.run(
        [sys.executable, str(_SHIM), verb],
        input=payload, capture_output=True, env=env, timeout=30, check=False,
    )


def test_a_fail_closed_unknown_verb_becomes_exit_0_with_a_notice(tmp_path: Path) -> None:
    r = _run(tmp_path, "unknown")
    assert r.returncode == 0
    assert r.stdout == b""
    assert b"does not know the mcp-connect-check hook" in r.stderr


def test_a_real_deny_still_denies(tmp_path: Path) -> None:
    r = _run(tmp_path, "deny", verb="pre-close-verification")
    assert r.returncode == 2
    assert b"blocked: not in an orchestrated session" in r.stderr


def test_stdin_and_stdout_pass_through_byte_for_byte(tmp_path: Path) -> None:
    payload = b'{"hook_event_name":"UserPromptSubmit","prompt":"caf\xc3\xa9"}'
    r = _run(tmp_path, "echo", payload=payload)
    assert r.returncode == 0
    assert r.stdout == payload


def test_a_ledger_exit_code_passes_through(tmp_path: Path) -> None:
    assert _run(tmp_path, "ledger").returncode == 70


def test_no_nx_hook_at_all_is_exit_0_with_a_notice(tmp_path: Path) -> None:
    r = _run(tmp_path, None, verb="auto-approve")
    assert r.returncode == 0
    assert b"`nx-hook` is not installed" in r.stderr


def test_a_signalled_shim_takes_its_nx_hook_child_down_with_it(tmp_path: Path) -> None:
    """hooks.json's timeout ends the shim with a signal; the child must not
    outlive it (review finding, nexus-rcoze). Verified RED against the shim
    before its signal forwarding: the child was still alive after the shim
    exited."""
    bindir = tmp_path / "bin"
    bindir.mkdir()
    fake = bindir / "nx-hook"
    fake.write_text(_FAKE.format(python=sys.executable))
    fake.chmod(0o755)
    pidfile = tmp_path / "child.pid"
    env = {"PATH": str(bindir), "FAKE_MODE": "sleep", "FAKE_PIDFILE": str(pidfile)}
    shim = subprocess.Popen([sys.executable, str(_SHIM), "mailbox-drain"],
                            stdin=subprocess.PIPE, env=env)
    shim.stdin.write(b"{}")
    shim.stdin.close()
    deadline = time.monotonic() + 20
    while not pidfile.exists() or not pidfile.read_text().strip():
        assert time.monotonic() < deadline, "the fake nx-hook never started"
        time.sleep(0.05)
    child = int(pidfile.read_text())
    shim.send_signal(signal.SIGTERM)
    assert shim.wait(timeout=20) == 128 + signal.SIGTERM
    deadline = time.monotonic() + 10
    while True:
        try:
            os.kill(child, 0)
        except ProcessLookupError:
            break
        assert time.monotonic() < deadline, f"nx-hook child {child} outlived the shim"
        time.sleep(0.05)


def test_the_shim_imports_only_the_standard_library() -> None:
    tree = ast.parse(_SHIM.read_text())
    found: set[str] = set()
    for n in tree.body:
        if isinstance(n, ast.Import):
            found |= {a.name.split(".")[0] for a in n.names}
        elif isinstance(n, ast.ImportFrom) and n.module:
            found.add(n.module.split(".")[0])
    assert found, "no imports found; the scan examined nothing"
    assert found <= set(sys.stdlib_module_names) | {"__future__"}, found


@pytest.mark.lint
@pytest.mark.parametrize("tag", ["v7.55.0", "v7.55.3", "v7.56.0", "v7.57.0"])
def test_the_shim_matches_the_message_those_releases_print(tag: str) -> None:
    """Rendered from the release's own entry.py text, not retyped."""
    src = subprocess.run(
        ["git", "-C", str(_ROOT), "show", f"{tag}:src/nexus/_hook_runtime/entry.py"],
        capture_output=True, text=True, timeout=30, check=False,
    )
    if src.returncode != 0:
        pytest.fail(f"git show {tag} failed; fetch tags before trusting this pin: {src.stderr}")
    m = re.search(r'f"(nx-hook: unknown verb \{verb!r\}[^"]*)"', src.stdout)
    assert m, f"{tag}: unknown-verb message not found in entry.py"
    rendered = m.group(1).replace("{verb!r}", repr("mcp-connect-check")).replace("\\n", "\n")
    spec = importlib.util.spec_from_file_location("nx_hook_shim", _SHIM)
    assert spec is not None and spec.loader is not None
    shim = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(shim)
    assert shim.UNKNOWN_VERB_LINE.search(rendered), (tag, rendered)
