# Shared Context Protocol for Agents

This file documents the standard context exchange protocol used by all agents for consistent relays, context recovery, and knowledge management.

## RECEIVE (Before Starting Work)

### Proactive Search Agents (Planning & Research)

These agents **MUST proactively search** for context before starting:
- **strategic-planner**: Search nx T3 store for prior decisions, nx T2 memory for active work
- **java-architect-planner**: Search nx T3 store for architectural patterns, design decisions
- **deep-research-synthesizer**: Search nx T3 store for prior research, web resources for related docs
- **codebase-deep-analyzer**: Search nx T3 store for codebase knowledge, architecture notes

**Search Sources in Order**:
1. **Bead**: `bd show <id>` for task context, design field, dependencies
2. **Project Infrastructure**: T2 memory and beads context is auto-injected by SessionStart and SubagentStart hooks
3. **nx T3 store**: `nx search "[topic]" --corpus knowledge --n 5`
4. **nx T2 memory**: `nx memory get --project {project} --title ACTIVE_INDEX.md`
5. **T1 scratch** (current session): `nx scratch search "[topic]"` for any in-flight notes

### Relay-Reliant Agents (Execution & Validation)

These agents **rely on relays** for context (do not proactively search):
- **java-developer**: Expects architecture/plan in relay
- **code-review-expert**: Expects files to review in relay
- **plan-auditor**: Expects plan document in relay
- **test-validator**: Expects code/test paths in relay
- **java-debugger**: Expects failure description in relay

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

**T1 CLI:**
```bash
nx scratch put "<content>" [--tags TAG1,TAG2] [--persist] [--project PROJECT] [--title TITLE]
# Note: --project/--title on 'put' pre-configure the T2 flush destination for flag/promote; they do NOT give cross-session access to the content.
nx scratch get <id>
nx scratch search "<query>" [--n N]
nx scratch list
nx scratch flag <id> [--project PROJECT] [--title TITLE]   # mark for auto-flush to T2 at SessionEnd
nx scratch unflag <id>                                     # remove the auto-flush marking
nx scratch promote <id> --project PROJECT --title TITLE    # immediate T2 copy
nx scratch clear
```

The SessionEnd hook (`nx hook session-end`) runs automatically at session close and auto-promotes flagged T1 items to T2. This is not user-callable; flagging items with `nx scratch flag` is how you opt in.

**Promote to T2 when:**
- Hypothesis validated (worth preserving across sessions)
- Interim findings that a future session may need
- Working notes that inform future work

## Storage Tier Quick Reference

| Tier | Name | Scope | CLI Entry | Use Cases | TTL |
|------|------|-------|-----------|-----------|-----|
| T1 | nx scratch | Session (ephemeral) | `nx scratch put` | Working notes, hypotheses, debug traces | Wiped on SessionEnd (flag to survive) |
| T2 | nx memory | Per-project, persistent | `nx memory put` | Session state, project context, agent relay, active work | 30d default; `permanent` available |
| T3 | nx store / nx search | Permanent, cross-session | `nx store put` | Research findings, architectural decisions, validated patterns | `permanent` or explicit TTL |

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
| Large-file noise reduction | Add `--max-file-chunks 20` to any `--corpus code` search |

**When NOT to use nx search:**
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
  ```bash
  # Store ephemeral working note
  nx scratch put "<hypothesis or interim finding>" --tags "hypothesis"
  # Flag for auto-flush to T2 at session end
  nx scratch flag <id> --project {project} --title interim-notes.md
  # Or promote immediately
  nx scratch promote <id> --project {project} --title interim-findings.md
  ```

### Naming Conventions

- **nx store title**: `{domain}-{agent-type}-{topic}` (e.g., `decision-architect-cache-strategy`)
- **nx memory**: `--project {project} --title {topic}.md` (e.g., `--project ART --title auth-implementation.md`)


## RELAY (Standard Format)

All relays to downstream agents use this structure:

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
1. Search nx T3 store for related prior work: `nx search "[topic]" --corpus knowledge --n 5`
2. Check nx T2 memory for session state: `nx memory search "[topic]" --project {project}`
3. Check T1 scratch for in-session notes: `nx scratch search "[topic]"`
4. Query `bd list --status=in_progress` for active work
5. Document assumption in bead notes
6. Flag incomplete context in downstream relay

## Beads Integration

All agents should:
- Check `bd ready` for available work before starting
- Update bead status when starting: `bd update <id> --status=in_progress`
- Close beads when complete: `bd close <id>`
- Create new beads for discovered work: `bd create "Title" -t <type>`
- Always commit `.beads/issues.jsonl` with code changes

## nx Store Patterns

### Document Title Prefixes by Domain
- `research-` - Research findings and literature reviews
- `decision-` - Architectural and design decisions
- `pattern-` - Reusable code patterns and solutions
- `debug-` - Debugging insights and root causes
- `analysis-` - Deep analysis findings
- `insight-` - Developer/agent discoveries

### Storage Commands
```bash
# Store a document
echo "content" | nx store put - --collection knowledge --title "research-topic-date" --tags "category"

# Search stored knowledge
nx search "query" --corpus knowledge --n 5
nx search "query" --corpus knowledge --json

# List stored documents
nx store list --collection knowledge
```

### Metadata
nx store uses `--tags` for categorization (comma-separated strings).

## nx Memory Organization

Three project namespaces are in use:
- `{repo}` — agent working notes and relay state (e.g., `--project nexus`)
- `{repo}_rdr` — RDR records and gate results (e.g., `--project nexus_rdr`)

Common titles under `{repo}`:
- `--title hypotheses.md` - Current working hypotheses
- `--title findings.md` - Validated discoveries
- `--title blockers.md` - Active blockers and impediments
- `--title relay.md` - Pending relay context

### Memory Commands
```bash
# Write to memory
nx memory put "content" --project {project} --title findings.md --ttl 30d

# Read from memory
nx memory get --project {project} --title findings.md

# Search memory
nx memory search "query" --project {project}

# List memory files
nx memory list --project {project}
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
