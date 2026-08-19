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
#   --self-test        run the pure-function unit checks (nexus-1ktd5 items
#                       A/B/C) against synthetic fixtures only — no wheel
#                       build, no venv, no engine, no network. Safe anywhere.
# Exit 0 == FRESH-INSTALL MVV PASSED (the literal sentinel on the last line).
set -euo pipefail

# ── pure helper functions (exercised directly by --self-test) ──────────────
# House precedent: tests/e2e/local-index-memory-gate.sh extracts pure
# functions + a --self-test arm for exactly this reason (nexus-1ktd5
# MANDATORY ACCEPTANCE CRITERION) — a full sandboxed run of THIS script
# costs minutes (wheel build, engine download, PG bundle) and --published
# additionally hits the real network/PyPI, so RED/GREEN demonstrations of
# an assertion's logic run here against synthetic fixtures instead of a
# live end-to-end run.

# Extract `version = "X.Y.Z"` from a pyproject.toml-shaped file. Echoes the
# bare version string, or empty if no `version =` line is found (missing
# file included — read failure is not an error here, just "unknown").
_pyproject_version() {
    local file="$1"
    [ -r "$file" ] || return 0
    grep -m1 -E '^version[[:space:]]*=' "$file" 2>/dev/null \
        | sed -E 's/^version[[:space:]]*=[[:space:]]*"([^"]*)".*/\1/'
}

# nexus-1ktd5 item A: decide whether an installed conexus version matches
# what was EXPECTED (an explicit --published X.Y.Z, or this checkout's own
# pyproject.toml version when no version was given — the shakedown-layer
# contract: confirm PyPI actually served what was just tagged, not a stale
# index cache entry or an unrelated version). Pure — echoes "PASS|<msg>" or
# "FAIL|<msg>", touches neither filesystem nor network.
_version_check_verdict() {
    local expected="$1" installed="$2" source="$3"
    if [ -z "$expected" ]; then
        echo "FAIL|could not determine an expected version (source: $source) — cannot verify what PyPI actually served"
        return
    fi
    if [ -z "$installed" ]; then
        echo "FAIL|could not determine the installed version — cannot verify it against expected $expected (source: $source)"
        return
    fi
    if [ "$expected" != "$installed" ]; then
        echo "FAIL|PyPI served $installed but expected $expected (source: $source) — index mismatch, stale cache, or resolver bug; the run must not pass silently on a different artifact than intended"
        return
    fi
    echo "PASS|confirmed installed version $installed matches expected $expected (source: $source)"
}

# nexus-1ktd5 item B: does *log* carry the discriminating evidence that the
# MARKDOWN doc's own search leg matched -- not just any "fresh-mvv"
# substring, which the store-put doc's output ALSO satisfies (title
# "fresh-mvv-sentinel"). "fresh-mvv-markdown-note" is the markdown file's
# stem, embedded in its source_path (formatters.py format_plain's
# source_path:line:content rendering for file-backed results) and absent
# from the store-put doc's title/content -- a disjoint, leg-specific
# literal. Extracted so --self-test exercises the SAME logic the runtime
# leg calls, not a parallel copy of it.
_search_log_confirms_markdown_leg() {
    grep -q "fresh-mvv-markdown-note" "$1"
}

# nexus-1ktd5 item C: real non-vacuity check for a journey leg's log file.
# `test -s` alone (the prior check) proves only that SOME bytes were
# written — a log holding nothing but an uncaught traceback (e.g. a
# background-thread exception that never propagates a nonzero exit)
# satisfies `-s` and adds no evidence beyond what each leg's own content
# assertion already checks elsewhere in this script. This adds the one
# thing `-s` cannot: no unhandled Python traceback anywhere in the leg's
# own log. Echoes "OK" or "FAIL|<reason>".
_leg_log_is_substantive() {
    local f="$1"
    if [ ! -s "$f" ]; then
        echo "FAIL|empty — a journey leg silently skipped"
        return
    fi
    if grep -q "Traceback (most recent call last)" "$f" 2>/dev/null; then
        echo "FAIL|contains an uncaught Python traceback — the leg's own log is non-empty but that is not evidence it worked (nexus-1ktd5 item C)"
        return
    fi
    echo "OK"
}

