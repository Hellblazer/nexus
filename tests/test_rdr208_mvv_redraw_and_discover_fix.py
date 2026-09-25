# SPDX-License-Identifier: AGPL-3.0-or-later
"""RDR-219 nexus-wauo1.13 coordinator follow-up (2026-09-25): two real
defects found while proving the credential migration, both in
tests/e2e/rdr208-mvv/mvv_in_container.sh's /resume name-collision retry
machinery. Neither is credential-related, but both were fixed in the same
worktree per the coordinator's explicit instruction rather than filed for
later. Two consecutive real proof runs of the SAME image (2026-09-25) both
reported spurious step-2 "collisions" -- 4 of 5, then 1 of 2, consecutive
attempts landing on the identical pre-resume name -- which is not
plausible under the harness's own stated ~1-in-256 uniform-draw model
(P(both runs collide at all) ~ 1.5e-5). Both defects below fully explain
it without any change to the actual name-draw rate.

1. `redraw_until_distinct`'s REDRAW_FN argument is `launch()`+`arm()` for
   real, both of which print PASS/FAIL/progress lines to STDOUT. Left
   unredirected, that noise leaked into the function's own
   `_rd_out="$(redraw_until_distinct ...)"` capture at its two call
   sites, corrupting the documented two-line "NAME\nREDRAW_COUNT"
   contract -- exactly the symptom both proof runs hit: a garbled name
   (`FAIL directory/  session A2: id ...` -- literally "directory/" with
   an empty name spliced in) and a multi-line "count" that failed
   `check_redraw_rate`'s integer comparison with `4: integer expression
   expected`.

2. `discover_name`'s directory scan is ambiguous for a RESUMED session:
   `/resume` keeps the SAME session id as the pre-resume launch, and a
   plain `/exit` never releases that launch's own directory entry
   (`stop()`'s own comment says so explicitly). So once A2 shares A's
   session id, EVERY subspace A ever armed also lists A2's session id as
   a holder, and an unqualified "does any subspace's holders include
   this session id" scan cannot tell A's still-live stale entry apart
   from A2's own freshly-armed one -- it returns whichever the query
   happens to enumerate first, observed both runs to be the stale
   pre-resume name.

A THIRD round (same date, coordinator review of the first two): the same
ambiguity infects `armed_name_known`'s "did A2 arm at all" verification,
and the FIRST fix for it (an EXCLUDE parameter that falls back to the
UNQUALIFIED check on ambiguity) put the vacuous-pass gap right back --
that unqualified fallback always finds A's stale entry, so a resumed
session whose arm genuinely never happened would still report success,
exactly the case the fix was meant to catch. The corrected fallback reads
LEASE evidence instead of guessing: `directory/<name>` rows carry
per-entry `created_at` (`src/nexus/commands/tuple_cmd.py`), and a
genuinely NEW arm always writes a NEW row with a fresh nonce
(`src/nexus/mcp/subscriptions.py`'s `_directory_heartbeat`: a fresh nonce
on first send, the SAME nonce -- extending the SAME row -- on every 60s
renewal after that). A BEFORE/AFTER snapshot of the excluded name's own
`created_at` values, taken around the arm attempt, proves whether
anything was actually written: if not, `arm()` now reports a real
FAILURE rather than a silent pass.

These are executable, BEHAVIORAL tests, not this file's usual grep-based
structural style, because both functions are pure enough to extract and
exercise directly: `redraw_until_distinct` only touches its caller-named
REDRAW_FN/DISCOVER_FN; `discover_name`'s EXCLUDE parameter is plain string
logic once `nx`/`jq` are stubbed. Each function body is extracted FRESH
from the tracked file via regex (never retyped), so these tests track the
real implementation and cannot silently drift from it.
"""
from __future__ import annotations

import pathlib
import re
import subprocess

import pytest

pytestmark = pytest.mark.lint

_ROOT = pathlib.Path(__file__).resolve().parent.parent
_MVV_SH = _ROOT / "tests" / "e2e" / "rdr208-mvv" / "mvv_in_container.sh"


