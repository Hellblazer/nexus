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
``HookResult.exit_code`` instead of having it forced to 0.

A MISSING verb argument (``hooks.json`` invoking ``nx-hook`` with no verb
at all) is a genuine invocation error and stays exit 2 with a one-line
diagnostic -- that shape is always this CLI's own misconfiguration to fix.

An UNKNOWN verb -- one this CLI's :data:`VERB_TABLE` has never heard of --
exits 0 instead of 2 for a NON-ledger verb (2 for a ledger-SHAPED one --
see below), still with a named stderr diagnostic AND, for the exit-0 case,
a ``systemMessage`` written to the real stdout envelope so the diagnostic
actually reaches the person running the session (stderr from a hook does
not; see the ``main`` dispatch code). The plugin marketplace updates
``hooks.json`` before an installed ``nx`` CLI upgrades to the wheel that
declares a new verb (the CLI upgrade itself runs through the
version-lockstep hook, on a session's own cadence): a plugin bump can
therefore name a verb this CLI does not register yet. Exiting 2 for that
case makes every ``UserPromptSubmit`` and every ``PreToolUse`` on ``Bash``
refuse outright for the whole session, with no way to self-heal -- measured
at the 7.58.0 release blocker (nexus-t9klx): seven such entries reached
``hooks.json`` before the CLI that registers them shipped, and the
version-lockstep hook that upgrades the CLI was one of the seven, so
nothing could recover on its own.

**Be plain about what fail-open COSTS for a DECIDING gate.** Two of the
seven verbs nexus-t9klx measured are deciding gates --
``pre-close-verification`` and ``phase-review-close-gate`` -- registered
in ``nexus.mcp.hooks._NEVER_TOOL_TIER`` precisely because a crash at the
tool boundary renders as allow and these two rules must still deny. This
exit-0 path is a THIRD way for that same failure shape to occur, at the
command-tier boundary instead: on a CLI older than the plugin, an unknown
deciding-gate verb means the gate LITERALLY DOES NOT RUN and the action it
would have gated -- a ``bd close`` with no review marker, a phase boundary
closed without its cross-walk -- is ALLOWED, exactly as if the gate had
been deleted. This is NOT "the same contract every real verb gets" (an
earlier draft of this docstring claimed that, and it was wrong): a real
verb's ``never_fail`` failure is a crash inside code that ran; this is code
that never ran at all, and calling the two the same thing hides the
difference between "the gate tried and gave up" and "the gate was never
invoked." The trade actually being made is: a session blocked outright
with no self-heal path (exit 2, the 7.58.0 incident) against a gate that
is silently absent for the minutes-to-hours between the plugin update
landing and the session's own version-lockstep hook finishing its
detached upgrade. That window is bounded and self-closing; a blocked
session is not. Nothing here narrows the window further than that -- it is
the accepted cost of choosing recoverable over safe for this one case, not
a claim that the gate is somehow still enforced.

A ledger-SHAPED unknown verb (its name starts with
:data:`_LEDGER_VERB_PREFIX`, ``"expectations_"``) is the one case that
still exits nonzero: :data:`_LEDGER_CRASH_EXIT` (70), never 0. A real
ledger verb's exit code IS its contract (see :data:`LEDGER_VERBS` above),
and 0 means "clean" in that vocabulary -- so an UNKNOWN ledger verb failing
open at exit 0 would read as a clean audit that examined nothing, the
exact silent miss RDR-184 exists to catch. This cannot be checked against
:data:`LEDGER_VERBS` membership, because that table only names verbs that
already resolve; the prefix is the only signal available for a verb this
CLI has never registered.
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
    # The three SessionStart scripts that were plugin Python, ported into
    # the wheel at bead nexus-q02nx.21. Their closures permitted it: two
    # are stdlib-only and rdr_hook's sole non-stdlib import was
    # _hook_logging, whose one public function has a same-name equivalent
    # in _io. Full reasoning and the measured closure: T2
    # nexus_rdr/215-tier-resolution-bead-21. That bead left the other three
    # Python hooks plugin-resident in python3 exec form, reasoning that each
    # reaches _endpoint_resolve.py, which cannot leave the plugin. nexus-t9klx
    # answered that instead of accepting it: the mirror is not carried across,
    # the ported module calls the client's own primitives. _endpoint_resolve
    # and the two plugin copies that still imported it were then deleted
    # (nexus-z9cz2): nothing shipped executed them.
    #
    # "session-context", not "session-start": these are two DIFFERENT
    # SessionStart hooks and the good name was already taken above by the
    # port of the `nx hook session-start` Click verb.
    "preflight": "nexus.hooks.preflight_verb",
    "session-context": "nexus.hooks.session_context",
    # The first of the five bare-`python3` entries ported at nexus-t9klx.
    # Stock Windows has no python3 on PATH; a console-script verb gets a
    # real .exe shim from the installer. Stdlib-only and storage-free, so
    # the port is the script's body with print() replaced by HookResult.
    "behaviour-census": "nexus.hooks.behaviour_census",
    # The second, ported in the same bead. Its detached action moved with
    # it (nexus.hooks.version_lockstep_action) rather than being reached
    # back into the plugin: one caller is all it takes to keep a
    # plugin-resident script alive, which is what RDR-215 removes.
    "version-lockstep": "nexus.hooks.version_lockstep",
    # The routing framework's one fail_closed rule (nexus-t9klx). It was
    # already command-tier-only by ruling -- nexus.mcp.hooks._NEVER_TOOL_TIER
    # refuses to register it as an mcp_tool, because a tool-boundary crash
    # renders as allow and this rule must still deny. The port keeps it here.
    "phase-review-close-gate": "nexus.hooks.phase_review_close_gate",
    # The routing framework's other guard, and the deliberately FAIL-OPEN
    # one (nexus-t9klx). Its posture is the opposite of the line above and
    # stays that way: registry.yaml carries Sam's 2026-07-25 reasoning that
    # a crash in a broken guard must not brick every agent's Bash. Porting
    # it emptied `routing/` of code -- `routing/_lib.py` had no plugin
    # importer left and went with it.
    "subagent-git-write-gate": "nexus.hooks.subagent_git_write_gate",
    # The last of the five (nexus-t9klx): the UserPromptSubmit mailbox floor.
    # The only verb that streams its stdout during run() (_io.stream), because
    # a row it has consumed at the engine must be shown before its recovery
    # record is dropped.
    "mailbox-drain": "nexus.hooks.mailbox_drain",
    "rdr": "nexus.hooks.rdr_verb",
    # The two SessionStart entries that carried SHELL LOGIC in their command
    # string -- `nx upgrade --auto 2>/dev/null || echo ... >&2` and `nx self
    # gc >/dev/null 2>&1 || true` (bead nexus-q02nx.22, Approach item 6).
    # Exec form has no shell, so the redirects and the `||` moved into the
    # verbs. Both still spawn `nx` as a separate process: they reason about
    # install generations and flip `<tools>/current`, which is not something
    # to do inside the hook interpreter that is running out of one.
    "upgrade-auto": "nexus.hooks.upgrade_auto",
    "self-gc": "nexus.hooks.self_gc",
    # The RDR-184 ledger's operator verbs (bead nexus-q02nx.14). All four
    # resolve to ONE module, which re-reads sys.argv[1] to tell them apart
    # -- they take arguments rather than a hook payload, so they are the
    # only entries here that are not fired by an event. Command tier only:
    # the answer IS the exit code, and an MCP tool has none.
    "expectations_census": "nexus.hooks.ledger_verbs",
    "expectations_undeclared": "nexus.hooks.ledger_verbs",
    "expectations_reconcile": "nexus.hooks.ledger_verbs",
    "expectations_expect": "nexus.hooks.ledger_verbs",
    # The three DECIDING hooks (bead nexus-17i1n). Each of these was wired
    # as an `mcp_tool` entry at bead nexus-q02nx.21 and shipped inert in
    # conexus 7.55.0: an `mcp_tool` hook CANNOT return a permission or stop
    # decision. Claude Code's own hooks guide lists the four hook types
    # that can decide -- prompt, agent, command, http -- and `mcp_tool` is
    # not among them; its documented failure posture is "non-blocking
    # error", and its output is read for context, never for a verdict.
    # Measured 2026-09-20 against CLI 2.1.278 with the 7.55.0 pin: a
    # `bd close` naming a bead with no review marker reached `bd` itself
    # and closed it, while the same payload through `run()` returns a
    # correct deny. So these three take the command tier, for the same
    # reason `phase_review_close_requires_gate` was never allowed on the
    # tool tier at all (see nexus.mcp.hooks' `_NEVER_TOOL_TIER`).
    #
    # They keep their tool-tier registrations, which stay useful for
    # diagnosis and for a caller that wants the verdict as data; what
    # changed is which tier `hooks.json` WIRES. `_DECIDING_HOOKS` in
    # nexus.mcp.hooks names the set, and
    # tests/test_deciding_hooks_are_command_tier.py refuses a hooks.json
    # that wires any of them as an mcp_tool again.
    "pre-close-verification": "nexus.hooks.pre_close_verification",
    "subagent-stop": "nexus.hooks.subagent_stop",
    "auto-approve": "nexus.hooks.auto_approve",
    # The interactive MCP connection barrier (bead nexus-veh77, Sam's
    # 2026-09-23 ruling). SessionStart, `startup` matcher only: waits,
    # bounded and fail-open, for THIS session's nx-mcp to publish its
    # connect marker (nexus.mcp.connect_marker) -- published unconditionally
    # from every branch of nexus.mcp.core._t1_lifespan right before its own
    # yield, independent of T1 mint/lease outcome (round 2: a T1-lease-keyed
    # signal stalled every session on a T1-degraded box for the full bound)
    # -- before turn 1 can outrun the connection the way the interactive
    # ladder measured it doing 24/24 on macOS and 20/20 on WSL2 with no
    # barrier at all.
    "mcp-connect-wait": "nexus.hooks.mcp_connect_wait",
    # The MID-SESSION half (bead nexus-veh77 round 5): mcp-connect-wait
    # protects only SessionStart. UserPromptSubmit, sibling to
    # mailbox-drain rather than folded into it (unrelated concern, no
    # network, own cost/test budget -- see the module's own docstring):
    # warns once per disconnect episode when this session's nx-mcp
    # connect marker names a pid that pid_alive() (the ONE shared
    # liveness implementation, nexus.daemon.service_registry) no longer
    # finds alive, having previously been alive. A session that never
    # connected stays silent -- that is mcp-connect-wait's own job.
    "mcp-connect-check": "nexus.hooks.mcp_connect_check",
}

