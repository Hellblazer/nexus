#!/usr/bin/env bash
# Behavioural test of cloud-client-path-gate.sh's leg B3 compare logic
# (nexus-20onx, round 3). The gate itself needs a cloud-mode box; the compare
# function does not. This extracts _ownerless_mode_verdict from the REAL script
# (never a copy), feeds it canned /v1/status bodies and checks the return code
# and the line it prints. It also checks the final-sentinel wiring: an
# unasserted mode must reach the PASSED line, and a violation must not.
#
# Prints "cloud_client_path_gate_b3_test.sh: N passed, M failed";
# tests/scripts/test_shell_suite_wiring.py holds the floor.
set -uo pipefail

HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
# The gate fragments this suite extracts call "$E2E_PYTHON", which the real gate sets by sourcing
# lib/python.sh at start; the extraction has no such preamble (nexus-u67ow).
# shellcheck source=lib/python.sh disable=SC1091
source "$HERE/lib/python.sh"
e2e_python_resolve || exit 2
GATE="$HERE/cloud-client-path-gate.sh"
NAME="cloud_client_path_gate_b3_test.sh"
PASS=0
FAIL=0

TMP="$(mktemp -d "${TMPDIR:-/tmp}/ccpg-b3-test.XXXXXX")"
trap 'rm -rf "$TMP"' EXIT

sed -n '/^_ownerless_mode_verdict() {/,/^}/p' "$GATE" > "$TMP/fn.sh"
# Anchored on the function's own shape, not a line count: it opens on its name line and the extraction
# must end on the closing brace (a function whose body moved or lost its end would fail here).
if [ "$(head -n 1 "$TMP/fn.sh")" != '_ownerless_mode_verdict() {' ] || [ "$(tail -n 1 "$TMP/fn.sh")" != '}' ]; then
  echo "[FAIL] _ownerless_mode_verdict not found or not closed in $GATE (extracted $(wc -l < "$TMP/fn.sh") lines)"
  echo "$NAME: 0 passed, 1 failed"
  exit 1
fi

# case <label> <want-rc> <want-text> <status-body> <expected-mode>
run_case() {
  local label="$1" want_rc="$2" want_text="$3" body="$4" expected="$5" out rc
  # shellcheck disable=SC1091
  out="$(source "$TMP/fn.sh"; _ownerless_mode_verdict "$body" "$expected")"
  rc=$?
  if [ "$rc" = "$want_rc" ] && [[ "$out" == *"$want_text"* ]]; then
    PASS=$((PASS + 1)); echo "[ok]   $label"
  else
    FAIL=$((FAIL + 1)); echo "[FAIL] $label: want rc=$want_rc text='$want_text', got rc=$rc"
    printf '%s\n' "$out" | sed 's/^/         /'
  fi
}

# A served /v1/status always carries embedding_mode; an engine before RDR-223 P3.2 has no mode field.
LOG='{"embedding_mode":"voyage","ownerless_write_mode":"log-only"}'
ENF='{"embedding_mode":"voyage","ownerless_write_mode":"enforce"}'
OLD='{"embedding_mode":"voyage"}'

run_case "expected log-only, engine log-only -> ok (0)" 0 "ok [B3]" "$LOG" log-only
run_case "expected enforce, engine enforce -> ok (0)" 0 "ok [B3]" "$ENF" enforce
run_case "expected log-only, engine enforce -> violation (1): the dangerous mis-wire" \
  1 "reports ownerless_write_mode='enforce', expected 'log-only'" "$ENF" log-only
run_case "expected enforce, engine log-only -> violation (1)" \
  1 "reports ownerless_write_mode='log-only', expected 'enforce'" "$LOG" enforce
run_case "expected set, engine reports no mode -> violation (1)" \
  1 "(absent)" "$OLD" log-only
run_case "expected set, status body unreadable -> violation (1)" \
  1 "no readable status body" "" enforce
run_case "expected set, status body is not JSON -> violation (1)" \
  1 "no readable status body" "<html>502</html>" log-only
# The skip-pass hole (critic H1): a live mode with no assertion must not pass.
run_case "expected UNSET, engine reports log-only -> violation (1), names the knob" \
  1 "NX_EXPECTED_OWNERLESS_WRITE_MODE is unset" "$LOG" ""
