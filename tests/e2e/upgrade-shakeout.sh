#!/usr/bin/env bash
# upgrade-shakeout.sh — pre-merge verification of a breaking upgrade.
#
# What it does (rename PR #937 example, nexus-mkj6u). Steps below are the
# ACTUAL 12 the script executes end to end (nexus-okbr8: this list used to
# document only 10, and one entry described a step that no longer matched
# the code):
#   1.  Tear down + create a fresh sandbox HOME.
#   2.  uv tool install conexus==<FROM_VERSION> from PyPI (the live shipped
#       version).
#   3.  nx hooks install — writes the baseline stanza (may or may not
#       predate the pgrep guard, depending on FROM_VERSION).
#   4.  Pre-upgrade state snapshot — capture the installed version BEFORE
#       mutating anything; step 5 diffs the post-upgrade version against
#       this snapshot (nexus-x8fuq item B: previously captured and never
#       read).
#   5.  uv tool install --reinstall from REPO_ROOT (the upgrade-under-test).
#       Asserts the version actually progressed past the step-4 snapshot
#       (nexus-x8fuq item C) — a version-identical "upgrade" is a hard
#       failure here, not a note, since it means this run never exercised
#       the upgrade path at all.
#   6.  nx doctor — capture whether the stanza-drift health-check fires and
#       that doctor names no demoted verb as a remedy (nexus-x8fuq item A),
#       then cross-check (step 7) that doctor's drift claim agrees with
#       whether the stanza bytes actually changed (nexus-a3nqp). This makes
#       the test runnable from any baseline: a pre-guard baseline exercises
#       the drift→reconcile path, the latest stable exercises the
#       clean-no-op path, and a doctor false-positive/negative fails the
#       run.
#   7.  nx hooks update — verify the hook stanza is refreshed / idempotent,
#       cross-checked against step 6's drift claim.
#   8.  nx doctor again — verify the drift warning is gone post-update.
#   9.  Inspect marketplace.json — verify the rename (nx -> conexus) and
#       tag-pinned source.ref took effect.
#  10.  nx doctor with a planted stale 'nx' plugin.json — verify plugin-name
#       drift is surfaced with both migration hints.
#  11.  .mcpb bundle packs from REPO_ROOT/mcpb within a sane byte range
#       (empty/near-empty AND oversized both fail — nexus-x8fuq item E);
#       mcpb/ absence on a branch is a distinct SKIP, never folded into a
#       plain pass (nexus-okbr8).
#  12.  Final assertion summary + a greppable UPGRADE-SHAKEOUT PASSED
#       sentinel carrying the skip count (nexus-okbr8).
#
# Why this exists:
#   The release-sandbox.sh script tests a single version. It cannot catch
#   regressions in the upgrade path itself (drift warnings missing, hook
#   stanza fix not propagating, plugin rename not surfacing in marketplace.json).
#   nexus-mkj6u (2026-05-23) introduced four migration touchpoints — the
#   stanza pgrep guard, the `nx hooks update` command, the `nx doctor`
#   stanza-drift check, and the plugin name change — all of which need to
#   work in concert for an existing user to upgrade cleanly.
#
# Modes:
#   run        Execute the full sequence; exit 0 on green, 1 on any
#              assertion failure. ~3-5 minutes (first run, includes one
#              wheel install from PyPI).
#   reset      Tear down the sandbox without running anything.
#   self-test  Run the pure assertion-helper checks (item A/C/E detectors)
#              against planted fixture text — no sandbox, no network, no
#              lock. Demonstrates each detector actually catches the break
#              it claims to catch (nexus-x8fuq mandatory acceptance
#              criterion).
#
# Usage:
#   $0 run [--from-version <X.Y.Z>]
#   $0 reset
#   $0 self-test
#
# Defaults:
#   FROM_VERSION = the highest stable conexus on PyPI at script start.
#                  Runnable from any baseline; pass --from-version 4.34.6 to
#                  exercise the pre-pgrep-guard drift→reconcile path explicitly.

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_ROOT="$(cd "$SCRIPT_DIR/../.." && pwd)"
SANDBOX="$HOME/nexus-upgrade-sandbox"
FAKE_REPO="$SANDBOX/fakerepo"

