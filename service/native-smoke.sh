#!/usr/bin/env bash
# nexus-lp2qo: native-image smoke test. Boots the native binary against a
# pgvector Postgres and asserts the full runtime path works: liquibase migration
# applies, and jOOQ INSERT / SELECT / Postgres-FTS-search all return 200 with
# real rows. Exits non-zero on any failure — used as the CI native gate and for
# local verification.
#
# Env (optional):
#   NX_DB_URL / NX_DB_USER / NX_DB_PASS  — point at an existing Postgres.
#       When unset, a throwaway pgvector/pgvector:pg17 container is started.
#   BIN  — path to the native binary (default: target/nexus-service)
set -uo pipefail
cd "$(dirname "$0")"
BIN="${BIN:-target/nexus-service}"
[ -x "$BIN" ] || { echo "FAIL: native binary not found/executable at $BIN"; exit 2; }

OWN_PG=0
if [ -z "${NX_DB_URL:-}" ]; then
  OWN_PG=1
  PGPORT=$(python3 -c "import socket;s=socket.socket();s.bind(('',0));print(s.getsockname()[1]);s.close()")
  docker rm -f lp2qo-smoke-pg >/dev/null 2>&1 || true
  docker run -d --name lp2qo-smoke-pg -e POSTGRES_DB=nexus -e POSTGRES_USER=nexus \
    -e POSTGRES_PASSWORD=nexus -p ${PGPORT}:5432 pgvector/pgvector:pg17 >/dev/null
  until docker exec lp2qo-smoke-pg pg_isready -U nexus >/dev/null 2>&1; do sleep 1; done
  sleep 2
  export NX_DB_URL="jdbc:postgresql://localhost:${PGPORT}/nexus"
  export NX_DB_USER=nexus NX_DB_PASS=nexus
fi

SVCPORT=$(python3 -c "import socket;s=socket.socket();s.bind(('',0));print(s.getsockname()[1]);s.close()")
export NX_SERVICE_PORT=$SVCPORT NX_SERVICE_TOKEN=smoketoken NX_EMBED_MODE=onnx

# nexus-rxqqd review follow-up (substantive-critic): when OWN_PG=1 (default),
# the whole throwaway docker container is torn down at exit, so the rows the
# curl assertions / real-Python-client blocks below write are irrelevant.
# When pointed at an EXTERNAL NX_DB_URL, though, those SAME rows have no
# cleanup path today and accumulate in whatever real Postgres was passed in.
# The real-Python-client blocks (which already have delete/close primitives
# on hand) best-effort clean up their own rows when this is set; the
# curl-only assertions near the top of this file are lower-priority (no
# convenient single-call delete without adding new curl plumbing) and are
# left as a smaller, separately-trackable residual if this ever bites --
# tracked as nexus-hnh20.
NATIVE_SMOKE_CLEANUP_ROWS=0
[ "$OWN_PG" != "1" ] && NATIVE_SMOKE_CLEANUP_ROWS=1

cleanup() {
  [ -n "${SVCPID:-}" ] && kill "$SVCPID" 2>/dev/null
  [ "$OWN_PG" = "1" ] && docker rm -f lp2qo-smoke-pg >/dev/null 2>&1
  # nexus-rxqqd review follow-up (code-review-expert): the real-Python-client
  # blocks below create their own mktemp -d NEXUS_CONFIG_DIR isolation dirs
  # and rm -rf them on their own normal-path exit, but a Ctrl-C (or any other
  # signal) mid-`uv run` skipped straight past that rm -rf and leaked the
  # tmpdir on disk. Covering both here too so the EXIT trap is the actual
  # backstop, not just the happy path.
  [ -n "${T1_PY_TMPDIR:-}" ] && rm -rf "$T1_PY_TMPDIR"
  [ -n "${T2_PY_TMPDIR:-}" ] && rm -rf "$T2_PY_TMPDIR"
}
trap cleanup EXIT

"$BIN" > /tmp/native-smoke-svc.log 2>&1 &
SVCPID=$!
U="http://localhost:${SVCPORT}"

