#!/usr/bin/env bash
# nexus-cfgo9 — PACKAGE-UPGRADE convergence MVV. Runs INSIDE the container.
#
# Reproduces the exact GH #1402 failure shape and proves the fix:
#
#   pip install conexus==$PREV_RELEASE   real PREVIOUS PyPI release
#   nx init --service                    cold-acquires its OWN engine
#                                         ($PREV_ENGINE_TAG) from GitHub
#   seed a pre-upgrade T1 scratch row
#   uv pip install --reinstall <worktree wheel>   PACKAGE upgrade ONLY —
#                                         the engine binary is NEVER touched
#                                         by this harness from here on
#   nx daemon restart-stale              the convergence pass under test:
#                                         must acquire $NEW_ENGINE_TAG for
#                                         real (published release, no local
#                                         supply) and cycle the service
#   assert: /version == $NEW_ENGINE_TAG, service healthy, chash probe runs
#           the VIEW-era statements (no legacy fallback), T1 round-trips and
#           the pre-upgrade row survived the cycle.
#
# UPGRADE TARGET (Stage 4): run.sh stages EITHER the working-tree wheel
# (default) OR, when NEXUS_TARGET_RELEASE was set on the host, the REAL
# published PyPI wheel for that version (sha256-verified against PyPI's own
# JSON API before it ever reaches this box) — see run.sh's own header for
# the mechanics. Either way the file lands at the SAME path
# ($HOME/worktree-wheel/conexus-*.whl); this script does not need to know
# which one it is to install it. TARGET_RELEASE (optional; forwarded by
# run.sh only in published-target mode) exists PURELY so this script's own
# logging and verdict line can NAME which axis ran — a log reader must never
# have to guess "worktree" vs "published X.Y.Z" (nexus-86mx2, 2026-08-14).
#
# JOURNEY OWNERSHIP: this script owns the UPGRADE axis (an EXISTING install's
# PACKAGE moving forward while its engine converges). It does not test
# whether a client that has NEVER upgraded can WRITE against a newer engine
# fresh — that is tests/e2e/published-client-write-gate.sh's axis (nexus-
# 86mx2). Neither substitutes for the other.
set -uo pipefail

PREV_RELEASE="${PREV_RELEASE:?PREV_RELEASE must be set (e.g. 6.9.0)}"
PREV_ENGINE_TAG="${PREV_ENGINE_TAG:?PREV_ENGINE_TAG must be set (e.g. engine-service-v0.1.42)}"
NEW_ENGINE_TAG="${NEW_ENGINE_TAG:?NEW_ENGINE_TAG must be set (e.g. engine-service-v0.1.43)}"
TARGET_RELEASE="${TARGET_RELEASE:-}"
PREV_EXPECT="${PREV_ENGINE_TAG#engine-service-v}"
NEW_EXPECT="${NEW_ENGINE_TAG#engine-service-v}"
if [ -n "$TARGET_RELEASE" ]; then
  UPGRADE_TARGET_LABEL="published conexus $TARGET_RELEASE"
else
  UPGRADE_TARGET_LABEL="working tree"
fi
FAILS=0

say()  { printf '\n\033[1m== %s ==\033[0m\n' "$*"; }
ok()   { printf '  \033[32mPASS\033[0m %s\n' "$*"; }
bad()  { printf '  \033[31mFAIL\033[0m %s\n' "$*"; FAILS=$((FAILS+1)); }
note() { printf '       %s\n' "$*"; }