# _print_help — inline, NOT a re-invocation of "$0" (RDR-184 P0.2,
# code-review CRITICAL-2 fix). The pre-existing code called `"$0" --help`
# as a subprocess whenever MODE was anything other than "reset"/"run" —
# including MODE="help", the DEFAULT when no args are given at all. Since
# the child's own MODE becomes "--help" (consumed as the mode positional,
# never reaching the arg-parsing while loop's `--help|-h` case below, which
# only fires for a SUBSEQUENT option), the child ALSO hit "MODE != reset/
# run" and re-invoked "$0" --help again — unbounded recursion on a bare
# no-arg invocation, entirely pre-existing and independent of the lock (a
# background verification run of this exact bug spawned 100+ live
# processes before being force-killed; do not re-run a bare invocation of
# the unfixed script). Extracting the usage text into a plain shell
# function (mirroring release-sandbox.sh's `_print_help`) prints it
# in-process with no subprocess and no recursion.
_print_help() {
    printf '%s\n' \
        "Usage: $0 <mode> [--from-version X.Y.Z]" \
        "" \
        "Modes:" \
        "  run        Full upgrade-shakeout sequence" \
        "  reset      Remove sandbox HOME without running" \
        "  self-test  Run assertion-helper checks against fixtures (no sandbox)" \
        "" \
        "Defaults:" \
        "  --from-version <latest stable on PyPI>"
}

MODE="${1:-help}"; shift || true
FROM_VERSION=""
while [[ $# -gt 0 ]]; do
    case "$1" in
        --from-version) FROM_VERSION="$2"; shift 2 ;;
        --help|-h) _print_help; exit 0 ;;
        *) echo "ERROR: unknown arg $1" >&2; exit 2 ;;
    esac
done

_die() { echo "FAIL: $*" >&2; exit 1; }
_pass() { echo "  ✓ $*"; }
# _skip / _note (nexus-okbr8, nexus-x8fuq item C): distinct from _pass on
# purpose. _pass claims something was VERIFIED; _skip means a step's real
# assertion did not run at all (counted separately, surfaced in the final
# sentinel); _note is pure classification ("which baseline are we in") that
# makes no pass/fail claim of its own — the real assertion for that branch
# lives elsewhere (see step 3 and the step 6/7 drift cross-check).
SKIP_COUNT=0
_skip() { echo "  ⚠ SKIP: $*"; SKIP_COUNT=$((SKIP_COUNT + 1)); }
_note() { echo "  · $*"; }
_step() { echo; echo "── $* ──"; }

# ── pure assertion helpers (nexus-x8fuq) ────────────────────────────────────
# Extracted so each detector's RED/GREEN behavior can be demonstrated via
# `$0 self-test` against planted fixture text, without driving the full
# sandboxed uv-install sequence (the acceptance-criterion escape hatch named
# in the bead: "extract the assertion into a function testable against
# planted fixture text").

# nx doctor's fix_suggestions surface must never recommend a verb that has
# been demoted out of --help (RDR-185 P4.1) or deleted outright (RDR-155
# P4b). health.py currently has ZERO producers of any of these five strings
# (verified at fix time: `grep -c 'guided-upgrade\|migrate-to-service\|
# migration-audit\|backfill-hash\|update-all' src/nexus/health.py` == 0) —
# this check is a live-output regression guard should that ever change, not
# a check expected to fire under normal operation. self-test proves the
# regex itself actually catches a planted occurrence.
_DEMOTED_VERB_RE='nx (guided-upgrade|migrate-to-service|migration-audit|collection backfill-hash|hooks update-all)'
_check_no_demoted_verb() {
    # $1 = doctor output text. Returns 0 (clean) or 1 (demoted verb found).
    if [[ "$1" =~ $_DEMOTED_VERB_RE ]]; then
        return 1
    fi
    return 0
}

# A version-identical "upgrade" never exercised the upgrade path at all —
# must be a hard failure, not a note (nexus-x8fuq item C).
_check_version_progressed() {
    # $1 = new version, $2 = pre-upgrade version. Returns 0 (progressed) or
    # 1 (identical).
    if [[ "$1" == "$2" ]]; then
        return 1
    fi
    return 0
}

# mcpb pack must be bounded on BOTH sides — an empty/near-empty bundle is as
# broken as an oversized one (nexus-x8fuq item E). MCPB_MIN_BYTES is
# deliberately loose (mcpb/manifest.json alone is >6 KB uncompressed; a real
# pack is comfortably five figures) — it exists to catch "produced nothing"
# failures, not to be a tight size budget.
MCPB_MIN_BYTES=500
MCPB_MAX_BYTES=100000
_check_mcpb_size() {
    # $1 = byte count (may be empty/unset). Echoes a verdict tag on stdout;
    # returns 0 (OK) or 1 (any other verdict).
    local sz="${1:-}"
    if [[ -z "$sz" ]]; then
        echo "EMPTY"
        return 1
    fi
    if [[ "$sz" -lt "$MCPB_MIN_BYTES" ]]; then
        echo "TOO_SMALL"
        return 1
    fi
    if [[ "$sz" -gt "$MCPB_MAX_BYTES" ]]; then
        echo "TOO_LARGE"
        return 1
    fi
    echo "OK"
    return 0
}

