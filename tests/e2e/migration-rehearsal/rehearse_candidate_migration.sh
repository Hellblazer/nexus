#!/usr/bin/env bash
# nexus-z0ylb — the CANDIDATE-MIGRATION rehearsal. Runs INSIDE the
# container.
#
# From the nexus-eo3qv critique of the --chash-window redesign
# (substantive-critic, 2026-08-14, T2
# nexus/critique-nexus-eo3qv-chash-window-redesign-2026-08-14): no leg in
# this harness exercised a locally-built CANDIDATE engine's Liquibase walk
# against a genuinely POPULATED store. --chash-window drives the
# PUBLISHED floor engine's changeset walk over a populated store (never
# the candidate); --shakeout drives the candidate, but only on a FRESH
# install with zero pre-existing data — it carries no chash/rdr180/cohort
# migration logic at all. A CANDIDATE engine carrying a NEW migration
# changeset (the exact class vectors-004 was: ALWAYS-COPY over 385k
# populated rows, RDR-191) reached the tag gate with zero pre-tag
# rehearsal against a populated store. This leg closes that gap.
#
#   Stage 1  uv tool install the WORKING-TREE wheel (the client under
#            test throughout — this leg tests the ENGINE delta, not a
#            client-upgrade axis; no old release is ever installed).
#   Stage 2  install-binary the PUBLISHED FLOOR engine for real, provision
#            + serve it.
#   Stage 3  POPULATE through the floor engine — the leg's soul: store
#            puts, an md index, and a real taxonomy discovery pass so
#            nexus.taxonomy_centroids and nexus.topic_assignments hold
#            genuine rows too. Every invariant this leg proves is
#            captured HERE, before the swap.
#   Stage 4  stop the service (PG stays up — nx daemon service stop
#            without --with-pg); hand-swap the LOCALLY-BUILT candidate
#            binary in at the well-known location; rewrite ONLY the
#            provenance sidecar's sha256 field to match the candidate's
#            real bytes, keeping tag/version pinned at the floor. This
#            is HARNESS bookkeeping, not a production converge-safety
#            technique — a real release install-binary's the tag and
#            writes an HONEST sidecar at download time; nothing in
#            production ever hand-swaps a binary under a sidecar that
#            names a different one. The rewrite exists so nothing INSIDE
#            this test run (a `nx doctor` / `nx daemon restart-stale`
#            fired later in Stage 5) silently re-acquires the floor tag
#            over the candidate mid-rehearsal and invalidates the rest of
#            the leg without saying so.
#   Stage 5  start; assert healthy — the candidate's FULL Liquibase pass
#            over the populated store. Assert the changeset delta, EXACT
#            row invariants, and that reads/writes/search still serve.
#
# COVERAGE (substantive-critic finding, 2026-08-14, T2
# nexus/critique-nexus-z0ylb-candidate-migration-rehearsal-2026-08-14
# [22547]) — what this leg DOES and does NOT prove. It exercises: boot
# succeeding over populated data (a broken changeset that assumes an
# empty table fails here, never silently); RLS not going DML-blind mid-
# migration (every INSERT/UPDATE this leg's own population produced must
# still be visible/writable after the walk); a changeset's own GRANT/
# ownership statements not bricking boot (a missing grant on a newly
# created object is exactly a boot-time failure, not a silent no-op);
# CASCADE fallout on a DROP TABLE (a stray dependent object the author
# didn't know about shows up as a boot failure, not a quiet orphan); and
# checksum/row-count integrity (Liquibase's own checksum re-validation,
# plus this leg's EXACT row-invariant asserts).
#
# It structurally CANNOT catch two classes, by construction of what this
# leg seeds:
#   (a) CROSS-SHARD PK COLLISION (the vectors-004/taxonomy-007-style
#       "cross-shard (tenant_id, collection, ...) collision" DO $$ guard
#       those changesets carry) — this leg seeds ONE embedding dimension
#       (bge-768) only. Reproducing a genuine collision needs a SECOND
#       populated dimension sharing a colliding key, which this leg's
#       single-embedder posture cannot produce. A dim-diverse populated-
#       store rehearsal would need to seed under two embedders in one
#       store, not a gap this leg closes today.
#   (b) PLANNER STATISTICS FLIPS from a stats-absent post-migration table
#       (ANALYZE not yet run) picking a different query plan under real
#       data volume — this leg's corpus tops out around ~190 rows, far
#       too small to exhibit a stats-driven plan flip. That class is
#       pinned at the JAVA layer instead:
#       SchemaMigratorIntegrationTest::rdr180Rewrite_leavesPlannerStatsFresh
#       (service/src/test/java/dev/nexus/service/SchemaMigratorIntegrationTest.java).
#
# Row invariants captured span T3 (chunks, catalog manifest/documents,
# taxonomy centroids/assignments) — the chunk-migration surface this leg
# exists to rehearse. T2 tables (memory, plans, telemetry, ...) are
# DELIBERATELY out of scope: T2 is a separate store family with its own
# migration surface, and this leg's population never writes to it.
set -uo pipefail

