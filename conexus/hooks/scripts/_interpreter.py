#!/usr/bin/env python3
# SPDX-License-Identifier: AGPL-3.0-or-later
"""Re-exec a plugin-resident hook under an interpreter that can serve it.

RDR-215 (bead nexus-q02nx.21) re-declared the five plugin-resident Python
hooks in exec form, ``{"command": "python3", "args": [<script>]}``, which
discards the resolution ``_run_python_hook.sh`` performed. Sam's ruling of
2026-09-19 was to put that resolution back in Python rather than keep the
bash: no bash in the hook path, and no dependence on PATH order.

TWO THINGS ARE LOST WITH THE SHIM, not one, and both are restored here:

* **The interpreter floor.** Four of the five scripts refuse to run under
  Python 3.12-, measured, not read off their guards -- only one spells the
  guard itself, the rest inherit it through ``_lib`` / ``_endpoint_resolve``.
  On a box where ``/usr/bin/python3`` (3.9 on macOS) wins PATH,
  ``phase_review_close_requires_gate`` -- the routing framework's only
  ``fail_closed`` rule -- exits 1 with no envelope. That is not a deny:
  Claude Code treats it as a non-blocking error and the close gate FAILS
  OPEN, the exact inversion the command tier was chosen to prevent.

* **The interpreter's ``nexus``.** The generation python is the only
  interpreter on a box guaranteed to import ``nexus``, which
  ``phase_review_close_requires_gate`` needs for
  ``nexus.session.find_immediate_claude_pid``. Under a bare Homebrew
  python3.13 that import fails, the hook degrades to ``os.getppid()``, and
  it misreads quietly -- the nexus-owna8 class, where ``rdr_hook`` reported
  a fully indexed tree as NOT indexed on every session start while its own
  tests passed in the dev venv. This one bites at 3.13 too, so a preamble
  gated on ``version_info < (3, 12)`` alone would leave it live on the
  box that found it.

So the resolution runs unconditionally and mirrors ``_run_python_hook.sh``'s
order exactly.

WHAT IT COSTS, measured rather than asserted (nexus-q02nx.21 review; an
earlier version of this paragraph claimed "one ``os.stat`` chain and no
process", and both halves were false). Each candidate is checked by
RUNNING it, because an executable bit does not mean a partially reaped or
wrong-arch interpreter will start. A candidate that is already
``sys.executable`` is returned without a probe and without an exec, which
is the free path and the common one for a hook the generation's own
python launched.

Measured on this box, median of 10, ``VIRTUAL_ENV`` unset:

    launched by the generation python (the skip)   17.8 ms
    marker set, resolution short-circuited         23.2 ms
    launched by PATH python3, must re-exec         57.8 ms

So the skip is genuinely free -- it comes in BELOW the short-circuit
baseline, because the generation's 3.12 starts faster than the Homebrew
3.13 the other two rows run under. The cost lands only where the
interpreter actually has to change: about 35 ms, being one probe plus a
second CPython start. The bash shim did the same probes and started bash
instead, so the delta against it is roughly one interpreter startup.

Two carriers fire often -- ``mailbox_drain`` on every UserPromptSubmit,
the two routing hooks on every Bash tool call -- so that cost is real and
is the price of not letting PATH order decide which interpreter serves a
fail-closed gate.

The probe budget sits UNDER the hooks' own declared timeout. Those
entries declare ``"timeout": 5`` in hooks.json; an unbudgeted chain of
three 10-second probes could reach 30 seconds, and a hook killed by its
own timeout writes no envelope, which for
``phase_review_close_requires_gate`` is not a deny but a FAIL-OPEN --
precisely the inversion this module exists to prevent. So a probe gets
:data:`_PROBE_TIMEOUT_S` and the whole resolution gets
:data:`_RESOLVE_BUDGET_S`, after which it gives up and stays put.

Stdlib only, and it must stay parseable on Python 3.9: it is imported by
scripts whose whole purpose is to be reachable from a 3.9 interpreter.
"""

from __future__ import annotations

import os
import shutil
import subprocess
import sys
import time

_PROBE_TIMEOUT_S = 1.5
"""Per-probe ceiling. A healthy interpreter starts in tens of ms; a probe
that takes longer than this is a sick candidate, and waiting on it costs
more than skipping it."""

_RESOLVE_BUDGET_S = 3.0
"""Whole-resolution ceiling, under the 5s these hooks declare in
hooks.json. Past it :func:`resolve` returns None and the caller stays on
the interpreter it has, which at worst reproduces the pre-preamble
behaviour -- where an unbudgeted chain would instead let the hook be
killed with no envelope."""

