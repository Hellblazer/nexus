#!/usr/bin/env bash
# release-sandbox.sh — high-fidelity local pre-merge verification.
#
# Combines:
#   - scripts/reinstall-tool.sh   (wheel-equivalent install via uv tool)
#   - tests/e2e/sandbox.sh        (isolated $HOME for Claude Code state)
#   - tests/cc-validation/lib.sh  (tmux primitives, used by tmux mode)
#
# Why this exists: merging to main to "test things out" is dangerous because
# the wheel-install path (uv tool install) resolves package data and version-
# gated migrations differently from the editable install that pytest uses.
# Run this BEFORE pushing/merging anything that touches: install/packaging,
# T2 migrations, MCP servers, hooks, plugin manifests, commands that read
# T2/T3 state, or anything tagged "ships to users".
#
# Modes:
#   smoke     — install + activate + post-install canary checks. ~2 min.
#   shakedown — full ensemble: smoke + index repo/pdf/rdr + search/query/T1/T2 +
#               link graph readback + T1 turd sniff. ~5–10 min.
#   shell     — install + activate + drop into a subshell with sandbox env.
#               Exit the subshell to tear down (HOME restored automatically).
#   tmux      — install + activate + launch Claude Code in tmux against
#               the sandbox. Useful for exercising MCP / hooks / skills.
#   reset     — tear down ~/nexus-sandbox without reinstalling.
#
# Source-of-truth doc: tests/e2e/release-sandbox.md
# Companion gist: https://gist.github.com/Hellblazer/511a05e1bf79dd6ea20be962d0ca04af

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_ROOT="$(cd "$SCRIPT_DIR/../.." && pwd)"
SANDBOX="$HOME/nexus-sandbox"
TMUX_SESSION="${TMUX_SESSION:-nexus-sandbox}"

MODE="${1:-help}"
shift || true

_die() { echo "ERROR: $*" >&2; exit 1; }

_print_help() {
    # printf rather than here-doc: bash here-docs hang in some non-
    # interactive shell contexts (Claude Code harness, certain CI
    # runners) where parent stdin is wired to a pipe the here-doc
    # machinery never closes. printf has no such dependency.
    printf '%s\n' \
        "Usage: $0 <mode> [options]" \
        "" \
        "Modes:" \
        "  smoke      Reinstall + activate + run nx upgrade --dry-run + nx doctor checks." \
        "             Verifies the wheel install + migrations + health surface. ~2 min." \
        "  shakedown  Full ensemble: smoke + nx index repo/pdf/rdr + cross-corpus search" \
        "             + T2 memory roundtrip + T1 scratch use + catalog link readback +" \
        "             T1 turd sniff. Exercises every pipeline against a fresh install." \
        "             ~5–10 min on warm cache, +10–15 min if MinerU models are not yet downloaded." \
        "             Probes tc-sql.pdf (Docling path) AND bft-to-smr.pdf (MinerU path)." \
        "  shell      Reinstall + activate + drop into a subshell with HOME=\$SANDBOX." \
        "             Use this for manual nx index, nx search, etc. Exit normally to" \
        "             tear down." \
        "  tmux       Reinstall + activate + launch Claude Code interactively in tmux." \
        "             Useful for end-to-end exercises against MCP / plugin / hooks." \
        "             Requires tests/e2e/.claude-auth/.credentials.json (run" \
        "             tests/e2e/auth-login.sh first)." \
        "  service    RDR-157 P4.2 fresh-machine LOCAL-mode E2E: position the service" \
        "             artifact (native binary via NEXUS_SERVICE_BIN, else the repo JAR)," \
        "             then prove ONE command (nx init --service) goes fresh-install ->" \
        "             serving with zero manual steps, idempotent on re-run, then stop." \
        "             Requires a local PG with pgvector (host or NEXUS_PG_BUNDLE) and the" \
        "             bge-768 ONNX (auto-fetched by init; ~416 MB on a cold cache)." \
        "  reset      Remove ~/nexus-sandbox. Does NOT reinstall." \
        "  help       Print this message." \
        "" \
        "Common options (post-mode):" \
        "  --skip-install   Skip the reinstall step. Useful when the tool venv is" \
        "                   already at the version you want to exercise." \
        "  --keep-existing  Reuse \$HOME/nexus-sandbox if it exists (default: blow away" \
        "                   and recreate so state is reproducible)." \
        "" \
        "Examples:" \
        "  # Pre-merge smoke after a refactor" \
        "  $0 smoke" \
        "" \
        "  # Hand-test indexing into the sandbox" \
        "  $0 shell" \
        "  (sandbox) nx index repo /path/to/test-repo" \
        "  (sandbox) nx taxonomy status" \
        "  (sandbox) exit" \
        "" \
        "  # Spin up Claude Code against the sandbox" \
        "  $0 tmux" \
        "" \
        "  # Skip reinstall (e.g. iterating on shell flow)" \
        "  $0 shell --skip-install" \
        ""
}

