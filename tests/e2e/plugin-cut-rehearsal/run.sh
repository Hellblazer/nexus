#!/usr/bin/env bash
# tests/e2e/plugin-cut-rehearsal/run.sh — end-to-end rehearsal of an RDR-197
# plugin cut (plugin-vX.Y.Z-n) against a FAKE origin. Nothing here touches
# github.com: the clone's `origin` is a bare repository inside the sandbox.
#
# Why this exists: the channel shipped with unit fixtures of every piece and
# deferred the end-to-end exercise to "a spike cut against the real repo"
# (nexus-a2wmi.12). That spike, 2026-08-30, refused three times in a row on
# machinery that only ever runs at cut time — the ledger rewrite, the proof
# target before the tag exists, a pin-shape parse in a hooks test — each
# round costing a PR to main. Every one of them is reachable here without a
# push. Same doctrine as tests/e2e/migration-rehearsal for the engine.
#
# What it walks, in order (each step is what the real flow does):
#   1. fake bare origin; clone of the source checkout with `origin` rewired;
#      local `main` and the source HEAD (as `develop`) pushed there, tags too;
#      UNCOMMITTED working-tree changes of the source (tracked diff AND
#      untracked non-ignored files) are committed on the clone's develop,
#      so machinery fixes are rehearsed before they land
#   2. `uv sync --group dev` in the clone (what CI does)
#   3. the real `scripts/cut_plugin_release.py <base-tag> --repo <clone>`,
#      INCLUDING its own battery (lint bucket, ledger contract, tests/hooks/,
#      release-sandbox smoke)
#   4. the cut PR's CI, shape-faithfully: the branch pushed to the fake
#      origin, a real `--no-ff` merge into main published as refs/pull/1/merge,
#      a DEPTH-1 checkout of that ref (what actions/checkout gives a
#      pull_request job — no tags, no branch), plugin-drift-ledger.yml's own
#      fetch step (shallow --tags, the PR head sha, each pinned ref with the
#      release-window shapes tolerated), a pull_request event payload carrying
#      the cut's head sha, GITHUB_HEAD_REF set, and the checks that read the
#      pin or the proof target under that environment with the require flag
#      and vacuity grep
#   5. merge into main and the annotated anchored tag, pushed to the fake origin
#   6. the tag workflow's steps (.github/workflows/plugin-release.yml) on a
#      DEPTH-1 checkout of the tag with ONLY the derived base tag fetched (the
#      workflow's own derive step, replicated): drift ledger (+ vacuity grep),
#      plugin structure, tests/hooks/, the lint bucket minus the wire-contract
#      module, check_cut_ledger_clean.py
#   7. back-merge main -> develop via scripts/plugin_cut_back_merge.sh (the
#      cut's ledger rewrite conflicts with develop by construction) and
#      develop's own drift contract afterwards
#
# Modes:
#   (default)   container — builds the image from ./Dockerfile and runs this
#               script inside it with the source checkout bind-mounted
#               read-only; nothing on the host is written except the image
#               and two named cache volumes (uv, nexus downloads)
#   --host      run directly on this box (git isolation only; the cut's
#               battery already sandboxes $HOME for the smoke)
# Options:
#   --promote-machinery land develop's NEVER-DELIVERED paths (tests/ docs/
#                       scripts/ .github/) on the fake main first, as the
#                       machinery PR to main would; without it the cut
#                       branches off main's copies exactly as a real cut
#                       does, and the rehearsal NAMES the drift so nobody
#                       attempts a real cut with machinery still on develop
#   --repo <path>       source checkout (default: this repo)
#   --base-tag vX.Y.Z   the released client tag to anchor to
#                       (default: v<pyproject version>)
#   --keep              keep the sandbox on success (always kept on failure)
#
# Verdict line (grep for it): PLUGIN-CUT REHEARSAL PASSED — <tag> / FAILED at step N.
#
# T2 substrate: the rehearsal exports NX_TEST_T2_SUBSTRATE=none and
# GITHUB_ACTIONS=true — the GitHub runners' own shape (plugin-release.yml,
# ci.yml's lint job): engine-substrate tests skip as they do there. The
# subject here is the channel machinery, none of which needs an engine.
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
DEFAULT_REPO="$(cd "$SCRIPT_DIR/../../.." && pwd)"

