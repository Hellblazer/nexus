#!/usr/bin/env bash
# RDR-185 P4.3 (nexus-n7u38.30) — ERA-SPANNING HOP MVV. Runs INSIDE the container.
#
# The RDR's Success Criterion, end to end: "a fresh-or-ancient install converges
# with `nx upgrade` alone — the 2026-07-16 work-instance shape (pre-RDR-108 ids,
# store_put-only collections, Chroma substrate) reaches current UNATTENDED; the
# 18-collection report becomes a progress line, not homework."
#
#   pip install conexus==$ERA_RELEASE     a REAL old PyPI release (the era)
#   nx daemon service install-binary +    stand the era's install up the way
#   nx init --service                      that era actually did: pre-stage
#                                          binary + PG bundle, then provision.
#                                          The always-download bundle is 6.4.0+
#                                          (GH #1381), so a <= 6.3.x era MUST
#                                          pre-stage or it demands a system PG.
#   seed_legacy.py --era-hop              the ancient DATA: Chroma substrate,
#                                          pre-RDR-108 16-char ids as full
#                                          catalog/T2 citizens, plus a
#                                          store_put-only note with NO source
#                                          content (re-index is impossible for
#                                          it — the GH #1408 dead end)
#   uv pip install --reinstall <wheel>    update the CODE. That is step one of
#                                          the whole upgrade story.
#   nx upgrade                            THE ONLY VERB. Converges the engine
#                                          precondition (old -> new), then walks
#                                          the ladder: T2 schema rung, then the
#                                          substrate rung with wire re-id.
#
# UNATTENDED is load-bearing: no TTY, no prompts, no --yes on anything. A walk
# that needs an answer here is a failure, not a pause — the seeded footprint is
# deliberately free of the three genuine decisions (nothing billable: the local
# bge-768 service re-embeds at no cost; no source-gone collection; no rollback).
#
# ONE VERB is asserted mechanically, not by reading the script: a PATH shim logs
# every TOP-LEVEL nx invocation (recursion-suppressed via NX_VERB_AUDIT_DEPTH so
# anything the product spawns internally is correctly NOT counted as something
# the user ran), and the audit at the end fails on any verb but `upgrade` and
# the read-only `doctor`.
set -uo pipefail

ERA_RELEASE="${ERA_RELEASE:?ERA_RELEASE must be set (e.g. 6.0.0)}"
ERA_ENGINE_TAG="${ERA_ENGINE_TAG:?ERA_ENGINE_TAG must be set (e.g. engine-service-v0.1.11)}"
NEW_ENGINE_TAG="${NEW_ENGINE_TAG:?NEW_ENGINE_TAG must be set (e.g. engine-service-v0.1.44)}"
ERA_EXPECT="${ERA_ENGINE_TAG#engine-service-v}"
NEW_EXPECT="${NEW_ENGINE_TAG#engine-service-v}"
CHROMA_LOCAL="${CHROMA_LOCAL:-/home/nexus/legacy-chroma}"
SEED_N="${SEED_N:-12}"
VERB_LOG="/tmp/nx-verb-audit.log"
FAILS=0

say()  { printf '\n\033[1m== %s ==\033[0m\n' "$*"; }
ok()   { printf '  \033[32mPASS\033[0m %s\n' "$*"; }
bad()  { printf '  \033[31mFAIL\033[0m %s\n' "$*"; FAILS=$((FAILS+1)); }
note() { printf '       %s\n' "$*"; }

export NX_SERVICE_MAX_HEAP="${NX_SERVICE_MAX_HEAP:-1g}"
git config --global user.email "era-hop@nexus.local" >/dev/null 2>&1 || true
git config --global user.name  "nexus era hop"       >/dev/null 2>&1 || true

# THE SEEDED STORE MUST BE AT THE PATH THE PRODUCT READS. `nx upgrade` has no
# --local-path knob — deliberately: naming your store is configuration, not
# upgrade ceremony — so the substrate rung's footprint gate resolves the store
# via NX_LOCAL_CHROMA_PATH -> $XDG_DATA_HOME/nexus/chroma. Seeding somewhere
# else and expecting the walk to find it is the nexus-id750 / GH #1381 class
# exactly: a detector pointed at a directory the store does not live in sees no
# footprint and no-ops "successfully".
#
# Observed live 2026-07-16 (first --era-hop run): seeded to /home/nexus/
# legacy-chroma while the rung read the XDG default, so the rung reported N/A,
# `nx upgrade` exited 0 and `nx doctor` printed "no pending rungs" — with ZERO
# collections migrated. Only the parity assert caught it.
export NX_LOCAL_CHROMA_PATH="$CHROMA_LOCAL"

