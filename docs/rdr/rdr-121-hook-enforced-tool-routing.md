---
title: "Hook-Enforced Tool Routing: PreToolUse as Backstop for Soft Guidance"
id: RDR-121
type: Architecture
status: closed
priority: medium
author: Hal Hildebrand
reviewed-by: self
created: 2026-05-19
accepted_date: 2026-05-20
closed_date: 2026-05-20
close_reason: implemented
shipped_in: 4.33.0
related_issues: []
related_rdrs: [RDR-008, RDR-024, RDR-045, RDR-105, RDR-120]
related_tests:
  - tests/test_routing_hooks.py
  - tests/test_routing_phase_review_close.py
  - tests/test_routing_grep_for_symbols.py
  - tests/test_routing_git_add_all.py
  - tests/test_phase_review_sentinel.py
  - tests/test_hook_routing_stats.py
implementation_notes: |
  Shipped in conexus 4.33.0 (2026-05-20). Six implementation beads
  closed (mzvwa.1 through .6); mzvwa.7 (P4 30-day soak review) left
  open as a tracked follow-up, fires +30 days post-merge (~2026-06-19).
  The soak runs against a closed RDR by design; observations either
  get a small RDR addendum or follow-on beads.

  Problem Statement closure pointers (Pass 2):
  - Gap 1 (session-start guidance bypassable): enforcement at
    nx/hooks/scripts/routing/grep_for_symbols_redirects_to_serena.py:1
    (and the other two cohort hooks). PreToolUse fires regardless of
    whether the session-start preamble was read.
  - Gap 2 (memory-loaded guidance is optional context):
    nx/hooks/scripts/routing/phase_review_close_requires_gate.py:1.
    The phase-review-close rule was a recurring memory feedback entry
    (feedback_phase_closeout_scope_audit); the hook makes the
    cross-walk a deterministic precondition for `bd close`.
  - Gap 3 (no enforcement layer at the action boundary):
    nx/hooks/scripts/routing/_lib.py:1 + nx/hooks/hooks.json.
    The framework gives every rule a PreToolUse action-boundary
    enforcement surface with the contract documented in
    nx/hooks/scripts/routing/README.md.
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

- `PreToolUse`: fires before a tool call; can block by emitting `{"hookSpecificOutput": {"hookEventName": "PreToolUse", "permissionDecision": "deny", "reason": "..."}}` on stdout and exiting 0 (A1-verified protocol; the stderr-exit-2 path is documented but the JSON envelope is the contract this RDR uses).
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

Design lifted from in-tree precedent `nx/hooks/scripts/pre_close_verification_hook.sh` (245 LOC production hook; same JSON-envelope contract; see A1 evidence in T2 `121-research-A1`).

**Language commitment (A2-driven, locked at P1)**: Routing hooks are **Python-native**: `#!/usr/bin/env python3` shebang, single-process per hook, no nested bash + python3 calls. The A2 spike measured bash + python3-per-call at 74-89ms p95 (40ms of which is python3 interpreter startup amortized once per shell invocation but paid again on every helper call). Python-native eliminates the *per-helper* python3 startup cost within a single hook by collapsing all logic into one process; the ~40ms interpreter-startup baseline is paid once per hook instead of three to four times. Single-hook budget: **<50ms p95 per hook** (40ms startup + ~10ms logic), within the original target.

**Cumulative budget accounting (corrected)**: One rule = one hook script means each routing hook spawns its own Python interpreter. With `N` routing hooks matching the same `tool_input`, the cumulative cost is `N × ~40ms` startup + `N × ~10ms` logic. The realistic operating envelope:
- **Common case** (1 routing hook matches a given Bash call): ~50ms p95. Well within budget.
- **Worst case** (all 3 P2 hooks plus the existing `pre_close_verification_hook.sh` fire on one call): ~200ms p95 startup + ~40ms logic ≈ **240ms p95**. This exceeds the originally stated 200ms ceiling, so the per-Bash-call ceiling is **revised to <300ms p95** with an enforced **hook-count cap of 4 active routing hooks**. Adding a fifth requires either consolidating two existing hooks into one script or accepting a budget revision.
- **Mitigation if 4× startup proves uncomfortable**: Phase 3 telemetry will measure real-world cumulative p95. If it trends high, the framework can adopt a **single-dispatcher script** that internally runs N rules in one Python process (trading per-rule failure isolation for amortized startup); this is filed as a follow-on under §Open Questions rather than ship-blocking, since the matcher-set will rarely overlap on the same call.