# ── self-test: pure-function checks against planted fixture text ───────────
# No sandbox, no uv install, no network, no lock. Safe anywhere.
_self_test() {
    local failures=0

    _assert_pass() {
        local label="$1"; shift
        if "$@" >/dev/null 2>&1; then
            _pass "$label"
        else
            echo "  ✗ FAIL: $label" >&2
            failures=$((failures + 1))
        fi
    }
    _assert_fail() {
        local label="$1"; shift
        if "$@" >/dev/null 2>&1; then
            echo "  ✗ FAIL: $label (expected failure, got success)" >&2
            failures=$((failures + 1))
        else
            _pass "$label"
        fi
    }
    _assert_eq() {
        local label="$1" got="$2" want="$3"
        if [[ "$got" == "$want" ]]; then
            _pass "$label"
        else
            echo "  ✗ FAIL: $label — expected [$want] got [$got]" >&2
            failures=$((failures + 1))
        fi
    }

    _step "self-test: _check_no_demoted_verb (nexus-x8fuq item A)"
    _assert_fail "RED: planted 'nx guided-upgrade' in doctor output is caught" \
        _check_no_demoted_verb "Everything looks fine. Run: nx guided-upgrade to fix it."
    _assert_fail "RED: planted 'nx hooks update-all' is caught" \
        _check_no_demoted_verb "Try: nx hooks update-all"
    _assert_pass "GREEN: clean doctor output (the real live shape) passes" \
        _check_no_demoted_verb "All checks passed. Run: nx hooks update to refresh the stanza."

    _step "self-test: _check_version_progressed (nexus-x8fuq item C)"
    _assert_fail "RED: identical versions is caught" \
        _check_version_progressed "7.10.0" "7.10.0"
    _assert_pass "GREEN: distinct versions passes" \
        _check_version_progressed "7.10.0" "7.9.0"

    _step "self-test: _check_mcpb_size (nexus-x8fuq item E)"
    _assert_eq "RED: empty size -> EMPTY verdict" "$(_check_mcpb_size "" 2>/dev/null || true)" "EMPTY"
    _assert_eq "RED: near-empty 10B -> TOO_SMALL verdict" "$(_check_mcpb_size 10 2>/dev/null || true)" "TOO_SMALL"
    _assert_eq "RED: oversized 500000B -> TOO_LARGE verdict" "$(_check_mcpb_size 500000 2>/dev/null || true)" "TOO_LARGE"
    _assert_eq "GREEN: 50000B -> OK verdict" "$(_check_mcpb_size 50000 2>/dev/null || true)" "OK"
    _assert_fail "RED: empty size fails the gate" _check_mcpb_size ""
    _assert_pass "GREEN: 50000B passes the gate" _check_mcpb_size 50000

    _step "self-test: bash -n on this script itself"
    if bash -n "${BASH_SOURCE[0]}"; then
        _pass "bash -n clean"
    else
        echo "  ✗ FAIL: bash -n reported a syntax error" >&2
        failures=$((failures + 1))
    fi

    _step "RESULT"
    if [[ "$failures" -eq 0 ]]; then
        echo "SELF-TEST PASSED — assertion helpers verified against planted fixtures (no sandbox, no network)"
        return 0
    else
        echo "SELF-TEST FAILED — $failures check(s)" >&2
        return 1
    fi
}

# Help/usage dispatch BEFORE the lock (RDR-184 P0.2, code-review CRITICAL-2
# fix): neither "help" (the default MODE when no args are given at all) nor
# any garbage MODE touches $SANDBOX, so neither needs the lock. This MUST
# stay ahead of lock_acquire below — the original placement had this branch
# AFTER the lock, so a bare no-arg invocation would acquire the lock, THEN
# re-invoke `"$0" --help` as a CHILD process that tried to acquire the SAME
# lock its own parent was still holding. The child's lock_acquire failed
# against its own parent, and under `set -e` that failure aborted the
# parent before it ever reached its `exit 0` — a bare `./upgrade-shakeout.sh`
# exited 1 with a lock-contention error instead of printing help. Calling
# _print_help directly (not `"$0" --help`, which is what ALSO caused the
# pre-existing unbounded recursion noted above _print_help's definition)
# means this path is not just lock-free but subprocess-free. Only "reset"
# and "run" mutate $SANDBOX and reach the lock below; every other MODE,
# including "self-test" (nexus-x8fuq — pure-function checks, no sandbox),
# exits right here.
if [[ "$MODE" == "self-test" ]]; then
    _self_test; exit $?
