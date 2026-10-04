#!/usr/bin/env bash
# Host orchestrator for the two container journeys that survive in this
# directory.
#
#   tests/e2e/migration-rehearsal/run.sh --package-upgrade
#   NEXUS_TARGET_RELEASE=X.Y.Z tests/e2e/migration-rehearsal/run.sh --package-upgrade
#   NEXUS_SERVICE_TAG=engine-service-vX.Y.Z tests/e2e/migration-rehearsal/run.sh --acquire
#
# --package-upgrade (nexus-cfgo9): the ONE-engine convergence MVV. A package-only
#   upgrade from a real previous release (PREV_RELEASE, installed from PyPI) to
#   the working-tree wheel, the engines acquired for real by the product's own
#   code, never supplied by this harness. Docker image: Dockerfile.package-upgrade,
#   driver: rehearse_package_upgrade.sh. NEXUS_TARGET_RELEASE=X.Y.Z switches the
#   upgrade TARGET to the real PUBLISHED PyPI wheel (sha256-verified against
#   PyPI's own JSON API) instead of the worktree build (nexus-86mx2).
#
# --acquire (nexus-1ddsy): the PUBLISHED-artifact gate. Cold-acquires
#   NEXUS_SERVICE_TAG (mandatory, never defaulted: it is the artifact under test)
#   on a bare box and drives it. Docker image: Dockerfile.cold, driver:
#   rehearse_acquire.sh.
#
# Neither journey builds a native binary: every engine is acquired at runtime
# from a published release. Only the conexus wheel is built, on the host.
#
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
set -euo pipefail
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

cd "$(git -C "$SCRIPT_DIR" rev-parse --show-toplevel)"  # the SCRIPT's checkout, never the caller's cwd: invoked from another checkout this built the wrong tree (2026-09-05 A/B)
HERE="tests/e2e/migration-rehearsal"
# One interpreter >= 3.10, resolved once; never a bare python3 (nexus-u67ow).
# shellcheck source=lib/python.sh disable=SC1091
source "$SCRIPT_DIR/../lib/python.sh"
e2e_python_resolve || exit 2
IMAGE="nexus-migration-rehearsal"
ACQUIRE=0
PACKAGE_UPGRADE=0
# The release_version the current tree pins: derived from the product constant
# (engine_version.REQUIRED_ENGINE_VERSION — the ONLY floor constant after the
# nexus-b6qlf unification) so it can never go stale. No fallback: if the
# constant can't be parsed, abort loudly.
CURRENT_ENGINE_VERSION="$(
  "$E2E_PYTHON" - <<'PY'
import re, pathlib
src = pathlib.Path("src/nexus/engine_version.py").read_text()
m = re.search(r"REQUIRED_ENGINE_VERSION[^=]*=\s*\((\d+),\s*(\d+),\s*(\d+)\)", src)
print(".".join(m.groups()) if m else "")
PY
)"
[ -n "$CURRENT_ENGINE_VERSION" ] || { echo "FATAL: could not parse REQUIRED_ENGINE_VERSION from src/nexus/engine_version.py — fix the regex/path before rehearsing" >&2; exit 2; }

