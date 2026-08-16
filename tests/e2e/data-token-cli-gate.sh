#!/usr/bin/env bash
# nexus-rftfs: DATA-TOKEN CLI GATE — the sandboxed-HOME, real-subprocess
# journey for client-side data-token self-minting (RDR-005 2a, nexus-wrwb7 /
# nexus-ssqk9).
#
# Why this exists: nexus-wrwb7/ssqk9 shipped 04158dad4 with a real-engine
# pytest E2E (tests/db/test_data_token_manager_e2e.py) that proves the
# DataTokenManager resolution seam IN-PROCESS — constructing
# HttpVectorClient directly inside the pytest worker. It does not touch
# `nx config set`, config.yml resolution, the `nx service token issue`
# CLI, or `nx doctor`'s check as a REAL SUBPROCESS would exercise them.
# Critic round-2 on nexus-wrwb7 (recorded on nexus-rftfs) asked for exactly
# that: drive the real `nx` binary, in a real subprocess, through the
# whole mint_token/mint_tenant journey, before Hal sets mint_token on his
# PRODUCTION install.
#
# Pattern mirrored EXACTLY from tests/e2e/fresh-install-mvv.sh (the
# canonical scrubbed-HOME virgin-journey gate): build the wheel under
# test, install into an isolated venv, `env -i` allowlisted sandbox HOME
# (nexus project memory: a sandboxed HOME does NOT isolate a service
# install by itself — PATH scrubbing to the sandbox's own BIN_DIR is what
# actually prevents talking to a live install; see fresh-install-mvv.sh's
# `_nx()`/`_uv_sandboxed()` comments for the nexus-enfoh lesson this
# mirrors), local NX_LOCAL=1 engine provisioning via `nx init`, explicit
# PASSED/FAILED verdict line, non-vacuity asserts, trap-based cleanup.
#
# JOURNEY:
#   1. Provision the local engine (nx init -y --no-autostart, bge-768).
#   2. Issue a scope=mint-locked credential bound to tenant "gate-dtok"
#      (deliberately NOT "default" — nexus-ssqk9's tenant-asymmetric
#      shape: every Http*Store defaults its own tenant kwarg to
#      "default", but a real deployed credential is bound to whatever
#      tenant the operator issued it under).
#   3. `nx config set mint_token` / `mint_tenant` — assert mint_token
#      masks, mint_tenant does not (nexus-ssqk9 NON_SECRET_CREDENTIALS).
#   4. store put + search round trip that can ONLY succeed via the
#      self-minted data token: NX_SERVICE_TOKEN is poisoned to a garbage
#      sentinel for these two calls specifically (same technique as the
#      pytest E2E — see _nx_poisoned() below for why this is a REAL proof
#      here, not a weaker one: nexus.db.http_vector_client._request_once
#      resolves the static/lease token FIRST and then unconditionally
#      overwrites it with the minted data token when mint_token is
#      configured, so merely leaving service_token unset would still
#      succeed via a perfectly valid lease-derived token and prove
#      nothing — poisoning it is what makes a pass mean the mint path
#      specifically authenticated).
#   5. `nx doctor` green, mint check line present with a live round trip
#      + granted TTL (never the "not configured" skip wording).
#   6. Residue discipline (nexus-lgiqw) at the granularity the CLI
#      subprocess model actually supports — see the leg 6 header comment
#      for why "exactly one mint across every command in this script" is
#      NOT the correct invariant to assert here, and what is asserted
#      instead.
#   7. Cross-process lease reuse (nexus-9c7t9): the lease file store put
#      (leg 5) wrote is what makes leg 5's `nx doctor` invocation (leg 6,
#      itself a fresh subprocess with an EMPTY in-process cache) report
#      "reused the cached (lease file)" instead of "minted a fresh" — this
#      leg asserts that wording directly, then drives a THIRD subprocess
#      (another poisoned search) and asserts its stderr shows the
#      lease-reuse event with NO new mint. Also asserts the lease file
#      itself: exists, mode 0600, never contains the raw mint-locked
#      credential.
#   8. Negative arm: wrong mint_tenant -> loud typed 403, never a silent
#      fallback, never a hang; restore + one more round trip proves
#      recovery.
#   9. Non-vacuity + explicit verdict line + trap-based teardown.
#
# Cost: ~5-15 min (engine binary + PG bundle + bge ONNX download fresh
# unless DATA_TOKEN_GATE_CACHE seeds the model cache — see fresh-install-
# mvv.sh's FRESH_MVV_CACHE for the identical pattern).
#
# Usage: tests/e2e/data-token-cli-gate.sh
# Exit 0 == DATA-TOKEN CLI GATE PASSED (the literal sentinel on the last line).
set -euo pipefail

