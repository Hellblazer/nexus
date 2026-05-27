---
title: "Command Preambles via the nx CLI: Thin Commands, Tested Logic, No Inlined Bash"
id: RDR-130
type: Architecture
status: closed
priority: high
author: Hal Hildebrand
reviewed-by: self
created: 2026-05-26
accepted_date: 2026-05-26
closed_date: 2026-05-27
related_issues: [nexus-61fzg, nexus-ln9y5, nexus-t1b1k]
related_rdrs: [RDR-120, RDR-128]
supersedes: []
related_tests: [tests/test_plugin_structure.py, tests/test_command_context_command.py, tests/test_command_context_detector.py, tests/cc-validation/scenarios/21_rdr130_output_fence_safe.sh, tests/cc-validation/scenarios/24_rdr130_agent_relay_renders.sh, tests/cc-validation/scenarios/25_rdr130_continuation_renders.sh]
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

## Research Findings

Empirically established this cycle and verified against real Claude Code via the
`tests/cc-validation` harness (scenarios 19, 20, 21, 22; fresh creds 2026-05-26).

- Valid CC bash-injection forms: inline `` !`cmd` `` and fenced ```` ```! ````.
  The inline single-line form — which every RDR-130 command uses — executes and
  injects its stdout (VERIFIED scenario 22, real CC, PATH-independent probe).
  `!{ }` braces are NOT a recognized form (emit raw) — confirmed by scenario 19
  Part B (negative control).
- A fenced-bang block closes at the first literal triple-backtick in the source;
  4-backtick fences do not change this (probe-confirmed). The block source must
  therefore contain zero triple-backticks before its closing fence — this is the
  nexus-61fzg root cause.
- **Critical assumption, VERIFIED (scenario 21):** injected **output** containing
  triple-backticks is inserted as plain text and is NOT re-parsed. A `` ```! ``
  block whose source has no literal triple-backtick (built via `printf` octal
  `\140`) but whose output emits ` ``` ` fences rendered intact. RDR-130 rests on
  this — the `nx` preamble subcommands emit markdown tables / fences in their
  output, and that is now confirmed safe rather than merely doc-claimed.
- `$ARGUMENTS` is substituted by CC in the block; `$CLAUDE_PLUGIN_ROOT` is empty
  in command-bash context (scoped to hooks/MCP/LSP) — so commands must call an
  on-PATH binary (`nx`), not a plugin-relative script.
- The `nx` CLI is always on PATH (it is the package entry point).

Conclusion: the RDR-130 design (one-line `nx` invocation per command; logic in
the `nx` CLI emitting markdown) is mechanically sound and de-risked. Full T2
record: `nexus_rdr/130-research-1`.

## Proposed Solution

**Keep the injected bash trivial; move all preamble logic into the `nx` CLI.**

Each command becomes a single-line invocation with no literal triple-backtick and
no inlined logic, e.g.:

    !`nx rdr preamble list -- "$ARGUMENTS"`

The invoked `nx` subcommand does the work in normal, unit-tested Python and prints
the preamble markdown (fences and all — output is not re-parsed). This eliminates
Gaps 1-4 at once: the `.md` source has nothing for the fence parser to choke on,
no `$CLAUDE_PLUGIN_ROOT` dependency, and the logic is testable like any CLI command.

**Argument passing.** CC substitutes `$ARGUMENTS` and the double-quotes prevent
word-splitting; the `--` terminator stops `nx` option-parsing so a leading-dash
argument is treated as a positional. This does NOT escape embedded quotes /
backticks / `$` in the argument value, so Gap 2 is *narrowed*, not eliminated. In
practice RDR command arguments are numeric IDs and flags (safe); agent-relay
commands that accept freeform text must document the constraint or pass the raw
arg through without shell re-evaluation.

**Command inventory (25).** The migration covers all 25 `conexus/commands/*.md`:

- **RDR-lifecycle (9):** rdr-create, rdr-list, rdr-show, rdr-gate, rdr-accept,
  rdr-close, rdr-research, rdr-audit, phase-review-gate. Logic already Python in
  `conexus/resources/rdr_commands/*.py`; port to `nx rdr preamble <name>`.
- **Agent-relay (16):** analyze-code, architecture, create-plan, implement, debug,
  deep-analysis, enrich-plan, knowledge-tidy, pdf-process, plan-audit, research,
  review-code, substantive-critique, test-validate, nx-preflight, continuation.
  Port the shell preambles (working-dir, the unified ~21-ecosystem project-type
  detector from nexus-ln9y5, bead/git context) into `nx` subcommands (e.g.
  `nx command-context <name>`).
- **Special case — `continuation`:** its preamble is a file-WRITER (writes a dated
  handoff doc to `/tmp` and emits a `cat <path>` line), not a context-printer. It
  needs its own subcommand contract (e.g. `nx command-context continuation` that
  writes the file as a side effect and prints the retrieval line), distinct from
  the read-and-print pattern. Flagged so P2 does not discover it mid-sprint.

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
- Adds `nx` process-startup cost per command invocation (Click + module import is
  ~200-500ms cold; acceptable for interactive commands, not a hot loop). Measure
  once during P1 to confirm across slow hardware.
- Reduces the shell-quoting surface to single-arg `$ARGUMENTS` expansion (Gap 2
  narrowed, not eliminated — see Proposed Solution §Argument passing).
- Centralizes preamble logic in the CLI where it is testable and reusable.

## Implementation Plan

- **P0 — DONE (shipped in 5.1.3, nexus-61fzg):** stripped literal triple-backticks
  from the 17 affected blocks (dropped shell fence-echoes; `chr(96)*3` in the 3
  Python ones) as interim relief. P1 begins from the 5.1.3 state, not the broken
  5.1.2 state.
- **P1:** RDR commands -> `nx rdr preamble <name>`. Port the 9 scripts; flip the 9
  `.md` files to one-line `nx` invocations; delete `resources/rdr_commands/*.py`
  and the inline-sync test. **Storage-boundary lint (RDR-128, live on CI):** the
  ported logic moves into `src/nexus/commands/rdr.py`, which IS in the lint scan
  scope (unlike the `conexus/` resources today). The existing scripts open
  `T2Database` directly; in `src/nexus/` that is a new construction site the lint
  will flag. For each preamble subcommand that reads T2, either route through the
  T2 daemon (`T2Client`) or annotate `# epsilon-allow: short-lived read-only
  preamble CLI` and acknowledge the epsilon-allow count delta vs the RDR-128
  baseline. Resolve this per subcommand or P1 CI fails.
- **P2:** agent-relay commands -> `nx command-context <name>` (or per-group verbs).
  Port the shell logic + the ~21-ecosystem detector to Python; flip the 16 `.md`.
  Handle `continuation` as the file-writer special case (see Proposed Solution).
  Same storage-boundary-lint rule applies to any T2/T3 reads.
- **P3:** retire the inline machinery; the storage of preamble logic is the CLI.

## Test Plan

- Unit tests per `nx` preamble subcommand (deterministic, no real CC needed).
- Static guard (`test_plugin_structure`): every command bash block is a single-line
  `nx` invocation; reject any literal triple-backtick and any multi-line/heredoc
  body inside an injection block. This matches CC's real parser constraint and
  would have caught both Gap 1 and the t1b1k class.
- cc-validation (real Claude Code; runnable from this repo's harness): the
  mechanism is already covered — scenario 21 (output-with-fences renders intact),
  scenario 22 (inline `` !`cmd` `` executes + injects stdout), scenario 20
  (previously-broken command runs clean). P1/P2 add a scenario per migrated
  command category that asserts the `` !`nx …` `` form renders the expected
  preamble output.

## Validation

Migration is validated when: all 25 commands render through real Claude Code
without error; the static guard passes; cc-validation covers an output-with-fences
command; full unit suite green.

## Finalization Gate

**PASSED — 2026-05-26.** Layer 1 (structural) and Layer 2 (assumption audit) pass;
all assumptions are evidenced by cc-validation, none left "Assumed" without risk
notes. Layer 3 (substantive-critic): 0 Critical, 5 Significant, 3 Observations.
All 5 Significant findings were resolved in-place before accept:
1. Inline `` !`nx …` `` execution — VERIFIED (added scenario 22).
2. Storage-boundary lint collision on the `src/nexus/` move — addressed in P1
   (epsilon-allow or `T2Client` per preamble subcommand; acknowledge baseline delta).
3. 25-command inventory enumerated; `continuation` flagged as a file-writer special case.
4. P0 corrected to DONE (shipped in 5.1.3).
5. `$ARGUMENTS` quoting residual documented (`--` terminator; Gap 2 narrowed, not eliminated).
Gate result in T2: `nexus_rdr/130-gate-latest`.

## References

- nexus-61fzg (P0 regression: triple-backtick truncates fenced-bang block)
- nexus-ln9y5 (5.1.2 fix that introduced Gap 1), nexus-t1b1k (5.1.1 brace-form fix)
- RDR-120 (storage substrate split), RDR-128 (single-writer)

## Revision History

- 2026-05-26: Drafted after the 5.1.2 inline-preamble regression (nexus-61fzg).
- 2026-05-26: Gate PASSED (0 critical, 5 significant resolved in-place). Inline
  form verified (scenario 22); P1 storage-boundary-lint step, 25-command
  inventory, continuation special case, P0-done, and `$ARGUMENTS` quoting added.
- 2026-05-27: CLOSED. Shipped P0 (5.1.3 interim fence-strip), P1 (5.1.4 — 9 RDR
  commands via `nx rdr preamble`), P2 (5.1.5 — 16 agent-relay commands via
  `nx command-context`), P3 (residue retired, docs updated, holistic validation).
  All 25 commands are thin single-line `nx` invocations; no inlined bash; a
  static guard enforces the form. P3-GATE §Validation cross-walk PASSED (static
  guard 25/25, output-fence cc-validation scenario 21, categorical real-CC
  scenarios 23/24/25, full suite green). Out-of-scope follow-ups tracked:
  nexus-exa2p (daemon side-orphan reap), nexus-u3mfr (cold-start probe timeout).