run_case "expected UNSET, engine reports enforce -> violation (1)" \
  1 "NX_EXPECTED_OWNERLESS_WRITE_MODE is unset" "$ENF" ""
run_case "expected UNSET, engine reports no mode -> not run (3)" \
  3 "NOT RUN [B3]" "$OLD" ""
# The unreadable-body hole (critic S4): with the variable unset, an edge failure must not read as "an
# engine with no mode" and let the gate print PASSED.
run_case "expected UNSET, status body empty (curl failed) -> violation (1), never not-run" \
  1 "no readable status body" "" ""
run_case "expected UNSET, edge 401 JSON error body -> violation (1)" \
  1 "no readable status body" '{"error":"unauthorized"}' ""
run_case "expected UNSET, edge 502 HTML page -> violation (1)" \
  1 "no readable status body" "<html>502 Bad Gateway</html>" ""
run_case "expected UNSET, JSON that is not an object -> violation (1)" \
  1 "no readable status body" '["embedding_mode"]' ""

# Leg J's compare logic (nexus-wbfpw.50): the reaper liveness verdict, extracted from the real script and
# fed canned /v1/status bodies with a fixed clock (13:00:00Z; limit = 3 x 3600 + 600 = 11400 s).
sed -n '/^_reaper_status_verdict() {/,/^}/p' "$GATE" > "$TMP/jfn.sh"
# Anchored on the closing PY heredoc line (the python body ends there, then the function's brace), so a
# truncated extraction cannot pass by being long enough.
if [ "$(head -n 1 "$TMP/jfn.sh")" != '_reaper_status_verdict() {' ] \
   || [ "$(tail -n 2 "$TMP/jfn.sh" | head -n 1)" != 'PY' ] || [ "$(tail -n 1 "$TMP/jfn.sh")" != '}' ]; then
  echo "[FAIL] _reaper_status_verdict not found or not closed on its PY heredoc in $GATE (extracted $(wc -l < "$TMP/jfn.sh") lines)"
  echo "$NAME: $PASS passed, $((FAIL + 1)) failed"
  exit 1
fi
NOW="$("$E2E_PYTHON" -c "from datetime import datetime, timezone; print(int(datetime(2026, 10, 4, 13, 0, 0, tzinfo=timezone.utc).timestamp()))")"

# case <label> <want-rc> <want-text> <status-body>
run_j() {
  local label="$1" want_rc="$2" want_text="$3" body="$4" out rc
  # shellcheck disable=SC1091
  out="$(source "$TMP/jfn.sh"; _reaper_status_verdict "$body" "$NOW")"
  rc=$?
  if [ "$rc" = "$want_rc" ] && [[ "$out" == *"$want_text"* ]]; then
    PASS=$((PASS + 1)); echo "[ok]   $label"
  else
    FAIL=$((FAIL + 1)); echo "[FAIL] $label: want rc=$want_rc text='$want_text', got rc=$rc"
    printf '%s\n' "$out" | sed 's/^/         /'
  fi
}
# reaper_body <enabled> <interval> <budget> <last-json> <failed-json> [<last_pass-json>]
reaper_body() {
  local lp='{"tenants_visited":3,"tenants_errored":0,"tenants_refused":0,"tenants_empty":1}'
  [ "$#" -ge 6 ] && lp="$6"
  printf '{"embedding_mode":"voyage","reaper":{"enabled":%s,"interval_seconds":%s,"wall_clock_budget_seconds":%s,"last_completed_pass_at":%s,"failed_passes_total":%s,"last_pass":%s}}' \
    "$1" "$2" "$3" "$4" "$5" "$lp"
}

run_j "J: enabled, fresh pass, no failures -> ok (0)" 0 "ok [J]" \
  "$(reaper_body true 3600 600 '"2026-10-04T12:30:00Z"' 0)"
run_j "J: a pass exactly at the limit (three intervals plus the budget) -> ok (0)" 0 "ok [J]" \
  "$(reaper_body true 3600 600 '"2026-10-04T09:50:00Z"' 0)"
run_j "J: a pass one second past the limit -> violation (1)" 1 "more than 3 intervals" \
  "$(reaper_body true 3600 600 '"2026-10-04T09:49:59Z"' 0)"
run_j "J: a stale pass (12 h) -> violation (1)" 1 "the reaper may be dead" \
  "$(reaper_body true 3600 600 '"2026-10-04T01:00:00Z"' 0)"
