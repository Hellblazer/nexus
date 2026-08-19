#!/usr/bin/env bash
# RDR-195 Minimum Viable Validation (bead nexus-kmtlp.13).
#
# Re-indexes a large source repository end to end through a LOCALLY BUILT
# engine running in Voyage mode (NX_VOYAGE_API_KEY set at engine boot — the
# "local install calling Voyage" posture: local.embed_model=voyage-code-3,
# NX_LOCAL=1 client), with ZERO TOO_MANY_TOKENS_IN_BATCH occurrences, then
# confirms semantic search returns hits from the newly indexed files.
#
# Records the four RDR-195 § Performance Expectations measurements by
# grepping the engine + client structured logs for the exact events the
# Phase 2 implementation emits (VoyageEmbedder.java / VectorHandler.java /
# http_vector_client.py — see the harness's own comments below for the
# event vocabulary):
#   1. Voyage requests per upsert page      (voyage_subrequest_sent / http_vector_upsert_chunks_request)
#   2. Adaptive-split event count           (voyage_adaptive_split)
#   3. Observed bytes-per-token for the corpus (voyage_subrequest_sent requestBytes/usageTokens)
#   4. 429 count                            (voyage_retry status=429)
# plus the terminal-failure count that must be exactly zero for a pass:
#   vector_too_many_tokens_in_batch (engine) / "TOO_MANY_TOKENS_IN_BATCH" (client)
#
# HERMETIC, SELF-PROVISIONING, PATTERNED AFTER tests/_engine_substrate.py's
# _boot()/_teardown() (the Python unit suite's own T2 engine substrate) and
# tests/e2e/local-service-gate.sh's isolation discipline — but this script
# runs the jar + PG as plain foreground/background PROCESSES it owns
# directly (NOT `nx daemon service start`/`install`), and boots the engine
# with NX_VOYAGE_API_KEY so it embeds via Voyage rather than the bundled
# ONNX/bge model. Never touches ~/.config/nexus or ~/.local/share/nexus —
# HOME is fully sandboxed for the duration of the run (see --home below).
#
# Usage:
#   tests/e2e/rdr195-voyage-mvv.sh <repo-path> [--home <dir>] [--skip-index]
#
#   <repo-path>    Path to an already-cloned repository to index. Not
#                  cloned by this script — the caller owns acquisition (a
#                  read-only stand-in repo is the documented MVV shape).
#   --home <dir>   Sandbox HOME (also becomes NEXUS_CONFIG_DIR's parent via
#                  the default ~/.config/nexus resolution). REQUIRED to be
#                  outside any real HOME — refuses to run against $HOME.
#                  Defaults to a fresh mktemp -d under TMPDIR.
#   --skip-index   Boot infra and measure from an EXISTING run's logs only
#                  (skip `nx index repo`) — for re-running the search/
#                  measurement steps without re-spending on a failed run's
#                  half-indexed state. Requires --home to point at a prior
#                  run's sandbox.
#
# Never loops or retries the index step: a partial/failed run's tokens are
# already billed. On failure this script tears infra down and reports —
# it never automatically re-indexes.
#
# NEVER run this against a live production install. This script refuses to
# start if $HOME (the ambient one, before --home substitution) looks like
# it is about to be reused as the sandbox.
set -euo pipefail

REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)"
cd "$REPO_ROOT"

# ── Args ─────────────────────────────────────────────────────────────────
TARGET_REPO=""
SANDBOX_HOME=""
SKIP_INDEX=0
while [[ $# -gt 0 ]]; do
  case "$1" in
    --home) SANDBOX_HOME="$2"; shift 2 ;;
    --skip-index) SKIP_INDEX=1; shift ;;
    -h|--help)
      sed -n '2,45p' "$0"; exit 0 ;;
    *)
      if [[ -z "$TARGET_REPO" ]]; then TARGET_REPO="$1"; shift; else
        echo "[mvv] unrecognized arg: $1" >&2; exit 2
      fi ;;
  esac
done

if [[ -z "$TARGET_REPO" ]]; then
  echo "RDR-195 MVV FAILED: usage: $0 <repo-path> [--home <dir>] [--skip-index]" >&2
  exit 2