# ── Option parsing ───────────────────────────────────────────────────────────

SKIP_INSTALL=0
KEEP_EXISTING=0
while [[ $# -gt 0 ]]; do
    case "$1" in
        --skip-install) SKIP_INSTALL=1; shift ;;
        --keep-existing) KEEP_EXISTING=1; shift ;;
        --help|-h) _print_help; exit 0 ;;
        *) _die "unknown option: $1 (use $0 help)" ;;
    esac
done

# ── Mode dispatch ────────────────────────────────────────────────────────────

if [[ "$MODE" == "help" || "$MODE" == "--help" || "$MODE" == "-h" ]]; then
    _print_help
    exit 0
fi

if [[ "$MODE" == "reset" ]]; then
    if [[ -d "$SANDBOX" ]]; then
        echo "Removing $SANDBOX ..."
        rm -rf "$SANDBOX"
        echo "Sandbox removed."
    else
        echo "No sandbox at $SANDBOX — nothing to reset."
    fi
    exit 0
fi

if [[ "$MODE" != "smoke" && "$MODE" != "shakedown" && "$MODE" != "shell" \
      && "$MODE" != "tmux" && "$MODE" != "service" ]]; then
    _die "unknown mode: $MODE (use $0 help)"
fi

# ── Step 1 — reinstall (unless skipped) ──────────────────────────────────────

if (( SKIP_INSTALL == 0 )); then
    echo "[1/3] Reinstalling nx CLI from $REPO_ROOT ..."
    (cd "$REPO_ROOT" && uv sync >/dev/null 2>&1)
    "$REPO_ROOT/scripts/reinstall-tool.sh" >/dev/null
    echo "      $(nx --version 2>/dev/null || echo 'nx --version failed')"
else
    echo "[1/3] Skipping reinstall (--skip-install). nx version: $(nx --version 2>/dev/null || echo 'unknown')"
fi

# ── Step 2 — create sandbox HOME ─────────────────────────────────────────────

if [[ -d "$SANDBOX" && $KEEP_EXISTING -eq 0 ]]; then
    echo "[2/3] Recreating sandbox at $SANDBOX (use --keep-existing to reuse)"
    rm -rf "$SANDBOX"
elif [[ -d "$SANDBOX" ]]; then
    echo "[2/3] Reusing existing sandbox at $SANDBOX"
fi

if [[ ! -d "$SANDBOX" ]]; then
    echo "[2/3] Creating fresh sandbox at $SANDBOX ..."
    "$REPO_ROOT/tests/e2e/sandbox.sh" >/dev/null
fi

# ── Step 3 — execute mode ────────────────────────────────────────────────────

# shellcheck source=/dev/null
. "$SANDBOX/activate"