def _extract_function(text: str, name: str) -> str:
    """The full `name() { ... }` block, extracted verbatim from `text`.
    Tries the one-liner shape first (`name() { ...; }` all on one line,
    e.g. `armed_name_known`), then the multi-line shape (header line
    through a closing brace at column 0, e.g. `arm`/`discover_name`)."""
    m = re.search(rf"^{re.escape(name)}\(\) \{{[^\n]*\}}\n", text, re.M)
    if m:
        return m.group(0)
    m = re.search(rf"^{re.escape(name)}\(\) \{{.*?\n(?:.*?\n)*?^\}}\n", text, re.M)
    assert m, f"could not find a `{name}() {{ ... }}` block"
    return m.group(0)


def _run_bash(script: str) -> subprocess.CompletedProcess:
    return subprocess.run(["bash", "-c", script], capture_output=True, text=True, timeout=30)


_NOISY_DISTINCT_AFTER_TWO = r"""
set -uo pipefail
COUNTER_FILE="$(mktemp)"
echo 0 > "$COUNTER_FILE"
noisy_redraw_fn() {
    echo "  PASS  something happened"
    echo "  FAIL  something else happened"
    echo "  session X armed its own name: whatever"
    return 0
}
flaky_discover_fn() {
    local n; n="$(cat "$COUNTER_FILE")"
    n=$((n + 1))
    echo "$n" > "$COUNTER_FILE"
    if [ "$n" -le 2 ]; then echo "stale-name"; else echo "fresh-name"; fi
}
_rd_out="$(redraw_until_distinct "stale-name" noisy_redraw_fn flaky_discover_fn 8)"
rc=$?
NAME="${_rd_out%%$'\n'*}"
COUNT="${_rd_out#*$'\n'}"
printf 'RC=%s\nNAME=%s\nCOUNT=%s\n' "$rc" "$NAME" "$COUNT"
"""


def test_redraw_until_distinct_does_not_leak_redraw_fns_stdout() -> None:
    """A REDRAW_FN that prints noisy PASS/FAIL lines to stdout (exactly
    what launch()+arm() do for real) and eventually produces a distinct
    name must not corrupt the captured NAME/REDRAW_COUNT. RED without the
    `1>&2` fix: NAME comes back containing the noise text, and COUNT comes
    back multi-line."""
    mvv = _MVV_SH.read_text(encoding="utf-8")
    fn = _extract_function(mvv, "redraw_until_distinct")
    proc = _run_bash(fn + _NOISY_DISTINCT_AFTER_TWO)
    assert proc.returncode == 0, proc.stderr
    out = dict(line.split("=", 1) for line in proc.stdout.splitlines() if "=" in line)
    assert out.get("RC") == "0", proc.stdout + proc.stderr
    assert out.get("NAME") == "fresh-name", (
        f"expected the clean discovered name, got {out.get('NAME')!r} -- "
        f"redraw_fn's own stdout noise leaked into the capture:\n{proc.stdout!r}"
    )
    assert out.get("COUNT") == "2", (
        f"expected a clean integer redraw count, got {out.get('COUNT')!r}:\n{proc.stdout!r}"
    )


_STALE_ENTRY_STUB = r"""
set -uo pipefail
declare -A SID_OF=([A2]="SID1")
nx() {
    if [ "$1" = tuple ] && [ "$2" = list ]; then
        printf '%s\n' '[{"subspace":"directory/work-79"}__EXTRA_SUBSPACE__]'
    elif [ "$1" = tuple ] && [ "$2" = directory ]; then
        if [ "$3" = work-79 ] || { [ -n "${SECOND_NAME:-}" ] && [ "$3" = "$SECOND_NAME" ]; }; then
            printf '%s\n' '{"holders":["SID1"],"entries":[{"session_id":"SID1","created_at":"2026-09-25T10:00:00.000000Z"}]}'
        else
            printf '%s\n' '{"holders":[],"entries":[]}'
        fi
    fi
}
jq() { command jq "$@"; }
"""


def test_discover_name_unqualified_returns_the_first_enumerated_match() -> None:
    """Pin the EXISTING (still-present, by design) ambiguity: with no
    EXCLUDE, discover_name returns whatever the scan enumerates first --
    here, deliberately, the STALE entry -- documenting exactly why the
    /resume site cannot use the unqualified form."""
    mvv = _MVV_SH.read_text(encoding="utf-8")
    fn = _extract_function(mvv, "discover_name")
    script = (
        fn
        + _STALE_ENTRY_STUB.replace("__EXTRA_SUBSPACE__", ',{"subspace":"directory/work-2a"}')
        + '\nSECOND_NAME=work-2a discover_name A2\n'
    )
    proc = _run_bash(script)
    assert proc.returncode == 0, proc.stderr
    assert proc.stdout == "work-79", proc.stdout


