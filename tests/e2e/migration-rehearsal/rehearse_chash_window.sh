#!/usr/bin/env bash
# nexus-p78a0 (RDR-180, critic-180-cohort finding 6) — the CHASH-WINDOW
# rehearsal. Runs INSIDE the container.
#
# The window under test: a pre-cutover (legacy 32-hex chash) store whose
# ENGINE has been swapped to the RDR-180 cohort (Liquibase rdr180-001/002
# converts the chash columns to bytea at boot) while the client's
# chash-rekey rung has NOT yet run. In production this window opens when
# the CLI auto-converge (check_version_transition -> converge_engine)
# installs the cohort engine on first invocation after a package upgrade;
# `nx upgrade` closes it. The critique's claim, rehearsed end to end:
#
#   1. the window is LOUD  — pending_data_rung_callout surfaces
#      "chash-rekey PENDING" in the [upgrade-finish] summary line;
#   2. the window is SAFE  — legacy rows stay intact and readable
#      (search serves seeded content), STRICT new writes succeed
#      (64-hex -> 32 bytes passes the NOT VALID octet CHECK), and a
#      legacy 32-hex citation is DANGLING-CLEAN (None, never a crash);
#   3. the window CLOSES   — `nx upgrade` runs freeze -> rekey ->
#      VALIDATE -> re-provision; afterwards every content row keys by
#      its digest, the five octet CHECKs are convalidated, and citations
#      resolve at BOTH widths (64 direct, 32 via the chash_alias route).
#
#   uv tool install conexus==$OLD_RELEASE   the last pre-cohort release,
#                                            from real PyPI. A TOOL install
#                                            deliberately (not a venv):
#                                            running_from_tool_install()
#                                            gates the auto-transition
#                                            finish pass, which is the
#                                            exact surface the callout
#                                            assert exercises.
#   nx daemon service install-binary +      the last pre-cohort engine
#   nx init --service                        ($OLD_ENGINE_TAG): TEXT-era
#                                            chash schema, provenance
#                                            sidecar written at the FLOOR
#                                            version.
#   nx store put xN                         the OLD producer seeds real
#                                            legacy 32-hex chashes.
#   <binary swap>                           THE ONLY ENGINE SUPPLY IN THIS
#                                            HARNESS, deliberate: the
#                                            cohort tag is UNPUBLISHED
#                                            pre-cutover, so converge's
#                                            download path cannot deliver
#                                            it (that path is
#                                            --package-upgrade's claim).
#                                            The provenance sidecar is
#                                            left untouched at the floor
#                                            == $OLD_ENGINE_TAG, so
#                                            converge_engine must NO-OP
#                                            over the swap — asserted by
#                                            sha256 after the transition.
#   uv tool install --force <wheel>         the RDR-180-aware client.
#   nx daemon service start                 FIRST new-client invocation:
#                                            the transition fires, the
#                                            callout must surface; boot
#                                            runs the Liquibase bytea
#                                            conversion.
#   nx upgrade                              closes the window.
set -uo pipefail

OLD_RELEASE="${OLD_RELEASE:?OLD_RELEASE must be set (e.g. 6.13.1)}"
OLD_ENGINE_TAG="${OLD_ENGINE_TAG:?OLD_ENGINE_TAG must be set (e.g. engine-service-v0.1.47)}"
FLOOR_VERSION="${FLOOR_VERSION:?FLOOR_VERSION must be set (e.g. 0.1.47)}"
OLD_EXPECT="${OLD_ENGINE_TAG#engine-service-v}"
SEED_N="${SEED_N:-6}"
FAILS=0

say()  { printf '\n\033[1m== %s ==\033[0m\n' "$*"; }
ok()   { printf '  \033[32mPASS\033[0m %s\n' "$*"; }
bad()  { printf '  \033[31mFAIL\033[0m %s\n' "$*"; FAILS=$((FAILS+1)); }
note() { printf '       %s\n' "$*"; }

export NX_SERVICE_MAX_HEAP="${NX_SERVICE_MAX_HEAP:-1g}"
git config --global user.email "chash-window@nexus.local" >/dev/null 2>&1 || true
git config --global user.name  "nexus chash window"       >/dev/null 2>&1 || true

