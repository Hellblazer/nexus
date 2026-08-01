#!/usr/bin/env bash
# SPDX-License-Identifier: AGPL-3.0-or-later
#
# nexus-2oh5q: the mac-arm64 SIGNED binary actually runs, and its bundled JNI
# libraries still load under Hardened Runtime.
#
# WHY THIS EXISTS, AND WHY IT CANNOT BE A CI JOB
# ----------------------------------------------
# Developer-ID signing with `--options runtime` turns ON macOS Library
# Validation: any dylib the process dlopen()s at runtime must be signed by the
# SAME Team ID or be an Apple system library. The engine bundles TWO third-party
# native libs as native-image resources — onnxruntime-java and the DJL
# HuggingFace tokenizers lib — extracted to a temp file and System.load()ed when
# local-mode embedding initialises. Without
# `com.apple.security.cs.disable-library-validation` those loads are REFUSED and
# the SIGNED binary crashes exactly where the ad-hoc one worked, i.e. a properly
# signed artifact that is STRICTLY WORSE than the unsigned status quo it
# replaced (precedent: jnr-ffi#257, zstd-jni#321).
#
# The entitlement is shipped (service/deploy/mac-entitlements.plist) and its
# presence is shape-pinned by tests/test_engine_release_workflow_signing.py. But
# a shape pin proves the FLAG is passed to codesign — it cannot prove the loads
# succeed. Only executing a signed binary on real mac hardware does that, and:
#
#   * `codesign --verify` CANNOT see this. It validates the signature, not
#     whether a dlopen at runtime will be permitted.
#   * the release workflow's mac-arm64 leg is `smoke: false` — macos-14 runners
#     have no Docker, so native-smoke.sh cannot run there. The mac binary is
#     never booted in CI at all.
#   * codesign runs AFTER the (linux-only) smoke in the job, so even if that
#     changed, the smoked bytes would be the pre-signature ones.
#
# So this gate is manual, runs on an arm64 Mac, and is the ONLY thing standing
# between a signed release and a mac-arm64 install whose first `nx init
# --service` embed dies with UnsatisfiedLinkError.
#
# RUN IT AFTER the first signed tag publishes, BEFORE setting the repo variable
# APPLE_SIGNING_REQUIRED=true. Passing here is what earns that flag.
#
#   NEXUS_SERVICE_TAG=engine-service-v0.1.59 tests/e2e/mac-signed-binary-gate.sh
#
# FAILS LOUD, NEVER SKIP-PASSES. Every precondition (wrong arch, missing model,
# absent Docker) is an ERROR, not a skip: a gate for a signing hazard that
# quietly reports success without loading a single JNI library is worse than no
# gate, because it would be cited as evidence.
set -euo pipefail

fail=0
note() { printf '  %s\n' "$*"; }
ok()   { printf '  \033[32mok\033[0m   %s\n' "$*"; }
bad()  { printf '  \033[31mFAIL\033[0m %s\n' "$*"; fail=1; }
die()  { printf '\033[31mERROR\033[0m %s\n' "$*" >&2; exit 2; }

TAG="${NEXUS_SERVICE_TAG:-}"
[ -n "$TAG" ] || die "NEXUS_SERVICE_TAG is required (e.g. engine-service-v0.1.59).
The point of this gate is a SPECIFIC published artifact — never a default."

REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)"

# ── Preconditions ────────────────────────────────────────────────────────────
printf '\033[1m== Preconditions ==\033[0m\n'

[ "$(uname -s)" = "Darwin" ] || die "this gate must run on macOS — Library Validation is a macOS mechanism"
[ "$(uname -m)" = "arm64" ]  || die "this gate must run on arm64 — the published artifact is mac-arm64"
ok "arm64 macOS host"

command -v gh       >/dev/null || die "gh is required to download the release asset"
command -v codesign >/dev/null || die "codesign is required (install Xcode command line tools)"
command -v spctl    >/dev/null || die "spctl is required"
command -v docker   >/dev/null || die "docker is required — native-smoke.sh boots a throwaway pgvector"
docker info >/dev/null 2>&1    || die "docker is installed but not running — start Docker Desktop"
ok "gh / codesign / spctl / docker present"

