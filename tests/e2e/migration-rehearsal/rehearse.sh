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

note "nx daemon service start — spawn native binary, migrate changesets, await /health…"
nx daemon service start 2>&1 | sed 's/^/       /' || true
# Poll the status surface (pebfx.5) for a healthy service.
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

# The guided migrate (and the parity/ref harnesses) resolve the service endpoint
# via NX_SERVICE_URL + NX_SERVICE_TOKEN, else the supervisor lease. Export both
# explicitly from authoritative sources so a separate subprocess does not depend
# on lease-heartbeat timing: the URL from the live status port, the token from
# the pg_credentials sourced above (`nx init --service` persists it there).
SVC_PORT="$(nx daemon service status 2>/dev/null | awk -F': *' '/^[[:space:]]*port:/{print $2; exit}')"
if [ -n "$SVC_PORT" ]; then
  export NX_SERVICE_URL="http://127.0.0.1:${SVC_PORT}"
  export NX_SERVICE_PORT="$SVC_PORT" NX_SERVICE_HOST="127.0.0.1"
  ok "service endpoint exported (NX_SERVICE_URL=$NX_SERVICE_URL)"
else
  bad "could not parse service port from status — migrate will rely on lease discovery"
fi
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

note "nx migrate-to-service --dry-run (classify footprint)…"
nx migrate-to-service --dry-run --local-path "$CHROMA_LOCAL" 2>&1 | sed 's/^/       /' \
  && ok "dry-run classified the footprint" || bad "dry-run failed"

note "nx migrate-to-service (detect → ETL → validate → unlock)…"
if nx migrate-to-service --local-path "$CHROMA_LOCAL" 2>&1 | sed 's/^/       /'; then
  ok "migrate-to-service completed"
else
  bad "migrate-to-service failed"
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

# RDR-162 P2: the SOURCELESS note's topic_assignment must be re-pointed from the
# minilm source name to the bge-768 target, and that target must be a live
# pgvector collection (the taxonomy-consistency guarantee for store_put notes
# embed_migrate cannot upgrade).
note "validating sourceless-note reference remap (topic_assignment -> bge target)…"
python - "$SEED_JSON" <<'PY' && ok "sourceless note refs resolve to the bge target" || bad "sourceless note refs not remapped / dangling"
import json, sys
m = json.loads(sys.argv[1])
cross = m.get("cross_model", {})
note_src = "knowledge__rehearsal-note__minilm-l6-v2-384__v1"
note_tgt = cross.get(note_src)
if not note_tgt:
    print("       no sourceless note in seed manifest — cannot validate"); sys.exit(1)
try:
    from nexus.db.t2 import T2Database
    from nexus.config import nexus_config_dir
    db = T2Database(nexus_config_dir() / "memory.db")
    rows = {
        r[0]
        for r in db.taxonomy.conn.execute(
            "SELECT DISTINCT source_collection FROM topic_assignments "
            "WHERE source_collection IS NOT NULL"
        ).fetchall()
    }
    if note_src in rows:
        print(f"       FAIL: assignment still names the dead source {note_src}")
        sys.exit(1)
    if note_tgt not in rows:
        print(f"       FAIL: assignment was not re-pointed to {note_tgt} (rows={sorted(rows)})")
        sys.exit(1)
    from nexus.db import make_t3
    if make_t3().count(note_tgt) <= 0:
        print(f"       FAIL: target {note_tgt} not populated in pgvector")
        sys.exit(1)
    print(f"       assignment re-pointed {note_src} -> {note_tgt} (live in pgvector)")
    sys.exit(0)
except Exception as e:
    print(f"       ref-remap harness error: {e}"); sys.exit(1)
PY

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