# ── Quarantine ───────────────────────────────────────────────────────────────
say "Quarantine — nothing pre-staged"
command -v initdb >/dev/null 2>&1 && bad "system PostgreSQL present — not a clean box" || ok "no system PostgreSQL (bundle must provide it)"
test ! -e "$HOME/.config/nexus/service/nexus-service" && ok "no native binary pre-staged" || bad "native binary already present"

WHEEL="$(ls "$HOME"/worktree-wheel/conexus-*.whl 2>/dev/null | head -1)"
[ -n "$WHEEL" ] || { bad "no worktree wheel in $HOME/worktree-wheel/"; say "ABORT"; exit 1; }
WHEEL_VER="$(basename "$WHEEL" | sed -E 's/^conexus-([0-9]+\.[0-9]+\.[0-9]+).*/\1/')"
# The transition (and with it the callout surface) fires only on a version
# CHANGE — equal versions would silently skip the exact assert this leg
# exists for, so refuse the vacuous fixture up front.
if [ "$WHEEL_VER" = "$OLD_RELEASE" ]; then
  bad "worktree wheel version ($WHEEL_VER) equals OLD_RELEASE — check_version_transition would never fire and the callout assert would be vacuous. Set NEXUS_CHASH_OLD_RELEASE to a different published release."
  say "ABORT"; exit 1
fi
ok "worktree wheel $WHEEL_VER vs old release $OLD_RELEASE — the transition will fire"

# ── Stage 1: the OLD release, as a uv TOOL install ───────────────────────────
say "Stage 1 — uv tool install conexus==$OLD_RELEASE (real PyPI, tool install)"
if uv tool install --python 3.12 "conexus==$OLD_RELEASE" 2>&1 | tail -4 | sed 's/^/       /'; then
  ok "tool-installed conexus==$OLD_RELEASE"
else
  bad "uv tool install conexus==$OLD_RELEASE failed"; say "ABORT"; exit 1
fi
GOT_VER="$(nx --version 2>&1 | grep -oE '[0-9]+\.[0-9]+\.[0-9]+' | head -1)"
[ "$GOT_VER" = "$OLD_RELEASE" ] && ok "nx --version reports $GOT_VER" \
  || bad "nx --version reports $GOT_VER, expected $OLD_RELEASE"
TOOLPY="$HOME/.local/share/uv/tools/conexus/bin/python"
if [ -x "$TOOLPY" ]; then
  ok "tool venv python at $TOOLPY (install root satisfies running_from_tool_install)"
else
  bad "no python at $TOOLPY — the transition gate would never fire"; say "ABORT"; exit 1
fi

# ── Stage 2: the OLD engine + provision ──────────────────────────────────────
# install-binary writes the provenance sidecar the convergence detector
# reads; the tag is the FLOOR tag, so after the wheel swap converge_engine
# sees installed == required and must not touch the (by then swapped)
# binary. run.sh guards OLD_ENGINE_TAG == floor before launching.
say "Stage 2a — pre-stage the pre-cohort engine + PG bundle ($OLD_ENGINE_TAG)"
unset NEXUS_SERVICE_TAG NX_SERVICE_TAG 2>/dev/null || true
if nx daemon service install-binary "$OLD_ENGINE_TAG" 2>&1 | tail -8 | sed 's/^/       /'; then
  ok "install-binary acquired + verified $OLD_ENGINE_TAG (provenance sidecar written)"
else
  bad "install-binary failed for $OLD_ENGINE_TAG"; say "ABORT"; exit 1
fi

say "Stage 2b — nx init --service (provision PG, fetch bge, serve)"
export NEXUS_SERVICE_TAG="$OLD_ENGINE_TAG"   # already installed: ensure-binary no-ops
if nx init --service --embedder bge-768 --yes 2>&1 | tail -15 | sed 's/^/       /'; then
  ok "nx init --service (provisioned PG + $OLD_ENGINE_TAG + started)"
else
  bad "nx init --service failed"; say "ABORT (provision failed)"; exit 1
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

if _wait_healthy 30; then ok "service healthy on the pre-cohort engine"; else
  nx daemon service status 2>&1 | sed 's/^/       /' || true
  bad "service did not reach healthy on $OLD_RELEASE"; say "ABORT"; exit 1
fi
RV0="$(_release_version)"
[ "$RV0" = "$OLD_EXPECT" ] && ok "/version release_version=$RV0 ($OLD_ENGINE_TAG)" \
  || bad "/version release_version=$RV0, expected $OLD_EXPECT — wrong starting engine"

