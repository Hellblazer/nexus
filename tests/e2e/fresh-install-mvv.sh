#!/usr/bin/env bash
# nexus-nolqs: FRESH-INSTALL MVV — the virgin-journey release gate.
#
# Why this exists (release-process root cause, 2026-07-21): every other E2E
# release gate (rehearsal, era-hop, guided MVV, package-upgrade convergence)
# starts from a POPULATED install and tests the UPGRADE axis; the unit suite
# pins the SQLite opt-out backend; and the fresh-box bugs were silent
# best-effort skips — so an entire defect class (f1itv/e9ru2/kmo9h/r5f3c/
# 9xfx5: lost catalog registrations, divergent local substrates, voyage-env
# mode flips, vacuous pending rungs) shipped through a heavyweight release
# process untouched. This gate is the missing axis: PROVE the full data
# journey on a virgin box with the wheel under test.
#
# Journey: install (wheel or PyPI, see below) -> isolated venv + scrubbed
# env + virgin HOME -> NX_LOCAL init (engine download + portable PG +
# bge-768) -> ladder converged at init -> store put (catalog row asserted,
# manifest warning absent) -> index md (catalog row asserted) -> semantic
# search returns both -> doctor has zero X and zero non-allowlisted
# warnings.
#
# TWO INSTALL LAYERS (nexus-796zn; T2 nexus/shakedown-playbook SS2 S1 GAP):
#   (default)          builds the wheel from THIS checkout and `uv pip
#                       install`s it into an isolated venv. This is the
#                       release-battery layer: "does the tree under test
#                       work end to end." Dependency versions resolve from
#                       whatever `uv.lock`/the wheel's own metadata pins —
#                       it does NOT reproduce a fresh `uv tool install`'s
#                       independent dependency resolution.
#   --published [X.Y.Z] installs the PUBLISHED artifact from PyPI via
#                       `uv tool install conexus[==X.Y.Z]` — the exact
#                       layer that killed nexus-l2ku5 (mcp>=1.0 unbounded
#                       resolved mcp 2.0.0 on fresh uv-tool installs,
#                       killing both MCP servers for 4 days; every OTHER
#                       gate ran pinned to the dev venv's uv.lock and saw
#                       nothing). This is the SHAKEDOWN layer: post-publish
#                       (T1) or ad hoc verification of what PyPI is
#                       actually serving. Omitted version = latest.
#
# Both layers run the SAME 9 journey legs after install; only the install
# step (and the mode-aware bits called out inline) differs. The final
# PASSED line always names which layer ran, so a transcript can never be
# mistaken for the other (a shakedown that silently ran the local-wheel
# layer would be a vacuous S1 leg — see the playbook's honest-recording
# concern).
#
# Self-provisioning (feedback_gates_scripted_not_ambient): everything it
# needs it builds; nothing ambient is trusted. Env is allowlist-scrubbed —
# an exported VOYAGE_API_KEY must NOT reach the journey (the r5f3c lesson).
# --published never touches the live uv tool install: HOME is redirected to
# the sandbox BEFORE `uv tool install` runs, so `uv tool dir` (which is
# purely HOME-derived — see scripts/reinstall-tool.sh's identical pattern)
# resolves entirely inside the sandbox. Failure is always loud: an
# unreachable PyPI, a version that does not exist, or a resolver failure
# hard-fails with a named cause — there is no skip-pass path (a skip-pass
# here would be vacuous exactly where nexus-l2ku5 lived).
#
# Cost: ~2-4 min warm (engine binary + PG bundle + bge ONNX are cached under
# the sandbox only for the run; cold downloads dominate the first run).
# --published re-resolves the full dependency set from PyPI every run (no
# ambient uv cache is trusted either) and so is typically slower than the
# default layer.
#
# Usage: tests/e2e/fresh-install-mvv.sh [--published [VERSION]]
#   (no args)          install the LOCAL wheel built from this checkout
#                       (release-battery layer, default)
#   --published        install the LATEST published conexus from PyPI
#   --published X.Y.Z  install that EXACT published version from PyPI
# Exit 0 == FRESH-INSTALL MVV PASSED (the literal sentinel on the last line).
set -euo pipefail

