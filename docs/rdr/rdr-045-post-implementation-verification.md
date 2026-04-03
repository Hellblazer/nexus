---
id: RDR-045
title: Post-Implementation Verification Gate
type: Architecture
status: closed
closed_date: 2026-04-02
close_reason: implemented
priority: P2
created: 2026-04-01
accepted_date: 2026-04-01
reviewed-by: self
---

# RDR-045: Post-Implementation Verification Gate

## Problem

Agents routinely declare work "done" when it isn't — outputs unwired, return values discarded, components half-implemented. This is observable across projects and appears to be a fundamental LLM satisficing behavior, not a project-specific issue.

The nx toolkit provides optional quality gates (critique agents, code review, test validation) but they're all manually invoked. The agent that cuts corners on implementation also cuts corners on invoking verification.

## Design Principle

nx is an opinionated plugin. Beads is the task tracker. RDRs are the design workflow. The verification gate should leverage all of these — the goal is not framework-agnostic generality but addressing universal failure classes (premature closure, untested code, abandoned state) within the nx workflow.

The constraint is: don't encode project-specific domain knowledge (specific equations, specific architectures). Encode structural properties the workflow is supposed to ensure (tests pass, review happened, nothing dangling).

## Research Findings

### R1: PreToolUse limitations and the Stop hook alternative

PreToolUse hooks fire on EVERY tool call with ~5s timeout. They have NO conversation context — only tool name + arguments via JSON stdin. They cannot make semantic judgments. The `readonly-agent-guard.sh` (a PreToolUse hook) was tried for enforcement and abandoned in v2.2.0.

**However**, a PreToolUse hook that pattern-matches a specific command (`bd close`) and runs a purely mechanical check (test suite exit code) avoids both problems: it's a fast no-op on non-matching calls, and the check requires no conversation context. This is fundamentally different from `readonly-agent-guard.sh`, which tried to make permission decisions about arbitrary tool calls. The distinction: mechanical external checks (run tests, check exit code) are valid at PreToolUse; context-dependent reasoning (was the work actually done?) is not.

**Stop hooks** fire when the agent declares "done", can block completion with a reason, and the agent retries with feedback. The `stop_hook_active` field indicates a retry pass (prevents infinite loops).

### R2: Industry convergence on three generic patterns

1. **Output guardrail function** (CrewAI): `f(output) -> (pass, feedback)` with bounded retry. Most portable — just a function signature.
2. **Stop/completion hooks** (Claude Code, Agent SDK): Block at the "declare done" moment. Most directly applicable to nx.
3. **Deterministic script postconditions** (OpenAI): `test + lint + typecheck` as non-negotiable gates. Most reliable.

Key finding (arXiv:2512.12791): Agents achieve 100% tool sequencing but only 33% policy adherence. Prompt-level instructions ("always verify") compete with satisficing. Mechanical enforcement is the only reliable countermeasure.

### R3: Failure mode classification (14 cases from ART project)

| Category | Count | Detectable by | Example |
|----------|-------|---------------|---------|
| Static analysis | 3/14 | Linter/dead-code tool | Orphan classes, unused returns, no-op overrides |
| Test execution | 4/14 | Purpose-built tests | Inert pathways, wrong signal source, scalar approximations |
| Semantic understanding | 7/14 | **Nothing automated** | Correct equations but disconnected circuit, wrong formula, fabricated variables |

**A PreToolUse hook catches 3/14.** A Stop hook running test+lint catches ~7/14. The remaining 7/14 require specification comparison — domain knowledge that cannot be mechanically enforced without being workflow-specific.

### R4: What nx already has

- `test-validator` agent with quality gates (test pass, coverage, no smells)
- `java-developer` agent with a "Completion Protocol (MANDATORY)" — 6-step checklist
- A half-built `verification-before-completion` skill in a worktree with the principle: "NO COMPLETION CLAIMS WITHOUT FRESH VERIFICATION EVIDENCE"
- The Gate Function pattern: IDENTIFY → RUN → READ → VERIFY

These exist but are prompt-enforced, not mechanically enforced.

### R5: Beads plugin architecture and extension model

The beads plugin provides three interaction surfaces: `bd` CLI, `/beads:*` slash commands, and an optional MCP server (`beads-mcp`, separate PyPI package, not installed by default).

**The `bd` CLI via Bash is the intended and actual interface for Claude Code.** The docs say "Prefer CLI + hooks when shell is available." The MCP server is for MCP-only environments (Claude Desktop). In practice, agents call `bd close`, `bd ready`, etc. directly via Bash — never through the skill commands or MCP.

**Beads has no pre-close verification gate.** The plugin registers only two Claude Code hooks (SessionStart and PreCompact, both running `bd prime`). No PreToolUse, PostToolUse, or Stop hooks. The `bd gate` system is for formula-scoped async coordination (human approval, CI), not for validating work completeness before closure. "Don't close issues unless work is actually complete" is a behavioral instruction in the task-agent prompt.

