#!/usr/bin/env bash
# nexus-86mx2 — PUBLISHED-CLIENT WRITE GATE.
#
# THE HOLE (Hal question 2026-08-14; root cause T2 nexus/rdr-191-manifest-400-
# caller-trace-2026-08-14, nexus-sh9v2): every existing gate tests a
# CONSISTENT client/engine pair — full suite = develop x develop, fresh-
# install MVV = new client x pinned engine, package-upgrade MVV = old client
# UPGRADING TO new (never writes against the OLD engine's stricter successor),
# cloud-client-path-gate = READ path only, --acquire = candidate-CLIENT x
# published-ENGINE. Nothing ran the CURRENTLY-PUBLISHED client's WRITE path
# against a CANDIDATE (not-yet-deployed) engine — so when
# engine-service-v0.1.73 started enforcing RDR-191 GATE-2 (manifest writes
# must name their collection explicitly), the released 7.6.1 client — which
# structurally could not send it — 400'd silently in PRODUCTION: content
# landed in T3, catalog registration was lost, 910 live documents accumulated
# invisible to catalog-aware retrieval. This gate is the missing leg: it
# would have 400'd v0.1.73 HERE, before deploy, instead of in production.
#
# JOURNEY OWNERSHIP (coordinate with the package-upgrade rehearsal — same
# harness territory, different axis): this script owns the FRESH-WRITE axis
# — a client that has NEVER upgraded, writing against a NEWER engine it has
# never seen. tests/e2e/migration-rehearsal/run.sh --package-upgrade (see its
# own header) owns the UPGRADE axis — an existing install's PACKAGE moving
# forward while its engine converges. Neither substitutes for the other: a
# client that upgrades correctly can still have shipped with a structurally
# incapable write path (exactly this bug), and a client that writes
# correctly fresh says nothing about whether an in-place upgrade converges
# the engine. Do not fold one into the other.
#
# WHAT IT DOES (fully self-provisioned, host-level, no Docker — see the
# ISOLATION note below for why that is safe here):
#   1. Provisions a CANDIDATE engine: by default the working tree's own
#      build-gate-jar dev jar (scripts/build-gate-jar.sh), started via the
#      exact throwaway-PG + `nx daemon service start` idiom
#      tests/e2e/local-service-gate.sh already uses (steps 1-4 there).
#      NEXUS_SERVICE_TAG=engine-service-vX.Y.Z swaps this for a cold-acquired
#      PUBLISHED engine tag instead (tag-mode; mirrors
#      migration-rehearsal/rehearse_acquire.sh's install-binary leg) — lets
#      the same script validate "would the NEXT engine tag work against
#      today's published client" as well as "would today's published client
#      work against the engine about to be cut."
#   2. Installs the CURRENTLY-PUBLISHED conexus client from real PyPI
#      (`uv tool install conexus` — latest, or NX_PUBLISHED_CLIENT_VERSION=
#      X.Y.Z to pin) into its own scrubbed-HOME sandbox — the identical
#      isolation idiom tests/e2e/fresh-install-mvv.sh --published uses
#      (nexus-796zn / nexus-enfoh's env -i allowlist; never touches the live
#      ~/.local/share/uv or ~/.local/bin).
#   3. Points the published client's `nx store put` / `nx index md` at the
#      candidate engine directly via NX_SERVICE_HOST/PORT/TOKEN (the same
#      env-halves pinning tests/db's self-provisioning fixtures and this
#      repo's own integration gate use — http_vector_client.py /
#      http_catalog_client.py honor these unconditionally, independent of
#      which client version issued the request).
#   4. Asserts ENGINE-CATALOG REGISTRATION on the CANDIDATE ENGINE'S OWN
#      ground truth — GET /v1/catalog/manifest/verify?doc_id=<tumbler>,
#      which reports the actual manifest ROW COUNT (`referenced`), not a
#      cached counter — for exactly ONE store-put fixture and ONE index-md
#      fixture. NON-VACUOUS BY CONSTRUCTION: both fixtures are deliberately
#      tiny (well under any chunking threshold) so each produces EXACTLY ONE
#      chunk; the gate asserts `referenced == 1 and missing == 0`, never
#      merely "the CLI exited 0" (the 7.6.1 outage was exactly a case where
#      the CLI's own reported success/200 was not proof — content landed,
#      registration silently did not).
#
# ISOLATION (per house memory project_home_does_not_isolate_launchd.md,
# nexus-d5yu5): a swapped HOME/NEXUS_CONFIG_DIR does NOT isolate an
# `nx init --yes`-driven system-unit (launchctl/systemctl) install — the
# unit label is a hard constant, uid-scoped, so it can collide with and stop
# a REAL production nexus service regardless of HOME. This script never
# passes `--yes` to `nx init --service` (the structural consent gate:
# `_decide_autostart` DECLINES a system unit on any non-interactive run with
# no explicit yes), and starts/stops the candidate engine only via
# `nx daemon service start|stop` — a plain child process under this script's
# own throwaway PG + scratch NEXUS_CONFIG_DIR, never a launchd/systemd unit.
# This is the identical safety shape tests/e2e/local-service-gate.sh and
# tests/e2e/fresh-install-mvv.sh already rely on; no container is needed
# because no autostart consent is ever granted.
#
# KNOWN-INCOMPATIBLE WINDOW (nexus-sh9v2). While a fix is cut but not yet
# published, the CURRENTLY-PUBLISHED client can be legitimately, KNOWINGLY
# incapable of registering manifests against a GATE-2-conformant candidate
# engine — that IS today's outage, and re-discovering it here every run
# would block engine cuts on a fix that already exists on develop. Acknowledge
# it explicitly and by name:
#
#   NX_EXPECTED_CLIENT_LAG=nexus-sh9v2 tests/e2e/published-client-write-gate.sh
#
# This turns an assertion failure into a NAMED, COUNTED "EXPECTED-
# INCOMPATIBLE" verdict (exit 2) — never a silent pass; the verdict line
# always names the acknowledging bead. The acknowledgment is REFUSED (falls
# through to a hard FAILED, exit 1) once the published client the gate
# actually resolved is >= 7.7.0 — the version CHANGELOG.md records as
# shipping the client halves of 498c92953 / b361a8106 / 8c75a61a3 (the fix
# for nexus-sh9v2's exact failure shape). That threshold, (7, 7, 0), is
# fixed IN THIS SCRIPT (see FIXED_IN_VERSION below) because it names a
# SPECIFIC bead's SPECIFIC fix — it does not track REQUIRED_ENGINE_VERSION
# or any other floor, and must be hand-updated (or the ack retired outright)
# if a FUTURE regression of the same shape earns a new bead.
#
# Usage:
#   tests/e2e/published-client-write-gate.sh
#       Candidate engine = working-tree dev jar (build-gate-jar.sh). Published
#       client = latest on PyPI.
#
#   NEXUS_SERVICE_TAG=engine-service-vX.Y.Z tests/e2e/published-client-write-gate.sh
#       Candidate engine = that PUBLISHED tag instead (cold-acquired,
#       cosign-verified — mirrors rehearse_acquire.sh).
#
#   NX_PUBLISHED_CLIENT_VERSION=7.6.1 tests/e2e/published-client-write-gate.sh
#       Pin the published client under test instead of resolving latest.
#
#   NX_EXPECTED_CLIENT_LAG=nexus-sh9v2 tests/e2e/published-client-write-gate.sh
#       Acknowledge the known incompatible window (see above).
#
# Exit codes (the verdict line is always the last line of output):
#   0  PUBLISHED-CLIENT WRITE GATE PASSED
#   1  PUBLISHED-CLIENT WRITE GATE FAILED (real, unacknowledged failure, OR a
#      stale/refused acknowledgment — the published client resolved to
#      FIXED_IN_VERSION or newer and STILL failed, which is a real
#      regression, not the known window)
#   2  PUBLISHED-CLIENT WRITE GATE EXPECTED-INCOMPATIBLE (acknowledged via
#      NX_EXPECTED_CLIENT_LAG, named + counted, NOT a silent pass)
#
# Cost: ~5-10 min (dev-jar rebuild + throwaway PG provisioning dominate;
# `uv tool install` re-resolves the full dependency set from PyPI every run,
# same as fresh-install-mvv.sh --published).
set -euo pipefail

REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)"
cd "$REPO_ROOT"

# The bead this ack names, and the version its fix shipped in. Hand-updated —
# see the KNOWN-INCOMPATIBLE WINDOW header note above.
EXPECTED_LAG_BEAD="nexus-sh9v2"
FIXED_IN_VERSION="7.7.0"

NEXUS_SERVICE_TAG="${NEXUS_SERVICE_TAG:-}"
NX_PUBLISHED_CLIENT_VERSION="${NX_PUBLISHED_CLIENT_VERSION:-}"
NX_EXPECTED_CLIENT_LAG="${NX_EXPECTED_CLIENT_LAG:-}"

if [ -n "$NX_EXPECTED_CLIENT_LAG" ] && [ "$NX_EXPECTED_CLIENT_LAG" != "$EXPECTED_LAG_BEAD" ]; then
  echo "FATAL: NX_EXPECTED_CLIENT_LAG=$NX_EXPECTED_CLIENT_LAG does not match the only acknowledgment this script knows ($EXPECTED_LAG_BEAD) — either fix the env var, or this is a NEW regression that needs its own bead + its own threshold in this script's FIXED_IN_VERSION, not a reused ack." >&2
  exit 2
fi

echo "================================================================"
echo " PUBLISHED-CLIENT WRITE GATE (nexus-86mx2)"
echo "   candidate engine : $( [ -n "$NEXUS_SERVICE_TAG" ] && echo "$NEXUS_SERVICE_TAG (tag-mode)" || echo "working-tree dev jar (default)" )"
echo "   published client : $( [ -n "$NX_PUBLISHED_CLIENT_VERSION" ] && echo "$NX_PUBLISHED_CLIENT_VERSION (pinned)" || echo "latest (unpinned)" )"
echo "   expected lag ack : $( [ -n "$NX_EXPECTED_CLIENT_LAG" ] && echo "$NX_EXPECTED_CLIENT_LAG" || echo "(none)" )"
echo "================================================================"