def test_discover_name_exclude_skips_the_named_candidate() -> None:
    """RED without the EXCLUDE parameter (the call below would raise a
    bash 'unbound variable' under set -u, or -- on a version tolerant of
    the extra positional arg -- silently ignore it and still return
    work-79). GREEN after: excluding the known stale name surfaces the
    genuinely different entry."""
    mvv = _MVV_SH.read_text(encoding="utf-8")
    fn = _extract_function(mvv, "discover_name")
    script = (
        fn
        + _STALE_ENTRY_STUB.replace("__EXTRA_SUBSPACE__", ',{"subspace":"directory/work-2a"}')
        + '\nSECOND_NAME=work-2a discover_name A2 work-79\n'
    )
    proc = _run_bash(script)
    assert proc.returncode == 0, proc.stderr
    assert proc.stdout == "work-2a", proc.stdout


def test_discover_name_exclude_falls_through_when_nothing_else_exists() -> None:
    """Excluding the only entry that exists must find nothing (empty),
    not error -- discover_a2_name relies on this to fall back correctly."""
    mvv = _MVV_SH.read_text(encoding="utf-8")
    fn = _extract_function(mvv, "discover_name")
    script = fn + _STALE_ENTRY_STUB.replace("__EXTRA_SUBSPACE__", "") + "\ndiscover_name A2 work-79\n"
    proc = _run_bash(script)
    assert proc.returncode == 1, (proc.returncode, proc.stdout, proc.stderr)
    assert proc.stdout == "", proc.stdout


def test_discover_a2_name_prefers_a_genuinely_distinct_entry_over_the_stale_one() -> None:
    """The actual /resume-site fix: with both A's stale entry (work-79)
    and A2's own fresh one (work-2a) present for the same session id,
    discover_a2_name must report the fresh one, not the stale one --
    RED against the pre-fix `printf '%s' "${NAME_OF[A2]:-}"` body (which
    this test does not exercise directly, since NAME_OF[A2] is set
    elsewhere by arm() -- the meaningful RED/GREEN split here is against
    discover_name's own unqualified scan, covered above; this test pins
    the wrapper's control flow on top of it)."""
    mvv = _MVV_SH.read_text(encoding="utf-8")
    fn = _extract_function(mvv, "discover_name") + _extract_function(mvv, "discover_a2_name")
    script = (
        fn
        + _STALE_ENTRY_STUB.replace("__EXTRA_SUBSPACE__", ',{"subspace":"directory/work-2a"}')
        + '\nA_NAME=work-79\nSECOND_NAME=work-2a discover_a2_name\n'
    )
    proc = _run_bash(script)
    assert proc.returncode == 0, proc.stderr
    assert proc.stdout == "work-2a", proc.stdout


def test_discover_a2_name_falls_back_to_the_pre_resume_name_when_no_rename_yet() -> None:
    """Before A2 has armed anything new (or on a genuine same-name
    re-collision), only the pre-resume entry exists -- discover_a2_name
    must report $A_NAME itself (not empty), so redraw_until_distinct's
    `cur = pre` comparison correctly triggers a real redraw rather than
    misreading "nothing found yet" as a hard arm failure."""
    mvv = _MVV_SH.read_text(encoding="utf-8")
    fn = _extract_function(mvv, "discover_name") + _extract_function(mvv, "discover_a2_name")
    script = fn + _STALE_ENTRY_STUB.replace("__EXTRA_SUBSPACE__", "") + "\nA_NAME=work-79\ndiscover_a2_name\n"
    proc = _run_bash(script)
    assert proc.returncode == 0, proc.stderr
    assert proc.stdout == "work-79", proc.stdout


# ──────────────────── armed_name_known / arm(): the vacuous-pass gap ─────────


_ARM_STUB = r"""
set -uo pipefail
declare -A SID_OF=([A2]="SID1") NAME_OF=()
prompt() { return 0; }
wait_for() { shift; "$@"; }
tok() { printf 'TOK'; }
"""


