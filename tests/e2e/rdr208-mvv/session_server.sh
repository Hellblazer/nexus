#!/usr/bin/env bash
# SPDX-License-Identifier: AGPL-3.0-or-later
# Stand-in for one Claude Code process in the RDR-208 local-mode MVV.
#
# Run as `claude session_server.sh NAME`, where `claude` is a copy of bash:
# nexus.session.find_immediate_claude_pid matches any ancestor whose command
# name starts with "claude", so every hook and watcher this loop runs finds
# this process as its claude ancestor, exactly as under Claude Code. The pid
# stays fixed across a simulated /clear or /branch, as a real one does.
#
# Commands arrive on $RUN/NAME.fifo, one per line: "ID MODE BASE64-CMD".
#   run    run the command to completion; stdout/stderr/rc land in $RUN/ID.*
#   spawn  start the command as a direct background child (a Monitor's shape);
#          its pid lands in $RUN/ID.pid
#   quit   exit the loop
set -u
NAME="$1"
FIFO="$RUN/$NAME.fifo"
rm -f "$FIFO"
mkfifo "$FIFO"
echo "$$" > "$RUN/$NAME.server.pid"
exec 3<>"$FIFO"
_done() { echo "$2" > "$RUN/$1.rc.tmp"; mv "$RUN/$1.rc.tmp" "$RUN/$1.rc"; }
while IFS= read -r line <&3; do
    id="${line%% *}"
    rest="${line#* }"
    mode="${rest%% *}"
    cmd="$(printf '%s' "${rest#* }" | base64 -d)"
    case "$mode" in
        run)
            bash -c "$cmd" > "$RUN/$id.out" 2> "$RUN/$id.err"
            _done "$id" "$?"
            ;;
        spawn)
            ( exec bash -c "$cmd" ) > "$RUN/$id.out" 2> "$RUN/$id.err" &
            echo "$!" > "$RUN/$id.pid"
            _done "$id" 0
            ;;
        quit)
            _done "$id" 0
            exit 0
            ;;
    esac
done