# Evidence dump for the convergence leg. `nx daemon restart-stale` drives the
# stop/start cycle INSIDE the product, so when it reports "still running the
# old engine" the harness log otherwise shows only the verdict. This prints
# the lease, the live process table (read from /proc -- this box deliberately
# ships no procps, see Dockerfile.package-upgrade), and the tail of every log
# the supervisor writes. Cheap, and it runs on the happy path too so a PASS
# still records what a healthy cycle looks like.
_diag() {
  printf '\n       ---- diag: %s ----\n' "$1"
  printf '       [lease]\n'
  ls -la "$HOME"/.config/nexus/storage_service_addr.* 2>&1 | sed 's/^/       /'
  cat "$HOME"/.config/nexus/storage_service_addr.* 2>&1 | sed 's/^/       /'
  printf '\n       [conexus processes, from /proc]\n'
  for p in /proc/[0-9]*; do
    printf '%s %s\n' "${p#/proc/}" "$(tr '\0' ' ' < "$p/cmdline" 2>/dev/null)"
  done 2>/dev/null | grep -iE "nexus|nxenv" | sed 's/^/       /'
  printf '       [status --json]\n'
  nx daemon service status --json 2>&1 | head -40 | sed 's/^/       /'
  for f in storage_service.log storage_service_native.log storage_service.crash.log; do
    printf '       [tail %s]\n' "$f"
    tail -25 "$HOME/.config/nexus/logs/$f" 2>&1 | sed 's/^/       /'
  done
  printf '       ---- end diag: %s ----\n\n' "$1"
}

export NX_SERVICE_MAX_HEAP="${NX_SERVICE_MAX_HEAP:-1g}"
git config --global user.email "package-upgrade@nexus.local" >/dev/null 2>&1 || true
git config --global user.name  "nexus package-upgrade"       >/dev/null 2>&1 || true

# ── Quarantine: nothing pre-staged (same posture as Dockerfile.cold) ─────────
say "Quarantine — nothing pre-staged"
command -v initdb >/dev/null 2>&1 && bad "system PostgreSQL present — not a clean box" || ok "no system PostgreSQL (bundle must provide it)"
test ! -e "$HOME/.config/nexus/service/nexus-service" && ok "no native binary pre-staged" || bad "native binary already present"

# ── Stage 1: install the PREVIOUS release from real PyPI ─────────────────────
say "Stage 1 — pip install conexus==$PREV_RELEASE (real PyPI)"
if uv pip install --python "$HOME/nxenv" "conexus==$PREV_RELEASE" 2>&1 | tail -5 | sed 's/^/       /'; then
  ok "installed conexus==$PREV_RELEASE from PyPI"
else
  bad "pip install conexus==$PREV_RELEASE failed"; say "ABORT"; exit 1
fi
GOT_VER="$(nx --version 2>&1 | grep -oE '[0-9]+\.[0-9]+\.[0-9]+' | head -1)"
[ "$GOT_VER" = "$PREV_RELEASE" ] && ok "nx --version reports $GOT_VER" \
  || bad "nx --version reports $GOT_VER, expected $PREV_RELEASE"

# ── Stage 2: nx init --service — the PREVIOUS release cold-acquires ITS OWN
#    engine (whatever it was built and tested against). No override — this
#    proves 6.9.0's own PINNED_SERVICE_TAG resolves to $PREV_ENGINE_TAG.
say "Stage 2 — nx init --service (real cold-acquire of $PREV_ENGINE_TAG)"
unset NEXUS_SERVICE_TAG NX_SERVICE_TAG 2>/dev/null || true
if nx init --service --embedder bge-768 --yes 2>&1 | sed 's/^/       /'; then
  ok "nx init --service (provisioned PG + $PREV_ENGINE_TAG + started)"
else
  bad "nx init --service failed"; say "ABORT (provision failed)"; exit 1
fi
export NX_STORAGE_BACKEND=service
# shellcheck disable=SC1091
[ -f "$HOME/.config/nexus/pg_credentials" ] && { set -a; . "$HOME/.config/nexus/pg_credentials"; set +a; }
unset NX_SERVICE_URL NX_SERVICE_PORT NX_SERVICE_HOST 2>/dev/null || true

_wait_healthy() {
  local tries="${1:-30}" i
  for i in $(seq 1 "$tries"); do
    if nx daemon service status 2>&1 | grep -qiE "health.*ok|healthy|serving|status.*ok|running"; then
      return 0
    fi
    sleep 2
  done
  return 1
}

if _wait_healthy 30; then
  ok "service healthy on the PREVIOUS release's own engine ($PREV_ENGINE_TAG)"
else
  nx daemon service status 2>&1 | sed 's/^/       /' || true
  bad "service did not reach healthy on $PREV_RELEASE"; say "ABORT"; exit 1
fi

