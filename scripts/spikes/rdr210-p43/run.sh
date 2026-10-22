#!/usr/bin/env bash
# SPDX-License-Identifier: AGPL-3.0-or-later
# RDR-210 P4.3 ranking measurement (bead nexus-t189m): provision a FENCED scratch
# engine the way tests/e2e/dual-embedder-gate.sh does, run the harness against it.
#
#   scripts/spikes/rdr210-p43/run.sh --plan              # corpora, collections, token estimate; calls nothing
#   scripts/spikes/rdr210-p43/run.sh --keyless           # measurement B only, bge-only engine, no key, no Voyage
#   scripts/spikes/rdr210-p43/run.sh                     # full run (A, B, C) on a keyed dual engine
#   scripts/spikes/rdr210-p43/run.sh --cap-tokens 2500000    # raise the Voyage spend cap (default 1,500,000)
#   scripts/spikes/rdr210-p43/run.sh --clean             # also delete the work dir (and with it every collection) at the end
#
# Every other flag goes to p43_run.py (--only A,B,C, --limit-queries N, --resamples, --depths ...; see README.md).
#
# PERSISTENT WORK DIR (idempotent re-runs). Unlike the gate, the scratch config, its PG and the
# results live in $P43_WORKDIR (default ${TMPDIR:-/tmp}/nx-local-service-gate.p43) and SURVIVE the
# run, so a rerun after a failure reuses every collection already built (keyed by the corpus content
# hash) and re-embeds nothing. The engine and PG are stopped on exit either way. --clean removes it.
#
# THE KEY (full run only) is resolved from the same credential chain as the dual gate, before the home
# is fenced, held in a shell variable, passed to the engine through the environment of the daemon-start
# subshell only (never argv), never written to the scratch config, never printed. The Python harness
# never sees it. An absent key is a failure, never a skip.
#
# THE VOYAGE-PROFILE SEED BOOT (full run): a Voyage collection can only be registered while the install's
# profile is Voyage, so the first boot sets local.embed_model to a Voyage model, registers the two Voyage
# corpus collections, stops, and switches the config to bge. The engine under test then boots keyed with
# the bge profile (embedding_mode dual) and holds both families.
#
# Exit: 0 = ran; 1 = a check failed or the key is absent; 2 = the environment could not be set up;
#       3 = the plan is over the token cap (nothing was called).
set -euo pipefail
export NX_NO_TELEMETRY=1

REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/../../.." && pwd)"
HERE="$REPO_ROOT/scripts/spikes/rdr210-p43"

PLAN=0
KEYLESS=0
CLEAN=0
PY_ARGS=()
for a in "$@"; do
  case "$a" in
    --plan) PLAN=1; PY_ARGS+=("$a") ;;
    --keyless) KEYLESS=1; PY_ARGS+=("$a") ;;
    --clean) CLEAN=1 ;;
    *) PY_ARGS+=("$a") ;;
  esac
done

# ── --plan: no engine, no key, no scratch; the harness reads local files only ──
if [ "$PLAN" -eq 1 ]; then
  cd "$REPO_ROOT"
  exec uv run python "$HERE/p43_run.py" "${PY_ARGS[@]}"
fi