# The embed is the WHOLE POINT of this gate. native-smoke.sh WARN+skips its bge
# section when the model is absent, which here would mean passing without ever
# touching the JNI libs under test. Require it up front with the remedy.
BGE_MODEL="${NX_BGE_MODEL_PATH:-$HOME/.cache/nexus/onnx_models/bge-base-en-v1.5/onnx/model.onnx}"
[ -f "$BGE_MODEL" ] || die "bge-768 ONNX model absent at:
  $BGE_MODEL
This gate CANNOT run without it — the embed path is precisely what Library
Validation would refuse, so a model-less run would prove nothing while
reporting success. Provision with 'nx init --service' or set NX_BGE_MODEL_PATH."
ok "bge-768 model present ($(du -h "$BGE_MODEL" | cut -f1))"

WORK="$(mktemp -d)"
trap 'rm -rf "$WORK"' EXIT
BIN="$WORK/nexus-service"

# ── Acquire the published artifact ───────────────────────────────────────────
printf '\n\033[1m== Acquire published mac-arm64 artifact (%s) ==\033[0m\n' "$TAG"

gh release download "$TAG" --repo Hellblazer/nexus \
  --pattern 'nexus-service-mac-arm64' --output "$BIN" --clobber \
  || die "could not download nexus-service-mac-arm64 from $TAG — has the release published?"
chmod +x "$BIN"
ok "downloaded $(du -h "$BIN" | cut -f1) to $BIN"

# Simulate a BROWSER download. `nx daemon service install-binary` fetches via the
# API, which sets no com.apple.quarantine xattr, so Gatekeeper never adjudicates
# and the notarization ticket is never checked — which is exactly why this
# hazard has never bitten anyone yet. Setting the xattr makes this gate test the
# case notarization exists to serve.
xattr -w com.apple.quarantine "0081;$(printf '%x' "$(date +%s)");Safari;$(uuidgen)" "$BIN"
xattr -p com.apple.quarantine "$BIN" >/dev/null 2>&1 \
  && ok "quarantine xattr applied (browser-download simulation)" \
  || bad "quarantine xattr did not stick — Gatekeeper will not adjudicate"

# ── Signature shape ──────────────────────────────────────────────────────────
printf '\n\033[1m== Signature ==\033[0m\n'

codesign --verify --strict --verbose=2 "$BIN" 2>/dev/null \
  && ok "codesign --verify --strict" \
  || bad "codesign --verify --strict rejected the binary"

SIGINFO="$(codesign -dv --verbose=4 "$BIN" 2>&1 || true)"

if echo "$SIGINFO" | grep -q "TeamIdentifier=" && ! echo "$SIGINFO" | grep -q "TeamIdentifier=not set"; then
  ok "Developer ID signed ($(echo "$SIGINFO" | grep -o 'TeamIdentifier=[^ ]*' | head -1))"
else
  bad "ad-hoc signature — TeamIdentifier not set. APPLE_DEV_ID_* secrets were not
       provisioned for this tag, so there is no Hardened Runtime and this gate
       proves nothing about the signed configuration."
fi

# Hardened Runtime is what ENABLES Library Validation. If it is off, the rest of
# this gate is vacuous — the JNI loads would succeed for the wrong reason.
if echo "$SIGINFO" | grep -qE "flags=.*runtime"; then
  ok "Hardened Runtime enabled (Library Validation is therefore ACTIVE)"
else
  bad "Hardened Runtime NOT enabled — the embed below would pass for the wrong
       reason. This gate is only meaningful against a runtime-hardened binary."
fi

ENTS="$(codesign -d --entitlements - --xml "$BIN" 2>/dev/null || true)"
if echo "$ENTS" | grep -q "com.apple.security.cs.disable-library-validation"; then
  ok "disable-library-validation entitlement present"
else
  bad "disable-library-validation entitlement MISSING — the bundled onnxruntime /
       DJL tokenizer dylibs will be refused at System.load(). See
       service/deploy/mac-entitlements.plist."