FLOOR_VERSION="${FLOOR_VERSION:?FLOOR_VERSION must be set (e.g. 0.1.75)}"
FLOOR_TAG="engine-service-v${FLOOR_VERSION}"
SEED_N="${SEED_N:-6}"
TAXO_TOPICS=4
TAXO_PER_TOPIC=15
FAILS=0

say()  { printf '\n\033[1m== %s ==\033[0m\n' "$*"; }
ok()   { printf '  \033[32mPASS\033[0m %s\n' "$*"; }
bad()  { printf '  \033[31mFAIL\033[0m %s\n' "$*"; FAILS=$((FAILS+1)); }
note() { printf '       %s\n' "$*"; }

export NX_SERVICE_MAX_HEAP="${NX_SERVICE_MAX_HEAP:-1g}"
git config --global user.email "candidate-migration@nexus.local" >/dev/null 2>&1 || true
git config --global user.name  "nexus candidate migration"       >/dev/null 2>&1 || true

SVC_NATIVE_DIR="/opt/nexus-service-native"
SVC_WELL_KNOWN_DIR="$HOME/.config/nexus/service"

# ── Quarantine ────────────────────────────────────────────────────────────
say "Quarantine — no system PG, no engine at the well-known location yet"
command -v initdb >/dev/null 2>&1 && bad "system PostgreSQL present — not a clean box" || ok "no system PostgreSQL (bundle must provide it)"
test ! -e "$SVC_WELL_KNOWN_DIR/nexus-service" && ok "no engine pre-staged at the well-known location" || bad "engine already present at the well-known location"
test -x "$SVC_NATIVE_DIR/nexus-service" && ok "candidate native binary staged at $SVC_NATIVE_DIR (positioned only at Stage 4)" || { bad "candidate binary missing at $SVC_NATIVE_DIR"; exit 1; }

WHEEL="$(ls "$HOME"/worktree-wheel/conexus-*.whl 2>/dev/null | head -1)"
[ -n "$WHEEL" ] || { bad "no worktree wheel in $HOME/worktree-wheel/"; say "ABORT"; exit 1; }

# ── Stage 1: the working-tree wheel (the ONLY client this leg ever runs) ──
say "Stage 1 — uv tool install the working-tree wheel"
if uv tool install --python 3.12 "$WHEEL" 2>&1 | tail -4 | sed 's/^/       /'; then
  ok "tool-installed the working-tree wheel"
else
  bad "uv tool install $WHEEL failed"; say "ABORT"; exit 1
fi
nx --version >/dev/null 2>&1 && ok "nx installed ($(nx --version 2>&1))" || { bad "nx --version failed"; say "ABORT"; exit 1; }
TOOLPY="$HOME/.local/share/uv/tools/conexus/bin/python"
[ -x "$TOOLPY" ] || { bad "no python at $TOOLPY"; say "ABORT"; exit 1; }

# ── Stage 2: the PUBLISHED FLOOR engine — install for real, provision ────
say "Stage 2a — install-binary the PUBLISHED FLOOR engine ($FLOOR_TAG)"
unset NEXUS_SERVICE_TAG NX_SERVICE_TAG 2>/dev/null || true
if nx daemon service install-binary "$FLOOR_TAG" 2>&1 | tail -8 | sed 's/^/       /'; then
  ok "install-binary acquired + verified $FLOOR_TAG (provenance sidecar written)"
else
  bad "install-binary failed for $FLOOR_TAG"; say "ABORT"; exit 1
fi

say "Stage 2b — nx init --service (provision PG, fetch bge, serve; --no-autostart: bare-container posture)"
export NEXUS_SERVICE_TAG="$FLOOR_TAG"   # already installed: ensure-binary no-ops
init_ok=0
for attempt in 1 2; do
  note "nx init --service --embedder bge-768 --yes --no-autostart (attempt $attempt) …"
  if nx init --service --embedder bge-768 --yes --no-autostart 2>&1 | tail -15 | sed 's/^/       /'; then
    init_ok=1; break
  fi
  note "attempt $attempt failed; supervisor log tail:"
  for f in "$HOME/.config/nexus/logs/storage_service.log" "$HOME/.config/nexus/logs/service_supervisor.log"; do
    [ -f "$f" ] && tail -20 "$f" | sed 's/^/       | /'
  done
  sleep 5
