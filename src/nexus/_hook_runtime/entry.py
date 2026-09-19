# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (c) 2026 Hal Hildebrand. All rights reserved.
"""nx-hook: the command-tier entry point for ported Claude Code hooks (RDR-215).

``hooks.json``'s ``SessionStart`` entries (and a handful of shell-form ``nx``
invocations, RDR-215 Approach item 6) cannot go through the tool tier
(``nx-mcp``'s ``hook_<name>`` tools, ``src/nexus/mcp/hooks.py``): they run
before any MCP server is guaranteed connected. This module is the one
process those events spawn instead.

**Why not ``nx``.** ``nx`` registers 38 command modules eagerly at import
time and costs about 0.8 s to do it (research-5, T2
``nexus_rdr/215-research-5``); routing a hook through it would add that cost
to every ``SessionStart``. ``nexus.commands.hook`` alone -- the Click group
that already carries ``session-start`` -- costs 0.080 s on its own, most of
that Click's own import. ``nx-hook`` pays neither: this module is not a
Click command, has no group to register into, and never imports
``nexus.cli``. Built like ``nx-session-end-launcher``
(``src/nexus/_session_end_launcher.py``), whose own docstring records the
same race this module is built to avoid on the ``SessionEnd`` side: only
``os``, ``sys`` and ``json`` are imported at module scope; everything else
-- ``importlib``, the logging bridge, ``nexus._hook_runtime._io``, and the verb's own
module -- is deferred into :func:`main`, so a verb that is never invoked
never pays for what it would have imported.

**Why this module is not in ``nexus.hooks``.** Python runs a package's
``__init__`` before it can reach any module inside it, and
``nexus/hooks/__init__.py`` imports ``structlog`` and ``nexus.session`` at
module scope. While this file lived there, resolving the console script
ran all of that first: measured 0.06 s, against 0.01 s for a bare ``import
nexus`` (nexus-br31l, dev Mac, median of 9 per module). No ordering
discipline inside THIS file could have avoided it -- which is why the
fix was to move, not to reorder.

Moving this file alone would not have been enough, and the earlier
nexus-q02nx.2/.3 critique was right to call that a red herring at the
time: :func:`main` imports ``never_fail`` and ``read_payload`` on every
real dispatch, so any genuine verb paid the package ``__init__`` anyway
through ``_io``. What changed is that ``_io`` and ``_config`` moved out
with it, into :mod:`nexus._hook_runtime`, whose ``__init__`` is a
docstring and nothing else. The cheap path is now cheap all the way
through.

That margin is the whole point, and it is only visible on the cheap
hooks. ``phase_review_close_requires_gate`` is stdlib-only and costs
0.03 s end to end as bash on this box -- 0.04 s in bead .2's harness --
so a port paying 0.06 s to reach
``never_fail`` would have been a hot-path regression rather than the
speedup RDR-215 promises. ``session-start`` is the other extreme and is
deliberately not optimised here: it genuinely needs ``nexus.session``,
pays for it legitimately, and its own 187-221 ms is mostly real I/O --
a bounded tuple-surface probe and a stale-MCP-host scan -- against the
~904 ms ``nx hook session-start`` Click path it replaces (bead
nexus-q02nx.5, median of 10). Judge this module by the close gate, not
by ``session-start`` -- but on the right number.

What 0.02 s measures is the dispatch FLOOR -- a synthetic stdlib-only
verb through the real entry point. That is the figure the close gate's
COMMON path will pay, the one that runs on every Bash call and exits
early via ``_lib.allow()``. Its narrow phase-review branch additionally
imports ``nexus.session`` and shells out to ``bd show``; that cost is
real, is its own, and stays unmeasured until the port lands.

``tests/hooks/test_hook_runtime_thin.py`` keeps it that way: it asserts
this package's ``__init__`` imports nothing and that the modules beside
it import only the standard library at module scope. Without it the
property rots the first time someone adds a convenient import, and no
functional test would notice -- the wrong import makes hooks slower,
never wrong.

**Verb resolution.** :data:`VERB_TABLE` maps a verb name to the dotted
module path of the object that implements it. Every entry's module defines
one function, ``run(payload: dict | None) -> HookResult``
(:class:`nexus._hook_runtime._io.HookResult`) -- the exact function the tool tier's
``hook_<name>`` tools call too (RDR-215 Approach item 4: "one implementation,
two entries"). The first real verb, ``session-start`` (nexus-q02nx.5), is
registered below; it never reaches the tool tier at all, since
``SessionStart`` fires before any MCP server is guaranteed connected
(Approach item 1). The ledger verbs
(``expect``/``start``/``census``/``undeclared``/``reconcile``) are Phase 2.
Resolution happens once, per invocation, via ``importlib`` -- there is no
eager import of every registered verb's module merely because one of them
is being dispatched.

**Exit codes.** Every hook verb exits 0, matching the bash layer, where a
block or deny is encoded in the JSON body on stdout and never in the exit
status (RDR-215 Contracts). The ledger verbs are the one exception: their
callers branch on the code ``run()`` returns (``undeclared``: 0/1/2/3;
``reconcile``: 0/2/4; ``census``: 0/1; ``expect``/``start``: 2 on invalid
input), so :data:`LEDGER_VERBS` names which verbs propagate their
``HookResult.exit_code`` instead of having it forced to 0. An unknown or
missing verb is neither of those: it is a dispatch failure -- a
misconfigured ``hooks.json`` entry, or a typo -- and gets its own
diagnosable failure (a one-line message on stderr, exit 2), not the silent
"hook chose to do nothing" contract every real verb gets via
:func:`nexus._hook_runtime._io.never_fail`.
"""
from __future__ import annotations

