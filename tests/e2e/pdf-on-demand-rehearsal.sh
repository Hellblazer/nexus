#!/usr/bin/env bash
# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (c) 2026 Hal Hildebrand. All rights reserved.
#
# nexus-2k66t.4.2: PDF-STACK-ON-DEMAND REHEARSAL (design legs a-f; g and h are
# manual, see tests/e2e/pdf-on-demand-rehearsal.md).
#
# The unit tests prove the state machine against fakes and the real CLI in an
# interpreter that cannot see the stack. They cannot prove that a real
# `uv pip install` of the real wheel produces a working stack, that the
# re-exec lands in the new generation, or that torch is the CPU build on
# Linux. That is this script's job, against a wheel built from THIS checkout
# (the release candidate), in a throwaway sandbox that never touches the live
# install: HOME, NX_TOOLS_DIR, NX_BIN_DIR, UV_CACHE_DIR and every XDG variable
# are redirected, and the environment is allow-listed (`env -i`).
#
# Run it on chas (Linux container class) and on a hellmini throwaway account,
# as the lab hosts' rules require, e.g.
#
#   lab-run -c lab-l -- tests/e2e/pdf-on-demand-rehearsal.sh --engine
#
# This script was written for the rehearsal and has NOT been run by its author
# (the legs need chas, hellmini and a Windows guest). Run `bash -n` and read
# the legs first; a leg that cannot run fails loudly, it never skips green.
#
# LEGS (what each proves). They run in the order a, c, d, e, b, f, p, u: e and the
# refuse legs need the SLIM install, and b installs the stack, so a leg that
# asserts "refused" after b would find the stack present and fail (or pass by
# doing nothing).
#   a  Fresh install by the bootstrap route has NO torch, docling, mineru,
#      pdftext; the doctor row is informational, not red; `nx --version` and
#      `nx doctor --check-mineru` work; the install's size is recorded.
#      Proves: the slim default and that a missing stack is not an error.
#   b  Non-TTY `nx index pdf <formula fixture>` installs the stack, re-runs in
#      the new generation and exits 0; MinerU took the formula route; the LaTeX
#      survived into search; installed versions equal uv.lock's pins; the
#      receipt lists pdf; the conexus version is unchanged. (needs --engine)
#      Proves: the whole on-demand path, and that the fetch is not an upgrade.
#   c  Under NX_PDF_STACK=refuse the same command exits 69, names the command,
#      and wrote nothing (no generation, no config files; with --engine, zero
#      catalog and chunk rows for the file).
#      Proves: refusal happens before any write.
#   d  With UV_OFFLINE=1 and a cold cache the same command exits 75 and writes
#      nothing: no new generation, `current` unmoved.
#      Proves: the network exit class and that a failed fetch leaves no trace.
#   e  A background hook-shaped run (`NX_PDF_STACK=refuse nx index repo
#      --since-head`) over a repo with code, prose and a PDF indexes the code
#      and prose, does NOT index or register the PDF, logs an ERROR naming it,
#      exits 69, and `nx doctor`'s PDF support row counts it. (needs --engine)
#      Proves: the containment shape of the hook policy.
#   f  `nx self install --version <next>` from a generation that has the stack:
#      the new receipt keeps pdf, a second formula PDF indexes with no install
#      step, and no wheel is downloaded again (uv's cache serves the rebuild).
#      Proves: the receipt carries the extra through upgrades. (needs --engine)
#
#   u  The upgrade rule, end to end, with a REAL old installer. A second sandbox
#      gets the previous release (--old-version, default 7.85.0, from PyPI) by its
#      own bootstrap: its receipt is pre-split (installer_schema 1, no pdf) and it
#      has the stack. THAT release's `nx self install --version <rc>` then builds
#      the release candidate, which is slim (the old installer cannot know the
#      rule): receipt still schema 1 without pdf, no stack modules. `nx upgrade`
#      from that generation then carries the stack forward: the receipt lists pdf
#      and has installer_schema 2, the version is the candidate's, and (with
#      --engine) a formula PDF indexes with NO install step.
#      Proves: an existing install keeps the stack across the first post-split
#      upgrade even when the installer that moved it did not know the rule.
#      Needs the network (the previous release comes from PyPI).
#
#   p  The default install route, for real. The installers and the plugin's setup run
#      `nx self install --bootstrap --extras pdf` (nexus-2k66t.4.9); every other leg of
#      this script bootstraps bare. This one runs that exact command in its own virgin
#      HOME with real uv and the candidate wheel, and asserts the generation it built:
#      the receipt lists pdf, has installer_schema 2, all five stack modules import
#      from the generation, torch is the CPU build on Linux, the version is the
#      candidate's, and the shim runs. No engine needed.
#      Proves: --extras reaches the generation through the bootstrap, not only through
#      install_generation.sh (which fresh-install-mvv.sh leg 9 drives).
#
# Usage: pdf-on-demand-rehearsal.sh [--legs a,b,c,d,e,f,u,p] [--engine]
#            [--formula-pdf PATH] [--second-formula-pdf PATH] [--old-version X.Y.Z]
#
#   --engine   run `nx init` in local mode (engine + PG download, bge-768) so
#              legs b, e and f can index. Without it those legs are REPORTED AS
#              NOT RUN, and the script exits non-zero if the requested legs
#              include one of them: a rehearsal that skipped its core leg has
#              not rehearsed.
set -euo pipefail

