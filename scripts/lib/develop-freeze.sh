#!/usr/bin/env bash
# shellcheck disable=SC2034  # the FREEZE_* results are read by the sourcing script
# SPDX-License-Identifier: AGPL-3.0-or-later
#
# scripts/lib/develop-freeze.sh: read the develop freeze off the tuple space.
# Sourced, never executed. Shared by scripts/develop-freeze.sh (status) and
# scripts/git-push-develop.sh (the refusal), so the two cannot disagree about
# what "frozen" means (nexus-eusu6).
#
# The freeze is one topic on the EXISTING board/<topic> template:
# board/develop-freeze, body a small JSON object
#   {"state":"frozen"|"open","holder":"...","reason":"...","set_at":"<iso>"}
# The NEWEST post decides. No posts at all is open. Board rows expire on their
# own (7-day retention ceiling), which is the bound on a crashed owner: a
# freeze nobody clears lapses to "open" rather than wedging develop forever.
#
# freeze_read <nx-bin> sets these globals and returns:
#   0  read succeeded: FREEZE_STATE is "open" or "frozen"
#   1  the board could not be read (nx failed), FREEZE_ERR holds why
#   2  the newest post is malformed (not JSON / unknown state), FREEZE_ERR
#      holds why. Fails closed on purpose: anyone in the tenant can post to a
#      board, and a junk post must not read as "open".
FREEZE_SUBSPACE="board/develop-freeze"
FREEZE_STATE=""
FREEZE_HOLDER=""
FREEZE_REASON=""
FREEZE_AGE=""
FREEZE_ERR=""

freeze_read() {
  local nxbin="$1" json parsed errf rc=0 limit="${FREEZE_READ_TIMEOUT:-20}"
  FREEZE_STATE=""; FREEZE_HOLDER=""; FREEZE_REASON=""; FREEZE_AGE=""; FREEZE_ERR=""
  # stdout only is parsed: a warning on stderr with rc 0 must not break the
  # JSON (review finding). The read is bounded: a HUNG tuple space (the case
  # NX_PUSH_SKIP_LOCK exists for) must not hang the push either way. perl's
  # alarm survives exec and is on every macOS and linux host; GNU timeout is not.
  errf="$(mktemp "${TMPDIR:-/tmp}/develop-freeze-rd.XXXXXX")"
  if json="$(perl -e 'alarm shift; exec @ARGV or exit 127' "$limit" \
      "$nxbin" tuple rd "$FREEZE_SUBSPACE" --newest -n 1 --json 2>"$errf")"; then
    rc=0
  else
    rc=$?
  fi
  FREEZE_ERR="$(cat "$errf" 2>/dev/null || true)"
  rm -f "$errf"
  if [[ $rc -ne 0 ]]; then
    [[ $rc -eq 142 ]] && FREEZE_ERR="timed out after ${limit}s reading $FREEZE_SUBSPACE${FREEZE_ERR:+: $FREEZE_ERR}"
    [[ -n "$FREEZE_ERR" ]] || FREEZE_ERR="nx tuple rd exited $rc with no message"
    return 1
  fi
  FREEZE_ERR=""
  # One field per line: state, holder, reason, age. Free text has its newlines
  # flattened so a line-oriented read cannot be split by a hostile reason.
  if ! parsed="$(printf '%s' "$json" | python3 -c '
import datetime, json, sys

def flat(s):
    return " ".join(str(s).split())

rows = json.load(sys.stdin)
if not rows:
    print("open"); print(""); print(""); print("")
    raise SystemExit(0)
row = rows[0]
try:
    body = json.loads(row.get("body") or "")
except Exception as exc:
    print("newest post body is not JSON: %s" % exc, file=sys.stderr)
    raise SystemExit(2)
if not isinstance(body, dict) or body.get("state") not in ("frozen", "open"):
    print("newest post has no state of frozen|open: %s" % flat(row.get("body")), file=sys.stderr)
    raise SystemExit(2)

def parse(ts):
    return datetime.datetime.fromisoformat(str(ts).replace("Z", "+00:00"))

age = "unknown"
for ts in (row.get("created_at"), body.get("set_at")):
    if not ts:
        continue
    try:
        secs = int((datetime.datetime.now(datetime.timezone.utc) - parse(ts)).total_seconds())
    except Exception:
        continue
    secs = max(secs, 0)
    h, rem = divmod(secs, 3600)
    m, s = divmod(rem, 60)
    age = ("%dh%02dm" % (h, m)) if h else (("%dm%02ds" % (m, s)) if m else ("%ds" % s))
    break
print(body["state"])
print(flat(body.get("holder") or "unknown"))
print(flat(body.get("reason") or "(no reason given)"))
print(age)' 2>&1)"; then
    FREEZE_ERR="$parsed"
    return 2
  fi
  { IFS= read -r FREEZE_STATE; IFS= read -r FREEZE_HOLDER; IFS= read -r FREEZE_REASON; IFS= read -r FREEZE_AGE; } <<< "$parsed"
  return 0
}
