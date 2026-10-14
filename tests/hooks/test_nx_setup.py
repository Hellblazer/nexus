# SPDX-License-Identifier: AGPL-3.0-or-later
"""conexus/launch/nx_setup.py: the setup process and the setup directory (RDR-228
Phase 2 Step 2, nexus-sv2wx.15; residuals 1 to 5 of nexus-9i4d4).

The cases run the real script as a subprocess, the way the setup tools will, with
small stand-in programs named ``uv`` and ``nx`` that record their arguments and
behave as a rules file says. Every child environment sets ``NEXUS_CONFIG_DIR``,
``NX_TOOLS_DIR``, ``HOME`` and ``PATH`` to directories the test owns: nothing here
reads or writes the developer's own install, config directory or ``PATH``.
"""
from __future__ import annotations

import ast
import errno
import importlib.util
import json
import os
import shutil
import signal
import stat
import subprocess
import sys
import threading
import time
from pathlib import Path
from typing import Any

import pytest

from tests._module_seam import setattr_in
from tests._platform import (
    IS_WINDOWS,
    WINDOWS_SHEBANG,
    assert_owner_only,
    install_windows_exe_fake,
    posix_only,
    scrubbed_env,
)

_ROOT = Path(__file__).resolve().parents[2]
_SETUP = _ROOT / "conexus" / "launch" / "nx_setup.py"
_PLUGIN_JSON = _ROOT / "conexus" / ".claude-plugin" / "plugin.json"
_PLUGIN_VERSION = json.loads(_PLUGIN_JSON.read_text(encoding="utf-8"))["version"]

#: A stand-in program. Every start and end is one JSON line in ``FAKE_LOG``. What
#: it does is the first rule in ``FAKE_BEHAVE`` (a JSON list) whose ``prog`` is its
#: name and whose ``match`` is a prefix of its arguments: ``sleep`` seconds,
#: ``effects`` (``touch`` files, ``copytree`` a directory, ``point_current`` a
#: ``<tools>/current`` link, ``write`` a file), ``early_out`` text written and
#: flushed BEFORE the sleep, ``out`` text on standard output after it and ``exit``
#: code. With no rule, an ``nx`` answers its two identity probes: ``--version`` (a
#: Nexus form, or Nrwl's when its marker is ``foreign``) and ``self install
#: --bootstrap --dry-run`` (exit 1: "an install already exists"). A probe is logged
#: with ``"probe": true``.
_FAKE = """{shebang}
import json, os, sys, time

PROG = {name!r}
MARKER = {marker!r}


def log(event, **extra):
    rec = {{"ev": event, "prog": PROG, "marker": MARKER, "args": sys.argv[1:], "pid": os.getpid(),
            "t": time.time(), "cwd": os.getcwd(),
            "probe": PROG == "nx" and sys.argv[1:] in (["--version"], ["self", "install", "--bootstrap", "--dry-run"])}}
    rec.update(extra)
    with open(os.environ["FAKE_LOG"], "a") as f:
        f.write(json.dumps(rec) + "\\n")


def point_current(tools, gen):
    link = os.path.join(tools, "current")
    if os.path.lexists(link):
        return
    if os.name == "nt":
        import _winapi
        _winapi.CreateJunction(gen, link)
    else:
        os.symlink(gen, link)


stdin_bytes = sys.stdin.buffer.read() if "--stdin" in sys.argv[1:] else b""
log("start", stdin=len(stdin_bytes))
code = 0
try:
    rules = json.load(open(os.environ["FAKE_BEHAVE"])) if os.environ.get("FAKE_BEHAVE") else []
    args = sys.argv[1:]
    rule = next((r for r in rules if r["prog"] == PROG and args[:len(r["match"])] == r["match"]), {{}})
    if not rule and PROG == "nx" and args == ["--version"]:
        rule = {{"out": "22.1.0\\n" if MARKER == "foreign" else "nx, version 7.0.0\\n"}}
    elif not rule and PROG == "nx" and args == ["self", "install", "--bootstrap", "--dry-run"]:
        rule = {{"exit": 1}}
    for eff in rule.get("effects", []):
        if "touch" in eff:
            for p in eff["touch"]:
                os.makedirs(os.path.dirname(p), exist_ok=True)
                open(p, "a").close()
        if "copytree" in eff:
            import shutil
            shutil.copytree(eff["copytree"][0], eff["copytree"][1], dirs_exist_ok=True)
        if "point_current" in eff:
            point_current(*eff["point_current"])
        if "repoint_current" in eff:
            link = os.path.join(eff["repoint_current"][0], "current")
            if os.path.lexists(link):
                os.rmdir(link) if os.name == "nt" else os.unlink(link)
            point_current(*eff["repoint_current"])
        if "write" in eff:
            os.makedirs(os.path.dirname(eff["write"][0]), exist_ok=True)
            open(eff["write"][0], "w").write(eff["write"][1])
    sys.stdout.buffer.write(rule.get("early_out", "").encode("utf-8"))
    sys.stdout.flush()
    time.sleep(rule.get("sleep", 0))
    sys.stdout.buffer.write(rule.get("out", "").encode("utf-8"))  # bytes: a Windows console codepage cannot encode the glyphs
    sys.stdout.flush()
    code = rule.get("exit", 0)
finally:
    log("end", code=code)
sys.exit(code)
"""

#: Makes a socket bind an error in every interpreter on ``PYTHONPATH``.
_NO_LISTEN = """
import socket
def _refuse(*a, **k):
    raise RuntimeError("the setup process must not listen on a socket in this mode")
socket.socket.bind = _refuse
socket.socket.listen = _refuse
"""

_NO_NEXUS = "import sys\nsys.modules['nexus'] = None\n"

_TERMINAL = {"done", "doctor-failed", "failed", "unsupported"}


def _load_module(path: Path = _SETUP) -> Any:
    spec = importlib.util.spec_from_file_location("nx_setup_under_test", path)
    assert spec is not None and spec.loader is not None
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


class Box:
    """Every directory a setup run touches, owned by the test."""

    def __init__(self, root: Path) -> None:
        self.root = root
        self.bin = root / "bin"
        self.tools = root / "tools"
        self.config = root / "config"
        self.home = root / "home"
        self.stage = root / "stage"
        self.site = root / "site"
        self.log = root / "fake.jsonl"
        self.behave_file = root / "behave.json"
        for d in (self.bin, self.config, self.home, self.stage, self.site):
            d.mkdir(parents=True, exist_ok=True)
        self.log.write_text("")
        self.behave([])
        self.extra_env: dict[str, str] = {}
        self.install(self.bin, "uv")

    # -- stand-ins -------------------------------------------------------------

    def install(self, directory: Path, name: str, marker: str = "path") -> Path:
        directory.mkdir(parents=True, exist_ok=True)
        if IS_WINDOWS:
            script = _FAKE.format(shebang=WINDOWS_SHEBANG, name=name, marker=marker)
            return install_windows_exe_fake(directory, name, script)
        fake = directory / name
        fake.write_text(_FAKE.format(shebang=f"#!{sys.executable}", name=name, marker=marker))
        fake.chmod(0o755)
        return fake

    def nexus_on_path(self) -> None:
        """A Nexus CLI on ``PATH`` and no layout: ``nx`` with ``nx-mcp`` beside it."""
        self.install(self.bin, "nx", "path")
        self.install(self.bin, "nx-mcp", "path")

    def layout(self, marker: str = "layout") -> Path:
        """A generation holding an ``nx``, with ``<tools>/current`` pointing at it."""
        gen = self.tools / "gen-20260101T000000Z"
        self.install(gen / "bin", "nx", marker)
        self.point_current(gen)
        return gen

    def point_current(self, gen: Path) -> None:
        link = self.tools / "current"
        if IS_WINDOWS:
            import _winapi  # noqa: PLC0415 -- Windows only

            _winapi.CreateJunction(str(gen), str(link))
        else:
            os.symlink(gen, link)

    def stage_generation(self) -> Path:
        """The generation a stand-in ``uv`` builds: an ``nx`` under ``stage/``."""
        gen = self.stage / "gen-20260202T000000Z"
        self.install(gen / "bin", "nx", "built")
        return gen

    def uv_builds_nx(self, *, sleep: float = 0.0, exit: int = 0, repoint: bool = False) -> dict[str, Any]:
        """The rule for a ``uv`` that, when it runs the bootstrap build, leaves a
        generation and a ``current`` link behind. With *repoint* the link is moved
        even when one exists, as the real bootstrap flips ``current`` over a
        generation that cannot run."""
        gen = self.stage_generation()
        target = self.tools / gen.name
        rule: dict[str, Any] = {"prog": "uv", "match": [], "sleep": sleep, "exit": exit}
        if exit == 0:
            rule["effects"] = [
                {"copytree": [str(gen), str(target)]},
                {("repoint_current" if repoint else "point_current"): [str(self.tools), str(target)]},
            ]
        return rule

    def behave(self, rules: list[dict[str, Any]]) -> None:
        self.behave_file.write_text(json.dumps(rules))

    def provision(self) -> None:
        """The files ``nx init`` leaves for a local service: no lease, no URL."""
        binary = "nexus-service.exe" if IS_WINDOWS else "nexus-service"
        for rel in ("pg_credentials", f"service/{binary}", "postgres/PG_VERSION"):
            target = self.config / rel
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_text("x")

    def init_effects(self) -> list[dict[str, Any]]:
        binary = "nexus-service.exe" if IS_WINDOWS else "nexus-service"
        return [{"touch": [str(self.config / r) for r in ("pg_credentials", f"service/{binary}", "postgres/PG_VERSION")]}]

    # -- running ---------------------------------------------------------------

    @property
    def setup(self) -> Path:
        return self.config / "setup"

    def env(self, **extra: str) -> dict[str, str]:
        base = scrubbed_env(
            PATH=str(self.bin), NX_TOOLS_DIR=str(self.tools), NEXUS_CONFIG_DIR=str(self.config),
            HOME=str(self.home), USERPROFILE=str(self.home), FAKE_LOG=str(self.log),
            FAKE_BEHAVE=str(self.behave_file),
        )
        base.update(self.extra_env)
        base.update(extra)
        return base

    def hook_site(self, code: str) -> None:
        """Run every interpreter in the chain with *code* at start-up."""
        (self.site / "sitecustomize.py").write_text(code)
        self.extra_env["PYTHONPATH"] = str(self.site)

    def cli(self, *args: str, timeout: float = 60, **extra: str) -> subprocess.CompletedProcess[str]:
        return subprocess.run(
            [sys.executable, str(_SETUP), *args], capture_output=True, text=True, env=self.env(**extra),
            timeout=timeout, check=False,
        )

    def start(self, mode: str, autostart: bool | None = None, **extra: str) -> dict[str, Any]:
        args = ["start", "--mode", mode] + (["--autostart"] if autostart else [])
        r = self.cli(*args, **extra)
        assert r.returncode == 0, f"start failed: rc={r.returncode} stderr={r.stderr!r} stdout={r.stdout!r}"
        return json.loads(r.stdout)

    def status(self) -> dict[str, Any]:
        r = self.cli("status")
        assert r.returncode == 0, r.stderr
        return json.loads(r.stdout)

    def status_file(self) -> dict[str, Any] | None:
        path = self.setup / "status.json"
        for _ in range(100):
            try:
                return json.loads(path.read_text(encoding="utf-8"))
            except FileNotFoundError:
                return None
            except (PermissionError, ValueError):  # a replace in flight on Windows
                time.sleep(0.02)
        raise AssertionError("status.json is unreadable")

    def wait_terminal(self, timeout: float = 90) -> dict[str, Any]:
        """The status once the run has ended and its lock is gone."""
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            data = self.status_file()
            if data is not None and data.get("state") in _TERMINAL and not (self.setup / "lock").exists():
                return data
            time.sleep(0.05)
        raise AssertionError(f"the run did not end: status={self.status_file()!r} log={self.read_log()!r}")

    def events(self) -> list[dict[str, Any]]:
        out = []
        for line in self.log.read_text().splitlines():
            if line.strip():
                out.append(json.loads(line))
        return out

    def calls(self, prog: str | None = None, *, probes: bool = False) -> list[dict[str, Any]]:
        """Every program start. The two identity probes (``nx --version`` and
        ``nx self install --bootstrap --dry-run``) are left out unless *probes*."""
        return [
            e for e in self.events()
            if e["ev"] == "start" and (prog is None or e["prog"] == prog) and (probes or not e.get("probe"))
        ]

    def probes(self) -> list[list[str]]:
        return [[e["marker"], *e["args"]] for e in self.calls("nx", probes=True) if e.get("probe")]

    def argvs(self) -> list[list[str]]:
        return [[e["prog"], *e["args"]] for e in self.calls()]

    def wait_call(self, prog: str, first_arg: str, timeout: float = 60) -> dict[str, Any]:
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            for e in self.calls(prog):
                if e["args"][:1] == [first_arg]:
                    return e
            time.sleep(0.05)
        raise AssertionError(f"{prog} {first_arg} never started: {self.argvs()}")

    def read_log(self) -> str:
        path = self.setup / "setup.log"
        return path.read_text(errors="replace") if path.exists() else ""


