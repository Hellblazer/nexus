---
title: "Hook-Enforced Tool Routing: PreToolUse as Backstop for Soft Guidance"
id: RDR-121
type: Architecture
status: draft
priority: medium
author: Hal Hildebrand
reviewed-by: self
created: 2026-05-19
accepted_date:
related_issues: []
related_rdrs: [RDR-008, RDR-024, RDR-045, RDR-105, RDR-120]
related_tests: []
implementation_notes: ""
---

# RDR-121: Hook-Enforced Tool Routing: PreToolUse as Backstop for Soft Guidance

> Revise during planning; lock at implementation.
> If wrong, abandon code and iterate RDR.

## Problem Statement

Nexus has a working catalogue of "preferred tool" rules: Serena for symbol-level work, `/nx:phase-review-gate` before phase close, `/nx:brainstorming-gate` before any production edit, `/nx:debug` after two failed fix attempts, `/nx:plan-audit` when a plan exists, T3 `nx search` before codebase exploration, `/nx:receiving-review` before acting on review feedback. Each rule lives in some combination of:

- The user's global `CLAUDE.md` (e.g. "Serena for symbols; Grep for text")
- The project `CLAUDE.md`
- `nx/skills/using-nx-skills/SKILL.md` (catted at SessionStart)
- `nx/hooks/scripts/subagent-start.sh` (injected at SubagentStart)
- Per-skill `description:` lines visible during skill discovery
- Persistent memory entries with `When to use` guidance

Despite all of this, agents reach for the default-available tool instead of the preferred one. Steve's Claude diagnosed it precisely: "Serena's tools are listed as 'deferred' (requiring a ToolSearch call to load schemas), while grep and Read are immediately available. The path of least resistance wins every time, and I rationalize it as 'just a quick check.'" The same pattern shows up in nexus: the agent grep-walks Python symbols instead of calling `jet_brains_find_symbol`; closes phase-review beads with `bd close` without running `/nx:phase-review-gate`; runs the third retry-fix instead of invoking `/nx:debug`; opens Edit on production code without `/nx:brainstorming-gate`.

The failure mode is universal and the diagnosis is consistent across instances. Documentation and session-start context are necessary but not sufficient. The agent reads them, agrees with them, and then rationalizes past them at the moment of action because the wrong tool is one tool call away while the right tool needs ToolSearch, a slash-command dispatch, or a multi-step preamble.

### Enumerated gaps to close

#### Gap 1: Session-start guidance is bypassable by path-of-least-resistance behavior

`using-nx-skills/SKILL.md` enumerates routing rules. Memory entries reinforce them. The agent has read both at session start. At decision time, the agent picks the easier tool anyway and writes a post-hoc rationalization ("just a quick check", "I know this codebase", "the gate would be overkill here").

#### Gap 2: Memory-loaded guidance is treated as optional context

Steve's instance: "I see [MEMORY.md] is available but I treat it as optional context rather than a mandatory precondition." Memory recall is a soft signal. The decision-time prompt offers all tools equally; nothing escalates the memory's recommendation above "FYI."

#### Gap 3: No enforcement layer at the action boundary

The skills themselves (`/nx:phase-review-gate`, `/nx:brainstorming-gate`) ARE hard-enforcement preambles (Python scripts the agent cannot reason past), but invoking them is soft. The hard part of "must run the gate" is the *decision* to run it, not the gate itself. Once invoked, the gate works; the gap is that the agent doesn't invoke it.

This RDR is the meta-solution to the pattern that `/nx:phase-review-gate` is one instance of: skill-level hard enforcement only protects you after you've decided to invoke the skill. To force the invocation, the enforcement has to move earlier, to the moment the agent reaches for the wrong tool.

## Context

### Background

Three precedents in nexus that already use the hook-at-action-time pattern:

- **`PreToolUse` hook on `Bash` matcher** at `nx/hooks/scripts/pre_close_verification_hook.sh`. Already in production for verifying pre-close conditions. The hook intercepts Bash invocations and runs a verification check before the command executes.
- **`PostToolUse` hook on `Write|Edit` matcher** at `nx/hooks/scripts/divergence-language-guard.sh`. Already in production for language-divergence checks after edits.
- **`PermissionRequest` hook on `mcp__plugin_nx_.*` matcher** at `nx/hooks/scripts/auto-approve-nx-mcp.sh`. Already in production for auto-approving nx MCP tools.