done
if [ "$init_ok" = 1 ]; then
  ok "init --service (provision + serve on the floor engine)"
else
  bad "nx init --service failed after 2 attempts"; say "ABORT"; exit 1
fi
unset NEXUS_SERVICE_TAG NX_SERVICE_TAG 2>/dev/null || true
export NX_STORAGE_BACKEND=service
# shellcheck disable=SC1091
[ -f "$HOME/.config/nexus/pg_credentials" ] && { set -a; . "$HOME/.config/nexus/pg_credentials"; set +a; }
unset NX_SERVICE_URL NX_SERVICE_PORT NX_SERVICE_HOST 2>/dev/null || true

_wait_healthy() {
  local tries="${1:-30}"
  for _ in $(seq 1 "$tries"); do
    if nx daemon service status 2>&1 | grep -qiE "health.*ok|healthy|serving|status.*ok|running"; then
      return 0
    fi
    sleep 2
  done
  return 1
}
_release_version() {
  nx daemon service status --json 2>/dev/null \
    | python3 -c 'import sys,json;print(json.load(sys.stdin).get("service_release_version") or "")' 2>/dev/null
}

if _wait_healthy 30; then ok "service healthy on the floor engine"; else
  nx daemon service status 2>&1 | sed 's/^/       /' || true
  bad "service did not reach healthy on the floor engine"; say "ABORT"; exit 1
fi
RV0="$(_release_version)"
[ "$RV0" = "$FLOOR_VERSION" ] && ok "/version release_version=$RV0 ($FLOOR_TAG)" \
  || bad "/version release_version=$RV0, expected $FLOOR_VERSION — wrong starting engine"

# ── SQL probe plumbing (bundle's own PG SUPERUSER, not nexus_diag) ────────
# Direct psql, NOT the product's diag choke point: these are harness
# measurements (content-reading probes the read-only-metadata lint would
# rightly refuse the product itself). tail -1: discover_pg_binaries logs a
# structlog debug line to stdout before the path prints — the path is
# always the LAST line.
#
# nexus-z0ylb post-review fix (CLUSTER 2, live acceptance finding): every
# post-swap invariant read as nexus_diag failed "permission denied for
# table <X>" while the IDENTICAL pre-swap reads had succeeded. Root
# cause, confirmed by reading grants-nexus-diag.xml +
# upgrade_ladder/rungs/chash_rekey.py: grants-nexus-diag-1/-2 are
# runAlways changesets, era-gated on whether nexus.diag_chash_conformance
# EXISTS — legacy era (view absent) grants nexus_diag direct SELECT on
# every nexus/t1 table; view era (view present) REVOKES it, leaving only
# the view + the Liquibase journal. The view does not exist at the FIRST
# engine boot (Stage 2b, before any chash-bearing table exists), so that
# boot's Liquibase run applies the LEGACY grants -- pre-swap reads see
# direct-table SELECT. `nx init --service`'s own chash-rekey rung
# convergence THEN creates the view client-side, via the bootstrap
# superuser, AFTER that boot's Liquibase has already run and moved on --
# so the runAlways REVOKE does not retroactively fire mid-session. Stage
# 5's candidate boot is a SECOND, independent Liquibase run against the
# SAME cluster: this time the view already exists, so grants-nexus-diag-2
# fires for real and revokes nexus_diag's direct-table access. This is a
# REAL, confirmed product behaviour (nexus_diag's effective grants can
# narrow across an ordinary engine restart, once something has created
# the conformance view) — reported to Hal for a bead (T2
# nexus/candidate-migration-nexus-diag-grants-narrowing-2026-08-14),
# NOT silently designed around here.
#
# The fix for THIS harness: this is a throwaway sandbox cluster the
# harness fully owns, so read every invariant as the bundle's own PG
# SUPERUSER instead of the tenant-scoped nexus_diag role — trust-
# authenticated local TCP (pg_hba.conf --auth=trust), BYPASSES RLS *and*
# every grant, so it is immune to the era flip above and gives an
# apples-to-apples comparison pre- and post-swap. Identity resolution
# mirrors bootstrap_superuser() (src/nexus/db/pg_provision.py) EXACTLY —
# same env, same process tree, so this always names the same role `nx
# init --service` provisioned as superuser, whether that's "nexus" (this
# container's OS user) or the "postgres" fallback.
PSQL_BIN="$("$TOOLPY" -c 'from nexus.db.pg_provision import discover_pg_binaries; print(discover_pg_binaries().psql)' 2>/dev/null | tail -1)"
[ -x "$PSQL_BIN" ] || { bad "cannot resolve the bundled psql (got: '$PSQL_BIN')"; say "ABORT"; exit 1; }
PG_SUPERUSER="${USER:-${LOGNAME:-postgres}}"
diag_sql() {
  "$PSQL_BIN" -h 127.0.0.1 -p "$PG_PORT" \
    -U "$PG_SUPERUSER" -d nexus -tA -c "$1" 2>&1
}

