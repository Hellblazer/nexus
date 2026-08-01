# `session-75695009-prefix.redacted.jsonl`

Frozen fixture backing `test_golden_session_75695009_exact_counts` (nexus-h33x8.1
VERIFICATION 1 and 4).

## Why it is frozen

Claude Code transcripts are **live, append-only files**. Session
`75695009-d060-4433-a687-214cb946389a` was measured mid-flight by nexus-3sij7 and
kept running for hours afterwards, so an assertion against the live path under
`~/.claude/projects/` fails on day one:

| moment | Bash | Edit | Agent | Read | Write | ToolSearch |
|---|---|---|---|---|---|---|
| nexus-3sij7 snapshot | 150 | 19 | 7 | 6 | 3 | 2 |
| plan audit, 2026-07-31 00:23 | 190 | 26 | 8 | 6 | 4 | 2 |
| session end | 286 | 31 | 8 | 6 | 7 | 3 |

The plan-audit comment on nexus-h33x8.1 prescribed the fix directly: freeze a copy
into `tests/`. Relaxing the assertion to `>=` instead would destroy VERIFICATION 4,
because falsify-by-deletion needs exact counts to detect a deleted classification
branch.

## What it is

The first **1,709 records** of that transcript — the prefix ending at
`2026-07-31T23:41:10.436Z`, the moment `Bash` reaches 150, which is where all six
of nexus-3sij7's counts hold simultaneously.

**It is a redacted projection, not a verbatim copy.** The verbatim prefix is 2.8 MB
and contains real prompts, shell commands, and absolute paths from a developer's
machine — not something to commit to a public repository. Each record keeps only:

- `type`, `timestamp`, `isSidechain`
- `message.role`
- `message.content[]`, where `tool_use` blocks keep `name` (plus
  `input.subagent_type` for `Agent` blocks — a type name, not content) and every
  other block is stubbed to `{"type": ...}`

Record count, record ordering, record types, and every tool-call name are
preserved exactly, so the counts are bit-identical to the live prefix. Audited at
creation: no key outside the list above survives, and zero occurrences of user
paths or URLs.

**What this fixture therefore does NOT prove:** that the parser handles verbatim
tool-call `input` payloads or free-text block bodies. Those paths are covered by
the synthetic fixtures in `tests/test_census.py`.

## Regenerating

Not reproducible from a clean checkout by design — it is frozen precisely so it
cannot move. The generating projection is the `redact()` function recorded in
T2 `nexus/h33x8.1-census-verification-reconciliation-2026-07-31` [21323].
