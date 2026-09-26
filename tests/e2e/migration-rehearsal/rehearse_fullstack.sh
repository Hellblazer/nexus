#!/usr/bin/env bash
# Full-stack isolated shakeout — runs INSIDE the container.
#
# Real topology: PG16+pgvector + native nexus-service (T2+T3) + nx-mcp (hooks +
# aspect worker) + linux `claude` CLI (RDR-219 automation token via
# CLAUDE_CODE_OAUTH_TOKEN in the environment). Drives the surfaces THROUGH the
# nexus MCP via `claude -p` so the post-store hooks ENQUEUE aspects and the MCP
# worker DRAINS them with REAL extraction — what the bare-CLI box could not do.
# Auth: the harness's own automation token, never the operator's interactive
# login (real billed calls). NOT DinD: PG provisioned in-box by `nx init --service`.
set -uo pipefail
source "$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)/lib/require_container.sh"
# RDR-219 amendment Phase 3b Step 3 (nexus-wauo1.40): the nx-mcp dispatch
# grant proofs. Forwarded from run.sh's --grant sub-flag (--fullstack
# --grant) as NX_FULLSTACK_GRANT; forwarded unconditionally as 0 or 1 (never
# only when set), so an absent variable and an explicit "off" are the same
# thing here. GRANT MODE (1): claude runs through claude_mcp_grant.sh
# instead of a plain file-based MCP config, the Phase 2 aspect-worker
# pre-start is skipped so nx-mcp must spawn its own worker under the grant,
# and the workload adds operator_summarize, a tool-granting nested dispatch
# (nx_enrich_beads) and a no-leak check. Default (0): every check below is
# byte-for-byte the pre-existing --fullstack behavior.
GRANT_MODE="${NX_FULLSTACK_GRANT:-0}"
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
command -v initdb >/dev/null 2>&1 && bad "system PostgreSQL present — bare-machine posture violated (nexus-5qefg)" || ok "no system PostgreSQL (bundle must provide it)"
test -x "$SVC_NATIVE_DIR/nexus-service" && ok "native service binary present" || bad "native binary missing"
mkdir -p "$SVC_WELL_KNOWN_DIR" && cp "$SVC_NATIVE_DIR"/* "$SVC_WELL_KNOWN_DIR/" && chmod +x "$SVC_WELL_KNOWN_DIR/nexus-service" \
  && ok "native binary positioned" || bad "could not position native binary"
export NX_SERVICE_MAX_HEAP="${NX_SERVICE_MAX_HEAP:-1g}"
# --no-autostart (RDR-174 P2.4): session supervisor only, no persistent OS unit
# in the container (pre-P2.4 `--yes` was a no-op; it now installs the unit).
note "nx init --service (provision PG16+pgvector+bge-768)…"
if nx init --service --embedder bge-768 --no-autostart 2>&1 | sed 's/^/       /'; then ok "nx init --service"; else bad "nx init --service failed"; say "ABORT"; exit 1; fi
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
# nexus-5qefg: the image ships NO system PostgreSQL — resolve psql from the
# signed bundle `nx init` extracted (<config>/pg-bundle/**/bin/psql).
PSQL="$(find "$HOME/.config/nexus/pg-bundle" -type f -name psql 2>/dev/null | head -1)"
[ -n "$PSQL" ] || PSQL=psql # host-PG dev-box fallback
q() { "$PSQL" -tAqc "set nexus.tenant='default'; $1" 2>/dev/null | tr -d '[:space:]'; }

# ── Phase F: full-stack MCP-driven enqueue + worker drain + real extraction ──
say "Phase F — MCP-driven workload + queue enqueue + worker drain (real claude)"