_release_version() {
  nx daemon service status --json 2>/dev/null \
    | python3 -c 'import sys,json;print(json.load(sys.stdin).get("service_release_version") or "")' 2>/dev/null
}
RV="$(_release_version)"
if [ "$RV" = "$PREV_EXPECT" ]; then
  ok "/version release_version=$RV (the previous release's own engine)"
else
  bad "/version release_version=$RV, expected $PREV_EXPECT (wrong starting engine)"
fi

# ── Stage 3: seed a PRE-UPGRADE T1 row that must survive the service cycle ──
say "Stage 3 — seed a pre-upgrade T1 scratch row"
MARKER="pre-upgrade-marker-$$-$(date +%s)"
PUT_OUT="$(nx scratch put "$MARKER" --tags rehearsal-cfgo9 2>&1)"
printf '%s\n' "$PUT_OUT" | sed 's/^/       /'
PRE_ID="$(printf '%s\n' "$PUT_OUT" | grep -oE '[0-9a-fA-F-]{8,}' | tail -1)"
if [ -n "$PRE_ID" ]; then
  ok "seeded pre-upgrade T1 row $PRE_ID"
else
  bad "could not parse a T1 entry id from: $PUT_OUT"; say "ABORT"; exit 1
fi

# ── Stage 4: PACKAGE upgrade ONLY — the engine binary is NEVER touched from
#    here on. This is the exact GH #1402 shape: `uv tool upgrade conexus`
#    moves the client, the engine on disk does not move by itself.
say "Stage 4 — PACKAGE upgrade only (uv pip install --reinstall <$UPGRADE_TARGET_LABEL wheel>)"
WHEEL="$(ls "$HOME"/worktree-wheel/conexus-*.whl 2>/dev/null | head -1)"
if [ -z "$WHEEL" ]; then
  bad "no upgrade-target wheel found in $HOME/worktree-wheel/"; say "ABORT"; exit 1
fi
BEFORE_SHA="$(sha256sum "$HOME/.config/nexus/service/nexus-service" 2>/dev/null | awk '{print $1}')"
if uv pip install --python "$HOME/nxenv" --reinstall "$WHEEL" 2>&1 | tail -5 | sed 's/^/       /'; then
  ok "package upgraded to $UPGRADE_TARGET_LABEL ($WHEEL)"
else
  bad "package upgrade failed"; say "ABORT"; exit 1
fi
if [ -n "$TARGET_RELEASE" ]; then
  GOT_CLIENT_VER="$(nx --version 2>&1 | grep -oE '[0-9]+\.[0-9]+\.[0-9]+' | head -1)"
  [ "$GOT_CLIENT_VER" = "$TARGET_RELEASE" ] \
    && ok "nx --version reports $GOT_CLIENT_VER (matches published target $TARGET_RELEASE — the PUBLISHED bytes, not a worktree rebuild)" \
    || bad "nx --version reports $GOT_CLIENT_VER, expected published target $TARGET_RELEASE"
fi
AFTER_SHA="$(sha256sum "$HOME/.config/nexus/service/nexus-service" 2>/dev/null | awk '{print $1}')"
if [ "$BEFORE_SHA" = "$AFTER_SHA" ]; then
  ok "engine binary untouched by the package upgrade itself (sha256 unchanged: ${BEFORE_SHA:0:12}…)"
else
  bad "engine binary changed during the PACKAGE upgrade step — harness leaked engine supply, invalidating the scenario"
fi
RV_STILL_OLD="$(_release_version)"
[ "$RV_STILL_OLD" = "$PREV_EXPECT" ] && ok "service still reports the OLD engine $RV_STILL_OLD post package-upgrade (as expected — nothing has converged it yet)" \
  || note "service now reports $RV_STILL_OLD (informational — service process caches; not itself a failure)"

