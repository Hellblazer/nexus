#!/usr/bin/env bash
# nexus-h8rf6 (candidate-shakeout leg) — full CLI-verb + workload shakeout of
# the CANDIDATE engine (the locally-built -Ob native binary of the current
# service/ tree), run INSIDE the container. NO publish, NO deploy, NO cloud.
#
# Born from the 2026-07-03 shakeout retro: every finding except the edge-proxy
# class was discoverable locally against a served candidate — the port-gap
# crashes (delete-by-title, staleness shape, reranker seam) needed only
# CLI-verbs-against-a-served-engine, and the catalog_collections lock convoy
# (nexus-h8rf6.2) is PG-level. This leg tests the JOURNEY, not the artifact:
#
#   Phase A  provision + serve the candidate  (nx init --service, bge-768)
#   Phase B  CLI verb matrix                  (store/search/memory/scratch/
#                                              catalog/collection/taxonomy,
#                                              incl. the umvh2 delete-by-title
#                                              regression)
#   Phase C  index + staleness                (index a synthetic repo; assert
#                                              zero staleness-cache failures;
#                                              re-index must be incremental,
#                                              not a full re-embed)
#   Phase D  concurrent write load            (parallel index runs + store
#                                              puts; assert ZERO client-rc
#                                              failures across the concurrent
#                                              workload — the lock-convoy
#                                              tier. No 5xx log scan: the
#                                              candidate has no request
#                                              logger to scan, nexus-xm0cp)
#   Phase E  collections-drift                (nexus-syfes class: the server-
#                                              side GC prune — catalog-023/024
#                                              — must never leave a permanent
#                                              catalog_collections row for a
#                                              quarantine/origin sibling on a
#                                              zero-orphan/zero-restore pass.
#                                              Phase C's run 1 already drives
#                                              a zero-orphan GC pass on a
#                                              freshly-created collection —
#                                              see the RED/GREEN falsification
#                                              transcripts on nexus-syfes for
#                                              proof this is not vacuous.
#                                              This is the ONLY pre-tag gate
#                                              that can catch this class —
#                                              v0.1.70 shipped it and was
#                                              caught only by the client
#                                              shakedown, a full day later.)
#   Phase F  native-smoke.sh probe set        (nexus-l8xnz: the SAME script
#                                              engine-service-release.yml
#                                              runs, byte-for-byte, against
#                                              this candidate over the
#                                              already-provisioned Postgres —
#                                              closes the release-only-
#                                              procedures-rot gap that let a
#                                              stale probe fixture (16-char
#                                              doc_id vs the RDR-194 P3c
#                                              64-hex width validation) burn
#                                              the v0.1.77 tag with a
#                                              perfectly good binary.)
#
# Exit 0 only when every phase passes: "CANDIDATE SHAKEOUT PASSED".
#
# gap-15 (T2 [22511]): `-e` added (was `set -uo pipefail`, no -e). This is
# NOT a blanket revert — the ok/bad collect-and-continue design throughout
# this script is intentional and must survive. Every bare command
# substitution / pipeline this addition would otherwise turn fatal (and
# was NOT already protected by an if/&&/|| construct that -e exempts) got
# an explicit per-command disposition alongside this change: either
# `|| true` (content is checked downstream, not rc) or an explicit
# `RC=0; OUT="$(cmd)" || RC=$?` capture (rc IS the assertion). See the
# gap-8/15 fix commit for the specific sites; genuinely fatal prelude
# commands (binary positioning, etc.) were left bare on purpose — those
# SHOULD abort loud.
set -euo pipefail