fi
TARGET_REPO="$(cd "$TARGET_REPO" && pwd)"
if [[ ! -d "$TARGET_REPO" ]]; then
  echo "RDR-195 MVV FAILED: repo path not a directory: $TARGET_REPO" >&2
  exit 2
fi

if [[ -z "$SANDBOX_HOME" ]]; then
  SANDBOX_HOME="$(mktemp -d "${TMPDIR:-/tmp}/rdr195-mvv-home.XXXXXX")"
fi
mkdir -p "$SANDBOX_HOME"
SANDBOX_HOME="$(cd "$SANDBOX_HOME" && pwd)"

# Fail loud, never silently reuse, the ambient HOME as the sandbox.
if [[ "$SANDBOX_HOME" == "$HOME" ]]; then
  echo "RDR-195 MVV FAILED: --home resolved to the ambient HOME ($HOME)." \
       "Refusing — this script must never touch a real install." >&2
  exit 2
fi

echo "[mvv] sandbox HOME: $SANDBOX_HOME"
echo "[mvv] target repo:  $TARGET_REPO"

# ── Jar freshness (fail loud, no rebuild here — see the relay's rule: a
#    rebuild must not run concurrently with the Python unit suite) ────────
JAR="$REPO_ROOT/service/target/nexus-service-1.0-SNAPSHOT.jar"
FRESHNESS="$(uv run python -c "
from tests.db._service_fixture import jar_freshness_skip_reason
r = jar_freshness_skip_reason()
print(r or '')
")"
if [[ -n "$FRESHNESS" ]]; then
  echo "RDR-195 MVV FAILED: engine jar stale/missing: $FRESHNESS" >&2
  exit 1
fi
echo "[mvv] engine jar fresh: $JAR"

# ── Voyage key — from the repo's OWN .env (dev integration env), never the
#    production config ─────────────────────────────────────────────────────
if [[ ! -f "$REPO_ROOT/.env" ]]; then
  echo "RDR-195 MVV FAILED: $REPO_ROOT/.env not found (need VOYAGE_API_KEY)" >&2
  exit 1
fi
set -a
# shellcheck disable=SC1091
. "$REPO_ROOT/.env"
set +a
if [[ -z "${VOYAGE_API_KEY:-}" ]]; then
  echo "RDR-195 MVV FAILED: VOYAGE_API_KEY not set after sourcing .env" >&2
  exit 1
fi
echo "[mvv] VOYAGE_API_KEY loaded from .env (redacted)"

PG_BIN="$(uv run python -c "
from tests.db._service_fixture import pg_bin_dir
print(pg_bin_dir())
")"
if [[ ! -x "$PG_BIN/initdb" ]]; then
  echo "RDR-195 MVV FAILED: no usable PG bundle at $PG_BIN" >&2
  exit 1
fi
echo "[mvv] PG bin: $PG_BIN"

free_port() {
  uv run python -c "
import socket
s = socket.socket()
s.bind(('127.0.0.1', 0))
print(s.getsockname()[1])
s.close()
"
}

PGDATA="$SANDBOX_HOME/pgdata"
PG_PORT=""
SVC_PORT=""
SVC_PID=""
ENGINE_LOG="$SANDBOX_HOME/engine.log"
CLIENT_LOG="$SANDBOX_HOME/client.log"
INDEX_LOG="$SANDBOX_HOME/index.log"
SEARCH_LOG="$SANDBOX_HOME/search.log"
BEARER="rdr195-mvv-bearer"
DBNAME="nexus_rdr195_mvv"

