#!/usr/bin/env bash
# nexus-8nlj4 — TWO-HOP STRANDED-REDIRECT rehearsal. Runs INSIDE the container.
# HOP 3 (nexus-4922x) extends it below: hops 1-2 alone left the redirect's
# own literal step 3 unverified, and it turned out to be unfollowable —
# see the HOP 3 comment ahead of Stage 11 for the full root-cause trace.
#
# HOP 1 (the detector fires): a box carrying real pre-PG artifacts (written
# by seed_legacy.py under the PIN release's own libraries, exactly as
# era-hop/fullstack already do — nexus-8nlj4 2026-08-08:
# confirmed live raw material) package-upgrades straight to the working
# tree. The FIRST invocation of the upgraded CLI must refuse LOUD with the
# exact two-hop message (`nexus.stranded_install.StrandedInstall.message`)
# — before any provisioning is attempted.
#
# HOP 2 (the redirect is followable): downgrading BACK to the exact pin the
# message names (`pip install conexus==$PIN_RELEASE` — literally the
# message's own instruction) against the SAME on-disk artifacts, then
# running `nx init` + `nx upgrade` there, must migrate the data for real —
# the RDR-185 ladder's substrate-etl rung converges and verifies.
#
# HOP 3 (nexus-4922x, the redirect's own literal step 3 — "upgrade back to
# this version"): package-upgrading from the pin BACK to the working tree a
# SECOND time (Stage 11, below) must leave `nx doctor` / `nx init` / plain
# CLI startup SILENT — no re-refusal. Found by substantive-critique of this
# very rehearsal (2026-08-08): the original hops 1-2 never actually
# exercised hop 3, and static analysis + the rehearsal's own observed
# doctor output ("Migration reports: no migrations recorded") showed the
# pre-nexus-4922x detector's only migrated-signal (a
# <config>/migration-reports/*.json format neither remedy path the message
# can name ever writes) would never be satisfied — an unfollowable infinite
# two-hop loop for a real user doing exactly what the message told them to
# do.
#
# NON-VACUITY (bead nexus-8nlj4's own acceptance language: "never assert
# against a disarmed detector"), three legs:
#   (a) POSITIVE — real artifacts + armed detector -> trips, exact message.
#   (b) NEGATIVE CONTROL A — a FRESH box (same current install, zero
#       artifacts) -> stays silent. Proves (a) is not vacuously true
#       regardless of state.
#   (c) NEGATIVE CONTROL B — the SAME real artifacts, detector DISARMED
#       in-process (LAST_MIGRATION_CAPABLE monkeypatched to None, exactly
#       what a migration-capable release ships) -> stays silent. Proves (a)
#       is keyed on the constant being armed, not merely on file presence.
#
# Judgment call (relay 2026-08-08, "full-migration-in-container may be
# heavy — state the choice honestly"): hop 2 runs the REAL `nx init` + real
# `nx upgrade` at the pin (not just a --help/existence check) because that
# is the redirect's actual, load-bearing claim, and the cost is the same
# order as --package-upgrade's existing Stage 2 (PG bundle + bge-768 ONNX
# download, no native build). It does NOT re-prove the full ladder walk
# from the OLDEST supported release (6.0.0, with pre-RDR-108 ids, cross-
# model remap, etc.) — that is --era-hop's job, already covered, and
# re-running it a second time here at the pin would be redundant cost with
# no new coverage.
set -uo pipefail

PIN_RELEASE="${PIN_RELEASE:?PIN_RELEASE must be set (e.g. 6.18.1 — see the STRAND_PIN_RELEASE derivation in run.sh)}"
CHROMA_LOCAL="${CHROMA_LOCAL:-/home/nexus/legacy-chroma}"
FRESH_CHROMA="/home/nexus/fresh-chroma"
FRESH_CONFIG="/home/nexus/fresh-config"
SEED_N="${SEED_N:-3}"
FAILS=0

say()  { printf '\n\033[1m== %s ==\033[0m\n' "$*"; }
ok()   { printf '  \033[32mPASS\033[0m %s\n' "$*"; }
bad()  { printf '  \033[31mFAIL\033[0m %s\n' "$*"; FAILS=$((FAILS+1)); }
note() { printf '       %s\n' "$*"; }

export NX_SERVICE_MAX_HEAP="${NX_SERVICE_MAX_HEAP:-1g}"
git config --global user.email "stranded@nexus.local" >/dev/null 2>&1 || true
git config --global user.name  "nexus stranded-redirect" >/dev/null 2>&1 || true

