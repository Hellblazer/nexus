---
title: "nx Workflow Integration: Protocol Standardization and Knowledge Accumulation"
id: RDR-008
type: architecture
status: closed
priority: medium
author: Hal Hildebrand
reviewed-by: self
created: 2026-02-28
accepted_date: 2026-03-01
closed_date: 2026-03-01
close_reason: implemented
related_issues:
  - RDR-007
  - RDR-009
---

## RDR-008: nx Workflow Integration: Protocol Standardization and Knowledge Accumulation

## Summary

All nx plugin agents were written before nx reached its current form. The result is
uneven integration: a few agents use nx deeply and correctly; others treat it as a
fallback; the nx wrapper skill for brainstorming (`nx:brainstorming-gate`) mentions
nx prior-art search but leaves it too vague to be reliably executed. The `--hybrid`
search flag — which merges semantic, ripgrep, and frecency results — is unused outside
`codebase-deep-analyzer`. Knowledge written to T3 by one agent is rarely recovered by the next
agent that could benefit from it.

This RDR proposes targeted, proportional updates across two tracks: (B) protocol
standardization — ensuring the agents that should proactively search nx actually do,
and the most useful flags are documented and used; and (C) knowledge accumulation —
ensuring findings written to T3 by one agent compound into future work rather than
sitting inert.

**Scope constraint**: Changes are limited to files in the local nexus repo
(`nx/agents/`, `nx/agents/_shared/`, `nx/skills/`). The superpowers plugin
(`claude-plugins-official`) source files are not modified. Superpowers skills
(`brainstorming`, `systematic-debugging`) are invoked exactly as they are.
nx integration for these workflows is delivered via the nx wrapper skills
(`nx:brainstorming-gate`) that already exist in this repo, not via changes
to superpowers itself.

Additionally, every proposed change must pass a clear value test: does this specific
addition meaningfully improve the quality or continuity of agent output? If the
honest answer is "probably not," the change is omitted.

## Motivation

The value proposition of nx is compounding: a debugging insight stored to T3 today
should help the next debugging session. An architectural decision stored after a
planning session should inform the next architect agent. Currently, compounding is
unreliable because:

1. Agents that should search before starting often don't. `java-developer` is
   "relay-reliant" — if the relay was hand-crafted by the user rather than assembled
   by `strategic-planner`, all nx context is lost before the agent even starts.

2. Agents that do search use only basic forms. `nx search --corpus knowledge` is used,
   but `--hybrid` (which merges semantic, ripgrep, and frecency results) is used only
   by `codebase-deep-analyzer`. The `--answer` and `--agentic` flags that existed in
   earlier versions are being removed by RDR-009 (pre-1.0 cleanup — both required an
   unconfigured Anthropic API key and are vestigial in an agent-first tool). The
   agent's own reasoning already handles query reformulation via multi-query patterns
   (see B3).

3. The `nx:brainstorming-gate` skill — the required entry point before any
   implementation work — mentions "nx search for prior art" in its checklist but
   gives no concrete query. Agents treat it as optional context and skip it.
   Brainstorming without searching prior decisions risks re-litigating settled
   questions that are already in T3.

4. Findings are stored inconsistently. `code-review-expert` documents pattern
   violations but stores them only as a secondary step, conditionally. `deep-analyst`
   stores findings via generic unstructured echo — retrieved findings have no
   guaranteed sections for conclusion, evidence, or confidence level.

The counter-risk — over-indexing on nx — is real. Forcing every agent to perform
nx queries on every task adds latency and noise without proportional benefit. This
RDR explicitly scopes out changes that don't pass the value test.

## Evidence Base

### T1 Scratch Assessment

Every agent file includes T1 scratch in two places: the RECOVER protocol checks
`nx scratch search "[topic]"` for in-session notes, and the PRODUCE section
instructs agents to write working notes to scratch and promote to T2 when
validated. On inspection, most of this is boilerplate that does not work as
described in practice.

