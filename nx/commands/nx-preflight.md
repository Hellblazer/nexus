---
description: Check that all nx plugin dependencies are correctly installed and configured
disable-model-invocation: false
---

!{
  echo "## nx Plugin Preflight Check"
  echo ""

  # ── 1. nx CLI ────────────────────────────────────────────────────────────────
  echo "### 1. nx CLI"
  echo ""
  if command -v nx &>/dev/null; then
    NX_VERSION=$(nx --version 2>&1)
    echo "Status: PASS"
    echo "Version: $NX_VERSION"
  else
    echo "Status: FAIL"
    echo "nx not found in PATH"
    echo "Install: see docs/getting-started.md"
  fi
  echo ""

  # ── 2. nx configuration ──────────────────────────────────────────────────────
  echo "### 2. nx configuration (nx doctor)"
  echo ""
  if command -v nx &>/dev/null; then
    NX_DOCTOR=$(nx doctor 2>&1)
    NX_DOCTOR_EXIT=$?
    echo "$NX_DOCTOR"
    echo ""
    if [ $NX_DOCTOR_EXIT -eq 0 ]; then
      echo "Status: PASS"
    else
      echo "Status: FAIL — run 'nx doctor' for details"
    fi
  else
    echo "Status: SKIP — nx not installed"
  fi
  echo ""

  # ── 3. bd (Beads) ────────────────────────────────────────────────────────────
  echo "### 3. bd (Beads) CLI"
  echo ""
  if command -v bd &>/dev/null; then
    BD_VERSION=$(bd --version 2>&1)
    echo "Status: PASS"
    echo "Version: $BD_VERSION"
  else
    echo "Status: FAIL"
    echo "bd not found in PATH"
    echo "Install: https://github.com/BeadsProject/beads"
  fi
  echo ""

  # ── 4. superpowers plugin ────────────────────────────────────────────────────
  echo "### 4. superpowers plugin"
  echo ""
  PLUGINS_CACHE="${HOME}/.claude/plugins/cache"
  if [ -d "$PLUGINS_CACHE" ] && ls "$PLUGINS_CACHE" 2>/dev/null | grep -qi superpowers; then
    echo "Status: PASS"
    echo "superpowers directory found in $PLUGINS_CACHE"
  else
    echo "Status: FAIL"
    echo "superpowers not found in $PLUGINS_CACHE"
    echo "Install: /plugin marketplace add anthropics/claude-plugins-official"
  fi
  echo ""
}

## Summary

Based on the preflight output above, produce a summary table:

| Dependency | Status | Action needed |
|-----------|--------|---------------|
| nx CLI | — | — |
| nx doctor | — | — |
| bd (beads) | — | — |
| superpowers | — | — |

Fill in each row from the check results above. Use "PASS" or "FAIL" for Status. Leave Action needed blank for passing checks; for failures, provide the install command or link.

If all checks pass: print "nx plugin is ready"
If any check fails: print "Fix the above before using nx agents"