# ── The credential chain (full run only; resolved BEFORE the home is fenced) ──
p43_trim() {
  local v="$1"
  v="${v#"${v%%[![:space:]]*}"}"; v="${v%"${v##*[![:space:]]}"}"
  v="${v#[\"\']}"; v="${v%[\"\']}"
  v="${v#"${v%%[![:space:]]*}"}"; v="${v%"${v##*[![:space:]]}"}"
  case "$v" in null | '~') v="" ;; esac
  printf '%s' "$v"
}
p43_env_file_value() {  # <file> <NAME>
  [ -f "$1" ] || return 0
  awk -v want="$2" '{ line = $0; sub(/^[ \t]*export[ \t]+/, "", line) }
    index(line, want "=") == 1 { v = substr(line, length(want) + 2); sub(/[ \t]+#.*$/, "", v); print v; exit }' "$1"
}
p43_yml_credential() {  # <config.yml>
  [ -f "$1" ] || return 0
  awk '/^credentials:/ { inb = 1; next } /^[^ \t#]/ { inb = 0 }
    inb && $1 == "voyage_api_key:" { v = $0; sub(/^[ \t]*voyage_api_key:[ \t]*/, "", v); sub(/[ \t]+#.*$/, "", v); print v; exit }' "$1"
}
p43_resolve_voyage_key() {
  local v name envfile cfgdir
  for v in "${NX_VOYAGE_API_KEY:-}" "${VOYAGE_API_KEY:-}"; do
    v="$(p43_trim "$v")"
    if [ -n "$v" ]; then printf '%s' "$v"; return 0; fi
  done
  envfile="${NX_DUAL_GATE_ENV_FILE:-$REPO_ROOT/.env}"
  for name in NX_VOYAGE_API_KEY VOYAGE_API_KEY; do
    v="$(p43_trim "$(p43_env_file_value "$envfile" "$name")")"
    if [ -n "$v" ]; then printf '%s' "$v"; return 0; fi
  done
  cfgdir="${NEXUS_CONFIG_DIR:-$HOME/.config/nexus}"
  v="$(p43_trim "$(p43_yml_credential "$cfgdir/config.yml")")"
  if [ -n "$v" ]; then printf '%s' "$v"; fi
  return 0
}

P43_KEY=""
if [ "$KEYLESS" -eq 0 ]; then
  P43_KEY="$(p43_resolve_voyage_key)"
  if [ -z "$P43_KEY" ]; then
    {
      echo "[p43] FAIL: no Voyage API key (VOYAGE_API_KEY / NX_VOYAGE_API_KEY). The full run boots a KEYED dual engine."
      echo "[p43]   Looked: the environment; ${NX_DUAL_GATE_ENV_FILE:-$REPO_ROOT/.env}; credentials.voyage_api_key in ${NEXUS_CONFIG_DIR:-$HOME/.config/nexus}/config.yml."
      echo "[p43]   Use --keyless for measurement B on a bge-only engine, or --plan to see the spend first."
    } >&2
    exit 1
  fi
fi
unset VOYAGE_API_KEY NX_VOYAGE_API_KEY

# shellcheck source=tests/e2e/lib/python.sh disable=SC1091
source "$REPO_ROOT/tests/e2e/lib/python.sh"
e2e_python_resolve || exit 2
cd "$REPO_ROOT"

# ── Work dir and fence ───────────────────────────────────────────────────────
WORK="${P43_WORKDIR:-${TMPDIR:-/tmp}/nx-local-service-gate.p43}"
case "${WORK##*/}" in nx-local-service-gate.?*) ;; *)
  echo "[p43] ERROR: the work dir name must start with nx-local-service-gate. (the fence's own shape); got $WORK" >&2
  exit 2 ;;
esac
mkdir -p "$WORK"
SCRATCH="$WORK/cfg"          # NEXUS_CONFIG_DIR of the scratch install; PG data lives under it
mkdir -p "$SCRATCH" "$WORK/results"
export P43_WORKDIR="$WORK"
echo "[p43] work dir (persistent): $WORK"

REAL_HOME="$HOME"
# shellcheck source=tests/e2e/lib/fence_home.sh disable=SC1091
source "$REPO_ROOT/tests/e2e/lib/fence_home.sh"
GATE_HOME="$WORK/home"
fence_home "$REAL_HOME" "$GATE_HOME"
export HOME="$GATE_HOME"
fence_home_env "$GATE_HOME"
fence_uv_env "$REAL_HOME"
fence_cache_env "$REAL_HOME"

P43_SHIM="$WORK/shim"
mkdir -p "$P43_SHIM"
cat > "$P43_SHIM/nx" <<'SHIM'
#!/bin/sh
echo "FATAL: bare 'nx' called inside the P43 harness (PPID=$PPID)." >&2
echo "  It resolves to the INSTALLED tool and writes the real ~/.config/nexus. Use 'uv run nx'." >&2
exit 1
SHIM
chmod +x "$P43_SHIM/nx"
export PATH="$P43_SHIM:$PATH"
echo "[p43] fenced: HOME is an empty scratch dir at $HOME; PATH shim active"

# shellcheck disable=SC2329  # invoked via the EXIT trap
cleanup() {
  local rc=$?
  set +e
  NX_LOCAL=1 NEXUS_CONFIG_DIR="$SCRATCH" uv run nx daemon service stop --with-pg >/dev/null 2>&1
  if [ -f "$SCRATCH/pg_credentials" ]; then
    # shellcheck disable=SC1090,SC1091
    source "$SCRATCH/pg_credentials" >/dev/null 2>&1
    pg_ctl -D "$SCRATCH/postgres" stop -m fast >/dev/null 2>&1
  fi
  if [ "$CLEAN" -eq 1 ]; then
    fence_remove_home "$GATE_HOME" "$rc" || true
    rm -rf "$WORK"
    echo "[p43] cleaned up $WORK"
  else
    echo "[p43] engine and PG stopped; kept $WORK (results in $WORK/results; rerun reuses every built collection)"
  fi
}
trap cleanup EXIT

# ── 1. Provision the scratch PG (bge-768 profile, keyless scratch config) ───
if [ ! -f "$SCRATCH/pg_credentials" ]; then
  NEXUS_CONFIG_DIR="$SCRATCH" uv run nx init --service --no-autostart </dev/null
  # nx init also starts the PINNED RELEASE engine; the engine under test is the one started below.
  echo "[p43] stopping the pinned-release engine nx init auto-started (nexus-4e96a)"
  NX_LOCAL=1 NEXUS_CONFIG_DIR="$SCRATCH" uv run nx daemon service stop
else
  echo "[p43] reusing the scratch PG provisioned by an earlier run"
fi

# ── 2. The engine artifact ───────────────────────────────────────────────────
P43_JAR="$REPO_ROOT/service/target/nexus-service-1.0-SNAPSHOT.jar"
P43_BUILD_REF=""
if [ -n "${NEXUS_SERVICE_BIN:-}" ]; then
  echo "[p43] native binary: $NEXUS_SERVICE_BIN"
else
  P43_BUILD_ARGS=()
  if [ -n "${NX_P43_LAB_RUN_CLASS:-}" ]; then
    P43_BUILD_ARGS=(--lab-run "$NX_P43_LAB_RUN_CLASS")
  fi
  if ! P43_BUILD_OUT="$("$REPO_ROOT/scripts/build-gate-jar.sh" ${P43_BUILD_ARGS[@]+"${P43_BUILD_ARGS[@]}"} 2>&1)"; then
    printf '%s\n' "$P43_BUILD_OUT" >&2
    echo "[p43] ERROR: scripts/build-gate-jar.sh failed -- fix the Maven build and run again" >&2
    exit 2
  fi
  printf '%s\n' "$P43_BUILD_OUT" | sed 's/^/[p43]   build: /'
  P43_BUILD_REF="$(awk -F= '/^stamped build_ref=/ { v = $2 } END { print v }' <<<"$P43_BUILD_OUT")"
  if [ -z "$P43_BUILD_REF" ] || [ ! -f "$P43_JAR" ]; then
    echo "[p43] ERROR: no build_ref in the build output, or $P43_JAR is missing" >&2
    exit 2
  fi
fi

# ── Engine start, stop, embed choice ────────────────────────────────────────
p43_start_engine() {
  # The key (full run) goes into the supervisor's environment from inside this subshell: an exported
  # variable, not an argument, so `ps` never shows it. A keyless run exports none.
  (
    if [ -n "$P43_KEY" ]; then export NX_VOYAGE_API_KEY="$P43_KEY"; fi
    export NX_LOCAL=1
    export NEXUS_CONFIG_DIR="$SCRATCH"
    if [ -n "${NEXUS_SERVICE_BIN:-}" ]; then
      export NEXUS_SERVICE_BIN
    else
      export NEXUS_SERVICE_JAR="$P43_JAR"
    fi
    exec uv run nx daemon service start --config-dir "$SCRATCH"
  )
}
p43_stop_engine() { NX_LOCAL=1 NEXUS_CONFIG_DIR="$SCRATCH" uv run nx daemon service stop; }
p43_set_embed_choice() {  # the supervisor derives the engine's profile from this; no key is written
  NEXUS_CONFIG_DIR="$SCRATCH" uv run python -c "import pathlib, sys, yaml; p = pathlib.Path('$SCRATCH/config.yml'); d = (yaml.safe_load(p.read_text()) if p.exists() else None) or {}; d.setdefault('local', {})['embed_model'] = sys.argv[1]; p.write_text(yaml.safe_dump(d))" "$1"
}

# The harness runs in the fenced env with the scratch engine as its only reachable service.
p43_harness() {
  P43_FENCED=1 P43_SCRATCH_CONFIG_DIR="$SCRATCH" NX_LOCAL=1 NEXUS_CONFIG_DIR="$SCRATCH" PYTHONUNBUFFERED=1 \
    uv run python "$HERE/p43_run.py" --workdir "$WORK" "$@"
}

# ── 3. Voyage-profile seed boot (full run, only if a Voyage collection is not registered yet) ──
if [ "$KEYLESS" -eq 0 ]; then
  set +e
  P43_FENCED=1 P43_SCRATCH_CONFIG_DIR="$SCRATCH" uv run python "$HERE/p43_run.py" --workdir "$WORK" --seed-needed ${PY_ARGS[@]+"${PY_ARGS[@]}"}
  seed_rc=$?
  set -e
  if [ "$seed_rc" -eq 10 ]; then
    echo "[p43] seeding: a Voyage-profile boot registers the two Voyage collections"
    p43_set_embed_choice voyage-context-3
    p43_start_engine
    p43_harness --seed-register ${PY_ARGS[@]+"${PY_ARGS[@]}"}
    p43_stop_engine
  elif [ "$seed_rc" -ne 0 ]; then
    echo "[p43] ERROR: --seed-needed failed (rc=$seed_rc)" >&2
    exit 2
  fi
  p43_set_embed_choice bge-base-en-v15-768
fi

# ── 4. The engine under test, then the measurements ──────────────────────────
p43_start_engine
set +e
p43_harness ${PY_ARGS[@]+"${PY_ARGS[@]}"}
rc=$?
set -e
exit "$rc"
