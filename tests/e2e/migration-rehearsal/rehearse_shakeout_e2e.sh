#!/usr/bin/env bash
# Daily-driver install-to-shakeout journey — runs INSIDE the container.
#
# Answers: "can someone install the local version, take it through a
# reasonable shakeout across nx and conexus, and be reasonably assured it
# won't completely suck?" A SIBLING of rehearse_fullstack.sh (that journey's
# own narrow aspect-pipeline drain is untouched) — reuses the SAME image
# (Dockerfile.fullstack: native service + conexus wheel + linux `claude`,
# NO system PostgreSQL, nexus-5qefg) but drives a different, broader path:
# install -> real-corpus code ingest -> md/pdf ingest -> search/query
# retrieval -> T2/T1 round-trip -> doctor -> MCP tool surface, with a live
# peak-RSS assertion on the engine during the code-ingest step.
#
# Step 8 exists because of a REAL production incident (nexus-33hpq,
# conexus 7.5.0 cut day, 2026-08-09): `nx index repo` against a LOCAL
# (onnx-local/bge-768) engine drove the native nexus-service to 77.4 GB
# RSS, ~15 cores pegged, until it stopped answering /livez and its
# supervisor killed it as wedged. Reproduced 3/3. Root cause: the
# per-flush upsert-chunk cap was 300 in local mode, a number derived from
# the MANAGED control-plane's 30s request timeout — irrelevant to a local
# ONNX embedder, which is bound by MEMORY, not network time (attention is
# O(batch * heads * seq^2); Bge768Embedder.embedBatch runs ONE forward pass
# over a rectangular [batch, seq<=512] tensor, no sub-batching). Fixed by a
# memory-derived cap of 16 (src/nexus/db/http_vector_client.py
# _ONNX_LOCAL_UPSERT_CHUNK_CAP), which measured a 3.03 GB peak on the
# release fixture. NOTHING in the entire test suite measured memory before
# this — the blowup was invisible to every assertion in every gate; it was
# caught only because a service died and a human watched `ps`. Step 8
# exists so a future cap raise cannot silently reintroduce it.
#
# RSS BUDGET ARITHMETIC (nexus-33hpq measurements, same 36-file shakedown
# fixture, same box, cap applied uniformly to every collection prefix under
# onnx-local):
#     cap=300 (pre-fix)  -> 77.4 GB peak RSS, ~15 cores pegged, wedged/killed
#     cap=16  (this fix) -> 3.03 GB peak RSS, step cleared, gate green
# The scaling between those two points is markedly SUPER-linear (a 5.75x
# batch increase, ~16->92 chunks actually reached, produced a ~25x RSS
# increase) — see per_collection_chunk_cap()'s own docstring for why no
# closed-form model is offered. That super-linearity is exactly why a
# GENEROUS-looking budget still catches a regression fast: any cap creep
# that lets batches grow much past 16 blows past a several-GB budget almost
# immediately, it does not degrade gracefully.
#
# BUDGET, and why it is 3 GB rather than 8 (tightened 2026-08-09 after the
# substantive-critic review). The first cut used 8 GB, reasoning only about
# the 77.4 GB catastrophe. The critic's objection is correct and decisive: the
# cap-bind check and the RSS check test DIFFERENT invariants, so a regression
# that KEEPS cap=16 but grows per-batch memory would land anywhere in a
# ~1.5-8 GB band and pass BOTH checks silently. A budget that only catches
# catastrophe is a catastrophe detector, not a memory gate.
#
# Observed in this container across two real runs: 1.21 GB and 1.44 GB peak.
# Budget = 3 GB, i.e. ~2x the highest observation. That absorbs GC/allocator
# overshoot and normal run-to-run variance (the two runs differ by ~19%)
# without flapping, while still failing a ~2x memory regression that the old
# 8 GB budget would have slept through. It remains far below both the 3.03 GB
# release-fixture figure's pathological cousin and the 77.4 GB shape.
#
# If this trips on a legitimately heavier corpus, RAISE IT DELIBERATELY and
# record the new measurement here — do not widen it to make a red run green.
# Override for a one-off via NX_SHAKEOUT_E2E_RSS_BUDGET_GB.
#
# CORPUS SIZING (Hal correction, 2026-08-09 — supersedes an earlier overshoot
# in this journey's own design brief): the cap is 16 CHUNKS, not files or
# lines. The release shakedown fixture (36 small files, 92 chunks total)
# lands ONE chunk short of binding it (largest flush observed: 15) — see
# nexus-97dp4. This corpus is deliberately SMALL: one file engineered to
# chunk into >16 pieces on its own (verified empirically below, not
# guessed), which DETERMINISTICALLY forces ChunkBatcher to reject it as
# oversize-for-one-batch and route it through the legacy per-file paged
# upload (HttpVectorClient.upsert_chunks) — whose FIRST page is
# mathematically exactly `min(total, cap)` chunks. Any file chunking into
# more than `cap` pieces binds the cap on page 1, regardless of exact
# chunk-boundary luck; no multi-file boundary-alignment gamble required.
# Measured against THIS tree's real chunker (llama-index CodeSplitter,
# chunk_lines=150/15% overlap — src/nexus/chunker.py) on a synthetic
# function-filler python file: 682 lines / 40 functions -> 21 chunks, a
# comfortable 5-chunk margin over the 16-chunk cap. A second, small file
# (67 lines / 5 functions -> 3 chunks) exercises the NORMAL in-cap
# ChunkBatcher combined-write path for contrast, and a short markdown note
# exercises the docs__ prefix. Total: 3 files, ~24 chunks, generated in
# well under a second, indexed in low tens of seconds on CPU bge-768 —
# "fast and provably non-vacuous", not "large".
set -uo pipefail
FAILS=0
say()  { printf '\n\033[1m== %s ==\033[0m\n' "$*"; }
ok()   { printf '  \033[32mPASS\033[0m %s\n' "$*"; }
bad()  { printf '  \033[31mFAIL\033[0m %s\n' "$*"; FAILS=$((FAILS+1)); }
note() { printf '       %s\n' "$*"; }