# 0. Pre-start the RDR-173 leased aspect-worker daemon from THIS shell, which
# has CLAUDE_CODE_OAUTH_TOKEN (docker -e, RDR-219) — not from nx-mcp, which
# never gets it. nexus-wauo1.15, empirically reproduced: a `claude -p` Bash-
# tool child reports `env | grep -c CLAUDE_CODE_OAUTH_TOKEN` == 0; an explicit
# `"env": {"CLAUDE_CODE_OAUTH_TOKEN": "${CLAUDE_CODE_OAUTH_TOKEN}"}` block in
# an MCP server's own .mcp.json config STILL leaves it absent in that server's
# environment (while the identical shape with an arbitrary non-credential var
# DOES pass through) — Claude Code deliberately strips CLAUDE_CODE_OAUTH_TOKEN from
# every subprocess it spawns itself, Bash tool and MCP stdio server alike,
# regardless of how the child asks for it. That silently starved the aspect-
# worker daemon's own nested `claude -p` once this harness moved off the
# credentials FILE (readable by any process regardless of inherited env) onto
# the token-only mechanism — not a bug in the migration's credential plumbing,
# a hard security boundary in Claude Code itself, so no env-based workaround
# from these scripts can cross it.
# ensure_aspect_worker_daemon() is itself idempotent spawn-if-absent (it
# discovers the RDR-149 leased-tier registry; a fresh, current-version lease
# means it returns without spawning) — this call and nx-mcp's own later call
# (once store_put fires the enqueue hook, Phase F step 3) converge on the SAME
# daemon. The second caller (nx-mcp) just finds it already up and skips its
# own (env-stripped, non-functional) spawn.
# nexus-wauo1.15 (round 3): liveness is checked via the SAME RDR-149 registry
# lease ensure_aspect_worker_daemon() itself consults (registry.discover),
# never `pgrep` -- this image does not apt-get install procps (nexus-5qefg
# posture), so `pgrep`/`ps` are not on PATH at all and a pgrep-based check
# gives no signal whether it hard-fails or merely notes, which is why the RF-4
# post-teardown check below has printed "no live aspect-worker process found"
# on EVERY run, success or failure alike, since it was written (already
# flagged as a likely vacuity in this file's own `_service_pids()` comment
# above -- confirmed, not just suspected). A registry lease is a REAL signal:
# it is exactly what a live, current-version daemon publishes and exactly
# what nx-mcp's own later ensure_aspect_worker_daemon() call reads to decide
# whether to spawn at all.
NXENV_PY="/home/nexus/nxenv/bin/python3"
# RDR-219 P3b.3 (nexus-wauo1.40), GRANT MODE proof 2 (nx-mcp-spawned aspect
# worker): this pre-start must NOT run, so nx-mcp itself has to spawn the
# worker later under the dispatch grant. Non-vacuity: assert the registry
# lease is ABSENT right here, before the first store_put -- a document_aspects
# count alone would not distinguish "nx-mcp spawned it" from "the pre-started
# worker already did the work", so this check plus the post-teardown RF-4
# LIVE check below are what make the later document_aspects>0 assertion mean
# what proof 2 claims.
if [ "$GRANT_MODE" = 1 ]; then
  note "GRANT MODE (RDR-219 P3b.3): skipping the Phase 2 aspect-worker pre-start -- nx-mcp must spawn its OWN worker under the dispatch grant"
  worker_lease_before="$("$NXENV_PY" -c "
from nexus.config import nexus_config_dir
from nexus.daemon.aspect_worker_daemon import TIER
from nexus.daemon.service_registry import ServiceRegistry, ttl_for_tier
registry = ServiceRegistry(dir=nexus_config_dir(), tier=TIER, ttl=ttl_for_tier(TIER))
print('LIVE' if registry.discover('default') is not None else 'ABSENT')
")"
  if [ "$worker_lease_before" = "ABSENT" ]; then
    ok "no worker lease before the first store_put (pre-start genuinely skipped -- GRANT MODE proof 2 non-vacuity)"
  else
    bad "a worker lease is ALREADY live before the first store_put -- the pre-start was not skipped, so a later document_aspects>0 would be vacuous for proof 2"
  fi
