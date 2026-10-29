# SPDX-License-Identifier: AGPL-3.0-or-later
"""nexus-vyy65: every child that imports mineru/torch/onnxruntime puts nx's app-local
VC++ DLL directories on its own DLL search path first.

``os.add_dll_directory`` is per process, and on Python >= 3.8 Windows does not search PATH
for an extension module's dependent DLLs. MinerU runs in children that never imported nexus
(the per-batch worker, the mineru-api server), so on a Windows box with no VC++
redistributable they died with ``ImportError: DLL load failed while importing
onnxruntime_pybind11_state`` (torch the same way on c10.dll), and ``nx doctor --check-mineru``
stayed green because it imported mineru in its own process (the one that had run the helper).

``import nexus`` runs ``_add_vc_runtime_dirs`` (nexus/__init__.py), so a child that imports
nexus before mineru has run it. Windows is not available in CI, so the order is pinned three
ways: by the script text, by a child run against a fake ``mineru`` package that records
whether nexus was already imported when mineru's module body ran, and by the argv a launch
site builds.
"""
from __future__ import annotations

import ast
import importlib
import json
import os
import re
import subprocess
import sys
import types
from pathlib import Path

import pytest

from nexus import _mineru_api_child, _mineru_spawn, pdf_extractor, pdf_stack, upgrade_finish
from nexus.commands import doctor
from nexus.pdf_stack import StackState
from nexus.upgrade_finish import _classify
from nexus.util.nx_argv import console_python
from tests._mineru_rearm import NX_MINERU_EXPECTED, mineru_importorskip
from tests._module_seam import setattr_in

_SRC = Path(__file__).resolve().parents[1] / "src" / "nexus"
_HEAVY = ("mineru", "torch", "onnxruntime", "magika")


def _first_heavy_import_offset(script: str) -> int:
    m = re.search(r"^\s*(?:from|import)\s+(?:%s)\b" % "|".join(_HEAVY), script, re.M)
    assert m, "the script imports nothing heavy; the order test has nothing to pin"
    return m.start()


def _nexus_import_offset(script: str) -> int:
    m = re.search(r"^\s*import\s+nexus\b", script, re.M)
    assert m, "the script never imports nexus, so the DLL-directory helper never runs in it"
    return m.start()


def _fake_mineru(root: Path, common_body: str, extra: dict[str, str] | None = None) -> Path:
    """A ``mineru`` package ahead of the real one on a child's PYTHONPATH."""
    pkg = root / "mineru"
    (pkg / "cli").mkdir(parents=True)
    (pkg / "__init__.py").write_text("")
    (pkg / "cli" / "__init__.py").write_text("")
    (pkg / "cli" / "common.py").write_text(common_body)
    for name, body in (extra or {}).items():
        (pkg / "cli" / name).write_text(body)
    return root


def _env(path: Path) -> dict[str, str]:
    env = dict(os.environ)
    env["PYTHONPATH"] = str(path)
    return env


# ── the helper really runs at import ───────────────────────────────────────


def test_import_nexus_runs_the_dll_dir_helper_at_module_level() -> None:
    """The children rely on this: a top-level call, not one behind a branch or a function."""
    tree = ast.parse((_SRC / "__init__.py").read_text(encoding="utf-8"))
    calls = [
        n.value.func.id
        for n in tree.body
        if isinstance(n, ast.Expr) and isinstance(n.value, ast.Call) and isinstance(n.value.func, ast.Name)
    ]
    assert "_add_vc_runtime_dirs" in calls


# ── the per-batch worker ───────────────────────────────────────────────────


def test_worker_script_imports_nexus_before_any_heavy_import() -> None:
    script = pdf_extractor._MINERU_WORKER_SCRIPT
    assert _nexus_import_offset(script) < _first_heavy_import_offset(script)


def test_worker_child_has_imported_nexus_when_mineru_loads(tmp_path: Path) -> None:
    marker = tmp_path / "marker"
    fake = _fake_mineru(
        tmp_path / "fake",
        f"import sys\nopen({str(marker)!r}, 'w').write(str('nexus' in sys.modules))\n"
        "def do_parse(*a, **k):\n    pass\n",
    )
    pdf = tmp_path / "x.pdf"
    pdf.write_bytes(b"%PDF-1.4\n")
    proc = subprocess.run(
        [sys.executable, "-c", pdf_extractor._MINERU_WORKER_SCRIPT, str(pdf), str(tmp_path / "out"), "0", "none"],
        env=_env(fake), capture_output=True, text=True, timeout=60,
    )
    assert proc.returncode == 0, proc.stderr
    assert marker.read_text() == "True", "mineru was imported before nexus: the DLL dirs were not on the search path"


# ── the mineru-api server child ────────────────────────────────────────────


def test_server_child_module_imports_nexus_before_anything_that_loads_mineru() -> None:
    """THE guard for the ``import nexus`` line in the server child.

    Under ``python -m nexus._mineru_api_child`` Python imports the ``nexus`` package before it runs
    this module's body, so no child-process test can fail when the line is removed or moved (the
    package init that sets the DLL directories runs either way). The line is what keeps
    ``python <path to the file>`` and any other launch form safe, and it states the contract; this
    check of the source is what fails on its removal or on a mineru import or entry-point load that
    precedes it."""
    tree = ast.parse((_SRC / "_mineru_api_child.py").read_text(encoding="utf-8"))
    nexus_at = min(
        (n.lineno for n in ast.walk(tree) if isinstance(n, ast.Import) and any(a.name == "nexus" for a in n.names)),
        default=None,
    )
    assert nexus_at is not None, "the child never imports nexus, so the DLL-directory helper never runs in it"
    loads = [
        n.lineno
        for n in ast.walk(tree)
        if (isinstance(n, ast.ImportFrom) and (n.module or "").split(".")[0] in _HEAVY)
        or (isinstance(n, ast.Import) and any(a.name.split(".")[0] in _HEAVY for a in n.names))
        or (isinstance(n, ast.Call) and isinstance(n.func, ast.Attribute) and n.func.attr == "load")
    ]
    assert loads, "the child loads nothing from mineru; the order test has nothing to pin"
    assert all(nexus_at < at for at in loads), (nexus_at, loads)