teardown() {
  local ec=$?
  echo "[mvv] tearing down (exit=$ec)"
  if [[ -n "$SVC_PID" ]] && kill -0 "$SVC_PID" 2>/dev/null; then
    kill "$SVC_PID" 2>/dev/null || true
    for _ in $(seq 1 20); do
      kill -0 "$SVC_PID" 2>/dev/null || break
      sleep 0.5
    done
    kill -9 "$SVC_PID" 2>/dev/null || true
  fi
  if [[ -n "$PG_PORT" && -d "$PGDATA" ]]; then
    "$PG_BIN/pg_ctl" -D "$PGDATA" stop -m immediate >/dev/null 2>&1 || true
  fi
  echo "[mvv] engine log:  $ENGINE_LOG"
  echo "[mvv] client log:  $CLIENT_LOG"
  echo "[mvv] index log:   $INDEX_LOG"
  echo "[mvv] search log:  $SEARCH_LOG"
  exit "$ec"
}
trap teardown EXIT

# ── Boot PG (mirrors tests/_engine_substrate.py _boot()). With --skip-index
#    and a pre-existing pgdata (a prior run's populated database), reuse it
#    via a plain pg_ctl start rather than initdb — re-running initdb against
#    a non-empty target directory fails outright, and re-initializing would
#    also throw away the prior run's already-embedded, already-billed data. ─
PG_PORT="$(free_port)"
REUSE_PGDATA=0
if [[ -d "$PGDATA" ]]; then
  REUSE_PGDATA=1
  # Recover the port this pgdata was initialized with (postgresql.conf is
  # authoritative — a freshly chosen free_port would not match the data
  # directory's own listener config).
  RECOVERED_PORT="$(grep -E '^port = ' "$PGDATA/postgresql.conf" | tail -1 | grep -oE '[0-9]+')"
  if [[ -n "$RECOVERED_PORT" ]]; then PG_PORT="$RECOVERED_PORT"; fi
  echo "[mvv] reusing existing pgdata at $PGDATA (port $PG_PORT)"
  "$PG_BIN/pg_ctl" -D "$PGDATA" -l "$PGDATA/pg.log" start -w >"$SANDBOX_HOME/pg_ctl.log" 2>&1
else
  echo "[mvv] booting fresh PG on port $PG_PORT (pgdata=$PGDATA)"
  "$PG_BIN/initdb" -D "$PGDATA" --no-locale -E UTF8 --auth=trust >"$SANDBOX_HOME/initdb.log" 2>&1
  {
    echo ""
    echo "port = $PG_PORT"
    echo "listen_addresses = '127.0.0.1'"
    echo "fsync = off"
    echo "synchronous_commit = off"
    echo "full_page_writes = off"
    # TCP-only (-h 127.0.0.1 everywhere below): the sandbox HOME path under
    # the session scratchpad exceeds the 103-byte Unix-domain-socket path
    # limit, so disable the socket listener entirely rather than routing it
    # through a separate short-path dir.
    echo "unix_socket_directories = ''"
  } >>"$PGDATA/postgresql.conf"
  "$PG_BIN/pg_ctl" -D "$PGDATA" -l "$PGDATA/pg.log" \
    -o "-p $PG_PORT" start -w >"$SANDBOX_HOME/pg_ctl.log" 2>&1
  PG_USER="$USER"
  "$PG_BIN/createdb" -h 127.0.0.1 -p "$PG_PORT" -U "$PG_USER" "$DBNAME"
  "$PG_BIN/psql" -h 127.0.0.1 -p "$PG_PORT" -U "$PG_USER" -d "$DBNAME" -v ON_ERROR_STOP=1 -c "
DO \$\$ BEGIN
  IF NOT EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'nexus_svc') THEN
    CREATE ROLE nexus_svc LOGIN PASSWORD 'nexus_svc_pass'
    NOSUPERUSER NOCREATEDB NOCREATEROLE NOBYPASSRLS;
  END IF;
END \$\$;
"
fi
PG_USER="${PG_USER:-$USER}"
echo "[mvv] PG up (pid via postmaster.pid in $PGDATA)"