echo "================================================================"
echo " DATA-TOKEN CLI GATE — sandboxed-HOME real-subprocess journey"
echo " (RDR-005 2a self-minting, nexus-rftfs / nexus-wrwb7 / nexus-ssqk9)"
echo "================================================================"

REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)"
WORK="$(mktemp -d /tmp/dtok-gate-XXXXXX)"
HOME_DIR="$WORK/home"
VENV="$WORK/venv"
LOGS="$WORK/logs"
mkdir -p "$HOME_DIR" "$LOGS"

BIN_DIR=""

# Optional model-cache seed (nexus-nolqs FRESH_MVV_CACHE pattern) — the
# engine binary + PG bundle are still downloaded fresh every run (that
# download+verify IS part of the journey under test); only the 416MB bge
# ONNX cache is worth reusing across iterations.
if [ -n "${DATA_TOKEN_GATE_CACHE:-}" ] && [ -d "$DATA_TOKEN_GATE_CACHE/nexus" ]; then
    mkdir -p "$HOME_DIR/.cache"
    cp -R "$DATA_TOKEN_GATE_CACHE/nexus" "$HOME_DIR/.cache/nexus"
    echo "  (seeded model cache from $DATA_TOKEN_GATE_CACHE)"
fi

GATE_OK=0
_fail() { echo "DATA-TOKEN CLI GATE FAILED: $*" >&2; exit 1; }

cleanup() {
    if [ -n "$BIN_DIR" ] && [ -x "$BIN_DIR/nx" ]; then
        _nx daemon service stop --with-pg >/dev/null 2>&1 || true
    fi
    if [ "$GATE_OK" = 1 ]; then
        rm -rf "$WORK"
    else
        echo "FAILURE EVIDENCE PRESERVED: $LOGS (home: $HOME_DIR)" >&2
    fi
}
trap cleanup EXIT

# ── env allowlist — identical shape to fresh-install-mvv.sh's _nx() ────────
# Deliberately absent: VOYAGE_API_KEY, NX_MINT_TOKEN/NX_MINT_TENANT env
# (those are configured via `nx config set`, exercising config.yml
# resolution — the whole point of this gate over the pytest E2E, which
# configures them via env), NX_SERVICE_TOKEN (present only in
# _nx_poisoned() below).
_nx() {
    env -i \
        HOME="$HOME_DIR" \
        PATH="$BIN_DIR:/usr/bin:/bin" \
        TERM="${TERM:-dumb}" \
        NX_LOCAL=1 \
        ${HTTPS_PROXY:+HTTPS_PROXY="$HTTPS_PROXY"} \
        ${HTTP_PROXY:+HTTP_PROXY="$HTTP_PROXY"} \
        "$BIN_DIR/nx" "$@"
}

