#!/usr/bin/env bash
# Soup-to-nuts migration dress rehearsal — runs INSIDE the container.
#
# Phase A  install + provision + serve   (the fragile legs: nx init --service
#                                          provisions PG+pgvector; the native
#                                          nexus-service binary migrates 64
#                                          changesets and serves /health)
# Phase B  seed legacy Chroma + migrate  (nx migrate-to-service: detect → ETL →
#                                          validate; parity assertion)
# Phase C  rollback rehearsal            (Chroma source intact; degrade-loud, no
#                                          bare-empty-index)
#
# CHROMA_LOCAL: the legacy on-disk Chroma store (the migration SOURCE).
# WITH_CLOUD=1 adds the voyage-context-3 leg (needs NX_VOYAGE_API_KEY in env).
set -uo pipefail

CHROMA_LOCAL="${CHROMA_LOCAL:-/home/nexus/legacy-chroma}"
SEED_N="${SEED_N:-12}"
WITH_CLOUD="${WITH_CLOUD:-0}"
FAILS=0

say()  { printf '\n\033[1m== %s ==\033[0m\n' "$*"; }
ok()   { printf '  \033[32mPASS\033[0m %s\n' "$*"; }
bad()  { printf '  \033[31mFAIL\033[0m %s\n' "$*"; FAILS=$((FAILS+1)); }
note() { printf '       %s\n' "$*"; }

# ── Phase A: install + provision + serve ─────────────────────────────────────
say "Phase A — install + provision + serve"

# RDR-161: the native nexus-service binary is the SOLE launch artifact (no JRE,
# no java -jar). The image ships it + its native-image .so siblings under
# /opt/nexus-service-native/; position the whole set at the well-known location
# (the launcher's job) so the binary resolves its dlopen'd JDK libs co-located.
SVC_NATIVE_DIR="/opt/nexus-service-native"
SVC_WELL_KNOWN_DIR="$HOME/.config/nexus/service"

nx --version >/dev/null 2>&1 && ok "nx installed ($(nx --version 2>&1))" || bad "nx --version failed"
command -v initdb >/dev/null 2>&1 && ok "PG16 binaries on PATH ($(initdb --version))" || bad "initdb not found"
test -x "$SVC_NATIVE_DIR/nexus-service" && ok "native service binary present ($(du -h "$SVC_NATIVE_DIR/nexus-service" | cut -f1))" || bad "native binary missing at $SVC_NATIVE_DIR"