PUBLISHED_MODE=0
PUBLISHED_VERSION=""
while [ $# -gt 0 ]; do
    case "$1" in
        --published)
            PUBLISHED_MODE=1
            shift
            if [ $# -gt 0 ] && [[ "$1" != -* ]]; then
                PUBLISHED_VERSION="$1"
                shift
            fi
            ;;
        -h|--help)
            echo "Usage: $0 [--published [VERSION]]"
            echo "  (no args)          install the LOCAL wheel built from this checkout (default)"
            echo "  --published        install the latest PUBLISHED conexus from PyPI"
            echo "  --published X.Y.Z  install that exact PUBLISHED version from PyPI"
            exit 0
            ;;
        *)
            echo "unknown argument: $1" >&2
            exit 2
            ;;
    esac
done

if [ "$PUBLISHED_MODE" = 1 ]; then
    if [ -n "$PUBLISHED_VERSION" ]; then
        PKG_SPEC="conexus==$PUBLISHED_VERSION"
    else
        PKG_SPEC="conexus"
    fi
    echo "================================================================"
    echo " FRESH-INSTALL MVV — PUBLISHED artifact (uv-tool resolution layer)"
    echo " installing: $PKG_SPEC  (from PyPI, NOT this checkout)"
    echo "================================================================"
else
    echo "================================================================"
    echo " FRESH-INSTALL MVV — LOCAL WHEEL (release-battery layer, default)"
    echo "================================================================"
fi

REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)"
WORK="$(mktemp -d /tmp/fresh-mvv-XXXXXX)"
HOME_DIR="$WORK/home"
VENV="$WORK/venv"
LOGS="$WORK/logs"
mkdir -p "$HOME_DIR" "$LOGS"

# BIN_DIR: directory holding the installed `nx` / `nx-mcp` / `nx-mcp-catalog`
# console scripts (used by _nx()'s PATH and the MCP-handshake leg).
# PROBE_PYTHON: the interpreter of the venv the journey actually installed
# into (used for sysconfig/dist-info introspection). Both are mode-dependent
# and set once the install step completes.
BIN_DIR=""
PROBE_PYTHON=""

# Optional download cache (CI cost discipline): seed the virgin HOME's
# ~/.cache/nexus (the 416MB bge ONNX) from FRESH_MVV_CACHE and save it back
# on success. The engine binary + PG bundle are still downloaded + verified
# fresh every run (that verification IS part of the journey under test).
if [ -n "${FRESH_MVV_CACHE:-}" ] && [ -d "$FRESH_MVV_CACHE/nexus" ]; then
    mkdir -p "$HOME_DIR/.cache"
    cp -R "$FRESH_MVV_CACHE/nexus" "$HOME_DIR/.cache/nexus"
    echo "  (seeded model cache from $FRESH_MVV_CACHE)"
fi

GATE_OK=0
_fail() { echo "FRESH-INSTALL MVV FAILED: $*" >&2; exit 1; }

cleanup() {
    # Stop the sandbox service + PG so nothing leaks past the gate.
    if [ -n "$BIN_DIR" ] && [ -x "$BIN_DIR/nx" ]; then
        _nx daemon service stop --with-pg >/dev/null 2>&1 || true
    fi
    if [ "$GATE_OK" = 1 ]; then
        rm -rf "$WORK"
    else
        echo "FAILURE EVIDENCE PRESERVED: $LOGS (home: $HOME_DIR)" >&2
    fi
}
trap cleanup EXIT

# ── env allowlist: the ONLY ambient state the journey may see ───────────────
# Deliberately absent: VOYAGE_API_KEY (r5f3c — an ambient key must not flip
# the engine voyage-only), NX_* steering vars, XDG overrides.
_nx() {
    env -i \
        HOME="$HOME_DIR" \
        PATH="$BIN_DIR:/usr/bin:/bin" \
        TERM="${TERM:-dumb}" \
        NX_LOCAL=1 \
        ${HTTPS_PROXY:+HTTPS_PROXY="$HTTPS_PROXY"} \
        ${HTTP_PROXY:+HTTP_PROXY="$HTTP_PROXY"} \
        "$BIN_DIR/nx" "$@"
}