WORK="$(mktemp -d "${TMPDIR:-/tmp}/nx-pcwg.XXXXXX")"
ENGINE_HOME="$WORK/engine-config"
CLIENT_HOME="$WORK/client-home"
CLIENT_CONFIG="$WORK/client-config"
LOGS="$WORK/logs"
mkdir -p "$ENGINE_HOME" "$CLIENT_HOME" "$CLIENT_CONFIG" "$LOGS"
echo "[gate] scratch: $WORK"

GATE_OK=0
_fail() { echo "PUBLISHED-CLIENT WRITE GATE FAILED: $*" >&2; exit 1; }

cleanup() {
  set +e
  NX_LOCAL=1 NEXUS_CONFIG_DIR="$ENGINE_HOME" uv run nx daemon service stop >/dev/null 2>&1
  if [ -f "$ENGINE_HOME/pg_credentials" ]; then
    # shellcheck disable=SC1090,SC1091
    source "$ENGINE_HOME/pg_credentials" >/dev/null 2>&1
    pg_ctl -D "$ENGINE_HOME/postgres" stop -m fast >/dev/null 2>&1
  fi
  git checkout -- "service/src/main/resources/META-INF/nexus/release.properties" 2>/dev/null || true
  if [ "$GATE_OK" = 1 ]; then
    rm -rf "$WORK"
  else
    echo "FAILURE EVIDENCE PRESERVED: $WORK" >&2
  fi
}
trap cleanup EXIT