**The session-scope problem**: Scratch is scoped to the current session. When an
agent is spawned via the Agent tool, it runs as a new subagent with its own fresh
session — scratch entries from the parent session are not visible. The RECOVER
check (`nx scratch search`) is therefore a no-op for virtually every real agent
invocation. It will never find anything because there is nothing to find.

**The staging-area problem**: Several agents (java-developer, code-review-expert)
instruct: write to scratch, then promote to T2 if validated. This adds a step
without adding value. If the content is worth keeping, write to T2 directly. If
it is not worth keeping, don't write at all. The intermediate scratch hop is
friction, not discipline.

**Where scratch is genuinely useful**: `codebase-deep-analyzer` has the only
pattern that actually works — parallel subtask coordination within a single
session. When subtasks are spawned within the same session scope, they share
scratch and can pass findings to the orchestrating agent for synthesis. This is
a real use case. It is also narrow: it requires the orchestrating agent to remain
in the same session while subtasks complete, which is how codebase-deep-analyzer
is designed to operate.

**Decision**: This RDR does not add scratch usage to any agent. The RECOVER
protocol's scratch check is retained as a no-op-safe step (it costs little and
would work correctly in the rare case of same-session continuation), but no new
scratch-based workflows are added. The existing codebase-deep-analyzer pattern is
kept. All other scratch guidance in agent PRODUCE sections should be evaluated for
removal in a future cleanup pass — they are documentation debt, not working
features.

### Audit Methodology

All 15 agent files in `nx/agents/` and `nx/agents/_shared/CONTEXT_PROTOCOL.md` were
read and classified against two criteria:

- Does the agent proactively search nx before starting work?
- Does the agent store findings to T3 consistently?

Additionally, `nx search --help` was reviewed to enumerate capabilities not present
in any agent file.

### Current Integration Map

| Agent | Proactive nx search | T3 storage | Notes |
| --- | --- | --- | --- |
| codebase-deep-analyzer | Yes — Phase 0 indexes repo, uses hybrid search throughout | Yes — architecture maps, patterns | **Baseline. No changes needed.** |
| deep-research-synthesizer | Yes — T3, T2, web, code in order | Yes — mandatory relay to knowledge-tidier | **Baseline. No changes needed.** |
| plan-auditor | Yes — searches T3 for prior art, uses code search to verify references | Yes — validation results to T3 | Code reference validation uses unreliable code corpus; Grep is the correct primary path (B2). |
| strategic-planner | Yes — prior art, patterns, similar features | Yes — PM infrastructure, beads | No structural changes needed. |
| code-review-expert | Partial — nx pattern discovery documented but not Step 0 in review workflow | Conditional — only if pattern violation is "recurring" | Needs promotion to primary pre-review step (B4); storage condition needs fix (C5). |
| deep-analyst | Fallback only — RECOVER protocol | Yes — generic `echo` to `analysis-{topic}-{date}`; unstructured | Needs proactive search at Phase 3 start; structured storage format would improve retrieved findings. |
| java-developer | Fallback only — RECOVER protocol | Yes — `insight-developer-{topic}`, conditional on "architecturally significant" qualifier; vague | Storage exists but qualification language is too vague; no proactive prior-art search. |
| java-debugger | Fallback only — RECOVER protocol | Yes — `debug-finding-{issue}` (flat format); `pattern-prevention-{topic}` | Storage exists but flat format loses structure; no mandatory prior-traces search in RECEIVE. |
| knowledge-tidier | Yes — this is its job | Yes — consolidation | No changes needed. |
| java-architect-planner | Yes — searches T3 for architectural patterns | Yes — architecture decisions | No changes needed. |
| orchestrator | N/A | N/A | No changes needed. |
| substantive-critic | Fallback only | None | Low value-add to critique quality. No changes. |
| pdf-chromadb-processor | N/A — PDF ingestion pipeline only | N/A | No changes needed. |
| project-management-setup | N/A | N/A | No changes needed. |
| test-validator | Fallback only | None | Test validation is code-local; semantic search adds minimal signal. No changes. |

### Unused Capabilities

`nx search --help` documents the following flags not used in any agent or skill file:

