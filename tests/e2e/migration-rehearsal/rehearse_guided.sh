#!/usr/bin/env bash
# RDR-002 ez5.13 + RDR-180 nexus-jxizy.10.10 — the --guided container gate.
# Runs INSIDE the container. Drives the SINGLE `nx guided-upgrade` entry point
# over the LAND-THEN-TRANSFORM contract (design of record: T2
# nexus_rdr/180-land-transform-design + -reconciliation) and asserts the NINE
# checklist items the hermetic suites structurally cannot prove (the P3
# checklist pinned on bead nexus-jxizy.10.10):
#
#   seed (--rdr180)      pre-RDR-160 install + the RDR-180 shapes: _SHORTID
#                        (16-char GH #1408 population) as a FULL CITIZEN,
#                        collapse pairs both directions, Item8 empty-text
#                        dispositions, 16-char-keyed pointer stores. The seed
#                        emits EXACT expected numbers; the gate asserts values.
#   Phase 0A             5b9v0 collision pair -> TargetNameCollisionBlocked
#                        (+ the nexus-5qefg PG-bundle ACQUIRE assert).
#   Phase 0B  (item 9)   nonconformant name + NO-TEXT shapes ->
#                        ModelPreGateBlocked. The RETIRED legacy-id width
#                        block must NOT fire (negative assert) — its old
#                        fixture now MIGRATES in the success phase.
#   Phase 0C  (item 8)   pre-land source census + disk preflight against the
#                        REAL SQLite sources + REAL bundled-PG datadir, both
#                        clean AND non-vacuous.
#   Phase 1   (items 1,2) lost-response pre-stage: land + promote _SHORTID
#                        via the REAL /v1/staging wire client, then let the
#                        full run converge over it (mid-run promote-failure /
#                        lost-response idempotency class). Promote envelope
#                        field names + exact counts asserted over real HTTP.
#   success   (item 9)   nx guided-upgrade end to end; collapse-aware parity.
#   Phase 3   (items 3,4,8) exact-number teeth over the live store: alias map,
#                        16-char citation via chash_alias, full client
#                        resolver, collapse reconciliation both directions,
#                        pointer-store cascade, manifest convergence, zero
#                        legacy residue (the engine's own census already
#                        gated FATALLY inside finalize).
#   Phase 4   (items 1,6,7) direct staging-API scenario: two-collection
#                        768 + 1024 land/promote/reconcile, LIVE embed_fill,
#                        finalize envelope field names, and ALL THREE Item8
#                        dispositions incl. orphan_policy=synthesize on an
#                        idempotent re-finalize.
#   Phase 5   (item 5)   MUTATION FALSIFICATION (falsify-by-deleting): with
#                        the alias-build's EFFECT removed (staging-built
#                        chash_alias rows deleted — the world where the
#                        engine's alias-build statement never ran), the
#                        citation + alias-count asserts MUST FAIL, proving
#                        they are load-bearing.
#
# The native binary in this image is built with a STAMPED release.properties
# (release_version >= the guided-upgrade floor, done by run.sh --guided) so
# the version-pin PASSES; an unstamped binary reports release_version=null
# and guided-upgrade correctly fail-closes.
set -uo pipefail

# nexus-id750 (GH #1381): seed the legacy Chroma at the PRODUCT's default
# location ($XDG_DATA_HOME -> ~/.local/share/nexus/chroma) and invoke
# guided-upgrade BARE — no --local-path. The MVV must exercise the exact
# command a real installed user runs; passing --local-path here masked a
# wrong-default-path bug for four releases.
CHROMA_LOCAL="${CHROMA_LOCAL:-/home/nexus/.local/share/nexus/chroma}"
SEED_N="${SEED_N:-12}"
FAILS=0

say()  { printf '\n\033[1m== %s ==\033[0m\n' "$*"; }
ok()   { printf '  \033[32mPASS\033[0m %s\n' "$*"; }
bad()  { printf '  \033[31mFAIL\033[0m %s\n' "$*"; FAILS=$((FAILS+1)); }
note() { printf '       %s\n' "$*"; }

# Service-env python: the f2qvx endpoint/token resolution chain the REAL
# migration client uses. Subshell so the main shell's env stays pristine for
# the guided-upgrade invocations (which self-load their own env).
svc_py() (
  set -a
  # shellcheck disable=SC1091
  . "$HOME/.config/nexus/pg_credentials" 2>/dev/null || true
  set +a
  unset NX_SERVICE_URL NX_SERVICE_PORT NX_SERVICE_HOST 2>/dev/null || true
  NX_STORAGE_BACKEND=service python "$@"
)

