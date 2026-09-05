#!/usr/bin/env bash
# Host orchestrator for the soup-to-nuts migration dress rehearsal.
#
#   tests/e2e/migration-rehearsal/run.sh              # ONNX leg only (secret-free)
#   tests/e2e/migration-rehearsal/run.sh --with-cloud # + Voyage leg (reads .env)
#   tests/e2e/migration-rehearsal/run.sh --no-build   # reuse existing wheel/JAR
#   tests/e2e/migration-rehearsal/run.sh --hole-punch # verify-fill delta-fill proof (nexus-s3dd4.7)
#   tests/e2e/migration-rehearsal/run.sh --era-hop    # RDR-185 era-spanning hop: ancient install -> current via `nx upgrade` ALONE (nexus-n7u38.30)
#   tests/e2e/migration-rehearsal/run.sh --stranded    # nexus-8nlj4 two-hop stranded-redirect: ancient Chroma artifacts + package-upgrade straight to current must trip the LAST_MIGRATION_CAPABLE detector; downgrading to the pin must be able to migrate them for real
#   tests/e2e/migration-rehearsal/run.sh --candidate-migration # nexus-z0ylb: the locally-built CANDIDATE engine's full Liquibase walk over a POPULATED store — provision the PUBLISHED FLOOR engine, populate through it (content + manifests + taxonomy), hand-swap the candidate binary in (sidecar stays at the floor tag), boot, assert the candidate's changesets apply clean over populated data with EXACT row invariants
#   NEXUS_TARGET_RELEASE=X.Y.Z tests/e2e/migration-rehearsal/run.sh --package-upgrade  # nexus-86mx2 PUBLISHED-TARGET mode: upgrade to the REAL published PyPI wheel X.Y.Z (sha256-verified against PyPI's own JSON API) instead of the worktree build; unset = worktree behavior unchanged
#   tests/e2e/migration-rehearsal/run.sh --comprehensive # Phase D: daily-driver surface (T2/T1/T3/catalog/doctor), deterministic bge-768 LOCAL only
#   tests/e2e/migration-rehearsal/run.sh --stress      # Phase E: concurrency + queue-drain stress, same bge-768-local dependency as Phase D
#
# KNOWN COVERAGE GAP (nexus-f4apk): --comprehensive/--stress and --with-cloud are
# mutually exclusive by construction (Phase D/E are bge-768 LOCAL; --with-cloud
# boots a voyage-only service, and the guard below refuses the combination
# rather than let it 422 downstream at the engine). NO invocation of this
# harness therefore exercises the Phase D/E daily-driver surface against
# Voyage — a bare --with-cloud run asserts nothing past Phase A's install-time
# checks, and --comprehensive/--stress only ever run on onnx-local. Tracked as
# a deliberate, not-yet-built follow-up: nexus-itxet (voyage-capable Phase-D
# variant).
#
# Builds the wheel on the host and the LINUX native nexus-service binary in a
# GraalVM container (RDR-161: the native binary is the sole launch artifact; the
# java -jar path is expunged). The native build runs IN a linux container so the
# binary matches the rehearsal image's platform — a host build would produce the
# wrong-OS binary. It uses Docker-out-of-Docker (mounted socket) because -Pnative
# runs jOOQ codegen via a Testcontainers pgvector. Both go into an ephemeral image
# with PG16 + pgvector (no JRE), running the full operator path (install → provision
# → serve → seed → migrate → validate → rollback). NOT DinD: PG is provisioned
# inside the box by nx itself.
set -euo pipefail

# ── Machine-readable output, always (2026-07-24) ─────────────────────────────
# This harness redirects CLI stdout into files that are later parsed by other
# tools (requirements.txt -> `uv pip install -r` inside the image). An agent
# shell exports FORCE_COLOR (Claude Code sets it for its own rendering), which
# makes uv emit ANSI escapes EVEN WHEN stdout is a file — so byte 0 of
# requirements.txt became ESC and the container build died with
# "Unexpected '<ESC>', expected '-c', '-e', '-r' ..." at 1:1.
#
# The asymmetry is the dangerous part: this gate passes when Hal runs it by
# hand and fails only when an agent runs it, which is precisely when nobody is
# watching a terminal. Neutralize color for the whole script rather than per
# call site, so a future redirect cannot reintroduce the class.
export NO_COLOR=1
unset FORCE_COLOR CLICOLOR_FORCE

# Captured BEFORE the `cd` below so it is robust to the invocation cwd (RDR-184
# P0.2, nexus-ccs9v.2): BASH_SOURCE is relative to wherever this script was
# invoked FROM, not the repo root the next line cd's into.
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"

# nexus-f2g8u: silent-exit guard. Armed as early as possible — before any
# other trap this script installs — so no exit path anywhere below can
# terminate the harness with zero diagnostic on either stream (observed
# once at Step 11c of the 7.27.0 release, exit 1 after a SUCCESSFUL wheel
# build with nothing printed on stdout or stderr). See the lib file's own
# header for the mechanism; every later EXIT-trap reassignment in this file
# chains `diag_exit_guard` first rather than clobbering it.
# shellcheck source=../lib/exit_diagnostics.sh disable=SC1091
source "$SCRIPT_DIR/../lib/exit_diagnostics.sh"
diag_arm_err_trap
trap 'diag_exit_guard' EXIT

cd "$(git rev-parse --show-toplevel)"
HERE="tests/e2e/migration-rehearsal"
IMAGE="nexus-migration-rehearsal"
WITH_CLOUD=0
DO_BUILD=1
GUIDED=0
COLD=0
COMPREHENSIVE=0
STRESS=0
FULLSTACK=0
SHAKEOUT_E2E=0
HOLE_PUNCH=0
SHAKEOUT=0
ACQUIRE=0
PACKAGE_UPGRADE=0
ERA_HOP=0
STRANDED=0
CANDIDATE_MIGRATION=0
# RDR-002 ez5.13: the release_version the guided MVV stamps into the binary so
# its /version reports >= the guided-upgrade version-pin floor and PASSES.
# Derived from the product constant (engine_version.REQUIRED_ENGINE_VERSION —
# the ONLY floor constant after the nexus-b6qlf unification) so this stamp can
# never go stale: a hardcoded "0.1.6" silently fell below the bumped v0.1.8 floor
# and the MVV fail-closed at the version gate without ever exercising the
# migration (nexus-... 6.0.0 validation). Then the derivation itself went stale
# the same way: it parsed REQUIRED_RELEASE_VERSION out of guided_upgrade.py after
# the constant moved to engine_version.py, and the silent "0.1.8" fallback
# fail-closed the v0.1.37 pre-tag rehearsal at the version gate. No fallback:
# if the constant can't be parsed, abort loudly.
GUIDED_STAMP_VERSION="$(
  python3 - <<'PY'