# nexus-enfoh CRITICAL (code-review-expert + substantive-critic, empirically
# verified 2026-08-05): a bare `env HOME=... uv ...` invocation does NOT
# clear the rest of the ambient environment — it only sets/overrides HOME
# while everything else, including UV_TOOL_DIR, still leaks through. An
# operator with UV_TOOL_DIR exported (or UV_TOOL_BIN_DIR, XDG_DATA_HOME,
# XDG_BIN_HOME — any of which can independently override the HOME-derived
# tool location) would have this step install straight into their LIVE
# conexus tool venv, exactly the class this whole sandbox exists to prevent
# (feedback_dont_break_live_nexus_install). Fixed with the SAME `env -i`
# allowlist pattern `_nx()` already uses above, not the earlier "just set
# HOME" shape. Ambient `PATH` (not the sandboxed BIN_DIR one) is
# deliberately kept — this step needs to find the real `uv` binary and any
# system Python — but everything UV_*/XDG_*/PIP_* is scrubbed, INCLUDING
# UV_INDEX_URL/UV_DEFAULT_INDEX/UV_EXTRA_INDEX_URL/PIP_INDEX_URL/
# PIP_EXTRA_INDEX_URL: this is a RESOLUTION-layer test (nexus-l2ku5) and
# must resolve against the REAL PyPI, never an operator's configured mirror
# or proxy index. scripts/reinstall-tool.sh's `uv tool dir` pattern is NOT
# an equivalent precedent to copy here — that script deliberately targets
# the LIVE ambient install (that is its entire job); this script's job is
# the opposite; the two must not share an env-handling shape.
_uv_sandboxed() {
    env -i \
        HOME="$HOME_DIR" \
        PATH="$PATH" \
        TERM="${TERM:-dumb}" \
        ${HTTPS_PROXY:+HTTPS_PROXY="$HTTPS_PROXY"} \
        ${HTTP_PROXY:+HTTP_PROXY="$HTTP_PROXY"} \
        uv "$@"
}

if [ "$PUBLISHED_MODE" = 1 ]; then
    echo "── 1/9 Install PUBLISHED artifact from PyPI (uv-tool resolution layer) ──"
    # nexus-796zn / T2 nexus/shakedown-playbook SS2 S1 GAP: HOME is
    # redirected to the sandbox BEFORE `uv tool install` runs, via
    # _uv_sandboxed()'s env -i allowlist (see its definition above for why
    # a plain `env HOME=...` was insufficient — nexus-enfoh). `uv tool dir`
    # (and `--bin`) are purely HOME-derived (verified against this uv
    # release) once UV_TOOL_DIR/UV_TOOL_BIN_DIR/XDG_* are out of the way, so
    # the tool venv and its exposed console scripts land entirely inside
    # $HOME_DIR/.local/{share/uv/tools,bin} and NEVER touch the operator's
    # live ~/.local/share/uv or ~/.local/bin.
    _uv_sandboxed tool install --python 3.12 "$PKG_SPEC" \
        >"$LOGS/install.log" 2>&1 \
        || _fail "uv tool install $PKG_SPEC failed (see $LOGS/install.log) — network unreachable, version not published on PyPI, or dependency resolution failed at the uv-tool layer (the nexus-l2ku5 layer); no skip-pass permitted"
    TOOL_DIR="$(_uv_sandboxed tool dir)"
    TOOL_BIN_DIR="$(_uv_sandboxed tool dir --bin)"
    TOOL_VENV="$TOOL_DIR/conexus"
    [ -d "$TOOL_VENV" ] \
        || _fail "uv tool install reported success but $TOOL_VENV does not exist"
    BIN_DIR="$TOOL_BIN_DIR"
    PROBE_PYTHON="$TOOL_VENV/bin/python"
    [ -x "$BIN_DIR/nx" ] \
        || _fail "uv tool install did not expose $BIN_DIR/nx"
    [ -x "$PROBE_PYTHON" ] \
        || _fail "uv tool install did not produce a python interpreter at $PROBE_PYTHON"
    echo "  installed into (sandbox): $TOOL_VENV"
    echo "  console scripts at (sandbox): $BIN_DIR"

    echo "── 2/9 Confirm the installed version resolved from PyPI ──"
    _nx --version
else
    echo "── 1/9 Build the wheel under test ──"
    ( cd "$REPO_ROOT" && uv build --wheel -o "$WORK/dist" ) >"$LOGS/build.log" 2>&1 \
        || _fail "wheel build failed (see $LOGS/build.log)"
    WHEEL="$(ls "$WORK"/dist/conexus-*.whl)"
    echo "  $WHEEL"

    echo "── 2/9 Virgin venv + install ──"
    uv venv --python 3.12 -q "$VENV"
    uv pip install -q --python "$VENV/bin/python" "$WHEEL"
    BIN_DIR="$VENV/bin"
    PROBE_PYTHON="$VENV/bin/python"
    _nx --version
