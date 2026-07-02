#!/usr/bin/env bash
# nexus-s3dd4.7 — HOLE-PUNCH e2e: verify-fill (delta) migration proven against
# a REAL fault-injected Postgres target. Runs INSIDE the container.
#
# Composition choice (documented per the bead): reuses the --cold journey's
# machinery (nx daemon service install-binary + nx init --service + nx
# guided-upgrade) through "Migration VERIFIED and unlocked" — the CHEAPEST leg
# to compose here, since it needs no native GraalVM build (~2-3m) the way
# --guided does, and it already cold-acquires the published engine-service
# release whose PG bundle this script punches holes in directly via psql.
# COLD_TAG (run.sh) defaults to engine-service-v0.1.18+, the tag that carries
# the ``/v1/telemetry/ids/probe`` membership-probe endpoint the telemetry
# delta-fill path (verify_fill_telemetry, P3b) requires.
#
# After the guided-upgrade leaves source (local T2/catalog SQLite) and target
# (the cold-acquired bundled PG) at genuine parity, this script:
#
#   1. Punches a small, REAL hole target-side via direct psql against the
#      journey's OWN bundled PG (fixture surgery, never prod — the harness
#      owns this Postgres cluster end to end): K rows deleted from
#      nexus.catalog_document_chunks (the 2026-07-01 incident table) and K
#      rows from nexus.hook_failures (a telemetry table).
#   2. Runs `nx storage migrate all --verify-fill` and asserts the report's
#      per-table filled counts equal exactly K (delta, not a full re-send),
#      and report["verification"] == "verified".
#   3. Runs `--verify-fill` a SECOND time and asserts ZERO fills — the
#      load-bearing anti-regression this bead exists for: incremental !=
#      full re-send, now proven end-to-end through the real engine and a
#      real Postgres, not a fake/ephemeral target (tests/migration/
#      test_verify_fill_regression.py already covers the fake-target case;
#      this is the composed, cloud-gated proof of the SAME claim).
#
# A zero-th baseline verify-fill run (Phase 2, before the punch) proves the
# no-op claim isn't vacuous — the freshly-migrated store must ALSO be a
# true no-op before we go trusting the post-punch no-op in Phase 6.
#
# Knobs (env-overridable): SEED_N (document_chunks rows, default 12 — one
# registered catalog doc), HOOK_N (synthetic hook_failures rows seeded
# pre-migrate, default 8), CHUNK_K / HOOK_K (rows punched out target-side per
# table, default 3 each — a SMALL hole, not the whole store).
set -uo pipefail

SERVICE_TAG="${NEXUS_SERVICE_TAG:?NEXUS_SERVICE_TAG must be set (e.g. engine-service-v0.1.18)}"
EXPECT_RELEASE_VERSION="${SERVICE_TAG#engine-service-v}"
CHROMA_LOCAL="${CHROMA_LOCAL:-/home/nexus/legacy-chroma}"
export SEED_N="${SEED_N:-12}"
export HOOK_N="${HOOK_N:-8}"
export CHUNK_K="${CHUNK_K:-3}"
export HOOK_K="${HOOK_K:-3}"
FAILS=0

say()  { printf '\n\033[1m== %s ==\033[0m\n' "$*"; }
ok()   { printf '  \033[32mPASS\033[0m %s\n' "$*"; }
bad()  { printf '  \033[31mFAIL\033[0m %s\n' "$*"; FAILS=$((FAILS+1)); }
note() { printf '       %s\n' "$*"; }

# ── Phase 0: cold-acquire + provision + serve (mirrors rehearse_cold.sh) ─────
say "Phase 0 — cold-acquire + provision + serve"
nx --version >/dev/null 2>&1 && ok "nx installed ($(nx --version 2>&1))" || bad "nx --version failed"
command -v initdb >/dev/null 2>&1 && bad "system PostgreSQL present — not a cold box" || ok "no system PostgreSQL (bundle must provide it)"
test ! -e "$HOME/.config/nexus/service/nexus-service" && ok "no native binary pre-staged" || bad "native binary already present — not cold"

export NX_SERVICE_MAX_HEAP="${NX_SERVICE_MAX_HEAP:-1g}"
git config --global user.email "holepunch@nexus.local" >/dev/null 2>&1 || true
git config --global user.name  "nexus hole-punch"      >/dev/null 2>&1 || true

say "Cold-acquire — nx daemon service install-binary $SERVICE_TAG"
if nx daemon service install-binary "$SERVICE_TAG" 2>&1 | sed 's/^/       /'; then
  ok "install-binary acquired + verified binary + PG bundle"
else
  bad "install-binary failed (cold-acquire of binary/bundle)"; say "ABORT"; exit 1
