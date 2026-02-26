---
description: Show detailed information about a specific RDR including content, research findings, and linked beads
---

# RDR Show

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

  # Determine RDR ID from arguments
  RDR_ID="${ARGUMENTS:-}"
  RDR_NUM=$(echo "$RDR_ID" | grep -o '[0-9]\+' | head -1)

  echo "**RDR directory:** \`$RDR_DIR\`"
  echo ""

  if [ ! -d "$RDR_DIR" ]; then
    echo "> No RDRs found — \`$RDR_DIR\` does not exist in this repo."
    exit 0
  fi

  if [ -n "$RDR_NUM" ]; then
    # Find the specific RDR file
    RDR_FILE=$(ls "$RDR_DIR"/${RDR_NUM}-*.md 2>/dev/null | head -1)
    if [ -n "$RDR_FILE" ]; then
      echo "### RDR File: $(basename "$RDR_FILE")"
      echo '```'
      head -80 "$RDR_FILE"
      echo '```'
      echo ""
    else
      echo "> RDR $RDR_NUM not found in \`$RDR_DIR\`"
      echo ""
    fi
  fi

  echo "### T2 Metadata"
  echo '```'
  REPO=$(basename "$(git rev-parse --show-toplevel 2>/dev/null)" 2>/dev/null)
  if [ -n "$REPO" ] && [ -n "$RDR_NUM" ] && command -v nx &> /dev/null; then
    nx memory get --project "${REPO}_rdr" --title "$RDR_NUM" 2>/dev/null || echo "No T2 record for RDR $RDR_NUM"
    echo ""
    echo "# Research findings:"
    nx memory list --project "${REPO}_rdr" 2>/dev/null | grep "^${RDR_NUM}-research" | head -20 || echo "No research findings"
  else
    echo "T2 not available or no RDR ID specified"
  fi
  echo '```'

  if [ -n "$RDR_NUM" ]; then
    echo "### Linked Beads"
    echo '```'
    if command -v bd &> /dev/null; then
      # Show in-progress and open beads referencing this RDR number
      bd list --status=open --limit=10 2>/dev/null | grep -i "rdr.*$RDR_NUM\|$RDR_NUM.*rdr" || echo "No beads linked (check epic_bead in T2)"
    else
      echo "Beads not available"
    fi
    echo '```'
  fi
}

## RDR to Show

$ARGUMENTS

## Action

Invoke the **rdr-show** skill using the context above.

- RDR directory is the `RDR_DIR` shown above (from `.nexus.yml` `indexing.rdr_paths[0]`)
- RDR ID is parsed from `$ARGUMENTS` (e.g. `003` or `NX-003`)
- If no ID given, default to the most recently modified RDR in the directory
- Display: metadata, research findings by classification, linked beads, supersedes/superseded-by
- If ID not found, fallback to rdr-list behavior
