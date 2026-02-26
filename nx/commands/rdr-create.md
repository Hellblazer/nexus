---
description: Create a new RDR — scaffold from template, assign sequential ID, register in T2
---

# New RDR

!{
  # Detect RDR directory from .nexus.yml (fallback: docs/rdr)
  RDR_DIR=$(python3 -c "
import os, re, sys
f = '.nexus.yml'
if not os.path.exists(f): print('docs/rdr'); sys.exit()
t = open(f).read()
try:
    import yaml; d = yaml.safe_load(t) or {}; paths = (d.get('indexing') or {}).get('rdr_paths', ['docs/rdr']); print(paths[0] if paths else 'docs/rdr')
except ImportError:
    m = re.search(r'rdr_paths[^\[]*\[([^\]]+)\]', t) or re.search(r'rdr_paths:\s*\n\s+-\s*(.+)', t)
    v = m.group(1) if m else ''; parts = re.findall(r'[a-z][a-z0-9/_-]+', v)
    print(parts[0] if parts else 'docs/rdr')
" 2>/dev/null || echo "docs/rdr")

  echo "**RDR directory:** \`$RDR_DIR\`"
  echo ""

  # Existing RDRs (for ID assignment)
  echo "### Existing RDRs"
  echo '```'
  if [ -d "$RDR_DIR" ]; then
    ls "$RDR_DIR"/[0-9]*.md 2>/dev/null | xargs -I{} basename {} || echo "None — this will be the first RDR"
  else
    echo "Directory does not exist — bootstrap required"
  fi
  echo '```'
  echo ""

  # Project prefix
  PROJECT_PREFIX=$(basename "$(git rev-parse --show-toplevel 2>/dev/null)" | tr '[:lower:]' '[:upper:]' | tr -cd '[:alnum:]' | head -c 3)
  echo "**Project prefix:** \`${PROJECT_PREFIX:-???}\`"
  echo ""

  # Active beads (for "Related issues" field)
  echo "### Active Beads (for Related Issues field)"
  echo '```'
  if command -v bd &> /dev/null; then
    bd list --status=in_progress --limit=5 2>/dev/null || echo "No in-progress beads"
  else
    echo "Beads not available"
  fi
  echo '```'
  echo ""

  # PM context
  echo "### PM Context"
  if command -v nx &> /dev/null; then
    nx pm status 2>/dev/null || echo "No PM initialized"
  fi
}

## Title / Details

$ARGUMENTS

## Action

Invoke the **rdr-create** skill using the context above:

- RDR directory is the `RDR_DIR` shown above (from `.nexus.yml` `indexing.rdr_paths[0]`)
- Use the existing RDR list to determine the next sequential ID
- Use the project prefix shown above
- If `$ARGUMENTS` contains a title, pre-fill it (otherwise prompt)
- If RDR directory doesn't exist, run bootstrap (create dir, copy templates from `$CLAUDE_PLUGIN_ROOT/resources/rdr/`)
