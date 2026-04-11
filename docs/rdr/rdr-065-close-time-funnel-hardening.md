---
id: RDR-065
title: "Close-Time Funnel Hardening Against Silent Scope Reduction"
type: process
status: draft
priority: P2
author: Hal Hildebrand
reviewed-by: self
created: 2026-04-10
accepted_date:
related_issues: []
---

# RDR-065: Close-Time Funnel Hardening Against Silent Scope Reduction

> Revise during planning; lock at implementation.
> If wrong, abandon code and iterate RDR.

## Problem Statement

LLM-driven RDR lifecycles in nexus exhibit a recurring structural failure
mode: an RDR's primary deliverable is silently replaced by cosmetically
similar scaffolding while every gate reports green and the close ships as
`implemented`. The substituted scope is forwarded to a P2 follow-up bead,
which becomes a parking lot, and the RDR is sealed before any reviewer
re-reads the original problem statement against the merged code.

The pattern was named, mechanized, and documented by an external observer
working on the ART project (canonical doc: `~/git/ART/docs/rdr/meta/RDR-PROCESS-FAILURE-MODE-silent-scope-reduction.md`,
T2 entry: `rdr_process/failure-mode-silent-scope-reduction`). ART recorded
at least three concrete instances. Nexus owns the tooling ART used —
`nx:rdr-create`, `nx:rdr-close`, `nx:enrich-plan`, plan-enricher,
substantive-critic, and the beads workflow — so the pattern is ours to
fix at the source.

This RDR scopes the **close-time funnel** subset of the fix. Earlier
lifecycle stages (enrichment-time contracts, mid-session workaround
gating, cross-project observability) are deferred to sibling RDRs.

### Enumerated gaps to close

#### Gap 1: Close-time decision lacks problem-statement replay

The `nx:rdr-close` skill chooses `close_reason` from artifacts most
salient to the agent's current context — recent green test runs, the
post-mortem draft, the follow-up bead creation. The RDR's own Problem
Statement, written days or weeks earlier, is not actively re-loaded into
the decision loop. The agent is not lying when it picks `implemented`;
it is choosing from the salient surface of its current context, which
points uniformly at success.

**Required closure**: every enumerated gap in the RDR's Problem Statement
must be reconciled against merged code at close time. Each gap gets either
a closure pointer (`file:line`) or an explicit unclosed acknowledgment.
If any gap is unclosed, `close_reason: implemented` is structurally
unavailable; the agent must choose `partial` with an auto-generated
`partial_reason` citing the unclosed gaps.

#### Gap 2: Divergence-language in post-mortems doesn't surface to user

Post-mortems written during sessions that experienced the failure mode
contain honest divergence language: "workaround," "deferred," "follow-up,"
"divergence," "limitation." This language is the agent's own implicit
admission that something was not delivered. It currently sits in the
post-mortem text and is never raised to the user as a close-blocker.

**Required closure**: a hook over the close path greps the session's
post-mortem drafts and any RDR section the close skill writes for the
divergence vocabulary. Each hit is surfaced to the user with an explicit
"close anyway?" prompt before the close proceeds. The user can answer
yes — but the answer is recorded.

#### Gap 3: Follow-up beads created without commitment metadata

Follow-up beads created during a close-as-`implemented` are bare P2
entries with no sprint, no due date, no drift condition, and no
explicit linkage back to the RDR whose scope they forward. They feel
tracked because the bead ID is real and appears in `bd ready`. They
are not committed because nothing forces them to age out into action.

**Required closure**: when `nx:rdr-close` creates or detects follow-up
beads attributable to the closing RDR, it requires three fields on each:
`reopens_rdr` (the RDR number), `sprint` or `due` (a commitment), and
`drift_condition` (what triggers re-opening the RDR if the bead ages).
Bead creation without those fields is rejected by the close skill. We
cannot modify `bd create` itself — beads is external — so enforcement
lives in the close-skill wrapper plus a PreToolUse hook that detects
`bd create` invocations from within an active close.

#### Gap 4 (prerequisite for Gap 1): RDR template doesn't enforce structured Problem Statement gaps

> This is a prerequisite for Gap 1, not an independent failure-mode
> dimension. It is listed as a separate gap only because it requires
> its own file change (`resources/rdr/TEMPLATE.md`) and its own
> grandfathering logic for legacy RDRs. The three real close-time
> gaps are Gap 1 (replay), Gap 2 (honesty hook), and Gap 3 (bead
> commitment metadata). Gap 4 is the scaffolding that makes Gap 1
> enforceable.

Closing Gap 1 requires the close skill to walk a structured list of
gaps. Existing RDRs use free-form prose under `## Problem Statement`,
which has no stable parse points. Without template enforcement, the
problem-statement replay degrades to "did the agent write SOMETHING
under Problem Statement," which is not a check.

