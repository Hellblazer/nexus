# Agent Maintenance Guide

## Quick Reference

- **Context Protocol**: All agents reference `_shared/CONTEXT_PROTOCOL.md`
- **Error Handling**: Common patterns in `_shared/ERROR_HANDLING.md`
- **Versions**: v2.0 standard, v2.1 for enhanced agents (strategic-planner)

## Adding a New Agent

1. Create agent file with frontmatter:
   ```yaml
   ---
   name: new-agent
   version: "2.0"
   description: What this agent does
   model: sonnet
   color: blue
   ---
   ```

2. Add core content sections (Usage Examples, main behavior, etc.)

3. Add Beads Integration section

4. Add Context Protocol reference:
   ```markdown
   ## Context Protocol

   This agent follows the [Shared Context Protocol](./_shared/CONTEXT_PROTOCOL.md).

   ### Agent-Specific PRODUCE
   - **Output Type 1**: Description
   - **Output Type 2**: Description
   ```

5. Add any specialized sections (Tool Usage, Problem-Solving, etc.)

6. Add "Recommended Next Step" section (see existing agents for pattern — agents output a next-step block, the caller dispatches)

## Updating the Shared Context Protocol

1. Edit `_shared/CONTEXT_PROTOCOL.md`
2. All agents automatically use new version
3. No per-agent edits needed

## Adding Search Integration

See developer.md or code-review-expert.md for example patterns:
- Use natural language queries in the search MCP tool
- Pass `corpus="code"` for code search
- Use `n` in 10-30 range

Example section:
```markdown
## Code Discovery with Search

**Find Related Code**:
```
Use search tool: query="how does feature X work in our codebase", corpus="code", limit=15
```

### Integration with Workflow
1. User requests task
2. Use search tool to understand existing patterns
3. Execute task following discovered patterns
4. Store discoveries via store_put tool if novel
```

## Consistency Checks

Run from the **plugin root** (the `nx/` directory):

```bash
# All agents reference shared protocol
grep -l "Shared Context Protocol.*_shared" agents/*.md | wc -l

# No inline RECEIVE sections (should return nothing)
grep -l "^### RECEIVE " agents/*.md

# Validate markdown frontmatter
for f in agents/*.md; do
  head -7 "$f" | grep -q "^---" && echo "✓ $(basename $f)" || echo "✗ $(basename $f)"
done
```

## Version Management

- **v2.0**: Standard version with shared Context Protocol reference
- **v2.1**: Enhanced version (currently: strategic-planner)

Update version in frontmatter when making significant changes to an agent.

## Troubleshooting

**Agent not finding shared protocol**:
- Check that `_shared/CONTEXT_PROTOCOL.md` exists
- Verify link syntax: `[Shared Context Protocol](./_shared/CONTEXT_PROTOCOL.md)`

**PRODUCE section missing**:
- Each agent should have `### Agent-Specific PRODUCE` section
- Content comes from original agent's PRODUCE section

**MCP tools not available**:
- Check that the nexus MCP server is registered in `.mcp.json`
- Verify `nx-mcp` entry point is installed: `which nx-mcp`
- Fall back to `nx` CLI via Bash tool (degraded mode)
