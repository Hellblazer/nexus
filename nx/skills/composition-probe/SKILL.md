---
name: composition-probe
description: Use when a coordinator bead's inter-bead composition needs verification before downstream beads begin
effort: medium
---

# Composition Probe Skill

Validates that a coordinator bead's inter-bead composition is wired correctly before downstream beads begin.
A coordinator bead is any bead tagged `metadata.coordinator=true` (set by plan-enricher when a bead's
implementation composes outputs from ≥2 prior beads).

## When This Skill Activates

- User invokes `/nx:composition-probe <coordinator-bead-id>`
- Plan execution reaches a step tagged `probe: <bead-id>` in the enriched bead description
- User says "run composition probe on bead X", "check probe bead X"

## Inputs

- **Coordinator bead ID** (required) — e.g., `nexus-abc1`

## Tool Budget (Read-Only)

The dispatched subagent uses `Read`, `Grep`, `Glob` only. Serena symbol resolution is NOT needed — the
Phase 1a hard-case spike verified CA-1 as `READ-ONLY-SUFFICIENT` against
`src/nexus/search_engine.py:149` (`search_cross_corpus`). Nexus and ART coordinator targets use `Any`
at injection boundaries with runtime dict-key contracts; typed generics requiring inference were not
present in the verified corpus.

Do NOT include Serena tool instructions in the subagent prompt. If a future target requires Serena
escalation (e.g., `typing.Protocol` / `TypeVar` intensive composition), escalate to the user before
dispatching — do not silently extend the tool budget.

**T2 reference**: `nexus_rdr/066-research-5-ca1-ca2-ca3-hard-case-spike`

## Behavior

### Step 1: Read Coordinator Bead

```bash
bd show <coordinator-bead-id> --json
```

Extract:
- `description` — full bead description, including declared entry point
- `metadata.coordinator` — verify it is `true`; warn but continue if absent (may be an untagged coordinator)
- `.dependencies` (or `.waits_for`) — list of dependency bead IDs

### Step 2: Read Dependency Declared Outputs

For each dependency bead ID from Step 1:

```bash
bd show <dep-bead-id> --json
```

Extract `description` — specifically the declared output shape or return value described in the bead.
Collect as `{dep_bead_id: declared_output}` map.

### Step 3: Detect Test Runner

In the project root, check for build files in order:

```
if pyproject.toml present:    runner = "uv run pytest /tmp/probe-<id>.py -xvs"
elif pom.xml present:         runner = "mvn test -Dtest=<classname> --no-transfer-progress"
elif package.json present:    runner = "npm test -- /tmp/probe-<id>.ts"
else:                         surface language-detection failure to the user; halt
```

Map the runner to a file extension: `pytest` → `.py`, `mvn` → `.java`, `npm` → `.ts`.

### Step 4: Dispatch Subagent

Use the Agent tool with `subagent_type="general-purpose"`. Populate the prompt below verbatim,
substituting `<entry_point>` with the coordinator bead's entry point and `{list of dep bead IDs + their
declared outputs}` with the map from Step 2.

---

**Pinned subagent prompt** (do not paraphrase):

> Generate a minimal (30-50 line) end-to-end smoke test against `<entry_point>` that exercises the
> composition of dependencies {list of dep bead IDs + their declared outputs}. The test should:
> - Use realistic input data (not mocks, not stubs, not defaults)
> - Assert on output shape AND intermediate value dimensionality
> - Fail fast on any exception
> - Print which dependency's contract was violated if the composition fails
>
> Write the test to `/tmp/probe-{bead_id}.{ext}` and run it. Report pass/fail + which dependency (if
> any) violated its declared output shape.
>
> If you cannot attribute the failure to a specific dependency bead, say so explicitly rather than
> guessing.

---

### Step 5: Parse Output and Report

The subagent writes the test to `/tmp/probe-<bead-id>.<ext>` and runs it. Parse the output.

**PASS**: emit the one-line confirmation format below. The coordinator bead unblocks; downstream beads
may begin.

**FAIL**: emit the structured report below. The coordinator bead remains blocked; surface the failing
dependency for reopen.

**Unattributed failure (CA-2 fallback)**: if the subagent reports it cannot attribute the failure to a
specific dependency (e.g., NullPointerException deep in a call stack, ambiguous exception, or an explicit
"cannot attribute" statement), do NOT attempt auto-attribution. Surface the raw test output to the user,
flag the coordinator bead for manual investigation, and do not suggest a `bd reopen` target. This is the
CA-2 unattributed-failure degradation mode documented in RDR-066 §Failure Modes.

**Timeout / generation failure**: if the subagent fails to generate or run the test (dispatch timeout
>5 minutes, subagent crashes, or cannot write the test file), surface as advisory:
`> Composition probe could not be generated for <coordinator-bead-id>; manual composition check required.`
Do not block the coordinator.

**Flaky result (2/3 passes)**: if the probe is run 3 times and passes 2 of 3, surface ambiguity to the
user with the raw inconsistent outputs. Do not block, but warn. Up to 3 retries are permitted to
distinguish a real failure from flakiness; consistent failures (3/3) block.

## Output Format

### PASS

```
> Composition probe PASSED for <coordinator-bead-id> (<N> assertions, <elapsed>s). Coordinator unblocks.
```

### FAIL (attributed)

```
> Composition probe FAILED for <coordinator-bead-id>
>
> Failing dependency: <dep-bead-id> (<dep-title>)
> Assertion: <the specific assertion that broke, with file:line>
> Expected: <shape/type/value>
> Actual: <shape/type/value>
>
> Suggested: `bd reopen <dep-bead-id>` to address the contract violation before proceeding.
```

### FAIL (unattributed — CA-2 fallback)

```
> Composition probe FAILED for <coordinator-bead-id> (unattributed failure)
>
> The subagent could not attribute the failure to a specific dependency bead.
> Raw test output:
> <raw output>
>
> Manual investigation required. Do not proceed with downstream beads until the composition point
> is verified. Flag this coordinator bead for review before continuing.
```

## Relay Template

When the composition probe skill dispatches its general-purpose subagent via the Agent tool, use this
exact structure:

```markdown
## Relay: general-purpose