@pytest.fixture
def box(tmp_path: Path) -> Box:
    return Box(tmp_path)


def _alive(pid: int) -> bool:
    """Whether the process *pid* is running. Windows: an exit-code query, because
    ``os.kill(pid, 0)`` there TERMINATES the process."""
    if not IS_WINDOWS:
        try:
            os.kill(pid, 0)
        except ProcessLookupError:
            return False
        except PermissionError:
            return True
        return True
    import ctypes  # noqa: PLC0415 -- Windows only
    from ctypes import wintypes  # noqa: PLC0415 -- Windows only

    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    kernel32.OpenProcess.argtypes = [wintypes.DWORD, wintypes.BOOL, wintypes.DWORD]
    kernel32.OpenProcess.restype = wintypes.HANDLE
    kernel32.GetExitCodeProcess.argtypes = [wintypes.HANDLE, ctypes.POINTER(wintypes.DWORD)]
    kernel32.CloseHandle.argtypes = [wintypes.HANDLE]
    handle = kernel32.OpenProcess(0x1000, False, pid)  # PROCESS_QUERY_LIMITED_INFORMATION
    if not handle:
        return False
    try:
        code = wintypes.DWORD()
        return bool(kernel32.GetExitCodeProcess(handle, ctypes.byref(code))) and code.value == 259  # STILL_ACTIVE
    finally:
        kernel32.CloseHandle(handle)


def _wait_for(predicate: Any, timeout: float = 20.0, what: str = "the condition") -> None:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if predicate():
            return
        time.sleep(0.05)
    raise AssertionError(f"{what} did not come true within {timeout} s")


def _retry(action: Any) -> Any:
    """Run *action*, retrying a Windows sharing violation for a few seconds."""
    deadline = time.monotonic() + 5
    while True:
        try:
            return action()
        except PermissionError:
            if time.monotonic() > deadline:
                raise
            time.sleep(0.02)


def _terminate(pid: int) -> None:
    try:
        os.kill(pid, getattr(signal, "SIGKILL", signal.SIGTERM))
    except (OSError, ProcessLookupError):
        pass


def _make_stale_lock(setup: Path, *, age: float = 120.0, owner: str = "dead-owner") -> None:
    lock = setup / "lock"
    lock.mkdir(parents=True)
    (lock / "owner").write_text(owner)
    heartbeat = lock / "heartbeat"
    heartbeat.write_text("0")
    old = time.time() - age
    os.utime(heartbeat, (old, old))


_BUILD = ["tool", "run", "--python", "3.12", "--from", f"conexus=={_PLUGIN_VERSION}", "nx", "self", "install", "--bootstrap"]


# -- standard library only, no nexus ---------------------------------------------------


def test_it_imports_only_the_standard_library_and_its_siblings() -> None:
    tree = ast.parse(_SETUP.read_text(encoding="utf-8"))
    found: set[str] = set()
    for node in ast.walk(tree):  # every scope: a deferred nexus import is refused too
        if isinstance(node, ast.Import):
            found |= {a.name.split(".")[0] for a in node.names}
        elif isinstance(node, ast.ImportFrom):
            assert node.level == 0, "nx_setup.py has a relative import"
            found.add((node.module or "").split(".")[0])
    assert found, "no imports found; the scan examined nothing"
    assert found <= set(sys.stdlib_module_names) | {"__future__", "_exec_path", "_cli_lookup", "_endpoint_resolve"}, found


def test_a_whole_run_works_with_nexus_unimportable(box: Box) -> None:
    """Both interpreters in the chain (the caller's and the detached run's) have
    ``nexus`` poisoned in ``sys.modules`` before any code runs. The mode is
    ``local`` over a provisioned, stopped service, so ``_endpoint_resolve`` runs
    under the poison too."""
    box.hook_site(_NO_NEXUS)
    box.layout()
    box.provision()
    box.start("local")
    done = box.wait_terminal()
    assert done["state"] == "done", done
    assert ["nx", "daemon", "service", "start"] in box.argvs(), "the service step ran, so the resolver did"


# -- the not-now marker and the modes ---------------------------------------------------


def test_not_now_writes_the_marker_and_starts_nothing(box: Box) -> None:
    out = box.start("not-now")
    assert (box.setup / "not-now").is_file()
    assert box.calls() == []
    assert not (box.setup / "lock").exists()
    assert not (box.setup / "status.json").exists()
    assert out.get("not_now") is True
    assert box.status()["not_now"] is True


def test_any_other_mode_removes_the_marker(box: Box) -> None:
    box.layout()
    box.start("not-now")
    assert (box.setup / "not-now").is_file()
    out = box.start("cli-only")
    assert not (box.setup / "not-now").exists()
    assert out.get("not_now") is False
    box.wait_terminal()


def test_the_not_now_command_is_the_same_marker(box: Box) -> None:
    r = box.cli("not-now")
    assert r.returncode == 0, r.stderr
    assert (box.setup / "not-now").is_file()
    assert box.calls() == []


def test_not_now_while_a_run_is_live_touches_nothing_else(box: Box) -> None:
    box.layout()
    box.behave([{"prog": "nx", "match": ["init"], "sleep": 4, "effects": box.init_effects()}])
    box.start("local")
    box.wait_call("nx", "init")
    out = box.start("not-now")
    assert (box.setup / "not-now").is_file()
    assert out["stage"] == "init"
    assert len(box.calls("nx")) == 1
    box.wait_terminal()


def test_cli_only_builds_and_downloads_no_service_files(box: Box) -> None:
    box.behave([box.uv_builds_nx()])
    box.start("cli-only")
    done = box.wait_terminal()
    assert box.argvs() == [["uv", *_BUILD]]
    assert done["state"] == "done"
    assert done["mode"] == "cli-only"
    assert not (box.config / "service").exists()
    assert "init" not in [a[1] for a in box.argvs() if len(a) > 1]
    assert done["next"], "a terminal status names the next step"
    assert not (box.setup / "lock").exists()


def test_the_build_is_skipped_when_the_layout_has_a_complete_install(box: Box) -> None:
    box.layout()
    box.start("cli-only")
    done = box.wait_terminal()
    assert box.calls() == []
    assert done["state"] == "done"
    assert box.probes() == [["layout", "self", "install", "--bootstrap", "--dry-run"]], (
        "the layout's nx is asked whether --bootstrap would still act; it says no"
    )


def test_the_build_names_the_plugins_own_version_and_has_no_fallback(tmp_path: Path) -> None:
    """The version is read from ``plugin.json`` beside the script. With no readable
    version the build fails and nothing is run: the two documented commands would
    run the cache prune and leave two copies."""
    root = tmp_path / "plugin"
    (root / "launch").mkdir(parents=True)
    (root / "hooks" / "scripts").mkdir(parents=True)
    (root / ".claude-plugin").mkdir()
    for name in ("_cli_lookup.py", "_exec_path.py", "_endpoint_resolve.py"):
        shutil.copy(_ROOT / "conexus" / "hooks" / "scripts" / name, root / "hooks" / "scripts" / name)
    script = root / "launch" / "nx_setup.py"
    shutil.copy(_SETUP, script)
    box = Box(tmp_path / "box")

    def run() -> dict[str, Any]:
        r = subprocess.run(
            [sys.executable, str(script), "start", "--mode", "cli-only"], capture_output=True, text=True,
            env=box.env(), timeout=60, check=False,
        )
        assert r.returncode == 0, r.stderr
        return box.wait_terminal()

    (root / ".claude-plugin" / "plugin.json").write_text(json.dumps({"name": "conexus", "version": "9.9.9"}))
    box.behave([box.uv_builds_nx()])
    assert run()["state"] == "done"
    assert f"conexus==9.9.9" in box.calls("uv")[0]["args"]

    shutil.rmtree(box.tools)
    box.log.write_text("")
    (root / ".claude-plugin" / "plugin.json").write_text("{not json")
    done = run()
    assert done["state"] == "failed"
    assert done["failed_step"] == "build"
    assert box.calls() == [], "no fallback command is run"

    (root / ".claude-plugin" / "plugin.json").write_text(json.dumps({"name": "conexus"}))
    done = run()
    assert done["state"] == "failed"
    assert box.calls() == [], "a manifest with no version is not a version"