# Superuser SQL over the bundled PG (trust auth for local TCP, initdb
# --username <os-user> — pg_provision.bootstrap_superuser). Harness
# measurement plumbing, same posture as rehearse_chash_window.sh's diag_sql:
# these are content-reading probes of the REAL store, not product surface.
PSQL_BIN=""
sql() (
  set -a
  # shellcheck disable=SC1091
  . "$HOME/.config/nexus/pg_credentials" 2>/dev/null || true
  set +a
  "$PSQL_BIN" -h 127.0.0.1 -p "${PG_PORT:?pg_credentials must define PG_PORT}" \
    -U "$(id -un)" -d nexus -tA -c "$1" 2>&1
)
expect_sql() {  # label, sql, want — EXACT equality (the exact-number teeth)
  local got
  got="$(sql "$2")"
  if [ "$got" = "$3" ]; then ok "$1 = $got"; else bad "$1: got '$got', want $3"; fi
}

# Seed-manifest field extraction (the seed emits the expected numbers).
jget() { printf '%s' "$SEED_JSON" | python -c "import json,sys; print(json.load(sys.stdin)$1)"; }

# The 16-char citation assert (item 3) — invoked in BOTH directions: expect
# "resolve" post-migration (Phase 3), expect FAILURE after the alias map is
# mutated away (Phase 5 falsification: this same assert must go red).
citation16() {  # $1 = expect: resolve
  svc_py - "$N16" "$CANON64" "$1" <<'PY'
import sys
from nexus.db.t2.http_chash_index import HttpChashIndex
legacy, canon, expect = sys.argv[1], sys.argv[2], sys.argv[3]
rows = HttpChashIndex().lookup(legacy)
resolved = bool(rows) and all(r.get("chash") == canon for r in rows)
print(f"       lookup({legacy}) -> {len(rows)} row(s), resolved_to_canonical={resolved}")
sys.exit(0 if resolved else 1)
PY
}

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

# Min-rows floor (nexus-jxizy.10.10 original teeth): below this the
# exact-number assertions grade a toy store and the collapse/disposition
# arithmetic loses its discriminating power.
if [ "$SEED_N" -ge 12 ] 2>/dev/null; then
  ok "SEED_N=$SEED_N meets the min-rows floor (12)"
else
  bad "SEED_N=$SEED_N below the min-rows floor (12) — the exact-number teeth would be vacuous"
  say "ABORT"; exit 1
fi

# ── Seed the pre-RDR-160 footprint + RDR-180 shapes (BEFORE any service) ─────
say "Seed — legacy Chroma + T2/catalog (pre-RDR-160 state, --rdr180 shapes)"
if SEED_RAW="$(python /home/nexus/seed_legacy.py "$CHROMA_LOCAL" --rdr180 --n "$SEED_N")"; then
  SEED_JSON="$(printf '%s\n' "$SEED_RAW" | tail -1)"
  ok "seeded legacy footprint (+rdr180 shapes)"
else
  bad "seed failed"; say "ABORT (seed is the precondition)"; exit 1
fi

# The exact expected numbers, straight from the seed manifest.
N16="$(jget "['rdr180']['citation16']")"
CANON64="$(jget "['rdr180']['citation_canonical']")"
NOTE_CANON="$(jget "['rdr180']['note_doc_canonical']")"
STAGED_TOTAL="$(jget "['rdr180']['staged_total']")"
ALIAS_TOTAL="$(jget "['rdr180']['alias_total']")"
MANIFEST_TOTAL="$(jget "['rdr180']['manifest_total']")"
CHASHIDX_ROWS="$(jget "['rdr180']['chash_index_rows']")"
FRECENCY_ROWS="$(jget "['rdr180']['frecency_rows']")"
RELEVANCE_ROWS="$(jget "['rdr180']['relevance_rows']")"
P16="$(jget "['rdr180']['pair']['p16']")"
P32="$(jget "['rdr180']['pair']['p32']")"
PAIR_CANON="$(jget "['rdr180']['pair']['canonical']")"
PAIR_TEXT="$(jget "['rdr180']['pair']['text']")"
CROSS_REF="$(jget "['rdr180']['cross']['ref']")"
CROSS_CANON="$(jget "['rdr180']['cross']['canonical']")"
T_CROSS_A="$(jget "['rdr180']['cross']['targets'][0]")"
T_CROSS_B="$(jget "['rdr180']['cross']['targets'][1]")"
ORPHAN_REF="$(jget "['rdr180']['orphan_ref']")"
REF_ONLY="$(jget "['rdr180']['ref_only_ref']")"
REF_ONLY_CANON="$(jget "['rdr180']['ref_only_canonical']")"
SHORTID="$(jget "['rdr180']['shortid']")"
T_MINILM="$(jget "['cross_model']['knowledge__rehearsal__minilm-l6-v2-384__v1']")"
T_NOTE="$(jget "['cross_model']['knowledge__rehearsal-note__minilm-l6-v2-384__v1']")"
T_MISLABEL="$(jget "['cross_model']['knowledge__rehearsal-mislabel__voyage-context-3__v1']")"
note "expected: staged=$STAGED_TOTAL aliases=$ALIAS_TOTAL manifest=$MANIFEST_TOTAL citation16=$N16"
if [ "$STAGED_TOTAL" -ge 50 ] 2>/dev/null; then
  ok "staged-total floor met ($STAGED_TOTAL >= 50)"