# ── 1. Provision the candidate engine ───────────────────────────────────────
echo "── 1/4 Provision candidate engine (NX_LOCAL, throwaway PG, scratch config) ──"

if [ -n "$NEXUS_SERVICE_TAG" ]; then
  # Tag-mode: cold-acquire the named PUBLISHED engine tag FIRST (binary + PG
  # bundle, cosign-verified — mirrors rehearse_acquire.sh), THEN let
  # `nx init --service` provision from what was just installed.
  echo "[gate] tag-mode: cold-acquiring $NEXUS_SERVICE_TAG"
  NEXUS_CONFIG_DIR="$ENGINE_HOME" uv run nx daemon service install-binary "$NEXUS_SERVICE_TAG" \
    2>&1 | tee "$LOGS/install-binary.log" \
    || _fail "nx daemon service install-binary $NEXUS_SERVICE_TAG failed (see $LOGS/install-binary.log)"
  export NEXUS_SERVICE_TAG   # _ensure_service_binary_step no-ops: already installed
fi

NEXUS_CONFIG_DIR="$ENGINE_HOME" uv run nx init --service 2>&1 | tee "$LOGS/init.log" \
  || _fail "nx init --service failed (see $LOGS/init.log)"

# nexus-4e96a: init --service auto-starts a binary (the pinned release, or in
# tag-mode the tag just installed above) via its own short-circuit — stop it
# so step below's launch-artifact selection is what actually ends up serving.
echo "[gate] stopping the auto-started instance (nexus-4e96a)"
NX_LOCAL=1 NEXUS_CONFIG_DIR="$ENGINE_HOME" uv run nx daemon service stop \
  || _fail "could not stop the auto-started service"

START_ENV=(NX_LOCAL=1 "NEXUS_CONFIG_DIR=$ENGINE_HOME")
if [ -n "$NEXUS_SERVICE_TAG" ]; then
  BIN="$ENGINE_HOME/service/nexus-service"
  [ -x "$BIN" ] || _fail "install-binary reported success but $BIN is not executable"
  START_ENV+=("NEXUS_SERVICE_BIN=$BIN")
  echo "[gate] candidate engine artifact: published tag $NEXUS_SERVICE_TAG ($BIN)"
else
  echo "[gate] building + stamping the candidate dev jar (scripts/build-gate-jar.sh)"
  ( cd "$REPO_ROOT" && ./scripts/build-gate-jar.sh ) 2>&1 | tee "$LOGS/build-gate-jar.log" \
    || _fail "scripts/build-gate-jar.sh failed (see $LOGS/build-gate-jar.log)"
  JAR="$REPO_ROOT/service/target/nexus-service-1.0-SNAPSHOT.jar"
  [ -f "$JAR" ] || _fail "build-gate-jar.sh reported success but $JAR does not exist"
  START_ENV+=("NEXUS_SERVICE_JAR=$JAR")
  echo "[gate] candidate engine artifact: working-tree dev jar ($JAR)"
fi

env "${START_ENV[@]}" uv run nx daemon service start 2>&1 | tee "$LOGS/start.log" \
  || _fail "nx daemon service start failed for the candidate engine (see $LOGS/start.log)"

LEASE_JSON="$(cat "$ENGINE_HOME"/storage_service_addr.* 2>/dev/null)" \
  || _fail "no lease file under $ENGINE_HOME/storage_service_addr.* after start"
SERVICE_PORT="$(printf '%s' "$LEASE_JSON" | python3 -c "import sys,json;d=json.load(sys.stdin);print(d.get('endpoint',d)['port'])")"
SERVICE_TOKEN="$(printf '%s' "$LEASE_JSON" | python3 -c "import sys,json;d=json.load(sys.stdin);print(d.get('endpoint',d)['token'])")"
echo "[gate] candidate engine serving on 127.0.0.1:$SERVICE_PORT"

