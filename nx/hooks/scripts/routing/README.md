# Routing Hooks (RDR-121)

PreToolUse hooks that enforce soft guidance Hal repeatedly types as
feedback. Each hook is a Python-native script that imports ``_lib`` and
either allows the tool call to proceed or denies it with a redirect
message naming the preferred invocation.

## Contract

Every hook in this directory MUST honor:

1. **Python-native**. Shebang ``#!/usr/bin/env python3``. One process
   per hook invocation. No nested bash + python3 stacks; the per-call
   startup budget is ~40ms and a nested shell doubles it.
2. **JSON envelope on stdout, exit 0**. Never exit 2. The envelope shape
   is fixed by Claude Code's PreToolUse contract:

       {"hookSpecificOutput": {
           "hookEventName": "PreToolUse",
           "permissionDecision": "allow" | "deny",
           "reason": "..."              # deny only
           "additionalContext": "..."   # optional advisory text on allow
       }}

3. **Per-hook budget**: <50ms p95 (40ms python startup + ~10ms logic).
4. **Cumulative budget**: <300ms p95 with the cap of 4 active routing
   hooks per matcher. Bumping the cap requires a follow-on bead and a
   budget revision in RDR-121.
5. **Fail-open by default**; opt in to fail-closed via
   ``run_hook(..., fail_closed=True, rule_name="...")`` AND
   ``fail_closed: true`` in ``registry.yaml`` for that rule.
6. **Tool-name short-circuit** at the top of the hook. If the call is
   not for the matcher target, ``allow()`` immediately.
7. **Honor the escape token** ``# routing-allow: <reason>=8 chars>`` on
   every hook. Hard blocks with no escape produce hook fatigue and were
   rejected in RDR-121 Alternative 3.

## Authoring template

    #!/usr/bin/env python3
    # SPDX-License-Identifier: AGPL-3.0-or-later
    """One-line rationale: what this hook routes and why."""
    from __future__ import annotations

    import os
    import sys

    sys.path.insert(0, os.path.dirname(__file__))
    import _lib

    RULE_NAME = "this_rules_name"


    def body(payload):
        command = _lib.get_bash_command(payload)
        if not command:
            _lib.allow()
        if _lib.should_skip_for_reason(command):
            _lib.log_routing_event(
                rule=RULE_NAME, outcome="escape", tool_name="Bash",
                command_fragment=command,
            )
            _lib.allow()
        # Rule-specific matching here.
        if _matches(command):
            _lib.log_routing_event(
                rule=RULE_NAME, outcome="deny", tool_name="Bash",
                command_fragment=command,
            )
            _lib.deny(
                "Redirect message naming the preferred invocation. "
                "Escape with `# routing-allow: <reason>` (>=8 chars)."
            )
        _lib.allow()


    if __name__ == "__main__":
        _lib.run_hook(body, fail_closed=False, rule_name=RULE_NAME)

## Registry

``registry.yaml`` is the authoritative list of active rules. The shape
is documented inline in that file. Hooks may read their own entry at
runtime, but the file is also the source for documentation, telemetry
aggregation, and the 30-day soak review (RDR-121 §Phase 4).

## Testing

Every hook gets a test module under ``tests/`` that exercises:

* **Positive**: an offending command produces ``deny`` with the expected
  redirect message.
* **Negative**: a non-offending command (or a non-matcher tool call)
  produces ``allow``.
* **Escape**: an offending command with a valid ``# routing-allow:``
  token produces ``allow``.
* **Malformed input**: empty stdin or non-JSON produces ``allow`` (or
  ``deny`` for fail-closed rules).

See ``tests/test_routing_hooks.py`` for the framework-level contract
tests.
