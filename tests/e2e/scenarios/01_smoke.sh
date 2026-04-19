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

# API key presence (keys are set as env vars in container). Only
# meaningful in cloud mode — ``nx doctor`` in local mode emits
# "T3 mode: local (no API keys needed)" and deliberately omits the
# Voyage / ChromaDB lines since they aren't needed. Skip the key
# assertions when NX_LOCAL=1 so the harness's "not production"
# default doesn't trip them.
if [[ "${NX_LOCAL:-0}" == "1" ]]; then
    assert_cmd "nx doctor: local T3 mode reported" \
        "nx doctor 2>&1" \
        "T3 mode: local"
else
    if [[ -n "${VOYAGE_API_KEY:-}" ]]; then
        assert_cmd "nx doctor: Voyage AI key present" \
            "nx doctor 2>&1" \
            "Voyage AI.*set|✓.*Voyage"
    fi

    if [[ -n "${CHROMA_API_KEY:-}" ]]; then
        assert_cmd "nx doctor: ChromaDB keys present" \
            "nx doctor 2>&1" \
            "ChromaDB.*set|✓.*ChromaDB"
    fi
fi

scenario_end

# Plugin-load verification lives in scenario 00 (debug-load), which
# checks isolation, plugin manifest, hooks, on-disk skills layout, and
# agents + commands visibility. Duplicating the check here via a direct
# yes/no print-mode query was flaky — Claude's answer to a yes/no depends
# on how hard it parses the system prompt before deciding, and the
# phrasing matters more than the actual plugin state. Trust scenario 00.