MODE=container
KEEP=0
PROMOTE=0
BASE_TAG=""
SOURCE_REPO="$DEFAULT_REPO"
IMAGE="nexus-plugin-cut-rehearsal:latest"

usage() { sed -n '2,45p' "${BASH_SOURCE[0]}" | sed 's/^# \{0,1\}//'; }

while [ $# -gt 0 ]; do
    case "$1" in
        --host) MODE=host ;;
        --container) MODE=container ;;
        --keep) KEEP=1 ;;
        --promote-machinery) PROMOTE=1 ;;
        --base-tag) BASE_TAG="$2"; shift ;;
        --repo) SOURCE_REPO="$2"; shift ;;
        -h|--help) usage; exit 0 ;;
        *) echo "unknown argument: $1" >&2; usage >&2; exit 2 ;;
    esac
    shift
done

SOURCE_REPO="$(cd "$SOURCE_REPO" && pwd)"
if [ -z "$BASE_TAG" ]; then
    BASE_TAG="v$(python3 -c "import tomllib,sys;print(tomllib.load(open(sys.argv[1],'rb'))['project']['version'])" "$SOURCE_REPO/pyproject.toml")"
fi

# ── container mode: build the image, re-enter this script inside it ─────────
if [ "$MODE" = container ]; then
    command -v docker >/dev/null || { echo "docker not found; use --host" >&2; exit 2; }
    # Docker Desktop's credsStore=desktop helper cannot reach a locked login
    # keychain in a non-interactive session, which fails even anonymous base-
    # image resolution at BUILD time (docker run is unaffected). Same
    # workaround as tests/e2e/migration-rehearsal/run.sh: strip credsStore
    # for the build, restore on exit.
    DCFG="$HOME/.docker/config.json"
    DCFG_BAK=""
    if [ -f "$DCFG" ] && grep -q '"credsStore"' "$DCFG"; then
        DCFG_BAK="$(mktemp "${TMPDIR:-/tmp}/docker-config.XXXXXX")"
        cp "$DCFG" "$DCFG_BAK"
        python3 -c "import json,sys;p=sys.argv[1];d=json.load(open(p));d.pop('credsStore',None);json.dump(d,open(p,'w'),indent=2)" "$DCFG"
        trap 'cp "$DCFG_BAK" "$DCFG"; rm -f "$DCFG_BAK"' EXIT
        echo "   (temporarily stripped credsStore from $DCFG for the build; restored on exit)"
    fi
    echo "== building $IMAGE"
    docker build -q -t "$IMAGE" -f "$SCRIPT_DIR/Dockerfile" "$SCRIPT_DIR" >/dev/null
    # The sandbox stays on the container's OWN filesystem (a bind-mounted
    # working tree is virtiofs: racily-clean index entries made every
    # `git apply --index` refuse, and no runner or developer ships on that).
    # On failure the logs and git state are copied to this host directory,
    # so they survive the container's --rm.
    HOST_ARTIFACTS="${TMPDIR:-/tmp}/plugin-cut-rehearsal-container"
    mkdir -p "$HOST_ARTIFACTS"
    echo "== running the rehearsal in $IMAGE (source bind-mounted read-only at /src; failure artifacts under $HOST_ARTIFACTS)"
    keep_flag=(); [ "$KEEP" = 1 ] && keep_flag+=(--keep); [ "$PROMOTE" = 1 ] && keep_flag+=(--promote-machinery)
    # /home/nexus is a tmpfs: the clone, its venv and the smoke's sandbox in
    # RAM. On the overlay filesystem the engine's first-boot Liquibase walk
    # took 47 s per step and the supervisor's readiness window expired
    # (container run 4; on tmpfs all 354 changesets walk in under a second).
    # The uv cache volume and the artifacts dir are mounted beneath it.
    docker run --rm --init \
        --tmpfs /home/nexus:exec,uid=1000,gid=1000,size=16g \
        -v "$SOURCE_REPO:/src:ro" \
        -v "$HOST_ARTIFACTS:/home/nexus/artifacts" \
        -v nexus-plugin-cut-rehearsal-uv-cache:/home/nexus/uv-cache \
        -e UV_CACHE_DIR=/home/nexus/uv-cache \
        -e NX_REHEARSAL_ARTIFACTS=/home/nexus/artifacts \
        -e NX_REHEARSAL_IN_CONTAINER=1 \
        "$IMAGE" bash /src/tests/e2e/plugin-cut-rehearsal/run.sh \
            --host --repo /src --base-tag "$BASE_TAG" "${keep_flag[@]}"
    exit $?