# Same allowlist as _nx() plus a poisoned static service_token AND
# NEXUS_LOG_LEVEL=INFO (cli mode defaults to WARNING — see
# src/nexus/logging_setup.py's `mode == "cli"` branch, stderr only, no
# rotating file; INFO is what surfaces data_token_minted/refresh/
# mint_failed on stderr for leg 6's residue count). Used ONLY for
# commands where the whole point is proving the self-minted data token
# is what authenticated (leg 4's round trip, leg 7's negative arm +
# recovery) — NOT for `nx service token issue` (needs the REAL admin
# bearer to mint the mint-locked credential in the first place) and NOT
# for `nx doctor` (its own mint check never even attempts the static
# token — see _check_mint_token in src/nexus/health.py, which discards
# resolve_service_endpoint_with_evidence_gate()'s token entirely and
# calls manager.bearer_for() directly — so poisoning would only risk
# tripping doctor's OTHER, unrelated checks that still resolve via the
# static/lease token, for zero extra proof).
_POISON_SENTINEL="dtok-gate-deliberately-invalid-static-sentinel-$$"
_nx_poisoned() {
    env -i \
        HOME="$HOME_DIR" \
        PATH="$BIN_DIR:/usr/bin:/bin" \
        TERM="${TERM:-dumb}" \
        NX_LOCAL=1 \
        NX_SERVICE_TOKEN="$_POISON_SENTINEL" \
        NEXUS_LOG_LEVEL=INFO \
        ${HTTPS_PROXY:+HTTPS_PROXY="$HTTPS_PROXY"} \
        ${HTTP_PROXY:+HTTP_PROXY="$HTTP_PROXY"} \
        "$BIN_DIR/nx" "$@"
}

echo "── 1/9 Build the wheel under test + virgin venv install ──"
( cd "$REPO_ROOT" && uv build --wheel -o "$WORK/dist" ) >"$LOGS/build.log" 2>&1 \
    || _fail "wheel build failed (see $LOGS/build.log)"
WHEEL="$(ls "$WORK"/dist/conexus-*.whl)"
echo "  $WHEEL"
uv venv --python 3.12 -q "$VENV"
uv pip install -q --python "$VENV/bin/python" "$WHEEL"
BIN_DIR="$VENV/bin"
_nx --version

echo "── 2/9 nx init (local mode, virgin HOME, scrubbed env) ──"
_nx init -y --no-autostart 2>&1 | tee "$LOGS/init.log"
grep -Eq "the service backend is serving" "$LOGS/init.log" \
    || _fail "init did not confirm a serving backend"
if [ -n "${DATA_TOKEN_GATE_CACHE:-}" ] && [ -d "$HOME_DIR/.cache/nexus" ]; then
    mkdir -p "$DATA_TOKEN_GATE_CACHE"
    rm -rf "$DATA_TOKEN_GATE_CACHE/nexus"
    cp -R "$HOME_DIR/.cache/nexus" "$DATA_TOKEN_GATE_CACHE/nexus" 2>/dev/null || true
fi

echo "── 3/9 issue a scope=mint-locked credential bound to tenant 'gate-dtok' ──"
# nexus-ssqk9 tenant-asymmetric shape: "gate-dtok" is deliberately NOT
# "default" — every Http*Store the CLI constructs defaults its own tenant
# kwarg to "default", so mint_tenant (leg 4) is what makes the mint BODY
# carry the credential's real bound tenant instead of the caller's.
# HttpTokenStore()/`nx service token issue` auto-resolves the LOCAL
# supervisor's own bootstrap lease token, which is scope='root' (operator
# authority — TokenStore.SCOPE_ROOT, required server-side for
# scope=mint-locked issuance per TokenAdminHandler.requireOperator), so
# this call must run UNPOISONED (_nx, not _nx_poisoned).
_nx service token issue --tenant gate-dtok --label nexus-rftfs-cli-gate \
    --scope mint-locked >"$WORK/issue.out" 2>"$LOGS/issue.log" \
    || _fail "service token issue --scope mint-locked failed (see $LOGS/issue.log)"
grep -q "^Tenant: gate-dtok$" "$WORK/issue.out" \
    || _fail "issued token was not bound to tenant 'gate-dtok' (see $WORK/issue.out)"
# Extract the raw token WITHOUT ever echoing it — the line after the
# "shown once" banner. Read into a shell var only; never printed, never
# written to a log file from this point on (the $WORK/issue.out capture
# above is the one place it lands on disk, torn down with the rest of
# $WORK on GATE_OK=1; preserved only in the FAILURE EVIDENCE PRESERVED
# path, same as any other leg's raw command output, and this is a
# throwaway local-engine credential with no life outside this run).
MINT_LOCKED_TOKEN="$(sed -n '/^Token (shown once/{n;p;}' "$WORK/issue.out")"
[ -n "$MINT_LOCKED_TOKEN" ] || _fail "could not extract the issued mint-locked token"

