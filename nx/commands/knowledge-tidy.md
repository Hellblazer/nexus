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

  # Project management context
  echo "### Project Management Context"
  echo ""
  if command -v nx &> /dev/null; then
    echo "**PM Status:**"
    echo '```'
    echo '```'
    echo ""
    PROJECT=$(basename "$(git rev-parse --show-toplevel 2>/dev/null)" 2>/dev/null)
    if [ -n "$PROJECT" ]; then
      echo "**T2 Memory ($PROJECT):**"
      echo '```'
      nx memory list --project "$PROJECT" 2>/dev/null | head -8 || echo "No T2 memory"
      echo '```'
      echo ""
      echo "**Session Scratch (T1):**"
      echo '```'
      nx scratch list 2>/dev/null | head -5 || echo "No T1 scratch"
      echo '```'
    fi
  fi
}

## Knowledge to Persist

$ARGUMENTS

## Action

Invoke the **knowledge-tidying** skill with the following relay. Fill in dynamic fields from the context above:

```markdown
## Relay: knowledge-tidier

**Task**: Organize and persist knowledge about "$ARGUMENTS" into nx store (T3)
**Bead**: [fill from recently completed bead above or 'none']

### Input Artifacts
- Files: [fill from source files or documents containing findings]

### Knowledge to Organize
$ARGUMENTS

### Deliverable
Knowledge persisted to nx store T3 with correct title convention, meaningful tags, contradiction check against existing entries, and verified searchability.

### Quality Criteria
- [ ] Knowledge stored via `nx store put --collection knowledge`
- [ ] No contradictions with existing entries (checked and resolved)
- [ ] Title follows naming convention (research-*, decision-*, pattern-*, debug-*)
- [ ] Tags are meaningful and consistent with existing tag vocabulary
- [ ] Searchable -- verified with `nx search "topic" --corpus knowledge`
```

For full relay structure and optional fields, see [RELAY_TEMPLATE.md](../agents/_shared/RELAY_TEMPLATE.md).
