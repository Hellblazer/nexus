# SPDX-License-Identifier: AGPL-3.0-or-later
# Shared helpers for the live validation harness.
#
# Sourcing this file:
#  * exports sandbox paths pointing away from prod
#  * defines streaming-status helpers (ts, pass, fail, step, run)
#  * maintains PASS / FAIL counters for the calling script
#
# Expected env on entry:
#  * SANDBOX     — absolute sandbox directory (created by caller)
#  * ORIG_HOME   — caller's real $HOME (for .gitconfig copy)

set -uo pipefail   # no -e: we need to catch individual failures

# ── Sandbox env ──────────────────────────────────────────────────────────────
export HOME="$SANDBOX"
export NX_LOCAL=1
export NX_LOCAL_CHROMA_PATH="$SANDBOX/.local/share/nexus/chroma"
export NEXUS_CATALOG_PATH="$SANDBOX/.config/nexus/catalog"
mkdir -p "$SANDBOX/.config/nexus" "$SANDBOX/.local/share/nexus" "$SANDBOX/.cache"
cp "${ORIG_HOME:-}/.gitconfig" "$SANDBOX/.gitconfig" 2>/dev/null || true

# ── Counters & logs ──────────────────────────────────────────────────────────
PASS=${PASS:-0}
FAIL=${FAIL:-0}
FAILED_CASES=()

# Timestamp in ISO-ish short form for line-by-line observability
ts()    { date +"%H:%M:%S"; }
step()  { printf "\n[%s] ═══ %s ═══\n" "$(ts)" "$*" >&2; }
info()  { printf "[%s]    %s\n"          "$(ts)" "$*" >&2; }
pass()  { printf "[%s]  ✓ %s\n"          "$(ts)" "$*" >&2; PASS=$((PASS+1)); }
fail()  { printf "[%s]  ✗ %s\n"          "$(ts)" "$*" >&2; FAIL=$((FAIL+1)); FAILED_CASES+=("$*"); }

# run "<label>" <cmd...>  — streams stderr live, asserts exit code 0.
run() {
    local label="$1"; shift
    local start=$(date +%s)
    if "$@" >/dev/null 2>"$SANDBOX/.cache/last.err"; then
        pass "$label ($(( $(date +%s) - start ))s)"
    else
        fail "$label"
        info "  └─ stderr tail:"
        tail -5 "$SANDBOX/.cache/last.err" | sed 's/^/       /' >&2
    fi
}

# summary — call at end of the script
summary() {
    local suite="${1:-suite}"
    printf "\n[%s] ── %s: %d pass, %d fail ──\n" "$(ts)" "$suite" "$PASS" "$FAIL" >&2
    if [[ $FAIL -gt 0 ]]; then
        info "Failed cases:"
        printf '       - %s\n' "${FAILED_CASES[@]}" >&2
    fi
}