fi
test -x "$HOME/.config/nexus/service/nexus-service" \
  && ok "native binary now present (cold-acquired)" || bad "binary missing after install-binary"

say "Provision + serve — nx init --service (bundle-provisioned PG + bge ONNX)"
if nx init --service --embedder bge-768 --yes 2>&1 | sed 's/^/       /'; then
  ok "nx init --service (provision)"
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

RV="$(nx daemon service status --json 2>/dev/null | python -c 'import sys,json;print(json.load(sys.stdin).get("service_release_version") or "")' 2>/dev/null)"
if [ "$RV" = "$EXPECT_RELEASE_VERSION" ]; then
  ok "/version release_version=$RV matches the acquired tag (carries /v1/telemetry/ids/probe)"
else
  bad "release_version=$RV != expected $EXPECT_RELEASE_VERSION (wrong binary or stamp)"
fi

# psql lives ONLY in the cold-acquired PG bundle — Dockerfile.cold ships no
# postgresql-client package (the whole point of the cold box: the bundle IS
# the PG distribution, nothing pre-staged).
PSQL="$HOME/.config/nexus/pg-bundle/bundle/bin/psql"
test -x "$PSQL" && ok "psql resolved from the cold-acquired PG bundle" \
  || { bad "psql not found in the cold-acquired bundle at $PSQL"; say "ABORT"; exit 1; }

# ── Phase 1: seed legacy footprint + synthetic hook_failures + guided-upgrade ─
say "Phase 1 — seed legacy footprint (T3 chunks + catalog manifest, SEED_N=$SEED_N)"
if SEED_RAW="$(python /home/nexus/seed_legacy.py "$CHROMA_LOCAL" --n "$SEED_N")"; then
  SEED_JSON="$(printf '%s\n' "$SEED_RAW" | tail -1)"
  case "$SEED_JSON" in
    '{'*'}') ok "seeded legacy footprint: $SEED_JSON" ;;
    *) bad "seed produced no JSON manifest (got: '$SEED_JSON')"; say "ABORT"; exit 1 ;;
  esac
else
  bad "seed failed"; say "ABORT"; exit 1
fi

say "Seed $HOOK_N synthetic hook_failures rows (local T2 SQLite — the fill SOURCE)"
if HOOK_SEED_OUT="$(python - <<'PY'
import os

os.environ["NX_STORAGE_BACKEND"] = "sqlite"
from nexus.config import nexus_config_dir  # noqa: E402
from nexus.db.t2 import T2Database  # noqa: E402

cfg = nexus_config_dir()
db = T2Database(cfg / "memory.db", run_migrations=True)
n = int(os.environ["HOOK_N"])
for i in range(n):
    db.telemetry.record_hook_failure(
        doc_id=f"holepunch-{i}",
        collection="rehearsal",
        hook_name="e2e_verify_fill_probe",
        error="",
        chain="single",
    )
print(f"seeded {n} synthetic hook_failures rows (doc_id holepunch-0..{n - 1})")
PY
)"; then
  ok "$HOOK_SEED_OUT"
else
  bad "synthetic hook_failures seed failed"; say "ABORT"; exit 1
fi

say "nx guided-upgrade — full migration to a VERIFIED and unlocked state"
GU_OUT="$(nx guided-upgrade --local-path "$CHROMA_LOCAL" --timeout 180 --yes 2>&1)"
GU_RC=$?
printf '%s\n' "$GU_OUT" | sed 's/^/       /'
[ "$GU_RC" = 0 ] && ok "nx guided-upgrade exited 0" || { bad "nx guided-upgrade exited $GU_RC"; say "ABORT"; exit 1; }
printf '%s' "$GU_OUT" | grep -q "Migration VERIFIED and unlocked" \
  && ok "migration VERIFIED and unlocked (baseline: source == target, nothing to fill yet)" \
  || { bad "no 'Migration VERIFIED and unlocked' line"; say "ABORT"; exit 1; }

# ── Phase 2: baseline verify-fill — proves the no-op claim isn't vacuous ─────
say "Phase 2 — baseline verify-fill (expect ZERO fills on a freshly-migrated store)"
BASE_REPORT=/tmp/vf_report_baseline.json
BASE_OUT="$(nx storage migrate all --verify-fill --report "$BASE_REPORT" 2>&1)"
BASE_RC=$?
printf '%s\n' "$BASE_OUT" | sed 's/^/       /'
[ "$BASE_RC" = 0 ] || { bad "baseline verify-fill exited $BASE_RC"; say "ABORT"; exit 1; }

python - "$BASE_REPORT" <<'PY' && ok "baseline verify-fill total_filled == 0 (fresh migration is a true no-op)" || bad "baseline verify-fill was NOT a no-op — punch scenario invalid"
import json, sys

