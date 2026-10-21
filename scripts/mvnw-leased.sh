#!/usr/bin/env bash
# scripts/mvnw-leased.sh — thin wrapper around service/mvnw that takes the
# nexus-c00dw single-builder lease first, so an orchestrator invocation and
# a developer-agent invocation of ./mvnw never write service/target at the
# same time (see scripts/lib/build-lease.sh for the mechanism and the
# incident that motivated it).
#
# usage: scripts/mvnw-leased.sh [--lab-run <class>] [mvnw args...]
#
# On a lab pool host (chas, qwent) pass --lab-run <class> rather than
# wrapping this script in lab-run: the lease is then taken before the pool
# grant, so a build waiting its turn holds no cores (nexus-iqmih).
#
# A live holder is WAITED FOR (nexus-g6xpa): the lease is shared by every
# worktree of this repo (see scripts/lib/build-lease.sh § LEASE LOCATION),
# so a second engine build or suite on this box queues behind the first
# instead of running beside it. NX_BUILD_LEASE_WAIT=<seconds> bounds the
# wait (default 3600); 0 restores the old refuse-immediately behaviour.
# Exits 75 (EX_TEMPFAIL) without running mvnw at all only once that bound
# is exhausted — the refusal message names the holder. Inside a lab-run grant
# (LAB_CPUS set, not GitHub Actions) a held lease refuses at once with 75,
# whatever NX_BUILD_LEASE_WAIT says (nexus-dfg0q).
#
# SIGNAL HANDLING (review finding round 3, nexus-c00dw). A plain
# `trap '...release...' EXIT` is UNSAFE here. Under `set -m` the
# backgrounded ./mvnw child lives in its OWN process group, separate from
# this wrapper's — so SIGINT (Ctrl-C) or SIGTERM (a `timeout`, or an
# orchestrator killing just this wrapper's pid) reaches ONLY the wrapper,
# never the child. With no INT/TERM trap installed, bash's default
# disposition terminates the wrapper on that signal but still runs the
# EXIT trap first (the exact mechanism proven safe-in-isolation by
# scripts/lib/build-lease_test.sh's "SIGTERM'd holder releases via its own
# EXIT trap" case) — so an UNCONDITIONAL release there frees the lease
# while the real build (the child, still alive in its own group) keeps
# writing to service/target. Two changes close it:
#   1. The EXIT trap is now CONDITIONAL: it releases only when the tracked
#      build (child_pgid) is confirmed dead, or was never launched at all.
#      If it is still alive, the lease is left HELD with a WARNING naming
#      the live pgid — a future stale-reclaim picks it up once the child
#      actually exits; it is never silently double-held either.
#   2. INT and TERM traps FORWARD the signal to the child's process group,
#      wait (bounded, 30s) for it to actually exit, escalate to KILL if it
#      does not, and only then exit — so the now-safe EXIT trap releases
#      cleanly instead of abandoning a still-running build.
# Both now live in scripts/lib/leased-build.sh, shared with build-gate-jar.sh
# (nexus-t6t9f). Behaviour change there: HUP and QUIT are trapped and forwarded
# like INT and TERM (a closed terminal used to kill the wrapper with the build
# alive and the lease left held), and the wrapper exits 130 on any of the four
# (TERM used to end it with 143).

set -euo pipefail
set -m
# Job control (review finding round 2, nexus-c00dw): with `set -m`, a
# backgrounded job becomes its own process-GROUP leader — its pgid equals
# its own pid. Tracking the child's process group instead of just the
# wrapper's own pid is what makes round 3's signal handling above possible
# — see build_lease_track_pid / _build_lease_group_alive in
# scripts/lib/build-lease.sh.

repo_root="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
# shellcheck source=./lib/build-lease.sh disable=SC1091
source "$repo_root/scripts/lib/build-lease.sh"
# shellcheck source=./lib/leased-build.sh disable=SC1091
source "$repo_root/scripts/lib/leased-build.sh"

# --lab-run <class> (nexus-iqmih): run the build as `lab-run -c <class> --
# ./mvnw ...` AFTER taking the lease. The other order, `lab-run -- this
# script`, waits for the lease inside the pool grant: on chas two lab-m
# grants sat idle behind one lease holder while other requests queued
# behind them. Waiting here, outside any grant, holds no cores or memory.
lab_class=""
if [[ "${1:-}" == "--lab-run" ]]; then
    leased_build_check_lab_run mvnw-leased.sh "${2:-}"
    lab_class="$2"
    shift 2
fi

# Inside a lab-run grant a hand run refuses at once instead of waiting
# (build_lease_caller_wait, nexus-dfg0q).
build_lease_acquire_wait service "$(build_lease_caller_wait)" ./mvnw "$@" || {
    rc=$?
    [[ $rc -ne 75 ]] || build_lease_grant_refusal_hint mvnw-leased.sh "take the lease before the grant: scripts/mvnw-leased.sh --lab-run <class> ... outside lab-run"
    exit "$rc"
}

# Test Postgres containers are labelled dev.nexus.test.run=$NX_TEST_RUN_ID
# (nexus-xynce) so a host can count one build's containers; name this build
# unless the caller already did.
export NX_TEST_RUN_ID="${NX_TEST_RUN_ID:-mvnw-$(id -un 2>/dev/null || echo u)-$$-$(date +%s)}"

# Releases only once the build is gone, and forwards INT/TERM to its process
# group (scripts/lib/leased-build.sh).
leased_build_init mvnw-leased.sh service

# A gate-jar cache hit or store leaves service/target's jar hard-linked to the
# cache entry (nexus-k6zxp), and maven-jar-plugin rewrites an existing jar IN
# PLACE: through the link that would corrupt the entry (and on the read-only
# entry it fails the build). A goal that reaches the package phase therefore
# unlinks a shared jar first; a goal that stops short of it (compile, test)
# leaves the jar, and the cache link, alone. A plugin goal that writes the jar
# directly counts too (jar:jar, shade:shade, org.apache.maven.plugins:
# maven-jar-plugin:3.4.1:jar, ...), as does any phase that runs after package.
# Each argument is split on whitespace first: one quoted argument such as
# "clean package" reaches mvnw as one word but names the package phase
# (nexus-8qdpe). `${_words[@]+...}` keeps an empty argument safe under set -u on
# bash 3.2.
_unshare_needed=0
for _arg in "$@"; do
    read -r -a _words <<< "$_arg"
    for _word in ${_words[@]+"${_words[@]}"}; do
        case "$_word" in
            package|verify|install|deploy|pre-integration-test|integration-test|post-integration-test|\
            *:jar|*:shade|*jar-plugin*|*shade-plugin*)
                _unshare_needed=1 ;;
        esac
    done
done
if [[ $_unshare_needed -eq 1 ]]; then
    # shellcheck source=./lib/gate-jar-cache.sh disable=SC1091
    source "$repo_root/scripts/lib/gate-jar-cache.sh"
    gate_jar_cache_unshare "$repo_root/service/target/nexus-service-1.0-SNAPSHOT.jar"
fi

cd "$repo_root/service"
# A non-zero exit here exits this script with the same code (set -e).
leased_build_run "$lab_class" ./mvnw "$@"