def test_armed_name_known_is_not_vacuously_true_when_excluding_the_stale_entry() -> None:
    """The coordinator's exact complaint: with ONLY A's stale entry
    present for this session id, armed_name_known must NOT report success
    when asked to exclude that entry -- RED against the pre-fix
    `armed_name_known() { [ -n "$(discover_name "$1")" ]; }` (a single
    positional arg, silently ignoring a second one under `set -u`, so the
    call below would still see the stale entry and return 0)."""
    mvv = _MVV_SH.read_text(encoding="utf-8")
    fn = _extract_function(mvv, "discover_name") + _extract_function(mvv, "armed_name_known")
    script = fn + _STALE_ENTRY_STUB.replace("__EXTRA_SUBSPACE__", "") + "\narmed_name_known A2 work-79\n"
    proc = _run_bash(script)
    assert proc.returncode == 1, (proc.returncode, proc.stdout, proc.stderr)


def test_armed_name_known_excluding_the_stale_entry_finds_a_genuinely_new_one() -> None:
    """The positive case: a genuinely different entry exists alongside
    the stale one -- excluding the stale name must still report success."""
    mvv = _MVV_SH.read_text(encoding="utf-8")
    fn = _extract_function(mvv, "discover_name") + _extract_function(mvv, "armed_name_known")
    script = (
        fn
        + _STALE_ENTRY_STUB.replace("__EXTRA_SUBSPACE__", ',{"subspace":"directory/work-2a"}')
        + '\nSECOND_NAME=work-2a armed_name_known A2 work-79\n'
    )
    proc = _run_bash(script)
    assert proc.returncode == 0, proc.stderr


def test_arm_reports_the_genuinely_new_name_when_one_exists() -> None:
    """arm()'s primary, happy-path branch: EXCLUDE is given, and a
    genuinely distinct entry already exists (as if the model's
    tuple_subscribe call, stubbed here via a no-op prompt(), had already
    landed) -- arm() must report success and record the NEW name, not the
    excluded stale one."""
    mvv = _MVV_SH.read_text(encoding="utf-8")
    fn = (
        _extract_function(mvv, "discover_name")
        + _extract_function(mvv, "armed_name_known")
        + _extract_function(mvv, "_own_directory_created_ats")
        + _extract_function(mvv, "arm")
    )
    script = (
        fn
        + _ARM_STUB
        + _STALE_ENTRY_STUB.replace("__EXTRA_SUBSPACE__", ',{"subspace":"directory/work-2a"}')
        + '\nSECOND_NAME=work-2a arm A2 work-79\n'
        + 'rc=$?\n'
        + 'printf \'RC=%s\\nNAME=%s\\n\' "$rc" "${NAME_OF[A2]:-}"\n'
    )
    proc = _run_bash(script)
    assert proc.returncode == 0, proc.stderr
    out = dict(line.split("=", 1) for line in proc.stdout.splitlines() if "=" in line)
    assert out.get("RC") == "0", proc.stdout
    assert out.get("NAME") == "work-2a", proc.stdout


# The lease-evidence fallback (coordinator's second round, 2026-09-25): a
# naive fallback to the UNQUALIFIED check ("does ANY entry exist for this
# session id") puts the vacuous-pass gap right back -- A's stale entry
# always satisfies it, whether or not A2 ever armed anything. The fix
# reads LEASE evidence instead: directory/<name> entries carry per-row
# `created_at` (src/nexus/commands/tuple_cmd.py), and a genuinely NEW arm
# (a different process, its own fresh in-process lease state) always
# writes a NEW row with a fresh nonce (src/nexus/mcp/subscriptions.py),
# so a BEFORE/AFTER snapshot of created_at values under the excluded name
# reveals whether THIS arm attempt wrote anything, without guessing.
# `_own_directory_created_ats`'s own `nx` calls for directory/work-79 are
# counted here (via DIR_CALLS_FILE) so the stub can return the "before"
# shape on the first call and, when SIMULATE_NEW_ROW=1, an ADDITIONAL row
# from the second call onward -- simulating a real lease write that
# happened between arm()'s "before" snapshot and its "after" one.
_LEASE_EVIDENCE_STUB = r"""
set -uo pipefail
declare -A SID_OF=([A2]="SID1") NAME_OF=()
prompt() { return 0; }
wait_for() { shift; "$@"; }
tok() { printf 'TOK'; }
DIR_CALLS_FILE="$(mktemp)"
echo 0 > "$DIR_CALLS_FILE"
nx() {
    if [ "$1" = tuple ] && [ "$2" = list ]; then
        printf '%s\n' '[{"subspace":"directory/work-79"}]'
    elif [ "$1" = tuple ] && [ "$2" = directory ] && [ "$3" = work-79 ]; then
        local n; n="$(cat "$DIR_CALLS_FILE")"; n=$((n + 1)); echo "$n" > "$DIR_CALLS_FILE"
        if [ "$n" -ge 2 ] && [ "${SIMULATE_NEW_ROW:-0}" = 1 ]; then
            printf '%s\n' '{"holders":["SID1"],"entries":[{"session_id":"SID1","created_at":"2026-09-25T10:00:00.000000Z"},{"session_id":"SID1","created_at":"2026-09-25T10:05:00.000000Z"}]}'
        else
            printf '%s\n' '{"holders":["SID1"],"entries":[{"session_id":"SID1","created_at":"2026-09-25T10:00:00.000000Z"}]}'
        fi
    else
        printf '%s\n' '{"holders":[],"entries":[]}'
    fi
}
jq() { command jq "$@"; }
"""