UP=0
for i in $(seq 1 60); do
  kill -0 $SVCPID 2>/dev/null || { echo "FAIL: service exited during startup"; tail -40 /tmp/native-smoke-svc.log; exit 1; }
  curl -fsS "$U/health" >/dev/null 2>&1 && { UP=1; break; }
  sleep 1
done
[ "$UP" = "1" ] || { echo "FAIL: service never became healthy"; tail -40 /tmp/native-smoke-svc.log; exit 1; }

# Migration must have applied (changeset_count > 0).
VER=$(curl -fsS -H "Authorization: Bearer smoketoken" "$U/version")
echo "version: $VER"
echo "$VER" | grep -qE '"schema_changeset_count":[1-9]' || { echo "FAIL: migration did not apply"; tail -40 /tmp/native-smoke-svc.log; exit 1; }

fail=0
assert() { # name expected_code curl-args...
  local name="$1" exp="$2"; shift 2
  local code; code=$(curl -s -o /tmp/ns.out -w "%{http_code}" "$@")
  if [ "$code" = "$exp" ]; then echo "  ok   $name -> $code"; else echo "  FAIL $name -> $code (want $exp): $(head -c160 /tmp/ns.out)"; fail=1; fi
}
A=(-H "Authorization: Bearer smoketoken"); J=(-H "Content-Type: application/json")
echo "jOOQ runtime path:"
assert "memory/put (INSERT)"   200 "${A[@]}" "${J[@]}" -X POST -d '{"project":"smoke","title":"a","content":"native ok","tags":"t","ttl":30}' "$U/v1/memory/put"
assert "memory/get (SELECT)"   200 "${A[@]}" "$U/v1/memory/get?project=smoke&title=a"
assert "memory/search (FTS)"   200 "${A[@]}" "${J[@]}" -X POST -d '{"query":"native","project":"smoke"}' "$U/v1/memory/search"
assert "memory/list"           200 "${A[@]}" "$U/v1/memory/list?project=smoke"
assert "plans/search"          200 "${A[@]}" "${J[@]}" -X POST -d '{"query":"q","project":"smoke"}' "$U/v1/plans/search"
assert "taxonomy/topics"       200 "${A[@]}" "$U/v1/taxonomy/topics?collection=knowledge__x"
assert "chash/distinct"        200 "${A[@]}" "$U/v1/chash/distinct_collections"

# ── T1 scratch (separate jOOQ schema, nexus-opr9m) ───────────────────────────
# T1 scratch lives in its OWN generated jOOQ schema (t1, e.g.
# dev.nexus.service.jooq.t1.T1) — a completely separate schema model from every
# assertion above (all of which are in the `nexus` schema). JooqRecordReflectionFeature
# enumerated only Nexus.NEXUS.getTables() and never T1.T1.getTables(), so
# ScratchRecord's constructor was unreachable via reflection in every native image
# built since the t1 schema was introduced (nexus-gmiaf.13) — every deployed
# get/search/list against T1 500'd with MissingReflectionRegistrationError, and
# this gate (which ran on every native build the whole time) never caught it because
# nothing above touches /v1/t1/* or /v1/sessions/*. Mint a session first (the real
# production path — mirrors mcp/core.py's lifespan) then exercise the full T1
# put/get/search/list surface so a future new generated schema being added without
# updating JooqRecordReflectionFeature fails HERE, not silently in production.
echo "T1 scratch runtime path (separate jOOQ schema):"
SESSION_RESP=$(curl -fsS "${A[@]}" "${J[@]}" -X POST -d '{"session_id":"native-smoke-t1"}' "$U/v1/sessions/start")
SESSION_TOKEN=$(python3 -c "import json,sys; print(json.loads(sys.argv[1])['session_token'])" "$SESSION_RESP" 2>/dev/null)
if [ -z "$SESSION_TOKEN" ]; then
  echo "  FAIL t1/session-mint -> could not mint session: $SESSION_RESP"; fail=1