_MARKER = "NX_HOOK_INTERPRETER_REEXEC"
"""Set across the ``execv`` so a second pass can never loop.

A resolution that picks an interpreter which then resolves differently --
a wrong-arch generation python, a ``VIRTUAL_ENV`` that moves -- would
otherwise exec forever.
"""


def _is_current(exe: str) -> bool:
    """True when *exe* is the interpreter already running this code.

    Checked before any probe: there is nothing to verify about an
    interpreter that is demonstrably working, and nothing to exec into.
    """
    try:
        return os.path.realpath(exe) == os.path.realpath(sys.executable)
    except OSError:
        return False


def _runs(exe: str, *, probe: str = "") -> bool:
    """True when ``exe`` is executable and runs ``probe`` (default: nothing).

    An executable bit is not enough: a partially reaped generation, or one
    built for another architecture, is executable and does not start.
    """
    if not exe or not os.path.isfile(exe) or not os.access(exe, os.X_OK):
        return False
    if _is_current(exe):
        return True
    try:
        return subprocess.run(
            [exe, "-c", probe or ""],
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            timeout=_PROBE_TIMEOUT_S,
        ).returncode == 0
    except (OSError, subprocess.SubprocessError):
        return False


def _venv_python_for_this_checkout() -> str | None:
    """An active venv whose ``nexus`` is the checkout under the cwd.

    A developer's editable install must win over the installed generation,
    or every nexus-importing hook silently reads production while the tree
    is being edited (critique [24988]). A stale ``VIRTUAL_ENV`` from another
    worktree, or one with a packaged ``nexus``, does not qualify.
    """
    venv = os.environ.get("VIRTUAL_ENV")
    if not venv:
        return None
    exe = os.path.join(venv, "bin", "python")
    probe = (
        "import os, sys, nexus; "
        "sys.exit(0 if os.path.realpath(nexus.__file__)"
        ".startswith(os.path.realpath(os.getcwd()) + os.sep) else 1)"
    )
    return exe if _runs(exe, probe=probe) else None


def _generation_python() -> str | None:
    """The installed conexus generation's own python, if it runs.

    ``HOME`` may be absent under a scrubbed env; the run check keeps a
    partially reaped or wrong-arch generation from being exec'd into.
    """
    tools = os.environ.get("NX_TOOLS_DIR")
    if not tools:
        home = os.environ.get("HOME")
        if not home:
            return None
        tools = os.path.join(home, ".local", "share", "nexus", "tools")
    exe = os.path.join(tools, "current", "bin", "python")
    return exe if _runs(exe) else None


def resolve() -> str | None:
    """The interpreter this hook should run under, or None to stay put.

    Mirrors ``_run_python_hook.sh``: ``$NX_HOOK_PYTHON``, then a venv
    holding this checkout's ``nexus``, then the generation python, then
    ``python3.13`` and ``python3.12`` by name. Plain ``python3`` is
    deliberately absent -- we are already running under it, and falling
    through lets the script's own version guard print its error.
    """
    deadline = time.monotonic() + _RESOLVE_BUDGET_S
    explicit = os.environ.get("NX_HOOK_PYTHON")
    if explicit and _runs(explicit):
        return explicit
    if time.monotonic() >= deadline:
        return None
    venv = _venv_python_for_this_checkout()
    if venv:
        return venv
    if time.monotonic() >= deadline:
        return None
    generation = _generation_python()
    if generation:
        return generation
    if time.monotonic() >= deadline:
        return None
    for name in ("python3.13", "python3.12"):
        found = shutil.which(name)
        if found:
            return found
    return None


def reexec_if_needed() -> None:
    """Re-exec this script under :func:`resolve`'s winner, if that differs.

    Call it before importing anything that needs 3.12 or ``nexus`` -- in
    practice, before ``_lib`` / ``_endpoint_resolve``. A resolution failure
    of any kind returns silently: the caller's own version guard is the
    backstop, and a hook that cannot re-exec must still get to print why.
    """
    if os.environ.get(_MARKER):
        return
    target = resolve()
    if not target:
        return
    try:
        if os.path.realpath(target) == os.path.realpath(sys.executable):
            return
        script = os.path.abspath(sys.argv[0])
        os.environ[_MARKER] = "1"
        os.execv(target, [target, script, *sys.argv[1:]])
    except OSError:
        os.environ.pop(_MARKER, None)
        return