else
  bad "staged total $STAGED_TOTAL below floor 50 (vacuity guard)"; say "ABORT"; exit 1
fi

# ── Phase 0A (nexus-itme7): collision pair MUST block ────────────────────────
# The 5b9v0 collision guard (driver.py) raises BEFORE the sequencer pregate,
# so one guided-upgrade run emits exactly ONE of {TargetNameCollisionBlocked,
# ModelPreGateBlocked}. Both blocked sub-runs layer their shapes ON TOP of the
# main seed — migrate_cmd's T2/catalog existence pre-check fires before any
# guard, so a blocking-only footprint would die on the WRONG diagnostic.
#
# Sub-run A also owns the PG-bundle ACQUIRE assertion (nexus-5qefg): A is the
# first guided-upgrade invocation, and the collision fires inside
# _run_migration (step 3) AFTER provisioning (step 2), so THIS run downloads
# + verifies the bundle.
say "Phase 0A — collision pair MUST block (TargetNameCollisionBlocked)"
# Shape (iv): an honest bge-768 collection plus a stale voyage-NAMED sibling
# whose stored vectors measure 768-dim — the measured-dim override (nexus-
# nb7hr) remaps the stale half onto the honest sibling's name = target-name
# collision. The shipped 5b9v0 guard blocks UNCONDITIONALLY.
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
# download+verify, not a pre-staged extract. NOTE: init.py's sibling marker
# "extracted on first run" is deliberately NOT matched — nothing in this
# image pre-stages a PG bundle; pre-staging one would re-mask the acquire
# path this gate exists to exercise.
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

# ── Phase 0B (item 9, rewired): nonconformant + NO-TEXT MUST block ───────────
say "Phase 0B — nonconformant + no-text shapes MUST block (ModelPreGateBlocked)"
# Shapes (i)+(iii): (i) a token-less 2-segment name (32-char ids, dim!=768 so
# the measured-dim override cannot rescue it); (iii) a conformant, supported-
# model name whose sampled chunks carry NO TEXT AT ALL — the RDR-180 Q4
# residual honest block (nothing to rehash from server-side, P2.3).
#
# The RETIRED shape (ii) — a supported-model name holding pre-RDR-108 16-char
# chunk ids (GH #1390 canon-chat) — must NOT block any more (nexus-jxizy.10.8
# pregate evolution): land-then-transform rehashes chunk_text server-side, so
# that population MIGRATES. Its collections now ride the --rdr180 MAIN seed
# and are POSITIVELY asserted in the success phase (audit G1 inversion).
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
printf '%s' "$GB_OUT" | grep -q "no chunk text in the sampled rows" \
  && ok "shape (iii) no-text diagnostic rendered (the RDR-180 residual block)" \
  || bad "shape (iii) diagnostic missing ('no chunk text in the sampled rows')"
# THE INVERSION TRIPWIRE: the retired legacy-id width block must NOT fire.
# If this grep ever matches again, the RDR-180 pregate evolution regressed
# and the GH #1408 population is being refused instead of migrated.
printf '%s' "$GB_OUT" | grep -q "holds legacy chunk ids" \
  && bad "RETIRED legacy-id width block fired ('holds legacy chunk ids') — pregate evolution regressed; the GH #1408 population must MIGRATE" \
  || ok "retired legacy-id width block did not fire (inversion holds)"
# The pregate fires AFTER begin_migration (sequencer: begin → quiesce →
# pregate → …), so it DEFINITIVELY leaves the migrated-failed sentinel.
test -f "$HOME/.config/nexus/migration.state" \
  && ok "pregate block left the migrated-failed sentinel" \
  || bad "no migration sentinel after the pregate block (expected migrated-failed)"
# MANDATORY remediation: a lingering migrated-failed sentinel poisons every
# later assertion. Plain clear-state suffices for migrated-failed.
CLEAR_OUT="$(nx migration --clear-state 2>&1)"
if printf '%s' "$CLEAR_OUT" | grep -q "Cleared migration sentinel"; then
  ok "migration sentinel cleared: $CLEAR_OUT"
else
  bad "clear-state did not confirm ('Cleared migration sentinel' missing): $CLEAR_OUT"
  say "ABORT (a poisoned sentinel makes every later assertion misleading)"; exit 1
fi
REMOVE_B="$(python /home/nexus/seed_legacy.py "$CHROMA_LOCAL" --remove-blocking=pregate | tail -1)"
if printf '%s' "$REMOVE_B" | grep -q "legacybare" \
   && printf '%s' "$REMOVE_B" | grep -q "rehearsal-notext"; then
  ok "pregate shapes removed: $REMOVE_B"
else
  bad "pregate-shape removal incomplete: $REMOVE_B"
  say "ABORT (lingering pregate shapes would re-block the success run)"; exit 1
fi

