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
#: ``<tools>/current`` link, ``write`` a file), ``out`` text on standard output
#: and ``exit`` code.
_FAKE = """{shebang}
import json, os, sys, time

PROG = {name!r}
MARKER = {marker!r}


def log(event, **extra):
    rec = {{"ev": event, "prog": PROG, "marker": MARKER, "args": sys.argv[1:], "pid": os.getpid(),
            "t": time.time(), "cwd": os.getcwd()}}
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
        if "write" in eff:
            os.makedirs(os.path.dirname(eff["write"][0]), exist_ok=True)
            open(eff["write"][0], "w").write(eff["write"][1])
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


def _load_module() -> Any:
    spec = importlib.util.spec_from_file_location("nx_setup_under_test", _SETUP)
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

    def uv_builds_nx(self, *, sleep: float = 0.0, exit: int = 0) -> dict[str, Any]:
        """The rule for a ``uv`` that, when it runs the bootstrap build, leaves a
        generation and a ``current`` link behind."""
        gen = self.stage_generation()
        target = self.tools / gen.name
        rule: dict[str, Any] = {"prog": "uv", "match": [], "sleep": sleep, "exit": exit}
        if exit == 0:
            rule["effects"] = [
                {"copytree": [str(gen), str(target)]},
                {"point_current": [str(self.tools), str(target)]},
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

    def calls(self, prog: str | None = None) -> list[dict[str, Any]]:
        return [e for e in self.events() if e["ev"] == "start" and (prog is None or e["prog"] == prog)]

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
    ``nexus`` poisoned in ``sys.modules`` before any code runs."""
    box.hook_site(_NO_NEXUS)
    box.layout()
    box.start("cli-only")
    done = box.wait_terminal()
    assert done["state"] == "done", done


# -- the not-now marker and the modes ---------------------------------------------------


def test_not_now_writes_the_marker_and_starts_nothing(box: Box) -> None:
    out = box.start("not-now")
    assert (box.setup / "not-now").is_file()
    assert box.calls() == []
    assert not (box.setup / "lock").exists()
    assert not (box.setup / "status.json").exists()
    assert out.get("not_now") is True
    assert box.status()["not_now"] is True


@pytest.mark.parametrize("mode", ["cli-only", "cloud"])
def test_any_other_mode_removes_the_marker(box: Box, mode: str) -> None:
    box.layout()
    box.start("not-now")
    assert (box.setup / "not-now").is_file()
    out = box.start(mode)
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


def test_the_build_is_skipped_when_nx_already_resolves(box: Box) -> None:
    box.layout()
    box.start("cli-only")
    done = box.wait_terminal()
    assert box.calls() == []
    assert done["state"] == "done"


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
    box.start("local", True, NX_SERVICE_URL="http://127.0.0.1:9")
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


def test_nx_on_path_counts_when_there_is_no_layout(box: Box) -> None:
    box.install(box.bin, "nx", "path")
    box.start("local")
    box.wait_terminal()
    assert box.calls("uv") == [], "nx resolves, so the build is done"
    assert {c["marker"] for c in box.calls("nx")} == {"path"}


def test_cloud_ends_at_once_and_says_the_build_has_no_credentials_page(box: Box) -> None:
    out = box.start("cloud")
    assert out["state"] == "unsupported"
    assert "credentials page" in out["message"]
    assert box.calls() == []
    done = box.wait_terminal()
    assert done["state"] == "unsupported"
    assert not (box.setup / "lock").exists()


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


@pytest.mark.parametrize("ending", ["done", "failed", "doctor-failed", "unsupported"])
def test_after_any_end_the_lock_is_gone(box: Box, ending: str) -> None:
    box.layout()
    box.provision()
    if ending == "failed":
        box.behave([{"prog": "nx", "match": ["daemon"], "exit": 1}])
    elif ending == "doctor-failed":
        box.behave([{"prog": "nx", "match": ["doctor"], "exit": 1, "out": "[✗] x\n"}])
    box.start("cloud" if ending == "unsupported" else "local")
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

    def __init__(self, argv: list[str], **kw: Any) -> None:
        type(self).calls.append({"argv": argv, **kw})
        outcome = type(self).outcomes.pop(0) if type(self).outcomes else None
        if isinstance(outcome, OSError):
            raise outcome
        self._code = 0 if outcome is None else outcome

    def wait(self, timeout: float | None = None) -> int:
        return self._code


def _winerror(code: int) -> OSError:
    err = PermissionError(13, "Access is denied")
    err.winerror = code  # type: ignore[attr-defined]
    return err


@pytest.fixture
def fake_popen(monkeypatch: pytest.MonkeyPatch) -> type[_FakePopen]:
    _FakePopen.calls = []
    _FakePopen.outcomes = []
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