fi

echo "── 3/9 MCP entry-point handshake (nexus-l2ku5 layer test) ──"
# nexus-l2ku5: mcp 2.0.0 (2026-07-28) removed mcp.server.fastmcp; the
# unbounded `mcp>=1.0` floor let it into every fresh install for 4 days —
# both nx-mcp and nx-mcp-catalog died at import with ZERO signal (Claude
# Code swallows stderr; every OTHER gate runs pinned to the dev venv's
# uv.lock, never booting the entry point a real install resolves fresh).
# This leg is the missing layer test: probe the freshly INSTALLED venv's
# actual nx-mcp / nx-mcp-catalog binaries with a real JSON-RPC `initialize`
# handshake — not `uv run`, not the repo dev venv — using the SAME probe
# (nexus.health._probe_mcp_server) `nx doctor`'s check calls, so a
# regression here mechanically implies a `nx doctor` red on any user box.
# Meaningful in BOTH layers: --published is where this leg matters most
# (it is the layer nexus-l2ku5 actually broke), and the default layer keeps
# it as a release-battery tripwire against a locally-resolved regression.
cat > "$WORK/mcp_probe.py" <<'PYEOF'
import sys

from nexus.health import _MCP_ENTRY_POINTS, _probe_mcp_server

bin_dir = sys.argv[1]
ok_all = True
for binary_name, expected_name in _MCP_ENTRY_POINTS:
    binary_path = f"{bin_dir}/{binary_name}"
    ok, detail = _probe_mcp_server(binary_path, expected_name)
    print(f"{'OK' if ok else 'FAIL'} {binary_name}: {detail}")
    ok_all = ok_all and ok
sys.exit(0 if ok_all else 1)
PYEOF
if ! "$PROBE_PYTHON" "$WORK/mcp_probe.py" "$BIN_DIR" >"$LOGS/mcp-entrypoints.log" 2>&1; then
    cat "$LOGS/mcp-entrypoints.log" >&2
    _fail "MCP entry-point handshake failed (nexus-l2ku5) — the freshly installed venv's nx-mcp/nx-mcp-catalog did not respond to a JSON-RPC initialize with the expected serverInfo.name; see stderr excerpt above"
fi
cat "$LOGS/mcp-entrypoints.log"

# Name the dependency explicitly, not just its symptom: a future unbounded
# floor regression (the root cause here) must be caught even if some future
# mcp release keeps fastmcp importable but breaks something else.
SITE_PACKAGES="$("$PROBE_PYTHON" -c 'import sysconfig; print(sysconfig.get_path("purelib"))')"
MCP_DIST_INFO="$(find "$SITE_PACKAGES" -maxdepth 1 -name 'mcp-*.dist-info' 2>/dev/null | head -1)"
[ -n "$MCP_DIST_INFO" ] \
    || _fail "mcp dist-info not found under $SITE_PACKAGES — cannot verify the mcp<2 pin (nexus-l2ku5)"
MCP_VERSION="$(basename "$MCP_DIST_INFO" | sed -E 's/^mcp-([0-9]+(\.[0-9]+)*).*/\1/')"
MCP_MAJOR="${MCP_VERSION%%.*}"
echo "  mcp dist-info version: $MCP_VERSION"
if [ "$MCP_MAJOR" -ge 2 ]; then
    _fail "mcp resolved to $MCP_VERSION (>=2) in the fresh install — the mcp>=1.0,<2 pin regressed (nexus-l2ku5: mcp 2.0.0 removed mcp.server.fastmcp, killing both MCP servers at import)"
fi

# Published mode with an explicit version: confirm PyPI actually resolved
# the requested version rather than serving a stale index cache entry.
if [ "$PUBLISHED_MODE" = 1 ] && [ -n "$PUBLISHED_VERSION" ]; then
    CONEXUS_DIST_INFO="$(find "$SITE_PACKAGES" -maxdepth 1 -name 'conexus-*.dist-info' 2>/dev/null | head -1)"
    [ -n "$CONEXUS_DIST_INFO" ] \
        || _fail "conexus dist-info not found under $SITE_PACKAGES after a published install"
    INSTALLED_VERSION="$(basename "$CONEXUS_DIST_INFO" | sed -E 's/^conexus-([0-9]+(\.[0-9]+)*).*/\1/')"
    [ "$INSTALLED_VERSION" = "$PUBLISHED_VERSION" ] \
        || _fail "requested conexus==$PUBLISHED_VERSION but the installed dist-info reports $INSTALLED_VERSION — PyPI index mismatch or resolver bug"
    echo "  confirmed installed version: $INSTALLED_VERSION"