# ── Quarantine ───────────────────────────────────────────────────────────────
say "Quarantine — nothing pre-staged"
command -v initdb >/dev/null 2>&1 && bad "system PostgreSQL present — not a clean box" || ok "no system PostgreSQL (bundle must provide it)"
test ! -e "$HOME/.config/nexus/service/nexus-service" && ok "no native binary pre-staged" || bad "native binary already present"

# ── Stage 1: the OLD release, from real PyPI ─────────────────────────────────
say "Stage 1 — pip install conexus==$ERA_RELEASE (real PyPI, the era's code)"
if uv pip install --python "$HOME/nxenv" "conexus==$ERA_RELEASE" 2>&1 | tail -5 | sed 's/^/       /'; then
  ok "installed conexus==$ERA_RELEASE"
else
  bad "pip install conexus==$ERA_RELEASE failed"; say "ABORT"; exit 1
fi
GOT_VER="$(nx --version 2>&1 | grep -oE '[0-9]+\.[0-9]+\.[0-9]+' | head -1)"
[ "$GOT_VER" = "$ERA_RELEASE" ] && ok "nx --version reports $GOT_VER" \
  || bad "nx --version reports $GOT_VER, expected $ERA_RELEASE"

# ── Stage 2: the OLD engine + the era's own provisioning path ────────────────
# The pre-stage is NOT optional ceremony and must not be "simplified" away: the
# always-download Postgres bundle arrived in 6.4.0 (GH #1381). On a <= 6.3.x
# release — which is the whole point of an ERA hop — `nx init --service` does
# NOT fetch the bundle and hard-fails on a box with no system PostgreSQL
# ("Install PostgreSQL 17: sudo apt-get install postgresql-17"), which is
# precisely what this cold box is. The era's documented path is to pre-stage
# binary + bundle with `install-binary` and then init, exactly as
# rehearse_cold.sh does; 6.0.0's binary_install.py carries install_pg_bundle
# for this reason. (Observed live 2026-07-16: without this, Stage 2 aborts.)
#
# This step SUPPLIES the era engine tag rather than letting the release resolve
# its own pin, so it does not prove "6.0.0's PINNED_SERVICE_TAG resolves to
# v0.1.11" — that is --package-upgrade's claim, not this leg's. It reconstructs
# the state a real 6.0.0 install HAS (ERA_ENGINE_TAG is 6.0.0's own pin, so the
# reconstruction is faithful), and the /version assert below proves the era
# engine is genuinely what is running before the hop starts.
say "Stage 2a — pre-stage the era's binary + PG bundle (the <= 6.3.x path)"
unset NEXUS_SERVICE_TAG NX_SERVICE_TAG 2>/dev/null || true
if nx daemon service install-binary "$ERA_ENGINE_TAG" 2>&1 | tail -12 | sed 's/^/       /'; then
  ok "install-binary acquired + verified the era binary + PG bundle ($ERA_ENGINE_TAG)"
else
  bad "install-binary failed for $ERA_ENGINE_TAG on $ERA_RELEASE"; say "ABORT"; exit 1
fi

say "Stage 2b — nx init --service (provision PG from the era bundle, fetch bge, serve)"
export NEXUS_SERVICE_TAG="$ERA_ENGINE_TAG"   # already installed: the ensure-binary step no-ops
if nx init --service --embedder bge-768 --yes 2>&1 | tail -20 | sed 's/^/       /'; then
  ok "nx init --service (provisioned PG + the era engine + started)"
else
  bad "nx init --service failed on $ERA_RELEASE"; say "ABORT (provision failed)"; exit 1
fi
# The hop must converge the engine on its own merits from here on — leave no
# pin in the environment for the ladder's precondition to read.
unset NEXUS_SERVICE_TAG NX_SERVICE_TAG 2>/dev/null || true
export NX_STORAGE_BACKEND=service
# shellcheck disable=SC1091
[ -f "$HOME/.config/nexus/pg_credentials" ] && { set -a; . "$HOME/.config/nexus/pg_credentials"; set +a; }
unset NX_SERVICE_URL NX_SERVICE_PORT NX_SERVICE_HOST 2>/dev/null || true