- New directory `nx/hooks/scripts/routing/` for routing hooks.
- Shared Python helper `nx/hooks/scripts/routing/_lib.py` providing:
  - `allow([context])`: emits `{"hookSpecificOutput": {"hookEventName": "PreToolUse", "permissionDecision": "allow", "additionalContext": "<text>"}}` to stdout; exits 0. Pass-through case, with optional advisory message injected into agent context.
  - `deny(<reason>)`: emits `{"hookSpecificOutput": {"hookEventName": "PreToolUse", "permissionDecision": "deny", "reason": "<multi-line markdown>"}}` to stdout; exits 0. Blocks the tool call; agent sees the reason.
  - `warn(<message>)`: alias of `allow(<message>)`; named for the "advisory but not blocking" case. § Decision Rationale's "Block, not warn" rule maps to deny-vs-warn at the per-rule level.
  - `should_skip_for_reason(<command>)`: checks the agent's tool input for a `# routing-allow: <reason ≥8 chars>` token, mirroring the ε-lint allowlist pattern from `tests/test_no_direct_catalog_writes_outside_projector.py` (RDR-101 Phase 3). Returns 0 if escape valid; non-zero otherwise.
  - `log_routing_event(<rule>, <outcome>)`: appends a JSON line to `~/.config/nexus/routing_log.jsonl` for telemetry. Mirrors the audit-line pattern in `pre_close_verification_hook.sh`.
  - JSON serialization uses stdlib `json.dumps`; no shell-string interpolation; defensive `try/except` wraps every code path.
- **Hook script discipline** (Python adaptation of `pre_close_verification_hook.sh` invariants):
  - Top-level `try: ... except BaseException: print(allow_envelope()); sys.exit(0)` is the **default**. Every code path must produce valid JSON on stdout and exit 0. A failed hook (non-zero exit, malformed JSON) is treated by Claude Code as a hook error and the tool call proceeds silently. That fails open but produces no enforcement, which is worse than no hook at all.
  - **Fail-closed carve-out**: hooks marked `fail_closed: true` in `registry.yaml` MUST override the default with `except BaseException as e: print(deny_envelope("cannot verify, fail-closed: " + str(e))); sys.exit(0)`. The carve-out exists because some rules' enforcement property only holds if "cannot verify" is treated as "not verified". `phase_review_close_requires_gate` (Phase 2 hook 2) is the load-bearing instance: its sentinel-read failure modes (absent, stale, malformed, permission denied) are exactly the conditions that motivate the hook. Fail-open in those cases defeats the rule. The carve-out is opt-in per hook so it stays auditable in one place (`registry.yaml`) rather than scattered across scripts.
  - Tool-name short-circuit at the top: parse `tool_name` from stdin first; if not the matcher target, `allow()` immediately. Fast no-op for unrelated calls.
  - Tool-input parsing: extract `tool_input.command` (Bash) or `tool_input.file_path` (Edit/Write) from stdin. Pattern-match against the actual call, not just the tool name.
- A `nx/hooks/scripts/routing/registry.yaml` enumerating active rules with metadata: rule name, hook script path, matcher, mode (`deny` / `warn`), rationale, escape token.
- Test fixtures at `tests/test_routing_hooks.py` for each rule: positive case (preferred tool path then `allow`), negative case (default tool path then `deny` with redirect message), escape case (allowlisted with reason then `allow`), error case (malformed input then `allow`, fail-open is the only safe degradation).

**Phase 2: Initial cohort**

Three rules with the highest confidence and lowest false-positive risk. Each ships as its own hook script under `nx/hooks/scripts/routing/`.