def test_local_builds_runs_nx_init_with_its_flags_and_doctor_and_serves_no_page(box: Box) -> None:
    box.hook_site(_NO_LISTEN)
    box.behave([
        box.uv_builds_nx(),
        {"prog": "nx", "match": ["init"], "effects": box.init_effects()},
        {"prog": "nx", "match": ["doctor"], "out": "[✓] T3: reachable\n"},
    ])
    box.start("local")
    done = box.wait_terminal()
    assert done["state"] == "done", done
    assert box.argvs() == [
        ["uv", *_BUILD],
        ["nx", "init", "--yes", "--no-autostart", "--no-beads-prime"],
        ["nx", "doctor"],
    ]
    assert done["doctor"] == "pass"
    # nx is found after the build by the launcher's lookup: the generation layout.
    assert box.calls("nx")[0]["marker"] == "built"
    assert not (box.setup / "lock").exists()


def test_autostart_not_given_means_false(box: Box) -> None:
    box.layout()
    box.start("local")
    box.wait_terminal()
    assert box.argvs()[0] == ["nx", "init", "--yes", "--no-autostart", "--no-beads-prime"]


@pytest.mark.parametrize("autostart", [True, False])
def test_autostart_decides_the_login_unit_on_the_unprovisioned_path(box: Box, autostart: bool) -> None:
    box.layout()
    box.behave([{"prog": "nx", "match": ["init"], "effects": box.init_effects()}])
    box.start("local", autostart)
    box.wait_terminal()
    inits = [a for a in box.argvs() if a[1:2] == ["init"]]
    assert len(inits) == 1
    expected = ["nx", "init", "--yes", "--no-beads-prime"] if autostart else ["nx", "init", "--yes", "--no-autostart", "--no-beads-prime"]
    assert inits[0] == expected
    assert not [a for a in box.argvs() if a[1:3] == ["daemon", "service"]]


@pytest.mark.parametrize("autostart", [True, False])
def test_autostart_decides_the_login_unit_on_the_provisioned_and_down_path(box: Box, autostart: bool) -> None:
    box.layout()
    box.provision()
    box.start("local", autostart)
    done = box.wait_terminal()
    assert done["state"] == "done", done
    argvs = box.argvs()
    assert not [a for a in argvs if a[1:2] == ["init"]], "a provisioned, stopped service runs no nx init"
    service = [a for a in argvs if a[1:3] == ["daemon", "service"]]
    if autostart:
        assert service == [["nx", "daemon", "service", "install", "--autostart"], ["nx", "daemon", "service", "start"]]
    else:
        assert service == [["nx", "daemon", "service", "start"]]
    assert argvs[-1] == ["nx", "doctor"]


def test_a_service_that_resolves_is_left_alone(box: Box) -> None:
    box.layout()
    box.provision()
    box.start("local", True, NX_SERVICE_URL="http://127.0.0.1:9", NX_SERVICE_TOKEN="tok")
    done = box.wait_terminal()
    assert done["state"] == "done"
    assert box.argvs() == [["nx", "doctor"]]


def test_every_nx_init_passes_no_beads_prime(box: Box) -> None:
    box.layout()
    box.behave([{"prog": "nx", "match": ["init"], "exit": 1}])
    for autostart in (True, False):
        box.start("local", autostart)
        box.wait_terminal()
    inits = [a for a in box.argvs() if a[1:2] == ["init"]]
    assert len(inits) == 2
    assert all("--no-beads-prime" in a and "--yes" in a for a in inits)


def test_nx_is_found_in_the_layout_before_path(box: Box) -> None:
    box.layout("layout")
    box.install(box.bin, "nx", "path")
    box.start("local")
    box.wait_terminal()
    assert {c["marker"] for c in box.calls("nx")} == {"layout"}


def test_a_nexus_cli_on_path_counts_when_there_is_no_layout(box: Box) -> None:
    box.nexus_on_path()
    box.start("local")
    box.wait_terminal()
    assert box.calls("uv") == [], "an identified Nexus nx resolves, so the build is done"
    assert {c["marker"] for c in box.calls("nx")} == {"path"}
    assert box.probes() == [["path", "--version"]], "a PATH hit is identified by its version, not asked about --bootstrap"


# -- the doctor ----------------------------------------------------------------------------


def test_doctor_failing_rows_are_recorded_in_a_terminal_status(box: Box) -> None:
    box.layout()
    box.provision()
    box.behave([{
        "prog": "nx", "match": ["doctor"], "exit": 1,
        "out": "[✓] T2: ok\n[✗] T3: vector store unreachable\nsome detail\n[✗] Service: not running\n",
    }])
    box.start("local")
    done = box.wait_terminal()
    assert done["state"] == "doctor-failed"
    assert done["doctor"] == "fail"
    assert done["doctor_failing"] == ["[✗] T3: vector store unreachable", "[✗] Service: not running"]
    assert not (box.setup / "lock").exists()


# -- the lock and the status lifecycle ------------------------------------------------------


def test_a_second_call_during_a_fresh_heartbeat_starts_nothing_and_returns_the_stage(box: Box) -> None:
    box.layout()
    box.behave([{"prog": "nx", "match": ["init"], "sleep": 5, "effects": box.init_effects()}])
    first = box.start("local")
    assert first["started"] is True
    box.wait_call("nx", "init")
    second = box.start("local")
    assert second["started"] is False
    assert second["state"] == "running"
    assert second["stage"] == "init"
    assert len(box.calls("nx")) == 1, "the second call started no process"
    assert box.status()["state"] == "running"
    assert box.wait_terminal()["state"] == "done"


def test_a_stale_heartbeat_is_removed_and_the_run_resumes(box: Box) -> None:
    box.layout()
    _make_stale_lock(box.setup)
    (box.setup / "status.json").write_text(json.dumps({"state": "running", "stage": "init", "mode": "local"}))
    assert box.status()["state"] == "interrupted"
    out = box.start("local")
    assert out["started"] is True
    done = box.wait_terminal()
    assert done["state"] == "done"
    assert [a for a in box.argvs() if a[1:2] == ["init"]], "the run resumed"


def test_after_a_failure_the_next_run_resumes_at_the_failed_step(box: Box) -> None:
    box.behave([
        box.uv_builds_nx(),
        {"prog": "nx", "match": ["init"], "exit": 1, "out": "boom\n"},
    ])
    box.start("local")
    failed = box.wait_terminal()
    assert failed["state"] == "failed"
    assert failed["failed_step"] == "init"
    assert failed["log"].endswith("setup.log")
    assert "boom" in box.read_log()
    box.behave([{"prog": "nx", "match": ["init"], "effects": box.init_effects()}])
    box.start("local")
    done = box.wait_terminal()
    assert done["state"] == "done"
    assert [a[:2] for a in box.argvs()] == [["uv", "tool"], ["nx", "init"], ["nx", "init"], ["nx", "doctor"]]


def test_after_a_failed_build_the_next_run_builds_again(box: Box) -> None:
    box.behave([box.uv_builds_nx(exit=1)])
    box.start("cli-only")
    assert box.wait_terminal()["failed_step"] == "build"
    box.behave([box.uv_builds_nx()])
    box.start("cli-only")
    assert box.wait_terminal()["state"] == "done"
    assert len(box.calls("uv")) == 2


def test_a_missing_uv_fails_the_build_step(box: Box) -> None:
    empty = box.root / "empty"
    empty.mkdir()
    box.start("cli-only", PATH=str(empty))
    done = box.wait_terminal()
    assert done["state"] == "failed"
    assert done["failed_step"] == "build"
    assert box.calls() == []


def test_a_build_that_leaves_no_nx_fails_the_build_step(box: Box) -> None:
    box.behave([{"prog": "uv", "match": []}])  # exits 0 and leaves nothing
    box.start("cli-only")
    done = box.wait_terminal()
    assert done["state"] == "failed"
    assert done["failed_step"] == "build"


def test_a_run_killed_inside_nx_init_resumes_with_nx_init(box: Box) -> None:
    """The service files are on disk and no service answers, which alone reads as
    "provisioned and down". The status file says the run was inside ``nx init``, so
    the next run runs ``nx init`` again."""
    box.layout()
    box.behave([{"prog": "nx", "match": ["init"], "sleep": 60, "effects": box.init_effects()}])
    box.start("local")
    init = box.wait_call("nx", "init")
    deadline = time.monotonic() + 30
    while time.monotonic() < deadline and not (box.config / "pg_credentials").exists():
        time.sleep(0.05)
    assert (box.config / "pg_credentials").exists()
    pid = box.status_file()["pid"]
    _terminate(pid)
    _terminate(init["pid"])
    old = time.time() - 120
    os.utime(box.setup / "lock" / "heartbeat", (old, old))
    assert box.status()["state"] == "interrupted"
    box.behave([{"prog": "nx", "match": ["init"]}])
    box.log.write_text("")
    out = box.start("local")
    assert out["started"] is True
    done = box.wait_terminal()
    assert done["state"] == "done", done
    argvs = box.argvs()
    assert [a for a in argvs if a[1:2] == ["init"]], "nx init ran again"
    assert not [a for a in argvs if a[1:3] == ["daemon", "service"]], "not the start-only path"


def test_the_heartbeat_is_written_on_its_own_thread_while_a_child_runs(box: Box) -> None:
    box.layout()
    box.behave([{"prog": "nx", "match": ["init"], "sleep": 4, "effects": box.init_effects()}])
    box.start("local", NX_SETUP_HEARTBEAT_SECONDS="0.4")
    box.wait_call("nx", "init")
    seen: set[int] = set()
    heartbeat = box.setup / "lock" / "heartbeat"
    deadline = time.monotonic() + 3
    while time.monotonic() < deadline:
        try:
            seen.add(heartbeat.stat().st_mtime_ns)
        except OSError:
            pass
        time.sleep(0.05)
    assert len(seen) >= 3, f"the heartbeat did not advance while nx init ran: {len(seen)} distinct times"
    box.wait_terminal()


def _max_overlap(events: list[dict[str, Any]], prog: str) -> tuple[int, int]:
    spans: dict[int, list[float]] = {}
    for e in events:
        if e["prog"] != prog:
            continue
        spans.setdefault(e["pid"], [e["t"], e["t"]])
        spans[e["pid"]][1 if e["ev"] == "end" else 0] = e["t"]
    points = sorted([(s, 1) for s, _ in spans.values()] + [(f, -1) for _, f in spans.values()], key=lambda p: (p[0], p[1]))
    live = peak = 0
    for _, delta in points:
        live += delta
        peak = max(peak, live)
    return peak, len(spans)


