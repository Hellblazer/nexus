---
description: Index PDF files into nx store for semantic search using pdf-chromadb-processor agent
---

# PDF Processing Request

!{
  echo "## Context"
  echo ""
  echo "**Working directory:** $(pwd)"
  echo ""

  echo "### PDF Files in Current Directory"
  echo '```'
  find . -name "*.pdf" -not -path "./.git/*" 2>/dev/null | head -20 || echo "No PDF files found"
  echo '```'
  echo ""

  echo "### Existing Indexed Collections"
  echo '```'
  if command -v nx &> /dev/null; then
    nx store list 2>/dev/null | head -10 || echo "No collections found"
  else
    echo "nx not available"
  fi
  echo '```'
  echo ""

  echo "### Tip"
  echo ""
  echo "Specify PDF paths or a directory. The agent extracts text, chunks content,"
  echo "and indexes into nx store T3 for semantic search via 'nx search'."
}

## PDFs to Process

$ARGUMENTS

## Relay Instructions

Use the **Task tool** to delegate to pdf-chromadb-processor:

```markdown
## Relay: pdf-chromadb-processor

**Task**: Index "$ARGUMENTS" into nx store T3 for semantic search
**Bead**: [Create bead if processing significant documentation set or 'none']

### Input Artifacts
- nx store: [Check for existing indexed versions of these PDFs]
- nx memory: [project/title path or 'none']
- Files: [PDF file paths from above]

### PDFs to Index
$ARGUMENTS

### Deliverable
All specified PDFs indexed in nx store with extracted text, metadata preserved, and verified searchability

### Quality Criteria
- [ ] All PDFs processed without errors
- [ ] Text properly extracted with layout preserved
- [ ] Content chunked appropriately for semantic search
- [ ] Metadata (title, author, date) preserved
- [ ] Documents searchable — verified with `nx search` sample queries
- [ ] Collection name follows convention (docs__corpus-name)
```
