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

  # ── 4. uv (package manager) ──────────────────────────────────────────────────
  echo "### 4. uv (package manager)"
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

  # ── 5. Node.js / npx (plugin MCP servers) ───────────────────────────────────
  echo "### 5. Node.js / npx (required by plugin MCP servers)"
  echo ""
  if command -v npx &>/dev/null; then
    NODE_VERSION=$(node --version 2>&1)
    NPX_PATH=$(command -v npx)
    echo "Status: PASS"
    echo "Node: $NODE_VERSION"
    echo "npx:  $NPX_PATH"
  else
    echo "Status: FAIL"
    echo "npx not found in PATH — the plugin's sequential-thinking and context7 MCP"
    echo "servers are spawned via 'npx -y …' and will silently fail to start."
    echo "Install:"
    echo "  brew install node                         (macOS)"
    echo "  apt install nodejs npm                    (Ubuntu/Debian)"
    echo "  https://nodejs.org/                       (other platforms)"
  fi
  echo ""

  # ── 6. CLAUDE.md Agent Readiness ────────────────────────────────────────────
  echo "### 6. CLAUDE.md Agent Readiness"
  echo ""
  if [ -f "CLAUDE.md" ]; then
    echo "[x] CLAUDE.md exists"
    # Language detection (case-insensitive)
    LANG_MATCH=$(grep -iE "Python|Java|Go|Rust|TypeScript|Node\.js|C\+\+|C#|Ruby|Kotlin|Swift|Scala" CLAUDE.md | head -1)
    if [ -n "$LANG_MATCH" ]; then
      echo "[x] Language detected: $(echo "$LANG_MATCH" | head -c 60)"
    else
      echo "[?] Language: not found (optional — agents can detect from build files)"
    fi
    # Build system detection
    BUILD_MATCH=$(grep -iE "uv|maven|mvn|cargo|go build|go mod|npm|yarn|pnpm|gradle|make|cmake" CLAUDE.md | head -1)
    if [ -n "$BUILD_MATCH" ]; then
      echo "[x] Build system detected: $(echo "$BUILD_MATCH" | head -c 60)"
    else
      echo "[?] Build system: not found (optional)"
    fi
    # Test command detection
    TEST_MATCH=$(grep -iE "pytest|mvn test|go test|cargo test|npm test|jest|vitest|make test|uv run pytest" CLAUDE.md | head -1)
    if [ -n "$TEST_MATCH" ]; then
      echo "[x] Test command detected: $(echo "$TEST_MATCH" | head -c 60)"
    else
      echo "[?] Test command: not found (optional)"
    fi
    echo ""
    echo "Status: PASS (CLAUDE.md present)"
  else
    echo "[ ] CLAUDE.md not found"
    echo ""
    echo "Status: WARN"
    echo "Agents work best when CLAUDE.md specifies language, build system, and test command."
    echo "See: https://docs.anthropic.com/en/docs/claude-code/memory#claudemd"
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
| uv | — | — |
| Node.js / npx | — | — |
| CLAUDE.md | — | — |

Fill in each row from the check results above. Use "PASS", "FAIL", or "WARN" for Status. Leave Action needed blank for passing checks; for failures/warnings, provide the install command or link.

If all checks pass: print "nx plugin is ready"
If any check fails: print "Fix the above before using nx agents"
