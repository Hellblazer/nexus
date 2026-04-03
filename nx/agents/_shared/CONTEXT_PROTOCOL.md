# Shared Context Protocol for Agents

This file documents the standard context exchange protocol used by all agents for consistent relays, context recovery, and knowledge management.

## Degraded Mode

If nexus MCP tools (`mcp__plugin_nx_nexus__*`) are unavailable (e.g., MCP server not started, plugin not loaded), fall back to the `nx` CLI via Bash tool. MCP tools are the primary interface; CLI is the fallback.

## RECEIVE (Before Starting Work)

### Proactive Search Agents (Planning & Research)

These agents **MUST proactively search** for context before starting:
- **strategic-planner**: Search nx T3 store for prior decisions, nx T2 memory for active work
- **architect-planner**: Search nx T3 store for architectural patterns, design decisions
- **deep-research-synthesizer**: Search nx T3 store for prior research, web resources for related docs
- **codebase-deep-analyzer**: Search nx T3 store for codebase knowledge, architecture notes

**Search Sources in Order**:
1. **Bead**: `/beads:show <id>` for task context, design field, dependencies
2. **Project Infrastructure**: T2 memory and beads context is auto-injected by SessionStart and SubagentStart hooks
3. **nx T3 store**: Use search tool: `query="[topic]", corpus="knowledge", limit=5`
4. **nx T2 memory**: Use memory_get tool: `project="{project}", title="ACTIVE_INDEX.md"`
5. **T1 scratch** (current session): Use scratch tool: `action="search", query="[topic]"`

### Relay-Reliant Agents (Execution & Validation)

These agents **rely on relays** for context (do not proactively search):
- **developer**: Expects architecture/plan in relay
- **code-review-expert**: Expects files to review in relay
- **plan-auditor**: Expects plan document in relay
- **test-validator**: Expects code/test paths in relay
- **debugger**: Expects failure description in relay

**Sibling context (SHOULD, not MUST):** Before starting work, relay-reliant agents SHOULD search scratch for predecessor findings:

Use scratch tool: action="search", query="[task topic]", limit=5

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

**T1 MCP Tools:**
```
Use scratch tool: action="put", content="<content>", tags="TAG1,TAG2"
Use scratch tool: action="get", entry_id="<id>"
Use scratch tool: action="search", query="<query>", limit=10
Use scratch tool: action="list"
Use scratch_manage tool: action="flag", entry_id="<id>", project="PROJECT", title="TITLE"
Use scratch_manage tool: action="promote", entry_id="<id>", project="PROJECT", title="TITLE"
```

The SessionEnd hook runs automatically at session close and auto-promotes flagged T1 items to T2. Flagging items with scratch_manage `action="flag"` is how you opt in.

**Promote to T2 when:**
- Hypothesis validated (worth preserving across sessions)
- Interim findings that a future session may need
- Working notes that inform future work

## Storage Tier Quick Reference

| Tier | Name | Scope | MCP Tools | Use Cases | TTL |
|------|------|-------|-----------|-----------|-----|
| T1 | scratch | Session (ephemeral) | `scratch`, `scratch_manage` | Working notes, hypotheses, debug traces | Wiped on SessionEnd (flag to survive) |
| T2 | memory | Per-project, persistent | `memory_put`, `memory_get`, `memory_delete`, `memory_search` | Session state, project context, agent relay, active work | 30d default; `permanent` available |
| T3 | store / search | Permanent, cross-session | `search`, `store_put`, `store_get`, `store_list`, `store_delete` | Research findings, architectural decisions, validated patterns | `permanent` or explicit TTL |

## Pagination

`search`, `store_list`, and `memory_search` return paged results. Response footer format: `--- showing X-Y of Z. next: offset=N`. Re-call with `offset=N` for the next page. Stop when you see `(end)` or `No results at offset N`.

## Choosing Search Options

Use the right search form for the task:

| Goal | Tool Call |
|---|---|
| Find related prior knowledge | Use search tool: `query="topic", corpus="knowledge", limit=5` |
| Filter by year, tag, or metadata | Use search tool: `query="topic", where="bib_year>=2023"` |
| Filter by multiple criteria | Use search tool: `query="topic", where="bib_year>=2020,tags=arch"` |
| Research with uncertain vocabulary | Run 2 searches: primary term, then alternate framing |
| Conceptual code search (unfamiliar codebase) | Use search tool: `query="concept", corpus="code", limit=15` |
| Documentation search | Use search tool: `query="topic", corpus="docs", limit=10` |
| Exact code navigation | Use Grep tool instead — faster and more precise |
| Cross-corpus research | Run multiple search calls with different corpus values |
| List documents in a collection | Use store_list tool: `collection="knowledge__art", docs=true` |
| Browse collection contents | Use collection_info tool: `name="knowledge__art"` (shows sample titles) |

**When NOT to use search:**
- When the relay already contains the information needed
- For simple, bounded tasks where prior knowledge is unlikely to change the approach
- When Grep or file reads are faster and more precise (class/function lookups)

## PRODUCE

Agents produce artifacts based on their specialization:
- **Code Changes**: Committed with bead reference in message
- **Test Results**: Logged; failures create bug beads
- **Analysis/Research**: Store in nx T3 store with appropriate title pattern
- **Session State**: Store in nx T2 memory for multi-session work
- **Interim Working Notes**: Use T1 scratch for session-scoped state; promote to T2 when validated:
  ```
  # Store ephemeral working note
  Use scratch tool: action="put", content="<hypothesis or interim finding>", tags="hypothesis"
  # Flag for auto-flush to T2 at session end
  Use scratch_manage tool: action="flag", entry_id="<id>", project="{project}", title="interim-notes.md"
  # Or promote immediately
  Use scratch_manage tool: action="promote", entry_id="<id>", project="{project}", title="interim-findings.md"
  ```

### Naming Conventions

- **nx store title**: `{domain}-{agent-type}-{topic}` (e.g., `decision-architect-cache-strategy`)
- **nx memory**: `project="{project}", title="{topic}.md"` (e.g., `project="ART", title="auth-implementation.md"`)


## RELAY (Standard Format)

Relays are constructed by the **caller** (main conversation, skill, or orchestrator) when dispatching agents. Agents do not construct relays to other agents — subagents cannot spawn subagents. Instead, agents output a "Recommended Next Step" block that the caller uses to build the next relay.

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

## RECOVER (If Context Missing)

If expected context not received:
1. Search nx T3 store for related prior work: Use search tool: `query="[topic]", corpus="knowledge", limit=5`
2. Check nx T2 memory for session state: Use memory_search tool: `query="[topic]", project="{project}"`
3. Check T1 scratch for in-session notes: Use scratch tool: `action="search", query="[topic]"`
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
Use store_put tool: content="content", collection="knowledge", title="research-topic-date", tags="category"

# Search stored knowledge
Use search tool: query="query", corpus="knowledge", limit=5

# List stored documents
Use store_list tool: collection="knowledge"
```

### Metadata
store_put uses `tags` parameter for categorization (comma-separated strings).

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
Use memory_put tool: content="content", project="{project}", title="findings.md", ttl=30

# Read from memory
Use memory_get tool: project="{project}", title="findings.md"

# Search memory
Use memory_search tool: query="query", project="{project}"

# List memory files
Use memory_get tool: project="{project}", title=""
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
