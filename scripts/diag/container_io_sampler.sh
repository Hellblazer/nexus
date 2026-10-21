#!/usr/bin/env bash
# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (c) 2026 Hal Hildebrand. All rights reserved.
#
# scripts/diag/container_io_sampler.sh -- who writes to the host's disk while a command runs
# (nexus-3cwsw). Diagnostic for a chas run of the Java suite.
#
# usage: container_io_sampler.sh [-i SECS] [-x PROCNAME]... [-f DOCKER_PS_FILTER]... [-r CGROUP_ROOT] -- COMMAND [ARGS...]
#
# While COMMAND runs, every SECS seconds (default 2) it reads the block-device write bytes (wbytes
# in io.stat, cgroup v2) of every running docker container's cgroup, found through the container's
# init pid (`docker inspect` -> /proc/<pid>/cgroup). It keeps the MAX per container (a cgroup's
# counter only grows; the last tick before a container dies is the last one seen). It also reads
# the cgroups of dockerd, containerd and systemd-journald (more with -x: a process name, matched
# exactly with pgrep; -f restricts the containers sampled, e.g. -f label=dev.nexus.test.run=$NX_TEST_RUN_ID,
# the daemon cgroups stay host-wide) at the start and the end, and the command's own cgroup, and sizes each
# container's json log (docker's `LogPath`) where the file is readable.
#
# At the end it prints: the docker driver line; the sum of the per-container maxima; each daemon
# cgroup's wbytes delta; the json-log bytes total; the top 10 containers by wbytes with their
# dev.nexus.test.caller label, and the same summed by caller; the block device's own write delta
# (/proc/diskstats, for the devices that appeared in a sampled io.stat) and what no sampled cgroup
# accounts for ("unattributed = device - attributed"). The exit status is COMMAND's, except that it is 3
# when -f was given, COMMAND succeeded and zero containers were seen (a wrong filter measures nothing).
#
# Needs no root. What it cannot read it skips and says so in the report: a cgroup whose io.stat is
# unreadable, a json log under /var/lib/docker/containers (root-only on most hosts; the log is then
# sized by `docker logs | wc -c`, its payload without the json wrapper, so a lower bound; that re-reads
# the whole log through dockerd, so it runs every NX_IO_SAMPLER_LOGS_EVERY seconds, default 30, and at
# the final sample, not every tick; a container gone by then keeps the size of its last sizing, up to
# that many seconds stale, and one shorter-lived than that is sized only at its first tick).
#
# WHAT THE NUMBERS ARE NOT (attribution limits). cgroup wbytes count the bios a cgroup issues to a block
# device. They omit the filesystem's own journal and metadata writes and any kernel writeback charged to
# no sampled cgroup, so the sampled cgroups can sum to far less than the device saw: that is why the
# device delta and the "unattributed" line are printed. They assume the sampled cgroups are disjoint
# (do not name an -x process that shares a cgroup with another). NOT SEEN: a container that starts and
# dies between two ticks, and the last SECS seconds of each container's life, so the container sum is a
# LOWER bound; lower -i to tighten it. Block-device writes only: tmpfs (the test clusters' data
# directory) is not one. A device stacked directly on another listed one (dm/LVM or md on the NVMe,
# found through sysfs slaves) is reported but not added, so the stack is counted once, at the device
# below it; a stack with a layer missing from the set, or a dm spanning disks only some of which are
# listed, is not resolved. The daemon deltas and the device delta are HOST-WIDE: run when the pool is
# quiet, or they include other jobs, and compare an A/B arm against an idle window, not against zero.
# Measured chas numbers live in tests/AGENTS.md (the DDL counter section), not here.
#
# Every docker call is bounded (NX_IO_SAMPLER_DOCKER_TIMEOUT seconds, default 20); after COMMAND ends the
# sampler loop gets NX_IO_SAMPLER_GRACE seconds (default 10), then is killed, and the final sample gets
# NX_IO_SAMPLER_FINAL seconds (default 30). A bound that expires is counted in the report, so a stalled
# daemon yields a report that says what is missing, not a hang. The sampler loop also ends by itself if
# this script is killed (SIGKILL). The state directory is on /dev/shm when that is writable
# (NX_IO_SAMPLER_STATE_BASE overrides), so sampling does not write to the disk it measures.
#
# The functions below are sourceable (tests/scripts/test_container_io_sampler.py); main runs only
# when the file is executed.

