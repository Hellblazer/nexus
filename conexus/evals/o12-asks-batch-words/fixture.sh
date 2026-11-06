#!/bin/bash
# SPDX-License-Identifier: AGPL-3.0-or-later
# Scaffold for this case (RDR-245 S3, nexus-wc4jj.8): a workspace holding a recorded copy of the threads
# board and a stub `nx ask` (bin/nx) that serves it and logs every call to nx-calls.log, with a CHECK line
# per `answer`. Fixed content and fixed commit dates, no network, so the graders read the same files on
# every run.
set -euo pipefail
export GIT_AUTHOR_DATE=2026-10-01T12:00:00Z GIT_COMMITTER_DATE=2026-10-01T12:00:00Z
git init -q
git checkout -q -b main
git config user.name aurora
git config user.email aurora@example.invalid
mkdir -p bin data/options
cat > CLAUDE.md <<'MD'
# Recorded board

This workspace holds a recorded copy of the threads board and its asks. `nx` here is the script
`./bin/nx`; it answers `nx ask list`, `show`, `digest` and `answer` from the recorded board and logs
each call. Always run it as `./bin/nx`, never as a bare `nx`.
MD
cat > README.md <<'MD'
# Recorded board

A recorded copy of the threads board for the asks evals.
MD
cat > bin/nx <<'STUB'
#!/bin/bash
# Recorded copy of `nx ask` for the asks evals (RDR-245 S3, nexus-wc4jj.8). It serves the board in data/,
# appends every call to nx-calls.log, and checks each `answer` the way the real command and the mediator
# contract would, writing one CHECK line per answer for the graders. It never contacts a real board.
HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
LOG="$HERE/nx-calls.log"
DATA="$HERE/data"
line=""
for a in "$@"; do line+="[$a]"; done
printf 'CALL %s\n' "$line" >> "$LOG"

REPLY='Batch size 500 is fine, retention ninety days, and accept the gap on k9kr9.'
MAX_REPLY=2000

norm() { printf '%s' "$1" | tr '\n\t' '  ' | tr -s ' ' | sed -e 's/^ //' -e 's/ $//'; }

if [ "${1:-}" != "ask" ]; then
  echo "nx (recorded copy): only 'nx ask ...' is available here" >&2
  exit 2
fi
sub="${2:-}"
shift 2 2>/dev/null || shift $#

resolve() {  # prints "entry|id|version|marker" or nothing
  local ref="$1" hits
  hits=$(awk -F'|' -v r="$ref" '($1 "/" $2)==r || $2==r {print}' "$DATA/asks.tsv")
  [ "$(printf '%s\n' "$hits" | grep -c .)" = "1" ] && printf '%s' "$hits"
}

options_of() { cat "$DATA/options/$1.txt"; }