fi

# ── host mode: the rehearsal proper ────────────────────────────────────────
# The rehearsal's clone has no service jar (build output is untracked), so it
# runs in the SHAPE the GitHub runners have: NX_TEST_T2_SUBSTRATE=none is
# plugin-release.yml's and ci.yml's lint job's own opt-out, and
# GITHUB_ACTIONS=true is what makes an explicitly requested engine substrate
# SKIP with CI's reason (tests/conftest.py t2_service_env) instead of raising
# "service jar not built". The first host run errored two hooks tests for
# exactly that difference. What this does NOT rehearse: the developer-box
# battery WITH a live engine, which the real cut runs on top of this.
export NX_TEST_T2_SUBSTRATE="${NX_TEST_T2_SUBSTRATE:-none}"
export GITHUB_ACTIONS="${GITHUB_ACTIONS:-true}"
export GIT_AUTHOR_NAME="${GIT_AUTHOR_NAME:-plugin-cut-rehearsal}"
export GIT_AUTHOR_EMAIL="${GIT_AUTHOR_EMAIL:-rehearsal@invalid}"
export GIT_COMMITTER_NAME="$GIT_AUTHOR_NAME"
export GIT_COMMITTER_EMAIL="$GIT_AUTHOR_EMAIL"

SANDBOX="$(mktemp -d "${TMPDIR:-/tmp}/plugin-cut-rehearsal.XXXXXX")"
ORIGIN="$SANDBOX/origin.git"
CLONE="$SANDBOX/repo"
STEP=0
PASSED=0