# THE SEEDED STORE MUST BE AT THE PATH THE DETECTOR PROBES (same lesson as
# era-hop: NX_LOCAL_CHROMA_PATH -> $XDG_DATA_HOME/nexus/chroma default).
export NX_LOCAL_CHROMA_PATH="$CHROMA_LOCAL"

_wait_healthy() {
  local tries="${1:-30}" i
  for i in $(seq 1 "$tries"); do
    if nx daemon service status 2>&1 | grep -qiE "health.*ok|healthy|serving|status.*ok|running"; then
      return 0
    fi
    sleep 2
  done
  return 1
}

# ── Quarantine ───────────────────────────────────────────────────────────────
say "Quarantine — nothing pre-staged"
command -v initdb >/dev/null 2>&1 && bad "system PostgreSQL present — not a clean box" || ok "no system PostgreSQL (bundle must provide it)"
test ! -e "$HOME/.config/nexus/service/nexus-service" && ok "no native binary pre-staged" || bad "native binary already present"

# ── Stage 1: install the PIN release from real PyPI ───────────────────────────
say "Stage 1 — pip install conexus==$PIN_RELEASE (real PyPI, the redirect's pin)"
if uv pip install --python "$HOME/nxenv" "conexus==$PIN_RELEASE" 2>&1 | tail -5 | sed 's/^/       /'; then
  ok "installed conexus==$PIN_RELEASE"
else
  bad "pip install conexus==$PIN_RELEASE failed"; say "ABORT"; exit 1
fi
GOT_VER="$(nx --version 2>&1 | grep -oE '[0-9]+\.[0-9]+\.[0-9]+' | head -1)"
[ "$GOT_VER" = "$PIN_RELEASE" ] && ok "nx --version reports $GOT_VER" \
  || bad "nx --version reports $GOT_VER, expected $PIN_RELEASE"

# ── Stage 2: seed real pre-PG artifacts under the pin release's own libs ──────
say "Stage 2 — seed_legacy.py: real Chroma + T2 + catalog artifacts (under conexus $PIN_RELEASE)"
if SEED_RAW="$(python3 /home/nexus/seed_legacy.py "$CHROMA_LOCAL" --n "$SEED_N" 2>&1)"; then
  ok "seeded legacy artifacts: $(printf '%s' "$SEED_RAW" | tail -1)"
else
  bad "seed_legacy.py failed: $SEED_RAW"; say "ABORT"; exit 1
fi
for f in "$CHROMA_LOCAL/chroma.sqlite3" "$HOME/.config/nexus/memory.db" "$HOME/.config/nexus/catalog/.catalog.db"; do
  test -f "$f" && ok "artifact present: $f" || bad "expected artifact missing: $f"
done

# ── Stage 3: fresh-box negative-control fixtures (empty, never seeded) ────────
# Prepared NOW, before anything mutates the seeded dirs, so control (b) below
# probes a box that never had pre-PG data at all — not a data-shape overlap
# with the positive case.
mkdir -p "$FRESH_CHROMA" "$FRESH_CONFIG"

# ── Stage 4: PACKAGE upgrade ONLY — straight to the working tree ──────────────
say "Stage 4 — package-upgrade to the working tree (uv pip install --reinstall <worktree wheel>)"
WHEEL="$(ls "$HOME"/worktree-wheel/conexus-*.whl 2>/dev/null | head -1)"
if [ -z "$WHEEL" ]; then
  bad "no worktree wheel found in $HOME/worktree-wheel/"; say "ABORT"; exit 1
fi
if uv pip install --python "$HOME/nxenv" --reinstall "$WHEEL" 2>&1 | tail -5 | sed 's/^/       /'; then
  ok "package upgraded to the working-tree build ($WHEEL)"
else
  bad "package upgrade failed"; say "ABORT"; exit 1
fi

# ── Stage 5 — HOP 1, control (a): the FIRST invocation post-upgrade must trip,
#    LOUD, with the exact two-hop message, BEFORE any provisioning is
#    attempted. `nx doctor` is used FIRST (not `nx --version`) because the
#    CLOBBER GUARD documented in stranded_install.py rewrites the
#    `last_seen_version` stamp to the running version on the first
#    post-upgrade invocation — a prior invocation here would silently
#    degrade the era clause to the fallback ("an earlier, pre-PG conexus
#    release") before this assertion ever runs.
say "Stage 5 — HOP 1 positive: nx doctor trips the armed detector (first invocation)"
DOCTOR_OUT="$(nx doctor 2>&1)"; DOCTOR_RC=$?
printf '%s\n' "$DOCTOR_OUT" | grep -i stranded | sed 's/^/       /'
[ "$DOCTOR_RC" -ne 0 ] && ok "nx doctor exits non-zero on a stranded box (rc=$DOCTOR_RC)" \
  || bad "nx doctor exited 0 on a stranded box — the fatal check did not gate the exit code"