else
  echo "  ok   t1/session-mint -> 200"
  T1=(-H "Authorization: Bearer smoketoken" -H "X-Nexus-T1-Session: ${SESSION_TOKEN}")
  PUT_RESP=$(curl -fsS "${T1[@]}" "${J[@]}" -X POST -d '{"id":"native-smoke-t1-id","session_id":"native-smoke-t1","content":"t1 native smoke","tags":"","flagged":false}' "$U/v1/t1/put")
  echo "$PUT_RESP" | grep -q '"id"' && echo "  ok   t1/put (INSERT) -> 200" || { echo "  FAIL t1/put -> $PUT_RESP"; fail=1; }
  assert "t1/get (SELECT, separate schema)"  200 "${T1[@]}" "${J[@]}" -X POST -d '{"id":"native-smoke-t1-id","session_id":"native-smoke-t1"}' "$U/v1/t1/get"
  assert "t1/search (FTS, separate schema)"  200 "${T1[@]}" "${J[@]}" -X POST -d '{"query":"native smoke","session_id":"native-smoke-t1","limit":5}' "$U/v1/t1/search"
  assert "t1/list (separate schema)"         200 "${T1[@]}" "${J[@]}" -X POST -d '{"session_id":"native-smoke-t1"}' "$U/v1/t1/list"
fi

