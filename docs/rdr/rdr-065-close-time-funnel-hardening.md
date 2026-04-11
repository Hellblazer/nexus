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
- [x] **CA-2**: A Stop or PreToolUse hook can intercept the close
  sequence and surface divergence-language hits to the user before the
  close completes.
  — **Status**: Verified (2026-04-10) via source search of
  `nx/hooks/hooks.json` and `nx/hooks/scripts/pre_close_verification_hook.sh`.
  Findings: (a) PreToolUse Bash hook already exists
  (`pre_close_verification_hook.sh`, matches commands containing
  `bd close|done`, currently advisory-only); (b) the hook JSON schema
  supports `permissionDecision: deny` with `reason` for hard blocks,
  even though the existing hook only uses `allow`; (c) Stop hook
  exists at `stop_verification_hook.sh` with 180-second timeout —
  suitable for session-close honesty checks; (d) PostToolUse is
  supported but currently has no entries; suitable for divergence-
  language detection on post-mortem Writes. **Design correction**
  (see HA-1 below): the divergence-language check must be PostToolUse
  on Write for post-mortem files, not PreToolUse — PreToolUse fires
  before the Write and would see pre-write file state.
- [x] **CA-3**: A PreToolUse hook on `bd create` can inspect arguments
  and reject creations that lack `reopens_rdr`/`sprint`/`drift_condition`
  when the calling context is an active RDR close — **including when
  `bd create` is invoked inside a dispatched subagent** (not just at
  the top-level close skill).
  — **Status**: Verified (2026-04-10) via behavioral spike. **Method**:
  temporarily modified `pre_close_verification_hook.sh` to log every
  firing to `/tmp/nexus-ca3-spike.log`, dispatched a `general-purpose`
  subagent that ran two Bash `echo` commands, then inspected the log.
  **Findings**:
  1. **The hook fires on subagent Bash calls.** Both `echo` commands
     issued by the dispatched subagent appeared in the log with the
     same `session_id` as the parent conversation. PreToolUse hooks
     are session-wide, NOT isolated per-agent.
  2. **The stdin JSON distinguishes top-level from subagent calls.**
     Subagent calls include two fields the top-level calls do not:
     `"agent_id": "<uuid>"` and `"agent_type": "general-purpose"`.
     This means the hook can positively identify subagent context
     and apply selective enforcement — for example, blocking `bd
     create` from subagents during an active close while allowing
     top-level `bd create` to pass.
  3. **Session ID propagates across agent boundaries.** The shared
     `session_id` in the hook stdin confirms T1 scratch is visible to
     subagent contexts via the existing session propagation in
     `src/nexus/session.py`. The "active-close marker" stored in T1
     scratch will be visible to the hook when a subagent's `bd
     create` triggers it.

  **Contradiction with GitHub #21460**: RDR-035 line 20 cites GitHub
  #21460 ("PreToolUse hooks not enforced on subagent tool calls") in
  its related_issues list. That issue is either out of date, incorrect,
  or applies to a different configuration — the behavioral spike on
  the current Claude Code version demonstrates the opposite. The
  memory entry `pretooluse-hook-is-the-only-reliable-tool-enforcement`
  (2026-03-21) is the authoritative statement and is now confirmed by
  the spike.

  **Implication for Gap 3 design**: the enforcement hook can use the
  `agent_id` field to log which agent created a bead without the
  required metadata, enabling per-agent audit trails. The scoped-
  detection heuristic from HA-3 (bead must mention the RDR ID) is
  also viable now that we know subagent calls are reached. No bypass
  exists.
- [x] **CA-4**: Existing RDRs (rdr-001 through rdr-064) can be
  grandfathered without requiring retroactive Problem Statement
  rewrites. Warning is acceptable; blocking is not.
  — **Status**: Verified with concerning finding (2026-04-10) via
  corpus audit by Explore agent. **Data**: 65 pre-065 RDRs audited.
  45/65 (69%) have `## Problem Statement` sections; **0/65 use the
  `#### Gap N:` format RDR-065 requires**; 42/65 use numbered lists
  (1. 2. 3.) for enumeration; 3/65 are pure prose. This means the
  grandfathering warn-path will fire on 100% of legacy closes. The
  original design (date-based cutoff via `created` frontmatter) is
  replaced with **ID-based cutoff**: RDRs with ID < 065 are legacy
  (warn-only); RDRs with ID ≥ 065 require the anchor format. The
  warn message is reframed from "fix this" to **"This RDR predates
  structured gaps; no action required — gate does not apply."** This
  mutes the noise without losing the audit signal. Retrofit of
  legacy RDRs is out of scope but is a reasonable future enhancement
  (tracked as potential work in RDR-067 audit infrastructure).
