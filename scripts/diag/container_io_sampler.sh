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
# At the end it prints: the sum of the per-container maxima; each daemon cgroup's wbytes delta;
# the json-log bytes total; the top 10 containers by wbytes with their dev.nexus.test.caller
# label, and the same summed by caller. The exit status is COMMAND's.
#
# Needs no root. What it cannot read it skips and says so in the report: a cgroup whose io.stat is
# unreadable, a json log under /var/lib/docker/containers (root-only on most hosts; the log is then
# sized by `docker logs | wc -c`, its payload without the json wrapper, so a lower bound).
# NOT SEEN: a container that starts and dies between two ticks, and the last SECS seconds of each
# container's life, so the container sum is a LOWER bound; lower -i to tighten it. Block-device
# writes only: tmpfs (the test clusters' data directory) is not one.
#
# The functions below are sourceable (tests/scripts/test_container_io_sampler.py); main runs only
# when the file is executed.

set -uo pipefail

# Where /proc is (tests point this at a fixture tree).
PROC_ROOT=${NX_IO_SAMPLER_PROC:-/proc}

# ---- parsing helpers (pure; unit-tested with fixture files) ---------------------------------

# wbytes summed over every device row of an io.stat file; fails (prints nothing) when unreadable.
sum_wbytes() {
    local file=$1
    [[ -r $file ]] || return 1
    awk '{ for (i = 1; i <= NF; i++) if ($i ~ /^wbytes=/) { split($i, kv, "="); t += kv[2] } } END { printf "%d\n", t }' "$file"
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
    echo "command exit: $exit_code   duration: ${secs}s   samples: $samples   containers seen: $n"
    echo "containers whose cgroup io.stat was unreadable or never resolved: $unreadable"
    echo "sum of per-container wbytes (max per container, lower bound): $(fmt_bytes "$total")"
    local f name cg base end
    for f in "$state"/d/*.cg; do
        [[ -e $f ]] || continue
        name=$(basename "$f" .cg)
        cg=$(<"$f")
        if [[ -r $state/d/$name.base && -r $state/d/$name.end ]]; then
            base=$(<"$state/d/$name.base")
            end=$(<"$state/d/$name.end")
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
    echo "json-log bytes total: $(fmt_bytes "$logtotal")  (${logstat} sized by stat of docker's LogPath, ${logdocker} by 'docker logs | wc -c' payload because the file was unreadable)"
    echo "top 10 containers by wbytes:"
    container_rows "$state" | head -10 | awk '{ printf "  %14d  %s  %s\n", $1, $2, $3 }'
    echo "top 10 callers (dev.nexus.test.caller) by summed wbytes:"
    caller_rows "$state" | head -10 | awk '{ printf "  %14d  %4d containers  %s\n", $1, $2, $3 }'
}

# ---- sampling ----------------------------------------------------------------------------

# Resolve one daemon (process name) to its cgroup and record the baseline.
daemon_baseline() {
    local state=$1 root=$2 name=$3 pid cg w
    mkdir -p "$state/d"
    pid=$(pgrep -x "$name" 2>/dev/null | head -1)
    if [[ -z $pid ]]; then
        echo "no process named $name" >"$state/d/$name.skip"
        return 0
    fi
    if ! cg=$(cgroup_path_from_proc "$PROC_ROOT/$pid/cgroup"); then
        echo "no cgroup v2 path for pid $pid" >"$state/d/$name.skip"
        return 0
    fi
    printf '%s\n' "$cg" >"$state/d/$name.cg"
    if w=$(sum_wbytes "$root/$cg/io.stat"); then
        printf '%s\n' "$w" >"$state/d/$name.base"
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
        fi
    done
}

# One tick: every running container, resolved once (cgroup, caller, log path), then its wbytes and log size.
sample_once() {
    local state=$1 root=$2 id dir info pid logpath caller cg w size
    while IFS= read -r id; do
        [[ -n $id ]] || continue
        dir=$state/c/$id
        if [[ ! -d $dir ]]; then
            info=$(docker inspect -f '{{.State.Pid}}|{{.LogPath}}|{{index .Config.Labels "dev.nexus.test.caller"}}' "$id" 2>/dev/null) || continue
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
            fi
        fi
        logpath=$(<"$dir/logpath")
        if [[ -n $logpath ]] && size=$(stat -c %s "$logpath" 2>/dev/null || stat -f %z "$logpath" 2>/dev/null) && [[ $size =~ ^[0-9]+$ ]]; then
            record_max "$dir/logmax" "$size"
            echo stat >"$dir/logmode"
        else
            size=$(docker logs "$id" 2>&1 | wc -c | tr -d ' ')
            if [[ $size =~ ^[0-9]+$ ]]; then
                record_max "$dir/logmax" "$size"
                [[ -e $dir/logmode ]] || echo docker >"$dir/logmode"
            fi
        fi
    done < <(docker ps -q --no-trunc ${PS_FILTER[@]+"${PS_FILTER[@]}"} 2>/dev/null)
}

sampler_loop() {
    local state=$1 root=$2 interval=$3 n=0
    while [[ ! -e $state/stop ]]; do
        sample_once "$state" "$root"
        n=$((n + 1))
        echo "$n" >"$state/samples"
        sleep "$interval"
    done
}

STATE=""
SAMPLER_PID=""
PS_FILTER=() # extra `docker ps` arguments: --filter <expr> per -f

# A signal to this script stops the sampler; the command, in the same process group, gets it too.
on_signal() {
    [[ -n $STATE ]] && touch "$STATE/stop"
    [[ -n $SAMPLER_PID ]] && kill "$SAMPLER_PID" 2>/dev/null
}

usage() {
    sed -n '5,10p' "${BASH_SOURCE[0]}" | sed 's/^# \{0,1\}//' >&2
}

main() {
    local interval=2 root=/sys/fs/cgroup state names=(dockerd containerd systemd-journal)
    while (($#)); do
        case $1 in
            -i) interval=${2:?-i needs seconds}; shift 2 ;;
            -x) names+=("${2:?-x needs a process name}"); shift 2 ;;
            -f) PS_FILTER+=(--filter "${2:?-f needs a docker ps filter}"); shift 2 ;;
            -r) root=${2:?-r needs a directory}; shift 2 ;;
            -h | --help) usage; return 0 ;;
            --) shift; break ;;
            *) break ;;
        esac
    done
    if (($# == 0)); then
        usage
        return 2
    fi
    if ! command -v docker >/dev/null 2>&1 || ! docker ps -q >/dev/null 2>&1; then
        echo "container_io_sampler: docker is not usable as this user (docker ps failed); not running the command" >&2
        return 2
    fi
    state=$(mktemp -d "${TMPDIR:-/tmp}/nx-io-sampler.XXXXXX") || return 2
    mkdir -p "$state/c" "$state/d"
    local name own_cg own_base="" own_end=""
    for name in "${names[@]}"; do
        daemon_baseline "$state" "$root" "$name"
    done
    if own_cg=$(cgroup_path_from_proc "$PROC_ROOT/self/cgroup"); then
        own_base=$(sum_wbytes "$root/$own_cg/io.stat" || true)
    fi

    STATE=$state
    sampler_loop "$state" "$root" "$interval" &
    SAMPLER_PID=$!
    local start=$SECONDS rc
    trap on_signal INT TERM
    "$@"
    rc=$?
    trap - INT TERM
    touch "$state/stop"
    wait "$SAMPLER_PID" 2>/dev/null
    sample_once "$state" "$root" # one more look at what is still running
    daemon_end "$state" "$root"
    if [[ -n $own_cg && -n $own_base ]]; then
        own_end=$(sum_wbytes "$root/$own_cg/io.stat" || true)
        if [[ -n $own_end ]]; then
            printf '%s\n' "$own_cg" >"$state/d/command-own-cgroup.cg"
            printf '%s\n' "$own_base" >"$state/d/command-own-cgroup.base"
            printf '%s\n' "$own_end" >"$state/d/command-own-cgroup.end"
        fi
    fi
    printf 'meta_exit=%s\nmeta_secs=%s\nmeta_samples=%s\n' "$rc" "$((SECONDS - start))" "$(cat "$state/samples" 2>/dev/null || echo 0)" >"$state/meta"
    report "$state" >&2
    echo "(raw samples kept in $state)" >&2
    return "$rc"
}

if [[ ${BASH_SOURCE[0]} == "$0" ]]; then
    main "$@"
fi