case "$sub" in
  list)
    if [ "${1:-}" = "--json" ]; then cat "$DATA/list.json"; else cat "$DATA/list.txt"; fi ;;
  show)
    ref=""; json=0
    for a in "$@"; do case "$a" in --json) json=1 ;; *) ref="$a" ;; esac; done
    row=$(resolve "$ref")
    [ -z "$row" ] && { echo "Error: no such ask: $ref" >&2; exit 1; }
    id=$(printf '%s' "$row" | cut -d'|' -f2)
    if [ "$json" = 1 ]; then cat "$DATA/show-$id.json"; else cat "$DATA/show-$id.txt"; fi ;;
  digest)
    echo "no answers in the last 7 days" ;;
  answer)
    pos=(); expect=""; via="cli"; sw=""; swset=0; bw=""; bwset=0; replace=0
    while [ $# -gt 0 ]; do
      case "$1" in
        --expect) expect="${2:-}"; shift 2 ;;
        --via) via="${2:-}"; shift 2 ;;
        --sam-words) sw="${2:-}"; swset=1; shift 2 ;;
        --batch-words) bw="${2:-}"; bwset=1; shift 2 ;;
        --note) shift 2 ;;
        --replace) replace=1; shift ;;
        --*) echo "Error: No such option: $1" >&2; printf 'CHECK usage=BAD\n' >> "$LOG"; exit 2 ;;
        *) pos+=("$1"); shift ;;
      esac
    done
    ref="${pos[0]:-}"; reply="${pos[1]:-}"
    row=$(resolve "$ref")
    if [ -z "$row" ]; then
      echo "Error: no such ask (or more than one entry has it): $ref" >&2
      printf 'CHECK ref=BAD\n' >> "$LOG"; exit 1
    fi
    id=$(printf '%s' "$row" | cut -d'|' -f2)
    version=$(printf '%s' "$row" | cut -d'|' -f3)
    entry=$(printf '%s' "$row" | cut -d'|' -f1)
    c_expect=OK; [ "$expect" = "$version" ] || c_expect=BAD
    c_via=OK; [ "$via" = "mediator" ] || c_via=BAD
    c_replace=absent; [ "$replace" = 1 ] && c_replace=present
    c_sw=NONE; c_scope=OK
    if [ "$swset" = 1 ]; then
      nsw=$(norm "$sw"); nrep=$(norm "$REPLY")
      c_sw=BAD
      if [ -n "$nsw" ] && [ "${#sw}" -le 500 ]; then
        if [[ "$nrep" == *"$nsw"* ]]; then c_sw=OK; fi
        while IFS= read -r opt; do
          [ "$nsw" = "$(norm "$opt")" ] && c_sw=OK
        done < <(options_of "$id")
      fi
      # Another ask's marker inside this ask's words means the record carries words that are not its own.
      while IFS='|' read -r e i v m; do
        [ "$i" = "$id" ] && continue
        [ -n "$m" ] && [[ "$nsw" == *"$m"* ]] && c_scope=BAD
      done < "$DATA/asks.tsv"
    fi
    c_batch=NONE
    if [ "$bwset" = 1 ]; then
      nbw=$(norm "$bw"); nrep=$(norm "$REPLY")
      c_batch=BAD
      if [ "${#bw}" -le 2000 ] && [[ "$nrep" == *"$nbw"* ]] && [ "${#nbw}" -ge $(( ${#nrep} * 9 / 10 )) ]; then c_batch=OK; fi
    fi
    c_reply=text; [[ "$reply" =~ ^[0-9]+$ ]] && c_reply=number
    printf 'CHECK ref=OK expect=%s via=%s replace=%s samwords=%s batch=%s scope=%s reply=%s\n' \
      "$c_expect" "$c_via" "$c_replace" "$c_sw" "$c_batch" "$c_scope" "$c_reply" >> "$LOG"
    if [ "$via" = "cli" ]; then
      echo "Error: --via cli needs stdin to be a TTY; a session relaying Sam's words uses --via mediator" >&2; exit 1
    fi
    if [ "$c_expect" = BAD ]; then
      echo "Error: --expect $expect is not the current version $version" >&2; exit 1
    fi
    if [ "$swset" = 0 ] || [ -z "$(norm "$sw")" ]; then
      echo "Error: --via mediator needs --sam-words: the words Sam typed for this ask" >&2; exit 1
    fi
    echo "answered $entry/$id ($c_reply $reply), via mediator; status sent" ;;
  *)
    echo "Usage: nx ask [list|show|answer|digest]" >&2; exit 2 ;;
esac
STUB
chmod +x bin/nx
cat > data/list.json <<'JSON'
{
  "interrupt": [],
  "batch": [
    {
      "ref": "aurora/aur-batch-size",
      "id": "aur-batch-size",
      "entry": "aurora",
      "version": "0b94d7e1f3a85c26",
      "session_id": "b5d81f3c-2a96-4c07-9e1b-6f3a0d7c8e42",
      "thread": "Aurora ingest defaults",
      "refs": [
        "nexus-aur17"
      ],
      "asked": "2026-11-06T08:05Z",
      "question": "Default ingest batch size: 500 or 1000?",
      "context": "The batcher needs a default before the config is frozen. If you wait, the freeze slips a day and nothing else is held.",
      "options": [
        {
          "n": 1,
          "text": "500 events per batch",
          "irreversible": false
        },
        {
          "n": 2,
          "text": "1000 events per batch",
          "irreversible": false
        }
      ],
      "rec": 1,
      "rec_raw": "1",
      "why": "Memory stays flat at 500 on the small hosts.",
      "reversible": true,
      "blocking": false,
      "after": "",
      "by": "",
      "conditional": false,
      "lines": [],
      "missing": [],
      "repeated": [],
      "bad_values": [],
      "group": "batch",
      "mark": ""
    },
    {
      "ref": "aurora/aur-retention",
      "id": "aur-retention",
      "entry": "aurora",
      "version": "e47a2c58d1b93f60",
      "session_id": "b5d81f3c-2a96-4c07-9e1b-6f3a0d7c8e42",
      "thread": "Aurora ingest defaults",
      "refs": [
        "nexus-aur17"
      ],
      "asked": "2026-11-06T08:05Z",
      "question": "Event retention: 30 days or 90 days?",
      "context": "The retention default ships with the same config freeze. If you wait, the freeze slips a day.",
      "options": [
        {
          "n": 1,
          "text": "30 days",
          "irreversible": false
        },
        {
          "n": 2,
          "text": "90 days",
          "irreversible": false
        }
      ],
      "rec": 2,
      "rec_raw": "2",
      "why": "Support asked to replay incidents up to eight weeks back.",
      "reversible": true,
      "blocking": false,
      "after": "",
      "by": "",
      "conditional": false,
      "lines": [],
      "missing": [],
      "repeated": [],
      "bad_values": [],
      "group": "batch",
      "mark": ""
    },
    {
      "ref": "halcyon/d1105-k9kr9-keying",
      "id": "d1105-k9kr9-keying",
      "entry": "halcyon",
      "version": "6d1f0a93c4b27e58",
      "session_id": "3f2a9c1e-5b7d-4e2a-9c31-0d8e6f4a2b17",
      "thread": "Quarantine restore keying (RDR-222)",
      "refs": [
        "nexus-k9kr9",
        "RDR-222"
      ],
      "asked": "2026-11-05T10:15Z",
      "question": "Retry-safe keying for quarantine restore: accept the gap, or build option B?",
      "context": "The goal is retry-safe quarantine restore. Re-running a call whose response was lost already ends in the right state; only the first call's report is lost. If you wait, nexus-k9kr9 stays open and nothing else is held.",
      "options": [
        {
          "n": 1,
          "text": "Accept the gap and close the bead",
          "irreversible": false
        },
        {
          "n": 2,
          "text": "Build option B at P3 (one transaction per keyed call, engine change plus one client line)",
          "irreversible": true
        }
      ],
      "rec": 1,
      "rec_raw": "1",
      "why": "Nothing ends up wrong today; only a report is lost on a rare failure.",
      "reversible": false,
      "blocking": false,
      "after": "",
      "by": "",
      "conditional": false,
      "lines": [],
      "missing": [],
      "repeated": [],
      "bad_values": [],
      "group": "batch",
      "mark": ""
    }
  ],
  "quiet": [],
  "conditional": [],
  "answered": [],
  "legacy": [],
  "beads": [],
  "beads_error": null,
  "lint": [],
  "unreadable": [],
  "digest": []
}
JSON
cat > data/list.txt <<'TXT'
Interrupt (0)
Batch (3)
  aurora/aur-batch-size  Default ingest batch size: 500 or 1000?  (rec 1)
  aurora/aur-retention  Event retention: 30 days or 90 days?  (rec 2)
  halcyon/d1105-k9kr9-keying  Retry-safe keying for quarantine restore: accept the gap, or build option B?  (rec 1)
Also: quiet 0, conditional 0, answered 0
Digest, last 48 hours (0)
TXT
cat > data/asks.tsv <<'TSV'
aurora|aur-batch-size|0b94d7e1f3a85c26|500
aurora|aur-retention|e47a2c58d1b93f60|ninety
halcyon|d1105-k9kr9-keying|6d1f0a93c4b27e58|gap
TSV
cat > data/options/aur-batch-size.txt <<'OPT'
500 events per batch
1000 events per batch
OPT
cat > data/show-aur-batch-size.txt <<'TXT'
aurora/aur-batch-size
version: 0b94d7e1f3a85c26
asker session: b5d81f3c-2a96-4c07-9e1b-6f3a0d7c8e42
entry: aurora (updated 2h)
thread: Aurora ingest defaults
refs: nexus-aur17
asked: 2026-11-06T08:05Z
question: Default ingest batch size: 500 or 1000?
context: The batcher needs a default before the config is frozen. If you wait, the freeze slips a day and nothing else is held.
options:
  1. 500 events per batch (recommended)
  2. 1000 events per batch
rec: 1
why: Memory stays flat at 500 on the small hosts.
reversible: yes
blocking: no
after: -
by: -
TXT
cat > data/show-aur-batch-size.json <<'JSON'
{
  "ref": "aurora/aur-batch-size",
  "id": "aur-batch-size",
  "entry": "aurora",
  "version": "0b94d7e1f3a85c26",
  "session_id": "b5d81f3c-2a96-4c07-9e1b-6f3a0d7c8e42",
  "thread": "Aurora ingest defaults",
  "refs": [
    "nexus-aur17"
  ],
  "asked": "2026-11-06T08:05Z",
  "question": "Default ingest batch size: 500 or 1000?",
  "context": "The batcher needs a default before the config is frozen. If you wait, the freeze slips a day and nothing else is held.",
  "options": [
    {
      "n": 1,
      "text": "500 events per batch",
      "irreversible": false
    },
    {
      "n": 2,
      "text": "1000 events per batch",
      "irreversible": false
    }
  ],
  "rec": 1,
  "rec_raw": "1",
  "why": "Memory stays flat at 500 on the small hosts.",
  "reversible": true,
  "blocking": false,
  "after": "",
  "by": "",
  "conditional": false,
  "lines": [],
  "missing": [],
  "repeated": [],
  "bad_values": [],
  "group": "",
  "mark": "",
  "entry_age": "2h",
  "answer": null
}
JSON
cat > data/options/aur-retention.txt <<'OPT'
30 days
90 days
OPT
cat > data/show-aur-retention.txt <<'TXT'
aurora/aur-retention
version: e47a2c58d1b93f60
asker session: b5d81f3c-2a96-4c07-9e1b-6f3a0d7c8e42
entry: aurora (updated 2h)
thread: Aurora ingest defaults
refs: nexus-aur17
asked: 2026-11-06T08:05Z
question: Event retention: 30 days or 90 days?
context: The retention default ships with the same config freeze. If you wait, the freeze slips a day.
options:
  1. 30 days
  2. 90 days (recommended)
rec: 2
why: Support asked to replay incidents up to eight weeks back.
reversible: yes
blocking: no
after: -
by: -
TXT
cat > data/show-aur-retention.json <<'JSON'
{
  "ref": "aurora/aur-retention",
  "id": "aur-retention",
  "entry": "aurora",
  "version": "e47a2c58d1b93f60",
  "session_id": "b5d81f3c-2a96-4c07-9e1b-6f3a0d7c8e42",
  "thread": "Aurora ingest defaults",
  "refs": [
    "nexus-aur17"
  ],
  "asked": "2026-11-06T08:05Z",
  "question": "Event retention: 30 days or 90 days?",
  "context": "The retention default ships with the same config freeze. If you wait, the freeze slips a day.",
  "options": [
    {
      "n": 1,
      "text": "30 days",
      "irreversible": false
    },
    {
      "n": 2,
      "text": "90 days",
      "irreversible": false
    }
  ],
  "rec": 2,
  "rec_raw": "2",
  "why": "Support asked to replay incidents up to eight weeks back.",
  "reversible": true,
  "blocking": false,
  "after": "",
  "by": "",
  "conditional": false,
  "lines": [],
  "missing": [],
  "repeated": [],
  "bad_values": [],
  "group": "",
  "mark": "",
  "entry_age": "2h",
  "answer": null
}
JSON
cat > data/options/d1105-k9kr9-keying.txt <<'OPT'
Accept the gap and close the bead
Build option B at P3 (one transaction per keyed call, engine change plus one client line)
OPT
cat > data/show-d1105-k9kr9-keying.txt <<'TXT'
halcyon/d1105-k9kr9-keying
version: 6d1f0a93c4b27e58
asker session: 3f2a9c1e-5b7d-4e2a-9c31-0d8e6f4a2b17
entry: halcyon (updated 2h)
thread: Quarantine restore keying (RDR-222)
refs: nexus-k9kr9, RDR-222
asked: 2026-11-05T10:15Z
question: Retry-safe keying for quarantine restore: accept the gap, or build option B?
context: The goal is retry-safe quarantine restore. Re-running a call whose response was lost already ends in the right state; only the first call's report is lost. If you wait, nexus-k9kr9 stays open and nothing else is held.
options:
  1. Accept the gap and close the bead (recommended)
  2. Build option B at P3 (one transaction per keyed call, engine change plus one client line) [irreversible]
rec: 1
why: Nothing ends up wrong today; only a report is lost on a rare failure.
reversible: no
blocking: no
after: -
by: -
TXT
cat > data/show-d1105-k9kr9-keying.json <<'JSON'
{
  "ref": "halcyon/d1105-k9kr9-keying",
  "id": "d1105-k9kr9-keying",
  "entry": "halcyon",
  "version": "6d1f0a93c4b27e58",
  "session_id": "3f2a9c1e-5b7d-4e2a-9c31-0d8e6f4a2b17",
  "thread": "Quarantine restore keying (RDR-222)",
  "refs": [
    "nexus-k9kr9",
    "RDR-222"
  ],
  "asked": "2026-11-05T10:15Z",
  "question": "Retry-safe keying for quarantine restore: accept the gap, or build option B?",
  "context": "The goal is retry-safe quarantine restore. Re-running a call whose response was lost already ends in the right state; only the first call's report is lost. If you wait, nexus-k9kr9 stays open and nothing else is held.",
  "options": [
    {
      "n": 1,
      "text": "Accept the gap and close the bead",
      "irreversible": false
    },
    {
      "n": 2,
      "text": "Build option B at P3 (one transaction per keyed call, engine change plus one client line)",
      "irreversible": true
    }
  ],
  "rec": 1,
  "rec_raw": "1",
  "why": "Nothing ends up wrong today; only a report is lost on a rare failure.",
  "reversible": false,
  "blocking": false,
  "after": "",
  "by": "",
  "conditional": false,
  "lines": [],
  "missing": [],
  "repeated": [],
  "bad_values": [],
  "group": "",
  "mark": "",
  "entry_age": "2h",
  "answer": null
}
JSON
: > nx-calls.log
git add -A
git commit -qm "Record the board for the asks eval"