import json
import os
import sys

#: Hook verb name -> dotted module path. Each module defines
#: ``run(payload: dict | None) -> HookResult``. Resolved and imported
#: lazily, per verb, at dispatch time -- never eagerly -- so nx-hook's own
#: cost never scales with how many verbs are registered. ``session-start``
#: (nexus-q02nx.5) is the first real port; populated incrementally as later
#: beads in the epic land.
VERB_TABLE: dict[str, str] = {
    "session-start": "nexus.hooks.session_start_verb",
}

#: Verbs whose exit code nx-hook must propagate from ``run()`` instead of
#: forcing 0 -- the ledger's callers branch on it (RDR-215 Contracts).
#: Populated alongside VERB_TABLE as those verbs are ported (Phase 2).
LEDGER_VERBS: frozenset[str] = frozenset()

#: Test-only dispatch override, read solely by
#: ``tests/hooks/test_nx_hook_entry.py``. A JSON object string mapping verb
#: name to dotted module path, e.g. ``{"probe": "probe_verb"}``. Exists
#: because this bead builds the dispatch mechanism before any real verb is
#: registered (nexus-q02nx.5 ports the first one) -- it lets the test suite
#: exercise dispatch against the REAL entry point (an unknown verb, a verb
#: that raises, a verb that inspects its own import environment) without
#: editing VERB_TABLE. Never set outside a test process.
_TEST_VERB_OVERRIDE_ENV = "_NX_HOOK_TEST_VERB_OVERRIDE"

#: Test-only ledger-membership override, same rationale as
#: :data:`_TEST_VERB_OVERRIDE_ENV`: a comma-separated list of verb names to
#: treat as ledger verbs for :data:`LEDGER_VERBS` purposes, so the
#: exit-code-propagation contract is provable before a real ledger verb
#: exists (Phase 2).
_TEST_LEDGER_OVERRIDE_ENV = "_NX_HOOK_TEST_LEDGER_VERBS"


def _resolve_verb_module(verb: str) -> str | None:
    """Return the dotted module path for *verb*, or ``None`` if unknown.

    Pure lookup: no import happens here. ``json`` is used here, before real
    dispatch, to parse the test-only override table above -- the one thing
    this module reads before it knows whether a real dispatch is even
    happening.
    """
    if verb in VERB_TABLE:
        return VERB_TABLE[verb]
    raw = os.environ.get(_TEST_VERB_OVERRIDE_ENV)
    if not raw:
        return None
    try:
        overrides = json.loads(raw)
    except Exception:  # noqa: BLE001 — malformed test-only override must never crash nx-hook
        return None
    if not isinstance(overrides, dict):
        return None
    module = overrides.get(verb)
    return module if isinstance(module, str) else None


def _is_ledger_verb(verb: str) -> bool:
    if verb in LEDGER_VERBS:
        return True
    raw = os.environ.get(_TEST_LEDGER_OVERRIDE_ENV, "")
    return verb in {name for name in raw.split(",") if name}