1. **`grep_for_symbols_redirects_to_serena`**: Bash matcher; detects `grep` / `rg` against `*.py`, `*.swift`, `*.java`, `*.ts`, `*.tsx`, `*.go`, `*.rs` with patterns that look like identifier searches (no spaces, no regex metacharacters). Emits a block message: "This looks like a symbol search. Use `mcp__plugin_sn_serena__jet_brains_find_symbol` for symbol definitions, `mcp__plugin_sn_serena__jet_brains_find_referencing_symbols` for callers. To override, add `# routing-allow: <reason ≥8 chars>` to the command."
2. **`phase_review_close_requires_gate`**: Bash matcher; detects `bd close .*` where the bead title (looked up via `bd show`) contains "phase" or "review". Claude Code provides no native session-history API to PreToolUse hooks, so "recent invocation of `/nx:phase-review-gate`" is observed via a **sentinel file** at `${TMPDIR:-/tmp}/nx-phase-gate-sentinel/<claude_pid>-<rdr-id>-<phase>.json` written by the `/nx:phase-review-gate` skill on successful pass. The hook checks: (a) sentinel exists for the closing bead's `(rdr-id, phase)`, (b) sentinel `mtime` is newer than the current Claude session-start time (read from `~/.config/nexus/t1_addr.<claude_pid>` ctime), (c) sentinel content reports `outcome: "PASSED"`. Any of: sentinel absent, sentinel stale (pre-session-start), sentinel `outcome != "PASSED"`, or sentinel unreadable produces **fail-closed deny** with a redirect message naming the exact gate invocation. RDR-120 § Enforcement Backstops cites this as a load-bearing follow-on. The sentinel-write side ships in this RDR's P2 alongside the hook (single PR; sentinel + reader are coupled and tested together). Pattern precedent: `pre_close_verification_hook.sh` uses scratch markers the same way; this is the file-system analogue.
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
| Hook script shared lib | `nx/hooks/scripts/_run_python_hook.sh` | Extend pattern; new `_lib.py` for routing-specific helpers. |
| Test harness | `tests/cc-validation/` scenarios | Extend pattern; new `tests/test_routing_hooks.py` for unit-level coverage and `tests/cc-validation/scenarios/14_routing_*.sh` for E2E. |

### Decision Rationale

**PreToolUse, not PostToolUse.** Post-tool warning fires after the wrong action has happened; the agent already wasted the call. PreToolUse blocks the wrong action before it executes. The cost difference is real: a blocked Bash call is free; a warned-after-the-fact grep means the agent has the wrong output in context and has to remember to redo it with Serena. Trust falls off when warnings don't actually prevent anything.

**Block, not warn, for high-confidence rules.** Warnings get ignored by the same path-of-least-resistance pressure that motivated this RDR. The catalogue above marks each rule with `block` or `warn`; the default for confident rules is `block`.

**Escape mechanism is non-negotiable.** Hard blocks without an escape produce hook fatigue. The `# routing-allow: <reason ≥8 chars>` pattern lets the agent (or human user) opt out of the block when the rule is genuinely wrong for this case. The reason text is what makes the escape auditable; routing-log captures the escape events so high-escape-rate rules surface as refactoring targets.

**Telemetry from day one.** The routing-log enables the Phase 3 refinement loop. Without it, we'd be running rules forever without knowing if they catch real failures.

**One rule = one hook script.** Resist the temptation to multiplex rules into a single mega-hook. Per-rule scripts mean per-rule tests, per-rule disable (just remove the hooks.json entry), per-rule iteration without affecting siblings. The shared `_lib.py` keeps DRY.

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
- (−) Hook latency on every Bash / Edit / Write call. Per-hook <50ms p95 (Python-native, A2-measured). Cumulative <300ms p95 with a hook-count cap of 4 active routing hooks per matcher; see §Approach "Cumulative budget accounting" and §Performance Expectations.
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
- [ ] **P2 co-requirement**: `nx/skills/phase-review-gate/SKILL.md` updated to write the sentinel file on PASSED outcome (`${TMPDIR:-/tmp}/nx-phase-gate-sentinel/<claude_pid>-<rdr-id>-<phase>.json`). The hook reader and sentinel writer MUST ship in the same PR; shipping the hook without the writer denies every phase-review close indefinitely.

