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
import io
import json
import os
import re
import signal
import subprocess
import sys
import time
import types
from pathlib import Path

import pytest
from tests._module_seam import setattr_in
from tests._platform import (
    IS_WINDOWS,
    install_windows_exe_fake,
    long_dir,
    posix_only,
    prune_python,
    scrubbed_env,
    write_uv_polyglot,
)

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


def _no_cli_env(bindir: Path) -> dict[str, str]:
    """An environment with *bindir* as the whole ``PATH`` and no generation layout."""
    return scrubbed_env(PATH=str(bindir), NX_TOOLS_DIR=str(bindir.parent / "no-tools"))


def _install_fake(bindir: Path, source: str) -> dict[str, str]:
    """Put a fake ``nx-hook`` first on PATH; returns the env the shim needs.

    POSIX: a shebang script. Windows cannot execute one, and the shim finds
    ``nx-hook`` through PATHEXT and refuses a batch file, so the fake there is
    a real ``nx-hook.exe``: setuptools' console launcher, which runs the
    ``nx-hook-script.py`` beside it with the interpreter its ``#!`` line names.
    The env carries ``SYSTEMROOT``/``COMSPEC`` (``scrubbed_env``).

    ``NX_TOOLS_DIR`` names a directory that does not exist: the shim looks in the
    generation layout before ``PATH`` (RDR-228), and without the override it
    would find the developer's own install.
    """
    env = _no_cli_env(bindir)
    if not IS_WINDOWS:
        fake = bindir / "nx-hook"
        fake.write_text(source.format(python=sys.executable), encoding="utf-8")
        fake.chmod(0o755)
        return env
    install_windows_exe_fake(bindir, "nx-hook", source.format(python=f'"{sys.executable}"'))
    return env


def _run(tmp_path: Path, mode: str | None, verb: str = "mcp-connect-check", payload: bytes = b"{}"):
    bindir = tmp_path / "bin"
    bindir.mkdir(exist_ok=True)
    env = _install_fake(bindir, _FAKE) if mode is not None else _no_cli_env(bindir)
    env |= {"FAKE_MODE": mode or "", "FAKE_PIDFILE": str(tmp_path / "child.pid")}
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
    r = _run(tmp_path, "deny", verb="auto-approve")
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
    # With no CLI at all there is nothing to upgrade, so the note no longer names
    # an upgrade command (RDR-228). It is the launcher's line.
    assert b"nx self install" not in r.stderr
    assert len(r.stderr.decode().splitlines()) == 1, r.stderr


# -- an nx-hook that is found and cannot start (nexus-sv2wx.20) ---------------------------------
#
# uv writes a console script as an sh polyglot when its install path is over 127 characters.
# With the Python pruned, that script STARTS (the kernel runs the shell) and exits 126 or 127,
# which the shim used to pass on as the hook's verdict: a failed SessionStart.


def _shim(bindir: Path, *, tools: Path | None = None, verb: str = "mcp-connect-wait", **env: str):
    environ = _no_cli_env(bindir) | env
    if tools is not None:
        environ["NX_TOOLS_DIR"] = str(tools)
    return subprocess.run(
        [sys.executable, str(_SHIM), verb], input=b"{}", capture_output=True, env=environ, timeout=30, check=False,
    )


def _assert_quiet_could_not_start(r: subprocess.CompletedProcess[bytes], *names: Path) -> None:
    assert r.returncode == 0, (r.returncode, r.stderr)
    assert r.stdout == b""
    lines = r.stderr.decode().splitlines()
    assert len(lines) == 1, lines
    assert "could not start" in lines[0] and "is not installed" not in lines[0], lines
    for name in names:
        assert str(name) in lines[0], (name, lines)


@posix_only("a polyglot is a POSIX script the shell runs")
def test_a_polyglot_whose_interpreter_is_gone_is_exit_0_with_one_line(tmp_path: Path) -> None:
    """No ``pyvenv.cfg`` near the script (a ``uv tool`` bin directory): only the polyglot's
    second line says the interpreter is gone."""
    gone = long_dir(tmp_path) / "bin" / "python"
    bindir = tmp_path / "bin"
    write_uv_polyglot(bindir / "nx-hook", gone)
    _assert_quiet_could_not_start(_shim(bindir), gone)