# INSTRUMENTATION, not the upgrade path. These two helpers ask the box what
# state it is in; they never converge anything. They deliberately call the REAL
# nx rather than the audited shim, because the audit's question is "did
# converging this install require any verb besides `nx upgrade`?" — and a
# harness taking a MEASUREMENT is not the user running a verb. Routing them
# through the shim logged `daemon` twice and failed the audit on the harness's
# own thermometer (observed live 2026-07-16). Anything that actually acts on
# the install must go through plain `nx` so the audit sees it.
REAL_NX="$(command -v nx)"
[ -n "$REAL_NX" ] || { bad "cannot resolve nx"; say "ABORT"; exit 1; }
_wait_healthy() {
  local tries="${1:-30}"
  for _ in $(seq 1 "$tries"); do
    if "$REAL_NX" daemon service status 2>&1 | grep -qiE "health.*ok|healthy|serving|status.*ok|running"; then
      return 0
    fi
    sleep 2
  done
  return 1
}
_release_version() {
  "$REAL_NX" daemon service status --json 2>/dev/null \
    | python3 -c 'import sys,json;print(json.load(sys.stdin).get("service_release_version") or "")' 2>/dev/null
}

if _wait_healthy 30; then
  ok "service healthy on the era's own engine"
else
  nx daemon service status 2>&1 | sed 's/^/       /' || true
  bad "service did not reach healthy on $ERA_RELEASE"; say "ABORT"; exit 1
fi
RV0="$(_release_version)"
if [ "$RV0" = "$ERA_EXPECT" ]; then
  ok "/version release_version=$RV0 — the era engine ($ERA_ENGINE_TAG)"
else
  bad "/version release_version=$RV0, expected $ERA_EXPECT — wrong starting engine, the hop would not span an era"
fi

# ── Stage 3: the ancient DATA ────────────────────────────────────────────────
say "Stage 3 — seed the GH #1408 work-instance footprint (Chroma + pre-RDR-108 ids)"
note "16-char ids as FULL catalog/T2 citizens + a store_put-only note with no"
note "source content: re-indexing it is IMPOSSIBLE, so only wire re-id converges it."
if SEED_RAW="$(python /home/nexus/seed_legacy.py "$CHROMA_LOCAL" --era-hop --n "$SEED_N" 2>&1)"; then
  SEED_JSON="$(printf '%s\n' "$SEED_RAW" | tail -1)"
  case "$SEED_JSON" in
    '{'*'}') ok "seeded era footprint" ; note "$SEED_JSON" ;;
    *) bad "seed produced no JSON manifest (got: '$SEED_JSON')"; say "ABORT"; exit 1 ;;
  esac
else
  printf '%s\n' "$SEED_RAW" | tail -5 | sed 's/^/       /'
  bad "seed failed"; say "ABORT"; exit 1
fi
# Non-vacuity: the era shapes must really be legacy, or every convergence
# assertion below passes trivially against already-conformant ids.
python3 - "$SEED_JSON" <<'PY' && ok "seeded ids are genuinely pre-RDR-108 (16-char)" || { bad "seed did not produce legacy ids — every convergence assert below would be vacuous"; say "ABORT"; exit 1; }
import json, sys
m = json.loads(sys.argv[1])
legacy = m.get("legacy_ids") or {}
if not legacy:
    print("       no legacy_ids in the manifest"); sys.exit(1)
for coll, ids in legacy.items():
    if not ids or any(len(i) != 16 for i in ids):
        print(f"       {coll}: not all ids are 16-char: {ids[:3]}"); sys.exit(1)
    print(f"       {coll}: {len(ids)} legacy 16-char ids")
sys.exit(0)
PY

# ── Stage 4: update the CODE (and nothing else) ──────────────────────────────
say "Stage 4 — package upgrade to the working tree (the ONLY manual step in the story)"
WHEEL="$(ls "$HOME"/worktree-wheel/conexus-*.whl 2>/dev/null | head -1)"
[ -n "$WHEEL" ] || { bad "no worktree wheel in $HOME/worktree-wheel/"; say "ABORT"; exit 1; }
BEFORE_SHA="$(sha256sum "$HOME/.config/nexus/service/nexus-service" 2>/dev/null | awk '{print $1}')"
if uv pip install --python "$HOME/nxenv" --reinstall "$WHEEL" 2>&1 | tail -5 | sed 's/^/       /'; then
  ok "package upgraded to the working-tree build"
else
  bad "package upgrade failed"; say "ABORT"; exit 1
