# SPDX-License-Identifier: AGPL-3.0-or-later
"""A test's fake for a stdlib function must stay inside the module under test.

nexus-hkafl: ``tests/test_vector_retry.py`` installed a blocking fake sleep with
``monkeypatch.setattr(retry_mod.time, "sleep", fc.sleep)``. ``retry_mod.time`` is the
stdlib ``time`` module, so the fake replaced ``time.sleep`` for the whole worker. A
substrate teardown in the same worker (``drop_test_tenant``) then ran
``subprocess.run(timeout=60)``, whose wait loop sleeps through ``time.sleep``, and
blocked on the fake until the run was killed about 50 minutes later.

The same holds for every stdlib module: ``monkeypatch.setattr(sub.subprocess, "run",
f)``, ``patch("nexus.x.os.kill")`` and ``patch("asyncio.create_subprocess_exec")``
each replace the attribute on the one module every thread and fixture shares.
``tests/_module_seam.py`` replaces the module's own binding instead. These tests pin
that a fake installed that way is seen by the module and not by anyone else, and
lint ``tests/`` so the global forms do not come back.
"""
from __future__ import annotations

import ast
import os
import os.path
import subprocess
import sys
import threading
import time
import warnings
from pathlib import Path
from types import ModuleType
from unittest.mock import MagicMock, patch

import pytest

import nexus.rate_brake as rate_brake_mod
import nexus.retry as retry_mod
from tests import _engine_substrate as sub
from tests._module_seam import ModuleProxy, module_proxy, module_time, patch_in, patch_time, setattr_in

_TESTS = Path(__file__).resolve().parent

#: The blocking fake gives up after this long, so a regression fails the test
#: instead of hanging the worker the way nexus-hkafl did.
_FAKE_SLEEP_GIVE_UP_S = 20.0


class _NeverAdvancingSleep:
    """The ``_SyncFakeClock.sleep`` shape with nobody calling ``advance()``:
    every call blocks. Records who called it."""

    def __init__(self) -> None:
        self.calls: list[float] = []
        self._never = threading.Event()

    def __call__(self, seconds: float) -> None:
        self.calls.append(seconds)
        if not self._never.wait(timeout=_FAKE_SLEEP_GIVE_UP_S):
            raise AssertionError("a blocking fake sleep was reached outside the module under test")


def _child_that_outlives_a_few_wait_polls() -> list[str]:
    return [sys.executable, "-c", "import time; time.sleep(0.3)"]


def test_subprocess_run_with_timeout_returns_while_a_module_fake_sleep_blocks(monkeypatch) -> None:
    fake = _NeverAdvancingSleep()
    module_time(monkeypatch, retry_mod).sleep = fake

    # The fake is live for the module under test...
    assert retry_mod.time.sleep is fake
    # ...and for the brake that module's callers reach.
    assert rate_brake_mod.time is retry_mod.time
    # The process-wide module is untouched.
    assert time.sleep is not fake

    started = time.monotonic()
    result = subprocess.run(_child_that_outlives_a_few_wait_polls(), timeout=60, check=False)  # noqa: S603
    assert result.returncode == 0
    assert time.monotonic() - started < _FAKE_SLEEP_GIVE_UP_S
    assert fake.calls == []


def test_patch_time_is_local_and_restores(monkeypatch) -> None:
    original_binding = retry_mod.time
    with patch_time("nexus.retry", "sleep") as mock_sleep:
        retry_mod.time.sleep(5.0)
        assert time.sleep is not mock_sleep
        result = subprocess.run(_child_that_outlives_a_few_wait_polls(), timeout=60, check=False)  # noqa: S603
        assert result.returncode == 0
    mock_sleep.assert_called_once_with(5.0)
    assert retry_mod.time is original_binding
    assert rate_brake_mod.time is time


def test_module_time_reuses_one_proxy_and_forwards_the_rest(monkeypatch) -> None:
    proxy = module_time(monkeypatch, "nexus.retry")
    assert isinstance(proxy, ModuleProxy)
    assert module_time(monkeypatch, retry_mod) is proxy
    proxy.monotonic = lambda: 42.0
    assert retry_mod.time.monotonic() == 42.0
    assert retry_mod.time.perf_counter is time.perf_counter