The hook infrastructure is mature. The pattern this RDR proposes generalizes the same shape (intercept-at-action-time, with redirect) to the tool-routing problem.

Three other RDRs frame the why:

- **RDR-008** established the routing convention for nx skills.
- **RDR-024** established RDR-process guardrails to prevent implementation before gate/accept; conceptually identical to what this RDR proposes for tool routing.
- **RDR-105** moved T1 into a host-service with env-passdown discovery; similar substrate-level enforcement that closed a class of silent-failure bugs by construction.

And one direct precedent:

- **RDR-120 § Enforcement Backstops** (drafted 2026-05-19) catalogues four working enforcement mechanisms and three tooling gaps for the substrate moratorium. The "tooling gaps" list includes three PreToolUse-shaped hooks (cross-RDR moratorium awareness in `/nx:rdr-create`, scope-gain check in `/nx:phase-review-gate`, `bd create` grep against banned-topic lists). RDR-121 generalizes those into a single architectural pattern.

### Technical Environment

Hook events available (per Claude Code SubagentStart hook contract verified in `tests/cc-validation/`):

- `PreToolUse`: fires before a tool call; can block via stderr exit code 2.
- `PostToolUse`: fires after a tool call; can warn but cannot retroactively block.
- `PermissionRequest`: fires when permission is requested for a gated tool.
- `SessionStart`, `SubagentStart`: fire once per session/subagent; not action-time.
- `Stop`, `StopFailure`: fire at task end.

For tool routing, `PreToolUse` is the load-bearing event. It runs before the agent's tool call executes, can inspect the tool name and arguments, and can block with a structured stderr message that the agent receives as feedback.

Matchers in `hooks.json` are regex against the tool name. Current production matchers: `Bash`, `Write|Edit`, `mcp__plugin_nx_.*`. The matcher can target the default tool (e.g. `Bash` for grep / `bd close`, `Edit|Write` for production edits) and the hook script inspects the call arguments to decide whether to fire.

## Scope Boundaries

### In scope

- A framework / convention for authoring PreToolUse routing hooks: shared bash/python helpers, escape-mechanism contract, output format, test discipline.
- A catalogued set of routing rules each candidate for a hook, with explicit per-rule decisions (block vs warn vs leave-as-soft).
- An initial cohort of 2-3 hooks shipped under the framework, with regression tests.
- Documentation of when a "preferred tool" rule should become a hook vs stay as session-start guidance.

### Out of scope

- Aggressive enforcement of every routing rule from `using-nx-skills/SKILL.md` (would create hook fatigue; only the load-bearing rules earn hooks).
- Hooks on subagent invocations themselves (subagents already get conditional context injection via `subagent-start.sh`; layering PreToolUse on top would conflict).
- Replacing session-start guidance (the hooks are a backstop, not a substitute; session-start context still does the bulk of the routing work).
- Cross-IDE coordination (hook contract is Claude Code; other IDEs are out of scope).

## Catalogue of Known Routing Failures

This is the inventory the design must address. Each row is a place where the preferred tool is documented but the default tool is what gets reached for.

| Default tool reached for | Preferred tool | Documented in | Hook candidate? |
|---|---|---|---|
| `Bash: grep .*\.py` on symbol names | `jet_brains_find_symbol` (Serena) | CLAUDE.md, using-nx-skills | Yes: PreToolUse Bash matcher with `--include='*.py' --include='*.swift' --include='*.java'` pattern detection |
| `Bash: bd close <bead>` on a phase-review bead | `/nx:phase-review-gate <rdr-id> --phase N` first | RDR-120, using-nx-skills (after this branch lands) | Yes: PreToolUse Bash matcher on `bd close.*` with bead-title inspection |
| `Edit` / `Write` on production code | `/nx:brainstorming-gate` first | using-nx-skills | Maybe: high false-positive risk on tests and docs; needs tight matcher |
| `Bash: pytest` retry after second failure | `/nx:debug` | using-nx-skills, memory feedback_robot_no_fatigue | Maybe: needs session-state (retry count) which PreToolUse can't see; better as PostToolUse warn |
| `Edit` on code with an in-flight plan | `/nx:plan-audit` first | using-nx-skills | Yes: PreToolUse Edit/Write matcher checking for active plan in T2 |
| `Edit` implementing review feedback | `/nx:receiving-review` first | CLAUDE.md feedback, using-nx-skills | Hard: requires understanding that the edit is "implementing feedback" vs ordinary work |
| `Bash: ls / find` for codebase exploration | T3 `nx search` first | using-nx-skills Red Flags | Yes: PreToolUse Bash matcher on `find . -name` / `ls -R` shapes |
| `bd create` on substrate-adjacent work during RDR-120 moratorium | Stop and read RDR-120 § Scope Boundaries first | RDR-120 | Yes: PreToolUse Bash matcher on `bd create.*` with description regex against banned-topic list |
| `git add -A` / `git add .` | `git add <explicit path>` | CLAUDE.md feedback, memory feedback_no_git_add_all | Yes: PreToolUse Bash matcher; simple regex |