NXENV_PY="/home/nexus/nxenv/bin/python3"
MARK="skoe2e$$"

# ── /proc-only process introspection (nexus-5qefg posture: this image does
#    NOT apt-get install procps, so `ps`/`pgrep` are NOT assumed present —
#    rehearse_fullstack.sh's own `pgrep -af aspect-worker` liveness check is
#    UNVERIFIED against that assumption, a possible pre-existing vacuity in
#    that journey, out of scope here but worth a T2 note). /proc is always
#    present on a real Linux kernel regardless of installed userspace tools.
_service_pids() {
  local p comm
  for p in /proc/[0-9]*; do
    [ -r "$p/comm" ] || continue
    comm="$(cat "$p/comm" 2>/dev/null)"
    [ "$comm" = "nexus-service" ] && basename "$p"
  done
}
_service_rss_kb_total() {
  local pid total=0 kb
  for pid in $(_service_pids); do
    kb="$(awk '/^VmRSS:/{print $2}' "/proc/$pid/status" 2>/dev/null)"
    [ -n "${kb:-}" ] && total=$((total + kb))
  done
  [ "$total" -gt 0 ] 2>/dev/null && echo "$total" || echo ""
}
_kb_to_gb_str() { # bash-only fixed-point, no awk/bc dependency assumed
  local kb="$1" whole frac
  whole=$(( kb / 1024 / 1024 ))
  frac=$(( (kb * 100 / 1024 / 1024) % 100 ))
  printf '%d.%02d' "$whole" "$frac"
}
_largest_flush_in_log() {
  # Max over BOTH the normal ChunkBatcher combined-write flush event
  # (chunk_flush_complete, chunks=N — chunk_batcher.py) and the legacy
  # oversize-file paged-upload event (http_vector_upsert_chunks_request,
  # count=N — http_vector_client.py upsert_chunks paging). Pre-filtering
  # each grep by the event name before extracting the number avoids the
  # ` count=` pattern accidentally matching the OTHER event's
  # `distinct_chash_count=` field (no leading space precedes "count=" in
  # that field name, but the safety margin costs nothing).
  local log="$1" a b
  a="$(grep -F 'chunk_flush_complete' "$log" 2>/dev/null | grep -oE 'chunks=[0-9]+' | cut -d= -f2 | sort -n | tail -1)"
  b="$(grep -F 'http_vector_upsert_chunks_request' "$log" 2>/dev/null | grep -oE ' count=[0-9]+' | tr -d ' ' | cut -d= -f2 | sort -n | tail -1)"
  a="${a:-0}"; b="${b:-0}"
  if [ "$a" -ge "$b" ] 2>/dev/null; then echo "$a"; else echo "$b"; fi
}

