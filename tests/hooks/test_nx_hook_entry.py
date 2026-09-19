# SPDX-License-Identifier: AGPL-3.0-or-later
"""Tests for ``nexus._hook_runtime.entry`` -- the ``nx-hook`` command-tier dispatch
mechanism (RDR-215 Phase 1, bead nexus-q02nx.2).

These tests exercise the dispatch mechanism itself against the REAL entry
point, using the module's own test-only override env vars
(``_NX_HOOK_TEST_VERB_OVERRIDE`` / ``_NX_HOOK_TEST_LEDGER_VERBS``,
documented on the module) to register throwaway fixture verbs for the
duration of one subprocess call, rather than the one real verb
:data:`VERB_TABLE` carries (``session-start``, nexus-q02nx.5 -- its own
coverage lives in ``tests/hooks/test_session_start_verb.py``). Every
scenario spawns ``python -m nexus._hook_runtime.entry <verb>`` -- exactly the
callable the ``nx-hook`` console script's generated stub also calls
(``nexus._hook_runtime.entry:main``), so this needs no prior
``scripts/reinstall-tool.sh`` run to be meaningful.
"""
from __future__ import annotations

import ast
import json
import os
import subprocess
import sys
import textwrap

from nexus._hook_runtime.entry import _LEDGER_CRASH_EXIT
from pathlib import Path

from nexus._hook_runtime import entry

_ENTRY_ARGV = [sys.executable, "-m", "nexus._hook_runtime.entry"]


def _env(tmp_path: Path, **extra: str) -> dict[str, str]:
    env = dict(os.environ)
    # Never let a real dispatch touch the developer's own config dir (the
    # logging bridge writes <config>/logs/hook.log on any real verb path).
    env["NEXUS_CONFIG_DIR"] = str(tmp_path / "config")
    env.update(extra)
    return env


def _write_fixture_verb(tmp_path: Path, module_name: str, body: str) -> Path:
    """Write a throwaway verb module under *tmp_path* and return its dir,
    for the caller to add to PYTHONPATH."""
    fixtures = tmp_path / "fixtures"
    fixtures.mkdir(exist_ok=True)
    (fixtures / f"{module_name}.py").write_text(textwrap.dedent(body))
    return fixtures


def _run(argv: list[str], env: dict[str, str], stdin: str = "") -> subprocess.CompletedProcess:
    return subprocess.run(
        [*_ENTRY_ARGV, *argv], input=stdin, env=env, capture_output=True, text=True, timeout=30,
    )


# -- unknown / missing verb: a dispatch failure, not a silent hook ----------

def test_unknown_verb_exits_two_with_a_named_diagnostic(tmp_path: Path) -> None:
    proc = _run(["frobnicate"], _env(tmp_path))
    assert proc.returncode == 2, proc.stderr
    assert proc.stdout == ""
    assert "frobnicate" in proc.stderr
    assert "unknown verb" in proc.stderr


def test_missing_verb_exits_two_with_a_named_diagnostic(tmp_path: Path) -> None:
    proc = _run([], _env(tmp_path))
    assert proc.returncode == 2, proc.stderr
    assert proc.stdout == ""
    assert "missing verb" in proc.stderr


def test_a_malformed_test_override_is_swallowed_as_unknown_verb(tmp_path: Path) -> None:
    """The test-only override itself must never crash nx-hook on bad input --
    it gets the same never-fail posture as everything else in this module."""
    proc = _run(["anything"], _env(tmp_path, _NX_HOOK_TEST_VERB_OVERRIDE="{not json"))
    assert proc.returncode == 2, proc.stderr
    assert "unknown verb" in proc.stderr


# -- a registered verb: payload plumbing ------------------------------------

_ECHO_VERB = textwrap.dedent(
    """
    import json

    def run(payload):
        from nexus._hook_runtime._io import HookResult
        return HookResult(stdout=json.dumps({"received": payload}))
    """
)


def test_a_registered_verb_receives_the_parsed_payload(tmp_path: Path) -> None:
    fixtures = _write_fixture_verb(tmp_path, "echo_verb", _ECHO_VERB)
    env = _env(
        tmp_path,
        PYTHONPATH=str(fixtures),
        _NX_HOOK_TEST_VERB_OVERRIDE=json.dumps({"echo": "echo_verb"}),
    )
    proc = _run(["echo"], env, stdin='{"session_id": "s1"}')
    assert proc.returncode == 0, proc.stderr
    assert json.loads(proc.stdout) == {"received": {"session_id": "s1"}}


