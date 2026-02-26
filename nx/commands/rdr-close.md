---
description: Close an RDR with optional post-mortem, bead decomposition, and T3 archival
---

# RDR Close

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

  RDR_ID=$(echo "${ARGUMENTS:-}" | grep -o '[0-9]\+' | head -1)
  REPO=$(basename "$(git rev-parse --show-toplevel 2>/dev/null)" 2>/dev/null)

  echo "**RDR directory:** \`$RDR_DIR\`"
  echo ""

  if [ -z "$RDR_ID" ]; then
    echo "> **Usage**: \`/rdr-close <id> [--reason implemented|reverted|abandoned|superseded]\`"
    echo ""
    echo "### Open/Draft RDRs"
    echo '```'
    ls "$RDR_DIR"/[0-9]*.md 2>/dev/null | xargs -I{} basename {} || echo "No RDRs found"
    echo '```'
    exit 0
  fi

  RDR_FILE=$(ls "$RDR_DIR"/${RDR_ID}-*.md 2>/dev/null | head -1)
  if [ -z "$RDR_FILE" ]; then
    echo "> RDR $RDR_ID not found in \`$RDR_DIR\`"
    exit 0
  fi

  echo "### RDR: $(basename "$RDR_FILE")"
  echo ""

  echo "### T2 Metadata (current status)"
  echo '```'
  if command -v nx &> /dev/null && [ -n "$REPO" ]; then
    nx memory get --project "${REPO}_rdr" --title "$RDR_ID" 2>/dev/null || echo "No T2 record — will use file metadata"
  else
    echo "T2 not available"
  fi
  echo '```'
  echo ""

  echo "### Implementation Plan (for bead decomposition)"
  echo '```'
  awk '/^## Implementation Plan/,/^## [^I]/' "$RDR_FILE" 2>/dev/null | head -40 || echo "No Implementation Plan section found"
  echo '```'
  echo ""

  echo "### Active Beads"
  echo '```'
  if command -v bd &> /dev/null; then
    bd list --status=in_progress --limit=5 2>/dev/null || echo "No in-progress beads"
  else
    echo "Beads not available"
  fi
  echo '```'
}

## RDR to Close

$ARGUMENTS

## Action

Invoke the **rdr-close** skill using the context above:

- RDR directory is the `RDR_DIR` shown above (from `.nexus.yml` `indexing.rdr_paths[0]`)
- Parse RDR ID and close reason from `$ARGUMENTS`
- Pre-check: warn if status is not "Final" and reason is "Implemented"
- For Implemented: ask about divergences, optionally create post-mortem, decompose into beads
- For Reverted/Abandoned: offer post-mortem, no beads
- For Superseded: prompt for superseding RDR ID, cross-link both
- Post-mortem archive location: `$RDR_DIR/post-mortem/NNN-kebab-title.md`
