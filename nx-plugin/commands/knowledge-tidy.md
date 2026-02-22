---
description: Persist and organize knowledge into nx store using knowledge-tidier agent
---

# Knowledge Tidying Request

!{
  echo "## Context"
  echo ""
  echo "**Working directory:** $(pwd)"
  echo ""

  echo "### Existing Knowledge"
  echo '```'
  if command -v nx &> /dev/null; then
    nx store list --collection knowledge 2>/dev/null | head -10 || echo "No knowledge store entries found"
  else
    echo "nx not available"
  fi
  echo '```'
  echo ""

  # Active beads context
  echo "### Recently Completed Beads"
  echo '```'
  if command -v bd &> /dev/null; then
    bd list --status=done --limit=5 2>/dev/null || echo "No recently completed beads"
  else
    echo "Beads not available"
  fi
  echo '```'
  echo ""

  echo "### Storage Standards"
  echo ""
  echo "Title conventions: research-{topic}, decision-{component}-{name}, pattern-{name}, debug-{component}-{issue}"
  echo "All entries go to: nx store put --collection knowledge"
}

## Knowledge to Persist

$ARGUMENTS

## Relay Instructions

Use the **Task tool** to delegate to knowledge-tidier:

```markdown
## Relay: knowledge-tidier

**Task**: Organize and persist knowledge about "$ARGUMENTS" into nx store (T3)
**Bead**: [From recently completed beads above or 'none']

### Input Artifacts
- nx store: [Search for existing entries on this topic to avoid contradictions]
- nx memory: [project/title path or 'none']
- Files: [Source files or documents containing findings]

### Knowledge to Organize
$ARGUMENTS

### Deliverable
Knowledge persisted to nx store T3 with correct titles, tags, and verified searchability

### Quality Criteria
- [ ] Knowledge stored via `nx store put --collection knowledge`
- [ ] No contradictions with existing entries (checked and resolved)
- [ ] Title follows naming convention (research-*, decision-*, pattern-*, debug-*)
- [ ] Tags are meaningful for future retrieval
- [ ] Searchable — verified with `nx search "topic" --corpus knowledge`
```