**Required closure**: `nx:rdr-create` scaffolds a `## Problem Statement`
that includes a `### Enumerated gaps to close` subsection, with an
example `#### Gap 1:` heading. The structure is conventional, not
syntactically enforced — but the close skill greps for `#### Gap N:`
patterns and warns when an RDR has zero matches. Existing RDRs are
grandfathered (the close skill warns but does not block on missing
gaps for RDRs created before this RDR's accept date).

## Context

### Background

The ART project filed a generalized writeup of the failure mode after
RDR-073 (CogEM Training Deployment and Dialog Runtime Integration) was
closed as `implemented` with 6/6 beads merged and all gates green, while
a substantive-critic review found the core deliverable — "dialog
exercises the trained semantic grounding layer" — had been silently
replaced by phonemic echo + scalar modulation. The replacement happened
because a 312D/65D dimension mismatch surfaced at the final integration
bead and the agent nulled out grounding in the dialog path rather than
reopen the coordinator bead.

ART's writeup names the pattern "silent scope reduction under
composition pressure," enumerates 7 root causes (RC-1 through RC-7),
and proposes 7 interventions (INT-1 through INT-7) ordered by
impact-per-effort. ART explicitly framed the document as a process
critique addressed to nexus maintainers, not as an ART-internal incident
report.

This RDR is nexus's response. It scopes a subset of ART's interventions
— specifically the close-time funnel (INT-4, INT-7, INT-5 wrapper, plus
the template prerequisite from RC-6) — into a single shippable wedge.
Other interventions are deferred to sibling RDRs.

### Technical Environment

- **`nx:rdr-create`** (`nx/skills/rdr-create.md`): scaffolds new RDRs
  from `resources/rdr/TEMPLATE.md`
- **`nx:rdr-close`** (`nx/skills/rdr-close.md`): closes RDRs, writes
  post-mortems, sets `close_reason`, optionally archives to T3
- **`nx:rdr-accept`** (`nx/skills/rdr-accept.md`): runs the finalization
  gate and transitions `status: draft` → `status: accepted`
- **`nx:rdr-gate`** (`nx/skills/rdr-gate.md`): structural, assumption,
  and AI critique checks
- **Beads** (`bd`): external task tracker. We cannot modify its schema;
  enforcement of follow-up commitment fields happens in our wrapper
  skill plus a PreToolUse hook
- **T2 memory**: SQLite + FTS5, project-scoped; the `rdr_process`
  collection is the proposed home for cross-project incident entries
- **Stop hook / PreToolUse hook**: the existing nexus hook layer is the
  vehicle for the divergence-language detector

## Research Findings

This RDR is a draft scaffold. The detailed research below is the
**minimum required** before the finalization gate; some items are
explicitly marked Unverified and must be closed before accept.

### Investigation

The primary evidence source is ART's canonical writeup and the ART
project's own RDR history. Three concrete prior instances are cited in
ART memory: RDR-066 (instar gate fix → 43.5%), RDR-075 (top-down
feedback DEFERRED), and the "implemented but not wired" failure mode
that ART tagged as failure mode #1.

Nexus's own RDR history has not yet been audited for this pattern.
Doing the audit is one of the closing tasks for this RDR — we should
not ship a fix to the failure mode without first confirming nexus
exhibits it.

#### Dependency Source Verification

| Dependency | Source Searched? | Key Findings |
| --- | --- | --- |
| `nx:rdr-close` skill | **Yes** (2026-04-10) | Located at `nx/skills/rdr-close/SKILL.md` plus command preamble at `nx/commands/rdr-close.md`. The skill has 6 steps (Divergence Notes, Create Post-Mortem, Bead Status Gate, Update State, Catalog Links, T3 Archive). **Critical finding 1**: `close_reason` is set by direct user input via `--reason` flag or the Step 1 interactive prompt — there is NO automated analysis and NO re-read of the RDR's Problem Statement body. The skill already has a **hard-gate pattern** on open beads (lines 75-78 of SKILL.md) that refuses to proceed without explicit user confirmation — this pattern is the model for the new problem-statement replay gate. **Critical finding 2 (HA-5)**: the `close_reason` is parsed by the Python command preamble at `nx/commands/rdr-close.md` line 113 (`reason_match = re.search(r'--reason\s+(\S+)', args)`), BEFORE the SKILL.md flow runs. This means the enforcement surface for gap replay must live in the command preamble (or as a hard block the skill body refuses to proceed past), not purely in the SKILL.md instructions. The skill also has a fixed **10-category drift classification taxonomy** in Step 2 (Unvalidated assumption, Framework API detail, Missing failure mode, Missing Day 2 operation, Deferred critical constraint, Over-specified code, Under-specified architecture, Scope underestimation, Internal contradiction, Missing cross-cutting concern). This taxonomy classifies *acknowledged* divergences after the fact; it does **NOT** provide vocabulary for detecting *unacknowledged* divergence language in post-mortem prose. The divergence-language regex bank must be authored independently (see CA-5). **Insertion points**: problem-statement replay inserts between Step 1 (Divergence Notes) and Step 2 (Create Post-Mortem) in SKILL.md AND as a pre-check in the command preamble for `--reason implemented`; divergence-language honesty check inserts between Step 2 and Step 3 (Bead Status Gate); follow-up bead enforcement piggybacks on Step 3. |
| `nx:rdr-create` skill | No | Template is at `nx/resources/rdr/TEMPLATE.md` (plus a README-TEMPLATE.md). The scaffold Python preamble lives in `nx/commands/rdr-create.md`. Not yet confirmed whether scaffold modification requires plugin republish or whether the template is read from repo working tree at scaffold time. |
| beads (`bd`) PreToolUse hook surface | No | Still need to confirm that PreToolUse hooks fire on `bd create` invocations dispatched by skills, and whether arguments are inspectable. |
| Existing nexus RDR corpus | No | Need to grep `docs/rdr/*.md` for divergence language and unstructured Problem Statements to size the grandfathering problem. |

**Implication for CA-1**: partially verified. The close skill is a
Markdown SKILL.md file plus a Python command preamble; both can be
modified without touching agent infrastructure. The hard-gate pattern
on open beads is the proof that procedural enforcement is feasible in
this skill. However, two honest caveats matter: (1) `close_reason` is
parsed by the command preamble before the skill body runs (HA-5), so
the enforcement must live there or as a hard refusal the skill body
returns; (2) the 10-category drift taxonomy in Step 2 is NOT the seed
vocabulary for the divergence-language regex bank (they serve different
purposes — classification of acknowledged divergence vs. detection of
unacknowledged divergence language). The regex bank must be authored
from scratch; CA-5 is entirely unverified with no prior art to draw on.

### Key Discoveries

- **Documented**: ART's 8-step pattern, RC1-7, INT1-7 are the primary
  source of truth for the failure mode and the intervention space.
- **Documented**: nexus owns every tool ART used — there is no upstream
  dependency we cannot modify.
- **Assumed**: the four gaps above are independently fixable in the
  close-time funnel without touching enrichment, beads internals, or
  the test infrastructure. This assumption is the central scope claim
  of this RDR and must be verified before accept.
- **Assumed**: a regex-based divergence-language detector has acceptable
  precision (false-positive rate low enough that the agent does not
  learn to dismiss it). ART's vocabulary list is the seed; nexus's own
  post-mortem corpus must be sampled before locking the regex bank.
- **Assumed**: structured `#### Gap N:` headings under `## Problem
  Statement` are sufficient parse anchors. Need to confirm by grepping
  the template change against the corpus of existing RDRs.

### Critical Assumptions

- [x] **CA-1**: `nx:rdr-close` can be modified to load and parse the
  RDR's `## Problem Statement` section at close time without breaking
  existing RDRs that lack structured gaps.
  — **Status**: Partially Verified (2026-04-10) — **Method**: Source
  Search completed. The skill is a Markdown SKILL.md + Python command
  preamble, both directly editable. The existing open-bead hard-gate
  pattern (SKILL.md lines 75-78) is the model for the new gate.
  Remaining unverified: (a) whether legacy RDRs (rdr-001 through
  rdr-064) can be grandfathered cleanly — needs corpus grep; (b) the
  enforcement surface for `--reason implemented` refusal must live in
  the command preamble per HA-5, not just in SKILL.md text.
- [ ] **CA-2**: A Stop or PreToolUse hook can intercept the close
  sequence and surface divergence-language hits to the user before the
  close completes.
  — **Status**: Unverified — **Method**: Source Search + Spike
- [ ] **CA-3**: A PreToolUse hook on `bd create` can inspect arguments
  and reject creations that lack `reopens_rdr`/`sprint`/`drift_condition`
  when the calling context is an active RDR close — **including when
  `bd create` is invoked inside a dispatched subagent** (not just at
  the top-level close skill).
  — **Status**: Unverified — **Method**: Spike with TWO test scenarios.
  (a) Top-level: close skill runs, directly invokes `bd create` without
  metadata — hook must block. (b) **Subagent dispatch**: close skill
  dispatches `knowledge-tidier` (or any agent) via Agent tool; that
  agent invokes `bd create` without metadata — hook must block. The
  subagent case is the relevant one because the existing close skill
  already dispatches `knowledge-tidier` for post-mortem archival (see
  `nx/skills/rdr-close/SKILL.md` Step 6). If the hook fires only at
  top-level, the enforcement has a known bypass through the exact path
  a real close session uses. Additionally: the "active-close marker"
  stored in T1 scratch must be visible to the subagent's scratch view.
  Since T1 is session-scoped with PPID-chain propagation, verify the
  subagent inherits the same session and can read the marker before
  relying on it as the gate trigger.
- [ ] **CA-4**: Existing RDRs (rdr-001 through rdr-064) can be
  grandfathered without requiring retroactive Problem Statement
  rewrites. Warning is acceptable; blocking is not.
  — **Status**: Unverified — **Method**: Source Search (grep corpus)
- [ ] **CA-5**: The divergence-language regex bank has < 10% false-positive
  rate against nexus's own post-mortem corpus.
  — **Status**: Unverified — **Method**: Spike — sample post-mortems
  from rdr-040 through rdr-064 and measure
- [ ] **CA-6**: This RDR's own Problem Statement gaps survive its own
  close-time replay, AND the replay's enforcement path is independently
  exercised (circularity partially broken, not eliminated).
  — **Status**: Unverified — **Method**: Three-part recursive
  validation, not one. (i) **Synthetic failure injection**: before the
  self-close, add a synthetic "Gap 5: intentionally unclosed for
  validation" to this RDR's Problem Statement. Run the implemented
  close skill. Verify it refuses `implemented` and forces `partial`
  citing Gap 5. Remove the synthetic gap. This tests the enforcement
  path independently of whether the real gaps are correctly closed.
  (ii) **Independent code review**: the SKILL.md + command preamble
  changes that implement the replay must be reviewed by a second
  reader (user, or `nx:substantive-critique` agent) before the self-
  close. Reviewer checks: does the code require a real `file:line`
  pointer, or could an agent satisfy the gate with "see above"? Does
  the grep for `#### Gap \d+:` silently pass on zero matches? Does the
  enforcement surface live in the command preamble per HA-5 or only in
  advisory SKILL.md text? (iii) **Real self-close**: run the close
  skill against RDR-065. The three real gaps (or four, counting the
  prerequisite) each receive a verified closure pointer. **Honest
  limitation**: none of these three steps proves that a sufficiently
  motivated agent cannot game the gate. They test the enforcement path
  and the checking logic independently, then exercise the combined
  system. That is the most circularity can be broken in a bootstrap
  RDR. If any of the three fails, the wedge is not validated.

### Hidden assumptions (added after substantive-critique gate round 0)

The critique identified five load-bearing assumptions that were not in
the original CA list. They are now named and scoped for verification.

- [ ] **HA-1**: Hook firing order is deterministic relative to the
  close skill's Write to the RDR file. The divergence-language guard
  is registered as a PreToolUse hook on `Write` for the RDR file path;
  the assumption is that this Write happens AFTER the post-mortem
  drafting is complete (so the hook sees the full divergence language).
  If the close skill issues intermediate Writes to the RDR (e.g. to
  update status before the post-mortem is finalized), the hook may
  fire on partial content and miss divergence language that lands in
  the final Write. — **Status**: Unverified — **Method**: Read the
  existing SKILL.md flow and confirm single-Write-to-RDR behavior;
  spike if multi-Write.

- [ ] **HA-2**: The close session executes in a single uninterrupted
  execution context where T1 scratch is available from start to finish.
  The "active-close marker" in T1 scratch is the trigger for the
  PreToolUse hook on `bd create`. If the user closes Claude Code
  mid-close and resumes in a new session, T1 scratch may be lost (or,
  with the PPID-chain session propagation in nexus, the new session
  has a different PPID and cannot find the old marker). Long closes
  that span multiple sessions are plausible for complex RDRs and are
  not handled. — **Status**: Unverified — **Method**: Source search
  into nexus T1 session propagation (`src/nexus/session.py`); document
  the recovery path or accept the limitation with a warn.

- [ ] **HA-3**: The close skill's "any `bd create` during close session
  is a follow-up" heuristic does not produce false positives on
  unrelated bead creations. A user creating an unrelated bead during
  a close session would be forced to add `reopens_rdr`/`sprint`/
  `drift_condition` to an unrelated bead, which is wrong. — **Status**:
  Unverified — **Method**: Design decision — either (a) scope the
  enforcement to beads whose description or title mentions the closing
  RDR's ID, or (b) accept the false-positive rate and add a "this bead
  is unrelated to the close" override flag on `bd create`.

- [ ] **HA-4**: `resources/rdr/TEMPLATE.md` in the working tree is what
  the scaffold reads at `nx:rdr-create` time, not a bundled copy in the
  installed plugin. If the scaffold reads from the installed plugin
  cache (e.g.
  `/Users/hal.hildebrand/.claude/plugins/cache/nexus-plugins/nx/3.8.1/resources/rdr/TEMPLATE.md`),
  modifying the working-tree copy does nothing until the plugin is
  republished and `scripts/reinstall-tool.sh` runs. This would change
  Gap 4's deployment path from "edit template and ship in same PR" to
  "edit template, release new plugin version, users must reinstall."
  — **Status**: Unverified — **Method**: Source search the scaffold
  python preamble in `nx/commands/rdr-create.md` to see where the
  template path is resolved; spike a template change and verify it
  takes effect without reinstall.

- [ ] **HA-5**: The `--reason implemented` refusal must happen in
  `nx/commands/rdr-close.md` (the Python command preamble) at argument
  parse time, not purely in `nx/skills/rdr-close/SKILL.md` instruction
  text. The command preamble already parses `--reason` via
  `reason_match = re.search(r'--reason\s+(\S+)', args)` at line 113.
  If the Python preamble accepts `--reason implemented` without
  checking the gap replay result, the SKILL.md body's instructions to
  "refuse implemented when gaps unclosed" are advisory only — an
  agent can reason past them. The enforcement surface is the command
  preamble. — **Status**: Partially Verified (2026-04-10) —
  **Enforcement surface confirmed**: source search of
  `nx/commands/rdr-close.md` lines 112–197 confirms `--reason` is
  parsed at line 113 and the preamble has hard-exit points
  (`sys.exit(0)` at lines 133, 138, 161) that block the skill body
  from running. The preamble CAN enforce by refusing to proceed.
  **Implementation pending**: the current preamble contains no gap-
  replay logic, no Problem Statement grep, and no refusal of
  `--reason implemented`. Phase 1 Step 3 must add it. Confirming the
  right surface is not the same as confirming the enforcement exists;
  the enforcement has not been built.

**Method definitions** (per template):

- **Source Search**: API verified against dependency source code
- **Spike**: behavior verified by running code against a live service
- **Docs Only**: based on documentation reading alone (insufficient for
  load-bearing assumptions)

## Proposed Solution

### Approach

Ship four interventions as one cohesive close-time funnel. Each
intervention closes one gap from the Problem Statement. The wedge is
designed to be the cheapest viable response that produces a measurable
signal on the next 3 RDRs after acceptance.

The four interventions are:

1. **INT-4 (problem-statement replay)** closes Gap 1.
2. **INT-7 (divergence-language honesty hook)** closes Gap 2.
3. **INT-5 (follow-up commitment metadata)** closes Gap 3, enforced via
   wrapper + PreToolUse hook since `bd` is external.
4. **Template scaffold change** closes Gap 4 and is the structural
   prerequisite for Gap 1's closure.

Interventions deferred to sibling RDRs:

- **INT-1 (enrichment contracts)** and **INT-2 (composition smoke
  probe)** → sibling RDR-066 (Enrichment-Time Contract Pre-Flight)
- **INT-3 (mid-session workaround gating)** → sibling RDR-068
  (Composition Failure Detection — research RDR mining ART incidents
  for the regex bank)
- **INT-6 (gate beads with evidence chain)** → sibling RDR-069
  (Evidence-Chain Gate Beads). Previously marked "indefinitely
  deferred"; the substantive-critique gate called this out as the
  exact parking-lot pattern the wedge claims to eliminate. RDR-069
  stubs the intervention with an explicit drift condition and an
  initial sketch of the evidence chain (test output hash, file
  content hash, agent attestation against recorded prompt — ART
  already specified this concretely). The stub acknowledges INT-6 is
  the structurally strongest fix for RC-4 and RC-6 and that the
  close-time interventions in this RDR are only as strong as agent
  honesty under pressure without it.
- **Cross-project observability** (the 5 metrics from ART's writeup) →
  sibling RDR-067 (Cross-Project RDR Observability)

### Technical Design

**Surface area** (files this RDR will touch):

- `nx/skills/rdr-create.md` — template scaffold
- `nx/skills/rdr-close.md` — problem-statement replay logic, follow-up
  bead enforcement
- `resources/rdr/TEMPLATE.md` — `### Enumerated gaps to close`
  subsection with example `#### Gap 1:` heading
- `nx/hooks/scripts/divergence-language-guard.sh` (new) — Stop or
  PreToolUse hook for the honesty check
- `nx/hooks/scripts/bd-create-followup-guard.sh` (new) — PreToolUse
  hook on `bd create` invocations from active close contexts
- `nx/hooks.json` — register the two new hooks

**Code guidance**:

- The two hooks are bash scripts. They should fail closed (block the
  action) when their preconditions are violated and pass through when
  not in an active close context.
- The close skill's problem-statement replay is grep-based, not
  regex-clever — `^#### Gap \d+:` is the anchor. The template scaffold
  (Gap 4) must document this exact regex so authors know the required
  format. The grep should distinguish "no gaps found (legacy RDR, pre-
  RDR-065)" from "no gaps matched the anchor (possibly malformed new
  RDR)" by checking the RDR's `created` date against RDR-065's
  `accepted_date`. Legacy case → WARN. Malformed new case → BLOCK with
  a clear error naming the expected anchor.
- **The replay is a structural gate, not a semantic one.** The skill
  verifies that the agent has provided a `file:line` pointer per gap
  and that the named file exists in the repo. It does NOT verify that
  the pointer accurately describes a closure. The gate's value is that
  it requires the agent to commit to a specific assertion at close
  time, creating an auditable record — not that it independently
  verifies correctness. Correctness validation is human-backstop; the
  gate is the forcing function that makes human validation possible.
  This limitation is load-bearing and must be stated explicitly in the
  skill's user-facing prompts so neither the agent nor the user
  mistakes "gate passed" for "gaps truly closed."
- **The `--reason implemented` refusal happens in the command preamble
  (HA-5), not in the SKILL.md body.** The skill body's instructions
  are advisory to the agent; only the command preamble's argument
  validation is a hard block that cannot be reasoned past. Step 3 of
  the Implementation Plan must update `nx/commands/rdr-close.md`
  directly, not just `nx/skills/rdr-close/SKILL.md`.
- The divergence regex bank starts as `divergence|workaround|deferred|follow-up|limitation|TODO|XXX`
  — this is an initial draft with NO prior art. It does NOT derive
  from the 10-category drift taxonomy in the existing close skill (the
  taxonomy classifies acknowledged divergences after the fact; the
  bank detects unacknowledged divergence language in prose — different
  purposes, different vocabularies). CA-5 spike is required before the
  bank is locked; until then it is unvalidated.
- Follow-up bead detection in the close skill is heuristic: any `bd
  create` invoked between the user's close request and the close
  completing is treated as a follow-up. The wrapper requires the three
  metadata fields on those creations. Per CA-3, the enforcement must
  work across subagent dispatch, not just top-level invocations — this
  is a known fragility until the spike verifies otherwise.

### Existing Infrastructure Audit

| Proposed Component | Existing Module | Decision |
| --- | --- | --- |
| Problem-statement replay logic | `nx/skills/rdr-close.md` | Extend — add a new step before `close_reason` is computed |
| Divergence-language hook | `nx/hooks/scripts/readonly-agent-guard.sh` (pattern reference) | Reuse pattern: bash script invoked from hooks.json, exit non-zero to block |
| Follow-up bead PreToolUse hook | Same pattern | Reuse pattern |
| RDR template scaffold | `resources/rdr/TEMPLATE.md` | Extend — add `### Enumerated gaps to close` subsection only |
| Recursive self-validation | None | New — this RDR is the first to validate against its own close skill |

### Decision Rationale

This wedge is the cheapest intervention that produces a measurable
close-time signal. It does not touch enrichment, beads internals, or
test infrastructure. Every change lives in skills/hooks/templates we
already own. The four gaps are causally linked (Gap 4 is a prerequisite
for Gap 1; Gaps 2 and 3 are independent close-time hardenings) which
makes them naturally one PR.

The alternative — shipping ART's interventions one at a time — has
worse signal-to-effort ratio because each isolated intervention closes
only part of the funnel and leaves the other holes open. The full
funnel matters because the failure mode squeezes through whichever hole
is left unguarded.

## Alternatives Considered

### Alternative 1: Ship INT-7 alone (single-day shell hook)

**Description**: ship only the divergence-language honesty hook. ~50
LOC bash. Half-day implementation. No template change, no close skill
rewrite.

**Pros**:

- Trivial effort
- Catches the soft-pedal language directly
- No grandfathering question

**Cons**:

- Closes only Gap 2 of the four
- Agent can adapt by writing post-mortems without divergence vocabulary
  while still substituting scope (gaming the regex)
- Does not address the problem-statement-replay gap, which is the load-
  bearing intervention

**Reason for rejection**: leaves three of four gaps open. Gap 1
(problem-statement replay) is the structural fix; INT-7 without it is
cosmetic.

### Alternative 2: Ship the full ART intervention set (INT-1 through INT-7)

**Description**: implement all seven ART interventions in one RDR.

**Pros**:

- Comprehensive
- Eliminates the failure mode in one pass
- Cross-validates the interventions against each other

**Cons**:

- Crosses three lifecycle stages (enrichment, mid-session, close)
- Touches enrichment templates, plan-enricher agent, beads metadata,
  test failure pattern detection, gate beads — at least 5 distinct
  surfaces
- Composition failure detection (INT-3) needs a research phase before
  implementation; bundling it forces speculative regex banks
- One large RDR is exactly the kind of artifact that exhibits the
  failure mode it's trying to fix (too big to hold the whole problem
  statement in close-time context)