run_j "J: the limit reads interval and budget from the object (900 s + 0 budget, pass 4 intervals ago) -> violation (1)" \
  1 "more than 3 intervals of 900s" "$(reaper_body true 900 0 '"2026-10-04T12:00:00Z"' 0)"
run_j "J: no completed pass yet (null) -> violation (1)" 1 "has made no completed pass" \
  "$(reaper_body true 3600 600 null 0)"
run_j "J: an unparseable pass time -> violation (1)" 1 "not an ISO-8601 instant" \
  "$(reaper_body true 3600 600 '"yesterday"' 0)"
run_j "J: failed_passes_total 2 -> violation (1)" 1 "failed_passes_total is 2" \
  "$(reaper_body true 3600 600 '"2026-10-04T12:30:00Z"' 2)"
run_j "J: a nonzero failed_passes_total is called a since-boot counter, not a live outage" 1 "since-boot counter" \
  "$(reaper_body true 3600 600 '"2026-10-04T12:30:00Z"' 1)"
run_j "J: last_pass.tenants_errored 2 -> violation (1)" 1 "last_pass.tenants_errored is 2" \
  "$(reaper_body true 3600 600 '"2026-10-04T12:30:00Z"' 0 '{"tenants_visited":3,"tenants_errored":2,"tenants_refused":0,"tenants_empty":1}')"
run_j "J: last_pass.tenants_errored absent -> violation (1)" 1 "last_pass.tenants_errored is None" \
  "$(reaper_body true 3600 600 '"2026-10-04T12:30:00Z"' 0 '{"tenants_visited":3}')"
run_j "J: last_pass null despite a completed pass -> violation (1)" 1 "reaper.last_pass is None" \
  "$(reaper_body true 3600 600 '"2026-10-04T12:30:00Z"' 0 null)"
run_j "J: the ok line prints the last_pass counts" 0 "last_pass visited=3 errored=0 refused=0 empty=1" \
  "$(reaper_body true 3600 600 '"2026-10-04T12:30:00Z"' 0)"
run_j "J: failed_passes_total absent -> violation (1)" 1 "failed_passes_total is None" \
  '{"embedding_mode":"voyage","reaper":{"enabled":true,"interval_seconds":3600,"wall_clock_budget_seconds":600,"last_completed_pass_at":"2026-10-04T12:30:00Z"}}'
run_j "J: reaper disabled -> violation (1), never not-applicable" 1 "reaper.enabled is False" \
  "$(reaper_body false 3600 600 '"2026-10-04T12:30:00Z"' 0)"
run_j "J: {\"enabled\":false} alone -> violation (1)" 1 "reaper.enabled is False" \
  '{"embedding_mode":"voyage","reaper":{"enabled":false}}'
run_j "J: reaper key absent (an engine before it, or an edge that strips it) -> violation (1)" \
  1 "no \`reaper\` object" "$OLD"
run_j "J: reaper is not an object -> violation (1)" 1 "no \`reaper\` object" \
  '{"embedding_mode":"voyage","reaper":"enabled"}'
run_j "J: a boolean interval is not an integer -> violation (1)" 1 "interval_seconds is True" \
  "$(reaper_body true true 600 '"2026-10-04T12:30:00Z"' 0)"
run_j "J: status body unreadable (curl failed) -> violation (1)" 1 "no readable status body" ""
run_j "J: status body is an edge HTML page -> violation (1)" 1 "no readable status body" "<html>502 Bad Gateway</html>"
run_j "J: an edge 401 JSON error body -> violation (1)" 1 "no readable status body" '{"error":"unauthorized"}'

# Leg K's judge, _edge_expect, extracted from the real script: a status code first, then the engine's OWN
# words. An edge or WAF can answer a 400 with a JSON {"error": ...} of its own, so a 400 probe passes only
# when the error string carries the fragment the engine's handler writes (nexus-wbfpw.50 review, I2).
sed -n '/^_edge_expect() {/,/^}/p' "$GATE" > "$TMP/efn.sh"
if [ "$(head -n 1 "$TMP/efn.sh")" != '_edge_expect() {' ] \
   || [ "$(tail -n 2 "$TMP/efn.sh" | head -n 1)" != 'PY' ] || [ "$(tail -n 1 "$TMP/efn.sh")" != '}' ]; then
  echo "[FAIL] _edge_expect not found or not closed on its PY heredoc in $GATE (extracted $(wc -l < "$TMP/efn.sh") lines)"
  echo "$NAME: $PASS passed, $((FAIL + 1)) failed"
  exit 1