- [x] **CA-5**: The divergence-language regex bank has acceptable
  precision against nexus's own post-mortem corpus (original target
  <10% false-positive rate was over-specified — revised to ≥70%
  precision based on CA-5 measurements).
  — **Status**: Verified with precision-driven refinement (2026-04-10)
  via two corpus audits by Explore agents. **Baseline bank**
  `divergence|workaround|deferred|follow-up|limitation|TODO|XXX|not yet|for now|partial|drift`
  measured ~57% precision on a hand-audited sample of 3 high-density
  post-mortems (32 TP / 56 total hits). **Per-pattern precision from
  baseline**: `workaround` 100% (rare), `limitation` 100% (rare),
  `deferred` 83%, `partial` 80%, `divergence` 71%, `follow-up` 50%,
  `drift` 43% (structural section headers dominate), `TODO/XXX/for
  now` near-zero frequency. **Held-out precision measurement** on
  21 post-mortems excluding the 3 already audited: 50% precision
  overall. **Critical finding from held-out**: `partial` measured
  **0% precision** in the new sample (3 FPs: "partial corruption
  handling," "partial pagination results," "partial CCE batch
  failure" — all legitimate robustness/bug descriptions, not scope
  cuts). `partial` is dropped from the bank. `deferred` measured
  100% (2 TPs); `divergence` measured 100% (1 TP); `workaround` and
  `limitation` were absent from the held-out sample. **Coverage**:
  14/24 post-mortems (58%) have any divergence language under the
  baseline bank. **Final refined bank** for implementation:
  `divergence|workaround|limitation|deferred|follow-up\s+RDR|Phase\s+\d+\s+(deferred|required)|out\s+of\s+scope|not\s+in\s+scope`
  (8 patterns, `partial` removed). Pre-filter excludes lines
  starting with `#` (section headers) and lines inside markdown
  tables (surrounded by `|`). Projected precision after refinement:
  ~83% (weighted by baseline + held-out TPs). The hook's role is
  **secondary signal**, not primary gate. When it fires, user sees
  divergence vocabulary and decides. When it doesn't fire, the
  problem-statement replay (Gap 1) is the primary enforcement path.
  **Post-launch refinement**: measure precision on real close-time
  hook data after 5 RDR closes; drop further low-signal patterns
  if needed.
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

- [x] **HA-1**: Hook firing order is deterministic relative to the
  close skill's Write to the RDR/post-mortem file.
  — **Status**: Verified with design correction (2026-04-10) via
  source search of `nx/skills/rdr-close/SKILL.md` and
  `nx/hooks/hooks.json`. **Finding**: the original design placed
  the divergence-language guard as a PreToolUse hook on Write. This
  is wrong — PreToolUse fires BEFORE the tool executes, so the hook
  would see the pre-Write state of the file (empty, or the old
  content) rather than the divergence language the author just
  wrote. **Correct design**: PostToolUse hook matching Write (and
  Edit) for paths under `docs/rdr/post-mortem/`. PostToolUse fires
  AFTER the tool completes successfully, so the hook sees the final
  file content. `hooks.json` currently has no PostToolUse entries;
  a new entry must be added. The matcher: `tool_name == "Write" ||
  tool_name == "Edit"` AND `tool_input.file_path` contains
  `docs/rdr/post-mortem/`. The hook script reads the written file
  from disk, greps the refined regex bank (see CA-5), and emits
  `additionalContext` with each hit + prompts the user for "close
  anyway?" if any matches are found. **Separate concern**: the
  close skill's Step 4.2 ("Update status in RDR markdown metadata")
  writes to the RDR file itself (not the post-mortem). That Write
  is for frontmatter `status:` field only — not a target for
  divergence checking.