# ── SQL probe plumbing (nexus_diag: BYPASSRLS + SELECT on schema nexus) ──────
# Direct psql, NOT the product's diag choke point: these are harness
# measurements (content-reading probes the read-only-metadata lint would
# rightly refuse the product itself).
# tail -1: discover_pg_binaries logs a structlog debug line to stdout
# before the path prints — the path is always the LAST line.
PSQL_BIN="$("$TOOLPY" -c 'from nexus.db.pg_provision import discover_pg_binaries; print(discover_pg_binaries().psql)' 2>/dev/null | tail -1)"
[ -x "$PSQL_BIN" ] || { bad "cannot resolve the bundled psql (got: '$PSQL_BIN')"; say "ABORT"; exit 1; }
diag_sql() {
  PGPASSWORD="$NX_DB_DIAG_PASS" "$PSQL_BIN" -h 127.0.0.1 -p "$PG_PORT" \
    -U "$NX_DB_DIAG_USER" -d nexus -tA -c "$1" 2>&1
}

# ── ERA GUARD: the old engine must really be TEXT-era ────────────────────────
# The package-upgrade/era-hop staleness-guard morphology, enforced against
# the REAL store instead of tag arithmetic: once the cohort ships as the
# pinned engine, this leg's "old era" would already be bytea and every
# window assert below would grade a store that never had a window. At that
# point the leg needs a redesign (OLD_* moves to the last pre-cohort pair,
# and the swap step can retire in favour of converge's real download path).
say "Era guard — the pre-cohort store must be TEXT-era"
COLTYPE="$(diag_sql "SELECT data_type FROM information_schema.columns WHERE table_schema='nexus' AND table_name='chunks_768' AND column_name='chash'")"
if [ "$COLTYPE" = "bytea" ]; then
  bad "chunks_768.chash is ALREADY bytea under $OLD_ENGINE_TAG — there is no window to rehearse (post-cutover: repoint NEXUS_CHASH_OLD_RELEASE/NEXUS_CHASH_OLD_ENGINE_TAG at the last pre-cohort pair)"
  say "ABORT (vacuous fixture)"; exit 1
fi
ok "chunks_768.chash is '$COLTYPE' — genuinely pre-cohort"

# ── Stage 3: seed legacy content via the OLD producer ────────────────────────
say "Stage 3 — seed $SEED_N notes with the old client (legacy 32-hex chashes)"
MARKER1="quokkamarker1window"
for i in $(seq 1 "$SEED_N"); do
  printf 'chash window rehearsal note %s — quokkamarker%swindow content body' "$i" "$i" \
    | nx store put - --title "window-note-$i" --collection knowledge >/dev/null 2>&1 \
    || bad "nx store put failed for note $i"
done
PRE_COUNT="$(diag_sql "SELECT count(*) FROM nexus.chunks_768 WHERE chunk_text <> ''")"
if [ "${PRE_COUNT:-0}" -ge "$SEED_N" ] 2>/dev/null; then
  ok "seeded: $PRE_COUNT content row(s) in chunks_768"
else
  bad "expected >= $SEED_N content rows, counted '$PRE_COUNT'"; say "ABORT"; exit 1
fi
# Non-vacuity gate A: every seeded key must be a LEGACY 32-hex TEXT value,
# or the conversion/rekey asserts below grade already-conformant rows.
NON_LEGACY="$(diag_sql "SELECT count(*) FROM nexus.chunks_768 WHERE chash !~ '^[0-9a-f]{32}\$'")"
if [ "$NON_LEGACY" = "0" ]; then
  ok "every chash is a legacy 32-hex value — the era is genuine"
else
  bad "$NON_LEGACY row(s) not 32-hex under the OLD producer — the fixture is not legacy-shaped"; say "ABORT"; exit 1
fi
ROW="$(diag_sql "SELECT chash || ' ' || encode(sha256(convert_to(chunk_text,'UTF8')),'hex') FROM nexus.chunks_768 WHERE chunk_text <> '' ORDER BY chash LIMIT 1")"
LEGACY_CHASH="${ROW%% *}"; CANON="${ROW##* }"
if [ "${#LEGACY_CHASH}" = 32 ] && [ "${#CANON}" = 64 ]; then
  ok "captured citation pair: legacy=$LEGACY_CHASH canonical=${CANON:0:16}…"
else
  bad "could not capture a (legacy, canonical) pair (got: '$ROW')"; say "ABORT"; exit 1