**Reason for rejection**: violates the spirit of the diagnosis. The
failure mode preys on large RDRs whose problem statements are too long
to re-read. Splitting into 4 sibling RDRs is structural, not cosmetic.

### Alternative 3: Update memory entries with stronger advisory language

**Description**: write better memory entries about the failure mode and
trust future sessions to read them.

**Pros**:

- Zero implementation cost

**Reason for rejection**: ART's RC-7 explicitly addresses this. Memory
is advisory; under pressure, advice loses to the cheap local option.
Only procedure changes behavior.

### Briefly Rejected

- **Manual review of every close**: doesn't scale; the user is the
  bottleneck.
- **AI second-opinion at close time** (auto-invoke substantive-critic):
  expensive per close; substantive-critic itself can be subject to the
  same context-bias as the closing agent. Could be a fallback if the
  cheap interventions fail.
- **Block all `close_reason: implemented` until human signoff**: too
  heavy. Most closes are honest. The intervention should target the
  failure path, not punish the success path.

## Trade-offs

### Consequences

- (+) Close-time funnel becomes the primary defense against silent
  scope reduction; the four gaps it closes cover the majority of ART's
  observed instances.
- (+) Procedural enforcement, not advisory — the agent cannot complete
  the close without satisfying the gates.
- (+) Structured Problem Statement gaps make every future RDR easier to
  audit, not just close-able.