#: Verbs whose exit code nx-hook must propagate from ``run()`` instead of
#: forcing 0 -- the ledger's callers branch on it (RDR-215 Contracts).
#: Populated alongside VERB_TABLE as those verbs are ported (Phase 2).
#: sysexits EX_SOFTWARE. A ledger verb that CRASHED exits this instead of a
#: vocabulary value, so a caller branching on 0/1/2/3/4 can tell "the audit
#: could not run" from any real verdict.
_LEDGER_CRASH_EXIT = 70

LEDGER_VERBS: frozenset[str] = frozenset(
    {
        "expectations_census",
        "expectations_undeclared",
        "expectations_reconcile",
        "expectations_expect",
    }
)

#: Every real ledger verb starts with this prefix (see :data:`LEDGER_VERBS`
#: above), so it also identifies a ledger-SHAPED verb this CLI has never
#: registered -- an UNKNOWN verb nx-hook cannot look up in ``LEDGER_VERBS``,
#: because that table only names verbs that resolve. The unknown-verb branch
#: in :func:`main` uses this prefix check, not membership, precisely because
#: membership is unavailable for a verb with no resolved module (code review
#: on 69b6cac76): a plugin naming a NEW ledger verb the installed CLI
#: predates must not read as 0 ("clean"), which is what plain fail-open
#: would do.
_LEDGER_VERB_PREFIX = "expectations_"

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
        # A plugin ahead of this installed CLI is expected to name a verb
        # this VERB_TABLE has never heard of (see the module docstring's
        # "Exit codes" section, nexus-t9klx). Exiting nonzero here for a
        # NON-ledger verb would fail every UserPromptSubmit/PreToolUse for
        # the whole session with no self-heal path -- exactly the 7.58.0
        # release blocker this guards against. The stderr line is the same
        # in both branches below; only the exit code (and, for a non-ledger
        # verb, a stdout signal) differs.
        sys.stderr.write(
            f"nx-hook: unknown verb {verb!r} -- no hook is registered under that name "
            "in this installed nx CLI. The conexus plugin may be ahead of the "
            "installed nx CLI; it upgrades via the version-lockstep hook.\n"
        )
        if verb.startswith(_LEDGER_VERB_PREFIX):
            # LEDGER SAFETY (code review on 69b6cac76): a ledger verb's exit
            # code IS its contract (undeclared 0/1/2/3, reconcile 0/2/4,
            # census 0/1), and 0 there means "clean". Fail-open's plain
            # exit 0 would make a plugin/CLI skew on THIS surface read as a
            # clean audit that examined nothing -- the exact silent miss
            # RDR-184 exists to catch. Reserved code instead, the same one
            # a CRASHED known ledger verb gets: "I could not run this" is
            # not "there was nothing to report", for the same reason in
            # both cases. No stdout write here -- an unknown ledger verb
            # produces no JSON body, same as a crashed one.
            sys.exit(_LEDGER_CRASH_EXIT)
        # Non-ledger unknown verb: fail open (exit 0), but say so on the
        # REAL envelope channel too -- stderr from a hook never reaches the
        # person running the session (Claude Code does not surface it), so
        # exit 0 plus a stderr line alone is silent to the one audience that
        # can act on it (substantive-critic finding on 69b6cac76). This
        # write happens BEFORE the stdout/stderr fd-redirect dance below (it
        # returns via sys.exit before reaching that code), so `sys.stdout`
        # here is genuinely the real channel Claude Code parses -- exactly
        # what a plain top-level `systemMessage` field is for: Claude
        # Code's own hooks contract accepts it "on every event" (it is not
        # nested under `hookSpecificOutput`, which IS event-shaped), so no
        # per-event branch is needed here.
        sys.stdout.write(
            json.dumps(
                {
                    "systemMessage": (
                        "conexus plugin is ahead of the installed nx CLI: hook verb "
                        f"{verb!r} is not in this CLI and was skipped. Run `nx upgrade` "
                        "(or restart after the background upgrade finishes)."
                    )
                }
            )
            + "\n"
        )
        sys.stdout.flush()
        sys.exit(0)

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

        from nexus._hook_runtime._io import (  # noqa: PLC0415 — deferred: only a real dispatch pays this
            install_stream_sink,
            never_fail,
            read_payload,
        )

        # The real stdout, for the one verb that must write BEFORE it returns
        # (``_io.stream``; mailbox-drain). fd 1 now points at stderr, so the
        # sink writes to the saved duplicate of the original, not to fd 1.
        def _sink(text: str) -> None:
            data = (text + "\n").encode("utf-8")
            if saved_stdout_fd is not None:
                while data:
                    data = data[os.write(saved_stdout_fd, data):]
            else:
                real_stdout.write(text + "\n")
                real_stdout.flush()

        install_stream_sink(_sink)

        def _dispatch():
            if failed_import is not None:
                raise failed_import
            # A LEDGER VERB IS NOT FIRED BY AN EVENT, so it must not read
            # stdin. Every other verb is a hook and its payload arrives
            # there; these four are invoked with arguments by an operator
            # or an audit script, which does not redirect stdin at all.
            # read_payload guards a TTY, but a script's inherited pipe is
            # not a TTY and has no writer, so read() blocks until an EOF
            # that never comes -- measured: `nx-hook
            # expectations_undeclared <sid>` from a shell hung until
            # killed, and every class-1 consumer bead nexus-q02nx.14
            # repoints would have hung the same way. They take no payload
            # (reconcile takes its own as an argument), so None is not a
            # degraded input here, it is the correct one.
            payload = None if _is_ledger_verb(verb) else read_payload(sys.stdin)
            return module.run(payload)

        result = never_fail(_dispatch, verb)
    finally:
        # sys.modules, not a fresh import: an import that failed above would
        # otherwise run again here, inside a finally.
        _io_module = sys.modules.get("nexus._hook_runtime._io")
        if _io_module is not None:
            _io_module.install_stream_sink(None)
        sys.stdout = real_stdout
        if saved_stdout_fd is not None and guarded_fd is not None:
            sys.stderr.flush()
            os.dup2(saved_stdout_fd, guarded_fd)
            os.close(saved_stdout_fd)

    if result.stdout is not None:
        real_stdout.write(result.stdout + "\n")
        real_stdout.flush()

    # A LEDGER VERB'S EXIT CODE IS ITS CONTRACT, so a crash must not wear a
    # vocabulary value. undeclared uses 0/1/2/3, reconcile 0/2/4, census 0/1,
    # and every one of those means something a caller branches on; a crashed
    # verb exiting 0 reads as "clean", which is the silent miss this whole
    # subsystem exists to prevent (measured, bead nexus-q02nx.9). It exits
    # EX_SOFTWARE instead -- reserved, colliding with no ledger vocabulary.
    #
    # Reserved rather than folded into undeclared's 3 ("no ledger file,
    # nothing checkable"): "I could not tell you" is not "there was nothing
    # to tell", and folding them loses the distinction exactly when someone
    # is diagnosing a flapping audit. Sam's ruling, 2026-09-19; RDR-215
    # Contracts amended to name this third case.
    #
    # Non-ledger verbs are unchanged and still forced to 0: for them a crash
    # IS the hook choosing to say nothing, which is failing open.
    if _is_ledger_verb(verb):
        sys.exit(_LEDGER_CRASH_EXIT if result.crashed else result.exit_code)
    sys.exit(0)


if __name__ == "__main__":
    main()