| Flag | Effect | Value if used | Requires |
| --- | --- | --- | --- |
| `--hybrid` (with `--corpus code`) | Merges semantic + ripgrep + frecency | More reliable code navigation; only `codebase-deep-analyzer` uses this | None |
| `--max-file-chunks N` (code corpora only) | Excludes chunks from files with more than N chunks | Direct mitigation for RDR-007 Finding 3 large-file dominance; usable today without re-indexing | None |
| `--corpus docs` | Search documentation corpus specifically | Useful for documentation-heavy projects; not in any agent guidance | None |

**Note**: `--answer` and `--agentic` are not listed here — they are being removed from
`nx search` by RDR-009 (pre-1.0 cleanup). Both required an Anthropic API key not
configured in nx and are vestigial in an agent-first context where the calling model
already provides reasoning capability.

**Note on `--hybrid`**: Only meaningful with `--corpus code`.

### Value Assessment per Change

Not all gaps are equally worth closing:

- **High value**: Fixing `plan-auditor` code reference validation to use Grep — the
  agent currently uses unreliable code corpus search; Grep is faster and accurate
  today. `--hybrid` with `--max-file-chunks` is the right semantic fallback after
  RDR-006 re-indexing.
- **High value**: Adding T3 storage to `java-debugger` — debugging root causes are
  among the most reusable knowledge artifacts; they cost significant time to derive
  and transfer directly across sessions.
- **Medium value**: Proactive prior-art search in `java-developer` — useful for
  non-trivial implementations; wasteful for small tasks.
- **Low value**: Adding nx to `test-validator` — test validation is inherently
  code-local; semantic search adds minimal signal.
- **Not worth doing**: Adding nx to every skill's checklist steps — this would make
  skills verbose and slow without proportional benefit.

## Proposed Solution

### Design Principles

1. **Surgical, not sweeping.** Change only agents where a specific gap has clear
   evidence and a specific fix with a clear value case.
2. **Proportional.** `--hybrid` is high-leverage for code corpus searches — add it
   where cross-file pattern discovery matters. Multi-query patterns (two searches,
   primary + alternate framing) replace the removed `--agentic` flag using the
   agent's own reasoning at zero additional API cost.
3. **Preserve relay architecture.** The proactive-search / relay-reliant split in
   CONTEXT_PROTOCOL.md is sound. Don't convert relay-reliant agents to proactive
   searchers wholesale — only add targeted proactive search where the relay is
   frequently incomplete (java-developer) or where the task type reliably benefits
   from prior knowledge (java-debugger, deep-analyst).
4. **Accumulation over access.** The higher leverage is ensuring knowledge gets
   stored reliably than ensuring it gets retrieved. Storage is one-time; retrieval
   opportunities are unlimited.

### Track B: Protocol Standardization

#### B1: CONTEXT_PROTOCOL.md — Add advanced search examples and fix stale naming

Two changes to `_shared/CONTEXT_PROTOCOL.md`:

**Change 1**: Add a "Choosing Search Options" section:

```markdown
## Choosing Search Options

Use the right search form for the task:

| Goal | Command |
|---|---|
| Find related prior knowledge | `nx search "topic" --corpus knowledge --n 5` |
| Research with uncertain vocabulary | Run 2 searches: primary term, then alternate framing |
| Conceptual code search (unfamiliar codebase) | `nx search "concept" --corpus code --hybrid --n 15` |
| Documentation search | `nx search "topic" --corpus docs --n 10` |
| Exact code navigation | Use Grep tool instead — faster and more precise |
| Cross-corpus research | Repeat `--corpus` flag (e.g., `--corpus knowledge --corpus docs`) |

**When NOT to use nx search:**
- When the relay already contains the information needed
- For simple, bounded tasks where prior knowledge is unlikely to change the approach
- When grep or file reads are faster and more precise (class/function lookups)
```