def test_windows_server_argv_runs_the_child_module_under_this_interpreter(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The interpreter is the running one, in its console form, never a guess from where
    ``mineru-api.exe`` sits.

    A ``mineru-api.exe`` found on PATH can belong to a foreign install whose ``python.exe`` has no
    nexus, and a shim directory can hold a python of its own; either gives ``No module named nexus``
    and a silent fallback to the per-page worker. The decoy ``python.exe`` beside the script is what
    the old code picked. The daemon runs under ``pythonw``, which has no stdout, so the console form
    is used."""
    scripts = tmp_path / "Scripts"
    scripts.mkdir()
    (scripts / "python.exe").write_bytes(b"MZ")  # the decoy
    setattr_in(monkeypatch, "nexus._mineru_spawn", "sys.executable", r"C:\gen\Scripts\pythonw.exe")
    argv = _mineru_spawn.mineru_server_argv(str(scripts / "mineru-api.exe"), 8123, platform="win32")
    assert argv == [
        r"C:\gen\Scripts\python.exe", "-m", "nexus._mineru_api_child", "--host", "127.0.0.1", "--port", "8123",
    ]


def test_windows_server_argv_with_a_console_interpreter_is_that_interpreter(tmp_path: Path) -> None:
    argv = _mineru_spawn.mineru_server_argv(str(tmp_path / "mineru-api.exe"), 7, platform="win32")
    assert argv[:3] == [console_python(sys.executable), "-m", "nexus._mineru_api_child"]


def test_posix_server_argv_is_the_console_script_unchanged() -> None:
    assert _mineru_spawn.mineru_server_argv("/v/bin/mineru-api", 9, platform="linux") == [
        "/v/bin/mineru-api", "--host", "127.0.0.1", "--port", "9",
    ]


def test_spawn_server_process_uses_the_windows_argv(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """The one launch core (nx mineru start, the on-demand lifecycle, the crash restart)."""
    seen: list[list[str]] = []

    class _Proc:
        pid = 4242

    scripts = tmp_path / "Scripts"
    scripts.mkdir()
    (scripts / "python.exe").write_bytes(b"MZ")  # a decoy beside the console script
    monkeypatch.setattr(_mineru_spawn, "_resolve_mineru_api_bin", lambda: str(scripts / "mineru-api.exe"))
    setattr_in(monkeypatch, "nexus._vcrt", "sys.platform", "win32")
    setattr_in(monkeypatch, "nexus._mineru_spawn", "sys.executable", r"C:\gen\Scripts\pythonw.exe")
    setattr_in(
        monkeypatch, "nexus._mineru_spawn", "subprocess.Popen", lambda cmd, **kw: seen.append(list(cmd)) or _Proc(),
    )
    payloads: list[dict] = []
    monkeypatch.setattr(_mineru_spawn, "_write_pid_file", lambda _path, payload: payloads.append(payload))
    monkeypatch.setenv("NEXUS_CONFIG_DIR", str(tmp_path))
    assert _mineru_spawn.spawn_server_process(8123) is not None
    assert seen[0][1:3] == ["-m", "nexus._mineru_api_child"] and seen[0][-2:] == ["--port", "8123"]
    # The pid file names the interpreter the server was launched under (the stale-server sweep judges
    # it): the console form of the running one. It is neither the decoy beside the script nor the
    # pythonw that ran the spawner, so a regression to either choice fails here.
    assert seen[0][0] == r"C:\gen\Scripts\python.exe"
    assert payloads[0]["python"] == r"C:\gen\Scripts\python.exe"
    # and the shape it was launched in, which the image-only classification (below) relies on
    assert payloads[0]["server_module"] == "nexus._mineru_api_child"


def test_server_child_runs_mineru_apis_entry_with_the_servers_arguments(tmp_path: Path) -> None:
    """The child hands its command line to mineru-api's own entry point and returns its exit status.
    It does NOT prove nexus was imported first (the package import under ``-m`` makes that
    unobservable here); the source check above guards that."""
    marker = tmp_path / "marker"
    fake = _fake_mineru(
        tmp_path / "fake",
        "",
        {
            "fast_api.py": (
                f"import sys\nopen({str(marker)!r}, 'w').write(' '.join(sys.argv[1:]))\n"
                "def main():\n    pass\n"
            )
        },
    )
    proc = subprocess.run(
        [sys.executable, "-m", "nexus._mineru_api_child", "--host", "127.0.0.1", "--port", "5"],
        env=_env(fake), capture_output=True, text=True, timeout=60,
    )
    assert proc.returncode == 0, proc.stderr
    assert marker.read_text() == "--host 127.0.0.1 --port 5"


class _FakeEntryPoint:
    def __init__(self, target, name: str = "mineru-api", group: str = "console_scripts") -> None:  # noqa: ANN001
        self._target = target
        self.name = name
        self.group = group

    def load(self):  # noqa: ANN201
        return self._target


class _FakeDistribution:
    def __init__(self, *entry_points: _FakeEntryPoint) -> None:
        self.entry_points = list(entry_points)


def _fake_mineru_distribution(monkeypatch: pytest.MonkeyPatch, *entry_points: _FakeEntryPoint) -> list[str]:
    """Make ``importlib.metadata.distribution("mineru")`` answer with *entry_points*, and make the
    all-distributions ``entry_points()`` lookup fail the test: the launcher reads the mineru
    distribution only. Returns the names the launcher asked ``distribution`` for."""
    asked: list[str] = []

    def fake_distribution(name: str) -> _FakeDistribution:
        asked.append(name)
        return _FakeDistribution(*entry_points)

    def any_distribution(**kw):  # noqa: ANN003, ANN202
        raise AssertionError(f"the launcher searched every installed distribution: {kw}")

    setattr_in(monkeypatch, "nexus._mineru_console", "importlib.metadata.distribution", fake_distribution)
    setattr_in(monkeypatch, "nexus._mineru_console", "importlib.metadata.entry_points", any_distribution)
    return asked


def test_server_entry_is_the_console_scripts_entry_point(monkeypatch: pytest.MonkeyPatch) -> None:
    """``mineru-api`` is whatever the installed mineru's ``console_scripts`` says it is, so a mineru
    that moves ``main`` still starts; the module path is only the fallback."""
    def sentinel() -> None: ...

    def other() -> None: ...

    asked = _fake_mineru_distribution(
        monkeypatch, _FakeEntryPoint(other, "mineru-gradio"), _FakeEntryPoint(sentinel, "mineru-api"),
    )
    assert _mineru_api_child.server_entry() is sentinel
    assert asked == ["mineru"]


def test_server_entry_falls_back_to_the_known_module_path(monkeypatch: pytest.MonkeyPatch) -> None:
    def fallback_main() -> None: ...

    fake = types.ModuleType("mineru.cli.fast_api")
    fake.main = fallback_main  # type: ignore[attr-defined]
    monkeypatch.setitem(sys.modules, "mineru", types.ModuleType("mineru"))
    monkeypatch.setitem(sys.modules, "mineru.cli", types.ModuleType("mineru.cli"))
    monkeypatch.setitem(sys.modules, "mineru.cli.fast_api", fake)
    _fake_mineru_distribution(monkeypatch)
    assert _mineru_api_child.server_entry() is fallback_main


def test_server_entry_falls_back_when_the_mineru_distribution_is_absent(monkeypatch: pytest.MonkeyPatch) -> None:
    import importlib.metadata  # noqa: PLC0415

    def fallback_main() -> None: ...

    fake = types.ModuleType("mineru.cli.fast_api")
    fake.main = fallback_main  # type: ignore[attr-defined]
    monkeypatch.setitem(sys.modules, "mineru", types.ModuleType("mineru"))
    monkeypatch.setitem(sys.modules, "mineru.cli", types.ModuleType("mineru.cli"))
    monkeypatch.setitem(sys.modules, "mineru.cli.fast_api", fake)

    def missing(name: str) -> None:
        raise importlib.metadata.PackageNotFoundError(name)

    setattr_in(monkeypatch, "nexus._mineru_console", "importlib.metadata.distribution", missing)
    assert _mineru_api_child.server_entry() is fallback_main


def test_console_entry_ignores_a_script_another_distribution_declares(monkeypatch: pytest.MonkeyPatch) -> None:
    """Only the ``mineru`` distribution's scripts are launchable: a ``mineru-*`` script some other
    package declares (the all-distributions lookup) is never loaded."""
    from nexus import _mineru_console  # noqa: PLC0415

    _fake_mineru_distribution(monkeypatch, _FakeEntryPoint(lambda: None, "mineru-api"))
    assert _mineru_console.console_entry("mineru-evil") is None
    non_console = _FakeEntryPoint(lambda: None, "mineru-x", group="mineru.plugins")
    _fake_mineru_distribution(monkeypatch, non_console)
    assert _mineru_console.console_entry("mineru-x") is None


def _require_real_mineru(modname: str) -> None:
    """The dev group carries the PDF stack, so a missing mineru is a broken environment, not a reason
    to skip: where it is expected (``NX_MINERU_EXPECTED``, or CI) a missing import is an error. A
    local box without it skips, as the other real-mineru tests do (``tests/_mineru_rearm.py``)."""
    if os.environ.get(NX_MINERU_EXPECTED) or os.environ.get("CI"):
        importlib.import_module(modname)
        return
    mineru_importorskip(modname)


def test_the_real_mineru_server_entry_starts_under_dash_m(tmp_path: Path) -> None:
    """Against the REAL installed mineru, not a fake: ``python -m nexus._mineru_api_child --help``
    resolves mineru-api's entry point and answers. The fake-mineru tests above cannot see an entry
    point that moved, a changed click signature, or an import error in ``mineru.cli.fast_api``."""
    _require_real_mineru("mineru.cli.fast_api")
    proc = subprocess.run(
        [sys.executable, "-m", "nexus._mineru_api_child", "--help"],
        capture_output=True, text=True, timeout=180, cwd=tmp_path,
    )
    assert proc.returncode == 0, proc.stderr
    assert "--host" in proc.stdout and "--port" in proc.stdout, proc.stdout


def test_a_missing_mineru_is_an_error_where_it_is_expected_not_a_skip(monkeypatch: pytest.MonkeyPatch) -> None:
    """The non-vacuity of the real-mineru tests above: under CI (or ``NX_MINERU_EXPECTED``) an
    absent module raises instead of skipping."""
    monkeypatch.delenv(NX_MINERU_EXPECTED, raising=False)
    monkeypatch.setenv("CI", "true")
    with pytest.raises(ModuleNotFoundError):
        _require_real_mineru("mineru_that_is_not_installed_nexus_vyy65")
    monkeypatch.delenv("CI")
    monkeypatch.setenv(NX_MINERU_EXPECTED, "1")
    with pytest.raises(ModuleNotFoundError):
        _require_real_mineru("mineru_that_is_not_installed_nexus_vyy65")


# ── the worker's own render-pool children (grandchildren) ──────────────────

_NATIVE_STACK = ("torch", "onnxruntime", "magika", "cv2", "mineru_vl_utils")


def test_render_pool_grandchildren_load_none_of_the_native_stack() -> None:
    """Why the ``-c`` worker may leave its render-pool children without the DLL prologue.

    The worker is ``python -c``, so a ``spawn`` child it starts cannot re-import the worker's main
    module and never runs ``import nexus``. That is safe only because such a child imports just the
    module holding the task function, ``mineru.utils.pdf_image_tools``, and nothing under it loads
    torch, onnxruntime, magika, cv2 or mineru_vl_utils (the libraries that need the app-local DLL
    directories). If a mineru release makes that import reach one of them, the grandchildren die on a
    box without the VC++ redistributable and this fails first. ``-I``: no PYTHONPATH, no user site, so
    the answer is the installed mineru's."""
    _require_real_mineru("mineru.utils.pdf_image_tools")
    script = (
        "import sys\n"
        "import mineru.utils.pdf_image_tools\n"
        f"print(sorted(m for m in {_NATIVE_STACK!r} if m in sys.modules))\n"
    )
    proc = subprocess.run([sys.executable, "-I", "-c", script], capture_output=True, text=True, timeout=180)
    assert proc.returncode == 0, proc.stderr
    assert proc.stdout.strip() == "[]", proc.stdout


def test_the_child_module_is_still_recognised_as_a_mineru_server() -> None:
    """nx mineru stop / the pid-recycle guard / the stale-server sweep classify by argv; a python
    running the child module must still be a mineru-api."""
    for exe in ("/v/bin/python", r"C:\v\Scripts\python.exe"):
        cmd = f"{exe} -m nexus._mineru_api_child --host 127.0.0.1 --port 5"
        assert _classify(cmd) == "mineru", cmd
    assert _classify("/v/bin/python -m nexus.cli doctor") == "other"


def test_the_classifiers_module_name_is_the_spawners() -> None:
    assert _mineru_spawn.MINERU_SERVER_CHILD_MODULE == "nexus._mineru_api_child"
    assert f"-m {_mineru_spawn.MINERU_SERVER_CHILD_MODULE}" in " ".join(
        _mineru_spawn.mineru_server_argv("x", 1, platform="win32")
    )


# ── the image-only read of a server's command line ─────────────────────────
#
# On Windows ``process_command`` falls back to the process IMAGE alone when it cannot read the
# command line. The old server was ``mineru-api.exe``, so its image classified as mineru. The new
# one is ``python.exe -m nexus._mineru_api_child``, whose image is just ``python.exe``: "other", so
# ``nx mineru stop`` and the replacement path would drop the pid file and leave the server running.

_IMAGE = r"C:\gen\Scripts\python.exe"
_PID = 5150
_STARTED_AT = "2026-10-29T16:00:00+00:00"
_STARTED_UNIX = 1793289600.0  # _STARTED_AT as a Unix time


def _write_pid(tmp_path: Path, monkeypatch: pytest.MonkeyPatch, *, created: float | None = _STARTED_UNIX - 2.0,
               **over: object) -> None:
    info = {
        "pid": _PID, "port": 8123, "python": _IMAGE, "executable": r"C:\gen\Scripts\mineru-api.exe",
        "server_module": "nexus._mineru_api_child", "started_at": _STARTED_AT,
    }
    info.update(over)
    monkeypatch.setenv("NEXUS_CONFIG_DIR", str(tmp_path))
    (tmp_path / "mineru.pid").write_text(json.dumps(info), encoding="utf-8")
    # the server process was created just before the pid file stamped started_at
    monkeypatch.setattr(upgrade_finish, "_process_start_unix", lambda pid: created)


def test_started_at_constant_is_that_instant() -> None:
    assert upgrade_finish._recorded_start_unix(_STARTED_AT) == _STARTED_UNIX


def test_an_image_only_read_of_the_registered_server_is_a_mineru_server(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    _write_pid(tmp_path, monkeypatch)
    assert _classify(_IMAGE) == "other", "the bare image names no server; the pid file is what does"
    assert upgrade_finish._classify_pid(_PID, _IMAGE) == "mineru"
    monkeypatch.setattr("nexus.upgrade_finish.process_command", lambda pid: _IMAGE)
    assert _mineru_spawn.classify_server_pid(_PID) == "mineru"


def test_the_image_only_rule_is_as_narrow_as_the_pid_file(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    _write_pid(tmp_path, monkeypatch)
    # a pid the file does not name
    assert upgrade_finish._classify_pid(_PID + 1, _IMAGE) == "other"
    # another interpreter's image (the pid was recycled to a python of a different install)
    assert upgrade_finish._classify_pid(_PID, r"C:\other\python.exe") == "other"
    # an image that is not an interpreter at all
    assert upgrade_finish._classify_pid(_PID, r"C:\Windows\notepad.exe") == "other"
    # a readable command line is judged by its argv, never by the pid file
    assert upgrade_finish._classify_pid(_PID, rf"{_IMAGE} -m nexus.cli doctor") == "other"
    # a pid file written before this shape existed records no server module
    _write_pid(tmp_path, monkeypatch, server_module=None)
    assert upgrade_finish._classify_pid(_PID, _IMAGE) == "other"
    # no pid file at all
    (tmp_path / "mineru.pid").unlink()
    assert upgrade_finish._classify_pid(_PID, _IMAGE) == "other"


def test_a_reused_pid_running_the_same_interpreter_is_not_the_server(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A stale pid file whose pid now belongs to a bare run of the same python.exe (a REPL, an
    unreadable command line) matches pid, image and module; only the creation time tells it apart."""
    _write_pid(tmp_path, monkeypatch, created=_STARTED_UNIX + 3600.0)
    assert upgrade_finish._classify_pid(_PID, _IMAGE) == "other"
    _write_pid(tmp_path, monkeypatch, created=_STARTED_UNIX - 3600.0)
    assert upgrade_finish._classify_pid(_PID, _IMAGE) == "other"
    # an unreadable creation time is no match: never stop a process we cannot tie to the server
    _write_pid(tmp_path, monkeypatch, created=None)
    assert upgrade_finish._classify_pid(_PID, _IMAGE) == "other"
    # a pid file with no started_at (or a bad one) is no match either
    _write_pid(tmp_path, monkeypatch, started_at=None)
    assert upgrade_finish._classify_pid(_PID, _IMAGE) == "other"
    _write_pid(tmp_path, monkeypatch, started_at="not a time")
    assert upgrade_finish._classify_pid(_PID, _IMAGE) == "other"


def test_a_readable_command_line_is_classified_without_the_pid_file(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("NEXUS_CONFIG_DIR", str(tmp_path))
    assert upgrade_finish._classify_pid(1, "/v/bin/python -m nexus._mineru_api_child --port 5") == "mineru"
    assert upgrade_finish._classify_pid(1, "/v/bin/mineru-api --port 5") == "mineru"
    assert upgrade_finish._classify_pid(1, "") == "other"


# ── nx doctor --check-mineru probes in a fresh child ───────────────────────


@pytest.fixture()
def _stack_present(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(pdf_stack, "state", lambda: StackState("present", "test"))
    monkeypatch.setattr(doctor, "_mineru_pipeline_model_dir_configured", lambda: False)
    monkeypatch.setattr(doctor, "_probe_cv2", lambda timeout_s=None: (None, "9.9.9"))
    monkeypatch.setattr(doctor, "_MINERU_CHILD_PROBE_MODULES", ("mineru.cli.common",))


DLL_ERROR = "ImportError: DLL load failed while importing onnxruntime_pybind11_state: The specified module could not be found."


def test_the_child_probe_reports_the_childs_import_error(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(doctor, "_MINERU_CHILD_PROBE_MODULES", ("mineru.cli.common",))
    fake = _fake_mineru(tmp_path, f"raise ImportError({DLL_ERROR.split(': ', 1)[1]!r})\n")
    monkeypatch.setenv("PYTHONPATH", str(fake))
    error = doctor._probe_mineru_child_import()
    assert error is not None and "DLL load failed" in error


def test_the_child_probe_is_green_when_the_child_imports(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(doctor, "_MINERU_CHILD_PROBE_MODULES", ("mineru.cli.common",))
    monkeypatch.setenv("PYTHONPATH", str(_fake_mineru(tmp_path, "def do_parse(*a, **k):\n    pass\n")))
    assert doctor._probe_mineru_child_import() is None


def test_the_child_probe_never_imports_mineru_into_the_doctor_process(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(doctor, "_MINERU_CHILD_PROBE_MODULES", ("mineru.cli.common",))
    marker = tmp_path / "ran"
    monkeypatch.setenv(
        "PYTHONPATH",
        str(_fake_mineru(tmp_path / "f", f"open({str(marker)!r}, 'w').write('x')\ndef do_parse(*a, **k):\n    pass\n")),
    )
    monkeypatch.delitem(sys.modules, "mineru.cli.common", raising=False)
    assert doctor._probe_mineru_child_import() is None
    assert marker.exists(), "the probe did not run mineru's module body in a child"
    assert "mineru.cli.common" not in sys.modules


def test_the_child_probe_script_starts_like_the_worker() -> None:
    """One prologue, so the probe fails exactly where the worker would."""
    script = doctor._mineru_child_probe_script(("mineru.cli.common", "torch"))
    prologue = _mineru_spawn.MINERU_CHILD_PROLOGUE
    assert prologue in script and prologue in pdf_extractor._MINERU_WORKER_SCRIPT
    assert _nexus_import_offset(script) < _first_heavy_import_offset(script)


def test_a_hung_child_probe_is_an_error_not_a_hung_doctor(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(doctor, "_MINERU_CHILD_PROBE_MODULES", ("mineru.cli.common",))
    monkeypatch.setenv("PYTHONPATH", str(_fake_mineru(tmp_path, "import time\ntime.sleep(60)\n")))
    error = doctor._probe_mineru_child_import(timeout_s=1.0)
    assert error is not None and "did not finish" in error


def test_the_doctor_row_is_red_when_only_the_child_cannot_load(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str], _stack_present: None,
) -> None:
    """The in-process import is fine (this process ran the helper); the child's is not. The row that
    used to read green for that box now reads red with the child's error."""
    monkeypatch.setenv("PYTHONPATH", str(_fake_mineru(tmp_path, f"raise ImportError({DLL_ERROR.split(': ', 1)[1]!r})\n")))
    fake_common = types.ModuleType("mineru.cli.common")
    fake_common.do_parse = lambda *a, **k: None  # type: ignore[attr-defined]
    monkeypatch.setitem(sys.modules, "mineru", types.ModuleType("mineru"))
    monkeypatch.setitem(sys.modules, "mineru.cli", types.ModuleType("mineru.cli"))
    monkeypatch.setitem(sys.modules, "mineru.cli.common", fake_common)
    assert doctor._run_check_mineru() is True
    out = capsys.readouterr().out
    row = next(ln for ln in out.splitlines() if "child" in ln and "↳" not in ln)
    assert "✗" in row and "DLL load failed" in row, out
    assert any("MinerU import" in ln and "✓" in ln for ln in out.splitlines())


def test_the_doctor_row_is_green_when_the_child_loads(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str], _stack_present: None,
) -> None:
    monkeypatch.setenv("PYTHONPATH", str(_fake_mineru(tmp_path, "def do_parse(*a, **k):\n    pass\n")))
    fake_common = types.ModuleType("mineru.cli.common")
    fake_common.do_parse = lambda *a, **k: None  # type: ignore[attr-defined]
    monkeypatch.setitem(sys.modules, "mineru", types.ModuleType("mineru"))
    monkeypatch.setitem(sys.modules, "mineru.cli", types.ModuleType("mineru.cli"))
    monkeypatch.setitem(sys.modules, "mineru.cli.common", fake_common)
    assert doctor._run_check_mineru() is False
    out = capsys.readouterr().out
    row = next(ln for ln in out.splitlines() if "child" in ln and "↳" not in ln)
    assert "✓" in row and "✗" not in out


def test_the_default_child_probe_covers_the_server_entry_module() -> None:
    """The server runs ``mineru.cli.fast_api``; a probe that stops at ``mineru.cli.common`` cannot see
    that module missing or unloadable."""
    assert "mineru.cli.fast_api" in doctor._MINERU_CHILD_PROBE_MODULES
    assert "mineru.cli.fast_api" in doctor._mineru_child_probe_script(doctor._MINERU_CHILD_PROBE_MODULES)


def test_the_real_child_probe_is_green_on_a_healthy_install() -> None:
    """The default module list against the real installed mineru: nothing in it is unimportable."""
    _require_real_mineru("mineru.cli.fast_api")
    assert doctor._probe_mineru_child_import() is None


def _child_row_remedy(monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str], *, windows: bool, error: str) -> str:
    """The text under the red child row, with the in-process import green and the child's error given."""
    fake_common = types.ModuleType("mineru.cli.common")
    fake_common.do_parse = lambda *a, **k: None  # type: ignore[attr-defined]
    monkeypatch.setitem(sys.modules, "mineru", types.ModuleType("mineru"))
    monkeypatch.setitem(sys.modules, "mineru.cli", types.ModuleType("mineru.cli"))
    monkeypatch.setitem(sys.modules, "mineru.cli.common", fake_common)
    monkeypatch.setattr(doctor, "_probe_mineru_child_import", lambda timeout_s=None: error)
    if windows:
        setattr_in(monkeypatch, "nexus._vcrt", "sys.platform", "win32")
    assert doctor._run_check_mineru() is True
    out = capsys.readouterr().out
    lines = out.splitlines()
    at = next(i for i, ln in enumerate(lines) if "child" in ln and "✗" in ln)
    return "\n".join(lines[at + 1 :])


def test_a_child_only_dll_failure_on_windows_names_the_remedy_that_places_the_runtime(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str], _stack_present: None,
) -> None:
    """The in-process import is green, so the install is not broken: the child cannot find nx's
    app-local VC++ DLLs. Reinstalling does not place them; ``nx init`` (and ``nx upgrade --auto``)
    does, as does Microsoft's redistributable."""
    text = _child_row_remedy(monkeypatch, capsys, windows=True, error=DLL_ERROR)
    assert "nx init" in text and "nx upgrade --auto" in text and "vc_redist.x64.exe" in text, text
    assert "Reinstall" not in text and "--reinstall" not in text, text


def test_a_child_failure_that_is_not_a_dll_error_keeps_the_reinstall_remedy(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str], _stack_present: None,
) -> None:
    text = _child_row_remedy(monkeypatch, capsys, windows=True, error="ModuleNotFoundError: No module named 'torch'")
    assert "Reinstall" in text and "nx init" not in text, text