fi

echo "── 4/9 nx init (local mode, virgin HOME, scrubbed env) ──"
_nx init -y --no-autostart 2>&1 | tee "$LOGS/init.log"
grep -Eq "the service backend is serving" "$LOGS/init.log" \
    || _fail "init did not confirm a serving backend"
# nexus-9xfx5: init converges the ladder — a virgin box must not boot with a
# vacuous pending rung. ("converged and verified" is the runner's literal
# success line — upgrade.py's LadderRunner output.)
grep -q "converged and verified" "$LOGS/init.log" \
    || _fail "init did not converge the upgrade ladder (9xfx5 regression)"
# Save the model cache as soon as it exists — a later-step failure must not
# force the next run to re-download 416 MB.
if [ -n "${FRESH_MVV_CACHE:-}" ] && [ -d "$HOME_DIR/.cache/nexus" ]; then
    mkdir -p "$FRESH_MVV_CACHE"
    rm -rf "$FRESH_MVV_CACHE/nexus"
    cp -R "$HOME_DIR/.cache/nexus" "$FRESH_MVV_CACHE/nexus" 2>/dev/null || true
fi

echo "── 5/9 store put: catalog row + manifest (the f1itv assertions) ──"
SENTINEL="fresh-mvv-sentinel: portable pgvector never ships the builder ISA"
echo "$SENTINEL" | _nx store put - --title "fresh-mvv-sentinel" \
    >"$LOGS/store.log" 2>&1 || _fail "store put failed (see $LOGS/store.log)"
grep -Eq "Stored: [0-9a-f]{64}" "$LOGS/store.log" \
    || _fail "store put did not emit a full-digest doc id (RDR-180 shape)"
if grep -q "manifest_hook_batch_missing_doc_identity" "$LOGS/store.log"; then
    _fail "manifest skipped — catalog doc identity missing on a fresh box (nexus-f1itv class)"
fi
_nx catalog search "fresh-mvv-sentinel" >"$LOGS/catalog-store.log" 2>&1 || true
grep -q "fresh-mvv-sentinel" "$LOGS/catalog-store.log" \
    || _fail "store put did not register in the engine catalog (nexus-f1itv class)"

# nexus-sdp0u: re-put the SAME title with DIFFERENT content must reconcile
# onto the SAME catalog document (the synthesized source_uri identity),
# never mint a sibling. The production symptom this pins: three puts of
# one title minted three documents (1.1.1/1.1.2/1.1.3) with contradictory
# content, because source_uri was always empty and the engine's
# upsert-on-(tenant, source_uri) identity never matched.
SENTINEL_V2="fresh-mvv-sentinel-v2: the re-put must win, not duplicate"
echo "$SENTINEL_V2" | _nx store put - --title "fresh-mvv-sentinel" \
    >"$LOGS/store-reput.log" 2>&1 || _fail "re-put failed (see $LOGS/store-reput.log)"
grep -Eq "Stored: [0-9a-f]{64}" "$LOGS/store-reput.log" \
    || _fail "re-put did not emit a full-digest doc id (RDR-180 shape)"
_nx catalog search "fresh-mvv-sentinel" >"$LOGS/catalog-reput.log" 2>&1 || true
REPUT_DOC_COUNT="$(grep -c "fresh-mvv-sentinel" "$LOGS/catalog-reput.log" || true)"
[ "$REPUT_DOC_COUNT" = "1" ] \
    || _fail "re-put of the same title left $REPUT_DOC_COUNT catalog documents instead of reconciling onto one (nexus-sdp0u class); see $LOGS/catalog-reput.log"
_nx search "re-put must win not duplicate" >"$LOGS/search-reput.log" 2>&1 || true
grep -q "fresh-mvv-sentinel-v2" "$LOGS/search-reput.log" \
    || _fail "semantic search after re-put did not surface the NEW content (nexus-sdp0u class); see $LOGS/search-reput.log"