_here="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
_repo="$(cd "$_here/../.." && pwd)"
# shellcheck source=lib/python.sh
source "$_here/lib/python.sh"
e2e_python_resolve || exit 2

LEGS="a,b,c,d,e,f,u,p"
OLD_VERSION="7.85.0"
ENGINE=0
NX_LOCAL_ENGINE=""
FORMULA_PDF="$_repo/tests/fixtures/bft-to-smr.pdf"
SECOND_PDF="$_repo/tests/fixtures/tc-sql.pdf"
while [ $# -gt 0 ]; do
    case "$1" in
        --legs) LEGS="${2-}"; shift 2 ;;
        --engine) ENGINE=1; shift ;;
        --formula-pdf) FORMULA_PDF="${2-}"; shift 2 ;;
        --second-formula-pdf) SECOND_PDF="${2-}"; shift 2 ;;
        --old-version) OLD_VERSION="${2-}"; shift 2 ;;
        *) echo "unknown argument: $1" >&2; exit 64 ;;
    esac
done

if [ "$ENGINE" = 1 ]; then NX_LOCAL_ENGINE=1; fi

SANDBOX="$(mktemp -d "${TMPDIR:-/tmp}/nx-pdf-rehearsal.XXXXXX")"
HOME_DIR="$SANDBOX/home"
TOOLS="$SANDBOX/tools"
BIN="$SANDBOX/bin"
WHEELS="$SANDBOX/wheels"
CACHE="$SANDBOX/uvcache"
LOGS="$SANDBOX/logs"
mkdir -p "$HOME_DIR" "$TOOLS" "$BIN" "$WHEELS" "$CACHE" "$LOGS"

cleanup() {
    local rc=$?
    # The sandbox holds no live process of ours unless --engine started one.
    if [ -x "$BIN/nx" ]; then
        _nx daemon service stop >/dev/null 2>&1 || true
    fi
    if [ "${NX_REHEARSAL_KEEP:-0}" = "1" ] || [ "$rc" -ne 0 ]; then
        echo "sandbox kept: $SANDBOX" >&2
    else
        rm -rf "$SANDBOX"
    fi
}
trap cleanup EXIT
trap 'exit 143' TERM INT

# The ONLY ambient state a leg may see. UV_FIND_LINKS makes the builder resolve
# the release candidate from the local wheel directory instead of PyPI.
_clean_env() {
    env -i \
        HOME="$HOME_DIR" \
        PATH="$BIN:$(dirname "$(command -v uv)"):/usr/bin:/bin" \
        TERM="${TERM:-dumb}" \
        NX_NO_TELEMETRY=1 \
        NX_TOOLS_DIR="$TOOLS" \
        NX_BIN_DIR="$BIN" \
        NEXUS_CONFIG_DIR="$HOME_DIR/.config/nexus" \
        XDG_CONFIG_HOME="$HOME_DIR/.config" \
        XDG_DATA_HOME="$HOME_DIR/.local/share" \
        UV_CACHE_DIR="${NX_REHEARSAL_UV_CACHE:-$CACHE}" \
        UV_FIND_LINKS="$WHEELS" \
        ${NX_LOCAL_ENGINE:+NX_LOCAL=1} \
        ${NX_PDF_STACK:+NX_PDF_STACK="$NX_PDF_STACK"} \
        ${UV_OFFLINE:+UV_OFFLINE="$UV_OFFLINE"} \
        ${HTTPS_PROXY:+HTTPS_PROXY="$HTTPS_PROXY"} \
        ${HTTP_PROXY:+HTTP_PROXY="$HTTP_PROXY"} \
        "$@"
}
_nx() { _clean_env "$BIN/nx" "$@"; }