fi
# run_e <label> <want-rc> <want-text> <code> <body> <expect-args...>
run_e() {
  local label="$1" want_rc="$2" want_text="$3" code="$4" body="$5" out rc
  shift 5
  # shellcheck disable=SC1091,SC2034
  out="$(K_COLLECTION="knowledge__ccpg-unregistered-1-2__voyage-context-3__v1"; EDGE_CODE="$code"; EDGE_BODY="$body"
         source "$TMP/efn.sh"; _edge_expect "$@" 2>&1)"
  rc=$?
  if [ "$rc" = "$want_rc" ] && [[ "$out" == *"$want_text"* ]]; then
    PASS=$((PASS + 1)); echo "[ok]   $label"
  else
    FAIL=$((FAIL + 1)); echo "[FAIL] $label: want rc=$want_rc text='$want_text', got rc=$rc"
    printf '%s\n' "$out" | sed 's/^/         /'
  fi
}
run_e "E: 400 carrying the engine's message fragment -> ok" 0 "ok [K5]" 400 \
  '{"error":"missing required field: collection"}' K5 400 error "missing required field: collection"
run_e "E: 400 with an edge's own JSON error -> violation (the request may never have reached the engine)" 1 "expected it to contain the engine's" \
  400 '{"error":"Request blocked by security policy"}' K5 400 error "missing required field: collection"
run_e "E: 400 with an edge HTML page -> violation" 1 "not a JSON object" 400 '<html>400 Bad Request</html>' \
  K5 400 error "missing required field: collection"
run_e "E: the wrong status (403 from a WAF) -> violation" 1 "HTTP 403, expected 400" 403 \
  '{"error":"missing required field: collection"}' K5 400 error "missing required field: collection"
run_e "E: an error probe that names no fragment -> violation, never a pass on any error string" 1 "must name the engine message fragment" \
  400 '{"error":"anything"}' K5 400 error
run_e "E: the engine's exact 404 body -> ok" 0 "ok [K1]" 404 '{"error":"not found"}' K1 404 notfound
run_e "E: an edge's own 404 JSON -> violation" 1 "expected exactly the engine's" 404 '{"error":"Not Found","message":"no route"}' \
  K1 404 notfound
run_e "E: 422 with reason unregistered_collection -> ok" 0 "ok [K2]" 422 \
  '{"error":"collection is not registered","reason":"unregistered_collection"}' K2 422 unregistered
run_e "E: 422 without the reason key -> violation" 1 "reason is None" 422 '{"error":"unprocessable"}' K2 422 unregistered
run_e "E: 200 empty reapable page in the engine's shape -> ok" 0 "ok [K8]" 200 \
  '{"collection":"knowledge__ccpg-unregistered-1-2__voyage-context-3__v1","grace_seconds":null,"returned":0,"next_after":null,"chunks":[]}' \
  K8 200 reapable_empty

# Wiring: the not-run state reaches the final sentinel, a violation is a leg
# failure, and the function is called with the bearer-resolved status body.
check_wiring() {
  local label="$1" pattern="$2"
  if grep -Eq -- "$pattern" "$GATE"; then
    PASS=$((PASS + 1)); echo "[ok]   $label"
  else
    FAIL=$((FAIL + 1)); echo "[FAIL] $label: /$pattern/ not found in $GATE"
  fi
}
check_wiring "B3 return 3 sets B3_NOT_RUN" '3\) echo "  \$B3_LINE"; B3_NOT_RUN=1'
check_wiring "B3 violation is a leg failure" '\*\) _leg_fail "\$B3_LINE"'
check_wiring "J is judged on the status body leg B fetched" 'J_LINE="\$\(_reaper_status_verdict "\$STATUS_BODY"\)"'
check_wiring "J violation is a leg failure" '_leg_fail "\$J_LINE"'
check_wiring "the leg battery expects 9 legs (A B C+D E F H I J K)" '^EXPECTED_LEGS=9$'