# ── SKEW-WINDOW ASSERT (bead nexus-cfgo9, Hal's scope addition, ruling
#    revised 2026-07-15): a T1 op issued with the NEW client against the
#    OLD (not-yet-converged) engine must reach a DETERMINISTIC BOUNDED
#    outcome in the upgrade window — the product's obligation here is
#    LEGIBILITY, not service. Two acceptable outcomes: (a) real success
#    (content round-trips) — the expected branch against $PREV_ENGINE_TAG
#    (v0.1.42), which is above the T1 reflection floor (v0.1.38,
#    nexus-opr9m) and genuinely capable; or (b) a LOUD error naming
#    convergence/engine (the product emits none such today — tracked as
#    nexus-by875, not fixed in this arc, so branch (b) is theoretical for
#    THIS engine pair). A timeout is ALWAYS a FAIL — a hang is never a
#    pass, regardless of which branch it would have hit.
#
#    Diagnosis (2026-07-15, reported + confirmed): a prior run of this
#    exact scenario (same conexus 6.9.0 start, same engine-service-v0.1.42,
#    same working-tree client) hung past a 20s bound on the get() call. A
#    follow-up diagnostic pass — harness-only instrumentation, no product
#    change — split `nx scratch get`'s two internal HTTP calls
#    (list_entries() then get(), see _resolve_entry_id in
#    nexus/commands/scratch.py) with individual timing and reproduced
#    NEITHER the hang NOR any slowness (both calls: 0.00s). Every httpx
#    client on this path sets an explicit 30s timeout (HttpScratchStore,
#    HttpTokenStore) and no unbounded blocking call was found in code —
#    the hang is an intermittent, engine-version-independent T1-CLI
#    latency characteristic (candidates: Postgres row-lock contention on
#    the per-invocation token-rotation UPSERT, container network jitter),
#    NOT a "talking to a stale engine" defect. Filed as nexus-by875 (T1
#    CLI path has no overall wall-clock budget or slow-path message) —
#    fast-follow, not this arc. The 60s bound below is calibrated well
#    above the observed common case (0.00s) while remaining well short of
#    "hung forever", per that diagnosis.
say "Skew window — new client + old engine (v0.1.42), before convergence"
SKEW_MARKER="skew-window-marker-$$-$(date +%s)"
SKEW_PUT="$(timeout 60 nx scratch put "$SKEW_MARKER" --tags rehearsal-cfgo9 2>&1)"
SKEW_PUT_RC=$?
printf '%s\n' "$SKEW_PUT" | sed 's/^/       /'
if [ "$SKEW_PUT_RC" = 124 ]; then
  bad "skew-window T1 put HUNG past the 60s bound against the old engine — a hang is never a pass"
elif [ "$SKEW_PUT_RC" != 0 ]; then
  # Non-timeout failure: acceptable ONLY if it is a LOUD, convergence/
  # engine-naming error (branch b) — never a bare/opaque failure.
  if printf '%s' "$SKEW_PUT" | grep -qiE "converg|engine"; then
    ok "skew-window T1 put failed LOUD, naming convergence/engine (rc=$SKEW_PUT_RC): $SKEW_PUT"
  else
    bad "skew-window T1 put failed (rc=$SKEW_PUT_RC) with no convergence/engine-naming message — opaque, not legible: $SKEW_PUT"
  fi
elif [ -z "$SKEW_PUT" ]; then
  bad "skew-window T1 put returned rc=0 but empty output — silent, not deterministic"
else
  SKEW_ID="$(printf '%s\n' "$SKEW_PUT" | grep -oE '[0-9a-fA-F-]{8,}' | tail -1)"
  if [ -z "$SKEW_ID" ]; then
    bad "skew-window T1 put produced no parseable entry id: $SKEW_PUT"
  else
    SKEW_GET="$(timeout 60 nx scratch get "$SKEW_ID" 2>&1)"
    SKEW_GET_RC=$?
    if [ "$SKEW_GET_RC" = 124 ]; then
      bad "skew-window T1 get HUNG past the 60s bound against the old engine — a hang is never a pass"
    elif [ "$SKEW_GET_RC" != 0 ]; then
      if printf '%s' "$SKEW_GET" | grep -qiE "converg|engine"; then
        ok "skew-window T1 get failed LOUD, naming convergence/engine (rc=$SKEW_GET_RC): $SKEW_GET"
      else
        bad "skew-window T1 get failed (rc=$SKEW_GET_RC) with no convergence/engine-naming message — opaque, not legible: $SKEW_GET"
      fi
    elif ! printf '%s' "$SKEW_GET" | grep -q "$SKEW_MARKER"; then
      bad "skew-window T1 get returned rc=0 but silent/wrong content (got: $SKEW_GET) — never a silent empty read"
    else
      ok "skew-window T1 put/get succeeded, bounded and non-silent, against the old engine (v0.1.42, above the T1 floor): $SKEW_MARKER"
    fi
  fi
