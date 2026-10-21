#!/bin/bash
# SPDX-License-Identifier: AGPL-3.0-or-later
#
# RDR-228 MVV row 1 (local, start at login) inside the macOS test guest, as the
# guest's auto-login account (admin). launch.sh stages this file to
# ~/nxmvv/row1.sh and runs it twice:
#
#   row1.sh pre  [MODE]   before the reboot: Claude Code runs /conexus:setup's
#                         "local, start at login" answer, stores a probe note
#   row1.sh post [MODE]   after the reboot: the login session, the launchd job,
#                         the engine and the note came back without a human
#
# MODE is Claude Code's permission mode (default auto). The automation token
# arrives in the environment from claude_credentials.py run --remote and stays
# there: it is never written to a file or printed. Every check prints one line,
# "CHECK PASS <name>: <detail>" or "CHECK FAIL <name>: <detail>", into
# ~/nxmvv/out/checks-<phase>.txt; the exit status is the number of failures
# (0 = all passed), capped at 100.
set -uo pipefail

phase="${1:-}"; MODE="${2:-auto}"
[[ "$phase" == pre || "$phase" == post ]] || { echo "usage: row1.sh pre|post [MODE]" >&2; exit 2; }
# This script installs nx, a LaunchAgent and a Claude Code session into the account
# that runs it, and removes ~/nxmvv. It runs only inside a tart guest: a physical
# Mac reports its own model (Mac17,16 on hellmini), a Virtualization.framework
# guest reports VirtualMac.
hwm="$(sysctl -n hw.model 2>/dev/null || true)"
[[ "$hwm" == VirtualMac* ]] || { echo "row1.sh: refusing to run outside a tart guest (hw.model is '${hwm:-unknown}', not VirtualMac*)" >&2; exit 2; }
D="$HOME/nxmvv"; OUT="$D/out"; PLUGIN="$D/plugin/conexus"
mkdir -p "$OUT" "$HOME/proj"
CHECKS="$OUT/checks-$phase.txt"; : >"$CHECKS"
LABEL=com.nexus.service
PLIST="$HOME/Library/LaunchAgents/$LABEL.plist"
UIDN="$(id -u)"
fails=0

# The automation token arrives in the environment (claude_credentials.py run
# --remote). It moves into an unexported shell variable here, so nx, uv,
# launchctl and every other command this script starts never see it; only the
# claude calls in turn() get it.
TOK="${CLAUDE_CODE_OAUTH_TOKEN:-}"
unset CLAUDE_CODE_OAUTH_TOKEN

# A clean environment: nothing from the ssh login or the harness leaks into the
# row except the model defaults claude_credentials.py sets.
for v in $(compgen -e | grep -E '^(NX_|NEXUS_|CLAUDECODE$|CLAUDE_CODE_ENTRYPOINT$|CLAUDE_CODE_SSE_PORT$|CLAUDE_PROJECT_DIR$|CLAUDE_PLUGIN_|CLAUDE_CONFIG_DIR$|UV_|VIRTUAL_ENV$|PYTHONPATH$|PYTHONHOME$)'); do unset "$v"; done
export NX_NO_TELEMETRY=1
# The image's Claude Code stays the version the image was built with.
export DISABLE_AUTOUPDATER=1
export PATH="$HOME/.local/bin:/usr/bin:/bin:/usr/sbin:/sbin"
# launch.sh --wheel stages a candidate wheel into ~/nxmvv/dist; uv then finds it there.
if [[ -f "$D/use-wheel" ]]; then export UV_FIND_LINKS="$D/dist"; fi

check() {  # check <name> <0|1 (1 = pass)> <detail>
    if [[ "$2" -eq 1 ]]; then echo "CHECK PASS $1: $3" | tee -a "$CHECKS"
    else echo "CHECK FAIL $1: $3" | tee -a "$CHECKS"; fails=$((fails + 1)); fi
}

# Dependencies the row needs; absent means FAIL, never a skip.
command -v claude >/dev/null 2>&1 || { check deps 0 "claude is not on PATH ($PATH)"; exit 1; }
command -v uv >/dev/null 2>&1 || { check deps 0 "uv is not on PATH ($PATH)"; exit 1; }
[[ -d "$PLUGIN" ]] || { check deps 0 "no plugin at $PLUGIN"; exit 1; }
[[ -n "$TOK" ]] || { check deps 0 "no automation token in the environment"; exit 1; }

PROMPT_SETUP="Nexus is not installed on this machine. My answer to the Nexus setup question: Local, and yes, start it at login. Please run the setup now and follow its status until it finishes, then tell me the final status."
PROMPT_STORE='Use the nexus MCP tools. Store a short note with store_put: collection mvv-probe, title "mvv row probe", content "the quick brown fox checks RDR-228". Then search for "brown fox RDR-228" and quote the top result with its distance.'
PROMPT_SEARCH='Use the nexus MCP search tool to search for "brown fox RDR-228" and quote the top result with its distance. Do not store anything.'

turn() {  # turn <file> <prompt>: one claude -p turn, bounded, output to a file
    echo "##### $(date -u +%FT%TZ) claude -p ($MODE): $1" >"$OUT/$1"
    ( cd "$HOME/proj" && CLAUDE_CODE_OAUTH_TOKEN="$TOK" claude -p --permission-mode "$MODE" --plugin-dir "$PLUGIN" "$2" </dev/null >>"$OUT/$1" 2>&1 ) &
    local p=$! n=0
    while kill -0 "$p" 2>/dev/null; do
        sleep 5; n=$((n + 5))
        if [[ $n -ge 1500 ]]; then pkill -TERM -P "$p" 2>/dev/null; kill -TERM "$p" 2>/dev/null; echo "##### TIMED OUT after 1500 s" >>"$OUT/$1"; break; fi
    done
    wait "$p"; echo "##### exit $?" >>"$OUT/$1"
}

