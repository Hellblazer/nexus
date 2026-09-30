#!/usr/bin/env bash
# SPDX-License-Identifier: AGPL-3.0-or-later
#
# Set, clear, or read the develop freeze (nexus-eusu6).
#
# Why: on 2026-09-29 the 7.67.0 release freeze was announced only by direct
# messages to a hand-picked list of sessions, and a session that was not on the
# list pushed to develop mid-freeze. A freeze that lives in messages reaches
# exactly the sessions someone remembered. This puts it where every pusher looks:
# a post on the board/develop-freeze topic, which scripts/git-push-develop.sh
# reads before it pushes and refuses on.
#
# Usage:
#   develop-freeze.sh set --reason "<text>" [--holder <name>]
#   develop-freeze.sh clear [--reason "<text>"] [--holder <name>]
#   develop-freeze.sh status
#
# `set` posts state=frozen, `clear` posts state=open; the NEWEST post decides and
# no posts at all means open. Board rows expire on their own (7-day retention
# ceiling on the board/<topic> template), so a release owner that crashed
# without clearing cannot freeze develop for longer than that. There is no lock
# and no owner check: any session in the tenant can clear, on purpose, so a
# stranded freeze is never stuck behind an absent owner.
#
# Sessions can also subscribe for push delivery instead of polling:
#   mcp__plugin_conexus_nexus__tuple_subscribe("board/develop-freeze")
#
# Environment:
#   NX_SESSION_ID / CLAUDE_CODE_SESSION_ID
#                    the default --holder (else ~/.config/nexus/current_session,
#                    else user@host)
#
# Exit codes:
#   0  set/clear posted, or `status` found develop open
#   10 `status` found develop FROZEN (deliberately not 1: a script can tell a
#      freeze from a failure)
#   2  usage error (unknown subcommand/flag, missing or oversized --reason)
#   3  `status`: the board could not be read, or its newest post is malformed
#   4  only a dev-checkout/venv nx is on PATH (same refusal as
#      git-push-develop.sh: the installed generation was not found)
#   5  the post failed (tuple space unreachable or refused it)

set -euo pipefail

_script_dir="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd -P)"
# shellcheck source=./lib/installed-nx.sh
source "$_script_dir/lib/installed-nx.sh"
# shellcheck source=./lib/develop-freeze.sh
source "$_script_dir/lib/develop-freeze.sh"

# The template caps a board body at 1024 bytes; the JSON envelope (state,
# holder, set_at, quoting) eats some of it, so cap the reason well inside.
_MAX_REASON_BYTES=600
# The binding check is on the encoded body (below); these are early, friendly
# refusals. The board template's max_body_bytes is 1024.
_MAX_HOLDER_BYTES=128
_MAX_BODY_BYTES=1024

usage() {
  sed -n '/^# Usage:/,/^#   develop-freeze.sh status/p' "${BASH_SOURCE[0]}" | sed 's/^# \{0,1\}//' >&2
}

_default_holder() {
  local sess="${NX_SESSION_ID:-${CLAUDE_CODE_SESSION_ID:-}}" cfg_dir
  if [[ -z "$sess" ]]; then
    cfg_dir="${NEXUS_CONFIG_DIR:-$HOME/.config/nexus}"
    if [[ -r "$cfg_dir/current_session" ]]; then
      sess="$(cat "$cfg_dir/current_session" 2>/dev/null || true)"
    fi
  fi
  if [[ -z "$sess" ]]; then
    sess="${USER:-unknown}@$(hostname -s 2>/dev/null || echo unknown-host)"
  fi
  printf '%s' "$sess"
}