def test_empty_stdin_reaches_the_verb_as_a_none_payload(tmp_path: Path) -> None:
    fixtures = _write_fixture_verb(tmp_path, "echo_verb", _ECHO_VERB)
    env = _env(
        tmp_path,
        PYTHONPATH=str(fixtures),
        _NX_HOOK_TEST_VERB_OVERRIDE=json.dumps({"echo": "echo_verb"}),
    )
    proc = _run(["echo"], env, stdin="")
    assert proc.returncode == 0, proc.stderr
    assert json.loads(proc.stdout) == {"received": None}


def test_malformed_stdin_reaches_the_verb_as_a_none_payload(tmp_path: Path) -> None:
    fixtures = _write_fixture_verb(tmp_path, "echo_verb", _ECHO_VERB)
    env = _env(
        tmp_path,
        PYTHONPATH=str(fixtures),
        _NX_HOOK_TEST_VERB_OVERRIDE=json.dumps({"echo": "echo_verb"}),
    )
    proc = _run(["echo"], env, stdin="{not json")
    assert proc.returncode == 0, proc.stderr
    assert json.loads(proc.stdout) == {"received": None}


# -- a crashing verb: exits 0, prints nothing on stdout, logs to stderr ----

_BOOM_VERB = textwrap.dedent(
    """
    def run(payload):
        raise RuntimeError("kaboom")
    """
)


def test_a_raising_verb_exits_zero_and_prints_nothing_on_stdout(tmp_path: Path) -> None:
    fixtures = _write_fixture_verb(tmp_path, "boom_verb", _BOOM_VERB)
    env = _env(
        tmp_path,
        PYTHONPATH=str(fixtures),
        _NX_HOOK_TEST_VERB_OVERRIDE=json.dumps({"boom": "boom_verb"}),
    )
    proc = _run(["boom"], env, stdin="{}")
    assert proc.returncode == 0, proc.stderr
    assert proc.stdout == ""


def test_a_raising_verb_still_logs_diagnosably_to_stderr(tmp_path: Path) -> None:
    """Swallowed is not invisible: never_fail logs the crash, and the
    logging bridge (this module's own fix for the cnzei.2 defect class)
    must route that log to stderr/file, never to the stdout channel
    Claude Code parses."""
    fixtures = _write_fixture_verb(tmp_path, "boom_verb", _BOOM_VERB)
    env = _env(
        tmp_path,
        PYTHONPATH=str(fixtures),
        _NX_HOOK_TEST_VERB_OVERRIDE=json.dumps({"boom": "boom_verb"}),
    )
    proc = _run(["boom"], env, stdin="{}")
    assert proc.returncode == 0, proc.stderr
    assert "kaboom" in proc.stderr
    assert "boom" in proc.stderr


# -- ledger verbs propagate their exit code; everything else forces 0 ------

_EXIT_CODE_VERB = textwrap.dedent(
    """
    def run(payload):
        from nexus._hook_runtime._io import HookResult
        return HookResult(exit_code=3)
    """
)


def test_a_ledger_verb_propagates_its_exit_code(tmp_path: Path) -> None:
    fixtures = _write_fixture_verb(tmp_path, "exit_code_verb", _EXIT_CODE_VERB)
    env = _env(
        tmp_path,
        PYTHONPATH=str(fixtures),
        _NX_HOOK_TEST_VERB_OVERRIDE=json.dumps({"reconcile-probe": "exit_code_verb"}),
        _NX_HOOK_TEST_LEDGER_VERBS="reconcile-probe",
    )
    proc = _run(["reconcile-probe"], env, stdin="{}")
    assert proc.returncode == 3, proc.stderr


def test_a_non_ledger_verb_always_exits_zero_even_with_a_nonzero_result(tmp_path: Path) -> None:
    fixtures = _write_fixture_verb(tmp_path, "exit_code_verb", _EXIT_CODE_VERB)
    env = _env(
        tmp_path,
        PYTHONPATH=str(fixtures),
        _NX_HOOK_TEST_VERB_OVERRIDE=json.dumps({"not-a-ledger-verb": "exit_code_verb"}),
    )
    proc = _run(["not-a-ledger-verb"], env, stdin="{}")
    assert proc.returncode == 0, proc.stderr


# -- lazy resolution: the verb's own module loads before _io/logging setup -

