# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (c) 2026 Hal Hildebrand. All rights reserved.
"""The :mod:`nexus._hook_runtime` package must stay cheap to import (nexus-br31l).

This guards a property no functional test can see. Every hook verb in the
command tier reaches :func:`nexus._hook_runtime._io.never_fail` on its way
to doing anything at all, so whatever that import drags in is paid once per
hook event, forever. A wrong import here makes hooks slower; it never makes
them wrong, so the whole test suite stays green while the margin the port
was for disappears.

The margin is real and small. ``phase_review_close_requires_gate`` runs
stdlib-only and costs 0.03 s end to end as bash on this box (0.04 s in bead
.2's harness). Before nexus-br31l
these modules lived in ``nexus.hooks``, whose ``__init__`` imports
``structlog`` and ``nexus.session``, and Python runs a package's
``__init__`` before it can reach any module inside it -- so ``_io``, whose
own imports are pure stdlib, still cost 0.06 s to import against 0.01 s for
a bare ``import nexus``. Porting the close gate onto that would have been a
hot-path regression rather than a speedup.

Two checks, deliberately overlapping. :func:`test_package_init_imports_nothing`
and :func:`test_module_scope_imports_are_stdlib_only` read the AST, so they
fail with a precise file and line naming what was added.
:func:`test_importing_io_in_a_fresh_interpreter_stays_cheap` spawns a real
interpreter and asserts on ``sys.modules``, so it catches what the AST
cannot: a stdlib-looking import whose own transitive graph is heavy, and any
future re-export added through ``__getattr__`` rather than an import
statement. The AST checks alone would be an approximation of the property;
the subprocess check alone would not say where the cost came from.
"""
from __future__ import annotations

import ast
import json
import os
import subprocess
import sys
from pathlib import Path

import pytest

from nexus._hook_runtime.entry import VERB_TABLE

_PKG = Path(__file__).resolve().parents[2] / "src" / "nexus" / "_hook_runtime"

#: Modules the cheap path is allowed to pull in beyond the standard library.
#: Empty on purpose -- there is no third-party import this package needs, and
#: an addition here should be a decision someone argued for, not a default.
_ALLOWED_THIRD_PARTY: frozenset[str] = frozenset()


def _module_scope_imports(path: Path) -> list[tuple[int, str]]:
    """Return ``(lineno, root_module)`` for each import at MODULE scope.

    Deferred imports -- the ones inside a function body, which is how this
    package pays for ``importlib``, ``nexus.logging_setup`` and each verb's
    own module -- are deliberately not reported: only a real dispatch pays
    those, which is the entire design. ``ast.walk`` would flatten that
    distinction away, so the tree's top level is iterated directly.
    """
    tree = ast.parse(path.read_text(), filename=str(path))
    found: list[tuple[int, str]] = []
    for node in tree.body:
        if isinstance(node, ast.Import):
            found.extend((node.lineno, alias.name.split(".")[0]) for alias in node.names)
        elif isinstance(node, ast.ImportFrom):
            if node.level:  # a relative import cannot leave the package
                continue
            if node.module == "__future__":
                continue
            found.append((node.lineno, (node.module or "").split(".")[0]))
    return found


def _package_modules() -> list[Path]:
    mods = sorted(p for p in _PKG.glob("*.py") if p.name != "__init__.py")
    assert mods, f"no modules found under {_PKG} -- this test would pass vacuously"
    return mods


def test_package_init_imports_nothing() -> None:
    """``__init__.py`` is the one file whose imports EVERY hook event pays.

    Not even a convenience re-export belongs here: ``from ._io import
    never_fail`` would import ``_io`` for a caller who only wanted
    ``_config``, which is the exact mistake ``nexus.hooks`` makes with
    ``nexus.session``.
    """
    init = _PKG / "__init__.py"
    imports = _module_scope_imports(init)
    assert imports == [], (
        f"{init} must import nothing at module scope; found "
        + ", ".join(f"{name!r} (line {lineno})" for lineno, name in imports)
        + ". Python runs this file before any module beside it, so this cost "
        "is paid by every hook event on every dispatch. Import the submodule "
        "you want directly instead."
    )