def test_a_child_failure_off_windows_keeps_the_reinstall_remedy(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str], _stack_present: None,
) -> None:
    text = _child_row_remedy(monkeypatch, capsys, windows=False, error="ImportError: libGL.so.1: cannot open")
    assert "Reinstall" in text and "nx init" not in text, text


# ── round 2: the in-process import row, and the other mineru console scripts ──


def _fake_failing_mineru(tmp_path: Path, monkeypatch: pytest.MonkeyPatch, message: str) -> None:
    """A ``mineru`` whose ``mineru.cli.common`` raises *message* on import, in THIS process."""
    root = _fake_mineru(tmp_path, f"raise ImportError({message!r})\n")
    monkeypatch.syspath_prepend(str(root))
    for name in ("mineru", "mineru.cli", "mineru.cli.common"):
        monkeypatch.delitem(sys.modules, name, raising=False)


def _in_process_import_remedy(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str], *, windows: bool, message: str,
) -> str:
    _fake_failing_mineru(tmp_path, monkeypatch, message)
    if windows:
        setattr_in(monkeypatch, "nexus._vcrt", "sys.platform", "win32")
    assert doctor._run_check_mineru() is True
    return capsys.readouterr().out


def test_an_in_process_dll_failure_on_windows_names_the_runtime_remedy(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str], _stack_present: None,
) -> None:
    """On a bare Windows box the app-local DLLs are missing, so the doctor's OWN import fails with
    ``DLL load failed``. Reinstalling places nothing; ``nx init`` (or ``nx upgrade --auto``) does."""
    out = _in_process_import_remedy(
        tmp_path, monkeypatch, capsys, windows=True,
        message="DLL load failed while importing onnxruntime_pybind11_state: The specified module could not be found.",
    )
    assert "MinerU import" in out and "✗" in out, out
    assert "nx init" in out and "nx upgrade --auto" in out and "vc_redist.x64.exe" in out, out
    assert "Reinstall" not in out and "--reinstall" not in out, out