healthy=0
for _ in $(seq 1 30); do
  code="$(curl -sS -o /dev/null -w '%{http_code}' "http://127.0.0.1:$SERVICE_PORT/health" 2>/dev/null || true)"
  [ "$code" = "200" ] && { healthy=1; break; }
  sleep 2
done
[ "$healthy" = 1 ] || _fail "candidate engine did not reach healthy on 127.0.0.1:$SERVICE_PORT"
echo "[gate] candidate engine healthy"

# ── 2. Install the PUBLISHED client into its own scrubbed sandbox ──────────
echo "── 2/4 Install PUBLISHED client from real PyPI (scrubbed HOME sandbox) ──"

if [ -n "$NX_PUBLISHED_CLIENT_VERSION" ]; then
  PKG_SPEC="conexus==$NX_PUBLISHED_CLIENT_VERSION"
else
  PKG_SPEC="conexus"
fi

# nexus-enfoh: env -i, not a bare `env HOME=...` — ambient UV_TOOL_DIR/
# XDG_*/PIP_* must not leak the install into the live tool venv. Same
# allowlist shape as fresh-install-mvv.sh --published; deliberately does
# NOT scrub PATH (needs the real `uv`/system python) but DOES scrub every
# UV_*/XDG_*/PIP_* index-routing var so resolution hits real PyPI.
_uv_sandboxed() {
  env -i \
    HOME="$CLIENT_HOME" \
    PATH="$PATH" \
    TERM="${TERM:-dumb}" \
    ${HTTPS_PROXY:+HTTPS_PROXY="$HTTPS_PROXY"} \
    ${HTTP_PROXY:+HTTP_PROXY="$HTTP_PROXY"} \
    uv "$@"
}

_uv_sandboxed tool install --python 3.12 "$PKG_SPEC" >"$LOGS/client-install.log" 2>&1 \
  || _fail "uv tool install $PKG_SPEC failed (see $LOGS/client-install.log) — network unreachable, version not published, or dependency resolution failed; no skip-pass permitted"

CLIENT_BIN_DIR="$(_uv_sandboxed tool dir --bin)"
[ -x "$CLIENT_BIN_DIR/nx" ] || _fail "uv tool install did not expose $CLIENT_BIN_DIR/nx"

# The published client's write path under test — pinned at the candidate
# engine via NX_SERVICE_HOST/PORT/TOKEN (http_vector_client.py /
# http_catalog_client.py honor these unconditionally, ranked above lease-
# file discovery, independent of client version). NEXUS_CONFIG_DIR is the
# client's OWN scratch dir (never `nx init`'d) — this gate exercises only
# the pinned-endpoint write path, not local-mode bootstrap.
_client_nx() {
  env -i \
    HOME="$CLIENT_HOME" \
    PATH="$CLIENT_BIN_DIR:/usr/bin:/bin" \
    TERM="${TERM:-dumb}" \
    NX_LOCAL=1 \
    NX_STORAGE_BACKEND=service \
    NEXUS_CONFIG_DIR="$CLIENT_CONFIG" \
    NX_SERVICE_HOST=127.0.0.1 \
    NX_SERVICE_PORT="$SERVICE_PORT" \
    NX_SERVICE_TOKEN="$SERVICE_TOKEN" \
    "$CLIENT_BIN_DIR/nx" "$@"
}

CLIENT_VERSION_RAW="$(_client_nx --version 2>&1)"
echo "[gate] published client resolved: $CLIENT_VERSION_RAW"
CLIENT_VERSION=""
if [[ "$CLIENT_VERSION_RAW" =~ ([0-9]+\.[0-9]+\.[0-9]+) ]]; then
    CLIENT_VERSION="${BASH_REMATCH[1]}"
fi
[ -n "$CLIENT_VERSION" ] || _fail "could not parse a version out of: $CLIENT_VERSION_RAW"

# ── 3. Drive the write journeys ─────────────────────────────────────────────
echo "── 3/4 Drive write journeys (store put + index md) against the candidate engine ──"