fi
AFTER_SHA="$(sha256sum "$HOME/.config/nexus/service/nexus-service" 2>/dev/null | awk '{print $1}')"
[ "$BEFORE_SHA" = "$AFTER_SHA" ] \
  && ok "engine untouched by the package upgrade itself (sha256 unchanged) — nothing has converged yet" \
  || bad "engine binary changed during the PACKAGE step — the harness leaked engine supply, invalidating the hop"

# ── The verb audit shim ──────────────────────────────────────────────────────
# From here on, every TOP-LEVEL `nx` the harness runs is recorded. Recursion is
# suppressed (NX_VERB_AUDIT_DEPTH): a subprocess the PRODUCT spawns is its own
# implementation detail, not a verb the user ran, and counting it would make
# "one verb" untestable rather than true.
say "Arming the verb audit — every top-level nx invocation is recorded"
mkdir -p "$HOME/verbaudit"
cat > "$HOME/verbaudit/nx" <<SHIM
#!/usr/bin/env bash
if [ -z "\${NX_VERB_AUDIT_DEPTH:-}" ]; then
  printf '%s\n' "\${1:-<none>}" >> "$VERB_LOG"
fi
export NX_VERB_AUDIT_DEPTH=1
exec "$REAL_NX" "\$@"
SHIM
chmod +x "$HOME/verbaudit/nx"
export PATH="$HOME/verbaudit:$PATH"
: > "$VERB_LOG"
[ "$(command -v nx)" = "$HOME/verbaudit/nx" ] && ok "shim is first on PATH (real nx: $REAL_NX)" \
  || bad "shim did not take effect — the one-verb audit would be vacuous"

# ── NON-VACUITY GATE: there must BE something to converge ────────────────────
# The lesson of the first live run. `nx upgrade` exited 0 and `nx doctor`
# printed "✓ Upgrade ladder: no pending rungs (2 registered)" on a box where
# NOTHING migrated — because the rung correctly saw no footprint at the path it
# reads. Both of those are the assertions a reasonable person writes first, and
# BOTH were green over a total no-op. A leg that cannot see its own fixture
# passes loudest.
#
# So: before the walk, the product itself must SAY the substrate rung is
# pending. This is the product's own read-only detect() — not a harness
# re-derivation — so it fails when the fixture is invisible for ANY reason
# (wrong path, kill switch, a footprint gate that regressed), rather than
# letting every downstream assert grade a walk that never had work to do.
say "Non-vacuity gate — the product must SEE the seeded era footprint"
DRY_OUT="$(nx upgrade --dry-run 2>&1 < /dev/null)"
printf '%s\n' "$DRY_OUT" | sed 's/^/       /'
if printf '%s' "$DRY_OUT" | grep -q "rung 'substrate-etl' pending"; then
  ok "the substrate rung reports PENDING before the walk — there is real work to converge"
else
  bad "nx upgrade --dry-run does NOT report the substrate rung pending — the seeded footprint is invisible to the product (check NX_LOCAL_CHROMA_PATH=$NX_LOCAL_CHROMA_PATH vs the rung's resolver). Every convergence assert below would grade a no-op."
  say "ABORT (vacuous fixture — refusing to report a pass over work that never existed)"
  exit 1
fi

# ── Stage 5: THE WHOLE UPGRADE ───────────────────────────────────────────────
say "Stage 5 — nx upgrade (the single trigger; unattended, no TTY, no --yes)"
UP_OUT="$(nx upgrade 2>&1 < /dev/null)"
UP_RC=$?
printf '%s\n' "$UP_OUT" | sed 's/^/       /'
[ "$UP_RC" = 0 ] && ok "nx upgrade exited 0" || bad "nx upgrade exited $UP_RC"

# P4.2, live: the everyday output must not advertise a verb demoted out of --help.
if printf '%s' "$UP_OUT" | grep -qE 'nx (guided-upgrade|migrate-to-service|migration-audit)'; then
  bad "nx upgrade advertised a DEMOTED verb as a remedy — the story is not one verb"
else
  ok "nx upgrade named no demoted verb"
fi

# ── Assert: the engine precondition converged (old era -> current) ───────────
say "Assert — the engine precondition converged across the era"
if _wait_healthy 45; then ok "service healthy after the walk"; else
  nx daemon service status 2>&1 | sed 's/^/       /' || true
  bad "service not healthy after nx upgrade"
fi
RV1="$(_release_version)"
[ "$RV1" = "$NEW_EXPECT" ] \
  && ok "/version release_version=$RV1 — converged $ERA_EXPECT -> $NEW_EXPECT by nx upgrade alone" \
  || bad "/version release_version=$RV1, expected $NEW_EXPECT — the engine precondition did not converge"