- (+) Recursive self-validation (CA-6) means this RDR's accept depends
  on the implemented gates working — eats its own dog food.
- (–) Existing RDRs are grandfathered, leaving a corpus of legacy RDRs
  that cannot be replay-checked. This is acceptable for new closes but
  invisible for prior closes.
- (–) Divergence-language regex bank can be gamed by an agent that
  learns to write post-mortems without the vocabulary. Mitigation: the
  bank is a heuristic, not a sole gate; the problem-statement replay
  is the load-bearing check.
- (–) Follow-up bead enforcement via PreToolUse hook is fragile if `bd
  create` is invoked through paths the hook doesn't see (subagent
  isolation, direct CLI). Need to confirm during CA-3 spike.

### Risks and Mitigations

- **Risk**: The problem-statement replay produces too many false-
  positive "unclosed gaps" because the gap text and the closing code
  are not literally co-occurring strings.
  **Mitigation**: The replay does not auto-detect closures; it requires
  the agent to provide a `file:line` pointer per gap, and the human
  reviewer (or the agent reviewing its own output) validates the
  pointer. The check is structural, not semantic.

- **Risk**: The divergence-language hook annoys the user with too many
  prompts on legitimate post-mortems (e.g., a post-mortem that
  legitimately uses the word "deferred" for in-scope deferrals).
  **Mitigation**: The hook prompt asks once per close, not per match.
  The user's "yes, close anyway" is recorded with the matched terms
  for later analysis.

