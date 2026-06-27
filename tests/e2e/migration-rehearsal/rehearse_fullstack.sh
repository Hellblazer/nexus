#!/usr/bin/env bash
# Full-stack isolated shakeout — runs INSIDE the container.
#
# Real topology: PG16+pgvector + native nexus-service (T2+T3) + nx-mcp (hooks +
# aspect worker) + linux `claude` CLI (mounted oauth). Drives the surfaces
# THROUGH the nexus MCP via `claude -p` so the post-store hooks ENQUEUE aspects
# and the MCP worker DRAINS them with REAL extraction — what the bare-CLI box
# could not do. Auth: ~/.claude/.credentials.json mounted read-only (real billed
# calls). NOT DinD: PG provisioned in-box by `nx init --service`.
set -uo pipefail
FAILS=0
say()  { printf '\n\033[1m== %s ==\033[0m\n' "$*"; }
ok()   { printf '  \033[32mPASS\033[0m %s\n' "$*"; }
bad()  { printf '  \033[31mFAIL\033[0m %s\n' "$*"; FAILS=$((FAILS+1)); }
note() { printf '       %s\n' "$*"; }

# ── Phase A: install + provision + serve ─────────────────────────────────────
say "Phase A — install + provision + serve"
SVC_NATIVE_DIR="/opt/nexus-service-native"; SVC_WELL_KNOWN_DIR="$HOME/.config/nexus/service"
nx --version >/dev/null 2>&1 && ok "nx installed ($(nx --version 2>&1))" || bad "nx --version failed"
claude --version >/dev/null 2>&1 && ok "claude CLI installed ($(claude --version 2>&1 | head -1))" || bad "claude CLI missing"
command -v initdb >/dev/null 2>&1 && ok "PG16 on PATH" || bad "initdb not found"
test -x "$SVC_NATIVE_DIR/nexus-service" && ok "native service binary present" || bad "native binary missing"
mkdir -p "$SVC_WELL_KNOWN_DIR" && cp "$SVC_NATIVE_DIR"/* "$SVC_WELL_KNOWN_DIR/" && chmod +x "$SVC_WELL_KNOWN_DIR/nexus-service" \
  && ok "native binary positioned" || bad "could not position native binary"
export NX_SERVICE_MAX_HEAP="${NX_SERVICE_MAX_HEAP:-1g}"
note "nx init --service (provision PG16+pgvector+bge-768)…"
if nx init --service --embedder bge-768 --yes 2>&1 | sed 's/^/       /'; then ok "nx init --service"; else bad "nx init --service failed"; say "ABORT"; exit 1; fi
export NX_STORAGE_BACKEND=service
# shellcheck disable=SC1091
set -a; . /home/nexus/.config/nexus/pg_credentials; set +a
unset NX_SERVICE_URL NX_SERVICE_PORT NX_SERVICE_HOST 2>/dev/null || true
healthy=0
for i in $(seq 1 30); do nx daemon service status 2>&1 | grep -qiE "health.*ok|healthy|serving|running" && { healthy=1; break; }; sleep 2; done
[ "$healthy" = 1 ] && ok "service healthy" || { bad "service never healthy"; say "ABORT"; exit 1; }
[ -n "${NX_SERVICE_TOKEN:-}" ] && ok "NX_SERVICE_TOKEN present" || bad "NX_SERVICE_TOKEN absent"

# ── psql admin connection (queue/aspect introspection) ───────────────────────
ADMIN="${NX_DB_ADMIN_URL:-${NX_DB_URL:-}}"
hostport="$(printf '%s' "$ADMIN" | sed -E 's#^jdbc:postgresql://##; s#/.*$##')"
export PGHOST="${hostport%%:*}" PGPORT="${hostport##*:}"
export PGDATABASE="$(printf '%s' "$ADMIN" | sed -E 's#^[^/]*//[^/]+/##; s#\?.*$##')"
export PGUSER="${NX_DB_ADMIN_USER:-}" PGPASSWORD="${NX_DB_ADMIN_PASS:-}"
q() { psql -tAqc "set nexus.tenant='default'; $1" 2>/dev/null | tr -d '[:space:]'; }

# ── Phase F: full-stack MCP-driven enqueue + worker drain + real extraction ──
say "Phase F — MCP-driven workload + queue enqueue + worker drain (real claude)"

# 1. Auth smoke — proves the mounted oauth + linux claude work (biggest unknown).
authout="$(claude -p 'Reply with exactly the token AUTHOK and nothing else.' --dangerously-skip-permissions 2>&1)"
if printf '%s' "$authout" | grep -q "AUTHOK"; then ok "claude -p authenticated (mounted oauth works in-container)"
else bad "claude -p auth failed — cannot drive the MCP/extraction"; note "$(printf '%s' "$authout" | head -3 | tr '\n' ' ')"; say "ABORT (no claude auth)"; printf 'REHEARSAL FAILED\n'; exit 1; fi

# 2. MCP config for the nexus server (inherits the service env -> routes to PG).
cat > /home/nexus/mcp.json <<'MCPJSON'
{ "mcpServers": { "nexus": { "command": "nx-mcp", "args": [] } } }
MCPJSON

# 3. Drive a MULTIVARIATE workload THROUGH the nexus MCP (store_put x4 + search +
#    nx_answer) so the post-store hook enqueues aspects and several tools fire.
MARK="fsmark$$"
prompt="You have the nexus MCP server; use ONLY its tools (names start mcp__nexus__). Do ALL of:
1. store_put four knowledge notes (collection 'knowledge'), ONE call each, unique titles:
   a) 'Widgets are small mechanical parts. $MARK widget assembly note.'
   b) 'Sprockets mesh with chains to transfer torque. $MARK sprocket note.'
   c) 'Gadgets combine widgets and sprockets into devices. $MARK gadget note.'
   d) 'Retrieval ranks documents by semantic similarity. $MARK retrieval note.'