echo "── 4/9 nx config set mint_token / mint_tenant — masking assertions ──"
_nx config set mint_token "$MINT_LOCKED_TOKEN" >"$LOGS/config-set-token.log" 2>&1 \
    || _fail "config set mint_token failed"
_nx config set mint_tenant gate-dtok >"$LOGS/config-set-tenant.log" 2>&1 \
    || _fail "config set mint_tenant failed"
if grep -qF "$MINT_LOCKED_TOKEN" "$LOGS/config-set-token.log"; then
    _fail "nx config set mint_token echoed the raw credential to its own output"
fi
GET_TOKEN_MASKED="$(_nx config get mint_token 2>"$LOGS/config-get-token.log")"
if [ "$GET_TOKEN_MASKED" = "$MINT_LOCKED_TOKEN" ]; then
    _fail "nx config get mint_token returned the RAW credential unmasked (nexus-ssqk9 NON_SECRET_CREDENTIALS regression: mint_token must mask, only mint_tenant is exempt)"
fi
GET_TENANT="$(_nx config get mint_tenant 2>"$LOGS/config-get-tenant.log")"
[ "$GET_TENANT" = "gate-dtok" ] \
    || _fail "nx config get mint_tenant did not show the plain unmasked value (got: $GET_TENANT)"
_nx config list >"$LOGS/config-list.log" 2>&1 || _fail "config list failed"
if grep -qF "$MINT_LOCKED_TOKEN" "$LOGS/config-list.log"; then
    _fail "nx config list leaked the raw mint_token credential"
fi
grep -Eq "mint_token +.*\*\*\*" "$LOGS/config-list.log" \
    || _fail "nx config list did not show mint_token in masked form"
grep -Eq "mint_tenant +gate-dtok" "$LOGS/config-list.log" \
    || _fail "nx config list did not show mint_tenant unmasked as 'gate-dtok'"

echo "── 5/9 store put + search round trip — self-minted data token ONLY ──"
# NX_SERVICE_TOKEN is poisoned for these two calls (see _nx_poisoned()'s
# header comment for why this is the strong proof, not the weak one). A
# pass here is only possible via nexus.db.data_token.DataTokenManager
# actually minting and presenting a data token for BOTH the T3 vector
# write/read AND the T2 catalog registration store put also performs.
SENTINEL="dtok-gate-sentinel: self-minted data token round trip ($$)"
echo "$SENTINEL" | _nx_poisoned store put - --title "dtok-gate-sentinel" \
    >"$LOGS/store-put.log" 2>"$LOGS/store-put.stderr.log" \
    || _fail "store put via self-minted data token failed (see $LOGS/store-put.log / .stderr.log) — the mint path did not authenticate"
grep -Eq "Stored: [0-9a-f]{64}" "$LOGS/store-put.log" \
    || _fail "store put did not emit a full-digest doc id"
_nx_poisoned search "self-minted data token round trip" \
    >"$LOGS/search.log" 2>"$LOGS/search.stderr.log" \
    || _fail "search via self-minted data token failed (see $LOGS/search.log / .stderr.log)"
grep -q "dtok-gate-sentinel" "$LOGS/search.log" \
    || _fail "search did not return the sentinel stored via the self-minted data token"
# Non-vacuity for the poisoning itself: confirm the sentinel value never
# appears verbatim as a WORKING credential elsewhere (a no-op poison —
# e.g. an env var name typo — would still pass the two assertions above
# via the lease token and prove nothing). We cannot assert a negative
# HTTP outcome after the fact, so instead assert the mint actually fired
# by grepping the stderr this leg captured at NEXUS_LOG_LEVEL=INFO.
grep -q "data_token_minted" "$LOGS/store-put.stderr.log" \
    || _fail "store put's own log shows no data_token_minted event — the self-mint path was not exercised (poisoning may be a no-op)"