# nexus-cfgo9: the PACKAGE-UPGRADE leg's starting point — a REAL, already
# published PyPI release + the engine tag ITS OWN PINNED_SERVICE_TAG resolves
# to. DERIVED, NOT HAND-TYPED (2026-08-19): both values are facts already in
# this repo's history, so they are computed from it rather than re-typed at
# every floor bump. PREV_RELEASE is the newest published `v*` tag that is NOT
# the current working tree's own version, and PREV_ENGINE_TAG is whatever
# engine_version.py pinned AT that tag. THE UNIT IS RELEASES, NOT ENGINE TAGS:
# an engine tag that is cut, published and gated but never pinned by any
# release is a SKIPPED version and never appears in any release tag's
# engine_version.py, so it can never be selected here. Override either via the
# NEXUS_* env vars; a derivation that comes up empty fails loud rather than
# falling back to a stale literal.
# Reads REQUIRED_ENGINE_VERSION as a dotted tuple from a release tag's tree.
# Empty (NOT fatal) when a tag predates the constant or cannot be read, so the
# walk below skips such tags instead of aborting on the oldest history.
_engine_tuple_at_release() {
  git show "v$1:src/nexus/engine_version.py" 2>/dev/null \
    | sed -n 's/^REQUIRED_ENGINE_VERSION[^(]*(\([0-9]*\), *\([0-9]*\), *\([0-9]*\)).*/\1.\2.\3/p' \
    | head -1
}
# NOT ALWAYS THE IMMEDIATELY-PRECEDING RELEASE (2026-08-22). The selector is
# "the newest release that pinned a STRICTLY OLDER engine than this tree does"
# — because a release that bumps NO floor is a normal shape, and for one of
# those the immediately-preceding release pins the SAME engine, leaving this
# leg with nothing to converge (7.14.0 and 7.15.0 both pin 0.1.85, as did
# 7.8.0/7.9.0 and 7.6.0/7.6.1 before them). Walking back to the newest
# genuinely-older pin keeps the hop REAL (a user on that release upgrading to
# this one) and keeps the convergence assertion non-vacuous, which is the
# whole point of the leg (nexus-cfgo9, GH #1402).
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
# nexus-86mx2 (2026-08-14) PUBLISHED-TARGET mode for --package-upgrade: when
# set, the UPGRADE TARGET is the real PUBLISHED PyPI wheel for that version
# instead of the working-tree build — the published-BYTES upgrade journey,
# closing the loop the pre-tag worktree run cannot prove ("identical tree" is
# an argument, not a run; see the release skill's post-publish step). Unset
# (default) leaves the worktree-wheel behavior below completely unchanged.
# The wheel is downloaded + sha256-verified against PyPI's own JSON API
# digest — fail loud on mismatch, never a silently-wrong artifact staged into
# the box.
NEXUS_TARGET_RELEASE="${NEXUS_TARGET_RELEASE:-}"
# The NEW required engine — the same constant CURRENT_ENGINE_VERSION is, so
# this leg tracks a floor bump automatically (nexus-b6qlf: one source of truth).
NEW_ENGINE_TAG="engine-service-v${CURRENT_ENGINE_VERSION}"

for a in "$@"; do
  case "$a" in
    --package-upgrade) PACKAGE_UPGRADE=1 ;;  # standalone: nexus-cfgo9 ONE-engine convergence MVV — package-only upgrade from a real previous release, engine acquired for real by the product, never supplied by this harness
    --acquire)         ACQUIRE=1 ;;          # standalone: nexus-1ddsy PUBLISHED-artifact gate — cold-acquire NEXUS_SERVICE_TAG on a bare box and drive it
    *) echo "unknown arg: $a (journeys: --package-upgrade | --acquire)" >&2; exit 2 ;;
  esac
done
{ [ "$PACKAGE_UPGRADE" = 1 ] || [ "$ACQUIRE" = 1 ]; } || { echo "name a journey: --package-upgrade | --acquire" >&2; exit 2; }
[ "$PACKAGE_UPGRADE" = 1 ] && [ "$ACQUIRE" = 1 ] && { echo "--package-upgrade and --acquire are standalone journeys (each its own entrypoint); do not combine them" >&2; exit 2; }

if [ "$PACKAGE_UPGRADE" = 1 ]; then
  PREV_RELEASE="${NEXUS_PREV_RELEASE:-$(_derive_prev_release)}"
  PREV_ENGINE_TAG="${NEXUS_PREV_ENGINE_TAG:-$(_derive_prev_engine_tag "$PREV_RELEASE")}"
  [ "${PREV_ENGINE_TAG#engine-service-v}" = "$CURRENT_ENGINE_VERSION" ] && {
    echo "FATAL: PREV_ENGINE_TAG ($PREV_ENGINE_TAG) already equals the current REQUIRED_ENGINE_VERSION ($CURRENT_ENGINE_VERSION) — the package-upgrade scenario is no longer 'stale'. Set NEXUS_PREV_RELEASE/NEXUS_PREV_ENGINE_TAG to the release immediately before this floor bump." >&2
    exit 2
  }
fi
# nexus-1ddsy: --acquire validates a PUBLISHED tag, so the tag is mandatory and
# there is nothing to infer: NEXUS_SERVICE_TAG is the artifact under test,
# never a default.
[ "$ACQUIRE" = 1 ] && [ -z "${NEXUS_SERVICE_TAG:-}" ] && { echo "--acquire requires NEXUS_SERVICE_TAG=<published tag>, e.g. NEXUS_SERVICE_TAG=engine-service-v0.1.55 (it exercises the PUBLISHED artifact, not a local build)" >&2; exit 2; }
[ -z "$NEXUS_TARGET_RELEASE" ] || [ "$PACKAGE_UPGRADE" = 1 ] || { echo "NEXUS_TARGET_RELEASE only applies to --package-upgrade" >&2; exit 2; }

LEG="package-upgrade"
[ "$ACQUIRE" = 1 ] && LEG="acquire"