import re, pathlib
src = pathlib.Path("src/nexus/engine_version.py").read_text()
m = re.search(r"REQUIRED_ENGINE_VERSION[^=]*=\s*\((\d+),\s*(\d+),\s*(\d+)\)", src)
print(".".join(m.groups()) if m else "")
PY
)"
[ -n "$GUIDED_STAMP_VERSION" ] || { echo "FATAL: could not parse REQUIRED_ENGINE_VERSION from src/nexus/engine_version.py — the guided stamp would be wrong; fix the regex/path before rehearsing" >&2; exit 2; }
RELEASE_PROPS="service/src/main/resources/META-INF/nexus/release.properties"
# nexus-4mm24: the published engine-service tag the COLD box auto-acquires from.
# Must be >= the guided-upgrade version-pin floor (REQUIRED_ENGINE_VERSION); a
# stale default fail-closes the --cold MVV at the version gate. Kept literal (it
# names a PUBLISHED release tag, which need not equal the floor) but bumped to
# track it; override via NEXUS_SERVICE_TAG. (nexus-v0zmv)
#
# DEAD as a downstream rotation target since 2026-07-24 (nexus-8nlj4 bead
# note, re-confirmed 2026-08-08): its ONLY consumer is the run_env branch
# below guarded by `[ "$COLD" = 1 ] || [ "$HOLE_PUNCH" = 1 ]`, and both
# --cold and --hole-punch are refused pre-build (RDR-155 P4b — see the
# RETIRED block above, they drive the deleted `nx guided-upgrade`) before
# execution can ever reach that branch. --acquire reuses the SAME
# Dockerfile.cold image but supplies its OWN mandatory NEXUS_SERVICE_TAG
# (never this default) via the ACQUIRE branch just below. Left frozen
# rather than deleted: Dockerfile.cold + rehearse_cold.sh/
# rehearse_hole_punch.sh are still copied into the --acquire image (unused,
# but harmless — see the ACQUIRE staging comment) and a future un-retirement
# of --cold would want this constant already tracking the floor.
#
# BUMP IT AT EVERY FLOOR BUMP. This comment previously read "Bumping it
# further is a no-op; do not 'fix' it at the next floor bump" — true about
# RUNTIME effect (--cold is retired; nothing on a live path reads this) but
# FALSE as an instruction, because
# TestDownstreamConsumersTrackTheFloor::test_cold_rehearsal_tag_is_at_least_the_floor
# mechanically requires COLD_TAG >= REQUIRED_ENGINE_VERSION and reddens the
# suite when it drifts. Following the old wording blocked the 7.6.0 release
# battery (2026-08-10). A prose comment that contradicts a mechanical test
# loses to the test.
COLD_TAG="${NEXUS_SERVICE_TAG:-engine-service-v0.1.100}"
# nexus-cfgo9: the PACKAGE-UPGRADE leg's starting point — a REAL, already
# published PyPI release + the engine tag ITS OWN PINNED_SERVICE_TAG
# resolves to (see CHANGELOG.md's "[6.9.0]" entry: "Ships with (and
# requires) engine-service-v0.1.42"). Kept literal (like COLD_TAG) but
# bumped alongside REQUIRED_ENGINE_VERSION so the scenario never silently
# stops being "stale" — the guard below fails loud if it does.
# Rotated 2026-08-08 (nexus-8nlj4 owed-rotation sweep): the identity moved
# 0.1.65 (7.2.0/7.3.0, unchanged across that pair) -> 0.1.68 (7.4.0).
# 7.1.2/v0.1.62 had gone TWO releases stale (7.2.0, 7.3.0 both shipped
# without this rotating) rather than the required ONE — the standing memory
# note ("rotates at floor-bump time") was not honored at either bump.
# PREV_RELEASE/PREV_ENGINE_TAG must stay ONE release behind the current
# identity or the --package-upgrade convergence leg stops testing a
# realistic hop (nexus-cfgo9); the guard below fails loud if it ever
# collapses to zero (equals the current floor), but does NOT itself detect
# "two releases behind" — that still needs a human/agent check at the next
# floor bump.
# THE UNIT IS RELEASES, NOT ENGINE TAGS (2026-08-11). PREV_ENGINE_TAG is the
# engine the PREVIOUS RELEASE PINNED. An engine tag that is cut, published and
# gated but never pinned by any release is a SKIPPED version and must NOT be
# rotated onto — it would point this leg's "previous install" at a hop no user
# ever made. Worked example: v0.1.70 was cut, then a defect (nexus-syfes) sent
# 7.6.0 out on v0.1.71 instead, so at that floor bump COLD_TAG moved to
# v0.1.71 while these two correctly STAYED at 7.5.0 / v0.1.69 — 7.5.0 being
# the previous release and v0.1.69 what it pinned. The guard below does not
# catch this case either; only a human/agent check does.
# DERIVED, NOT HAND-TYPED (2026-08-19). Both values are facts already in this
# repo's history, so they are computed from it rather than re-typed at every
# floor bump: PREV_RELEASE is the newest published `v*` tag that is NOT the
# current working tree's own version, and PREV_ENGINE_TAG is whatever
# engine_version.py pinned AT that tag. The skipped-tag trap the block above
# describes cannot occur by construction — an engine tag no release ever
# pinned never appears in any release tag's engine_version.py, so it can
# never be selected here. The "two releases behind" drift the guard below
# cannot see is likewise structurally impossible: the derivation always picks
# the immediately-preceding release. Override either via the NEXUS_* env vars
# (unchanged contract); a derivation that comes up empty fails loud rather
# than falling back to a stale literal.
# Reads REQUIRED_ENGINE_VERSION as a dotted tuple from a release tag's tree.
# Empty (NOT fatal) when a tag predates the constant or cannot be read, so the
# walk below skips such tags instead of aborting on the oldest history.
_engine_tuple_at_release() {
  git show "v$1:src/nexus/engine_version.py" 2>/dev/null \
    | sed -n 's/^REQUIRED_ENGINE_VERSION[^(]*(\([0-9]*\), *\([0-9]*\), *\([0-9]*\)).*/\1.\2.\3/p' \
    | head -1
}
# NOT ALWAYS THE IMMEDIATELY-PRECEDING RELEASE (2026-08-22). The unit is still
# releases, but the selector is "the newest release that pinned a STRICTLY
# OLDER engine than this tree does" — because a release that bumps NO floor is
# a normal shape, and for one of those the immediately-preceding release pins
# the SAME engine, leaving this leg with nothing to converge. That is not a
# hypothetical: 7.14.0 and 7.15.0 both pin 0.1.85, as did 7.8.0/7.9.0 (0.1.79)
# and 7.6.0/7.6.1 (0.1.71) before them. The staleness guard below caught it
# correctly and its remedy text ("bump to the release immediately before this
# floor bump") assumed a floor bump that does not exist in that shape.
# Walking back to the newest genuinely-older pin keeps the hop REAL (a user on
# that release upgrading to this one) and keeps the convergence assertion
# non-vacuous, which is the whole point of the leg (nexus-cfgo9, GH #1402).
# The skipped-engine-tag trap documented above is still impossible by
# construction: only engines some release actually pinned are ever selectable.
_derive_prev_release() {
  local self_version cur_engine rel tuple
  self_version="$(sed -n 's/^version = "\(.*\)"/\1/p' "$(pwd)/pyproject.toml" | head -1)"
  cur_engine="$(sed -n 's/^REQUIRED_ENGINE_VERSION[^(]*(\([0-9]*\), *\([0-9]*\), *\([0-9]*\)).*/\1.\2.\3/p' "$(pwd)/src/nexus/engine_version.py" | head -1)"
  [ -n "$cur_engine" ] || { echo "FATAL: cannot read REQUIRED_ENGINE_VERSION from the working tree" >&2; exit 2; }
  # Anchored to canonical vX.Y.Z: an off-shape tag (rc/beta/typo) must never
  # be selectable as "the previous release" (substantive-critic, 2026-08-19).
  # Newest-first via awk rather than `sort -Vr`: -r composed with -V is not
  # portable across BSD/GNU sort and this runs on both.
  for rel in $(git tag -l 'v[0-9]*' \
               | grep -E '^v[0-9]+\.[0-9]+\.[0-9]+$' | sed 's/^v//' \
               | grep -vx "$self_version" | sort -V \
               | awk '{a[NR]=$0} END{for(i=NR;i>=1;i--) print a[i]}'); do
    tuple="$(_engine_tuple_at_release "$rel")"
    [ -n "$tuple" ] || continue
    [ "$tuple" = "$cur_engine" ] && continue
    if [ "$(printf '%s\n%s\n' "$tuple" "$cur_engine" | sort -V | head -1)" = "$tuple" ]; then
      printf '%s' "$rel"; return 0
    fi
  done
  echo "FATAL: cannot derive PREV_RELEASE — no published v* release pins an engine older than $cur_engine" >&2
  exit 2
}
_derive_prev_engine_tag() {
  local rel tuple
  rel="$1"
  tuple="$(git show "v$rel:src/nexus/engine_version.py" 2>/dev/null \
           | sed -n 's/^REQUIRED_ENGINE_VERSION[^(]*(\([0-9]*\), *\([0-9]*\), *\([0-9]*\)).*/\1.\2.\3/p' | head -1)"
  [ -n "$tuple" ] || { echo "FATAL: cannot derive PREV_ENGINE_TAG — v$rel has no readable REQUIRED_ENGINE_VERSION" >&2; exit 2; }
  printf 'engine-service-v%s' "$tuple"
}
PREV_RELEASE="${NEXUS_PREV_RELEASE:-$(_derive_prev_release)}"
PREV_ENGINE_TAG="${NEXUS_PREV_ENGINE_TAG:-$(_derive_prev_engine_tag "$PREV_RELEASE")}"
# nexus-86mx2 (2026-08-14) PUBLISHED-TARGET mode for --package-upgrade: when
# set, the UPGRADE TARGET is the real PUBLISHED PyPI wheel for that version
# instead of the working-tree build — the published-BYTES upgrade journey,
# closing the loop the pre-tag worktree run cannot prove ("identical tree" is
# an argument, not a run; see the release skill's post-publish step). Unset
# (default) leaves the worktree-wheel behavior below completely unchanged.
# This is a companion axis to tests/e2e/published-client-write-gate.sh, not a
# duplicate of it: that gate owns the FRESH-WRITE axis (a client that has
# never upgraded, writing against a NEWER engine); this mode owns the UPGRADE
# axis (an existing install's PACKAGE moving forward). Wheel is downloaded +
# sha256-verified against PyPI's own JSON API digest — fail loud on mismatch,
# never a silently-wrong artifact staged into the box.
NEXUS_TARGET_RELEASE="${NEXUS_TARGET_RELEASE:-}"
# RDR-185 P4.3 (nexus-n7u38.30): the ERA-HOP's starting point. Deliberately NOT
# "one release back" like PREV_RELEASE — this leg's whole claim is that an
# ANCIENT install converges, so the default is the OLDEST install the product
# still promises to carry: conexus 6.0.0, the migration-capable release (the
# two-release deprecation window's first half, docs/migration-runbook.md §0.1)
# and the exact population that holds the GH #1408 shape. ERA_ENGINE_TAG is
# 6.0.0's OWN PINNED_SERVICE_TAG — the engine that install would be running —
# and is acquired at runtime by 6.0.0's own code, never supplied by us.
#
# NOT the OLD_TAG rotation: nexus-dlhub owns that (a hop REDESIGN, not a bump).
# When RDR-155 P4b deletes the Chroma read path, this leg's SOURCE disappears
# with it and the whole scenario retires — it is a deprecation-window leg by
# construction.
ERA_RELEASE="${NEXUS_ERA_RELEASE:-6.0.0}"
ERA_ENGINE_TAG="${NEXUS_ERA_ENGINE_TAG:-engine-service-v0.1.11}"
# The NEW required engine — derived from the SAME constant COLD_TAG's
# default and GUIDED_STAMP_VERSION are, so this leg tracks a floor bump
# automatically (nexus-b6qlf: one source of truth).
NEW_ENGINE_TAG="engine-service-v${GUIDED_STAMP_VERSION}"
for a in "$@"; do
  case "$a" in
    --with-cloud) WITH_CLOUD=1 ;;
    --no-build)   DO_BUILD=0 ;;
    --guided)     GUIDED=1 ;;   # RDR-002 ez5.13: drive nx guided-upgrade
    --cold)       COLD=1 ;;     # nexus-4mm24: cold-acquire from the published release
    --comprehensive) COMPREHENSIVE=1 ;;  # Phase D: daily-driver surface on the default rehearse.sh
    --stress)     STRESS=1 ;;            # Phase E: concurrency + queue-drain stress on the default rehearse.sh
    --fullstack)  FULLSTACK=1 ;;         # standalone: full topology (service + nx-mcp + claude) MCP-driven enqueue + worker drain
    --shakeout-e2e) SHAKEOUT_E2E=1 ;;    # standalone: nexus-33hpq-class daily-driver shakeout — real-corpus code/md/pdf ingest, search/query retrieval, T2/T1 round-trip, doctor, MCP surface, live peak-RSS assertion during code ingest
    --hole-punch) HOLE_PUNCH=1 ;;        # standalone: verify-fill delta-fill proof against a real fault-injected PG target (nexus-s3dd4.7)
    --acquire)    ACQUIRE=1 ;;         # nexus-1ddsy: PUBLISHED-artifact gate — cold-acquire NEXUS_SERVICE_TAG on a bare box and drive it
    --shakeout)   SHAKEOUT=1 ;;          # standalone: CANDIDATE shakeout — CLI verb matrix + incremental index + concurrent load against the locally-built -Ob binary (nexus-h8rf6)
    --package-upgrade) PACKAGE_UPGRADE=1 ;;  # standalone: nexus-cfgo9 ONE-engine convergence MVV — package-only upgrade from a real previous release, engine acquired for real by the product, never supplied by this harness
    --era-hop)    ERA_HOP=1 ;;           # standalone: RDR-185 nexus-n7u38.30 — ancient install (old release + old engine + pre-RDR-108 ids + Chroma substrate) -> current via `nx upgrade` ALONE, unattended
    --stranded)   STRANDED=1 ;;          # standalone: nexus-8nlj4 — two-hop stranded-redirect: ancient Chroma artifacts + package-upgrade to current trips LAST_MIGRATION_CAPABLE; downgrade to the pin must be able to migrate them for real
    --candidate-migration) CANDIDATE_MIGRATION=1 ;;  # standalone: nexus-z0ylb — the locally-built CANDIDATE engine's Liquibase walk over a POPULATED store (floor engine populates for real, candidate binary hand-swapped in, sidecar stays at the floor tag)
    *) echo "unknown arg: $a" >&2; exit 2 ;;
  esac