FAILS=0
say()  { printf '\n\033[1m== %s ==\033[0m\n' "$*"; }
ok()   { printf '  \033[32mPASS\033[0m %s\n' "$*"; }
bad()  { printf '  \033[31mFAIL\033[0m %s\n' "$*"; FAILS=$((FAILS+1)); }
note() { printf '       %s\n' "$*"; }
# run_check <label> <expected-regex> <cmd...>: capture combined output, PASS when
# it matches, FAIL **and print the captured output** otherwise (maiden-run
# lesson: assertions that swallow output are undiagnosable).
run_check() {
  local label="$1" want="$2"; shift 2
  local out
  # gap-15: content-checked below, not rc-gated -- a nonzero "$@" must not
  # abort under -e (added same change) before the grep check runs.
  out="$("$@" 2>&1)" || true
  if printf '%s' "$out" | grep -qiE "$want"; then
    ok "$label"
  else
    bad "$label"
    printf '%s\n' "$out" | sed 's/^/       | /' | tail -12
  fi
}

SVC_NATIVE_DIR="/opt/nexus-service-native"
SVC_WELL_KNOWN_DIR="$HOME/.config/nexus/service"

# ── Phase A: position the candidate binary + provision + serve ───────────────
say "Phase A — provision + serve the CANDIDATE binary"
nx --version >/dev/null 2>&1 && ok "nx installed ($(nx --version 2>&1))" || bad "nx --version failed"
command -v initdb >/dev/null 2>&1 && bad "system PostgreSQL present — bare-machine posture violated (nexus-5qefg)" || ok "no system PostgreSQL (bundle must provide it)"
test -x "$SVC_NATIVE_DIR/nexus-service" && ok "candidate native binary present" || { bad "candidate binary missing"; exit 1; }