report = json.load(open(sys.argv[1]))
total = report.get("verify_fill", {}).get("total_filled", -1)
print(f"       baseline total_filled={total}")
sys.exit(0 if total == 0 else 1)
PY

# ── Phase 3: resolve the fill-target identities from the LOCAL source ────────
say "Phase 3 — resolve punch targets from the local catalog SQLite (source of truth)"
RESOLVE_OUT="$(python - <<'PY'
import sqlite3

from nexus.config import nexus_config_dir

conn = sqlite3.connect(str(nexus_config_dir() / "catalog" / ".catalog.db"))
rows = conn.execute(
    "SELECT doc_id, COUNT(*) c FROM document_chunks GROUP BY doc_id ORDER BY c DESC LIMIT 1"
).fetchall()
conn.close()
print(f"{rows[0][0]} {rows[0][1]}" if rows else "NONE 0")
PY
)"
DOC_ID="$(printf '%s' "$RESOLVE_OUT" | cut -d' ' -f1)"
N_CHUNKS="$(printf '%s' "$RESOLVE_OUT" | cut -d' ' -f2)"
[ -n "$DOC_ID" ] && [ "$DOC_ID" != "NONE" ] && [ "${N_CHUNKS:-0}" -ge "$CHUNK_K" ] 2>/dev/null \
  && ok "punch target resolved: doc_id=$DOC_ID n_chunks=$N_CHUNKS (need >= $CHUNK_K)" \
  || { bad "could not resolve a document_chunks punch target (doc_id='$DOC_ID' n_chunks='$N_CHUNKS')"; say "ABORT"; exit 1; }

# ── Phase 4: PUNCH THE HOLE — direct psql against the journey's own PG ───────
say "Phase 4 — punch the hole (delete $CHUNK_K document_chunks rows + $HOOK_K hook_failures rows, TARGET ONLY)"
ADMIN="${NX_DB_ADMIN_URL:-${NX_DB_URL:-}}"
hostport="$(printf '%s' "$ADMIN" | sed -E 's#^jdbc:postgresql://##; s#/.*$##')"
export PGHOST="${hostport%%:*}" PGPORT="${hostport##*:}"
export PGDATABASE="$(printf '%s' "$ADMIN" | sed -E 's#^[^/]*//[^/]+/##; s#\?.*$##')"
export PGUSER="${NX_DB_ADMIN_USER:-}" PGPASSWORD="${NX_DB_ADMIN_PASS:-}"
q() { "$PSQL" -v ON_ERROR_STOP=1 -tAqc "set nexus.tenant='default'; $1" 2>&1 | tr -d '[:space:]'; }

PRE_CHUNKS="$(q "select count(*) from nexus.catalog_document_chunks where tenant_id='default' and doc_id='$DOC_ID'")"
PRE_HOOKS="$(q "select count(*) from nexus.hook_failures where tenant_id='default' and doc_id like 'holepunch-%'")"
note "pre-punch target counts: document_chunks=$PRE_CHUNKS hook_failures=$PRE_HOOKS"
[ "$PRE_CHUNKS" = "$N_CHUNKS" ] && ok "target document_chunks count matches source ($N_CHUNKS) before the punch" \
  || bad "target document_chunks pre-punch count $PRE_CHUNKS != source $N_CHUNKS"
[ "$PRE_HOOKS" = "$HOOK_N" ] && ok "target hook_failures count matches source ($HOOK_N) before the punch" \
  || bad "target hook_failures pre-punch count $PRE_HOOKS != source $HOOK_N"

hook_ids=""
for ((i = 0; i < HOOK_K; i++)); do
  hook_ids="${hook_ids}${hook_ids:+,}'holepunch-$i'"
done

"$PSQL" -v ON_ERROR_STOP=1 -c \
  "set nexus.tenant='default'; delete from nexus.catalog_document_chunks where tenant_id='default' and doc_id='$DOC_ID' and position < $CHUNK_K;" \
  >/dev/null 2>&1
"$PSQL" -v ON_ERROR_STOP=1 -c \
  "set nexus.tenant='default'; delete from nexus.hook_failures where tenant_id='default' and doc_id in ($hook_ids);" \
  >/dev/null 2>&1

POST_CHUNKS="$(q "select count(*) from nexus.catalog_document_chunks where tenant_id='default' and doc_id='$DOC_ID'")"
POST_HOOKS="$(q "select count(*) from nexus.hook_failures where tenant_id='default' and doc_id like 'holepunch-%'")"
note "post-punch target counts: document_chunks=$POST_CHUNKS hook_failures=$POST_HOOKS"
[ "$POST_CHUNKS" = "$((N_CHUNKS - CHUNK_K))" ] && ok "punched exactly $CHUNK_K document_chunks rows target-side" \
  || bad "expected document_chunks=$((N_CHUNKS - CHUNK_K)) after punch, got $POST_CHUNKS"