_PROBE_VERB = textwrap.dedent(
    """
    import json
    import sys

    # Captured at MODULE LOAD time -- i.e. at the moment nx-hook's own
    # importlib.import_module() call reaches this file, before nx-hook has
    # wired up the shared _io/logging-bridge machinery for the dispatch.
    _SNAPSHOT = sorted(
        m for m in sys.modules
        if m == "click" or m.split(".")[0] == "nexus"
    )

    def run(payload):
        from nexus._hook_runtime._io import HookResult
        return HookResult(stdout=json.dumps({"modules_before_verb_import": _SNAPSHOT}))
    """
)


def test_the_verb_module_loads_before_the_shared_io_and_logging_machinery(tmp_path: Path) -> None:
    """Proves RDR-215 Approach item 2's 'resolves the verb to its module
    lazily': nx-hook imports the TARGET verb's module first and only then
    wires up nexus._hook_runtime._io -- so a verb inspecting its own import
    environment at load time sees neither it nor nexus.logging_setup.

    This docstring used to carry a caveat saying the RDR's 'imports only os,
    sys and json before dispatch' claim was unreachable, because the entry
    point's own module path forced nexus/hooks/__init__.py first. That was
    true while the entry point lived at nexus.hooks.entry; nexus-br31l moved
    it, and nexus/_hook_runtime/__init__.py is a docstring with no imports at
    all, so nothing is forced any more. The claim is now reachable and this
    test's assertions below are what hold it: nexus.logging_setup in
    particular is no longer imported on ANY dispatch, cheap or expensive,
    because main() stopped configuring logging up front.
    """
    fixtures = _write_fixture_verb(tmp_path, "probe_verb", _PROBE_VERB)
    env = _env(
        tmp_path,
        PYTHONPATH=str(fixtures),
        _NX_HOOK_TEST_VERB_OVERRIDE=json.dumps({"probe": "probe_verb"}),
    )
    proc = _run(["probe"], env, stdin="{}")
    assert proc.returncode == 0, proc.stderr
    modules = json.loads(proc.stdout)["modules_before_verb_import"]
    assert "nexus.cli" not in modules
    assert "click" not in modules
    assert "nexus._hook_runtime._io" not in modules
    assert "nexus.logging_setup" not in modules


# -- `nx hook` (the Click group) is untouched -------------------------------

def test_this_module_does_not_touch_the_click_hook_group() -> None:
    """nx-hook is a NEW console script beside `nx hook`'s Click verbs, not a
    replacement for them (RDR-215: 'no hooks.json entry names nx'; `nx
    hook` stays for a human at a terminal). This module must not import
    nexus.commands.hook or click -- doing so would be a sign this became a
    Click wrapper instead of the hand-rolled dispatcher it is meant to be.

    AST-based, not a substring grep: the module's own docstring names both
    ``nexus.commands.hook`` and ``Click`` in prose, which a naive substring
    check would misread as an import.
    """
    tree = ast.parse(Path(entry.__file__).read_text())
    imported = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            imported.update(alias.name for alias in node.names)
        elif isinstance(node, ast.ImportFrom) and node.module:
            imported.add(node.module)
    assert not any(m == "click" or m.startswith("click.") for m in imported), imported
    assert not any(
        m == "nexus.commands.hook" or m.startswith("nexus.commands.hook.") for m in imported
    ), imported


# -- a verb whose MODULE fails to import (bead nexus-q02nx.8 critique) ------

_UNIMPORTABLE_VERB = textwrap.dedent(
    """
    import this_module_does_not_exist_rdr215  # noqa: F401

    def run(payload):
        return None
    """
)


def test_a_verb_whose_module_cannot_import_still_exits_zero(tmp_path: Path) -> None:
    """A raising verb and an UNIMPORTABLE verb are different code paths.

    `importlib.import_module` used to sit outside the never-fail boundary,
    one line above it, so a verb module with an import-time error — a
    syntax error, a missing transitive dependency — propagated a raw
    traceback and exited 1. Reproduced before the fix: EXIT=1 with a
    traceback on stderr, directly contradicting entry.py's own documented
    "every hook verb exits 0" contract, which is the property the bash
    layer's deliberately absent `set -e` guaranteed.

    test_a_raising_verb_exits_zero... covers a verb that imports fine and
    then raises; this covers one that never imports at all. Phase 2's
    ledger verbs are new modules that shell out to bd and git, so this is
    the likelier of the two failures there.
    """
    fixtures = _write_fixture_verb(tmp_path, "unimportable_verb", _UNIMPORTABLE_VERB)
    env = _env(
        tmp_path,
        PYTHONPATH=str(fixtures),
        _NX_HOOK_TEST_VERB_OVERRIDE=json.dumps({"nope": "unimportable_verb"}),
    )
    proc = _run(["nope"], env, stdin="{}")
    assert proc.returncode == 0, (
        f"an unimportable verb module must not break the exit-0 contract: {proc.stderr}"
    )
    assert proc.stdout == "", "the decision channel must stay clean"