fi
if [ "$GRANT_MODE" != 1 ]; then
  note "pre-starting the RDR-173 leased aspect-worker daemon (inherits CLAUDE_CODE_OAUTH_TOKEN from this shell)…"
  worker_status="$("$NXENV_PY" -c "
import time
from nexus.config import nexus_config_dir
from nexus.daemon.aspect_worker_daemon import TIER, ensure_aspect_worker_daemon
from nexus.daemon.service_registry import ServiceRegistry, ttl_for_tier
config_dir = nexus_config_dir()
ensure_aspect_worker_daemon(config_dir=config_dir, tenant='default')
registry = ServiceRegistry(dir=config_dir, tier=TIER, ttl=ttl_for_tier(TIER))
# 180 s: in a cold container the pre-started worker published its lease
# later than 15 s and then later than 60 s (2026-09-26, two runs), while the
# same registry read after the workload found it live and that worker did the
# extraction. Only a genuinely broken start waits the full time.
for _ in range(180):
    if registry.discover('default') is not None:
        print('LIVE')
        break
    time.sleep(1)
else:
    print('ABSENT')
")"
  # Informational, not a failure (2026-09-26): in this container the spawned
  # `nx daemon aspect-worker start` takes about three minutes to publish its
  # lease (pid assigned right after the service's, "started" logged ~3 min
  # later, three runs), so a bounded wait here reports a slow boot, not a
  # broken pre-start. The real proof is document_aspects > 0 below, which
  # passed in every run. The slow boot is tracked separately.
  if [ "$worker_status" = "LIVE" ]; then ok "leased aspect-worker daemon pre-started (registry lease confirmed live)"; else note "leased aspect-worker daemon lease not yet published after the wait (slow boot; extraction below is the proof)"; fi
fi

# 1. Auth smoke — proves the mounted oauth + linux claude work (biggest unknown).
authout="$(claude -p 'Reply with exactly the token AUTHOK and nothing else.' --dangerously-skip-permissions 2>&1)"
if printf '%s' "$authout" | grep -q "AUTHOK"; then ok "claude -p authenticated (mounted oauth works in-container)"
else bad "claude -p auth failed — cannot drive the MCP/extraction"; note "$(printf '%s' "$authout" | head -3 | tr '\n' ' ')"; say "ABORT (no claude auth)"; printf 'REHEARSAL FAILED\n'; exit 1; fi

# 2. MCP config for the nexus server. Default mode: a plain file-based config
#    (unchanged). GRANT MODE (RDR-219 P3b.3, nexus-wauo1.40): no file at all --
#    claude runs through claude_mcp_grant (sourced from the staged lib/), which
#    builds and pipes the config itself, with NX_HARNESS_CLAUDE_OAUTH_TOKEN in
#    the nexus server's OWN env block so nx-mcp's nested claude -p calls
#    (operator tools, the aspect worker it must now spawn itself) authenticate.
if [ "$GRANT_MODE" = 1 ]; then
  # shellcheck disable=SC1091
  source "$HOME/lib/claude_mcp_grant.sh"
  note "GRANT MODE: claude will run through claude_mcp_grant (nexus MCP server env carries NX_HARNESS_CLAUDE_OAUTH_TOKEN only)"
  if [ -z "${ANTHROPIC_API_KEY:-}" ]; then
    ok "ANTHROPIC_API_KEY absent in the container before the workload"
  else
    bad "ANTHROPIC_API_KEY is set in the container -- the dispatch-grant proofs must run on the harness automation token alone"
  fi
else
  cat > /home/nexus/mcp.json <<'MCPJSON'
{ "mcpServers": { "nexus": { "command": "nx-mcp", "args": [] } } }
MCPJSON
fi

