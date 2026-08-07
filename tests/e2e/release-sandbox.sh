#!/usr/bin/env bash
# release-sandbox.sh — high-fidelity local pre-merge verification.
#
# Combines:
#   - scripts/reinstall-tool.sh   (wheel-equivalent install via uv tool)
#   - tests/e2e/sandbox.sh        (isolated $HOME for Claude Code state)
#   - tests/e2e/lib.sh            (tmux primitives, used by tmux mode)
#
# Why this exists: merging to main to "test things out" is dangerous because
# the wheel-install path (uv tool install) resolves package data and version-
# gated migrations differently from the editable install that pytest uses.
# Run this BEFORE pushing/merging anything that touches: install/packaging,
# T2 migrations, MCP servers, hooks, plugin manifests, commands that read
# T2/T3 state, or anything tagged "ships to users".
#
# Modes:
#   smoke     — install + activate + self-provision (engine + PG + bge-768
#               embedder) + post-install canary checks. ~4–6 min (cold cache
#               pays the native binary + PG bundle + ~416 MB ONNX download;
#               warm cache is faster).
#   shakedown — full ensemble: smoke + index a bounded fixture repo/pdf/rdr +
#               search/query/T1/T2 + link graph readback + T1 turd sniff.
#               ~15–30 min (bounded fixture corpus, nexus-m7kcv — the
#               unbounded full-repo index this used to run was ~2.5h at
#               sandbox local-embed speed and was never actually a routine
#               gate step in practice).
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

# critic Significant 5 (T2 [21599]): this machine's /bin/bash is confirmed
# 3.2.57, and a stripped-PATH LaunchAgent/cron invocation context has
# already been proven (elsewhere in this repo's e2e suite) to reach that
# bash instead of a Homebrew bash4+ on an interactive PATH. Under 3.2,
# bash4-only constructs (${VAR,,} lowercasing, associative arrays) fail at
# PARSE time with "bad substitution" — a cryptic failure mode, not a
# graceful one. Guard early so this script fails loud with a clear message
# instead, before any such construct (present or future) is reached. This
# guard line itself is deliberately 3.2-parseable (arithmetic + indexed
# arrays predate bash4) so the check can run under the very shell it is
# rejecting.
(( BASH_VERSINFO[0] >= 4 )) || _die "bash >= 4 required (found $BASH_VERSION) — invoke with an explicit bash4+ (e.g. /opt/homebrew/bin/bash $0 ...), not a stripped-PATH default"

# ── Local-service helpers (RDR-157 P4.2 / nexus-596jm) ──────────────────────
# Shared by the `service` mode below AND by `_provision_local_service` (used
# by smoke/shakedown). Factored out rather than duplicated per-mode.
_svc_field() {  # $1 = json key
    nx daemon service status --json 2>/dev/null \
        | python3 -c "import sys,json;print(json.load(sys.stdin).get('$1',''))" \
        2>/dev/null || true
}

