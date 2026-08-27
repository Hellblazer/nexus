# Shared Context Protocol for Agents

This file documents the standard context exchange protocol used by all agents for consistent relays, context recovery, and knowledge management.

## Degraded Mode

If nexus MCP tools (`mcp__plugin_conexus_nexus__*`) are unavailable (e.g., MCP server not started, plugin not loaded), fall back to the `nx` CLI via Bash tool. MCP tools are the primary interface; CLI is the fallback.

## RECEIVE (Before Starting Work)

### Proactive Search Agents (Planning & Research)

These agents **MUST proactively search** for context before starting:
- **strategic-planner**: Search T3 store for prior decisions, T2 memory for active work
- **architect-planner**: Search T3 store for architectural patterns, design decisions
- **deep-research-synthesizer**: Search T3 store for prior research, web resources for related docs
- **codebase-deep-analyzer**: Search T3 store for codebase knowledge, architecture notes

**Search Sources in Order**:
1. **Bead**: `/beads:show <id>` for task context, design field, dependencies
2. **Project Infrastructure**: T2 memory and beads context is auto-injected by SessionStart and SubagentStart hooks
3. **T3 store**: mcp__plugin_conexus_nexus__search( `query="[topic]", corpus="knowledge", limit=5`
4. **Catalog link graph**: `mcp__plugin_conexus_nexus__query( question="[topic]", follow_links="implements" )` — the `query` tool automatically boosts results from documents with precise `implements` links
5. **T2 memory**: mcp__plugin_conexus_nexus__memory_get( `project="{project}", title="ACTIVE_INDEX.md"`
6. **T1 scratch** (current session): mcp__plugin_conexus_nexus__scratch( `action="search", query="[topic]"`

### Relay-Reliant Agents (Execution & Validation)

These agents **rely on relays** for context (do not proactively search):
- **developer**: Expects architecture/plan in relay
- **code-review-expert**: Expects files to review in relay
- **nx_plan_audit** (`mcp__plugin_conexus_nexus__nx_plan_audit`): call with `plan_json` + `context` (RDR-080 — no relay needed)
- **test-validator**: Expects code/test paths in relay
- **debugger**: Expects failure description in relay

**Sibling context (SHOULD, not MUST):** Before starting work, relay-reliant agents SHOULD search scratch for predecessor findings:

mcp__plugin_conexus_nexus__scratch( action="search", query="[task topic]", limit=5

If results exist, incorporate them as supplementary context. If scratch is empty, proceed normally. This adds one MCP call (~100ms) and provides context that relays may omit.

**Precedence rule:** Relay context takes precedence over scratch context. Scratch entries are hints, not authoritative. If a scratch `decision` entry conflicts with the relay, proceed per the relay and note the discrepancy.

**If relay is incomplete**, use RECOVER protocol (search as fallback).

### Relay Validation (All Agents)

If relay received, verify it contains:
- [ ] Bead ID(s) with current status (or 'none')
- [ ] Input Artifacts section (nx store/memory/Files)
- [ ] Deliverable description
- [ ] Quality criteria checkboxes

## T1 — Session Scratch (Ephemeral)

T1 is session-scoped: all entries are wiped at SessionEnd unless flagged.

**When to use T1:**
- Ephemeral working notes and hypotheses during a single session
- Intermediate analysis results before validation
- Step-by-step debug traces that may not be worth persisting
- Routing or coordination notes within a pipeline run

### Standard Scratch Tags

All agents SHOULD use these tags when writing to scratch:

| Tag | Meaning | Written by | Useful for |
|-----|---------|-----------|------------|
| `impl` | General implementation work (combine with others) | developer | any successor |
| `checkpoint` | Implementation step completed | developer | reviewer, test-validator |
| `failed-approach` | Attempted fix/approach that didn't work | developer, debugger | reviewer, debugger |
| `hypothesis` | Working theory about a problem | debugger, analyst | developer |
| `discovery` | Unexpected finding during work | any agent | any successor |
| `decision` | Design/approach choice made during work | planner, architect | developer |

Tags are comma-separated. Combine with domain tags: `failed-approach,auth,retry`.

### RESERVED — never write `review-completed`

`review-completed` is not a note tag. It is the token
`pre_close_verification_hook.sh` reads to decide whether a bead may close, and
it matches by SUBSTRING over T1 tags/content and T2 title/content. Any entry
carrying that token plus a bead id IS that bead's review coverage, whatever the
entry meant to say.

**No agent writes it. Only the session that owns the gate does, once, after
every mandated reviewer has run and any resulting fixes have landed.**

This is not hypothetical. On 2026-08-26 a dispatched reviewer finished the FIRST
of a two-reviewer gate and left a handoff note for its sibling beginning
`review-completed bead=nexus-utpuw.23 (RG-C reviewer 1/2: ...)`. It was honest
and it said "1/2" — and the hook cannot read that. The gate would have closed
with the critic never dispatched, on a gate whose own text says the critic is
never optional (nexus-e3mak).

The failure is structural, not a lapse of care: a gate whose evidence is written
by one of the parties it gates cannot distinguish coverage from a progress
report. The same shape is filed as nexus-1f98p, where a scope audit read an
allowlist typed by the agent whose commits it audited.

To hand findings to a sibling reviewer or to the dispatcher, write to T2 with a
descriptive title and no reserved token, and return them in your response text:

```
nx memory put - -p <project> -t "<gate>-review-<role>-findings"   # no reserved token
```

**T1 MCP Tools:**
```
mcp__plugin_conexus_nexus__scratch(action="put", content="<content>", tags="TAG1,TAG2")
mcp__plugin_conexus_nexus__scratch(action="get", entry_id="<id>")
mcp__plugin_conexus_nexus__scratch(action="search", query="<query>", limit=10)
mcp__plugin_conexus_nexus__scratch(action="list")
mcp__plugin_conexus_nexus__scratch_manage(action="flag", entry_id="<id>", project="PROJECT", title="TITLE")
mcp__plugin_conexus_nexus__scratch_manage(action="promote", entry_id="<id>", project="PROJECT", title="TITLE")
```

The SessionEnd hook runs automatically at session close and auto-promotes flagged T1 items to T2. Flagging items with scratch_manage `action="flag"` is how you opt in.

**Promote to T2 when:**
- Hypothesis validated (worth preserving across sessions)
- Interim findings that a future session may need
- Working notes that inform future work

### Parallel dispatch that MUTATES needs a mutex

A fan-out of agents is safe while they READ. The moment their task involves
changing the tree — mutating a file to check whether an assertion can fail,
running a build, restoring afterwards — concurrent agents collide, and the
collision does not read as a collision.

**The caller hands out the lock at dispatch time**, in the prompt, alongside
the task (orchestration stays with the caller — see RELAY below). Alternatively
give each agent its own worktree; the lock is cheaper when the fan-out is
short-lived.

**RETROFITTING IT DOES NOT RELIABLY WORK, and an agent is right to refuse.** An
out-of-band message that arrives mid-task, contradicts the original briefing,
and asks the recipient to adopt a new protocol is exactly the shape of a prompt
injection. Measured on the RG-E run below: the lock was sent to all three
agents mid-flight, and one correctly declined it as a likely injection — then
went on to observe the very collision it was meant to prevent (a shared file
transiently carrying a peer's mutation, self-resolving ~50s later), avoiding
corrupted findings only because it had sourced its restore from git HEAD rather
than trusting its own earlier copy.

So after dispatch the honest options are: let the run finish and re-run
serially, or kill and re-dispatch. "I will just message them" is not one. If
you must send such a message anyway, give the recipient a fact it can verify in
the repo — authority cannot be asserted over that channel.

    until mkdir /tmp/<gate>.lock 2>/dev/null; do sleep 2; done
    # mutate -> run -> RESTORE -> confirm `git status --short` is clean
    rmdir /tmp/<gate>.lock

**PREFER NOT SHARING THE TREE AT ALL — the lock is the fallback.** Ranked:
give each agent its own `git worktree` (full isolation, the real suite still
runs); or, when the check is pure logic, extract what you need with
`git show <rev>:<path>` into a private temp copy and mutate that, which needs no
lock because nothing shared is written; and only then a mutex on the shared
tree, for mutations that must be visible to code resolving from the worktree.
Measured on the RG-E run below: the agent that declined the lock and worked from
`git show` copies reproduced the finding exactly and hit none of the collisions
the lock-holders did.

**Every agent that mutates the SHARED tree:**

- Take the lock before the first mutation. `mkdir` is atomic, so it is a real
  mutex.
- Hold it for mutate → run → restore → verify, and nothing else. Never across
  analysis, write-up, or a full suite run you did not need under mutation.
- Restore from a copy you took yourself, never by hand-reconstructing — and
  **namespace the backup path** (`/tmp/$$-layout.sh.orig`, not
  `/tmp/layout.sh.orig`). Measured on the same RG-E run: an agent that DID hold
  the lock still restored a SIBLING's backup from the shared unnamespaced path,
  leaving its own mutation in place after a "successful" restore. Prefer
  `git checkout -- <path>` for a tracked file — there is nothing to namespace.
- **Verify the restore against the repo, not against your own copy.** Diffing
  against the backup you just restored from is a tautology. `git status --short`
  is the check; that is how the above was caught.
- **An unexpectedly dirty tree is a COLLISION, not a finding.** Release, wait,
  re-check, and report it as a collision. Never draw a conclusion from it, and
  re-run any ambiguous result under the lock before reporting — an
  unreproducible mutation result is not evidence.
- Report positively whether you observed a collision, so an undetected one does
  not pass silently.

WHY THIS IS WORSE THAN AN ORDINARY RACE, and why it belongs in the gate
protocol rather than in general hygiene: a peer restoring the tree mid-run is
indistinguishable from *the assertion under test flipping colour*. On a gate
whose entire output is "can this check fail?", that yields a confident **"this
assertion cannot fail"** derived purely from an artifact — the wrong answer,
stated with evidence, to the exact question the gate exists to answer.

Origin: RG-E (nexus-utpuw.25) on 2026-08-26 dispatched three agents
concurrently — both reviewers plus test-validator, which that gate mandates
"because the arc's whole safety claim rests on these tests being non-vacuous" —
every one of them instructed to mutate the same four files. The lock was
retrofitted mid-run. Same family as the RESERVED section above: a gate whose
evidence is produced by parties that can interfere with each other cannot
distinguish a result from an artifact.

## Storage Tier Quick Reference

Conexus MCP tools use the full prefix `mcp__plugin_conexus_nexus__` (e.g. `mcp__plugin_conexus_nexus__search`).

| Tier | Name | Scope | MCP Tools | Use Cases | TTL |
|------|------|-------|-----------|-----------|-----|
| T1 | scratch | Session (ephemeral) | `scratch`, `scratch_manage` | Working notes, hypotheses, debug traces | Wiped on SessionEnd (flag to survive) |
| T2 | memory | Per-project, persistent | `memory_put`, `memory_get`, `memory_delete`, `memory_search`, `memory_consolidate` | Session state, project context, agent relay, active work. Consolidation: find overlaps, merge duplicates, flag stale entries | 30d default; `permanent` available. Heat-weighted: highly-accessed entries survive longer |
| T3 | store / search | Permanent, cross-session | `search`, `query`, `store_put`, `store_get`, `store_list` | Research findings, architectural decisions, validated patterns. Results include `chunk_text_hash` metadata | `permanent` or explicit TTL |
| Catalog | document registry | Permanent, cross-session | `search`, `show`, `links`, `link`, `resolve` | Author/corpus/provenance queries; typed links between documents; content-addressed span references | Permanent |

## Pagination

`search`, `store_list`, and `memory_search` return paged results. Response footer format: `--- showing X-Y of Z. next: offset=N`. Re-call with `offset=N` for the next page. Stop when you see `(end)` or `No results at offset N`.

## Choosing Search Options

Use the right search form for the task:

| Goal | Tool Call |
|---|---|
| Find related prior knowledge | mcp__plugin_conexus_nexus__search( `query="topic", corpus="knowledge", limit=5` |
| Research question (which documents match?) | mcp__plugin_conexus_nexus__query( `question="topic", corpus="knowledge"` |
| Filter by year, tag, or metadata | mcp__plugin_conexus_nexus__query( `question="topic", where="bib_year>=2023"` |
| Filter by multiple criteria | mcp__plugin_conexus_nexus__query( `question="topic", where="bib_year>=2020,tags=arch"` |
| Research with uncertain vocabulary | Run 2 queries: primary term, then alternate framing |
| Conceptual code search (unfamiliar codebase) | mcp__plugin_conexus_nexus__search( `query="concept", corpus="code", limit=15` |
| Documentation search | mcp__plugin_conexus_nexus__search( `query="topic", corpus="docs", limit=10` |
| Exact code navigation | Use Grep tool instead — faster and more precise |
| Search by author | mcp__plugin_conexus_nexus__query( `question="topic", author="Fagin"` |
| Search by content type | mcp__plugin_conexus_nexus__query( `question="topic", content_type="rdr"` |
| Follow citation links | mcp__plugin_conexus_nexus__query( `question="topic", follow_links="cites", depth=1` |
| Search document subtree | mcp__plugin_conexus_nexus__query( `question="topic", subtree="1.1"` |
| Search within a topic cluster | mcp__plugin_conexus_nexus__search( `query="question", topic="PDF Extraction"` |
| Cross-corpus research | Use query tool with `corpus="all"` or multiple query calls |
| List documents in a collection | mcp__plugin_conexus_nexus__store_list( `collection="knowledge__art-1-1__voyage-context-3__v1", docs=true` (RDR-103 4-segment shape) |

**When NOT to use search:**
- When the relay already contains the information needed
- For simple, bounded tasks where prior knowledge is unlikely to change the approach
- When Grep or file reads are faster and more precise (class/function lookups)

## PRODUCE

Agents produce artifacts based on their specialization:
- **Code Changes**: Committed with bead reference in message
- **Test Results**: Logged; failures create bug beads
- **Analysis/Research**: Store in T3 store with appropriate title pattern
- **Session State**: Store in T2 memory for multi-session work
- **Interim Working Notes**: Use T1 scratch for session-scoped state; promote to T2 when validated:
  ```
  # Store ephemeral working note
  mcp__plugin_conexus_nexus__scratch( action="put", content="<hypothesis or interim finding>", tags="hypothesis"
  # Flag for auto-flush to T2 at session end
  mcp__plugin_conexus_nexus__scratch_manage( action="flag", entry_id="<id>", project="{project}", title="interim-notes.md"
  # Or promote immediately
  mcp__plugin_conexus_nexus__scratch_manage( action="promote", entry_id="<id>", project="{project}", title="interim-findings.md"
  ```

### Naming Conventions

- **nx store title**: `{domain}-{agent-type}-{topic}` (e.g., `decision-architect-cache-strategy`)
- **nx memory**: `project="{project}", title="{topic}.md"` (e.g., `project="ART", title="auth-implementation.md"`)

## Deliverable Length

Match written length to what the task needs. No filler sections, no redundant
summaries, no boilerplate restating the relay back to the caller. A write-back
is read by an agent (or a human) paying for every byte on every subsequent
turn.

Two-tier rule:
- **Structural contract** (section headings, verdict blocks, machine-parsed
  fields) is fixed and exempt from trimming — see `substantive-critic.md` §
  Output Format for the canonical example of what NOT to compress.
- **Prose content** within each section states the verdict, the findings at
  file:line, the evidence chain, and what was not checked. No restated relay,
  no narrated methodology, no praise.

Evidence density is the protected quantity: trim only when file:line
references and reachability chains stay flat or increase. If length and
evidence density fall together, the trim cut evidence, not filler — revert it.

## RELAY (Standard Format)

Relays are constructed by the **caller** (main conversation or skill) when dispatching agents. Agents do not construct relays to other agents — nested Agent dispatch works and is ledgered (probe-verified 2026-08-03, T2 [21371]), but orchestration stays with the caller by convention. Instead, agents output a "Recommended Next Step" block that the caller uses to build the next relay.

Standard relay structure:

```
## Relay: [Target Agent]

**Task**: [1-2 sentence summary]
**Bead**: [ID] (status: [status])

### Input Artifacts
- nx store: [document titles or "none"]
- nx memory: [project/title path or "none"]
- nx scratch: [scratch IDs or "none"]
- Files: [key files touched]

### Deliverable
[What the receiving agent should produce]

### Quality Criteria
- [ ] [Criterion 1]
- [ ] [Criterion 2]

### Context Notes
[Special context, blockers, or warnings]
```

See [RELAY_TEMPLATE.md](./RELAY_TEMPLATE.md) for the full template, extended template, and optional fields reference.

## Escalation (conditional, never routine)

Recommend a next agent only when a NAMED trigger fired, and name exactly one:

- you hit a blocker outside your role (e.g. developer -> debugger on confirmed intermittency)
- you found something whose remediation needs a plan you cannot write
- the relay's own Quality Criteria cannot be met with what you have

Otherwise end with your result and hand back. Do not recommend the routine
review gate — the caller owns the review-gate-commit tail already
(`~/.claude/CLAUDE.md` § Review Discipline; `orchestration/SKILL.md` Quick
Routing), and a per-agent reminder to dispatch it is compounding scaffolding:
it restates standing policy the caller already knows, and gets paid for again
on every subsequent turn that re-reads your output. This section removes the
per-agent REMINDER, not the gate: both reviewers still run, always, on every
implementation.

## Scope (MANDATORY, both directions)

Deliver what was asked, at the scope intended — do not quietly narrow it,
widen it, or transform it into an adjacent problem. If you believe the right
scope differs from the relay's, say so in one sentence and proceed per the
relay until told otherwise.

- An accepted plan, RDR, or bead graph is a record of intended work, not a
  standing order. If the relay's stated goal is already satisfied by work
  that exists, report "goal met; remaining planned items are X" and STOP. Do
  not auto-continue into the next phase.
- A change whose blast radius explodes mid-flight — one edit breaking more
  than ~20 tests, or requiring a parallel repair fleet — is a
  STOP-AND-SURFACE point. Hand back with the blast radius named. It is not a
  delegation problem to manage harder.
- Narrowing is equally a defect: dropping a relay item without an explicit
  DEVIATION line is silent scope reduction, which `/conexus:phase-review-gate`
  exists to catch.

## RECOVER (If Context Missing)

If expected context not received:
1. Search T3 store for related prior work: mcp__plugin_conexus_nexus__search( `query="[topic]", corpus="knowledge", limit=5`
2. Check T2 memory for session state: mcp__plugin_conexus_nexus__memory_search( `query="[topic]", project="{project}"`
3. Check T1 scratch for in-session notes: mcp__plugin_conexus_nexus__scratch( `action="search", query="[topic]"`
4. Query active work via `/beads:list` with status=in_progress
5. Document assumption in bead notes
6. Flag incomplete context in downstream relay

## Beads Integration

All agents should:
- Check `/beads:ready` for available work before starting
- Update bead status when starting: `/beads:update <id>` with status=in_progress
- Close beads when complete: `/beads:close <id>`
- Create new beads for discovered work: `/beads:create`
- Always commit `.beads/issues.jsonl` with code changes

## nx Store Patterns

### Document Title Prefixes by Domain
- `research-` - Research findings and literature reviews
- `decision-` - Architectural and design decisions
- `pattern-` - Reusable code patterns and solutions
- `debug-` - Debugging insights and root causes
- `analysis-` - Deep analysis findings
- `insight-` - Developer/agent discoveries

### Storage Tools
```
# Store a document
mcp__plugin_conexus_nexus__store_put( content="content", collection="knowledge", title="research-topic-date", tags="category", agent="<your-role>"

# Search stored knowledge
mcp__plugin_conexus_nexus__search( query="query", corpus="knowledge", limit=5

# List stored documents
mcp__plugin_conexus_nexus__store_list( collection="knowledge"
```

### Metadata
store_put uses `tags` parameter for categorization (comma-separated strings). Always pass `agent="<your-role>"` (mirrors `memory_put`'s attribution, nexus-4ftd7) — an unmarked write collapses onto the shared `"mcp"` fallback marker, which defeats `_flag_contradictions`'s agent-diversity precondition for every anonymous MCP writer.

## nx Memory Organization

Three project namespaces are in use:
- `{repo}` — agent working notes and relay state (e.g., `project="nexus"`)
- `{repo}_rdr` — RDR records and gate results (e.g., `project="nexus_rdr"`)

Common titles under `{repo}`:
- `title="hypotheses.md"` - Current working hypotheses
- `title="findings.md"` - Validated discoveries
- `title="blockers.md"` - Active blockers and impediments
- `title="relay.md"` - Pending relay context

### Memory Tools
```
# Write to memory
mcp__plugin_conexus_nexus__memory_put( content="content", project="{project}", title="findings.md", ttl=30

# Read from memory
mcp__plugin_conexus_nexus__memory_get( project="{project}", title="findings.md"

# Search memory
mcp__plugin_conexus_nexus__memory_search( query="query", project="{project}"

# List memory files
mcp__plugin_conexus_nexus__memory_get( project="{project}", title=""
```

## Usage in Agent Files

Agents should reference this protocol instead of duplicating:

```markdown
## Context Protocol

This agent follows the [Shared Context Protocol](./_shared/CONTEXT_PROTOCOL.md).

### Agent-Specific PRODUCE
- [Additional artifacts this agent produces]
- [Custom nx store title patterns]

### Agent-Specific RELAY
[Any modifications to standard relay format]
```