fi

if [[ "$MODE" != "reset" && "$MODE" != "run" ]]; then
    _print_help; exit 0
fi

# RDR-184 P0.2 (nexus-ccs9v.2): serialize on the machine-global fixed
# resource this harness mutates — $HOME/nexus-upgrade-sandbox. Shared by
# BOTH the "reset" branch just below and the "run" body further down (both
# mutate $SANDBOX) — reached only once the guard above has confirmed MODE
# is one of those two. Lock dir is a HARD-CODED /tmp path, deliberately NOT
# ${TMPDIR:-/tmp} (code-review SIGNIFICANT fix): on darwin, an interactive
# shell's TMPDIR is a per-user /var/folders/... path while a LaunchAgent/CI/
# stripped-env invocation sees plain /tmp — two different invocation
# contexts would silently compute DIFFERENT lockdirs and never contend,
# defeating the whole point of a machine-global guard (this repo runs
# LaunchAgents that could race an interactive run). /tmp is always the same
# path across every context on the same host.
# shellcheck source=./lib/lock.sh disable=SC1091
source "$SCRIPT_DIR/lib/lock.sh"
LOCKDIR="/tmp/nexus-e2e-locks/upgrade-shakeout.lock"
mkdir -p "$(dirname "$LOCKDIR")"
lock_acquire "$LOCKDIR" || exit 1
trap 'lock_release "$LOCKDIR" 2>/dev/null || true' EXIT
echo "[rdr-184] lock acquired: $LOCKDIR (pid $$)" >&2
# Test seam (RDR-184 P0.2, nexus-ccs9v.2): tests/e2e/lib/harness_lock_test.sh
# sets this to prove a concurrent invocation gets PAST the lock without ever
# running this harness's real body (uv tool install / sandbox rm -rf). No-op
# — unset in every normal invocation.
[[ -n "${NX_E2E_LOCK_SELFTEST:-}" ]] && exit 0

if [[ "$MODE" == "reset" ]]; then
    [[ -d "$SANDBOX" ]] && rm -rf "$SANDBOX"
    echo "Sandbox removed."
    exit 0
fi

# MODE == "run" — guaranteed by the guard above (every other value exited
# already, lock-free); no redundant re-check needed here.

# Resolve FROM_VERSION default — latest stable on PyPI.
if [[ -z "$FROM_VERSION" ]]; then
    FROM_VERSION=$(
        curl -s "https://pypi.org/pypi/conexus/json" \
        | python3 -c "import sys,json; print(json.load(sys.stdin)['info']['version'])"
    )
    [[ -z "$FROM_VERSION" ]] && _die "could not resolve latest conexus version from PyPI"
fi

# Pipe-free tail (nexus-i66g4/wbeyi class): take the first line via
# parameter expansion instead of `| head -1` -- under this script's
# `set -o pipefail`, a still-writing grep closed early by head risks its
# SIGPIPE getting promoted over head's own (successful) exit status.
REPO_PKG_VERSION_ALL="$(grep '^version' "$REPO_ROOT/pyproject.toml" | cut -d'"' -f2)"
REPO_PKG_VERSION="${REPO_PKG_VERSION_ALL%%$'\n'*}"
echo "Upgrade-shakeout: $FROM_VERSION  →  ${REPO_PKG_VERSION} (REPO_ROOT)"
echo "Sandbox: $SANDBOX"

# ── 1. Fresh sandbox ─────────────────────────────────────────────────────────
_step "1/12 Fresh sandbox"
rm -rf "$SANDBOX"
mkdir -p "$SANDBOX/.config/nexus"
mkdir -p "$FAKE_REPO/.git/hooks"
# Fake repo registry entry so nx doctor's _check_git_hooks sees the repo.
python3 -c "
import json, pathlib
p = pathlib.Path('$SANDBOX/.config/nexus/repos.json')
p.write_text(json.dumps({'repos': {'$FAKE_REPO': {}}}))
"
_pass "sandbox + fakerepo at $FAKE_REPO"