def test_an_in_process_failure_that_is_not_a_dll_error_keeps_the_reinstall_text(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str], _stack_present: None,
) -> None:
    out = _in_process_import_remedy(
        tmp_path, monkeypatch, capsys, windows=True, message="No module named 'torch'",
    )
    assert "Reinstall" in out and "nx init" not in out, out


def test_an_in_process_dll_failure_off_windows_keeps_the_reinstall_text(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str], _stack_present: None,
) -> None:
    out = _in_process_import_remedy(
        tmp_path, monkeypatch, capsys, windows=False, message="DLL load failed while importing x",
    )
    assert "Reinstall" in out and "nx init" not in out, out


def test_console_module_imports_nexus_before_anything_that_loads_mineru() -> None:
    """The same guard as for the server child, for the generic launcher: ``python -m nexus._mineru_console``
    and any other launch form must have the DLL directories set before a console script's entry loads."""
    tree = ast.parse((_SRC / "_mineru_console.py").read_text(encoding="utf-8"))
    nexus_at = min(
        (n.lineno for n in ast.walk(tree) if isinstance(n, ast.Import) and any(a.name == "nexus" for a in n.names)),
        default=None,
    )
    assert nexus_at is not None, "the launcher never imports nexus, so the DLL-directory helper never runs in it"
    loads = [
        n.lineno
        for n in ast.walk(tree)
        if (isinstance(n, ast.ImportFrom) and (n.module or "").split(".")[0] in _HEAVY)
        or (isinstance(n, ast.Import) and any(a.name.split(".")[0] in _HEAVY for a in n.names))
        or (isinstance(n, ast.Call) and isinstance(n.func, ast.Attribute) and n.func.attr == "load")
    ]
    assert loads, "the launcher loads nothing from mineru; the order test has nothing to pin"
    assert all(nexus_at < at for at in loads), (nexus_at, loads)


