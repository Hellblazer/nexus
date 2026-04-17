# Shared Agent Resources

This directory contains shared resources used across multiple agents.

## Files

- **CONTEXT_PROTOCOL.md**: Standard context exchange protocol for agent relays, context recovery, and knowledge management. All agents should reference this for consistent behavior.
- **RELAY_TEMPLATE.md**: Canonical relay format with full template, extended template, and optional fields reference. Used by skills and the main conversation when dispatching agents. Agents themselves do not construct relays (subagents cannot spawn subagents) — they output "Recommended Next Step" blocks that the caller uses to construct the next dispatch.
- **ERROR_HANDLING.md**: T1/T2/T3 error patterns, TTL guard documentation, common failure modes and recovery steps.
- **MAINTENANCE.md**: Agent maintenance procedures, consistency checks, and guidance on adding new agents.

## Usage Pattern

All 13 agents (10 active + 3 RDR-080 MCP-tool redirect stubs) reference this shared Context Protocol using:

```markdown
## Context Protocol

This agent follows the [Shared Context Protocol](./_shared/CONTEXT_PROTOCOL.md).

### Agent-Specific PRODUCE
- [Agent-specific artifacts this agent produces]
```

This approach reduces maintenance burden and ensures consistency across all agents.

## Error Handling

See [ERROR_HANDLING.md](./ERROR_HANDLING.md) for common error handling patterns.

## Maintenance

See [MAINTENANCE.md](./MAINTENANCE.md) for agent maintenance procedures, consistency checks, and guidance on adding new agents.
