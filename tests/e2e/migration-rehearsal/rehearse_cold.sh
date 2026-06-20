#!/usr/bin/env bash
# nexus-4mm24 — COLD-ACQUIRE fresh-machine validation. Runs INSIDE the container.
#
# Proves the fresh-user journey the MVV skips by pre-staging: from a clean box
# with ONLY the conexus wheel, cold-acquire every service artifact from the
# PUBLISHED engine-service release, provision, and migrate. Secret-free (ONNX
# leg only — no Voyage key needed; the local service embeds with bge-768).
#
#   install-binary <tag>  cold-acquire native binary + PG bundle (cosign-verified)
#   init --service        extract bundle -> provision PG -> fetch bge ONNX -> serve
#   /version              assert release_version == the acquired tag (published stamp)
#   seed legacy           pre-RDR-160 minilm-384 footprint + sourceless note
#   guided-upgrade        detect -> verify -> migrate -> "VERIFIED and unlocked"
#   parity + source-intact
set -uo pipefail

SERVICE_TAG="${NEXUS_SERVICE_TAG:?NEXUS_SERVICE_TAG must be set (e.g. engine-service-v0.1.6)}"
EXPECT_RELEASE_VERSION="${SERVICE_TAG#engine-service-v}"
CHROMA_LOCAL="${CHROMA_LOCAL:-/home/nexus/legacy-chroma}"
SEED_N="${SEED_N:-12}"
FAILS=0

say()  { printf '\n\033[1m== %s ==\033[0m\n' "$*"; }
ok()   { printf '  \033[32mPASS\033[0m %s\n' "$*"; }
bad()  { printf '  \033[31mFAIL\033[0m %s\n' "$*"; FAILS=$((FAILS+1)); }
note() { printf '       %s\n' "$*"; }

# Quarantine assertions: prove the box really is bare before we acquire.
say "Quarantine — nothing pre-staged"
nx --version >/dev/null 2>&1 && ok "nx installed ($(nx --version 2>&1))" || bad "nx --version failed"
command -v initdb >/dev/null 2>&1 && bad "system PostgreSQL present — not a cold box" || ok "no system PostgreSQL (bundle must provide it)"
test ! -e "$HOME/.config/nexus/service/nexus-service" && ok "no native binary pre-staged" || bad "native binary already present — not cold"

export NX_SERVICE_MAX_HEAP="${NX_SERVICE_MAX_HEAP:-1g}"
git config --global user.email "cold@nexus.local" >/dev/null 2>&1 || true
git config --global user.name  "nexus cold"       >/dev/null 2>&1 || true

# ── Cold-acquire: native binary + PG bundle from the PUBLISHED release ────────
say "Cold-acquire — nx daemon service install-binary $SERVICE_TAG"
note "downloads the native binary AND the PG+pgvector bundle from the release,"
note "cosign-verified (offline, sigstore-python) — no system PG, no pre-stage."
if nx daemon service install-binary "$SERVICE_TAG" 2>&1 | sed 's/^/       /'; then
  ok "install-binary acquired + verified binary + PG bundle"
else
  bad "install-binary failed (cold-acquire of binary/bundle)"; say "ABORT"; exit 1
fi
test -x "$HOME/.config/nexus/service/nexus-service" \
  && ok "native binary now present (cold-acquired)" || bad "binary missing after install-binary"

# ── Provision + serve from the cold-acquired artifacts ───────────────────────
say "Provision + serve — nx init --service (extract bundle, provision PG, fetch bge ONNX)"
export NEXUS_SERVICE_TAG="$SERVICE_TAG"   # _ensure_service_binary_step no-ops (already installed)
if nx init --service --embedder bge-768 --yes 2>&1 | sed 's/^/       /'; then
  ok "nx init --service (bundle-provisioned PG + bge ONNX + service started)"
else
  bad "nx init --service failed"; say "ABORT (provision failed)"; exit 1
fi
export NX_STORAGE_BACKEND=service
# shellcheck disable=SC1091
[ -f "$HOME/.config/nexus/pg_credentials" ] && { set -a; . "$HOME/.config/nexus/pg_credentials"; set +a; }
unset NX_SERVICE_URL NX_SERVICE_PORT NX_SERVICE_HOST 2>/dev/null || true