def test_console_main_runs_the_named_scripts_entry_with_its_arguments(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    """``sys.argv`` is ``[script name, *arguments]``, as a console-script stub leaves it, and the exit
    status is the entry's. (click's usage line does not come from argv[0] under ``python -m``: it
    reads ``python -m nexus.<script>``, which is cosmetic.)"""
    from nexus import _mineru_console  # noqa: PLC0415

    monkeypatch.setattr(sys, "argv", list(sys.argv))  # main() rewrites it; restore it after the test
    seen: list[list[str]] = []

    def entry() -> int:
        seen.append(list(sys.argv))
        return 3

    _fake_mineru_distribution(
        monkeypatch, _FakeEntryPoint(lambda: 0, "mineru-api"), _FakeEntryPoint(entry, "mineru-models-download"),
    )
    with pytest.raises(SystemExit) as exc:
        _mineru_console.main(["mineru-models-download", "--model_type", "pipeline"])
    assert exc.value.code == 3
    assert seen == [["mineru-models-download", "--model_type", "pipeline"]]


def test_console_main_refuses_an_unknown_script(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str],
) -> None:
    from nexus import _mineru_console  # noqa: PLC0415

    _fake_mineru_distribution(monkeypatch)
    for argv in (["mineru-nonesuch"], []):
        with pytest.raises(SystemExit) as exc:
            _mineru_console.main(argv)
        assert exc.value.code == 2, argv
    assert "mineru-nonesuch" in capsys.readouterr().err


def test_console_main_name_guard_refuses_a_script_that_is_not_a_mineru_one(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str],
) -> None:
    """The guard has its own work to do: with an entry named ``pip`` available, ``main(["pip"])`` still
    exits 2 and never calls it. Delete the guard and the entry runs (and this fails)."""
    from nexus import _mineru_console  # noqa: PLC0415

    called: list[str] = []
    _fake_mineru_distribution(monkeypatch, _FakeEntryPoint(lambda: called.append("pip"), "pip"))
    with pytest.raises(SystemExit) as exc:
        _mineru_console.main(["pip"])
    assert exc.value.code == 2
    assert called == []
    assert "usage" in capsys.readouterr().err


def test_the_real_mineru_models_download_runs_under_the_console_module(tmp_path: Path) -> None:
    """Against the REAL installed mineru: ``python -m nexus._mineru_console mineru-models-download --help``
    resolves the console script's entry point (via importlib.metadata) and answers, so the command the
    doctor prints works on a box where the console script itself cannot load torch."""
    _require_real_mineru("mineru.cli.models_download")
    proc = subprocess.run(
        [sys.executable, "-m", "nexus._mineru_console", "mineru-models-download", "--help"],
        capture_output=True, text=True, timeout=180, cwd=tmp_path,
    )
    assert proc.returncode == 0, proc.stderr
    assert "--model_type" in proc.stdout or "Usage" in proc.stdout, proc.stdout