done
# nexus-8nlj4 (post-P4b acceptance-harness reshape): the two-hop
# stranded-redirect leg's pin release — derived from the working tree's own
# LAST_MIGRATION_CAPABLE constant (src/nexus/stranded_install.py), so this
# leg tracks a future rotation of the stamp automatically (same discipline
# as GUIDED_STAMP_VERSION above; nexus-b6qlf's one-source-of-truth pattern).
# Unlike PREV_RELEASE/ERA_ENGINE_TAG this constant is NOT expected to rotate
# every release — it is a one-time stamp armed once at the 7.0.0 cut and
# left alone — so there is no periodic-staleness guard here, only the
# self-referential sanity check below (the pin must not equal the working
# tree's own version, or the redirect message would point a release at
# itself and every hop-1 assertion would be vacuous).
# Gated on --stranded (review-nexus-8nlj4 [21881] Important + critique [21883]
# Significant-2): a parse failure here must fail THIS leg loud, not FATAL
# every other leg of the harness.
if [ "$STRANDED" = 1 ]; then
  STRAND_PIN_RELEASE="$(
    python3 - <<'PY'
import re, pathlib
src = pathlib.Path("src/nexus/stranded_install.py").read_text()
m = re.search(r'LAST_MIGRATION_CAPABLE:\s*str \| None\s*=\s*"([^"]+)"', src)
print(m.group(1) if m else "")
PY
  )"
  [ -n "$STRAND_PIN_RELEASE" ] || { echo "FATAL: could not parse LAST_MIGRATION_CAPABLE from src/nexus/stranded_install.py — the stranded-redirect leg's pin would be wrong; fix the regex/path before rehearsing" >&2; exit 2; }
  [ "$STRAND_PIN_RELEASE" = "$(python3 -c 'import tomllib,pathlib;print(tomllib.loads(pathlib.Path("pyproject.toml").read_text())["project"]["version"])')" ] && {
    echo "FATAL: STRAND_PIN_RELEASE ($STRAND_PIN_RELEASE) equals the working tree's own version — LAST_MIGRATION_CAPABLE was rotated forward without leaving a distinct pin, and the stranded-redirect leg's hop-1 assertions would be vacuous (redirecting a release to itself)." >&2
    exit 2
  }
fi
[ "$PACKAGE_UPGRADE" = 1 ] && [ "${PREV_ENGINE_TAG#engine-service-v}" = "$GUIDED_STAMP_VERSION" ] && {
  echo "FATAL: PREV_ENGINE_TAG ($PREV_ENGINE_TAG) already equals the current REQUIRED_ENGINE_VERSION ($GUIDED_STAMP_VERSION) — the package-upgrade scenario is no longer 'stale'. Bump NEXUS_PREV_RELEASE/NEXUS_PREV_ENGINE_TAG in run.sh to the release immediately before this floor bump." >&2
  exit 2
}

# --guided: stamp release.properties so the native binary reports a release
# version (an unstamped build -> release_version=null -> version-pin fail-closes,
# which is not the success path this MVV exercises). Force a native rebuild so
# the stamp is actually baked in, and restore the file on exit. The stamp must
# happen BEFORE the native build below.
# Single restore hook for the stamped release.properties (folded into every EXIT
# trap below so a later `trap ... EXIT` does not clobber it). Defined + armed
# BEFORE the stamp mutation so a signal in the stamp window still restores it.
_guided_restore() {
  # MUST list every leg that stamps RELEASE_PROPS above. A leg added to the
  # stamp condition but not here leaves `release_version=<version>` committed
  # into the working tree — which then bakes a stamp into every subsequent
  # local build and can be committed by accident. Observed exactly that way
  # 2026-08-09 when --shakeout-e2e was added to the stamp side only.
  # nexus-z0ylb: --candidate-migration ADDED to this list — it DOES build
  # a local native candidate (the leg's whole point) and stamps it with
  # the floor version so the candidate's own /version self-reports the
  # floor tag it is hand-swapped in under (the harness's Stage 4/5 hand-
  # swap bookkeeping depends on this — see rehearse_candidate_migration.sh's
  # own header for what that bookkeeping does and does not claim).
  if { [ "$GUIDED" = 1 ] || [ "$SHAKEOUT_E2E" = 1 ] || [ "$CANDIDATE_MIGRATION" = 1 ]; }; then
    rm -f "$RELEASE_PROPS.tmp" 2>/dev/null || true
    # Pre-invocation BYTES, never `git checkout` (nexus-iws18: HEAD is not
    # what was in the tree when this run started; a checkout destroys any
    # uncommitted edit to release.properties on every run).
    cp "$RELEASE_PROPS_SNAPSHOT" "$RELEASE_PROPS" 2>/dev/null || true
  fi
  rm -f "$RELEASE_PROPS_SNAPSHOT" 2>/dev/null || true
}
# nexus-iws18: snapshot release.properties' actual bytes before any leg can
# stamp it; _guided_restore puts exactly these back on exit.
RELEASE_PROPS_SNAPSHOT="$(mktemp "${TMPDIR:-/tmp}/release.properties.snapshot.XXXXXX")"
cp "$RELEASE_PROPS" "$RELEASE_PROPS_SNAPSHOT"
trap 'diag_exit_guard; _guided_restore' EXIT

[ "$COLD" = 1 ] && [ "$GUIDED" = 1 ] && { echo "--cold and --guided are different flows; pick one" >&2; exit 2; }

# nexus-1ddsy: --acquire is a standalone published-artifact gate.
[ "$ACQUIRE" = 1 ] && { [ "$COLD" = 1 ] || [ "$GUIDED" = 1 ] || [ "$WITH_CLOUD" = 1 ] || [ "$SHAKEOUT" = 1 ] || [ "$PACKAGE_UPGRADE" = 1 ] || [ "$ERA_HOP" = 1 ] || [ "$FULLSTACK" = 1 ] || [ "$STRANDED" = 1 ]; } && { echo "--acquire is a standalone published-artifact gate (its own entrypoint); do not combine with other legs" >&2; exit 2; }
# It validates a PUBLISHED tag, so the tag is mandatory and there is nothing to
# infer: NEXUS_SERVICE_TAG is the artifact under test, never a default.
[ "$ACQUIRE" = 1 ] && [ -z "${NEXUS_SERVICE_TAG:-}" ] && { echo "--acquire requires NEXUS_SERVICE_TAG=<published tag>, e.g. NEXUS_SERVICE_TAG=engine-service-v0.1.55 (it exercises the PUBLISHED artifact, not a local build)" >&2; exit 2; }

# ── RETIRED journeys (RDR-155 P4b, 2026-07-24, nexus-8nlj4) ──────────────────
# --guided / --cold / --hole-punch drive `nx guided-upgrade`, and the DEFAULT
# rehearse.sh Phase B drives `nx migrate-to-service` — verbs DELETED in P4b P2.
# The Chroma->PG guided-migration journey is replaced by the two-hop
# stranded-install redirect; its acceptance rehearsal is tracked in nexus-8nlj4
# (cut-time, gated on the LAST_MIGRATION_CAPABLE stamp). Refuse loud, pre-build.
if [ "$GUIDED" = 1 ] || [ "$COLD" = 1 ] || [ "$HOLE_PUNCH" = 1 ]; then
  echo "RETIRED (RDR-155 P4b): --guided/--cold/--hole-punch drive nx guided-upgrade, deleted in P4b P2. Superseded by the two-hop stranded-redirect rehearsal (nexus-8nlj4). The surviving journeys are --era-hop, --package-upgrade, --shakeout, --shakeout-e2e, --fullstack, --candidate-migration, and the default rehearse.sh (Phases A/D/E; its migrate leg is skipped)." >&2
  exit 2