# 3. Drive a MULTIVARIATE workload THROUGH the nexus MCP (store_put x4 + search +
#    nx_answer) so the post-store hook enqueues aspects and several tools fire.
#    GRANT MODE additionally drives operator_summarize and a tool-granting
#    nested dispatch (nx_enrich_beads) in the SAME prompt, exercising nx-mcp's
#    own nested claude -p under the dispatch grant (proofs 1 and 3).
MARK="fsmark$$"
prompt="You have the nexus MCP server; use ONLY its tools (names start mcp__nexus__). Do ALL of:
1. store_put four knowledge notes (collection 'knowledge'), ONE call each, unique titles.
   Doc (a) MUST be a PAPER-SHAPED research fragment (RDR-172 P2.2 / RDR-145 Gap-3:
   knowledge__ stays in-surface via shape-aware routing; a paper shape routes to
   scholarly-paper-v1 and yields a populated document_aspects row — the non-vacuous
   positive signal). Keep them verbatim:
   a) 'We propose a widget-assembly index. In this paper we present a method for mechanical-part retrieval, evaluated against the prior approach of Gear et al. (2021). $MARK widget paper fragment.'
   b) 'Sprockets mesh with chains to transfer torque. $MARK sprocket note.'
   c) 'Gadgets combine widgets and sprockets into devices. $MARK gadget note.'
   d) 'Retrieval ranks documents by semantic similarity. $MARK retrieval note.'
2. search 'widgets and sprockets' in the knowledge corpus.
3. nx_answer the question 'what are widgets and sprockets?'."
declare -a allowed_tools=(mcp__nexus__store_put mcp__nexus__search mcp__nexus__nx_answer)
if [ "$GRANT_MODE" = 1 ]; then
  prompt="$prompt
4. operator_summarize the text 'Widgets, sprockets and gadgets combine via retrieval-ranked assembly.'; put its returned summary text in your reply after the literal marker SUMMARY:.
5. nx_enrich_beads with bead_description 'Test bead: assemble a widget from a sprocket and a gadget.'; put a short excerpt of its returned enriched description in your reply after the literal marker ENRICHED:."
  allowed_tools+=(mcp__nexus__operator_summarize mcp__nexus__nx_enrich_beads)
fi
prompt="$prompt
End your reply with the literal token WORKLOADDONE."
if [ "$GRANT_MODE" = 1 ]; then
  note "driving multivariate MCP workload via claude_mcp_grant (store_put x4 + search + nx_answer + operator_summarize + nx_enrich_beads)…"
  wlout="$(claude_mcp_grant nx-mcp -- -p "$prompt" --dangerously-skip-permissions \
    --allowedTools "${allowed_tools[@]}" 2>&1)"
else
  note "driving multivariate MCP workload via claude -p (store_put x4 + search + nx_answer)…"
  wlout="$(claude -p "$prompt" --mcp-config /home/nexus/mcp.json --dangerously-skip-permissions \
    --allowedTools "${allowed_tools[@]}" 2>&1)"
fi
note "claude workload tail: $(printf '%s' "$wlout" | tail -3 | tr '\n' ' ' | cut -c1-280)"
printf '%s' "$wlout" | grep -q "WORKLOADDONE" && ok "MCP workload completed (claude drove the tools)" || bad "MCP workload did not finish cleanly"

# 3b. Did store_put REALLY execute? (disambiguates 'claude didn't call the tool /
#     MCP didn't connect' from 'tool ran but hook didn't enqueue'.)
sleep 3
if nx collection list 2>/dev/null | grep -qi "knowledge"; then ok "store_put materialized a knowledge collection (MCP tools really executed)"; STORED_OK=1
else bad "no knowledge collection — claude did NOT actually call store_put (MCP connect / allowedTools issue)"; note "$(nx collection list 2>&1 | head -3 | tr '\n' ' ')"; STORED_OK=0; fi

# 3c. nx_answer produced a grounded composed answer (from the workload).
printf '%s' "$wlout" | grep -qiE "widget|sprocket|gadget" && ok "nx_answer (MCP) returned a grounded composed answer" || note "nx_answer answer not evident in workload output"