# ── 2. Install FROM_VERSION ──────────────────────────────────────────────────
_step "2/12 uv tool install conexus==$FROM_VERSION (the OLD version)"
# Use a sandbox-local UV_TOOL_DIR so this install does not clobber the dev
# tool install. The UV_TOOL_BIN_DIR puts the resulting `nx` on PATH for
# this script's subsequent commands.
# (RDR-155 P4b: the nexus-0rwwv bridge probe and its NX_MIGRATION_NOTICE
# kill switch are gone — no pin needed.)
export UV_TOOL_DIR="$SANDBOX/uv_tools"
export UV_TOOL_BIN_DIR="$SANDBOX/uv_bin"
export PATH="$UV_TOOL_BIN_DIR:$PATH"
uv tool install "conexus==$FROM_VERSION" --reinstall >/dev/null 2>&1 \
    || _die "uv tool install conexus==$FROM_VERSION failed"
_pass "installed: $(nx --version)"
[[ "$(nx --version)" == *"$FROM_VERSION"* ]] || _die "version mismatch after install"

# ── 3. Install hooks (baseline stanza) ───────────────────────────────────────
_step "3/12 nx hooks install (writes the baseline stanza)"
# Run inside fakerepo so the hook lands in fakerepo/.git/hooks
(cd "$FAKE_REPO" && git init -q && git config user.email t@t.invalid && git config user.name T)
HOME="$SANDBOX" nx hooks install "$FAKE_REPO" >/dev/null
HOOK_OLD=$(cat "$FAKE_REPO/.git/hooks/post-commit")
[[ "$HOOK_OLD" == *'# >>> nexus managed begin >>>'* ]] || _die "baseline hook missing sentinel"
_pass "baseline hook installed with managed-block sentinel"
# The pgrep guard shipped in 5.0.1; whether the baseline already carries it
# depends on FROM_VERSION. Do NOT presume a pre-guard baseline — the script
# detects stanza drift at runtime (step 7) so it stays runnable from any
# baseline, including the latest stable. (nexus-a3nqp)
# nexus-x8fuq item C: this is pure classification, not a check that can
# fail (the sentinel assertion right above is the real one for this step) —
# _note, not _pass, so a reader never mistakes "both branches always print
# a checkmark" for a verified outcome.
if [[ "$HOOK_OLD" == *'pgrep -f'* ]]; then
    _note "baseline already has pgrep guard — expecting a clean upgrade"
else
    _note "baseline is pre-pgrep-guard — expecting drift on upgrade"
fi

# ── 4. Pre-upgrade snapshot ──────────────────────────────────────────────────
_step "4/12 Pre-upgrade state snapshot"
# nexus-x8fuq item B: previously assigned OLD_PLUGIN_DIR (never read) and
# discarded `nx --version`'s output, so this step was structurally unable
# to fail. It now captures the actually-installed pre-upgrade version and
# step 5 diffs the post-upgrade version against THIS snapshot (not just the
# nominal --from-version pin, which could in principle drift from what is
# really on disk).
OLD_VERSION_LINE="$(nx --version)"
OLD_VERSION="$(echo "$OLD_VERSION_LINE" | awk '{print $NF}')"
[[ -n "$OLD_VERSION" ]] \
    || _die "could not parse a version out of 'nx --version' pre-upgrade (got: $OLD_VERSION_LINE)"
_pass "pre-upgrade snapshot: $OLD_VERSION_LINE"

# ── 5. Upgrade to REPO_ROOT (this branch) ────────────────────────────────────
_step "5/12 uv tool install --reinstall from REPO_ROOT (the NEW version)"
uv tool install --reinstall "$REPO_ROOT" >/dev/null 2>&1 \
    || _die "uv tool install from REPO_ROOT failed"
NEW_VER="$(nx --version | awk '{print $NF}')"
# nexus-x8fuq item C: a version-identical "upgrade" never exercised the
# upgrade path at all — hard failure, not the old "(note: ...)" arm that
# let the run continue as if nothing were wrong.
_check_version_progressed "$NEW_VER" "$OLD_VERSION" \
    || _die "REPO_ROOT version ($NEW_VER) == pre-upgrade snapshot version ($OLD_VERSION) — this run never exercised the upgrade path. Bump pyproject.toml's version, or pass --from-version to pin an older baseline."
_pass "upgraded: $OLD_VERSION -> $NEW_VER"