@posix_only("a polyglot is a POSIX script the shell runs")
def test_a_polyglot_in_a_pruned_generation_at_a_long_path_is_exit_0_with_one_line(tmp_path: Path) -> None:
    """The journey's shape: the layout's generation is at a path over 127 characters, its
    scripts are polyglots, and its Python was pruned. Through ``<tools>/current``."""
    tools = tmp_path / "tools"
    gen = long_dir(tools) / "gen-20260101T000000Z"
    (gen / "bin").mkdir(parents=True)
    gone = prune_python(gen, tmp_path / "uv" / "cpython-3.12-gone")
    write_uv_polyglot(gen / "bin" / "nx-hook", gen / "bin" / "python")
    os.symlink(gen, tools / "current")
    (tmp_path / "bin").mkdir()
    _assert_quiet_could_not_start(_shim(tmp_path / "bin", tools=tools), gone)


@posix_only("a shebang names the interpreter the kernel starts")
def test_a_shebang_script_whose_interpreter_is_gone_says_could_not_start_not_not_installed(tmp_path: Path) -> None:
    """The shebang case already exited 0 (Popen's FileNotFoundError), but said the CLI
    "is not installed", which is false and sends the user to install what is there."""
    bindir = tmp_path / "bin"
    bindir.mkdir()
    fake = bindir / "nx-hook"
    fake.write_text(f"#!{tmp_path}/pruned/bin/python\nprint('never run')\n", encoding="utf-8")
    fake.chmod(0o755)
    _assert_quiet_could_not_start(_shim(bindir), tmp_path / "pruned" / "bin" / "python")


@posix_only("an exec-format failure is a POSIX errno")
def test_a_program_the_system_will_not_execute_is_exit_0_with_one_line(tmp_path: Path) -> None:
    """Not judged by ``cannot_start`` (a binary has no shebang) and refused by the
    system at the start: ``Popen`` raises ``OSError``, which used to end the shim in a
    traceback and exit 1."""
    bindir = tmp_path / "bin"
    bindir.mkdir()
    junk = bindir / "nx-hook"
    junk.write_bytes(b"\x7fELF" + bytes(64))
    junk.chmod(0o755)
    _assert_quiet_could_not_start(_shim(bindir), junk)


@posix_only("a polyglot is a POSIX script the shell runs")
def test_a_working_polyglot_is_run_and_passes_its_stdio_through(tmp_path: Path) -> None:
    """The decision is "cannot start", not "is a polyglot": a polyglot whose interpreter
    is there runs, through the shell, as the entry that uv wrote."""
    body = _FAKE.format(python="").split("\n", 1)[1]
    bindir = tmp_path / "bin"
    bindir.mkdir()
    write_uv_polyglot(bindir / "nx-hook", sys.executable, body)
    r = subprocess.run(
        [sys.executable, str(_SHIM), "mcp-connect-wait"], input=b'{"x":1}', capture_output=True,
        env=_no_cli_env(bindir) | {"FAKE_MODE": "echo"}, timeout=30, check=False,
    )
    assert (r.returncode, r.stdout) == (0, b'{"x":1}'), r.stderr


@posix_only("SIGTERM sent to the shim and forwarded to its nx-hook child; Windows has no signal delivery between processes")
def test_a_signalled_shim_takes_its_nx_hook_child_down_with_it(tmp_path: Path) -> None:
    """hooks.json's timeout ends the shim with a signal; the child must not
    outlive it (review finding, nexus-rcoze). Verified RED against the shim
    before its signal forwarding: the child was still alive after the shim
    exited."""
    bindir = tmp_path / "bin"
    bindir.mkdir()
    fake = bindir / "nx-hook"
    fake.write_text(_FAKE.format(python=sys.executable), encoding="utf-8")
    fake.chmod(0o755)
    pidfile = tmp_path / "child.pid"
    env = _no_cli_env(bindir) | {"FAKE_MODE": "sleep", "FAKE_PIDFILE": str(pidfile)}
    shim = subprocess.Popen([sys.executable, str(_SHIM), "mailbox-drain"],
                            stdin=subprocess.PIPE, env=env)
    shim.stdin.write(b"{}")
    shim.stdin.close()
    deadline = time.monotonic() + 20
    while not pidfile.exists() or not pidfile.read_text(encoding="utf-8").strip():
        assert time.monotonic() < deadline, "the fake nx-hook never started"
        time.sleep(0.05)
    child = int(pidfile.read_text(encoding="utf-8"))
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