printf '%s' "$DOCTOR_OUT" | grep -q "unmigrated pre-PG data" \
  && ok "message names the pre-PG data" || bad "message missing 'unmigrated pre-PG data'"
printf '%s' "$DOCTOR_OUT" | grep -q "conexus==$PIN_RELEASE" \
  && ok "message pins the exact redirect target: conexus==$PIN_RELEASE" \
  || bad "message missing the exact pin 'conexus==$PIN_RELEASE'"
printf '%s' "$DOCTOR_OUT" | grep -q 'run `nx upgrade` there' \
  && ok "message names the exact verb: nx upgrade" \
  || bad "message missing the exact verb clause 'run \`nx upgrade\` there'"
printf '%s' "$DOCTOR_OUT" | grep -q "upgrade back to this version" \
  && ok "message states the third hop (upgrade back)" \
  || bad "message missing the third-hop clause"
printf '%s' "$DOCTOR_OUT" | grep -qE '\[stranded-install\]' \
  && ok "CLI startup banner fired (the loud, un-missable surface)" \
  || bad "no [stranded-install] startup banner in the output"

# `nx init` must refuse the SAME way, BEFORE attempting to provision —
# never a fresh empty install beside the unmigrated data.
say "Stage 5b — HOP 1 positive: nx init refuses BEFORE provisioning"
INIT_OUT="$(nx init --yes 2>&1)"; INIT_RC=$?
printf '%s\n' "$INIT_OUT" | grep -i stranded | sed 's/^/       /'
[ "$INIT_RC" -ne 0 ] && ok "nx init exits non-zero on a stranded box (rc=$INIT_RC)" \
  || bad "nx init exited 0 on a stranded box"
printf '%s' "$INIT_OUT" | grep -q "conexus==$PIN_RELEASE" \
  && ok "nx init's refusal also names the pin" || bad "nx init's refusal is missing the pin"
test ! -e "$HOME/.config/nexus/pg_credentials" \
  && ok "no pg_credentials written — init refused before provisioning anything" \
  || bad "pg_credentials exists — init proceeded PAST the refusal and provisioned"

# ── Stage 6 — HOP 1, control (b): fresh box, same current install, zero
#    artifacts. Must stay silent. Falsifies "the harness always prints a
#    stranded banner regardless of state".
say "Stage 6 — non-vacuity control (b): fresh box (same install, no artifacts) stays silent"
FRESH_OUT="$(NEXUS_CONFIG_DIR="$FRESH_CONFIG" NX_LOCAL_CHROMA_PATH="$FRESH_CHROMA" nx doctor 2>&1)"
# Pattern note (run-1 harness defect, 2026-08-08): the pattern must match only
# the DETECTOR's surfaces — the `[stranded-install]` banner tag and the fatal
# message's opening phrase — never the generic substring "unmigrated pre-PG
# data", because doctor's own CLEAN check row reads "Stranded pre-PG install:
# no unmigrated pre-PG data found" and contains that substring on a
# perfectly silent box.
if printf '%s' "$FRESH_OUT" | grep -qE '\[stranded-install\]|This install carries unmigrated'; then
  printf '%s\n' "$FRESH_OUT" | grep -E '\[stranded-install\]|This install carries unmigrated' | sed 's/^/       /'
  bad "fresh box (no artifacts) ALSO tripped the stranded banner — control (a) may be vacuous"
else
  ok "fresh box stays silent — no stranded banner, no carries-unmigrated-data message"
fi
printf '%s' "$FRESH_OUT" | grep -qi "stranded pre-pg install: no unmigrated" \
  && ok "doctor's own check row reads clean on the fresh box" \
  || note "doctor's stranded-install check row wording differs — see raw output above if this matters"