Nine candidates. Some are unambiguous wins (git-add-all, the phase gate, Serena-for-symbols). Some require state the hook can't easily see (retry count, "is this implementing feedback"). The framework needs to support both the easy cases and a documented "this rule stays soft" decision for the hard ones.

## Proposed Solution

### Approach

Three phases. Each ships independently and is reversible.

**Phase 1: Hook framework**

- New directory `nx/hooks/scripts/routing/` for routing hooks.
- Shared bash helper `nx/hooks/scripts/routing/_lib.sh` providing:
  - `should_skip_for_reason(<reason>)`: checks the last command in the agent's call log for a `# routing-allow: <reason>` token (≥8-char reason), mirroring the ε-lint allowlist pattern from `tests/test_no_direct_catalog_writes_outside_projector.py` (RDR-101 Phase 3).
  - `emit_block(<message>)`: emits a structured stderr block telling the agent what to do instead; exits 2.
  - `emit_warn(<message>)`: emits to stderr but exits 0.
  - `log_routing_event(<rule>, <outcome>)`: appends a JSON line to `~/.config/nexus/routing_log.jsonl` for telemetry on which rules fire and how often.
- A `nx/hooks/scripts/routing/registry.yaml` enumerating active rules with metadata: rule name, hook script path, matcher, mode (`block` / `warn`), rationale, escape token.
- Test fixtures at `tests/test_routing_hooks.py` for each rule: positive case (preferred tool path) + negative case (default tool path with redirect) + escape case (allowlisted with reason).

**Phase 2: Initial cohort**

Three rules with the highest confidence and lowest false-positive risk. Each ships as its own hook script under `nx/hooks/scripts/routing/`.

1. **`grep_for_symbols_redirects_to_serena`**: Bash matcher; detects `grep` / `rg` against `*.py`, `*.swift`, `*.java`, `*.ts`, `*.tsx`, `*.go`, `*.rs` with patterns that look like identifier searches (no spaces, no regex metacharacters). Emits a block message: "This looks like a symbol search. Use `mcp__plugin_sn_serena__jet_brains_find_symbol` for symbol definitions, `mcp__plugin_sn_serena__jet_brains_find_referencing_symbols` for callers. To override, add `# routing-allow: <reason ≥8 chars>` to the command."
2. **`phase_review_close_requires_gate`**: Bash matcher; detects `bd close .*` where the bead title (looked up via `bd show`) contains "phase" or "review". Checks for a recent `/nx:phase-review-gate <rdr-id> --phase N` invocation in the session log; blocks if absent. RDR-120 § Enforcement Backstops cites this as a load-bearing follow-on.
3. **`git_add_all_redirects_to_explicit_paths`**: Bash matcher; detects `git add -A`, `git add .`, `git add --all`. Emits a block message: "Stage by explicit path. Wildcard adds pull in unrelated untracked drafts (`feedback_no_git_add_all.md`). To override, add `# routing-allow: <reason ≥8 chars>` to the command."

**Phase 3: Telemetry and refinement**

After 30 days of Phase 2 in production:
- Read `~/.config/nexus/routing_log.jsonl`. For each rule, count fire-rate, block-rate, escape-rate.
- High fire-rate + high escape-rate = the rule is producing false positives; refine matcher or downgrade to warn.
- Low fire-rate = either the agent is well-behaved or the rule isn't catching the real failures; revisit.
- Zero fires for a rule that's supposed to fire = matcher is wrong; spike and fix.

The telemetry loop closes the long-term question: which rules actually catch failures and which are theatre.

### Existing Infrastructure Audit

