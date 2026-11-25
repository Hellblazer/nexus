# SPDX-License-Identifier: AGPL-3.0-or-later
"""``_interpreter.reexec_if_needed()`` re-execs a hook only when it is run AS the script (nexus-sby49.2).

Four plugin scripts call it at module scope: the git guard, the credential guard,
``mailbox_drain`` and ``version_lockstep_hook`` (``version_lockstep_action`` only
documents it: the hook spawns the action under its own interpreter). It
replaces the process with ``resolve()``'s interpreter via ``os.execv(target,
[target, sys.argv[0], ...])``. That is right for ``python3 script.py`` and wrong for
a test that IMPORTS the script (``spec_from_file_location`` + ``exec_module``):
``sys.argv[0]`` is then pytest, so a resolved interpreter that differs from the
venv's replaces pytest. On Windows an ``execv`` is a spawn followed by an exit, so
pytest's own output is lost and the run exits 0 with no summary (found on qwent: every
run of ``tests/test_routing_subagent_git_write.py`` stopped at the test that imports
the guard, about 97 percent of the way through).

The rule: the call re-execs only when its caller is the process's ``__main__``. It lives
in ``reexec_if_needed`` itself, not in four ``if __name__ == "__main__"`` lines, so a fifth
script cannot forget it. Run as a script, behaviour is unchanged.
"""
from __future__ import annotations

import ast
import importlib.util
import os
import pathlib
import sys

import pytest

from tests._module_seam import setattr_in

SCRIPTS = pathlib.Path(__file__).resolve().parents[2] / "conexus" / "hooks" / "scripts"
if str(SCRIPTS) not in sys.path:
    sys.path.insert(0, str(SCRIPTS))
import _interpreter  # noqa: E402

CALLERS = (
    "routing/subagent_git_write_requires_orchestrator.py",
    "routing/credential_print_guard.py",
    "mailbox_drain.py",
    "version_lockstep_hook.py",
)
OTHER_PYTHON = os.path.join(os.sep, "nonexistent", "other", "python")


@pytest.fixture()
def execs(monkeypatch):
    """Force ``resolve()`` to name a different interpreter and record every ``os.execv``."""
    calls: list[tuple[str, list[str]]] = []
    monkeypatch.delenv(_interpreter._MARKER, raising=False)
    monkeypatch.setattr(_interpreter, "resolve", lambda: OTHER_PYTHON)
    setattr_in(monkeypatch, _interpreter, "os.execv", lambda target, argv: calls.append((target, list(argv))))
    yield calls
    os.environ.pop(_interpreter._MARKER, None)  # the call sets it before it execs


def _load(rel: str, name: str):
    spec = importlib.util.spec_from_file_location(name, SCRIPTS / rel)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


@pytest.mark.parametrize("rel", CALLERS)
def test_importing_a_hook_script_never_execs(rel, execs):
    _load(rel, "imported_" + pathlib.Path(rel).stem)
    assert execs == [], f"importing {rel} called os.execv: {execs}"


def test_a_script_run_as_main_still_reexecs(execs, tmp_path):
    probe = tmp_path / "probe_hook.py"
    probe.write_text(
        f"import sys\nsys.path.insert(0, {str(SCRIPTS)!r})\nimport _interpreter\n_interpreter.reexec_if_needed()\n"
    )
    import runpy

    runpy.run_path(str(probe), run_name="__main__")
    assert len(execs) == 1, execs
    target, argv = execs[0]
    assert target == OTHER_PYTHON and argv[0] == OTHER_PYTHON and argv[1] == os.path.abspath(str(probe))


def test_the_same_probe_imported_under_another_name_does_not_reexec(execs, tmp_path):
    probe = tmp_path / "probe_hook.py"
    probe.write_text(
        f"import sys\nsys.path.insert(0, {str(SCRIPTS)!r})\nimport _interpreter\n_interpreter.reexec_if_needed()\n"
    )
    spec = importlib.util.spec_from_file_location("probe_hook", probe)
    assert spec is not None and spec.loader is not None
    spec.loader.exec_module(importlib.util.module_from_spec(spec))
    assert execs == []


def test_the_marker_still_stops_a_second_pass_when_run_as_main(execs, tmp_path, monkeypatch):
    monkeypatch.setenv(_interpreter._MARKER, "1")
    probe = tmp_path / "probe_hook.py"
    probe.write_text(
        f"import sys\nsys.path.insert(0, {str(SCRIPTS)!r})\nimport _interpreter\n_interpreter.reexec_if_needed()\n"
    )
    import runpy

    runpy.run_path(str(probe), run_name="__main__")
    assert execs == []


def test_each_caller_still_calls_it_at_module_scope():
    """Non-vacuity: the import test above proves nothing about a script that stopped calling it."""
    found = 0
    for rel in CALLERS:
        tree = ast.parse((SCRIPTS / rel).read_text(encoding="utf-8"))
        calls = [
            n for n in tree.body
            if isinstance(n, ast.Expr) and isinstance(n.value, ast.Call)
            and isinstance(n.value.func, ast.Attribute) and n.value.func.attr == "reexec_if_needed"
        ]
        assert len(calls) == 1, f"{rel}: expected one module-scope reexec_if_needed() call, found {len(calls)}"
        found += 1
    assert found == 4


def test_no_other_script_calls_it_at_module_scope_unlisted():
    """A new caller joins CALLERS (and so the import check) or it is a finding."""
    callers = sorted(
        str(p.relative_to(SCRIPTS)).replace(os.sep, "/")
        for p in SCRIPTS.rglob("*.py")
        if p.name != "_interpreter.py" and "_interpreter.reexec_if_needed()" in p.read_text(encoding="utf-8")
        and any(
            isinstance(n, ast.Expr) and isinstance(n.value, ast.Call) and getattr(n.value.func, "attr", "") == "reexec_if_needed"
            for n in ast.parse(p.read_text(encoding="utf-8")).body
        )
    )
    assert callers == sorted(CALLERS), callers
