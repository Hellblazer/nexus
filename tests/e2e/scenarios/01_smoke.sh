#!/usr/bin/env bash
# Scenario 01: Smoke tests — nx CLI, credentials, plugin load

scenario "01 smoke: nx CLI and doctor"

# nx --version
assert_cmd "nx --version reports a version" \
    "nx --version" \
    "nx, version"

# nx doctor — check structural items (Python, rg, git always present in container)
assert_cmd "nx doctor: Python >= 3.12" \
    "nx doctor 2>&1" \
    "Python ≥ 3.12"

assert_cmd "nx doctor: ripgrep found" \
    "nx doctor 2>&1" \
    "ripgrep.*rg"

assert_cmd "nx doctor: git found" \
    "nx doctor 2>&1" \
    "git"

# API key presence (keys are set as env vars in container)
if [[ -n "${VOYAGE_API_KEY:-}" ]]; then
    assert_cmd "nx doctor: Voyage AI key present" \
        "nx doctor 2>&1" \
        "Voyage AI.*✓|voyage.*ok"
fi

if [[ -n "${CHROMA_API_KEY:-}" ]]; then
    assert_cmd "nx doctor: ChromaDB keys present" \
        "nx doctor 2>&1" \
        "ChromaDB.*✓|chroma.*ok"
fi

scenario_end

# ─── Plugin load check ───────────────────────────────────────────────────────

scenario "01 smoke: plugin loaded in Claude"

# Use claude -p (print mode) to check if nx skills are available.
# Print mode loads plugins from ~/.claude just like interactive mode.
echo "    Asking Claude to list nx skills (print mode)..."
plugin_check=$(crun "claude --dangerously-skip-permissions -p 'List the names of all skills provided by the nx plugin. Just the names, one per line.' 2>&1" || true)

if echo "$plugin_check" | grep -qiE "nexus|sequential.thinking|rdr|brainstorm"; then
    pass "nx plugin skills visible to Claude"
else
    fail "nx plugin skills not visible — output: $plugin_check"
fi

scenario_end