# ── Assert: parity + WIRE RE-ID + the cascade ────────────────────────────────
say "Assert — every collection converged, ids are conformant, references follow"
python3 - "$SEED_JSON" <<'PY' && ok "parity + wire re-id + cascade validated" || bad "convergence assertions failed"
import json, sys

m = json.loads(sys.argv[1])
seeded = m.get("collections", {})
cross = m.get("cross_model", {})
legacy = m.get("legacy_ids", {})
expected = m.get("expected_reid", {})
fails = 0

from nexus.db import make_t3
t3 = make_t3()

# 1. Parity: pgvector serves every seeded collection at full count.
for name, want in seeded.items():
    target = cross.get(name, name)
    try:
        got = t3.count(target)
    except Exception as e:
        print(f"       {target}: count() error: {e}"); fails += 1; continue
    flag = "ok" if got == want else "MISMATCH"
    print(f"       {name} -> {target}: service={got} seeded={want} [{flag}]")
    if got != want:
        fails += 1

# 2. Wire re-id: the legacy ids are GONE and the derived ones are PRESENT.
#    `existing_ids(collection, ids)` returns the subset of *ids* present, so
#    each direction is an exact membership probe. Exactness matters here more
#    than usual: a 16-char legacy id is a strict PREFIX of its 32-char
#    successor (both are sha256 of the same text), so any substring-flavoured
#    check would report success against a store that never converged at all.
for coll, old_ids in legacy.items():
    target = cross.get(coll, coll)
    want_new = expected.get(coll, [])
    try:
        still_legacy = sorted(t3.existing_ids(target, list(old_ids)))
        present_new = t3.existing_ids(target, list(want_new))
    except Exception as e:
        print(f"       {target}: existing_ids() error: {e}"); fails += 1; continue
    missing_new = sorted(set(want_new) - present_new)
    if still_legacy:
        print(f"       {target}: {len(still_legacy)} LEGACY id(s) survived: {still_legacy[:3]}")
        fails += 1
    if missing_new:
        print(f"       {target}: {len(missing_new)} derived id(s) absent: {missing_new[:3]}")
        fails += 1
    if not still_legacy and not missing_new:
        print(f"       {target}: all {len(old_ids)} legacy ids -> their derived 32-char chashes")

# 3. Nothing non-conformant anywhere in the migrated collections — the
#    membership probes above are per-id, so this catches a stray id neither
#    list names (a partial carry, a duplicated row).
for name in seeded:
    target = cross.get(name, name)
    try:
        bad_len = sorted(
            cid for cid, _ in t3.list_chunks_with_metadata(target) if len(cid) != 32
        )
    except Exception as e:
        print(f"       {target}: list_chunks_with_metadata() error: {e}"); fails += 1; continue
    if bad_len:
        print(f"       {target}: non-32-char id(s) present: {bad_len[:3]}"); fails += 1
if not fails:
    print("       every migrated collection holds only conformant 32-char chunk ids")

sys.exit(1 if fails else 0)
PY

# ── Assert: the cascade re-pointed the catalog manifest + topic assignment ───
say "Assert — the old->new map cascaded (catalog manifest + topic_assignments)"
python3 - "$SEED_JSON" <<'PY' && ok "no dangling legacy chash left in T2/catalog" || bad "cascade left legacy chashes behind"
import json, sqlite3, sys
from pathlib import Path

m = json.loads(sys.argv[1])
legacy = {i for ids in (m.get("legacy_ids") or {}).values() for i in ids}
if not legacy:
    print("       no legacy ids in manifest — nothing to check (vacuous)"); sys.exit(1)

from nexus.config import nexus_config_dir
cfg = nexus_config_dir()
fails = 0

# topic_assignments.doc_id is chash-keyed (the store RDR-180's inventory missed
# and the .13 audit re-found). A surviving legacy doc_id means the sourceless
# note's assignment dangles.
mem = cfg / "memory.db"
if mem.exists():
    con = sqlite3.connect(mem)
    try:
        rows = [r[0] for r in con.execute("SELECT doc_id FROM topic_assignments")]
        left = sorted(set(rows) & legacy)
        if left:
            print(f"       topic_assignments still keyed by legacy chash: {left[:3]}"); fails += 1
        else:
            print(f"       topic_assignments: {len(rows)} row(s), no legacy chash")
    except Exception as e:
        print(f"       topic_assignments probe error: {e}"); fails += 1
    finally:
        con.close()
