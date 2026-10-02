#!/usr/bin/env bash
# SPDX-License-Identifier: AGPL-3.0-or-later
#
# nexus-aq9y8 / nexus-27i0m: Developer ID codesign + notarization of a bare
# Mach-O, shared by engine-service-release.yml (the real release) and
# mac-signing-rehearsal.yml (the same bytes, exercised off-release). Keeping
# the mechanics here is what lets the rehearsal prove the release path: the
# first run of the old inline YAML was the v0.1.142 release itself, and it
# failed there ("1 identity imported" followed by "no identity found").
#
#   mac-sign.sh sign <binary>       needs APPLE_DEV_ID_CERT_P12 (base64),
#                                   APPLE_DEV_ID_CERT_PASSWORD, APPLE_DEV_ID_IDENTITY
#   mac-sign.sh notarize <binary>   needs APPLE_NOTARY_KEY_P8 (base64),
#                                   APPLE_NOTARY_KEY_ID, APPLE_NOTARY_ISSUER_ID
#   mac-sign.sh cleanup             deletes the temp keychain and restores the
#                                   user keychain search list; always safe
#
# SIGN_WORKDIR (required) holds the temp keychain and the saved search list
# between the sign and cleanup calls; the workflows pass $RUNNER_TEMP.
#
# Output contract: stdout carries exactly ONE line, the outcome summary the
# release workflow copies into the step summary. Everything else (tool
# chatter, the notarytool transcript, diagnostics) goes to stderr, so a caller
# that captures stdout can never swallow the evidence of a failure.
#
# Secret-presence policy (absent → warn, partial → fail, APPLE_SIGNING_REQUIRED)
# stays in the release workflow; this script assumes its inputs are present.
#
# Why each keychain step is shaped the way it is (the hellmini runner is a
# LaunchDaemon for a user who has never logged in — no login keychain, and no
# guarantee the machine's System keychain holds the right intermediate):
#   * The G2 intermediate is imported INTO the temp keychain from a vendored,
#     SHA-256-pinned copy. codesign only offers an identity whose chain it can
#     build to a trusted root; a G2 leaf without the G2 intermediate is not an
#     identity at all ("no identity found"), and relying on whatever the runner
#     machine happens to have installed is ambient state.
#   * The temp keychain is PREPENDED to the existing user search list, and the
#     original list is saved and restored. The old step replaced the list with
#     "$KEYCHAIN login.keychain-db", and its cleanup left ghrunner's list
#     pointing at a login keychain that does not exist.
#   * codesign gets --keychain, so identity lookup does not depend on the
#     search list at all.
#   * A preflight `security find-identity -v` must show the identity before
#     codesign runs. On a miss it prints the unfiltered listing, which names
#     the reason an identity is invalid (e.g. CSSMERR_TP_NOT_TRUSTED).
set -euo pipefail
umask 077

HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
ENTITLEMENTS="$HERE/mac-entitlements.plist"
G2_CER="$HERE/apple/DeveloperIDG2CA.cer"
# apple.com/certificateauthority/DeveloperIDG2CA.cer — CN=Developer ID
# Certification Authority, OU=G2; issuer Apple Root CA; valid to 2031-09-17.
G2_SHA256="f16cd3c54c7f83cea4bf1a3e6a0819c8aaa8e4a1528fd144715f350643d2df3a"

die() { echo "FAIL: $*" >&2; exit 1; }

: "${SIGN_WORKDIR:?SIGN_WORKDIR must name a scratch directory (the workflows pass \$RUNNER_TEMP)}"
KEYCHAIN="$SIGN_WORKDIR/sign.keychain-db"
SAVED_LIST="$SIGN_WORKDIR/sign.searchlist.orig"

# Prints the current user-domain keychain search list, one path per line.
user_search_list() {
  local line
  while IFS= read -r line; do
    line="${line#"${line%%[![:space:]]*}"}"
    line="${line#\"}"
    line="${line%\"}"
    [ -n "$line" ] && printf '%s\n' "$line"
  done < <(security list-keychains -d user)
}

team_identifier() {
  local info
  info="$(codesign -dv "$1" 2>&1 || true)"
  if [[ "$info" =~ TeamIdentifier=([^[:space:]]+) ]] && [ "${BASH_REMATCH[1]}" != "not" ]; then
    printf '%s\n' "${BASH_REMATCH[1]}"
  fi
}