# Baseline changeset count — captured HERE (Stage 3, right after init),
# before any population, so the Stage 5 delta reflects ONLY what the
# CANDIDATE's boot adds, never anything the floor's own init already ran.
CHANGESET_PRE="$(diag_sql "SELECT count(*) FROM public.databasechangelog")"
note "baseline DATABASECHANGELOG rows (post-floor-init): $CHANGESET_PRE"

# ── Stage 3: POPULATE through the floor engine — the leg's soul ──────────
say "Stage 3a — seed $SEED_N marker notes (content + catalog manifests)"
MARKER1="candmigmarker1populate"
for i in $(seq 1 "$SEED_N"); do
  printf 'candidate-migration rehearsal note %s — candmigmarker%spopulate content body' "$i" "$i" \
    | nx store put - --title "candmig-note-$i" --collection knowledge__candmig >/dev/null 2>&1 \
    || bad "nx store put failed for marker note $i"
done
ok "seeded $SEED_N marker notes"

say "Stage 3b — index 2 docs-shaped markdown files"
DOC1=/tmp/candmig-doc-1.md
DOC2=/tmp/candmig-doc-2.md
printf '# Candidate migration rehearsal doc 1\n\nThe quixotic ferroequinologist marker anchors this document for retrieval.\n' > "$DOC1"
printf '# Candidate migration rehearsal doc 2\n\nA second undulating marmoset marker anchors this companion document.\n' > "$DOC2"
if nx index md "$DOC1" --corpus candmig 2>&1 | tail -4 | sed 's/^/       /'; then ok "indexed doc 1"; else bad "index md doc 1 failed"; fi
if nx index md "$DOC2" --corpus candmig 2>&1 | tail -4 | sed 's/^/       /'; then ok "indexed doc 2"; else bad "index md doc 2 failed"; fi

say "Stage 3c — seed a real taxonomy corpus ($TAXO_TOPICS topics x $TAXO_PER_TOPIC notes)"
# Four topically-distinct, well-separated vocabularies so HDBSCAN
# (min_cluster_size=5, cap floor 50 per nexus-9b9oi — irrelevant at this
# n) has real density to find. bge-768 is a deterministic local ONNX
# embedder (no randomness), so clustering here is repeatable run to run —
# still, only >=1 centroid is HARD-asserted below; the >=2 expectation is
# reported as an observation, not gated on, per the bead's own
# non-vacuity-vs-flakiness instruction (nexus-z0ylb).
_topic_text() {
  case "$1" in
    1) printf 'Astronomy topic note %d: the orbital mechanics of a binary star system trace an elliptical path around their common barycenter, governed by gravitational attraction and angular momentum.' "$2" ;;
    2) printf 'Culinary topic note %d: braising the short rib in red wine and aromatics for three hours yields a tender, deeply reduced sauce over polenta.' "$2" ;;
    3) printf 'Distributed systems topic note %d: a Raft leader replicates log entries to followers and commits once a quorum acknowledges the append, preserving linearizability.' "$2" ;;
    4) printf 'Gardening topic note %d: transplanting tomato seedlings after the last frost, spaced eighteen inches apart, encourages deep root growth and heavier fruit set.' "$2" ;;
  esac
}
t=1
while [ "$t" -le "$TAXO_TOPICS" ]; do
  n=1
  while [ "$n" -le "$TAXO_PER_TOPIC" ]; do
    _topic_text "$t" "$n" \
      | nx store put - --title "candmig-taxo-t${t}-n${n}" --collection knowledge__candmig >/dev/null 2>&1 \
      || bad "taxonomy seed put failed (topic $t note $n)"
    n=$((n+1))
  done
  t=$((t+1))
done
ok "seeded $((TAXO_TOPICS * TAXO_PER_TOPIC)) taxonomy-corpus notes across $TAXO_TOPICS topics"