def main() -> None:
    """Dispatch ``sys.argv[1]`` to its verb's ``run()``.

    Deliberately hand-rolled, not Click: the whole reason this console
    script exists beside ``nx`` is to avoid paying for a command framework
    on every hook event (see the module docstring). Order matters here: the
    verb's own module is imported FIRST, via ``importlib``, before ``_io``
    -- so a verb module that snapshots its own import environment at load
    time sees only what was true before the shared machinery existed, never
    after.

    Nothing here configures logging. A verb that logs through an ambient
    ``structlog.get_logger()`` calls
    :func:`nexus._hook_runtime._io.configure_hook_logging` itself, because
    only it knows whether it needs a sink; doing it here charged every
    dispatch 0.06 s for one most verbs never use (nexus-br31l). The
    envelope does not depend on that choice -- see the stdout guard below.
    """
    argv = sys.argv[1:]
    if not argv:
        sys.stderr.write("nx-hook: missing verb argument\n")
        sys.exit(2)
    verb = argv[0]
    module_path = _resolve_verb_module(verb)
    if module_path is None:
        sys.stderr.write(
            f"nx-hook: unknown verb {verb!r} -- no hook is registered under that name\n"
        )
        sys.exit(2)

    import importlib  # noqa: PLC0415 — deferred: only a real dispatch pays this

    # stdout IS the decision channel: Claude Code parses it as the hook's JSON.
    # Anything else a dispatch writes there corrupts a decision and reads as a
    # hook malfunction (the nexus D9 defect class). Rather than pre-emptively
    # configure structlog to prevent one instance of that -- which is what this
    # module used to do, at 0.06 s per dispatch for a sink most verbs never
    # write to -- send everything except the envelope to stderr for the whole
    # dispatch, and keep the real handle here to write the envelope through.
    #
    # Two levels, because one is not enough. Rebinding ``sys.stdout`` catches
    # in-process Python writes: an unconfigured structlog logger, a ``print()``
    # in a verb, a library's import banner. It does NOT catch a child process,
    # which inherits the real OS fd 1 and never consults this interpreter's
    # ``sys`` module -- so a verb shelling out lands straight in the pipe Claude
    # Code is parsing. No verb does that today (``session-start``'s one
    # ``subprocess.run`` captures its output), but the Phase 2 ledger verbs
    # named above are ports of bash scripts that shell out to ``bd`` and
    # ``git``, which is exactly the shape that would hit it. So fd 1 is
    # redirected too, and restored in the ``finally``.
    #
    # ``fileno()`` raises when stdout is not a real file -- pytest's capture, an
    # ``io.StringIO`` -- and then the fd half is simply skipped: there is no OS
    # fd for a child to inherit in that case either, so the Python-level rebind
    # is the whole guarantee and is sufficient.
    real_stdout = sys.stdout
    saved_stdout_fd: int | None = None
    guarded_fd: int | None = None
    try:
        guarded_fd = sys.stdout.fileno()
        stderr_fd = sys.stderr.fileno()
    except Exception:  # noqa: BLE001 — not a real fd (captured/StringIO); Python-level rebind still applies
        guarded_fd = None
    else:
        sys.stdout.flush()
        saved_stdout_fd = os.dup(guarded_fd)
        os.dup2(stderr_fd, guarded_fd)

    sys.stdout = sys.stderr
    try:
        # THE VERB MODULE IS IMPORTED FIRST, AND THE FAILURE IS CARRIED.
        # Order is load-bearing in both directions here, which is why this is
        # not simply wrapped in never_fail:
        #
        #  - the verb's module must import BEFORE nexus._hook_runtime._io, so
        #    a verb inspecting its own import environment sees neither _io nor
        #    nexus.logging_setup (RDR-215 Approach item 2; held by
        #    test_the_verb_module_loads_before_the_shared_io_and_logging_machinery).
        #    Importing never_fail first to guard the import would invert that.
        #  - but the import must not ESCAPE. It used to sit unguarded one line
        #    above never_fail, so a verb module with an import-time error --
        #    a syntax error, a missing transitive dependency -- propagated a
        #    raw traceback and exited 1, contradicting this module's own
        #    "every hook verb exits 0" contract (bead nexus-q02nx.8 critique,
        #    reproduced: EXIT=1 with a traceback). That is the property the
        #    bash layer's deliberately absent `set -e` guaranteed.
        #
        # So the import is caught here and RE-RAISED inside the boundary,
        # which keeps one error path rather than two: never_fail logs it the
        # same way it logs a crash inside run(). `except Exception` and not
        # BaseException deliberately mirrors never_fail's own posture, so a
        # KeyboardInterrupt during import still propagates.
        #
        # Phase 2 meets this first: the ledger verbs are new modules that
        # shell out to bd and git, so an import-time failure in one of them
        # is a likely early defect rather than a hypothetical.
        module = None
        failed_import: Exception | None = None
        try:
            module = importlib.import_module(module_path)
        except Exception as exc:  # noqa: BLE001 — re-raised inside never_fail below
            failed_import = exc

        from nexus._hook_runtime._io import never_fail, read_payload  # noqa: PLC0415 — deferred: only a real dispatch pays this

        def _dispatch():
            if failed_import is not None:
                raise failed_import
            return module.run(read_payload(sys.stdin))

        result = never_fail(_dispatch, verb)
    finally:
        sys.stdout = real_stdout
        if saved_stdout_fd is not None and guarded_fd is not None:
            sys.stderr.flush()
            os.dup2(saved_stdout_fd, guarded_fd)
            os.close(saved_stdout_fd)

    if result.stdout is not None:
        real_stdout.write(result.stdout + "\n")
        real_stdout.flush()

    sys.exit(result.exit_code if _is_ledger_verb(verb) else 0)


if __name__ == "__main__":
    main()