fi

# ── Stage 5: THE CONVERGENCE PASS UNDER TEST ──────────────────────────────────
say "Stage 5 — nx daemon restart-stale (engine convergence + diag-view heal)"
unset NEXUS_SERVICE_TAG NX_SERVICE_TAG 2>/dev/null || true
_diag "pre-restart-stale"
RS_OUT="$(nx daemon restart-stale 2>&1)"
RS_RC=$?
printf '%s\n' "$RS_OUT" | sed 's/^/       /'
_diag "post-restart-stale"
[ "$RS_RC" = 0 ] && ok "nx daemon restart-stale exited 0" || bad "nx daemon restart-stale exited $RS_RC"
printf '%s' "$RS_OUT" | grep -q "converged engine" \
  && ok "convergence action fired (engine was stale, as expected)" \
  || bad "no 'converged engine' action line — convergence did not fire"
# `nx daemon restart-stale` runs FOUR INDEPENDENT legs (process-skew, engine
# convergence, diag-view heal, launchagent unload) and any of them may emit a
# NEEDS HUMAN. Grepping the whole output and blaming CONVERGENCE conflates
# them. It only ever passed here because the process-skew leg could not run at
# all on this procps-less box; once it could, its CORRECT report of the
# pre-upgrade supervisor tripped a convergence assert.
#
# The process-skew leg's lines have a distinct shape -- `NEEDS HUMAN: <kind>
# (pid N) ...`. Everything else belongs to a leg this assert is actually about.
# NOTE: plain BRE — parens are LITERAL here and must NOT be backslash-escaped
# (`\(` is a GROUP in BRE, so an escaped form silently matches nothing, which
# would make the non-vacuity assert below fail and the exclusion filter a
# no-op). Verified against real restart-stale output before use.
SKEW_SHAPE='NEEDS HUMAN: [a-z-]* (pid [0-9]*)'
OTHER_NEEDS_HUMAN="$(printf '%s\n' "$RS_OUT" | grep -i "NEEDS HUMAN" | grep -v "$SKEW_SHAPE" || true)"
if [ -n "$OTHER_NEEDS_HUMAN" ]; then
  bad "a non-process-skew NEEDS HUMAN line appeared — convergence/heal was blocked or failed: $OTHER_NEEDS_HUMAN"
else
  ok "no convergence/heal NEEDS HUMAN lines — those legs completed cleanly"
fi
# NON-VACUITY + a real property of this scenario: the supervisor that was
# started by $PREV_RELEASE is, after a package-only upgrade, genuinely running
# OLD code, so the process-skew leg MUST name it. This is the leg that silently
# did nothing on this box before the /proc fallback -- assert it ran.
printf '%s' "$RS_OUT" | grep -q "$SKEW_SHAPE" \
  && ok "process-skew leg RAN and named the pre-upgrade storage service (no ps on this box — /proc fallback)" \
  || bad "process-skew leg named no stale process — after a package-only upgrade the running supervisor IS stale, so this leg either did not run or failed to see it"

# ── Assert: the engine on disk is now the NEW tag, acquired for real ─────────
say "Assert — installed engine converged to $NEW_ENGINE_TAG"
if _wait_healthy 30; then
  ok "service healthy after convergence"
else
  nx daemon service status 2>&1 | sed 's/^/       /' || true
  bad "service did not become healthy after convergence"
fi
RV2="$(_release_version)"
if [ "$RV2" = "$NEW_EXPECT" ]; then
  ok "/version release_version=$RV2 — converged to the working tree's required engine"
else
  bad "/version release_version=$RV2, expected $NEW_EXPECT — convergence did not actually install the new engine"
fi
AFTER_CONVERGE_SHA="$(sha256sum "$HOME/.config/nexus/service/nexus-service" 2>/dev/null | awk '{print $1}')"
if [ -n "$AFTER_CONVERGE_SHA" ] && [ "$AFTER_CONVERGE_SHA" != "$AFTER_SHA" ]; then
  ok "engine binary sha256 changed by convergence (real re-acquire, not a no-op): ${AFTER_CONVERGE_SHA:0:12}…"