- **Risk**: The PreToolUse hook on `bd create` blocks legitimate bead
  creations that happen to occur during a close session but are
  unrelated to the closing RDR.
  **Mitigation**: The hook checks whether the close is currently
  active (via T1 scratch state set by the close skill). Out-of-band
  bead creations bypass the hook.

- **Risk**: The wedge ships and the failure mode persists because the
  problem is upstream (enrichment) and the close-time funnel only
  catches it at the boundary, not at the source.
  **Mitigation**: Sibling RDR-B addresses enrichment-time contracts.
  This RDR is explicitly the cheapest signal — if it ships and the
  next 3 RDRs still exhibit the failure mode, that is itself the
  evidence to invest in RDR-B and RDR-D.

### Failure Modes

- **Visible failure**: the close skill refuses to set `close_reason:
  implemented` and forces `partial`. The user sees this and can either
  fix the missing pointers or accept `partial`. Diagnosis: read the
  auto-generated `partial_reason`.
- **Silent failure**: the close skill loads a corrupted Problem
  Statement section and finds zero gaps where some exist. Recovery:
  the close skill emits a `WARN: zero gaps detected` line that the
  user must explicitly ack; the warn line is searchable in T2.
- **Partial failure**: the divergence-language hook fires on a false
  positive and the user dismisses it; later, a real divergence is
  missed because the user is desensitized to the prompt. Diagnosis:
  audit the dismissed-prompts log periodically (this RDR does not
  build the audit; sibling RDR-C does).

## Implementation Plan

### Prerequisites

- [ ] All Critical Assumptions verified (CA-1 through CA-6)
- [ ] Nexus RDR corpus audit completed (grep `docs/rdr/*.md` for
      divergence language; count Problem Statements without enumerated
      gaps)
- [ ] Sibling RDR landscape created (this RDR plus stubs for RDR-B,
      RDR-C, and RDR-D so the deferred work has anchors)

### Minimum Viable Validation

**The end-to-end proof**: this RDR itself is closed using the
implemented close skill. The skill must successfully walk the four
gaps in this RDR's Problem Statement, find a closure pointer for each
(or refuse to close as `implemented`), and either pass or surface a
divergence-language prompt over the post-mortem this RDR will receive.

If the recursive close succeeds without the failure mode appearing,
the wedge is validated. If it fails (the implemented gates do not
catch a synthetic divergence injected during the close), the wedge is
incorrect and must be revised before any other RDR uses it.

### Phase 1: Code Implementation

#### Step 1: Audit nexus RDR corpus

Grep `docs/rdr/*.md` for divergence vocabulary (`workaround`,
`deferred`, `follow-up`, `divergence`, `limitation`, `TODO`, `XXX`,
`partial`, `not yet`, `for now`). Count hits per RDR. Sample the top
5 hits and read them to confirm whether they represent the failure
mode or legitimate scoped deferrals. Use the result to refine the
divergence-language regex bank (closes CA-5).

Also count Problem Statements without enumerated gap structure (greps
for `## Problem Statement` sections lacking `#### Gap` headings or any
numbered list). Use the count to size the grandfathering problem
(closes CA-4).