set -uo pipefail

# Where /proc is (tests point this at a fixture tree).
PROC_ROOT=${NX_IO_SAMPLER_PROC:-/proc}
# Where the block devices' sysfs entries are, by maj:min (tests point this at a fixture tree).
SYS_DEV_BLOCK=${NX_IO_SAMPLER_SYS_DEV_BLOCK:-/sys/dev/block}
# Bounds, in seconds (see the header).
DOCKER_TIMEOUT=${NX_IO_SAMPLER_DOCKER_TIMEOUT:-20}
SAMPLER_GRACE=${NX_IO_SAMPLER_GRACE:-10}
FINAL_SAMPLE_SECS=${NX_IO_SAMPLER_FINAL:-30}
# How often, in seconds, a container whose json log file is unreadable is sized by `docker logs | wc -c`,
# which re-reads the whole log through dockerd each time. The final sample always sizes it.
LOGS_EVERY=${NX_IO_SAMPLER_LOGS_EVERY:-30}
FINAL_TICK=""
STATE=""

# ---- parsing helpers (pure; unit-tested with fixture files) ---------------------------------

# The ids among the arguments (maj:min) that sit on another of them: a dm or md device one of whose slaves
# is another argument (a partition slave answers for the disk that holds it). A write to such a stack is
# counted at both layers, in io.stat and in /proc/diskstats, so adding every row counts it twice
# (nexus-eyu4m; chas has LVM 252:0 on nvme0n1p3, disk 259:0). The DDL counter applies the same rule.
stacked_ids() {
    local id slave node below
    for id in "$@"; do
        for slave in "$SYS_DEV_BLOCK/$id"/slaves/*; do
            [[ -e $slave ]] || continue
            node=$(cd -P "$slave" 2>/dev/null && pwd) || continue
            [[ -e $node/partition ]] && node=${node%/*}
            [[ -r $node/dev ]] || continue
            below=$(<"$node/dev")
            if [[ $below != "$id" && " $* " == *" $below "* ]]; then
                echo "$id"
                break
            fi
        done
    done
}

# wbytes summed over the device rows of an io.stat file, leaving out a device stacked on another row
# (stacked_ids); fails (prints nothing) when unreadable.
sum_wbytes() {
    local file=$1 skip
    [[ -r $file ]] || return 1
    # shellcheck disable=SC2046  # maj:min ids, split on purpose
    skip=$(stacked_ids $(awk 'NF { print $1 }' "$file") | tr '\n' ' ')
    awk -v skip=" $skip" 'index(skip, " " $1 " ") { next }
        { for (i = 1; i <= NF; i++) if ($i ~ /^wbytes=/) { split($i, kv, "="); t += kv[2] } } END { printf "%d\n", t }' "$file"
}

# The cgroup v2 path (no leading slash) of a /proc/<pid>/cgroup file: its "0::" line. Fails when
# there is none (cgroup v1) or the file is unreadable.
cgroup_path_from_proc() {
    local file=$1 line
    [[ -r $file ]] || return 1
    while IFS= read -r line; do
        if [[ $line == 0::* ]]; then
            line=${line#0::}
            printf '%s\n' "${line#/}"
            return 0
        fi
    done <"$file"
    return 1
}

# Keep the larger of the number in FILE and VALUE in FILE.
record_max() {
    local file=$1 value=$2 old=0
    [[ $value =~ ^[0-9]+$ ]] || return 1
    [[ -r $file ]] && old=$(<"$file")
    [[ $old =~ ^[0-9]+$ ]] || old=0
    if ((value > old)); then
        printf '%s\n' "$value" >"$file"
    elif [[ ! -e $file ]]; then
        printf '%s\n' "$old" >"$file"
    fi
}

# 1234567 -> "1234567 (1.18 MiB)"
fmt_bytes() {
    awk -v n="$1" 'BEGIN {
        split("B KiB MiB GiB TiB", u, " "); v = n; i = 1
        while (v >= 1024 && i < 5) { v /= 1024; i++ }
        if (i == 1) printf "%d B", n; else printf "%d (%.2f %s)", n, v, u[i]
    }'
}

# A positive number (integer or decimal), for -i.
positive_number() {
    [[ $1 =~ ^[0-9]+(\.[0-9]+)?$ ]] && awk -v v="$1" 'BEGIN { exit !(v > 0) }'
}

# A positive integer, for the bounds.
positive_int() {
    [[ $1 =~ ^[1-9][0-9]*$ ]]
}

# A name as one path component: anything but letters, digits, dot, underscore and dash becomes "_", and a
# leading dot gets a "_" before it.
safe_key() {
    local key=${1//[^A-Za-z0-9._-]/_}
    [[ $key == .* ]] && key=_$key # a dotfile would hide from the report's globs
    printf '%s\n' "$key"
}

# Where the state directory goes: NX_IO_SAMPLER_STATE_BASE, else /dev/shm (NX_IO_SAMPLER_SHM names another)
# when writable, so sampling does not write to the disk it measures, else TMPDIR, else /tmp.
pick_state_base() {
    local shm=${NX_IO_SAMPLER_SHM:-/dev/shm}
    if [[ -n ${NX_IO_SAMPLER_STATE_BASE:-} ]]; then
        printf '%s\n' "$NX_IO_SAMPLER_STATE_BASE"
    elif [[ -d $shm && -w $shm ]]; then
        printf '%s\n' "$shm"
    else
        printf '%s\n' "${TMPDIR:-/tmp}"
    fi
}

# Every process below PID, from the process table (the deep ones too: a bounded docker call sits under
# a command substitution under the loop).
descendants() {
    ps -A -o pid=,ppid= 2>/dev/null | awk -v root="$1" '
        { parent[$1] = $2; pids[n++] = $1 }
        END {
            for (i = 0; i < n; i++) {
                p = pids[i]
                while ((p in parent) && p != root && p != 0) p = parent[p]
                if (p == root && pids[i] != root) print pids[i]
            }
        }'
}

# Stop PID and everything below it (the descendants are listed first: once PID is gone they are orphans).
kill_tree() {
    local pid=$1 all p
    all=$(descendants "$pid")
    for p in $all "$pid"; do
        kill -TERM "$p" 2>/dev/null
    done
    sleep 0.3
    for p in $all "$pid"; do
        if kill -0 "$p" 2>/dev/null; then kill -KILL "$p" 2>/dev/null; fi
    done
    return 0
}

# Run a command (or a function of this script) for at most SECS to SECS+1 seconds; killed after that, and
# the status is 124 (as timeout(1) gives). Otherwise the command's own status. Output passes through.
# Plain bash (no timeout(1), absent on macOS) and no watchdog process to leak.
bounded() {
    local secs=$1 pid deadline
    shift
    "$@" &
    pid=$!
    deadline=$((SECONDS + secs + 1))
    while kill -0 "$pid" 2>/dev/null; do
        if ((SECONDS >= deadline)); then
            kill_tree "$pid"
            wait "$pid" 2>/dev/null
            return 124
        fi
        sleep 0.1
    done
    wait "$pid"
}

# A docker call, bounded; one that expires is noted in STATE/timeouts for the report.
dock() {
    bounded "$DOCKER_TIMEOUT" docker "$@"
    local rc=$?
    if ((rc == 124)) && [[ -n $STATE ]]; then
        echo "docker $1 did not answer in ${DOCKER_TIMEOUT}s" >>"$STATE/timeouts" 2>/dev/null
    fi
    return "$rc"
}

# The device ids (major:minor) in an io.stat file, appended to STATE/devices.
note_devices() {
    local state=$1 file=$2
    [[ -r $file ]] || return 0
    awk 'NF { print $1 }' "$file" >>"$state/devices" 2>/dev/null
    return 0
}

# Lines "<name> <maj:min> <bytes written>" between two /proc/diskstats copies, for the ids in DEVFILE.
# Field 10 of a diskstats row is sectors written; a sector is 512 bytes there whatever the device's own.
diskstats_delta() {
    local base=$1 end=$2 devfile=$3
    awk '
        FILENAME == ARGV[1] { want[$1] = 1; next }
        FILENAME == ARGV[2] { k = $1 ":" $2; if (k in want) b[k] = $10; next }
        { k = $1 ":" $2; if ((k in want) && (k in b)) printf "%s %s %.0f\n", $3, k, ($10 - b[k]) * 512 }
    ' "$devfile" "$base" "$end"
}

# Lines "<wbytes> <id12> <caller>" for every container directory under STATE/c, biggest first.
container_rows() {
    local state=$1 dir id caller w
    for dir in "$state"/c/*/; do
        [[ -d $dir ]] || continue
        id=$(basename "$dir")
        caller=$(<"$dir/caller")
        w=0
        [[ -r $dir/wmax ]] && w=$(<"$dir/wmax")
        printf '%s %s %s\n' "$w" "${id:0:12}" "${caller:--}"
    done | sort -k1,1nr
}