| Proposed component | Existing precedent | Decision |
|---|---|---|
| PreToolUse hook on Bash | `nx/hooks/scripts/pre_close_verification_hook.sh` | Extend pattern; same hooks.json entry shape. |
| PreToolUse hook on Edit/Write | (none on PreToolUse; PostToolUse precedent in `divergence-language-guard.sh`) | New; the divergence guard is the structural template, just moved to PreToolUse. |
| Allowlist token convention | `# epsilon-allow: <reason ≥8 chars>` at `tests/test_no_direct_catalog_writes_outside_projector.py` | Direct port; rename token to `# routing-allow:` to distinguish concern. |
| Telemetry log | `~/.config/nexus/` config dir convention (RDR-105) | Extend; add `routing_log.jsonl`. |
| Hook script shared lib | `nx/hooks/scripts/_run_python_hook.sh` | Extend pattern; new `_lib.sh` for routing-specific helpers. |
| Test harness | `tests/cc-validation/` scenarios | Extend pattern; new `tests/test_routing_hooks.py` for unit-level coverage and `tests/cc-validation/scenarios/14_routing_*.sh` for E2E. |

### Decision Rationale

**PreToolUse, not PostToolUse.** Post-tool warning fires after the wrong action has happened; the agent already wasted the call. PreToolUse blocks the wrong action before it executes. The cost difference is real: a blocked Bash call is free; a warned-after-the-fact grep means the agent has the wrong output in context and has to remember to redo it with Serena. Trust falls off when warnings don't actually prevent anything.

**Block, not warn, for high-confidence rules.** Warnings get ignored by the same path-of-least-resistance pressure that motivated this RDR. The catalogue above marks each rule with `block` or `warn`; the default for confident rules is `block`.

**Escape mechanism is non-negotiable.** Hard blocks without an escape produce hook fatigue. The `# routing-allow: <reason ≥8 chars>` pattern lets the agent (or human user) opt out of the block when the rule is genuinely wrong for this case. The reason text is what makes the escape auditable; routing-log captures the escape events so high-escape-rate rules surface as refactoring targets.

**Telemetry from day one.** The routing-log enables the Phase 3 refinement loop. Without it, we'd be running rules forever without knowing if they catch real failures.

**One rule = one hook script.** Resist the temptation to multiplex rules into a single mega-hook. Per-rule scripts mean per-rule tests, per-rule disable (just remove the hooks.json entry), per-rule iteration without affecting siblings. The shared `_lib.sh` keeps DRY.

**Catalogue what's NOT a hook.** Some rules (debug-after-two-fails, receiving-review) require state the hook can't easily see. Document those explicitly in the registry so future authors don't try to build them as hooks and silently produce false positives. The catalogue above does this.

## Alternatives Considered

### Alternative 1: Status quo (soft guidance only)

**Description**: Keep session-start guidance and memory entries as the only mechanism. Trust agent self-discipline.

**Cons**: This is what we have. Steve's Claude documents it explicitly fails. The pattern is universal across Claude instances. Not solving the problem.

**Reason for rejection**: The problem statement.

### Alternative 2: PostToolUse warnings, no blocks

**Description**: Replace PreToolUse blocks with PostToolUse warnings. Agent sees a warning after the wrong call, can redo if appropriate.

**Pros**: Lower friction. No false positives that block legitimate work. Warnings are educational without being adversarial.

**Cons**: The wrong action has already happened. Agent has grep output in context now and may not actually redo with Serena. Same path-of-least-resistance pressure that motivated this RDR; warnings are exactly the soft mechanism that's not working today.

**Reason for rejection**: Warnings are the failure mode, not the fix. Reserve PostToolUse warnings for low-confidence rules where blocking would over-fire.

### Alternative 3: Hard block, no escape mechanism

**Description**: PreToolUse hooks that block unconditionally. No `# routing-allow:` token.

**Pros**: Maximum enforcement. Zero escape pressure on the agent.