@pytest.mark.skipif(sys.platform == "win32", reason="Windows waits on the process handle, not a time.sleep poll")
def test_control_a_global_fake_sleep_is_reached_by_subprocess_wait() -> None:
    """The exposure the seam removes, shown directly: with ``time.sleep`` replaced
    process-wide, ``subprocess.run(timeout=...)`` calls the fake. If this stops
    being true the regression test above proves nothing on this platform."""

    class _Reached(Exception):
        pass

    def boom(_seconds: float) -> None:
        raise _Reached

    with patch("time.sleep", side_effect=boom), pytest.raises(_Reached):
        subprocess.run(_child_that_outlives_a_few_wait_polls(), timeout=60, check=False)  # noqa: S603


# ── the general seam: any stdlib module a module imports ─────────────────────


def test_setattr_in_fakes_subprocess_for_the_module_only(monkeypatch) -> None:
    """The sidecar-ordering shape: a fake ``subprocess.run`` for the substrate
    module, while a real ``subprocess.run`` elsewhere in the process still runs."""
    seen: list[list[str]] = []
    setattr_in(monkeypatch, sub, "subprocess.run", lambda args, **_k: seen.append(args))

    sub.subprocess.run(["fake"])
    assert seen == [["fake"]]
    assert subprocess.run is not sub.subprocess.run
    assert sub.subprocess.Popen is subprocess.Popen
    result = subprocess.run(_child_that_outlives_a_few_wait_polls(), timeout=60, check=False)  # noqa: S603
    assert result.returncode == 0
    assert seen == [["fake"]]


def test_setattr_in_restores_the_binding_at_teardown() -> None:
    original = sub.subprocess
    mp = pytest.MonkeyPatch()
    setattr_in(mp, sub, "subprocess.run", lambda *_a, **_k: None)
    assert isinstance(sub.subprocess, ModuleProxy)
    mp.undo()
    assert sub.subprocess is original is subprocess


def test_setattr_in_refuses_a_misspelled_attribute(monkeypatch) -> None:
    with pytest.raises(AttributeError):
        setattr_in(monkeypatch, sub, "subprocess.rnu", lambda: None)
    setattr_in(monkeypatch, sub, "os.not_on_this_platform", 1, raising=False)
    assert sub.os.not_on_this_platform == 1
    assert not hasattr(os, "not_on_this_platform")


def test_patch_in_as_context_manager_decorator_and_start_stop() -> None:
    with patch_in("tests._engine_substrate", "subprocess.run", return_value=7) as fake:
        assert sub.subprocess.run() == 7
        assert subprocess.run is not fake
    assert sub.subprocess is subprocess

    @patch_in(sub, "subprocess.Popen")
    @patch_in(sub, "subprocess.run")
    def decorated(run: MagicMock, popen: MagicMock) -> None:
        assert sub.subprocess.run is run
        assert sub.subprocess.Popen is popen
        assert subprocess.run is not run

    decorated()
    assert sub.subprocess is subprocess

    patcher = patch_in(sub, "os.kill")
    kill = patcher.start()
    try:
        assert sub.os.kill is kill
        assert os.kill is not kill
    finally:
        patcher.stop()
    assert sub.os is os


def test_patch_in_reaches_a_submodule_and_several_modules() -> None:
    with patch_in(sub, "os.path.exists", return_value=True) as exists:
        assert sub.os.path.exists("/no/such/path") is True
        assert os.path.exists("/no/such/path") is False
        assert sub.os.path.join is os.path.join
    exists.assert_called_once_with("/no/such/path")
    assert sub.os is os

    with patch_in((retry_mod, rate_brake_mod), "time.monotonic", return_value=1.0):
        assert retry_mod.time is rate_brake_mod.time
        assert rate_brake_mod.time.monotonic() == 1.0
    assert retry_mod.time is time


def test_nested_patches_share_one_proxy_and_unwind_in_order() -> None:
    with patch_in(sub, "subprocess.run") as run:
        proxy = sub.subprocess
        with patch_in(sub, "subprocess.Popen") as popen:
            assert sub.subprocess is proxy
            assert (sub.subprocess.run, sub.subprocess.Popen) == (run, popen)
        assert sub.subprocess is proxy
        assert sub.subprocess.Popen is subprocess.Popen
    assert sub.subprocess is subprocess