# Leg K runs against the LIVE engine, so its read-only property is pinned here rather than left to a comment,
# and pinned STRUCTURALLY: the audit below holds an allowlist of what the gate may send, not a per-probe
# trust in the status each probe says it expects (a probe that declares 400 or 404 proves nothing about what
# the engine would do with a full body). It asserts:
#   - the only request paths are /v1/vectors/gc/this-route-does-not-exist-*, the gc routes quarantine-restore,
#     quarantine-orphans, restore-rereferenced and expire-quarantine, /v1/vectors/reapable and
#     /v1/vectors/manifest-less-census;
#   - the three non-restore gc routes and the missing route carry the body '{}' exactly, and expect 400 / 404;
#   - quarantine-restore carries origin_collection=$K_COLLECTION and dry_run:true, and expects 400 or 422;
#   - reapable and manifest-less-census name $K_COLLECTION (200 or 400) or $K_QUARANTINE (400) and nothing else;
#   - K_COLLECTION, K_QUARANTINE and K_STAMP are each assigned exactly once, to the unregistered-name shape;
#   - _edge_post appears only inside leg K, and no curl with a write flag (-X, -d, --data, --json, -F, -T,
#     --upload-file, --request) exists anywhere but the one `-X POST` inside _edge_post;
#   - no client write call (.put/.delete/.post/.patch/.out/.ack/.renew/.nack/store_put/tuple_out) appears in
#     any leg but the two that already wrote before leg K existed: E (T2 probe row) and H (tuple-space
#     renew and ack-with-reply).
# The audit is falsified below: it must REJECT mutated copies of the gate, one per bypass.
cat > "$TMP/k_audit.py" <<'PY'
import re
import sys

text = open(sys.argv[1]).read()
errs = []
WRITE_LEGS_BEFORE_K = {"E", "H"}

# Leg segments, by the _leg_enter lines (the definition `_leg_enter() {` has no leg name).
legs = {"pre": []}
cur = "pre"
for ln in text.split("\n"):
    m = re.match(r"_leg_enter\s+(\S+)\s", ln)
    if m:
        cur = m.group(1)
        legs.setdefault(cur, [])
    legs[cur].append(ln)


def code(lines):
    return [ln for ln in lines if not ln.lstrip().startswith("#")]


def logical(lines):
    out, acc = [], ""
    for ln in lines:
        acc += ln
        if ln.rstrip().endswith("\\"):
            acc = acc.rstrip()[:-1] + " "
            continue
        out.append(acc)
        acc = ""
    if acc:
        out.append(acc)
    return out


all_code = "\n".join(code(text.split("\n")))
if "K" not in legs:
    print("no leg K found")
    sys.exit(0)
k_code = "\n".join(code(legs["K"]))

# 1. _edge_post only inside leg K.
for name, lines in legs.items():
    if name != "K" and re.search(r"\b_edge_post\b", "\n".join(code(lines))):
        errs.append("_edge_post used outside leg K (in %s)" % name)

# 2. The leg-K names are assigned exactly once, to the unregistered shape, and never rebound another way.
want_assign = {
    "K_COLLECTION": 'K_COLLECTION="knowledge__ccpg-unregistered-${K_STAMP}__voyage-context-3__v1"',
    "K_QUARANTINE": 'K_QUARANTINE="quarantine-${K_COLLECTION}"',
    "K_STAMP": 'K_STAMP="$(date +%s)-$RANDOM"',
}
for var, line in want_assign.items():
    assigns = re.findall(r"(?<![\w$])%s\+?=[^\n]*" % var, all_code)
    if len(assigns) != 1:
        errs.append("%s is assigned %d times, expected exactly once" % (var, len(assigns)))
    elif assigns[0].strip() != line:
        errs.append("%s is assigned %r, expected %r" % (var, assigns[0].strip(), line))
    if re.search(r"\b(read|printf\s+-v|declare|typeset|local|unset|eval|mapfile|readarray)\b[^\n]*\b%s\b" % var, all_code):
        errs.append("%s is rebound or unset by a builtin" % var)

# 3. curl: a write flag is allowed only on the one POST inside _edge_post.
WRITE_FLAG = re.compile(r"(?<=\s)(--request|--data[\w-]*|--json|--form[\w-]*|--upload-file|-[A-Za-z]*[XdFT][A-Za-z]*)(?![\w-])")
write_curls = {}
for name, lines in legs.items():
    for ll in logical(code(lines)):
        for m in re.finditer(r"(?<![\w-])curl\s+-", ll):
            tail = ll[m.start():]
            if WRITE_FLAG.search(tail):
                write_curls.setdefault(name, []).append(tail)