_self_test() {
    local failures=0 t
    t="$(mktemp -d /tmp/fresh-install-mvv-selftest-XXXXXX)"
    trap 'rm -rf "$t"' RETURN

    _assert_eq() {
        if [ "$2" = "$3" ]; then
            echo "  PASS $1"
        else
            echo "  FAIL $1 -- expected [$3] got [$2]"
            failures=$((failures + 1))
        fi
    }
    _assert_prefix() {
        case "$2" in
            "$3"*) echo "  PASS $1" ;;
            *) echo "  FAIL $1 -- expected prefix [$3] got [$2]"; failures=$((failures + 1)) ;;
        esac
    }

    echo "== self-test: _pyproject_version =="
    printf '[project]\nname = "conexus"\nversion = "7.10.0"\ndescription = "x"\n' > "$t/pyproject.toml"
    _assert_eq "reads a normal version line" "$(_pyproject_version "$t/pyproject.toml")" "7.10.0"
    printf '[project]\nname = "conexus"\n' > "$t/no-version.toml"
    _assert_eq "no version line -> empty" "$(_pyproject_version "$t/no-version.toml")" ""
    _assert_eq "missing file -> empty, not an error" "$(_pyproject_version "$t/does-not-exist.toml")" ""

    echo "== self-test: _version_check_verdict (item A -- RED/GREEN) =="
    # RED: the exact break item A describes -- PyPI served a DIFFERENT
    # version than expected. Must FAIL and name both versions.
    _assert_prefix "RED: served != expected -> FAIL naming both versions" \
        "$(_version_check_verdict "7.10.0" "7.9.0" "explicit --published arg")" "FAIL|PyPI served 7.9.0 but expected 7.10.0"
    # GREEN: repaired -- served == expected.
    _assert_prefix "GREEN: served == expected -> PASS" \
        "$(_version_check_verdict "7.10.0" "7.10.0" "explicit --published arg")" "PASS|"
    _assert_prefix "no expected version resolvable -> FAIL, not a silent pass" \
        "$(_version_check_verdict "" "7.10.0" "this checkout's pyproject.toml")" "FAIL|could not determine an expected version"

    echo "== self-test: item B -- markdown-search literal must not cross-satisfy the store-put doc (RED/GREEN) =="
    # Synthetic fixtures standing in for the two 'nx search' output shapes:
    # the store-put doc's own output (title-style, no source_path -- see
    # formatters.py format_plain) and the indexed-markdown doc's output
    # (source_path-style, filename embedded in the path column).
    printf '[0.1234] fresh-mvv-sentinel\n  fresh-mvv-sentinel: portable pgvector never ships the builder ISA\n' > "$t/store-doc-search-output.log"
    printf '/tmp/xxx/fresh-mvv-markdown-note.md:1:The era-32 wire re-id recomputes during re-embed, and fresh boxes register markdown in the service catalog.\n' > "$t/markdown-doc-search-output.log"
    # RED: the OLD weak literal "fresh-mvv" is satisfied by BOTH fixtures --
    # a search leg using it cannot prove it searched the markdown doc's OWN
    # indexing path, since the store-put doc's output also contains it.
    # (Exercised directly, not via the shared function -- this documents
    # the bug the fix replaced; the GREEN checks below exercise the REAL
    # runtime function.)
    if grep -q "fresh-mvv" "$t/store-doc-search-output.log" && grep -q "fresh-mvv" "$t/markdown-doc-search-output.log"; then
        echo "  PASS RED: old literal 'fresh-mvv' cross-satisfies both fixtures (the bug)"
    else
        echo "  FAIL RED: expected the old literal to cross-satisfy both fixtures"
        failures=$((failures + 1))
    fi
    # GREEN: the REAL runtime function (_search_log_confirms_markdown_leg,
    # the same one the 7/9 leg calls) matches ONLY the markdown doc's own
    # output, never the store-put doc's.
    if _search_log_confirms_markdown_leg "$t/markdown-doc-search-output.log"; then
        echo "  PASS GREEN: runtime function matches the markdown doc's own output"
    else
        echo "  FAIL GREEN: runtime function did not match the markdown doc's own output"
        failures=$((failures + 1))
    fi
    if _search_log_confirms_markdown_leg "$t/store-doc-search-output.log"; then
        echo "  FAIL GREEN: runtime function spuriously matches the store-put doc's output too -- not disjoint"
        failures=$((failures + 1))
    else
        echo "  PASS GREEN: runtime function does NOT match the store-put doc's output (disjoint)"
    fi

    echo "== self-test: _leg_log_is_substantive (item C -- RED/GREEN) =="
    printf '' > "$t/empty.log"
    _assert_prefix "empty log -> FAIL" "$(_leg_log_is_substantive "$t/empty.log")" "FAIL|empty"
    printf 'Traceback (most recent call last):\n  File "x.py", line 1, in <module>\nRuntimeError: boom\n' > "$t/traceback-only.log"
    # RED: the OLD check (`test -s`) treats this as fine (non-empty) even
    # though the leg crashed -- this is the exact "-s only" vacuity item C
    # describes. Demonstrate that directly.
    if [ -s "$t/traceback-only.log" ]; then
        echo "  PASS RED: old '-s only' check would treat a traceback-only log as fine (the bug)"
    else
        echo "  FAIL RED: expected the traceback fixture to be non-empty"
        failures=$((failures + 1))
    fi
    _assert_prefix "GREEN: new check FAILs a traceback-only log naming the real cause" \
        "$(_leg_log_is_substantive "$t/traceback-only.log")" "FAIL|contains an uncaught Python traceback"
    printf 'Stored: %s\n' "$(printf 'a%.0s' $(seq 1 64))" > "$t/real-content.log"
    _assert_eq "GREEN: a real, traceback-free log -> OK" "$(_leg_log_is_substantive "$t/real-content.log")" "OK"

    echo "== self-test: bash -n on this script itself =="
    if bash -n "${BASH_SOURCE[0]}"; then
        echo "  PASS bash -n clean"
    else
        echo "  FAIL bash -n reported a syntax error"
        failures=$((failures + 1))
    fi

    echo
    if [ "$failures" -eq 0 ]; then
        echo "SELF-TEST PASSED -- nexus-1ktd5 items A/B/C verified against synthetic fixtures (no wheel build, no venv, no engine, no network)"
        return 0
    else
        echo "SELF-TEST FAILED -- $failures check(s)"
        return 1
    fi
}