@pytest.mark.parametrize("module", _package_modules(), ids=lambda p: p.name)
def test_module_scope_imports_are_stdlib_only(module: Path) -> None:
    """Module scope stays stdlib; anything heavier is deferred into a function."""
    offenders = [
        (lineno, name)
        for lineno, name in _module_scope_imports(module)
        if name not in sys.stdlib_module_names and name not in _ALLOWED_THIRD_PARTY
    ]
    assert not offenders, (
        f"{module} imports non-stdlib modules at module scope: "
        + ", ".join(f"{name!r} (line {lineno})" for lineno, name in offenders)
        + ". Defer it into the function that needs it, so a dispatch that does "
        "not reach that code path does not pay for it."
    )


def test_importing_io_in_a_fresh_interpreter_stays_cheap() -> None:
    """The property itself, not an AST approximation of it.

    ``_io`` is the module every real dispatch imports, so it is the one whose
    transitive graph decides the floor. ``structlog`` is named explicitly
    because it was the measured cost (~0.06 s here, its ``__init__`` pulling
    ``structlog.dev`` -> ``rich.traceback`` -> ``pygments`` -> an
    ``importlib.metadata`` entry-point scan) and because ``rich`` is NOT
    dev-only tooling: ``sigstore``, a runtime dependency of conexus, requires
    it, so a real user install carries the whole chain.
    """
    probe = (
        "import sys; import nexus._hook_runtime._io; "
        "print('\\n'.join(m for m in ('structlog', 'rich', 'click', "
        "'nexus.session', 'nexus.hooks', 'nexus.cli') if m in sys.modules))"
    )
    proc = subprocess.run(
        [sys.executable, "-c", probe],
        capture_output=True,
        text=True,
        check=False,
    )
    assert proc.returncode == 0, f"probe failed:\n{proc.stderr}"
    leaked = [line for line in proc.stdout.split("\n") if line.strip()]
    assert not leaked, (
        "importing nexus._hook_runtime._io pulled in " + ", ".join(leaked) + ". "
        "Every hook event pays this. Check what was added to "
        f"{_PKG}/__init__.py or to _io.py's module scope."
    )


def _dispatch_probe(verb_source: str, tmp_path: Path) -> subprocess.CompletedProcess[str]:
    """Run a real ``nx-hook`` dispatch against a synthetic verb in *tmp_path*."""
    (tmp_path / "probe_verb.py").write_text(verb_source)
    env = {
        **os.environ,
        "PYTHONPATH": str(tmp_path),
        "_NX_HOOK_TEST_VERB_OVERRIDE": json.dumps({"probe": "probe_verb"}),
    }
    return subprocess.run(
        [sys.executable, "-m", "nexus._hook_runtime.entry", "probe"],
        capture_output=True,
        text=True,
        input="{}",
        env=env,
        check=False,
    )


def test_a_stdlib_only_verb_dispatch_never_loads_structlog(tmp_path: Path) -> None:
    """The end-to-end property, pinned without timing.

    A timing assertion here would be flaky on a loaded box -- this repo's
    suite routinely runs beside several Postgres substrates -- so the
    observable stands in for the cost: ``structlog`` costs about 0.06 s
    against about 0.01 s of interpreter startup, so "did the dispatch import
    it" and "did the dispatch stay cheap" are the same question.

    This is the check that would have caught the defect nexus-br31l found
    second. Moving these modules out of ``nexus.hooks`` made ``import
    nexus._hook_runtime._io`` cheap, but ``main()`` still called a logging
    bridge eagerly, so a real dispatch measured 0.06 s while the import floor
    measured 0.02 s. Every AST check passed throughout.
    """
    verb = (
        "import sys\n"
        "from nexus._hook_runtime._io import HookResult\n"
        "def run(payload):\n"
        "    heavy = [m for m in ('structlog', 'rich', 'pygments', 'nexus.session',\n"
        "                         'nexus.hooks', 'nexus.cli', 'click') if m in sys.modules]\n"
        "    return HookResult(stdout='LOADED:' + ','.join(heavy))\n"
    )
    proc = _dispatch_probe(verb, tmp_path)
    assert proc.returncode == 0, f"dispatch failed:\n{proc.stderr}"
    loaded = proc.stdout.strip().removeprefix("LOADED:")
    assert loaded == "", (
        f"a stdlib-only verb dispatch imported {loaded}. Something on the "
        "dispatch path stopped deferring -- check main() and _io's module "
        "scope. This is the whole margin: the close gate's common path "
        "costs 0.03 s as bash, so 0.06 s of imports before it does "
        "anything makes the port a regression."
    )