def test_the_shim_runs_where_signal_has_no_sighup(monkeypatch: pytest.MonkeyPatch) -> None:
    """Windows has no ``signal.SIGHUP``; the forwarding loop named it
    unconditionally, so on Windows the shim raised AttributeError after it had
    started ``nx-hook`` and never returned its exit code (nexus-efk2h, RDR-224
    Phase 4). The loop is driven here against a ``signal`` stand-in that lacks
    it, with ``nx-hook`` faked, so the proof does not need a Windows box."""
    spec = importlib.util.spec_from_file_location("nx_hook_shim_nosighup", _SHIM)
    assert spec is not None and spec.loader is not None
    shim = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(shim)

    installed: list[int] = []
    shim.signal = types.SimpleNamespace(
        SIGTERM=15, SIGINT=2, signal=lambda sig, _handler: installed.append(sig),
    )

    class _Proc:
        returncode = 0

        def communicate(self, payload: bytes) -> tuple[bytes, bytes]:
            return b"verdict", b""

    setattr_in(monkeypatch, shim, "subprocess.Popen", lambda *a, **k: _Proc())
    # A found program, not the host's own install: with none, the shim exits before the start.
    monkeypatch.setattr(shim._cli_lookup, "resolve", lambda _name: "/fake/bin/nx-hook")
    monkeypatch.setattr(sys, "stdin", types.SimpleNamespace(buffer=io.BytesIO(b"{}")))
    out = io.BytesIO()
    monkeypatch.setattr(sys, "stdout", types.SimpleNamespace(buffer=out))

    assert shim.main(["auto-approve"]) == 0
    assert out.getvalue() == b"verdict"
    assert installed == [15, 2], "the signals this platform has must still be forwarded"


def test_the_shim_imports_only_the_standard_library() -> None:
    tree = ast.parse(_SHIM.read_text(encoding="utf-8"))
    found: set[str] = set()
    for n in tree.body:
        if isinstance(n, ast.Import):
            found |= {a.name.split(".")[0] for a in n.names}
        elif isinstance(n, ast.ImportFrom) and n.module:
            found.add(n.module.split(".")[0])
    assert found, "no imports found; the scan examined nothing"
    # ``_cli_lookup`` is the lookup the launcher and the shim share (RDR-228): the
    # generation layout, then the PATH-only ``_exec_path`` (finding A,
    # nexus-f9bgu.36). tests/hooks/test_nx_launch.py pins that it is standard
    # library only.
    assert found <= set(sys.stdlib_module_names) | {"__future__", "_cli_lookup"}, found
    assert "_cli_lookup" in found, "the shim no longer resolves nx-hook through the shared lookup"


