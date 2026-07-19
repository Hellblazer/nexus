#!/usr/bin/env bash
# RDR-002 ez5.13 — MVV (minimum viable verification) container E2E for the
# ONE-command `nx guided-upgrade`. Runs INSIDE the container.
#
# Mirrors rehearse.sh but drives the SINGLE `nx guided-upgrade` entry point
# instead of the manual `nx init --service` + `nx migrate-to-service` sequence:
#
#   seed pre-RDR-160 install  (minilm-384 + sourceless note + voyage-named
#                              mislabel whose vectors are really 768-dim ONNX)
#   Phase 0 (nexus-itme7)     two BLOCKED sub-runs layered on the main seed:
#                              A) 5b9v0 collision pair -> TargetNameCollisionBlocked
#                              B) nonconformant + legacy-id -> ModelPreGateBlocked,
#                              then mandatory `nx migration --clear-state`
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
command -v initdb >/dev/null 2>&1 && bad "system PostgreSQL present — bare-machine posture violated (nexus-5qefg: the image must ship NO host PG so the signed-bundle acquisition path is exercised)" || ok "no system PostgreSQL (bundle must provide it)"
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

# ── Phase 0 (nexus-itme7): blocked pre-flight — the shipped guards must FIRE ─
# Two SEPARATE blocked sub-runs: the 5b9v0 collision guard (driver.py) raises
# BEFORE the sequencer pregate, so one guided-upgrade run emits exactly ONE of
# {TargetNameCollisionBlocked, ModelPreGateBlocked}. Both sub-runs layer their
# blocking shapes ON TOP of the main seed above — migrate_cmd's T2/catalog
# existence pre-check fires before any guard, so a blocking-only footprint
# (no memory.db yet) would die on the WRONG diagnostic (it5wo demonstration,
# 2026-07-12). All pinned substrings below are the demonstration's captured
# rendered bytes, not paraphrases.
#
# Sub-run A also owns the PG-bundle ACQUIRE assertion (nexus-5qefg, moved here
# from the success phase): A is the first guided-upgrade invocation, and the
# collision fires inside _run_migration (step 3) AFTER provisioning (step 2),
# so THIS run downloads + verifies the bundle. Later runs find the bundle on
# disk and print the (deliberately unmatched) "extracted on first run" marker
# instead — see the nexus-5qefg comment retained below.
say "Phase 0A — collision pair MUST block (TargetNameCollisionBlocked)"
# Shape (iv): an honest bge-768 collection plus a stale voyage-NAMED sibling
# whose stored vectors measure 768-dim — the measured-dim override (nexus-
# nb7hr) remaps the stale half onto the honest sibling's name = target-name
# collision. The shipped 5b9v0 guard blocks UNCONDITIONALLY (there is no
# benign-merge carve-out), so via guided-upgrade the pair is a Phase-0 BLOCK
# fixture — the design's original "benign merge, union count at the target"
# outcome is unreachable and deliberately NOT asserted anywhere.
if BLOCK_A="$(python /home/nexus/seed_legacy.py "$CHROMA_LOCAL" --blocking=collision --n "$SEED_N")"; then
  ok "seeded collision pair: $(printf '%s\n' "$BLOCK_A" | tail -1)"
else
  bad "collision-pair seed failed"; say "ABORT (Phase-0 precondition)"; exit 1
fi
GA_OUT="$(nx guided-upgrade --timeout 180 --yes 2>&1)"
GA_RC=$?
printf '%s\n' "$GA_OUT" | sed 's/^/       /'
[ "$GA_RC" -ne 0 ] \
  && ok "sub-run A exited non-zero ($GA_RC)" \
  || bad "sub-run A exited 0 — the collision guard did NOT fire (vacuous fixture)"
printf '%s' "$GA_OUT" | grep -q "target-name collision detected across" \
  && ok "collision diagnostic rendered" \
  || bad "collision diagnostic missing ('target-name collision detected across')"
printf '%s' "$GA_OUT" | grep -q "Run 'nx migration-audit'" \
  && ok "migration-audit remedy rendered" \
  || bad "migration-audit remedy missing (\"Run 'nx migration-audit'\")"
# nexus-5qefg acceptance: the bundle-acquisition path MUST have run — a fresh
# download+verify, not a pre-staged extract. A revert of the always-install
# wiring (nexus-yv5m4) turns this (and the whole provision) RED on this
# PG-less image. NOTE: init.py's sibling marker "extracted on first run"
# (pre-staged bundle found) is deliberately NOT matched — nothing in this
# image pre-stages a PG bundle, and pre-staging one (e.g. caching it the way
# bge-768 is baked in) would re-mask exactly the acquire path this gate
# exists to exercise. If this grep ever fails with the pre-staged marker in
# GA_OUT, someone added bundle caching to the image — remove it.
printf '%s' "$GA_OUT" | grep -q "Using bundled PostgreSQL (downloaded + verified)" \
  && ok "PG bundle acquired (downloaded + verified — the yv5m4 path)" \
  || bad "PG bundle acquisition marker missing (always-install path not exercised)"