PASSED=""
FAILED=""
NOT_RUN=""
# exit, not return: a leg runs in a subshell under `if`, where errexit is off, so a
# failed check must end the leg itself or the leg would carry on and report green.
fail() { echo "FAIL: $*" >&2; exit 1; }
want() { case ",$LEGS," in *",$1,"*) return 0 ;; *) return 1 ;; esac; }

gens() { find "$TOOLS" -maxdepth 1 -name 'gen-*' | sort; }
current_gen() { readlink "$TOOLS/current"; }
gen_python() { echo "$(current_gen)/bin/python"; }
receipt_field() { "$(gen_python)" -c "import json,sys;d=json.load(open('$(current_gen)/nexus-install.json'));print(d['$1'])"; }

# No `cmd | grep -q` anywhere in this script: under pipefail an early-exiting consumer turns the
# producer's SIGPIPE into the pipeline's status (nexus-i66g4 class). Capture, then match.
extras_have_pdf() { local e; e="$(receipt_field extras)"; [[ "$e" == *pdf* ]]; }
nx_version_is() { local v; v="$(_nx --version)"; [[ "$v" == *"$1"* ]]; }
stack_module_count() { local m; m="$(stack_modules_present)"; if [ -z "$m" ]; then echo 0; else local IFS=,; set -- $m; echo $#; fi; }

stack_modules_present() {
    "$(gen_python)" -c "
import importlib.util as u
print(','.join(m for m in ('docling','mineru','torch','torchvision','pdftext') if u.find_spec(m)))"
}

# ── build the release candidate wheel (and a successor for leg f) ───────────
echo "── building the release-candidate wheel from $_repo ──"
(cd "$_repo" && uv build --wheel --out-dir "$WHEELS" >"$LOGS/build.log" 2>&1) || { cat "$LOGS/build.log" >&2; exit 1; }
_rc_wheels=("$WHEELS"/conexus-*.whl)
RC_WHEEL="${_rc_wheels[0]}"
RC_VERSION="$(basename "$RC_WHEEL" | cut -d- -f2)"
echo "rc wheel: $RC_WHEEL (conexus $RC_VERSION)"

if want f; then
    NEXT_VERSION="$("$E2E_PYTHON" - "$RC_VERSION" <<'PY'
import sys
parts = sys.argv[1].split(".")
parts[-1] = str(int(parts[-1]) + 1)
print(".".join(parts))
PY
)"
    COPY="$SANDBOX/next-src"
    mkdir -p "$COPY"
    (cd "$_repo" && tar --exclude=.venv --exclude=service/target --exclude=.git --exclude=node_modules -cf - .) | (cd "$COPY" && tar -xf -)
    sed -i.bak "s/^version = \"$RC_VERSION\"/version = \"$NEXT_VERSION\"/" "$COPY/pyproject.toml"
    grep -q "^version = \"$NEXT_VERSION\"" "$COPY/pyproject.toml" || fail "could not bump the successor wheel's version"
    (cd "$COPY" && uv build --wheel --out-dir "$WHEELS" >"$LOGS/build-next.log" 2>&1) || { cat "$LOGS/build-next.log" >&2; exit 1; }
    echo "successor wheel: conexus $NEXT_VERSION"
fi

# ── fresh install by the documented bootstrap route ─────────────────────────
# A BARE --bootstrap is the slim shape (--slim, a bare uv tool install, the .mcpb bundle). The one-line installers and
# the plugin's setup pass --extras pdf since nexus-2k66t.4.9; fresh-install-mvv.sh covers that default.
echo "── fresh install (slim: bare bootstrap route) ──"
_clean_env uv tool run --python 3.12 --from "$RC_WHEEL" nx self install --bootstrap >"$LOGS/bootstrap.log" 2>&1 \
    || { cat "$LOGS/bootstrap.log" >&2; fail "bootstrap install failed"; }