fi

# ── Gatekeeper (notarization, checked ONLINE) ────────────────────────────────
printf '\n\033[1m== Gatekeeper assessment ==\033[0m\n'
# Deliberately BEFORE any exec: a quarantined, un-notarized binary is killed on
# first exec, and asking spctl first gives a clean verdict instead of an opaque
# "Killed: 9" further down.
if spctl -a -t exec -vv "$BIN" 2>&1 | tee "$WORK/spctl.out" | grep -q "accepted"; then
  ok "spctl accepted ($(grep -o 'source=.*' "$WORK/spctl.out" | head -1))"
else
  bad "spctl REJECTED the quarantined binary: $(cat "$WORK/spctl.out")
       A browser-downloaded copy will not run. Notarization missing or failed."
fi

# ── THE LOAD-BEARING PART: boot it and force a real embed ────────────────────
printf '\n\033[1m== Runtime: boot the signed binary and drive the JNI embed ==\033[0m\n'
note "reusing service/native-smoke.sh (BIN override) — its bge-768 section drives"
note "the DJL tokenizer JNI + onnxruntime session, the exact loads at risk."

SMOKE_LOG="$WORK/native-smoke.log"
set +e
( cd "$REPO_ROOT/service" && BIN="$BIN" ./native-smoke.sh ) >"$SMOKE_LOG" 2>&1
smoke_rc=$?
set -e

if [ $smoke_rc -eq 0 ]; then
  ok "native-smoke.sh exited 0 against the SIGNED binary"
else
  bad "native-smoke.sh exited $smoke_rc against the signed binary"
fi

# NON-VACUITY: exit 0 is not enough. The embed section WARN+skips when the model
# is absent, and a skip must never read as "embed covered" — that is the whole
# failure mode this gate exists to prevent.
if grep -q "ok   embed (DJL tokenizer JNI + onnx run)" "$SMOKE_LOG"; then
  ok "embed executed: DJL tokenizer JNI + onnxruntime loaded and returned 768-dim"
elif grep -q "WARN embed path NOT covered" "$SMOKE_LOG"; then
  bad "the embed was SKIPPED, so no JNI library was loaded and this gate proved
       NOTHING about Library Validation. Precondition check should have caught
       this — investigate before trusting any earlier green run."
else
  bad "could not find the embed verdict in the smoke log — native-smoke.sh's bge
       section changed shape; re-point this assertion rather than dropping it."
fi

# The specific dyld/JVM signatures of a library-validation refusal, called out by
# name so a failure reads as "this is the 2oh5q hazard" rather than generic.
if grep -qiE "not valid for use in process|library validation|UnsatisfiedLinkError|code signature.*invalid" "$SMOKE_LOG"; then
  bad "LIBRARY VALIDATION REFUSAL detected in the smoke log — this IS nexus-2oh5q:
$(grep -iE 'not valid for use in process|library validation|UnsatisfiedLinkError|code signature.*invalid' "$SMOKE_LOG" | head -3)"
else
  ok "no library-validation refusal in the service log"
fi

# ── Verdict ──────────────────────────────────────────────────────────────────
printf '\n\033[1m== Verdict ==\033[0m\n'
if [ "$fail" -eq 0 ]; then
  printf '\033[32mMAC SIGNED-BINARY GATE PASSED\033[0m — %s runs quarantined on arm64 macOS\n' "$TAG"
  printf 'with Hardened Runtime active and the bundled JNI libraries loading.\n'
  printf 'This is what earns APPLE_SIGNING_REQUIRED=true (nexus-2oh5q).\n'
  exit 0
fi
printf '\033[31mMAC SIGNED-BINARY GATE FAILED\033[0m — do NOT set APPLE_SIGNING_REQUIRED=true.\n'
printf 'Full smoke log: %s (copied to /tmp/mac-signed-gate.log)\n' "$SMOKE_LOG"
cp "$SMOKE_LOG" /tmp/mac-signed-gate.log 2>/dev/null || true
exit 1