note "positioning the native binary + libs at the well-known location…"
if mkdir -p "$SVC_WELL_KNOWN_DIR" \
   && cp "$SVC_NATIVE_DIR"/* "$SVC_WELL_KNOWN_DIR/" \
   && chmod +x "$SVC_WELL_KNOWN_DIR/nexus-service"; then
  ok "native binary positioned ($SVC_WELL_KNOWN_DIR/nexus-service)"
else
  bad "could not position native binary"
fi

# nexus-lz3f2: bound the native service's heap so the boot memory peak (bge-768
# ONNX load + PG + the Python supervisor in this container's shared VM) does not
# trip the cgroup OOM killer — which previously SIGKILLed the SUPERVISOR (not the
# JVM), silently vanishing the lease and failing the migrate intermittently. The
# supervisor inherits this env and passes -Xmx to the native binary.
export NX_SERVICE_MAX_HEAP="${NX_SERVICE_MAX_HEAP:-1g}"
note "nx init --service — provisioning PG16 + pgvector + nexus DB (bge-768 service embedder, RDR-160; NX_SERVICE_MAX_HEAP=$NX_SERVICE_MAX_HEAP)…"
if nx init --service --embedder bge-768 --yes 2>&1 | sed 's/^/       /'; then
  ok "nx init --service (provision)"
else
  bad "nx init --service failed"; say "ABORT (provision failed)"; exit 1
fi

note "configuring service backend env (NX_STORAGE_BACKEND + pg_credentials)…"
export NX_STORAGE_BACKEND=service
# shellcheck disable=SC1091
set -a; . /home/nexus/.config/nexus/pg_credentials; set +a
[ "$WITH_CLOUD" = 1 ] && [ -n "${NX_VOYAGE_API_KEY:-${VOYAGE_API_KEY:-}}" ] \
  && export VOYAGE_API_KEY="${VOYAGE_API_KEY:-$NX_VOYAGE_API_KEY}" \
            NX_VOYAGE_API_KEY="${NX_VOYAGE_API_KEY:-$VOYAGE_API_KEY}"

# RDR-160: the service's local ONNX embedder is bge-768, fetched by
# `nx init --service --embedder bge-768` above (the standard fp32 ONNX, closing
# the nexus-jrrve fresh-install gap for the bge model). No separate minilm
# warmup — minilm-384 is exactly the model the migrate must treat as UNSERVABLE
# (the seeded legacy collections), so the rehearsal proves the cross-model
# re-embed, not a minilm-served fallback.

# nexus-qke1e: `nx init --service` ALREADY started the persistent, heartbeated
# supervisor. Do NOT call `nx daemon service start` again — a second supervisor
# races the first (short-circuits on the live lease, then its heartbeat loop
# respawns the service), causing a mid-run port-churn + lease-fencing that breaks
# the migrate. Just verify the init-started service is healthy.
note "verifying the init-started service is healthy (no second supervisor)…"
healthy=0
for i in $(seq 1 30); do
  if nx daemon service status 2>&1 | grep -qiE "health.*ok|healthy|serving|status.*ok|running"; then
    healthy=1; break
  fi
  sleep 2
done
nx daemon service status 2>&1 | sed 's/^/       /' || true
[ "$healthy" = 1 ] && ok "service healthy (native binary serving, schema migrated)" || bad "service did not reach healthy"

if [ "$healthy" != 1 ]; then say "ABORT (service never came up — Phase A is the gate)"; exit 1; fi

# Clients resolve the endpoint via the supervisor LEASE (now maintained by the
# persistent supervisor — nexus-qke1e). Do NOT pin NX_SERVICE_URL/PORT: the
# supervisor re-allocates the port + republishes the lease on any service
# respawn, and a pinned env port would defeat that recovery (the stale-env-port
# trap in service_endpoint.py). The token is stable across restarts, so the
# pg_credentials NX_SERVICE_TOKEN (sourced above) is safe to keep.
unset NX_SERVICE_URL NX_SERVICE_PORT NX_SERVICE_HOST 2>/dev/null || true
[ -n "${NX_SERVICE_TOKEN:-}" ] && ok "NX_SERVICE_TOKEN present (from pg_credentials)" \
  || bad "NX_SERVICE_TOKEN absent — guided migrate requires it (pg_credentials did not carry it)"

# ── Phase D: comprehensive daily-driver surface (COMPREHENSIVE=1) ─────────────
# Proves 6.0.0 replaces the day-to-day nexus surface through the REAL service
# (PG16+pgvector+bge-768), fully isolated. Deterministic, no API creds: the
# bge-768 knowledge path + T2 memory + T1 scratch + catalog + doctor are HARD
# assertions; the LLM-composition verbs (nx_answer, nx enrich aspects) and code
# indexing (voyage-code-3) are creds-gated and reported as NOTED-SKIP, never
# faked green. Runs on the clean service BEFORE the migration phases mutate it.
if [ "${COMPREHENSIVE:-0}" = 1 ]; then
  say "Phase D — daily-driver surface (deterministic bge-768 local; no API creds)"
  MARK="ddmark$$"
  DD=/tmp/dd.out
  _why() { note "↳ $(tail -4 "$DD" 2>/dev/null | tr '\n' ' ' | sed 's/  */ /g')"; }

  # T2 memory (FTS5): put -> search round-trip
  if nx memory put "comprehensive shakeout $MARK widget sprocket note" -p ddshakeout -t "note-$MARK" --tags rehearsal >"$DD" 2>&1; then
    if nx memory search "$MARK" 2>/dev/null | grep -q "$MARK"; then ok "T2 memory put+search round-trip"
    else bad "T2 memory search did not surface the put"; fi
  else bad "nx memory put failed"; _why; fi

  # T1 scratch: T1 is MCP-session working memory (RDR-105). Service-backed T1
  # requires a MINTED session token (the MCP lifespan mints it via /v1/sessions/
  # start); a bare CLI cannot, so it uses the in-process ephemeral store. Force
  # ephemeral (env -u beats the container's NX_STORAGE_BACKEND_T1=service, which
  # otherwise wins over NX_T1_ISOLATED) and assert the CLI write+list path; note
  # that persistent/cross-process T1 is MCP-session-scoped, not a bare-CLI property.
  if env -u NX_STORAGE_BACKEND -u NX_STORAGE_BACKEND_T1 NX_T1_ISOLATED=1 \
       nx scratch put "scratch shakeout $MARK" >"$DD" 2>&1 \
     && env -u NX_STORAGE_BACKEND -u NX_STORAGE_BACKEND_T1 NX_T1_ISOLATED=1 \
       nx scratch list >"$DD" 2>&1; then
    ok "T1 scratch CLI write+list (ephemeral; persistent T1 is MCP-session-scoped, RDR-105)"
  else bad "nx scratch CLI failed"; _why; fi

  # T3 knowledge (bge-768): store put -> semantic search round-trip
  if printf 'Comprehensive shakeout knowledge doc %s. The quick brown fox indexes widgets and sprockets deterministically for bge-768 retrieval.\n' "$MARK" \
       | nx store put - -t "shakeout-$MARK" --tags rehearsal >"$DD" 2>&1; then
    if nx search "widgets and sprockets for retrieval" --corpus knowledge -m 5 2>/dev/null | grep -q "$MARK"; then
      ok "T3 store put + bge-768 semantic search round-trip"
    else bad "T3 semantic search did not surface the stored doc"; fi
  else bad "nx store put failed"; _why; fi

  # T3 collection listing reflects the new knowledge collection
  if nx collection list 2>/dev/null | grep -qi "knowledge"; then ok "nx collection list shows the knowledge collection"
  else bad "nx collection list did not show a knowledge collection"; fi

  # Catalog surface responds (service-mode catalog over PG)
  if nx catalog list 2>/dev/null >/dev/null; then ok "catalog surface responds (nx catalog list)"
  else bad "nx catalog list failed"; fi

  # doctor: SERVICE + schema health only. npx-not-found and MinerU-unreachable are
  # expected in the minimal container (no node, no MinerU server) — advisory, not a
  # 6.0.0 readiness signal. Fail only on a real service/schema/pgvector problem.
  nx doctor >"$DD" 2>&1 || true
  if grep -qiE "service.*(unreachable|down|not running)|schema.*(mismatch|behind|drift)|pgvector.*(missing|unreachable)|database.*(unreachable|locked)" "$DD"; then
    bad "nx doctor: service/schema problem"; _why
  else ok "nx doctor: service+schema healthy (npx/MinerU advisories ignored in the minimal box)"; fi

  # index repo (code): voyage-code-3 is creds-gated in this no-voyage box — attempt,
  # but NOTE (do not fail) if the model-identity guard blocks it. Honest coverage.
  ddrepo="/tmp/dd-corpus-$$"
  mkdir -p "$ddrepo"
  printf 'def widget_sprocket():\n    """Deterministic fixture %s."""\n    return 42\n' "$MARK" > "$ddrepo/mod.py"
  ( cd "$ddrepo" && git init -q && git add -A && git -c user.email=r@n.local -c user.name=r commit -qm seed ) 2>/dev/null || true
  if nx index repo "$ddrepo" >/tmp/dd_index.txt 2>&1; then
    ok "nx index repo succeeded (code embedding available in this box)"
  else
    note "nx index repo NOT exercised here (voyage-code-3 creds-gated in the no-API-key box) — expected; covered by the credentialed/cloud path, not this airtight leg"
  fi
  rm -rf "$ddrepo" "$DD" /tmp/dd_index.txt 2>/dev/null || true

  note "Phase D covers the DETERMINISTIC surface; nx_answer + nx enrich aspects (LLM/Voyage dispatch) are intentionally OUT of the airtight leg (need API creds) — not faked."
