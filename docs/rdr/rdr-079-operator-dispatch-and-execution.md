---
title: "RDR-079: Operator Dispatch + Plan Execution End-to-End"
status: abandoned
type: feature
priority: P1
created: 2026-04-15
revised: 2026-04-16
accepted_date: 2026-04-15
abandoned_date: 2026-04-16
abandon_reason: architecture
related: [RDR-042, RDR-078, RDR-080]
superseded_by: "nexus.operators.dispatch (PR #168)"
---

# RDR-079: Operator Dispatch + Plan Execution End-to-End

## Status: Abandoned

This RDR was accepted and partially implemented on branch
`feature/nexus-05i-rdr-078-impl` (PR #167). The branch was abandoned and
never merged. The core deliverable — operator tools backed by `claude -p`
subprocess workers — was reimplemented correctly in PR #168 without the
pool architecture.

## What went wrong

RDR-079 proposed an **operator pool**: long-lived `claude -p` worker
subprocesses pre-warmed and managed as a singleton (`OperatorPool`,
`RewindPool`). 1,425 lines across `pool.py` and `rewind_pool.py`.

The fatal flaw was `check_auth()`:

```python
# pool.py — this call blocks the asyncio event loop
def check_auth() -> None:
    result = subprocess.run(
        ["claude", "auth", "status", "--json"], ...
    )
```

`subprocess.run` is synchronous. Called from inside a FastMCP async
handler, it blocked the event loop entirely — no other coroutine could
run while auth was being checked. The pool existed to manage worker
lifecycle; the auth check existed to gate pool creation; the auth check
blocked everything.

The auth check was also unnecessary: `claude -p` inherits Claude Code's
OAuth session auth. There is no API key. There is nothing to check. The
assumption that auth needed to be verified pre-flight was wrong from the
start.

## What replaced it

`nexus.operators.dispatch.claude_dispatch(prompt, schema, timeout)`:

- Single async function, ~40 lines
- `asyncio.create_subprocess_exec` — never blocks the event loop
- No pool, no auth check, no worker lifecycle
- One subprocess per call; auth is inherited

Five operator MCP tools (`operator_extract`, `operator_rank`,
`operator_compare`, `operator_summarize`, `operator_generate`) and four
orchestration tools (`nx_answer`, `nx_tidy`, `nx_enrich_beads`,
`nx_plan_audit`) all dispatch via `claude_dispatch`.

## Lessons

- `claude -p` inherits Claude Code auth. Never add a pre-flight auth
  check to MCP code. It will block the event loop.
- Pool architectures add lifecycle complexity with no benefit when the
  subprocess is stateless (each `claude -p` call is independent).
- The async event-loop blocking test (`test_dispatch_does_not_block_event_loop`)
  would have caught this on the first commit. Write it first.