# ── Boot the engine jar in VOYAGE mode ────────────────────────────────
SVC_PORT="$(free_port)"
echo "[mvv] booting engine on port $SVC_PORT (Voyage mode)"
NX_SERVICE_PORT="$SVC_PORT" \
NX_SERVICE_TOKEN="$BEARER" \
NX_DB_URL="jdbc:postgresql://127.0.0.1:$PG_PORT/$DBNAME" \
NX_DB_USER="nexus_svc" \
NX_DB_PASS="nexus_svc_pass" \
NX_POOL_SIZE="8" \
NX_DB_ADMIN_URL="jdbc:postgresql://127.0.0.1:$PG_PORT/$DBNAME" \
NX_DB_ADMIN_USER="$PG_USER" \
NX_DB_ADMIN_PASS="" \
NX_VOYAGE_API_KEY="$VOYAGE_API_KEY" \
java -jar "$JAR" >>"$ENGINE_LOG" 2>&1 &
SVC_PID=$!
echo "[mvv] engine pid: $SVC_PID"

# Wait for the port to bind (up to 90s — cold JVM + Liquibase migrations).
ready=0
for _ in $(seq 1 90); do
  if curl -fsS "http://127.0.0.1:$SVC_PORT/health" >/dev/null 2>&1; then
    ready=1
    break
  fi
  if ! kill -0 "$SVC_PID" 2>/dev/null; then
    echo "RDR-195 MVV FAILED: engine process died during boot. Tail of $ENGINE_LOG:" >&2
    tail -80 "$ENGINE_LOG" >&2 || true
    exit 1
  fi
  sleep 1
done
if [[ "$ready" -ne 1 ]]; then
  echo "RDR-195 MVV FAILED: engine did not answer /health within 90s. Tail of $ENGINE_LOG:" >&2
  tail -80 "$ENGINE_LOG" >&2 || true
  exit 1
fi
echo "[mvv] engine healthy"

if ! grep -q 'event=embedding_mode_banner mode=voyage' "$ENGINE_LOG"; then
  echo "RDR-195 MVV FAILED: engine did not boot in Voyage mode (embedding_mode_banner absent/wrong). Tail:" >&2
  grep 'embedding_mode_banner' "$ENGINE_LOG" >&2 || tail -40 "$ENGINE_LOG" >&2
  exit 1
fi
echo "[mvv] confirmed engine embedding_mode=voyage"

# ── Point the sandboxed client at this engine ─────────────────────────
export HOME="$SANDBOX_HOME"
export NX_LOCAL=1
export NX_SERVICE_HOST=127.0.0.1
export NX_SERVICE_PORT="$SVC_PORT"
export NX_SERVICE_TOKEN="$BEARER"
export NEXUS_LOG_LEVEL=INFO
unset NEXUS_CONFIG_DIR || true

if [[ "$SKIP_INDEX" -eq 0 ]]; then
  echo "[mvv] configuring client: local.embed_model=voyage-code-3"
  (cd "$REPO_ROOT" && uv run nx config set local.embed_model voyage-code-3) 2>>"$CLIENT_LOG"
  (cd "$REPO_ROOT" && uv run nx config set voyage_api_key "$VOYAGE_API_KEY") 2>>"$CLIENT_LOG"

  echo "[mvv] indexing $TARGET_REPO (this spends real Voyage tokens — NOT looped)"
  set +e
  (cd "$REPO_ROOT" && uv run nx index repo "$TARGET_REPO" --monitor) \
    >"$INDEX_LOG" 2>>"$CLIENT_LOG"
  INDEX_RC=$?
  set -e
  echo "[mvv] index exit code: $INDEX_RC"
  echo "$INDEX_RC" >"$SANDBOX_HOME/index.rc"
else
  echo "[mvv] --skip-index: engine/PG (re)booted against the existing sandbox at $SANDBOX_HOME; not re-indexing"
  INDEX_RC="$(cat "$SANDBOX_HOME/index.rc" 2>/dev/null || echo 1)"
fi