# ── Stage 7 — HOP 1, control (c): the SAME seeded artifacts, detector
#    DISARMED in-process (LAST_MIGRATION_CAPABLE = None — the state of
#    every migration-capable release). Must ALSO stay silent. This is the
#    literal falsification the bead's acceptance language names: "never
#    assert against a disarmed detector" — proving positive control (a) is
#    keyed on the constant being ARMED, not merely on artifact presence.
say "Stage 7 — non-vacuity control (c): disarmed constant, same real artifacts, stays silent"
DISARM_OUT="$(NX_LOCAL_CHROMA_PATH="$CHROMA_LOCAL" "$HOME/nxenv/bin/python3" - <<'PY' 2>&1
import sys
import nexus.stranded_install as si
from nexus.config import detect_stranded_install_default

armed = detect_stranded_install_default()
if armed is None:
    print("ARMED_CHECK_FAILED: expected a hit against the real seeded artifacts before disarming")
    sys.exit(1)

si.LAST_MIGRATION_CAPABLE = None
disarmed = detect_stranded_install_default()
if disarmed is not None:
    print(f"DISARM_FAILED: still detected after LAST_MIGRATION_CAPABLE=None: {disarmed!r}")
    sys.exit(1)

print("OK: armed detects, disarmed (same artifacts) is silent")
PY
)"
DISARM_RC=$?
printf '%s\n' "$DISARM_OUT" | sed 's/^/       /'
[ "$DISARM_RC" -eq 0 ] && ok "disarm-in-process control passed: armed trips, disarmed (same files) is silent" \
  || bad "disarm-in-process control FAILED (rc=$DISARM_RC) — see output above"

# ── Stage 8 — HOP 2: pin BACK to the exact older version the message named ──────
say "Stage 8 — HOP 2: downgrade back to conexus==$PIN_RELEASE (literally the message's own instruction)"
if uv pip install --python "$HOME/nxenv" --reinstall "conexus==$PIN_RELEASE" 2>&1 | tail -5 | sed 's/^/       /'; then
  ok "downgraded to conexus==$PIN_RELEASE"
else
  bad "downgrade to conexus==$PIN_RELEASE failed"; say "ABORT"; exit 1
fi
GOT_VER2="$(nx --version 2>&1 | grep -oE '[0-9]+\.[0-9]+\.[0-9]+' | head -1)"
[ "$GOT_VER2" = "$PIN_RELEASE" ] && ok "nx --version reports $GOT_VER2 (back at the pin)" \
  || bad "nx --version reports $GOT_VER2, expected $PIN_RELEASE"

# ── Stage 9: nx init at the pin — provisions local PG, and per the pin's own
#    first-run wiring (_converge_ladder_best_effort) opportunistically walks
#    the ladder too. The artifacts from Stage 2 are STILL on disk
#    (copy-not-move — nothing in this rehearsal or the product deletes
#    them), so this is the SAME data the message told the user to migrate.
say "Stage 9 — nx init at the pin (provision local PG + best-effort ladder convergence)"
unset NEXUS_SERVICE_TAG NX_SERVICE_TAG 2>/dev/null || true
# Full output CAPTURED, not just tailed (run-1 harness defect, 2026-08-08):
# the pin's init runs _converge_ladder_best_effort, so the substrate-etl
# convergence line legitimately appears HERE, not in Stage 10's nx upgrade
# output — Stage 10's assertion greps the combined transcript.
INIT_PIN_OUT="$(nx init --embedder bge-768 --yes 2>&1)"; INIT_PIN_RC=$?
printf '%s\n' "$INIT_PIN_OUT" | tail -20 | sed 's/^/       /'
if [ "$INIT_PIN_RC" -eq 0 ]; then
  ok "nx init (provisioned PG + started, at the pin release)"
else
  bad "nx init failed at the pin release (rc=$INIT_PIN_RC)"; say "ABORT (provision failed)"; exit 1
fi
export NX_STORAGE_BACKEND=service
# shellcheck disable=SC1091
[ -f "$HOME/.config/nexus/pg_credentials" ] && { set -a; . "$HOME/.config/nexus/pg_credentials"; set +a; }
if _wait_healthy 30; then
  ok "service healthy at the pin release"
else
  nx daemon service status 2>&1 | sed 's/^/       /' || true
  bad "service did not reach healthy at the pin release"; say "ABORT"; exit 1
fi