def test_a_module_without_the_binding_is_refused() -> None:
    """``from subprocess import run`` leaves nothing to proxy; patch ``mod.run``."""
    fake_mod = ModuleType("fake_mod")
    with pytest.raises(AttributeError, match="no module-level 'subprocess'"), patch_in(fake_mod, "subprocess.run"):
        pass


def test_module_proxy_returns_the_installed_proxy(monkeypatch) -> None:
    proxy = module_proxy(monkeypatch, sub, "shutil")
    proxy.which = lambda _name: "/fake/bin/x"
    assert sub.shutil.which("x") == "/fake/bin/x"
    assert module_proxy(monkeypatch, "tests._engine_substrate", "shutil") is proxy


# ── lint: no test patches a stdlib module process-wide ───────────────────────

#: Stdlib modules whose attributes tests fake. Patching ``<anything>.<one of
#: these>.<attr>`` (or the bare module) replaces the attribute for the process.
COVERED: frozenset[str] = frozenset({
    "asyncio", "atexit", "ctypes", "datetime", "getpass", "importlib", "multiprocessing",
    "os", "platform", "queue", "random", "select", "selectors", "shutil", "signal",
    "socket", "sqlite3", "subprocess", "sys", "tarfile", "tempfile", "threading",
    "time", "urllib", "uuid", "webbrowser",
})

#: Process-wide by nature: the stdlib module itself (or the interpreter) reads
#: these, so a per-module proxy would not reach the reader. Redirecting them is
#: what ``capsys`` and ``tmp_path`` setups do anyway.
GLOBAL_BY_NATURE: dict[str, str] = {
    "sys.stdin": "the interpreter's stdin, read by input() and every consumer",
    "sys.stdout": "the interpreter's stdout, as capsys replaces it",
    "sys.stderr": "the interpreter's stderr, as capsys replaces it",
    "sys.argv": "the process argv that click and argparse read from sys",
    "tempfile.tempdir": "read by tempfile itself; a proxy would not reach mkdtemp",
}

#: Sites that must stay global, each with the reason. Keyed by (test file, target).
ALLOWED: dict[tuple[str, str], str] = {
    ("tests/test_module_seam.py", "time.sleep"):
        "the control test shows the global exposure on purpose",
    ("tests/daemon/_children.py", "os.getuid"):
        "adds a missing os.getuid on Windows (raising=False) for this helper's own call",
    ("tests/daemon/test_service_status_clarity.py", "urllib.request.urlopen"):
        "nexus.commands.daemon imports urllib.request inside _probe_health (deferred for CLI startup)",
    ("tests/test_nx_answer.py", "asyncio.sleep"):
        "nexus.mcp.core imports asyncio inside each function; the spy asserts no positive sleep anywhere",
    ("tests/test_session_end_launcher.py", "subprocess.Popen"):
        "nexus._session_end_launcher imports subprocess inside the function, off the POSIX pre-fork path",
    ("tests/test_phase5_integration.py", "importlib.metadata.version"):
        "nexus.mcp_infra imports importlib.metadata.version inside the function",
    ("tests/test_upgrade_finish.py", "importlib.metadata.distribution"):
        "nexus.upgrade_finish imports importlib.metadata inside the function",
    ("tests/upgrade/test_ladder_runner.py", "importlib.metadata.version"):
        "nexus.upgrade_ladder.runner imports importlib.metadata.version inside the function",
    ("tests/hooks/test_exec_off_cwd.py", "sys.platform"):
        "the scripts under test run shutil.which, which reads sys.platform itself to take its Windows path",
    ("tests/test_cli_windows_utf8.py", "sys.platform"):
        "re-imports nexus.cli, whose import-time code reads sys.platform before any binding exists",
    ("tests/test_deferred_labeling.py", "subprocess.Popen"):
        "nexus.commands.index imports subprocess inside _spawn_deferred_labeling (spawn-only branch)",
    ("tests/test_enrich_aspects.py", "subprocess.run"):
        "tripwire: asserts no code path in the process spawns",
    ("tests/test_false_clean_diagnostics_service_mode.py", "sqlite3.connect"):
        "tripwire: asserts no code path in the process opens SQLite",
    ("tests/test_operator_dispatch.py", "subprocess.run"):
        "tripwire: asserts no code path in the process uses the sync spawn",
    ("tests/test_operator_dispatch.py", "subprocess.Popen"):
        "tripwire: asserts no code path in the process uses the sync spawn",
    ("tests/test_windows_process_identity_sites.py", "subprocess.run"):
        "tripwire: asserts the Windows branch reaches no POSIX probe anywhere",
    ("tests/test_windows_process_identity_sites.py", "subprocess.check_output"):
        "tripwire: asserts the Windows branch reaches no POSIX probe anywhere",
    ("tests/test_windows_process_identity_sites.py", "subprocess.Popen"):
        "tripwire: asserts the Windows branch reaches no POSIX probe anywhere",
}