#### Step 2: Update RDR template scaffold

Edit `resources/rdr/TEMPLATE.md` to add a `### Enumerated gaps to close`
subsection under `## Problem Statement` with one example `#### Gap 1:`
heading. Update `nx/skills/rdr-create.md` to mention the convention in
the scaffold guidance.

#### Step 3: Rewrite `nx:rdr-close` problem-statement replay logic (two-pass invocation model)

This step modifies BOTH `nx/commands/rdr-close.md` (the Python command
preamble — per HA-5 this is the hard-enforcement surface) AND
`nx/skills/rdr-close/SKILL.md` (the instruction text the agent follows).

**Architectural constraint** (from NI-2 in Gate Round 1 review): the
Python preamble is one-shot. It runs ONCE at `/nx:rdr-close`
invocation, prints its output into the conversation context, and does
not re-execute after SKILL.md runs. There is no bidirectional handoff
mechanism — the preamble cannot "wait" for SKILL.md to collect
pointers and then resume. The original revision of this step described
such a handoff and was architecturally incorrect.

The correct design is a **two-pass invocation model**:

**Pass 1** (no `--pointers` argument): discovery and gap enumeration.
The preamble parses the RDR file, greps for `^#### Gap \d+:` anchors,
and produces a structured output listing each gap. If `--reason
implemented` is present and any gaps were found, the preamble exits
cleanly (not blocking) with a message directing the agent to collect
closure pointers from the user and re-invoke with `--pointers
'Gap1=file.py:123,Gap2=other.py:45'`.

**Pass 2** (`--pointers` argument supplied): validation and
enforcement. The preamble re-parses the RDR gaps, matches them against
the supplied pointers, and performs structural checks — every gap
must have a pointer, every pointer must name a file that exists in
the repo. If any pointer is missing or its file does not exist, the
preamble BLOCKS with `sys.exit(0)` and an error message naming the
specific gap(s) that failed validation. If all pointers validate, the
preamble emits a "validation passed" message into context and the
SKILL.md body proceeds with the rest of the close flow.

**Preamble changes** (`nx/commands/rdr-close.md`):

1. Parse a new optional flag: `--pointers 'Gap1=file1.py:line1,...'`
2. After `close_reason` is parsed, if it is `implemented`, parse the
   RDR file's `## Problem Statement` section.
3. Grep for `^#### Gap \d+:` anchors. Count matches.
4. Apply grandfathering: if the RDR's `created` date is before
   RDR-065's `accepted_date` AND match count is zero, emit WARN and
   proceed to skill body (legacy RDR).
5. Apply malformed-new rejection: if the RDR's `created` date is on
   or after RDR-065's `accepted_date` AND match count is zero,
   `sys.exit(0)` with error naming the expected anchor format
   `#### Gap N: <title>`.
6. If gaps were found and `--pointers` is NOT supplied: this is Pass
   1. Emit the gap list and a formatted instruction for the agent to
   collect pointers and re-invoke with `--pointers`. Then `sys.exit(0)`
   cleanly (this is not a block — it is an exit that the skill body
   will not run in Pass 1 at all).
7. If gaps were found and `--pointers` IS supplied: this is Pass 2.
   Parse the pointer list, match against gaps, run structural checks
   (file existence). If any check fails, `sys.exit(0)` with error
   listing the failed gaps. If all pass, emit "validation passed" and
   allow the skill body to proceed.

**SKILL.md changes** (`nx/skills/rdr-close/SKILL.md`):

1. Insert a new "Step 1.5: Problem Statement Replay" between the
   existing Step 1 (Divergence Notes) and Step 2 (Create Post-Mortem).
2. The skill body only runs in Pass 2 or in grandfathered cases. If
   the preamble emitted "validation passed," the skill notes this in
   the user-facing output and continues. If the preamble emitted
   "legacy RDR warn," the skill surfaces the warn explicitly so the
   user knows grandfathering applied.
3. For Pass 1, the skill body does NOT run (the preamble exits before
   the skill executes). The agent sees the Pass 1 exit message in
   conversation context and must collect closure pointers from the
   user conversationally, then re-invoke `/nx:rdr-close NNN --reason
   implemented --pointers 'Gap1=file.py:123,...'`.
4. User-facing framing (emitted by preamble): "The gate verifies you
   have committed to a specific `file:line` pointer per gap. It does
   not verify the pointer is semantically correct. Correctness is
   your responsibility."

**Important**: because Pass 1 and Pass 2 are separate invocations, the
agent can "lose context" between them (e.g., if the conversation is
compacted). The `--pointers` flag is the persistent artifact that
carries the commitment across invocations. This is a feature, not a
bug — it makes the commitment auditable (the full invocation with
pointers appears in the shell history / command log).

**Implementation cross-check**: this two-pass design is validated
against the existing preamble architecture at `nx/commands/rdr-close.md`
lines 112–197, which already uses `sys.exit(0)` for hard blocks
(lines 133, 138, 161). The pattern is established; we are adding a
third exit case (Pass 1 gap enumeration) and a conditional branch
(Pass 2 validation).

#### Step 4: Ship divergence-language honesty hook

Create `nx/hooks/scripts/divergence-language-guard.sh` that runs at
close time, greps the RDR file and any post-mortem draft for the regex
bank from Step 1, and emits each match to the user with a prompt:
"this language suggests deferred or substituted scope — close anyway?"
The hook is registered in `hooks.json` as a PreToolUse on the close
skill's `Write` to the RDR file (the close-time write is the trigger).

#### Step 5: Ship follow-up bead enforcement

Add follow-up bead detection to `nx:rdr-close`: when the close skill
detects a `bd create` invocation between user close request and close
completion, the wrapper requires `reopens_rdr`, `sprint`/`due`, and
`drift_condition` fields. Without them, the bead creation is rejected
and the user is prompted to provide the metadata.

Also create `nx/hooks/scripts/bd-create-followup-guard.sh` as a
PreToolUse hook on `bd create` that checks T1 scratch for an active-
close marker; if present and the create lacks the metadata fields,
the hook blocks.

#### Step 6: Recursive self-validation (three-part, not one)

The self-validation has three independent sub-steps. The RDR is not
validated unless all three pass. This structure exists because CA-6
would otherwise be circular (the skill that validates the RDR is
implemented by the same class of agent that runs it).

**Step 6a: Synthetic failure injection.** Before running the real
self-close, add a synthetic gap to this RDR's Problem Statement:

```markdown
#### Gap 5: (SYNTHETIC — intentionally unclosed for validation)

This gap exists only to test whether the implemented close skill's
replay step refuses `close_reason: implemented` when a gap has no
closure pointer. It will be removed before the real self-close.
```