# ── Step 1 — install + provision + serve ──────────────────────────────────
say "Step 1 — install + provision + serve"
SVC_NATIVE_DIR="/opt/nexus-service-native"; SVC_WELL_KNOWN_DIR="$HOME/.config/nexus/service"
nx --version >/dev/null 2>&1 && ok "nx installed ($(nx --version 2>&1))" || bad "nx --version failed"
claude --version >/dev/null 2>&1 && ok "claude CLI installed ($(claude --version 2>&1 | head -1))" || bad "claude CLI missing"
command -v initdb >/dev/null 2>&1 && bad "system PostgreSQL present — bare-machine posture violated (nexus-5qefg)" || ok "no system PostgreSQL (bundle must provide it)"
test -x "$SVC_NATIVE_DIR/nexus-service" && ok "native service binary present" || bad "native binary missing"
mkdir -p "$SVC_WELL_KNOWN_DIR" && cp "$SVC_NATIVE_DIR"/* "$SVC_WELL_KNOWN_DIR/" && chmod +x "$SVC_WELL_KNOWN_DIR/nexus-service" \
  && ok "native binary positioned" || bad "could not position native binary"

# Provenance sidecar for the hand-staged binary.
#
# WHY THIS EXISTS: detect_engine_convergence() (upgrade_finish.py) reads the
# provenance sidecar written by binary_lifecycle's install path — deliberately
# a DISK RECORD, not a live /version probe, because a wedged engine may never
# answer /version. This harness bypasses that install path entirely: it copies
# a locally-BUILT linux binary into the well-known dir rather than downloading
# a published asset. With no sidecar, `nx doctor` correctly reports
# "installed vunknown" and its convergence check degrades to a warning that
# says nothing.
#
# The wrong fix is to allowlist that warning — that suppresses a real check for
# every future run. Writing a TRUTHFUL sidecar instead makes the convergence
# check a live assertion in this journey.
#
# Every field below is true. sha256 is computed from the staged bytes, not
# copied from a manifest. There is deliberately NO source_url and NO published
# `asset`: nothing was downloaded, and inventing those would fake a verified
# provenance this binary does not have. installed_by names the harness so the
# record is never mistaken for a real install.
SIDECAR="$SVC_WELL_KNOWN_DIR/nexus-service.meta.json"
ENGINE_V="$("$NXENV_PY" -c 'from nexus.engine_version import REQUIRED_ENGINE_VERSION as v; print(".".join(str(p) for p in v))' 2>/dev/null)"
if [ -n "$ENGINE_V" ]; then
  BIN_SHA="$(sha256sum "$SVC_WELL_KNOWN_DIR/nexus-service" 2>/dev/null | cut -d' ' -f1)"
  cat > "$SIDECAR" <<EOF
{
  "version": "$ENGINE_V",
  "tag": "engine-service-v$ENGINE_V",
  "sha256": "$BIN_SHA",
  "installed_at": "$(date -u +%Y-%m-%dT%H:%M:%S+00:00)",
  "installed_by": "rehearse_shakeout_e2e.sh (locally-built binary staged by the harness; NOT a published-asset install)"
}
EOF
  ok "provenance sidecar written (v$ENGINE_V) — doctor's convergence check can now assert instead of reporting 'unknown'"
else
  bad "could not resolve REQUIRED_ENGINE_VERSION from the installed package — convergence check would be UNMEASURED"
fi
export NX_SERVICE_MAX_HEAP="${NX_SERVICE_MAX_HEAP:-1g}"
note "nx init --service (provision PG17+pgvector+bge-768)…"
if nx init --service --embedder bge-768 --no-autostart 2>&1 | sed 's/^/       /'; then ok "nx init --service"; else bad "nx init --service failed"; say "ABORT"; exit 1; fi
export NX_STORAGE_BACKEND=service
# shellcheck disable=SC1091
set -a; . /home/nexus/.config/nexus/pg_credentials; set +a
unset NX_SERVICE_URL NX_SERVICE_PORT NX_SERVICE_HOST 2>/dev/null || true
healthy=0
for i in $(seq 1 30); do nx daemon service status 2>&1 | grep -qiE "health.*ok|healthy|serving|running" && { healthy=1; break; }; sleep 2; done
[ "$healthy" = 1 ] && ok "service healthy" || { bad "service never healthy"; say "ABORT"; exit 1; }
[ -n "${NX_SERVICE_TOKEN:-}" ] && ok "NX_SERVICE_TOKEN present" || bad "NX_SERVICE_TOKEN absent"

# VERIFY the sidecar against the binary that is actually serving.
#
# The sidecar written above states a version taken from REQUIRED_ENGINE_VERSION
# — that is an ASSERTION about whatever bytes happen to sit at
# service/target/nexus-service, not a measurement of them. The substantive
# critic flagged this (2026-08-09) as the same shape as nexus-hdumg, the P1
# fixed EARLIER THE SAME DAY: convergence declared on a claim rather than on
# verified state. If a developer builds a different engine locally and runs
# this journey, the sidecar would quietly lie and doctor would "converge"
# against a version that is not running.
#
# So: now that the service is up, ask the RUNNING binary what it is (the same
# /version-backed field rehearse_acquire.sh asserts on) and require it to match
# what we wrote. An unreachable/absent field is UNMEASURED, not a pass — but it
# does not abort the journey, because the rest of the shakeout is still worth
# running on an engine whose version we merely could not read.
# BOUNDED RETRY. The health-wait loop above greps status TEXT for
# healthy/serving, which can go true before /version answers: daemon.py:1308
# only populates service_release_version when fetch_service_version() (a 3s
# probe) succeeds AND health != unreachable. The maiden run of this check
# reported UNMEASURED for exactly that reason — correctly, but spuriously.
# Retry for ~30s before believing it. A genuinely absent field still ends as
# UNMEASURED; this only removes the startup race, it does not soften the check.
LIVE_RV=""
for _ in $(seq 1 10); do
  LIVE_RV="$(nx daemon service status --json 2>/dev/null \
    | "$NXENV_PY" -c 'import sys,json;print(json.load(sys.stdin).get("service_release_version") or "")' 2>/dev/null)"
  [ -n "$LIVE_RV" ] && break
  sleep 3
done
if [ -z "${ENGINE_V:-}" ]; then
  note "sidecar version unknown — skipping the live cross-check (already failed above)"
elif [ -z "$LIVE_RV" ]; then
  bad "UNMEASURED: could not read service_release_version from the running engine — the provenance sidecar (v$ENGINE_V) is UNVERIFIED against the binary actually serving"
elif [ "$LIVE_RV" = "$ENGINE_V" ]; then
  ok "provenance sidecar VERIFIED against the running binary (/version release_version=$LIVE_RV == sidecar v$ENGINE_V)"
else
  bad "provenance sidecar LIES: it claims v$ENGINE_V but the running binary reports release_version=$LIVE_RV — doctor's convergence verdict would be based on a false record"
fi

LADDER_OUT="$(nx doctor 2>&1)"
if printf '%s' "$LADDER_OUT" | grep -qi "no pending rung"; then
  ok "upgrade ladder converged at init ($(printf '%s' "$LADDER_OUT" | grep -io 'no pending rung[a-z ]*([0-9]* registered)' | head -1))"
else
  bad "upgrade ladder NOT converged on a virgin install"
  printf '%s' "$LADDER_OUT" | grep -i "ladder" | sed 's/^/       /'
fi

# ── Step 2 — nx index repo on a REAL corpus (the cap-bind non-vacuity gate) ──
say "Step 2 — nx index repo on a real corpus + live peak-RSS sampling (Step 8 data collected here)"

CORPUS_DIR="/tmp/shakeout-e2e-corpus-$$"
mkdir -p "$CORPUS_DIR"

note "generating a deterministic, self-contained corpus (no network, no fixtures baked into the image)…"
"$NXENV_PY" - "$CORPUS_DIR" "$MARK" <<'PYEOF'
import sys
from pathlib import Path

corpus_dir, mark = Path(sys.argv[1]), sys.argv[2]


def make_func(i: int, nlines: int) -> str:
    body = "\n".join(
        f"    v{j} = {j} * {i} + {j % 7}  # filler line {j} of func {i}"
        for j in range(nlines)
    )
    return (
        f"def widget_function_{i:04d}(x: int, y: int = {i}) -> int:\n"
        f'    """Deterministic shakeout-e2e filler function {i}."""\n'
        f"{body}\n"
        f"    return v0 + v{nlines - 1} + x + y\n"
    )


def make_file(nfuncs: int, nlines: int) -> str:
    parts = ["# Generated shakeout-e2e fixture — deterministic filler content.\n"]
    parts += [make_func(i, nlines) for i in range(nfuncs)]
    return "\n\n".join(parts) + "\n"


# 40 functions x 12 filler lines -> 682 lines -> 21 chunks measured against
# this tree's real chunker (CodeSplitter, chunk_lines=150/15%), a 5-chunk
# margin over the onnx-local cap (16) — deliberately oversize-for-one-batch
# so ChunkBatcher.add() rejects it and routes it through the legacy
# per-file paged upload, whose first page is exactly min(total, cap)
# chunks (see this script's header comment for the full argument).
(corpus_dir / "big_filler.py").write_text(make_file(40, 12))

# 5 functions x 8 filler lines -> 67 lines -> 3 chunks, safely IN-cap: the
# normal ChunkBatcher combined-write flush path, for contrast with the
# oversize file above. Carries the sentinel for code__ retrieval.
small_body = make_file(5, 8)
small_body += (
    f'\n\ndef sentinel_widget_sprocket_{mark}() -> str:\n'
    f'    """Shakeout-e2e retrieval sentinel {mark} — widgets and sprockets."""\n'
    f'    return "{mark}"\n'
)
(corpus_dir / "small_sentinel.py").write_text(small_body)

(corpus_dir / "notes.md").write_text(
    f"# Shakeout-e2e corpus notes {mark}\n\n"
    f"This is a small, deterministic prose fixture ({mark}) describing widgets "
    "and sprockets for the docs corpus. It exists purely to give the docs__ "
    "prefix at least one real chunk during a local-mode shakeout of `nx index "
    "repo`, alongside the code__ chunks from the two python files in this "
    "same fixture repository.\n"
)
PYEOF

FILE_COUNT="$(find "$CORPUS_DIR" -type f | wc -l | tr -d ' ')"
TOTAL_BYTES="$(du -sb "$CORPUS_DIR" 2>/dev/null | cut -f1)"
note "corpus shape (up front): ${FILE_COUNT} files, ${TOTAL_BYTES:-?} bytes total"
note "predicted chunks: big_filler.py ~21 (oversize, forces the cap-bind page), small_sentinel.py ~3, notes.md ~1 — measured against THIS tree's real chunker, confirmed at authoring time, not asserted as-is at runtime (actuals below are what's graded)"

( cd "$CORPUS_DIR" && git init -q && git add -A \
    && git -c user.email=shakeout@nexus.local -c user.name=shakeout-e2e commit -qm seed ) \
  >/tmp/shakeout-e2e-git.log 2>&1 \
  && ok "corpus fixture repo committed" || { bad "corpus git init/commit failed"; cat /tmp/shakeout-e2e-git.log | sed 's/^/       /'; }

CONFIGURED_CAP="$("$NXENV_PY" -c 'from nexus.db.http_vector_client import _ONNX_LOCAL_UPSERT_CHUNK_CAP as c; print(c)' 2>/dev/null)"
if [ -n "${CONFIGURED_CAP:-}" ]; then
  note "configured onnx-local per-flush chunk cap (nexus-33hpq): ${CONFIGURED_CAP}"
else
  note "could not read _ONNX_LOCAL_UPSERT_CHUNK_CAP from the installed package — the cap-bind assertion below will report UNMEASURED"
fi

RSS_BUDGET_GB="${NX_SHAKEOUT_E2E_RSS_BUDGET_GB:-3}"
RSS_BUDGET_KB=$(( RSS_BUDGET_GB * 1024 * 1024 ))
note "peak-RSS budget for this step: ${RSS_BUDGET_GB} GB (see this script's header comment for the full arithmetic; override via NX_SHAKEOUT_E2E_RSS_BUDGET_GB)"

INDEX_TIMEOUT_S="${NX_SHAKEOUT_E2E_INDEX_TIMEOUT_S:-1800}"
note "index hard deadline: ${INDEX_TIMEOUT_S}s (override via NX_SHAKEOUT_E2E_INDEX_TIMEOUT_S)"

MARKER_FILE="/tmp/.shakeout-e2e-log-marker-$$"
touch "$MARKER_FILE"
INDEX_STDOUT="/tmp/shakeout-e2e-index-stdout-$$.log"

say "indexing (nx index repo) — live progress below, printed at least every ~12s"
nx index repo "$CORPUS_DIR" >"$INDEX_STDOUT" 2>&1 &
INDEX_PID=$!

PEAK_RSS_KB=0
RSS_SAMPLES=0
RUN_LOG=""
T0="$(date +%s)"
LAST_PROGRESS="$T0"
while true; do
  if [ -z "$RUN_LOG" ]; then
    RUN_LOG="$(find "$HOME/.config/nexus/logs" -maxdepth 1 -name 'index-*.log' -newer "$MARKER_FILE" 2>/dev/null | head -1)"
  fi
  RSS_NOW="$(_service_rss_kb_total)"
  if [ -n "$RSS_NOW" ]; then
    RSS_SAMPLES=$((RSS_SAMPLES + 1))
    [ "$RSS_NOW" -gt "$PEAK_RSS_KB" ] && PEAK_RSS_KB="$RSS_NOW"
  fi
  NOW="$(date +%s)"
  if [ $((NOW - LAST_PROGRESS)) -ge 12 ]; then
    LAST_PROGRESS="$NOW"
    FLUSHES_SO_FAR=0
    LARGEST_SO_FAR=0
    if [ -n "$RUN_LOG" ] && [ -r "$RUN_LOG" ]; then
      FLUSHES_SO_FAR="$(grep -cF 'chunk_flush_complete' "$RUN_LOG" 2>/dev/null || echo 0)"
      LARGEST_SO_FAR="$(_largest_flush_in_log "$RUN_LOG")"
    fi
    PEAK_SO_FAR_STR="$(_kb_to_gb_str "$PEAK_RSS_KB")"
    LAST_LINE="$(tail -1 "$INDEX_STDOUT" 2>/dev/null | tr -d '\r')"
    note "…still indexing ($((NOW - T0))s elapsed): flushes=${FLUSHES_SO_FAR} largest_flush_or_page=${LARGEST_SO_FAR} peak_rss=${PEAK_SO_FAR_STR}GB rss_samples=${RSS_SAMPLES} | last: ${LAST_LINE:-<no output yet>}"
  fi
  if ! kill -0 "$INDEX_PID" 2>/dev/null; then break; fi
  # HARD DEADLINE. Without this the loop waits forever on the index process —
  # and it would do so in EXACTLY the scenario this journey exists to detect
  # (nexus-33hpq: engine at 77 GB, /livez unanswerable, indexing client stuck
  # retrying for 16+ minutes). A wedge detector that wedges reports nothing;
  # it just hangs CI until something outside the script intervenes. Code-review
  # finding, 2026-08-09: Step 3 already had `timeout 240` on its pdf run while
  # this, the longest and riskiest step, had none.
  #
  # 1800s is ~27x the observed 66s happy path — generous enough that a merely
  # slow box or a cold model cache never trips it, short enough that a genuine
  # wedge fails the run in bounded time with the diagnostics below intact.
  if [ $((NOW - T0)) -ge "$INDEX_TIMEOUT_S" ]; then
    kill -9 "$INDEX_PID" 2>/dev/null
    wait "$INDEX_PID" 2>/dev/null
    INDEX_TIMED_OUT=1
    break
  fi
  sleep 3
done
if [ "${INDEX_TIMED_OUT:-0}" = 1 ]; then
  INDEX_RC=124
else
  wait "$INDEX_PID"; INDEX_RC=$?
fi
ELAPSED=$(( $(date +%s) - T0 ))

if [ "${INDEX_TIMED_OUT:-0}" = 1 ]; then
  bad "nx index repo EXCEEDED the ${INDEX_TIMEOUT_S}s deadline (killed at ${ELAPSED}s) — this is the nexus-33hpq wedge shape: peak RSS reached $(_kb_to_gb_str "$PEAK_RSS_KB")GB over ${RSS_SAMPLES} samples before the deadline"
  tail -30 "$INDEX_STDOUT" | sed 's/^/       /'
elif [ "$INDEX_RC" = 0 ]; then ok "nx index repo exited 0 (${ELAPSED}s wall)"
else bad "nx index repo exited ${INDEX_RC}"; tail -30 "$INDEX_STDOUT" | sed 's/^/       /'; fi

FILES_INDEXED="$(grep -cE '^\s*\[[0-9]+/[0-9]+\]' "$INDEX_STDOUT" 2>/dev/null || echo 0)"
[ "${FILES_INDEXED:-0}" -ge 3 ] 2>/dev/null && ok "on_file progress reported ${FILES_INDEXED} files (>= 3 expected)" \
  || bad "on_file progress reported only ${FILES_INDEXED:-0} files — expected >= 3"

# Catalog registration, not just T3 chunks (fresh-install-mvv precedent —
# nexus-e9ru2 class: T3-only assertions miss a broken catalog write).
if nx catalog list 2>/dev/null | grep -q "$MARK\|big_filler\|small_sentinel\|notes"; then
  ok "corpus files registered in the engine catalog"
else
  note "nx catalog list did not obviously show the corpus by name — cross-checked via search retrieval in Step 4 instead (catalog list output format is not filename-grep-friendly by design)"
fi

if [ -n "$RUN_LOG" ]; then
  note "run log: $RUN_LOG ($(wc -l < "$RUN_LOG" 2>/dev/null || echo '?') lines)"
else
  note "never located an index-*.log run log under \$HOME/.config/nexus/logs — the cap-bind assertion below will report UNMEASURED"
fi
LARGEST_FLUSH="0"
[ -n "$RUN_LOG" ] && [ -r "$RUN_LOG" ] && LARGEST_FLUSH="$(_largest_flush_in_log "$RUN_LOG")"

say "Step 2 non-vacuity — the cap must actually BIND, not just be configured"
if [ -z "${CONFIGURED_CAP:-}" ] || [ -z "$RUN_LOG" ] || [ ! -r "$RUN_LOG" ]; then
  bad "UNMEASURED: could not read the configured cap and/or the run log — this run proves NOTHING about the cap binding (nexus-97dp4 class); fix the probe before trusting a future green here"
elif [ "$LARGEST_FLUSH" -ge "$CONFIGURED_CAP" ] 2>/dev/null; then
  ok "cap-bind non-vacuity: largest observed flush/page = ${LARGEST_FLUSH} chunks, reached the configured cap (${CONFIGURED_CAP}) — nexus-33hpq's cap genuinely bound this run"
else
  bad "cap NEVER BOUND: largest observed flush/page = ${LARGEST_FLUSH} chunks, configured cap is ${CONFIGURED_CAP} — this run is UNMEASURED for cap-regression purposes (the exact nexus-97dp4 class: a corpus too small to reach the cap grades a no-op as a pass). Do not treat this as a clean result; re-check the corpus generator above."
fi

# ── Step 8 (asserted here — data collected during Step 2's live sampler) ────
say "Step 8 — peak engine RSS during code ingest (nexus-33hpq regression gate)"
if [ "$RSS_SAMPLES" -le 0 ] 2>/dev/null; then
  bad "RSS sampler took ZERO samples — probe failure (could not find a 'nexus-service' /proc entry), not a clean result; the peak-RSS budget below is UNMEASURED"
elif [ "$PEAK_RSS_KB" -le 0 ] 2>/dev/null; then
  bad "RSS sampler ran ${RSS_SAMPLES} time(s) but peak stayed 0 — cannot trust the PID lookup; UNMEASURED"
else
  PEAK_STR="$(_kb_to_gb_str "$PEAK_RSS_KB")"
  if [ "$PEAK_RSS_KB" -le "$RSS_BUDGET_KB" ]; then
    ok "peak engine RSS ${PEAK_STR}GB <= budget ${RSS_BUDGET_GB}GB (${RSS_SAMPLES} samples; nexus-33hpq measured 3.03GB at cap=16 vs 77.4GB at the pre-fix cap=300)"
  else
    bad "peak engine RSS ${PEAK_STR}GB EXCEEDED budget ${RSS_BUDGET_GB}GB (${RSS_SAMPLES} samples) — possible cap regression toward the pre-fix 77.4GB shape"
  fi
fi

# ── Step 3 — nx index md + nx index pdf (the other two ingest routes) ──────
say "Step 3 — nx index md + nx index pdf"

MD_PATH="/tmp/shakeout-e2e-note-$$.md"
printf '# Shakeout-e2e standalone note %s\n\nA short markdown note for the `nx index md` route, distinct from the repo corpus in Step 2. Mentions widgets and sprockets for retrieval.\n' "$MARK" > "$MD_PATH"
if nx index md "$MD_PATH" >/tmp/shakeout-e2e-md.log 2>&1; then
  ok "nx index md succeeded"
else
  bad "nx index md failed"; tail -20 /tmp/shakeout-e2e-md.log | sed 's/^/       /'
fi

# No PDF fixture is baked into this image (Dockerfile.fullstack is
# reused UNCHANGED except for the one COPY line for this script — see the
# run.sh/Dockerfile diff). Generate a minimal, spec-valid, single-page PDF
# in pure Python (no reportlab/weasyprint dependency) as a best-effort real
# attempt; a genuine extraction failure (missing model weights, no network
# for a first-use Docling download, etc.) is reported LOUD and SKIPPED,
# never faked as a pass.
PDF_PATH="/tmp/shakeout-e2e-doc-$$.pdf"
if "$NXENV_PY" - "$PDF_PATH" "$MARK" <<'PYEOF'
import sys

path, mark = sys.argv[1], sys.argv[2]


def build_minimal_pdf(text: str) -> bytes:
    objects = [
        b"<< /Type /Catalog /Pages 2 0 R >>",
        b"<< /Type /Pages /Kids [3 0 R] /Count 1 >>",
        b"<< /Type /Page /Parent 2 0 R /Resources << /Font << /F1 4 0 R >> >> "
        b"/MediaBox [0 0 400 200] /Contents 5 0 R >>",
        b"<< /Type /Font /Subtype /Type1 /BaseFont /Helvetica >>",
    ]
    stream = f"BT /F1 16 Tf 20 100 Td ({text}) Tj ET".encode("latin-1")
    objects.append(
        b"<< /Length " + str(len(stream)).encode() + b" >>\nstream\n"
        + stream + b"\nendstream"
    )
    out = bytearray(b"%PDF-1.4\n")
    offsets = [0]
    for i, body in enumerate(objects, start=1):
        offsets.append(len(out))
        out += f"{i} 0 obj\n".encode() + body + b"\nendobj\n"
    xref_offset = len(out)
    n = len(objects) + 1
    out += f"xref\n0 {n}\n".encode() + b"0000000000 65535 f \n"
    for off in offsets[1:]:
        out += f"{off:010d} 00000 n \n".encode()
    out += (
        b"trailer\n" + f"<< /Size {n} /Root 1 0 R >>\n".encode()
        + b"startxref\n" + f"{xref_offset}\n".encode() + b"%%EOF"
    )
    return bytes(out)


with open(path, "wb") as f:
    f.write(build_minimal_pdf(f"shakeout-e2e pdf sentinel {mark}"))
PYEOF
then
  note "generated a minimal synthetic single-page PDF ($(wc -c < "$PDF_PATH") bytes) — attempting nx index pdf (bounded to 240s; first-use extractor model download, if any, can be slow)"
  if timeout 240 nx index pdf "$PDF_PATH" >/tmp/shakeout-e2e-pdf.log 2>&1; then
    ok "nx index pdf succeeded on the synthetic fixture"
    PDF_INDEXED=1
  else
    note "SKIPPED (loud, not a pass): nx index pdf failed on the synthetic fixture — no real PDF fixture is baked into this image by design (Dockerfile.fullstack changed by exactly one COPY line for this journey's own script); see /tmp/shakeout-e2e-pdf.log tail below"
    tail -15 /tmp/shakeout-e2e-pdf.log | sed 's/^/       /'
    PDF_INDEXED=0
  fi
else
  note "SKIPPED (loud, not a pass): could not even generate the synthetic PDF fixture in-container"
  PDF_INDEXED=0
fi

# ── Step 4 — nx search (CLI) + MCP `query` retrieval ────────────────────────
say "Step 4 — nx search (CLI) + MCP query tool (retrieval must return what was indexed)"
# `nx query` has NO CLI verb (only an MCP tool — confirmed against
# src/nexus/cli.py's add_command() list: no `query` group is registered).
# Its leg of this step is therefore driven via the MCP `query` tool inside
# Step 7's claude -p workload (one real billed call covers store_put +
# search + query + nx_answer together — CI-cost discipline), scoped at the
# Step-2 repo corpus so it also proves document-level catalog-aware
# retrieval of the REAL corpus, not just fresh MCP-authored content.

# SEMANTICALLY REAL query, deliberately (Hal's call, 2026-08-09). The first cut
# of this assertion searched for "$MARK widget sprocket" — a random run-unique
# token — and asserted the token came back. That tests embedding luck, not
# retrieval: a nonsense string has no meaningful position in the embedding
# space, so a miss says nothing about whether search works. It duly failed on
# the maiden run while the MCP `query` leg retrieved the SAME content from the
# SAME corpus, proving the content was indexed and reachable all along.
#
# Now: query the real English phrase that genuinely appears in the sentinel
# function's docstring ("widgets and sprockets") and assert the FILE surfaces.
# Both halves are real — a real query against real prose, asserting a real
# identifier — so a failure here means retrieval is actually broken.
if nx search "widgets and sprockets" --corpus code -c -m 5 2>/dev/null | grep -q "small_sentinel"; then
  ok "nx search (code__) retrieved the Step 2 corpus file by semantic content ('widgets and sprockets' -> small_sentinel.py)"
else
  bad "nx search (code__) did NOT surface small_sentinel.py for its own docstring phrase — semantic retrieval over the code corpus is broken"
fi

if nx search "$MARK widgets sprockets" --corpus docs -c -m 5 2>/dev/null | grep -q "$MARK"; then
  ok "nx search (docs__) retrieved the Step 2/3 corpus sentinel"
else
  bad "nx search (docs__) did NOT retrieve the Step 2/3 corpus sentinel"
fi

if [ "${PDF_INDEXED:-0}" = 1 ]; then
  if nx search "shakeout-e2e pdf sentinel $MARK" --corpus docs -c -m 5 2>/dev/null | grep -q "$MARK"; then
    ok "nx search (docs__) retrieved the Step 3 PDF sentinel"
  else
    bad "nx search (docs__) did NOT retrieve the Step 3 PDF sentinel (pdf indexing reported success but content is not retrievable)"
  fi
else
  note "PDF search retrieval SKIPPED — Step 3 PDF indexing was itself skipped/failed"
fi

# ── Step 5 — T2 memory + T1 scratch round-trip (content equality, not exit codes) ──
say "Step 5 — nx memory put/get + nx scratch put/get (equality-asserted)"

MEM_PROJECT="shakeoutE2E"
MEM_TITLE="note-$MARK"
MEM_CONTENT="shakeout-e2e T2 memory round-trip content $MARK — widgets and sprockets."
if nx memory put "$MEM_CONTENT" -p "$MEM_PROJECT" -t "$MEM_TITLE" --tags shakeout-e2e >/tmp/shakeout-e2e-mem-put.log 2>&1; then
  MEM_GOT="$(nx memory get -p "$MEM_PROJECT" -t "$MEM_TITLE" 2>/dev/null)"
  if [ "$MEM_GOT" = "$MEM_CONTENT" ]; then
    ok "T2 memory put+get round-trip: content is byte-exact"
  else
    bad "T2 memory get returned different content than was put"
    note "put:  $MEM_CONTENT"
    note "got:  ${MEM_GOT:-<empty>}"
  fi
else
  bad "nx memory put failed"; tail -10 /tmp/shakeout-e2e-mem-put.log | sed 's/^/       /'
fi

SCR_CONTENT="shakeout-e2e T1 scratch round-trip content $MARK"
SCR_PUT_OUT="$(nx scratch put "$SCR_CONTENT" 2>/tmp/shakeout-e2e-scr-put.err)"
SCR_ID="$(printf '%s' "$SCR_PUT_OUT" | grep -oE 'Stored: .*' | sed 's/^Stored: //')"
if [ -n "$SCR_ID" ]; then
  SCR_GOT="$(nx scratch get "$SCR_ID" 2>/dev/null)"
  if [ "$SCR_GOT" = "$SCR_CONTENT" ]; then
    ok "T1 scratch put+get round-trip: content is byte-exact (id=$SCR_ID)"
  else
    bad "T1 scratch get returned different content than was put"
    note "put:  $SCR_CONTENT"
    note "got:  ${SCR_GOT:-<empty>}"
  fi
else
  bad "nx scratch put did not print a parseable 'Stored: <id>' line"
  cat /tmp/shakeout-e2e-scr-put.err | sed 's/^/       /'
fi

# ── Step 6 — nx doctor: zero ✗, warnings allowlisted ────────────────────────
say "Step 6 — nx doctor (zero ✗, warnings allowlisted)"
nx doctor >/tmp/shakeout-e2e-doctor.log 2>&1
if grep -q "✗" /tmp/shakeout-e2e-doctor.log; then
  grep "✗" /tmp/shakeout-e2e-doctor.log | sed 's/^/       /'
  bad "doctor shows red ✗ during the shakeout"
else
  ok "doctor: zero ✗"
fi
# Warnings allowlist — EMPTY by design, mirroring tests/e2e/fresh-install-mvv.sh's
# convention (read that script before adding an entry). Every warning this
# journey surfaces is a decision: fix it, or allowlist it HERE with a
# rationale + bead reference. Covers BOTH channels: structlog lines
# (level='warning') and doctor's human-facing soft-warn rows (⚠).
# The sentinel below matches NOTHING (an empty regex would match everything
# through grep -v -E and silently allowlist every warning).
ALLOWLIST_REGEX='__NO_ALLOWLISTED_WARNINGS_SENTINEL__'
WARNING_LINES="$(grep -E "level='warning'|\[warning|⚠" /tmp/shakeout-e2e-doctor.log || true)"
UNALLOWLISTED="$(printf '%s\n' "$WARNING_LINES" | grep -v -E "$ALLOWLIST_REGEX" | grep -v '^$' || true)"
if [ -n "$UNALLOWLISTED" ]; then
  echo "$UNALLOWLISTED" | sed 's/^/       /'
  bad "non-allowlisted warnings in doctor output (fix or allowlist with a rationale)"
else
  ok "doctor: zero non-allowlisted warnings"
fi
if [ -n "$WARNING_LINES" ]; then
  note "(allowlisted warnings present):"
  printf '%s\n' "$WARNING_LINES" | sed 's/^/         /'
fi

# ── Step 7 — MCP tool surface through claude -p ─────────────────────────────
say "Step 7 — MCP tool surface (store_put, search, query, nx_answer) via claude -p"

authout="$(claude -p 'Reply with exactly the token AUTHOK and nothing else.' --dangerously-skip-permissions 2>&1)"
if printf '%s' "$authout" | grep -q "AUTHOK"; then ok "claude -p authenticated (mounted oauth works in-container)"
else bad "claude -p auth failed — cannot drive the MCP tool surface"; note "$(printf '%s' "$authout" | head -3 | tr '\n' ' ')"; say "ABORT (no claude auth)"; printf 'SHAKEOUT-E2E FAILED\n'; exit 1; fi

cat > /home/nexus/mcp.json <<'MCPJSON'
{ "mcpServers": { "nexus": { "command": "nx-mcp", "args": [] } } }
MCPJSON

MCP_MARK="mcp$MARK"
prompt="You have the nexus MCP server; use ONLY its tools (names start mcp__nexus__). Do ALL of, in order:
1. store_put ONE knowledge note (collection 'knowledge'), title unique, content EXACTLY:
   'Shakeout-e2e MCP workload note $MCP_MARK. Widgets mesh with sprockets to form gadgets.'
2. search 'widgets and sprockets' in the knowledge corpus.
3. query (document-level catalog-aware search) the code and docs corpora for: 'what does the shakeout-e2e fixture repository's sentinel function do, and what is its return value token $MARK?'
4. nx_answer the question 'what are widgets, sprockets, and gadgets in the shakeout-e2e fixtures?'.
End your reply with the literal token WORKLOADDONE."
note "driving the MCP workload via claude -p (store_put + search + query + nx_answer, one call)…"
wlout="$(claude -p "$prompt" --mcp-config /home/nexus/mcp.json --dangerously-skip-permissions \
  --allowedTools mcp__nexus__store_put mcp__nexus__search mcp__nexus__query mcp__nexus__nx_answer 2>&1)"
note "claude workload tail: $(printf '%s' "$wlout" | tail -4 | tr '\n' ' ' | cut -c1-320)"
printf '%s' "$wlout" | grep -q "WORKLOADDONE" && ok "MCP workload completed (claude drove the tools)" || bad "MCP workload did not finish cleanly"

sleep 2
if nx collection list 2>/dev/null | grep -qi "knowledge"; then
  ok "MCP store_put materialized a knowledge collection (tool really executed, not just claimed)"
else
  bad "no knowledge collection after the MCP workload — store_put did not actually execute"
fi

printf '%s' "$wlout" | grep -qiE "widget|sprocket|gadget" && ok "MCP search/nx_answer output is grounded (widget/sprocket/gadget present)" \
  || bad "MCP workload output does not mention widget/sprocket/gadget — not grounded"

printf '%s' "$wlout" | grep -q "$MARK" && ok "MCP query tool retrieved the Step 2 repo corpus sentinel ($MARK) — document-level catalog-aware retrieval of the REAL corpus works" \
  || bad "MCP query tool output does not contain the Step 2 corpus sentinel ($MARK) — the query leg of Step 4 is unproven"

nx daemon service status 2>&1 | grep -qiE "health.*ok|healthy|serving|running" && ok "service healthy after the full shakeout" || bad "service unhealthy after the shakeout"

say "RESULT"
if [ "$FAILS" -eq 0 ]; then
  printf '\033[32mSHAKEOUT-E2E PASSED\033[0m — install -> real-corpus code/md/pdf ingest -> search/query retrieval -> T2/T1 round-trip -> doctor -> MCP tool surface, with the onnx-local cap (nexus-33hpq) verified BOUND (not just configured) and peak engine RSS inside budget\n'
  printf '       SCOPE OF THIS PASS (do not over-read it): LINUX CONTAINER ONLY, on a locally-BUILT engine binary hand-staged by the harness. It does NOT exercise the published-asset download + sha256/signature-verify install path, and it covers NEITHER macOS NOR Windows — including Darwin, where this repo is primarily developed. A green run says the product installs and works end-to-end on this one platform via this one path.\n'
  exit 0
else
  printf '\033[31mSHAKEOUT-E2E FAILED — %d check(s)\033[0m\n' "$FAILS"
  exit 1
fi
