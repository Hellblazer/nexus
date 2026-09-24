# SPDX-License-Identifier: AGPL-3.0-or-later
"""nexus-4ahul: the RDR-208 local-mode MVV asserted a resumed session's
instance name DIFFERS from its pre-resume name (mvv_in_container.sh step 2),
but that name is Claude Code's own -- a work-XX two-hex suffix redrawn at
every process start, uniformly over 256 values and not seeded by anything
this harness controls (see the RDR-208 doc's own "changes: a new random
suffix at every process start"). About once in 256 resumes the pre-resume
and resumed sessions draw the SAME suffix, which is not the rename failing
to happen -- it is two independent draws landing on the same value -- and it
cascaded into step 4's release check too, because the pre-resume session was
stopped rather than released, so its own lease under that name was still
live. Measured 2026-09-19: 68 passed / 2 failed on a billed run (T2
nexus/rdr208-mvv-name-collision-flake-2026-09-19).

The fix (mvv_in_container.sh's ``redraw_until_distinct``) does not widen the
random space -- that only lowers the odds of the SAME failure, it does not
make it impossible -- and it does not seed the draw, because Claude Code's
own draw is opaque to this harness; there is nothing here to seed. What
makes the collision impossible to observe as a false PASS or a false FAIL is
redrawing for real: a relaunch is a genuine new process start, so each
redraw is an independent draw, bounded by a cap so a run that is
inconclusive every time cannot be mistaken for a run that is fine
(nexus-moht0 non-vacuity) -- a cap exhaustion is reported through its own
exit code, never silently folded into PASS or FAIL.

ROUND 2 (review T2 nexus/critique-nexus-4ahul-rdr208-mvv-redraw-fix-2026-09-24
[26761]), four more findings, each covered below:

1. FALSE PASS ON AN EMPTY DRAW: ``discover_fn`` reading an unset NAME_OF
   slot (arm() never got that far) returns "", and "" != PRE_NAME is true --
   so an EMPTY name looked like a successful rename. Now its own exit code
   (3), never folded into "distinct".
2. SIBLING SITE: the A/B independent-launch site carries the identical
   collision risk (RDR-208 Gap 2) and is now redrawn the same way.
3. ORPHANED LEASES: a collided intermediate A2 arm attempt still subscribes
   to directory/$A_NAME for real before being killed, so step 3b's TTL wait
   is re-anchored to when the WHOLE redraw sequence ends, not to when it
   started -- a safe upper bound on the last possible such subscribe.
4. SILENT RATE DRIFT: each site's redraw count is reported in the MVV
   output and threshold-checked (>2 redraws, ~1-in-16.7M under the stated
   uniform-draw model, fails loudly rather than being silently absorbed).

This module forces the exact colliding draw deterministically via stub
REDRAW_FN/DISCOVER_FN callables (no tmux, no Claude Code, no billed run) and
executes the real functions EXTRACTED VERBATIM from the script via
``bash -c``, so a future edit to the actual code under test, not a
hand-copied duplicate, is what these assertions exercise.
"""
from __future__ import annotations

import re
import subprocess
from pathlib import Path

import pytest

pytestmark = pytest.mark.lint

_ROOT = Path(__file__).resolve().parent.parent
_SCRIPT = _ROOT / "tests" / "e2e" / "rdr208-mvv" / "mvv_in_container.sh"


def _extract_function(text: str, name: str) -> str:
    """The named top-level ``name() { ... }`` function, verbatim, ending at
    the first line that is exactly ``}``. Raises if the function is not
    found or the terminating brace is missing, rather than silently
    returning a truncated or empty body: a stale extraction pattern must
    fail this test loudly, not pass on an empty script."""
    lines = text.splitlines()
    start = next((i for i, ln in enumerate(lines) if ln.startswith(f"{name}() {{")), None)
    assert start is not None, f"{name}() not found in {_SCRIPT}"
    end = next((i for i in range(start + 1, len(lines)) if lines[i] == "}"), None)
    assert end is not None, f"no closing brace found for {name}() in {_SCRIPT}"
    return "\n".join(lines[start : end + 1])


@pytest.fixture(scope="module")
def script_text() -> str:
    return _SCRIPT.read_text(encoding="utf-8")


@pytest.fixture(scope="module")
def redraw_fn_src(script_text: str) -> str:
    return _extract_function(script_text, "redraw_until_distinct")


@pytest.fixture(scope="module")
def check_redraw_rate_src(script_text: str) -> str:
    return _extract_function(script_text, "check_redraw_rate")


