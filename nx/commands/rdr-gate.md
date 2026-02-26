---
description: Run finalization gate on an RDR — structural, assumption audit, and AI critique
---

# RDR Gate

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
    echo "> **Usage**: \`/rdr-gate <id>\` — e.g. \`/rdr-gate 003\`"
    echo ""
    echo "### Available RDRs"
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

  echo "### RDR File: $(basename "$RDR_FILE")"
  echo '```'
  # Show section headings to check structural completeness
  grep '^## ' "$RDR_FILE" 2>/dev/null
  echo '```'
  echo ""

  echo "### T2 Metadata"
  echo '```'
  if command -v nx &> /dev/null && [ -n "$REPO" ]; then
    nx memory get --project "${REPO}_rdr" --title "$RDR_ID" 2>/dev/null || echo "No T2 record"
  else
    echo "T2 not available"
  fi
  echo '```'
  echo ""

  echo "### Research Findings Summary (T2)"
  echo '```'
  if command -v nx &> /dev/null && [ -n "$REPO" ]; then
    nx memory list --project "${REPO}_rdr" 2>/dev/null | grep "^${RDR_ID}-research" || echo "No research findings"
  else
    echo "T2 not available"
  fi
  echo '```'
}

## RDR to Gate

$ARGUMENTS

## Action

Invoke the **rdr-gate** skill using the context above:

- RDR directory is the `RDR_DIR` shown above (from `.nexus.yml` `indexing.rdr_paths[0]`)
- Run all three layers in sequence: structural validation → assumption audit → AI critique
- Use the section headings above for Layer 1 pre-check (completeness check)
- Use the T2 research findings above for Layer 2 assumption audit
- Layer 3 dispatches the `substantive-critic` agent via Task tool