say "Stage 3d — nx taxonomy discover --all"
# nexus-z0ylb post-review fix: `--collection knowledge__candmig` (the
# BARE, legacy-2-segment name I typed at Stage 3a/3c) is NOT what
# `store put` actually wrote to — `store put`'s own --collection option
# runs through t3_collection_name(), which auto-promotes a bare/legacy
# name to the conformant 4-segment physical collection (here,
# knowledge__candmig__bge-base-en-v15-768__v1 under this leg's local
# bge-768 embedder). `nx taxonomy discover --collection <name>` does NOT
# apply that same promotion (unlike store/search/index) -- it passes the
# argument straight through to PgVectorRepository.count(), which
# REQUIRES a four-segment conformant name and throws otherwise
# (dimForCollection, service/.../PgVectorRepository.java). Root-caused
# during live acceptance: the swallowed HTTP error surfaced as
# 'taxonomy_service_count_failed' -> silent "skipped" -> 0 topics. Fixed
# by using --all, the SAME enumeration path `store put`/`search` already
# resolve through (t3.list_collections(), which returns REAL physical
# names) -- sidesteps the bare-name pitfall entirely rather than trying
# to reproduce the promotion logic by hand in bash.
DISCOVER_OUT="$(nx taxonomy discover --all 2>&1 < /dev/null)"
printf '%s\n' "$DISCOVER_OUT" | sed 's/^/       /'
# Non-vacuity fix (live acceptance finding): the prior `case ... *"topics"*`
# match PASSED on "Total: 0 topics, 0 labeled." too -- the literal substring
# "topics" appears in that failure message. Parse the actual count instead
# and ABORT here, not three stages later at 3e's centroid/assignment
# asserts, when discovery genuinely produced nothing.
TOTAL_TOPICS="$(printf '%s' "$DISCOVER_OUT" | grep -oE 'Total: [0-9]+ topics' | grep -oE '[0-9]+' | head -1)"
if [ -n "${TOTAL_TOPICS:-}" ] && [ "$TOTAL_TOPICS" -ge 1 ] 2>/dev/null; then
  ok "taxonomy discover reported $TOTAL_TOPICS topic(s)"
else
  bad "taxonomy discover reported 0 (or unparseable) topics — every downstream taxonomy invariant would be vacuous"
  say "ABORT"; exit 1
fi

say "Stage 3e — capture pre-swap invariants"
CHUNKS_PRE="$(diag_sql "SELECT count(*) FROM nexus.chunks WHERE embedding_768 IS NOT NULL AND chunk_text <> ''")"
MANIFEST_PRE="$(diag_sql "SELECT count(*) FROM nexus.catalog_document_chunks")"
DOCS_PRE="$(diag_sql "SELECT count(*) FROM nexus.catalog_documents WHERE deleted_at IS NULL")"
CENTROIDS_PRE="$(diag_sql "SELECT count(*) FROM nexus.taxonomy_centroids")"
TOPIC_ASSIGN_PRE="$(diag_sql "SELECT count(*) FROM nexus.topic_assignments")"
note "chunks=$CHUNKS_PRE manifest_rows=$MANIFEST_PRE live_docs=$DOCS_PRE centroids=$CENTROIDS_PRE topic_assignments=$TOPIC_ASSIGN_PRE"
# Floor only, not exact: the 2 md docs each add >=1 chunk of their own
# (chunk count per doc depends on the chunker's own size/section
# behavior, not asserted here), so the true CHUNKS_PRE is always >= this
# floor, never under it.
if [ "${CHUNKS_PRE:-0}" -ge "$((SEED_N + TAXO_TOPICS * TAXO_PER_TOPIC))" ] 2>/dev/null; then
  ok "chunks: $CHUNKS_PRE content row(s) captured"
else
  bad "expected >= $((SEED_N + TAXO_TOPICS * TAXO_PER_TOPIC)) content rows, counted '$CHUNKS_PRE'"
fi
if [ "${DOCS_PRE:-0}" -ge "$((SEED_N + TAXO_TOPICS * TAXO_PER_TOPIC + 2))" ] 2>/dev/null; then
  ok "catalog: $DOCS_PRE live document(s) captured (notes + 2 md docs)"
else
  bad "expected >= $((SEED_N + TAXO_TOPICS * TAXO_PER_TOPIC + 2)) live documents, counted '$DOCS_PRE'"
fi
# HARD floor only (nexus-z0ylb: do not build a flaky multi-cluster gate on
# an un-rehearsed corpus size) — >=1 centroid proves discovery genuinely
# persisted rows; the topic count actually observed is reported above for
# a human reader, never asserted beyond this floor.
if [ "${CENTROIDS_PRE:-0}" -ge 1 ] 2>/dev/null; then
  ok "taxonomy: $CENTROIDS_PRE centroid row(s) — discovery persisted real topics"
else
  bad "expected >= 1 taxonomy centroid row after discovery, counted '$CENTROIDS_PRE'"