def _run(script_body: str, *args: str) -> subprocess.CompletedProcess[str]:
    """Run redraw_until_distinct PRE_NAME redraw_fn discover_fn CAP in a
    fresh bash, with the caller's own redraw_fn/discover_fn/counter
    definitions prepended as *script_body*."""
    full = f"set -uo pipefail\n{script_body}\nredraw_until_distinct {' '.join(args)}\n"
    return subprocess.run(["bash", "-c", full], capture_output=True, text=True, timeout=30)


def _lines(stdout: str) -> tuple[str, str]:
    """redraw_until_distinct's contract: stdout is always exactly two lines,
    "NAME\\nREDRAW_COUNT"."""
    parts = stdout.splitlines()
    assert len(parts) == 2, f"expected exactly 2 stdout lines, got {parts!r}"
    return parts[0], parts[1]


def test_function_is_defined_and_parses(redraw_fn_src: str) -> None:
    proc = subprocess.run(
        ["bash", "-c", f"{redraw_fn_src}\ntype redraw_until_distinct"],
        capture_output=True, text=True,
    )
    assert proc.returncode == 0, proc.stderr


def test_no_collision_returns_immediately_with_zero_redraws(redraw_fn_src: str) -> None:
    """The common case: the resumed session draws a distinct name on the
    first arm. No redraw callback is ever invoked, and the reported redraw
    count is 0."""
    body = f"""
{redraw_fn_src}
discover() {{ printf 'work-b9'; }}
redraw() {{ echo REDRAW_CALLED_UNEXPECTEDLY >&2; exit 9; }}
"""
    proc = _run(body, "work-66", "redraw", "discover", "8")
    assert proc.returncode == 0, proc.stderr
    name, count = _lines(proc.stdout)
    assert name == "work-b9"
    assert count == "0"
    assert "REDRAW_CALLED_UNEXPECTEDLY" not in proc.stderr
    assert proc.stderr == "", "no redraw diagnostics expected when nothing collided"


def test_forced_collision_then_a_distinct_draw_recovers(redraw_fn_src: str) -> None:
    """The exact nexus-4ahul scenario, forced deterministically: the first
    draw collides with the pre-resume name (work-66 == work-66, precisely
    what killed the billed run on 2026-09-19); the harness must redraw for
    real (the stub counts real invocations of its redraw callback) and land
    on the distinct second draw, never asserting the false "differs" check
    against the colliding first draw. The reported count is 1."""
    body = f"""
{redraw_fn_src}
STATE_FILE="$(mktemp)"
echo 0 > "$STATE_FILE"
discover() {{
    n="$(cat "$STATE_FILE")"
    if [ "$n" = 0 ]; then printf 'work-66'; else printf 'work-c0'; fi
}}
redraw() {{
    echo "$(($(cat "$STATE_FILE") + 1))" > "$STATE_FILE"
}}
"""
    proc = _run(body, "work-66", "redraw", "discover", "8")
    assert proc.returncode == 0, proc.stderr
    name, count = _lines(proc.stdout)
    assert name == "work-c0", "must land on the SECOND, distinct draw"
    assert count == "1"
    assert "collided with work-66 (draw 1/8): redrawing" in proc.stderr
    assert "distinct name after 1 redraw(s): work-c0" in proc.stderr


def test_persistent_collision_exhausts_the_cap_and_reports_it_not_pass_or_fail(redraw_fn_src: str) -> None:
    """A collision on every single draw (persistent, not one-in-256) is a
    real regression -- e.g. Claude Code's naming stopped being random -- and
    must never be silently absorbed as a PASS (the differs-check would be
    false) or misreported as an ordinary FAIL indistinguishable from the
    thing the redraw exists to rule out. The cap trips its own exit code,
    and the reported count equals the cap."""
    body = f"""
{redraw_fn_src}
discover() {{ printf 'work-66'; }}
CALLS_FILE="$(mktemp)"; echo 0 > "$CALLS_FILE"
redraw() {{ echo "$(($(cat "$CALLS_FILE") + 1))" > "$CALLS_FILE"; }}
"""
    proc = _run(body, "work-66", "redraw", "discover", "3")
    assert proc.returncode == 1, (proc.stdout, proc.stderr)
    name, count = _lines(proc.stdout)
    assert name == "work-66"
    assert count == "3"
    assert "redraw cap (3) exhausted: every draw collided with work-66" in proc.stderr
    # exactly CAP redraw attempts were made -- never fewer (giving up early)
    # and never an unbounded loop (nexus-moht0: a skip must carry a cap).
    assert proc.stderr.count("redrawing") == 3


