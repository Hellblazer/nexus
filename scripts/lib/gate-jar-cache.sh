#!/usr/bin/env bash
# scripts/lib/gate-jar-cache.sh — content-keyed cache of the stamped gate
# jar (bead nexus-g6xpa). Sourced, never executed. Used by
# scripts/build-gate-jar.sh; nothing else should write the cache.
#
# WHY: a fresh worktree has no service/target, so every worktree agent paid
# a full ~9 minute `mvnw package` for a jar byte-identical to the primary's
# (measured 2026-09-07, three agents in one afternoon). The build is a pure
# function of the service/ tree, the stamped release_version, and the extra
# Maven args, so it is cached on exactly that key and rebuilt only on a miss
# — "never rebuild deterministic artifacts", applied to the dev box.
#
# KEY. `gate_jar_cache_key <repo> <release_version> <mvn-args>` stages the
# service/ directory of the WORKING TREE into a throwaway copy of the
# checkout's index (`git add -A -- service` under GIT_INDEX_FILE) and takes
# the resulting subtree hash: tracked, modified, and untracked-but-not-
# ignored files all count; service/target and service/.build-lease do not
# (gitignored). That hash, the release_version and the args are sha256'd
# into the key. Two worktrees with identical service/ content share a key
# regardless of which commits they sit on.
#
# LOCATION. $NX_GATE_JAR_CACHE if set to a directory; "off" disables both
# lookup and store; else <git common dir>/nexus-gate-jar-cache (shared by
# every worktree), else <repo>/service/.gate-jar-cache. Each entry is a
# directory <root>/<key>/ holding `jar` and `build_ref`, published by
# rename so a reader never sees a half-copied jar. The newest
# GATE_JAR_CACHE_KEEP entries survive a store; older ones are pruned.
#
# STAMP LINES ARE NORMALIZED OUT OF THE KEY. release.properties carries
# `release_version=` and `build_ref=` blank in source and stamped in place
# during a build (restored on exit). If a crashed run ever leaves the stamp
# behind, hashing it verbatim would give every later invocation a key no
# build can match — a silently defeated cache, never a wrong jar. So the
# key stages the file with those two lines blanked (the release_version is
# a separate key component already).
#
# A hit is verified before use: the cached jar must open as a zip and carry
# META-INF/MANIFEST.MF (the same completeness test
# tests/db/_service_fixture.py applies), else it is a miss and the entry is
# discarded.
#
# `git write-tree` writes real loose objects into the repo's object store
# on every key computation (the staged blobs and trees). They are ordinary
# unreachable objects; `git gc` / `git prune` reap them on the usual
# schedule. Documented so nobody mistakes them for corruption.

set -u -o pipefail

GATE_JAR_CACHE_KEEP="${GATE_JAR_CACHE_KEEP:-5}"

gate_jar_cache_root() {
    local repo="${1:?gate_jar_cache_root: usage: gate_jar_cache_root <repo>}"
    case "${NX_GATE_JAR_CACHE:-}" in
        off|OFF|0) return 1 ;;
        "") ;;
        *) printf '%s\n' "$NX_GATE_JAR_CACHE"; return 0 ;;
    esac
    local common
    if common="$(git -C "$repo" rev-parse --path-format=absolute --git-common-dir 2>/dev/null)" && [[ -n "$common" ]]; then
        printf '%s/nexus-gate-jar-cache\n' "$common"
        return 0
    fi
    printf '%s/service/.gate-jar-cache\n' "$repo"
}