# 3d. GRANT MODE proofs 1 and 3: operator_summarize and the tool-granting
#     nested dispatch (nx_enrich_beads) must have returned REAL results --
#     never an error or "Not logged in" -- proving nx-mcp's OWN nested
#     claude -p authenticated under the dispatch grant.
if [ "$GRANT_MODE" = 1 ]; then
  if printf '%s' "$wlout" | grep -q "SUMMARY:" && ! printf '%s' "$wlout" | grep -qi "not logged in"; then
    ok "operator_summarize (dispatch grant) returned a real reply"
  else
    bad "operator_summarize did not return a real reply under the dispatch grant"
  fi
  if printf '%s' "$wlout" | grep -q "ENRICHED:" && ! printf '%s' "$wlout" | grep -qi "not logged in"; then
    ok "nx_enrich_beads (tool-granting dispatch, nested nx-mcp) returned a real result under the dispatch grant"
  else
    bad "nx_enrich_beads did not return a real result under the dispatch grant"
  fi
fi

# 3e. GRANT MODE proof 4 (no leak): a REAL Bash-tool child must not inherit
#     either token name, and no process argv anywhere in the container may
#     match a token-shaped pattern while the session runs. The diagnostic
#     prints COUNTS ONLY (TOKEN RULE) -- never a value or a whole environment.
if [ "$GRANT_MODE" = 1 ]; then
  # Sample every process's argv concurrently with the claude call below
  # (not just a post-hoc snapshot after it exits): a background poller
  # records the WORST count observed across the whole call.
  ARGV_LEAK_FILE="$(mktemp)"
  printf '0' > "$ARGV_LEAK_FILE"
  ( while true; do
      n="$(ps -axww -o args 2>/dev/null | grep -c '[s]k-ant-o' || true)"
      cur="$(cat "$ARGV_LEAK_FILE" 2>/dev/null || echo 0)"
      if [ "${n:-0}" -gt "${cur:-0}" ] 2>/dev/null; then printf '%s' "${n:-0}" > "$ARGV_LEAK_FILE"; fi
      sleep 0.5
    done ) &
  ARGV_POLLER_PID=$!

  leak_prompt="You have a Bash tool. Run exactly this one command and nothing else:
env | grep -c NX_HARNESS_CLAUDE_OAUTH_TOKEN; env | grep -c CLAUDE_CODE_OAUTH_TOKEN
Reply with exactly two lines, using the ACTUAL numbers the command printed, nothing else:
HARNESS_COUNT=<n>
TOKEN_COUNT=<n>
Then end with the literal token LEAKCHECKDONE. Never print the command's own environment or any variable's value, only the two counts."
  leakout="$(claude_mcp_grant nx-mcp -- -p "$leak_prompt" --dangerously-skip-permissions --allowedTools Bash 2>&1)"

  kill "$ARGV_POLLER_PID" 2>/dev/null || true
  wait "$ARGV_POLLER_PID" 2>/dev/null || true
  argv_hits="$(cat "$ARGV_LEAK_FILE" 2>/dev/null || echo 0)"
  rm -f "$ARGV_LEAK_FILE"

  note "leak-check tail: $(printf '%s' "$leakout" | tail -3 | tr '\n' ' ' | cut -c1-200)"
  if printf '%s' "$leakout" | grep -q "LEAKCHECKDONE" \
     && printf '%s' "$leakout" | grep -qE 'HARNESS_COUNT=0\b' \
     && printf '%s' "$leakout" | grep -qE 'TOKEN_COUNT=0\b'; then
    ok "no leak: a real Bash-tool child's environment names neither token (HARNESS_COUNT=0, TOKEN_COUNT=0)"
  else
    bad "no-leak diagnostic did not confirm both counts are 0 (leak, or the diagnostic itself failed)"
  fi
  if [ "${argv_hits:-0}" -eq 0 ] 2>/dev/null; then
    ok "no process argv matched a token-shaped pattern while the session ran (ps -axww -o args | grep -c '[s]k-ant-o' = 0)"
  else
    bad "a process argv matched a token-shaped pattern while the session ran (${argv_hits} hit(s))"
  fi