# ── 6. nx doctor drift report (captured; cross-checked in step 7) ─────────────
_step "6/12 nx doctor stanza-drift report (captured for cross-check)"
# (2026-07-08 postmortem correction: an earlier fix attempt re-seeded
# repos.json here, on the theory that a baseline migration had deleted the
# step-1 seeding. False — the file survives the whole run; the real defect
# was list_repos_dual discarding the REGISTRY leg when the service-mode
# catalog proxy raises, fixed in src/nexus/repos.py. The step-1 seeding is
# sufficient; nothing extra is needed here.)
# gap-8 (T2 [22511]): a bare `|| true` here swallowed BOTH "doctor
# reported actionable warnings" (rc!=0, benign) AND "doctor crashed with a
# traceback" (rc!=0, NOT benign) into the same "continue" outcome. The
# negative-substring tests downstream (stanza-drift absence here; the
# demoted-verb absence right after) would then pass vacuously on a crash
# that printed no output at all. Capture the rc explicitly and assert
# doctor actually RAN (non-empty output, no traceback) before trusting any
# absence-based check against its content.
DOCTOR_RC=0
DOCTOR_OUT="$(HOME="$SANDBOX" nx doctor 2>&1)" || DOCTOR_RC=$?
[[ -n "$DOCTOR_OUT" ]] || _die "nx doctor (step 6) produced no output at all (rc=$DOCTOR_RC) — cannot assert on stanza-drift content"
[[ "$DOCTOR_OUT" != *'Traceback (most recent call last)'* ]] \
    || _die "nx doctor (step 6) crashed with a traceback (rc=$DOCTOR_RC). Output:\n$DOCTOR_OUT"
_pass "nx doctor ran (rc=$DOCTOR_RC, non-empty output, no traceback)"
if [[ "${DOCTOR_OUT,,}" == *'stanza drift'* ]]; then
    DRIFT_REPORTED=1
    [[ "$DOCTOR_OUT" == *'nx hooks update'* ]] \
        || _die "doctor reported drift but omitted the 'nx hooks update' fix suggestion"
    _pass "doctor reports stanza drift + names 'nx hooks update' as the fix"
else
    DRIFT_REPORTED=0
    _pass "doctor reports no stanza drift"
fi

# RDR-185 P4.2 (nexus-n7u38.29), asserted on the REAL upgraded install: the
# upgrade story is `nx upgrade` + `nx doctor`. Every remedy doctor prints must
# name a verb the user can actually find — a verb demoted out of --help is a
# dead end. This is the live counterpart to the source-level pin in
# tests/upgrade/test_verb_demotion.py: that one reads the module's strings,
# this one reads what a shipped install actually says to a real user.
# _check_no_demoted_verb / _DEMOTED_VERB_RE are defined above (nexus-x8fuq
# item A) so `$0 self-test` can demonstrate the regex catching a planted
# occurrence — health.py has zero live producers of these strings today,
# so this run's DOCTOR_OUT can never organically exercise the RED path;
# the guard stands against a future regression, not today's output.
_check_no_demoted_verb "$DOCTOR_OUT" \
    || _die "nx doctor advertised a DEMOTED verb as a remedy. Output:\n$DOCTOR_OUT"
_pass "nx doctor names no demoted verb (the story is nx upgrade + nx doctor)"

# ── 7. nx hooks update + drift cross-check ───────────────────────────────────
_step "7/12 nx hooks update refreshes the stanza in place"
HOME="$SANDBOX" nx hooks update "$FAKE_REPO" >/dev/null
HOOK_NEW=$(cat "$FAKE_REPO/.git/hooks/post-commit")
[[ "$HOOK_NEW" == *'# >>> nexus managed begin >>>'* ]] \
    || _die "after update, sentinel block missing"
SENTINEL_COUNT=$(echo "$HOOK_NEW" | grep -c '# >>> nexus managed begin >>>' || true)
[[ "$SENTINEL_COUNT" == "1" ]] || _die "expected 1 sentinel block, found $SENTINEL_COUNT"

# Cross-check the two INDEPENDENT drift signals: what nx doctor claimed
# (DRIFT_REPORTED, step 6) must agree with whether the stanza bytes actually
# changed on update (STANZA_CHANGED). This catches both doctor false-negatives
# (claims clean, stanza changed) and false-positives (claims drift, no change)
# without presuming any particular baseline version. (nexus-a3nqp)
if [[ "$HOOK_NEW" != "$HOOK_OLD" ]]; then STANZA_CHANGED=1; else STANZA_CHANGED=0; fi
[[ "$STANZA_CHANGED" == "$DRIFT_REPORTED" ]] || _die \
    "drift signal mismatch: nx doctor DRIFT_REPORTED=$DRIFT_REPORTED but actual STANZA_CHANGED=$STANZA_CHANGED"

if [[ "$STANZA_CHANGED" == "1" ]]; then
    # When the baseline predated the pgrep guard, the refreshed stanza must
    # now carry it — the concrete 5.0.1 migration this script was born to guard.
    if [[ "$HOOK_OLD" != *'pgrep -f'* ]]; then
        [[ "$HOOK_NEW" == *'pgrep -f'* ]] \
            || _die "stanza changed but pgrep guard still absent after update"
    fi
    _pass "stanza drift detected + reconciled (doctor and byte-diff agree)"