for name, items in write_curls.items():
    if name != "K":
        errs.append("a curl with a write flag in leg %s: %s" % (name, items[0][:80]))
k_writes = write_curls.get("K", [])
fn = re.search(r"^_edge_post\(\) \{\n(.*?)^\}", k_code, re.M | re.S)
if len(k_writes) != 1:
    errs.append("leg K has %d write curls, expected exactly the one in _edge_post" % len(k_writes))
elif fn is None or " ".join(k_writes[0].split()) not in " ".join(fn.group(1).replace("\\\n", " ").split()):
    errs.append("leg K's write curl is not inside _edge_post")
else:
    methods = re.findall(r"(?<=\s)-X\s+(\S+)", k_writes[0])
    if methods != ["POST"]:
        errs.append("_edge_post's curl uses methods %r, expected exactly ['POST']" % (methods,))

# 4. No client write outside the legs that already wrote.
CLIENT_WRITE = re.compile(r"\.(put|post|delete|patch|out|ack|renew|nack|store_put|upsert\w*)\(|\btuple_out\b")
for name, lines in legs.items():
    if name not in WRITE_LEGS_BEFORE_K | {"K"} and CLIENT_WRITE.search("\n".join(code(lines))):
        errs.append("a client write call in leg %s, which has none today" % name)

# 5. Every probe: allowlisted path, body and status; every _edge_post paired with an _edge_expect.
CALL = re.compile(
    r'^[ \t]*_edge_post\s+"([^"]+)"\s*(?:\\\n\s*)?(\'[^\']*\'|"(?:[^"\\]|\\.)*")[ \t]*\n'
    r"[ \t]*_edge_expect\s+(K\d+)\s+(\d+)\s+(\w+)(?:\s+(\"[^\"]*\"))?\s*\|\|\s*K_BAD=1[ \t]*$",
    re.M,
)
calls = CALL.findall(k_code)
posted = len(re.findall(r"^[ \t]*_edge_post\s", k_code, re.M))
expects = len(re.findall(r"^[ \t]*_edge_expect\s", k_code, re.M))
if not (len(calls) == posted == expects) or posted < 12:
    errs.append("expected every one of >= 12 _edge_post calls to pair with one _edge_expect, got %d paired, %d posted, %d expected"
                % (len(calls), posted, expects))
labels = [c[2] for c in calls]
if len(set(labels)) != len(labels):
    errs.append("duplicate probe labels")
GC_EMPTY = ("quarantine-orphans", "restore-rereferenced", "expire-quarantine")
for path, body, label, code_s, kind, fragment in calls:
    b = body.replace('\\"', '"')
    if re.fullmatch(r"/v1/vectors/gc/this-route-does-not-exist-\$\{K_STAMP\}", path):
        if body != "'{}'" or code_s != "404" or kind != "notfound":
            errs.append("%s: the missing-route probe must post '{}' and expect 404 notfound" % label)
    elif path in ["/v1/vectors/gc/" + r for r in GC_EMPTY]:
        if body != "'{}'":
            errs.append("%s: %s must post the body '{}' exactly, got %s" % (label, path, body))
        if code_s != "400":
            errs.append("%s: %s must expect 400, got %s" % (label, path, code_s))
    elif path == "/v1/vectors/gc/quarantine-restore":
        if '"origin_collection":"$K_COLLECTION"' not in b:
            errs.append("%s: a quarantine-restore probe that does not name origin_collection=$K_COLLECTION" % label)
        if '"dry_run":true' not in b or '"dry_run":false' in b:
            errs.append("%s: a quarantine-restore probe without dry_run:true" % label)
        if code_s not in ("400", "422"):
            errs.append("%s: quarantine-restore must expect 400 or 422, got %s" % (label, code_s))
    elif path in ("/v1/vectors/reapable", "/v1/vectors/manifest-less-census"):
        names = re.findall(r'"collection":"([^"]*)"', b)
        if len(names) != 1 or len(re.findall(r'"collection"', b)) != 1 or names[0] not in ("$K_COLLECTION", "$K_QUARANTINE"):
            errs.append("%s: %s must name exactly $K_COLLECTION or $K_QUARANTINE, got %r" % (label, path, names))
        elif names[0] == "$K_QUARANTINE" and code_s != "400":
            errs.append("%s: a read naming the quarantine collection must expect 400, got %s" % (label, code_s))
        elif code_s not in ("200", "400"):
            errs.append("%s: %s must expect 200 or 400, got %s" % (label, path, code_s))
    else:
        errs.append("%s: path %s is not in the allowlist" % (label, path))
    if code_s == "400" and kind == "error" and not fragment:
        errs.append("%s: a 400 probe must name the engine message fragment it expects" % label)