fi

# 4-6. ASPECT PIPELINE IN SERVICE MODE — POSITIVE END-TO-END ASSERTION.
# RF-9 (RDR-172) corrects the prior stale comment here: store_put of a knowledge__
# note DOES enqueue aspects. store_put fires the post-document hook (mcp/core.py)
# and knowledge__* IS extractor-eligible — select_config → scholarly-paper-v1, then
# per-document shape routing (RDR-145 Gap-3 / nexus-kmbys): a PAPER-shaped doc (the
# workload's doc (a)) extracts via scholarly-paper-v1, prose via general-prose-v1.
# So store_put enqueues and the leased daemon drains it. Assert the END STATE
# (document_aspects > 0) as a HARD failure, not a soft note — guarded by the
# non-vacuity check that store_put actually landed the document (the knowledge-
# collection assertion above).
#
# RDR-173 LIFECYCLE (supersedes the pre-RDR-173 P2.1 caveat below): the worker is
# NO LONGER an in-nx-mcp daemon thread. In SERVICE mode the enqueue hook spawns a
# LEASED aspect-worker DAEMON, DETACHED via start_new_session (aspect_worker_daemon
# .py), so it OUTLIVES the claude -p / nx-mcp teardown and drains the shared PG
# queue independently (RF-4 — extraction no longer gated by the storing process's
# lifetime). This post-teardown poll therefore LEGITIMATELY extends the drain
# window: nx-mcp is already dead, yet the detached daemon keeps draining as long as
# the container is alive. Give it real time — real extraction calls `claude -p`
# per document (cold start, slow); 36s (the old in-process window) was far too
# short for the 4-doc workload.
#
# Non-vacuity is PRESERVED by extending the window: on pre-RDR-173 code the in-
# process worker dies with nx-mcp and NOTHING drains during this post-teardown poll
# no matter how long it runs → document_aspects stays 0. Only the detached daemon
# can move the needle here, so a green is the RF-4 proof, not a softened gate.
#
# Diagnostics first: prove the detached daemon actually outlived nx-mcp (RF-4) and
# surface its crash/daemon logs so a 0-row result distinguishes "daemon never
# spawned / crashed" (real bug) from "daemon draining, needs more time".
LOGS_DIR="$HOME/.config/nexus/logs"
note "leased-daemon liveness after nx-mcp teardown (RF-4 check):"
# nexus-wauo1.15 (round 3): registry.discover, not pgrep -- this image has no
# procps (nexus-5qefg posture), so pgrep gave no signal here either, in any
# run, success or failure alike. See the pre-start comment above for the full
# account.
rf4_status="$("$NXENV_PY" -c "
from nexus.config import nexus_config_dir
from nexus.daemon.aspect_worker_daemon import TIER
from nexus.daemon.service_registry import ServiceRegistry, ttl_for_tier
registry = ServiceRegistry(dir=nexus_config_dir(), tier=TIER, ttl=ttl_for_tier(TIER))
print('LIVE' if registry.discover('default') is not None else 'ABSENT')
")"
if [ "$rf4_status" = "LIVE" ]; then
  ok "leased aspect-worker daemon SURVIVED nx-mcp teardown (RF-4: extraction host is process-independent)"
else
  note "leased aspect-worker daemon registry lease not live post-teardown"
fi
for lg in aspect_worker_daemon.crash.log aspect_worker_daemon.log; do
  if [ -s "$LOGS_DIR/$lg" ]; then note "tail $lg:"; tail -25 "$LOGS_DIR/$lg" | sed 's/^/       /'; fi
done
# Extended drain window: up to ~10 min (150 x 4s), break the instant a row lands.
for _ in $(seq 1 150); do
  asp="$(q "select count(*) from nexus.document_aspects")"
  [ "${asp:-0}" -gt 0 ] 2>/dev/null && break
  sleep 4