_svc_teardown() {
    echo "  ── teardown (nx daemon service stop --with-pg) ──"
    nx daemon service stop --with-pg 2>&1 | tail -3 | sed 's/^/    /' || true

    # CRITICAL (substantive-critic, T2 [21599], script-side mitigation —
    # product-side fix tracked separately as nexus-f7t9e): `--with-pg`
    # silently no-ops when pg_credentials does not exist yet
    # (daemon.py service_stop_cmd: `if not creds_path.exists(): return`),
    # but pg_provision.provision() starts postgres (_start_cluster) BEFORE
    # writing credentials (_write_credentials) — three subprocess calls
    # (_create_db, _create_vector_extension, _create_roles) sit in that
    # gap. A crash mid-provision (elevated likelihood on this machine per
    # the documented pipe-degradation history) leaves a live postmaster
    # the command above cannot discover (no lease, no creds) — a leak the
    # NEXT run's --keep-existing warm-cache reuse then collides with
    # (orphaned process holding the old port/PGDATA). Fall back to direct
    # discovery, scoped strictly to the SANDBOX HOME's own PGDATA so this
    # never touches a real install's Postgres.
    local pgdata="$HOME/.config/nexus/postgres"
    if [[ -f "$pgdata/postmaster.pid" ]]; then
        echo "  [fallback] postmaster.pid still present under sandbox PGDATA after 'stop --with-pg' (credentials-write race) — stopping it directly"
        local pg_ctl_bin="$HOME/.config/nexus/pg-bundle/bundle/bin/pg_ctl"
        if [[ -x "$pg_ctl_bin" ]]; then
            echo "    using bundled pg_ctl: $pg_ctl_bin"
            "$pg_ctl_bin" -D "$pgdata" stop -m fast 2>&1 | sed 's/^/    /' || true
        elif command -v pg_ctl >/dev/null 2>&1; then
            echo "    using PATH pg_ctl: $(command -v pg_ctl)"
            pg_ctl -D "$pgdata" stop -m fast 2>&1 | sed 's/^/    /' || true
        else
            local pm_pid
            pm_pid=$(head -1 "$pgdata/postmaster.pid" 2>/dev/null || true)
            if [[ -n "$pm_pid" ]]; then
                echo "    no pg_ctl available anywhere — killing postmaster pid $pm_pid directly"
                # SIGINT = PG fast shutdown, matching the pg_ctl -m fast
                # branches above (SIGTERM is smart shutdown: waits on clients).
                kill -INT "$pm_pid" 2>/dev/null || true
            fi
        fi
    fi

    # nexus-m7kcv (second half): the formula-routing PDF indexer (shakedown
    # step 3b) lazily spawns a `mineru-api --host 127.0.0.1` daemon under
    # the sandbox HOME that outlives a plain teardown — observed leaked
    # after a TERM-killed test run. Match on the live process's own command
    # path (not just the process name) so this only ever targets a process
    # actually rooted under THIS sandbox HOME, never a real install's
    # mineru-api.
    local mineru_pid mineru_cmd
    for mineru_pid in $(pgrep -f "mineru-api" 2>/dev/null || true); do
        mineru_cmd=$(ps -p "$mineru_pid" -o command= 2>/dev/null || true)
        if [[ "$mineru_cmd" == *"$HOME"* ]]; then
            echo "  [fallback] terminating leftover sandbox mineru-api (pid $mineru_pid)"
            kill "$mineru_pid" 2>/dev/null || true
        fi
    done
}

# nexus-596jm: smoke and shakedown exercise substrate steps (plan reseed,
# index repo/pdf/rdr, cross-corpus search, T2 roundtrip) and doctor checks
# (--check-schema, --check-plan-library, bare doctor's vector-service row)
# that need a live storage service — but neither mode ever provisioned one.
# Those steps became fail-capable this cycle (6xkdu removed `|| true` from
# the indexing steps; fe6452a4 de-theatred doctor), so running them against
# an unprovisioned sandbox HOME is now a structural red, not a genuine
# defect signal: ServiceEndpointUnresolvableError with nothing behind it.
#
# Mirrors tests/e2e/fresh-install-mvv.sh's provisioning mechanics: plain
# `nx init` in LOCAL mode self-provisions the native binary + PG bundle +
# bge-768 embedder with zero manual positioning, so this stays self-
# contained — no dependence on the live install's config/lease (the live
# box is cloud-mode and must not be touched). NEXUS_SERVICE_BIN /
# NEXUS_PG_BUNDLE / NEXUS_SERVICE_TAG, if already exported by the caller,
# flow straight through (this script does not `env -i` scrub the way the
# MVV's virgin-box layer does), so a warm cache avoids re-downloading the
# signed binary + PG bundle + ~416 MB bge-768 ONNX on every run; combine
# with --keep-existing to preserve that cache across invocations of this
# script. Shares `_svc_field` / `_svc_teardown` above with the `service`
# mode below rather than each mode duplicating its own copy.
#
# Provisioning failure is always loud (_die), never a skip-pass. Teardown is
# installed as the EXIT trap BEFORE `nx init` runs (mirrors the MVV's
# `trap cleanup EXIT` set before its own install step) so a failure mid-
# provisioning still tears down whatever got partially started; `nx daemon
# service stop --with-pg` is a no-op-safe call even when nothing came up.
_provision_local_service() {
    echo "  ── self-provisioning local service (nexus-596jm) ──"
    trap '_svc_teardown; lock_release "$LOCKDIR" 2>/dev/null || true' EXIT
    if ! nx init -y --no-autostart 2>&1 | sed 's/^/    /'; then
        _die "nx init did not reach serving inside the sandbox (self-provisioning failed — see remedy above)"
    fi
    local health
    health=$(_svc_field health)
    [[ "$health" == "ok" ]] || _die "service not serving after provisioning: /health=$health (expected ok)"
    echo "    [ok] local service serving, /health=ok"
}