print("; ".join(errs) if errs else "OK %d" % len(calls))
PY
K_AUDIT="$("$E2E_PYTHON" "$TMP/k_audit.py" "$GATE")"
if [[ "$K_AUDIT" == OK\ * ]]; then
  PASS=$((PASS + 1)); echo "[ok]   K probes are read-only by construction (${K_AUDIT#OK } probes audited against the allowlist)"
else
  FAIL=$((FAIL + 1)); echo "[FAIL] K probes are read-only by construction: $K_AUDIT"
fi

# Falsify the audit: each case rewrites ONE spot of a copy of the real gate into a bypass the audit must refuse,
# and requires the refusal to name the right cause. A case whose rewrite does not apply (the gate text moved)
# fails loudly, never passes vacuously.
# neg_case <label> <want-text> <old> <new>   (old must occur exactly once in the gate)
neg_case() {
  local label="$1" want="$2" old="$3" new="$4" out
  if ! "$E2E_PYTHON" - "$GATE" "$TMP/mut.sh" "$old" "$new" <<'PY'
import sys

src, dst, old, new = sys.argv[1:5]
t = open(src).read()
if t.count(old) != 1:
    sys.exit(3)
open(dst, "w").write(t.replace(old, new))
PY
  then
    FAIL=$((FAIL + 1)); echo "[FAIL] audit negative: $label: the rewrite did not apply (the gate text moved), so the case is vacuous"
    return
  fi
  out="$("$E2E_PYTHON" "$TMP/k_audit.py" "$TMP/mut.sh")"
  if [[ "$out" != OK\ * && "$out" == *"$want"* ]]; then
    PASS=$((PASS + 1)); echo "[ok]   audit negative: $label"
  else
    FAIL=$((FAIL + 1)); echo "[FAIL] audit negative: $label: want a refusal naming '$want', got: $out"
  fi
}
FULL_ORPHANS='"{\"collection\":\"$K_COLLECTION\",\"quarantine_collection\":\"$K_QUARANTINE\",\"quarantined_at\":\"2026-01-01T00:00:00Z\"}"'
neg_case "a full-body quarantine-orphans probe that expects 400" "quarantine-orphans must post the body '{}'" \
  '_edge_post "/v1/vectors/gc/quarantine-orphans" '"'{}'" '_edge_post "/v1/vectors/gc/quarantine-orphans" '"$FULL_ORPHANS"
neg_case "a store-delete probe that expects 404" "is not in the allowlist" \
  '    _edge_expect K12 400 error "is a quarantine collection" || K_BAD=1
' '    _edge_expect K12 400 error "is a quarantine collection" || K_BAD=1
    _edge_post "/v1/vectors/store-delete" '"'{}'"'
    _edge_expect K13 404 error "not found" || K_BAD=1
'
neg_case "K_COLLECTION reassigned to a real name before the probes" "K_COLLECTION is assigned 2 times" \
  'K_QUARANTINE="quarantine-${K_COLLECTION}"' 'K_COLLECTION="knowledge__real__voyage-context-3__v1"
K_QUARANTINE="quarantine-${K_COLLECTION}"'
neg_case "K_COLLECTION reassigned inside the probe block" "K_COLLECTION is assigned 2 times" \
  '    K_BAD=0
' '    K_BAD=0
    K_COLLECTION="knowledge__real__voyage-context-3__v1"
'
neg_case "K_COLLECTION rebound by read" "K_COLLECTION is rebound or unset by a builtin" \
  '    K_BAD=0
' '    K_BAD=0
    read -r K_COLLECTION <<< "knowledge__real__voyage-context-3__v1"
'
neg_case "a raw curl -X POST inside leg K" "leg K has 2 write curls" \
  '    K_BAD=0
' '    K_BAD=0
    curl -sS -X POST -H @"$BEARER_FILE" --data '"'{}'"' "$SERVICE_URL/v1/vectors/gc/quarantine-orphans" >/dev/null