[ -x "$BIN/nx" ] || fail "bootstrap left no shim"
FRESH_GEN="$(current_gen)"
FRESH_KB="$(du -sk "$FRESH_GEN" | cut -f1)"

if [ "$ENGINE" = 1 ]; then
    echo "── nx init (local mode) ──"
    _nx init -y --no-autostart >"$LOGS/init.log" 2>&1 || { tail -40 "$LOGS/init.log" >&2; fail "nx init failed"; }
fi

run_leg() {
    local leg="$1"; shift
    echo "── leg $leg ──"
    if ( "$@" ); then PASSED="$PASSED $leg"; else FAILED="$FAILED $leg"; fi
}

needs_engine() {
    if [ "$ENGINE" = 1 ]; then return 0; fi
    NOT_RUN="$NOT_RUN $1"
    echo "leg $1 NOT RUN: it needs --engine" >&2
    return 1
}

leg_a() {
    local present
    present="$(stack_modules_present)"
    [ -z "$present" ] || fail "a fresh install already has: $present"
    echo "fresh generation: $FRESH_KB KiB on disk (record this beside the 7.85.0 figure)"
    nx_version_is "$RC_VERSION" || fail "nx --version does not name $RC_VERSION"
    _nx doctor --check-mineru >"$LOGS/a-mineru.log" 2>&1 || true   # its exit status is not the signal; the line is
    grep -q "MinerU import: N/A" "$LOGS/a-mineru.log" \
        || fail "doctor --check-mineru did not report N/A on a slim install"
    "$(gen_python)" - <<'PY' || fail "PDF support row is red or missing on a slim install"
from nexus import health
(row,) = health._check_pdf_support()
assert row.label == "PDF support" and row.ok and not row.warn and "not installed" in row.detail, row
print("row:", row.detail)
PY
    if extras_have_pdf; then fail "a fresh install's receipt lists pdf"; fi
    return 0
}

leg_b() {
    local before_version after_version
    before_version="$(_nx --version)"
    # Non-TTY on purpose: the commonest caller is an agent's Bash tool.
    _nx index pdf "$FORMULA_PDF" --monitor </dev/null >"$LOGS/b-index.log" 2>&1 \
        || { tail -60 "$LOGS/b-index.log" >&2; fail "index pdf exited non-zero"; }
    grep -q "Installing PDF support for conexus $RC_VERSION" "$LOGS/b-index.log" || fail "no install notice on stderr"
    [ "$(current_gen)" != "$FRESH_GEN" ] || fail "no new generation was built"
    extras_have_pdf || fail "the new receipt does not list pdf"
    after_version="$(_nx --version)"
    [ "$before_version" = "$after_version" ] || fail "the conexus version moved: $before_version -> $after_version"
    grep -qi "switching to MinerU" "$LOGS/b-index.log" || fail "the formula PDF did not take the MinerU route"
    grep -q "pymupdf_normalized" "$LOGS/b-index.log" && fail "a quiet fallback to pymupdf_normalized"
    # Installed versions == uv.lock: the lock check the 2k66t.4.1 leg uses.
    # lock_check.py takes the constraints file the generation's own wheel shipped, and (this
    # generation carries the stack) the pdf extra's witness as well as the default ones.
    local shipped
    local _shipped=("$(current_gen)"/lib/python*/site-packages/nexus/_install/constraints.txt)
    shipped="${_shipped[0]}"
    [ -f "$shipped" ] || fail "the generation holds no nexus/_install/constraints.txt"
    "$(gen_python)" "$_repo/tests/e2e/lib/lock_check.py" "$shipped" onnxruntime pymupdf mcp opencv-python-headless >"$LOGS/b-lock.log" 2>&1 || { cat "$LOGS/b-lock.log" >&2; fail "installed versions differ from the lock"; }
    # Linux: torch must be the CPU build. A bare `uv pip install conexus[pdf]` would not be.
    if [ "$(uname -s)" = "Linux" ]; then
        "$(gen_python)" -c "import importlib.metadata as m; v=m.version('torch'); assert '+cpu' in v, v" \
            || fail "torch is not the CPU build"
    fi
    # The LaTeX survived into the index.
    _nx search "equation" --corpus docs --n 10 >"$LOGS/b-search.log" 2>&1 || true
    grep -Eq '\$|\\\\[a-zA-Z]+' "$LOGS/b-search.log" \
        || fail "no LaTeX marker in search results: formulas were lost"
}