fi
# Same discover pass writes per-doc assignments (nexus.topic_assignments)
# alongside the centroids — captured here so the row-invariants-exact
# assert below covers the WHOLE taxonomy surface this leg populates, not
# just the centroid half of it.
if [ "${TOPIC_ASSIGN_PRE:-0}" -ge 1 ] 2>/dev/null; then
  ok "taxonomy: $TOPIC_ASSIGN_PRE assignment row(s) — discovery persisted per-doc topic links"
else
  bad "expected >= 1 topic_assignments row after discovery, counted '$TOPIC_ASSIGN_PRE'"
fi

say "Stage 3f — pre-swap read sanity"
PRE_SEARCH="$(nx search "$MARKER1" --corpus knowledge -m 3 2>&1)"
if printf '%s' "$PRE_SEARCH" | grep -q "candmigmarker1populate"; then
  ok "floor-engine search serves the seeded marker"
else
  printf '%s\n' "$PRE_SEARCH" | head -8 | sed 's/^/       /'
  bad "floor-engine search cannot find the seeded marker — fixture broken before the swap"
fi

# ── Stage 4: stop; hand-swap the CANDIDATE binary; harness bookkeeping ───
say "Stage 4 — nx daemon service stop (PG stays up: no --with-pg)"
if nx daemon service stop 2>&1 | tail -6 | sed 's/^/       /'; then
  ok "storage service stopped"
else
  bad "nx daemon service stop failed"; say "ABORT"; exit 1
fi