case "$MODE" in
    smoke)
        # Force local mode so the smoke does not contact ChromaDB Cloud
        # even if the parent shell has CHROMA_* set. The sandbox HOME is
        # empty by design — there is no cloud data to populate from. Same
        # pattern shakedown uses below; tests/e2e/run.sh has the long
        # explanation.
        export NX_LOCAL=1
        unset CHROMA_API_KEY CHROMA_TENANT CHROMA_DATABASE
        echo "[3/3] Smoke checks (running from /tmp, NX_LOCAL=1):"
        cd /tmp
        echo "  nx --version: $(nx --version)"
        echo
        echo "  nx upgrade --dry-run:"
        nx upgrade --dry-run 2>&1 | sed 's/^/    /' || true
        echo
        echo "  nx upgrade (apply):"
        nx upgrade 2>&1 | sed 's/^/    /' || true
        echo
        # nx catalog setup seeds 12 builtin plan templates that
        # --check-plan-library verifies. Without this step the doctor
        # check fails on every fresh sandbox — that is "you forgot the
        # second setup step", not "something is genuinely broken." Make
        # smoke green-green-green when the install is healthy.
        echo "  nx catalog setup (seeds plan library + initializes catalog):"
        nx catalog setup 2>&1 | tail -5 | sed 's/^/    /' || true
        echo
        for check in --check-schema --check-plan-library --check-taxonomy; do
            echo "  nx doctor $check:"
            if nx doctor "$check" 2>&1 | sed 's/^/    /'; then
                echo "    [pass]"
            else
                echo "    [FAIL] -- exit non-zero" >&2
            fi
            echo
        done
        echo "[done] Sandbox state at $SANDBOX. Run '$0 reset' to tear down."
        ;;

    shakedown)
        # Ensemble pipeline check: every nx surface exercised in sequence
        # against the wheel install. Uses the smaller PDF fixture (tc-sql)
        # for speed. T1 sniff at the start + end catches lifecycle bugs
        # (orphan tmpdirs, leaked session files).
        #
        # Force local mode so the shakedown does not contact ChromaDB Cloud
        # even if the parent shell has CHROMA_* set. Mirrors tests/e2e/run.sh.
        export NX_LOCAL=1
        unset CHROMA_API_KEY CHROMA_TENANT CHROMA_DATABASE
        echo "[3/3] Shakedown: full pipeline ensemble (running from /tmp, NX_LOCAL=1)"
        cd /tmp

        echo
        echo "── T1 sniff: BEFORE ──"
        T1_DIR_PARENT="${TMPDIR%/}"; [[ -z "$T1_DIR_PARENT" ]] && T1_DIR_PARENT=/tmp
        BEFORE_SESSIONS=$( { ls "$HOME/.config/nexus/sessions/" 2>/dev/null || true; } | wc -l | tr -d ' ')
        BEFORE_TMPDIRS=$( { ls -d "$T1_DIR_PARENT"/nx_t1_* 2>/dev/null || true; } | wc -l | tr -d ' ')
        echo "  session files: $BEFORE_SESSIONS  | tmpdirs: $BEFORE_TMPDIRS"

        echo
        echo "── nx --version + upgrade ──"
        nx --version | sed 's/^/  /'
        nx upgrade 2>&1 | sed 's/^/  /' || true

        echo
        echo "── 1/11 nx catalog setup (seeds plan library) ──"
        nx catalog setup 2>&1 | tail -5 | sed 's/^/  /' || true

        echo
        echo "── 2/11 nx index repo ($REPO_ROOT) ──"
        nx index repo "$REPO_ROOT" 2>&1 | tail -5 | sed 's/^/  /' || true

        echo
        echo "── 3a/11 nx index pdf (tc-sql.pdf — Docling path, no formulas) ──"
        nx index pdf "$REPO_ROOT/tests/fixtures/tc-sql.pdf" \
            --collection knowledge__shakedown 2>&1 | tail -5 | sed 's/^/  /' || true

        echo
        echo "── 3b/11 nx index pdf (bft-to-smr.pdf — MinerU path, formulas) ──"
        # nexus-2fyb: shakedown previously tested ONLY tc-sql.pdf which has
        # zero formulas and never invokes MinerU. After mineru was promoted
        # to a default dep, the shakedown must actually exercise that code
        # path — otherwise a regression in the formula-routing/MinerU code
        # would slip through (which is exactly how the original silent-
        # corruption bug shipped). bft-to-smr.pdf has 9 raw math symbols,
        # crosses the auto-route threshold, and is the smallest formula
        # fixture available (~440 KB). First MinerU run downloads ~2-3 GB
        # of models, so this step pays the model-download cost on cold
        # sandbox runs.
        nx index pdf "$REPO_ROOT/tests/fixtures/bft-to-smr.pdf" \
            --collection knowledge__shakedown 2>&1 | tail -5 | sed 's/^/  /' || true

        echo
        echo "── 4/11 nx index rdr ──"
        nx index rdr "$REPO_ROOT" 2>&1 | tail -5 | sed 's/^/  /' || true

        echo
        echo "── 5/11 nexus-e5uw greenfield acceptance: no deprecated chunk keys ──"
        # Bead nexus-e5uw acceptance: a fresh greenfield index must produce
        # 0 chunks carrying any of {source_path, git_branch, git_commit_hash,
        # git_project_name, git_remote_url, corpus, store_type, git_meta}.
        # RDR-102 Phase B drops source_path; normalize() drops the four
        # flat git_* keys; RDR-101 Phase 5c additionally dropped corpus,
        # store_type, git_meta from ALLOWED_TOP_LEVEL.
        #
        # nexus-iftc retired ``nx catalog prune-deprecated-keys``. Delegate
        # to the canonical pytest regression guard at
        # tests/test_indexer_e2e.py::test_greenfield_index_writes_no_deprecated_keys,
        # which walks T3 via ``col.get(include=["metadatas"])`` and asserts
        # zero leaks across all collections this test produced. Running
        # under ``uv run`` from REPO_ROOT picks up the editable install
        # so the assertion runs against the same in-tree code the
        # sandbox's nx wheel was built from.
        # ``-m integration`` is required: the test module is marked
        # ``pytestmark = pytest.mark.integration`` and the project
        # default in pyproject.toml deselects that marker. Without
        # the flag pytest exits 5 ("no tests collected") and the
        # shakedown reads it as FAIL even though the regression
        # guard never ran.
        if (cd "$REPO_ROOT" && uv run pytest -x -q --no-header -m integration \
                tests/test_indexer_e2e.py::test_greenfield_index_writes_no_deprecated_keys \
                2>&1) | tail -5 | sed 's/^/  /'; then
            echo "  [pass] greenfield index produced 0 chunks with deprecated keys"
        else
            echo "  [FAIL] greenfield index leaked deprecated keys"
            echo "         nexus-e5uw regression: indexer is writing pruned keys."
            echo "         Investigate before merge."
            exit 1
        fi

        echo
        echo "── 6/11 cross-corpus search ──"
        nx search "catalog link graph" -m 3 2>&1 | tail -10 | sed 's/^/  /' || true

        echo
        echo "── 7/11 T2 memory roundtrip ──"
        SHAKE_TS=$(date +%s)
        nx memory put "shakedown marker $SHAKE_TS" \
            --project nexus_shakedown --title shakedown.md 2>&1 | tail -2 | sed 's/^/  /' || true
        nx memory get --project nexus_shakedown --title shakedown.md 2>&1 \
            | head -3 | sed 's/^/  /' || true

        echo
        echo "── 8/11 T1 scratch use (write + readback) ──"
        # Outside a Claude Code session, no SessionStart hook fires to
        # publish a T1 chroma address (RDR-105 hybrid discovery: env
        # passdown from MCP parent OR addr-file PPID walk to a claude
        # ancestor). With neither path available, ``nx scratch *`` fails
        # loud with ``T1ServerNotFoundError`` (the silent EphemeralClient
        # fallback was removed in 4.27.0 because it produced data-loss
        # bugs where put + list landed in different per-process clients).
        #
        # The shakedown opts into the documented escape hatch:
        # ``NX_T1_ISOLATED=1`` makes T1Database open an in-process
        # ``EphemeralClient`` for THIS invocation only. Cross-invocation
        # readback is still impossible without a real session — that's
        # tested by the cc-validation harness.
        SCRATCH_OUT=$(NX_T1_ISOLATED=1 nx scratch put "shakedown probe $SHAKE_TS" --tags=shakedown 2>&1 | tail -1)
        if echo "$SCRATCH_OUT" | grep -qE "Stored:"; then
            echo "  put: ok ($SCRATCH_OUT)"
        else
            echo "  put: [WARN] unexpected output — $SCRATCH_OUT"
        fi
        echo "  note: cross-invocation readback only works inside a Claude Code session"

        echo
        echo "── 9/11 catalog stats (registry + link graph readback) ──"
        nx catalog stats 2>&1 | head -15 | sed 's/^/  /' || true

        echo
        echo "── 10/11 nx doctor (all checks, post-activity) ──"
        for check in --check-schema --check-plan-library --check-taxonomy \
                     --check-tmpdirs; do
            echo "  $check:"
            nx doctor "$check" 2>&1 | tail -5 | sed 's/^/    /' || true
        done

        echo
        echo "── 11/11 nx catalog doctor (collections-drift release gate, nexus-o6aa.14) ──"
        # RDR-101 Phase 6: collections-drift is a release blocker.
        #
        # The indexer creates T3 collections on first write; the
        # collections projection is populated by ``nx catalog
        # backfill-collections`` (the documented remediation in the
        # doctor's own output). Run backfill THEN drift so the gate
        # validates the full create-register-check sequence rather
        # than failing on the transient unregistered window. A genuine
        # drift (orphan projection rows, missing T3 collections, or a
        # backfill that cannot reach a clean state) still surfaces
        # because the second check runs WITHOUT ``|| true`` and exits
        # non-zero on FAIL.
        echo "  [pre] nx catalog backfill-collections --no-dry-run:"
        # The verb defaults to --dry-run; the gate needs the actual
        # registration so the doctor's drift check sees the populated
        # projection on the next call.
        nx catalog backfill-collections --no-dry-run 2>&1 | tail -5 | sed 's/^/    /' || true
        echo "  [check] nx catalog doctor --collections-drift:"
        if ! nx catalog doctor --collections-drift 2>&1 | sed 's/^/    /'; then
            echo "  [fail] collections-drift survived backfill: release blocked"
            echo "         The projection cannot reach a steady state vs T3."
            echo "         Investigate before tagging."
            exit 1
        fi

        echo
        echo "── T1 sniff: AFTER ──"
        AFTER_SESSIONS=$( { ls "$HOME/.config/nexus/sessions/" 2>/dev/null || true; } | wc -l | tr -d ' ')
        AFTER_TMPDIRS=$( { ls -d "$T1_DIR_PARENT"/nx_t1_* 2>/dev/null || true; } | wc -l | tr -d ' ')
        echo "  session files: $AFTER_SESSIONS (was $BEFORE_SESSIONS)"
        echo "  tmpdirs:       $AFTER_TMPDIRS (was $BEFORE_TMPDIRS)"
        DELTA_S=$((AFTER_SESSIONS - BEFORE_SESSIONS))
        DELTA_T=$((AFTER_TMPDIRS - BEFORE_TMPDIRS))
        echo "  delta:         sessions+$DELTA_S  tmpdirs+$DELTA_T"
        if (( DELTA_S > 2 || DELTA_T > 2 )); then
            echo "  [WARN] T1 turd risk: net delta exceeds expected steady-state"
            echo "         Investigate $HOME/.config/nexus/sessions/ and $T1_DIR_PARENT/nx_t1_*"
        else
            echo "  [ok] T1 lifecycle within expected bounds"
        fi

        echo
        echo "[done] Sandbox state at $SANDBOX. Run '$0 reset' to tear down."
        ;;

    service)
        # RDR-157 P4.2 (bead nexus-vwvv5.18): fresh-machine -> serving with ZERO
        # manual steps, LOCAL mode. The sandbox HOME is the "fresh machine"; we
        # position the distribution artifact (what the release archive/launcher
        # ships), then prove a single `nx init --service` collapses
        # provision-PG -> fetch-bge-768 -> start-service -> /health green, is
        # idempotent on re-run, and tears down cleanly.
        export NX_LOCAL=1
        export NX_STORAGE_BACKEND=service
        unset CHROMA_API_KEY CHROMA_TENANT CHROMA_DATABASE
        echo "[3/3] Service E2E (LOCAL mode, fresh sandbox HOME=$SANDBOX):"
        cd /tmp

        # ── Artifact positioning (the launcher's job; P4.1 made the supervisor
        #    able to launch whatever is positioned here). Native binary wins. ──
        SVC_WELL_KNOWN="$HOME/.config/nexus/service/nexus-service"
        if [[ -n "${NEXUS_SERVICE_BIN:-}" && -x "${NEXUS_SERVICE_BIN}" ]]; then
            echo "  artifact: native binary (NEXUS_SERVICE_BIN=$NEXUS_SERVICE_BIN)"
        elif [[ -x "$SVC_WELL_KNOWN" ]]; then
            echo "  artifact: native binary (well-known $SVC_WELL_KNOWN)"
        else
            # Fall back to the repo-built JAR. P4.1 supports both; the native
            # binary is exercised on CI/release where it is built.
            JAR=""
            for cand in "$REPO_ROOT"/service/target/nexus-service-*.jar; do
                [[ -f "$cand" ]] || continue           # glob did not match
                [[ "$cand" == *-sources.jar ]] && continue
                JAR="$cand"
            done
            [[ -n "$JAR" ]] || _die "no service artifact: set NEXUS_SERVICE_BIN to a native binary, or build the JAR (cd service && mvn package -DskipTests -Pprebuilt-jooq -q)"
            echo "  artifact: JAR (dev fallback) $JAR"
            nx daemon service install-jar "$JAR" 2>&1 | tail -2 | sed 's/^/    /' \
                || _die "install-jar failed"
        fi

        # ── PG source (host PG on PATH, or a ship-alongside bundle). The bundle
        #    extract/initdb/provision is what nx init --service drives. ──
        if [[ -n "${NEXUS_PG_BUNDLE:-}" ]]; then
            echo "  pg source: ship-alongside bundle (NEXUS_PG_BUNDLE=$NEXUS_PG_BUNDLE)"
        elif command -v initdb >/dev/null 2>&1; then
            echo "  pg source: host PostgreSQL ($(command -v initdb))"
        else
            _die "no PostgreSQL: put initdb/pg_ctl on PATH (with pgvector) or set NEXUS_PG_BUNDLE to a ship-alongside bundle"
        fi

        # A field extractor for `nx daemon service status --json`. Keeps the
        # assertions below honest: we parse the actual health/port/pid, not just
        # exit codes (a stale lease can outlive a dead JVM for up to the TTL,
        # so "the command exited 0" is NOT proof of serving).
        _svc_field() {  # $1 = json key
            nx daemon service status --json 2>/dev/null \
                | python3 -c "import sys,json;print(json.load(sys.stdin).get('$1',''))" \
                2>/dev/null || true
        }

        # ── Teardown trap: once the service is up, ANY exit (including _die /
        #    SIGTERM) must stop the JVM + PG, never orphan them. ──
        _svc_teardown() {
            echo "  ── teardown (nx daemon service stop --with-pg) ──"
            nx daemon service stop --with-pg 2>&1 | tail -3 | sed 's/^/    /' || true
        }

        # ── The one command: fresh-install -> serving, zero manual steps. ──
        echo
        echo "  ── nx init --service (the one-command collapse) ──"
        if ! nx init --service 2>&1 | sed 's/^/    /'; then
            _die "nx init --service did not reach serving (see remedy above)"
        fi
        trap _svc_teardown EXIT

        # ── serving proof: /health == ok (NOT merely "a lease exists"). ──
        echo
        echo "  ── service health (must be ok) ──"
        nx daemon service status 2>&1 | sed 's/^/    /' || true
        HEALTH=$(_svc_field health)
        PORT1=$(_svc_field port); PID1=$(_svc_field pid)
        [[ "$HEALTH" == "ok" ]] || _die "service not serving: /health=$HEALTH (expected ok)"
        [[ -n "$PORT1" && -n "$PID1" ]] || _die "no endpoint published (port=$PORT1 pid=$PID1)"
        echo "    [ok] serving on port $PORT1 (pid $PID1), /health=ok"
        # Visibility only: embedding_mode is voyage-vs-onnx-local (driven by
        # whether a Voyage key is present), NOT a clean bge-768 signal, so we
        # report it but do not assert on it. The bge-768 LOCAL ONNX is fetched +
        # validated by `nx init --service` itself (fail-loud); the JAR-fallback
        # path here does not re-prove the Java service's model load.
        echo "    embedding_mode=$(_svc_field embedding_mode)"

        # ── idempotency: re-run must hit the live-lease short-circuit and
        #    return the SAME endpoint, not spawn a second service. ──
        echo
        echo "  ── nx init --service AGAIN (idempotent re-run) ──"
        if ! nx init --service 2>&1 | sed 's/^/    /'; then
            _die "re-run of nx init --service failed (not idempotent)"
        fi
        PORT2=$(_svc_field port); PID2=$(_svc_field pid)
        [[ "$PORT2" == "$PORT1" && "$PID2" == "$PID1" ]] \
            || _die "re-run was NOT idempotent: endpoint changed ($PORT1/$PID1 -> $PORT2/$PID2)"
        echo "    [ok] idempotent: same endpoint $PORT2 (pid $PID2)"

        # ── teardown via the trap; clear it so we report honestly below. ──
        echo
        trap - EXIT
        _svc_teardown
        # Re-confirm the lease is gone (teardown actually stopped the service).
        if [[ "$(_svc_field health)" == "ok" ]]; then
            _die "service still serving after stop --with-pg — teardown failed"
        fi

        echo
        echo "[done] Service E2E green: fresh sandbox -> serving in one command -> stopped."
        echo "       Sandbox state at $SANDBOX. Run '$0 reset' to tear down."
        ;;

    shell)
        echo "[3/3] Dropping into subshell with HOME=$SANDBOX ..."
        echo "      Exit the subshell to restore your real \$HOME."
        echo
        # Subshell: env stays sandboxed, exit returns control + restores HOME.
        cd "$SANDBOX"
        exec env \
            HOME="$SANDBOX" \
            PATH="$SANDBOX/.local/bin:$PATH" \
            VOYAGE_API_KEY="${VOYAGE_API_KEY:-}" \
            CHROMA_API_KEY="${CHROMA_API_KEY:-}" \
            CHROMA_TENANT="${CHROMA_TENANT:-}" \
            CHROMA_DATABASE="${CHROMA_DATABASE:-default_database}" \
            ANTHROPIC_API_KEY="${ANTHROPIC_API_KEY:-}" \
            NEXUS_SANDBOX=1 \
            PS1="(sandbox) $ " \
            bash --noprofile --norc -i
        ;;

    tmux)
        if ! command -v tmux >/dev/null 2>&1; then
            _die "tmux not installed (brew install tmux)"
        fi
        AUTH_DIR="$REPO_ROOT/tests/e2e/.claude-auth"
        if [[ ! -f "$AUTH_DIR/.credentials.json" ]]; then
            _die "missing $AUTH_DIR/.credentials.json — run tests/e2e/auth-login.sh first"
        fi
        # Reuse cc-validation lib for tmux primitives + claude_start.
        export TEST_HOME="$SANDBOX"
        export TMUX_SESSION
        echo "[3/3] Launching Claude Code in tmux session '$TMUX_SESSION' ..."
        echo "      Detach: Ctrl-b d   |   Kill: tmux kill-session -t $TMUX_SESSION"
        echo
        # shellcheck source=/dev/null
        . "$REPO_ROOT/tests/e2e/lib.sh"
        # Ensure auth credentials are reachable inside the sandbox HOME.
        mkdir -p "$SANDBOX/.claude"
        cp "$AUTH_DIR/.credentials.json" "$SANDBOX/.claude/.credentials.json"
        if tmux has-session -t "$TMUX_SESSION" 2>/dev/null; then
            tmux kill-session -t "$TMUX_SESSION"
        fi
        tmux new-session -d -s "$TMUX_SESSION" -x 220 -y 50 \
            "env HOME='$SANDBOX' PATH='$SANDBOX/.local/bin:$PATH' bash -i"
        sleep 1
        tmux send-keys -t "$TMUX_SESSION" "claude" Enter
        echo "Attaching ... (Ctrl-b d to detach without killing)"
        tmux attach -t "$TMUX_SESSION"
        ;;
esac
