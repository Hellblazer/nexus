---
allowed-tools: Bash
description: Index PDF files into the T3 knowledge store for semantic search
---

# PDF Processing Request

```!
  echo "## Context"
  echo ""
  echo "**Working directory:** $(pwd)"
  echo ""

  echo "### PDF Files in Current Directory"
  find . -name "*.pdf" -not -path "./.git/*" 2>/dev/null | head -20 || echo "No PDF files found"
  echo ""

  echo "### Existing Indexed Collections"
  echo ""
  echo "Use **store_list** tool to list existing indexed collections."
  echo ""

  echo "### Tip"
  echo ""
  echo "Specify PDF paths or a directory. nx index pdf extracts text, chunks content,"
  echo "and indexes into T3 for semantic search via the search tool."
```

## PDFs to Process

$ARGUMENTS

## Action

Run `nx index pdf <file> --collection <name>` for each PDF. For batch processing, use a loop:

```bash
# Short form: t3_collection_name auto-promotes to the conformant
# 4-segment shape knowledge__<corpus>__voyage-context-3__v1.
for f in *.pdf; do nx index pdf "$f" --collection knowledge__<corpus>; done
```

Pass through the user's arguments: $ARGUMENTS
