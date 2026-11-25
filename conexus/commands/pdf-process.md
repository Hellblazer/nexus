---
allowed-tools: Bash
description: Index PDF files into the T3 knowledge store for semantic search
---

# PDF Processing Request

!`nx command-context pdf-process`

## PDFs to Process

$ARGUMENTS

## Action

Run `nx index pdf <file> --collection <subject>` for each PDF. `<subject>` is the bare subject
the papers belong to (`distributed-systems`), never a model token or a `knowledge__...` name: reuse an
existing subject when one fits (`nx collection list`), and the catalog names the collection
([collection rules](https://github.com/Hellblazer/nexus/blob/main/docs/collections.md)). For batch processing, use a loop:

```bash
for f in *.pdf; do nx index pdf "$f" --collection <subject>; done
```

Pass through the user's arguments: $ARGUMENTS
