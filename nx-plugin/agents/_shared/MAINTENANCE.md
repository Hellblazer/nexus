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

6. Add Relay Protocol section

## Updating the Shared Context Protocol

1. Edit `_shared/CONTEXT_PROTOCOL.md`
2. All agents automatically use new version
3. No per-agent edits needed

## Adding nx search Integration

See java-developer.md or code-review-expert.md for example patterns:
- Use natural language queries
- Use `--hybrid` for code search (semantic + ripgrep)
- Use `--n` in 10-30 range

Example section:
```markdown
## Code Discovery with nx search

**Find Related Code**:
```bash
nx search "how does feature X work in our codebase" --corpus code --hybrid --n 15
```

### Integration with Workflow
1. User requests task
2. Use `nx search` to understand existing patterns
3. Execute task following discovered patterns
4. Store discoveries in nx store if novel
```

## Consistency Checks

```bash
# All agents reference shared protocol
grep -l "Shared Context Protocol.*_shared" nx-plugin/agents/*.md | wc -l

# No inline RECEIVE sections (should return nothing)
grep -l "^### RECEIVE " nx-plugin/agents/*.md

# Validate markdown frontmatter
for f in nx-plugin/agents/*.md; do
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

**nx search not returning results**:
- Run `nx index code <path>` first to index the repo
- Use `--hybrid` flag for best results with code
- Try broader queries if results are sparse
- Use `nx health` to verify Nexus server is running