# Provisioner nx: THIS checkout's own `uv run nx`, pointed at the same
# candidate engine, used ONLY to resolve title -> tumbler + read
# ground-truth registration state. Trusted (this tree is what the full
# suite gates); never the subject under test.
_provisioner_nx() {
  NX_LOCAL=1 NEXUS_CONFIG_DIR="$ENGINE_HOME" uv run nx "$@"
}

_manifest_verify() {
  local doc_id="$1"
  curl -sS -H "Authorization: Bearer $SERVICE_TOKEN" \
    "http://127.0.0.1:$SERVICE_PORT/v1/catalog/manifest/verify?doc_id=$doc_id"
}

RUN_ID="$$-$(date +%s)"
STORE_OK=0
MD_OK=0
FAIL_REASONS=()

# -- a. store put: one tiny fixture, deterministically ONE chunk. -----------
STORE_TITLE="pcwg-store-$RUN_ID"
echo "[gate] store put: title=$STORE_TITLE"
STORE_PUT_OUT="$(printf 'published-client-write-gate probe %s\n' "$RUN_ID" \
  | _client_nx store put - --title "$STORE_TITLE" --collection knowledge 2>&1)" || true
printf '%s\n' "$STORE_PUT_OUT" | sed 's/^/       /' | tee "$LOGS/store-put.log" >/dev/null
STORE_SHOW_JSON="$(_provisioner_nx catalog show "$STORE_TITLE" --json 2>/dev/null)" || STORE_SHOW_JSON=""
if [ -n "$STORE_SHOW_JSON" ]; then
  STORE_TUMBLER="$(printf '%s' "$STORE_SHOW_JSON" | python3 -c "import sys,json;print(json.load(sys.stdin).get('tumbler',''))" 2>/dev/null)"
else
  STORE_TUMBLER=""
fi
if [ -n "$STORE_TUMBLER" ]; then
  STORE_VERIFY_JSON="$(_manifest_verify "$STORE_TUMBLER")"
  STORE_REFERENCED="$(printf '%s' "$STORE_VERIFY_JSON" | python3 -c "import sys,json;print(json.load(sys.stdin).get('referenced',-1))" 2>/dev/null || echo -1)"
  STORE_MISSING="$(printf '%s' "$STORE_VERIFY_JSON" | python3 -c "import sys,json;print(json.load(sys.stdin).get('missing',-1))" 2>/dev/null || echo -1)"
  if [ "$STORE_REFERENCED" = "1" ] && [ "$STORE_MISSING" = "0" ]; then
    STORE_OK=1
    echo "[gate] store put OK: tumbler=$STORE_TUMBLER referenced=1 missing=0 (exact expected count)"
  else
    FAIL_REASONS+=("store put: catalog registration incomplete (tumbler=$STORE_TUMBLER referenced=$STORE_REFERENCED missing=$STORE_MISSING, expected referenced=1 missing=0)")
  fi
else
  FAIL_REASONS+=("store put: no catalog document registered for title '$STORE_TITLE' — content may have landed in T3 while registration was lost (the exact nexus-sh9v2 shape)")
fi

# -- b. index md: one tiny fixture file, deterministically ONE chunk. -------
MD_TITLE="pcwg-md-$RUN_ID"
MD_FIXTURE="$WORK/fixture.md"
cat > "$MD_FIXTURE" <<EOF
---
title: $MD_TITLE
---

published-client-write-gate probe $RUN_ID.
EOF
echo "[gate] index md: title=$MD_TITLE path=$MD_FIXTURE"
MD_OUT="$(_client_nx index md "$MD_FIXTURE" --corpus pcwg-gate 2>&1)" || true
printf '%s\n' "$MD_OUT" | sed 's/^/       /' | tee "$LOGS/index-md.log" >/dev/null
MD_SHOW_JSON="$(_provisioner_nx catalog show "$MD_TITLE" --json 2>/dev/null)" || MD_SHOW_JSON=""
if [ -n "$MD_SHOW_JSON" ]; then
  MD_TUMBLER="$(printf '%s' "$MD_SHOW_JSON" | python3 -c "import sys,json;print(json.load(sys.stdin).get('tumbler',''))" 2>/dev/null)"