fi
# nexus-gilf2: --guided seeds local-ONNX (bge-768) cross-model targets, while
# --with-cloud boots a voyage-only service. The combination is incoherent: the
# bge-768 targets have no embedder in voyage mode and the pebfx.2 guard 422s the
# leg. Use --guided alone for the local bge-768 MVV.
[ "$GUIDED" = 1 ] && [ "$WITH_CLOUD" = 1 ] && { echo "--guided and --with-cloud are incoherent (guided seeds local bge-768 targets; cloud is voyage-only); run --guided alone" >&2; exit 2; }
[ "$COLD" = 1 ] && [ "$DO_BUILD" = 0 ] && { echo "--cold always rebuilds the wheel + cold-acquires the binary; --no-build is irrelevant" >&2; exit 2; }
# --comprehensive adds Phase D to the DEFAULT rehearse.sh entrypoint; --cold and
# --guided override the entrypoint (rehearse_cold.sh; --guided is RETIRED,
# RDR-155 P4b, and refused pre-build above) and so never run Phase D. Reject
# the incoherent combination loudly.
[ "$COMPREHENSIVE" = 1 ] && { [ "$COLD" = 1 ] || [ "$GUIDED" = 1 ]; } && { echo "--comprehensive runs on the default rehearse path; it cannot combine with --cold/--guided (they override the entrypoint)" >&2; exit 2; }
[ "$STRESS" = 1 ] && { [ "$COLD" = 1 ] || [ "$GUIDED" = 1 ]; } && { echo "--stress runs on the default rehearse path; it cannot combine with --cold/--guided (they override the entrypoint)" >&2; exit 2; }
# nexus-f4apk: same incoherence class as the --guided+--with-cloud guard above
# (nexus-gilf2) — Phase D is declared deterministic bge-768 LOCAL (rehearse.sh's
# own Phase D banner), while --with-cloud boots a voyage-only service. Before
# this guard the combination arg-parsed cleanly and only failed downstream at
# the engine with HTTP 422 (correct cross-model-contamination refusal, not an
# engine defect — v0.1.69 rehearsal, T2 conexus/[22073]). Phase E shares the
# same dependency: its store/memory puts land in the identical
# bge-768-vs-voyage-serving-mode collision, so it gets the identical guard.
[ "$COMPREHENSIVE" = 1 ] && [ "$WITH_CLOUD" = 1 ] && { echo "--comprehensive and --with-cloud are incoherent (Phase D is deterministic bge-768 local; --with-cloud boots a voyage-only service — the engine correctly 422s the cross-model write); run --comprehensive alone" >&2; exit 2; }
[ "$STRESS" = 1 ] && [ "$WITH_CLOUD" = 1 ] && { echo "--stress and --with-cloud are incoherent (Phase E's store/memory puts hit the same bge-768-vs-voyage-serving-mode collision as Phase D); run --stress alone" >&2; exit 2; }
[ "$FULLSTACK" = 1 ] && { [ "$COLD" = 1 ] || [ "$GUIDED" = 1 ] || [ "$WITH_CLOUD" = 1 ] || [ "$COMPREHENSIVE" = 1 ] || [ "$STRESS" = 1 ]; } && { echo "--fullstack is a standalone full-topology run (its own entrypoint); do not combine with other legs" >&2; exit 2; }
# --hole-punch is a standalone journey: it reuses the --cold box's staging
# internally (cheapest to compose — no native GraalVM build) but drives its
# own entrypoint (rehearse_hole_punch.sh, nexus-s3dd4.7), never combined with
# another flow flag.
[ "$HOLE_PUNCH" = 1 ] && { [ "$COLD" = 1 ] || [ "$GUIDED" = 1 ] || [ "$WITH_CLOUD" = 1 ] || [ "$COMPREHENSIVE" = 1 ] || [ "$STRESS" = 1 ] || [ "$FULLSTACK" = 1 ]; } && { echo "--hole-punch is a standalone verify-fill delta-fill journey (its own cold-acquire entrypoint); do not combine with other legs" >&2; exit 2; }
[ "$HOLE_PUNCH" = 1 ] && [ "$DO_BUILD" = 0 ] && { echo "--hole-punch always rebuilds the wheel + cold-acquires the binary; --no-build is irrelevant" >&2; exit 2; }
# --shakeout is a standalone candidate-validation journey: it builds the current
# service/ tree natively (like --guided) and drives its own entrypoint
# (rehearse_shakeout.sh, nexus-h8rf6) — never combined with another flow flag.
[ "$SHAKEOUT" = 1 ] && { [ "$COLD" = 1 ] || [ "$GUIDED" = 1 ] || [ "$WITH_CLOUD" = 1 ] || [ "$COMPREHENSIVE" = 1 ] || [ "$STRESS" = 1 ] || [ "$FULLSTACK" = 1 ] || [ "$HOLE_PUNCH" = 1 ]; } && { echo "--shakeout is a standalone candidate shakeout (its own entrypoint); do not combine with other legs" >&2; exit 2; }
# --shakeout is the PRE-TAG candidate gate: its whole purpose is "prove THIS
# candidate binary", so a stale binary satisfying it inverts the gate
# (nexus-mbeke, reopening nexus-ndve9 on the pre-tag leg). Refuse --no-build
# exactly like every other native-build leg; the candidate IDENTITY assertion
# that would make --no-build safe is nexus-dk5wb.
[ "$SHAKEOUT" = 1 ] && [ "$DO_BUILD" = 0 ] && { echo "--shakeout is the pre-tag candidate gate and always rebuilds the native candidate + working-tree wheel (a stale binary would satisfy it silently, nexus-mbeke); --no-build is irrelevant" >&2; exit 2; }
# --package-upgrade is a standalone journey (nexus-cfgo9): NO native build (the
# NEW engine is acquired for real by the product's own convergence code, never
# locally built or supplied by this harness) — never combined with another
# flow flag.
[ "$PACKAGE_UPGRADE" = 1 ] && { [ "$COLD" = 1 ] || [ "$GUIDED" = 1 ] || [ "$WITH_CLOUD" = 1 ] || [ "$COMPREHENSIVE" = 1 ] || [ "$STRESS" = 1 ] || [ "$FULLSTACK" = 1 ] || [ "$HOLE_PUNCH" = 1 ] || [ "$SHAKEOUT" = 1 ]; } && { echo "--package-upgrade is a standalone convergence journey (its own entrypoint); do not combine with other legs" >&2; exit 2; }
[ "$PACKAGE_UPGRADE" = 1 ] && [ "$DO_BUILD" = 0 ] && { echo "--package-upgrade always rebuilds the working-tree wheel; --no-build is irrelevant" >&2; exit 2; }
# --era-hop is a standalone journey (nexus-n7u38.30): NO native build (both
# engines are acquired for real by the product's own code) — never combined.
[ "$ERA_HOP" = 1 ] && { [ "$COLD" = 1 ] || [ "$GUIDED" = 1 ] || [ "$WITH_CLOUD" = 1 ] || [ "$COMPREHENSIVE" = 1 ] || [ "$STRESS" = 1 ] || [ "$FULLSTACK" = 1 ] || [ "$HOLE_PUNCH" = 1 ] || [ "$SHAKEOUT" = 1 ] || [ "$PACKAGE_UPGRADE" = 1 ]; } && { echo "--era-hop is a standalone era-spanning journey (its own entrypoint); do not combine with other legs" >&2; exit 2; }
[ "$ERA_HOP" = 1 ] && [ "$DO_BUILD" = 0 ] && { echo "--era-hop always rebuilds the working-tree wheel; --no-build is irrelevant" >&2; exit 2; }
# Staleness guard, mirroring the --package-upgrade one: if the era's engine has
# caught up to the current floor there is no era left to span, and every
# convergence assertion in the leg would pass vacuously.
[ "$ERA_HOP" = 1 ] && [ "${ERA_ENGINE_TAG#engine-service-v}" = "$GUIDED_STAMP_VERSION" ] && {
  echo "FATAL: ERA_ENGINE_TAG ($ERA_ENGINE_TAG) already equals the current REQUIRED_ENGINE_VERSION ($GUIDED_STAMP_VERSION) — there is no era to span and the hop's convergence asserts would be vacuous. Fix NEXUS_ERA_RELEASE/NEXUS_ERA_ENGINE_TAG in run.sh." >&2
  exit 2
}
# --stranded is a standalone journey (nexus-8nlj4): NO native build (like
# --era-hop/--package-upgrade — both the pin release and the working tree's
# own package are installed from real PyPI / the local wheel; no engine
# binary or PG bundle is staged by this harness) — never combined.
[ "$STRANDED" = 1 ] && { [ "$COLD" = 1 ] || [ "$GUIDED" = 1 ] || [ "$WITH_CLOUD" = 1 ] || [ "$COMPREHENSIVE" = 1 ] || [ "$STRESS" = 1 ] || [ "$FULLSTACK" = 1 ] || [ "$HOLE_PUNCH" = 1 ] || [ "$SHAKEOUT" = 1 ] || [ "$PACKAGE_UPGRADE" = 1 ] || [ "$ERA_HOP" = 1 ] || [ "$ACQUIRE" = 1 ]; } && { echo "--stranded is a standalone two-hop stranded-redirect journey (its own entrypoint); do not combine with other legs" >&2; exit 2; }
[ "$STRANDED" = 1 ] && [ "$DO_BUILD" = 0 ] && { echo "--stranded always rebuilds the working-tree wheel; --no-build is irrelevant" >&2; exit 2; }
# --candidate-migration is a standalone journey (nexus-z0ylb): rebuilds
# BOTH the native candidate (like --shakeout — this leg's whole point is
# hand-swapping a locally-built candidate binary) AND the working-tree
# wheel client (installed via `uv tool install`, like --era-hop/
# --package-upgrade) — the FLOOR engine is the ONLY
# artifact acquired at runtime (`nx daemon service install-binary`),
# mirroring those legs' runtime-acquisition posture for the OTHER engine
# in play — never combined with another flow flag.
[ "$CANDIDATE_MIGRATION" = 1 ] && { [ "$COLD" = 1 ] || [ "$GUIDED" = 1 ] || [ "$WITH_CLOUD" = 1 ] || [ "$COMPREHENSIVE" = 1 ] || [ "$STRESS" = 1 ] || [ "$FULLSTACK" = 1 ] || [ "$HOLE_PUNCH" = 1 ] || [ "$SHAKEOUT" = 1 ] || [ "$PACKAGE_UPGRADE" = 1 ] || [ "$ERA_HOP" = 1 ] || [ "$ACQUIRE" = 1 ] || [ "$STRANDED" = 1 ]; } && { echo "--candidate-migration is a standalone populated-store candidate rehearsal (its own entrypoint); do not combine with other legs" >&2; exit 2; }
[ "$CANDIDATE_MIGRATION" = 1 ] && [ "$DO_BUILD" = 0 ] && { echo "--candidate-migration always rebuilds the native candidate + working-tree wheel; --no-build is irrelevant" >&2; exit 2; }
# --shakeout-e2e is a standalone journey (nexus-33hpq-class daily-driver
# shakeout): same native-binary staging as the default path (it needs a
# REAL locally-built service to embed the Step-2 corpus locally via
# bge-768 — no engine artifact is acquired at runtime) — never combined
# with another flow flag.
[ "$SHAKEOUT_E2E" = 1 ] && { [ "$COLD" = 1 ] || [ "$GUIDED" = 1 ] || [ "$WITH_CLOUD" = 1 ] || [ "$COMPREHENSIVE" = 1 ] || [ "$STRESS" = 1 ] || [ "$FULLSTACK" = 1 ] || [ "$HOLE_PUNCH" = 1 ] || [ "$SHAKEOUT" = 1 ] || [ "$PACKAGE_UPGRADE" = 1 ] || [ "$ERA_HOP" = 1 ] || [ "$ACQUIRE" = 1 ] || [ "$STRANDED" = 1 ] || [ "$CANDIDATE_MIGRATION" = 1 ]; } && { echo "--shakeout-e2e is a standalone daily-driver shakeout (its own entrypoint); do not combine with other legs" >&2; exit 2; }