'
neg_case "a raw curl -X DELETE inside leg J" "a curl with a write flag in leg J" \
  'J_RC=0
' 'J_RC=0
curl -sS -X DELETE -H @"$BEARER_FILE" "$SERVICE_URL/v1/vectors/store-delete" >/dev/null
'
neg_case "a curl -d inside leg A" "a curl with a write flag in leg A" \
  'VERSION_BODY="$(curl -sS -m 20 "$SERVICE_URL/version")"' 'VERSION_BODY="$(curl -sS -m 20 -d x "$SERVICE_URL/version")"'
neg_case "a curl with a clustered -sSX write flag outside leg K" "a curl with a write flag in leg B" \
  'NOAUTH_STATUS="$(curl -sS -m 20 -o /dev/null' 'NOAUTH_STATUS="$(curl -sSX POST -m 20 -o /dev/null'
neg_case "the helper's method changed to DELETE" "methods ['DELETE']" \
  'curl -sS -m 30 -X POST' 'curl -sS -m 30 -X DELETE'
neg_case "_edge_post called from leg J" "_edge_post used outside leg K (in J)" \
  'J_RC=0
' 'J_RC=0
_edge_post "/v1/vectors/reapable" '"'{}'"'
'
neg_case "a client write call added to leg J" "a client write call in leg J" \
  'J_RC=0
' 'J_RC=0
uv run python -c '"'store.put(1)'"'
'
neg_case "a quarantine-restore probe without dry_run" "without dry_run:true" \
  '\"chashes\":[\"$K_CHASH\"],\"dry_run\":true}"
    _edge_expect K2' '\"chashes\":[\"$K_CHASH\"]}"
    _edge_expect K2'
neg_case "a quarantine-restore probe with dry_run:false" "without dry_run:true" \
  '\"dry_run\":true}"
    _edge_expect K3' '\"dry_run\":false}"
    _edge_expect K3'
neg_case "a quarantine-restore probe naming a real origin" "does not name origin_collection=\$K_COLLECTION" \
  '{\"origin_collection\":\"$K_COLLECTION\",\"chashes\":[\"$K_CHASH\"],\"dry_run\":true}"' \
  '{\"origin_collection\":\"knowledge__real__voyage-context-3__v1\",\"chashes\":[\"$K_CHASH\"],\"dry_run\":true}"'
neg_case "a 200 read of a real collection" "must name exactly \$K_COLLECTION or \$K_QUARANTINE" \
  '{\"collection\":\"$K_COLLECTION\",\"limit\":1}"
    _edge_expect K8' '{\"collection\":\"knowledge__real__voyage-context-3__v1\",\"limit\":1}"
    _edge_expect K8'
neg_case "a read of the quarantine collection that expects 200" "must expect 400" \
  '_edge_expect K9 400 error "is a quarantine collection"' '_edge_expect K9 200 reapable_empty'
neg_case "a gc route probe that expects 200" "must expect 400" \
  '_edge_expect K5 400 error "missing required field: collection"' '_edge_expect K5 200 error "x"'
neg_case "a probe body from a variable" "must post the body '{}' exactly" \
  '"/v1/vectors/gc/expire-quarantine" '"'{}'" '"/v1/vectors/gc/expire-quarantine" "$K_BODY"'
neg_case "a probe path from a variable" "is not in the allowlist" \
  '_edge_post "/v1/vectors/reapable" "{\"collection\":\"$K_QUARANTINE\"}"' '_edge_post "$K_PATH" "{\"collection\":\"$K_QUARANTINE\"}"'
neg_case "a 400 probe with no engine message fragment" "must name the engine message fragment" \
  '_edge_expect K7 400 error "missing required field: quarantine_collection"' '_edge_expect K7 400 error'
neg_case "an unpaired _edge_post" "to pair with one _edge_expect" \
  '    _edge_expect K11 200 census_empty || K_BAD=1
' '    _edge_expect K11 200 census_empty || K_BAD=1
    _edge_post "/v1/vectors/reapable" "{\"collection\":\"$K_COLLECTION\"}"
'
check_wiring "the PASSED line carries the unasserted-mode note" 'violations=0 \(ownerless-write mode NOT asserted'

echo "$NAME: $PASS passed, $FAIL failed"
[ "$FAIL" -eq 0 ]