else:
    print(f"       no memory.db at {mem}"); fails += 1

cat = cfg / "catalog" / ".catalog.db"
if cat.exists():
    con = sqlite3.connect(cat)
    try:
        rows = [r[0] for r in con.execute("SELECT chash FROM document_chunks")]
        left = sorted(set(rows) & legacy)
        if left:
            print(f"       document_chunks manifest still holds legacy chash: {left[:3]}"); fails += 1
        else:
            print(f"       document_chunks: {len(rows)} manifest row(s), no legacy chash")
    except Exception as e:
        print(f"       document_chunks probe error: {e}"); fails += 1
    finally:
        con.close()
else:
    print(f"       no .catalog.db at {cat}"); fails += 1

sys.exit(1 if fails else 0)
PY

# ── Assert: RDR-176 immutable source ─────────────────────────────────────────
say "Assert — the legacy Chroma source is untouched (rollback target intact)"
python3 - "$CHROMA_LOCAL" "$SEED_JSON" <<'PY' && ok "legacy Chroma intact, legacy ids still there" || bad "legacy Chroma was mutated — the immutable-source invariant broke"
import json, sys, chromadb
path, m = sys.argv[1], json.loads(sys.argv[2])
seed = m.get("collections", {})
legacy = m.get("legacy_ids", {})
client = chromadb.PersistentClient(path=path)
fails = 0
for name, want in seed.items():
    got = client.get_collection(name).count()
    if got != want:
        print(f"       {name}: source now {got}, seeded {want}"); fails += 1
# The source must still hold its ORIGINAL legacy ids: wire re-id happens in
# flight, never by rewriting the source (RDR-176).
for coll, old_ids in legacy.items():
    ids = set(client.get_collection(coll).get()["ids"])
    if not set(old_ids) <= ids:
        print(f"       {coll}: source ids were rewritten — not an immutable source"); fails += 1
    else:
        print(f"       {coll}: source still holds its {len(old_ids)} original legacy ids")
sys.exit(1 if fails else 0)
PY

# ── Assert: doctor reports a converged ladder ────────────────────────────────
say "Assert — nx doctor reports no pending rungs"
DOC_OUT="$(nx doctor 2>&1 < /dev/null)"
printf '%s\n' "$DOC_OUT" | grep -iE 'upgrade ladder|chunk-id era' | sed 's/^/       /' || true
if printf '%s' "$DOC_OUT" | grep -qiE 'pending upgrade rung'; then
  bad "nx doctor still reports pending rung(s) after a clean walk"
else
  ok "no pending rungs — the ladder is converged"
fi
if printf '%s' "$DOC_OUT" | grep -qE 'nx (guided-upgrade|migrate-to-service|migration-audit)'; then
  bad "nx doctor advertised a DEMOTED verb"
else
  ok "nx doctor named no demoted verb"
fi

# ── THE ONE-VERB AUDIT ───────────────────────────────────────────────────────
say "Assert — ONLY nx upgrade was invoked (mechanical audit)"
note "recorded top-level invocations:"
sort "$VERB_LOG" | uniq -c | sed 's/^/       /'
UNEXPECTED="$(grep -vxE 'upgrade|doctor' "$VERB_LOG" | sort -u || true)"
if [ -n "$UNEXPECTED" ]; then
  bad "verbs other than upgrade/doctor were invoked after the code update: $(printf '%s' "$UNEXPECTED" | tr '\n' ' ')"
else
  ok "only 'upgrade' (the trigger) and 'doctor' (read-only report) were invoked"
fi
# Non-vacuity: an empty log would pass the check above trivially.
grep -qx 'upgrade' "$VERB_LOG" \
  && ok "the audit really observed the upgrade (log is not empty)" \
  || bad "verb log never recorded 'upgrade' — the shim did not capture, audit is vacuous"

say "RESULT"
if [ "$FAILS" -eq 0 ]; then
  printf '\033[32mERA-SPANNING HOP MVV PASSED\033[0m — conexus %s + %s + pre-RDR-108 ids + Chroma substrate -> current, via `nx upgrade` ALONE, unattended\n' \
    "$ERA_RELEASE" "$ERA_ENGINE_TAG"
  exit 0
else
  printf '\033[31mERA-SPANNING HOP MVV FAILED — %d check(s) failed\033[0m\n' "$FAILS"
  exit 1
fi