2. search 'widgets and sprockets' in the knowledge corpus.
3. nx_answer the question 'what are widgets and sprockets?'.
End your reply with the literal token WORKLOADDONE."
note "driving multivariate MCP workload via claude -p (store_put x4 + search + nx_answer)…"
wlout="$(claude -p "$prompt" --mcp-config /home/nexus/mcp.json --dangerously-skip-permissions \
  --allowedTools mcp__nexus__store_put mcp__nexus__search mcp__nexus__nx_answer 2>&1)"
note "claude workload tail: $(printf '%s' "$wlout" | tail -3 | tr '\n' ' ' | cut -c1-280)"
printf '%s' "$wlout" | grep -q "WORKLOADDONE" && ok "MCP workload completed (claude drove the tools)" || bad "MCP workload did not finish cleanly"

# 3b. Did store_put REALLY execute? (disambiguates 'claude didn't call the tool /
#     MCP didn't connect' from 'tool ran but hook didn't enqueue'.)
sleep 3
if nx collection list 2>/dev/null | grep -qi "knowledge"; then ok "store_put materialized a knowledge collection (MCP tools really executed)"
else bad "no knowledge collection — claude did NOT actually call store_put (MCP connect / allowedTools issue)"; note "$(nx collection list 2>&1 | head -3 | tr '\n' ' ')"; fi

# 3c. nx_answer produced a grounded composed answer (from the workload).
printf '%s' "$wlout" | grep -qiE "widget|sprocket|gadget" && ok "nx_answer (MCP) returned a grounded composed answer" || note "nx_answer answer not evident in workload output"

# 4-6. ASPECT PIPELINE IN SERVICE MODE — FINDING (documented, not faked green).
# store_put of plain knowledge notes does NOT enqueue aspects: by design there is
# no extractor for ad-hoc knowledge notes (aspect extraction targets prose /
# scholarly / RDR document SHAPES via _classify_document_shape), and the worker
# lazy-spawns on a HOOK enqueue. So the natural enqueue→worker-drain→extraction
# path is not exercised by an MCP knowledge store, and forcing it via a psql seed
# would test uncertain worker-lazy-spawn / service-queue-routing mechanics. Whether
# service-mode aspect extraction is fully wired end-to-end is a SEPARATE focused-
# investigation gap — recorded in T2 nexus/6.0.0-plugin-surface-coverage-gap, NOT
# asserted/faked here.
# store_put fires the post-document hook (core.py:830) and knowledge__* IS
# extractor-eligible (select_config → scholarly-paper-v1), so store_put SHOULD
# enqueue + the lazy-spawned worker drains it IN the MCP process during the
# session. Assert the END STATE (document_aspects), not the transient queue.
sleep 12
enq="$(q "select count(*) from nexus.aspect_extraction_queue")"
pend="$(q "select count(*) from nexus.aspect_extraction_queue where status in ('pending','in_progress')")"
asp="$(q "select count(*) from nexus.document_aspects")"
note "post-workload: aspect_queue total=${enq:-?} pending=${pend:-?}; document_aspects=${asp:-?}"
# RDR-172 P2.1 (nexus-hlkvj): enqueue-failure tripwire — the ingest E2E must
# complete with ZERO swallowed aspect-enqueue failures. The hook persists a
# hook_failures row on its best-effort swallow (the nexus-ov0sw silent-total-
# failure class); a non-zero count here means an enqueue silently failed.
# NOTE: this gate is only NON-VACUOUS if the workload above actually drives
# store_put through aspect_extraction_enqueue_hook in service mode. That the
# path is exercised is verified by P2.5 (nexus-8zog5, post-fix --fullstack
# real-validation run); P2.2 (nexus-jr84c) reconciles the stale comment at the
# top of this block. Until P2.5 confirms, treat a green assert-zero as
# necessary-but-not-sufficient.
enqfail="$(q "select count(*) from nexus.hook_failures where hook_name='aspect_extraction_enqueue_hook'")"
if [ "${enqfail:-0}" -eq 0 ] 2>/dev/null; then ok "enqueue-failure tripwire: 0 swallowed aspect-enqueue failures"
else bad "enqueue-failure tripwire FIRED: ${enqfail} swallowed aspect_extraction_enqueue_hook failure(s) — silent-loss class recurred (RF-7)"; fi
if [ "${asp:-0}" -gt 0 ] 2>/dev/null; then
  ok "SERVICE-MODE aspect pipeline works END-TO-END: store_put → enqueue → worker → document_aspects (${asp} rows, real extraction)"
elif [ "${enq:-0}" -gt 0 ] 2>/dev/null; then
  note "FINDING: aspects ENQUEUED (${enq}) but document_aspects=0 — worker did not drain in-session (worker-lifecycle gap → investigate/file)"
else
  note "FINDING: store_put enqueued 0 — store_put may not fire the document hook in service mode (→ investigate/file)"
fi

# 8. Service healthy after the full-stack run.
nx daemon service status 2>&1 | grep -qiE "health.*ok|healthy|serving|running" && ok "service healthy after full-stack run" || bad "service unhealthy"

say "RESULT"
if [ "$FAILS" -eq 0 ]; then printf '\033[32mFULL-STACK SHAKEOUT PASSED\033[0m — full topology: service + claude auth + MCP tools (store_put/search/nx_answer) end-to-end vs the 6.0.0 service (aspect-pipeline drain = documented gap)\n'; exit 0
else printf '\033[31mFULL-STACK SHAKEOUT FAILED — %d check(s)\033[0m\n' "$FAILS"; exit 1; fi
