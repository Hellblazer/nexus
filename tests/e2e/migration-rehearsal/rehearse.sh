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

note "nx init --service — provisioning PG16 + pgvector + nexus DB…"
if nx init --service --embedder minilm-384 --yes 2>&1 | sed 's/^/       /'; then
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

# nexus-jrrve: the Java service constructs OnnxEmbedder UNCONDITIONALLY at boot
# (the embedder choice is minilm-384 from `nx init`; a Voyage key only ADDS the
# cloud leg on top, it does not replace onnx-local). So the minilm model must be
# present even on the cloud leg, but no nx step fetches it on a fresh install.
# Warm it the way a real first local-embed use would (chromadb downloads the
# model to ~/.cache/chroma/onnx_models/...).
note "warming all-MiniLM-L6-v2 ONNX cache (nexus-jrrve workaround)…"
if python - <<'PY' 2>&1 | sed 's/^/       /'
from chromadb.utils.embedding_functions import ONNXMiniLM_L6_V2
ONNXMiniLM_L6_V2()(["warmup"])
print("onnx minilm model materialized")
PY
then :; else
  # Don't let a silent warmup failure masquerade as a service/PG fault below.
  bad "ONNX warmup failed (nexus-jrrve workaround) — service start will likely fail"
fi

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

# Parity: the migrated collections should now be served from pgvector. Compare
# the per-collection live count against the seeded count.
note "validating parity (service collection counts == seeded)…"
python - "$SEED_JSON" <<'PY' && ok "parity validated" || bad "parity mismatch / unverified"
import json, sys
seeded = json.loads(sys.argv[1]).get("collections", {})
if not seeded:
    print("       no seed manifest — cannot validate"); sys.exit(1)
try:
    from nexus.db import make_t3
    t3 = make_t3()
    bad = 0
    for name, want in seeded.items():
        try:
            got = t3.count(name)
        except Exception as e:
            print(f"       {name}: count() error: {e}"); bad += 1; continue
        flag = "ok" if got == want else "MISMATCH"
        print(f"       {name}: service={got} seeded={want} [{flag}]")
        if got != want:
            bad += 1
    sys.exit(1 if bad else 0)
except Exception as e:
    print(f"       validation harness error: {e}"); sys.exit(1)
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