grep -q "data_token_mint_failed" "$LOGS/store-put.stderr.log" \
    && _fail "store put logged a data_token_mint_failed event despite the round trip reporting success — investigate before trusting this leg"

echo "── 6/9 nx doctor: mint check green, live round trip + TTL ──"
# Runs UNPOISONED (_nx, not _nx_poisoned) — see _nx_poisoned()'s header
# comment: _check_mint_token() never attempts the static/lease token at
# all, so poisoning here would only risk failing doctor's UNRELATED
# checks for zero extra proof on this one.
_nx doctor >"$LOGS/doctor.log" 2>&1 || _fail "doctor exited non-zero"
if grep -q "✗" "$LOGS/doctor.log"; then
    grep "✗" "$LOGS/doctor.log" >&2
    _fail "doctor shows a red ✗ after mint_token/mint_tenant provisioning"
fi
DOCTOR_MINT_LINE="$(grep "Data-token self-minting (mint_token)" "$LOGS/doctor.log" || true)"
[ -n "$DOCTOR_MINT_LINE" ] \
    || _fail "doctor did not print the Data-token self-minting (mint_token) check line at all"
echo "  $DOCTOR_MINT_LINE"
# Non-vacuity: the check must NOT have taken its unconfigured skip
# branch (src/nexus/health.py _check_mint_token: "not configured —
# self-minting inactive") — a gate that silently skip-passed the one
# check it exists to exercise must fail loud.
# nexus-i66g4/6zxfb/wbeyi class: never pipe an already-captured variable
# into an early-exit grep -q consumer under set -o pipefail — bash-native
# [[ =~ ]] / [[ == * ]] instead (this file's own lint:
# tests/test_pipefail_early_exit_consumer_lint.py).
if [[ "$DOCTOR_MINT_LINE" == *"not configured"* ]]; then
    _fail "doctor's mint check took the UNCONFIGURED skip branch — mint_token did not take effect (config resolution bug)"
fi
_DOCTOR_TTL_RE='data token via .+\(granted TTL [0-9.]+s\)'
[[ "$DOCTOR_MINT_LINE" =~ $_DOCTOR_TTL_RE ]] \
    || _fail "doctor's mint check line did not report a granted TTL round trip (got: $DOCTOR_MINT_LINE)"
[[ "$DOCTOR_MINT_LINE" == *"✓"* ]] \
    || _fail "doctor's mint check line is not ok=True (got: $DOCTOR_MINT_LINE)"

echo "── 7/9 cross-process lease reuse (nexus-9c7t9) ──"
# Leg 5 (store put) minted and published the cross-process lease file for
# (base_url, tenant="default"). Leg 6's `nx doctor` above is itself a
# FRESH subprocess with an EMPTY in-process cache — its own mint check
# should therefore have BORROWED that lease rather than minting again.
# Assert the wording directly rather than trusting leg 6's looser
# "✓ + granted TTL" checks to have caught a regression here.
[[ "$DOCTOR_MINT_LINE" == *"reused the cached (lease file)"* ]] \
    || _fail "doctor did not report reusing the cross-process lease file (got: $DOCTOR_MINT_LINE) — the cross-process cache did not take effect, or doctor minted its own token instead of borrowing store put's"
if [[ "$DOCTOR_MINT_LINE" == *"minted a fresh"* ]]; then
    _fail "doctor minted a FRESH token instead of reusing the lease store put (leg 5) already published — nexus-9c7t9 regression"
fi

# The lease file itself: exists, tight perms, never the raw credential.
LEASE_FILES=("$HOME_DIR"/.config/nexus/data_token_lease.*)
[ -e "${LEASE_FILES[0]}" ] \
    || _fail "no data_token_lease.* file found under \$HOME/.config/nexus after store put + doctor"
LEASE_FILE="${LEASE_FILES[0]}"
LEASE_PERMS="$(stat -f '%Lp' "$LEASE_FILE" 2>/dev/null || stat -c '%a' "$LEASE_FILE" 2>/dev/null)"
[ "$LEASE_PERMS" = "600" ] \
    || _fail "lease file $LEASE_FILE has perms $LEASE_PERMS, expected 600"