PUBLISHED_MODE=0
PUBLISHED_VERSION=""
SELF_TEST=0
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
        --self-test)
            SELF_TEST=1
            shift
            ;;
        -h|--help)
            echo "Usage: $0 [--published [VERSION]] [--self-test]"
            echo "  (no args)          install the LOCAL wheel built from this checkout (default)"
            echo "  --published        install the latest PUBLISHED conexus from PyPI"
            echo "  --published X.Y.Z  install that exact PUBLISHED version from PyPI"
            echo "  --self-test        run the pure-function unit checks (nexus-1ktd5 items"
            echo "                     A/B/C) against synthetic fixtures only -- no wheel"
            echo "                     build, no venv, no engine, no network. Safe anywhere."
            exit 0
            ;;
        *)
            echo "unknown argument: $1" >&2
            exit 2
            ;;
    esac
done

if [ "$SELF_TEST" = 1 ]; then
    _self_test
    exit $?
fi

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

    # PyPI upload validation, pre-tag. Until 2026-08-11 the FIRST execution of
    # twine's metadata check anywhere in the pipeline was the publish step
    # itself, so a nine-gates-green release could still die at upload.
    #
    # SCOPE HONESTY — read this before trusting it. This does NOT reproduce the
    # 2026-08-11 7.6.0 failure ("InvalidDistribution: '2.5' is not a valid
    # metadata version"). That was VERSION SKEW between the uploader's bundled
    # twine (pinned at a 2026-02 commit) and the Metadata-Version 2.5 that
    # hatchling 1.32.0 emits. This check runs a CURRENT twine, which accepts
    # 2.5 — verified: it passes on the exact wheel PyPI rejected. Keeping the
    # uploader current is Dependabot's job (.github/dependabot.yml), not this
    # check's.
    #
    # What this DOES catch: a wheel whose metadata is genuinely malformed or
    # unrenderable, which PyPI refuses regardless of uploader version. That is
    # worth catching while a fix is still cheap, but do not read a pass here as
    # "the upload will succeed".
    echo "  [check] twine metadata validation (what PyPI's upload gate runs)"
    if ! uvx twine check --strict "$WHEEL" >"$LOGS/twine-check.log" 2>&1; then
        sed 's/^/    /' "$LOGS/twine-check.log" >&2
        _fail "twine check rejected the wheel — PyPI would refuse this upload (see $LOGS/twine-check.log)"
    fi
    sed 's/^/    /' "$LOGS/twine-check.log"

    echo "── 2/9 Virgin venv + install ──"
    # nexus-1ktd5 item E (wave-2 review fast-follow): the local-wheel
    # install layer used to run with the AMBIENT env while --published ran
    # under _uv_sandboxed's env -i — an operator's UV_INDEX_URL /
    # PIP_INDEX_URL / UV_* overrides could steer THIS layer's dependency
    # resolution at a mirror (the nexus-l2ku5 resolver-drift class) while
    # the sandboxed layer resolved the real index. Same helper both layers.
    _uv_sandboxed venv --python 3.12 -q "$VENV"
    _uv_sandboxed pip install -q --python "$VENV/bin/python" "$WHEEL"
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