#: The seam's own docstrings and implementation name the forms they replace.
_EXEMPT_FILES = {"tests/_module_seam.py"}


def _dotted(node: ast.expr) -> str | None:
    parts: list[str] = []
    while isinstance(node, ast.Attribute):
        parts.append(node.attr)
        node = node.value
    if isinstance(node, ast.Name):
        parts.append(node.id)
        return ".".join(reversed(parts))
    return None


def _imported_modules(tree: ast.Module) -> set[str]:
    names: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for alias in node.names:
                names.add(alias.asname or alias.name.split(".")[0])
    return names


def _stdlib_target(dotted: str, *, bare_ok: bool) -> str | None:
    """``"nexus.x.subprocess.run"`` -> ``"subprocess.run"``; ``None`` when the path
    does not go through a covered module (or only names the module itself)."""
    parts = dotted.split(".")
    for i, part in enumerate(parts[:-1]):
        if part in COVERED:
            if i == 0 and not bare_ok:
                return None
            return ".".join(parts[i:])
    return None


def _is_patch(func: ast.expr) -> bool:
    return (isinstance(func, ast.Name) and func.id == "patch") or (
        isinstance(func, ast.Attribute) and func.attr == "patch"
    )


def _global_patches(source: str) -> list[tuple[int, str]]:
    """(line, stdlib target) for every call that patches a covered stdlib module
    process-wide: ``patch("…<mod>.<attr>")``, ``patch.object(<…mod>, "attr")``,
    ``<mp>.setattr("…<mod>.<attr>", v)`` and ``<mp>.setattr(<…mod>, "attr", v)``."""
    with warnings.catch_warnings():
        warnings.simplefilter("ignore", SyntaxWarning)  # a test file's own escape sequences
        tree = ast.parse(source)
    imported = _imported_modules(tree)
    found: list[tuple[int, str]] = []
    for node in ast.walk(tree):
        if not isinstance(node, ast.Call) or not node.args:
            continue
        func, first = node.func, node.args[0]
        is_object = isinstance(func, ast.Attribute) and func.attr == "object" and _is_patch(func.value)
        is_setattr = isinstance(func, ast.Attribute) and func.attr == "setattr"
        if not (_is_patch(func) or is_object or is_setattr):
            continue
        target: str | None = None
        if isinstance(first, ast.Constant) and isinstance(first.value, str) and not is_object:
            target = _stdlib_target(first.value, bare_ok=True)
        elif (is_object or is_setattr) and len(node.args) >= 2:
            attr = node.args[1]
            dotted = _dotted(first)
            if dotted and isinstance(attr, ast.Constant) and isinstance(attr.value, str):
                bare_ok = dotted.split(".")[0] in imported
                target = _stdlib_target(f"{dotted}.{attr.value}", bare_ok=bare_ok)
        if target is not None:
            found.append((node.lineno, target))
    return found


def _offenders() -> tuple[list[str], set[tuple[str, str]]]:
    found: list[str] = []
    used: set[tuple[str, str]] = set()
    for path in sorted(_TESTS.rglob("*.py")):
        rel = path.relative_to(_TESTS.parent).as_posix()
        if rel in _EXEMPT_FILES:
            continue
        for line, target in _global_patches(path.read_text(encoding="utf-8")):
            if any(target == k or target.startswith(k + ".") for k in GLOBAL_BY_NATURE):
                continue
            if (rel, target) in ALLOWED:
                used.add((rel, target))
                continue
            found.append(f"{rel}:{line}: {target}")
    return found, used