def test_arm_fails_when_no_new_lease_write_since_the_arm_began() -> None:
    """The coordinator's exact scenario: A2's arm never genuinely happens
    (prompt() is stubbed as a no-op -- nothing calls tuple_subscribe), and
    A's stale entry is the only thing present, unchanged throughout. RED
    against the FIRST round's fallback (`if ! armed_name_known "$1"; then
    ...; fi` -- an UNQUALIFIED check, which finds the stale entry and
    reports success): GREEN only once the fallback requires lease
    evidence of an actual write during this arm attempt, which does not
    exist here."""
    mvv = _MVV_SH.read_text(encoding="utf-8")
    fn = (
        _extract_function(mvv, "discover_name")
        + _extract_function(mvv, "armed_name_known")
        + _extract_function(mvv, "_own_directory_created_ats")
        + _extract_function(mvv, "arm")
    )
    script = fn + _LEASE_EVIDENCE_STUB + "\narm A2 work-79\n"
    proc = _run_bash(script)
    assert proc.returncode == 1, (proc.returncode, proc.stdout, proc.stderr)


def test_arm_treats_a_new_lease_write_under_the_excluded_name_as_success() -> None:
    """The genuine same-name re-collision, now backed by real evidence
    instead of guessed: a SECOND row appears under directory/work-79 for
    this session id, with a created_at after the arm began -- proof that
    THIS attempt's tuple_subscribe call actually landed, even though
    Claude happened to re-pick the identical name. arm() must treat this
    as success, not a failure -- redraw_until_distinct's own comparison
    against $exclude handles it like any other collision from there."""
    mvv = _MVV_SH.read_text(encoding="utf-8")
    fn = (
        _extract_function(mvv, "discover_name")
        + _extract_function(mvv, "armed_name_known")
        + _extract_function(mvv, "_own_directory_created_ats")
        + _extract_function(mvv, "arm")
    )
    script = (
        fn
        + _LEASE_EVIDENCE_STUB
        + 'SIMULATE_NEW_ROW=1 arm A2 work-79\n'
        + 'rc=$?\n'
        + 'printf \'RC=%s\\nNAME=%s\\n\' "$rc" "${NAME_OF[A2]:-}"\n'
    )
    proc = _run_bash(script)
    assert proc.returncode == 0, proc.stderr
    out = dict(line.split("=", 1) for line in proc.stdout.splitlines() if "=" in line)
    assert out.get("RC") == "0", proc.stdout
    assert out.get("NAME") == "work-79", proc.stdout


def test_arm_reports_a_real_failure_when_nothing_exists_at_all() -> None:
    """Not even the excluded name's own entry exists for this session id
    -- a genuine arm failure (the model never subscribed anything, and
    somehow not even the pre-resume entry survives, which should not
    happen but is checked rather than assumed). arm() must return
    non-zero."""
    mvv = _MVV_SH.read_text(encoding="utf-8")
    fn = (
        _extract_function(mvv, "discover_name")
        + _extract_function(mvv, "armed_name_known")
        + _extract_function(mvv, "_own_directory_created_ats")
        + _extract_function(mvv, "arm")
    )
    stub = _STALE_ENTRY_STUB.replace("__EXTRA_SUBSPACE__", "").replace(
        '[ "$3" = work-79 ]', '[ "$3" = nothing-matches-anything ]'
    )
    script = fn + _ARM_STUB + stub + '\narm A2 work-79\n'
    proc = _run_bash(script)
    assert proc.returncode == 1, (proc.returncode, proc.stdout, proc.stderr)