def test_stray_stdout_from_a_verb_cannot_corrupt_the_envelope(tmp_path: Path) -> None:
    """stdout is the decision channel; main() routes everything else to stderr.

    Claude Code parses this process's stdout as the hook's JSON decision, so
    one stray line reads as a hook malfunction (the nexus D9 class). This
    used to be prevented by configuring structlog eagerly, which cost every
    dispatch 0.06 s and only covered structlog. The guard replacing it is
    structural and wider: it catches a bare ``print()`` too, as here.
    """
    verb = (
        "print('stray line from the verb body')\n"
        "from nexus._hook_runtime._io import HookResult\n"
        "def run(payload):\n"
        "    print('another stray line')\n"
        "    return HookResult(stdout='{\"decision\": \"allow\"}')\n"
    )
    proc = _dispatch_probe(verb, tmp_path)
    assert proc.returncode == 0, f"dispatch failed:\n{proc.stderr}"
    assert proc.stdout == '{"decision": "allow"}\n', (
        f"stdout carried more than the envelope: {proc.stdout!r}"
    )
    assert "stray line from the verb body" in proc.stderr
    assert "another stray line" in proc.stderr


def test_a_subprocess_spawned_by_a_verb_cannot_reach_the_envelope(tmp_path: Path) -> None:
    """A child process inherits OS fd 1; rebinding ``sys.stdout`` does not stop it.

    This is the half of the guard a Python-level swap cannot provide, and the
    reason ``main()`` also redirects the fd. No verb shells out today --
    ``session-start``'s one ``subprocess.run`` captures its output -- but the
    Phase 2 ledger verbs are ports of bash scripts that call ``bd`` and
    ``git``, so this is the shape that will arrive next.
    """
    verb = "\n".join([
        "import subprocess, sys",
        "from nexus._hook_runtime._io import HookResult",
        "def run(payload):",
        "    subprocess.run([sys.executable, '-c', \"print('child wrote to fd 1')\"], check=False)",
        "    return HookResult(stdout='{\"decision\": \"allow\"}')",
        "",
    ])
    proc = _dispatch_probe(verb, tmp_path)
    assert proc.returncode == 0, f"dispatch failed:\n{proc.stderr}"
    assert proc.stdout == '{"decision": "allow"}\n', (
        f"a child process reached the decision channel: {proc.stdout!r}"
    )
    assert "child wrote to fd 1" in proc.stderr


def test_a_swallowed_crash_still_reaches_the_hook_log(tmp_path: Path) -> None:
    """``never_fail``'s diagnostic must survive the removal of the eager bridge.

    Before nexus-br31l, ``main()`` configured logging unconditionally, so this
    warning always had a sink. Removing that call is what makes a stdlib-only
    verb cheap -- but ``main()`` reads the payload and runs the verb before any
    verb could configure anything, so no verb, however diligent, could put a
    sink in place in time. ``_io._emit`` therefore configures one itself at the
    moment it needs it, which keeps the cost on the path that actually logs.

    Note what this does NOT cover, measured rather than assumed:
    ``read_payload``'s own parse-failure lines are emitted at DEBUG and hook
    mode's sink defaults to INFO, so they never reached the file and did not
    before this change either. Only the warning-level boundary is a real
    diagnostic here.
    """
    config_dir = tmp_path / "cfg"
    verb = "\n".join([
        "from nexus._hook_runtime._io import HookResult",
        "def run(payload):",
        "    raise RuntimeError('deliberate crash')",
        "",
    ])
    (tmp_path / "probe_verb.py").write_text(verb)
    proc = subprocess.run(
        [sys.executable, "-m", "nexus._hook_runtime.entry", "probe"],
        capture_output=True,
        text=True,
        input="{}",
        env={
            **os.environ,
            "PYTHONPATH": str(tmp_path),
            "NEXUS_CONFIG_DIR": str(config_dir),
            "_NX_HOOK_TEST_VERB_OVERRIDE": json.dumps({"probe": "probe_verb"}),
        },
        check=False,
    )
    assert proc.returncode == 0, f"a swallowed crash must still exit 0:\n{proc.stderr}"
    log = config_dir / "logs" / "hook.log"
    assert log.exists(), f"no hook.log under {config_dir}"
    body = log.read_text()
    assert "hook_boundary_swallowed_exception" in body, (
        f"the crash-swallow diagnostic never reached {log}; contents: {body!r}"
    )
    assert "deliberate crash" in body
    # nexus-3lc5s: the streams must tell a crash from a verb with nothing to
    # say; before this both were exit 0 with stdout and stderr empty.
    assert "[nx-hook] probe: swallowed RuntimeError: deliberate crash" in proc.stderr
    assert proc.stdout == ""