# The collision is a PRE-WRITE ClickException (before begin_migration): no
# sentinel, and the command prints no clear-state recovery advice.
test ! -f "$HOME/.config/nexus/migration.state" \
  && ok "no migration sentinel after the collision block (pre-write)" \
  || bad "collision block left a migration sentinel (expected none — pre-write)"
REMOVE_A="$(python /home/nexus/seed_legacy.py "$CHROMA_LOCAL" --remove-blocking=collision | tail -1)"
if printf '%s' "$REMOVE_A" | grep -q "rehearsal-pair__bge-base-en-v15-768" \
   && printf '%s' "$REMOVE_A" | grep -q "rehearsal-pair__voyage-context-3"; then
  ok "collision pair removed: $REMOVE_A"
else
  bad "collision-pair removal incomplete: $REMOVE_A"
  say "ABORT (a lingering pair would re-block the success run)"; exit 1
fi

say "Phase 0B — nonconformant + legacy-id shapes MUST block (ModelPreGateBlocked)"
# Shapes (i)+(ii): (i) a token-less 2-segment name (32-char ids, dim!=768 so
# the measured-dim override cannot rescue it); (ii) a supported-model NAME
# holding pre-RDR-108 16-char chunk ids (the GH #1390 canon-chat shape). The
# sequencer pregate joins BOTH per-collection reasons under ONE
# "migration blocked: N collection(s)" message — both greps hit the SAME
# captured output.
if BLOCK_B="$(python /home/nexus/seed_legacy.py "$CHROMA_LOCAL" --blocking=pregate --n "$SEED_N")"; then
  ok "seeded pregate shapes: $(printf '%s\n' "$BLOCK_B" | tail -1)"
else
  bad "pregate seed failed"; say "ABORT (Phase-0 precondition)"; exit 1
fi
GB_OUT="$(nx guided-upgrade --timeout 180 --yes 2>&1)"
GB_RC=$?
printf '%s\n' "$GB_OUT" | sed 's/^/       /'
[ "$GB_RC" -ne 0 ] \
  && ok "sub-run B exited non-zero ($GB_RC)" \
  || bad "sub-run B exited 0 — the model pregate did NOT fire (vacuous fixture)"
printf '%s' "$GB_OUT" | grep -q "not four-segment conformant" \
  && ok "shape (i) nonconformant-name diagnostic rendered" \
  || bad "shape (i) diagnostic missing ('not four-segment conformant')"
printf '%s' "$GB_OUT" | grep -q "holds legacy chunk ids" \
  && ok "shape (ii) legacy-id diagnostic rendered" \
  || bad "shape (ii) diagnostic missing ('holds legacy chunk ids')"
printf '%s' "$GB_OUT" | grep -q "(GH #1390)" \
  && ok "shape (ii) GH #1390 pointer rendered" \
  || bad "shape (ii) GH #1390 pointer missing"
# The pregate fires AFTER begin_migration (sequencer: begin → quiesce →
# pregate → T2 → T3), so it DEFINITIVELY leaves the migrated-failed sentinel
# — and blocks before the T2 leg ships anything.
test -f "$HOME/.config/nexus/migration.state" \
  && ok "pregate block left the migrated-failed sentinel" \
  || bad "no migration sentinel after the pregate block (expected migrated-failed)"
# MANDATORY remediation: a lingering migrated-failed sentinel poisons every
# later assertion (reads degrade LOUD; the success run would be judged against
# a dirty state). Plain clear-state suffices for migrated-failed (no --force);
# it is filesystem-only (<config>/migration.state), no service env needed.
CLEAR_OUT="$(nx migration --clear-state 2>&1)"
if printf '%s' "$CLEAR_OUT" | grep -q "Cleared migration sentinel"; then
  ok "migration sentinel cleared: $CLEAR_OUT"
else
  bad "clear-state did not confirm ('Cleared migration sentinel' missing): $CLEAR_OUT"
  say "ABORT (a poisoned sentinel makes every later assertion misleading)"; exit 1
fi
REMOVE_B="$(python /home/nexus/seed_legacy.py "$CHROMA_LOCAL" --remove-blocking=pregate | tail -1)"
if printf '%s' "$REMOVE_B" | grep -q "legacybare" \
   && printf '%s' "$REMOVE_B" | grep -q "rehearsal-shortid"; then
  ok "pregate shapes removed: $REMOVE_B"
else
  bad "pregate-shape removal incomplete: $REMOVE_B"
  say "ABORT (lingering pregate shapes would re-block the success run)"; exit 1
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
# nexus-5qefg PG-bundle ACQUIRE marker: asserted in Phase 0A (the first,
# provisioning run) — NOT here. This already-provisioned rerun finds the
# bundle on disk and prints "extracted on first run" instead, so matching
# the acquire marker here would be permanently red (nexus-itme7).
# "Service verified" DOES reprint unconditionally on a rerun
# (guided_upgrade_cmd.py) — keep asserting it here.
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