mkdir -p "$SVC_WELL_KNOWN_DIR"
cp "$SVC_NATIVE_DIR"/* "$SVC_WELL_KNOWN_DIR/" && chmod +x "$SVC_WELL_KNOWN_DIR/nexus-service" \
  && ok "candidate positioned at well-known location" || { bad "positioning failed"; exit 1; }

export NX_SERVICE_MAX_HEAP="${NX_SERVICE_MAX_HEAP:-1g}"
git config --global user.email "shakeout@nexus.local" >/dev/null 2>&1 || true
git config --global user.name  "nexus shakeout"       >/dev/null 2>&1 || true

# init is idempotent (RDR-174); the -Ob quick-build binary's FIRST boot
# (native init + 144 Liquibase changesets on fresh PG) can exceed the
# supervisor's 60s readiness budget under Docker-on-Mac variance, so allow
# one bounded retry before declaring the candidate broken.
init_ok=0
for attempt in 1 2; do
  note "nx init --service --embedder bge-768 --no-autostart (attempt $attempt) …"
  if nx init --service --embedder bge-768 --no-autostart 2>&1 | tail -3 | sed 's/^/       /'; then
    init_ok=1; break
  fi
  note "attempt $attempt failed; supervisor log tail:"
  tail -6 "$HOME/.config/nexus/logs/storage_service.log" 2>/dev/null | sed 's/^/       | /' || true
  sleep 5
done
if [ "$init_ok" = 1 ]; then
  ok "init --service (provision + serve)"
else
  bad "nx init --service failed after 2 attempts"; say "ABORT"; exit 1
fi
export NX_STORAGE_BACKEND=service
# shellcheck disable=SC1091
set -a; . "$HOME/.config/nexus/pg_credentials"; set +a

healthy=0
for _ in $(seq 1 30); do
  # gap-15: the poll's expected FALSE outcome (not yet healthy) is a bare
  # && with no trailing || -- under -e this died on iteration 1 instead of
  # polling up to 30 times. `|| true` restores the intended poll-and-wait.
  nx daemon service status 2>&1 | grep -qiE "health.*ok|status.*live" && { healthy=1; break; } || true
  sleep 2
done
[ "$healthy" = 1 ] && ok "candidate serving (healthy)" || { bad "service never healthy"; exit 1; }

# ── Phase B: CLI verb matrix ─────────────────────────────────────────────────
say "Phase B — CLI verb matrix (every verb against the served candidate)"

# T2 memory roundtrip
nx memory put -p shakeout -t probe-1 "verb matrix probe" --ttl 1d >/dev/null 2>&1 \
  && [ "$(nx memory get -p shakeout -t probe-1 2>/dev/null)" = "verb matrix probe" ] \
  && ok "memory put/get" || bad "memory put/get"
# gap-15: `yes |` into an early-closing reader is the sanctioned
# early-exit-consumer SIGPIPE hazard (nexus-6zxfb/i66g4 class) -- under
# pipefail (already set) the pipeline's rc can be yes's own SIGPIPE exit,
# not nx's. The very next line is the real assertion (row survives/gone);
# this line's rc was never meant to gate anything.
yes | nx memory delete -p shakeout -t probe-1 >/dev/null 2>&1 || true
nx memory get -p shakeout -t probe-1 >/dev/null 2>&1 && bad "memory delete (row survived)" || ok "memory delete"

# T1 scratch — the SANCTIONED bare-CLI path: service-backed T1 is PG-only
# (nexus-4lkmz — the in-process NX_T1_ISOLATED=1 escape hatch retired
# outright, it now hard-fails). A bare CLI mints its own persisted
# CLI-dedicated session token (nexus-rn3wo.1) — no opt-in flag needed;
# the minted-token path is exercised by the --fullstack leg (real MCP).
# Also assert a STALE/bogus token fails CLEANLY (actionable
# ClickException, not a traceback).
run_check "scratch put"    "Stored"   nx scratch put "shakeout scratch probe" --tags shakeout
# gap-15: this call is EXPECTED to fail (bogus/unminted token) -- content
# checked below, not rc-gated.
UNMINTED_OUT="$(NX_T1_SESSION=unminted-probe nx scratch list 2>&1)" || true
if printf '%s' "$UNMINTED_OUT" | grep -q "Traceback"; then
  bad "unminted scratch must fail cleanly (got a traceback)"
  printf '%s\n' "$UNMINTED_OUT" | sed 's/^/       | /' | tail -6
elif printf '%s' "$UNMINTED_OUT" | grep -q "nx daemon service start"; then
  ok "unminted scratch fails cleanly with actionable guidance"
else
  bad "unminted scratch error lacks the sanctioned-path guidance"
  printf '%s\n' "$UNMINTED_OUT" | sed 's/^/       | /' | tail -6
fi

# T3 store: put -> search -> delete-by-TITLE (umvh2 regression) -> verify gone
PROBE_MD=/tmp/shakeout-probe.md
printf '# Shakeout probe\n\nThe amaranthine zeppelin quotient verifies retrieval.\n' > "$PROBE_MD"
if nx store put "$PROBE_MD" --collection knowledge__shakeout --title "shakeout-probe" --tags shakeout 2>&1 | grep -q "Stored:"; then
  ok "store put"
else
  bad "store put"
fi
sleep 2
nx search "amaranthine zeppelin quotient" --corpus knowledge -m 2 2>/dev/null | grep -q "shakeout-probe" \
  && ok "search finds stored note" || bad "search finds stored note"
# gap-15: same yes-pipe SIGPIPE hazard as the memory-delete call above,
# plus content (not rc) is what the check below actually gates on.
DEL_OUT="$(yes | nx store delete --title "shakeout-probe" --collection knowledge__shakeout 2>&1)" || true
if printf '%s' "$DEL_OUT" | grep -qiE "delet"; then
  ok "store delete --title (umvh2 regression)"
else
  bad "store delete --title (umvh2 regression)"
  printf '%s\n' "$DEL_OUT" | sed 's/^/       | /' | tail -12
fi
nx search "amaranthine zeppelin quotient" --corpus knowledge -m 1 2>/dev/null | grep -q "shakeout-probe" \
  && bad "deleted note still searchable" || ok "deleted note gone from search"

# Plan library — real-client round-trip against the served candidate
# (nexus-o02xe recurrence guard: the CLI verbs used to hardcode the local
# SQLite snapshot, leaving `nx plan` dark in service mode. reseed writes
# builtins through HttpPlanLibrary; list must read them back from the
# SAME live library, not echo "T2 database not found" / an empty snapshot.)
PLAN_SEED_OUT="$(nx plan reseed 2>&1)" || true  # gap-15: content-checked below, not rc-gated
printf '%s' "$PLAN_SEED_OUT" | grep -qE "Seeded [0-9]+ new builtin row" \
  && ok "plan reseed writes through the service" \
  || { bad "plan reseed"; printf '%s\n' "$PLAN_SEED_OUT" | sed 's/^/       | /' | tail -6; }
# nexus-xm0cp: the "T2 database not found" fallback this used to guard
# against was the local SQLite PlanLibrary snapshot; that code path was
# deleted outright at nexus-i711w Stage 2 sub-stage A3 (_open_plan_library
# now only ever constructs HttpPlanLibrary — a resolution failure raises
# "plans service unavailable: ..." instead, untestable here since Phase A
# already gates on a healthy service). No live producer for that literal
# remains in src/; collapsed to the one real assertion below.
PLAN_LIST_OUT="$(nx plan list 2>&1)" || true  # gap-15: content-checked below, not rc-gated
if printf '%s' "$PLAN_LIST_OUT" | grep -qiE "builtin"; then
  ok "plan list reads seeded builtins back from the service"
else
  bad "plan list returned no builtin rows after reseed (o02xe regression)"
  printf '%s\n' "$PLAN_LIST_OUT" | sed 's/^/       | /' | tail -6
fi

# Catalog + collections + taxonomy + doctor surfaces
nx catalog stats 2>/dev/null | grep -qE "Documents:" && ok "catalog stats" || bad "catalog stats"
nx collection list >/dev/null 2>&1 && ok "collection list" || bad "collection list"
nx taxonomy status >/dev/null 2>&1 && ok "taxonomy status" || bad "taxonomy status"
DOCTOR_OUT="$(nx doctor 2>&1)" || true  # gap-15: content (traceback presence) checked below, not rc-gated
printf '%s\n' "$DOCTOR_OUT" | grep -q "Traceback" && bad "doctor raised a traceback" || ok "doctor runs traceback-free"

# ── Phase C: index + staleness (incremental must work) ──────────────────────
say "Phase C — index a synthetic repo; staleness must make re-index incremental"
REPO=/tmp/shakeout-repo
rm -rf "$REPO"; mkdir -p "$REPO/src" "$REPO/docs"
for i in $(seq 1 20); do
  printf 'def fn_%d(x):\n    """Shakeout function %d."""\n    return x * %d\n' "$i" "$i" "$i" > "$REPO/src/mod_$i.py"
  printf '# Doc %d\n\nShakeout markdown document number %d about the flux capacitor array.\n' "$i" "$i" > "$REPO/docs/doc_$i.md"
done
( cd "$REPO" && git init -q && git add -A && git -c user.email=s@x -c user.name=s commit -qm seed )

IDX1=/tmp/shakeout-index-1.log
if nx index repo "$REPO" > "$IDX1" 2>&1; then ok "index run 1 (exit 0)"; else bad "index run 1 failed"; fi
# nexus-xm0cp: the chash dual-write hook this used to guard (`grep -q
# "dual_write_failed"`) was retired at RDR-187 / nexus-piwya.4 -- the
# chunks tables are the chash store now, nothing left to dual-write, and
# the string has zero producers anywhere in src/ or service/ (confirmed
# by exhaustive inverse-grep). Deleted rather than retargeted at a signal
# nothing emits.
grep -q "docs_for_chashes_failed" "$IDX1" && bad "staleness-cache failure in run 1"   || ok "staleness cache built (no shape crash)"

# Touch 2 files; the re-index must be incremental (h8rf6.3 regression).
printf '\n# touched\n' >> "$REPO/src/mod_1.py"
printf '\nTouched line.\n' >> "$REPO/docs/doc_1.md"
( cd "$REPO" && git add -A && git -c user.email=s@x -c user.name=s commit -qm touch )
IDX2=/tmp/shakeout-index-2.log
if nx index repo "$REPO" > "$IDX2" 2>&1; then ok "index run 2 (exit 0)"; else bad "index run 2 failed"; fi
grep -q "docs_for_chashes_failed" "$IDX2" && bad "staleness-cache failure in run 2" || ok "staleness cache ok in run 2"
# Incremental assertion: run 2 must process far fewer files than run 1.
# Skipped files still emit "[n/40] ... skipped" lines — incremental means
# few NON-skipped (actually re-processed) files, not fewer lines (maiden-run
# lesson: run 4 proved the skip works while the line-count assertion lied).
files1=$(grep -E '^\s+\[[0-9]+/[0-9]+\]' "$IDX1" | grep -cv "skipped" || true)
files2=$(grep -E '^\s+\[[0-9]+/[0-9]+\]' "$IDX2" | grep -cv "skipped" || true)
note "run1 re-processed $files1 files; run2 re-processed $files2 (skipped lines excluded)"
if [ "${files2:-0}" -le 6 ] && [ "${files1:-0}" -ge 10 ]; then
  ok "re-index is incremental ($files2 << $files1)"
else
  bad "re-index NOT incremental (run1=$files1 run2=$files2) — h8rf6.3 regression"
  note "run-2 log tail for diagnosis:"
  sed 's/^/       | /' "$IDX2" | tail -20
  note "run-2 staleness/skip lines:"
  grep -iE "stale|skip|unchanged|cache" "$IDX2" | sed 's/^/       | /' | head -10 || true
fi
nx search "flux capacitor array" --corpus docs -m 2 2>/dev/null | grep -qi "doc" \
  && ok "indexed content searchable" || bad "indexed content not searchable"

# ── Phase D: concurrent write load (the lock-convoy tier) ───────────────────
say "Phase D — concurrent writes: parallel index + store puts; zero failures allowed"
REPO2=/tmp/shakeout-repo-2
rm -rf "$REPO2"; mkdir -p "$REPO2/src"
for i in $(seq 1 15); do
  printf 'def par_%d(y):\n    """Parallel shakeout %d."""\n    return y + %d\n' "$i" "$i" "$i" > "$REPO2/src/par_$i.py"
done
( cd "$REPO2" && git init -q && git add -A && git -c user.email=s@x -c user.name=s commit -qm seed )

IDXA=/tmp/shakeout-load-a.log; IDXB=/tmp/shakeout-load-b.log
( nx index repo "$REPO" --force > "$IDXA" 2>&1 ) &
PA=$!
( nx index repo "$REPO2" > "$IDXB" 2>&1 ) &
PB=$!
# nexus-xm0cp: there is no service-side 5xx signal to scan for -- the
# candidate is com.sun.net.httpserver with no request logger, and
# service/src/main/resources/logback.xml has no status field in its one
# appender pattern. The only real assertion this phase can produce is a
# CLIENT-SIDE census over the concurrent workload it already spawns, via
# lib/store_put_census.sh (shared with tests/test_shakeout_store_put_census.py's
# durable falsifiability demonstration -- both source the same function,
# never a look-alike). `nx store put` raises on a non-2xx, non-retryable
# service response (HttpVectorClient's urllib.error.HTTPError handling,
# re-raised through src/nexus/commands/store.py's `except Exception: ...
# raise`, uncaught by Click), which surfaces as a non-zero CLI exit -- so a
# failed rc here IS observable evidence of a lock-convoy failure, not a
# proxy for nothing. Previously these 10 calls sent stdout+stderr to
# /dev/null and never read rc; zero assertions came out of this half of
# the load phase.
# shellcheck source=./lib/store_put_census.sh disable=SC1091
source "$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)/lib/store_put_census.sh"
census_concurrent_store_puts 10 "/tmp/load-put"
# gap-15: `wait PID` returns the waited process's own exit status -- a
# bare `wait "$PA"; RA=$?` dies at `wait` under -e before RA=$? ever runs
# if the backgrounded index job legitimately failed (exactly the class
# this phase exists to observe, not abort past). Capture via the `||`
# branch instead.
RA=0; wait "$PA" || RA=$?
RB=0; wait "$PB" || RB=$?
[ "$RA" = 0 ] && ok "parallel index A (exit 0)" || bad "parallel index A failed"
[ "$RB" = 0 ] && ok "parallel index B (exit 0)" || bad "parallel index B failed"
if [ "$STORE_FAILS" = 0 ]; then
  ok "10/10 concurrent store puts succeeded (exit 0)"
else
  bad "$STORE_FAILS/10 concurrent store puts failed under load (lock-convoy class):$STORE_FAIL_IDX"
  for i in $STORE_FAIL_IDX; do
    note "load-$i failure tail:"
    sed 's/^/       | /' "/tmp/load-put-$i.log" | tail -6
  done
fi
# nexus-xm0cp Finding 2: HttpVectorClient absorbs 502/503/504 via a bounded
# retry (_GATEWAY_RETRY_CODES / _GATEWAY_RETRY_SLEEPS,
# src/nexus/db/http_vector_client.py) BEFORE ever raising, so a transient
# gateway blip that resolves within ~17s never reaches a client rc -- the
# STORE_FAILS census above is structurally blind to exactly the shape a
# lock convoy is most likely to take. JUDGEMENT CALL (per review, recorded
# here rather than left implicit): a captured vector_gateway_retry in this
# phase is treated as a FAIL, not a report-only observation. The retry
# mechanism was designed for slow CCE embedding on LARGE batches
# (nexus-duoak.4) -- but these 10 docs are trivial single-chunk ~20-byte
# files against knowledge__shakeout, so an embedding-timeout explanation
# does not plausibly apply here; a retry on THIS workload is far more
# likely to be connection/thread-pool contention under concurrent load,
# i.e. the exact convoy signal this phase exists to catch. A release gate
# over-detecting a rare, fully-diagnosable FAIL (the log tail prints below,
# same as the failure branch above) is preferable to silently shipping a
# convoy defect the way the ORIGINAL /dev/null-swallowed loop did.
if [ "$STORE_RETRIES" = 0 ]; then
  ok "zero gateway-retry (502/503/504) absorptions across the concurrent workload"
else
  bad "$STORE_RETRIES/10 concurrent store puts absorbed a gateway retry under load (lock-convoy class, see vector_gateway_retry):$STORE_RETRY_IDX"
  for i in $STORE_RETRY_IDX; do
    note "load-$i gateway-retry tail:"
    grep "vector_gateway_retry" "/tmp/load-put-$i.log" | sed 's/^/       | /'
  done
fi

# ── Phase E: collections-drift (nexus-syfes class regression guard) ─────────
say "Phase E — collections-drift: catalog_collections projection vs T3 (nexus-syfes class)"
# nexus-syfes (engine-service-v0.1.70, 2026-08-11): gc_quarantine_orphans /
# gc_restore_rereferenced registered their destination collection in
# catalog_collections UNCONDITIONALLY, before the anti-join ran, so a GC
# pass that moved zero rows still left a permanently unreferenced quarantine
# projection row behind. Phase C above already drives exactly this
# condition — every full ``nx index repo`` walk invokes
# ``indexer._prune_deleted_files`` for each populated collection, which
# calls the server-side restore+quarantine route (``_prune_collection_
# serverside``) UNCONDITIONALLY, with no "only if there's something to
# move" guard. Phase C's run 1 is a *zero*-orphan pass by construction (a
# just-created repo has nothing superseded or deleted yet), so it alone
# reproduces the pre-catalog-024 trigger without any extra workload here.
# See the RED/GREEN falsification transcripts attached to nexus-syfes: this
# assertion goes RED with catalog-024 neutralised and GREEN with it restored.
#
# Only pre-tag place this can be caught: the client release-sandbox
# shakedown (tests/e2e/release-sandbox.sh step 11/11) runs the identical
# ``nx catalog doctor --collections-drift`` check, but that only runs
# AFTER an engine tag is already cut and published — the 2026-08-11
# incident this guards against cost a full re-cut because of exactly that
# gap. This phase closes it pre-tag.
#
# backfill-collections first, same as the client gate: it can only ADD a
# missing projection row for a collection T3 legitimately already has, so
# it cannot mask the nexus-syfes class (an orphan projection row with NO
# backing T3 collection) — it only prevents an unrelated false FAIL from a
# collection that legitimately hasn't been backfilled yet.
nx catalog backfill-collections --no-dry-run >/dev/null 2>&1 || true
run_check "collections-drift (no orphaned quarantine/projection rows, nexus-syfes class)" \
  "collections-drift: PASS" nx catalog doctor --collections-drift

# ── Phase F: native-smoke.sh probe set (nexus-l8xnz) ─────────────────────────
say "Phase F — native-smoke.sh probe set against the CANDIDATE (pre-tag, not release-workflow-only)"
# nexus-l8xnz: native-smoke.sh's raw-curl probe set (taxonomy/assignments/
# details' 64-hex doc_id width validation, T1's separate-jOOQ-schema
# reflection check, memory/plans/taxonomy/chash read/write routes, the
# bge-768 embed path, the fused-rerank stage) previously ran ONLY inside
# engine-service-release.yml -- a stale probe fixture (the pre-fix 16-char
# doc_id literal) burned the v0.1.77 tag on both linux release legs while
# the binary itself was fine, and this journey (like every other local
# gate) stayed green because nothing local exercised the script at all.
#
# This phase runs the IDENTICAL script byte-for-byte -- only the CALLER is
# adapted, never the script -- against the SAME candidate binary Phases A-E
# already proved healthy, pointed at the SAME already-provisioned Postgres
# (NX_DB_URL / NX_DB_USER / NX_DB_PASS, sourced from pg_credentials at the
# end of Phase A) instead of native-smoke.sh's own throwaway-docker-pgvector
# default: this image has no Docker socket (Dockerfile header: "NOT DinD").
# Pointing native-smoke.sh at an external NX_DB_URL is a FIRST-CLASS,
# already-documented mode of the script (its own header: "NX_DB_URL / ... --
# point at an existing Postgres. When unset, a throwaway ... container is
# started."), not a fork of it -- it self-selects OWN_PG=0 and self-skips
# its own docker-only voyage-mode-boot phase (that phase needs a resettable
# database; accepted gap, the voyage/egress-proxy boot path has no other
# local coverage today). The bge-768 embed section runs FOR REAL (the same
# model this image bakes in for Phases A/C); the cross-encoder rerank
# section takes its documented LOUD-degrade path (that model is not baked
# into this image); the T1 / memory-plans-taxonomy-chash real-Python-client
# blocks self-skip via native-smoke.sh's own documented WARN (no service/
# tree or pyproject.toml in this image) -- that exact coverage (routing +
# backend together, against this SAME candidate through this SAME real
# client code) is already proven by Phase B's CLI verb matrix above.
NATIVE_SMOKE_LOG=/tmp/native-smoke.log
NATIVE_SMOKE_RC=0
# gap-15 shape: rc captured explicitly via `||`, never left bare under -e --
# a native-smoke.sh failure must increment FAILS and let this journey reach
# the Verdict section, not abort the whole rehearsal mid-phase.
BIN="$SVC_NATIVE_DIR/nexus-service" bash "$HOME/native-smoke.sh" > "$NATIVE_SMOKE_LOG" 2>&1 \
  || NATIVE_SMOKE_RC=$?
if [ "$NATIVE_SMOKE_RC" = 0 ] && grep -q "^NATIVE SMOKE PASS$" "$NATIVE_SMOKE_LOG"; then
  ok "native-smoke.sh probe set (exit 0, NATIVE SMOKE PASS)"
else
  bad "native-smoke.sh probe set (exit $NATIVE_SMOKE_RC) -- full log follows for diagnosis:"
  sed 's/^/       | /' "$NATIVE_SMOKE_LOG"
fi

# ── Verdict ──────────────────────────────────────────────────────────────────
say "Verdict"
if [ "$FAILS" = 0 ]; then
  printf '\033[32mCANDIDATE SHAKEOUT PASSED\033[0m — verb matrix + incremental index + concurrent load, all against the locally-built candidate; nothing published, nothing deployed\n'
  exit 0
else
  printf '\033[31mCANDIDATE SHAKEOUT FAILED\033[0m — %d failure(s); see PASS/FAIL lines above\n' "$FAILS"
  exit 1
fi