# ── Stage 10 — THE REDIRECT'S ACTUAL CLAIM UNDER TEST: nx upgrade must
#    converge the substrate-etl rung against the SAME on-disk data the
#    detector refused to touch.
say "Stage 10 — nx upgrade at the pin (the redirect's step 2, for real)"
UPGRADE_OUT="$(nx upgrade --yes 2>&1)"; UPGRADE_RC=$?
printf '%s\n' "$UPGRADE_OUT" | sed 's/^/       /'
[ "$UPGRADE_RC" -eq 0 ] && ok "nx upgrade exited 0" || bad "nx upgrade exited $UPGRADE_RC"
# The convergence line may come from EITHER surface (run-1 harness defect,
# 2026-08-08): in practice the pin's init already converged the ladder
# (best-effort), leaving nx upgrade to correctly report nothing pending.
# What the redirect promises is that the DATA MIGRATES at the pin — proven
# by the converged-and-verified line from whichever step performed it, plus
# the retrievability assert in Stage 10b below.
printf '%s\n%s' "$INIT_PIN_OUT" "$UPGRADE_OUT" | grep -qE "rung 'substrate-etl'.*(converged and verified|verified)" \
  && ok "substrate-etl rung converged and verified at the pin (init or upgrade output)" \
  || bad "no 'substrate-etl' converged/verified line in EITHER nx init or nx upgrade output — the redirect's data migration did not run"

# ── Stage 10b — the data-presence proof: the SEEDED text must be retrievable
#    at the pin post-migration. "converged and verified" alone could in
#    principle be satisfied by a vacuous rung (run 1's post-doctor read
#    "Migration reports: no migrations recorded", which is exactly why this
#    positive assert exists); seed_legacy.py's default seed writes chunks
#    with the literal text "onnx chunk NNNN", so semantic search at the pin
#    must surface it from the migrated store.
say "Stage 10b — migrated data is retrievable at the pin (nx search finds the seeded text)"
SEARCH_OUT="$(nx search "onnx chunk" 2>&1)"
if printf '%s' "$SEARCH_OUT" | grep -q "onnx chunk"; then
  ok "nx search returns the seeded chunk text — the Chroma data genuinely landed in PG"
else
  printf '%s\n' "$SEARCH_OUT" | tail -15 | sed 's/^/       /'
  bad "nx search found no seeded 'onnx chunk' text — the migration converged without actually moving the data"
fi

# ── Assert: doctor is clean afterward — no more pending rungs, a migration
#    report was recorded, and (best-effort) the pin's own doctor no longer
#    reports the pre-PG artifacts as a live concern for THIS release (the
#    pin is migration-capable, so its own detector stays disarmed
#    regardless — this just confirms the ladder's bookkeeping closed out).
say "Assert — post-migration state is clean at the pin"
POST_DOCTOR="$(nx doctor 2>&1)"
printf '%s' "$POST_DOCTOR" | grep -qi "upgrade ladder: no pending rungs" \
  && ok "upgrade ladder reports no pending rungs post-migration" \
  || note "doctor's ladder-summary wording differs — see raw output above if this matters: $(printf '%s' "$POST_DOCTOR" | grep -i 'upgrade ladder' | head -3)"
printf '%s' "$POST_DOCTOR" | grep -qi "migration reports" \
  && note "migration-reports check: $(printf '%s' "$POST_DOCTOR" | grep -i 'migration reports' | head -1)"

# ── Stage 11 — HOP 3 (nexus-4922x): the redirect's own literal step 3
#    ("upgrade back to this version"). Package-upgrade from the pin BACK to
#    the working tree a SECOND time (the box already did this once at
#    Stage 4, but that was BEFORE the pin ever migrated anything) and
#    assert nx doctor / nx init / plain CLI startup all stay SILENT — no
#    re-refusal, no [stranded-install] banner.
#
#    Root cause this is the adjudicating fixture for: pre-nexus-4922x,
#    detect_stranded_install's ONLY migrated-signal was
#    _has_verified_migration_report() reading <config>/migration-reports/
#    *.json — a format NEITHER of the two remedy paths the redirect
#    message can name ever writes (nx upgrade == the ladder, records
#    completion engine-side via HttpLadderStore; nx guided-upgrade ==
#    hidden, delegates to run_guided_upgrade, also never writes there —
#    verified against the v6.18.1 tag's own source, see the bead's T2
#    write-up). Pre-PG artifacts are copy-not-move and stay on disk
#    forever, so on the OLD code this stage would find the SAME files
#    present, no verified report anywhere, and re-trip the detector right
#    here — an unfollowable infinite two-hop loop for a real user doing
#    exactly what the message told them to do. The fix rekeys the primary
#    de-strand signal off the engine-side ladder-completion record that
#    Stage 10 (nx upgrade at the pin) actually wrote.
say "Stage 11 — HOP 3 (nexus-4922x): upgrade back to the working tree a SECOND time; must stay SILENT"
if uv pip install --python "$HOME/nxenv" --reinstall "$WHEEL" 2>&1 | tail -5 | sed 's/^/       /'; then
  ok "package upgraded back to the working-tree build a second time ($WHEEL)"
