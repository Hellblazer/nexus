---
description: Add, track, or verify structured research findings for an active RDR
---

# RDR Research

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

  # Parse RDR ID from arguments (e.g. "add 003", "status 003", "verify 003 2")
  RDR_ID=$(echo "${ARGUMENTS:-}" | grep -o '[0-9]\+' | head -1)

  echo "**RDR directory:** \`$RDR_DIR\`"
  echo ""

  REPO=$(basename "$(git rev-parse --show-toplevel 2>/dev/null)" 2>/dev/null)

  if [ -n "$RDR_ID" ]; then
    # Show current RDR metadata
    RDR_FILE=$(ls "$RDR_DIR"/${RDR_ID}-*.md 2>/dev/null | head -1)
    if [ -n "$RDR_FILE" ]; then
      echo "### RDR $RDR_ID: $(basename "$RDR_FILE" .md | sed 's/^[0-9]*-//')"
      echo '```'
      # Show just the Research Findings section
      awk '/^## Research Findings/,/^## [^R]/' "$RDR_FILE" 2>/dev/null | head -30 || head -20 "$RDR_FILE"
      echo '```'
      echo ""
    fi

    # Show existing research findings from T2
    echo "### Existing Research Findings (T2)"
    echo '```'
    if command -v nx &> /dev/null && [ -n "$REPO" ]; then
      nx memory list --project "${REPO}_rdr" 2>/dev/null | grep "^${RDR_ID}-research" || echo "No research findings recorded yet"
    else
      echo "T2 not available"
    fi
    echo '```'
    echo ""
  else
    # No RDR ID — show available RDRs
    echo "### Available RDRs"
    echo '```'
    if [ -d "$RDR_DIR" ]; then
      ls "$RDR_DIR"/[0-9]*.md 2>/dev/null | xargs -I{} basename {} || echo "No RDRs found"
    else
      echo "No RDR directory found at $RDR_DIR"
    fi
    echo '```'
  fi
}

## Subcommand and Arguments

$ARGUMENTS

## Action

Invoke the **rdr-research** skill using the context above:

- RDR directory is the `RDR_DIR` shown above (from `.nexus.yml` `indexing.rdr_paths[0]`)
- Subcommands: `add <id>`, `status <id>`, `verify <id> <seq>`
- Parse subcommand and RDR ID from `$ARGUMENTS`
- Existing T2 findings are pre-loaded above for the identified RDR
- Dispatch `codebase-deep-analyzer` or `deep-research-synthesizer` if investigation (not just recording) is requested