# ── T1 via the REAL Python client (nexus-97oz3) ──────────────────────────────
# Everything above proves the native binary's /v1/t1/* endpoints work when
# driven by raw curl. It does NOT prove nexus.db.t1.get_t1_database()'s
# three-tier session-routing, HttpTokenStore.start_session() minting, or
# HttpScratchStore's request/response handling actually work against this
# SAME compiled artifact — nexus-opr9m (the reflection bug this file's T1
# section above was written to catch) came back clean on the JVM AND would
# have come back clean here too if this section had bypassed the real client
# code the way the curl section above does. Routing-correctness (tested
# elsewhere, against a JVM backend) and backend-correctness (tested above,
# via curl) have never been proven TOGETHER against the actual production
# artifact reached through the actual production client code. This closes
# that seam FOR T1 SPECIFICALLY: mint + put + get + search + list through
# the real Python nexus.db.t1.get_t1_database() factory (the identical code
# path a live bare-CLI `nx scratch` invocation takes) against the native
# binary this script just booted.
#
# The identical gap for the other jOOQ-backed endpoint families this script
# already curls above (memory/plans/taxonomy/chash) is now ALSO closed --
# see the "memory/plans/taxonomy/chash via the real Python client" section
# below (nexus-rxqqd), same fix shape, same CI wiring.
#
# ALSO requires `uv` on PATH with the nexus package importable (`uv sync`
# from the repo root) to actually run — see the CI workflow step this
# depends on (engine-service-release.yml, gated on matrix.target.smoke).
# WARN+skip loudly if unavailable rather than silently pass — a run without
# this dependency present does NOT cover routing+backend together, only
# backend-via-curl above, regardless of how green the rest of the output is.
# NOTE (unlike the bge-embed section below, whose model IS provisioned in CI
# via .github/actions/prime-bge-onnx and only rarely skips): until
# nexus-l8ybx's companion CI workflow change lands, this WARN is the
# deterministic 100%-of-runs outcome in CI, not a rare fallback — do not
# read a green run as proof this section executed.
echo "T1 via the real Python client (routing + backend together):"
REPO_ROOT="$(cd .. && pwd)"
# `timeout` is GNU coreutils -- present on every CI runner (Linux) but not
# guaranteed on a vanilla local macOS dev machine (only via `brew install
# coreutils`, and even then often as `gtimeout`). Degrade gracefully rather
# than break local runs that lack it.
TIMEOUT_CMD=""
command -v timeout >/dev/null 2>&1 && TIMEOUT_CMD="timeout 60"
if command -v uv >/dev/null 2>&1 && [ -f "$REPO_ROOT/pyproject.toml" ]; then
  T1_PY_TMPDIR=$(mktemp -d)
  # NX_SERVICE_URL='' (nexus-rxqqd review): NEXUS_CONFIG_DIR isolation alone is
  # NOT sufficient. resolve_service_endpoint() -> get_credential("service_url")
  # checks the NX_SERVICE_URL env var BEFORE falling back to config.yml -- so an
  # AMBIENT NX_SERVICE_URL already exported in the invoking shell (a real,
  # documented practice on this project) would pass through unisolated and
  # target that URL instead of this script's local native binary, bearing the
  # smoketoken bearer. Explicitly clearing it here closes that leg too.
  PY_OUT=$(cd "$REPO_ROOT" && NEXUS_CONFIG_DIR="$T1_PY_TMPDIR" NX_SERVICE_URL='' \
    NX_SERVICE_HOST=127.0.0.1 NX_SERVICE_PORT="$SVCPORT" NX_SERVICE_TOKEN=smoketoken \
    NX_STORAGE_BACKEND=service NATIVE_SMOKE_CLEANUP_ROWS="$NATIVE_SMOKE_CLEANUP_ROWS" \
    $TIMEOUT_CMD uv run python -c '
import os

from nexus.db.t1 import get_t1_database

t1 = get_t1_database()
doc_id = t1.put("t1 native smoke via real python client", tags="native-smoke-py")
assert doc_id, "put returned no id"

got = t1.get(doc_id)
assert got is not None, "get returned None for a just-put id"
assert got["content"] == "t1 native smoke via real python client", got

results = t1.search("native smoke via real python", n_results=5)
assert any(r["id"] == doc_id for r in results), f"search did not find {doc_id}: {results}"

entries = t1.list_entries()
assert any(e["id"] == doc_id for e in entries), f"list_entries did not find {doc_id}: {entries}"

if os.environ.get("NATIVE_SMOKE_CLEANUP_ROWS") == "1":
    # Best-effort, deliberately isolated from the assertions above (review
    # follow-up, substantive-critic): a cleanup-only exception here must
    # never read as an assertion failure to whoever is debugging a FAIL --
    # print CLEANUP-WARN and keep going rather than let it propagate past
    # print("OK"), which would make grep -q "^OK$" fail identically to a
    # genuine assertion failure despite every real check above having passed.
    try:
        t1.delete(doc_id)
    except Exception as exc:  # noqa: BLE001 — best-effort cleanup, see comment above
        print(f"CLEANUP-WARN: t1.delete({doc_id!r}) failed: {exc}")

print("OK")
' 2>&1)
  rm -rf "$T1_PY_TMPDIR"
  if echo "$PY_OUT" | grep -q "^OK$"; then
    echo "  ok   t1 real-client put/get/search/list (routing + backend together)"
  else
    echo "  FAIL t1 real-client check:"; echo "$PY_OUT" | sed 's/^/    /'; fail=1
  fi
else
  echo "  WARN skipping (uv or pyproject.toml not found at $REPO_ROOT) -- this run does NOT cover routing+backend together, only backend-via-curl above"
fi

# ── memory/plans/taxonomy/chash via the REAL Python client (nexus-rxqqd) ────
# nexus-97oz3 closed the routing-correctness x backend-correctness seam for T1
# specifically (block above). The identical gap remained open for every OTHER
# jOOQ-backed endpoint family this script already curls near the top of this
# file (memory/put,get,search,list; plans/search; taxonomy/topics;
# chash/distinct_collections) -- each has its own real Python HTTP client
# (src/nexus/db/t2/http_memory_store.py and siblings, structurally identical
# to http_scratch_store.py) that was equally untested against the compiled
# native artifact. A future native-image-only reflection gap in any of these
# OTHER jOOQ schemas would ship exactly the way T1's (nexus-opr9m) did,
# silently, through this same gate. Unlike get_t1_database(), these four
# classes take no session-lease routing -- HttpMemoryStore() etc. construct
# ready to use with zero args, reading the same NX_SERVICE_HOST/PORT/TOKEN
# env below. Reuses the SAME already-running native-smoke instance and the
# SAME uv/Python CI wiring nexus-l8ybx already added for the T1 block above
# -- no new CI plumbing needed. taxonomy's smoke-tested endpoints (topics)
# are pure reads that 200 even against empty state, so it seeds one row
# first (import_topic) to make the "found" assertion meaningful, mirroring
# T1's own put-then-find shape. chash is the exception since RDR-187: writes
# are accept-and-no-op and reads serve from the chunks tables, so
# read-your-write is impossible BY DESIGN -- its assertions below pin that
# contract (write acked, then NOT visible) instead of put-then-find.
#
# Deliberately a SEPARATE `uv run python -c` subprocess from the T1 block
# above, not more assertions appended into that SAME block (nexus-rxqqd
# review, substantive-critic): the T1 block sets NX_STORAGE_BACKEND=service
# because get_t1_database() branches on it; these four clients take no
# such env var and construct directly (HttpMemoryStore() etc., zero args),
# so merging the blocks would either leave NX_STORAGE_BACKEND=service set
# for no reason here or, worse, invite a future edit to rely on it. If a
# future endpoint family's client DOES need get_t1_database()-style env
# branching, put it in the T1-shaped block, not this one.
echo "memory/plans/taxonomy/chash via the real Python client:"
if command -v uv >/dev/null 2>&1 && [ -f "$REPO_ROOT/pyproject.toml" ]; then
  T2_PY_TMPDIR=$(mktemp -d)
  # NEXUS_CONFIG_DIR isolation alone is NOT sufficient -- see the identical
  # NX_SERVICE_URL='' comment on the T1 block above; the same ambient-env-var
  # leak applies here and is closed the same way.
  PY_OUT=$(cd "$REPO_ROOT" && NEXUS_CONFIG_DIR="$T2_PY_TMPDIR" NX_SERVICE_URL='' \
    NX_SERVICE_HOST=127.0.0.1 NX_SERVICE_PORT="$SVCPORT" NX_SERVICE_TOKEN=smoketoken \
    NATIVE_SMOKE_CLEANUP_ROWS="$NATIVE_SMOKE_CLEANUP_ROWS" \
    $TIMEOUT_CMD uv run python -c '
import os

from nexus.db.t2.http_chash_index import HttpChashIndex
from nexus.db.t2.http_memory_store import HttpMemoryStore
from nexus.db.t2.http_plan_library import HttpPlanLibrary
from nexus.db.t2.http_taxonomy_store import HttpTaxonomyStore

mem = HttpMemoryStore()
mem.put(project="native-smoke-py", title="a", content="memory native smoke", tags="t")
got = mem.get(project="native-smoke-py", title="a")
assert got is not None, "memory.get returned None for a just-put entry"
assert got["content"] == "memory native smoke", got
results = mem.search("native smoke", project="native-smoke-py")
assert any(r.get("title") == "a" for r in results), f"memory.search did not find title=a: {results}"
entries = mem.list_entries(project="native-smoke-py")
assert any(e.get("title") == "a" for e in entries), f"memory.list_entries did not find title=a: {entries}"

plans = HttpPlanLibrary()
plan_id = plans.save_plan(query="native smoke plan query", plan_json="{\"steps\": []}", project="native-smoke-py")
plan_results = plans.search_plans("native smoke plan", project="native-smoke-py")
assert plan_results, f"plans.search_plans found nothing: {plan_results}"

tax = HttpTaxonomyStore()
# Fixed, not random (nexus-rxqqd review): deterministic per project convention,
# and import_topic is an ID-preserving upsert -- reusing the same src_id on a
# rerun overwrites in place instead of accumulating a fresh row every run.
src_id = 999_888_777
tax.import_topic(
    src_id=src_id, label="native-smoke-topic", parent_id=None,
    collection="native-smoke-py", centroid_hash=None, doc_count=1,
    created_at="2026-01-01T00:00:00Z", review_status="approved", terms=None,
)
topics = tax.get_all_topics(collection="native-smoke-py")
assert any(t.get("label") == "native-smoke-topic" for t in topics), f"taxonomy.get_all_topics did not find the seeded topic: {topics}"

# RDR-187 P6 410-flip (nexus-piwya.11, engine-service-v0.1.53): the
# /v1/chash/* WRITE endpoints are now 410 Gone (the accept-and-no-op window
# closed). Chunk ingest is the sole write path; the chash router is retired.
# So the leg pins the RETIRED-write contract: upsert now RAISES HTTP 410
# (the production caller dual_write_chash_index swallows this best-effort;
# the smoke calls the client directly so it observes the raise), while the
# surviving READ endpoints still respond and surface nothing.
import httpx
chash = HttpChashIndex()
try:
    chash.upsert(chash="deadbeef" * 8, collection="native-smoke-py")
    raise AssertionError("chash.upsert must 410 post-RDR-187 P6, but it did not raise")
except httpx.HTTPStatusError as exc:
    assert exc.response.status_code == 410, f"chash.upsert must 410 (RDR-187 retirement), got HTTP {exc.response.status_code}"
collections = chash.distinct_collections()
assert "native-smoke-py" not in collections, f"retired chash.upsert must leave no trace: {collections}"
rows = chash.lookup("deadbeef" * 8)
assert rows == [], f"retired chash.upsert must leave no readable row: {rows}"

if os.environ.get("NATIVE_SMOKE_CLEANUP_ROWS") == "1":
    # nexus-rxqqd review follow-up (substantive-critic): best-effort row
    # cleanup, only when pointed at an external (non-throwaway) NX_DB_URL --
    # see the NATIVE_SMOKE_CLEANUP_ROWS comment near the top of this file for why.
    #
    # Each delete wrapped individually and isolated from the assertions above
    # (2nd-round review follow-up, substantive-critic): a cleanup-only
    # exception must never masquerade as an assertion failure to whoever is
    # debugging a FAIL -- print CLEANUP-WARN and keep going for each surface
    # independently, rather than let one failure (a) abort the remaining
    # three deletes and (b) skip print("OK"), making grep -q "^OK$" fail
    # identically to a genuine assertion failure despite every real check
    # above having already passed.
    for _label, _cleanup in (
        ("memory", lambda: mem.delete(project="native-smoke-py", title="a")),
        ("plans", lambda: plans.delete_plan(plan_id)),
        ("taxonomy", lambda: tax.delete_topic(src_id)),
        ("chash", lambda: chash.delete_collection("native-smoke-py")),
    ):
        try:
            _cleanup()
        except Exception as exc:  # noqa: BLE001 — best-effort cleanup, see comment above
            print(f"CLEANUP-WARN: {_label} cleanup failed: {exc}")

print("OK")
' 2>&1)
  rm -rf "$T2_PY_TMPDIR"
  if echo "$PY_OUT" | grep -q "^OK$"; then
    echo "  ok   memory/plans/taxonomy/chash real-client put/get/search/list (routing + backend together)"
  else
    echo "  FAIL memory/plans/taxonomy/chash real-client check:"; echo "$PY_OUT" | sed 's/^/    /'; fail=1
  fi
else
  echo "  WARN skipping (uv or pyproject.toml not found at $REPO_ROOT) -- this run does NOT cover routing+backend together for memory/plans/taxonomy/chash, only backend-via-curl above"
fi

# ── Local bge-768 EMBED (nexus-pqatt) ────────────────────────────────────────
# The embed path drives the DJL HuggingFace tokenizers JNI (libtokenizers.so) and
# the onnxruntime session run — both of which need jniAccessible registrations the
# native image previously omitted, so the FIRST embed SIGABRTed at lib.rs:475
# (Result::unwrap on a JavaException) while every other endpoint above worked. The
# old gate never embedded, so the crash shipped. We assert the encode+infer path
# returns a real 768-dim vector. Requires the ~416MB bge ONNX model (provisioned by
# `nx init --service`). If it is absent we WARN+skip loudly rather than silently
# pass — a model-less CI run must not read as "embed covered".
BGE_MODEL="${NX_BGE_MODEL_PATH:-$HOME/.cache/nexus/onnx_models/bge-base-en-v1.5/onnx/model.onnx}"
if [ -f "$BGE_MODEL" ]; then
  echo "local bge-768 embed path:"
  ecode=$(curl -s -o /tmp/ns-embed.out -w "%{http_code}" "${A[@]}" "${J[@]}" -X POST \
    -d '{"collection":"knowledge__x","texts":["native embed smoke"]}' "$U/v1/vectors/embed")
  if [ "$ecode" = "200" ] && grep -q '"embeddings"' /tmp/ns-embed.out \
     && [ "$(python3 -c "import json,sys;print(len(json.load(open('/tmp/ns-embed.out'))['embeddings'][0]))" 2>/dev/null)" = "768" ]; then
    echo "  ok   embed (DJL tokenizer JNI + onnx run) -> 200, 768-dim"
  else
    echo "  FAIL embed -> $ecode (want 200 + 768-dim): $(head -c200 /tmp/ns-embed.out)"; fail=1
  fi
else
  echo "  WARN embed path NOT covered — bge model absent at $BGE_MODEL"
  echo "       (set NX_BGE_MODEL_PATH or provision via 'nx init --service' to gate the JNI embed path)"
fi

# ── Fused rerank stage (RDR-188 P1) ──────────────────────────────────────────
# Reranks (query, chunk) pairs through the local ms-marco cross-encoder — DJL
# PAIR tokenization + a second OrtSession, the same native JNI risk class as the
# bge embed above. store-put two chunks, search with rerank=true, and assert the
# structured envelope. With the ~91MB model present (CI: prime-crossencoder-onnx)
# the STRONG path must return real scores; absent, the LOUD-degrade contract is
# asserted instead — either way the envelope is exercised, never silently skipped.
if [ -f "$BGE_MODEL" ]; then
  echo "fused rerank stage (RDR-188):"
  RCOL="knowledge__nativesmoke__bge-base-en-v15-768__v1"
  put_rerank_chunk() {
    curl -s -o /tmp/ns-rerank-put.out -w "%{http_code}" "${A[@]}" "${J[@]}" -X POST \
      -d "{\"collection\":\"$RCOL\",\"doc_id\":\"$1\",\"content\":$2}" "$U/v1/vectors/store-put"
  }
  p1=$(put_rerank_chunk "$(printf 'e%.0s' {1..64})" '"Mix flour and water, ferment the dough, bake the bread in a hot oven."')
  p2=$(put_rerank_chunk "$(printf 'f%.0s' {1..64})" '"Quantum chromodynamics describes the strong interaction between quarks."')
  if [ "$p1" = "200" ] && [ "$p2" = "200" ]; then
    rcode=$(curl -s -o /tmp/ns-rerank.out -w "%{http_code}" "${A[@]}" "${J[@]}" -X POST \
      -d "{\"query\":\"how do I bake bread\",\"collections\":[\"$RCOL\"],\"n_results\":2,\"rerank\":true}" \
      "$U/v1/vectors/search")
    CE_MODEL="${NX_CROSSENCODER_MODEL_PATH:-$HOME/.cache/nexus/onnx_models/ms-marco-minilm-l6-v2/onnx/model.onnx}"
    if [ -f "$CE_MODEL" ]; then
      if [ "$rcode" = "200" ] && python3 - <<'PYEOF' 2>/dev/null
import json
r = json.load(open("/tmp/ns-rerank.out"))
assert r["rerank_degraded"] is False, r.get("rerank_error")
assert r["rerank_model"] == "ms-marco-minilm-l6-v2"
rows = r["results"]
assert len(rows) == 2 and all("rerank_score" in x for x in rows)
assert "bread" in rows[0]["content"]
PYEOF
      then
        echo "  ok   rerank=true -> cross-encoder scores in native image (correct top doc)"
      else
        echo "  FAIL rerank strong path -> $rcode: $(head -c300 /tmp/ns-rerank.out)"; fail=1
      fi
    else
      if [ "$rcode" = "200" ] && python3 - <<'PYEOF' 2>/dev/null
import json
r = json.load(open("/tmp/ns-rerank.out"))
assert r["rerank_degraded"] is True and "not found" in r["rerank_error"]
assert len(r["results"]) == 2
PYEOF
      then
        echo "  ok   rerank=true -> LOUD structured degrade (model absent at $CE_MODEL)"
        echo "       (prime the ms-marco ONNX to exercise the strong scoring path)"
      else
        echo "  FAIL rerank degrade path -> $rcode: $(head -c300 /tmp/ns-rerank.out)"; fail=1
      fi
    fi
  else
    echo "  FAIL rerank fixture store-put -> $p1/$p2: $(head -c200 /tmp/ns-rerank-put.out)"; fail=1
  fi
else
  echo "  WARN rerank stage NOT covered — bge model absent (store-put needs the embedder)"
fi

if grep -qiE "MissingReflection|NoClassDefFound|UnsatisfiedLink|NullPointerException" /tmp/native-smoke-svc.log; then
  echo "FAIL: native runtime error in service log:"; grep -iE "MissingReflection|NoClassDefFound|UnsatisfiedLink|NullPointerException" /tmp/native-smoke-svc.log | head; fail=1
fi

# ── Voyage-mode boot + egress-proxy wiring (nexus-myg2d) ──────────────────────
# The local-mode boot above never exercises the CLOUD (voyage) config path — the
# exact coverage gap that let two native-image-vs-JVM regressions ship to conexus
# deploys: nexus-0n7uc (voyage-branch OnnxEmbedder boot segfault) and nexus-f1syh
# (Voyage HttpClient ignored the egress proxy). Boot the SAME binary in voyage mode
# with a proxy and assert it (1) boots clean (segfault guard), (2) selected voyage
# mode, (3) wired the proxy onto the client from HTTPS_PROXY. The proxy points at a
# closed local port: construction must succeed (no network call at build time);
# real Voyage routing-through-proxy is covered by EgressProxyTest + the cloud STEP-6.
echo "voyage-mode boot + egress proxy:"
kill "$SVCPID" 2>/dev/null; wait "$SVCPID" 2>/dev/null
# The engine migrates on boot (Main: SchemaMigrator before the HTTP bind), so a second
# boot must target a CLEAN database — re-running Liquibase over the local-mode DB hits
# "relation already exists". With our own PG, create a fresh DB in the same container;
# with an external NX_DB_URL we can't safely reset it, so skip this phase there.
if [ "$OWN_PG" != "1" ]; then
  echo "  skip   voyage-mode phase (external NX_DB_URL — needs a clean DB)"
else
  docker exec lp2qo-smoke-pg psql -U nexus -d nexus -c 'CREATE DATABASE voyagesmoke;' >/dev/null 2>&1 || true
  DEADPORT=$(python3 -c "import socket;s=socket.socket();s.bind(('',0));print(s.getsockname()[1]);s.close()")
  NX_DB_URL="jdbc:postgresql://localhost:${PGPORT}/voyagesmoke" \
    NX_VOYAGE_API_KEY=dummy-smoke-key HTTPS_PROXY="http://127.0.0.1:${DEADPORT}" \
    "$BIN" > /tmp/native-smoke-voyage.log 2>&1 &
SVCPID=$!
VUP=0
for i in $(seq 1 60); do
  kill -0 $SVCPID 2>/dev/null || { echo "FAIL: voyage-mode service exited during startup (segfault?)"; tail -40 /tmp/native-smoke-voyage.log; exit 1; }
  curl -fsS "$U/health" >/dev/null 2>&1 && { VUP=1; break; }
  sleep 1
done
[ "$VUP" = "1" ] || { echo "FAIL: voyage-mode service never became healthy"; tail -40 /tmp/native-smoke-voyage.log; exit 1; }
# (2) took the cloud (voyage) embedding branch, not local bge/onnx
if grep -qE 'event=embedding_mode_banner mode=voyage' /tmp/native-smoke-voyage.log; then
  echo "  ok   voyage-mode boot (no segfault)"
else
  echo "  FAIL voyage mode not selected:"; grep embedding_mode_banner /tmp/native-smoke-voyage.log | head; fail=1
fi
# (3) EgressProxy parsed HTTPS_PROXY and set the proxy on the Voyage client
if grep -qE "event=egress_proxy_configured.*port=${DEADPORT}" /tmp/native-smoke-voyage.log; then
  echo "  ok   egress proxy wired from HTTPS_PROXY -> 127.0.0.1:${DEADPORT}"
else
  echo "  FAIL egress proxy not configured from HTTPS_PROXY:"; grep egress_proxy /tmp/native-smoke-voyage.log | head; fail=1
fi
if grep -qiE "MissingReflection|NoClassDefFound|UnsatisfiedLink|NullPointerException" /tmp/native-smoke-voyage.log; then
  echo "FAIL: native runtime error in voyage-mode service log:"; grep -iE "MissingReflection|NoClassDefFound|UnsatisfiedLink|NullPointerException" /tmp/native-smoke-voyage.log | head; fail=1
fi
fi  # end voyage-mode phase (OWN_PG)

if [ "$fail" = "0" ]; then echo "NATIVE SMOKE PASS"; exit 0; else echo "NATIVE SMOKE FAIL"; exit 1; fi