else
    _pass "no stanza drift (update is a clean no-op; doctor and byte-diff agree)"
fi

# Idempotency: a second update must not change the stanza further.
HOME="$SANDBOX" nx hooks update "$FAKE_REPO" >/dev/null
HOOK_NEW2=$(cat "$FAKE_REPO/.git/hooks/post-commit")
[[ "$HOOK_NEW2" == "$HOOK_NEW" ]] || _die "nx hooks update is not idempotent (second run changed the stanza)"
_pass "nx hooks update is idempotent"

# ── 8. nx doctor: drift resolved ─────────────────────────────────────────────
_step "8/12 nx doctor should NOT report drift after update"
# gap-8 (T2 [22511]): same positive-assertion-first fix as step 6 above —
# a doctor crash here previously produced empty/garbage output that could
# not contain "stanza drift" either, so the negative check below would
# have reported "drift resolved" even though doctor never actually ran.
DOCTOR_RC=0
DOCTOR_OUT="$(HOME="$SANDBOX" nx doctor 2>&1)" || DOCTOR_RC=$?
[[ -n "$DOCTOR_OUT" ]] || _die "nx doctor (step 8) produced no output at all (rc=$DOCTOR_RC) — cannot assert drift is resolved"
[[ "$DOCTOR_OUT" != *'Traceback (most recent call last)'* ]] \
    || _die "nx doctor (step 8) crashed with a traceback (rc=$DOCTOR_RC). Output:\n$DOCTOR_OUT"
if [[ "${DOCTOR_OUT,,}" == *'stanza drift'* ]]; then
    _die "drift warning persists after nx hooks update. Output:\n$DOCTOR_OUT"
fi
_pass "nx doctor ran clean (rc=$DOCTOR_RC) and drift resolved"

# ── 9. Plugin rename + tag-pin surface (marketplace.json) ────────────────────
_step "9/12 plugin marketplace.json reflects rename + tag pinning"
MJ="$REPO_ROOT/.claude-plugin/marketplace.json"
# Plugin name = conexus
grep -q '"name": "conexus"' "$MJ" \
    || _die "REPO_ROOT marketplace.json missing 'name: conexus'"
! grep -q '"name": "nx"' "$MJ" \
    || _die "REPO_ROOT marketplace.json still contains 'name: nx' (rename incomplete)"
# Source uses git-subdir object form with path + ref pinning
python3 -c "
import json, pathlib, sys
mj = json.loads(pathlib.Path('$MJ').read_text())
for p in mj['plugins']:
    src = p.get('source')
    if not isinstance(src, dict):
        sys.exit(f'plugin {p[\"name\"]!r} source must be object form, got {src!r}')
    if src.get('source') != 'git-subdir':
        sys.exit(f'plugin {p[\"name\"]!r} source.source must be git-subdir, got {src.get(\"source\")!r}')
    if not src.get('ref', '').startswith('v'):
        sys.exit(f'plugin {p[\"name\"]!r} source.ref must be tag form (vX.Y.Z), got {src.get(\"ref\")!r}')
    if src.get('ref') != f\"v{p['version']}\":
        sys.exit(f'plugin {p[\"name\"]!r} source.ref {src.get(\"ref\")!r} != version v{p[\"version\"]}')
print('  all plugins: git-subdir source + ref pinned to v{version}')
" || _die "marketplace.json pinning check failed"
_pass "marketplace.json: rename (conexus) + tag pinning (git-subdir + ref=v\$version)"

# ── 10. Plugin-name drift detection (nexus-mkj6u) ────────────────────────────
_step "10/12 plugin-name drift detected when OLD nx plugin still installed"
# Simulate the user's "ran uv tool upgrade but didn't reinstall plugin"
# state by planting a fake CLAUDE_PLUGIN_ROOT with name=nx.
FAKE_PLUGIN_ROOT="$SANDBOX/fake_plugin_root"
mkdir -p "$FAKE_PLUGIN_ROOT/.claude-plugin"
cat > "$FAKE_PLUGIN_ROOT/.claude-plugin/plugin.json" << 'PJEOF'
{
  "name": "nx",
  "version": "4.34.5",
  "description": "stale OLD plugin still installed in Claude Code"
}
PJEOF