status_field() {  # status_field <key>: a field of the setup status.json
    /usr/bin/plutil -extract "$1" raw -o - "$HOME/.config/nexus/setup/status.json" 2>/dev/null
}

nx_version() { local v; v="$(nx --version 2>&1)"; echo "${v%%$'\n'*}"; }

job_pid() { launchctl print "gui/$UIDN/$LABEL" 2>/dev/null | awk '$1 == "pid" && $2 == "=" {print $3; exit}'; }

# grep without -q: an early-exiting grep under pipefail can SIGPIPE tee and fail a found note.
note_found() { nx search "brown fox RDR-228" --corpus knowledge >"$OUT/$1" 2>&1; grep "quick brown fox" "$OUT/$1" >/dev/null; }

memory_snapshot() {  # display only; it gates nothing
    { echo "##### $(date -u +%FT%TZ) $1"; memory_pressure 2>/dev/null | tail -1; vm_stat | sed -n '1,8p';
      sysctl -n hw.memsize; ps -A -o rss=,comm= | sort -rn | sed -n '1,12p'; } >>"$OUT/memory.txt" 2>&1
}

if [[ "$phase" == pre ]]; then
    { sw_vers; echo "claude $(claude --version 2>&1)"; echo "uv $(uv --version 2>&1)"; uname -m; } >"$OUT/guest.txt" 2>&1
    if command -v nx >/dev/null 2>&1 || [[ -e "$HOME/.config/nexus" ]]; then
        check clean_start 0 "the nexus CLI or ~/.config/nexus is present before setup: $(command -v nx || echo no-nx)"
    else
        check clean_start 1 "neither the nexus CLI nor ~/.config/nexus before setup"
    fi
    memory_snapshot "before setup"
    turn 10-setup.txt "$PROMPT_SETUP"
    # Follow the setup's own record until it stops running (the turn asks
    # Claude Code to follow it, but a turn can end early).
    for _ in $(seq 1 180); do [[ "$(status_field state)" == running ]] || break; sleep 5; done
    st="$(status_field state)"; mode="$(status_field mode)"; auto="$(status_field autostart)"
    check setup_done "$([[ "$st" == "done" ]] && echo 1 || echo 0)" "status.json state=$st mode=$mode autostart=$auto"
    check setup_local_autostart "$([[ "$mode" == local && "$auto" == true ]] && echo 1 || echo 0)" "mode=$mode autostart=$auto"
    check launchagent_plist "$([[ -f "$PLIST" ]] && echo 1 || echo 0)" "$PLIST"
    pid="$(job_pid)"
    check launchd_job_running_pre "$([[ -n "$pid" ]] && echo 1 || echo 0)" "gui/$UIDN/$LABEL pid=${pid:-none}"
    echo "${pid:-}" >"$OUT/pre-job-pid"
    nx doctor >"$OUT/11-doctor-pre.txt" 2>&1; rc=$?
    check doctor_pre "$([[ $rc -eq 0 ]] && echo 1 || echo 0)" "nx doctor exit $rc, nx $(nx_version)"
    memory_snapshot "after setup"
    turn 20-store.txt "$PROMPT_STORE"
    note_found 21-search-cli-pre.txt && ok=1 || ok=0
    check note_stored_pre "$ok" "nx search finds the probe note stored through MCP"
else
    # The login session: auto-login put admin on the console and launchd made
    # its gui domain; nothing here logs in.
    ok=0
    for _ in $(seq 1 60); do
        if [[ "$(stat -f %Su /dev/console)" == "$(id -un)" ]] && launchctl print "gui/$UIDN" >/dev/null 2>&1; then ok=1; break; fi
        sleep 5
    done
    check login_session_back "$ok" "console user $(stat -f %Su /dev/console), gui/$UIDN $(launchctl print "gui/$UIDN" >/dev/null 2>&1 && echo present || echo absent)"
    pid=''
    for _ in $(seq 1 60); do pid="$(job_pid)"; [[ -n "$pid" ]] && break; sleep 5; done
    prev="$(cat "$OUT/pre-job-pid" 2>/dev/null || true)"
    # A job that ran before the reboot and has the same pid now was not restarted by
    # launchd: the reboot did not happen to this guest.
    check launchd_job_running_post "$([[ -n "$pid" && -n "$prev" && "$pid" != "$prev" ]] && echo 1 || echo 0)" \
        "gui/$UIDN/$LABEL pid=${pid:-none}, before the reboot ${prev:-none} (must be running, and a different pid), nx $(nx_version)"
    launchctl print "gui/$UIDN/$LABEL" >"$OUT/30-launchctl-post.txt" 2>&1
    ok=0
    for _ in $(seq 1 36); do nx doctor >"$OUT/31-doctor-post.txt" 2>&1 && { ok=1; break; }; sleep 5; done
    check doctor_post "$ok" "nx doctor after the reboot (retried up to 180 s while the job starts)"
    note_found 32-search-cli-post.txt && ok=1 || ok=0
    check note_survives_reboot "$ok" "nx search finds the probe note after the reboot"
    memory_snapshot "after reboot"
    turn 40-search-session.txt "$PROMPT_SEARCH"
    grep -q "quick brown fox checks RDR-228" "$OUT/40-search-session.txt" && ok=1 || ok=0
    check session_search_post "$ok" "a new Claude Code session finds the note through the nexus MCP server"
fi
echo "ROW1 $phase: $(grep -c '^CHECK PASS' "$CHECKS") passed, $fails failed" | tee -a "$CHECKS"
[[ $fails -le 100 ]] || fails=100
exit "$fails"