**Change 2**: Fix the stale namespace documentation. The file currently states that
all memory uses "bare `{repo}` naming" with no `_pm` suffix. This contradicts the
actual conventions used by the hooks and confirmed in `reference.md` since RDR-007
Fix 3. Update the namespace paragraph to document the three real namespaces:
`{repo}` (agent working notes), `{repo}_rdr` (RDR records), `{repo}_pm` (PM
infrastructure). This correction should land in the same commit as Change 1 to
avoid leaving a correct search table alongside stale namespace guidance.

#### B2: `plan-auditor` — Use Grep-primary path for code reference validation

The agent's code reference validation section currently uses plain `nx search` against
the code corpus. Per RDR-007 Finding 3, the code corpus with default chunk sizes
returns 0/7 canonical results — code existence checks against it produce unreliable
results.

Change the code reference validation to use Grep as primary, with conditional semantic
fallback after RDR-006 re-indexing:

```markdown
### Code Reference Validation

Use Grep for existence checks — faster and accurate regardless of index state:

  grep -r "EntityManager" --include="*.java" src/

For conceptual cross-file pattern questions where grep is insufficient, and only after
RDR-006 re-indexing with small chunks:

  nx search "EntityManager usage patterns" --corpus code --hybrid --max-file-chunks 20 --n 5

The --max-file-chunks 20 flag prevents large-file chunk dominance. Use Grep as the
primary path; semantic search as a supplement for conceptual queries only.
```

#### B3: `deep-research-synthesizer` — Multi-query pattern for Phase 2 gathering

Research tasks often start with imprecise vocabulary. The stored document may use
different terminology than the initial question. The `--agentic` flag (now removed
by RDR-009) would have addressed this via Haiku-based query refinement, but the same
effect is achievable by the agent itself: Claude Sonnet (the agent in session)
reformulates queries better than Haiku and is already in context at zero additional
API cost.

In Phase 2 (Information Gathering), replace single-query searches on conceptual
topics with a two-query pattern:

```markdown
For conceptual topics where initial vocabulary may not match stored documents:

nx search "{primary term or framing}" --corpus knowledge --n 5
nx search "{alternate term or related concept}" --corpus knowledge --n 5

Use both result sets before concluding no prior knowledge exists.
```

Limit this two-query pattern to Phase 2 initial gathering. Once vocabulary is known
from first results, subsequent targeted queries do not need the alternate formulation.

#### B4: `code-review-expert` — Promote pattern baseline to Step 0 with correct tool priority

The current agent has a "Code Pattern Discovery with Nexus" section that is not
ordered as Step 0 in the primary review workflow. The fix is to rename and promote
this existing section — not add a new block alongside it.

**Implementation note**: Rename the existing "Code Pattern Discovery with Nexus"
section to "Step 0: Pattern Baseline (required before reading code)" and move it
to the top of the review workflow. Remove the existing "Integration with Review
Process" numbered list that duplicates the steps. This prevents two overlapping
"before reading code" sections.

The step content should use the correct tool priority per RDR-007 Finding 3.
Code collections indexed with default chunk sizes are currently unreliable for
pattern discovery (0/7 canonical results). Grep is the primary reliable path
until RDR-006 re-indexing:

```markdown
### Step 0: Pattern Baseline (required before reading code)

Use Grep to establish known patterns in the codebase:

- Error handling conventions: search for existing try/catch or error return patterns
- Naming conventions: search for class/method naming in the same package
- Style patterns: search for analogous implementations of the feature being reviewed

If the project's code collection has been re-indexed with small chunks (RDR-006),
supplement with semantic search for conceptual patterns:

  nx search "error handling patterns in this module" --corpus code --hybrid --max-file-chunks 20 --n 10

The --max-file-chunks 20 flag prevents large-file chunk dominance in results.
Use Grep as the primary path; nx search as a supplement for conceptual queries
when you need cross-file pattern discovery that grep cannot express.
```

#### B5: `deep-analyst` — Add targeted nx evidence gathering in Phase 3

The agent's analytical process doesn't mention nx as a source of evidence. For
multi-session investigations, prior analysis stored in T3 is highly relevant.