def test_a_failed_relaunch_is_distinguished_from_a_persistent_collision(redraw_fn_src: str) -> None:
    """REDRAW_FN failing (the relaunch itself, e.g. `launch` timing out) is
    a different failure than the name never becoming distinct, and gets its
    own exit code so the caller's diagnostic does not misname the cause."""
    body = f"""
{redraw_fn_src}
discover() {{ printf 'work-66'; }}
redraw() {{ return 1; }}
"""
    proc = _run(body, "work-66", "redraw", "discover", "8")
    assert proc.returncode == 2, (proc.stdout, proc.stderr)


def test_empty_first_draw_is_a_hard_failure_not_a_false_pass(redraw_fn_src: str) -> None:
    """Round 2, finding 1: discover_fn returning EMPTY (e.g. NAME_OF[A2] was
    never set because arm() itself failed) must NOT be read as "distinct
    from PRE_NAME" just because "" != "work-66" is textually true. Forced
    deterministically: discover always returns empty, redraw is never
    called (an empty draw is not a collision to retry, it is a broken arm)."""
    body = f"""
{redraw_fn_src}
discover() {{ printf ''; }}
redraw() {{ echo REDRAW_CALLED_UNEXPECTEDLY >&2; exit 9; }}
"""
    proc = _run(body, "work-66", "redraw", "discover", "8")
    assert proc.returncode == 3, (proc.stdout, proc.stderr)
    name, count = _lines(proc.stdout)
    assert name == "", "the empty name is reported, not silently substituted"
    assert count == "0"
    assert "REDRAW_CALLED_UNEXPECTEDLY" not in proc.stderr, "an empty draw is not a collision to redraw"
    assert "discovered name is EMPTY" in proc.stderr


def test_empty_draw_after_a_redraw_is_also_a_hard_failure(redraw_fn_src: str) -> None:
    """The same emptiness check applies mid-loop: a collision redraw whose
    re-arm itself then fails (NAME_OF never re-set) must not be read as a
    resolved collision just because the empty string differs from PRE_NAME."""
    body = f"""
{redraw_fn_src}
STATE_FILE="$(mktemp)"; echo 0 > "$STATE_FILE"
discover() {{
    n="$(cat "$STATE_FILE")"
    if [ "$n" = 0 ]; then printf 'work-66'; else printf ''; fi
}}
redraw() {{ echo "$(($(cat "$STATE_FILE") + 1))" > "$STATE_FILE"; }}
"""
    proc = _run(body, "work-66", "redraw", "discover", "8")
    assert proc.returncode == 3, (proc.stdout, proc.stderr)
    name, count = _lines(proc.stdout)
    assert name == ""
    assert count == "1"
    assert "discovered name is EMPTY after redraw 1" in proc.stderr


def test_check_redraw_rate_fires_past_the_threshold(check_redraw_rate_src: str) -> None:
    """Round 2, finding 4: a redraw count above REDRAW_ANOMALY_THRESHOLD
    (default 2, so >=3 -- (1/256)^3 ~ 1-in-16.7M under the stated uniform
    draw) must fail loudly even though the site itself went on to succeed,
    so a real rise in the collision rate is not silently absorbed by an
    unbounded-looking sequence of redraws."""
    for count, expect_fire in [("0", False), ("2", False), ("3", True), ("8", True)]:
        body = f"""
REDRAW_ANOMALY_THRESHOLD=2
bad() {{ echo "BAD_CALLED: $*"; }}
{check_redraw_rate_src}
check_redraw_rate "some-site" "{count}"
"""
        proc = subprocess.run(["bash", "-c", body], capture_output=True, text=True, timeout=10)
        assert proc.returncode == 0, proc.stderr
        fired = "BAD_CALLED:" in proc.stdout
        assert fired == expect_fire, (count, proc.stdout, proc.stderr)
        if fired:
            assert "some-site" in proc.stdout and f"{count} consecutive name redraws" in proc.stdout


def test_call_site_wires_the_redraw_before_the_differs_assertion(script_text: str) -> None:
    """The step-2 call site must redraw BEFORE the `differs` check runs
    (not after -- a check-then-redraw ordering would still record the false
    result), and must report the cap-exhausted case distinctly rather than
    silently reusing the colliding name."""
    redraw_call = script_text.index('redraw_until_distinct "$A_NAME" redraw_a2')
    differs_check = script_text.index('check "the resumed session\'s name differs')
    assert redraw_call < differs_check, "the redraw must run before the differs assertion"
    cap_report = re.search(r'1\)\s*bad "step 2:.*redraws in a row all collided', script_text)
    assert cap_report, "cap exhaustion (exit 1) must be reported via bad(), not silently absorbed"