@pytest.mark.lint
def test_no_test_patches_a_stdlib_module_process_wide() -> None:
    """Every ``<module>.subprocess`` (``.os``, ``.time``, ...) is the one stdlib
    module, so these forms fake it for every thread and every fixture teardown in
    the worker. Use ``tests._module_seam.patch_in`` / ``setattr_in`` /
    ``module_proxy`` naming the module(s) under test, or add the site to
    ``ALLOWED`` with the reason it must be process-wide."""
    offenders, used = _offenders()
    assert offenders == [], "process-wide stdlib patches (use tests/_module_seam.py):\n" + "\n".join(offenders)
    stale = sorted(set(ALLOWED) - used)
    assert stale == [], f"ALLOWED entries that no longer match a site: {stale}"


_BAD_SAMPLES = (
    ('from unittest.mock import patch\npatch("nexus.retry.time.sleep")', "time.sleep"),
    ('from unittest.mock import patch\npatch("time.sleep", side_effect=f)', "time.sleep"),
    ('monkeypatch.setattr("nexus.retry.time.sleep", f)', "time.sleep"),
    ('monkeypatch.setattr(retry_mod.time, "sleep", f)', "time.sleep"),
    ('import time\nmonkeypatch.setattr(time, "monotonic", f)', "time.monotonic"),
    ('monkeypatch.setattr(\n        sub.subprocess, "run", f)', "subprocess.run"),
    ('monkeypatch.setattr(sub.subprocess, "Popen", f)', "subprocess.Popen"),
    ('patch("nexus.commands.mineru.os.killpg")', "os.killpg"),
    ('mock.patch("nexus.x.subprocess.run", return_value=1)', "subprocess.run"),
    ('unittest.mock.patch("asyncio.create_subprocess_exec")', "asyncio.create_subprocess_exec"),
    ('patch.object(retry_mod.random, "random", return_value=0.5)', "random.random"),
    ('import os\npatch.object(os, "kill")', "os.kill"),
    ('import shutil\nmp.setattr(shutil, "which", f)', "shutil.which"),
    ('monkeypatch.setattr("sys.platform", "win32")', "sys.platform"),
    ('import sys\nmonkeypatch.setattr(sys, "prefix", p)', "sys.prefix"),
    ('patch("nexus.health.os.path.exists")', "os.path.exists"),
    ('import os\nmonkeypatch.setattr(os.path, "isjunction", f)', "os.path.isjunction"),
    ('patched.setattr(mod.signal, "signal", f)', "signal.signal"),
    ('patch("socket.socket")', "socket.socket"),
    ('monkeypatch.setattr(m.uuid, "uuid4", f)', "uuid.uuid4"),
    ('patch("threading.Thread")', "threading.Thread"),
)

_GOOD_SAMPLES = (
    'patch_time("nexus.retry", "sleep")',
    'module_time(monkeypatch, retry_mod).sleep = f',
    'patch_in("nexus.x", "subprocess.run")',
    'setattr_in(monkeypatch, sub, "subprocess.run", f)',
    'patch("nexus.indexer.timeout_s", 3)',
    'patch("nexus.retry.random_jitter")',
    'monkeypatch.setattr(mod, "time", proxy)',
    'monkeypatch.setattr(mod, "run", f)',
    'patch("nexus.x.Runner.time")',
    'patch.object(queue, "_post")',  # a fixture named queue, no `import queue`
    '# patch("subprocess.run") in a comment',
    '"""monkeypatch.setattr(sub.subprocess, "run", f) in a docstring"""',
)


@pytest.mark.lint
@pytest.mark.parametrize(("sample", "target"), _BAD_SAMPLES)
def test_the_lint_sees_each_global_form(sample: str, target: str) -> None:
    """Non-vacuity: every shape the lint exists for is matched, as the right target."""
    assert [t for _line, t in _global_patches(sample)] == [target]


@pytest.mark.lint
@pytest.mark.parametrize("sample", _GOOD_SAMPLES)
def test_the_lint_passes_local_forms(sample: str) -> None:
    assert _global_patches(sample) == []


@pytest.mark.lint
def test_the_lint_has_sites_to_check() -> None:
    """Non-vacuity over the tree: the sweep parses every test file and finds the
    allowlisted global sites, so an empty offender list means something."""
    files = list(_TESTS.rglob("*.py"))
    assert len(files) > 500
    _offenders_found, used = _offenders()
    assert used == set(ALLOWED)