def test_two_processes_that_find_a_stale_lock_together_leave_one_holder(box: Box) -> None:
    """Both callers read the same stale heartbeat and pause before removing it
    (``NX_SETUP_RECLAIM_PAUSE``); the first wakes, removes the lock and makes its
    own, and the second wakes holding a verdict that is no longer true. The test counts the builds
    running at the same moment, not exit codes: a loser that exits 0 and a winner
    that exits 0 look the same."""
    _make_stale_lock(box.setup)
    (box.setup / "status.json").write_text(json.dumps({"state": "running", "stage": "build", "mode": "cli-only"}))
    box.behave([box.uv_builds_nx(sleep=2.0)])
    env = box.env(NX_SETUP_RECLAIM_PAUSE="0.9")
    procs = []
    for _ in range(2):  # 0.3 s apart: both read the stale lock before either removes it (0.9 s pause)
        procs.append(subprocess.Popen(
            [sys.executable, str(_SETUP), "start", "--mode", "cli-only"], stdout=subprocess.PIPE, stderr=subprocess.PIPE,
            text=True, env=env,
        ))
        time.sleep(0.3)
    outs = [p.communicate(timeout=60) for p in procs]
    assert [p.returncode for p in procs] == [0, 0], outs
    started = sorted(json.loads(o[0])["started"] for o in outs)
    assert started == [False, True], f"exactly one caller took the lock: {outs}"
    box.wait_terminal()
    peak, builds = _max_overlap(box.events(), "uv")
    assert builds >= 1
    assert peak == 1, f"{peak} builds ran at the same moment"


def test_a_finishing_run_does_not_swallow_the_next_call(box: Box) -> None:
    """Between a run's terminal status and its lock removal a call finds a fresh
    lock and a terminal status. It waits for the lock instead of dropping."""
    mod = _load_module()
    setup = box.setup
    setup.mkdir(parents=True)
    lock = setup / "lock"
    lock.mkdir()
    (lock / "owner").write_text("finishing")
    (lock / "heartbeat").write_text("0")
    (setup / "status.json").write_text(json.dumps({"state": "done", "stage": "verify", "mode": "local"}))
    threading.Timer(0.4, lambda: shutil.rmtree(lock)).start()
    token = mod.acquire_lock(setup, wait_for_finish=True)
    assert token is not None


@pytest.mark.parametrize("ending", ["done", "failed", "doctor-failed"])
def test_after_any_end_the_lock_is_gone(box: Box, ending: str) -> None:
    box.layout()
    box.provision()
    if ending == "failed":
        box.behave([{"prog": "nx", "match": ["daemon"], "exit": 1}])
    elif ending == "doctor-failed":
        box.behave([{"prog": "nx", "match": ["doctor"], "exit": 1, "out": "[✗] x\n"}])
    box.start("local")
    done = box.wait_terminal()
    assert done["state"] == ending
    assert not (box.setup / "lock").exists()
    assert not any(p.name.startswith("lock") for p in box.setup.iterdir()), list(box.setup.iterdir())


@posix_only("a terminated Windows process cannot run its own clean-up")
def test_a_terminated_run_leaves_a_terminal_status_and_no_lock(box: Box) -> None:
    box.layout()
    box.behave([{"prog": "nx", "match": ["init"], "sleep": 30, "effects": box.init_effects()}])
    box.start("local")
    init = box.wait_call("nx", "init")
    os.kill(box.status_file()["pid"], signal.SIGTERM)
    done = box.wait_terminal()
    assert done["state"] == "failed"
    assert done["failed_step"] == "init"
    assert not (box.setup / "lock").exists()
    time.sleep(0.2)
    with pytest.raises(OSError):  # the child it was running is gone too
        os.kill(init["pid"], 0)


def test_the_terminal_statuses_are_listed_in_one_place() -> None:
    mod = _load_module()
    assert set(mod.TERMINAL_STATES) == _TERMINAL
    assert set(mod.STATES) == _TERMINAL | {"running"}
    tree = ast.parse(_SETUP.read_text(encoding="utf-8"))
    for state in sorted(_TERMINAL):
        literals = [n for n in ast.walk(tree) if isinstance(n, ast.Constant) and n.value == state]
        assert len(literals) == 1, f"{state!r} is spelled {len(literals)} times; it belongs in one definition"


def test_status_before_any_setup_creates_nothing(box: Box) -> None:
    out = box.status()
    assert out["state"] == "none"
    assert out["not_now"] is False
    assert list(box.config.iterdir()) == []


def test_the_command_that_starts_a_run_returns_before_the_run_ends_and_holds_no_handle(box: Box) -> None:
    """If the detached process inherited the caller's standard output or error, the
    caller's read would not end until the run did."""
    box.layout()
    box.behave([{"prog": "nx", "match": ["init"], "sleep": 6, "effects": box.init_effects()}])
    began = time.monotonic()
    proc = subprocess.Popen(
        [sys.executable, str(_SETUP), "start", "--mode", "local"], stdout=subprocess.PIPE, stderr=subprocess.PIPE,
        stdin=subprocess.PIPE, text=True, env=box.env(),
    )
    out, err = proc.communicate(timeout=60)
    assert time.monotonic() - began < 5, "the caller's pipes stayed open until the run ended"
    assert json.loads(out)["started"] is True, err
    box.wait_terminal()


# -- the status file ------------------------------------------------------------------------