# RDR-184 P0.2 (nexus-ccs9v.2): serialize on the machine-global fixed
# resources this harness mutates — the fixed docker tag ($IMAGE) and the
# shared dist/ wheel output (the near-miss that motivated this bead: two
# concurrent rehearsals racing the same wheel/image). The lock dir lives
# under a stable machine-global temp root, NOT under this checkout — the
# resource being serialized (one docker daemon, one dist/ per host) is
# machine-global, so two different checkouts on the same host must still
# serialize against each other. Acquired here, after arg parsing/validation
# (usage errors don't need the lock) but strictly before the first mutation
# (the --guided release.properties stamp just below). Lock dir is a
# HARD-CODED /tmp path, deliberately NOT ${TMPDIR:-/tmp} (code-review
# SIGNIFICANT fix): on darwin, an interactive shell's TMPDIR is a per-user
# /var/folders/... path while a LaunchAgent/CI/stripped-env invocation sees
# plain /tmp — two different invocation contexts would silently compute
# DIFFERENT lockdirs and never contend, defeating the whole point of a
# machine-global guard (this repo runs LaunchAgents that could race an
# interactive run). /tmp is always the same path across every context on
# the same host.
# shellcheck source=../lib/lock.sh disable=SC1091
source "$SCRIPT_DIR/../lib/lock.sh"
LOCKDIR="/tmp/nexus-e2e-locks/migration-rehearsal.lock"
mkdir -p "$(dirname "$LOCKDIR")"
lock_acquire "$LOCKDIR" || exit 1
# nexus-c00dw: the native-build docker step further down writes
# service/target on the HOST (bind mount) — same resource
# scripts/build-gate-jar.sh / mvnw-leased.sh guard. Sourced here (same
# "safe to reference in a trap" point as LOCKDIR above — see the CRITICAL
# fix note this replaces) so every trap reassignment from here on can
# chain build_lease_release in as a failure-path backstop; the acquire
# itself happens locally around the docker invocation, not here.
# shellcheck source=../../../scripts/lib/build-lease.sh disable=SC1091
source "$SCRIPT_DIR/../../../scripts/lib/build-lease.sh"
# Code-review CRITICAL fix: the trap installed at the top of the script
# (before LOCKDIR existed) referenced $LOCKDIR unconditionally — any of the
# 12 argument-conflict guards ABOVE this point firing `exit 2` would invoke
# that trap under `set -u` with $LOCKDIR unbound, aborting on the trap's own
# evaluation instead of the documented exit 2. LOCKDIR cannot be referenced
# by a trap before this line, where it is first assigned — reassign the
# trap to the lock-aware form only now that it is safe to do so.
trap 'diag_exit_guard; _guided_restore; build_lease_release service 2>/dev/null || true; lock_release "$LOCKDIR" 2>/dev/null || true' EXIT
echo "[rdr-184] lock acquired: $LOCKDIR (pid $$)" >&2
# Test seam (RDR-184 P0.2, nexus-ccs9v.2): tests/e2e/lib/harness_lock_test.sh
# sets this to prove a concurrent invocation gets PAST the lock without ever
# running this harness's real body (wheel build / native build / docker).
# No-op — unset in every normal invocation.
[[ -n "${NX_E2E_LOCK_SELFTEST:-}" ]] && exit 0

if [ "$GUIDED" = 1 ] || [ "$SHAKEOUT_E2E" = 1 ] || [ "$CANDIDATE_MIGRATION" = 1 ]; then
  # --guided force-rebuilds the native binary with the stamp baked in, so
  # it is incompatible with --no-build (which would reuse a stale/unstamped
  # binary).
  #
  # --shakeout-e2e joins --guided for the SAME reason, proven empirically
  # 2026-08-09: release.properties ships release_version= BLANK and it is
  # stamped only by the engine-service-release workflow, so a locally-built
  # binary reports release_version=null on /version FOREVER — not as a startup
  # race. That made the journey's sidecar-vs-running-binary cross-check
  # permanently UNMEASURED, and (worse) it meant the provenance sidecar the
  # journey writes was asserting a version the binary could never corroborate
  # — the exact "claim, not verified state" shape of nexus-hdumg. Stamping
  # here makes the binary self-describe, so both doctor's convergence check
  # and the journey's cross-check become real measurements instead of claims.
  #
  # --candidate-migration joins for a THIRD, distinct reason (nexus-z0ylb):
  # the candidate is hand-swapped in under the FLOOR tag's provenance
  # sidecar — the harness's own Stage 4/5 bookkeeping self-consistency
  # check (a `nx daemon restart-stale --dry-run` no-op assertion INSIDE
  # this test run, not a production converge-safety claim) requires the
  # candidate's own /version to self-report the floor version too, or
  # that check would see a live version disagreeing with both the sidecar
  # AND the release dependency. Same unstamped-forever problem as
  # shakeout-e2e otherwise (release_version is blank in source).
  [ "$DO_BUILD" = 0 ] && { echo "--guided/--shakeout-e2e/--candidate-migration require a fresh native build; drop --no-build" >&2; exit 2; }
  echo "[stamp] stamping $RELEASE_PROPS release_version=$GUIDED_STAMP_VERSION (restored on exit)…"
  grep -v '^release_version=' "$RELEASE_PROPS" > "$RELEASE_PROPS.tmp"
  printf 'release_version=%s\n' "$GUIDED_STAMP_VERSION" >> "$RELEASE_PROPS.tmp"
  mv "$RELEASE_PROPS.tmp" "$RELEASE_PROPS"
  # Force a fresh native build so the stamp is baked in.
  rm -f service/target/nexus-service
fi

GRAAL_IMAGE="container-registry.oracle.com/graalvm/native-image-community:25"
if [ "$COLD" = 1 ] || [ "$HOLE_PUNCH" = 1 ] || [ "$PACKAGE_UPGRADE" = 1 ] || [ "$ERA_HOP" = 1 ] || [ "$ACQUIRE" = 1 ] || [ "$STRANDED" = 1 ]; then
  # nexus-4mm24 / nexus-s3dd4.7 / nexus-cfgo9 / nexus-n7u38.30 / nexus-8nlj4 /
  # nexus-eo3qv: these boxes acquire every engine binary at runtime
  # (PUBLISHED release) — NO local native build, NO stamping. Just the wheel.
  echo "[1/2] Building the conexus wheel (host)…"
  # Do NOT suppress this unconditionally: under `set -e` a failed build
  # exits the harness with no diagnosis at all (2026-07-25 — the
  # v0.1.55 acquire gate died here having logged only its own banner).
  if ! uv build --wheel > "${TMPDIR:-/tmp}/nexus-wheel-build.log" 2>&1; then
    echo "uv build --wheel FAILED:" >&2
    sed 's/^/    /' "${TMPDIR:-/tmp}/nexus-wheel-build.log" >&2
    exit 1
  fi
  ls dist/conexus-*.whl >/dev/null 2>&1 || { echo "no wheel in dist/" >&2; exit 1; }
elif [ "$DO_BUILD" = 1 ]; then
  echo "[1/3] Building the conexus wheel (host)…"
  # Do NOT suppress this unconditionally: under `set -e` a failed build
  # exits the harness with no diagnosis at all (2026-07-25 — the
  # v0.1.55 acquire gate died here having logged only its own banner).
  if ! uv build --wheel > "${TMPDIR:-/tmp}/nexus-wheel-build.log" 2>&1; then
    echo "uv build --wheel FAILED:" >&2
    sed 's/^/    /' "${TMPDIR:-/tmp}/nexus-wheel-build.log" >&2
    exit 1
  fi
  echo "[2/3] Building the LINUX native nexus-service binary (GraalVM container, ~2-3m)…"
  # nexus-ndve9: EXISTENCE IS NOT FRESHNESS. `mvn package` leaves
  # service/target/nexus-service on disk and nothing here removes it, so an
  # existence-only guard reuses whatever artifact happens to be there —
  # forever. The 2026-08-03 v0.1.63 pre-tag shakeout validated a binary dated
  # 2026-07-22 while reporting on abbcf1bd: 34 newer service/src/main files,
  # and four count-emitting endpoints (store-get, manifest/get_many,
  # manifest/chashes, manifest/docs_for_chashes) missing purely because the
  # artifact predated the commits that added them. Every JVM suite was green
  # on the tree under test — nothing below this layer could have caught it.
  # Rebuild whenever ANY service source (or the pom) is newer than the binary.
  if [ ! -x service/target/nexus-service ] \
     || [ -n "$(find service/src service/pom.xml -newer service/target/nexus-service -print -quit 2>/dev/null)" ]; then
    # Native build in a linux GraalVM container. The mounted Docker socket lets
    # -Pnative's Testcontainers jOOQ codegen reach the host daemon (DooD);
    # TESTCONTAINERS_HOST_OVERRIDE + the host-gateway alias make the build
    # container reach the sibling pgvector. -Ob = quick-build (correctness gate,
    # not a perf binary). Output: service/target/nexus-service + its *.so siblings.
    # Builder heap: the pom default (native.image.maxheap=5632m) is sized for
    # the 7GB CI runner; locally it GC-thrashes (403 GCs / 6.8% of build time
    # observed on the 8GB-VM default, 2026-07-13). Auto-size to ~70% of the
    # Docker VM's memory, never below the pom default.
    vm_mib=$(( $(docker info --format '{{.MemTotal}}') / 1048576 ))
    NATIVE_MAXHEAP="$(( vm_mib * 70 / 100 ))m"
    [ "$(( vm_mib * 70 / 100 ))" -lt 5632 ] && NATIVE_MAXHEAP=5632m
    echo "      (builder heap ${NATIVE_MAXHEAP} — 70% of the ${vm_mib}MiB Docker VM)"
    # nexus-c00dw: ./mvnw runs INSIDE the container, but /src is a bind
    # mount of this host checkout, so service/target is the same
    # single-writer resource the host-side lease guards — acquire on the
    # host, around the docker invocation.
    build_lease_acquire service docker-native-build migration-rehearsal
    docker run --rm --entrypoint bash \
      --add-host=host.docker.internal:host-gateway \
      -v "$PWD":/src -w /src/service \
      -v /var/run/docker.sock:/var/run/docker.sock \
      -e TESTCONTAINERS_RYUK_DISABLED=true \
      -e TESTCONTAINERS_HOST_OVERRIDE=host.docker.internal \
      "$GRAAL_IMAGE" \
      -c "./mvnw -B -Pnative -DskipTests -Dnative.image.opt=-Ob -Dnative.image.maxheap=${NATIVE_MAXHEAP} package"
    build_lease_release service
  else
    # nexus-ndve9: when we DO reuse, say how old the artifact is — the failing
    # shakeout's log recorded only "candidate native binary present", which
    # reads as a pass while a 12-day-stale binary is under test.
    _bin_mtime="$(date -r service/target/nexus-service '+%Y-%m-%d %H:%M:%S' 2>/dev/null || echo unknown)"
    echo "      (native binary up to date — reusing service/target/nexus-service, built $_bin_mtime)"
  fi
