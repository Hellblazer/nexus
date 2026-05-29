---
allowed-tools: Bash
description: Index PDF files into the T3 knowledge store for semantic search
---

# PDF Processing Request

!`nx command-context pdf-process`

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