@pytest.mark.lint
@pytest.mark.parametrize("tag", ["v7.55.0", "v7.55.3", "v7.56.0", "v7.57.0"])
def test_the_shim_matches_the_message_those_releases_print(tag: str) -> None:
    """Rendered from the release's own entry.py text, not retyped."""
    src = subprocess.run(
        ["git", "-C", str(_ROOT), "show", f"{tag}:src/nexus/_hook_runtime/entry.py"],
        capture_output=True, text=True, encoding="utf-8", timeout=30, check=False,
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


def test_the_unknown_verb_line_matches_with_a_crlf_ending() -> None:
    """On Windows ``nx-hook`` writes its stderr in text mode, so the line ends
    ``\\r\\n``; a ``$`` that only matches before ``\\n`` missed it and an older
    CLI's unknown verb blocked the session there (RDR-224)."""
    spec = importlib.util.spec_from_file_location("nx_hook_shim_crlf", _SHIM)
    assert spec is not None and spec.loader is not None
    shim = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(shim)
    line = "nx-hook: unknown verb 'mailbox-drain' -- no hook is registered under that name"
    assert shim.UNKNOWN_VERB_LINE.search(line + "\r\n")
    assert shim.UNKNOWN_VERB_LINE.search("warning\r\n" + line + "\r\n")
    assert not shim.UNKNOWN_VERB_LINE.search(line + " and more\r\n")


# -- the project directory (finding C, nexus-f9bgu.36) -----------------------------
#
# hooks.json launches the shim with `uv tool run --directory ${CLAUDE_PLUGIN_ROOT}`, so
# its process cwd is the plugin root. `nx-hook` verbs resolve the project from their
# own cwd, so the shim hands them the project: the payload's `cwd`, else
# CLAUDE_PROJECT_DIR, else it inherits.

_CWD_FAKE = """#!{python}
import os, sys
sys.stdin.buffer.read()
sys.stdout.write(os.path.realpath(os.getcwd()))
"""


def _run_cwd(tmp_path: Path, *, payload: bytes, project_env: Path | None, shim_cwd: Path):
    bindir = tmp_path / "bin"
    bindir.mkdir(exist_ok=True)
    env = _install_fake(bindir, _CWD_FAKE)
    if project_env is not None:
        env["CLAUDE_PROJECT_DIR"] = str(project_env)
    return subprocess.run(
        [sys.executable, str(_SHIM), "auto-approve"],
        input=payload, capture_output=True, env=env, timeout=30, check=False, cwd=str(shim_cwd),
    )


def test_nx_hook_runs_in_the_payload_cwd_not_the_plugin_root(tmp_path: Path) -> None:
    plugin_root, project, other = tmp_path / "plugin", tmp_path / "project", tmp_path / "other"
    for d in (plugin_root, project, other):
        d.mkdir()
    r = _run_cwd(tmp_path, payload=json.dumps({"cwd": str(project)}).encode(), project_env=other, shim_cwd=plugin_root)
    assert r.stdout.decode() == os.path.realpath(project), r


def test_nx_hook_falls_back_to_claude_project_dir(tmp_path: Path) -> None:
    plugin_root, project = tmp_path / "plugin", tmp_path / "project"
    plugin_root.mkdir()
    project.mkdir()
    for payload in (b"{}", b"not json", json.dumps({"cwd": str(tmp_path / "gone")}).encode()):
        r = _run_cwd(tmp_path, payload=payload, project_env=project, shim_cwd=plugin_root)
        assert r.stdout.decode() == os.path.realpath(project), (payload, r)


def test_nx_hook_inherits_the_cwd_when_no_project_is_known(tmp_path: Path) -> None:
    plugin_root = tmp_path / "plugin"
    plugin_root.mkdir()
    r = _run_cwd(tmp_path, payload=b"{}", project_env=None, shim_cwd=plugin_root)
    assert r.stdout.decode() == os.path.realpath(plugin_root), r


# -- one lookup, shared with the launcher (RDR-228 Phase 1 Step 1) -----------------
#
# conexus/launch/nx_launch.py runs the six `nx-hook` entries; this shim keeps three.
# Both must find a fresh install the same way: the generation layout first, because
# an application started before the install has the old PATH, then PATH.

_LAUNCH = _ROOT / "conexus" / "launch" / "nx_launch.py"

_WHO_FAKE = """#!{python}
import sys
sys.stdin.buffer.read()
sys.stdout.write({marker!r})
"""


def _install_who(bindir: Path, marker: str) -> None:
    bindir.mkdir(parents=True, exist_ok=True)
    _install_fake(bindir, _WHO_FAKE.replace("{marker!r}", repr(marker)))


@pytest.mark.parametrize(
    ("in_layout", "on_path", "expected"),
    [(True, True, "layout"), (True, False, "layout"), (False, True, "path"), (False, False, "")],
)
def test_the_shim_and_the_launcher_resolve_the_same_nx_hook(
    tmp_path: Path, in_layout: bool, on_path: bool, expected: str,
) -> None:
    tools, bindir = tmp_path / "tools", tmp_path / "bin"
    gen = tools / "gen-20260101T000000Z"
    gen.mkdir(parents=True)
    bindir.mkdir()
    if IS_WINDOWS:
        import _winapi  # noqa: PLC0415 -- Windows only

        _winapi.CreateJunction(str(gen), str(tools / "current"))
    else:
        os.symlink(gen, tools / "current")
    if in_layout:
        _install_who(gen / "bin", "layout")
    if on_path:
        _install_who(bindir, "path")
    env = scrubbed_env(PATH=str(bindir), NX_TOOLS_DIR=str(tools))
    seen = {}
    for label, argv in (("shim", [str(_SHIM), "auto-approve"]), ("launcher", [str(_LAUNCH), "nx-hook", "auto-approve"])):
        r = subprocess.run(
            [sys.executable, *argv], input=b"{}", capture_output=True, env=env, timeout=30, check=False,
        )
        assert r.returncode == 0, (label, r)
        seen[label] = r.stdout.decode()
        if not expected:
            assert b"`nx-hook` is not installed" in r.stderr, (label, r.stderr)
    assert seen == {"shim": expected, "launcher": expected}