done
enq="$(q "select count(*) from nexus.aspect_extraction_queue")"
pend="$(q "select count(*) from nexus.aspect_extraction_queue where status in ('pending','in_progress')")"
note "post-workload: aspect_queue total=${enq:-?} pending=${pend:-?}; document_aspects=${asp:-?}"
# RDR-172 P2.1 (nexus-hlkvj): enqueue-failure tripwire — the ingest E2E must
# complete with ZERO swallowed aspect-enqueue failures. The hook persists a
# hook_failures row on its best-effort swallow (the nexus-ov0sw silent-total-
# failure class); a non-zero count here means an enqueue silently failed.
# NOTE: this gate is only NON-VACUOUS if the workload above actually drives
# store_put through aspect_extraction_enqueue_hook in service mode. The workload
# now stores a paper-shaped knowledge doc (P2.2) so the path IS exercised; final
# liveness is confirmed by P2.5 (nexus-8zog5, post-fix --fullstack real run).
# Until P2.5 confirms, treat a green assert-zero as necessary-but-not-sufficient.
enqfail="$(q "select count(*) from nexus.hook_failures where hook_name='aspect_extraction_enqueue_hook'")"
if [ "${enqfail:-0}" -eq 0 ] 2>/dev/null; then ok "enqueue-failure tripwire: 0 swallowed aspect-enqueue failures"
else bad "enqueue-failure tripwire FIRED: ${enqfail} swallowed aspect_extraction_enqueue_hook failure(s) — silent-loss class recurred (RF-7)"; fi
# RDR-172 P2.2 (nexus-jr84c): HARD positive assertion. document_aspects MUST be
# populated when store_put landed a (paper-shaped) knowledge doc — the END-TO-END
# proof that store_put → enqueue → worker → document_aspects actually completes in
# service mode (closes Gap 2, half). Non-vacuity guard: only a hard FAIL when
# store_put demonstrably landed (STORED_OK); if it never landed, that miss is
# already a `bad` above and this would be vacuous.
if [ "${asp:-0}" -gt 0 ] 2>/dev/null; then
  ok "SERVICE-MODE aspect pipeline works END-TO-END: store_put → enqueue → worker → document_aspects (${asp} rows, real extraction)"
elif [ "${STORED_OK:-0}" -eq 1 ] 2>/dev/null; then
  if [ "${enq:-0}" -gt 0 ] 2>/dev/null; then
    bad "SERVICE-MODE aspect pipeline BROKEN: enqueued=${enq} (pending=${pend:-?}) but document_aspects=0 after the extended post-teardown drain window — the leased daemon did not drain the queue (see liveness/log diagnostics above: never spawned, crashed, or extraction stalled; RDR-173 RF-4 / Approach 5 / P2.2)"
  else
    bad "SERVICE-MODE aspect pipeline BROKEN: store_put landed but enqueued=0 AND document_aspects=0 — hook did not enqueue (the silent-loss class; Approach 5 / P2.2)"
  fi
else
  note "document_aspects=0 but store_put did not land (already failed above) — positive assertion vacuous this run"
fi

# 8. Service healthy after the full-stack run.
nx daemon service status 2>&1 | grep -qiE "health.*ok|healthy|serving|running" && ok "service healthy after full-stack run" || bad "service unhealthy"

say "RESULT"
if [ "$FAILS" -eq 0 ]; then printf '\033[32mFULL-STACK SHAKEOUT PASSED\033[0m — full topology: service + claude auth + MCP tools (store_put/search/nx_answer) end-to-end vs the 6.0.0 service (aspect-pipeline drain ASSERTED: document_aspects>0 + zero enqueue-failure tripwire; real-container liveness confirmed by P2.5 nexus-8zog5)\n'; exit 0
else printf '\033[31mFULL-STACK SHAKEOUT FAILED — %d check(s)\033[0m\n' "$FAILS"; exit 1; fi