**Cons**: False positives are unfixable; legitimate edge cases (debug grep that genuinely should be a grep, a phase-close that genuinely doesn't need the gate because the phase had zero §Approach items) become impossible. Hook fatigue manifests as the agent finding circumlocutions ("let me cat the file and look manually" instead of grep when grep is blocked). Hooks become adversarial.

**Reason for rejection**: Escape mechanism is what makes hard enforcement humane. The ε-lint precedent shows this works.

### Alternative 4: One mega-hook with rule registry

**Description**: One bash hook on `Bash` matcher (and one on `Edit|Write`); the hook reads a registry of rules and dispatches internally.

**Pros**: Single registration in `hooks.json`; rules live in YAML; easy to add/remove rules without touching hooks config.

**Cons**: Per-rule failure isolation is harder. A bug in one rule affects all rules. Test fixtures get tangled. Refactoring a single rule requires understanding the dispatch layer.

**Reason for rejection**: The per-rule-script approach is more maintainable for a long-lived rule set. The mega-hook would become the kind of monolith that's hard to evolve. Keep the dispatch flat.

### Briefly Rejected

- **AI critique in PreToolUse**: would add latency and cost; the rules are structural, not LLM-mediated.
- **Settings-only enforcement (no hooks)**: settings can enable/disable tools but can't conditionally redirect; not expressive enough.

## Trade-offs

### Consequences

- (+) Closes the path-of-least-resistance failure mode for high-confidence rules.
- (+) Telemetry loop produces evidence on which rules actually catch failures.
- (+) Escape mechanism preserves agent agency without creating unfixable false positives.
- (+) Each rule's effectiveness is observable and refineable.
- (−) Hook latency on every Bash / Edit / Write call (estimate: <50ms per check; per-call hook overhead is documented as <100ms in Claude Code's hook contract).
- (−) Maintenance burden grows linearly with rule count; need to keep the catalogue curated.
- (−) Hook authoring is a new discipline; bad hooks produce hook fatigue, which is worse than no hook.
- (−) Cross-platform shell compatibility (bash 3.2 on macOS, bash 5.x elsewhere) is a known footgun; the existing `subagent-start.sh` documents bash 5.3 heredoc deadlocks; routing hooks inherit the same risk.

### Risks and Mitigations

- **Risk**: Hook misfires on legitimate calls and produces hook fatigue.
  **Mitigation**: Tight matchers, telemetry from day one, refinement loop in Phase 3. Escape mechanism for unavoidable false positives.
- **Risk**: Hook scripts have bugs that break Bash entirely.
  **Mitigation**: Per-rule scripts (per-rule failure isolation). All hooks return exit 0 on internal error so a broken hook degrades to no-op rather than breaking Bash. Regression test for each hook covers the no-op-on-internal-error path.
- **Risk**: Hooks slow down every Bash call by enough to be noticeable.
  **Mitigation**: Budget is <50ms per hook; each new hook adds work that runs in parallel via the hook chain. Telemetry includes hook latency; refactor if any hook trends above budget.
- **Risk**: Agents (or humans) abuse `# routing-allow:` to silence inconvenient rules.
  **Mitigation**: Telemetry tracks escape-rate per rule. High escape-rate triggers a review: either the rule is wrong (refine) or the agent is being undisciplined (raise to the user). Reason text on every escape makes the audit possible.

### Failure Modes

- **Hook misfires on the wrong tool name**: hook is a no-op (matcher didn't match); harmless.
- **Hook misfires on the right tool but wrong argument shape**: emits a block on a legitimate call. Mitigation: tight matcher, escape mechanism, telemetry alert.
- **Hook script crashes**: PreToolUse exit code other than 0 or 2 is treated by Claude Code as a hook failure; tool call proceeds. Net effect: hook silently no-ops. Logged for inspection.
- **Agent never invokes the preferred tool even after block**: the block message must include the exact preferred-tool invocation. The agent retries with the right tool. If the agent fails to do this, the issue is prompt-level, not hook-level; raise to user.

## Implementation Plan

### Prerequisites

- [ ] `/nx:phase-review-gate` restored to main (RDR-120 P0 prerequisite; this RDR's Phase 2 hook 2 depends on it).
- [ ] Existing hook tests green on main.

### Minimum Viable Validation

One end-to-end demo per phase:

| Phase | MVV |
|---|---|
| P1 | A test fires `_lib.sh emit_block` from a stub hook; verifies the block message reaches the agent and exit code is 2. Escape token allows the call to proceed. |
| P2 | Each of the three initial hooks: positive case (preferred path) succeeds, negative case (default path) blocks with redirect message, escape case (with `# routing-allow:`) succeeds. |
| P3 | `routing_log.jsonl` accumulates entries for 30 days of normal use. A telemetry report enumerates per-rule fire-rate, block-rate, escape-rate. |

### Phasing

**P1: Framework + scaffolding** (week 1)
- `nx/hooks/scripts/routing/_lib.sh` with `should_skip_for_reason`, `emit_block`, `emit_warn`, `log_routing_event`
- `nx/hooks/scripts/routing/registry.yaml` (empty initially)
- `tests/test_routing_hooks.py` with framework tests
- Documentation: `nx/hooks/scripts/routing/README.md` describing the hook authoring convention

**P2: Initial cohort** (week 2-3)
- `grep_for_symbols_redirects_to_serena.sh` + tests
- `phase_review_close_requires_gate.sh` + tests
- `git_add_all_redirects_to_explicit_paths.sh` + tests
- `hooks.json` entries for each; `registry.yaml` populated
- E2E scenarios at `tests/cc-validation/scenarios/14_routing_*.sh`

**P3: Telemetry** (week 4)
- Telemetry helper `_lib.sh:log_routing_event` writes to `~/.config/nexus/routing_log.jsonl`
- CLI subcommand `nx hook routing-stats` reads the log and reports per-rule fire/block/escape rates
- Documentation: `docs/cli-reference.md` entry for `nx hook routing-stats`

**P4: 30-day soak and refinement** (week 8)
- Read telemetry; refine matchers; downgrade or remove rules with high escape-rate; add rules with proven failure modes.
- Open follow-on RDR if the catalogue grows enough to need its own architecture decisions.

### Day 2 Operations

| Operation | Command |
|---|---|
| List active routing rules | `cat nx/hooks/scripts/routing/registry.yaml` |
| Check telemetry | `nx hook routing-stats` |
| Add a rule | New script in `routing/`; new entry in `registry.yaml`; new entry in `hooks.json`; new tests |
| Disable a rule | Remove from `hooks.json`; keep script + tests for future re-enable |
| Override at call time | Append `# routing-allow: <reason ≥8 chars>` to the command |

### New Dependencies

None. Stdlib bash + python; existing `~/.config/nexus/` convention.

## Test Plan

- **Scenario**: stub hook fires `emit_block`. **Verify**: stderr contains the block message; exit code 2; tool call does not execute.
- **Scenario**: stub hook fires `emit_block`; user retries with `# routing-allow: <reason>`. **Verify**: stub hook fires `should_skip_for_reason`, returns 0, tool call proceeds.
- **Scenario**: `grep -r MyFunction src/nexus/ --include='*.py'` triggers `grep_for_symbols_redirects_to_serena`. **Verify**: block emitted, redirect message names `jet_brains_find_symbol`.
- **Scenario**: `grep -r "TODO" src/nexus/ --include='*.py'` (text search, multi-character non-identifier pattern). **Verify**: hook is no-op; legitimate text grep proceeds.
- **Scenario**: `bd close nexus-abc1` where bead title contains "Phase 3 review". **Verify**: hook checks session log for `/nx:phase-review-gate` invocation; if absent, blocks.
- **Scenario**: `bd close nexus-abc1` where bead title is "fix the formatter bug". **Verify**: hook is no-op (no phase/review pattern in title).
- **Scenario**: `git add -A` triggers `git_add_all_redirects_to_explicit_paths`. **Verify**: block emitted.
- **Scenario**: `git add src/nexus/foo.py` (explicit path). **Verify**: hook is no-op.
- **Scenario**: Hook script throws Python exception. **Verify**: tool call proceeds; error logged.
- **Scenario**: Hook chain latency. **Verify**: each hook completes under 50ms p95 on a representative call set.

## Validation

### Testing Strategy

1. **Unit tests** (`tests/test_routing_hooks.py`): per-rule positive / negative / escape cases.
2. **E2E tests** (`tests/cc-validation/scenarios/14_routing_*.sh`): full Claude Code hook invocation.
3. **Soak telemetry**: 30 days of `routing_log.jsonl` post-P3 ship; per-rule fire/block/escape rates inform Phase 4 refinement.

### Performance Expectations

Per-hook budget: <50ms p95. Cumulative per-Bash-call hook overhead (current hooks plus this RDR's additions): <200ms p95. If exceeded, refactor the slowest hook before adding new ones.

## Finalization Gate

To be completed during gate.

### Contradiction Check

To be completed.

### Assumption Verification

Critical assumptions to verify:
- **A1**: PreToolUse hooks can block tool execution via stderr exit code 2. **Status**: Documented (Claude Code hook contract). **Method**: Source Search of Claude Code hook documentation + existing nexus hook scripts (pre_close_verification_hook.sh).
- **A2**: Hook chain latency is bounded; <50ms per hook is achievable. **Status**: Unverified. **Method**: Spike against the stub hook in P1.
- **A3**: Tight matchers for symbol-grep detection achieve low false-positive rate. **Status**: Unverified. **Method**: Build the matcher, run against a representative corpus of historical grep calls (e.g. from this session's transcript), measure false-positive rate.
- **A4**: Escape mechanism is sufficient to handle edge cases without rules being silenced wholesale. **Status**: Unverified. **Method**: 30-day telemetry post-P2; escape-rate per rule below some threshold (TBD) signals the mechanism works.

### Scope Verification

This RDR is scoped to the routing-enforcement pattern. Specifically OUT of scope:
- Routing-rule additions beyond the initial cohort of 3 (those are P4 follow-on work).
- Hooks on subagent invocation (PreToolUse is for synchronous tool calls; subagent dispatch is a different concern).
- IDE-agnostic enforcement (Claude Code hook contract is the substrate).

### Cross-Cutting Concerns

- **Versioning**: hook contracts pinned to Claude Code 2.x hook schema; document the assumed schema version.
- **Build tool compatibility**: bash 3.2 (macOS) and bash 5.x (Linux) must both work; heredoc deadlocks per `subagent-start.sh` precedent apply.
- **Licensing**: no new third-party deps.
- **Deployment model**: hooks ship with the nx plugin; no separate install.
- **IDE compatibility**: N/A (Claude Code only).
- **Incremental adoption**: each rule ships independently; no global flag.
- **Secret/credential lifecycle**: hooks do not read or emit secrets.
- **Memory management**: telemetry log grows; needs rotation policy. P3 to spec.

### Proportionality

Document is sized to the architectural pattern, not to any single hook. The catalogue of nine candidate rules justifies the framework investment. Per-hook complexity is small (~30-50 lines of bash + 30-50 lines of tests per rule); the leverage comes from authoring discipline + telemetry, not from clever hook logic.

## Open Questions

- **Telemetry retention**: how long does `routing_log.jsonl` keep entries? Default 90 days, rotate weekly? Open for P3 to settle.
- **Cross-IDE generalization**: if other IDEs gain hook contracts compatible enough, can this framework be ported? Out of scope; flag for revisit if it ever matters.
- **Hook-author training**: a new contributor needs to understand the routing-hook discipline. Where does that guidance live? Probably in `nx/hooks/scripts/routing/README.md` produced during P1.
- **Interaction with `using-nx-skills/SKILL.md`**: should every routing-hook also be documented in the session-start guidance? Probably yes. Session-start tells the agent "we have rules"; the hooks enforce them. They reinforce each other.
- **Should `bd create` against an active moratorium's banned-topic list be a hook?** RDR-120 § Enforcement Backstops names this as a gap. It belongs in this RDR's catalogue (Phase 2 follow-on). Currently listed as a candidate but not in the initial cohort.

## References

- Tombstoned RDR-110-119 arc: `docs/rdr/rdr-{110,111,112,113,118,119}-*.md`.
- Postmortem: `docs/postmortem/2026-05-16-rdr110-113-remediation-chain.md`.
- RDR-120: `docs/rdr/rdr-120-storage-substrate-split.md` § Enforcement Backstops names three of the candidate rules.
- RDR-008: original nx skills routing convention.
- RDR-024: RDR-process guardrails (gate-before-implement); analogous enforcement at the RDR-lifecycle level.
- RDR-045: post-implementation verification gate; same shape applied to implementation-completion enforcement.
- RDR-105: T1 chroma architecture with env-passdown discovery; substrate-level enforcement precedent.
- `nx/hooks/scripts/pre_close_verification_hook.sh`: existing PreToolUse hook on Bash matcher (production).
- `nx/hooks/scripts/divergence-language-guard.sh`: existing PostToolUse hook on Write|Edit (production).
- `nx/hooks/scripts/auto-approve-nx-mcp.sh`: existing PermissionRequest hook (production).
- `tests/test_no_direct_catalog_writes_outside_projector.py`: ε-lint allowlist-token precedent (RDR-101 Phase 3).
- `feedback_no_git_add_all.md`: user memory entry documenting one of the rules.
- `feedback_robot_no_fatigue.md`: user memory entry pointing at debug-after-N-fails as a discipline gap.

## Revision History

- 2026-05-19: Draft. Filed in response to Steve's Serena-adoption issue and as the meta-solution to the pattern that RDR-120's `/nx:phase-review-gate` restoration is one instance of.