### Minimum Viable Validation

One end-to-end demo per phase:

| Phase | MVV |
|---|---|
| P1 | A test fires `_lib.py:deny()` from a stub hook; verifies the block message reaches the agent as `{"hookSpecificOutput": {"permissionDecision": "deny", "reason": "..."}}` on stdout and the hook exits 0 (A1-corrected protocol; exit 2 is explicitly rejected). Escape token (`# routing-allow: <reason>`) allows the call to proceed via `allow()`. |
| P2 | Each of the three initial hooks: positive case (preferred path) succeeds, negative case (default path) blocks with redirect message, escape case (with `# routing-allow:`) succeeds. |
| P3 | `routing_log.jsonl` accumulates entries for 30 days of normal use. A telemetry report enumerates per-rule fire-rate, block-rate, escape-rate. |

### Phasing

**P1: Framework + scaffolding** (week 1)
- `nx/hooks/scripts/routing/_lib.py` with `allow()`, `deny()`, `warn()`, `should_skip_for_reason()`, `log_routing_event()`
- `nx/hooks/scripts/routing/registry.yaml` (empty initially)
- `tests/test_routing_hooks.py` with framework tests (assert JSON envelope shape + exit 0 on every path)
- Documentation: `nx/hooks/scripts/routing/README.md` describing the Python-native hook authoring convention

**P2: Initial cohort** (week 2-3)
- `grep_for_symbols_redirects_to_serena.py` + tests
- `phase_review_close_requires_gate.py` + sentinel-write integration into `nx/skills/phase-review-gate/SKILL.md` (skill writes `${TMPDIR:-/tmp}/nx-phase-gate-sentinel/<claude_pid>-<rdr-id>-<phase>.json` on PASSED) + tests covering: sentinel present-and-fresh-and-PASSED → allow, sentinel absent → deny, sentinel stale (mtime < session start) → deny, sentinel `outcome != "PASSED"` → deny, sentinel unreadable (permissions / corrupt JSON) → deny.
- `git_add_all_redirects_to_explicit_paths.py` + tests
- `hooks.json` entries for each; `registry.yaml` populated
- E2E scenarios at `tests/cc-validation/scenarios/14_routing_*.sh`

**P3: Telemetry** (week 4)
- Telemetry helper `_lib.py:log_routing_event` writes to `~/.config/nexus/routing_log.jsonl`
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

- **Scenario**: stub hook calls `deny("<reason>")`. **Verify**: stdout contains `{"hookSpecificOutput": {"hookEventName": "PreToolUse", "permissionDecision": "deny", "reason": "..."}}`; process exits 0; tool call does not execute.
- **Scenario**: stub hook would call `deny()` but command carries `# routing-allow: <reason ≥8 chars>`. **Verify**: `should_skip_for_reason` returns True, hook emits `allow()` envelope, tool call proceeds; escape event written to `routing_log.jsonl`.
- **Scenario**: `grep -r MyFunction src/nexus/ --include='*.py'` triggers `grep_for_symbols_redirects_to_serena`. **Verify**: block emitted, redirect message names `jet_brains_find_symbol`.
- **Scenario**: `grep -r "TODO" src/nexus/ --include='*.py'` (text search, multi-character non-identifier pattern). **Verify**: hook is no-op; legitimate text grep proceeds.
- **Scenario**: `bd close nexus-abc1` where bead title contains "Phase 3 review"; sentinel file present, fresh, PASSED. **Verify**: hook reads sentinel, returns `allow()` with advisory context naming the verified gate run.
- **Scenario**: same `bd close` shape; sentinel file **absent**. **Verify**: hook returns `deny()` with redirect message naming exact gate invocation (`/nx:phase-review-gate <rdr-id> --phase N`); fail-closed.
- **Scenario**: same `bd close`; sentinel file present but `mtime` older than session-start ctime. **Verify**: `deny()` with "stale sentinel: gate must run in this session" message.
- **Scenario**: same `bd close`; sentinel exists with `outcome: "BLOCKED"`. **Verify**: `deny()` with "gate did not pass" message naming the BLOCKED outcome.
- **Scenario**: same `bd close`; sentinel exists but is unreadable (permissions denied or malformed JSON). **Verify**: `deny()` with "cannot verify gate, fail-closed" message; treats cannot-verify as not-verified.
- **Scenario**: `bd close nexus-abc1` where bead title is "fix the formatter bug". **Verify**: hook is no-op (no phase/review pattern in title).
- **Scenario**: `git add -A` triggers `git_add_all_redirects_to_explicit_paths`. **Verify**: block emitted.
- **Scenario**: `git add src/nexus/foo.py` (explicit path). **Verify**: hook is no-op.
- **Scenario**: Hook script throws Python exception. **Verify**: tool call proceeds (`allow()` from top-level except); error logged. Exception: the `phase_review_close_requires_gate` hook fails closed by design; its top-level except emits `deny()` not `allow()`.
- **Scenario**: Hook chain latency. **Verify**: each hook completes under 50ms p95 on a representative call set.