fi

# ── Phase B: seed legacy Chroma + migrate-to-service ─────────────────────────
say "Phase B — seed legacy Chroma + migrate-to-service"

# Catalog.init (in seed_legacy.py) runs `git init`; give git an identity.
git config --global user.email "rehearsal@nexus.local" >/dev/null 2>&1 || true
git config --global user.name  "nexus rehearsal"       >/dev/null 2>&1 || true

seed_args=("$CHROMA_LOCAL" "--n" "$SEED_N")
[ "$WITH_CLOUD" = 1 ] && seed_args+=("--with-cloud")
# seed_legacy.py builds T2/catalog SQLite (run_migrations logs to stdout); the
# JSON manifest is the LAST line. Capture only it so the parity parser gets
# clean JSON.
if SEED_RAW="$(python /home/nexus/seed_legacy.py "${seed_args[@]}")"; then
  SEED_JSON="$(printf '%s\n' "$SEED_RAW" | tail -1)"
  ok "seeded legacy footprint: $SEED_JSON"
else
  bad "seed failed"; SEED_JSON='{}'
fi

# Diagnostic: dump service status + logs on demand (a connection-refused at
# migrate time means the native service HTTP listener died after publishing a
# healthy lease — a service-lifecycle/native-binary fault, not migration logic).
_dump_service_diag() {
  nx daemon service status 2>&1 | sed 's/^/         /' || true
  for lg in storage_service.log storage_service_native.log storage_service.crash.log; do
    f="$HOME/.config/nexus/logs/$lg"
    [ -f "$f" ] && { echo "         --- tail $lg ---"; tail -30 "$f" | sed 's/^/         /'; }
  done
}

# Informational pre-migrate status (NOT a gate): `nx service probe` can resolve a
# configured MANAGED endpoint rather than the local lease, so it is a false
# signal here — the authoritative reachability test is the migrate itself, whose
# client resolves the local supervisor lease. The post-migrate log dump below is
# the real diagnostic for a native-service death.
note "pre-migrate service status (informational):"
nx daemon service status 2>&1 | grep -iE "status:|health:|port:|pid:" | sed 's/^/       /' || true