- [x] **HA-2**: The close session executes in a single uninterrupted
  execution context where T1 scratch is available from start to finish.
  — **Status**: Partially Verified (2026-04-10) via source search of
  `src/nexus/session.py`. **Within-session continuity is solid**:
  sessions propagate via `CLAUDE_SESSION_FILE` (flat file at
  `~/.config/nexus/current_session` written by SessionStart hook,
  shared by all Bash subprocesses within one Claude Code conversation).
  Legacy getsid-keyed file propagation also exists. T1 active-close
  markers survive across Bash subprocess boundaries within a session.
  **Across-session continuity is NOT preserved**: if the user closes
  Claude Code mid-close and resumes in a new conversation, a new
  SessionStart hook runs, writes a new session ID, and the old T1
  marker is lost. Sessions also age out after 24 hours
  (`_SESSION_MAX_AGE_SECONDS = 24 * 3600.0`). **Accepted limitation**:
  close interruption disables Gap 3 follow-up bead enforcement for
  the remainder of the close. Mitigation: when the close skill
  detects a resumed session (no active-close marker but
  `--reason implemented` provided and RDR status is `accepted`), it
  emits a warn: "This close appears to be resumed from a prior
  session; follow-up bead enforcement is degraded — please manually
  verify any beads created during the close carry the required
  commitment metadata." Not a hard block; a known gap.