if grep -qF "$MINT_LOCKED_TOKEN" "$LEASE_FILE"; then
    _fail "lease file $LEASE_FILE contains the raw mint-locked CREDENTIAL — the lease must hold only the short-TTL data token, never the credential used to mint it"
fi

# A THIRD subprocess (another poisoned search) must ALSO borrow the lease
# — no new mint, a lease-reuse event on its own stderr.
_nx_poisoned search "self-minted data token round trip" \
    >"$LOGS/second-search.log" 2>"$LOGS/second-search.stderr.log" \
    || _fail "second search (cross-process lease reuse leg) failed (see $LOGS/second-search.log / .stderr.log)"
grep -q "dtok-gate-sentinel" "$LOGS/second-search.log" \
    || _fail "second search did not return the sentinel"
if grep -q "data_token_minted" "$LOGS/second-search.stderr.log"; then
    _fail "second search minted a NEW token instead of reusing the cross-process lease (see $LOGS/second-search.stderr.log)"
fi
grep -q "data_token_lease_reused" "$LOGS/second-search.stderr.log" \
    || _fail "second search's stderr shows no data_token_lease_reused event — the cross-process borrow was not exercised (see $LOGS/second-search.stderr.log)"

echo "── 8/9 residue discipline (nexus-lgiqw) — at the CLI-subprocess granularity ──"
# HISTORY, updated for nexus-9c7t9: before the cross-process lease-file
# cache (leg 7, above), EVERY `nx <cmd>` invocation in this script minted
# its OWN data token — store put, search, doctor, and every subsequent
# command in this script each ran a fresh Python interpreter with an
# empty in-process cache and no way to see another process's mint. That
# is what made "assert EXACTLY ONE mint occurred across ALL the commands
# in this script" unachievable, and it is what nexus-9c7t9 exists to fix
# — leg 7 above now demonstrates doctor and a second search BOTH reusing
# store put's lease instead of minting fresh.
#
# This leg keeps the ORIGINAL, narrower residue-discipline invariant
# nexus-lgiqw actually documents (and that the pytest E2E at
# tests/db/test_data_token_manager_e2e.py proves): one live token per
# (base_url, tenant), reused across every engine call a SINGLE process
# makes, never re-minted per call, regardless of what the cross-process
# lease cache does or does not do. That is provable from outside a
# subprocess via its own stderr at NEXUS_LOG_LEVEL=INFO — which is
# exactly what leg 5 already captured and asserted (data_token_minted
# present, data_token_mint_failed absent) for store put, a command that
# makes at least two authenticated engine round trips in one process (T2
# catalog registration + T3 vector write) via the SAME manager
# singleton. This leg makes that assertion explicit and adds the
# exact-count form: exactly ONE data_token_minted event for store put's
# process, never two-or-more (which would mean the singleton was NOT
# being reused across store put's own internal calls — the actual
# regression nexus-lgiqw guards against). Store put is also still the
# FIRST engine-touching command in the whole script, so no lease exists
# yet when it runs — its own mint count is unaffected by leg 7's
# cross-process borrowing landing afterward.
STORE_PUT_MINT_COUNT="$(grep -c "data_token_minted" "$LOGS/store-put.stderr.log" || true)"
[ "$STORE_PUT_MINT_COUNT" = "1" ] \
    || _fail "store put minted $STORE_PUT_MINT_COUNT times in one process (expected exactly 1 — nexus-lgiqw residue-discipline regression: the process-wide DataTokenManager singleton was re-minting per engine call instead of reusing its cache)"
STORE_PUT_REFRESH_COUNT="$(grep -c "data_token_refresh" "$LOGS/store-put.stderr.log" || true)"
[ "$STORE_PUT_REFRESH_COUNT" = "0" ] \
    || _fail "store put logged $STORE_PUT_REFRESH_COUNT unexpected data_token_refresh event(s) — a fresh 3600s-TTL token should never need refreshing within one short-lived CLI process"

