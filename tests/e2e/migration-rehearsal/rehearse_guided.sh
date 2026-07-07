#!/usr/bin/env bash
# RDR-002 ez5.13 — MVV (minimum viable verification) container E2E for the
# ONE-command `nx guided-upgrade`. Runs INSIDE the container.
#
# Mirrors rehearse.sh but drives the SINGLE `nx guided-upgrade` entry point
# instead of the manual `nx init --service` + `nx migrate-to-service` sequence:
#
#   seed pre-RDR-160 install  (>=1 minilm-384 collection + 1 sourceless note)
#   nx guided-upgrade         (detect -> provision -> health-gate -> version-pin
#                              -> migrate -> validate -> unlock, all in one shot)
#   assert "Migration VERIFIED and unlocked" + cross-model parity + Chroma intact
#
# The native binary in this image is built with a STAMPED release.properties
# (release_version >= 0.1.5, done by run.sh --guided) so the version-pin PASSES;
# an unstamped binary reports release_version=null and guided-upgrade correctly
# fail-closes (which is NOT what this success-path MVV exercises).
set -uo pipefail

# nexus-id750 (GH #1381): seed the legacy Chroma at the PRODUCT's default
# location ($XDG_DATA_HOME -> ~/.local/share/nexus/chroma) and invoke
# guided-upgrade BARE — no --local-path. The MVV must exercise the exact
# command a real installed user runs; passing --local-path here masked a
# wrong-default-path bug for four releases (the detector probed
# <config>/chroma, which no install ever wrote).
CHROMA_LOCAL="${CHROMA_LOCAL:-/home/nexus/.local/share/nexus/chroma}"
SEED_N="${SEED_N:-12}"
FAILS=0

say()  { printf '\n\033[1m== %s ==\033[0m\n' "$*"; }
ok()   { printf '  \033[32mPASS\033[0m %s\n' "$*"; }
bad()  { printf '  \033[31mFAIL\033[0m %s\n' "$*"; FAILS=$((FAILS+1)); }
note() { printf '       %s\n' "$*"; }

# ── Prelude: position the native binary at the well-known location ───────────
say "Prelude — native binary + tooling"
SVC_NATIVE_DIR="/opt/nexus-service-native"
SVC_WELL_KNOWN_DIR="$HOME/.config/nexus/service"

nx --version >/dev/null 2>&1 && ok "nx installed ($(nx --version 2>&1))" || bad "nx --version failed"
command -v initdb >/dev/null 2>&1 && ok "PG16 binaries on PATH ($(initdb --version))" || bad "initdb not found"
test -x "$SVC_NATIVE_DIR/nexus-service" && ok "native service binary present" || bad "native binary missing at $SVC_NATIVE_DIR"

