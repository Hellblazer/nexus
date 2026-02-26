---
description: List all RDRs with status, type, and priority
---

# RDR List

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

  if [ ! -d "$RDR_DIR" ]; then
    echo "> No RDRs found — \`$RDR_DIR\` does not exist in this repo."
    exit 0
  fi

  echo "### RDR Files"
  echo '```'
  ls "$RDR_DIR"/[0-9]*.md 2>/dev/null | xargs -I{} basename {} || echo "No RDR files found"
  echo '```'
  echo ""

  echo "### T2 Records"
  echo '```'
  REPO=$(basename "$(git rev-parse --show-toplevel 2>/dev/null)" 2>/dev/null)
  if [ -n "$REPO" ] && command -v nx &> /dev/null; then
    nx memory list --project "${REPO}_rdr" 2>/dev/null | head -20 || echo "No T2 RDR records"
  else
    echo "T2 not available"
  fi
  echo '```'
}

## Filters

$ARGUMENTS

## Action

Invoke the **rdr-list** skill using the context above.

- RDR directory is the `RDR_DIR` shown above (from `.nexus.yml` `indexing.rdr_paths[0]`)
- Apply any filters from `$ARGUMENTS` (e.g. `--status=draft`, `--type=feature`)
- Merge T2 records with filesystem data (T2 takes precedence for status)
- Warn on drift (T2 record exists but file missing, or vice versa)