else
  echo "[1-2/3] --no-build: reusing existing wheel + native binary"
fi

# nexus-nyry9.13 (2026-08-21): --acquire is a cold-acquire leg too — it stages
# NO local native binary (see the ACQUIRE staging branch below), so it must be
# excluded here exactly like the other runtime-acquire legs. Without this, the
# published-artifact gate demanded service/target/nexus-service on any box that
# had not happened to leave a stale one behind, and failed before testing anything.
if [ "$COLD" = 0 ] && [ "$HOLE_PUNCH" = 0 ] && [ "$PACKAGE_UPGRADE" = 0 ] && [ "$ERA_HOP" = 0 ] && [ "$STRANDED" = 0 ] && [ "$ACQUIRE" = 0 ]; then
  ls dist/conexus-*.whl >/dev/null 2>&1 || { echo "no wheel in dist/ — drop --no-build" >&2; exit 1; }
  [ -x service/target/nexus-service ] || { echo "no native binary at service/target/nexus-service — drop --no-build" >&2; exit 1; }
fi

# ── Pre-flight Docker disk-pressure check (nexus-h8rf6.13) ────────────────────
# The recurring barf is Docker Desktop's capped VM disk, not the host:
# iteration-heavy sessions accumulate build cache + dangling rehearsal-image
# generations until builds crawl (~80GB observed across 4 shakeout iterations).
# When reclaimable build cache exceeds the threshold, prune — with headroom
# generous enough to KEEP the hot layers (v0.1.21 lesson: an aggressive
# --reserved-space 6GB evicted the freshly-unreferenced 692MB bge model layer
# and forced a full re-download on the next build). Raised 12GB->40GB and
# trigger 10GB->40GB on 2026-07-21 (Hal authorized the disk): the split-install
# dependency layer is ~5GB and was being LRU-evicted between same-day builds
# at the old budgets (alongside Docker Desktop's own defaultKeepStorage, raised
# 20GB->80GB in ~/.docker/daemon.json the same day) — evicting it re-costs
# ~2.5-7 min per rehearsal launch, which defeats the split. Old dangling image
# generations are pruned by age so the current lineage stays. Prune only
# touches unused entries, so this is safe even with other builds up.
preflight_docker_prune() {
  local reclaimable_gb
  # A probe, never a gate: a transient daemon error here must not kill the
  # rehearsal under pipefail (observed 2026-09-05 while another Testcontainers
  # run shared the daemon), so the pipeline is guarded and defaults to 0.
  reclaimable_gb="$( { docker system df --format '{{.Type}} {{.Reclaimable}}' 2>/dev/null \
    | awk '/^Build Cache/ {v=$3+0; if ($3 ~ /TB/) v=v*1024; else if ($3 !~ /GB/) v=0; print int(v)}'; } || true)"
  reclaimable_gb="${reclaimable_gb:-0}"
  if [ "${reclaimable_gb:-0}" -gt 40 ] 2>/dev/null; then
    echo "[preflight] Docker build cache reclaimable ~${reclaimable_gb}GB (>40GB) — pruning (reserved-space 40GB keeps hot layers incl. the bge model + the split deps layer)…"
    docker builder prune -f --reserved-space 40GB 2>/dev/null | tail -1 || true
    # Belt: drop dangling (untagged) image generations older than a day —
    # this is what actually releases superseded rehearsal-image layers.
    docker image prune -f --filter 'until=24h' 2>/dev/null | tail -1 || true
  fi
}
preflight_docker_prune

echo "[stage] Staging a minimal build context + building image (COLD=$COLD HOLE_PUNCH=$HOLE_PUNCH ERA_HOP=$ERA_HOP WITH_CLOUD=$WITH_CLOUD)…"
# Flatten wheel + JAR + driver to fixed names in a tiny throwaway context. The
# repo .dockerignore excludes dist/, and the inputs live in three different
# trees — staging sidesteps both without touching the shared .dockerignore.
STAGE="$(mktemp -d)"
trap 'diag_exit_guard; _guided_restore; rm -rf "$STAGE"; build_lease_release service 2>/dev/null || true; lock_release "$LOCKDIR" 2>/dev/null || true' EXIT
cp "$(ls -t dist/conexus-*.whl | head -1)"            "$STAGE/"   # keep real PEP 427 name
# Lock-derived dependency manifest for the split install layer (Dockerfile /
# .cold / .fullstack): the wheel's bytes churn every build (embedded mtimes),
# so a deps-install layer keyed on the wheel re-ran its full 5-7 min closure
# install every run (measured 2026-07-21: 430s). Keying it on uv.lock content
# instead makes it a cache hit until dependencies actually change; the wheel
# itself installs --no-deps in a later cheap layer. stdout redirect, NOT -o:
# uv embeds the -o path in the header comment, and $STAGE is a fresh mktemp
# every run — that alone would bust the layer cache. --locked fails loud on a
# stale uv.lock instead of exporting a closure the wheel does not match.
# Runs unconditionally for every leg: era-hop/package-upgrade/stranded
# never COPY it (they install deps at runtime from real PyPI — that is those
# scenarios' point), so for them it is a 1ms offline no-op in the context dir.
# --color never is belt-and-braces over the script-level NO_COLOR above:
# this particular redirect is the one that is PARSED, so state the
# requirement locally too rather than relying on ambient env hygiene.
uv export --color never --locked --no-dev --no-emit-project --no-hashes -q > "$STAGE/requirements.txt"
# Fail loud if it is still not machine-clean — a corrupt requirements.txt
# otherwise surfaces as an opaque failure minutes later, deep in a
# container build (2026-07-24).
if LC_ALL=C grep -q '[^[:print:][:space:]]' "$STAGE/requirements.txt"; then
  echo "FATAL: requirements.txt contains control bytes (ANSI colour leaked into a parsed file); check NO_COLOR/FORCE_COLOR" >&2
  exit 2
fi
# nexus-mt1tj: the lock resolves Linux torch/torchvision from the PyTorch CPU
# index (torch==2.8.0+cpu), and `uv export` writes the pinned versions but not
# the index they live on, so the image's `uv pip install -r` would look for a
# +cpu build on PyPI and fail. Name the index at the top of the file; PyPI
# stays the default index for everything else.
{ printf -- '--extra-index-url https://download.pytorch.org/whl/cpu\n'; cat "$STAGE/requirements.txt"; } > "$STAGE/requirements.txt.tmp" \
  && mv "$STAGE/requirements.txt.tmp" "$STAGE/requirements.txt"
if [ "$ERA_HOP" = 1 ]; then
  # nexus-n7u38.30: same posture as --package-upgrade (working-tree wheel in its
  # own subdirectory, real PEP 427 name preserved, no engine artifact staged at
  # all — BOTH engines are acquired at runtime by the product's own code) PLUS
  # the seeder, which writes the ancient Chroma/T2/catalog state under the ERA
  # release's own libraries.
  mkdir -p "$STAGE/worktree-wheel"
  cp "$(ls -t dist/conexus-*.whl | head -1)" "$STAGE/worktree-wheel/"
  cp "$HERE/Dockerfile.era-hop" "$STAGE/Dockerfile"
  cp "$HERE/rehearse_era_hop.sh" "$HERE/seed_legacy.py" "$STAGE/"
elif [ "$PACKAGE_UPGRADE" = 1 ]; then
  # nexus-cfgo9: the UPGRADE-TARGET wheel travels in under its OWN
  # subdirectory (real PEP 427 filename preserved — pip/uv parse the wheel
  # filename strictly and a prefix-mangled name fails with "invalid
  # version") so it never collides with the driver script's
  # `pip install conexus==$PREV_RELEASE` from real PyPI into the SAME venv.
  # No engine artifact is staged at all (both $PREV_ENGINE_TAG and
  # $NEW_ENGINE_TAG are acquired at runtime by the product's own code — the
  # harness never supplies an engine binary).
  mkdir -p "$STAGE/worktree-wheel"
  if [ -n "$NEXUS_TARGET_RELEASE" ]; then
    # nexus-86mx2: PUBLISHED-TARGET mode — download the REAL published wheel
    # from PyPI instead of building the working tree. Resolved + verified
    # via PyPI's own JSON API (never `pip download`: this box's dev venv has
    # no `pip` module, and a direct JSON-API fetch resolves the exact wheel
    # URL + expected digest in one round trip with no dependency-resolution
    # surface to trust).
    echo "[run.sh] NEXUS_TARGET_RELEASE=$NEXUS_TARGET_RELEASE — downloading the PUBLISHED wheel from PyPI (not the worktree build)"
    PYPI_META="$(mktemp)"
    curl -fsSL "https://pypi.org/pypi/conexus/$NEXUS_TARGET_RELEASE/json" -o "$PYPI_META" \
      || { rm -f "$PYPI_META"; echo "FATAL: could not fetch PyPI metadata for conexus==$NEXUS_TARGET_RELEASE — is it published?" >&2; exit 1; }
    TARGET_INFO="$(python3 -c "
import json
with open('$PYPI_META') as f:
    d = json.load(f)
for u in d['urls']:
    if u['packagetype'] == 'bdist_wheel':
        print(u['url'])
        print(u['digests']['sha256'])
        print(u['filename'])
        break
else:
    raise SystemExit(1)
" 2>/dev/null)" || { rm -f "$PYPI_META"; echo "FATAL: no bdist_wheel asset for conexus==$NEXUS_TARGET_RELEASE on PyPI" >&2; exit 1; }
    rm -f "$PYPI_META"
    TARGET_WHEEL_URL="$(sed -n '1p' <<<"$TARGET_INFO")"
    TARGET_WHEEL_SHA256="$(sed -n '2p' <<<"$TARGET_INFO")"
    TARGET_WHEEL_NAME="$(sed -n '3p' <<<"$TARGET_INFO")"
    curl -fsSL -o "$STAGE/worktree-wheel/$TARGET_WHEEL_NAME" "$TARGET_WHEEL_URL" \
      || { echo "FATAL: download of $TARGET_WHEEL_URL failed" >&2; exit 1; }
    GOT_SHA256="$(python3 -c "