fi
case "$CANON" in
  "$LEGACY_CHASH"*) ok "legacy key is the canonical's 32-hex prefix (the [:32] truncation lineage)" ;;
  *) bad "legacy key is NOT a prefix of sha256(chunk_text) — the seed is not the [:32] era shape" ;;
esac
MANIFEST_PRE="$(diag_sql "SELECT count(*) FROM nexus.catalog_document_chunks")"
note "catalog manifest rows pre-swap: $MANIFEST_PRE"

# Old-era read sanity (so a window read failure is attributable to the swap).
OLD_SEARCH="$(nx search "$MARKER1" --corpus knowledge -m 3 2>&1)"
if printf '%s' "$OLD_SEARCH" | grep -q "quokkamarker1window"; then
  ok "old-era search serves the seeded content"
else
  printf '%s\n' "$OLD_SEARCH" | head -8 | sed 's/^/       /'
  bad "old-era search cannot find the seeded marker — fixture broken before the swap"
fi

# ── Stage 4: stop, swap the ENGINE, swap the CLIENT ──────────────────────────
say "Stage 4 — stop service; swap in the cohort engine (the ONLY harness engine supply)"
nx daemon service stop 2>&1 | tail -3 | sed 's/^/       /' || true
SVCDIR="$HOME/.config/nexus/service"
OLD_SHA="$(sha256sum "$SVCDIR/nexus-service" | awk '{print $1}')"
cp /home/nexus/native/nexus-service "$SVCDIR/nexus-service"
chmod +x "$SVCDIR/nexus-service"
if compgen -G "/home/nexus/native/*.so" > /dev/null; then
  cp /home/nexus/native/*.so "$SVCDIR/"   # local -Ob build dlopen's JDK libs from its own dir
fi
COHORT_SHA="$(sha256sum "$SVCDIR/nexus-service" | awk '{print $1}')"
[ "$OLD_SHA" != "$COHORT_SHA" ] \
  && ok "cohort binary swapped in (sha ${COHORT_SHA:0:12}…, was ${OLD_SHA:0:12}…)" \
  || bad "binary sha unchanged after the swap — the cohort binary did not land"
note "provenance sidecar left at $OLD_ENGINE_TAG (== floor): converge_engine must no-op"

say "Stage 5 — package swap to the working tree (uv tool install --force)"
if uv tool install --python 3.12 --force "$WHEEL" 2>&1 | tail -4 | sed 's/^/       /'; then
  ok "tool reinstalled from the worktree wheel"
else
  bad "uv tool install --force failed"; say "ABORT"; exit 1
fi

# ── Stage 6: FIRST new-client invocation — the transition + the callout ──────
# `nx daemon service start` is deliberately the first invocation: the root
# CLI group runs check_version_transition BEFORE the subcommand, so the
# [upgrade-finish] summary (converge no-op + pending_data_rung_callout)
# lands in THIS capture, with the cohort binary on disk — the same summary
# a production box prints when auto-converge opens the window for real.
say "Stage 6 — nx daemon service start (transition fires; boot converts to bytea)"
START_OUT="$(nx daemon service start 2>&1 < /dev/null)"
printf '%s\n' "$START_OUT" | sed 's/^/       /'
if printf '%s' "$START_OUT" | grep -q "\[upgrade-finish\]"; then
  ok "the version transition fired on the first new-client invocation"
else
  bad "no [upgrade-finish] summary — the transition did not fire (tool-install gate? version stamp?)"
fi
if printf '%s' "$START_OUT" | grep -q "chash-rekey PENDING"; then
  ok "THE CALLOUT: 'chash-rekey PENDING' surfaced in the transition summary (critic finding 2)"
else
  bad "the transition summary does NOT carry the chash-rekey PENDING callout — the window is silent"
fi
GOT_VER2="$(nx --version 2>&1 | grep -oE '[0-9]+\.[0-9]+\.[0-9]+' | head -1)"
[ "$GOT_VER2" = "$WHEEL_VER" ] && ok "client is now the worktree build ($GOT_VER2)" \
  || bad "client reports $GOT_VER2, expected $WHEEL_VER"
POST_TRANSITION_SHA="$(sha256sum "$SVCDIR/nexus-service" | awk '{print $1}')"
[ "$POST_TRANSITION_SHA" = "$COHORT_SHA" ] \
  && ok "converge_engine left the swapped binary alone (floor satisfied by the sidecar)" \
  || bad "the transition REPLACED the cohort binary (sha ${POST_TRANSITION_SHA:0:12}…) — the floor/sidecar premise broke and the window collapsed"

if _wait_healthy 60; then ok "cohort engine healthy (Liquibase conversion completed at boot)"; else
  nx daemon service status 2>&1 | sed 's/^/       /' || true
  bad "cohort engine did not reach healthy — Liquibase conversion failed?"; say "ABORT"; exit 1
fi
RV1="$(_release_version)"
[ "$RV1" = "$FLOOR_VERSION" ] && ok "/version release_version=$RV1 (the stamped cohort build)" \
  || note "/version release_version=$RV1 (informational — identity is proven by sha + schema, not the stamp)"

# ── THE WINDOW: engine converted, rung not run ───────────────────────────────
say "Window assert — the store converted to bytea, legacy rows INTACT"
COLTYPE2="$(diag_sql "SELECT data_type FROM information_schema.columns WHERE table_schema='nexus' AND table_name='chunks_768' AND column_name='chash'")"
[ "$COLTYPE2" = "bytea" ] && ok "chunks_768.chash is bytea — rdr180-001 really ran" \
  || { bad "chunks_768.chash is '$COLTYPE2', expected bytea — the cohort boot did not convert"; say "ABORT"; exit 1; }
LEGACY_ROWS="$(diag_sql "SELECT count(*) FROM nexus.chunks_768 WHERE chunk_text <> '' AND octet_length(chash) = 16")"
[ "$LEGACY_ROWS" = "$PRE_COUNT" ] \
  && ok "all $PRE_COUNT legacy rows present as 16-byte keys (hex-decoded, nothing lost, nothing rekeyed yet)" \
  || bad "expected $PRE_COUNT 16-byte legacy rows, counted '$LEGACY_ROWS'"

say "Window assert — reads stay sane (search serves pre-existing content)"
WIN_SEARCH="$(nx search "$MARKER1" --corpus knowledge -m 3 2>&1)"
if printf '%s' "$WIN_SEARCH" | grep -q "quokkamarker1window"; then
  ok "search still serves the legacy-keyed content through the cohort engine"
else
  printf '%s\n' "$WIN_SEARCH" | head -10 | sed 's/^/       /'
  bad "search cannot find pre-existing content in the window"
fi

say "Window assert — STRICT writes succeed (64-hex -> 32 bytes, NOT VALID check enforced)"
WINDOW_MARKER="quokkawindowwrite$$"
WIN_PUT="$(printf 'strict window write — %s body' "$WINDOW_MARKER" \
  | nx store put - --title "window-strict-write" --collection knowledge 2>&1)"
if [ $? = 0 ]; then
  ok "nx store put succeeded in the window"
else
  printf '%s\n' "$WIN_PUT" | tail -10 | sed 's/^/       /'
  bad "nx store put FAILED in the window — strict writes are broken"
fi
STRICT_ROWS="$(diag_sql "SELECT count(*) FROM nexus.chunks_768 WHERE octet_length(chash) = 32")"
if [ "${STRICT_ROWS:-0}" -ge 1 ] 2>/dev/null; then
  ok "$STRICT_ROWS row(s) keyed at 32 bytes — the new write passed the octet CHECK"
else
  bad "no 32-byte rows after the strict write (counted '$STRICT_ROWS')"
fi
WIN_SEARCH2="$(nx search "$WINDOW_MARKER" --corpus knowledge -m 3 2>&1)"
if printf '%s' "$WIN_SEARCH2" | grep -q "$WINDOW_MARKER"; then
  ok "the window write is immediately searchable"
else
  printf '%s\n' "$WIN_SEARCH2" | head -8 | sed 's/^/       /'
  bad "the window write is not searchable"
fi
# Engine-side evidence for any window failure above: the service log tails
# (the raw driver message behind a typed 409 goes to the server log only).
note "logs dir: $(ls "$HOME/.config/nexus/logs/" 2>/dev/null | tr '\n' ' ')"
for lf in "$HOME"/.config/nexus/logs/*.log; do
  [ -f "$lf" ] || continue
  note "── tail $(basename "$lf"):"
  grep -iE "constraint|violat|error|warn|exception" "$lf" 2>/dev/null | tail -12 | sed 's/^/       /'
done

say "Window assert — legacy 32-hex citation is DANGLING-CLEAN (alias empty, no crash)"
if "$TOOLPY" - "$LEGACY_CHASH" <<'PY'; then ok "legacy citation resolved to None cleanly (unmapped = dangling, not an error)"; else bad "legacy citation resolution in the window crashed or resolved unexpectedly"; fi
import sys
legacy = sys.argv[1]
from nexus.catalog.catalog_spans import resolve_chash_globally
from nexus.db import make_t3
from nexus.db.t2.http_chash_index import HttpChashIndex
ref = resolve_chash_globally(f"chash:{legacy}", make_t3(), HttpChashIndex())
if ref is not None:
    print(f"       unexpectedly RESOLVED in the window: {ref.get('chunk_hash')}")
    sys.exit(1)
print("       None — dangling-clean, exactly the pre-rekey contract")
sys.exit(0)
PY
# Canonical-width resolution in the window is OBSERVATIONAL: the chash_index
# misses (its keys are still 16-byte legacy) but the metadata fallback scan
# may still serve it. Either outcome is window-legal; a CRASH is not.
if OBS="$("$TOOLPY" - "$CANON" <<'PY' 2>&1
import sys
canon = sys.argv[1]
from nexus.catalog.catalog_spans import resolve_chash_globally
from nexus.db import make_t3
from nexus.db.t2.http_chash_index import HttpChashIndex
ref = resolve_chash_globally(f"chash:{canon}", make_t3(), HttpChashIndex())
print("resolved" if ref is not None else "unresolved")
PY
)"; then
  ok "canonical citation in the window: no crash (observed: $(printf '%s' "$OBS" | tail -1))"
else
  bad "canonical citation resolution CRASHED in the window: $OBS"
fi

# ── NON-VACUITY GATE: the product itself must SEE the pending rung ───────────
say "Non-vacuity gate — nx upgrade --dry-run must report the chash-rekey rung pending"
DRY_OUT="$(nx upgrade --dry-run 2>&1 < /dev/null)"
printf '%s\n' "$DRY_OUT" | sed 's/^/       /'
if printf '%s' "$DRY_OUT" | grep -q "chash-rekey" && printf '%s' "$DRY_OUT" | grep -qi "pending"; then
  ok "the product reports chash-rekey pending — there is real work to converge"
else
  bad "nx upgrade --dry-run does not report chash-rekey pending — every convergence assert below would grade a no-op"
  say "ABORT (vacuous fixture)"; exit 1
fi

# ── Stage 7: CLOSE the window — nx upgrade ───────────────────────────────────
say "Stage 7 — nx upgrade (freeze -> rekey -> VALIDATE -> re-provision; unattended)"
UP_OUT="$(nx upgrade 2>&1 < /dev/null)"
UP_RC=$?
printf '%s\n' "$UP_OUT" | sed 's/^/       /'
[ "$UP_RC" = 0 ] && ok "nx upgrade exited 0" || bad "nx upgrade exited $UP_RC"
# The success path echoes only "rung '<name>' converged and verified." —
# the counts envelope lives in the structured log, so the count-shaped
# evidence below is drawn from SQL instead.
printf '%s' "$UP_OUT" | grep -q "rung 'chash-rekey' converged" \
  && ok "the chash-rekey rung converged and verified" \
  || bad "no chash-rekey convergence line in the walk output"

# ── Assert: every content row keys by its digest; checks VALIDATEd ───────────
say "Assert — full-digest keys everywhere, five octet CHECKs convalidated"
for T in chunks_384 chunks_768 chunks_1024; do
  MISMATCH="$(diag_sql "SELECT count(*) FROM nexus.$T WHERE chunk_text <> '' AND chash IS DISTINCT FROM sha256(convert_to(chunk_text,'UTF8'))")"
  [ "$MISMATCH" = "0" ] && ok "$T: zero digest-mismatched content rows" \
    || bad "$T: $MISMATCH content row(s) NOT keyed by sha256(chunk_text)"
  WIDTH_BAD="$(diag_sql "SELECT count(*) FROM nexus.$T WHERE octet_length(chash) <> 32")"
  [ "$WIDTH_BAD" = "0" ] && ok "$T: every key is exactly 32 bytes" \
    || bad "$T: $WIDTH_BAD row(s) not 32 bytes wide"
done
# Expected = the seeded legacy rows + the ONE strict window write (which
# succeeds since the store-put full-digest fix — it is a 32-byte row the
# rekey leaves untouched). Distinct texts, so no collapse.
WANT_COUNT=$((PRE_COUNT + 1))
POST_COUNT="$(diag_sql "SELECT count(*) FROM nexus.chunks_768 WHERE chunk_text <> ''")"
[ "$POST_COUNT" = "$WANT_COUNT" ] \
  && ok "content row count preserved through the rekey ($PRE_COUNT legacy + 1 window write = $POST_COUNT)" \
  || bad "content rows changed across the rekey: expected $WANT_COUNT ($PRE_COUNT legacy + 1 window write), counted $POST_COUNT"
VALIDATED="$(diag_sql "SELECT count(*) FROM pg_constraint WHERE conname IN ('chunks_384_chash_octet_check','chunks_768_chash_octet_check','chunks_1024_chash_octet_check','catalog_document_chunks_chash_octet_check','chash_index_chash_octet_check') AND convalidated")"
[ "$VALIDATED" = "5" ] \
  && ok "all five octet CHECKs are convalidated (the rung's admin VALIDATE ran)" \
  || bad "only $VALIDATED/5 octet CHECKs convalidated"

say "Assert — the alias map exists and the cascade left no dangling pointer"
ALIAS_ROWS="$(diag_sql "SELECT count(*) FROM nexus.chash_alias")"
if [ "${ALIAS_ROWS:-0}" -ge "$PRE_COUNT" ] 2>/dev/null; then
  ok "chash_alias holds $ALIAS_ROWS row(s) (>= $PRE_COUNT rekeyed legacy keys)"
else
  bad "chash_alias holds '$ALIAS_ROWS' row(s), expected >= $PRE_COUNT"
fi
ALIAS_HIT="$(diag_sql "SELECT count(*) FROM nexus.chash_alias WHERE old_ref = '$LEGACY_CHASH'")"
[ "$ALIAS_HIT" = "1" ] && ok "the captured legacy key has its alias row (old_ref recovered per the reversibility lemma)" \
  || bad "no alias row for old_ref=$LEGACY_CHASH (counted '$ALIAS_HIT')"
DANGLING="$(diag_sql "SELECT count(*) FROM nexus.catalog_document_chunks m WHERE NOT EXISTS (SELECT 1 FROM nexus.chunks_384 c WHERE c.chash = m.chash) AND NOT EXISTS (SELECT 1 FROM nexus.chunks_768 c WHERE c.chash = m.chash) AND NOT EXISTS (SELECT 1 FROM nexus.chunks_1024 c WHERE c.chash = m.chash)")"
[ "$DANGLING" = "0" ] && ok "catalog manifest: zero dangling chash pointers post-cascade" \
  || bad "catalog manifest holds $DANGLING dangling pointer(s) after the cascade"

# ── Assert: citations resolve at BOTH widths ─────────────────────────────────
say "Assert — citations resolve: 64-hex DIRECT, legacy 32-hex via the ALIAS route"
if "$TOOLPY" - "$CANON" "$LEGACY_CHASH" <<'PY'; then ok "both citation widths resolve to the digest-keyed chunk"; else bad "citation resolution failed post-rekey"; fi
import hashlib, sys
canon, legacy = sys.argv[1], sys.argv[2]
from nexus.catalog.catalog_spans import resolve_chash_globally
from nexus.db import make_t3
from nexus.db.t2.http_chash_index import HttpChashIndex
t3, idx = make_t3(), HttpChashIndex()
fails = 0

ref = resolve_chash_globally(f"chash:{canon}", t3, idx)
if ref is None:
    print("       64-hex citation did NOT resolve"); fails += 1
else:
    got = hashlib.sha256(ref["chunk_text"].encode()).hexdigest()
    if ref.get("chunk_hash") != canon:
        print(f"       64-hex resolved to wrong identity: {ref.get('chunk_hash')}"); fails += 1
    elif got != canon:
        print(f"       64-hex resolved text does not hash back to the citation ({got[:16]}…)"); fails += 1
    else:
        print(f"       64-hex direct: chunk text sha256 round-trips ({canon[:16]}…)")

ref = resolve_chash_globally(f"chash:{legacy}", t3, idx)
if ref is None:
    print("       legacy 32-hex citation did NOT resolve via the alias route"); fails += 1
elif ref.get("chunk_hash") != canon:
    print(f"       legacy citation rewrote to {ref.get('chunk_hash')}, expected the canonical {canon[:16]}…"); fails += 1
else:
    print("       legacy 32-hex: engine alias-chained, client rewrote to the canonical identity")

sys.exit(1 if fails else 0)
PY

# ── Assert: the freeze restored (writes + search live post-rekey) ────────────
say "Assert — post-rekey liveness (freeze restored, reads + writes serve)"
POST_MARKER="quokkapostrekey$$"
POST_PUT="$(printf 'post-rekey write — %s body' "$POST_MARKER" \
  | nx store put - --title "post-rekey-write" --collection knowledge 2>&1)"
if [ $? = 0 ]; then
  ok "post-rekey nx store put succeeded (writer freeze restored)"
else
  printf '%s\n' "$POST_PUT" | tail -8 | sed 's/^/       /'
  bad "post-rekey nx store put failed — the freeze did not restore"
fi
POST_SEARCH="$(nx search "$MARKER1" --corpus knowledge -m 3 2>&1)"
if printf '%s' "$POST_SEARCH" | grep -q "quokkamarker1window"; then
  ok "pre-existing content still serves after the rekey"
else
  printf '%s\n' "$POST_SEARCH" | head -8 | sed 's/^/       /'
  bad "pre-existing content lost after the rekey"
fi

# ── Idempotence + the quiet ladder ───────────────────────────────────────────
say "Idempotence — a SECOND nx upgrade must not re-run the rekey"
UP2_OUT="$(nx upgrade 2>&1 < /dev/null)"
UP2_RC=$?
[ "$UP2_RC" = 0 ] && ok "the second nx upgrade exited 0" || bad "the second nx upgrade exited $UP2_RC"
if printf '%s' "$UP2_OUT" | grep -q "rung 'chash-rekey' converged"; then
  bad "the second walk RE-CONVERGED chash-rekey — the completion ledger did not hold"
else
  ok "the second walk did not re-run the rekey"
fi

say "Assert — nx doctor reports a converged ladder, no lingering callout"
DOC_OUT="$(nx doctor 2>&1 < /dev/null)"
printf '%s\n' "$DOC_OUT" | grep -iE 'upgrade ladder|chash' | sed 's/^/       /' || true
if printf '%s' "$DOC_OUT" | grep -qiE 'pending upgrade rung'; then
  bad "nx doctor still reports pending rung(s) after the walk"
else
  ok "no pending rungs — the window is closed"
fi
if printf '%s' "$DOC_OUT" | grep -q "chash-rekey PENDING"; then
  bad "the chash-rekey PENDING callout survived a completed rekey"
else
  ok "the callout is gone once the rekey has run"
fi

# ── The NEXT transition must be quiet (no false PENDING after the rekey) ─────
# Simulates the next package upgrade on this box: reset the version stamp so
# check_version_transition fires once more on a CONVERGED store. The callout
# is only correct if it consults the completion ledger — a ledger-blind
# detect() sweep would warn "chash-rekey PENDING" forever, on every future
# upgrade, against a store that already rekeyed (the rung's own detect never
# self-reports converged; the ledger is the truth).
say "Future transition — the callout must NOT false-alarm on a converged store"
printf '0.0.0\n' > "$HOME/.config/nexus/last_seen_version"
NEXT_OUT="$(nx daemon service status 2>&1 < /dev/null)"
if printf '%s' "$NEXT_OUT" | grep -q "\[upgrade-finish\]"; then
  ok "the simulated next transition fired"
else
  bad "the simulated next transition did not fire — the false-alarm assert below is vacuous"
fi
if printf '%s' "$NEXT_OUT" | grep -q "chash-rekey PENDING"; then
  bad "FALSE ALARM: the transition summary claims 'chash-rekey PENDING' on a store that already rekeyed — the callout is ledger-blind"
else
  ok "no chash-rekey callout after the rekey — the callout respects completion"
fi

say "RESULT"
if [ "$FAILS" -eq 0 ]; then
  printf '\033[32mCHASH-WINDOW REHEARSAL PASSED\033[0m — conexus %s + %s -> cohort engine boot (LOUD + SAFE window) -> nx upgrade rekey (window CLOSED; citations resolve at both widths)\n' \
    "$OLD_RELEASE" "$OLD_ENGINE_TAG"
  exit 0
else
  printf '\033[31mCHASH-WINDOW REHEARSAL FAILED — %d check(s) failed\033[0m\n' "$FAILS"
  exit 1
fi