# nexus-1ktd5 item A: verify PyPI served the version EXPECTED, always -- not
# only when --published named an explicit version. Before this fix, an
# omitted version (the documented post-tag SHAKEDOWN form, T2
# nexus/shakedown-playbook §2 S1) asserted NOTHING about which version PyPI
# actually resolved. With no explicit version this now compares against
# THIS CHECKOUT's own pyproject.toml version -- the shakedown contract:
# confirm PyPI served exactly what was just tagged from this tree, not a
# stale index cache entry or an unrelated version.
if [ "$PUBLISHED_MODE" = 1 ]; then
    if [ -n "$PUBLISHED_VERSION" ]; then
        EXPECTED_VERSION="$PUBLISHED_VERSION"
        EXPECTED_VERSION_SOURCE="explicit --published arg"
    else
        EXPECTED_VERSION="$(_pyproject_version "$REPO_ROOT/pyproject.toml")"
        EXPECTED_VERSION_SOURCE="this checkout's pyproject.toml (no explicit --published version given)"
    fi
    CONEXUS_DIST_INFO="$(find "$SITE_PACKAGES" -maxdepth 1 -name 'conexus-*.dist-info' 2>/dev/null | head -1)"
    [ -n "$CONEXUS_DIST_INFO" ] \
        || _fail "conexus dist-info not found under $SITE_PACKAGES after a published install"
    INSTALLED_VERSION="$(basename "$CONEXUS_DIST_INFO" | sed -E 's/^conexus-([0-9]+(\.[0-9]+)*).*/\1/')"
    VERSION_VERDICT="$(_version_check_verdict "$EXPECTED_VERSION" "$INSTALLED_VERSION" "$EXPECTED_VERSION_SOURCE")"
    case "$VERSION_VERDICT" in
        PASS\|*) echo "  ${VERSION_VERDICT#PASS|}" ;;
        FAIL\|*) _fail "${VERSION_VERDICT#FAIL|}" ;;
    esac
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
_search_log_confirms_markdown_leg "$LOGS/search2.log" \
    || _fail "search did not return the indexed markdown (fresh-mvv-markdown-note)"

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
# nexus-ac4id CLOSED by nexus-5xn3k.6: the dangling-manifests check was
# re-armed on manifest_verify_all() (one engine call, no client-side T3
# enumeration) and no longer carried a deliberate DISABLED off-switch.
#
# RETIRED ENTIRELY, RDR-191 Phase 6 (nexus-o8dil.33), 2026-08-15: `nx
# doctor`'s `_check_dangling_manifests` (and its manifest_verify_all() call)
# no longer runs at all — the manifest-chunk FK makes the dangling state it
# detected unreachable by construction. The HISTORY paragraph below
# describes a check that existed at the time; it does not run on this
# checkout's doctor any more, so there is nothing here to allowlist for it
# regardless of engine floor. `_check_manifest_null_collection` (the
# EXPLICITLY-NOT-RETIRED census below) is unaffected and still runs.
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
# the check reads clean. (The 404 fail-open path itself stayed exercised in
# tests/test_health_service_checks.py's TestCheckDanglingManifests through
# RDR-191 Phase 5 — that class, and _check_dangling_manifests itself, are
# DELETED as of RDR-191 Phase 6 nexus-o8dil.33, so neither the check nor its
# test coverage exist any more; this journey's allowlist entry for it was
# temporary independent of that later removal.)
#
# CURRENT SCOPED ENTRIES: none. The nexus-796zn entries (chash-conformance
# route [du2dw engine half] + the vw594 engine-half-inert stale-fence
# signal) retired 2026-08-07 with the REQUIRED_ENGINE_VERSION bump to
# (0,1,67) — both engine halves are live at the pinned floor, so a virgin
# box's doctor now reads clean with ZERO allowlisted warnings. The
# removal was enforced mechanically by tests/test_engine_version.py::
# Test8hpadAllowlistDoesNotOutliveItsTrigger (the removal-trigger bead;
# see that test's docstring for the lineage). Future entries need a
# rationale + bead reference + a mechanized removal trigger, per the
# HISTORY above.
#
# NOT HERE BY DESIGN: health.py's `_check_manifest_null_collection` (C2,
# T2 nexus/chroma-residue-plan-2026-08-10) — GET
# /v1/catalog/manifest/null_collection has never shipped on ANY engine tag
# (REQUIRED_ENGINE_VERSION pinned at (0,1,69), before the route). Unlike
# the two allowlist idioms above, this check does NOT take the
# unconditional-WARN-plus-allowlist-entry path — that would put an entry
# in this file on day one that could never be exercised as "removed",
# since the check would never have rendered clean in the first place. It
# instead gates its OWN severity on the live REQUIRED_ENGINE_VERSION
# constant (ok=True informational while the floor is at or below (0,1,69),
# loud WARN once it advances past — see that function's docstring), so it
# self-corrects with no allowlist entry and no manual removal step ever
# required. Substantive critique finding 1, 2026-08-10.
#
# The sentinel below matches NOTHING (an empty regex would match
# everything through grep -v -E and silently allowlist every warning).
ALLOWLIST_REGEX='__NO_ALLOWLISTED_WARNINGS_SENTINEL__'
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
# nexus-1ktd5 item C: `test -s` alone only proves non-emptiness -- every
# log below already carries a real content assertion earlier in this
# script, so a bare `-s` check here was pure duplication that additionally
# let a log holding nothing but an uncaught traceback (a leg that exits 0
# despite a background-thread exception) read as fine. This calls
# _leg_log_is_substantive instead, which adds the one thing `-s` cannot: no
# unhandled Python traceback anywhere in the leg's own log.
LEGS_TO_CHECK="mcp-entrypoints.log init.log store.log store-reput.log search-reput.log index.log doctor.log"
if [ "$PUBLISHED_MODE" = 1 ]; then
    LEGS_TO_CHECK="install.log $LEGS_TO_CHECK"
