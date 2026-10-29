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
import os
import re
import subprocess
import sys
import types
from pathlib import Path

import pytest

from nexus import _mineru_spawn, pdf_extractor, pdf_stack
from nexus.commands import doctor
from nexus.pdf_stack import StackState
from tests._module_seam import setattr_in
from nexus.upgrade_finish import _classify

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


def test_server_child_module_imports_nexus_before_mineru() -> None:
    src = (_SRC / "_mineru_api_child.py").read_text(encoding="utf-8")
    assert _nexus_import_offset(src) < _first_heavy_import_offset(src)


def test_windows_server_argv_runs_the_child_module_under_the_sibling_python(tmp_path: Path) -> None:
    scripts = tmp_path / "Scripts"
    scripts.mkdir()
    (scripts / "python.exe").write_bytes(b"MZ")
    argv = _mineru_spawn.mineru_server_argv(str(scripts / "mineru-api.exe"), 8123, platform="win32")
    assert argv == [
        str(scripts / "python.exe"), "-m", "nexus._mineru_api_child", "--host", "127.0.0.1", "--port", "8123",
    ]


def test_windows_server_argv_falls_back_to_this_interpreter(tmp_path: Path) -> None:
    argv = _mineru_spawn.mineru_server_argv(str(tmp_path / "mineru-api.exe"), 7, platform="win32")
    assert argv[:3] == [sys.executable, "-m", "nexus._mineru_api_child"]


def test_posix_server_argv_is_the_console_script_unchanged() -> None:
    assert _mineru_spawn.mineru_server_argv("/v/bin/mineru-api", 9, platform="linux") == [
        "/v/bin/mineru-api", "--host", "127.0.0.1", "--port", "9",
    ]


def test_spawn_server_process_uses_the_windows_argv(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """The one launch core (nx mineru start, the on-demand lifecycle, the crash restart)."""
    seen: list[list[str]] = []

    class _Proc:
        pid = 4242

    monkeypatch.setattr(_mineru_spawn, "_resolve_mineru_api_bin", lambda: str(tmp_path / "mineru-api.exe"))
    setattr_in(monkeypatch, "nexus._vcrt", "sys.platform", "win32")
    setattr_in(
        monkeypatch, "nexus._mineru_spawn", "subprocess.Popen", lambda cmd, **kw: seen.append(list(cmd)) or _Proc(),
    )
    payloads: list[dict] = []
    monkeypatch.setattr(_mineru_spawn, "_write_pid_file", lambda _path, payload: payloads.append(payload))
    monkeypatch.setenv("NEXUS_CONFIG_DIR", str(tmp_path))
    assert _mineru_spawn.spawn_server_process(8123) is not None
    assert seen[0][1:3] == ["-m", "nexus._mineru_api_child"] and seen[0][-2:] == ["--port", "8123"]
    # the pid file names the interpreter the server runs under (the stale-server sweep judges it)
    assert payloads[0]["python"] == seen[0][0]


def test_server_child_has_imported_nexus_when_mineru_loads(tmp_path: Path) -> None:
    marker = tmp_path / "marker"
    fake = _fake_mineru(
        tmp_path / "fake",
        "",
        {
            "fast_api.py": (
                f"import sys\nopen({str(marker)!r}, 'w').write(str('nexus' in sys.modules) + ' ' + ' '.join(sys.argv[1:]))\n"
                "def main():\n    pass\n"
            )
        },
    )
    proc = subprocess.run(
        [sys.executable, "-m", "nexus._mineru_api_child", "--host", "127.0.0.1", "--port", "5"],
        env=_env(fake), capture_output=True, text=True, timeout=60,
    )
    assert proc.returncode == 0, proc.stderr
    assert marker.read_text() == "True --host 127.0.0.1 --port 5"


def test_the_child_module_is_still_recognised_as_a_mineru_server() -> None:
    """nx mineru stop / the pid-recycle guard / the stale-server sweep classify by argv; a python
    running the child module must still be a mineru-api."""
    for exe in ("/v/bin/python", r"C:\v\Scripts\python.exe"):
        cmd = f"{exe} -m nexus._mineru_api_child --host 127.0.0.1 --port 5"
        assert _classify(cmd) == "mineru", cmd
    assert _classify("/v/bin/python -m nexus.cli doctor") == "other"


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