if [[ $# -lt 1 ]]; then
  usage
  exit 2
fi
cmd="$1"
shift

reason=""
holder=""
while [[ $# -gt 0 ]]; do
  case "$1" in
    --reason) [[ $# -ge 2 ]] || { echo "develop-freeze: --reason needs a value" >&2; exit 2; }
              reason="$2"; shift 2 ;;
    --holder) [[ $# -ge 2 ]] || { echo "develop-freeze: --holder needs a value" >&2; exit 2; }
              holder="$2"; shift 2 ;;
    *) echo "develop-freeze: unknown argument: $1" >&2; usage; exit 2 ;;
  esac
done

case "$cmd" in
  set|clear|status) ;;
  *) echo "develop-freeze: unknown subcommand: $cmd" >&2; usage; exit 2 ;;
esac

if [[ "$cmd" == "set" && -z "$reason" ]]; then
  echo "develop-freeze: set needs --reason \"<text>\" (pushers see it in their refusal)" >&2
  exit 2
fi
if (( $(printf '%s' "$reason" | wc -c) > _MAX_REASON_BYTES )); then
  echo "develop-freeze: --reason is over ${_MAX_REASON_BYTES} bytes; the board caps a post at 1024" >&2
  exit 2
fi

if ! _nx_bin="$(installed_nx_resolve)"; then
  echo "develop-freeze: only a dev-checkout/venv nx is on PATH; the installed generation was not found." >&2
  echo "The nexus-a2qhz production-write guard refuses tuple-space writes from a dev-checkout CLI. Fix PATH so the" >&2
  echo "installed generation resolves first (avoid 'uv run' / an activated venv for this script), or reinstall it:" >&2
  echo "scripts/reinstall-tool.sh." >&2
  exit 4
fi
_scope="$(installed_nx_describe_scope "$_nx_bin")"

if [[ "$cmd" == "status" ]]; then
  rc=0
  freeze_read "$_nx_bin" || rc=$?
  if [[ $rc -ne 0 ]]; then
    echo "FREEZE_UNKNOWN could not read $FREEZE_SUBSPACE ($_scope): $FREEZE_ERR" >&2
    exit 3
  fi
  if [[ "$FREEZE_STATE" == "frozen" ]]; then
    echo "develop is FROZEN holder=$FREEZE_HOLDER age=$FREEZE_AGE reason=$FREEZE_REASON ($_scope)"
    exit 10
  fi
  echo "develop is open ($_scope)"
  exit 0
fi

[[ -n "$holder" ]] || holder="$(_default_holder)"
if [[ "$cmd" == "set" ]]; then
  state="frozen"
else
  state="open"
  [[ -n "$reason" ]] || reason="thawed"
fi

# JSON built by python, never by string concatenation: a reason with a quote or
# a backslash must not corrupt the post every pusher then parses.
if (( $(printf '%s' "$holder" | wc -c) > _MAX_HOLDER_BYTES )); then
  echo "develop-freeze: --holder is over ${_MAX_HOLDER_BYTES} bytes" >&2
  exit 2
fi
# ensure_ascii=False keeps a multibyte reason at its UTF-8 size (the default
# \uXXXX escaping roughly doubles it), and the ENCODED body is checked against
# the board's cap here rather than trusting the raw reason's byte count: quotes
# and backslashes escape to two bytes each (review finding).
if ! body="$(STATE="$state" HOLDER="$holder" REASON="$reason" MAXB="$_MAX_BODY_BYTES" python3 -c '
import datetime, json, os, sys
body = json.dumps({
    "state": os.environ["STATE"],
    "holder": os.environ["HOLDER"],
    "reason": os.environ["REASON"],
    "set_at": datetime.datetime.now(datetime.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
}, separators=(",", ":"), ensure_ascii=False)
n = len(body.encode("utf-8"))
if n > int(os.environ["MAXB"]):
    print("the encoded post is %d bytes; the board caps a post at %s. Shorten --reason." % (n, os.environ["MAXB"]), file=sys.stderr)
    raise SystemExit(2)
print(body)')"; then
  exit 2
fi

# The nonce makes each post a NEW tuple (board/<topic> is keys+nonce): posts are
# an append-only log and the newest wins. Time plus pid is unique per invocation.
nonce="freeze-$(date -u +%Y%m%dT%H%M%S)-$$-$RANDOM"
if ! out="$("$_nx_bin" tuple out "$FREEZE_SUBSPACE" --key "topic=develop-freeze" \
      --dim "from=$holder" --dim "kind=freeze" --nonce "$nonce" --body "$body" 2>&1)"; then
  echo "develop-freeze: could not post to $FREEZE_SUBSPACE ($_scope): $out" >&2
  exit 5
fi

if [[ "$state" == "frozen" ]]; then
  echo "FREEZE_SET develop is now FROZEN holder=$holder reason=$reason ($_scope)"
  echo "Clear it at thaw (after the tag and the main->develop back-merge): scripts/develop-freeze.sh clear"
else
  echo "FREEZE_CLEARED develop is open holder=$holder reason=$reason ($_scope)"
fi
