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

note "nx init --service — provisioning PG16 + pgvector + nexus DB (bge-768 service embedder, RDR-160)…"
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
if nx migrate-to-service --local-path "$CHROMA_LOCAL" 2>&1 | sed 's/^/       /'; then
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