say "Stage 4 — hand-swap the locally-built CANDIDATE binary into the well-known location"
CAND_SHA="$(sha256sum "$SVC_NATIVE_DIR/nexus-service" | awk '{print $1}')"
mkdir -p "$SVC_WELL_KNOWN_DIR"
if cp "$SVC_NATIVE_DIR"/* "$SVC_WELL_KNOWN_DIR/" && chmod +x "$SVC_WELL_KNOWN_DIR/nexus-service"; then
  ok "candidate positioned at the well-known location"
else
  bad "positioning the candidate binary failed"; say "ABORT"; exit 1
fi
ON_DISK_SHA="$(sha256sum "$SVC_WELL_KNOWN_DIR/nexus-service" | awk '{print $1}')"
[ "$ON_DISK_SHA" = "$CAND_SHA" ] && ok "on-disk binary matches the staged candidate ($CAND_SHA)" \
  || bad "positioned binary sha ($ON_DISK_SHA) does not match the staged candidate ($CAND_SHA)"

say "Stage 4 — rewrite ONLY the provenance sidecar's sha256 (tag/version stay pinned at the floor)"
SIDECAR="$SVC_WELL_KNOWN_DIR/nexus-service.meta.json"
[ -f "$SIDECAR" ] || { bad "no provenance sidecar at $SIDECAR — Stage 2a's install-binary should have written one"; say "ABORT"; exit 1; }
SIDECAR_OUT="$(python3 - "$SIDECAR" "$CAND_SHA" <<'PY'
import json, sys
path, new_sha = sys.argv[1], sys.argv[2]
with open(path) as f:
    data = json.load(f)
old_sha = data.get("sha256")
old_tag = data.get("tag")
old_version = data.get("version")
data["sha256"] = new_sha
with open(path, "w") as f:
    json.dump(data, f, indent=2)
print(f"old_sha={old_sha} new_sha={new_sha} tag={old_tag} version={old_version}")
PY
)"
note "$SIDECAR_OUT"
SIDECAR_TAG="$(python3 -c "import json;print(json.load(open('$SIDECAR')).get('tag',''))" 2>/dev/null)"
SIDECAR_VER="$(python3 -c "import json;print(json.load(open('$SIDECAR')).get('version',''))" 2>/dev/null)"
SIDECAR_SHA="$(python3 -c "import json;print(json.load(open('$SIDECAR')).get('sha256',''))" 2>/dev/null)"
[ "$SIDECAR_TAG" = "$FLOOR_TAG" ] && ok "sidecar tag still names the floor ($SIDECAR_TAG) — harness bookkeeping stays internally consistent" \
  || bad "sidecar tag drifted to '$SIDECAR_TAG', expected $FLOOR_TAG"
[ "$SIDECAR_VER" = "$FLOOR_VERSION" ] && ok "sidecar version still $SIDECAR_VER (== floor)" \
  || bad "sidecar version drifted to '$SIDECAR_VER', expected $FLOOR_VERSION"
[ "$SIDECAR_SHA" = "$CAND_SHA" ] && ok "sidecar sha256 now backs the swapped-in candidate ($CAND_SHA)" \
  || bad "sidecar sha256 ($SIDECAR_SHA) does not match the candidate ($CAND_SHA)"

# ── Stage 5: start the CANDIDATE against the populated store ─────────────
say "Stage 5 — nx daemon service start (candidate boot; full Liquibase pass over populated data)"
START_OUT="$(nx daemon service start 2>&1 < /dev/null)"
printf '%s\n' "$START_OUT" | sed 's/^/       /'
if _wait_healthy 60; then ok "candidate healthy (Liquibase pass over populated data completed)"; else
  nx daemon service status 2>&1 | sed 's/^/       /' || true
  for f in "$HOME/.config/nexus/logs/storage_service.log" "$HOME/.config/nexus/logs/service_supervisor.log"; do
    [ -f "$f" ] && { note "---- $(basename "$f") (last 40 lines) ----"; tail -40 "$f" | sed 's/^/       /'; }
  done
  bad "candidate did not reach healthy over the populated store"; say "ABORT"; exit 1
fi
RV1="$(_release_version)"
[ "$RV1" = "$FLOOR_VERSION" ] && ok "/version release_version=$RV1 — the candidate self-reports the floor stamp" \
  || bad "/version release_version=$RV1, expected $FLOOR_VERSION — the RELEASE_PROPS stamp did not take"
POST_SHA="$(sha256sum "$SVC_WELL_KNOWN_DIR/nexus-service" | awk '{print $1}')"
[ "$POST_SHA" = "$CAND_SHA" ] && ok "post-start binary sha is UNCHANGED from the swapped-in candidate — nothing silently re-acquired it" \
  || bad "post-start binary sha ($POST_SHA) differs from the swapped-in candidate ($CAND_SHA) — something replaced it during start"

say "Assert — the harness's own hand-swap bookkeeping is self-consistent (full integrity check, dry-run, no mutation)"
# NOT a production converge-safety claim (substantive-critic finding,
# nexus-z0ylb): a real release install-binary's the tag and writes an
# honest sidecar at download time, so converge_engine's version+integrity
# check has nothing to reconcile there. This assert exists ONLY to prove
# that THIS TEST RUN's own sidecar-sha rewrite (Stage 4) is internally
# consistent enough that nothing INSIDE the rehearsal — a later `nx
# doctor` or `nx daemon restart-stale` fired by a subsequent assert in
# THIS script — silently re-acquires the floor tag over the candidate and
# invalidates the rest of the leg without saying so. `nx daemon
# restart-stale --dry-run` is the right probe because it runs the FULL
# converge_engine() path (verify_installed_binary's sha comparison
# included), unlike `nx doctor`'s convergence check which is version-only.
RESTALE_OUT="$(nx daemon restart-stale --dry-run 2>&1 < /dev/null)"
printf '%s\n' "$RESTALE_OUT" | sed 's/^/       /'
if printf '%s' "$RESTALE_OUT" | grep -qi "engine: converged"; then
  ok "harness bookkeeping is self-consistent — the sidecar sha matches the swapped-in candidate, so nothing later in this run would re-acquire the floor over it"
else
  bad "harness bookkeeping is NOT self-consistent after the hand-swap — the sidecar update is wrong or incomplete, and a later step in this same run (nx doctor / nx daemon restart-stale) would re-download and clobber the candidate mid-rehearsal"
fi

say "Assert — changeset delta (the candidate's own Liquibase contribution)"
CHANGESET_POST="$(diag_sql "SELECT count(*) FROM public.databasechangelog")"
DELTA=$((CHANGESET_POST - CHANGESET_PRE))
note "DATABASECHANGELOG rows: pre=$CHANGESET_PRE post=$CHANGESET_POST delta=$DELTA"
if [ "$DELTA" -lt 0 ]; then
  bad "changeset count DECREASED across the candidate boot (pre=$CHANGESET_PRE post=$CHANGESET_POST) — impossible under Liquibase's append-only ledger"
elif [ -n "${EXPECT_NEW_CHANGESETS:-}" ]; then
  [ "$DELTA" = "$EXPECT_NEW_CHANGESETS" ] \
    && ok "changeset delta=$DELTA matches EXPECT_NEW_CHANGESETS=$EXPECT_NEW_CHANGESETS" \
    || bad "changeset delta=$DELTA does not match EXPECT_NEW_CHANGESETS=$EXPECT_NEW_CHANGESETS"
elif [ "$DELTA" = 0 ]; then
  ok "changeset delta=0 — the candidate carries no new changesets over the floor on this tree (still proves boot-over-populated-store + checksum stability + grants idempotence)"
else
  ok "changeset delta=$DELTA new changeset(s) applied cleanly over the populated store (EXPECT_NEW_CHANGESETS unset — reported, not asserted to an exact value)"
fi

say "Assert — row invariants EXACT across the candidate boot"
CHUNKS_POST="$(diag_sql "SELECT count(*) FROM nexus.chunks WHERE embedding_768 IS NOT NULL AND chunk_text <> ''")"
MANIFEST_POST="$(diag_sql "SELECT count(*) FROM nexus.catalog_document_chunks")"
DOCS_POST="$(diag_sql "SELECT count(*) FROM nexus.catalog_documents WHERE deleted_at IS NULL")"
CENTROIDS_POST="$(diag_sql "SELECT count(*) FROM nexus.taxonomy_centroids")"
TOPIC_ASSIGN_POST="$(diag_sql "SELECT count(*) FROM nexus.topic_assignments")"
[ "$CHUNKS_POST" = "$CHUNKS_PRE" ] && ok "chunks: $CHUNKS_POST unchanged" || bad "chunks changed: pre=$CHUNKS_PRE post=$CHUNKS_POST"
[ "$MANIFEST_POST" = "$MANIFEST_PRE" ] && ok "catalog manifest rows: $MANIFEST_POST unchanged" || bad "manifest rows changed: pre=$MANIFEST_PRE post=$MANIFEST_POST"
[ "$DOCS_POST" = "$DOCS_PRE" ] && ok "live catalog documents: $DOCS_POST unchanged" || bad "live documents changed: pre=$DOCS_PRE post=$DOCS_POST"
[ "$CENTROIDS_POST" = "$CENTROIDS_PRE" ] && ok "taxonomy centroids: $CENTROIDS_POST unchanged" || bad "taxonomy centroids changed: pre=$CENTROIDS_PRE post=$CENTROIDS_POST"
[ "$TOPIC_ASSIGN_POST" = "$TOPIC_ASSIGN_PRE" ] && ok "topic assignments: $TOPIC_ASSIGN_POST unchanged" || bad "topic assignments changed: pre=$TOPIC_ASSIGN_PRE post=$TOPIC_ASSIGN_POST"

say "Assert — serve: reads, writes, search all live over the candidate"
POST_SEARCH="$(nx search "$MARKER1" --corpus knowledge -m 3 2>&1)"
if printf '%s' "$POST_SEARCH" | grep -q "candmigmarker1populate"; then
  ok "the pre-swap marker is still searchable through the candidate"
else
  printf '%s\n' "$POST_SEARCH" | head -8 | sed 's/^/       /'
  bad "the pre-swap marker is not searchable through the candidate"
fi
POST_MARKER="candmigpoststart$$"
POST_PUT="$(printf 'post-candidate-boot write — %s body' "$POST_MARKER" \
  | nx store put - --title "candmig-post-start" --collection knowledge__candmig 2>&1)"
if [ $? = 0 ]; then
  ok "a NEW nx store put succeeds through the candidate"
else
  printf '%s\n' "$POST_PUT" | tail -10 | sed 's/^/       /'
  bad "nx store put FAILED through the candidate"
fi

say "Assert — nx doctor: clean, no pending rungs, no engine-convergence-pending"
DOC_OUT="$(nx doctor 2>&1 < /dev/null)"
printf '%s\n' "$DOC_OUT" | grep -iE 'upgrade ladder|engine convergence' | sed 's/^/       /' || true
if printf '%s' "$DOC_OUT" | grep -q "Traceback"; then
  bad "nx doctor raised a traceback"
else
  ok "nx doctor runs traceback-free"
fi
if printf '%s' "$DOC_OUT" | grep -qi "pending upgrade rung"; then
  bad "nx doctor reports pending upgrade rung(s) after the candidate boot"
else
  ok "no pending upgrade rungs"
fi
if printf '%s' "$DOC_OUT" | grep -qi "engine convergence pending"; then
  bad "nx doctor reports engine convergence pending — the swap did not settle cleanly"
else
  ok "no engine convergence pending — the hand-swap settled cleanly"
fi

say "RESULT"
if [ "$FAILS" -eq 0 ]; then
  printf '\033[32mCANDIDATE-MIGRATION REHEARSAL PASSED\033[0m — floor %s store (chunks=%s, manifests=%s, docs=%s, centroids=%s, topic_assignments=%s) -> candidate boot, changeset delta=%s, invariants EXACT\n' \
    "$FLOOR_TAG" "$CHUNKS_PRE" "$MANIFEST_PRE" "$DOCS_PRE" "$CENTROIDS_PRE" "$TOPIC_ASSIGN_PRE" "$DELTA"
  exit 0
else
  printf '\033[31mCANDIDATE-MIGRATION REHEARSAL FAILED\033[0m — %d check(s) failed\n' "$FAILS"
  exit 1
fi