[ "$POST_HOOKS" = "$((HOOK_N - HOOK_K))" ] && ok "punched exactly $HOOK_K hook_failures rows target-side" \
  || bad "expected hook_failures=$((HOOK_N - HOOK_K)) after punch, got $POST_HOOKS"

# ── Phase 5: verify-fill PASS 1 — delta fill, NOT a full re-send ─────────────
say "Phase 5 — verify-fill pass 1 (must fill EXACTLY the punched rows, nothing more)"
FILL_REPORT=/tmp/vf_report_fill.json
FILL_OUT="$(nx storage migrate all --verify-fill --report "$FILL_REPORT" 2>&1)"
FILL_RC=$?
printf '%s\n' "$FILL_OUT" | sed 's/^/       /'
[ "$FILL_RC" = 0 ] && ok "verify-fill pass 1 exited 0" || bad "verify-fill pass 1 exited $FILL_RC"

python - "$FILL_REPORT" "$CHUNK_K" "$HOOK_K" <<'PY' && ok "verify-fill pass 1: filled exactly the hole (delta, not a full re-send); verification=verified" || bad "verify-fill pass 1 assertions failed (see above)"
import json, sys

report = json.load(open(sys.argv[1]))
want_chunks, want_hooks = int(sys.argv[2]), int(sys.argv[3])
vf = report.get("verify_fill", {})
results = vf.get("results", {})

chunks_filled = results.get("catalog", {}).get("fill", {}).get("document_chunks", {}).get("filled", -1)
hooks_filled = results.get("telemetry", {}).get("fill", {}).get("hook_failures", {}).get("filled", -1)
total_filled = vf.get("total_filled", -1)
verification = report.get("verification", "?")

print(f"       document_chunks filled={chunks_filled} (want {want_chunks})")
print(f"       hook_failures filled={hooks_filled} (want {want_hooks})")
print(f"       total_filled={total_filled} verification={verification}")

fails = []
if chunks_filled != want_chunks:
    fails.append(f"document_chunks filled={chunks_filled} != {want_chunks}")
if hooks_filled != want_hooks:
    fails.append(f"hook_failures filled={hooks_filled} != {want_hooks}")
if total_filled != want_chunks + want_hooks:
    fails.append(
        f"total_filled={total_filled} != {want_chunks + want_hooks} "
        "(a bigger number here means a FULL RE-SEND leaked through — exactly "
        "the regression this journey exists to catch)"
    )
if verification != "verified":
    fails.append(f"verification={verification} != verified")

if fails:
    print("       FAIL: " + "; ".join(fails))
    sys.exit(1)
sys.exit(0)
PY

# ── Phase 6: verify-fill PASS 2 — the load-bearing no-op proof ───────────────
say "Phase 6 — verify-fill pass 2 (must be a TRUE no-op — the hole is already patched)"
NOOP_REPORT=/tmp/vf_report_noop.json
NOOP_OUT="$(nx storage migrate all --verify-fill --report "$NOOP_REPORT" 2>&1)"
NOOP_RC=$?
printf '%s\n' "$NOOP_OUT" | sed 's/^/       /'
[ "$NOOP_RC" = 0 ] && ok "verify-fill pass 2 exited 0" || bad "verify-fill pass 2 exited $NOOP_RC"

python - "$NOOP_REPORT" <<'PY' && ok "verify-fill pass 2: TRUE no-op (0 fills) — incremental != full re-send, proven end-to-end" || bad "verify-fill pass 2 was NOT a no-op — a diff that re-sends already-present rows is the exact bug this journey guards against"
import json, sys

report = json.load(open(sys.argv[1]))
vf = report.get("verify_fill", {})
results = vf.get("results", {})
chunks_filled = results.get("catalog", {}).get("fill", {}).get("document_chunks", {}).get("filled", -1)
hooks_filled = results.get("telemetry", {}).get("fill", {}).get("hook_failures", {}).get("filled", -1)
total_filled = vf.get("total_filled", -1)
print(f"       document_chunks filled={chunks_filled} hook_failures filled={hooks_filled} total_filled={total_filled}")
sys.exit(0 if (chunks_filled == 0 and hooks_filled == 0 and total_filled == 0) else 1)
PY

# ── RESULT ─────────────────────────────────────────────────────────────────
say "RESULT"
if [ "$FAILS" -eq 0 ]; then
  printf '\033[32mHOLE-PUNCH VERIFY-FILL MVV PASSED\033[0m — cold-acquired engine + real PG: punch -> delta-fill(K) -> no-op(0)\n'
  exit 0
else
  printf '\033[31mHOLE-PUNCH VERIFY-FILL MVV FAILED — %d check(s) failed\033[0m\n' "$FAILS"
  exit 1
fi
