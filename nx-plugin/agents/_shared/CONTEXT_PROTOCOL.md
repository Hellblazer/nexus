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
2. **Project Infrastructure**: `nx pm resume` if project uses nx PM
3. **nx T3 store**: `nx search "[topic]" --corpus knowledge --n 5`
4. **nx T2 memory**: `nx memory get --project {project}_active --title ACTIVE_INDEX.md`

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

## PRODUCE

Agents produce artifacts based on their specialization:
- **Code Changes**: Committed with bead reference in message
- **Test Results**: Logged; failures create bug beads
- **Analysis/Research**: Store in nx T3 store with appropriate title pattern
- **Session State**: Store in nx T2 memory for multi-session work

### Naming Conventions

- **nx store title**: `{domain}-{agent-type}-{topic}` (e.g., `decision-architect-cache-strategy`)
- **nx memory**: `--project {project}_active --title {phase}.md` (e.g., `--project ART_active --title phase2-implementation.md`)
- **Bead Description**: Include `Context: nx-plugin` line if project uses PM infrastructure

## RELAY (Standard Format)

All relays to downstream agents use this structure:

```
## Relay: [Target Agent]

**Task**: [1-2 sentence summary]
**Bead**: [ID] (status: [status])

### Input Artifacts
- nx store: [document titles or "none"]
- nx memory: [project/title path or "none"]
- Files: [key files touched]

### Deliverable
[What the receiving agent should produce]

### Quality Criteria
- [ ] [Criterion 1]
- [ ] [Criterion 2]

### Context Notes
[Special context, blockers, or warnings]
```

## RECOVER (If Context Missing)

If expected context not received:
1. Search nx T3 store for related prior work: `nx search "[topic]" --corpus knowledge --n 5`
2. Check nx T2 memory for session state: `nx memory search "[topic]" --project {project}_active`
3. Query `bd list --status=in_progress` for active work
4. Document assumption in bead notes
5. Flag incomplete context in downstream relay

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

Projects use `{repo}_active` naming for session work:
- `--title hypotheses.md` - Current working hypotheses
- `--title findings.md` - Validated discoveries
- `--title blockers.md` - Active blockers and impediments
- `--title relay.md` - Pending relay context

### Memory Commands
```bash
# Write to memory
nx memory put "content" --project {project}_active --title phase.md --ttl 30d

# Read from memory
nx memory get --project {project}_active --title phase.md

# Search memory
nx memory search "query" --project {project}_active

# List memory files
nx memory list --project {project}_active
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