cmd_sign() {
  local bin="${1:?usage: mac-sign.sh sign <binary>}"
  : "${APPLE_DEV_ID_CERT_P12:?}" "${APPLE_DEV_ID_CERT_PASSWORD:?}" "${APPLE_DEV_ID_IDENTITY:?}"
  [ -f "$bin" ] || die "no binary at $bin"
  [ -f "$ENTITLEMENTS" ] || die "entitlements missing at $ENTITLEMENTS"

  local got
  got="$(shasum -a 256 "$G2_CER")"
  got="${got%% *}"
  [ "$got" = "$G2_SHA256" ] || die "vendored G2 intermediate hash mismatch ($got) — refusing to trust it"

  local keypass
  keypass="$(uuidgen)"
  security create-keychain -p "$keypass" "$KEYCHAIN"
  security set-keychain-settings -lut 900 "$KEYCHAIN"
  security unlock-keychain -p "$keypass" "$KEYCHAIN"

  local p12="$SIGN_WORKDIR/devid.p12"
  printf '%s' "$APPLE_DEV_ID_CERT_P12" | base64 -d > "$p12"
  security import "$p12" -k "$KEYCHAIN" -P "$APPLE_DEV_ID_CERT_PASSWORD" -T /usr/bin/codesign >&2
  rm -f "$p12"
  security import "$G2_CER" -k "$KEYCHAIN" >&2
  security set-key-partition-list -S "apple-tool:,apple:" -s -k "$keypass" "$KEYCHAIN" >/dev/null

  local -a orig=()
  local kc
  while IFS= read -r kc; do orig+=("$kc"); done < <(user_search_list)
  [ -f "$SAVED_LIST" ] || { [ "${#orig[@]}" -eq 0 ] || printf '%s\n' "${orig[@]}"; } > "$SAVED_LIST"
  security list-keychains -d user -s "$KEYCHAIN" ${orig[@]+"${orig[@]}"} >&2

  local valid
  valid="$(security find-identity -v -p codesigning "$KEYCHAIN")"
  if [[ "$valid" != *"\"$APPLE_DEV_ID_IDENTITY\""* ]]; then
    echo "--- security find-identity -v -p codesigning (valid only) ---" >&2
    printf '%s\n' "$valid" >&2
    echo "--- security find-identity -p codesigning (all, with reasons) ---" >&2
    security find-identity -p codesigning "$KEYCHAIN" >&2 || true
    die "the imported certificate is not a valid codesigning identity in $KEYCHAIN (see the reasons above)"
  fi

  # --entitlements is LOAD-BEARING (critique 96677bf7 Critical / nexus-2oh5q):
  # Hardened Runtime implies Library Validation, which refuses the bundled
  # onnxruntime/DJL dylibs the binary System.load()s for local-mode embedding.
  codesign --force --options runtime --timestamp --keychain "$KEYCHAIN" \
    --entitlements "$ENTITLEMENTS" \
    --sign "$APPLE_DEV_ID_IDENTITY" "$bin"
  codesign --verify --strict --verbose=2 "$bin" >&2
  # Non-vacuity: a real team identity must have landed; an ad-hoc result
  # here means the sign silently failed.
  local team
  team="$(team_identifier "$bin")"
  [ -n "$team" ] || die "TeamIdentifier not set after codesign — ad-hoc signature survived"
  echo "Developer ID SIGNED (TeamIdentifier=$team)"
}

cmd_notarize() {
  local bin="${1:?usage: mac-sign.sh notarize <binary>}"
  : "${APPLE_NOTARY_KEY_P8:?}" "${APPLE_NOTARY_KEY_ID:?}" "${APPLE_NOTARY_ISSUER_ID:?}"
  # Notarizing an ad-hoc binary is a guaranteed Apple-side rejection minutes
  # later — fail here with the real reason instead.
  [ -n "$(team_identifier "$bin")" ] \
    || die "binary is not Developer-ID signed (are APPLE_DEV_ID_* secrets provisioned?) — cannot notarize"
  local key="$SIGN_WORKDIR/notary.p8" zip="$SIGN_WORKDIR/notarize.zip"
  printf '%s' "$APPLE_NOTARY_KEY_P8" | base64 -d > "$key"
  /usr/bin/ditto -c -k "$bin" "$zip"
  # The transcript streams to stderr live (the wait can run 20 minutes) and is
  # kept in a file for the verdict below.
  local transcript="$SIGN_WORKDIR/notary.out" rc
  set +e
  xcrun notarytool submit "$zip" \
    --key "$key" --key-id "$APPLE_NOTARY_KEY_ID" --issuer "$APPLE_NOTARY_ISSUER_ID" \
    --wait --timeout 20m 2>&1 | tee "$transcript" >&2
  rc=${PIPESTATUS[0]}
  set -e
  local out
  out="$(cat "$transcript")"
  rm -f "$transcript"
  # notarytool can exit 0 with a terminal status other than Accepted; the
  # final "status:" line is the verdict, not the exit code. Anchored to a
  # whole line so a progress line ("Current status: ...") cannot match.
  local nl=$'\n'
  if [ "$rc" -ne 0 ] || [[ ! "$nl$out$nl" =~ ${nl}[[:space:]]*status:\ Accepted[[:space:]]*${nl} ]]; then
    if [[ "$out" =~ id:[[:space:]]*([0-9a-f-]{36}) ]]; then
      xcrun notarytool log "${BASH_REMATCH[1]}" \
        --key "$key" --key-id "$APPLE_NOTARY_KEY_ID" --issuer "$APPLE_NOTARY_ISSUER_ID" >&2 || true
    fi
    rm -f "$key" "$zip"
    die "notarization not accepted (notarytool rc=$rc)"
  fi
  rm -f "$key" "$zip"
  echo "notarization ACCEPTED (ticket online — bare Mach-O is not stapleable)"
}

cmd_cleanup() {
  security delete-keychain "$KEYCHAIN" 2>/dev/null || true
  rm -f "$SIGN_WORKDIR/devid.p12" "$SIGN_WORKDIR/notary.p8" "$SIGN_WORKDIR/notarize.zip" "$SIGN_WORKDIR/notary.out"
  if [ -f "$SAVED_LIST" ]; then
    local -a orig=()
    local kc
    while IFS= read -r kc; do [ -n "$kc" ] && orig+=("$kc"); done < "$SAVED_LIST"
    security list-keychains -d user -s ${orig[@]+"${orig[@]}"} >&2 || true
    rm -f "$SAVED_LIST"
  fi
}

case "${1:-}" in
  sign) shift; cmd_sign "$@" ;;
  notarize) shift; cmd_notarize "$@" ;;
  cleanup) cmd_cleanup ;;
  *) echo "usage: mac-sign.sh {sign <binary>|notarize <binary>|cleanup}" >&2; exit 2 ;;
esac