**Beads' extension model is primitives, not policy.** It provides storage and coordination (`bd close`, `bd gate`, `bd set-state`, labels, `bd preflight`). Domain-specific intelligence — including verification — is expected to come from external tools via the skill/command layer. This is exactly the layer nx operates at.

**The `/beads:close` skill is a thin wrapper.** It tells the agent to call `bd close` (or the MCP tool) and suggests checking for unblocked dependents. No verification logic. Agents bypass it entirely and call `bd close` directly.

**Relevant beads primitives for verification:**
- `bd set-state <id> verification=passed` — label convention for auditability
- `bd preflight` — PR readiness checks (manual invocation)
- `bd lint` — structural validation of issue descriptions
- `bd close --force` — override for pinned/gated issues (implies normal close respects gates)

## Design

Two mechanical enforcement points at the boundaries where agents cut corners:

### Layer 1: Stop Hook — "Don't leave a mess"

Fires when the agent ends the conversation. The hook reads `stop_hook_active` from stdin JSON to detect retry passes.

**First pass** (`stop_hook_active: false`): runs all checks. Blocks on any failure.

| Check | Type | What it catches |
|-------|------|----------------|
| `git status` — uncommitted changes to tracked files? | Mechanical | Agent walks away from half-committed work |
| `bd list --status=in_progress` — beads still active? | Mechanical | Agent forgets to close or defer open work |
| Run test command (from `.nexus.yml`) — failures? | Mechanical | Agent leaves broken code behind |

**Retry pass** (`stop_hook_active: true`): runs the same checks again.
- Mechanical issues now fixed (committed, beads closed) → pass silently.
- Tests still failing → **let through with a prominent warning** to the user: "TESTS FAILING — agent could not resolve. Manual intervention needed."

This differentiation matters: mechanical failures (forgot to commit, forgot to close bead) are trivially fixable in one pass. Test failures may not be — and giving the agent unlimited retries incentivizes test-commenting or assertion removal to game the gate. One honest attempt; after that, escalate to the human.

**Timeout:** The test command has a configurable timeout (default 120s via `.nexus.yml`). If the test suite exceeds this, the hook warns but does not block — slow integration suites should not prevent session end.

**Command-not-found handling:** If the configured (or auto-detected) test command fails to execute (command not found, permission denied — exit code 127 or 126), the hook treats this as a skipped check with an advisory, not a blocking failure. This prevents misconfigured environments from permanently blocking all closures and session ends.

### Layer 2: PreToolUse Hook on `bd close` — "Don't close what isn't done"

Agents call `bd close` directly via Bash — not through `/beads:close` skills or MCP tools. This is the intended beads interaction model for Claude Code ("prefer CLI when shell is available"). The enforcement must meet the agent where it is.

PreToolUse hook on Bash, pattern-matching `bd close` and its alias `bd done`. Fast no-op on all other Bash calls.

#### Layer 2a: Mechanical gate (blocking)

| Check | What it catches |
|-------|----------------|
| Run test suite (from `.nexus.yml` verification config) | Agent closes bead with failing tests |

This is a purely mechanical check: run command, check exit code. No conversation context needed. Fundamentally different from `readonly-agent-guard.sh` — that hook tried to make permission decisions about arbitrary tool calls; this one runs an external command at a narrow trigger point.

If tests fail, the hook blocks the `bd close` call and returns the failure reason. The agent sees "tests failing, fix before closing" and can act on it.

#### Layer 2b: Best-effort advisory (non-blocking)

| Check | What it catches |
|-------|----------------|
| Check T1 scratch for review completion marker | Agent closes without review |

This check attempts to infer session context via a scratch proxy. It is explicitly **best-effort**: if the marker is absent (review-code was not invoked, or scratch is unavailable), the check **fails open** with an advisory warning: "No review marker found — consider running /nx:review-code before closing." It does NOT block.

**Why non-blocking:** The review marker is a context proxy, not a mechanical check. It has the same structural limitation R1 identified — no conversation context in hooks. Making it advisory rather than blocking prevents false confidence and avoids the `readonly-agent-guard.sh` mistake of enforcing a decision the hook cannot reliably make.

#### Review-code scratch marker (deliverable)

The `/nx:review-code` skill must be modified to write a T1 scratch marker on completion:

**Producer** (review-code skill, on successful completion):
```
nx scratch put "review-completed bead={bead-id} at={ISO-timestamp}" --tags "review,{bead-id}"
```

**Consumer** (Layer 2b hook, before allowing `bd close {bead-id}`):
```
nx scratch search "review-completed" --n 20
```
Then grep output for the bead ID. If found, review was completed this session. If not found, emit advisory.

- **Storage:** T1 scratch is session-ephemeral — no TTL needed, entries vanish when the session ends
- **Lookup:** Semantic search + tag/bead-ID grep (T1 has no key-value lookup; search is the retrieval mechanism)
- **Bead ID availability:** The review-code skill receives the bead ID when invoked with a bead context. When invoked without a bead (ad-hoc review), the marker is written with `bead=none` and the Layer 2b check skips it — fails open as designed

If the marker is absent, the hook emits an advisory. If present, the hook notes "review completed" in its output.

