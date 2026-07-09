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
#                                              zero dual-write/staleness
#                                              failures; re-index must be
#                                              incremental, not a full re-embed)
#   Phase D  concurrent write load            (parallel index runs + store
#                                              puts; assert ZERO 5xx in the
#                                              service log — the lock-convoy
#                                              tier)
#
# Exit 0 only when every phase passes: "CANDIDATE SHAKEOUT PASSED".
set -uo pipefail

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
  out="$("$@" 2>&1)"
  if printf '%s' "$out" | grep -qiE "$want"; then
    ok "$label"
  else
    bad "$label"
    printf '%s\n' "$out" | sed 's/^/       | /' | tail -12
  fi
}

SVC_NATIVE_DIR="/opt/nexus-service-native"
SVC_WELL_KNOWN_DIR="$HOME/.config/nexus/service"
SERVICE_LOG="$HOME/.config/nexus/logs/storage_service_native.log"

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
  nx daemon service status 2>&1 | grep -qiE "health.*ok|status.*live" && { healthy=1; break; }
  sleep 2
done
[ "$healthy" = 1 ] && ok "candidate serving (healthy)" || { bad "service never healthy"; exit 1; }

# ── Phase B: CLI verb matrix ─────────────────────────────────────────────────
say "Phase B — CLI verb matrix (every verb against the served candidate)"

# T2 memory roundtrip
nx memory put -p shakeout -t probe-1 "verb matrix probe" --ttl 1d >/dev/null 2>&1 \
  && [ "$(nx memory get -p shakeout -t probe-1 2>/dev/null)" = "verb matrix probe" ] \
  && ok "memory put/get" || bad "memory put/get"
yes | nx memory delete -p shakeout -t probe-1 >/dev/null 2>&1
nx memory get -p shakeout -t probe-1 >/dev/null 2>&1 && bad "memory delete (row survived)" || ok "memory delete"

# T1 scratch — the SANCTIONED bare-CLI path: service-backed T1 requires a
# MINTED session token (only the nx-mcp lifespan mints; re-minting rotates,
# so the CLI must not self-mint — nexus-h8rf6 T1-401 finding). Ephemeral
# isolated mode is the sanctioned standalone posture; the minted-token path
# is exercised by the --fullstack leg (real MCP). Also assert the unminted
# path fails CLEANLY (actionable ClickException, not a traceback).
run_check "scratch put (isolated)"    "Stored"   env NX_T1_ISOLATED=1 nx scratch put "shakeout scratch probe" --tags shakeout
UNMINTED_OUT="$(NX_T1_SESSION=unminted-probe nx scratch list 2>&1)"
if printf '%s' "$UNMINTED_OUT" | grep -q "Traceback"; then
  bad "unminted scratch must fail cleanly (got a traceback)"
  printf '%s\n' "$UNMINTED_OUT" | sed 's/^/       | /' | tail -6
elif printf '%s' "$UNMINTED_OUT" | grep -q "NX_T1_ISOLATED=1"; then
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
DEL_OUT="$(yes | nx store delete --title "shakeout-probe" --collection knowledge__shakeout 2>&1)"
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
PLAN_SEED_OUT="$(nx plan reseed 2>&1)"
printf '%s' "$PLAN_SEED_OUT" | grep -qE "Seeded [0-9]+ new builtin row" \
  && ok "plan reseed writes through the service" \
  || { bad "plan reseed"; printf '%s\n' "$PLAN_SEED_OUT" | sed 's/^/       | /' | tail -6; }
PLAN_LIST_OUT="$(nx plan list 2>&1)"
if printf '%s' "$PLAN_LIST_OUT" | grep -q "T2 database not found"; then
  bad "plan list fell back to the local snapshot (o02xe regression)"
elif printf '%s' "$PLAN_LIST_OUT" | grep -qiE "builtin"; then
  ok "plan list reads seeded builtins back from the service"
else
  bad "plan list returned no builtin rows after reseed"
  printf '%s\n' "$PLAN_LIST_OUT" | sed 's/^/       | /' | tail -6
fi

# Catalog + collections + taxonomy + doctor surfaces
nx catalog stats 2>/dev/null | grep -qE "Documents:" && ok "catalog stats" || bad "catalog stats"
nx collection list >/dev/null 2>&1 && ok "collection list" || bad "collection list"
nx taxonomy status >/dev/null 2>&1 && ok "taxonomy status" || bad "taxonomy status"
DOCTOR_OUT="$(nx doctor 2>&1)"
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
grep -q "dual_write_failed"      "$IDX1" && bad "chash dual-write failures in run 1" || ok "zero chash dual-write failures"
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
say "Phase D — concurrent writes: parallel index + store puts; zero 5xx allowed"
REPO2=/tmp/shakeout-repo-2
rm -rf "$REPO2"; mkdir -p "$REPO2/src"
for i in $(seq 1 15); do
  printf 'def par_%d(y):\n    """Parallel shakeout %d."""\n    return y + %d\n' "$i" "$i" "$i" > "$REPO2/src/par_$i.py"
done
( cd "$REPO2" && git init -q && git add -A && git -c user.email=s@x -c user.name=s commit -qm seed )

LOG_MARK=$(wc -l < "$SERVICE_LOG" 2>/dev/null || echo 0)
IDXA=/tmp/shakeout-load-a.log; IDXB=/tmp/shakeout-load-b.log
( nx index repo "$REPO" --force > "$IDXA" 2>&1 ) &
PA=$!
( nx index repo "$REPO2" > "$IDXB" 2>&1 ) &
PB=$!
for i in $(seq 1 10); do
  printf '# load doc %d\ncontent %d\n' "$i" "$i" > "/tmp/load-$i.md"
  nx store put "/tmp/load-$i.md" --collection knowledge__shakeout --title "load-$i" >/dev/null 2>&1
done
wait "$PA"; RA=$?
wait "$PB"; RB=$?
[ "$RA" = 0 ] && ok "parallel index A (exit 0)" || bad "parallel index A failed"
[ "$RB" = 0 ] && ok "parallel index B (exit 0)" || bad "parallel index B failed"

fivexx=$(tail -n "+$((LOG_MARK+1))" "$SERVICE_LOG" 2>/dev/null | grep -cE '\" (5[0-9][0-9]) |status=5[0-9][0-9]|HTTP 5[0-9][0-9]' || true)
dwf=$(cat "$IDXA" "$IDXB" 2>/dev/null | grep -c "dual_write_failed" || true)
[ "${fivexx:-0}" = 0 ] && ok "zero 5xx in service log under concurrent load" || bad "$fivexx 5xx responses under load (lock-convoy class)"
[ "${dwf:-0}" = 0 ]    && ok "zero dual-write failures under concurrent load"  || bad "$dwf dual-write failures under load"

# ── Verdict ──────────────────────────────────────────────────────────────────
say "Verdict"
if [ "$FAILS" = 0 ]; then
  printf '\033[32mCANDIDATE SHAKEOUT PASSED\033[0m — verb matrix + incremental index + concurrent load, all against the locally-built candidate; nothing published, nothing deployed\n'
  exit 0
else
  printf '\033[31mCANDIDATE SHAKEOUT FAILED\033[0m — %d failure(s); see PASS/FAIL lines above\n' "$FAILS"
  exit 1
fi