_step() { STEP=$((STEP + 1)); echo; echo "== step $STEP: $*"; }
_die()  { echo "PLUGIN-CUT REHEARSAL FAILED at step $STEP: $*" >&2; echo "sandbox kept: $SANDBOX" >&2; exit 1; }
cleanup() {
    if [ "$PASSED" = 1 ] && [ "$KEEP" = 0 ]; then rm -rf "$SANDBOX"; return; fi
    echo "sandbox: $SANDBOX"
    # Inside the container the sandbox dies with it: copy what a diagnosis
    # needs (the logs, the clone's branch/commit/status) to the host mount.
    if [ "$PASSED" = 0 ] && [ -n "${NX_REHEARSAL_ARTIFACTS:-}" ] && [ -d "$NX_REHEARSAL_ARTIFACTS" ]; then
        dest="$NX_REHEARSAL_ARTIFACTS/$(basename "$SANDBOX")"
        mkdir -p "$dest"
        cp "$SANDBOX"/*.log "$SANDBOX"/*.txt "$SANDBOX"/*.json "$dest"/ 2>/dev/null || true
        if [ -d "$CLONE/.git" ]; then
            { git -C "$CLONE" rev-parse --abbrev-ref HEAD; git -C "$CLONE" log --oneline -8; git -C "$CLONE" status --short; } > "$dest/git-state.txt" 2>&1 || true
        fi
        # The cut's battery runs release-sandbox smoke under $HOME/nexus-sandbox;
        # its engine/PG logs are the diagnosis when `nx init` fails to serve.
        if [ -d "$HOME/nexus-sandbox/.config/nexus/logs" ]; then
            mkdir -p "$dest/nexus-sandbox-logs"
            cp "$HOME"/nexus-sandbox/.config/nexus/logs/*.log "$dest/nexus-sandbox-logs/" 2>/dev/null || true
        fi
        echo "failure artifacts copied to $dest"
    fi
}
trap cleanup EXIT

# The clone is the only checkout any git command below touches. Never the source.
g() { git -C "$CLONE" "$@"; }
# Every pytest/uv invocation runs in the clone's own environment.
uvr() { (cd "$CLONE" && uv run "$@"); }

_step "fake origin + clone of $SOURCE_REPO (base tag $BASE_TAG)"
git init -q --bare "$ORIGIN"
git clone -q "$SOURCE_REPO" "$CLONE"
g remote set-url origin "$ORIGIN"
# origin/main FIRST: the real cut branches off origin/main and judges
# readiness there, and a local `main` is routinely stale in this project's
# batched-push workflow (the first host run rehearsed a main three merges
# behind and faithfully reproduced a defect that had already been fixed).
MAIN_SHA="$(git -C "$SOURCE_REPO" rev-parse --verify --quiet origin/main || git -C "$SOURCE_REPO" rev-parse --verify --quiet main)" \
    || _die "source has neither origin/main nor a local main"
DEV_SHA="$(git -C "$SOURCE_REPO" rev-parse HEAD)"
DEV_NAME="$(git -C "$SOURCE_REPO" rev-parse --abbrev-ref HEAD 2>/dev/null || echo detached)"
echo "   rehearsing the source's $DEV_NAME @ ${DEV_SHA:0:9} as develop; main from ${MAIN_SHA:0:9}"
g branch -q -f main "$MAIN_SHA"
g checkout -q -B develop "$DEV_SHA"
# The source's UNCOMMITTED state rides along: tracked changes as a diff, and
# untracked (non-ignored) files copied in — a new script or test that is not
# yet `git add`ed is exactly the machinery a rehearsal exists to try (run 6
# lost scripts/plugin_cut_back_merge.sh this way).
UNTRACKED="$(git -C "$SOURCE_REPO" ls-files --others --exclude-standard)"
if ! git -C "$SOURCE_REPO" diff --quiet HEAD -- . || [ -n "$UNTRACKED" ]; then
    if ! git -C "$SOURCE_REPO" diff --quiet HEAD -- .; then
        # Worktree apply + explicit add, never `apply --index`: on a bind-
        # mounted sandbox (container mode) a fresh checkout's stat data does
        # not match the index and `--index` refuses with "does not match
        # index" (container run 2). `update-index --refresh` is the belt.
        g update-index -q --refresh || true
        git -C "$SOURCE_REPO" diff --binary HEAD -- . | g apply \
            || _die "could not apply the source's working-tree diff onto the clone"
        while IFS= read -r rel; do
            [ -n "$rel" ] && g add -A -- "$rel"
        done <<< "$(git -C "$SOURCE_REPO" diff --name-only HEAD -- .)"
    fi
    if [ -n "$UNTRACKED" ]; then
        while IFS= read -r rel; do
            [ -n "$rel" ] || continue
            mkdir -p "$CLONE/$(dirname "$rel")"
            cp -p "$SOURCE_REPO/$rel" "$CLONE/$rel"
            g add -- "$rel"
        done <<< "$UNTRACKED"
    fi
    g commit -q -m "rehearsal: working-tree changes of $SOURCE_REPO at $DEV_SHA"
    echo "   applied the source's UNCOMMITTED changes as $(g rev-parse --short HEAD) ($(printf '%s\n' "$UNTRACKED" | grep -c . || true) untracked file(s) included)"
fi
# Machinery drift: paths under the never-delivered prefixes that develop has
# and main does not. A real cut branches off main and runs MAIN's copies of
# tests/ and scripts/, so a machinery fix that is still on develop cannot
# help the cut — the a2wmi.12 spike's rounds 2 and 3 were exactly this.
NEVER_DELIVERED=(tests docs scripts .github)
MACHINERY_DRIFT="$(g diff --name-only main develop -- "${NEVER_DELIVERED[@]}")"
if [ -n "$MACHINERY_DRIFT" ]; then
    drift_count="$(printf '%s\n' "$MACHINERY_DRIFT" | wc -l | tr -d ' ')"
    if [ "$PROMOTE" = 1 ]; then
        g checkout -q main
        g update-index -q --refresh || true
        g diff --binary main develop -- "${NEVER_DELIVERED[@]}" | g apply \
            || _die "could not promote develop's machinery onto the fake main"
        while IFS= read -r rel; do
            [ -n "$rel" ] && g add -A -- "$rel"
        done <<< "$MACHINERY_DRIFT"
        g commit -q -m "rehearsal: machinery promoted from develop to main ($drift_count never-delivered paths)"
        g checkout -q develop
        echo "   promoted $drift_count machinery path(s) from develop to the fake main as $(g rev-parse --short main) (what a PR to main lands)"
    else
        echo "   NOTE: develop carries $drift_count machinery path(s) main lacks (tests/ docs/ scripts/ .github/):"
        printf '%s\n' "$MACHINERY_DRIFT" | sed 's/^/         /'
        echo "         a real cut runs MAIN's copies; land them on main first (PR), or re-run with --promote-machinery to rehearse that landed state"
    fi
fi
g push -q origin main develop
g push -q origin --tags
g fetch -q origin
git -C "$SOURCE_REPO" rev-parse --verify --quiet "$BASE_TAG^{commit}" >/dev/null || _die "base tag $BASE_TAG does not resolve in the source"
echo "   main=$(g rev-parse --short main) develop=$(g rev-parse --short develop) tags=$(g tag -l | wc -l | tr -d ' ')"

_step "uv sync --group dev in the clone"
(cd "$CLONE" && uv sync -q --group dev) || _die "uv sync failed"

_step "the real cut: scripts/cut_plugin_release.py $BASE_TAG (includes its own battery)"
CUT_LOG="$SANDBOX/cut.log"
if ! uvr python scripts/cut_plugin_release.py "$BASE_TAG" --repo "$CLONE" 2>&1 | tee "$CUT_LOG"; then
    _die "the cut refused (see $CUT_LOG)"
fi
CUT_LINE="$(grep -E '^cut: plugin-v' "$CUT_LOG" | tail -1)"
[ -n "$CUT_LINE" ] || _die "the cut printed no 'cut: <tag> on <branch>' line"
TAG="$(printf '%s' "$CUT_LINE" | sed -E 's/^cut: (plugin-v[^ ]+) on (plugin-release\/[^ ]+).*/\1/')"
BRANCH="$(printf '%s' "$CUT_LINE" | sed -E 's/^cut: (plugin-v[^ ]+) on (plugin-release\/[^ ]+).*/\2/')"
[ "$(g rev-parse --abbrev-ref HEAD)" = "$BRANCH" ] || _die "the clone is on $(g rev-parse --abbrev-ref HEAD), not the cut branch $BRANCH"
CUT_HEAD="$(g rev-parse HEAD)"
echo "   cut $TAG on $BRANCH at ${CUT_HEAD:0:9}"
g diff --stat origin/main | tail -3

_step "the cut PR's CI: refs/pull/1/merge at depth 1, the drift-ledger workflow's tag fetch, pull_request payload, pin-reading checks"
g push -q -u origin "$BRANCH"
# GitHub builds the synthetic merge ref server-side; build it here on the
# full clone and publish it on the fake origin under the same ref name.
g checkout -q --detach origin/main
g merge -q --no-ff --no-edit "$BRANCH" || _die "merge of $BRANCH into main is not clean"
MERGE_SHA="$(g rev-parse HEAD)"
g push -q origin "HEAD:refs/pull/1/merge"
g checkout -q develop
# actions/checkout@v7 on a pull_request event: a DEPTH-1 checkout of
# refs/pull/<n>/merge with no tags and no branch — the shape every
# tag-visibility sentinel in the channel exists for. Never the full clone.
PRCI="$SANDBOX/prci"
git init -q "$PRCI" && git -C "$PRCI" remote add origin "$ORIGIN"
git -C "$PRCI" fetch -q --depth=1 origin refs/pull/1/merge
git -C "$PRCI" checkout -q --detach FETCH_HEAD
[ "$(git -C "$PRCI" rev-parse HEAD)" = "$MERGE_SHA" ] || _die "depth-1 checkout is not the merge ref"
# plugin-drift-ledger.yml's own fetch step, same commands in the same order:
# shallow --tags, the PR head sha, then each pinned ref with the two
# release-window shapes tolerated and anything else a hard failure.
git -C "$PRCI" fetch -q --depth=1 --tags --force origin
git -C "$PRCI" fetch -q --depth=1 origin "$CUT_HEAD"
pinned="$(python3 -c "
import json,sys
d = json.load(open(sys.argv[1]))
print(' '.join(sorted({p['source']['ref'] for p in d.get('plugins', []) if isinstance(p.get('source'), dict) and p['source'].get('ref')})))
" "$PRCI/.claude-plugin/marketplace.json")"
[ -n "$pinned" ] || _die "marketplace.json on the merge ref pins no ref"
version_re="$(printf '%s' "${BASE_TAG#v}" | sed 's/\./\\./g')"
for ref in $pinned; do
    if git -C "$PRCI" fetch -q --depth=1 origin "refs/tags/$ref:refs/tags/$ref" 2>/dev/null; then continue; fi
    if [ "$ref" = "$BASE_TAG" ]; then
        echo "   release window: $ref not yet cut — tolerated"
    elif [[ "$ref" =~ ^plugin-v${version_re}-[1-9][0-9]*$ ]]; then
        echo "   plugin release window: $ref is the anchored form for $BASE_TAG and not yet cut — tolerated"
    else
        _die "pinned ref $ref does not exist and is NOT this release's own tag"
    fi
done
EVENT="$SANDBOX/pull_request_event.json"
python3 - "$EVENT" "$CUT_HEAD" "$BRANCH" <<'PY'
import json, sys
path, head, branch = sys.argv[1:4]
json.dump({"action": "synchronize", "pull_request": {"head": {"sha": head, "ref": branch}, "base": {"ref": "main"}}}, open(path, "w"))
PY
echo "   merge ref ${MERGE_SHA:0:9} at depth 1 (tags: $(git -C "$PRCI" tag -l | wc -l | tr -d ' ')), pull_request.head.sha ${CUT_HEAD:0:9}"
# Each shallow checkout gets its OWN environment (what CI's `uv sync --group
# dev` step gives it): tests/conftest.py imports `nexus` at collection, and
# the clone's editable install points at $CLONE/src — on the PR leg that is
# develop's tree, not the merge ref's (R4-delta review). Warm cache: seconds.
(cd "$PRCI" && uv sync -q --group dev) || _die "uv sync failed in the PR checkout"
prci_env=(env GITHUB_EVENT_NAME=pull_request "GITHUB_EVENT_PATH=$EVENT" "GITHUB_HEAD_REF=$BRANCH" GITHUB_BASE_REF=main NX_REQUIRE_PLUGIN_DRIFT_CHECK=1)
DRIFT_LOG="$SANDBOX/prci-drift-ledger.txt"
"${prci_env[@]}" bash -c "cd '$PRCI' && uv run pytest tests/test_plugin_release_drift_ledger.py -q -rs -p no:cacheprovider" 2>&1 | tee "$DRIFT_LOG" \
    || _die "drift-ledger contract failed on the cut PR's merge ref (see $DRIFT_LOG)"
if grep -qE '[0-9]+ skipped' "$DRIFT_LOG"; then _die "VACUITY: drift-ledger tests skipped on the PR-CI leg"; fi
# tests/test_plugin_structure.py is lint-marked (addopts deselects lint by
# default): it needs its own `-m lint` invocation or it collects NOTHING
# and hides inside a multi-file run's green (the first rehearsal did that).
"${prci_env[@]}" bash -c "cd '$PRCI' && uv run pytest -m lint tests/test_plugin_structure.py -q -p no:cacheprovider" \
    || _die "tests/test_plugin_structure.py (the per-plugin proof) failed on the cut PR's merge ref"
"${prci_env[@]}" bash -c "cd '$PRCI' && uv run pytest tests/test_plugin_channel.py tests/test_cut_plugin_release.py tests/hooks/test_agent_dispatch_expect.py tests/test_sn_plugin.py tests/hooks/test_version_lockstep_hook.py -q -p no:cacheprovider" \
    || _die "pin-reading checks failed on the cut PR's merge ref"

_step "merge into main and push the anchored tag $TAG to the fake origin"
g checkout -q main
g merge -q --no-ff --no-edit "$BRANCH" || _die "merge into main failed"
g push -q origin main
g tag -a "$TAG" -m "$TAG"
g push -q origin "$TAG"
echo "   main=$(g rev-parse --short main) tag $TAG -> $(g rev-parse --short "$TAG^{commit}")"

_step "the tag workflow's steps (plugin-release.yml) on a depth-1 checkout of $TAG"
# actions/checkout@v7 on a tag push: depth 1 at the tag, nothing else; then
# the workflow's own derive-and-fetch step — the anchored-shape check, the
# base derived from the tag name, and a single explicit shallow tag fetch.
TAGCO="$SANDBOX/tagco"
git init -q "$TAGCO" && git -C "$TAGCO" remote add origin "$ORIGIN"
git -C "$TAGCO" fetch -q --depth=1 origin "refs/tags/$TAG:refs/tags/$TAG"
git -C "$TAGCO" checkout -q --detach "$TAG"
if ! [[ "$TAG" =~ ^plugin-v[0-9]+\.[0-9]+\.[0-9]+-[1-9][0-9]*$ ]]; then _die "CUT_TAG '$TAG' is not an anchored plugin tag"; fi
without_prefix="${TAG#plugin-}"
derived_base="${without_prefix%-*}"
[ "$derived_base" = "$BASE_TAG" ] || _die "the workflow's derived base tag '$derived_base' != '$BASE_TAG'"
git -C "$TAGCO" fetch -q --depth=1 origin "refs/tags/$derived_base:refs/tags/$derived_base"
echo "   $TAG at depth 1; derived base $derived_base fetched; tags present: $(git -C "$TAGCO" tag -l | tr '\n' ' ')"
# plugin-release.yml's `uv sync --group dev`: this checkout's own environment.
(cd "$TAGCO" && uv sync -q --group dev) || _die "uv sync failed in the tag checkout"
TAG_DRIFT_LOG="$SANDBOX/tag-drift-ledger.txt"
env -u GITHUB_EVENT_NAME -u GITHUB_EVENT_PATH -u GITHUB_HEAD_REF NX_REQUIRE_PLUGIN_DRIFT_CHECK=1 \
    bash -c "cd '$TAGCO' && uv run pytest tests/test_plugin_release_drift_ledger.py -q -rs -p no:cacheprovider" 2>&1 | tee "$TAG_DRIFT_LOG" \
    || _die "drift-ledger contract failed at the tag (see $TAG_DRIFT_LOG)"
if grep -qE '[0-9]+ skipped' "$TAG_DRIFT_LOG"; then _die "VACUITY: drift-ledger tests skipped at the tag"; fi
tagco_run() { env -u GITHUB_EVENT_NAME -u GITHUB_EVENT_PATH -u GITHUB_HEAD_REF bash -c "cd '$TAGCO' && uv run $*"; }
tagco_run pytest -m lint tests/test_plugin_structure.py -q -p no:cacheprovider || _die "tests/test_plugin_structure.py failed at the tag"
tagco_run pytest tests/hooks/ -q -p no:cacheprovider || _die "tests/hooks/ failed at the tag"
# Same two exclusions as plugin-release.yml: both modules walk v* tag
# history a depth-1, two-tag checkout cannot resolve.
tagco_run pytest -m lint -q -p no:cacheprovider --ignore=tests/test_wire_contract_pairing_lint.py --ignore=tests/test_rehearsal_native_legs_refuse_no_build.py || _die "-m lint failed at the tag"
tagco_run python scripts/check_cut_ledger_clean.py --base "$derived_base" --cut "$TAG" || _die "check_cut_ledger_clean.py failed"

_step "back-merge main -> develop (scripts/plugin_cut_back_merge.sh) and develop's own drift contract"
# The cut's ledger rewrite conflicts with develop's copy by construction; the
# script resolves exactly that and runs develop's drift contract as the net.
env -u GITHUB_EVENT_NAME -u GITHUB_EVENT_PATH -u GITHUB_HEAD_REF \
    bash "$CLONE/scripts/plugin_cut_back_merge.sh" "$CLONE" \
    || _die "back-merge main -> develop failed (scripts/plugin_cut_back_merge.sh)"
g push -q origin develop

PASSED=1
echo
echo "PLUGIN-CUT REHEARSAL PASSED — $TAG on $BRANCH (cut ${CUT_HEAD:0:9}, base $BASE_TAG, source $DEV_SHA)"