Add to Phase 3 (Deep Dive with Hypothesis Testing):

```markdown
### Prior Evidence Check (required at Phase 3 start, before gathering new evidence)

nx search "{component} analysis findings" --corpus knowledge --n 5
nx search "{error type or symptom}" --corpus knowledge --n 5

Prior root-cause analyses often contain evidence that would take significant time
to rediscover. Incorporate or refute, don't ignore.
```

This search is unconditional — the agent cannot evaluate "have I investigated this
before?" without searching, so the condition is unevaluable and will be skipped
incorrectly. When T3 is empty the cost is 2 cheap searches; when T3 has relevant
content, the entire investigation is shorter.

This is scoped to Phase 3 start only — not the whole agent. The hypothesis-testing
loop should not nx-search at every step.

### Track C: Knowledge Accumulation

#### C1: `java-developer` — Add proactive prior-implementation search (conditional)

The agent is correctly relay-reliant for its primary workflow. However, when the
relay arrives from a human (not `strategic-planner`), no prior-art context is
present.

Add to the RECEIVE section, as a conditional step:

```markdown
### Prior Implementation Search (if relay has no nx artifacts)

If the relay's Input Artifacts section contains no nx store titles and no nx memory
paths — i.e., no prior knowledge has been assembled — search before starting:

nx search "similar implementation patterns for {feature}" --corpus knowledge --n 5
nx search "{key class or interface}" --corpus code --hybrid --n 10

Skip this if the relay already includes nx store or nx memory artifacts. The relay
is the primary source of context; this is a fallback for when none was assembled.
```

The condition "no nx store titles and no nx memory paths in Input Artifacts" is
directly observable in the relay — no authorship inference required. This should
not become a mandatory step that runs before every implementation task.

#### C2: `java-developer` — Strengthen existing T3 storage qualifier with concrete examples

The agent already stores to T3 using `insight-developer-{topic}` with the qualifier
"architecturally significant." This qualifier is too vague — it does not help the
agent distinguish what should and should not be stored, leading to either over-storage
(noise) or under-storage (missed accumulation).

Replace the existing vague qualifier with concrete examples in the PRODUCE section:

```markdown
- **Implementation Discoveries**: Store non-obvious findings that future implementers
  would need to know and could not easily rediscover:
  `echo "..." | nx store put - --collection knowledge --title "insight-developer-{topic}" --tags "insight,java"`

  Store when: module initialization order has a non-obvious constraint; an API
  behaves differently than its documentation suggests; a pattern that appears
  reusable is actually tied to a specific context.
  Do not store: routine implementation steps, things directly readable from code,
  standard library behavior.
```

The title format `insight-developer-{topic}` is unchanged — this is a precision
enhancement to existing language, not a new storage directive.

#### C3: `java-debugger` — Upgrade storage format and make RECEIVE search mandatory

The agent already stores root cause findings using `debug-finding-{issue}` (flat
format) and prevention patterns using `pattern-prevention-{topic}`. The existing
storage directive is present. Two gaps remain:

**Gap 1 — Flat storage format loses structure.** The existing `echo "..." | nx store
put` pattern stores unstructured text. A future search retrieves an opaque blob with
no guaranteed sections for root cause, evidence, or fix. Replace the flat format with
a structured template in the existing PRODUCE directive:

```markdown
- **Root Cause Analysis**: After confirming root cause, replace the existing flat
  storage with structured format:
  `printf "# Debug: {symptom}\n## Root Cause\n{finding}\n## Evidence\n{key evidence}\n## Fix\n{fix applied}\n" | nx store put - --collection knowledge --title "debug-finding-{component}-{symptom}" --tags "debug,rootcause"`

  This replaces the existing `debug-finding-{issue}` format. The structured sections
  make retrieved findings immediately actionable without further parsing.
```

**Gap 2 — Prior-traces search is an example, not a required RECEIVE step.** The
agent currently includes a search example (`nx search "past issues with database
connection timeouts" --corpus knowledge --n 10`) in the Documentation Strategy
section. This should be a named, required step in RECEIVE before hypothesis
generation begins:

```markdown
### Prior Debug Traces Search (RECEIVE — before hypothesis generation)

nx search "{error message or symptom}" --corpus knowledge --n 5
nx search "{component or class} failures" --corpus knowledge --n 5

A prior root cause analysis for this failure class may immediately narrow the
hypothesis space. Incorporate confirmed prior findings into Thought 1.
```

#### C5: `code-review-expert` — Promote pattern violation storage from conditional to default

The Motivation identifies two deficiencies in code-review-expert: (a) pattern discovery
not at Step 0, and (b) T3 storage is conditional and secondary. B4 addresses (a).
This item addresses (b).

The current PRODUCE section stores pattern violations only when the violation is
"recurring" — a qualifier that requires the agent to already know violation history,
which it cannot know without T3 search. This creates a catch-22: storage is conditional
on recurrence, but recurrence can only be detected if prior violations were stored.

Replace the "recurring" condition with a default-store behavior bounded by a
significance threshold:

```markdown
- **Pattern Violations Found**: When a code review identifies a violation of established
  patterns (naming, error handling, structural conventions), store it to T3:
  `printf "# Review: Pattern Violation\n## Pattern\n{pattern name}\n## Violation\n{what was found}\n## File\n{path}\n## Recommendation\n{fix}\n" | nx store put - --collection knowledge --title "review-pattern-{pattern-name}-{date}" --tags "review,pattern,violation"`

  Store when: a pattern is violated across multiple locations in the reviewed code;
  a violation suggests the pattern itself may need documentation; the violation is
  non-obvious (not a typo).
  Do not store: single-instance style nits, formatting errors, trivial cases.
```

The threshold ("across multiple locations" or "non-obvious") replaces the
undetectable "recurring" condition and avoids flooding T3 with every style comment.

#### C4: `deep-analyst` — Upgrade T3 storage to structured format

The agent already stores findings to T3 using `analysis-{topic}-{date}` via generic
`echo`. Analysis of complex system behavior is expensive to reproduce; findings should
accumulate in a form that is immediately actionable when retrieved.

The unstructured echo format returns an opaque blob with no guaranteed sections for
conclusion, evidence, or confidence level. Replace with a structured template in the
existing PRODUCE directive:

```markdown
- **Significant Analysis Findings**: Store confirmed analytical conclusions to T3:
  `printf "# Analysis: {component}/{question}\n## Finding\n{conclusion}\n## Evidence\n{key evidence}\n" | nx store put - --collection knowledge --title "analysis-deep-{component}-{date}" --tags "analysis,deep-analyst"`

  Only store findings you are confident in, not working hypotheses. Storing a
  hypothesis that turns out to be wrong creates noise in future retrievals.
```

### Track B+C: nx Wrapper Skills for Superpowers Workflows

Superpowers skills are invoked unchanged. nx context is gathered by the nx wrapper
skills that gate them — specifically `nx:brainstorming-gate`, which is already
the required entry point before any implementation work.

#### B6: `nx:brainstorming-gate` — Concretize prior-art search in Step 1

The skill's checklist already reads "Explore project context — check files, docs,
recent commits, **nx search for prior art**." This is too vague to be actionable.
Replace with explicit queries:

```markdown
### Step 1: Prior Art Search (before exploring files or asking questions)

Search T3 for prior decisions on this topic:

    nx search "{feature or topic}" --corpus knowledge --n 5

If a prior decision exists, surface it immediately — either re-use it (if still
valid) or acknowledge it explicitly before proposing alternatives. Don't re-litigate
settled decisions without knowing they were settled.

If no prior decision exists, proceed to file and commit exploration.
```

This changes the prior-art search from a parenthetical hint to the **first
concrete action** in Step 1. The superpowers `brainstorming` skill is invoked
unchanged after this gate completes.

**No new nx:systematic-debugging wrapper skill**: The `superpowers:systematic-
debugging` skill has no existing nx wrapper and creating one risks fragmenting
the debugging workflow. The higher-leverage change is C3 (`java-debugger` T3
storage) — this directly adds root cause accumulation to the agent that does the
actual debugging work.