else
    LEGS_TO_CHECK="build.log $LEGS_TO_CHECK"
fi
for f in $LEGS_TO_CHECK; do
    LEG_VERDICT="$(_leg_log_is_substantive "$LOGS/$f")"
    case "$LEG_VERDICT" in
        OK) : ;;
        FAIL\|*) _fail "leg log $f: ${LEG_VERDICT#FAIL|}" ;;
    esac
done

GATE_OK=1
# Pipe-free tail (nexus-i66g4/6zxfb/wbeyi class): take the first line via
# parameter expansion instead of `| head -1` -- under this script's
# `set -o pipefail`, a still-writing grep closed early by head risks its
# SIGPIPE being promoted over head's own (successful) exit status.
VERSION_ALL="$(_nx --version 2>/dev/null | grep -oE '[0-9]+\.[0-9]+\.[0-9]+')"
VERSION_STRING="${VERSION_ALL%%$'\n'*}"
if [ "$PUBLISHED_MODE" = 1 ]; then
    echo "FRESH-INSTALL MVV PASSED — conexus $VERSION_STRING (PUBLISHED artifact, uv-tool resolution layer)"
else
    echo "FRESH-INSTALL MVV PASSED — conexus $VERSION_STRING (LOCAL WHEEL, release-battery layer)"
fi