# ── Search verification — BEST EFFORT regardless of INDEX_RC. A nonzero
#    INDEX_RC can come from a cause entirely unrelated to RDR-195 (e.g. a
#    single pathological non-text file tripping the PDF quality gate late
#    in a multi-thousand-file run — nx index repo's run_file_loop is
#    fail-fast on FIRST exception, so one bad file can abort an otherwise-
#    complete run whose Voyage embedding already succeeded and was already
#    durably flushed page-by-page). Search still tells us whether the
#    content that DID get indexed is retrievable — the caller weighs
#    INDEX_RC against this script's own log-derived RDR-195 measurements,
#    not the other way around. ─────────────────────────────────────────────
echo "[mvv] verifying semantic search against newly indexed content (best-effort; INDEX_RC=$INDEX_RC)"
{
  echo "=== query 1: Eloquent query builder where clause ==="
  (cd "$REPO_ROOT" && uv run nx search "Eloquent query builder where clause" --corpus code -m 5) || true
  echo "=== query 2: Blade template compiler ==="
  (cd "$REPO_ROOT" && uv run nx search "Blade template compiler" --corpus code -m 5) || true
  echo "=== query 3: queue worker retry ==="
  (cd "$REPO_ROOT" && uv run nx search "queue worker retry" --corpus code -m 5) || true
} >"$SEARCH_LOG" 2>&1
SEARCH_HITS="$(grep -cE '^\S+:[0-9]+:' "$SEARCH_LOG" || true)"
echo "[mvv] search result lines matched: $SEARCH_HITS"

# ── Measurements ───────────────────────────────────────────────────────────
echo ""
echo "===================== RDR-195 MVV MEASUREMENTS ====================="

# (1) Voyage requests per upsert page. TWO distinct client write paths reach
#     the engine (nexus-cy9u7 docstring in http_vector_client.py): the
#     ChunkBatcher's combined-write flush (client event=chunk_flush_complete,
#     one flush per page) for code files it accepts, and the direct
#     upsert_chunks page loop (event=http_vector_upsert_chunks_request) for
#     everything ChunkBatcher does NOT own (prose/PDF, oversize/rejected code
#     files). Both are real "upsert pages" reaching Voyage — summing them is
#     the correct denominator; counting either alone undercounts.
CHUNKBATCHER_FLUSHES="$(grep -c "event='chunk_flush_complete'" "$CLIENT_LOG" 2>/dev/null || true)"
UPSERT_PAGES_DIRECT="$(grep -c 'http_vector_upsert_chunks_request' "$CLIENT_LOG" 2>/dev/null || true)"
UPSERT_PAGES=$((CHUNKBATCHER_FLUSHES + UPSERT_PAGES_DIRECT))
VOYAGE_SUBREQUESTS="$(grep -c 'event=voyage_subrequest_sent' "$ENGINE_LOG" 2>/dev/null || true)"
echo "(1) upsert pages — ChunkBatcher flushes: $CHUNKBATCHER_FLUSHES, direct upsert_chunks pages: $UPSERT_PAGES_DIRECT, total: $UPSERT_PAGES"
echo "    voyage sub-requests sent (engine, voyage_subrequest_sent): $VOYAGE_SUBREQUESTS"
if [[ "$UPSERT_PAGES" -gt 0 ]]; then
  RATIO=$(uv run python -c "print(round($VOYAGE_SUBREQUESTS / $UPSERT_PAGES, 3))")
  echo "    ratio (voyage requests / upsert page): $RATIO"
fi

# (2) Adaptive-split events.
ADAPTIVE_SPLITS="$(grep -c 'event=voyage_adaptive_split' "$ENGINE_LOG" 2>/dev/null || true)"
echo "(2) adaptive-split events (voyage_adaptive_split): $ADAPTIVE_SPLITS"

# (3) Observed bytes-per-token — sum(requestBytes)/sum(usageTokens) over every
#     voyage_subrequest_sent event with a valid (non -1) usageTokens.
BYTES_PER_TOKEN="$(uv run python -c "
import re
path = '$ENGINE_LOG'
total_bytes = 0
total_tokens = 0
n = 0
pat = re.compile(r'event=voyage_subrequest_sent .*?requestBytes=(\d+) estTokens=(\d+) usageTokens=(-?\d+)')
with open(path, encoding='utf-8', errors='replace') as f:
    for line in f:
        m = pat.search(line)
        if not m:
            continue
        rb, et, ut = int(m.group(1)), int(m.group(2)), int(m.group(3))
        if ut < 0:
            continue
        total_bytes += rb
        total_tokens += ut
        n += 1