# ── Phase 0C (item 8): census + disk preflight against the REAL substrates ───
say "Phase 0C — pre-land census + disk preflight (real SQLite, real PG datadir)"
if python - "$CHROMA_LOCAL" <<'PY'; then ok "source census + disk preflight clean AND non-vacuous"; else bad "census/preflight probe failed"; fi
import sys, sqlite3
from pathlib import Path
from nexus.migration.staging_land import source_census
from nexus.migration.pregate import (
    StagingDiskPreflightBlocked, assert_disk_headroom, estimate_staging_source_bytes,
)
from nexus.migration.driver import _resolve_pg_path

cfg = Path.home() / ".config" / "nexus"
mem, catdb = cfg / "memory.db", cfg / "catalog" / ".catalog.db"
conns = {
    "catalog": sqlite3.connect(f"file:{catdb}?mode=ro", uri=True),
    "memory": sqlite3.connect(f"file:{mem}?mode=ro", uri=True),
}
report = source_census(conns)  # raises StagingCensusError on any unclaimed column
triples = {(f.db, f.table, f.column) for f in report.findings}
required = {
    ("catalog", "document_chunks", "chash"),
    ("memory", "chash_index", "chash"),
    ("memory", "frecency", "chunk_id"),
    ("memory", "relevance_log", "chunk_id"),
}
missing = required - triples
assert not missing, f"census failed to rediscover seeded inventory: {missing} (found: {sorted(triples)})"
print(f"       census clean; rediscovered {len(report.findings)} chash-bearing column(s) incl. all {len(required)} seeded pointer stores")

pg = _resolve_pg_path()
assert pg.name == "postgres" and pg.is_dir(), (
    f"_resolve_pg_path proxy is NOT the real bundled PG datadir: {pg}")
est = estimate_staging_source_bytes((mem, catdb), chroma_dir=Path(sys.argv[1]))
assert est > 0, "estimate is zero — the sources were not measured"
assert_disk_headroom(estimated_bytes=est, pg_path=pg)  # must PASS on the real disk
try:
    assert_disk_headroom(estimated_bytes=1 << 62, pg_path=pg)
    raise AssertionError("disk preflight is VACUOUS — an absurd estimate did not block")
except StagingDiskPreflightBlocked:
    pass
print(f"       disk preflight: PASS at estimated {est:,} bytes on {pg}; absurd estimate blocks (non-vacuous)")
PY

# ── Phase 1 (items 1+2): lost-response pre-stage over the REAL wire ──────────
say "Phase 1 — lost-response pre-stage: land + promote _SHORTID, then converge"
# The mid-run promote-failure class: a prior run promoted ONE collection and
# died before the client recorded anything (lost response). Simulated
# deterministically: land + promote _SHORTID via the REAL HttpStagingStore
# (the exact wire client the driver uses), then let the full guided run
# re-land and re-promote over it. Convergence = the Phase-3 exact final
# counts; a false C1 409 on the resume would fail the success run loud.
# Doubles as the item-1 wire-contract proof for the PROMOTE envelope: field
# names + exact counts over real HTTP+JSON against the real engine.
if svc_py - "$SEED_JSON" "$CHROMA_LOCAL" <<'PY'; then ok "pre-stage landed + promoted with exact envelope (lost-response fixture armed)"; else bad "pre-stage land/promote failed"; fi
import json, sys
import chromadb
from nexus.migration.staging_land import HttpStagingStore, chunk_rows

manifest = json.loads(sys.argv[1])
r = manifest["rdr180"]
shortid = r["shortid"]
col = chromadb.PersistentClient(path=sys.argv[2]).get_collection(shortid)
store = HttpStagingStore()
model = shortid.split("__")[2]
landed = 0
for batch in chunk_rows(col, target_name=shortid, target_model=model,
                        target_dim=768, source_model=model):
    landed += store.load("chunks", batch)
assert landed == r["shortid_staged"], f"landed {landed}, want {r['shortid_staged']}"
env = store.promote(shortid)
print(f"       promote envelope: {json.dumps(env)}")
for field in ("staged_content", "promoted", "alias_rows", "chash_index_promoted"):
    assert field in env, f"promote envelope missing field {field!r} (wire contract drift)"
assert env["staged_content"] == r["shortid_staged"], env
assert env["promoted"] == r["shortid_promoted"], (
    f"promoted={env['promoted']}, want {r['shortid_promoted']} "
    "(the same-collection pair must collapse via DISTINCT ON + M1 tiebreak)")
assert env["alias_rows"] == r["shortid_staged"], (
    f"alias_rows={env['alias_rows']}, want {r['shortid_staged']} "
    "(every 16-char ref is genuinely legacy -> one alias fact each)")
PY

# ── The ONE command: nx guided-upgrade ───────────────────────────────────────
say "nx guided-upgrade — detect → provision → health-gate → version-pin → land-then-transform"
note "release_version pin: the binary was built with a stamped release.properties"
GU_OUT="$(nx guided-upgrade --timeout 180 --yes 2>&1)"   # BARE: default-path resolution under test (nexus-id750)
GU_RC=$?
printf '%s\n' "$GU_OUT" | sed 's/^/       /'