else
  MD_TUMBLER=""
fi
if [ -n "$MD_TUMBLER" ]; then
  MD_VERIFY_JSON="$(_manifest_verify "$MD_TUMBLER")"
  MD_REFERENCED="$(printf '%s' "$MD_VERIFY_JSON" | python3 -c "import sys,json;print(json.load(sys.stdin).get('referenced',-1))" 2>/dev/null || echo -1)"
  MD_MISSING="$(printf '%s' "$MD_VERIFY_JSON" | python3 -c "import sys,json;print(json.load(sys.stdin).get('missing',-1))" 2>/dev/null || echo -1)"
  if [ "$MD_REFERENCED" = "1" ] && [ "$MD_MISSING" = "0" ]; then
    MD_OK=1
    echo "[gate] index md OK: tumbler=$MD_TUMBLER referenced=1 missing=0 (exact expected count)"
  else
    FAIL_REASONS+=("index md: catalog registration incomplete (tumbler=$MD_TUMBLER referenced=$MD_REFERENCED missing=$MD_MISSING, expected referenced=1 missing=0)")
  fi
else
  FAIL_REASONS+=("index md: no catalog document registered for title '$MD_TITLE' — content may have landed in T3 while registration was lost (the exact nexus-sh9v2 shape)")
fi

# ── 4. Verdict ───────────────────────────────────────────────────────────────
echo "── 4/4 Verdict ──"

if [ "$STORE_OK" = 1 ] && [ "$MD_OK" = 1 ]; then
  GATE_OK=1
  echo "PUBLISHED-CLIENT WRITE GATE PASSED — published conexus $CLIENT_VERSION registers real manifest rows against the candidate engine (store put + index md, exact expected counts, non-vacuous)"
  exit 0
fi

for r in "${FAIL_REASONS[@]}"; do
  echo "  - $r" >&2
done

if [ -n "$NX_EXPECTED_CLIENT_LAG" ]; then
  # Stale-ack refusal: compare tuples numerically, not lexicographically —
  # "7.10.0" >= "7.7.0" must hold even though it is not the lexical case.
  IS_STALE="$(python3 -c "
cv = tuple(int(x) for x in '$CLIENT_VERSION'.split('.'))
fv = tuple(int(x) for x in '$FIXED_IN_VERSION'.split('.'))
print('1' if cv >= fv else '0')
")"
  if [ "$IS_STALE" = "1" ]; then
    echo "ACKNOWLEDGMENT REFUSED (stale): published client resolved to $CLIENT_VERSION >= $FIXED_IN_VERSION, the version $EXPECTED_LAG_BEAD's fix is recorded as shipping in — this is a REAL regression, not the known window. Investigate; do not re-arm the ack." >&2
    echo "PUBLISHED-CLIENT WRITE GATE FAILED — acknowledgment for $EXPECTED_LAG_BEAD refused as stale (published $CLIENT_VERSION >= $FIXED_IN_VERSION)"
    exit 1
  fi
  GATE_OK=1
  echo "PUBLISHED-CLIENT WRITE GATE EXPECTED-INCOMPATIBLE (ack $EXPECTED_LAG_BEAD) — published conexus $CLIENT_VERSION (< $FIXED_IN_VERSION) cannot register manifests against the candidate engine; KNOWN, TRACKED, COUNTED — not a silent pass"
  exit 2
fi

echo "PUBLISHED-CLIENT WRITE GATE FAILED — published conexus $CLIENT_VERSION cannot register manifests against the candidate engine (${#FAIL_REASONS[@]} check(s) failed). If this is the known nexus-sh9v2 window, re-run with NX_EXPECTED_CLIENT_LAG=nexus-sh9v2 to acknowledge it explicitly."
exit 1