**Task**: Generate and run a 30-50 line composition smoke test against `<entry_point>` that exercises
the inter-bead composition for coordinator bead `<coordinator-bead-id>`.
**Bead**: none

### Input Artifacts
- nx store: [none — composition-probe does not consume T3 knowledge]
- nx memory: coordinator bead metadata from `bd show --json` (accessed via shell, not memory_get)
- nx scratch: [none — composition-probe is stateless across invocations]
- Files: `/tmp/probe-<coordinator-bead-id>.<ext>` (subagent writes the generated test here and reads source files referenced by the coordinator's entry point)
- Coordinator entry point: `<entry_point>`
- Dependency declared outputs: `{dep_bead_id: declared_output, ...}`
- Test runner: `<runner command>`

### Deliverable
Pass/fail result with attribution to the specific failing dependency bead (if failure), or explicit
"cannot attribute" statement if attribution is not possible.

### Quality Criteria
- [ ] Test is 30-50 lines
- [ ] Uses realistic input data (not mocks, not stubs, not defaults)
- [ ] Asserts on output shape AND intermediate value dimensionality
- [ ] Fails fast on any exception
- [ ] Prints which dependency's contract was violated if composition fails
- [ ] If attribution is impossible, says so explicitly rather than guessing
- [ ] Test written to `/tmp/probe-<bead-id>.<ext>` and run via the detected test runner
```

For full relay structure and optional fields, see [RELAY_TEMPLATE.md](../../agents/_shared/RELAY_TEMPLATE.md).

## Success Criteria

- [ ] Coordinator bead read and entry point + dependency IDs extracted
- [ ] Each dependency's declared output read from its bead description
- [ ] Test runner detected (pyproject.toml / pom.xml / package.json)
- [ ] Subagent dispatched with pinned prompt (verbatim — no paraphrasing)
- [ ] Tool budget enforced: subagent uses Read, Grep, Glob only (no Serena)
- [ ] PASS output is one-line confirmation with assertion count and elapsed time
- [ ] FAIL output is structured report with dependency attribution and suggested `bd reopen`
- [ ] CA-2 fallback (unattributed failure) surfaces raw output without guessing attribution
- [ ] Timeout and flaky-result paths handled without silent blocking

## Agent-Specific PRODUCE

Outputs produced by this skill directly:

- **Console output**: PASS/FAIL report on stdout with attribution (see Output Format above). PASS is a one-line confirmation; FAIL is a structured report citing the failing dependency bead with a `bd reopen` suggestion; unattributed FAIL surfaces raw test output without auto-attribution.
- **Filesystem (ephemeral)**: generated test file at `/tmp/probe-<coordinator-bead-id>.<ext>` — auto-cleanup after probe run; not retained across invocations.
- **Bead state**: no direct mutation. The coordinator bead remains in its current state; the user decides whether to close, reopen, or `bd reopen` a dependency based on the probe result. The skill surfaces recommendations, not commands.
- **T1 scratch (optional)**: for repeat invocations against the same coordinator (retry path or flaky-result investigation), intermediate results may be written to T1 scratch with tag `composition-probe,<coordinator-bead-id>` for session-scoped state. Not required for a single-run invocation; use when disambiguating a 2/3 flaky-pass result.

Outputs generated by the dispatched general-purpose subagent (not by the skill directly):

- **Generated test file**: `/tmp/probe-<coordinator-bead-id>.<ext>` — the 30-50 line composition smoke test the subagent authored
- **Test output**: pytest / mvn / npm output captured by the subagent and returned to the skill for parsing
- **Attribution**: specific dependency bead ID + the assertion or exception that broke (for attributed FAILs)

## MVV Re-Scope Note

The original MVV in the RDR (Phase 3 target: ART RDR-073 IMPL-04
`GroundedLanguageSystem.dialog().process("ball")` expecting `IllegalArgumentException: input length 312
!= state size 65`) is **un-runnable** against the current ART codebase — the RDR-073 fix already shipped,
so the failure mode no longer exists and any probe run would produce a false PASS.

The Phase 3 MVV is therefore re-scoped to structural validation only:

- Skill file is syntactically valid markdown with correct YAML frontmatter
- Subagent prompt is pinned verbatim from RDR-066 §Technical Design
- Test runner detection covers the three supported languages
- Skill shape matches the Phase 1a validated pattern (Read-only tool budget)

The **catch demonstration** (actual end-to-end FAIL→catch→PASS cycle) moves to Phase 5a
(`nexus-gxr`, Synthetic retcon injection), where a synthetic composition failure is deliberately
introduced and the probe's catch is verified end-to-end against a controlled target.

## Does NOT

- Close or update the coordinator bead automatically (user decides based on probe result)
- Run the full project test suite (the probe is targeted, ~30-120 seconds)
- Use Serena for symbol resolution (Read-only tool budget per Phase 1a CA-1 finding)
- Auto-attribute failures when the subagent cannot confidently identify the failing dependency