if [ "$GU_RC" != 0 ]; then
  bad "nx guided-upgrade exited $GU_RC"
  for lg in storage_service.log storage_service_native.log storage_service.crash.log; do
    f="$HOME/.config/nexus/logs/$lg"
    [ -f "$f" ] && { echo "         --- tail $lg ---"; tail -30 "$f" | sed 's/^/         /'; }
  done
else
  ok "nx guided-upgrade exited 0"
fi

# nexus-5qefg PG-bundle ACQUIRE marker: asserted in Phase 0A (the first,
# provisioning run) — NOT here. This already-provisioned rerun finds the
# bundle on disk and prints "extracted on first run" instead.
printf '%s' "$GU_OUT" | grep -q "Service verified" \
  && ok "service was provisioned + verified (healthy + version-pinned)" \
  || bad "no 'Service verified' line — provision/version-pin path did not complete"
printf '%s' "$GU_OUT" | grep -q "Migration VERIFIED and unlocked" \
  && ok "migration VERIFIED and unlocked (the MVV success signal)" \
  || bad "no 'Migration VERIFIED and unlocked' line"
printf '%s' "$GU_OUT" | grep -q "nx doctor" \
  && ok "post-migrate advisory emitted" || note "advisory line absent (non-fatal)"
# The clean unlock is ITSELF a disposition proof (reviewer-p2 CRITICAL
# arithmetic): verify raises unless staged == sum(staged_content) +
# (reference_only_resolved + orphans_dropped + orphans_synthesized) EXACTLY —
# with this seed that forces reference_only=1 + dropped=1. The Phase-3 alias
# asserts below then prove WHICH was which.

# ── Cross-model parity: pgvector TARGET counts == expected (collapse-aware) ──
say "Parity — pgvector serves the migrated collections (collapse-aware)"
python - "$SEED_JSON" <<'PY' && ok "cross-model parity validated" || bad "parity mismatch / unverified"
import json, sys, os
os.environ.setdefault("NX_STORAGE_BACKEND", "service")
m = json.loads(sys.argv[1])
seeded = m.get("collections", {})
cross = m.get("cross_model", {})
expected_content = m.get("rdr180", {}).get("expected_content", {})
if not seeded:
    print("       no seed manifest — cannot validate"); sys.exit(1)
try:
    from nexus.db import make_t3
    t3 = make_t3()
    bad = 0
    for name, raw in seeded.items():
        target = cross.get(name, name)
        # RDR-180: the service serves CONTENT rows — identical-text pairs
        # collapse and empty-text rows never promote, so the expected
        # service count is the seed's expected_content, not the raw count.
        want = expected_content.get(target, raw)
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
        print(f"       {name} -> {target}: service={got} expected={want} (raw seeded={raw}) [{flag}]")
        if got != want:
            bad += 1
    sys.exit(1 if bad else 0)
except Exception as e:
    print(f"       validation harness error: {e}"); sys.exit(1)
PY

# Sourceless-note (RDR-162 P2) guarantee: the parity block above asserts the
# NOTE's bge-768 TARGET holds its vectors and the minilm SOURCE name holds 0;
# the assignment re-point is asserted EXACTLY in Phase 3 (doc_id == the
# canonical 64-hex — stronger than the old indirect unlock gating).
note "sourceless-note: target vectors asserted by parity; assignment re-point asserted exactly in Phase 3"

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

# ── Phase 3 (items 3,4,8): exact-number teeth over the live store ────────────
say "Phase 3 — exact-number teeth (alias map, collapse, cascade, census)"
# tail -1: discover_pg_binaries logs a structlog line before the path prints.
PSQL_BIN="$(python -c 'from nexus.db.pg_provision import discover_pg_binaries; print(discover_pg_binaries().psql)' 2>/dev/null | tail -1)"
if [ -x "$PSQL_BIN" ]; then
  ok "bundled psql resolved ($PSQL_BIN)"
else
  bad "cannot resolve the bundled psql (got: '$PSQL_BIN')"; say "ABORT (no SQL teeth)"; exit 1
fi

# Staging fully cleared (the clean run's TRUNCATE-equivalent; also proves the
# unresolved orphan manifest row was retained-then-cleared, never promoted).
expect_sql "staging rows after the clean run" \
  "SELECT (SELECT count(*) FROM staging.chunks) + (SELECT count(*) FROM staging.document_chunks) + (SELECT count(*) FROM staging.chash_index) + (SELECT count(*) FROM staging.topic_assignments) + (SELECT count(*) FROM staging.frecency) + (SELECT count(*) FROM staging.relevance_log) + (SELECT count(*) FROM staging.document_aspects) + (SELECT count(*) FROM staging.aspect_extraction_queue)" \
  "0"

# Per-target content rows (collapse- and disposition-exact).
expect_sql "content rows: minilm target" \
  "SELECT count(*) FROM nexus.chunks_768 WHERE collection='$T_MINILM' AND chunk_text <> ''" "$((SEED_N+1))"