else
  bad "engine binary sha256 did not change — convergence did not really re-download the binary"
fi

# ── Assert: the chash probe runs the VIEW-era statements, no legacy fallback ─
# The "1 plsql line" validation: run chash_conformance_statements() (the
# Amendment A6 view-era one-line-per-table probe) directly via the SAME
# nexus_diag choke point the product uses, and require it to succeed WITHOUT
# falling back to legacy_chash_conformance_statements(). Re-runs nx init
# --service once more first (idempotent) so the diag view -- which needs the
# chash tables to exist, and those are only created by the engine's OWN first
# boot -- gets provisioned (nexus.db.pg_provision._provision_diag_conformance_view's
# documented "next re-provision... completes the swap").
say "Assert — chash-poison probe runs the VIEW path end-to-end (no legacy fallback)"
nx init --service --embedder bge-768 --yes >/tmp/reinit.log 2>&1 || true
python3 <<'PY'
import sys
from nexus.db.chash_tables import DIAG_CONFORMANCE_VIEW, chash_conformance_statements
from nexus.db.diag_connection import resolve_diag_credentials, run_diagnostic_sql

creds = resolve_diag_credentials()
if creds is None:
    print(f"       no nexus_diag credentials resolvable")
    sys.exit(1)
try:
    counts = run_diagnostic_sql(chash_conformance_statements(), creds)
except Exception as exc:
    print(f"       view-era probe raised (would have fallen back to legacy): {exc}")
    sys.exit(1)
print(f"       {DIAG_CONFORMANCE_VIEW} counts: {counts}")
nonconforming = sum(int(c) for c in counts)
if nonconforming != 0:
    print(f"       {nonconforming} non-conformant row(s) unexpected on a freshly-seeded box")
    sys.exit(1)
sys.exit(0)
PY
if [ $? -eq 0 ]; then
  ok "chash-poison probe answered via the VIEW path (nexus.diag_chash_conformance), zero non-conformant rows, no legacy fallback"
else
  bad "chash-poison probe did not cleanly answer via the view path"
fi

# ── Assert: T1 round-trips post-convergence, and the PRE-upgrade row survived
say "Assert — T1 scratch round-trip post-convergence"
_diag "pre-T1-roundtrip"
POST_MARKER="post-convergence-marker-$$-$(date +%s)"
POST_PUT="$(nx scratch put "$POST_MARKER" --tags rehearsal-cfgo9 2>&1)"
printf '%s\n' "$POST_PUT" | sed 's/^/       /'
POST_ID="$(printf '%s\n' "$POST_PUT" | grep -oE '[0-9a-fA-F-]{8,}' | tail -1)"
if [ -n "$POST_ID" ] && nx scratch get "$POST_ID" 2>/dev/null | grep -q "$POST_MARKER"; then
  ok "post-convergence T1 put/get round-trips"
else
  bad "post-convergence T1 round-trip failed"
fi

PRE_CONTENT="$(nx scratch get "$PRE_ID" 2>&1)"
if printf '%s' "$PRE_CONTENT" | grep -q "$MARKER"; then
  ok "pre-upgrade T1 row $PRE_ID survived the service cycle: $MARKER"
else
  bad "pre-upgrade T1 row $PRE_ID did NOT survive (got: $PRE_CONTENT)"
fi

say "RESULT"
if [ "$FAILS" -eq 0 ]; then
  printf '\033[32mPACKAGE-UPGRADE CONVERGENCE MVV PASSED\033[0m — %s + %s -> package-only upgrade to %s -> converged to %s for real, service healthy, chash probe clean via the view, T1 survives the cycle\n' \
    "conexus $PREV_RELEASE" "$PREV_ENGINE_TAG" "$UPGRADE_TARGET_LABEL" "$NEW_ENGINE_TAG"
  exit 0
else
  printf '\033[31mPACKAGE-UPGRADE CONVERGENCE MVV FAILED — %d check(s) failed -> %s\033[0m\n' "$FAILS" "$UPGRADE_TARGET_LABEL"
  exit 1
fi