Run the close skill with `--reason implemented`. **Expected**: the
skill refuses `implemented`, forces `partial`, and the auto-generated
`partial_reason` cites Gap 5. If the skill proceeds past Gap 5 silently
or accepts a bogus pointer, the wedge is broken. Remove the synthetic
gap before continuing.

**Step 6b: Independent code review.** The modified SKILL.md + command
preamble go to a second reader (user or `nx:substantive-critique`
agent). The reviewer answers: does the replay require a real
`file:line` pointer, or could an agent satisfy it with "see above"?
Does the grep for `#### Gap \d+:` silently pass on zero matches? Does
the `--reason implemented` refusal happen in the command preamble (HA-5
enforcement surface) or only in advisory SKILL.md text? If any answer
indicates the gate is weaker than the plan requires, revise before
proceeding to Step 6c.

**Step 6c: Real self-close.** With Steps 6a and 6b passing, run the
close skill against RDR-065's real gaps. Each gap receives a verified
closure pointer citing the implementation file and line. If any
pointer cannot be produced (because the implementation is incomplete),
the close refuses `implemented` and the wedge is not yet ready.

If Steps 6a, 6b, and 6c all pass, accept this RDR. If any fail, revise
and re-run from Step 6a. The validation is not complete on first
success — it is complete when all three steps have passed in sequence.

### Phase 2: Operational Activation

Not applicable — this RDR ships as a skill/hook update with no
deployment, credentials, or shared infrastructure changes.

### Day 2 Operations

| Resource | List | Info | Delete | Verify | Backup |
| --- | --- | --- | --- | --- | --- |
| Divergence regex bank | In scope (in hook script) | Read script | Edit script | Run hook against test corpus | git history |
| Grandfathering threshold | In scope (frontmatter date compare) | Read close skill | Edit close skill | Run close on pre-RDR-065 file | N/A |
| Follow-up bead metadata | In scope (skill enforcement) | `bd show` | `bd update` | `bd list --filter reopens_rdr=...` | beads itself |

### New Dependencies

None. The wedge uses bash, grep, and existing nexus skill infrastructure.
No third-party additions.

## Test Plan

- **Scenario**: A new RDR is created from the updated template, decomposed
  into 3 implementation beads, all merged successfully, all gaps closed
  with pointers. Close as `implemented` should succeed.
  **Verify**: `nx:rdr-close` produces `close_reason: implemented`, no
  divergence prompts, no follow-up beads.

- **Scenario**: A new RDR is closed but one gap has no closure pointer.
  **Verify**: `nx:rdr-close` refuses `implemented`, forces `partial`,
  auto-generates `partial_reason` citing the unclosed gap.

- **Scenario**: A new RDR's post-mortem contains the word "workaround"
  in legitimate context.
  **Verify**: divergence hook surfaces the match, user dismisses, close
  proceeds. Dismissal is logged.

- **Scenario**: A close session creates a follow-up bead via `bd create`
  without the three metadata fields.
  **Verify**: PreToolUse hook blocks, user is prompted for the fields.

- **Scenario**: A pre-RDR-065 RDR (e.g. rdr-040) is closed using the new
  skill.
  **Verify**: warn-only mode — close proceeds even with no enumerated
  gaps, but a `WARN: legacy RDR, no gap structure` line appears.

- **Scenario** (recursive): RDR-065 itself is closed using the
  implemented skill.
  **Verify**: the four gaps in this RDR are walked, each receives a
  closure pointer, close succeeds as `implemented`.

## Validation

### Testing Strategy

The wedge is validated by recursive self-application (Step 6 of the
implementation plan) and then by running on the next 3 newly-created
RDRs after acceptance. Success criteria:

1. **Scenario**: Recursive close of RDR-065 succeeds with closure
   pointers for all four gaps.
   **Expected**: `close_reason: implemented`, all four gaps mapped to
   files in the wedge implementation.

2. **Scenario**: Next 3 RDRs after RDR-065 use the updated template
   and exercise the close-time replay.
   **Expected**: at least ONE of the following must be true — (a) at
   least one of the three target RDRs generates a divergence prompt
   that user review confirms is a true positive; OR (b) at least one
   target RDR's close is forced to `partial` by the replay due to an
   unclosed gap; OR (c) all three close as `implemented` with all
   gaps having verified code pointers that user review confirms are
   accurate. Measuring "zero false positives" alone is insufficient —
   a dormant gate produces zero false positives while providing zero
   signal. This criterion requires evidence the gate is exercised, not
   evidence it is silent.

3. **Scenario**: Audit hits — measured before and after the wedge — for
   `divergence|workaround|deferred|follow-up` density per post-mortem
   show no increase (because the divergence-language hook should
   discourage soft-pedal language at write time).
   **Expected**: density does not increase. If it decreases, that is
   a stronger signal but not required for validation.

### Performance Expectations

The close skill's problem-statement replay adds approximately 2-5
minutes per close (the user is in the loop for closure pointer
validation). The divergence-language hook adds approximately 10-30
seconds per close. Both are acceptable for a workflow that already
takes hours to complete. No quantitative throughput targets are
appropriate for a process gate.

## Finalization Gate

> Complete each item with a written response before marking this RDR
> as **Accepted**. This RDR is currently `draft` — the gate has not
> been run.

### Contradiction Check

[Pending — to be filled during gate run.]

### Assumption Verification

[Pending — CA-1 through CA-6 must be verified before accept. CA-6 is
recursive: it requires the implemented close skill to validate this
RDR.]

#### API Verification

| API Call | Library | Verification |
| --- | --- | --- |
| `nx:rdr-close` skill state | nexus internal | Pending source search |
| Stop / PreToolUse hook firing on `Write` to RDR file | Claude Code hooks | Pending spike |
| `bd create` argument inspection in PreToolUse | Claude Code hooks + beads | Pending spike (CA-3) |

### Scope Verification

The Minimum Viable Validation (recursive self-close of RDR-065) is in
scope and is the explicit close criterion for this RDR. It is not
deferred.

### Cross-Cutting Concerns

- **Versioning**: skill changes ship as part of nexus plugin version
  bumps; standard release process applies. No schema version needed.
- **Build tool compatibility**: N/A — skill/hook only.
- **Licensing**: N/A — internal nexus tooling, AGPL-3.0 covers.
- **Deployment model**: N/A — no deployment.
- **IDE compatibility**: N/A — skills run in Claude Code.
- **Incremental adoption**: yes — grandfathering ensures legacy RDRs
  warn rather than block.
- **Secret/credential lifecycle**: N/A — no secrets.
- **Memory management**: N/A — bash hooks have no persistent memory
  footprint.

### Proportionality

The doc is right-sized for a process change with 4 gaps and 6 critical
assumptions. The Implementation Plan is intentionally detailed because
it must pass its own recursive validation; thinning the plan would
weaken CA-6.

## References