## Validation

### Testing Strategy

1. **Unit tests** (`tests/test_routing_hooks.py`): per-rule positive / negative / escape cases.
2. **E2E tests** (`tests/cc-validation/scenarios/14_routing_*.sh`): full Claude Code hook invocation.
3. **Soak telemetry**: 30 days of `routing_log.jsonl` post-P3 ship; per-rule fire/block/escape rates inform Phase 4 refinement.

### Performance Expectations

Per-hook budget: <50ms p95 (40ms Python-interpreter startup + ~10ms hook logic). Cumulative per-Bash-call overhead: <300ms p95 with up to 4 active routing hooks matching the same call (one-process-per-hook architecture means startup cost is paid per hook; see §Approach Phase 1 "Cumulative budget accounting" for the arithmetic). If 4 hooks consistently match the same call shape, consolidate or adopt the single-dispatcher fallback. Hook-count cap of 4 is enforced at `registry.yaml` review time; adding a fifth requires either consolidation or a budget revision.

## Finalization Gate

To be completed during gate.

### Contradiction Check

To be completed.

### Assumption Verification

Critical assumptions to verify:

- [x] **A1** (REVISED): PreToolUse hooks can block tool execution via JSON `permissionDecision: "deny"` envelope with `exit 0`, NOT via stderr exit code 2. **Status**: Verified (High confidence). **Method**: Source Search of `nx/hooks/scripts/pre_close_verification_hook.sh` and three sibling production hooks (all use the JSON envelope pattern); empirical: the pre-close hook is registered as PreToolUse on `Bash` matcher and has been denying `bd close` calls missing required metadata since at least 2026-04. **Evidence**: T2 entry `121-research-A1`. **Design implications folded into § Approach Phase 1 above**: helpers renamed (`emit_block` to `deny`, `emit_warn` to `warn`); helpers emit JSON with `permissionDecision` field + `reason` (deny) or `additionalContext` (allow) and exit 0; `set -e`/`set -u` PROHIBITED (every code path produces valid JSON); defensive python3 JSON escaping with shell-quoting fallback; tool-name + tool-input short-circuit at the top of every hook. The stderr-exit-2 mechanism is dropped from the design.
- [x] **A2** (REVISED, COMMITTED): Hook chain latency is bounded at <50ms p95/hook via **Python-native hook implementation** (committed at P1; see § Approach Phase 1 "Language commitment"). Original bash + python3-per-call pattern measured 74-89ms p95 (40ms python3-startup baseline), violating the original budget. **Status**: Verified (Spike + design commitment). **Method**: Stub hook at `/tmp/rdr121_stub_routing_hook.sh` modeled byte-for-byte on `pre_close_verification_hook.sh`, 50 iters per path on macOS darwin Py3.13.13. **Measured**: non-Bash short-circuit p95 52ms (bash); allow path p95 74ms (bash + 1 python3 call); deny path p95 89ms (bash + 2 python3 calls). Python-native eliminates the per-helper python3-startup cost by collapsing the entire hook into one process; measured cost is dominated by interpreter import (~40ms) which pays once. **Evidence**: T2 entry `121-research-A2`. **Cumulative budget** (corrected at re-gate; see §Approach "Cumulative budget accounting"): <300ms p95 per Bash call with a hook-count cap of 4 active routing hooks (~240ms 4× Python startup + ~40ms logic).
- [~] **A3** (REVISED): Matchers achieve high precision AND adequate recall on representative corpus. Original spec ("identifier search, no spaces, no regex metacharacters") REFUTED: 80% precision, 33% recall on n=20 corpus replay. Refined three-shape spec (single id + dotted-id chain + pipe-alternation, with disqualifier list including all-uppercase-short-token) predicted to reach 92%+ recall, ~100% precision. **Status**: Partially Verified at refined spec (corpus replay only; live matcher not yet built). **Method**: Corpus Replay (n=20: 12 actual session grep/rg invocations + 8 plausible negative-case variants). **Evidence**: T2 entry `121-research-A3` + `/tmp/rdr121_a3_corpus.txt`. **Design implication**: § Approach Phase 2 hook 1 (`grep_for_symbols_redirects_to_serena`) must adopt the refined three-shape matcher; original "no regex metacharacters" spec misses the dominant real-world shapes (dotted attrs, alternation of identifiers).
- [?] **A4** (ASSUMED): Escape mechanism is sufficient to handle edge cases without rules being silenced wholesale. **Status**: Assumed (acknowledged unverifiable pre-implementation). **Method**: Original method "30-day telemetry post-P2" is the only verification path; pre-implementation design audit cannot substitute. P3 telemetry loop is the closure path. **Acknowledged risk**: if escape-rate per rule exceeds an actionable threshold (TBD during P3), the rule is producing too many false positives and must be refined or removed. The matcher refinement from A3 (recall improvement) should reduce escape pressure on the symbol-grep rule specifically.

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
- **Memory management**: telemetry log grows; needs rotation policy. P3 to spec. Sentinel files at `${TMPDIR}/nx-phase-gate-sentinel/` accumulate across sessions; the OS clears `$TMPDIR` on reboot on macOS/Linux but not predictably. Add a sweep-on-write step to `/nx:phase-review-gate`'s sentinel writer that deletes sentinels whose `<claude_pid>` is no longer alive (cheap: one `kill -0` per file). Out of scope for the hook itself.

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
- 2026-05-20 (re-gate): Second-pass revisions (re-gate outcome PARTIAL with 0 Critical, 2 Significant). (1) Fail-closed carve-out moved from test-plan-only to §Approach Phase 1 "Hook script discipline" as an explicit `registry.yaml: fail_closed` opt-in; `phase_review_close_requires_gate` is the first declared instance. Removes the §Approach/test-plan contradiction the first re-gate caught. (2) Cumulative latency budget arithmetic corrected: one-process-per-hook architecture means 4× Python-startup baseline is paid, not amortized. New "Cumulative budget accounting" subsection in §Approach + §Performance Expectations + Trade-offs latency bullet now state <300ms p95 cumulative with a hook-count cap of 4. Single-dispatcher fallback noted as Phase 3 follow-on. (3) Prerequisites list updated with SKILL.md sentinel-write co-requirement (hook + writer ship in the same PR). (4) Sentinel cleanup policy added to Cross-Cutting Concerns memory bullet (sweep dead-pid sentinels at write time).
- 2026-05-20: Gate-driven revisions (gate outcome BLOCKED with 2 Critical, 2 Significant). (1) Hook 2 `phase_review_close_requires_gate` replaced the nonexistent "session log" lookup with a sentinel-file pattern written by `/nx:phase-review-gate`; sentinel write-side ships in P2 alongside the hook. Hook fails closed on sentinel absent/stale/non-PASSED/unreadable; five new test scenarios cover those cases. (2) MVV P1 row corrected from "exit code 2" to JSON `permissionDecision: "deny"` envelope + exit 0, matching A1 verification. (3) A2 language choice committed at P1 to Python-native (was deferred to P3); `_lib.sh` → `_lib.py`, helpers renamed; cumulative-budget bullet added (<200ms with 4 stacked hooks). A2 status promoted from `[~]` to `[x]`. Hook 2 fail-closed exception documented in test plan top-level-exception scenario.