- [x] **HA-3**: The close skill's "any `bd create` during close session
  is a follow-up" heuristic does not produce false positives on
  unrelated bead creations.
  — **Status**: Resolved via design decision (2026-04-10), informed by
  the CA-3 spike finding that subagent context is visible in the hook
  stdin. **Decision**: **scoped detection via RDR ID mention**. The
  hook matches `bd create` invocations and checks whether the `--title`
  or `--description` argument mentions the active RDR's ID (e.g.,
  `065`, `RDR-065`, or `rdr-065`). If the RDR ID is NOT mentioned in
  the bead content, the hook treats it as an unrelated bead and passes
  through with an advisory note ("RDR close active — if this bead is a
  follow-up for RDR-NNN, add commitment metadata"). If the RDR ID IS
  mentioned, the hook requires the three commitment fields and blocks
  on absence.
  **Rationale**: option (b) — override flag — was rejected because we
  cannot modify `bd create`'s argument schema (beads is external). A
  wrapper would be fragile. Scoped detection by RDR ID mention is
  lossier in one direction (an agent that forgets to reference the RDR
  in the follow-up's description will bypass the gate) but safer
  overall — false positives train agents to dismiss gates, which is
  worse than missed true positives. The CA-3 `agent_id` audit log
  mitigates the missed-true-positive case: every `bd create` during
  active close is logged with the invoking agent's ID, providing a
  post-hoc review trail.
  **Fallback if scoped detection proves insufficient**: add a
  convention in RDR post-mortems that every follow-up bead is listed
  in a dedicated `## Follow-Up Beads` section; the close skill's
  Step 4 (Update State) validates that every listed bead carries the
  commitment metadata. This moves enforcement from hook-time to
  skill-time for beads that would otherwise bypass scoped detection.
  Deferred to post-launch iteration based on observed behavior.

- [x] **HA-4**: `resources/rdr/TEMPLATE.md` in the working tree is what
  the scaffold reads at `nx:rdr-create` time, not a bundled copy in the
  installed plugin.
  — **Status**: Verified with concerning finding (2026-04-10) via
  source search of `nx/skills/rdr-create/SKILL.md` lines 46–54.
  **Finding**: the scaffold reads the template from
  `$CLAUDE_PLUGIN_ROOT/resources/rdr/TEMPLATE.md` — the **installed
  plugin cache location**, not the working tree. Modifying
  `nx/resources/rdr/TEMPLATE.md` in the working tree has no effect on
  running sessions until the plugin is republished (version bump +
  `scripts/reinstall-tool.sh`). Additionally: the scaffold bootstrap
  is per-repo and ONE-SHOT — on first use, `TEMPLATE.md` is copied
  into `$RDR_DIR/TEMPLATE.md` in the repo. After that, the per-repo
  copy is authoritative, NOT the plugin cache. The nexus repo itself
  has no `docs/rdr/TEMPLATE.md` (confirmed via `ls`) — the scaffold
  must be reading from the plugin cache directly, which means the
  bootstrap was either never run or was run without the template
  copy. Either way, the deployment story for Gap 4 has three steps:
  (1) edit `nx/resources/rdr/TEMPLATE.md` in the working tree, (2)
  release a new plugin version, (3) users install the new version.
  This is a plugin release, not a file edit. **Scope implication**:
  Phase 1 Step 2 (Update RDR template scaffold) must be bundled with
  a plugin version bump, not shipped as a loose file edit. The
  plugin version bump is a coordination point with the release
  process documented in `docs/contributing.md` (see project memory:
  "release discipline"). This RDR cannot close as `implemented`
  until the plugin release has shipped AND the new template has been
  installed locally and verified by creating a test RDR with
  structured gaps.

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
- The divergence regex bank is:
  `divergence|workaround|limitation|deferred|follow-up\s+RDR|Phase\s+\d+\s+(deferred|required)|out\s+of\s+scope|not\s+in\s+scope`
  (8 patterns). This was derived from two rounds of precision
  measurement against the nexus post-mortem corpus (CA-5 baseline +
  held-out). Pre-filter removes lines starting with `#` and lines
  inside markdown tables. `partial` was dropped after the held-out
  measurement showed 0% precision in a new sample (all hits were
  robustness/bug-description contexts, not scope reduction). The bank
  does NOT derive from the 10-category drift taxonomy in the existing
  close skill — those are post-hoc classification labels for
  acknowledged divergences and serve a different purpose.
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

#### Step 2: Update RDR template scaffold (bundled with plugin release per HA-4)

**HA-4 coordination note**: the template is loaded from
`$CLAUDE_PLUGIN_ROOT/resources/rdr/TEMPLATE.md` (installed plugin
cache), not the working tree. A working-tree edit has no effect on
running sessions until the plugin is republished and users install the
new version. This step must be bundled with a plugin version bump per
`docs/contributing.md`.

Concrete actions:

1. Edit `nx/resources/rdr/TEMPLATE.md` (working tree) to add a
   `### Enumerated gaps to close` subsection under `## Problem
   Statement` with example `#### Gap 1:` headings. Document the
   anchor convention explicitly: "The close skill greps for
   `^#### Gap \d+:` headings. Use exactly this format."
2. Update `nx/skills/rdr-create/SKILL.md` to mention the convention
   in the scaffold guidance and the behavior section.
3. Bump the plugin version per the release checklist in
   `docs/contributing.md`: `pyproject.toml`, `uv.lock`,
   `CHANGELOG.md`, `nx/CHANGELOG.md`, `.claude-plugin/marketplace.json`.
4. Run `scripts/reinstall-tool.sh` to install the new plugin locally.
5. Verify by creating a test RDR via `/nx:rdr-create` and confirming
   the new scaffold includes the `### Enumerated gaps to close`
   subsection.

**This step is the gating prerequisite for Phase 1 Step 6c (real
self-close).** Without the new template deployed locally, the close
skill's replay cannot find gaps in newly-authored RDRs, which means
CA-6's Step 6c cannot exercise the full close path.

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

#### Step 4: Ship divergence-language honesty hook (PostToolUse on Write)

**Design correction** (from CA verification, HA-1): the hook is
**PostToolUse**, not PreToolUse, and it matches the **post-mortem
file path**, not the RDR file. PreToolUse fires before the tool
executes and would see the pre-write state. PostToolUse fires after
the write completes and can read the final file content.

**Hook script**: create `nx/hooks/scripts/divergence-language-guard.sh`.

**Matcher** (registered in `nx/hooks/hooks.json` under `PostToolUse`):
```json
{
  "matcher": "Write|Edit",
  "hooks": [
    {"type": "command",
     "command": "bash $CLAUDE_PLUGIN_ROOT/hooks/scripts/divergence-language-guard.sh",
     "timeout": 10}
  ]
}
```

**Hook script logic**:

1. Read stdin (the hook receives JSON with `tool_name`, `tool_input`).
2. Fast no-op: if `tool_name` is not `Write` or `Edit`, emit allow
   and exit.
3. Fast no-op: if `tool_input.file_path` does not contain
   `docs/rdr/post-mortem/`, emit allow and exit.
4. Read the file from disk (it has just been written).
5. Apply the **refined regex bank** (from CA-5 verification, 8
   patterns after Rev 4 dropped `partial`):
   - Patterns: `divergence|workaround|limitation|deferred|follow-up\s+RDR|Phase\s+\d+\s+(deferred|required)|out\s+of\s+scope|not\s+in\s+scope`
   - Pre-filter: exclude lines starting with `#` (section headers)
     and lines inside markdown tables (surrounded by `|`).
6. If hits are found: emit `additionalContext` with the matched lines
   quoted, and a framing message: "This post-mortem contains
   divergence-language hits. These may indicate acknowledged scope
   deferral (intended) or silent scope reduction (unintended). Review
   each hit and decide: is this a real divergence that should force
   `close_reason: partial`, or a legitimate acknowledged deferral?"
7. The hook emits `additionalContext` with `permissionDecision: allow`
   — it does NOT hard-block. The hard-block path for `close_reason:
   implemented` refusal lives in the command preamble (Step 3). The
   divergence hook is a **secondary signal** (from CA-5: only 58% of
   post-mortems have any divergence vocabulary, so the hook is a
   partial coverage tool by design).

**Why PostToolUse, not Stop**: Stop fires at end-of-session, which
may be after the user has already moved on. PostToolUse on Write
fires immediately when the post-mortem is created, which is the
correct decision point.

**Rationale** for PostToolUse rather than integrating into the
command preamble: the preamble runs at `/nx:rdr-close` invocation,
BEFORE the skill body has created the post-mortem. It cannot grep a
file that doesn't yet exist. Post-mortem creation happens during the
skill body's Step 2. The PostToolUse Write hook fires at exactly
that moment — no coordination needed between preamble and skill.

#### Step 5: Ship follow-up bead enforcement (extend existing PreToolUse hook)

**Design refinement** (from CA-2 verification): rather than creating
a new hook script, **extend the existing**
`nx/hooks/scripts/pre_close_verification_hook.sh`. That script
already:
- Matches PreToolUse Bash calls
- Parses `bd close|done` commands via regex
- Checks T1 scratch for `review-completed` markers
- Emits `allow` with `additionalContext`

The extension adds:
- New regex: match `bd create` commands alongside `bd close|done`
- New T1 scratch check: look for `rdr-close-active` marker (set by
  the `/nx:rdr-close` command preamble at Pass 2 validation success)
- If `bd create` detected AND `rdr-close-active` marker exists AND
  the `bd create` command lacks `--reopens-rdr`, `--sprint` or
  `--due`, and `--drift-condition` fields: emit
  `permissionDecision: deny` with a structured reason message
- If any of those conditions is absent, fall through to existing
  behavior (allow)

**T1 scratch active-close marker**: set by the `/nx:rdr-close` command
preamble at the end of Pass 2 (after pointer validation passes, before
skill body runs). Marker content: `rdr-close-active` tag with RDR ID.
Cleared by the close skill's Step 4 (Update State) after the T2 record
update completes.

**HA-3 resolved**: scoped detection via RDR ID mention in the bead's
`--title` or `--description`. The hook parses the `bd create`
command text with a regex for the active RDR's ID. If the ID is
mentioned and the scratch marker exists, the hook requires the three
commitment fields (`reopens_rdr`, `sprint` or `due`, `drift_condition`
— passed as free-form text in the bead's description since `bd` does
not support these as first-class arguments). If the RDR ID is not
mentioned, the hook passes through with an advisory note suggesting
the author add the metadata if this bead is actually a follow-up.

**CA-3 verified**: PreToolUse Bash hooks fire on subagent-dispatched
tool calls. The behavioral spike (2026-04-10) confirmed this
unambiguously — the hook stdin even distinguishes top-level from
subagent context via `agent_id` and `agent_type` fields. No
subagent bypass exists. The existing `pre_close_verification_hook.sh`
is the correct extension point. Gap 3 enforcement will:

1. Extend the existing hook's regex bank from `bd\s+(close|done)` to
   `bd\s+(close|done|create)`.
2. On `bd create` match: read the `tool_input.command` string, grep
   for the active RDR's ID pattern (from the scratch marker), and
   check for the three commitment markers in the description field.
3. If the RDR ID is present and commitment markers are absent: emit
   `permissionDecision: deny` with a structured reason listing the
   missing fields. The agent is prompted to re-issue with the
   metadata.
4. If the RDR ID is absent: emit `permissionDecision: allow` with
   `additionalContext` reminding the author about the convention
   (advisory, not blocking).
5. **Audit logging**: regardless of decision, append a line to the
   session's T2 or scratch log recording the `agent_id`, `agent_type`,
   and bead content for post-close review.

**Beads wrapper**: because `bd create` is an external tool, the hook
is the only enforcement surface. No wrapper script is needed; the
hook inspects the `bd create` command text and decides.

**Scratch marker lifecycle**: the `/nx:rdr-close` command preamble
sets a scratch entry tagged `rdr-close-active` with the RDR ID as the
content. The marker is set at the end of Pass 2 (pointer validation
success) and cleared by the close skill's Step 4 (Update State) after
the T2 record update completes. If the close is interrupted (session
closed mid-close), the marker persists until the scratch TTL expires
or the next session's scratch cleanup runs — this is the HA-2
limitation, and a warn is emitted on close-skill resume if an orphan
marker is detected.

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

- 2026-04-10 — **CA verification batch (Revision 3).** Source searches
  and corpus audit completed; multiple CAs and Hidden Assumptions
  transitioned from Unverified to Verified or Partially Verified.
  Several design corrections required as a result.

  **CA-2 verified**: `nx/hooks/hooks.json` audited;
  `pre_close_verification_hook.sh` exists as a PreToolUse Bash hook
  with a proven pattern for matching `bd close|done` commands. JSON
  schema supports `permissionDecision: deny` for hard blocks. Stop
  hook, SubagentStart hook, and PostToolUse slot all present. The
  extension path for Step 5 (follow-up bead enforcement) is now
  concrete: extend the existing hook rather than creating a new one.

  **CA-4 verified with concerning finding**: corpus audit of 65
  pre-065 RDRs by Explore agent. 0/65 use `#### Gap N:` format. 42
  use numbered lists, 3 pure prose, 20 have no Problem Statement
  section at all. Grandfathering design refined from date-based to
  **ID-based cutoff** (pre-065 = legacy warn-only) with neutral warn
  framing. 100% of legacy closes hit the warn path, but the refined
  message ("legacy RDR, no action required") mutes the noise.

  **CA-5 partially verified**: baseline regex bank measured at ~57%
  precision on a hand-audited sample. Per-pattern analysis showed
  `drift` (43%), `follow-up` (50%), `TODO/XXX/for now` (near-zero
  frequency) are low-signal and should be dropped. `workaround`
  (100%), `limitation` (100%), `deferred` (83%), `partial` (80%),
  `divergence` (71%) are high-signal and retained. Refined bank
  adds `Phase\s+\d+\s+(deferred|required)`, `follow-up\s+RDR`,
  `out\s+of\s+scope`, `not\s+in\s+scope`. Pre-filter excludes lines
  starting with `#` and markdown table rows. Coverage: 58% of
  post-mortems have any divergence language — the hook is a
  **secondary signal** by design, not primary gate. Held-out
  precision measurement remains for Phase 1 Step 1.

  **HA-1 verified with design correction**: original design placed
  the divergence-language hook as PreToolUse on Write, which sees
  pre-Write state and misses the content. Corrected to **PostToolUse
  on Write|Edit** matching `docs/rdr/post-mortem/` paths. Step 4 of
  Implementation Plan rewritten to reflect this.

  **HA-2 partially verified**: within-session T1 continuity is solid
  (flat file + PPID chain via `src/nexus/session.py`). Across-session
  resumption loses T1 state due to new SessionStart hook and 24-hour
  session aging. Accepted limitation: interrupted closes degrade Gap
  3 follow-up bead enforcement; a warn is emitted when a resumed
  close is detected.

  **HA-4 verified with concerning finding**: template loaded from
  `$CLAUDE_PLUGIN_ROOT/resources/rdr/TEMPLATE.md` (installed plugin
  cache). Working-tree edits require plugin version bump +
  `scripts/reinstall-tool.sh`. Nexus repo does not have a
  `docs/rdr/TEMPLATE.md` bootstrap copy — the skill reads from the
  plugin cache directly. Phase 1 Step 2 rewritten to require a
  plugin release, not a loose file edit. This also means RDR-065
  cannot close as `implemented` until the plugin has shipped and
  the new template is installed and verified locally.

  **CA-3 likely verified (spike still recommended)**: hooks.json
  structure suggests PreToolUse hooks fire session-wide regardless of
  which agent issued the tool call (SubagentStart is a separate
  event). Full two-scenario spike (top-level + subagent-dispatched
  `bd create`) remains for Phase 1 as a precaution. Fallback if
  subagent hooks don't propagate: document the bypass as a known
  limitation.

  **HA-3 open design decision**: follow-up bead detection heuristic
  — decision deferred to implementation. Start with scoped
  enforcement (beads mentioning the closing RDR's ID); add an
  `--unrelated-to-close` override flag only if false positives are
  reported.

  **CAs still fully unverified after Rev 3**: CA-1 (grandfathering
  corpus audit — now partially verified via CA-4 by proxy); HA-5 was
  downgraded from "Verified" to "Partially Verified" in Rev 2.

  **Remaining work before finalization gate (round 2)**:
  1. Full CA-3 subagent spike (~30 minutes: create a test scenario)
  2. Held-out CA-5 precision measurement on rdr-001 through rdr-039
     post-mortems (~1 hour: run refined bank against corpus)
  3. Design pass on the `bd create` wrapper with `--reopens-rdr`,
     `--sprint`, `--drift-condition` flag syntax (~30 minutes)
  4. Re-dispatch substantive-critic for gate round 2 (post-CA-verify)

  Revision 3 does not change the 3-gap + 1-prerequisite structure.
  It hardens the CA claims and corrects three implementation details
  (divergence hook is PostToolUse not PreToolUse; bead enforcement
  extends existing hook rather than creating new one; template
  change is a plugin release not a file edit).

- 2026-04-10 — **Revision 4: CA-3 behavioral spike + CA-5 refinement
  + HA-3 design decision.** Three remaining CAs resolved, leaving only
  CA-6 (recursive self-validation, requires implementation) unverified.

  **CA-3 VERIFIED via behavioral spike.** Initial research via
  claude-code-guide agent returned an incorrect answer citing GitHub
  #21460 ("PreToolUse hooks not enforced on subagent tool calls").
  That citation turned out to be from RDR-035's `related_issues`
  reference list, not a validated conclusion. Session memory from
  2026-03-21 (`pretooluse-hook-is-the-only-reliable-tool-enforcement`)
  and RDR-045 line 32 both stated PreToolUse hooks fire on every
  tool call. To resolve the conflict, I ran a behavioral spike:
  temporarily modified `pre_close_verification_hook.sh` in the
  installed plugin cache to log every firing, dispatched a
  `general-purpose` subagent that ran two Bash `echo` commands, then
  inspected `/tmp/nexus-ca3-spike.log`. The log recorded 5 firings
  including both subagent calls, with `agent_id` and `agent_type`
  fields present in the subagent stdin. This is a better outcome
  than expected: the hook can positively distinguish subagent from
  top-level context, enabling selective enforcement. All spike
  changes reverted after the test (cache restored from backup,
  working tree restored via `git restore`, log file deleted).
  **Credit where due**: the claude-code-guide agent was incorrect,
  but it cited the relevant RDR, which let me find the contradiction
  and resolve it empirically. The lesson: citations to related_issues
  fields are references, not findings.

  **CA-5 refined via held-out measurement.** An Explore agent ran the
  baseline bank against 21 post-mortems (held out from the earlier
  3-post-mortem sample) and measured precision on a new 5-post-mortem
  sample. **Critical finding**: `partial` measured 0% precision in
  the held-out sample (3 FPs: "partial corruption handling,"
  "partial pagination results," "partial CCE batch failure" — all
  robustness/bug descriptions, not scope cuts). `partial` is
  **dropped** from the bank. Final refined bank is 8 patterns:
  `divergence|workaround|limitation|deferred|follow-up\s+RDR|Phase\s+\d+\s+(deferred|required)|out\s+of\s+scope|not\s+in\s+scope`
  with a pre-filter for section headers and markdown tables.
  Projected precision ~83% weighted by baseline + held-out TPs.
  Post-launch refinement planned.

  **HA-3 resolved** via design decision informed by the CA-3 spike
  finding. The decision: **scoped detection via RDR ID mention** in
  `bd create --title` or `--description`. Rejected alternative was
  override flag, which required a bd wrapper we cannot build. The
  `agent_id` field from the CA-3 spike enables per-agent audit
  logging even when the scoped detection misses a follow-up, so
  false negatives are recoverable via post-close review.

  **Implementation Plan Step 5 updated** to reflect the final
  design: extend the existing `pre_close_verification_hook.sh`
  regex to match `bd\s+(close|done|create)`; on `bd create` match,
  grep the command for the active RDR's ID, require commitment
  fields, audit-log every firing with `agent_id`/`agent_type`.
  Scratch marker lifecycle also documented.

  **State after Revision 4**: 10 of 11 assumptions verified or
  resolved. Only CA-6 remains (recursive self-validation — requires
  actual implementation to test). The RDR is ready for substantive-
  critic gate round 2 and then the real finalization gate.