# nx doctor with CLAUDE_PLUGIN_ROOT set to OLD plugin should surface the drift.
DOCTOR_OUT="$(CLAUDE_PLUGIN_ROOT="$FAKE_PLUGIN_ROOT" HOME="$SANDBOX" nx doctor 2>&1 || true)"
[[ "${DOCTOR_OUT,,}" == *'plugin name'* ]] \
    || _die "nx doctor did not surface plugin-name drift. Output:\n$DOCTOR_OUT"
[[ "$DOCTOR_OUT" == *'/plugin install conexus@nexus-plugins'* ]] \
    || _die "doctor warning missing /plugin install hint"
[[ "$DOCTOR_OUT" == *'/reload-plugins'* ]] \
    || _die "doctor warning missing /reload-plugins hint"
_pass "nx doctor names both /plugin install and /reload-plugins migration commands"
# (The structlog warning at every MCP startup is covered by unit test
# tests/test_plugin_name_drift.py::test_check_version_compatibility_logs_plugin_name_mismatch
# — easier to assert there than to spawn nx-mcp + watch stderr here.)

# ── 11. .mcpb production bundle packs from REPO_ROOT ─────────────────────────
_step "11/12 .mcpb bundle packs cleanly from REPO_ROOT/mcpb"
if [ -f "$REPO_ROOT/mcpb/manifest.json" ]; then
    cd "$REPO_ROOT/mcpb"
    rm -f conexus.mcpb
    npx -y @anthropic-ai/mcpb@latest pack . conexus.mcpb >/dev/null 2>&1 \
        || _die "mcpb pack failed in $REPO_ROOT/mcpb"
    BUNDLE_SIZE=$(stat -f%z conexus.mcpb 2>/dev/null || stat -c%s conexus.mcpb 2>/dev/null)
    rm -f conexus.mcpb
    cd - >/dev/null
    # nexus-x8fuq item E: bounded on BOTH sides now — an empty/near-empty
    # pack (a silently-broken `mcpb pack` that produced nothing usable) is
    # exactly as much a failure as an oversized one. _check_mcpb_size /
    # MCPB_MIN_BYTES / MCPB_MAX_BYTES are defined above; see `$0 self-test`
    # for the RED/GREEN demonstration against planted byte counts.
    MCPB_VERDICT="$(_check_mcpb_size "${BUNDLE_SIZE:-}")" \
        || _die "conexus.mcpb is ${BUNDLE_SIZE:-0} bytes ($MCPB_VERDICT; expected $MCPB_MIN_BYTES-$MCPB_MAX_BYTES bytes); check .mcpbignore / the pack step"
    _pass "mcpb pack produces a $BUNDLE_SIZE-byte bundle (within [$MCPB_MIN_BYTES, $MCPB_MAX_BYTES] bytes)"
else
    # gap-8 non-vacuity note (T2 [22511], house convention: a skip-passing
    # gate must say why skipping here is safe): mcpb/manifest.json is
    # present on every normal develop/release branch — it ships the .mcpb
    # bundle this repo publishes. This else-clause's ONLY legitimate
    # trigger is running this script against a branch that predates
    # mcpb/ entirely. It is not a routine no-op: on every ordinary
    # invocation of this script the `if` arm above runs for real and can
    # fail (a broken `mcpb pack` or an out-of-range bundle reds this step).
    # nexus-okbr8: a skip is reported DISTINCTLY from a pass — its own
    # marker, its own count (SKIP_COUNT), rolled into the final sentinel —
    # never rendered with the same ✓ a real pass gets.
    _skip "mcpb/ not present on this branch — see note above; not the common path"
fi

# ── 12. Summary ──────────────────────────────────────────────────────────────
_step "12/12 PASS"
echo "  Upgrade-shakeout green: $FROM_VERSION → $NEW_VER"
if [[ "$STANZA_CHANGED" == "1" ]]; then
    echo "  - hook stanza drift detected + reconciled (doctor + byte-diff agree)"
else
    echo "  - hook stanza unchanged across upgrade (clean no-op; doctor + byte-diff agree)"
fi
echo "  - nx doctor drift detection works (cross-checked against actual diff)"
echo "  - nx hooks update refreshes in-place + is idempotent"
echo "  - plugin rename visible in marketplace.json"
echo
# nexus-okbr8: a greppable, machine-checkable terminal sentinel carrying
# counts — not just a step-title echo. Under `set -euo pipefail`, every
# _die above exits before this line, so reaching it at all already means
# every real assertion passed; this line is not an unconditional claim
# layered on top, it is the natural last statement of a run that got here.
echo "UPGRADE-SHAKEOUT PASSED — steps=12 skipped=$SKIP_COUNT"
echo
echo "Sandbox preserved at $SANDBOX (inspect, then '$0 reset' to remove)"