def test_the_console_command_off_windows_is_the_plain_interpreter_in_posix_quoting(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Off Windows the path is ``sys.executable`` as it is: not a backslashed Windows path, and quoted
    for a POSIX shell when it holds a space."""
    import shlex  # noqa: PLC0415

    setattr_in(monkeypatch, "nexus._mineru_spawn", "sys.platform", "linux")
    setattr_in(monkeypatch, "nexus._mineru_spawn", "sys.executable", "/opt/my venv/bin/python")
    cmd = _mineru_spawn.mineru_console_command("mineru-models-download", "-s", "huggingface")
    assert cmd == shlex.join(
        ["/opt/my venv/bin/python", "-m", "nexus._mineru_console", "mineru-models-download", "-s", "huggingface"]
    ), cmd
    assert "\\" not in cmd, cmd


@pytest.mark.skipif(sys.platform == "win32", reason="asserts the unpatched POSIX interpreter path")
def test_the_console_command_off_windows_runs_this_very_interpreter() -> None:
    import shlex  # noqa: PLC0415

    cmd = _mineru_spawn.mineru_console_command("mineru-models-download")
    assert shlex.split(cmd) == [sys.executable, "-m", "nexus._mineru_console", "mineru-models-download"], cmd


def test_the_console_command_on_windows_is_a_powershell_call_with_a_spaced_path(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """PowerShell runs a quoted path only behind the call operator, and ``list2cmdline`` quoting
    (``"C:\\a b\\python.exe" -m ...``) is a string expression there, not a command. The console
    interpreter is used: ``pythonw.exe`` has no console to print to."""
    setattr_in(monkeypatch, "nexus._mineru_spawn", "sys.platform", "win32")
    setattr_in(monkeypatch, "nexus._mineru_spawn", "sys.executable", r"C:\Program Files\Python 3\pythonw.exe")
    cmd = _mineru_spawn.mineru_console_command("mineru-models-download", "-s", "huggingface", "-m", "pipeline")
    assert cmd == (
        r"& 'C:\Program Files\Python 3\python.exe' -m nexus._mineru_console "
        "mineru-models-download -s huggingface -m pipeline"
    ), cmd


def test_the_console_command_on_windows_escapes_a_quote_in_the_path(monkeypatch: pytest.MonkeyPatch) -> None:
    setattr_in(monkeypatch, "nexus._mineru_spawn", "sys.platform", "win32")
    setattr_in(monkeypatch, "nexus._mineru_spawn", "sys.executable", r"C:\Users\O'Neil\py\python.exe")
    cmd = _mineru_spawn.mineru_console_command("mineru-models-download")
    assert cmd.startswith(r"& 'C:\Users\O''Neil\py\python.exe' -m "), cmd


def test_the_console_command_on_windows_escapes_a_typographic_quote_in_the_path(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """PowerShell also ends a single-quoted string at U+2018..U+201B (a user folder like D’Arcy)."""
    setattr_in(monkeypatch, "nexus._mineru_spawn", "sys.platform", "win32")
    setattr_in(monkeypatch, "nexus._mineru_spawn", "sys.executable", "C:\\Users\\D\u2019Arcy\\py\\python.exe")
    cmd = _mineru_spawn.mineru_console_command("mineru-models-download")
    assert cmd.startswith("& 'C:\\Users\\D\u2019\u2019Arcy\\py\\python.exe' -m "), cmd


def _row_skip_text(monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]) -> str:
    fake_common = types.ModuleType("mineru.cli.common")
    fake_common.do_parse = lambda *a, **k: None  # type: ignore[attr-defined]
    monkeypatch.setitem(sys.modules, "mineru", types.ModuleType("mineru"))
    monkeypatch.setitem(sys.modules, "mineru.cli", types.ModuleType("mineru.cli"))
    monkeypatch.setitem(sys.modules, "mineru.cli.common", fake_common)
    monkeypatch.setattr(doctor, "_probe_mineru_child_import", lambda timeout_s=None: None)
    assert doctor._run_check_mineru() is False
    out = capsys.readouterr().out
    assert "weights not found" in out, out
    return out


def _skip_line(out: str) -> str:
    lines = [ln for ln in out.splitlines() if "weights not found" in ln]
    assert len(lines) == 1, out
    return lines[0]


@pytest.mark.parametrize("platform", ["win32", "linux"])
def test_the_real_parse_skip_line_prints_a_noninteractive_command_that_works_on_every_platform(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str], _stack_present: None, platform: str,
) -> None:
    """The ``mineru-models-download`` console script never imports nexus, so on bare Windows it dies
    with WinError 126 on torch's c10.dll; the skip line carries the ``-m nexus._mineru_console`` form.
    It names the source and the model type, so the command never stops at mineru's prompts and fetches
    only the pipeline weights nx parses with."""
    monkeypatch.delenv("MINERU_MODEL_SOURCE", raising=False)
    setattr_in(monkeypatch, ["nexus._vcrt", "nexus._mineru_spawn"], "sys.platform", platform)
    line = _skip_line(_row_skip_text(monkeypatch, capsys))
    assert "-m nexus._mineru_console mineru-models-download -s huggingface -m pipeline" in line, line
    if platform == "win32":
        assert "PowerShell" in line and "& '" in line, line
    assert "MINERU_MODEL_SOURCE" not in line, line


def test_the_real_parse_skip_line_is_short(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str], _stack_present: None,
) -> None:
    monkeypatch.delenv("MINERU_MODEL_SOURCE", raising=False)
    line = _skip_line(_row_skip_text(monkeypatch, capsys))
    assert len(line.split()) <= 35, (len(line.split()), line)


def test_the_skip_line_downloads_from_modelscope_and_says_that_source_is_not_searched(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str], _stack_present: None,
) -> None:
    monkeypatch.setenv("MINERU_MODEL_SOURCE", "modelscope")
    line = _skip_line(_row_skip_text(monkeypatch, capsys))
    assert "mineru-models-download -s modelscope -m pipeline" in line, line
    assert "MINERU_MODEL_SOURCE=modelscope" in line and "not searched" in line, line
    assert len(line.split()) <= 50, (len(line.split()), line)


def test_the_skip_line_asks_for_huggingface_when_the_source_is_one_the_download_does_not_take(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str], _stack_present: None,
) -> None:
    monkeypatch.setenv("MINERU_MODEL_SOURCE", "local")
    line = _skip_line(_row_skip_text(monkeypatch, capsys))
    assert "mineru-models-download -s huggingface -m pipeline" in line, line
    assert "MINERU_MODEL_SOURCE=local" in line, line


def test_the_download_command_options_are_the_ones_mineru_declares() -> None:
    """The printed ``-s`` / ``-m`` options and the sources the doctor accepts are mineru's own: a
    mineru that renames an option or a source fails here, not on a user's box."""
    _require_real_mineru("mineru.cli.models_download")
    from mineru.cli import models_download  # noqa: PLC0415

    options = {opt: p for p in models_download.download_models.params for opt in p.opts}
    assert "-s" in options and "-m" in options, sorted(options)
    assert tuple(options["-s"].type.choices) == tuple(doctor._MINERU_DOWNLOAD_SOURCES)
    assert "pipeline" in options["-m"].type.choices
    assert options["-s"].name == "model_source" and options["-m"].name == "model_type"


# ── the pipeline weights, with no download and no guessed cache path ────────

#: What ``opendatalab/PDF-Extract-Kit-1.0`` ships under the pipeline paths (a listing of the repo taken
#: against mineru 3.1.11), plus files the English pipeline does not load (another language's recognizer,
#: a README). Literal on purpose: the cache the probe is tested against is not built from the probe's
#: own table.
_HF_PIPELINE_FILES = (
    "models/Layout/PP-DocLayoutV2/config.json",
    "models/Layout/PP-DocLayoutV2/model.safetensors",
    "models/Layout/PP-DocLayoutV2/preprocessor_config.json",
    "models/MFR/pp_formulanet_plus_m/PP-FormulaNet_plus-M.pth",
    "models/MFR/pp_formulanet_plus_m/PP-FormulaNet_plus-M_inference.yml",
    "models/MFR/unimernet_hf_small_2503/README.md",
    "models/MFR/unimernet_hf_small_2503/config.json",
    "models/MFR/unimernet_hf_small_2503/generation_config.json",
    "models/MFR/unimernet_hf_small_2503/model.safetensors",
    "models/MFR/unimernet_hf_small_2503/special_tokens_map.json",
    "models/MFR/unimernet_hf_small_2503/tokenizer.json",
    "models/MFR/unimernet_hf_small_2503/tokenizer_config.json",
    "models/OCR/paddleocr_torch/ch_PP-OCRv5_det_infer.pth",
    "models/OCR/paddleocr_torch/ch_PP-OCRv5_rec_infer.pth",
    "models/OCR/paddleocr_torch/en_PP-OCRv5_rec_infer.pth",
    "models/OCR/paddleocr_torch/latin_PP-OCRv5_rec_infer.pth",
    "models/OriCls/paddle_orientation_classification/PP-LCNet_x1_0_doc_ori.onnx",
    "models/TabCls/paddle_table_cls/PP-LCNet_x1_0_table_cls.onnx",
    "models/TabRec/SlanetPlus/slanet-plus.onnx",
    "models/TabRec/UnetStructure/unet.onnx",
)

#: The subset the English pipeline loads with the default formula model (``unimernet_small``).
_REQUIRED_DEFAULT = (
    "models/Layout/PP-DocLayoutV2/config.json",
    "models/Layout/PP-DocLayoutV2/model.safetensors",
    "models/MFR/unimernet_hf_small_2503/config.json",
    "models/MFR/unimernet_hf_small_2503/model.safetensors",
    "models/MFR/unimernet_hf_small_2503/tokenizer.json",
    "models/MFR/unimernet_hf_small_2503/tokenizer_config.json",
    "models/OCR/paddleocr_torch/ch_PP-OCRv5_det_infer.pth",
    "models/OCR/paddleocr_torch/en_PP-OCRv5_rec_infer.pth",
    "models/OCR/paddleocr_torch/ch_PP-OCRv5_rec_infer.pth",
    "models/OriCls/paddle_orientation_classification/PP-LCNet_x1_0_doc_ori.onnx",
    "models/TabCls/paddle_table_cls/PP-LCNet_x1_0_table_cls.onnx",
    "models/TabRec/SlanetPlus/slanet-plus.onnx",
    "models/TabRec/UnetStructure/unet.onnx",
)

_UNIMERNET = "models/MFR/unimernet_hf_small_2503"
_FORMULANET = "models/MFR/pp_formulanet_plus_m"


def _hf_cache_with_weights(
    root: Path, *, without: tuple[str, ...] = (), empty: tuple[str, ...] = (), snapshot: bool = True,
) -> Path:
    """A Hugging Face cache laid out the way ``huggingface_hub`` lays it out (``snapshots/<rev>/...``),
    holding real files. *without* drops files (an exact path, or every file under a directory path);
    *empty* writes those files with no content."""
    repo = root / "models--opendatalab--PDF-Extract-Kit-1.0"
    (repo / "refs").mkdir(parents=True)
    (repo / "refs" / "main").write_text("a" * 40)
    if snapshot:
        snap = repo / "snapshots" / ("a" * 40)
        snap.mkdir(parents=True)
        for rel in _HF_PIPELINE_FILES:
            if any(rel == w or rel.startswith(w + "/") for w in without):
                continue
            target = snap / rel
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_bytes(b"" if rel in empty else b"weights")
    return root


@pytest.fixture()
def _no_mineru_json(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """The box MinerU's own first-use download leaves: weights in the HF cache, no mineru.json."""
    _require_real_mineru("mineru.utils.config_reader")
    home = tmp_path / "home"
    home.mkdir()
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.setenv("USERPROFILE", str(home))
    monkeypatch.delenv("MINERU_MODEL_SOURCE", raising=False)
    monkeypatch.delenv("MINERU_FORMULA_CH_SUPPORT", raising=False)
    monkeypatch.setenv("MINERU_TOOLS_CONFIG_JSON", "mineru.json")
    # config_reader binds the name at import: a box exporting an absolute MINERU_TOOLS_CONFIG_JSON
    # would still read its real config without this
    from mineru.utils import config_reader  # noqa: PLC0415

    monkeypatch.setattr(config_reader, "CONFIG_FILE_NAME", "mineru.json")


def _use_hf_cache(monkeypatch: pytest.MonkeyPatch, root: Path) -> None:
    import huggingface_hub.constants as hf_constants  # noqa: PLC0415

    monkeypatch.setattr(hf_constants, "HF_HUB_CACHE", str(root))


def test_weights_in_the_hf_cache_count_as_provisioned_without_a_mineru_json(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, _no_mineru_json: None,
) -> None:
    """What ``nx index pdf``'s first-use download leaves: no mineru.json, weights in the HF cache. The
    real parse runs there; a skip would hide the GH #1533 class on every box that has indexed a PDF."""
    _use_hf_cache(monkeypatch, _hf_cache_with_weights(tmp_path / "hub"))
    assert doctor._mineru_pipeline_model_dir_configured() is True


def test_the_default_required_set_is_what_the_fixture_marks_required() -> None:
    """Non-vacuity of the removal tests below: every name they remove is a file the cache holds."""
    assert set(_REQUIRED_DEFAULT) <= set(_HF_PIPELINE_FILES)
    assert len(_REQUIRED_DEFAULT) == 13


@pytest.mark.parametrize("missing", _REQUIRED_DEFAULT)
def test_a_cache_missing_any_one_required_file_is_not_provisioned(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, _no_mineru_json: None, missing: str,
) -> None:
    """An interrupted first-use download leaves some files of a directory and not others (the
    directories are downloaded file by file), so each file the parse loads is required by itself."""
    _use_hf_cache(monkeypatch, _hf_cache_with_weights(tmp_path / "hub", without=(missing,)))
    assert doctor._mineru_pipeline_model_dir_configured() is False, missing


@pytest.mark.parametrize(
    "directory",
    ["models/Layout/PP-DocLayoutV2", _UNIMERNET, "models/OCR/paddleocr_torch"],
)
def test_a_directory_that_is_present_and_empty_is_not_provisioned(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, _no_mineru_json: None, directory: str,
) -> None:
    root = _hf_cache_with_weights(tmp_path / "hub", without=(directory,))
    (next((root / "models--opendatalab--PDF-Extract-Kit-1.0" / "snapshots").iterdir()) / directory).mkdir(parents=True)
    _use_hf_cache(monkeypatch, root)
    assert doctor._mineru_pipeline_model_dir_configured() is False


def test_an_ocr_dir_without_the_english_recognizer_is_not_provisioned(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, _no_mineru_json: None,
) -> None:
    """``models/OCR/paddleocr_torch`` holds only the languages fetched; nx and the doctor parse with
    ``en``. A directory with the Chinese and Latin recognizers is not enough."""
    _use_hf_cache(
        monkeypatch,
        _hf_cache_with_weights(tmp_path / "hub", without=("models/OCR/paddleocr_torch/en_PP-OCRv5_rec_infer.pth",)),
    )
    assert doctor._mineru_pipeline_model_dir_configured() is False


def test_a_zero_byte_weight_is_not_provisioned(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, _no_mineru_json: None,
) -> None:
    _use_hf_cache(
        monkeypatch,
        _hf_cache_with_weights(tmp_path / "hub", empty=("models/TabRec/UnetStructure/unet.onnx",)),
    )
    assert doctor._mineru_pipeline_model_dir_configured() is False


def test_files_the_english_pipeline_does_not_load_are_not_required(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, _no_mineru_json: None,
) -> None:
    """Not over-strict: a language the pipeline does not load (latin), a README and the layout
    preprocessor config (mineru falls back to {} without it) may be absent."""
    _use_hf_cache(
        monkeypatch,
        _hf_cache_with_weights(
            tmp_path / "hub",
            without=(
                "models/OCR/paddleocr_torch/latin_PP-OCRv5_rec_infer.pth",
                f"{_UNIMERNET}/README.md",
                f"{_UNIMERNET}/generation_config.json",
                "models/Layout/PP-DocLayoutV2/preprocessor_config.json",
            ),
        ),
    )
    assert doctor._mineru_pipeline_model_dir_configured() is True


@pytest.mark.parametrize("value", ["true", "TRUE", "1", "yes", "Yes"])
def test_formula_ch_support_on_requires_the_formulanet_model_not_unimernet(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, _no_mineru_json: None, value: str,
) -> None:
    monkeypatch.setenv("MINERU_FORMULA_CH_SUPPORT", value)
    _use_hf_cache(monkeypatch, _hf_cache_with_weights(tmp_path / "no_formulanet", without=(_FORMULANET,)))
    assert doctor._mineru_pipeline_model_dir_configured() is False
    _use_hf_cache(monkeypatch, _hf_cache_with_weights(tmp_path / "no_unimernet", without=(_UNIMERNET,)))
    assert doctor._mineru_pipeline_model_dir_configured() is True
    _use_hf_cache(
        monkeypatch,
        _hf_cache_with_weights(tmp_path / "half_formulanet", without=(f"{_FORMULANET}/PP-FormulaNet_plus-M_inference.yml",)),
    )
    assert doctor._mineru_pipeline_model_dir_configured() is False


@pytest.mark.parametrize("value", [None, "false", "FALSE", "0", "no", "", "maybe"])
def test_formula_ch_support_off_unset_or_invalid_requires_unimernet_not_formulanet(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, _no_mineru_json: None, value: str | None,
) -> None:
    """mineru reads an invalid value as ``False`` (model_init.py)."""
    if value is not None:
        monkeypatch.setenv("MINERU_FORMULA_CH_SUPPORT", value)
    _use_hf_cache(monkeypatch, _hf_cache_with_weights(tmp_path / "no_unimernet", without=(_UNIMERNET,)))
    assert doctor._mineru_pipeline_model_dir_configured() is False
    _use_hf_cache(monkeypatch, _hf_cache_with_weights(tmp_path / "no_formulanet", without=(_FORMULANET,)))
    assert doctor._mineru_pipeline_model_dir_configured() is True


def test_a_partial_or_absent_hf_cache_is_not_provisioned(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, _no_mineru_json: None,
) -> None:
    _use_hf_cache(monkeypatch, _hf_cache_with_weights(tmp_path / "partial", without=("models/Layout/PP-DocLayoutV2",)))
    assert doctor._mineru_pipeline_model_dir_configured() is False
    _use_hf_cache(monkeypatch, _hf_cache_with_weights(tmp_path / "nosnap", snapshot=False))
    assert doctor._mineru_pipeline_model_dir_configured() is False
    (tmp_path / "empty").mkdir()
    _use_hf_cache(monkeypatch, tmp_path / "empty")
    assert doctor._mineru_pipeline_model_dir_configured() is False


def test_the_weights_probe_never_goes_to_the_network(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, _no_mineru_json: None,
) -> None:
    """Checking for weights must not download or even look them up: ``local_files_only`` is passed on
    every call (a missing cache is the answer ``False``, not a fetch)."""
    import huggingface_hub  # noqa: PLC0415

    _use_hf_cache(monkeypatch, _hf_cache_with_weights(tmp_path / "hub"))
    real = huggingface_hub.snapshot_download
    calls: list[dict] = []

    def spy(*a, **kw):  # noqa: ANN002, ANN003, ANN202
        calls.append(kw)
        return real(*a, **kw)

    monkeypatch.setattr(huggingface_hub, "snapshot_download", spy)
    assert doctor._mineru_pipeline_model_dir_configured() is True
    assert calls and all(kw.get("local_files_only") is True for kw in calls), calls


def test_another_model_source_is_not_probed_for_cached_weights(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, _no_mineru_json: None,
) -> None:
    """A cache is located the way mineru locates it for ``huggingface`` only; ``modelscope`` is not guessed at."""
    _use_hf_cache(monkeypatch, _hf_cache_with_weights(tmp_path / "hub"))
    monkeypatch.setenv("MINERU_MODEL_SOURCE", "modelscope")
    assert doctor._mineru_pipeline_model_dir_configured() is False


# ── the probe's file list against mineru's own (a mineru bump that moves either side fails here) ──


def _mineru_package_dir() -> Path:
    _require_real_mineru("mineru.utils.enum_class")
    import importlib.util  # noqa: PLC0415

    spec = importlib.util.find_spec("mineru")
    assert spec is not None and spec.origin is not None
    return Path(spec.origin).parent


def test_the_probe_requires_a_path_for_every_model_download_pipeline_models_fetches(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Runs mineru's ``download_pipeline_models`` with its downloader stubbed, and takes the paths it
    asks for. The probe's table covers exactly those (both formula models: which one loads depends on
    ``MINERU_FORMULA_CH_SUPPORT``), so a model mineru adds to, or drops from, the download fails here."""
    _require_real_mineru("mineru.cli.models_download")
    from mineru.cli import models_download  # noqa: PLC0415
    from mineru.utils.enum_class import ModelPath  # noqa: PLC0415

    fetched: list[tuple[str, str]] = []

    def fake_download(relative_path: str, repo_mode: str = "pipeline") -> str:
        fetched.append((relative_path, repo_mode))
        return "/nowhere"

    monkeypatch.setattr(models_download, "auto_download_and_get_model_root_path", fake_download)
    monkeypatch.setattr(models_download, "configure_model", lambda *a, **k: None)
    models_download.download_pipeline_models()

    assert fetched, "download_pipeline_models fetched nothing: the comparison below would be vacuous"
    assert {mode for _, mode in fetched} == {"pipeline"}
    probed = {getattr(ModelPath, name) for name in doctor._MINERU_PIPELINE_FILES}
    assert {path for path, _ in fetched} == probed, (sorted(p for p, _ in fetched), sorted(probed))


def test_the_probe_names_the_english_ocr_files_mineru_loads_for_en() -> None:
    """``models_config.yml`` (read by ``PytorchPaddleOCR`` for the language) names the detector and
    recognizer for ``en``; the probe requires those and nothing from another language. nx parses with
    ``en`` (``p_lang_list=["en"]``), asserted here from the source of both parse sites."""
    import yaml  # noqa: PLC0415

    config = yaml.safe_load(
        (_mineru_package_dir() / "model/utils/pytorchocr/utils/resources/models_config.yml").read_text("utf-8")
    )
    en = config["lang"]["en"]
    # the orientation classifier's own OCR (tables on) is built with lang="ch_lite", whatever the
    # parse language: its recognizer is loaded too (nexus-vyy65 confirmation round)
    model_init = (_mineru_package_dir() / "backend/pipeline/model_init.py").read_text("utf-8")
    orientation = model_init[model_init.index("def img_orientation_cls_model_init"):]
    orientation = orientation[: orientation.index("\ndef ", 1)]
    assert 'lang="ch_lite"' in orientation, "the orientation classifier no longer builds a ch_lite OCR"
    ch_lite = config["lang"]["ch_lite"]
    assert set(doctor._MINERU_PIPELINE_FILES["pytorch_paddle"]) == {en["det"], en["rec"], ch_lite["det"], ch_lite["rec"]}
    assert 'p_lang_list=["en"]' in (_SRC / "pdf_extractor.py").read_text("utf-8")
    assert 'p_lang_list=["en"]' in (_SRC / "commands" / "doctor.py").read_text("utf-8")


def test_the_probe_reads_the_formula_switch_the_way_mineru_does() -> None:
    """``MFR_MODEL`` in ``backend/pipeline/model_init.py``: ``true/1/yes`` selects ``pp_formulanet_plus_m``,
    ``false/0/no`` and anything invalid select ``unimernet_small`` (read at import)."""
    src = (_mineru_package_dir() / "backend/pipeline/model_init.py").read_text("utf-8")
    on = re.search(r'MFR_MODEL\.lower\(\) in (\[[^\]]*\]):\s*\n\s*MFR_MODEL = "pp_formulanet_plus_m"', src)
    off = re.search(r'MFR_MODEL\.lower\(\) in (\[[^\]]*\]):\s*\n\s*MFR_MODEL = "unimernet_small"', src)
    assert on and off, "model_init no longer reads MINERU_FORMULA_CH_SUPPORT this way"
    assert 'os.getenv(\'MINERU_FORMULA_CH_SUPPORT\', \'False\')' in src
    assert tuple(ast.literal_eval(on.group(1))) == doctor._MINERU_FORMULA_CH_ON
    assert tuple(ast.literal_eval(off.group(1))) == doctor._MINERU_FORMULA_CH_OFF


def test_every_probed_path_is_a_file_mineru_reads_by_that_name() -> None:
    """The per-directory file lists come from the loaders (``from_pretrained`` reads config.json and
    the weights and the tokenizer files; the FormulaNet loader opens its ``.pth`` and ``.yml``); a
    renamed file in mineru's source fails here."""
    base = _mineru_package_dir()
    formulanet = (base / "model/mfr/pp_formulanet_plus_m/predict_formula.py").read_text("utf-8")
    for name in doctor._MINERU_PIPELINE_FILES["pp_formulanet_plus_m"]:
        assert f'"{name}"' in formulanet, name
    layout = (base / "model/layout/pp_doclayoutv2.py").read_text("utf-8")
    assert "PPDocLayoutV2Config.from_pretrained(self.model_dir)" in layout
    assert "PPDocLayoutV2ForObjectDetection.from_pretrained(self.model_dir" in layout
    unimernet = (base / "model/mfr/unimernet/unimernet_hf/modeling_unimernet.py").read_text("utf-8")
    assert "AutoTokenizer.from_pretrained(model_path)" in unimernet
    assert "VisionEncoderDecoderConfig.from_pretrained(model_path)" in unimernet