leg_c() {
    local gens_before cfg_before rc
    if [ "$ENGINE" = 1 ]; then _nx collection list >"$LOGS/c-collections-before.log" 2>&1 || fail "collection list failed"; fi
    gens_before="$(gens | wc -l | tr -d ' ')"
    cfg_before="$(find "$HOME_DIR/.config" -type f 2>/dev/null | wc -l | tr -d ' ')"
    set +e
    NX_PDF_STACK=refuse _nx index pdf "$FORMULA_PDF" </dev/null >"$LOGS/c.log" 2>&1
    rc=$?
    set -e
    [ "$rc" = 69 ] || { tail -20 "$LOGS/c.log" >&2; fail "expected exit 69, got $rc"; }
    grep -q "nx self install --extras pdf" "$LOGS/c.log" || fail "the message does not name the command"
    [ "$(gens | wc -l | tr -d ' ')" = "$gens_before" ] || fail "a generation was built under refuse"
    [ "$(find "$HOME_DIR/.config" -type f 2>/dev/null | wc -l | tr -d ' ')" = "$cfg_before" ] || fail "files were written under refuse"
    if [ "$ENGINE" = 1 ]; then
        _nx collection list >"$LOGS/c-collections-after.log" 2>&1 || fail "collection list failed"
        diff "$LOGS/c-collections-before.log" "$LOGS/c-collections-after.log" >/dev/null || fail "a refused PDF changed the collections"
    fi
}

leg_d() {
    local gens_before rc
    gens_before="$(gens | wc -l | tr -d ' ')"
    # A cold cache: leg c and d must not be served by a leg b download.
    local cold="$SANDBOX/cold-cache"
    mkdir -p "$cold"
    set +e
    NX_REHEARSAL_UV_CACHE="$cold" UV_OFFLINE=1 _nx index pdf "$FORMULA_PDF" </dev/null >"$LOGS/d.log" 2>&1
    rc=$?
    set -e
    [ "$rc" = 75 ] || { tail -20 "$LOGS/d.log" >&2; fail "expected exit 75, got $rc"; }
    [ "$(gens | wc -l | tr -d ' ')" = "$gens_before" ] || fail "a failed fetch left a generation behind"
}

leg_e() {
    local repo="$SANDBOX/repo" rc
    mkdir -p "$repo"
    (cd "$repo" && git init -q && git config user.email t@t.invalid && git config user.name t \
        && printf 'def f():\n    return 1\n' > a.py && printf '# notes\n\nsome prose\n' > notes.md \
        && cp "$FORMULA_PDF" paper.pdf && git add -A && git commit -q -m init)
    set +e
    NX_PDF_STACK=refuse _nx index repo "$repo" --since-head --on-locked=skip </dev/null >"$LOGS/e.log" 2>&1
    rc=$?
    set -e
    [ "$rc" = 69 ] || { tail -30 "$LOGS/e.log" >&2; fail "expected exit 69, got $rc"; }
    grep -q "paper.pdf" "$LOGS/e.log" || fail "the held PDF is not named"
    _nx search "some prose" --n 3 >"$LOGS/e-search.log" 2>&1 || true
    grep -q "prose" "$LOGS/e-search.log" || fail "prose was not indexed"
    "$(gen_python)" -c "
from nexus import pdf_stack
n = pdf_stack.skipped_count()
assert n == 1, n" || fail "the skipped-PDF record does not count the held PDF"
}

