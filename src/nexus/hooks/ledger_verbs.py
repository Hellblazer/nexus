# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (c) 2026 Hal Hildebrand. All rights reserved.
"""The RDR-184 ledger's operator verbs on ``nx-hook`` (bead nexus-q02nx.14).

``expectations_census``, ``expectations_undeclared``,
``expectations_reconcile`` and ``expectations_expect`` were reachable only
by SOURCING a bash library -- ``source tests/e2e/lib/expectations.sh`` in
five scripts, five prose documents and two cc-validation scenarios. This
module is what those consumers call instead once the library goes.

**These are the one part of the hook layer that is not a hook.** Nothing
fires them; an operator or an audit script runs them, with arguments, and
BRANCHES ON THE EXIT CODE -- ``undeclared`` uses 0/1/2/3, ``reconcile``
0/2/4, ``census`` 0/1, and each value means something a caller acts on.
That is why they live on the command tier only and are absent from
``HOOK_TOOLS``: a model-callable MCP tool has no exit code to carry the
answer, and the answer IS the exit code.

**Arguments come from ``sys.argv``, not from the payload.** Every other
verb in the table is a real hook and receives a JSON payload on stdin;
these receive a session id (and, for ``reconcile``, the harness payload).
Reading argv here rather than widening ``run(payload)`` keeps ONE shared
signature across both tiers -- the tool tier has no argv at all, so a
signature carrying it would be meaningless there -- and keeps the
argv-parsing cost on the four verbs that need it.

One module serves all four: ``main()`` passes no verb name to ``run()``,
but ``sys.argv[1]`` carries it, so the dispatch below reads the same
string the entry point resolved.
"""
from __future__ import annotations

import sys

from nexus._hook_runtime._io import HookResult
from nexus.hooks import expectations as _exp

__all__ = ["VERBS", "run"]

#: Every verb this module answers for. ``VERB_TABLE`` maps each of these
#: to this module, and :func:`run` re-reads ``sys.argv[1]`` to tell them
#: apart.
VERBS: tuple[str, ...] = (
    "expectations_census",
    "expectations_undeclared",
    "expectations_reconcile",
    "expectations_expect",
)

#: Usage, per verb, for the one message an operator sees when they get the
#: arguments wrong. Exit 2 with a named usage line, never a traceback and
#: never a silent 0 -- a ledger verb that exits 0 having done nothing is
#: indistinguishable from a clean audit, which is the failure this
#: subsystem exists to prevent.
_USAGE: dict[str, str] = {
    "expectations_census": "expectations_census <session_id>",
    "expectations_undeclared": "expectations_undeclared <session_id>",
    "expectations_reconcile": "expectations_reconcile <session_id> <payload_json>",
    "expectations_expect": (
        "expectations_expect <session_id> <subagent_type> "
        "[background|sync] [dispatch_id]"
    ),
}

_USAGE_EXIT = 2


def _usage(verb: str) -> HookResult:
    sys.stderr.write(f"nx-hook: usage: {_USAGE[verb]}\n")
    return HookResult(exit_code=_USAGE_EXIT)


def _report(report: _exp.LedgerReport) -> HookResult:
    """Render a ledger report the way the bash printed it.

    Lines go to STDOUT, one per line, because callers pipe and grep them;
    the code is the verb's exit status. ``note`` is stderr -- it explains a
    code rather than being part of the answer, and a caller parsing stdout
    must not have to skip it.
    """
    if report.note:
        sys.stderr.write(report.note.rstrip("\n") + "\n")
    body = "\n".join(report.lines) if report.lines else None
    return HookResult(stdout=body, exit_code=report.code)


def run(payload: dict | None) -> HookResult:  # noqa: ARG001 — argv-driven; see the module docstring
    """Dispatch on ``sys.argv[1]`` and run that ledger verb.

    *payload* is ignored: these verbs are invoked with arguments, not with
    a hook event. It stays in the signature because both tiers call
    ``run(payload)`` and one shared signature is the whole point of that
    arrangement.
    """
    argv = sys.argv[1:]
    verb = argv[0] if argv else ""
    args = argv[1:]

    if verb not in _USAGE:
        sys.stderr.write(f"nx-hook: {verb!r} is not a ledger verb\n")
        return HookResult(exit_code=_USAGE_EXIT)

    if not args or not args[0]:
        return _usage(verb)
    session_id = args[0]

    if verb == "expectations_census":
        return _report(_exp.expectations_census(session_id))
    if verb == "expectations_undeclared":
        return _report(_exp.expectations_undeclared(session_id))
    if verb == "expectations_reconcile":
        if len(args) < 2:
            return _usage(verb)
        return _report(_exp.expectations_reconcile(session_id, args[1]))

    # expectations_expect: a WRITE, and the only verb here that changes the
    # ledger. Kept because AGENTS.md documents a hand-call for a dispatch
    # the PreToolUse hook cannot see, keyed on the subagent type verbatim.
    if len(args) < 2 or not args[1]:
        return _usage(verb)
    mode = args[2] if len(args) > 2 and args[2] else "background"
    dispatch_id = args[3] if len(args) > 3 else ""
    try:
        _exp.expectations_expect(session_id, args[1], mode, dispatch_id)
    except _exp.ExpectationsUsageError as exc:
        sys.stderr.write(f"nx-hook: expectations_expect refused: {exc}\n")
        return HookResult(exit_code=_USAGE_EXIT)
    return HookResult()