echo "── 9/9 negative arm: wrong mint_tenant -> loud 403, then recovery ──"
# nexus-9c7t9: a FRESH, unexpired lease for (base_url, tenant="default")
# already exists at this point (leg 7 proved it) — without removing it, a
# poisoned search below would silently BORROW that valid lease instead of
# ever attempting a mint, and the negative arm would prove nothing (the
# wrong mint_tenant only matters for an ACTUAL mint attempt's request
# body, never consulted on a lease-file cache hit). Delete it first so
# the next search is a genuine cache miss that must mint fresh under the
# now-wrong mint_tenant.
rm -f "$HOME_DIR"/.config/nexus/data_token_lease.* \
    || _fail "could not remove the lease file ahead of the negative arm"
_nx config set mint_tenant wrong-tenant >"$LOGS/config-set-wrong-tenant.log" 2>&1 \
    || _fail "config set mint_tenant wrong-tenant failed"
NEG_RC=0
_nx_poisoned search "self-minted data token round trip" \
    >"$LOGS/negative.log" 2>"$LOGS/negative.stderr.log" || NEG_RC=$?
[ "$NEG_RC" -ne 0 ] \
    || _fail "search with a cross-tenant mint_tenant should have failed loud but exited 0"
NEG_COMBINED="$LOGS/negative.combined.log"
cat "$LOGS/negative.log" "$LOGS/negative.stderr.log" > "$NEG_COMBINED" 2>/dev/null || true
grep -q "403" "$NEG_COMBINED" \
    || _fail "cross-tenant mint failure did not surface '403' anywhere in output (see $NEG_COMBINED)"
grep -q "wrong-tenant" "$NEG_COMBINED" \
    || _fail "cross-tenant mint failure did not name the misconfigured tenant 'wrong-tenant' (see $NEG_COMBINED)"
grep -q "nx config set mint_tenant" "$NEG_COMBINED" \
    || _fail "cross-tenant mint failure did not surface the teaching remedy ('nx config set mint_tenant ...') (see $NEG_COMBINED)"
if grep -qF "$MINT_LOCKED_TOKEN" "$NEG_COMBINED"; then
    _fail "cross-tenant mint failure leaked the raw mint-locked credential into its error output"
fi

# Restore + prove recovery — one more round trip must succeed.
_nx config set mint_tenant gate-dtok >"$LOGS/config-set-restore.log" 2>&1 \
    || _fail "config set mint_tenant gate-dtok (restore) failed"
_nx_poisoned search "self-minted data token round trip" \
    >"$LOGS/recovery.log" 2>"$LOGS/recovery.stderr.log" \
    || _fail "recovery round trip after restoring mint_tenant failed (see $LOGS/recovery.log / .stderr.log)"
grep -q "dtok-gate-sentinel" "$LOGS/recovery.log" \
    || _fail "recovery round trip did not return the sentinel"

echo "── non-vacuity ──"
# issue.log (stderr-only capture of `nx service token issue`) is
# EXPECTED empty on success — it is not a useful non-vacuity signal for
# that leg, which already has its own explicit assertion above (the
# "^Tenant: gate-dtok$" grep against $WORK/issue.out). Check issue.out
# here instead, from the directory it actually lands in.
LEGS_TO_CHECK="build.log init.log config-set-token.log store-put.log store-put.stderr.log search.log doctor.log negative.combined.log recovery.log"
for f in $LEGS_TO_CHECK; do
    [ -s "$LOGS/$f" ] || _fail "leg log $f is empty — a journey leg silently skipped"
done
[ -s "$WORK/issue.out" ] || _fail "issue.out is empty — the token-issue leg silently skipped"

GATE_OK=1
VERSION_ALL="$(_nx --version 2>/dev/null | grep -oE '[0-9]+\.[0-9]+\.[0-9]+')"
VERSION_STRING="${VERSION_ALL%%$'\n'*}"
echo "DATA-TOKEN CLI GATE PASSED — conexus $VERSION_STRING (local wheel, sandboxed-HOME real-subprocess journey)"