import hashlib
h = hashlib.sha256()
with open('$STAGE/worktree-wheel/$TARGET_WHEEL_NAME', 'rb') as f:
    for chunk in iter(lambda: f.read(1 << 20), b''):
        h.update(chunk)
print(h.hexdigest())
")"
    [ "$GOT_SHA256" = "$TARGET_WHEEL_SHA256" ] \
      || { echo "FATAL: downloaded wheel sha256 mismatch for conexus==$NEXUS_TARGET_RELEASE: got $GOT_SHA256, PyPI JSON API says $TARGET_WHEEL_SHA256" >&2; exit 1; }
    echo "[run.sh] verified $TARGET_WHEEL_NAME sha256=$TARGET_WHEEL_SHA256 (matches PyPI JSON API digest)"
  else
    cp "$(ls -t dist/conexus-*.whl | head -1)" "$STAGE/worktree-wheel/"
  fi
  cp "$HERE/Dockerfile.package-upgrade" "$STAGE/Dockerfile"
  cp "$HERE/rehearse_package_upgrade.sh" "$STAGE/"
elif [ "$STRANDED" = 1 ]; then
  # nexus-8nlj4: the WORKING-TREE wheel travels in under its OWN subdirectory
  # (real PEP 427 filename preserved — see the --package-upgrade rationale
  # above) so it never collides with the driver's `pip install
  # conexus==$PIN_RELEASE` from real PyPI into the SAME venv. seed_legacy.py
  # is the SAME raw-sqlite/chromadb seeder era-hop/fullstack already use
  # (nexus-8nlj4 2026-08-08 note: it is live raw material, not
  # dead weight) — it writes the pre-PG artifacts under the PIN release's
  # own libraries before the driver package-upgrades over them. No engine
  # artifact or PG bundle is staged (both the pin's own engine and the
  # working tree's required engine are acquired at runtime by the product's
  # own code).
  mkdir -p "$STAGE/worktree-wheel"
  cp "$(ls -t dist/conexus-*.whl | head -1)" "$STAGE/worktree-wheel/"
  cp "$HERE/Dockerfile.stranded" "$STAGE/Dockerfile"
  cp "$HERE/rehearse_stranded.sh" "$HERE/seed_legacy.py" "$STAGE/"
elif [ "$ACQUIRE" = 1 ]; then
  # nexus-1ddsy: same bare-box image as the retired cold leg — nothing the
  # service needs is staged, because acquiring it from the PUBLISHED release IS
  # the thing under test.
  cp "$HERE/Dockerfile.cold" "$STAGE/Dockerfile"
  cp "$HERE/rehearse_cold.sh" "$HERE/rehearse_hole_punch.sh" "$HERE/rehearse_acquire.sh" "$HERE/seed_legacy.py" "$STAGE/"
elif [ "$COLD" = 1 ] || [ "$HOLE_PUNCH" = 1 ]; then
  # nexus-4mm24: NOTHING the service needs is staged — the cold box acquires the
  # binary + PG bundle from the published release at runtime. Only the wheel +
  # both cold drivers (rehearse_cold.sh, rehearse_hole_punch.sh — nexus-s3dd4.7)
  # + seed travel in; the entrypoint below picks the right one.
  cp "$HERE/Dockerfile.cold" "$STAGE/Dockerfile"
  cp "$HERE/rehearse_cold.sh" "$HERE/rehearse_hole_punch.sh" "$HERE/seed_legacy.py" "$STAGE/"