expect_sql "content rows: note target" \
  "SELECT count(*) FROM nexus.chunks_768 WHERE collection='$T_NOTE' AND chunk_text <> ''" "$SEED_N"
expect_sql "content rows: mislabel target" \
  "SELECT count(*) FROM nexus.chunks_768 WHERE collection='$T_MISLABEL' AND chunk_text <> ''" "$((SEED_N+1))"
expect_sql "content rows: shortid (GH #1408 population MIGRATED)" \
  "SELECT count(*) FROM nexus.chunks_768 WHERE collection='$SHORTID' AND chunk_text <> ''" "$((SEED_N+1))"
expect_sql "no empty-text row promoted anywhere" \
  "SELECT count(*) FROM nexus.chunks_768 WHERE chunk_text = ''" "0"

# The alias map, exactly (4n+3: every distinct legacy content ref, the shared
# cross-collection ref counted ONCE).
expect_sql "chash_alias rows (staging-built)" \
  "SELECT count(*) FROM nexus.chash_alias WHERE source LIKE 'staging:%'" "$ALIAS_TOTAL"
expect_sql "chash_alias new_chash all 32-byte" \
  "SELECT count(*) FROM nexus.chash_alias WHERE octet_length(new_chash) <> 32" "0"

# Item 3 — GH #1408: the REAL 16-char legacy id resolves via chash_alias over
# the live wire (the engine's resolveLegacyRef read seam).
if citation16 resolve; then
  ok "16-char citation $N16 resolves to its canonical via chash_alias (item 3)"
else
  bad "16-char citation did NOT resolve post-promote (item 3)"
fi
# Item 3, full client path: resolve_chash_globally over the pair's 32-char
# ref — alias-chains engine-side, then serves the chunk text from pgvector.
if svc_py - "$P32" "$PAIR_CANON" "$PAIR_TEXT" <<'PY'; then ok "full client resolver serves the 32-char legacy citation end-to-end"; else bad "client resolver failed on the 32-char legacy citation"; fi
import sys
from nexus.catalog.catalog_spans import resolve_chash_globally
from nexus.db import make_t3
from nexus.db.t2.http_chash_index import HttpChashIndex
legacy32, canon, want_text = sys.argv[1], sys.argv[2], sys.argv[3]
ref = resolve_chash_globally(f"chash:{legacy32}", make_t3(), HttpChashIndex())
assert ref is not None, "legacy 32-hex citation did not resolve (dangling)"
assert ref["chash"] == canon, f"resolved chash {ref['chash']} != canonical {canon}"
assert ref["chunk_text"] == want_text, f"chunk_text mismatch: {ref['chunk_text']!r}"
print(f"       chash:{legacy32} -> {canon[:16]}… text={ref['chunk_text']!r}")
PY

# Item 4 — identical-text collapse, BOTH directions.
expect_sql "same-collection pair collapsed to ONE content row" \
  "SELECT count(*) FROM nexus.chunks_768 WHERE collection='$SHORTID' AND chash = decode('$PAIR_CANON','hex')" "1"
expect_sql "same-collection pair: BOTH era refs alias to the one canonical" \
  "SELECT count(*) FROM nexus.chash_alias WHERE old_ref IN ('$P16','$P32') AND new_chash = decode('$PAIR_CANON','hex')" "2"
expect_sql "cross-collection twin: content under target A" \
  "SELECT count(*) FROM nexus.chunks_768 WHERE collection='$T_CROSS_A' AND chash = decode('$CROSS_CANON','hex')" "1"
expect_sql "cross-collection twin: content under target B" \
  "SELECT count(*) FROM nexus.chunks_768 WHERE collection='$T_CROSS_B' AND chash = decode('$CROSS_CANON','hex')" "1"
expect_sql "cross-collection SHARED ref: exactly ONE alias fact (C1 idempotent pass, no 409)" \
  "SELECT count(*) FROM nexus.chash_alias WHERE old_ref = '$CROSS_REF'" "1"

# Item 7 (guided half) — the drop policy's dispositions, post-hoc: the
# reference-only ref aliases (via _MINILM's content); the orphan never
# entered the alias map or nexus (and its staged row cleared above).
expect_sql "reference-only ref aliased to its content canonical" \
  "SELECT count(*) FROM nexus.chash_alias WHERE old_ref = '$REF_ONLY' AND new_chash = decode('$REF_ONLY_CANON','hex')" "1"
expect_sql "orphan ref never aliased" \
  "SELECT count(*) FROM nexus.chash_alias WHERE old_ref = '$ORPHAN_REF'" "0"

# The pointer-store cascade (16-char keys MUST have converged via the alias).
expect_sql "chash_index promoted (n+1 canonicals, pair deduped)" \
  "SELECT count(*) FROM nexus.chash_index WHERE physical_collection = '$SHORTID'" "$CHASHIDX_ROWS"
expect_sql "chash_index keys all 32-byte" \
  "SELECT count(*) FROM nexus.chash_index WHERE octet_length(chash) <> 32" "0"