# Lines "<wbytes> <containers> <caller>", summed per caller, biggest first.
caller_rows() {
    container_rows "$1" | awk '{ w[$3] += $1; n[$3]++ } END { for (c in w) printf "%d %d %s\n", w[c], n[c], c }' | sort -k1,1nr
}

# How many containers have a directory in STATE/c.
count_containers() {
    local dir n=0
    for dir in "$1"/c/*/; do
        [[ -d $dir ]] && n=$((n + 1))
    done
    echo "$n"
}

# The block device's own write delta and what the sampled cgroups leave unaccounted for.
report_device() {
    local state=$1 attributed=$2 rows device=0 name dev bytes stacked
    if [[ -r $state/diskstats.base && -r $state/diskstats.end && -s $state/devices ]]; then
        sort -u "$state/devices" >"$state/devices.u"
        rows=$(diskstats_delta "$state/diskstats.base" "$state/diskstats.end" "$state/devices.u")
    fi
    if [[ -z ${rows:-} ]]; then
        echo "device writes (/proc/diskstats): none to reconcile (no block device appeared in a sampled io.stat, or /proc/diskstats was unreadable)"
        return 0
    fi
    # shellcheck disable=SC2046  # maj:min ids, split on purpose
    stacked=" $(stacked_ids $(cat "$state/devices.u") | tr '\n' ' ')"
    while read -r name dev bytes; do
        if [[ $stacked == *" $dev "* ]]; then
            echo "device wbytes (/proc/diskstats), $name ($dev): $(fmt_bytes "$bytes")  (stacked on another device listed here, which carries these bytes: not added)"
            continue
        fi
        echo "device wbytes (/proc/diskstats), $name ($dev): $(fmt_bytes "$bytes")"
        device=$((device + bytes))
    done <<<"$rows"
    if [[ ! -d $SYS_DEV_BLOCK && $(wc -l <<<"$rows") -gt 1 ]]; then
        echo "($SYS_DEV_BLOCK is unreadable: a device stacked on another is added twice, here and in the cgroup sums)"
    fi
    echo "attributed to sampled cgroups (containers + daemons + the command's cgroup): $(fmt_bytes "$attributed")"
    echo "unattributed = device - attributed: $(fmt_bytes $((device - attributed)))  (filesystem journal and metadata, writeback charged to no sampled cgroup, other jobs: the device delta is host-wide)"
}

# Print the end-of-run report. STATE has c/<id>/{caller,wmax,logmax,logmode}, d/<name>.{cg,base,end},
# meta (meta_exit, meta_secs, meta_samples).
report() {
    local state=$1 total=0 n=0 logtotal=0 logstat=0 logdocker=0 unreadable=0 dir w l mode
    for dir in "$state"/c/*/; do
        [[ -d $dir ]] || continue
        n=$((n + 1))
        w=0
        [[ -r $dir/wmax ]] && w=$(<"$dir/wmax")
        total=$((total + w))
        [[ -e $dir/wmax ]] || unreadable=$((unreadable + 1))
        l=0
        [[ -r $dir/logmax ]] && l=$(<"$dir/logmax")
        logtotal=$((logtotal + l))
        mode=none
        [[ -r $dir/logmode ]] && mode=$(<"$dir/logmode")
        case $mode in
            stat) logstat=$((logstat + 1)) ;;
            docker) logdocker=$((logdocker + 1)) ;;
        esac
    done
    local exit_code="?" secs="?" samples="?" meta_exit meta_secs meta_samples
    if [[ -r $state/meta ]]; then
        # shellcheck source=/dev/null
        . "$state/meta"
        exit_code=${meta_exit:-?}
        secs=${meta_secs:-?}
        samples=${meta_samples:-?}
    fi
    echo "== container io sampler =="
    echo "docker (driver, logging driver, debug, cgroup version): $(cat "$state/docker-info" 2>/dev/null || echo unavailable)"
    echo "command exit: $exit_code   duration: ${secs}s   samples: $samples   containers seen: $n"
    if [[ -s $state/timeouts ]]; then
        echo "BOUNDS THAT EXPIRED (those samples are missing; the numbers below are lower bounds): $(wc -l <"$state/timeouts" | tr -d ' ')"
        sort "$state/timeouts" | uniq -c | sed 's/^/    /'
    fi
    echo "containers whose cgroup io.stat was unreadable or never resolved: $unreadable"
    echo "sum of per-container wbytes (max per container, lower bound): $(fmt_bytes "$total")"
    local f name cg base end attributed=$total
    for f in "$state"/d/*.cg; do
        [[ -e $f ]] || continue
        name=$(basename "$f" .cg)
        cg=$(<"$f")
        if [[ -r $state/d/$name.base && -r $state/d/$name.end ]]; then
            base=$(<"$state/d/$name.base")
            end=$(<"$state/d/$name.end")
            attributed=$((attributed + end - base))
            echo "cgroup wbytes delta, $name (/${cg}): $(fmt_bytes $((end - base)))"
        else
            echo "cgroup wbytes delta, $name (/${cg}): unreadable (io.stat not readable as this user)"
        fi
    done
    for f in "$state"/d/*.skip; do
        [[ -e $f ]] || continue
        name=$(basename "$f" .skip)
        echo "cgroup wbytes delta, $name: skipped ($(<"$f"))"
    done
    report_device "$state" "$attributed"
    echo "json-log bytes total: $(fmt_bytes "$logtotal")  (${logstat} sized by stat of docker's LogPath, ${logdocker} by 'docker logs | wc -c' payload because the file was unreadable)"
    if ((logdocker > 0)); then
        echo "  (the 'docker logs' sizes are lower bounds taken every ${LOGS_EVERY}s: a container's last size before it died is missed; NX_IO_SAMPLER_LOGS_EVERY=1 for an A/B arm)"
    fi
    echo "top 10 containers by wbytes:"
    container_rows "$state" | awk 'NR <= 10 { printf "  %14d  %s  %s\n", $1, $2, $3 }'
    echo "top 10 callers (dev.nexus.test.caller) by summed wbytes:"
    caller_rows "$state" | awk 'NR <= 10 { printf "  %14d  %4d containers  %s\n", $1, $2, $3 }'
}

# ---- sampling ----------------------------------------------------------------------------

# Resolve one daemon (process name) to its cgroup and record the baseline. Files are named by the
# name as a safe path component, whatever -x was given.
daemon_baseline() {
    local state=$1 root=$2 name=$3 key pids pid cg w
    key=$(safe_key "$name")
    mkdir -p "$state/d"
    pids=$(pgrep -x "$name" 2>/dev/null || true)
    pid=${pids%%$'\n'*}
    if [[ -z $pid ]]; then
        echo "no process named $name" >"$state/d/$key.skip"
        return 0
    fi
    if ! cg=$(cgroup_path_from_proc "$PROC_ROOT/$pid/cgroup"); then
        echo "no cgroup v2 path for pid $pid" >"$state/d/$key.skip"
        return 0
    fi
    printf '%s\n' "$cg" >"$state/d/$key.cg"
    if w=$(sum_wbytes "$root/$cg/io.stat"); then
        printf '%s\n' "$w" >"$state/d/$key.base"
        note_devices "$state" "$root/$cg/io.stat"
    fi
}

daemon_end() {
    local state=$1 root=$2 f name cg w
    for f in "$state"/d/*.cg; do
        [[ -e $f ]] || continue
        name=$(basename "$f" .cg)
        cg=$(<"$f")
        if [[ -r $state/d/$name.base ]] && w=$(sum_wbytes "$root/$cg/io.stat"); then
            printf '%s\n' "$w" >"$state/d/$name.end"
            note_devices "$state" "$root/$cg/io.stat"
        fi
    done
}

# Whether a container's log is due a `docker logs` sizing: until one succeeds, every LOGS_EVERY seconds
# after the last that did, and on the final sample.
docker_log_due() {
    local dir=$1 last=""
    [[ -r $dir/logat ]] && last=$(<"$dir/logat")
    [[ -n $FINAL_TICK || -z $last ]] || ((SECONDS - last >= LOGS_EVERY))
}

# One tick: every running container, resolved once (cgroup, caller, log path), then its wbytes and log size.
sample_once() {
    local state=$1 root=$2 id dir info pid logpath caller cg w size
    while IFS= read -r id; do
        [[ -n $id ]] || continue
        dir=$state/c/$id
        if [[ ! -d $dir ]]; then
            info=$(dock inspect -f '{{.State.Pid}}|{{.LogPath}}|{{index .Config.Labels "dev.nexus.test.caller"}}' "$id" 2>/dev/null) || continue
            mkdir -p "$dir"
            IFS='|' read -r pid logpath caller <<<"$info"
            printf '%s\n' "${caller:--}" >"$dir/caller"
            printf '%s\n' "$logpath" >"$dir/logpath"
            if [[ $pid =~ ^[1-9][0-9]*$ ]] && cg=$(cgroup_path_from_proc "$PROC_ROOT/$pid/cgroup"); then
                printf '%s\n' "$cg" >"$dir/cg"
            fi
        fi
        if [[ -r $dir/cg ]]; then
            cg=$(<"$dir/cg")
            if w=$(sum_wbytes "$root/$cg/io.stat"); then
                record_max "$dir/wmax" "$w"
                if ((w > 0)); then note_devices "$state" "$root/$cg/io.stat"; fi
            fi
        fi
        logpath=$(<"$dir/logpath")
        if [[ -n $logpath ]] && size=$(stat -c %s "$logpath" 2>/dev/null || stat -f %z "$logpath" 2>/dev/null) && [[ $size =~ ^[0-9]+$ ]]; then
            record_max "$dir/logmax" "$size"
            echo stat >"$dir/logmode"
        # A container that died between the listing and now makes docker print an error and fail: that
        # text is not log. Pipefail carries docker's status through the pipe, so the size is skipped.
        elif docker_log_due "$dir" && size=$(dock logs "$id" 2>&1 | wc -c | tr -d ' ') && [[ $size =~ ^[0-9]+$ ]]; then
            record_max "$dir/logmax" "$size"
            echo "$SECONDS" >"$dir/logat"
            [[ -e $dir/logmode ]] || echo docker >"$dir/logmode"
        fi
    done < <(dock ps -q --no-trunc ${PS_FILTER[@]+"${PS_FILTER[@]}"} 2>/dev/null)
}

# Ticks until the stop file appears or PARENT (the script) is gone, so a SIGKILLed script leaves nothing running.
sampler_loop() {
    local state=$1 root=$2 interval=$3 parent=$4 n=0
    while [[ ! -e $state/stop ]] && kill -0 "$parent" 2>/dev/null; do
        sample_once "$state" "$root"
        n=$((n + 1))
        echo "$n" >"$state/samples.tmp" && mv -f "$state/samples.tmp" "$state/samples"  # a reader never sees it empty
        sleep "$interval"
    done
}

# After the command: tell the loop to stop, give it GRACE seconds, then kill it and say so in the report.
stop_sampler() {
    local state=$1 pid=$2 deadline
    touch "$state/stop"
    deadline=$((SECONDS + SAMPLER_GRACE + 1))
    while kill -0 "$pid" 2>/dev/null; do
        if ((SECONDS >= deadline)); then
            echo "sampler loop still running ${SAMPLER_GRACE}s after the command ended; killed" >>"$state/timeouts"
            kill_tree "$pid"
            break
        fi
        sleep 0.1
    done
    wait "$pid" 2>/dev/null
    return 0
}

SAMPLER_PID=""
PS_FILTER=() # extra docker listing arguments: --filter <expr> per -f

# A signal to this script stops the sampler; the command, in the same process group, gets it too.
on_signal() {
    [[ -n $STATE ]] && touch "$STATE/stop"
    [[ -n $SAMPLER_PID ]] && kill "$SAMPLER_PID" 2>/dev/null
}

usage() {
    sed -n '5,10p' "${BASH_SOURCE[0]}" | sed 's/^# \{0,1\}//' >&2
}

die() {
    echo "container_io_sampler: $*" >&2
}

main() {
    local interval=2 root=/sys/fs/cgroup state names=(dockerd containerd systemd-journal)
    while (($#)); do
        case $1 in
            -i | -x | -f | -r)
                (($# >= 2)) || { die "$1 needs an argument"; return 2; } ;;
        esac
        case $1 in
            -i)
                interval=$2
                positive_number "$interval" || { die "-i must be a positive number of seconds, got '$interval'"; return 2; }
                shift 2 ;;
            -x)
                [[ $2 == -* ]] && { die "-x name must not start with '-', got '$2'"; return 2; }
                names+=("$2"); shift 2 ;;
            -f)
                [[ $2 == *= ]] && {
                    die "filter '$2' has an empty value (is NX_TEST_RUN_ID exported?): it would match every container with an empty value"
                    return 2
                }
                [[ $2 == label=dev.nexus.test.run ]] && {
                    die "filter '$2' names the run label with no value: it would match every run's containers; give =\$NX_TEST_RUN_ID"
                    return 2
                }
                PS_FILTER+=(--filter "$2"); shift 2 ;;
            -r) root=$2; shift 2 ;;
            -h | --help) usage; return 0 ;;
            --) shift; break ;;
            *) break ;;
        esac
    done
    if (($# == 0)); then
        usage
        return 2
    fi
    local bound
    for bound in "$DOCKER_TIMEOUT" "$SAMPLER_GRACE" "$FINAL_SAMPLE_SECS" "$LOGS_EVERY"; do
        positive_int "$bound" || { die "the NX_IO_SAMPLER_DOCKER_TIMEOUT/GRACE/FINAL/LOGS_EVERY bounds must be positive whole seconds, got '$bound'"; return 2; }
    done
    if ! command -v docker >/dev/null 2>&1 || ! bounded "$DOCKER_TIMEOUT" docker ps -q >/dev/null 2>&1; then
        die "docker is not usable as this user (the listing failed or did not answer in ${DOCKER_TIMEOUT}s); not running the command"
        return 2
    fi
    state=$(mktemp -d "$(pick_state_base)/nx-io-sampler.XXXXXX") || return 2
    mkdir -p "$state/c" "$state/d"
    STATE=$state
    dock info --format '{{.Driver}} {{.LoggingDriver}} {{.Debug}} {{.CgroupVersion}}' >"$state/docker-info" 2>/dev/null || rm -f "$state/docker-info"
    cp "$PROC_ROOT/diskstats" "$state/diskstats.base" 2>/dev/null
    local name own_cg own_base="" own_end=""
    for name in "${names[@]}"; do
        daemon_baseline "$state" "$root" "$name"
    done
    if own_cg=$(cgroup_path_from_proc "$PROC_ROOT/self/cgroup"); then
        own_base=$(sum_wbytes "$root/$own_cg/io.stat" || true)
    fi

    sampler_loop "$state" "$root" "$interval" "$$" &
    SAMPLER_PID=$!
    local start=$SECONDS rc
    trap on_signal INT TERM
    "$@"
    rc=$?
    trap - INT TERM
    stop_sampler "$state" "$SAMPLER_PID"
    # one more look at what is still running
    FINAL_TICK=1
    bounded "$FINAL_SAMPLE_SECS" sample_once "$state" "$root" || echo "final sample did not finish in ${FINAL_SAMPLE_SECS}s" >>"$state/timeouts"
    daemon_end "$state" "$root"
    cp "$PROC_ROOT/diskstats" "$state/diskstats.end" 2>/dev/null
    if [[ -n $own_cg && -n $own_base ]]; then
        own_end=$(sum_wbytes "$root/$own_cg/io.stat" || true)
        if [[ -n $own_end ]]; then
            printf '%s\n' "$own_cg" >"$state/d/command-own-cgroup.cg"
            printf '%s\n' "$own_base" >"$state/d/command-own-cgroup.base"
            printf '%s\n' "$own_end" >"$state/d/command-own-cgroup.end"
            note_devices "$state" "$root/$own_cg/io.stat"
        fi
    fi
    printf 'meta_exit=%s\nmeta_secs=%s\nmeta_samples=%s\n' "$rc" "$((SECONDS - start))" "$(cat "$state/samples" 2>/dev/null || echo 0)" >"$state/meta"
    report "$state" >&2
    echo "(raw samples kept in $state)" >&2
    if ((${#PS_FILTER[@]} > 0)) && (($(count_containers "$state") == 0)); then
        echo "WARNING: -f was given and NO container was seen. The filter matched nothing (wrong NX_TEST_RUN_ID or label? the containers started and ended between two ticks? docker stalled?): the container figures above measure nothing. Exit status 3 unless the command itself failed." >&2
        if ((rc == 0)); then rc=3; fi
    fi
    return "$rc"
}

if [[ ${BASH_SOURCE[0]} == "$0" ]]; then
    main "$@"
fi
