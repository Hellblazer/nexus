---
title: "Command Preambles via the nx CLI: Thin Commands, Tested Logic, No Inlined Bash"
id: RDR-130
type: Architecture
status: draft
priority: high
author: Hal Hildebrand
reviewed-by: self
created: 2026-05-26
related_issues: [nexus-61fzg, nexus-ln9y5, nexus-t1b1k]
related_rdrs: [RDR-120, RDR-128]
supersedes: []
related_tests: [tests/test_plugin_structure.py, tests/test_command_preamble_sync.py, tests/cc-validation/scenarios/19_command_bash_injection_renders.sh]
---

# RDR-130: Command Preambles via the nx CLI: Thin Commands, Tested Logic, No Inlined Bash

## Problem Statement

conexus slash commands gather context in a preamble that runs at invocation time
and injects its output into the prompt. The preamble logic is **inlined** into the
command markdown as a bash-injection block. This has proven brittle: two separate
Claude Code fence-parser behaviors have broken it in consecutive releases.

#### Gap 1: The injection block source cannot contain a literal triple-backtick

Claude Code closes a fenced-bang (```` ```! ````) block at the **first literal
triple-backtick in the source**, on any line and at any position (including inside
`echo '<fence>'` or a Python `print`/regex). A longer (4-backtick) opening fence
does not help (empirically confirmed). Commands whose preamble emits markdown code
fences therefore truncate mid-content and the shell errors. Shipped in conexus
5.1.2: 17 of 25 commands broken (nexus-61fzg).

#### Gap 2: The block is bash, with all its hazards

Inlined logic must survive shell quoting and heredoc parsing. The 9 RDR commands
inline a multi-line Python heredoc; the 16 agent-relay commands inline multi-line
shell. Both are fragile and hard to read.

#### Gap 3: `$CLAUDE_PLUGIN_ROOT` is empty in command-bash context

It is scoped to hooks / MCP / LSP, not slash-command bash. So a command cannot
cleanly invoke a bundled script by path (this was the nexus-t1b1k Bug B), forcing
the brittle inline approach in the first place.

#### Gap 4: The render path is not unit-testable

Only a real Claude Code exercises the injection. Static checks model the parser
and drift from it: the nexus-ln9y5 verification checked for *bare* fence lines, the
effect-test extracted with a CommonMark-correct regex, and cc-validation covered
only the one safe representative. All three passed while 17 commands were broken.

## Context

RDR-120 split storage behind daemons; RDR-128 enforced T2 single-writer. nexus-t1b1k
(5.1.1) tried to fix non-executing `!{ }` brace blocks by extracting heredocs to
scripts, but kept the invalid `!{ }` wrapper. nexus-ln9y5 (5.1.2) moved all 25
commands to the documented ```` ```! ```` fenced form and inlined the logic — which
then hit Gap 1. The pattern across t1b1k -> ln9y5 -> 61fzg is the same: **inlining
command logic fights the harness**, and each fix discovers a new parser edge.

## Research Findings (empirically established this cycle)

- Valid CC bash-injection forms: inline `` !`cmd` `` and fenced ```` ```! ````.
  `!{ }` braces are NOT a recognized form (emit raw).
- A fenced-bang block closes at the first literal triple-backtick in the source;
  4-backtick fences do not change this.
- Injected **output** is inserted as plain text and is NOT re-scanned, so a tool's
  output may freely contain triple-backticks. The constraint is purely on the
  `.md` source.
- `$ARGUMENTS` is substituted by CC in the block; `$CLAUDE_PLUGIN_ROOT` is not.
- The `nx` CLI is always on PATH (it is the package entry point).

## Proposed Solution

**Keep the injected bash trivial; move all preamble logic into the `nx` CLI.**

Each command becomes a single-line invocation with no literal triple-backtick and
no inlined logic, e.g.:

    !`nx rdr preamble list "$ARGUMENTS"`

The invoked `nx` subcommand does the work in normal, unit-tested Python and prints
the preamble markdown (fences and all — output is not re-parsed). This eliminates
Gaps 1-4 at once: the `.md` source has nothing for the fence parser to choke on,
no shell quoting beyond the single arg, no `$CLAUDE_PLUGIN_ROOT` dependency, and
the logic is testable like any CLI command.

- **RDR commands (9):** port `conexus/resources/rdr_commands/*.py` into the existing
  `nx rdr` group as `nx rdr preamble <name>` subcommands (logic already Python).
- **Agent-relay commands (16):** port the shell preambles (working-dir, project-type
  detection, bead/git context) into `nx` subcommands (e.g. `nx command-context
  <name>`); the unified ~21-ecosystem detector from nexus-ln9y5 becomes Python.

## Alternatives Considered

- **Inline patch (strip literal fences from the 17 blocks).** Rejected: keeps the
  brittle inline form, is whack-a-mole against CC parser edges, and is throwaway
  work once logic moves to the CLI.
- **Drop preambles; let the agent self-gather via `## Action` + MCP tools.**
  Rejected as the primary path: loses pre-computed context (next RDR id, project
  type) and adds tool-call latency per invocation. Retained as the interim P0
  relief option (see Implementation Plan).
- **4-backtick fence.** Rejected: empirically does not change the truncation.

## Trade-offs

- More upfront work than a patch, but eliminates an entire bug class.
- Adds `nx` process-startup cost per command invocation (acceptable; commands are
  interactive, not hot-loop).
- Centralizes preamble logic in the CLI where it is testable and reusable.

## Implementation Plan

- **P0 (interim relief, optional 5.1.3):** if the broken commands need immediate
  relief before the migration lands, strip literal triple-backticks from the 17
  blocks (drop shell fence-wrapping; `chr(96)*3` in the 3 Python ones). Throwaway;
  only if the P0 cannot wait for P1.
- **P1:** RDR commands -> `nx rdr preamble <name>`. Port the 9 scripts; flip the 9
  `.md` files to one-line `nx` invocations; delete `resources/rdr_commands/*.py`
  and the inline-sync test.
- **P2:** agent-relay commands -> `nx command-context <name>` (or per-group verbs).
  Port the shell logic + the ~21-ecosystem detector to Python; flip the 16 `.md`.
- **P3:** retire the inline machinery; the storage of preamble logic is the CLI.

## Test Plan

- Unit tests per `nx` preamble subcommand (deterministic, no real CC needed).
- Static guard (`test_plugin_structure`): every command bash block is a single-line
  `nx` invocation; reject any literal triple-backtick and any multi-line/heredoc
  body inside an injection block. This matches CC's real parser constraint and
  would have caught both Gap 1 and the t1b1k class.
- cc-validation: a scenario that drives real Claude Code on a converted command
  whose output contains code fences, asserting it renders without truncation.
  Runnable from this repo's harness (creds refreshed 2026-05-26).

## Validation

Migration is validated when: all 25 commands render through real Claude Code
without error; the static guard passes; cc-validation covers an output-with-fences
command; full unit suite green.

## Finalization Gate

(pending)

## References

- nexus-61fzg (P0 regression: triple-backtick truncates fenced-bang block)
- nexus-ln9y5 (5.1.2 fix that introduced Gap 1), nexus-t1b1k (5.1.1 brace-form fix)
- RDR-120 (storage substrate split), RDR-128 (single-writer)

## Revision History

- 2026-05-26: Drafted after the 5.1.2 inline-preamble regression (nexus-61fzg).