def test_the_status_file_is_created_owner_only_in_the_creating_call(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """The file is created with its restriction in the same call (a mode on POSIX,
    a security descriptor on Windows), never widened and then narrowed. Writes go
    through a temporary file that is renamed over the status file, and that
    temporary file is created the same way."""
    mod = _load_module()
    setup = tmp_path / "setup"
    setup.mkdir()
    opened: list[tuple[str, int, int]] = []
    real_open = os.open

    def recording_open(path: Any, flags: int, mode: int = 0o777, **kw: Any) -> int:
        opened.append((str(path), flags, mode))
        return real_open(path, flags, mode, **kw)

    def refuse(*a: Any, **k: Any) -> None:
        raise AssertionError("the status file's restriction was applied after it was created")

    setattr_in(monkeypatch, mod, "os.open", recording_open)
    setattr_in(monkeypatch, mod, "os.chmod", refuse)
    if hasattr(os, "fchmod"):
        setattr_in(monkeypatch, mod, "os.fchmod", refuse)
    old_umask = os.umask(0)  # a permissive umask must not matter
    try:
        for i in range(3):
            mod.write_status(setup, {"state": "running", "stage": "build", "n": i})
    finally:
        os.umask(old_umask)
    status = setup / "status.json"
    assert json.loads(status.read_text())["n"] == 2
    assert_owner_only(status)
    if not IS_WINDOWS:
        creating = [(p, f, m) for p, f, m in opened if str(setup) in p]
        assert creating, "the scan saw no creation under the setup directory"
        for p, f, m in creating:
            assert f & os.O_CREAT and f & os.O_EXCL, f"{p} was not created exclusively"
            assert m == 0o600, f"{p} was created with mode {oct(m)}"


@posix_only("polls POSIX modes; the Windows form is the security descriptor asserted above")
def test_no_file_in_the_setup_directory_is_ever_seen_wider_than_owner_only(tmp_path: Path) -> None:
    mod = _load_module()
    setup = tmp_path / "setup"
    setup.mkdir()
    stop = threading.Event()
    bad: list[str] = []
    seen = [0]

    def watch() -> None:
        while not stop.is_set():
            try:
                names = os.listdir(setup)
            except OSError:
                continue
            for name in names:
                try:
                    mode = stat.S_IMODE(os.lstat(setup / name).st_mode)
                except OSError:
                    continue
                seen[0] += 1
                if mode & 0o077:
                    bad.append(f"{name}: {oct(mode)}")

    watcher = threading.Thread(target=watch, daemon=True)
    watcher.start()
    old_umask = os.umask(0)
    try:
        for i in range(300):
            mod.write_status(setup, {"state": "running", "stage": "build", "n": i})
    finally:
        os.umask(old_umask)
        stop.set()
        watcher.join(timeout=5)
    assert seen[0] > 0, "the watcher saw nothing"
    assert not bad, bad[:5]


@posix_only("the setup directory's mode is a POSIX mode; Windows inherits the config directory's access")
def test_the_setup_directory_is_owner_only(box: Box) -> None:
    box.layout()
    box.start("cli-only")
    box.wait_terminal()
    assert stat.S_IMODE(box.setup.stat().st_mode) == 0o700


def test_status_values_stay_inside_the_enumerated_set(box: Box) -> None:
    mod = _load_module()
    box.layout()
    box.provision()
    seen: set[str] = set()
    box.start("local")
    for _ in range(40):
        data = box.status_file()
        if data:
            seen.add(data["state"])
        time.sleep(0.05)
    seen.add(box.wait_terminal()["state"])
    assert seen <= set(mod.STATES), seen


# -- the detach -------------------------------------------------------------------------------


class _FakePopen:
    """Records each Popen. ``outcomes`` is consumed one entry per call: ``None``
    (started, and the relay exits 0), an int (the relay's exit code) or an OSError
    (raised at creation)."""

    calls: list[dict[str, Any]] = []
    outcomes: list[Any] = []
    kills: int = 0
    pid: int = 4242

    def __init__(self, argv: list[str], **kw: Any) -> None:
        type(self).calls.append({"argv": argv, **kw})
        outcome = type(self).outcomes.pop(0) if type(self).outcomes else None
        if isinstance(outcome, OSError):
            raise outcome
        self._hangs = outcome is _HANGS
        self._killed = False
        self._code = 0 if outcome is None or outcome is _HANGS else outcome

    def wait(self, timeout: float | None = None) -> int:
        if self._hangs and not self._killed:
            raise subprocess.TimeoutExpired("relay", timeout or 0)
        return self._code

    def kill(self) -> None:
        self._killed = True
        type(self).kills += 1


#: An outcome for :class:`_FakePopen`: the relay never exits (``wait`` times out
#: until it is killed).
_HANGS = object()


def _winerror(code: int) -> OSError:
    err = PermissionError(13, "Access is denied")
    err.winerror = code  # type: ignore[attr-defined]
    return err


@pytest.fixture
def fake_popen(monkeypatch: pytest.MonkeyPatch) -> type[_FakePopen]:
    _FakePopen.calls = []
    _FakePopen.outcomes = []
    _FakePopen.kills = 0
    return _FakePopen


def test_the_posix_detach_starts_a_new_session_with_no_inherited_stdio(tmp_path: Path, monkeypatch: pytest.MonkeyPatch, fake_popen: type[_FakePopen]) -> None:
    mod = _load_module()
    setattr_in(monkeypatch, mod, "subprocess.Popen", fake_popen)
    mod.detach(["run", "--token", "t"], tmp_path / "setup.log", windows=False)
    (call,) = fake_popen.calls
    assert call["argv"][:2] == [sys.executable, str(_SETUP)]
    assert call["start_new_session"] is True
    assert call["stdin"] == subprocess.DEVNULL
    assert call["stdout"].name == str(tmp_path / "setup.log")
    assert call["stderr"] == subprocess.STDOUT
    assert call.get("close_fds", True) is True


_DETACHED = 0x00000008
_NEW_GROUP = 0x00000200
_BREAKAWAY = 0x01000000


def test_the_windows_detach_is_a_double_launch_with_the_measured_flags(tmp_path: Path, monkeypatch: pytest.MonkeyPatch, fake_popen: type[_FakePopen]) -> None:
    mod = _load_module()
    setattr_in(monkeypatch, mod, "subprocess.Popen", fake_popen)
    mod.detach(["run", "--token", "t"], tmp_path / "setup.log", windows=True)
    (call,) = fake_popen.calls
    assert call["creationflags"] == _DETACHED | _NEW_GROUP | _BREAKAWAY
    assert call["argv"][2] == "_relay", "the relay starts the run and exits, so the run's parent is dead"
    assert call["argv"][3:] == ["run", "--token", "t"]
    # the intermediate inherits no handle of the server: all three standard handles are the null device
    assert call["stdin"] == call["stdout"] == call["stderr"] == subprocess.DEVNULL
    assert call.get("close_fds", True) is True


@pytest.mark.parametrize("first", [_winerror(5), 5], ids=["creation refused", "relay refused"])
def test_a_breakaway_refusal_retries_the_double_launch_without_breakaway(tmp_path: Path, monkeypatch: pytest.MonkeyPatch, fake_popen: type[_FakePopen], first: Any) -> None:
    """Claude Code's tree kill does not reach a child whose parent is dead, so the
    launch without breakaway is enough (228-research-33): no cannot-detach status."""
    mod = _load_module()
    setattr_in(monkeypatch, mod, "subprocess.Popen", fake_popen)
    fake_popen.outcomes = [first, None]
    mod.detach(["run", "--token", "t"], tmp_path / "setup.log", windows=True)
    one, two = fake_popen.calls
    assert one["creationflags"] & _BREAKAWAY
    assert two["creationflags"] == _DETACHED | _NEW_GROUP
    assert two["argv"][2:4] == ["_relay", "--no-breakaway"]
    assert two["argv"][4:] == ["run", "--token", "t"]
    assert two["stdin"] == two["stdout"] == two["stderr"] == subprocess.DEVNULL


def test_a_refusal_of_both_launches_is_reported_as_unsupported(tmp_path: Path, monkeypatch: pytest.MonkeyPatch, fake_popen: type[_FakePopen]) -> None:
    mod = _load_module()
    setattr_in(monkeypatch, mod, "subprocess.Popen", fake_popen)
    fake_popen.outcomes = [_winerror(5), 5]
    with pytest.raises(mod.DetachUnsupported) as info:
        mod.detach(["run"], tmp_path / "setup.log", windows=True)
    assert "cannot detach" in str(info.value)
    assert len(fake_popen.calls) == 2


def test_another_launch_error_is_not_retried(tmp_path: Path, monkeypatch: pytest.MonkeyPatch, fake_popen: type[_FakePopen]) -> None:
    mod = _load_module()
    setattr_in(monkeypatch, mod, "subprocess.Popen", fake_popen)
    fake_popen.outcomes = [_winerror(2)]
    with pytest.raises(OSError):
        mod.detach(["run"], tmp_path / "setup.log", windows=True)
    assert len(fake_popen.calls) == 1


def test_the_relay_drops_breakaway_when_told_to(tmp_path: Path, monkeypatch: pytest.MonkeyPatch, fake_popen: type[_FakePopen]) -> None:
    mod = _load_module()
    monkeypatch.setenv("NEXUS_CONFIG_DIR", str(tmp_path / "config"))
    (tmp_path / "config" / "setup").mkdir(parents=True)
    setattr_in(monkeypatch, mod, "subprocess.Popen", fake_popen)
    assert mod._relay(["run", "--token", "t"]) == 0
    assert mod._relay(["--no-breakaway", "run", "--token", "t"]) == 0
    first, second = fake_popen.calls
    assert first["creationflags"] == _DETACHED | _NEW_GROUP | _BREAKAWAY
    assert second["creationflags"] == _DETACHED | _NEW_GROUP
    assert second["argv"][2:] == ["run", "--token", "t"]
    fake_popen.outcomes = [_winerror(5)]
    assert mod._relay(["run"]) == 5


def test_an_unsupported_detach_ends_the_call_with_a_terminal_status_and_no_lock(box: Box, monkeypatch: pytest.MonkeyPatch) -> None:
    mod = _load_module()
    for key, value in box.env().items():
        monkeypatch.setenv(key, value)
    box.layout()

    def refuse(argv: list[str], log: Path, **kw: Any) -> None:
        raise mod.DetachUnsupported("setup cannot detach on this host (no WMI broker yet)")

    monkeypatch.setattr(mod, "detach", refuse)
    out = mod.start("local", False)
    assert out["state"] == "unsupported"
    assert "cannot detach" in out["message"]
    assert not (box.setup / "lock").exists()
    assert box.calls() == []


# -- fix round 1 (nexus-sv2wx.15 review): the heartbeat and a lost lock ----------------------


def _held_lock(tmp_path: Path, monkeypatch: pytest.MonkeyPatch, interval: str = "0.05") -> tuple[Any, Path, str]:
    mod = _load_module()
    setup = tmp_path / "setup"
    setup.mkdir()
    token = mod.acquire_lock(setup)
    assert token
    monkeypatch.setenv("NX_SETUP_HEARTBEAT_SECONDS", interval)
    monkeypatch.setattr(mod, "HEARTBEAT_RETRY_PAUSE", 0.01)
    return mod, setup, token


def _age_heartbeat(setup: Path, seconds: float = 100.0) -> None:
    old = time.time() - seconds
    _retry(lambda: os.utime(setup / "lock" / "heartbeat", (old, old)))


def test_a_transient_heartbeat_write_failure_does_not_end_the_heartbeat(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """One sharing violation used to end the thread for good: the lock went stale
    in 60 s while the run went on, and a second caller reclaimed it."""
    mod, setup, token = _held_lock(tmp_path, monkeypatch)
    real = mod._write_heartbeat
    failed = [0]

    def flaky(lock: Path) -> None:
        if failed[0] < 7:  # more than one beat's worth of attempts
            failed[0] += 1
            raise PermissionError(13, "Access is denied (a sharing violation)")
        real(lock)

    monkeypatch.setattr(mod, "_write_heartbeat", flaky)
    _age_heartbeat(setup)
    hb = mod._Heartbeat(setup / "lock", token)
    hb.start()
    try:
        _wait_for(lambda: failed[0] >= 7 and (mod._mtime_age(setup / "lock" / "heartbeat") or 999) < 5, what="a heartbeat after the failures")
        assert hb.is_alive()
        assert not hb.lost.is_set()
    finally:
        hb.stop()


@pytest.mark.parametrize("content", [b"\xff\xfe\x00bad", b""], ids=["not utf-8", "empty"])
def test_an_unreadable_owner_file_is_not_a_lost_lock(tmp_path: Path, monkeypatch: pytest.MonkeyPatch, content: bytes) -> None:
    mod, setup, token = _held_lock(tmp_path, monkeypatch)
    owner = setup / "lock" / "owner"
    owner.write_bytes(content)
    hb = mod._Heartbeat(setup / "lock", token)
    hb.start()
    try:
        time.sleep(0.6)  # a dozen beats over the unreadable file
        assert hb.is_alive(), "an owner read that raised ValueError ended the heartbeat"
        assert not hb.lost.is_set()
        owner.write_text(token)
        _age_heartbeat(setup)
        _wait_for(lambda: (mod._mtime_age(setup / "lock" / "heartbeat") or 999) < 5, what="a heartbeat once the owner file reads again")
    finally:
        hb.stop()


@pytest.mark.parametrize("how", ["foreign owner", "lock removed"])
def test_the_lock_is_lost_when_its_owner_is_someone_else_or_it_is_gone(tmp_path: Path, monkeypatch: pytest.MonkeyPatch, how: str) -> None:
    mod, setup, token = _held_lock(tmp_path, monkeypatch)
    hb = mod._Heartbeat(setup / "lock", token)
    hb.start()
    try:
        if how == "foreign owner":
            (setup / "lock" / "owner").write_text("someone-else")
        else:
            shutil.rmtree(setup / "lock")
        _wait_for(hb.lost.is_set, what="the loss")
    finally:
        hb.stop()


def test_holds_is_false_the_moment_the_owner_changes(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    mod, setup, token = _held_lock(tmp_path, monkeypatch)
    hb = mod._Heartbeat(setup / "lock", token)  # not started: no beat to notice it
    assert hb.holds() is True
    (setup / "lock" / "owner").write_text("another")
    assert hb.holds() is False
    assert hb.lost.is_set()


def test_update_end_and_fail_write_nothing_when_the_lock_is_not_ours(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    mod, setup, token = _held_lock(tmp_path, monkeypatch)
    run = mod._Run(setup, "local", False, mod._Heartbeat(setup / "lock", token))
    run.at("build")
    before = (setup / "status.json").read_bytes()
    (setup / "lock" / "owner").write_text("another")
    with pytest.raises(mod._LockLost):
        run.update(stage="x")
    assert run.end(mod.DONE, "m", "n") == 1
    assert run.fail("init", "m") == 1
    assert (setup / "status.json").read_bytes() == before
    assert not (setup / "status.error").exists()
    assert (setup / "lock" / "owner").read_text() == "another", "a displaced run does not touch the new holder's lock"


def test_a_run_that_loses_its_lock_kills_its_child_and_writes_nothing_more(box: Box) -> None:
    box.layout()
    box.behave([{"prog": "nx", "match": ["init"], "sleep": 60, "effects": box.init_effects()}])
    box.start("local", NX_SETUP_HEARTBEAT_SECONDS="0.2")
    init = box.wait_call("nx", "init")
    before = box.status_file()
    assert before is not None and before["stage"] == "init"
    owner = box.setup / "lock" / "owner"
    _retry(lambda: owner.write_text("someone-else"))
    _wait_for(lambda: not _alive(init["pid"]), 30, "the child's death")
    _wait_for(lambda: not _alive(before["pid"]), 30, "the run's exit")
    assert box.status_file() == before, "a displaced run wrote a status"
    assert owner.read_text() == "someone-else"
    assert (box.setup / "lock").is_dir()
    assert "no longer holds the lock" in box.read_log()


def _in_process(box: Box, monkeypatch: pytest.MonkeyPatch) -> Any:
    mod = _load_module()
    for key, value in box.env().items():
        monkeypatch.setenv(key, value)
    return mod


def test_a_status_that_cannot_be_written_still_releases_the_lock_and_says_why(box: Box, monkeypatch: pytest.MonkeyPatch) -> None:
    mod = _in_process(box, monkeypatch)
    box.layout()
    box.setup.mkdir(parents=True)
    token = mod.acquire_lock(box.setup)
    assert token

    def disk_full(setup: Path, data: dict[str, Any]) -> None:
        raise OSError(errno.ENOSPC, "No space left on device")

    monkeypatch.setattr(mod, "write_status", disk_full)
    assert mod.run_setup("cli-only", False, token) == 1
    assert not (box.setup / "lock").exists(), "the lock outlived a run whose status could not be written"
    view = mod.status_view(box.setup)
    assert view["state"] == "failed"
    assert "No space left" in view["message"]
    assert view["terminal"] is True


def test_the_next_start_clears_a_leftover_error_record(box: Box) -> None:
    box.layout()
    box.setup.mkdir(parents=True)
    (box.setup / "status.error").write_text(json.dumps({"state": "failed", "message": "old"}))
    assert box.status()["state"] == "failed"
    box.start("cli-only")
    assert box.wait_terminal()["state"] == "done"
    assert not (box.setup / "status.error").exists()
    assert box.status()["state"] == "done"


# -- the child's output and its death ----------------------------------------------------------


def test_a_child_programs_output_reaches_the_log_while_it_runs(box: Box) -> None:
    box.layout()
    box.behave([{"prog": "nx", "match": ["init"], "early_out": "EARLY-LINE\n", "sleep": 6, "effects": box.init_effects()}])
    box.start("local")
    box.wait_call("nx", "init")
    _wait_for(lambda: "EARLY-LINE" in box.read_log(), 4, "the child's first line in setup.log")
    data = box.status_file()
    assert data is not None and data["state"] == "running", "the line was read before the child ended"
    box.wait_terminal()


def test_doctor_rows_come_from_the_tail_of_a_large_log(box: Box) -> None:
    box.layout()
    box.provision()
    out = "noise line\n" * 40000 + "[✗] Service: not running\n"
    box.behave([{"prog": "nx", "match": ["doctor"], "exit": 1, "out": out}])
    box.start("local")
    done = box.wait_terminal()
    assert done["state"] == "doctor-failed"
    assert done["doctor_failing"] == ["[✗] Service: not running"]


_GRANDCHILD = (
    "import os, subprocess, sys, time\n"
    "p = subprocess.Popen([sys.executable, '-c', 'import time; time.sleep(120)'])\n"
    "open(sys.argv[1], 'w').write(f'{os.getpid()} {p.pid}')\n"
    "time.sleep(120)\n"
)


def test_a_step_that_times_out_is_killed_with_everything_it_started(box: Box, monkeypatch: pytest.MonkeyPatch) -> None:
    mod = _in_process(box, monkeypatch)
    box.setup.mkdir(parents=True)
    token = mod.acquire_lock(box.setup)
    assert token
    monkeypatch.setattr(mod, "STEP_TIMEOUT_SECONDS", 3.0)
    run = mod._Run(box.setup, "local", False, mod._Heartbeat(box.setup / "lock", token))
    pids = box.root / "pids"
    code, _ = run.cmd([sys.executable, "-c", _GRANDCHILD, str(pids)])
    assert code is None, "a timeout is reported as None"
    child, grandchild = (int(x) for x in pids.read_text().split())
    _wait_for(lambda: not _alive(child) and not _alive(grandchild), 10, "the death of the child and its child")
    assert "timed out" in box.read_log()


def test_children_start_without_a_console_window_on_windows(box: Box, monkeypatch: pytest.MonkeyPatch, fake_popen: type[_FakePopen]) -> None:
    mod = _in_process(box, monkeypatch)
    assert mod._child_kwargs(windows=True) == {"creationflags": 0x08000000}
    assert mod._child_kwargs(windows=False) == {"start_new_session": True}
    box.setup.mkdir(parents=True)
    token = mod.acquire_lock(box.setup)
    assert token
    run = mod._Run(box.setup, "local", False, mod._Heartbeat(box.setup / "lock", token))
    setattr_in(monkeypatch, mod, "subprocess.Popen", fake_popen)
    monkeypatch.setattr(mod, "_child_kwargs", lambda windows=None: {"creationflags": 0x08000000})
    assert run.cmd(["prog", "arg"]) == (0, "")
    (call,) = fake_popen.calls
    assert call["creationflags"] == 0x08000000


def test_the_windows_tree_kill_is_taskkill_by_pid_without_a_window(monkeypatch: pytest.MonkeyPatch, fake_popen: type[_FakePopen]) -> None:
    mod = _load_module()
    runs: list[tuple[list[str], dict[str, Any]]] = []

    def fake_run(argv: list[str], **kw: Any) -> subprocess.CompletedProcess[str]:
        runs.append((argv, kw))
        return subprocess.CompletedProcess(argv, 0)

    setattr_in(monkeypatch, mod, "subprocess.run", fake_run)
    proc = fake_popen(["x"])
    mod._kill_tree(proc, windows=True)
    ((argv, kw),) = runs
    assert argv[0].lower().endswith("taskkill.exe")
    assert argv[1:] == ["/T", "/F", "/PID", str(proc.pid)]
    assert kw["creationflags"] == 0x08000000
    assert fake_popen.kills == 1, "the process itself is killed too, in case taskkill failed"


# -- main never tracebacks -------------------------------------------------------------------


def test_a_config_directory_that_cannot_exist_gives_json_and_no_traceback(box: Box) -> None:
    blocker = box.root / "blocker"
    blocker.write_text("a file where a directory is needed")
    for args in (["start", "--mode", "cli-only"], ["start", "--mode", "local"], ["not-now"], ["status"]):
        r = box.cli(*args, NEXUS_CONFIG_DIR=str(blocker / "sub"))
        assert r.returncode == 0, (args, r.stderr)
        assert "Traceback" not in r.stderr, r.stderr
        out = json.loads(r.stdout)
        assert out["next"]
        if args[0] != "status":
            assert out["state"] == "error", out
            assert out["started"] is False
            assert "sub" in out["message"], "the OS error text names the path"
    assert box.calls() == []


def test_main_prints_an_error_object_for_an_unexpected_exception(box: Box, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]) -> None:
    mod = _in_process(box, monkeypatch)

    def boom(setup: Path) -> dict[str, Any]:
        raise RuntimeError("something nobody planned for")

    monkeypatch.setattr(mod, "status_view", boom)
    assert mod.main(["status"]) == 0
    out = json.loads(capsys.readouterr().out)
    assert out["state"] == "error"
    assert "RuntimeError" in out["message"] and "nobody planned" in out["message"]


def test_wrong_arguments_exit_2(box: Box) -> None:
    assert box.cli("start").returncode == 2
    assert box.cli("start", "--mode", "bogus").returncode == 2
    assert box.cli().returncode == 2


def test_a_status_write_failure_at_start_gives_an_error_object_and_releases_the_lock(box: Box, monkeypatch: pytest.MonkeyPatch) -> None:
    mod = _in_process(box, monkeypatch)
    box.layout()

    def disk_full(setup: Path, data: dict[str, Any]) -> None:
        raise OSError(errno.ENOSPC, "No space left on device")

    def no_run(*a: Any, **k: Any) -> None:
        raise AssertionError("a run must not start when its status cannot be written")

    monkeypatch.setattr(mod, "write_status", disk_full)
    monkeypatch.setattr(mod, "detach", no_run)
    out = mod.start("cli-only", False)
    assert out["state"] == "error"
    assert out["started"] is False
    assert "No space left" in out["message"]
    assert not (box.setup / "lock").exists()


def test_a_detach_failure_leaves_a_failed_status_the_status_command_returns(box: Box, monkeypatch: pytest.MonkeyPatch) -> None:
    mod = _in_process(box, monkeypatch)
    box.layout()

    def hung(argv: list[str], log: Path, **kw: Any) -> None:
        raise OSError("the setup relay did not return within 30 s")

    monkeypatch.setattr(mod, "detach", hung)
    out = mod.start("cli-only", False)
    assert out["state"] == "failed" and out["failed_step"] == "start"
    assert "relay" in out["message"]
    assert not (box.setup / "lock").exists()
    assert box.status()["state"] == "failed"


def test_a_relay_that_never_returns_is_killed_and_reported(tmp_path: Path, monkeypatch: pytest.MonkeyPatch, fake_popen: type[_FakePopen]) -> None:
    mod = _load_module()
    setattr_in(monkeypatch, mod, "subprocess.Popen", fake_popen)
    fake_popen.outcomes = [_HANGS]
    with pytest.raises(OSError) as info:
        mod.detach(["run"], tmp_path / "setup.log", windows=True)
    assert "relay" in str(info.value)
    assert fake_popen.kills == 1
    assert len(fake_popen.calls) == 1, "a hung relay is not retried: the first one may have started the run"


def test_an_orphaned_status_temp_file_is_swept_at_the_next_start(box: Box) -> None:
    box.layout()
    box.setup.mkdir(parents=True)
    old = box.setup / "status.json.4242.dead.tmp"
    fresh = box.setup / "status.json.4243.live.tmp"
    old.write_text("x")
    fresh.write_text("x")
    stale = time.time() - 300
    os.utime(old, (stale, stale))
    box.start("cli-only")
    box.wait_terminal()
    assert not old.exists()
    assert fresh.exists(), "a temp file a writer may still be using is left alone"


def test_the_status_is_flushed_to_disk_before_it_replaces_the_old_one(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    mod = _load_module()
    order: list[str] = []
    real_fsync, real_replace = os.fsync, os.replace

    def fsync(fd: int) -> None:
        order.append("fsync")
        real_fsync(fd)

    def replace(src: Any, dst: Any) -> None:
        order.append("replace")
        real_replace(src, dst)

    setattr_in(monkeypatch, mod, "os.fsync", fsync)
    setattr_in(monkeypatch, mod, "os.replace", replace)
    mod.write_status(tmp_path, {"state": "running"})
    assert order == ["fsync", "replace"]


# -- the build step: only an identified Nexus CLI ends it ---------------------------------------


def test_a_non_nexus_nx_on_path_is_never_run_and_the_build_runs(box: Box) -> None:
    box.install(box.bin, "nx", "foreign")  # no nx-mcp beside it
    box.behave([box.uv_builds_nx()])
    box.start("local")
    done = box.wait_terminal()
    assert done["state"] == "done", done
    assert len(box.calls("uv")) == 1, "the build ran"
    assert not [e for e in box.calls("nx", probes=True) if e["marker"] == "foreign"], "the foreign nx was never started"
    assert {c["marker"] for c in box.calls("nx")} == {"built"}, "nx init ran from the built install"


def test_a_foreign_nx_with_nx_mcp_beside_it_is_asked_for_its_version_and_not_trusted(box: Box) -> None:
    box.install(box.bin, "nx", "foreign")
    box.install(box.bin, "nx-mcp", "foreign")
    box.behave([box.uv_builds_nx()])
    box.start("cli-only")
    assert box.wait_terminal()["state"] == "done"
    assert len(box.calls("uv")) == 1
    assert [e["args"] for e in box.calls("nx", probes=True) if e["marker"] == "foreign"] == [["--version"]]


def test_after_a_build_a_foreign_nx_is_not_taken_for_the_install(box: Box) -> None:
    box.install(box.bin, "nx", "foreign")
    box.behave([{"prog": "uv", "match": []}])  # builds nothing
    box.start("local")
    done = box.wait_terminal()
    assert done["state"] == "failed" and done["failed_step"] == "build"
    assert not [e for e in box.calls("nx", probes=True)], "nx init was not run against a foreign nx"


def test_a_layout_install_that_bootstrap_would_still_finish_runs_the_build_step(box: Box) -> None:
    """The layout's nx exists but its shims are missing. ``--bootstrap`` itself
    says so: its dry run exits 0 where a complete install makes it refuse."""
    box.layout()
    box.behave([
        {"prog": "nx", "match": ["self", "install", "--bootstrap", "--dry-run"], "exit": 0},
        {"prog": "uv", "match": []},
    ])
    box.start("cli-only")
    done = box.wait_terminal()
    assert done["state"] == "done", done
    assert box.argvs() == [["uv", *_BUILD]]
    assert box.probes() == [["layout", "self", "install", "--bootstrap", "--dry-run"]]


# -- a Nexus CLI that is found and cannot start (Sam, 2026-10-14) ----------------------------------


def _prune_python(gen: Path, root: Path) -> None:
    """What ``uv`` does to a generation: the interpreter ``pyvenv.cfg`` names is gone."""
    (gen / "pyvenv.cfg").write_text(f"home = {root / 'uv' / 'cpython-3.12-pruned'}\n")


def test_a_layout_install_whose_interpreter_is_gone_is_not_asked_and_the_build_runs(box: Box) -> None:
    """Before: the layout's nx was asked ``--bootstrap --dry-run``, could not start,
    the OSError read as "nothing to do", and the build was skipped, so ``setup``
    reported done over an install that could not run."""
    gen = box.layout("broken")
    _prune_python(gen, box.root)
    box.behave([box.uv_builds_nx(repoint=True)])
    box.start("cli-only")
    done = box.wait_terminal()
    assert done["state"] == "done", done
    assert box.argvs() == [["uv", *_BUILD]], "the build ran"
    assert [m for m, *_ in box.probes()] == [], "the broken nx was never started"
    assert (box.tools / "current").resolve().name == "gen-20260202T000000Z", "current was moved to the new generation"


def test_a_layout_nx_that_cannot_be_spawned_at_all_is_a_build_and_not_a_skip(box: Box) -> None:
    """The OSError of the dry run itself (no pyvenv.cfg evidence, the system refuses
    to execute it) is a program that cannot start, and the build repairs it."""
    class CannotSpawn:
        def cmd(self, *_args: object, **_kwargs: object) -> None:
            raise OSError(8, "Exec format error")

    assert _load_module()._bootstrap_would_act(CannotSpawn(), "/x/nx") is True


def test_a_path_nexus_that_cannot_start_is_not_run_and_the_build_runs(box: Box) -> None:
    broken = box.root / "broken-venv"
    box.install(broken / "bin", "nx", "broken")
    box.install(broken / "bin", "nx-mcp", "broken")
    _prune_python(broken, box.root)
    box.extra_env["PATH"] = os.pathsep.join([str(box.bin), str(broken / "bin")])
    box.behave([box.uv_builds_nx()])
    box.start("cli-only")
    done = box.wait_terminal()
    assert done["state"] == "done", done
    assert len(box.calls("uv")) == 1
    assert not [e for e in box.calls("nx", probes=True) if e["marker"] == "broken"], "the broken nx was never started"


def test_a_build_that_leaves_an_nx_that_cannot_start_fails_the_build_step_and_says_why(box: Box) -> None:
    rule = box.uv_builds_nx()
    box.behave([rule])
    # The "built" generation's own pyvenv.cfg names a Python that is not there.
    _prune_python(box.stage / "gen-20260202T000000Z", box.root)
    box.start("cli-only")
    done = box.wait_terminal()
    assert done["state"] == "failed" and done["failed_step"] == "build"
    assert "cannot start" in done["message"] and "cpython-3.12-pruned" in done["message"]


def test_a_healthy_layout_install_is_still_asked_and_still_skipped(box: Box) -> None:
    """The cannot-start check must not turn a working install into a rebuild."""
    box.layout()
    (box.tools / "gen-20260101T000000Z" / "pyvenv.cfg").write_text(f"home = {box.root}\n")  # a directory that exists
    box.start("cli-only")
    done = box.wait_terminal()
    assert done["state"] == "done" and box.calls() == []
    assert len(box.probes()) == 1


def _saved_service(box: Box, *, token: bool) -> None:
    (box.config / "config.yml").write_text(
        "credentials:\n  service_url: https://svc.example\n" + ("  service_token: tok\n" if token else ""),
        encoding="utf-8",
    )


def test_a_saved_address_with_no_token_is_not_a_service_and_local_still_sets_one_up(box: Box) -> None:
    """Residual 9 at the setup process: ``local`` skipped its service step whenever an
    address resolved; the address alone is not a usable service."""
    box.layout()
    box.provision()
    _saved_service(box, token=False)
    box.start("local")
    done = box.wait_terminal()
    assert [a[1:] for a in box.argvs() if a[0] == "nx"] == [["daemon", "service", "start"], ["doctor"]], box.argvs()
    assert done["state"] == "done"


def test_a_saved_address_and_token_is_a_service_and_local_starts_nothing(box: Box) -> None:
    box.layout()
    box.provision()
    _saved_service(box, token=True)
    box.start("local")
    done = box.wait_terminal()
    assert [a[1:] for a in box.argvs() if a[0] == "nx"] == [["doctor"]], box.argvs()
    assert done["state"] == "done"


def test_find_uv_ignores_a_relative_path_entry(box: Box, monkeypatch: pytest.MonkeyPatch) -> None:
    mod = _load_module()
    rel = box.root / "rel"
    box.install(rel, "uv", "relative")
    monkeypatch.chdir(box.root)
    monkeypatch.setenv("PATH", os.pathsep.join(["rel", str(box.bin)]))
    found = mod._find_uv()
    assert found is not None and Path(found).parent == box.bin
    monkeypatch.setenv("PATH", "rel")
    assert mod._find_uv() is None


# -- the lock: the reclaim guard, a dead guard ---------------------------------------------------


def test_the_reclaim_guard_lets_only_one_of_two_reclaimers_remove_the_lock(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """Both read the same stale lock; the first is slow inside its removal. Without
    the guard the second passes its own check (the lock still exists, still
    stale, still that owner) and removes it again."""
    mod = _load_module()
    setup = tmp_path / "setup"
    _make_stale_lock(setup)
    lock = setup / "lock"
    removed: list[str] = []
    real = shutil.rmtree

    def slow_rmtree(path: Any, *a: Any, **k: Any) -> None:
        removed.append(str(path))
        time.sleep(0.8)
        real(path, *a, **k)

    setattr_in(monkeypatch, mod, "shutil.rmtree", slow_rmtree)
    results: dict[str, bool] = {}
    first = threading.Thread(target=lambda: results.__setitem__("first", mod._reclaim(setup, lock, "dead-owner")))
    second = threading.Thread(target=lambda: results.__setitem__("second", mod._reclaim(setup, lock, "dead-owner")))
    first.start()
    time.sleep(0.25)
    second.start()
    first.join(30)
    second.join(30)
    assert removed == [str(lock)], f"the lock was removed {len(removed)} times"
    assert results == {"first": True, "second": False}
    assert not (setup / "lock.reclaim").exists()


def test_a_reclaim_that_read_another_owner_removes_nothing(tmp_path: Path) -> None:
    mod = _load_module()
    setup = tmp_path / "setup"
    _make_stale_lock(setup, owner="current-owner")
    assert mod._reclaim(setup, setup / "lock", "an-owner-read-earlier") is False
    assert (setup / "lock").is_dir()


def test_a_dead_reclaimers_guard_is_removed_once_it_is_stale(tmp_path: Path) -> None:
    mod = _load_module()
    setup = tmp_path / "setup"
    _make_stale_lock(setup)
    guard = setup / "lock.reclaim"
    guard.mkdir()
    old = time.time() - 120
    os.utime(guard, (old, old))
    assert mod._reclaim(setup, setup / "lock", "dead-owner") is True
    assert not (setup / "lock").exists()
    assert not guard.exists()


def test_a_young_guard_is_respected(tmp_path: Path) -> None:
    mod = _load_module()
    setup = tmp_path / "setup"
    _make_stale_lock(setup)
    guard = setup / "lock.reclaim"
    guard.mkdir()
    assert mod._reclaim(setup, setup / "lock", "dead-owner") is False
    assert (setup / "lock").is_dir() and guard.is_dir()


# -- the plugin version, a corrupt status file ---------------------------------------------------


def _plugin_copy(tmp_path: Path) -> tuple[Path, Path]:
    root = tmp_path / "plugin"
    (root / "launch").mkdir(parents=True)
    (root / "hooks" / "scripts").mkdir(parents=True)
    (root / ".claude-plugin").mkdir()
    for name in ("_cli_lookup.py", "_exec_path.py", "_endpoint_resolve.py"):
        shutil.copy(_ROOT / "conexus" / "hooks" / "scripts" / name, root / "hooks" / "scripts" / name)
    script = root / "launch" / "nx_setup.py"
    shutil.copy(_SETUP, script)
    return root / ".claude-plugin" / "plugin.json", script


@pytest.mark.parametrize(
    "version, ok",
    [
        ("7.78.0", True), ("1.0.0rc1", True), ("2!1.0+local.1", True), ("0.1.0.dev3", True),
        ("1.0 --index-url=http://x", False), ("--version", False), ("", False), ("1.0\n", False),
        (" 1.0", False), ("1.0;rm", False), ("1.0\x00", False), (5, False), (None, False),
    ],
)
def test_the_plugin_version_is_accepted_only_when_it_looks_like_a_version(tmp_path: Path, version: Any, ok: bool) -> None:
    manifest, script = _plugin_copy(tmp_path)
    manifest.write_text(json.dumps({"name": "conexus", "version": version}))
    mod = _load_module(script)
    assert mod.plugin_version() == (version if ok else None)


@pytest.mark.parametrize(
    "content",
    [b"", b"{", b"[]", b"null", b'{"state": 5}', b'{"state": "bogus"}', b"\xff\xfe\x00\x01"],
)
def test_a_corrupt_status_file_reads_as_no_status_and_does_not_block_a_start(box: Box, content: bytes) -> None:
    box.layout()
    box.setup.mkdir(parents=True)
    (box.setup / "status.json").write_bytes(content)
    view = box.status()
    assert view["state"] == "none"
    assert view["terminal"] is False and view["next"]
    out = box.start("cli-only")
    assert out["started"] is True
    assert box.wait_terminal()["state"] == "done"


# -- Windows: the host's job -----------------------------------------------------------------------

_KILL_ON_CLOSE = 0x2000
_BREAKAWAY_OK = 0x0800
_SILENT_BREAKAWAY_OK = 0x1000


@pytest.mark.parametrize(
    "flags, retried",
    [
        (None, True), (0, True), (_BREAKAWAY_OK, True),
        (_KILL_ON_CLOSE | _BREAKAWAY_OK, True), (_KILL_ON_CLOSE | _SILENT_BREAKAWAY_OK, True),
        (_KILL_ON_CLOSE, False),
    ],
)
def test_the_no_breakaway_retry_is_not_made_into_a_job_that_would_kill_the_run(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, fake_popen: type[_FakePopen], flags: int | None, retried: bool,
) -> None:
    mod = _load_module()
    setattr_in(monkeypatch, mod, "subprocess.Popen", fake_popen)
    monkeypatch.setattr(mod, "_job_limit_flags", lambda: flags)
    fake_popen.outcomes = [_winerror(5), None]
    if retried:
        mod.detach(["run"], tmp_path / "setup.log", windows=True)
        assert len(fake_popen.calls) == 2
    else:
        with pytest.raises(mod.DetachUnsupported) as info:
            mod.detach(["run"], tmp_path / "setup.log", windows=True)
        assert "cannot detach" in str(info.value)
        assert len(fake_popen.calls) == 1, "no launch was made that the job would kill"


def test_the_job_query_answers_without_raising() -> None:
    mod = _load_module()
    flags = mod._job_limit_flags()
    if not IS_WINDOWS:
        assert flags is None
    else:
        assert flags is None or isinstance(flags, int)


# -- the interface P2.3 and P2.4 build on ---------------------------------------------------------


def test_no_status_text_names_the_cloud_mode() -> None:
    tree = ast.parse(_SETUP.read_text(encoding="utf-8"))
    docstrings = {
        id(node.body[0].value) for node in ast.walk(tree)
        if isinstance(node, (ast.Module, ast.FunctionDef, ast.ClassDef, ast.AsyncFunctionDef))
        and node.body and isinstance(node.body[0], ast.Expr) and isinstance(node.body[0].value, ast.Constant)
    }
    strings = [n.value for n in ast.walk(tree) if isinstance(n, ast.Constant) and isinstance(n.value, str) and id(n) not in docstrings]
    assert strings, "the scan examined nothing"
    assert [t for t in strings if "cloud" in t.lower() and t != "cloud"] == []


def test_a_cloud_call_changes_nothing_and_says_so(box: Box) -> None:
    box.layout()
    box.behave([{"prog": "nx", "match": ["init"], "exit": 1}])
    box.start("local")
    assert box.wait_terminal()["state"] == "failed"
    box.start("not-now")
    before = (box.setup / "status.json").read_bytes()
    calls = len(box.calls())
    out = box.start("cloud")
    assert out["started"] is False
    assert out["refused"]
    assert out["state"] == "failed", "the record is still the record"
    assert (box.setup / "status.json").read_bytes() == before
    assert (box.setup / "not-now").is_file(), "a call that does nothing does not remove the marker"
    assert not (box.setup / "lock").exists()
    assert len(box.calls()) == calls
    words = out["refused"] + " " + out["next"]
    assert "`" not in words and " nx " not in f" {words} ", "the cloud message names no CLI command"


def test_a_cloud_call_on_a_fresh_box_creates_nothing(box: Box) -> None:
    out = box.start("cloud")
    assert out["state"] == "none" and out["started"] is False and out["refused"]
    assert list(box.config.iterdir()) == []
    assert box.calls() == []


def test_a_cloud_call_while_a_run_is_live_leaves_it_alone(box: Box) -> None:
    box.layout()
    box.behave([{"prog": "nx", "match": ["init"], "sleep": 4, "effects": box.init_effects()}])
    box.start("local")
    box.wait_call("nx", "init")
    out = box.start("cloud")
    assert out["state"] == "running" and out["stage"] == "init" and out["started"] is False
    assert box.wait_terminal()["state"] == "done"


def test_a_refused_start_names_the_live_runs_mode(box: Box) -> None:
    box.layout()
    box.behave([{"prog": "nx", "match": ["init"], "sleep": 4, "effects": box.init_effects()}])
    box.start("local")
    box.wait_call("nx", "init")
    out = box.start("cli-only")
    assert out["started"] is False
    assert out["mode"] == "local"
    assert "local" in out["refused"] and "cli-only" in out["refused"]
    box.wait_terminal()


def test_provisioned_is_the_three_file_test(box: Box, monkeypatch: pytest.MonkeyPatch) -> None:
    mod = _in_process(box, monkeypatch)
    assert mod.provisioned(box.config) is False
    box.provision()
    assert mod.provisioned(box.config) is True
    assert mod.provisioned() is True, "the default is the Nexus config directory"
    binary = "nexus-service.exe" if IS_WINDOWS else "nexus-service"
    for rel in ("pg_credentials", f"service/{binary}", "postgres/PG_VERSION"):
        target = box.config / rel
        target.unlink()
        assert mod.provisioned(box.config) is False, f"{rel} missing"
        target.write_text("x")


def test_the_whole_state_set_is_public_and_status_view_stays_inside_it(box: Box, monkeypatch: pytest.MonkeyPatch) -> None:
    mod = _in_process(box, monkeypatch)
    assert set(mod.ALL_STATES) == _TERMINAL | {"running", "interrupted", "none", "error"}
    assert set(mod.STATES) == _TERMINAL | {"running"}, "STATES is what a status file may hold"
    assert mod.INTERRUPTED == "interrupted" and mod.NONE == "none"
    assert mod.status_view(box.setup)["state"] == "none"
    box.setup.mkdir(parents=True)
    (box.setup / "status.json").write_text(json.dumps({"state": "running"}))
    assert mod.status_view(box.setup)["state"] == "interrupted"
    for name in ("provisioned", "status_view", "config_dir", "setup_dir", "read_status"):
        assert callable(getattr(mod, name)) and getattr(mod, name).__doc__, name
    assert "imports" in mod.__doc__ and "P2.4" in mod.__doc__


def test_the_config_directory_is_absolute(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    mod = _load_module()
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("NEXUS_CONFIG_DIR", "relative/config")
    assert mod.config_dir().is_absolute()
    assert mod.config_dir() == tmp_path / "relative" / "config"
    assert mod.setup_dir().is_absolute()
    assert "NEXUS_CONFIG_DIR" in mod.config_dir.__doc__ and "P2.R2" in mod.config_dir.__doc__


def test_a_lock_directory_still_being_deleted_on_windows_reads_as_held(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path,
) -> None:
    """On Windows mkdir and stat of a directory pending deletion fail with access
    denied for a moment; that is a held lock, not an error. Elsewhere the
    PermissionError is a real one and propagates."""
    mod = _load_module()

    def pending(*_a: Any, **_k: Any) -> None:
        raise PermissionError(13, "access denied")

    setattr_in(monkeypatch, mod, "os.mkdir", pending)
    setattr_in(monkeypatch, mod, "os.name", "nt")
    assert mod._make_lock(tmp_path / "lock", "token") is False
    setattr_in(monkeypatch, mod, "os.name", "posix")
    with pytest.raises(PermissionError):
        mod._make_lock(tmp_path / "lock", "token")


def test_a_path_that_cannot_be_examined_reads_as_present(tmp_path: Path) -> None:
    mod = _load_module()

    class Unexaminable(type(tmp_path)):  # type: ignore[misc]
        def exists(self, *a: Any, **k: Any) -> bool:
            raise PermissionError(13, "access denied")

    assert mod._exists(Unexaminable(tmp_path)) is True
    assert mod._exists(tmp_path / "absent") is False


def test_a_relative_config_directory_reaches_the_detached_run(box: Box) -> None:
    """The run starts in the setup directory, so a relative NEXUS_CONFIG_DIR read
    there names a different tree: it found no lock of its own and exited, while
    start had said started (verification review of ceb991af2)."""
    box.behave([box.uv_builds_nx()])
    r = subprocess.run(
        [sys.executable, str(_SETUP), "start", "--mode", "cli-only"], capture_output=True, text=True,
        env=box.env(NEXUS_CONFIG_DIR=box.config.name), cwd=str(box.config.parent), timeout=60, check=False,
    )
    assert r.returncode == 0 and json.loads(r.stdout)["started"] is True, r.stdout + r.stderr
    done = box.wait_terminal()
    assert done["state"] == "done", box.read_log()
    assert box.argvs() == [["uv", *_BUILD]]
    assert not (box.setup / box.config.name).exists()


@pytest.mark.parametrize("autostart", [True, False])
def test_autostart_asked_of_a_running_service_says_the_login_unit_was_not_registered(box: Box, autostart: bool) -> None:
    box.layout()
    box.provision()
    box.start("local", autostart, NX_SERVICE_URL="http://127.0.0.1:9", NX_SERVICE_TOKEN="tok")
    done = box.wait_terminal()
    assert done["state"] == "done"
    assert not [a for a in box.argvs() if a[1:3] == ["daemon", "service"]]
    if autostart:
        assert "login unit was not registered" in done["message"]
        assert "nx daemon service install --autostart" in done["message"]
    else:
        assert "login unit" not in done["message"]