# RDR-184 P0.2 (nexus-ccs9v.2): serialize on the machine-global fixed
# resources this harness mutates — the fixed docker tag ($IMAGE) and the
# shared dist/ wheel output (the near-miss that motivated this bead: two
# concurrent rehearsals racing the same wheel/image). The lock dir lives
# under a stable machine-global temp root, NOT under this checkout — the
# resource being serialized (one docker daemon, one dist/ per host) is
# machine-global, so two different checkouts on the same host must still
# serialize against each other. Acquired here, after arg parsing/validation
# (usage errors don't need the lock) but strictly before the first mutation.
# Lock dir is a HARD-CODED /tmp path, deliberately NOT ${TMPDIR:-/tmp}
# (code-review SIGNIFICANT fix): on darwin, an interactive shell's TMPDIR is a
# per-user /var/folders/... path while a LaunchAgent/CI/stripped-env invocation
# sees plain /tmp — two different invocation contexts would silently compute
# DIFFERENT lockdirs and never contend, defeating the whole point of a
# machine-global guard. /tmp is always the same path across every context on
# the same host.
# shellcheck source=../lib/lock.sh disable=SC1091
source "$SCRIPT_DIR/../lib/lock.sh"
# Per-USER root (nexus-c6lsu): the uid is in the name, because /tmp is shared across Unix users and a
# root another user created is unwritable here (lock_acquire fails). $(id -u) is context-independent,
# so the cross-context contention above is unchanged for one user.
LOCKDIR="/tmp/nexus-e2e-locks-$(id -u)/migration-rehearsal.lock"
mkdir -p "$(dirname "$LOCKDIR")"
lock_acquire "$LOCKDIR" || exit 1
# Reassign the trap to the lock-aware form only now that $LOCKDIR is assigned:
# a trap that referenced it earlier would abort on its own evaluation under
# `set -u` when an argument-conflict guard above fired `exit 2`.
trap 'diag_exit_guard; lock_release "$LOCKDIR" 2>/dev/null || true' EXIT
echo "[rdr-184] lock acquired: $LOCKDIR (pid $$)" >&2
# Test seam (RDR-184 P0.2, nexus-ccs9v.2): tests/e2e/lib/harness_lock_test.sh
# sets this to prove a concurrent invocation gets PAST the lock without ever
# running this harness's real body (wheel build / docker).
# No-op — unset in every normal invocation.
[[ -n "${NX_E2E_LOCK_SELFTEST:-}" ]] && exit 0

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

# stage_wheel <dest-dir>: the freshest dist/conexus-*.whl, keeping its real
# PEP 427 name (pip/uv parse the wheel filename strictly).
stage_wheel() {
  local wheels
  wheels="$(ls -t dist/conexus-*.whl)"   # captured, not piped into head: pipefail would promote its SIGPIPE
  cp "${wheels%%$'\n'*}" "$1/"
}

# ── Pre-flight Docker disk-pressure check (nexus-h8rf6.13) ────────────────────
# The recurring barf is Docker Desktop's capped VM disk, not the host:
# iteration-heavy sessions accumulate build cache + dangling rehearsal-image
# generations until builds crawl (~80GB observed across 4 shakeout iterations).
# When reclaimable build cache exceeds the threshold, prune — with headroom
# generous enough to KEEP the hot layers (v0.1.21 lesson: an aggressive
# --reserved-space 6GB evicted the freshly-unreferenced 692MB bge model layer
# and forced a full re-download on the next build). Raised 12GB->40GB and
# trigger 10GB->40GB on 2026-07-21 (Hal authorized the disk). Old dangling
# image generations are pruned by age so the current lineage stays. Prune only
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
    echo "[preflight] Docker build cache reclaimable ~${reclaimable_gb}GB (>40GB) — pruning (reserved-space 40GB keeps hot layers)…"
    docker builder prune -f --reserved-space 40GB 2>/dev/null | tail -1 || true
    # Belt: drop dangling (untagged) image generations older than a day —
    # this is what actually releases superseded rehearsal-image layers.
    docker image prune -f --filter 'until=24h' 2>/dev/null | tail -1 || true
  fi
}
preflight_docker_prune