## Alternatives Considered

### Alternative A: Sweeping Protocol Mandate

Update CONTEXT_PROTOCOL.md to mandate nx search for all agents at every phase
boundary — RECEIVE, each analysis phase, PRODUCE.

**Rejected**: Adds latency and noise proportional to the number of agents and
phases. Most nx searches in this model would return either empty results (for
first-time tasks) or marginally relevant chunks. The benefit accrues only for
repeated task types where knowledge has accumulated. The mandatory form degrades
the common case to benefit the uncommon case.

### Alternative C-only: Knowledge Accumulation Without Protocol Changes

Focus only on ensuring agents store findings, without changing how they search.

**Rejected**: Accumulation without retrieval is invisible. If agents don't search
for prior findings, stored findings sit inert. Both tracks are needed, but the
accumulation changes (Track C) are higher-leverage than the protocol changes
(Track B) if forced to choose.

### Alternative: Add nx to Test Validator

Add prior test pattern search to `test-validator`.

**Rejected**: Test validation is inherently code-local — it runs tests and reads
coverage output. Semantic search for "prior test patterns" rarely changes what
the validator does. Low value-to-noise ratio.

## Trade-offs

### Positive Consequences

- Debugging root causes begin accumulating in T3 from the first `java-debugger`
  invocation; by the third similar failure, the prior analysis is available
- `plan-auditor` code reference validation becomes more reliable (Grep-primary path
  replaces unreliable code corpus search; `--hybrid --max-file-chunks` available
  as semantic fallback post-RDR-006 re-indexing)
- `brainstorming` avoids re-litigating settled decisions when prior art is in T3
- Implementation discoveries accumulate over time; later implementation tasks get
  progressively more context

### Negative Consequences

- All Track B changes add at least one nx search call per agent invocation; this
  adds latency (typically 1-2 seconds per search)
- Conditional language in C1 requires agents to correctly distinguish relay sources
  — LLMs are imperfect at this, and some agents will run the proactive search
  even when the relay is complete
- Stored findings that turn out to be wrong (C2, C3, C4) degrade future retrieval
  quality; mitigation is the explicit "only store confirmed findings" qualifiers

### Risks and Mitigations

- **Risk**: Agents store low-value noise to T3, diluting retrieval quality.
  **Mitigation**: Explicit qualifier "only store genuinely reusable / confirmed
  findings" in every storage instruction. Trust the LLM's judgment about
  what is novel and reusable — don't mandate storage for every task.

- **Risk**: CONTEXT_PROTOCOL.md changes affect all agents simultaneously.
  **Mitigation**: B1 adds only a reference table and a "when NOT to use" section —
  it doesn't mandate behavior. Agents already choosing not to search can continue
  to choose not to search.

- **Risk**: `java-developer` proactive search (C1) runs even for trivial tasks.
  **Mitigation**: The conditional framing ("if relay is from a human or incomplete")
  makes this a fallback, not a default. The agent should skip it for complete
  strategic-planner relays.

## Implementation Plan

### Phase 1: CONTEXT_PROTOCOL.md (foundation)

No agent changes. Low risk.

1. Add "Choosing Search Options" table (B1)
2. Fix stale namespace paragraph — replace bare-`{repo}` language with the correct
   three-namespace documentation (`{repo}`, `{repo}_rdr`, `{repo}_pm`) (B1)

**Validation**: Re-read the document; verify no existing agent instructions
contradict the new table. Verify no mandatory language has crept in. Verify the
namespace paragraph matches `reference.md` conventions.

**Pre-Phase 2 note**: B3 uses the multi-query pattern (not `--agentic`, which is
removed by RDR-009). All Phase 2 changes may proceed independently.

**Scratch cleanup**: Create a bead for removing the false scratch PRODUCE sections
from agents other than `codebase-deep-analyzer`. This is documentation debt
identified by this RDR but out of scope here:
`bd create "Remove false scratch PRODUCE sections from agents" -t chore -p 3`

