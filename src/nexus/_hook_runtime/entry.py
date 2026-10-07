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
hooks. The phase-review close gate (deleted at cleanup step A2) was stdlib-only and cost
0.03 s end to end as bash on this box -- 0.04 s in bead .2's harness --
so a port paying 0.06 s to reach
``never_fail`` would have been a hot-path regression rather than the
speedup RDR-215 promises. ``session-start`` is the other extreme and is
deliberately not optimised here: it genuinely needs ``nexus.session``,
pays for it legitimately, and its own 187-221 ms is mostly real I/O --
a bounded tuple-surface probe and a stale-MCP-host scan -- against the
~904 ms ``nx hook session-start`` Click path it replaces (bead
nexus-q02nx.5, median of 10). Judge this module by the cheap verbs, not
by ``session-start`` -- but on the right number.

What 0.02 s measures is the dispatch FLOOR -- a synthetic stdlib-only
verb through the real entry point. That is the figure the common path of
a hook that runs on every Bash call pays when it exits early.

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
(Approach item 1).
Resolution happens once, per invocation, via ``importlib`` -- there is no
eager import of every registered verb's module merely because one of them
is being dispatched.

**Exit codes.** Every hook verb exits 0, matching the bash layer, where a
block or deny is encoded in the JSON body on stdout and never in the exit
status (RDR-215 Contracts).

A MISSING verb argument (``hooks.json`` invoking ``nx-hook`` with no verb
at all) is a genuine invocation error and stays exit 2 with a one-line
diagnostic -- that shape is always this CLI's own misconfiguration to fix.

An UNKNOWN verb -- one this CLI's :data:`VERB_TABLE` has never heard of --
exits 0 instead of 2, still with a named stderr diagnostic AND
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

**Be plain about what fail-open COSTS for a DECIDING hook.** A deciding
hook (``auto-approve``, a PreToolUse permission) is exactly the shape for
which this exit-0 path is a way for a failure to read as allow: on a CLI
older than the plugin, an unknown deciding verb means the hook LITERALLY DOES
NOT RUN, and what it would have decided is left to Claude Code's own
permission flow, exactly as if the hook had been deleted. That is code that
never ran, which is not the same thing as a crash inside code that did. The
trade made is a session blocked outright with no self-heal path (exit 2, the
7.58.0 incident) against a hook that is silently absent for the minutes to
hours between the plugin update landing and the session's own
version-lockstep hook finishing its detached upgrade. That window is bounded
and self-closing; a blocked session is not.

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
    # No `version-lockstep` verb. nexus-t9klx ported it here, and the port
    # was the 7.58.0 release blocker: the hook that repairs plugin-ahead CLI
    # skew cannot depend on the CLI it repairs, and an older nx-hook exits 2
    # on a verb it lacks. It stays the stdlib plugin script
    # conexus/hooks/scripts/version_lockstep_hook.py for good (pinned by
    # tests/hooks/test_lockstep_survives_cli_skew.py); the unwired verb was
    # deleted at nexus-rcoze.
    # No `subagent-git-write-gate` verb either: hooks.json never wired it, so
    # the plugin script routing/subagent_git_write_requires_orchestrator.py is
    # the one copy (cleanup step A4, nexus-0r1uz).
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
    # The DECIDING hook (bead nexus-17i1n). `auto-approve` was wired as an
    # `mcp_tool` entry at bead nexus-q02nx.21 and shipped inert in conexus
    # 7.55.0: an `mcp_tool` hook CANNOT return a permission or stop
    # decision. Claude Code's own hooks guide lists the four hook types
    # that can decide -- prompt, agent, command, http -- and `mcp_tool` is
    # not among them; its documented failure posture is "non-blocking
    # error", and its output is read for context, never for a verdict.
    # So it takes the command tier. Its tool-tier registration stays useful
    # for diagnosis. `DECIDING_HOOKS` in nexus.mcp.hooks names the set, and
    # tests/test_deciding_hooks_are_command_tier.py refuses a hooks.json
    # that wires any of them as an mcp_tool.
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
    # Registered, silent no-op (nexus-qxyqz): the mid-session "nx-mcp is not
    # connected" warning it printed on UserPromptSubmit was deleted, but
    # published plugins still name this verb in hooks.json. Removing it would
    # make main() below print the "plugin is ahead of the installed nx CLI"
    # systemMessage on every prompt of an old plugin. Never remove it.
    # tests/hooks/test_mcp_connect_check_verb.py pins it; hook-cli-skew fires
    # only the current hooks.json and does not.
    "mcp-connect-check": "nexus.hooks.mcp_connect_check",
}

#: Test-only dispatch override, read solely by
#: ``tests/hooks/test_nx_hook_entry.py``. A JSON object string mapping verb
#: name to dotted module path, e.g. ``{"probe": "probe_verb"}``. Exists
#: because this bead builds the dispatch mechanism before any real verb is
#: registered (nexus-q02nx.5 ports the first one) -- it lets the test suite
#: exercise dispatch against the REAL entry point (an unknown verb, a verb
#: that raises, a verb that inspects its own import environment) without
#: editing VERB_TABLE. Never set outside a test process.
_TEST_VERB_OVERRIDE_ENV = "_NX_HOOK_TEST_VERB_OVERRIDE"

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



def _write_utf8(stream, text: str) -> None:
    """Write *text* to *stream* as UTF-8 bytes with no newline translation.

    Claude Code decodes a hook's stdout as UTF-8. A piped ``sys.stdout`` on
    Windows is in the locale code page (cp1252) and text mode, so a plain
    ``write`` sent other bytes and CRLF line ends, and a character outside the
    code page raised after ``never_fail`` had already returned (RDR-224). A
    stream with no ``buffer`` (a test's ``StringIO``) takes the text as is.
    """
    buffer = getattr(stream, "buffer", None)
    if buffer is None:
        stream.write(text)
        stream.flush()
        return
    stream.flush()
    buffer.write(text.encode("utf-8"))
    buffer.flush()

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
        # verb would fail every UserPromptSubmit/PreToolUse for
        # the whole session with no self-heal path -- exactly the 7.58.0
        # release blocker this guards against.
        sys.stderr.write(
            f"nx-hook: unknown verb {verb!r} -- no hook is registered under that name "
            "in this installed nx CLI. The conexus plugin may be ahead of the "
            "installed nx CLI; it upgrades via the version-lockstep hook.\n"
        )
        # Unknown verb: fail open (exit 0), but say so on the
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
    # ``subprocess.run`` captures its output), but a verb that shells out to
    # ``bd`` or ``git`` is exactly the shape that would hit it. So fd 1 is
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
                _write_utf8(real_stdout, text + "\n")

        install_stream_sink(_sink)

        def _dispatch():
            if failed_import is not None:
                raise failed_import
            payload = read_payload(sys.stdin)
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
        _write_utf8(real_stdout, result.stdout + "\n")

    # Every verb is forced to 0: a crash IS the hook choosing to say nothing,
    # which is failing open.
    sys.exit(0)


if __name__ == "__main__":
    main()