echo "[stage] Staging a minimal build context + building image (LEG=$LEG)…"
# Flatten wheel + driver to fixed names in a tiny throwaway context. The
# repo .dockerignore excludes dist/, so staging sidesteps it without touching
# the shared .dockerignore.
STAGE="$(mktemp -d)"
trap 'diag_exit_guard; rm -rf "$STAGE"; lock_release "$LOCKDIR" 2>/dev/null || true' EXIT
stage_wheel "$STAGE"
# Lock-derived dependency manifest for the split install layer (Dockerfile.cold):
# the wheel's bytes churn every build (embedded mtimes), so a deps-install layer
# keyed on the wheel re-ran its full 5-7 min closure install every run
# (measured 2026-07-21: 430s). Keying it on uv.lock content instead makes it a
# cache hit until dependencies actually change; the wheel itself installs
# --no-deps in a later cheap layer. stdout redirect, NOT -o: uv embeds the -o
# path in the header comment, and $STAGE is a fresh mktemp every run — that
# alone would bust the layer cache. --locked fails loud on a stale uv.lock
# instead of exporting a closure the wheel does not match.
# Runs unconditionally: --package-upgrade never COPYs it (it installs deps at
# runtime from real PyPI — that is the scenario's point), so for it this is a
# 1ms offline no-op in the context dir.
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
if [ "$PACKAGE_UPGRADE" = 1 ]; then
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
    TARGET_INFO="$("$E2E_PYTHON" -c "
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
    GOT_SHA256="$("$E2E_PYTHON" -c "
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
    stage_wheel "$STAGE/worktree-wheel"
  fi
  cp "$HERE/Dockerfile.package-upgrade" "$STAGE/Dockerfile"
  cp "$HERE/rehearse_package_upgrade.sh" "$STAGE/"
  # nexus-wo6sc: the rehearse script SOURCES lib/heartbeat_stall_note.sh, so
  # the library has to travel with it. Staged for the same reason the main
  # Dockerfile COPYs lib/ — without it the source fails inside the container
  # and the stall attribution is silently absent from exactly the failure
  # report it exists to annotate (measured 2026-09-13, battery at ef6d9c466).
  cp -R "$HERE/lib" "$STAGE/lib"
else
  # nexus-1ddsy: a bare-box image — nothing the service needs is staged,
  # because acquiring it from the PUBLISHED release IS the thing under test.
  cp "$HERE/Dockerfile.cold" "$STAGE/Dockerfile"
  cp "$HERE/rehearse_acquire.sh" "$STAGE/"
fi

# Docker Desktop's credsStore=desktop helper can't reach a locked login keychain
# in a non-interactive session, which fails even cached/anonymous image
# resolution at build time. Temporarily strip credsStore (the auths entries are
# empty), restore on exit. docker run is unaffected (only build-time auth fails).
DCFG="$HOME/.docker/config.json"
if [ -f "$DCFG" ] && grep -q '"credsStore"' "$DCFG"; then
  cp "$DCFG" "$STAGE/.docker-config.bak"
  "$E2E_PYTHON" -c "import json,os;p=os.path.expanduser('~/.docker/config.json');d=json.load(open(p));d.pop('credsStore',None);json.dump(d,open(p,'w'),indent=2)"
  trap 'diag_exit_guard; cp "$STAGE/.docker-config.bak" "$DCFG"; rm -rf "$STAGE"; lock_release "$LOCKDIR" 2>/dev/null || true' EXIT
  echo "      (temporarily stripped credsStore from ~/.docker/config.json — restored on exit)"
fi

# Progress streams deliberately (no -q): the image build is the longest quiet
# stage of a run (14-18 min uncached, measured 2026-07-21) and -q made a slow
# build indistinguishable from a hang. The step timings it prints are also the
# evidence base for the layer-caching work (nexus-imkxs).
docker build -f "$STAGE/Dockerfile" -t "$IMAGE" "$STAGE"

# nexus-h5olw follow-on: every rehearsal install is a throwaway, never a
# user; the anonymous install ping must not count it. `-e` is the only
# channel into the container, so the opt-out is forwarded here, not exported.
run_env=()
run_env+=(-e "NX_NO_TELEMETRY=1")
if [ "$ACQUIRE" = 1 ]; then
  # nexus-1ddsy: the tag under test is supplied by the operator and is NOT
  # defaulted — the whole point is to exercise a specific published artifact.
  run_env+=(-e "NEXUS_SERVICE_TAG=$NEXUS_SERVICE_TAG")
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

# NOT `exec` — exec replaces this shell and would suppress the EXIT trap that
# restores ~/.docker/config.json and removes the staging dir. Run as a child
# and propagate its exit code.
# nexus-cfgo9 / nexus-1ddsy: each image's default entrypoint IS its driver
# (Dockerfile.package-upgrade -> rehearse_package_upgrade.sh, Dockerfile.cold ->
# rehearse_acquire.sh).
docker run --rm "${run_env[@]}" "$IMAGE"
rc=$?
exit "$rc"
