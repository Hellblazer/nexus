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
  echo ""
  echo "Use **store_list** tool to list existing indexed collections."
  echo ""

  echo "### Tip"
  echo ""
  echo "Specify PDF paths or a directory. The agent extracts text, chunks content,"
  echo "and indexes into T3 for semantic search via the search tool."
}

## PDFs to Process

$ARGUMENTS

## Action

Invoke the **pdf-processing** skill with the following relay. Fill in dynamic fields from the context above:

```markdown
## Relay: pdf-chromadb-processor

**Task**: Index "$ARGUMENTS" into T3 store for semantic search
**Bead**: [fill from active bead above or 'none']

### Input Artifacts
- Files: [fill from PDF file paths listed above]

### PDFs to Index
$ARGUMENTS

### Deliverable
All specified PDFs extracted, chunked, and indexed in T3 store with metadata preserved (title, author, date) and searchability verified via search tool sample queries.

### Quality Criteria
- [ ] All PDFs processed without errors
- [ ] Text properly extracted with layout preserved
- [ ] Content chunked appropriately for semantic search
- [ ] Metadata (title, author, date) preserved in document records
- [ ] Documents searchable -- verified with search tool sample queries
- [ ] Collection name follows convention (docs__corpus-name)
```

For full relay structure and optional fields, see [RELAY_TEMPLATE.md](../agents/_shared/RELAY_TEMPLATE.md).