def test_an_unimportable_verb_is_still_diagnosable_on_stderr(tmp_path: Path) -> None:
    """Swallowed is not invisible — the same standard the raising-verb case
    is held to. Without this, an import-time failure would be a hook that
    silently does nothing forever."""
    fixtures = _write_fixture_verb(tmp_path, "unimportable_verb", _UNIMPORTABLE_VERB)
    env = _env(
        tmp_path,
        PYTHONPATH=str(fixtures),
        _NX_HOOK_TEST_VERB_OVERRIDE=json.dumps({"nope": "unimportable_verb"}),
    )
    proc = _run(["nope"], env, stdin="{}")
    assert proc.returncode == 0, proc.stderr
    assert "this_module_does_not_exist_rdr215" in proc.stderr, (
        "the log must name the module that could not be imported"
    )


# -- a crashed LEDGER verb is distinguishable from a clean one (Sam, 2026-09-19) --

_CLEAN_LEDGER_VERB = textwrap.dedent(
    """
    from nexus._hook_runtime._io import HookResult

    def run(payload):
        return HookResult(exit_code=0)
    """
)


def test_a_crashed_ledger_verb_exits_the_reserved_code(tmp_path: Path) -> None:
    """A ledger verb's exit code IS its contract, so a crash must not wear a
    vocabulary value.

    undeclared uses 0/1/2/3, reconcile 0/2/4, census 0/1, and a caller
    branches on every one. Before this, a crashed verb exited 0 —
    indistinguishable from a clean reconcile, which bead .13 reads as
    "nothing stranded". That is a silent miss in the subsystem built to
    catch silent misses. 70 is sysexits EX_SOFTWARE and collides with no
    ledger vocabulary.
    """
    fixtures = _write_fixture_verb(tmp_path, "boom_verb", _BOOM_VERB)
    env = _env(
        tmp_path,
        PYTHONPATH=str(fixtures),
        _NX_HOOK_TEST_VERB_OVERRIDE=json.dumps({"reconcile": "boom_verb"}),
        _NX_HOOK_TEST_LEDGER_VERBS="reconcile",
    )
    proc = _run(["reconcile"], env, stdin="{}")
    assert proc.returncode == 70, (
        f"a crashed ledger verb must not exit a vocabulary value: {proc.returncode}"
    )


def test_a_clean_ledger_verb_still_exits_its_own_code(tmp_path: Path) -> None:
    """The reserved code must not swallow the contract it protects."""
    fixtures = _write_fixture_verb(tmp_path, "clean_verb", _CLEAN_LEDGER_VERB)
    env = _env(
        tmp_path,
        PYTHONPATH=str(fixtures),
        _NX_HOOK_TEST_VERB_OVERRIDE=json.dumps({"reconcile": "clean_verb"}),
        _NX_HOOK_TEST_LEDGER_VERBS="reconcile",
    )
    assert _run(["reconcile"], env, stdin="{}").returncode == 0


def test_a_crashed_NON_ledger_verb_still_exits_zero(tmp_path: Path) -> None:
    """Unchanged, and deliberately: for a non-ledger hook a crash IS the
    hook choosing to say nothing, which is what failing open means. The
    reserved code applies only where an exit code is a contract."""
    fixtures = _write_fixture_verb(tmp_path, "boom_verb", _BOOM_VERB)
    env = _env(
        tmp_path,
        PYTHONPATH=str(fixtures),
        _NX_HOOK_TEST_VERB_OVERRIDE=json.dumps({"plain": "boom_verb"}),
    )
    assert _run(["plain"], env, stdin="{}").returncode == 0


def test_the_reserved_code_is_outside_every_ledger_vocabulary(tmp_path: Path) -> None:
    """A reserved code that collided with a real verdict would be worse than
    none: it would silently become that verdict."""
    vocabularies = {0, 1, 2, 3, 4}  # undeclared 0/1/2/3, reconcile 0/2/4, census 0/1
    assert _LEDGER_CRASH_EXIT not in vocabularies
