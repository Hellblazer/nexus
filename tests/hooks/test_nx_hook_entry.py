# SPDX-License-Identifier: AGPL-3.0-or-later
"""Tests for ``nexus.hooks.entry`` -- the ``nx-hook`` command-tier dispatch
mechanism (RDR-215 Phase 1, bead nexus-q02nx.2).

These tests exercise the dispatch mechanism itself against the REAL entry
point, using the module's own test-only override env vars
(``_NX_HOOK_TEST_VERB_OVERRIDE`` / ``_NX_HOOK_TEST_LEDGER_VERBS``,
documented on the module) to register throwaway fixture verbs for the
duration of one subprocess call, rather than the one real verb
:data:`VERB_TABLE` carries (``session-start``, nexus-q02nx.5 -- its own
coverage lives in ``tests/hooks/test_session_start_verb.py``). Every
scenario spawns ``python -m nexus.hooks.entry <verb>`` -- exactly the
callable the ``nx-hook`` console script's generated stub also calls
(``nexus.hooks.entry:main``), so this needs no prior
``scripts/reinstall-tool.sh`` run to be meaningful.
"""
from __future__ import annotations

import ast
import json
import os
import subprocess
import sys
import textwrap
from pathlib import Path

from nexus.hooks import entry

_ENTRY_ARGV = [sys.executable, "-m", "nexus.hooks.entry"]


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
        from nexus.hooks._io import HookResult
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
        from nexus.hooks._io import HookResult
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
        from nexus.hooks._io import HookResult
        return HookResult(stdout=json.dumps({"modules_before_verb_import": _SNAPSHOT}))
    """
)


def test_the_verb_module_loads_before_the_shared_io_and_logging_machinery(tmp_path: Path) -> None:
    """Proves RDR-215 Approach item 2's 'resolves the verb to its module
    lazily': nx-hook imports the TARGET verb's module first and only then
    wires up nexus.hooks._io / nexus.logging_setup -- so a verb inspecting
    its own import environment at load time sees neither. Also the closest
    thing to the RDR's 'imports only os, sys and json before dispatch'
    claim that is actually reachable given the entry point's own module
    path (nexus.hooks.entry) forces nexus/hooks/__init__.py first; see the
    module docstring's own note on that asymmetry. Never nexus.cli or
    click, in any case -- that is the one claim this test can make
    unconditionally.
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
    assert "nexus.hooks._io" not in modules
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
