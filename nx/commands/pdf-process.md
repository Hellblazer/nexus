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

Invoke the **pdf-processing** skill. It will determine the right approach:
- **Single PDF**: Run `nx index pdf` directly (no agent needed)
- **Multiple PDFs or complex scenarios**: Dispatch **pdf-chromadb-processor** agent

Pass through the user's arguments: $ARGUMENTS