# gate_jar_cache_key <repo> <release_version> <mvn-args-string>
gate_jar_cache_key() {
    local repo="${1:?gate_jar_cache_key: usage: gate_jar_cache_key <repo> <release_version> <mvn-args>}"
    local ver="${2:?gate_jar_cache_key: release_version required}"
    local args="${3:-}"
    local idx tree subtree
    idx="$(mktemp "${TMPDIR:-/tmp}/gate-jar-index.XXXXXX")"
    # Start from the checkout's own index so `add -A` only re-hashes what
    # changed; a missing index (fresh clone) just makes it slower.
    cp "$(git -C "$repo" rev-parse --path-format=absolute --git-path index)" "$idx" 2>/dev/null || true
    if ! GIT_INDEX_FILE="$idx" git -C "$repo" add -A -- service >/dev/null 2>&1; then
        rm -f "$idx"
        echo "gate_jar_cache_key: could not stage service/ into a throwaway index" >&2
        return 1
    fi
    # Normalize the stamp lines (see header) if release.properties is staged.
    local props="service/src/main/resources/META-INF/nexus/release.properties" blob
    if GIT_INDEX_FILE="$idx" git -C "$repo" ls-files --error-unmatch -- "$props" >/dev/null 2>&1; then
        blob="$(sed -E 's/^(release_version|build_ref)=.*/\1=/' "$repo/$props" | git -C "$repo" hash-object -w --stdin)"
        GIT_INDEX_FILE="$idx" git -C "$repo" update-index --cacheinfo "100644,$blob,$props" >/dev/null 2>&1 || {
            rm -f "$idx"; echo "gate_jar_cache_key: could not normalize $props in the throwaway index" >&2; return 1; }
    fi
    tree="$(GIT_INDEX_FILE="$idx" git -C "$repo" write-tree 2>/dev/null)"
    rm -f "$idx"
    [[ -n "$tree" ]] || { echo "gate_jar_cache_key: write-tree failed" >&2; return 1; }
    subtree="$(git -C "$repo" rev-parse --verify --quiet "$tree:service" 2>/dev/null)" || {
        echo "gate_jar_cache_key: no service/ subtree in $repo" >&2
        return 1
    }
    printf 'service=%s release_version=%s args=%s\n' "$subtree" "$ver" "$args" | shasum -a 256 | cut -c1-40
}

_gate_jar_cache_entry_ok() {
    local entry="$1"
    [[ -f "$entry/jar" && -f "$entry/build_ref" ]] || return 1
    python3 - "$entry/jar" <<'PY' >/dev/null 2>&1
import sys, zipfile
with zipfile.ZipFile(sys.argv[1]) as zf:
    zf.read("META-INF/MANIFEST.MF")
PY
}

# gate_jar_cache_lookup <repo> <key> — prints the entry dir on a hit (rc 0);
# rc 1 on a miss or when the cache is off. A corrupt entry is removed.
gate_jar_cache_lookup() {
    local repo="${1:?gate_jar_cache_lookup: usage: gate_jar_cache_lookup <repo> <key>}"
    local key="${2:?gate_jar_cache_lookup: key required}"
    local root entry
    root="$(gate_jar_cache_root "$repo")" || return 1
    entry="$root/$key"
    [[ -d "$entry" ]] || return 1
    if _gate_jar_cache_entry_ok "$entry"; then
        touch "$entry" 2>/dev/null || true   # recency for pruning
        printf '%s\n' "$entry"
        return 0
    fi
    echo "gate_jar_cache_lookup: discarding corrupt cache entry $entry" >&2
    rm -rf "$entry"
    return 1
}

# gate_jar_cache_store <repo> <key> <jar-path> <build_ref>
gate_jar_cache_store() {
    local repo="${1:?gate_jar_cache_store: usage: gate_jar_cache_store <repo> <key> <jar> <build_ref>}"
    local key="${2:?key required}" jar="${3:?jar required}" build_ref="${4:?build_ref required}"
    local root staging entry
    root="$(gate_jar_cache_root "$repo")" || return 0
    [[ -f "$jar" ]] || { echo "gate_jar_cache_store: no jar at $jar" >&2; return 1; }
    mkdir -p "$root" || return 1
    staging="$(mktemp -d "$root/.staging.XXXXXX")" || return 1
    if ! cp "$jar" "$staging/jar" || ! printf '%s\n' "$build_ref" > "$staging/build_ref"; then
        rm -rf "$staging"
        return 1
    fi
    entry="$root/$key"
    # Publish by rename: an existing entry is moved aside first (a reader
    # sees the old complete entry or the new complete one, never a gap or
    # a half-copied jar), then discarded.
    local aside="$entry.old.$$"
    [[ -d "$entry" ]] && mv "$entry" "$aside" 2>/dev/null
    if ! mv "$staging" "$entry"; then
        rm -rf "$staging"
        [[ -d "$aside" ]] && mv "$aside" "$entry" 2>/dev/null
        return 1
    fi
    rm -rf "${aside:?}"
    _gate_jar_cache_prune "$root"
    return 0
}

_gate_jar_cache_prune() {
    local root="$1" n=0 d
    # ls -t: newest first; keep GATE_JAR_CACHE_KEEP, drop the rest.
    while IFS= read -r d; do
        [[ -n "$d" && -d "$root/$d" ]] || continue
        [[ "$d" == .staging.* || "$d" == *.old.* ]] && continue
        n=$((n + 1))
        if (( n > GATE_JAR_CACHE_KEEP )); then
            rm -rf "${root:?}/${d:?}"
        fi
    done < <(ls -t "$root" 2>/dev/null)
}