#### Auditability

On successful verification (Layer 2a passes), set `bd set-state <id> verification=passed` before allowing the close through. This uses beads' native state dimension system to record that verification happened. This is also an extension point: `bd preflight` could check for `verification=passed` before PR creation.

### Layer 3: Skill Enhancements (prompt-enforced, not mechanical)

These address the gaps hooks can't reach:

| Enhancement | What it catches |
|-------------|----------------|
| `/nx:review-code` writes scratch marker on completion (see Layer 2b) | Enables review-presence tracking |
| `/nx:review-code` flags deferred items → prompts "create beads for these" | Deferred items vanishing into RDR prose |
| `/nx:substantive-critique` compares implementation against RDR stated goals | Category 3 semantic gaps (correct code, wrong behavior) |
| `finishing-branch` skill runs test-validate before allowing PR/merge | Final quality gate before integration |

Layer 3 remains advisory — the agent can still skip invoking these skills. But Layers 1-2 provide the mechanical backstop that catches the structural failures.

**Defense-in-depth:** `finishing-branch` also runs tests. If it was invoked, the Stop hook's test check is redundant but harmless. If it wasn't invoked, the Stop hook catches the gap. There is no signaling between them — this is intentional simplicity over coordination complexity.

### What's out of scope

Domain-specific semantic verification ("does this ODE match the paper?", "is this circuit wired correctly?"). These require specification knowledge that varies per project. The toolkit can surface the question (via critique agents) but can't mechanically enforce the answer.

**Tests-as-oracle caveat:** The mechanical gates assume the test suite is an independent correctness oracle. When tests and implementation are co-generated by the same agent in the same session, this assumption degrades — the tests may verify the wrong behavior. `/nx:test-validate` addresses test quality concerns and should be invoked for critical work. The mechanical gate catches "tests don't pass," not "tests test the right thing."

## Configuration

```yaml
# .nexus.yml
verification:
  test_command: "uv run pytest"     # auto-detected if omitted
  lint_command: "ruff check src/"   # optional
  test_timeout: 120                 # seconds (default 120; 0 = no timeout)
  on_stop: true                     # REQUIRED to enable Stop hook — default false
  on_close: true                    # REQUIRED to enable bd-close gate — default false
```

**Activation is explicit:** `on_stop` and `on_close` default to `false`. A `verification:` section without either flag set does nothing — no silent enforcement from a partially-written config. Projects opt in by setting the flags they want.

**Auto-detection:** If `test_command` is omitted but `on_stop` or `on_close` is `true`, the hook auto-detects from project files:

| Marker file | Test command |
|-------------|-------------|
| `pom.xml` | `mvn test` |
| `build.gradle` / `build.gradle.kts` | `./gradlew test` |
| `pyproject.toml` | `uv run pytest` (falls back to `pytest`) |
| `package.json` | `npm test` |
| `Cargo.toml` | `cargo test` |
| `Makefile` | `make test` |
| `go.mod` | `go test ./...` |

Detection order is first-match. If nothing is detected, the test check is skipped with an advisory: "No test command configured or detected."

## Interaction with Existing Workflow

| Existing component | Interaction |
|-------------------|-------------|
| `finishing-branch` skill | Defense-in-depth — finishing-branch runs tests during integration; Stop hook is the backstop if finishing-branch wasn't invoked. No signaling between them. |
| `test-validate` skill | Stop hook runs tests mechanically; test-validate adds coverage analysis and test quality review. Test-validate is the escalation when test quality (not just pass/fail) is in question. |
| `review-code` skill | Modified to write T1 scratch marker on completion (Layer 2b deliverable). Layer 3 enhances review to flag deferred items. |
| `brainstorming-gate` skill | Unchanged — protocol compliance remains prompt-enforced via SessionStart reminders |
| `rdr_hook.py` (SessionStart) | Unchanged — RDR state awareness at session start; verification hooks enforce at session end and task close |

## Deliverables

1. **Stop hook script** (`nx/hooks/scripts/stop_verification_hook.sh`) — reads `.nexus.yml`, runs checks, handles `stop_hook_active` retry logic, differentiates mechanical vs test failures on retry, handles command-not-found (exit 126/127) as skip-with-advisory
2. **PreToolUse hook script** (`nx/hooks/scripts/pre_close_verification_hook.sh`) — pattern-matches `bd close` and `bd done`, runs test suite (Layer 2a, blocking) + review scratch marker search (Layer 2b, advisory), sets `bd set-state <id> verification=passed` on success
3. **hooks.json update** — register Stop and PreToolUse hooks
4. **review-code skill modification** — write T1 scratch marker via `nx scratch put "review-completed bead={id} ..." --tags "review,{id}"` on completion; handle no-bead-context case (write `bead=none`)
5. **`.nexus.yml` schema update** — document `verification` section with `on_stop`, `on_close`, `test_command`, `lint_command`, `test_timeout`

## Decision

**Accepted** on 2026-04-01 after two gate rounds (4 critical, 3 significant issues identified and resolved).