healthy=0
for _ in $(seq 1 30); do
  if nx daemon service status 2>&1 | grep -qiE "health.*ok|healthy|serving|status.*ok|running"; then
    healthy=1; break
  fi
  sleep 2
done
nx daemon service status 2>&1 | sed 's/^/       /' || true
[ "$healthy" = 1 ] && ok "service healthy (cold-acquired binary serving on the bundled PG)" \
  || { bad "service did not reach healthy"; say "ABORT"; exit 1; }

# The PUBLISHED binary must report release_version == the acquired tag — proves
# both the Option-B stamp AND that install-binary fetched the right release.
RV="$(nx daemon service status --json 2>/dev/null | python -c 'import sys,json;print(json.load(sys.stdin).get("service_release_version") or "")' 2>/dev/null)"
[ "$RV" = "$EXPECT_RELEASE_VERSION" ] \
  && ok "/version release_version=$RV matches the acquired tag" \
  || bad "release_version=$RV != expected $EXPECT_RELEASE_VERSION (wrong binary or stamp)"

# ── Seed pre-RDR-160 footprint + guided-upgrade (ONNX leg, secret-free) ───────
say "Seed pre-RDR-160 footprint + nx guided-upgrade"
if SEED_RAW="$(python /home/nexus/seed_legacy.py "$CHROMA_LOCAL" --n "$SEED_N")"; then
  SEED_JSON="$(printf '%s\n' "$SEED_RAW" | tail -1)"
  ok "seeded legacy footprint: $SEED_JSON"
else
  bad "seed failed"; say "ABORT"; exit 1
fi

GU_OUT="$(nx guided-upgrade --local-path "$CHROMA_LOCAL" --timeout 180 --yes 2>&1)"
GU_RC=$?
printf '%s\n' "$GU_OUT" | sed 's/^/       /'
[ "$GU_RC" = 0 ] && ok "nx guided-upgrade exited 0" || bad "nx guided-upgrade exited $GU_RC"
printf '%s' "$GU_OUT" | grep -q "Migration VERIFIED and unlocked" \
  && ok "migration VERIFIED and unlocked (cold-acquired stack end-to-end)" \
  || bad "no 'Migration VERIFIED and unlocked' line"

# ── Parity + source intact (reuse the MVV checks) ────────────────────────────
say "Parity — pgvector serves the migrated collections"
python - "$SEED_JSON" <<'PY' && ok "cross-model parity validated" || bad "parity mismatch / unverified"
import json, sys
m = json.loads(sys.argv[1]); seeded = m.get("collections", {}); cross = m.get("cross_model", {})
if not seeded:
    print("       no seed manifest"); sys.exit(1)
try:
    from nexus.db import make_t3
    t3 = make_t3(); bad = 0
    for name, want in seeded.items():
        target = cross.get(name, name)
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
    print(f"       harness error: {e}"); sys.exit(1)
PY

say "Rollback safety — legacy Chroma untouched"
python - "$CHROMA_LOCAL" "$SEED_JSON" <<'PY' && ok "legacy Chroma still intact" || bad "legacy Chroma damaged"
import json, sys, chromadb
path, seed = sys.argv[1], json.loads(sys.argv[2]).get("collections", {})
client = chromadb.PersistentClient(path=path); bad = 0
for name, want in seed.items():
    got = client.get_collection(name).count()
    print(f"       {name}: source still has {got} (seeded {want})")
    if got != want:
        bad += 1
sys.exit(1 if bad else 0)
PY

say "RESULT"
if [ "$FAILS" -eq 0 ]; then
  printf '\033[32mCOLD-ACQUIRE MVV PASSED\033[0m — bare box -> install-binary -> init --service -> guided-upgrade, all artifacts cold-acquired from the published release\n'
  exit 0
else
  printf '\033[31mCOLD-ACQUIRE MVV FAILED — %d check(s) failed\033[0m\n' "$FAILS"
  exit 1
fi
