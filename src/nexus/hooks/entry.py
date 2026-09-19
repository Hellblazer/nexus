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
-- ``importlib``, the logging bridge, ``nexus.hooks._io``, and the verb's own
module -- is deferred into :func:`main`, so a verb that is never invoked
never pays for what it would have imported.

One asymmetry from that template is worth naming rather than glossing over,
and it means the bead's own "stays under 0.042 s" verification target is
NOT met as measured (nexus-q02nx.2 report, 2026-09-18, installed generation,
median of 10 runs): ``nx-session-end-launcher`` is
``nexus._session_end_launcher``, a direct child of the ``nexus`` package,
whose ``__init__.py`` is a one-line SPDX header. This module is
``nexus.hooks.entry``, and Python's import system runs
``nexus/hooks/__init__.py`` before it can reach this file at all, as an
unavoidable consequence of the entry point RDR-215 names (``"nx-hook =
nexus.hooks.entry:main"``, Technical Design's "The command tier") -- no
ordering discipline inside THIS file can change that. Measured: bare
interpreter + ``import nexus`` is ~0.013 s; + ``import nexus.hooks`` jumps
to ~0.076 s; + ``import nexus.hooks.entry`` is ~0.075 s (i.e. this module
adds nothing measurable beyond the forced package import). The jump is not
``nexus.session`` -- it is ``import structlog`` alone, which costs ~0.073 s
on its own here (structlog 25.5.0 pinned in ``uv.lock``): its ``__init__``
unconditionally pulls in ``structlog.dev`` -> ``rich.traceback`` ->
``pygments`` -> an ``importlib.metadata`` entry-point scan
(``python -X importtime -c "import structlog"`` shows the breakdown).
That means the cost is not really about WHERE this module lives; it is
that ANY real verb dispatch pays it anyway the moment it imports
``nexus.hooks._io`` (which imports ``structlog`` too), regardless of this
module's own package nesting. Relocating this file outside ``nexus.hooks``
would only cheapen the already-fast unknown-verb path, not a real
dispatch. Both the console-script placement and structlog's own import
behavior are outside this bead's scope to change; recorded here so a
future bead does not re-derive it from scratch. The invariant THIS module
does control, and the one the tests in ``test_nx_hook_entry.py`` actually
prove: no verb's module is imported until dispatch has resolved which one
is wanted, ``nexus.cli`` and ``click`` are never imported at all, and the
shared ``_io``/logging machinery is wired up only once a real verb is
confirmed -- never merely to discover that one is not.

**Verb resolution.** :data:`VERB_TABLE` maps a verb name to the dotted
module path of the object that implements it. Every entry's module defines
one function, ``run(payload: dict | None) -> HookResult``
(:class:`nexus.hooks._io.HookResult`) -- the exact function the tool tier's
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
:func:`nexus.hooks._io.never_fail`.
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


def _configure_hook_logging() -> None:
    """Bridge structlog to stderr + ``<config>/logs/hook.log`` before any
    other nexus import can log.

    structlog's default, unconfigured ``PrintLoggerFactory`` writes to
    **stdout** (confirmed against structlog's own source; see
    ``nexus.logging_setup.configure_logging``'s docstring for the full
    history) -- the exact channel a hook's JSON envelope goes out on and
    Claude Code parses. ``_io.read_payload``'s parse-failure log and
    ``_io.never_fail``'s crash-swallow log both go through the ambient
    ``structlog.get_logger()``, so without this bridge a malformed stdin
    payload or a crashing verb would print a stray log line ahead of (or
    instead of) the hook's own output. This is the identical defect class
    ``conexus/hooks/scripts/_hook_logging.py`` documents and fixes for the
    bash-launched Python hooks (nexus-cnzei.2); this is that fix's
    equivalent for the command tier. Best-effort: an interpreter missing
    ``nexus.logging_setup``, or a genuine bug in the logging setup itself,
    must never turn into a hook failure -- there is nothing useful to
    report if this call can't run.
    """
    try:
        from nexus.logging_setup import configure_logging  # noqa: PLC0415 — deferred: only a real dispatch pays this

        configure_logging(mode="hook")
    except Exception:  # noqa: BLE001 — best-effort; must never break the calling hook
        pass


def main() -> None:
    """Dispatch ``sys.argv[1]`` to its verb's ``run()``.

    Deliberately hand-rolled, not Click: the whole reason this console
    script exists beside ``nx`` is to avoid paying for a command framework
    on every hook event (see the module docstring). Order matters here:
    the verb's own module is imported FIRST, via ``importlib``, before the
    shared ``_io``/logging-bridge machinery is wired up -- so a verb module
    that snapshots its own import environment at load time sees only what
    was true before that machinery existed, never after.
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

    module = importlib.import_module(module_path)

    _configure_hook_logging()
    from nexus.hooks._io import never_fail, read_payload  # noqa: PLC0415 — deferred: only a real dispatch pays this

    payload = read_payload(sys.stdin)
    result = never_fail(lambda: module.run(payload), verb)

    if result.stdout is not None:
        sys.stdout.write(result.stdout + "\n")

    sys.exit(result.exit_code if _is_ledger_verb(verb) else 0)


if __name__ == "__main__":
    main()