_print_help() {
    # printf rather than here-doc: bash here-docs hang in some non-
    # interactive shell contexts (Claude Code harness, certain CI
    # runners) where parent stdin is wired to a pipe the here-doc
    # machinery never closes. printf has no such dependency.
    printf '%s\n' \
        "Usage: $0 <mode> [options]" \
        "" \
        "Modes:" \
        "  smoke      Reinstall + activate + self-provision local service (engine + PG +" \
        "             bge-768 embedder) + run nx upgrade --dry-run + nx doctor checks." \
        "             Verifies the wheel install + migrations + health surface." \
        "             ~4–6 min (cold cache pays the native binary + PG bundle + ~416 MB" \
        "             ONNX download; warm cache with --keep-existing is faster)." \
        "  shakedown  Full ensemble: smoke + indexing a bounded fixture repo/pdf/rdr +" \
        "             cross-corpus search + T2 memory roundtrip + T1 scratch use +" \
        "             catalog link readback + T1 turd sniff. Exercises every pipeline" \
        "             against a fresh install." \
        "             ~15–30 min on warm cache, +10–15 min if MinerU models are not yet" \
        "             downloaded. Repo indexing (step 2/11) uses a hard-coded ~36-file" \
        "             fixture subset, not the full nexus tree (nexus-m7kcv — the full" \
        "             tree at sandbox local-embed speed is ~2.5h)." \
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

# RDR-184 P0.2 (nexus-ccs9v.2): serialize on the machine-global fixed
# resources this harness mutates — $HOME/nexus-sandbox and the fixed tmux
# session name. Lock dir lives under a stable machine-global temp root, NOT
# under this checkout — the sandbox HOME and tmux session are per-machine
# singleton resources, so two different checkouts on the same host must
# still serialize. Acquired here, after option parsing/help dispatch (usage
# errors don't need the lock) but strictly before the first mutation (the
# "reset"/Step-1 sandbox rm -rf just below). Lock dir is a HARD-CODED /tmp
# path, deliberately NOT ${TMPDIR:-/tmp} (code-review SIGNIFICANT fix): on
# darwin, an interactive shell's TMPDIR is a per-user /var/folders/... path
# while a LaunchAgent/CI/stripped-env invocation sees plain /tmp — two
# different invocation contexts would silently compute DIFFERENT lockdirs
# and never contend, defeating the whole point of a machine-global guard
# (this repo runs LaunchAgents that could race an interactive run). /tmp is
# always the same path across every context on the same host.
# shellcheck source=./lib/lock.sh disable=SC1091
source "$SCRIPT_DIR/lib/lock.sh"
LOCKDIR="/tmp/nexus-e2e-locks/release-sandbox.lock"
mkdir -p "$(dirname "$LOCKDIR")"
lock_acquire "$LOCKDIR" || exit 1
trap 'lock_release "$LOCKDIR" 2>/dev/null || true' EXIT
echo "[rdr-184] lock acquired: $LOCKDIR (pid $$)" >&2
# Test seam (RDR-184 P0.2, nexus-ccs9v.2): tests/e2e/lib/harness_lock_test.sh
# sets this to prove a concurrent invocation gets PAST the lock without ever
# running this harness's real body (reinstall / index / sandbox rm -rf).
# No-op — unset in every normal invocation.
[[ -n "${NX_E2E_LOCK_SELFTEST:-}" ]] && exit 0

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

# ── Step 1 — create sandbox HOME ─────────────────────────────────────────────

if [[ -d "$SANDBOX" && $KEEP_EXISTING -eq 0 ]]; then
    echo "[1/3] Recreating sandbox at $SANDBOX (use --keep-existing to reuse)"
    rm -rf "$SANDBOX"
elif [[ -d "$SANDBOX" ]]; then
    echo "[1/3] Reusing existing sandbox at $SANDBOX"
fi

if [[ ! -d "$SANDBOX" ]]; then
    echo "[1/3] Creating fresh sandbox at $SANDBOX ..."
    NX_E2E_LOCK_HELD_BY_PARENT=1 "$REPO_ROOT/tests/e2e/sandbox.sh" >/dev/null
fi

# Activate the sandbox HOME/PATH BEFORE the reinstall below (not after, as
# this script did until nexus-h9f1w-follow-up): `uv tool install` resolves
# its default install location off $HOME (~/.local/share/uv/tools,
# ~/.local/bin — confirmed via `HOME=X uv tool dir`), so activating first
# makes the reinstall land entirely inside $SANDBOX/.local/{share/uv/tools,
# bin} — genuinely isolated from the live global conexus venv. Running the
# reinstall against the REAL $HOME (the old order) swapped the live venv
# out from under every concurrent Claude Code session's nx-mcp servers +
# T2/service daemons, tripping reinstall-tool.sh's live-holder guard with
# no safe remedy short of --force (unsafe) or --cycle-daemons (which can't
# even touch MCP-server subprocesses, only nx-managed daemons).
# shellcheck source=/dev/null
. "$SANDBOX/activate"

# ── Step 2 — reinstall into the now-isolated sandbox tool dir (unless skipped) ──

if (( SKIP_INSTALL == 0 )); then
    echo "[2/3] Reinstalling nx CLI from $REPO_ROOT (isolated: HOME=$SANDBOX) ..."
    (cd "$REPO_ROOT" && uv sync >/dev/null 2>&1)
    "$REPO_ROOT/scripts/reinstall-tool.sh" >/dev/null
    echo "      $(nx --version 2>/dev/null || echo 'nx --version failed')"
else
    echo "[2/3] Skipping reinstall (--skip-install). nx version: $(nx --version 2>/dev/null || echo 'unknown')"
fi

# ── Step 3 — execute mode ────────────────────────────────────────────────────

case "$MODE" in
    smoke)
        # Force local mode so the smoke does not contact ChromaDB Cloud
        # even if the parent shell has CHROMA_* set. The sandbox HOME is
        # empty by design — there is no cloud data to populate from. Same
        # pattern shakedown uses below; tests/e2e/run.sh has the long
        # explanation.
        export NX_LOCAL=1
        unset CHROMA_API_KEY CHROMA_TENANT CHROMA_DATABASE VOYAGE_API_KEY
        echo "[3/3] Smoke checks (running from /tmp, NX_LOCAL=1):"
        cd /tmp
        _provision_local_service
        echo "  nx --version: $(nx --version)"
        echo
        echo "  nx upgrade --dry-run:"
        nx upgrade --dry-run 2>&1 | sed 's/^/    /' || true
        echo
        echo "  nx upgrade (apply):"
        nx upgrade 2>&1 | sed 's/^/    /' || true
        echo
        # nx plan reseed seeds the builtin plan templates that
        # --check-plan-library verifies. Without this step the doctor
        # check fails on every fresh sandbox — that is "you forgot the
        # second setup step", not "something is genuinely broken." Make
        # smoke green-green-green when the install is healthy.
        #
        # nexus-vl8lk: this used to be `nx catalog setup`, which is
        # RETIRED (raises ClickException — the catalog is engine-owned,
        # nothing left here to set up) and had been silently swallowed by
        # `|| true` ever since, so the seeding half of this step had
        # already stopped running before --check-plan-library was ported
        # off its N/A stub. `nx plan reseed` is the live equivalent —
        # idempotent, routes through HttpPlanLibrary in every mode.
        echo "  nx plan reseed (seeds plan library):"
        nx plan reseed 2>&1 | tail -5 | sed 's/^/    /' || true
        echo
        # A gate that PRINTS a failure and exits 0 is not a gate. This loop
        # detected failures correctly (set -euo pipefail at the top makes the
        # pipeline carry nx doctor's status into the `if`) and then dropped
        # them on the floor: the caller saw exit 0 with "[FAIL]" in the log.
        # Found by the 7.0.0 release dry run, where --check-taxonomy failed and
        # the gate reported success.
        SMOKE_FAILED=()
        for check in --check-schema --check-plan-library --check-taxonomy; do
            echo "  nx doctor $check:"
            if nx doctor "$check" 2>&1 | sed 's/^/    /'; then
                echo "    [pass]"
            else
                echo "    [FAIL] -- exit non-zero" >&2
                SMOKE_FAILED+=("$check")
            fi
            echo
        done
        echo "[done] Sandbox state at $SANDBOX. Run '$0 reset' to tear down."
        if (( ${#SMOKE_FAILED[@]} )); then
            echo >&2
            echo "SMOKE FAILED: ${#SMOKE_FAILED[@]} doctor check(s) exited non-zero:" >&2
            printf '  nx doctor %s\n' "${SMOKE_FAILED[@]}" >&2
            exit 1
        fi
        echo "SMOKE PASSED: all doctor checks green."
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
        unset CHROMA_API_KEY CHROMA_TENANT CHROMA_DATABASE VOYAGE_API_KEY
        echo "[3/3] Shakedown: full pipeline ensemble (running from /tmp, NX_LOCAL=1)"
        cd /tmp
        _provision_local_service

        # nexus-6xkdu: the four indexing steps (2, 3a, 3b, 4) previously ran
        # under `|| true`, so a broken indexer — including a broken MinerU
        # path — could not turn this run red. Collect-and-continue rather
        # than abort-on-first: a failure here does not forfeit the
        # diagnostic value of the remaining steps (search, T2, T1, catalog
        # readback), but it DOES turn the final verdict FAILED. Mirrors the
        # SMOKE_FAILED pattern in the smoke arm above.
        SHAKEDOWN_FAILED=()

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
        echo "── 1/11 nx plan reseed (seeds plan library) ──"
        # nexus-vl8lk: was `nx catalog setup` (retired, raised, swallowed by
        # `|| true`) — see the smoke arm's comment above for the full story.
        nx plan reseed 2>&1 | tail -5 | sed 's/^/  /' || true

        echo
        echo "── 2/11 nx index repo (bounded fixture corpus) ──"
        # nexus-m7kcv (Hal-approved gate-scope decision, 2026-08-07):
        # indexing the FULL repo at sandbox local-embed speed (bge-768
        # ONNX, no Voyage key in the sandbox — ~2.8 chunks/s) is ~2.5h, so
        # this step was never actually completing inside anything
        # resembling a routine gate run ("shakedown scope" only in name —
        # vacuous-era affordability). Build a deterministic, explicitly
        # hard-coded fixture subset instead: a representative slice of
        # src/nexus/catalog (all 17 files — the module this shakedown's
        # own step 11 exercises via catalog doctor), a sample of
        # src/nexus/commands, and a handful of docs/*.md including one
        # AGENTS.md. Hard-coded rather than globbed so the corpus stays
        # reproducible run-to-run and reviewable in a diff — a glob would
        # silently pull in whatever the tree grows to.
        FIXTURE_FILES=(
            src/nexus/catalog/__init__.py
            src/nexus/catalog/auto_linker.py
            src/nexus/catalog/catalog_protocol.py
            src/nexus/catalog/catalog_spans.py
            src/nexus/catalog/chunk_quarantine.py
            src/nexus/catalog/collection_name.py
            src/nexus/catalog/dt_link_generator.py
            src/nexus/catalog/factory.py
            src/nexus/catalog/http_catalog_client.py
            src/nexus/catalog/link_generator.py
            src/nexus/catalog/manifest_backfill.py
            src/nexus/catalog/manifest_heal.py
            src/nexus/catalog/orphan_backfill.py
            src/nexus/catalog/store_hook.py
            src/nexus/catalog/tumbler.py
            src/nexus/catalog/types.py
            src/nexus/catalog/write_priority.py
            src/nexus/commands/catalog.py
            src/nexus/commands/daemon.py
            src/nexus/commands/doctor.py
            src/nexus/commands/index.py
            src/nexus/commands/init.py
            src/nexus/commands/memory.py
            src/nexus/commands/scratch.py
            src/nexus/commands/search_cmd.py
            src/nexus/commands/store.py
            src/nexus/commands/upgrade.py
            docs/architecture.md
            docs/catalog.md
            docs/cli-reference.md
            docs/configuration.md
            docs/contributing.md
            docs/getting-started.md
            docs/repo-indexing.md
            docs/storage-tiers.md
            AGENTS.md
        )
        FIXTURE_DIR="$SANDBOX/shakedown-fixture"
        rm -rf "$FIXTURE_DIR"
        for f in "${FIXTURE_FILES[@]}"; do
            src="$REPO_ROOT/$f"
            [[ -f "$src" ]] || _die "shakedown fixture file missing from repo: $f (FIXTURE_FILES in $0 is stale — update it)"
            dest="$FIXTURE_DIR/$f"
            mkdir -p "$(dirname "$dest")"
            cp "$src" "$dest"
        done
        # nx index repo refuses a path with no .git (nexus-git-guard), so
        # the fixture needs a real (throwaway) repo, not just loose files.
        # Stage by the same explicit FIXTURE_FILES list used to populate
        # the tree — never a blanket `git add -A` — so a stray file
        # dropped into $FIXTURE_DIR by a future edit can't silently widen
        # the indexed corpus.
        (cd "$FIXTURE_DIR" \
            && git init -q \
            && git add "${FIXTURE_FILES[@]}" \
            && git -c user.email=shakedown@nexus.local -c user.name=shakedown \
                   commit -q -m "shakedown fixture snapshot")
        echo "  fixture: ${#FIXTURE_FILES[@]} files under $FIXTURE_DIR"
        if ! nx index repo "$FIXTURE_DIR" 2>&1 | tail -5 | sed 's/^/  /'; then
            echo "  [FAIL] nx index repo exited non-zero" >&2
            SHAKEDOWN_FAILED+=("2/11 nx index repo")
        fi

        echo
        echo "── 3a/11 nx index pdf (tc-sql.pdf — Docling path, no formulas) ──"
        if ! nx index pdf "$REPO_ROOT/tests/fixtures/tc-sql.pdf" \
                --collection knowledge__shakedown 2>&1 | tail -5 | sed 's/^/  /'; then
            echo "  [FAIL] nx index pdf (tc-sql.pdf, Docling path) exited non-zero" >&2
            SHAKEDOWN_FAILED+=("3a/11 nx index pdf (Docling path)")
        fi

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
        #
        # nexus-6xkdu: this step used to run under `|| true`, which is the
        # reason the "shakedown covers MinerU end-to-end" docstrings
        # scattered across the pytest suite were false — the step could not
        # fail even when MinerU itself was broken. Now propagated: a
        # non-zero exit here is collected into SHAKEDOWN_FAILED and turns
        # the final verdict red.
        if ! nx index pdf "$REPO_ROOT/tests/fixtures/bft-to-smr.pdf" \
                --collection knowledge__shakedown 2>&1 | tail -5 | sed 's/^/  /'; then
            echo "  [FAIL] nx index pdf (bft-to-smr.pdf, MinerU path) exited non-zero" >&2
            SHAKEDOWN_FAILED+=("3b/11 nx index pdf (MinerU path)")
        fi

        echo
        echo "── 4/11 nx index rdr ──"
        if ! nx index rdr "$REPO_ROOT" 2>&1 | tail -5 | sed 's/^/  /'; then
            echo "  [FAIL] nx index rdr exited non-zero" >&2
            SHAKEDOWN_FAILED+=("4/11 nx index rdr")
        fi

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
        # critic Significant 2 (T2 [21599]): de-theatre — the sandbox now
        # has a real, provisioned service and (since the fixture corpus
        # above) real indexed content to find, so an empty result here is
        # a genuine search/embed-pipeline signal, not "the sandbox happens
        # to be empty". "catalog link graph" is chosen to land in the
        # step-2 fixture (docs/catalog.md, docs/storage-tiers.md,
        # docs/architecture.md all use that exact phrase; it also appears
        # in a handful of RDR docs indexed by step 4, so hits may come
        # from either corpus — the assert is "the pipeline finds indexed
        # content", not fixture-exclusivity). `nx search` exits 0 even on
        # zero hits (not an error condition for interactive use), so the
        # check is on the literal "No results." text — but a NON-ZERO
        # exit (real search/embed failure) must land in the verdict too,
        # not trip errexit past the collect-and-continue convention.
        if ! SEARCH_OUT=$(nx search "catalog link graph" -m 3 2>&1); then
            echo "$SEARCH_OUT" | tail -10 | sed 's/^/  /'
            echo "  [FAIL] nx search exited non-zero" >&2
            SHAKEDOWN_FAILED+=("6/11 nx search (non-zero exit)")
        else
            echo "$SEARCH_OUT" | tail -10 | sed 's/^/  /'
            if [[ "$SEARCH_OUT" == *"No results."* ]]; then
                echo "  [FAIL] cross-corpus search returned no hits (expected indexed-content hits)" >&2
                SHAKEDOWN_FAILED+=("6/11 nx search (no hits)")
            fi
        fi

        echo
        echo "── 7/11 T2 memory roundtrip ──"
        SHAKE_TS=$(date +%s)
        if ! nx memory put "shakedown marker $SHAKE_TS" \
                --project nexus_shakedown --title shakedown.md 2>&1 | tail -2 | sed 's/^/  /'; then
            echo "  [FAIL] nx memory put exited non-zero" >&2
            SHAKEDOWN_FAILED+=("7/11 nx memory put")
        fi
        if ! MEMORY_GET_OUT=$(nx memory get --project nexus_shakedown --title shakedown.md 2>&1); then
            # || true: head is an early-exit consumer — under pipefail a
            # SIGPIPE'd echo would abort the script BEFORE the [FAIL]
            # bookkeeping below (nexus-6zxfb, same class as nexus-i66g4).
            echo "$MEMORY_GET_OUT" | head -3 | sed 's/^/  /' || true
            echo "  [FAIL] nx memory get exited non-zero" >&2
            SHAKEDOWN_FAILED+=("7/11 nx memory get (non-zero exit)")
        else
            echo "$MEMORY_GET_OUT" | head -3 | sed 's/^/  /' || true
            if [[ "$MEMORY_GET_OUT" != *"$SHAKE_TS"* ]]; then
                echo "  [FAIL] T2 memory get did not return the marker written above" >&2
                SHAKEDOWN_FAILED+=("7/11 nx memory get roundtrip")
            fi
        fi

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
        if [[ "$SCRATCH_OUT" == *"Stored:"* ]]; then
            echo "  put: ok ($SCRATCH_OUT)"
        else
            echo "  put: [WARN] unexpected output — $SCRATCH_OUT"
        fi
        echo "  note: cross-invocation readback only works inside a Claude Code session"

        echo
        echo "── 9/11 catalog stats (registry + link graph readback) ──"
        # Deliberately informational-only, not fail-capable (unlike 6/7
        # above): `nx catalog stats` has no pass/fail contract of its own
        # to assert on — it's a readback for human eyeballing, and the
        # collections-drift release gate that DOES have a fail contract
        # over the same catalog state runs explicitly at 11/11 below.
        nx catalog stats 2>&1 | head -15 | sed 's/^/  /' || true

        echo
        echo "── 10/11 nx doctor (all checks, post-activity) ──"
        # (--check-tmpdirs retired at RDR-155 P4b with the chroma T1 tmpdirs.)
        # BARE `nx doctor` runs run_health_checks() — the corpus-integrity
        # instruments (chash conformance, stale index-run fences, dangling
        # manifests) live ONLY there, not behind any --check-* flag. The
        # strandedness audit (T2 [21590], 2026-08-07) found this step had
        # never invoked it, so the release gate was blind to all three and
        # `|| true` meant even the flag-scoped checks could not redden the
        # run. Fail-capable now, per the 6xkdu de-theatre precedent.
        #
        # nexus-u88vu: this used to call `_fail`, which is undefined anywhere
        # in this script — the first honest doctor red died with rc=127
        # "_fail: command not found" instead of reporting the failure.
        # Route through the same SHAKEDOWN_FAILED collect-and-continue
        # convention (6xkdu) every other fail-capable step in this arm
        # already uses, so a doctor red produces an explicit verdict line
        # rather than crashing the script.
        if ! nx doctor 2>&1 | sed 's/^/  /'; then
            echo "  [FAIL] nx doctor exited non-zero" >&2
            SHAKEDOWN_FAILED+=("10/11 nx doctor")
        fi
        for check in --check-schema --check-plan-library --check-taxonomy; do
            echo "  $check:"
            if ! nx doctor "$check" 2>&1 | tail -5 | sed 's/^/    /'; then
                echo "  [FAIL] nx doctor $check exited non-zero" >&2
                SHAKEDOWN_FAILED+=("10/11 nx doctor $check")
            fi
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
        if (( ${#SHAKEDOWN_FAILED[@]} )); then
            echo >&2
            echo "SHAKEDOWN FAILED: ${#SHAKEDOWN_FAILED[@]} release-gate step(s) exited non-zero:" >&2
            printf '  %s\n' "${SHAKEDOWN_FAILED[@]}" >&2
            exit 1
        fi
        echo "SHAKEDOWN PASSED: all release-gate steps green."
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
        unset CHROMA_API_KEY CHROMA_TENANT CHROMA_DATABASE VOYAGE_API_KEY
        echo "[3/3] Service E2E (LOCAL mode, fresh sandbox HOME=$SANDBOX):"
        cd /tmp

        # ── Artifact positioning. RDR-161: the native binary is the SOLE launch
        #    artifact — the java -jar fallback is expunged, so there is no
        #    repo-JAR dev fallback any more. Provide a native binary via
        #    NEXUS_SERVICE_BIN, the well-known location (e.g. from
        #    `nx daemon service install-binary <engine-service-v* tag>`), or
        #    `nx init --service` (which acquires + verifies it). ──
        SVC_WELL_KNOWN="$HOME/.config/nexus/service/nexus-service"
        if [[ -n "${NEXUS_SERVICE_BIN:-}" && -x "${NEXUS_SERVICE_BIN}" ]]; then
            echo "  artifact: native binary (NEXUS_SERVICE_BIN=$NEXUS_SERVICE_BIN)"
        elif [[ -x "$SVC_WELL_KNOWN" ]]; then
            echo "  artifact: native binary (well-known $SVC_WELL_KNOWN)"
        else
            _die "no native service binary: set NEXUS_SERVICE_BIN to a native nexus-service binary, or install one with 'nx daemon service install-binary <engine-service-v* tag>' (RDR-161: the java -jar path is expunged; there is no repo-JAR fallback)"
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

        # `_svc_field` / `_svc_teardown` are defined once near the top of this
        # script (shared with `_provision_local_service`, used by smoke/
        # shakedown) — keeps the actual health/port/pid parsing honest in one
        # place rather than duplicated per-mode (a stale lease can outlive a
        # dead JVM for up to the TTL, so "the command exited 0" is NOT proof
        # of serving).

        # ── The one command: fresh-install -> serving, zero manual steps. ──
        echo
        # --no-autostart (RDR-174 P2.4): the sandbox proves the SESSION-start
        # path (and its idempotent live-lease short-circuit below); it does not
        # want a persistent OS unit. Explicit since pre-P2.4 plain `nx init
        # --service` always session-started.
        echo "  ── nx init --service (the one-command collapse) ──"
        if ! nx init --service --no-autostart 2>&1 | sed 's/^/    /'; then
            _die "nx init --service did not reach serving (see remedy above)"
        fi
        # RDR-184 P0.2 (nexus-ccs9v.2): chain the top-level lock release into
        # this mode's own teardown trap — this assignment REPLACES the
        # top-level trap set earlier, so the lock release must be re-added
        # here or a crash in this window would leak the lock.
        trap '_svc_teardown; lock_release "$LOCKDIR" 2>/dev/null || true' EXIT

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
        if ! nx init --service --no-autostart 2>&1 | sed 's/^/    /'; then
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
        # trap - EXIT above cleared the combined teardown+lock-release trap,
        # so release explicitly now that teardown has run manually (RDR-184
        # P0.2, nexus-ccs9v.2).
        lock_release "$LOCKDIR" 2>/dev/null || true
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
        # NOT `exec` (RDR-184 P0.2, nexus-ccs9v.2): exec replaces this
        # script's own process image, which would skip the EXIT trap
        # entirely and leak the lock for the whole interactive session. Run
        # as a plain child instead — the lock stays held for the duration
        # of the interactive sandbox use (the correct semantics: another
        # invocation must not touch this sandbox while it is in use), and
        # releases via the normal EXIT trap once the user exits the shell.
        cd "$SANDBOX"
        env \
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
