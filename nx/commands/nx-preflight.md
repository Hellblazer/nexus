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
    echo "Install: uv tool install conexus  OR  pip install conexus"
    echo "Docs: https://github.com/Hellblazer/nexus"
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
  SUPERPOWERS_DIR=$(find "$PLUGINS_CACHE" -maxdepth 3 -type d -name "superpowers" 2>/dev/null | head -1)
  if [ -n "$SUPERPOWERS_DIR" ]; then
    echo "Status: PASS"
    echo "superpowers found: $SUPERPOWERS_DIR"
  else
    echo "Status: FAIL"
    echo "superpowers plugin not found under $PLUGINS_CACHE"
    echo "Install: /plugin marketplace add anthropics/claude-plugins-official"
  fi
  echo ""

  # ── 5. uv (package manager) ──────────────────────────────────────────────────
  echo "### 5. uv (package manager)"
  echo ""
  if command -v uv &>/dev/null; then
    UV_VERSION=$(uv --version 2>&1)
    echo "Status: PASS"
    echo "Version: $UV_VERSION"
  else
    echo "Status: WARN"
    echo "uv not found — nx can be installed with pip instead, but uv is recommended"
    echo "Install: curl -LsSf https://astral.sh/uv/install.sh | sh"
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
| uv | — | — |

Fill in each row from the check results above. Use "PASS", "FAIL", or "WARN" for Status. Leave Action needed blank for passing checks; for failures/warnings, provide the install command or link.

If all checks pass: print "nx plugin is ready"
If any check fails: print "Fix the above before using nx agents"