leg_f() {
    # Start from a generation that HAS the stack (leg b's), then upgrade.
    extras_have_pdf || fail "leg f needs the stack installed first (run leg b)"
    local gens_before
    gens_before="$(gens | wc -l | tr -d ' ')"
    local dl_before dl_after
    dl_before="$(find "$CACHE" -type f 2>/dev/null | wc -l | tr -d ' ')"
    _nx self install --version "$NEXT_VERSION" >"$LOGS/f-upgrade.log" 2>&1 \
        || { tail -30 "$LOGS/f-upgrade.log" >&2; fail "nx self install --version failed"; }
    [ "$(gens | wc -l | tr -d ' ')" -gt "$gens_before" ] || fail "no new generation"
    extras_have_pdf || fail "the upgraded receipt lost pdf"
    local spec_now
    spec_now="$(receipt_field spec)"
    [[ "$spec_now" == *"$NEXT_VERSION"* ]] || fail "the upgrade did not reach $NEXT_VERSION"
    # No install step: a second formula PDF just indexes.
    _nx index pdf "$SECOND_PDF" </dev/null >"$LOGS/f-index.log" 2>&1 || { tail -30 "$LOGS/f-index.log" >&2; fail "second PDF failed"; }
    grep -q "Installing PDF support" "$LOGS/f-index.log" && fail "the second PDF ran the installer again"
    dl_after="$(find "$CACHE" -type f -name '*.whl' 2>/dev/null | wc -l | tr -d ' ')"
    echo "uv cache files: $dl_before -> $dl_after (a rebuild is served from the cache; check $LOGS/f-upgrade.log for 'Downloaded')"
    grep -qi "downloaded.*\(torch\|mineru\|docling\)" "$LOGS/f-upgrade.log" && fail "the upgrade re-downloaded the stack"
    return 0
}

# Leg u builds its own box. It runs in a subshell (run_leg), so re-pointing HOME_DIR, TOOLS and BIN
# here changes nothing for the other legs, and its EXIT trap stops the service it started.
leg_u() {
    HOME_DIR="$SANDBOX/u-home"; TOOLS="$SANDBOX/u-tools"; BIN="$SANDBOX/u-bin"
    mkdir -p "$HOME_DIR" "$TOOLS" "$BIN"
    trap '_nx daemon service stop >/dev/null 2>&1 || true' EXIT
    local schema extras

    # 1. the previous release, by its own bootstrap: pre-split receipt, stack present.
    _clean_env uv tool run --python 3.12 --from "conexus==$OLD_VERSION" nx self install --bootstrap >"$LOGS/u-old-bootstrap.log" 2>&1 \
        || { tail -30 "$LOGS/u-old-bootstrap.log" >&2; fail "could not install conexus==$OLD_VERSION by its bootstrap"; }
    [ -x "$BIN/nx" ] || fail "the old bootstrap left no shim"
    schema="$(receipt_field installer_schema 2>/dev/null || echo missing)"
    [ "$schema" = "1" ] || [ "$schema" = "missing" ] || fail "conexus==$OLD_VERSION wrote installer_schema $schema, not a pre-split receipt: pick an older --old-version"
    [ "$(stack_module_count)" = 5 ] || fail "the old release's generation does not have the whole PDF stack"

    # 2. the OLD installer builds the release candidate: slim, receipt still pre-split.
    _nx self install --version "$RC_VERSION" >"$LOGS/u-old-installer.log" 2>&1 \
        || { tail -30 "$LOGS/u-old-installer.log" >&2; fail "the old installer could not build the candidate"; }
    nx_version_is "$RC_VERSION" || fail "nx --version does not name $RC_VERSION after the old installer's build"
    [ -z "$(stack_modules_present)" ] || fail "the old installer's build of the candidate has the stack (it cannot know the rule): $(stack_modules_present)"
    extras="$(receipt_field extras)"
    case "$extras" in *pdf*) fail "the old installer's receipt already lists pdf: $extras" ;; esac
    schema="$(receipt_field installer_schema 2>/dev/null || echo missing)"
    [ "$schema" = "1" ] || [ "$schema" = "missing" ] || fail "the old installer wrote installer_schema $schema"
    local slim_gen
    slim_gen="$(current_gen)"

    # 3. the first code of the new release to run: nx upgrade carries the stack forward.
    _nx upgrade >"$LOGS/u-upgrade.log" 2>&1 || true
    grep -q "carried forward" "$LOGS/u-upgrade.log" || { tail -40 "$LOGS/u-upgrade.log" >&2; fail "nx upgrade did not report carrying the PDF stack forward"; }
    [ "$(current_gen)" != "$slim_gen" ] || fail "no new generation after nx upgrade"
    extras_have_pdf || fail "the carried generation's receipt does not list pdf"
    [ "$(receipt_field installer_schema)" = "2" ] || fail "the carried generation's installer_schema is not 2"
    [ "$(stack_module_count)" = 5 ] || fail "the carried generation is missing stack modules: $(stack_modules_present)"
    nx_version_is "$RC_VERSION" || fail "the carry changed the conexus version"

    # 4. a formula PDF indexes with no install step.
    if [ "$ENGINE" != 1 ]; then
        echo "leg u: the index step NOT RUN (needs --engine); the receipt assertions above passed" >&2
        NOT_RUN_FILE="$SANDBOX/not-run-u"; echo u > "$NOT_RUN_FILE"
        return 0
    fi
    _nx init -y --no-autostart >"$LOGS/u-init.log" 2>&1 || { tail -40 "$LOGS/u-init.log" >&2; fail "nx init failed in the leg u box"; }
    _nx index pdf "$FORMULA_PDF" </dev/null >"$LOGS/u-index.log" 2>&1 || { tail -40 "$LOGS/u-index.log" >&2; fail "index pdf failed on the carried install"; }
    grep -q "Installing PDF support" "$LOGS/u-index.log" && fail "the carried install ran the installer again"
    grep -qi "switching to MinerU" "$LOGS/u-index.log" || fail "the formula PDF did not take the MinerU route on the carried install"
    return 0
}