else
  bad "hop-3 package upgrade failed"; say "ABORT"; exit 1
fi
GOT_VER3="$(nx --version 2>&1 | grep -oE '[0-9]+\.[0-9]+\.[0-9]+' | head -1)"
note "nx --version reports $GOT_VER3 (back on the working tree, post pin-migration)"

# The FIRST invocation post-hop-3 is the load-bearing assertion (same
# clobber-guard-order caveat as Stage 5): nx doctor is used first, before
# anything else gets a chance to rewrite the last_seen_version stamp.
HOP3_DOCTOR_OUT="$(nx doctor 2>&1)"; HOP3_DOCTOR_RC=$?
printf '%s\n' "$HOP3_DOCTOR_OUT" | grep -i stranded | sed 's/^/       /'
if printf '%s' "$HOP3_DOCTOR_OUT" | grep -qE '\[stranded-install\]|This install carries unmigrated'; then
  printf '%s\n' "$HOP3_DOCTOR_OUT" | grep -E '\[stranded-install\]|This install carries unmigrated' | sed 's/^/       /'
  bad "HOP 3: nx doctor RE-TRIPPED the stranded banner after a genuine pin migration — the two-hop redirect's own step 3 is unfollowable (nexus-4922x)"
else
  ok "HOP 3: nx doctor stays silent — no re-refusal after the real pin migration"
fi
[ "$HOP3_DOCTOR_RC" -eq 0 ] \
  && ok "nx doctor exits 0 post-hop-3" \
  || note "nx doctor rc=$HOP3_DOCTOR_RC post-hop-3 — check above whether this is the stranded check or something unrelated"

# nx init a second time must ALSO stay silent — idempotent re-init per
# commands/init.py's own "Installation is idempotent" contract, not a
# stranded-install refusal and not a re-provision from scratch.
HOP3_INIT_OUT="$(nx init --yes 2>&1)"; HOP3_INIT_RC=$?
if printf '%s' "$HOP3_INIT_OUT" | grep -qE '\[stranded-install\]|This install carries unmigrated|Refusing to initialize'; then
  printf '%s\n' "$HOP3_INIT_OUT" | tail -10 | sed 's/^/       /'
  bad "HOP 3: nx init RE-REFUSED after a genuine pin migration (nexus-4922x)"
else
  ok "HOP 3: nx init stays silent — no re-refusal"
fi
if [ "$HOP3_INIT_RC" -ne 0 ]; then
  printf '%s\n' "$HOP3_INIT_OUT" | tail -15 | sed 's/^/       /'
  bad "HOP 3: nx init exited non-zero (rc=$HOP3_INIT_RC) post-hop-3 — see output above (may be unrelated to stranded-install, but idempotent re-init should exit 0)"
else
  ok "nx init exits 0 post-hop-3 (idempotent re-init)"
fi

# Plain CLI startup (the [stranded-install] banner specifically) — any
# ordinary invocation must be clean, not just doctor/init.
HOP3_CLI_OUT="$(nx doctor --help 2>&1)"
if printf '%s' "$HOP3_CLI_OUT" | grep -qE '\[stranded-install\]'; then
  printf '%s\n' "$HOP3_CLI_OUT" | grep -E '\[stranded-install\]' | sed 's/^/       /'
  bad "HOP 3: CLI startup banner still fires post-migration (nexus-4922x)"
else
  ok "HOP 3: CLI startup banner stays silent"
fi

say "RESULT"
if [ "$FAILS" -eq 0 ]; then
  printf '\033[32mSTRANDED-REDIRECT MVV PASSED\033[0m — conexus %s carrying real pre-PG artifacts, package-upgraded straight to the working tree, tripped the armed detector with the exact two-hop message (pin=%s); both non-vacuity controls (fresh box, disarmed constant) stayed silent as required; downgrading back to conexus==%s and running nx init + nx upgrade there migrated the SAME data for real; HOP 3 (upgrading back to the working tree a second time, nexus-4922x) stayed silent — no re-refusal, the two-hop redirect is genuinely followable end to end\n' \
    "$PIN_RELEASE" "$PIN_RELEASE" "$PIN_RELEASE"
  exit 0
else
  printf '\033[31mSTRANDED-REDIRECT MVV FAILED — %d check(s) failed\033[0m\n' "$FAILS"
  exit 1
fi