note "nx migrate-to-service --dry-run (classify footprint)…"
nx migrate-to-service --dry-run --local-path "$CHROMA_LOCAL" 2>&1 | sed 's/^/       /' \
  && ok "dry-run classified the footprint" || bad "dry-run failed"

note "nx migrate-to-service (detect → ETL → validate → unlock)…"
# --yes: skip the Voyage re-embed cost confirmation (nexus-cewad). Harmless on the
# ONNX leg (nothing billed → no prompt); REQUIRED on --with-cloud where the
# cross-model→voyage re-embed is billed and click.confirm aborts on the rehearsal's
# non-interactive stream (else: empty pgvector target → parity MISMATCH).
if nx migrate-to-service --local-path "$CHROMA_LOCAL" --yes 2>&1 | sed 's/^/       /'; then
  ok "migrate-to-service completed"
else
  bad "migrate-to-service failed"
  note "service status + logs (post-migrate diagnosis):"; _dump_service_diag
fi

# Parity: each seeded collection should now be served from pgvector — but the
# legacy minilm-384 collections were CROSS-MODEL migrated (RDR-162), so their
# chunks land under the bge-768 TARGET name, not the source name. Compare the
# live count at the TARGET name (source name for the byte-for-byte voyage leg).
note "validating cross-model parity (pgvector TARGET counts == seeded)…"
python - "$SEED_JSON" <<'PY' && ok "cross-model parity validated" || bad "parity mismatch / unverified"
import json, sys
m = json.loads(sys.argv[1])
seeded = m.get("collections", {})
cross = m.get("cross_model", {})
if not seeded:
    print("       no seed manifest — cannot validate"); sys.exit(1)
try:
    from nexus.db import make_t3
    t3 = make_t3()
    bad = 0
    for name, want in seeded.items():
        target = cross.get(name, name)  # cross-model -> bge target; else same
        # Source name must be ABSENT from pgvector (re-embed lands at target).
        if target != name:
            try:
                stray = t3.count(name)
            except Exception:
                stray = 0
            if stray:
                print(f"       {name}: SOURCE name has {stray} in pgvector (should be 0)")
                bad += 1
        try:
            got = t3.count(target)
        except Exception as e:
            print(f"       {target}: count() error: {e}"); bad += 1; continue
        flag = "ok" if got == want else "MISMATCH"
        print(f"       {name} -> {target}: service={got} seeded={want} [{flag}]")
        if got != want:
            bad += 1
    sys.exit(1 if bad else 0)
except Exception as e:
    print(f"       validation harness error: {e}"); sys.exit(1)
PY

# RDR-162 P2: the SOURCELESS note proof. The note's topic_assignment named the
# minilm source; the cross-model migrate must re-point it to the bge-768 target.
# This is proven IMPLICITLY by the guided migrate's clean unlock above: the
# validation gate runs verify_taxonomy_consistency, which BLOCKS unlock unless
# every topic_assignments.source_collection resolves to a migrated (pgvector)
# collection. A clean unlock therefore means the sourceless note's assignment was
# re-pointed to its live bge target — the case embed_migrate cannot upgrade. (No
# direct SQL probe here: in service mode T2 is Postgres behind the HTTP store, so
# the migrate's own validation leg is the authoritative ref-remap check.)
note "sourceless-note ref-remap is gated by the guided migrate's clean unlock above"

# ── Phase C: rollback rehearsal (Chroma source intact) ───────────────────────
say "Phase C — rollback safety (copy-not-move: legacy Chroma intact)"
python - "$CHROMA_LOCAL" "$SEED_JSON" <<'PY' && ok "legacy Chroma still intact post-migration (copy-not-move)" || bad "legacy Chroma damaged by migration"
import json, sys, chromadb
path, seed = sys.argv[1], json.loads(sys.argv[2]).get("collections", {})
client = chromadb.PersistentClient(path=path)
bad = 0
for name, want in seed.items():
    got = client.get_collection(name).count()
    print(f"       {name}: source still has {got} (seeded {want})")
    if got != want:
        bad += 1
sys.exit(1 if bad else 0)
PY

# ── Summary ──────────────────────────────────────────────────────────────────
say "RESULT"
if [ "$FAILS" -eq 0 ]; then
  printf '\033[32mSOUP-TO-NUTS REHEARSAL PASSED\033[0m — install → provision → serve → seed → migrate → validate → rollback-safe\n'
  exit 0
else
  printf '\033[31mREHEARSAL FAILED — %d check(s) failed\033[0m\n' "$FAILS"
  exit 1
fi