echo "── 6/9 index md: catalog registration (the e9ru2 assertions) ──"
# File stem == frontmatter title on purpose: the pre-flight registration
# titles the catalog row by STEM, and the post-hook's update branch does not
# overwrite title from frontmatter (recorded as nexus bead — see gate docs);
# the assertion must not depend on which one wins.
cat > "$WORK/fresh-mvv-markdown-note.md" <<'EOF'
---
title: fresh-mvv-markdown-note
---
The era-32 wire re-id recomputes during re-embed, and fresh boxes register
markdown in the service catalog.
EOF
_nx index md "$WORK/fresh-mvv-markdown-note.md" --corpus fresh-mvv \
    >"$LOGS/index.log" 2>&1 || _fail "index md failed (see $LOGS/index.log)"
if grep -q "manifest_hook_batch_missing_doc_identity" "$LOGS/index.log"; then
    _fail "markdown manifest skipped on a fresh box (nexus-e9ru2 class)"
fi
_nx catalog search "fresh-mvv-markdown-note" >"$LOGS/catalog-md.log" 2>&1 || true
grep -q "fresh-mvv-markdown-note" "$LOGS/catalog-md.log" \
    || _fail "index md did not register in the engine catalog (nexus-e9ru2 class)"

echo "── 7/9 semantic search returns both ──"
_nx search "portable pgvector builder ISA" >"$LOGS/search1.log" 2>&1 || true
grep -q "fresh-mvv-sentinel" "$LOGS/search1.log" \
    || _fail "search did not return the stored sentinel"
_nx search "era-32 wire re-id re-embed" >"$LOGS/search2.log" 2>&1 || true
grep -q "fresh-mvv" "$LOGS/search2.log" \
    || _fail "search did not return the indexed markdown"

echo "── 8/9 doctor: zero ✗, zero ⚠, warnings allowlisted ──"
_nx doctor >"$LOGS/doctor.log" 2>&1 || _fail "doctor exited non-zero"
if grep -q "✗" "$LOGS/doctor.log"; then
    grep "✗" "$LOGS/doctor.log" >&2
    _fail "doctor shows red ✗ on a virgin box (9xfx5 class)"
