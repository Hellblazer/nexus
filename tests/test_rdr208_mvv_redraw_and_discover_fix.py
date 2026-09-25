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
    """The full `name() { ... }` block (header line through the closing
    brace at column 0), extracted verbatim from `text`."""
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
            printf '%s\n' '{"holders":["SID1"]}'
        else
            printf '%s\n' '{"holders":[]}'
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