if total_tokens == 0:
    print(f'n={n} total_bytes={total_bytes} total_tokens=0 ratio=UNDEFINED')
else:
    print(f'n={n} total_bytes={total_bytes} total_tokens={total_tokens} ratio={total_bytes/total_tokens:.4f}')
"
)"
echo "(3) bytes-per-token (engine voyage_subrequest_sent requestBytes/usageTokens): $BYTES_PER_TOKEN"
echo "    engine provisional divisor (PROVISIONAL_BYTES_PER_TOKEN) and client"
echo "    _CODE_UPSERT_BYTE_BUDGET (180000, implies 3.0 B/token assumption) are"
echo "    compared against this observed ratio in the write-up."

# (4) 429 count.
COUNT_429="$(grep -c 'event=voyage_retry attempt=[0-9]* status=429' "$ENGINE_LOG" 2>/dev/null || true)"
RETRY_TOTAL="$(grep -c 'event=voyage_retry' "$ENGINE_LOG" 2>/dev/null || true)"
echo "(4) 429 count (voyage_retry status=429): $COUNT_429"
echo "    total voyage_retry events (429+5xx): $RETRY_TOTAL"

# Terminal-failure count — must be zero for a pass.
TOO_MANY_ENGINE="$(grep -c 'event=vector_too_many_tokens_in_batch' "$ENGINE_LOG" 2>/dev/null || true)"
TOO_MANY_CLIENT="$(grep -c 'TOO_MANY_TOKENS_IN_BATCH' "$CLIENT_LOG" "$INDEX_LOG" 2>/dev/null | awk -F: '{s+=$2} END{print s+0}' || true)"
echo "TOO_MANY_TOKENS_IN_BATCH terminal failures — engine: $TOO_MANY_ENGINE, client/index: $TOO_MANY_CLIENT"

CHUNKS_WRITTEN="$(grep -oE 'chunks=[0-9]+' "$CLIENT_LOG" 2>/dev/null | grep -oE '[0-9]+' | awk '{s+=$1} END{print s+0}' || true)"
echo "total chunks written (best-effort, sum of chunk_flush_complete chunks=): $CHUNKS_WRITTEN"

echo "======================================================================"
echo ""

# RDR-195-SPECIFIC pass criteria: zero terminal TOO_MANY_TOKENS_IN_BATCH
# failures and a working search path. INDEX_RC is reported as a caveat,
# NOT folded into the gate — a nonzero exit from an unrelated cause (e.g. a
# single pathological non-text fixture tripping the PDF extractor's quality
# gate, which aborts run_file_loop via FIRST_EXCEPTION semantics regardless
# of how much Voyage embedding already succeeded and was already durably
# flushed) is a real bug worth reporting, but it is not evidence against
# RDR-195's batch-splitting behaviour, which is what this harness exists to
# validate. A caller that wants a stricter bar can additionally require
# INDEX_RC=0 — this script surfaces both signals distinctly rather than
# collapsing them.
PASS=1
[[ "$TOO_MANY_ENGINE" -eq 0 ]] || PASS=0
[[ "$TOO_MANY_CLIENT" -eq 0 ]] || PASS=0
[[ "$SEARCH_HITS" -gt 0 ]] || PASS=0

if [[ "$INDEX_RC" -ne 0 ]]; then
  echo "NOTE: nx index repo exited non-zero (rc=$INDEX_RC) — see $INDEX_LOG / $CLIENT_LOG tail for the cause."
  echo "      This harness's PASS/FAILED verdict below is scoped to RDR-195's batch-splitting"
  echo "      behaviour (measurements above) and does not by itself certify a clean full-repo index."
fi

if [[ "$PASS" -eq 1 ]]; then
  echo "RDR-195 MVV PASSED — zero TOO_MANY_TOKENS_IN_BATCH, search_hits=$SEARCH_HITS, 429_count=$COUNT_429, index_rc=$INDEX_RC"
else
  echo "RDR-195 MVV FAILED — too_many_engine=$TOO_MANY_ENGINE, too_many_client=$TOO_MANY_CLIENT, search_hits=$SEARCH_HITS, index_rc=$INDEX_RC"
fi