note "positioning the native binary + libs at the well-known location…"
if mkdir -p "$SVC_WELL_KNOWN_DIR" \
   && cp "$SVC_NATIVE_DIR"/* "$SVC_WELL_KNOWN_DIR/" \
   && chmod +x "$SVC_WELL_KNOWN_DIR/nexus-service"; then
  ok "native binary positioned"
else
  bad "could not position native binary"; say "ABORT"; exit 1
fi

# Bound the native service heap (nexus-lz3f2 OOM lore — the supervisor inherits
# this and passes -Xmx to the binary). guided-upgrade provisions via the same
# ensure_storage_supervisor path, so the export must precede it.
export NX_SERVICE_MAX_HEAP="${NX_SERVICE_MAX_HEAP:-1g}"

# Catalog.init (in seed_legacy.py) runs `git init`; give git an identity.
git config --global user.email "rehearsal@nexus.local" >/dev/null 2>&1 || true
git config --global user.name  "nexus rehearsal"       >/dev/null 2>&1 || true

# ── Seed the pre-RDR-160 footprint (BEFORE any service exists) ───────────────
say "Seed — legacy Chroma + T2/catalog (pre-RDR-160 state)"
if SEED_RAW="$(python /home/nexus/seed_legacy.py "$CHROMA_LOCAL" --n "$SEED_N")"; then
  SEED_JSON="$(printf '%s\n' "$SEED_RAW" | tail -1)"
  ok "seeded legacy footprint: $SEED_JSON"
else
  bad "seed failed"; say "ABORT (seed is the precondition)"; exit 1
fi

# ── The ONE command: nx guided-upgrade ───────────────────────────────────────
say "nx guided-upgrade — detect → provision → health-gate → version-pin → migrate"
note "release_version pin: the binary was built with a stamped release.properties"
GU_OUT="$(nx guided-upgrade --timeout 180 --yes 2>&1)"   # BARE: default-path resolution under test (nexus-id750)
GU_RC=$?
printf '%s\n' "$GU_OUT" | sed 's/^/       /'

# Diagnostic dump if the one command did not succeed.
if [ "$GU_RC" != 0 ]; then
  bad "nx guided-upgrade exited $GU_RC"
  for lg in storage_service.log storage_service_native.log storage_service.crash.log; do
    f="$HOME/.config/nexus/logs/$lg"
    [ -f "$f" ] && { echo "         --- tail $lg ---"; tail -30 "$f" | sed 's/^/         /'; }
  done
else
  ok "nx guided-upgrade exited 0"
fi

# The MVV assertions on the single command's own output.
printf '%s' "$GU_OUT" | grep -q "Service verified" \
  && ok "service was provisioned + verified (healthy + version-pinned)" \
  || bad "no 'Service verified' line — provision/version-pin path did not complete"
printf '%s' "$GU_OUT" | grep -q "Migration VERIFIED and unlocked" \
  && ok "migration VERIFIED and unlocked (the MVV success signal)" \
  || bad "no 'Migration VERIFIED and unlocked' line"
printf '%s' "$GU_OUT" | grep -q "nx doctor" \
  && ok "post-migrate advisory emitted" || note "advisory line absent (non-fatal)"

# Service env for the parity probe (guided-upgrade self-loaded these in its own
# process; this shell needs them to talk to the now-running service).
export NX_STORAGE_BACKEND=service
# shellcheck disable=SC1091
[ -f /home/nexus/.config/nexus/pg_credentials ] && { set -a; . /home/nexus/.config/nexus/pg_credentials; set +a; }
unset NX_SERVICE_URL NX_SERVICE_PORT NX_SERVICE_HOST 2>/dev/null || true

# ── Cross-model parity: pgvector TARGET counts == seeded ─────────────────────
say "Parity — pgvector serves the migrated collections"
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
        target = cross.get(name, name)
        if target != name:
            try:
                stray = t3.count(name)
            except Exception:
                stray = 0
            if stray:
                print(f"       {name}: SOURCE name has {stray} in pgvector (should be 0)"); bad += 1
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

# Sourceless-note (RDR-162 P2) guarantee — the case that motivated the design.
# Two-part proof: (1) the parity block above asserts the NOTE's bge-768 TARGET
# (knowledge__rehearsal-note__bge-base-en-v15-768__v1) holds SEED_N vectors and
# the minilm SOURCE name holds 0 — the cross-model re-embed landed; (2) the
# topic_assignments.source_collection re-point is gated INDIRECTLY by the clean
# "VERIFIED and unlocked" above (verify_taxonomy_consistency blocks unlock unless
# every assignment resolves to a migrated collection). In service mode T2 is
# Postgres behind the HTTP store, so there is no direct SQL probe here — the
# migrate's own validation leg is the authoritative ref-remap check (same
# honest limitation as the manual rehearse.sh).
note "sourceless-note: target vectors asserted by parity; assignment re-point gated by the clean unlock"

# ── Source intact (copy-not-move) ────────────────────────────────────────────
say "Rollback safety — legacy Chroma untouched (copy-not-move)"
python - "$CHROMA_LOCAL" "$SEED_JSON" <<'PY' && ok "legacy Chroma still intact" || bad "legacy Chroma damaged"
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
  printf '\033[32mGUIDED-UPGRADE MVV PASSED\033[0m — one command: detect → provision → verify → migrate → unlock; source intact\n'
  exit 0
else
  printf '\033[31mGUIDED-UPGRADE MVV FAILED — %d check(s) failed\033[0m\n' "$FAILS"
  exit 1
fi