def test_call_site_treats_empty_discover_as_its_own_hard_failure(script_text: str) -> None:
    """Round 2, finding 1, at the call site: redraw_rc=3 must be reported
    distinctly (never silently proceed to a "differs" check that would read
    an empty A2_NAME as a false pass)."""
    empty_case = re.search(r'3\)\s*bad "step 2: arm A2 never produced a name', script_text)
    assert empty_case, "redraw_rc=3 (empty discover) must be reported via its own bad() branch"
    skip_differs = re.search(
        r'if \[ "\$redraw_rc" = 3 \]; then\s*\n\s*bad "the resumed session\'s name differs.*SKIPPED',
        script_text,
    )
    assert skip_differs, "on redraw_rc=3 the differs check must be skipped/failed, not run against an empty name"


def test_the_redraw_relaunches_for_real_not_a_retry_of_the_same_process(script_text: str) -> None:
    """A widened random space or a bare retry of the SAME already-launched
    process would not make the collision impossible -- only a genuine new
    process start does (RDR-208: the name changes 'at every process start').
    The step-2 redraw callback must kill and relaunch the pane, and must
    re-arm so NAME_OF actually reflects the new draw."""
    m = re.search(r"redraw_a2\(\) \{ (.*) \}", script_text)
    assert m, "redraw_a2 callback not found"
    body = m.group(1)
    assert "kill-session" in body, "must kill the existing pane before relaunching"
    assert "launch A2" in body, "must relaunch (a new process start), not just re-poll"
    assert "arm A2" in body, "must re-arm so NAME_OF[A2] reflects the fresh draw"


def test_the_b_site_redraws_against_a_the_same_way(script_text: str) -> None:
    """Round 2, finding 2: A and B are two independent launches with the
    same 1-in-256 collision risk as the resume site. B must be redrawn
    against A_NAME via the SAME redraw_until_distinct, with a real
    kill+relaunch+re-arm callback -- not a bespoke, unaudited copy."""
    assert 'redraw_until_distinct "$A_NAME" redraw_b discover_b_name' in script_text, (
        "the A/B launch site must call redraw_until_distinct against A_NAME"
    )
    m = re.search(r"redraw_b\(\) \{ (.*) \}", script_text)
    assert m, "redraw_b callback not found"
    body = m.group(1)
    assert "kill-session" in body
    assert "launch B" in body
    assert "arm B" in body
    # B_NAME must be assigned AFTER the redraw (from the function's own
    # output), never captured before it could have changed.
    b_name_assign = script_text.index('B_NAME="${_rd_out%%$\'\\n\'*}"')
    redraw_call = script_text.index('redraw_until_distinct "$A_NAME" redraw_b discover_b_name')
    assert redraw_call < b_name_assign


def test_step_3b_anchors_the_ttl_wait_to_the_redraw_sequence_end(script_text: str) -> None:
    """Round 2, finding 3: a collided intermediate A2 arm attempt really
    does subscribe to directory/$A_NAME before being killed and redrawn
    (arm() subscribes to whatever ListAgents returns, which on a collision
    IS A_NAME), and that stray lease is never explicitly released -- it
    lapses on its own 300s TTL, measured from when IT was written. Step 3b's
    wait must therefore be anchored to when the WHOLE redraw sequence ended
    (a safe upper bound on any such write), not to when it started."""
    anchor_set = script_text.index('A_NAME_LEASE_ANCHOR="$(now)"')
    redraw_call = script_text.index('redraw_until_distinct "$A_NAME" redraw_a2')
    step3b_wait = script_text.index("wait_s=$(( A_NAME_LEASE_ANCHOR + 300 + 20")
    assert redraw_call < anchor_set, "the anchor must be captured AFTER the redraw sequence runs"
    assert "wait_s=$(( RESUME_T + 300 + 20" not in script_text, (
        "step 3b must no longer anchor its wait to RESUME_T (the redraw sequence's START)"
    )
    assert step3b_wait > anchor_set


def test_redraw_counts_are_reported_in_the_final_summary(script_text: str) -> None:
    """Round 2, finding 4: the redraw count per site must be visible in the
    MVV's own output, not just inferable from grepping stderr diagnostics."""
    assert 'name redraws: resume=$A2_REDRAWS ab=$B_REDRAWS' in script_text
    assert 'check_redraw_rate "step 2 (resume)" "$A2_REDRAWS"' in script_text
    assert 'check_redraw_rate "launch (A/B)" "$B_REDRAWS"' in script_text
