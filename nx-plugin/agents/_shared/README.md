# Shared Agent Resources

This directory contains shared resources used across multiple agents.

## Files

- **CONTEXT_PROTOCOL.md**: Standard context exchange protocol for agent relays, context recovery, and knowledge management. All agents should reference this for consistent behavior.

## Usage Pattern

All 15 agents now reference this shared Context Protocol using:

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