elif [ "$FULLSTACK" = 1 ] || [ "$SHAKEOUT_E2E" = 1 ]; then
  # Full topology: native binary + the fullstack Dockerfile (adds linux claude) +
  # BOTH drivers it now unconditionally COPYs (rehearse_fullstack.sh AND
  # rehearse_shakeout_e2e.sh — Dockerfile.fullstack is shared verbatim by
  # both journeys, so the staged context must satisfy every COPY line in it
  # regardless of which flag triggered this build; the entrypoint override
  # below picks the right one to actually RUN). Same native-binary staging
  # as the default path.
  mkdir -p "$STAGE/native"
  cp service/target/nexus-service "$STAGE/native/"
  if compgen -G "service/target/*.so" > /dev/null; then
    cp service/target/*.so "$STAGE/native/"
  fi
  cp "$HERE/Dockerfile.fullstack" "$STAGE/Dockerfile"
  cp "$HERE/rehearse_fullstack.sh" "$HERE/rehearse_shakeout_e2e.sh" "$HERE/seed_legacy.py" "$STAGE/"
elif [ "$CANDIDATE_MIGRATION" = 1 ]; then
  # nexus-z0ylb: BOTH staging shapes at once — the native/ candidate (like
  # the default/--shakeout path: the locally-built, now-stamped -Ob binary
  # + its .so siblings, hand-swapped in at Stage 4) AND the working-tree
  # wheel under its own subdirectory (like --era-hop/--package-upgrade:
  # installed via `uv tool install` at runtime, never
  # colliding with anything `pip`/`uv` resolves from real PyPI — this leg
  # installs no OLD release at all, so there is nothing to collide with,
  # but the own-subdirectory convention is kept for staging uniformity).
  # No engine artifact of any kind travels in — the FLOOR engine is
  # acquired for real by `nx daemon service install-binary` inside the
  # container (Stage 2).
  mkdir -p "$STAGE/native" "$STAGE/worktree-wheel"
  cp service/target/nexus-service "$STAGE/native/"
  if compgen -G "service/target/*.so" > /dev/null; then
    cp service/target/*.so "$STAGE/native/"
  fi
  cp "$(ls -t dist/conexus-*.whl | head -1)" "$STAGE/worktree-wheel/"
  cp "$HERE/Dockerfile.candidate-migration" "$STAGE/Dockerfile"
  cp "$HERE/rehearse_candidate_migration.sh" "$STAGE/"
else
  # The native binary travels into the image. A LOCAL -Pnative -Ob quick build also
  # emits native-image .so siblings (libjvm/libawt/liblcms/...) that must be
  # co-located (native-image dlopen's JDK libs from the executable's own dir); a
  # RELEASE binary (engine-service-v*) is self-contained with NO .so siblings. So
  # the .so copy is best-effort — present them when they exist, skip when they don't.
  mkdir -p "$STAGE/native"
  cp service/target/nexus-service "$STAGE/native/"
  if compgen -G "service/target/*.so" > /dev/null; then
    cp service/target/*.so "$STAGE/native/"
  fi
  cp "$HERE/Dockerfile" "$HERE/rehearse.sh" "$HERE/rehearse_shakeout.sh" "$HERE/seed_legacy.py" "$STAGE/"
  # nexus-l8xnz: the SAME service/native-smoke.sh the release workflow runs
  # (byte-for-byte -- callers are adapted, never the script). rehearse_
  # shakeout.sh's Phase F drives it against this candidate so a stale probe
  # fixture (the v0.1.77 16-char doc_id class) fails HERE, pre-tag, instead
  # of only inside engine-service-release.yml.
  cp service/native-smoke.sh "$STAGE/"
  # lib/ must reach the build context, not just the image: the Dockerfile's
  # COPY reads from HERE-staged files only, so a driver's `source lib/...`
  # silently resolves to nothing without this (nexus-xm0cp's Phase D census
  # was undefined in-container for its whole life; caught 2026-08-10).
  # Directory-wide on both sides so a second lib does not repeat it.
  cp -R "$HERE/lib" "$STAGE/lib"
fi

# Docker Desktop's credsStore=desktop helper can't reach a locked login keychain
# in a non-interactive session, which fails even cached/anonymous image
# resolution at build time. Temporarily strip credsStore (the auths entries are
# empty), restore on exit. docker run is unaffected (only build-time auth fails).
DCFG="$HOME/.docker/config.json"
if [ -f "$DCFG" ] && grep -q '"credsStore"' "$DCFG"; then
  cp "$DCFG" "$STAGE/.docker-config.bak"
  python3 -c "import json,os;p=os.path.expanduser('~/.docker/config.json');d=json.load(open(p));d.pop('credsStore',None);json.dump(d,open(p,'w'),indent=2)"
  trap 'diag_exit_guard; _guided_restore; cp "$STAGE/.docker-config.bak" "$DCFG"; rm -rf "$STAGE"; build_lease_release service 2>/dev/null || true; lock_release "$LOCKDIR" 2>/dev/null || true' EXIT
  echo "      (temporarily stripped credsStore from ~/.docker/config.json — restored on exit)"
fi

# nexus-myk4e/nexus-5votw: the image's bge fetch defaults to the self-hosted
# GitHub release asset (ci-assets-bge-768-v1, set in the Dockerfile ARGs);
# NEXUS_BGE_MODEL_URL/NEXUS_BGE_TOKENIZER_URL override for a re-cut asset tag.
BUILD_ARGS=()
[ -n "${NEXUS_BGE_MODEL_URL:-}" ] && BUILD_ARGS+=(--build-arg "BGE_MODEL_URL=$NEXUS_BGE_MODEL_URL")
[ -n "${NEXUS_BGE_TOKENIZER_URL:-}" ] && BUILD_ARGS+=(--build-arg "BGE_TOKENIZER_URL=$NEXUS_BGE_TOKENIZER_URL")
# Progress streams deliberately (no -q): the image build is the longest quiet
# stage of a run (14-18 min uncached, measured 2026-07-21) and -q made a slow
# build indistinguishable from a hang. The step timings it prints are also the
# evidence base for the layer-caching work (nexus-imkxs).
docker build ${BUILD_ARGS[@]+"${BUILD_ARGS[@]}"} -f "$STAGE/Dockerfile" -t "$IMAGE" "$STAGE"

run_env=(-e "WITH_CLOUD=$WITH_CLOUD" -e "COMPREHENSIVE=$COMPREHENSIVE" -e "STRESS=$STRESS")
if [ "$ACQUIRE" = 1 ]; then
  # nexus-1ddsy: the tag under test is supplied by the operator and is NOT
  # defaulted — the whole point is to exercise a specific published artifact.
  run_env+=(-e "NEXUS_SERVICE_TAG=$NEXUS_SERVICE_TAG")
elif [ "$COLD" = 1 ] || [ "$HOLE_PUNCH" = 1 ]; then
  # nexus-4mm24 / nexus-s3dd4.7: tell the cold box which published release to
  # acquire from (--hole-punch needs v0.1.18+ for /v1/telemetry/ids/probe).
  run_env+=(-e "NEXUS_SERVICE_TAG=$COLD_TAG")
fi
if [ "$PACKAGE_UPGRADE" = 1 ]; then
  run_env+=(-e "PREV_RELEASE=$PREV_RELEASE" -e "PREV_ENGINE_TAG=$PREV_ENGINE_TAG" -e "NEW_ENGINE_TAG=$NEW_ENGINE_TAG")
  # nexus-0j6gy follow-on: uv defaults to a 30s HTTP timeout INSIDE the
  # container, and Stage 1 pip-installs an OLD release whose transitive set
  # pulls hundred-MB wheels (onnxruntime, nvidia-cufft via mineru->torch).
  # Raising UV_HTTP_TIMEOUT on the HOST does nothing: this -e list is the only
  # channel into the container, so the value was silently discarded while the
  # failure text told the operator to raise exactly this variable. Observed
  # twice on 2026-08-24; only a `bash -x` trace showed the -e list.
  run_env+=(-e "UV_HTTP_TIMEOUT=${UV_HTTP_TIMEOUT:-300}")
  # nexus-86mx2: forward which upgrade target staged above (published wheel
  # vs worktree build) so rehearse_package_upgrade.sh's own logging + verdict
  # line can NAME it — a log reader must never have to guess which axis ran.
  [ -n "$NEXUS_TARGET_RELEASE" ] && run_env+=(-e "TARGET_RELEASE=$NEXUS_TARGET_RELEASE")
fi
if [ "$ERA_HOP" = 1 ]; then
  run_env+=(-e "ERA_RELEASE=$ERA_RELEASE" -e "ERA_ENGINE_TAG=$ERA_ENGINE_TAG" -e "NEW_ENGINE_TAG=$NEW_ENGINE_TAG")
fi
if [ "$STRANDED" = 1 ]; then
  run_env+=(-e "PIN_RELEASE=$STRAND_PIN_RELEASE")
fi
if [ "$CANDIDATE_MIGRATION" = 1 ]; then
  run_env+=(-e "FLOOR_VERSION=$GUIDED_STAMP_VERSION")
  # nexus-z0ylb: optional non-vacuity knob — when set, the leg asserts the
  # changeset delta EQUALS this count instead of merely reporting it. Only
  # forwarded when actually set (same "dead through the only documented
  # entrypoint" lesson as the shakeout-e2e knobs below): a Phase 5 run that
  # knows the candidate's exact new-changeset count can pin it; ordinary
  # runs report the delta without asserting a value.
  [ -n "${EXPECT_NEW_CHANGESETS:-}" ] && \
    run_env+=(-e "EXPECT_NEW_CHANGESETS=$EXPECT_NEW_CHANGESETS")
fi
if [ "$SHAKEOUT_E2E" = 1 ]; then
  # Forward the two knobs rehearse_shakeout_e2e.sh advertises in its own header.
  # Without this they are DEAD through the only documented entrypoint: setting
  # them on the host has no effect inside the container (code-review finding,
  # 2026-08-09). Only forwarded when actually set, so the in-container defaults
  # (RSS budget 3 GB; index timeout 1800s) stay authoritative otherwise.
  [ -n "${NX_SHAKEOUT_E2E_RSS_BUDGET_GB:-}" ] && \
    run_env+=(-e "NX_SHAKEOUT_E2E_RSS_BUDGET_GB=$NX_SHAKEOUT_E2E_RSS_BUDGET_GB")
  [ -n "${NX_SHAKEOUT_E2E_INDEX_TIMEOUT_S:-}" ] && \
    run_env+=(-e "NX_SHAKEOUT_E2E_INDEX_TIMEOUT_S=$NX_SHAKEOUT_E2E_INDEX_TIMEOUT_S")
fi
if [ "$WITH_CLOUD" = 1 ]; then
  # Forward the Voyage key from .env (export VOYAGE_API_KEY=…) under both names
  # the code probes. Never echoed.
  # shellcheck disable=SC1091
  set +u; . ./.env 2>/dev/null || true; set -u
  key="${VOYAGE_API_KEY:-${NX_VOYAGE_API_KEY:-}}"
  [ -n "$key" ] || { echo "--with-cloud needs VOYAGE_API_KEY in .env" >&2; exit 1; }
  run_env+=(-e "NX_VOYAGE_API_KEY=$key" -e "VOYAGE_API_KEY=$key")
fi

# NOT `exec` — exec replaces this shell and would suppress the EXIT trap that
# restores ~/.docker/config.json + release.properties and removes the staging
# dir. Run as a child and propagate its exit code.
if [ "$FULLSTACK" = 1 ]; then
  # Provide FRESH claude oauth so the in-container `claude -p` (MCP driver + real
  # aspect extraction) authenticates. The ~/.claude/.credentials.json FILE goes
  # stale within ~1h (oauth access tokens are short-lived + the refresh token
  # rotates); the live token lives in the macOS keychain. Pull it at run time
  # (same approach as tests/cc-validation), stage it (ephemeral, cleaned on exit),
  # mount read-only. Real, billed calls; data/PG stay container-isolated.
  FRESHCREDS="$(security find-generic-password -s 'Claude Code-credentials' -w 2>/dev/null || true)"
  if [ -z "$FRESHCREDS" ] && [ -f "$HOME/.claude/.credentials.json" ]; then
    echo "      (keychain miss — falling back to ~/.claude/.credentials.json, may be stale)" >&2
    FRESHCREDS="$(cat "$HOME/.claude/.credentials.json")"
  fi
  [ -n "$FRESHCREDS" ] || { echo "--fullstack needs claude oauth (keychain 'Claude Code-credentials' or ~/.claude/.credentials.json)" >&2; exit 1; }
  printf '%s' "$FRESHCREDS" > "$STAGE/.claude-credentials.json"; chmod 600 "$STAGE/.claude-credentials.json"
  docker run --rm "${run_env[@]}" \
    -v "$STAGE/.claude-credentials.json":/home/nexus/.claude/.credentials.json:ro \
    "$IMAGE"
elif [ "$SHAKEOUT_E2E" = 1 ]; then
  # nexus-33hpq-class daily-driver shakeout: same fresh-oauth mounting as
  # --fullstack (Step 7's MCP workload is a real, billed claude -p call
  # too), but override the image's default entrypoint (rehearse_fullstack.sh)
  # to run this journey's own driver instead — mirrors how --acquire/
  # --shakeout override the entrypoint on a shared/reused image.
  FRESHCREDS="$(security find-generic-password -s 'Claude Code-credentials' -w 2>/dev/null || true)"
  if [ -z "$FRESHCREDS" ] && [ -f "$HOME/.claude/.credentials.json" ]; then
    echo "      (keychain miss — falling back to ~/.claude/.credentials.json, may be stale)" >&2
    FRESHCREDS="$(cat "$HOME/.claude/.credentials.json")"
  fi
  [ -n "$FRESHCREDS" ] || { echo "--shakeout-e2e needs claude oauth (keychain 'Claude Code-credentials' or ~/.claude/.credentials.json)" >&2; exit 1; }
  printf '%s' "$FRESHCREDS" > "$STAGE/.claude-credentials.json"; chmod 600 "$STAGE/.claude-credentials.json"
  docker run --rm "${run_env[@]}" \
    -v "$STAGE/.claude-credentials.json":/home/nexus/.claude/.credentials.json:ro \
    --entrypoint /bin/bash "$IMAGE" /home/nexus/rehearse_shakeout_e2e.sh
elif [ "$HOLE_PUNCH" = 1 ]; then
  # nexus-s3dd4.7: override the cold box's default entrypoint to drive the
  # verify-fill hole-punch journey instead of the plain cold-acquire MVV.
  docker run --rm "${run_env[@]}" --entrypoint /bin/bash "$IMAGE" \
    /home/nexus/rehearse_hole_punch.sh
elif [ "$PACKAGE_UPGRADE" = 1 ]; then
  # nexus-cfgo9: Dockerfile.package-upgrade's default entrypoint IS
  # rehearse_package_upgrade.sh.
  docker run --rm "${run_env[@]}" "$IMAGE"
elif [ "$ERA_HOP" = 1 ]; then
  # nexus-n7u38.30: Dockerfile.era-hop's default entrypoint IS
  # rehearse_era_hop.sh.
  docker run --rm "${run_env[@]}" "$IMAGE"
elif [ "$STRANDED" = 1 ]; then
  # nexus-8nlj4: Dockerfile.stranded's default entrypoint IS
  # rehearse_stranded.sh.
  docker run --rm "${run_env[@]}" "$IMAGE"
elif [ "$CANDIDATE_MIGRATION" = 1 ]; then
  # nexus-z0ylb: Dockerfile.candidate-migration's default entrypoint IS
  # rehearse_candidate_migration.sh.
  docker run --rm "${run_env[@]}" "$IMAGE"
elif [ "$COLD" = 1 ]; then
  # nexus-4mm24: Dockerfile.cold's default entrypoint IS rehearse_cold.sh.
  docker run --rm "${run_env[@]}" "$IMAGE"
elif [ "$ACQUIRE" = 1 ]; then
  # nexus-1ddsy: drive the PUBLISHED artifact acquired at runtime.
  docker run --rm "${run_env[@]}" --entrypoint /bin/bash "$IMAGE" \
    /home/nexus/rehearse_acquire.sh
elif [ "$SHAKEOUT" = 1 ]; then
  # nexus-h8rf6: candidate shakeout — verb matrix + incremental-index +
  # concurrent-load assertions against the locally-built candidate binary.
  docker run --rm "${run_env[@]}" --entrypoint /bin/bash "$IMAGE" \
    /home/nexus/rehearse_shakeout.sh
# --guided is RETIRED (RDR-155 P4b) and refused pre-build above; there is no
# GUIDED branch here any more, and rehearse_guided.sh no longer ships
# (nexus-lgdel.l2 — its own leg was the file's only consumer).
else
  docker run --rm "${run_env[@]}" "$IMAGE"
fi
rc=$?
exit "$rc"