expect_sql "frecency promoted (GREATEST-merge, pair converged)" \
  "SELECT count(*) FROM nexus.frecency" "$FRECENCY_ROWS"
expect_sql "frecency zero legacy-width residue" \
  "SELECT count(*) FROM nexus.frecency WHERE chunk_id !~ '^[0-9a-f]{64}$'" "0"
expect_sql "relevance_log promoted" \
  "SELECT count(*) FROM nexus.relevance_log" "$RELEVANCE_ROWS"
expect_sql "relevance_log zero legacy-width residue" \
  "SELECT count(*) FROM nexus.relevance_log WHERE chunk_id !~ '^[0-9a-f]{64}$'" "0"
expect_sql "sourceless-note topic assignment repointed to the canonical doc_id" \
  "SELECT count(*) FROM nexus.topic_assignments WHERE doc_id = '$NOTE_CANON'" "1"

# Manifest convergence: every resolvable staged pointer promoted (the
# orphan's entry stayed staged, resolvable-only), and ZERO dangling.
expect_sql "catalog manifest rows (3n+5: orphan entry never promoted)" \
  "SELECT count(*) FROM nexus.catalog_document_chunks" "$MANIFEST_TOTAL"
expect_sql "manifest dangling (chash with no content row in any dim)" \
  "SELECT count(*) FROM nexus.catalog_document_chunks m WHERE NOT EXISTS (SELECT 1 FROM nexus.chunks_384 c WHERE c.chash = m.chash) AND NOT EXISTS (SELECT 1 FROM nexus.chunks_768 c WHERE c.chash = m.chash) AND NOT EXISTS (SELECT 1 FROM nexus.chunks_1024 c WHERE c.chash = m.chash)" \
  "0"

# Item 8 (engine half) — census corroboration. The AUTHORITATIVE census ran
# FATALLY inside finalize (ChashCensus.scan + assertDiscoversKnownInventory —
# the clean unlock above is its proof, incl. non-vacuity); these spot-checks
# corroborate independently from the harness side.
for T in chunks_384 chunks_768 chunks_1024; do
  expect_sql "residual digest mismatch in $T" \
    "SELECT count(*) FROM nexus.$T WHERE chunk_text <> '' AND chash IS DISTINCT FROM sha256(convert_to(chunk_text,'UTF8'))" "0"
done

# ── Phase 4 (items 1,6,7): direct staging-API scenario (768 + 1024) ──────────
say "Phase 4 — direct /v1/staging scenario: dual-dim, live embed_fill, all three dispositions"
# Drives the wire surface the guided flow cannot: a 1024-dim voyage-named
# PASSTHROUGH collection beside a 768 embed-fill collection (item 6), the
# finalize envelope's full field set over real HTTP+JSON (item 1), and the
# two never-driven Item8 policies — a second finalize with
# orphan_policy=synthesize on the SAME staged orphan proves finalize is
# idempotent AND re-runnable with a different policy (item 7).
DIRECT_OUT="$(svc_py - <<'PY'
import hashlib, json, sys
from nexus.migration.staging_land import HttpStagingStore

sha = lambda t: hashlib.sha256(t.encode()).hexdigest()
D768 = "knowledge__direct-emb__bge-base-en-v15-768__v1"
D1024 = "knowledge__direct-pass__voyage-context-3__v1"
texts768 = [f"direct embed-fill chunk {i}" for i in range(3)]
texts1024 = [f"direct passthrough chunk {i}" for i in range(3)]
orphan_ref = sha("direct scenario orphan")[:32]

rows = []
for t in texts768:
    # NO embedding — reuse illegal, embed_fill must cover these LIVE.
    rows.append({"collection": D768, "dim": 768, "legacy_ref": sha(t)[:32],
                 "chunk_text": t, "model": "bge-base-en-v15-768", "chunk_meta": None})
for i, t in enumerate(texts1024):
    # 1024-dim vectors staged verbatim — the same-model passthrough shape.
    rows.append({"collection": D1024, "dim": 1024, "legacy_ref": sha(t)[:32],
                 "chunk_text": t, "model": "voyage-context-3", "chunk_meta": None,
                 "embedding": [float(i)] + [1.0] * 1023})
# The empty-text orphan: 768-dim WITH an embedding so orphan_policy=synthesize
# can materialize the deterministic surrogate.
rows.append({"collection": D768, "dim": 768, "legacy_ref": orphan_ref,
             "chunk_text": "", "model": "bge-base-en-v15-768", "chunk_meta": None,
             "embedding": [9.0] + [1.0] * 767})

store = HttpStagingStore()
landed = store.load("chunks", rows)
assert landed == 7, f"landed {landed}, want 7"

fill = store.embed_fill(D768)
assert fill.get("filled") == 3 and fill.get("remaining") == 0, f"embed_fill: {fill}"
print(f"       embed_fill LIVE: {json.dumps(fill)}")