# Leg p builds its own box, like leg u: its subshell re-points HOME_DIR, TOOLS and BIN.
leg_p() {
    HOME_DIR="$SANDBOX/p-home"; TOOLS="$SANDBOX/p-tools"; BIN="$SANDBOX/p-bin"
    mkdir -p "$HOME_DIR" "$TOOLS" "$BIN"
    _clean_env uv tool run --python 3.12 --from "$RC_WHEEL" nx self install --bootstrap --extras pdf >"$LOGS/p-bootstrap.log" 2>&1 \
        || { tail -40 "$LOGS/p-bootstrap.log" >&2; fail "the bootstrap with the pdf extra failed"; }
    [ -x "$BIN/nx" ] || fail "the bootstrap left no shim"
    [ "$(gens | wc -l | tr -d ' ')" = 1 ] || fail "expected exactly one generation, found: $(gens | tr '\n' ' ')"
    extras_have_pdf || fail "the receipt does not list pdf: $(receipt_field extras)"
    [ "$(receipt_field installer_schema)" = "2" ] || fail "the receipt's installer_schema is not 2"
    [ "$(stack_module_count)" = 5 ] || fail "the generation is missing stack modules; present: $(stack_modules_present)"
    nx_version_is "$RC_VERSION" || fail "the shim does not report $RC_VERSION"
    if [ "$(uname -s)" = "Linux" ]; then
        "$(gen_python)" -c "import importlib.metadata as m; v=m.version('torch'); assert '+cpu' in v, v" \
            || fail "torch is not the CPU build"
    fi
    echo "leg p: $(du -sk "$(current_gen)" | cut -f1) KiB on disk with the pdf extra (record beside leg a's slim figure)"
    return 0
}

if want a; then run_leg a leg_a; fi
# c, d and e run on the SLIM install (they assert a refusal); b then installs the stack; f needs it.
if want c; then run_leg c leg_c; fi
if want d; then run_leg d leg_d; fi
if want e; then
    if needs_engine e; then run_leg e leg_e; fi
fi
if want b; then
    if needs_engine b; then run_leg b leg_b; fi
fi
if want f; then
    if needs_engine f; then run_leg f leg_f; fi
fi
if want p; then run_leg p leg_p; fi
if want u; then
    run_leg u leg_u
    if [ -f "$SANDBOX/not-run-u" ]; then NOT_RUN="$NOT_RUN u(index)"; fi
fi

echo
echo "PASSED:${PASSED:- none}"
echo "FAILED:${FAILED:- none}"
echo "NOT RUN:${NOT_RUN:- none}"
echo "layer: release-candidate wheel $RC_VERSION built from $_repo, sandbox $SANDBOX"
if [ -z "$PASSED" ] && [ -z "$FAILED" ]; then
    echo "no leg ran: this rehearsal proves nothing" >&2
    exit 1
fi
[ -z "$FAILED" ] && [ -z "$NOT_RUN" ]
