#!/usr/bin/env bash
# nexus-1ddsy — PUBLISHED-ARTIFACT acquire gate. Runs INSIDE the container.
#
# Exercises the ACTUAL RELEASE ARTIFACT on a bare box, before conexus is asked
# to deploy it. Hal directive 2026-07-24: "we *must* exercise the binary before
# conexus... I don't want to test this live on prod."
#
#   install-binary <tag>  cold-acquire native binary + PG bundle (cosign-verified)
#   init --service        extract bundle -> provision PG -> fetch bge ONNX -> serve
#   /version              assert release_version == the acquired tag
#   store/index/search    drive the binary: write, embed, read back
#   doctor                no ✗
#
# WHY THIS EXISTS SEPARATELY FROM --shakeout. The shakeout drives the LOCALLY
# BUILT -Ob candidate. The published artifact is a different set of bytes from a
# different builder: full native build (not quick-build), codesign, cosign, and
# the PG-bundle packaging. A defect introduced by the release workflow is
# invisible to the local shakeout BY CONSTRUCTION — nexus-2oh5q is exactly that
# hazard (signing breaking JNI dlopen of the bundled onnxruntime/DJL libs),
# dormant today only because the Apple secrets are unprovisioned. This leg is
# what would catch it.
#
# LINEAGE. This is the acquire half of the retired `rehearse_cold.sh`
# (nexus-4mm24), which RDR-155 P4b retired because its TAIL drove
# `nx guided-upgrade`. The acquire half never depended on those verbs; retiring
# the whole script took a published-artifact gate with it and left conexus's
# cloud gate as the first exercise of the published bytes — i.e. testing on
# production. Historically this gate caught nexus-pi3s3 + nexus-qeoxf
# (2026-06-26), defects every local suite missed.
#
# Secret-free: ONNX leg only (bge-768), no Voyage key.
set -uo pipefail

SERVICE_TAG="${NEXUS_SERVICE_TAG:?NEXUS_SERVICE_TAG must be set to the PUBLISHED tag (e.g. engine-service-v0.1.55)}"
EXPECT_RELEASE_VERSION="${SERVICE_TAG#engine-service-v}"
FAILS=0

say()  { printf '\n\033[1m== %s ==\033[0m\n' "$*"; }
ok()   { printf '  \033[32mPASS\033[0m %s\n' "$*"; }
bad()  { printf '  \033[31mFAIL\033[0m %s\n' "$*"; FAILS=$((FAILS+1)); }
note() { printf '       %s\n' "$*"; }

# ── Quarantine: prove the box really is bare before we acquire ───────────────
say "Quarantine — nothing pre-staged"
nx --version >/dev/null 2>&1 && ok "nx installed ($(nx --version 2>&1))" || bad "nx --version failed"
command -v initdb >/dev/null 2>&1 && bad "system PostgreSQL present — not a cold box" || ok "no system PostgreSQL (bundle must provide it)"
test ! -e "$HOME/.config/nexus/service/nexus-service" && ok "no native binary pre-staged" || bad "native binary already present — not cold"

export NX_SERVICE_MAX_HEAP="${NX_SERVICE_MAX_HEAP:-1g}"
git config --global user.email "acquire@nexus.local" >/dev/null 2>&1 || true
git config --global user.name  "nexus acquire"       >/dev/null 2>&1 || true

# ── Cold-acquire the PUBLISHED artifacts ────────────────────────────────────
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

# ── Provision + serve from the cold-acquired artifacts ──────────────────────
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
[ "$healthy" = 1 ] && ok "service healthy (published binary serving on the bundled PG)" \
  || { bad "service did not reach healthy"; say "ABORT"; exit 1; }

# The PUBLISHED binary must report release_version == the acquired tag — proves
# both the tag-time stamp AND that install-binary fetched the right release.
RV="$(nx daemon service status --json 2>/dev/null | python -c 'import sys,json;print(json.load(sys.stdin).get("service_release_version") or "")' 2>/dev/null)"
if [ "$RV" = "$EXPECT_RELEASE_VERSION" ]; then
  ok "/version release_version=$RV matches the acquired tag"
elif [ -z "$RV" ]; then
  bad "no release_version on /version (service unreachable or field absent), expected $EXPECT_RELEASE_VERSION"
else
  bad "release_version=$RV != expected $EXPECT_RELEASE_VERSION (wrong binary or stamp)"
fi

# ── DRIVE the published binary ───────────────────────────────────────────────
# Reporting a version only proves the process booted and can read its own
# stamp. The workflow-introduced defects this gate exists for (a signing step
# that breaks JNI dlopen of the bundled ONNX/tokenizers libs — nexus-2oh5q; a
# stripped native-image resource; a mis-packaged bundle) all present as a
# binary that STARTS and then fails the moment it must actually embed. So the
# gate must force an embed and a read-back, not just a handshake.
say "Drive — store + search round-trip (forces a real ONNX embed on the published binary)"
PROBE_TEXT="acquire-gate probe $(date +%s) pgvector bge768 roundtrip"
if nx store put "$PROBE_TEXT" --title "acquire-gate-probe" 2>&1 | sed 's/^/       /'; then
  ok "nx store put succeeded (embed + vector write on the published binary)"
else
  bad "nx store put failed — the published binary cannot embed/write"
fi

FOUND=0
for _ in $(seq 1 10); do
  if nx search "acquire-gate probe pgvector" 2>&1 | grep -qi "acquire-gate-probe"; then
    FOUND=1; break
  fi
  sleep 2
done
[ "$FOUND" = 1 ] && ok "nx search returned the stored document (embed + read-back path intact)" \
  || bad "nx search did not return the probe document — write or read path broken on the published binary"

# ── doctor must be clean ─────────────────────────────────────────────────────
say "Doctor — no ✗ on a freshly acquired install"
DOC_OUT="$(nx doctor 2>&1 || true)"
echo "$DOC_OUT" | sed 's/^/       /'
if echo "$DOC_OUT" | grep -q "✗"; then
  bad "nx doctor reported at least one ✗ on the published binary"
else
  ok "nx doctor clean"
fi

say "RESULT"
if [ "$FAILS" -eq 0 ]; then
  printf '  \033[32mACQUIRE GATE PASSED\033[0m — published %s exercised on a bare box\n' "$SERVICE_TAG"
  exit 0
fi
printf '  \033[31mACQUIRE GATE FAILED\033[0m — %s check(s) failed on published %s\n' "$FAILS" "$SERVICE_TAG"
printf '  A published tag is IMMUTABLE: fix and cut a new patch tag; never re-point.\n'
exit 1