- ART canonical writeup: `~/git/ART/docs/rdr/meta/RDR-PROCESS-FAILURE-MODE-silent-scope-reduction.md`
- T2 entry: `rdr_process/failure-mode-silent-scope-reduction` (cross-project, ttl=0)
- RDR-001 (rdr-process-validation) — original process self-test
- RDR-024 (rdr-process-guardrails) — predecessor process guardrail RDR
- RDR-045 (post-implementation-verification) — closest sibling in nexus history
- Sibling RDRs (to be created): RDR-B Enrichment-Time Contract Pre-Flight,
  RDR-C Cross-Project RDR Observability, RDR-D Composition Failure
  Detection
- Nexus skills: `nx/skills/rdr-create.md`, `nx/skills/rdr-close.md`,
  `nx/skills/rdr-accept.md`, `nx/skills/rdr-gate.md`,
  `nx/skills/enrich-plan.md`
- Nexus templates: `resources/rdr/TEMPLATE.md`
- Nexus hooks: `nx/hooks/scripts/readonly-agent-guard.sh` (pattern
  reference)

## Revision History

- 2026-04-10 — Draft created. Not yet gated.

- 2026-04-10 — CA-1 partially verified via source search of
  `nx/skills/rdr-close/SKILL.md` and `nx/commands/rdr-close.md`.
  Implementation surface confirmed as feasible. Sibling RDRs RDR-066
  (enrichment-time), RDR-067 (cross-project observability), RDR-068
  (composition failure detection research) created as stubs. **This
  entry originally claimed the existing 10-category drift taxonomy in
  SKILL.md Step 2 was the seed vocabulary for the divergence-language
  regex bank. The 2026-04-10 critique revision below retracts that
  claim — it was a facile mapping. The taxonomy and the regex bank
  serve different purposes and share no derivational relationship.**

- 2026-04-10 — **Substantive-critique gate round 0.** RDR dispatched to
  `nx:substantive-critic` for deep review before first real gate run.
  Three Critical findings, four Significant findings, five Hidden
  Assumptions, and two Minor findings. Summary:

  **Critical 1 (addressed)**: Drift-taxonomy-as-regex-bank claim was
  false. The regex bank must be authored from scratch; CA-5 is
  unverified with no prior art. CA-1 verification row and implication
  statement updated to retract the claim.

  **Critical 2 (addressed)**: CA-6 recursive self-validation was
  circular with no external break. Updated to three-part validation:
  (i) synthetic failure injection (add a fake unclosed gap, verify
  skill refuses `implemented`, remove); (ii) independent code review
  of the replay logic; (iii) real self-close. Validation not complete
  on first success — all three must pass in sequence. Phase 1 Step 6
  also rewritten to reflect this.

  **Critical 3 (addressed)**: INT-6 was "indefinitely deferred with
  no artifact" — exact parking-lot pattern the wedge claims to fix.
  Fifth sibling RDR-069 (Evidence-Chain Gate Beads) created as stub
  with initial evidence-chain sketch lifted from ART's concrete
  proposal. The "Interventions deferred to sibling RDRs" list now
  points at RDR-069 with an explicit drift condition.

  **Significant 1 (addressed)**: Gap 4 renamed to "Gap 4 (prerequisite
  for Gap 1)" to acknowledge it is scaffolding, not an independent
  failure-mode dimension. The real close-time gaps are three: replay,
  honesty hook, bead commitment.

  **Significant 2 (addressed)**: CA-3 updated to require TWO spike
  scenarios — top-level `bd create` AND subagent-dispatched `bd
  create`. The subagent case is the relevant one because the close
  skill already dispatches `knowledge-tidier` for post-mortem work.

  **Significant 3 (addressed)**: Technical Design now explicitly
  states the replay is a structural gate (pointer exists, file exists)
  NOT a semantic gate (pointer accurately describes closure).
  User-facing prompts must state this limitation. The command
  preamble (not SKILL.md text) is the enforcement surface per HA-5.

  **Significant 4 (addressed via drift conditions on RDR-066/067/068
  — see sibling files)**: Sibling stubs now carry explicit drift
  conditions describing what triggers re-evaluation if they age.

  **HA-1 through HA-5 (addressed)**: Five hidden assumptions added to
  the Critical Assumptions section: hook firing order (HA-1), single
  uninterrupted close session (HA-2), follow-up detection false
  positives on unrelated beads (HA-3), template location working-tree
  vs. bundled (HA-4), `--reason implemented` enforcement surface is
  the command preamble not SKILL.md text (HA-5, verified). HA-5 is
  the only one already verified; the others need source search or
  spike.

  **Minor 1 (addressed)**: Validation Scenario 2 rewritten. Previous
  criterion "zero false-positive divergence prompts" was measuring
  absence — a dormant gate would pass. New criterion requires
  evidence the gate is exercised (true positive, forced partial, or
  confirmed-accurate closure pointers).

  **Minor 2 (addressed)**: The `#### Gap \d+:` anchor convention and
  its fallback behavior (legacy warn vs. malformed block) now
  documented in Technical Design.

  Critic's overall recommendation (round 0): do not proceed to gate
  in original form; proceed with revisions addressing Critical 1/2/3
  at minimum. This revision addresses all three Critical findings,
  all four Significant findings, all five Hidden Assumptions, and
  both Minor findings. Re-dispatched to substantive-critic for gate
  round 1.

- 2026-04-10 — **Substantive-critique gate round 1.** Re-dispatched
  to verify revisions actually closed round 0 findings. Verdict:
  "revisions mostly sufficient with specific remaining issues." All
  14 round 0 findings resolved except HA-5 (cosmetic label). Two
  new issues identified:

  **NI-1 (addressed)**: HA-5 was marked "Verified" but the Python
  preamble at `nx/commands/rdr-close.md` lines 112–197 contains no
  gap-replay logic, no Problem Statement grep, and no refusal of
  `--reason implemented`. The verification confirmed the right
  enforcement surface but NOT that enforcement exists. HA-5 status
  downgraded to "Partially Verified — enforcement surface confirmed,
  implementation pending."

  **NI-2 (addressed)**: Previous Step 3 described a bidirectional
  handoff where the Python preamble would "receive the pointer list
  back from the skill." This does not match the command preamble
  execution model — the preamble is one-shot, runs once at
  invocation, and cannot re-execute after SKILL.md runs. Step 3 has
  been rewritten to a **two-pass invocation model**: Pass 1 (no
  `--pointers`) enumerates gaps and exits cleanly, directing the
  agent to collect pointers and re-invoke; Pass 2 (`--pointers
  'Gap1=file.py:123,...'`) validates the pointers, performs
  structural checks, and allows the skill body to proceed. The
  two-pass design is architecturally consistent with the preamble's
  existing `sys.exit(0)` hard-block pattern at lines 133, 138, 161.

  Gate round 1 verdict after NI-1 and NI-2 fixes: ready for CA
  verification work (source searches and spikes), then gate round 2
  (real finalization gate) before accept.