p768 = store.promote(D768)
p1024 = store.promote(D1024)
for label, env in (("768", p768), ("1024", p1024)):
    for field in ("staged_content", "promoted"):
        assert field in env, f"promote[{label}] envelope missing {field!r}"
    assert env["staged_content"] == 3 and env["promoted"] == 3, f"promote[{label}]: {env}"
print(f"       promote 768: {json.dumps(p768)}")
print(f"       promote 1024: {json.dumps(p1024)}")

f1 = store.finalize()  # policy: drop (the guided default)
for field in ("reference_only_resolved", "orphans_dropped", "orphans_synthesized",
              "residual_mismatched", "dangling_manifest"):
    assert field in f1, f"finalize envelope missing {field!r} (wire contract drift)"
assert f1["orphans_dropped"] == 1 and f1["orphans_synthesized"] == 0 \
    and f1["reference_only_resolved"] == 0, f"finalize(drop): {f1}"
assert f1["residual_mismatched"] == 0 and f1["dangling_manifest"] == 0, f1
print(f"       finalize(drop): {json.dumps(f1)}")

f2 = store.finalize(orphan_policy="synthesize")  # idempotent re-finalize, new policy
assert f2["orphans_synthesized"] == 1 and f2["orphans_dropped"] == 0, f"finalize(synthesize): {f2}"
print(f"       finalize(synthesize): {json.dumps(f2)}")

store.clear()
counts = store.counts()
assert all(v == 0 for v in counts.values()), f"staging not cleared: {counts}"
print("       staging cleared after the scenario")
PY
)"
DIRECT_RC=$?
printf '%s\n' "$DIRECT_OUT"
if [ "$DIRECT_RC" = 0 ]; then
  ok "direct scenario: dual-dim promote + live embed_fill + all finalize fields + both orphan policies"
else
  bad "direct staging-API scenario failed"
fi
# The synthesize policy's observable product: the deterministic surrogate.
expect_sql "synthetic surrogate row (chash_origin=synthetic)" \
  "SELECT count(*) FROM nexus.chunks_768 WHERE collection='knowledge__direct-emb__bge-base-en-v15-768__v1' AND chunk_text = '' AND metadata->>'chash_origin' = 'synthetic'" "1"
expect_sql "synthetic alias recorded (source=staging:synthetic)" \
  "SELECT count(*) FROM nexus.chash_alias WHERE source = 'staging:synthetic'" "1"
expect_sql "1024 leg promoted (passthrough vectors, 32-byte keys)" \
  "SELECT count(*) FROM nexus.chunks_1024 WHERE collection='knowledge__direct-pass__voyage-context-3__v1' AND octet_length(chash) = 32" "3"

# ── Phase 5 (item 5): MUTATION FALSIFICATION — the alias asserts must bite ───
say "Phase 5 — mutation falsification: alias-build disabled ⇒ the asserts MUST fail"
# Falsify-by-deleting: the engine alias-build statement's entire EFFECT is
# the staging-built chash_alias rows. Deleting them reproduces the world
# where that statement never ran (a disabled alias-build / patched binary)
# without a second native build. The citation and alias-count asserts above
# must then FAIL — if either still passes, it was vacuous all along. This
# runs LAST: it deliberately corrupts the throwaway container store.
DELETED="$(sql "DELETE FROM nexus.chash_alias WHERE source LIKE 'staging:%'")"
ALIAS_LEFT="$(sql "SELECT count(*) FROM nexus.chash_alias WHERE source LIKE 'staging:%'")"
if [ "$ALIAS_LEFT" = "0" ]; then
  ok "alias-build effect removed ($DELETED; the no-alias world is real, not a silent no-op)"
else
  bad "alias deletion did not take ($ALIAS_LEFT rows remain) — the falsification below would be vacuous"
fi
if citation16 resolve >/dev/null 2>&1; then
  bad "FALSIFICATION FAILED: the 16-char citation STILL resolves with the alias map gone — the item-3 assert is vacuous"
else
  ok "16-char citation assert FAILED without aliases — the item-3 assert is load-bearing"
fi
ALIAS_NOW="$(sql "SELECT count(*) FROM nexus.chash_alias WHERE source LIKE 'staging:%'")"
if [ "$ALIAS_NOW" = "$ALIAS_TOTAL" ]; then
  bad "FALSIFICATION FAILED: alias-count assert would still pass ($ALIAS_NOW) — vacuous"
else
  ok "alias-count assert FAILS without aliases ($ALIAS_NOW != $ALIAS_TOTAL) — load-bearing"
fi

# ── Summary ──────────────────────────────────────────────────────────────────
say "RESULT"
if [ "$FAILS" -eq 0 ]; then
  printf '\033[32mGUIDED LAND-THEN-TRANSFORM GATE PASSED\033[0m — all nine checklist items + Phase-0 blocks + exact-number teeth; source intact\n'
  exit 0
else
  printf '\033[31mGUIDED LAND-THEN-TRANSFORM GATE FAILED — %d check(s) failed\033[0m\n' "$FAILS"
  exit 1
fi