# ---------------------------------------------------------------------------
# The same property, over the verbs that actually ship (bead nexus-q02nx.21).
# ---------------------------------------------------------------------------


def test_the_real_verb_modules_import_no_structlog() -> None:
    """Every module in ``VERB_TABLE``, imported in a fresh interpreter.

    ``test_a_stdlib_only_verb_dispatch_never_loads_structlog`` above is a
    good check with a blind spot, and this file is the right place to say
    what it is. That test writes a SYNTHETIC verb into ``tmp_path`` and
    dispatches it. A verb in ``tmp_path`` is not in the ``nexus.hooks``
    package, so it never runs ``nexus/hooks/__init__.py`` -- and every verb
    that really ships IS in that package. The gate proved the spine was
    thin and was structurally incapable of seeing the verbs.

    What it missed, measured on the dev Mac (10 runs, median): bare python
    14 ms, the spine 17 ms, a real verb 78 ms, against the 40 ms
    ``_run_python_hook.sh`` path the command tier replaces. The package
    ``__init__`` imported ``structlog`` and ``nexus.session`` eagerly and
    Python runs it before any submodule, so the port was a 2x latency
    regression on hooks that fire per Bash call. nexus-br31l carved out
    ``nexus._hook_runtime`` to fix exactly this and fixed only the spine.
    After deferring both, a real verb costs 29 ms.

    So the rule this encodes is not "check the dispatch path" -- that was
    already checked. It is that a gate's reference point has to be the
    thing under test. A synthetic stand-in cannot move when the real
    subject does.
    """
    assert VERB_TABLE, "VERB_TABLE is empty -- this gate would examine nothing"

    modules = sorted(set(VERB_TABLE.values()))
    probe = (
        "import sys\n"
        + "".join(f"import {m}\n" for m in modules)
        + "heavy = [m for m in ('structlog', 'rich', 'pygments', 'click',\n"
        "                     'nexus.session', 'nexus.cli') if m in sys.modules]\n"
        "print(','.join(heavy))\n"
    )
    proc = subprocess.run(
        [sys.executable, "-c", probe],
        capture_output=True, text=True, check=False,
    )
    assert proc.returncode == 0, f"probe failed:\n{proc.stderr}"
    loaded = proc.stdout.strip()
    assert loaded == "", (
        f"importing the {len(modules)} real verb modules pulled in {loaded}. "
        "Check nexus/hooks/__init__.py first: it runs before every verb in "
        "that package, so one eager import there is paid by all of them."
    )


def test_the_hooks_package_init_defers_its_two_heavy_imports() -> None:
    """Names the two, so a re-add has to delete this test and say why.

    The general check above would also catch a re-add, but it would report
    it as "some verb got heavy". These two are the specific ones that were
    eager for the whole of Phase 2 and Phase 3, and naming them keeps the
    history attached to the constraint.
    """
    probe = (
        "import sys, nexus.hooks\n"
        "print(','.join(m for m in ('structlog', 'nexus.session') "
        "if m in sys.modules))"
    )
    proc = subprocess.run(
        [sys.executable, "-c", probe], capture_output=True, text=True, check=False
    )
    assert proc.returncode == 0, proc.stderr
    assert proc.stdout.strip() == "", (
        f"nexus.hooks re-acquired an eager {proc.stdout.strip()}. All nine "
        "_logger() call sites in that module are error paths and the "
        "nexus.session pair is used only in session_start(), so both belong "
        "behind a deferred import."
    )