### Phase 2: High-impact agent changes

Each file change is independent. Implement in any order.

3. `plan-auditor.md` — Grep-primary code reference validation (B2)
4. `deep-research-synthesizer.md` — multi-query pattern for Phase 2 gathering (B3)
5. `code-review-expert.md` — promote pattern baseline to Step 0; fix storage condition (B4 + C5)
6. `deep-analyst.md` — add prior evidence check to Phase 3, add T3 storage (B5 + C4)
7. `java-developer.md` — add conditional proactive search and T3 storage (C1 + C2)
8. `java-debugger.md` — add prior traces search and mandatory root cause storage (C3)

9. `nx/skills/brainstorming-gate/SKILL.md` — concretize Step 1 prior-art search (B6)

**Validation**: For each agent, read the full RECEIVE section and PRODUCE section
after change. Verify:

- No new mandatory steps have been added that would fire on every task
- Conditional language is clear and implementable
- T3 storage instructions include the "only if novel/confirmed" qualifier

## Test Plan

### Phase 1

- Read CONTEXT_PROTOCOL.md after change; ask: does the "when NOT to use" section
  prevent the forced-nx antipattern for simple tasks? If not, revise.

### Phase 2

- **B3** (`deep-research-synthesizer` multi-query): Invoke on a conceptual research
  topic with imprecise vocabulary. Verify Phase 2 initial gathering includes two
  nx search calls — primary term and alternate framing — before concluding no prior
  knowledge exists.
- **B4/B6** (`code-review-expert` Step 0, `nx:brainstorming-gate` Step 1): Invoke
  each on a codebase/topic with prior T3 content. Verify the pattern baseline /
  prior-art search fires before any files are read or questions asked. For B4
  specifically: verify the Step 0 output references Grep results (not nx search
  results) for at least one pattern check — this confirms Grep-primary, not nx
  search, is being used as the primary path.
- **B5** (`deep-analyst` prior evidence check): Invoke on a component that has a
  prior T3 analysis. Verify the prior finding is referenced in Phase 3 before new
  evidence gathering begins.
- **C1/C2** (`java-developer`): Invoke with a relay containing no nx artifacts;
  verify proactive search runs. Invoke with a relay that has nx store titles; verify
  search is skipped. After a session with a non-obvious implementation discovery,
  verify a T3 entry appears under `insight-developer-{topic}`.
- **C3** (`java-debugger`): Invoke on a bug class that has a prior `debug-finding-*`
  T3 entry. Verify prior trace surfaces before hypothesis generation. After root
  cause is confirmed, verify a new structured T3 entry appears with `## Root Cause`,
  `## Evidence`, `## Fix` sections.
- **C4** (`deep-analyst` T3 storage): After a confirmed analytical conclusion,
  verify a T3 entry appears under `analysis-deep-{component}-{date}`.
- **B2** (`plan-auditor` Grep-primary): Invoke plan-auditor on a plan referencing a
  class known to exist. Verify the code reference validation output cites Grep output,
  not nx search results. Confirm any nx semantic search is conditional on RDR-006
  re-indexing state, not executed unconditionally.
- **C5** (`code-review-expert` storage): After a review session that identifies a
  pattern violation across multiple locations, verify a T3 entry appears under
  `review-pattern-{pattern-name}-{date}` with `## Pattern`, `## Violation`,
  `## Recommendation` sections. **Note**: The significance threshold ("across multiple
  locations" or "non-obvious") is a prompt instruction with no external checksum —
  this test validates storage format and triggering, but cannot distinguish an agent
  that correctly applied the threshold from one that stored for a single-location nit.
  Accept this limitation: the threshold is meaningful guidance even if not fully
  testable.

## Open Questions

- **T3 naming collision**: Multiple agents now write to `knowledge` with overlapping
  title prefixes (`debug-`, `analysis-`, `insight-`). Is the existing
  `{domain}-{agent-type}-{topic}` convention sufficient to prevent confusion?
  No action required unless retrieval quality degrades after accumulation.