fi
# Warnings allowlist — EMPTY by design. Every new fresh-box warning is a
# decision: fix it or allowlist it HERE with a rationale + bead reference.
# Covers BOTH channels (critic-3modes M-H): structlog lines
# (level='warning') AND doctor's human-facing soft-warn rows (⚠ — the
# format_health_for_cli warn=True render, which contains no literal
# "warning" text and was invisible to the structlog grep alone).
#
# nexus-ac4id CLOSED by nexus-5xn3k.6: the dangling-manifests check is
# re-armed on manifest_verify_all() (one engine call, no client-side T3
# enumeration) and no longer carries a deliberate DISABLED off-switch.
#
# HISTORY: from nexus-5xn3k.6 (2026-08-02) until the nexus-koms3 floor bump
# (also 2026-08-02) this allowlist carried a scoped entry for the "engine
# predates the index-run fence" fail-open path (health.py's
# _check_dangling_manifests, `status == 404` branch): REQUIRED_ENGINE_VERSION
# then named v0.1.61, which predates 3cf64d48 (RUNFENCE .2 — the
# manifest/verify* engine routes), so a virgin box's freshly-provisioned
# engine 404'd manifest_verify_all() and the check took its own SKIPPED +
# soft-warning branch. tests/test_engine_version.py::
# TestMvvAllowlistDoesNotOutliveItsTrigger mechanized the removal trigger:
# it reds the moment REQUIRED_ENGINE_VERSION names a tag containing
# 3cf64d48 (v0.1.62+) unless the entry is already gone — it is, as of this
# floor bump, because a virgin box's bundled engine is now fence-aware and
# the check reads clean. (The 404 fail-open path itself stays exercised
# forever in tests/test_health_service_checks.py; only this journey's
# allowlist entry for it was ever temporary.)
#
# CURRENT SCOPED ENTRIES (added nexus-796zn, 2026-08-05 — discovered while
# validating THIS bead's --published mode; both fire unconditionally on the
# CURRENTLY-PINNED engine floor, in either install layer, so leaving them
# unallowlisted made the gate permanently red rather than testing anything):
#   1. "chash-conformance route" — health.py::_check_chash_conformance_report
#      (bead nexus-du2dw, RDR-180): a pre-route engine 404s
#      /chash/conformance and fails OPEN but LOUD by design. "ENGINE HALF
#      INERT until next engine-service tag" per the bead's own close note.
#   2. "stale index-run fences" naming "coverage gap" — health.py's
#      _check_stale_indexing_runs family (bead nexus-vw594 F1-F4): the
#      client-side fence-stamping shipped, but the engine's begin-many
#      route has not deployed yet, so freshly-indexed docs still show
#      index_state=NULL. "ENGINE HALF INERT ... older engines degrade with
#      recorded advisory, no regression on current installs" per the
#      bead's own close note.
# Both beads name the SAME blocker (the next engine-service tag). Tracked
# for removal as nexus-8hpad — delete BOTH entries in the same change that
# bumps REQUIRED_ENGINE_VERSION to a tag containing the du2dw route +
# vw594 F1 begin-many route. tests/test_engine_version.py::
# Test8hpadAllowlistDoesNotOutliveItsTrigger mechanizes the removal trigger
# (mirrors TestMvvAllowlistDoesNotOutliveItsTrigger above, keyed on
# REQUIRED_ENGINE_VERSION > (0, 1, 65) rather than a tag/commit sha, since
# — unlike the 3cf64d48 case — the future tag does not exist yet to pin
# one against). GREP-LEVEL PARITY with health.py's exact wording is pinned
# separately in tests/test_fresh_install_mvv_published_mode.py.
#
# chash.conformance (dot, not a literal hyphen) matches BOTH the
# human-facing detail's "chash-conformance route" AND the underlying
# structlog event name's "chash_conformance_report_engine_floor" — they
# render on separate lines and must both be covered.
#
# nexus-iocp8 SIGNIFICANT (substantive-critic, empirically verified): the
# label "stale index-run fences" is shared by TWO independent WARN
# branches in health.py's _check_stale_indexing_runs — the vw594
# engine-half-inert signal we WANT to allowlist ("N document(s) report
# index_state but it is NULL...coverage gap"), and a genuinely stuck
# mid-run alarm ("N document(s) stranded in index_state='indexing'
# beyond Nh...check for a stuck run"). Both render as
# "stale index-run fences: N document(s) ..." — a bare
# `stale index-run fences: [0-9]+ document` pattern swallows BOTH, which
# would silently blind this gate to a real stuck-run alarm. Anchored to
# the vw594 branch's own distinguishing wording ("report index_state but
# it is NULL") so the stuck-run branch can never match; pinned by a
# negative-case GREP-LEVEL PARITY test.
ALLOWLIST_REGEX='chash.conformance|stale index-run fences: [0-9]+ document\(s\) report index_state but it is NULL'
WARNING_LINES="$(grep -E "level='warning'|\[warning|⚠" "$LOGS/doctor.log" || true)"
UNALLOWLISTED="$(printf '%s\n' "$WARNING_LINES" | grep -v -E "$ALLOWLIST_REGEX" | grep -v '^$' || true)"
if [ -n "$UNALLOWLISTED" ]; then
    echo "$UNALLOWLISTED" >&2
    _fail "non-allowlisted warnings in a virgin box's doctor (add a fix or an allowlist entry with rationale)"
fi
if [ -n "$WARNING_LINES" ]; then
    echo "  (allowlisted, engine-floor-gated warnings present — see ALLOWLIST_REGEX above):"
    printf '%s\n' "$WARNING_LINES" | sed 's/^/    /'
fi

echo "── 9/9 non-vacuity ──"
# The gate must never skip-pass: prove the substantive legs actually ran.
LEGS_TO_CHECK="mcp-entrypoints.log init.log store.log store-reput.log search-reput.log index.log doctor.log"
if [ "$PUBLISHED_MODE" = 1 ]; then
    LEGS_TO_CHECK="install.log $LEGS_TO_CHECK"
else
    LEGS_TO_CHECK="build.log $LEGS_TO_CHECK"
fi
for f in $LEGS_TO_CHECK; do
    [ -s "$LOGS/$f" ] || _fail "leg log $f is empty — a journey leg silently skipped"
done

GATE_OK=1
VERSION_STRING="$(_nx --version 2>/dev/null | grep -oE '[0-9]+\.[0-9]+\.[0-9]+' | head -1)"
if [ "$PUBLISHED_MODE" = 1 ]; then
    echo "FRESH-INSTALL MVV PASSED — conexus $VERSION_STRING (PUBLISHED artifact, uv-tool resolution layer)"
else
    echo "FRESH-INSTALL MVV PASSED — conexus $VERSION_STRING (LOCAL WHEEL, release-battery layer)"
fi
