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

This module forces the exact colliding draw deterministically via stub
REDRAW_FN/DISCOVER_FN callables (no tmux, no Claude Code, no billed run) and
executes ``redraw_until_distinct`` EXTRACTED VERBATIM from the real script
via ``bash -c``, so a future edit to the actual function under test, not a
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
def redraw_fn_src() -> str:
    return _extract_function(_SCRIPT.read_text(encoding="utf-8"), "redraw_until_distinct")


def _run(script_body: str, *args: str) -> subprocess.CompletedProcess[str]:
    """Run redraw_until_distinct PRE_NAME redraw_fn discover_fn CAP in a
    fresh bash, with the caller's own redraw_fn/discover_fn/counter
    definitions prepended as *script_body*."""
    full = f"set -uo pipefail\n{script_body}\nredraw_until_distinct {' '.join(args)}\n"
    return subprocess.run(["bash", "-c", full], capture_output=True, text=True, timeout=30)


def test_function_is_defined_and_parses(redraw_fn_src: str) -> None:
    proc = subprocess.run(
        ["bash", "-c", f"{redraw_fn_src}\ntype redraw_until_distinct"],
        capture_output=True, text=True,
    )
    assert proc.returncode == 0, proc.stderr


def test_no_collision_returns_immediately_with_zero_redraws(redraw_fn_src: str) -> None:
    """The common case: the resumed session draws a distinct name on the
    first arm. No redraw callback is ever invoked."""
    body = f"""
{redraw_fn_src}
discover() {{ printf 'work-b9'; }}
redraw() {{ echo REDRAW_CALLED_UNEXPECTEDLY >&2; exit 9; }}
"""
    proc = _run(body, "work-66", "redraw", "discover", "8")
    assert proc.returncode == 0, proc.stderr
    assert proc.stdout.strip() == "work-b9"
    assert "REDRAW_CALLED_UNEXPECTEDLY" not in proc.stderr
    assert proc.stderr == "", "no redraw diagnostics expected when nothing collided"


def test_forced_collision_then_a_distinct_draw_recovers(redraw_fn_src: str) -> None:
    """The exact nexus-4ahul scenario, forced deterministically: the first
    draw collides with the pre-resume name (work-66 == work-66, precisely
    what killed the billed run on 2026-09-19); the harness must redraw for
    real (the stub counts real invocations of its redraw callback) and land
    on the distinct second draw, never asserting the false "differs" check
    against the colliding first draw."""
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
    assert proc.stdout.strip() == "work-c0", "must land on the SECOND, distinct draw"
    assert "collided with work-66 (draw 1/8): redrawing" in proc.stderr
    assert "distinct name after 1 redraw(s): work-c0" in proc.stderr


def test_persistent_collision_exhausts_the_cap_and_reports_it_not_pass_or_fail(redraw_fn_src: str) -> None:
    """A collision on every single draw (persistent, not one-in-256) is a
    real regression -- e.g. Claude Code's naming stopped being random -- and
    must never be silently absorbed as a PASS (the differs-check would be
    false) or misreported as an ordinary FAIL indistinguishable from the
    thing the redraw exists to rule out. The cap trips its own exit code."""
    body = f"""
{redraw_fn_src}
discover() {{ printf 'work-66'; }}
CALLS_FILE="$(mktemp)"; echo 0 > "$CALLS_FILE"
redraw() {{ echo "$(($(cat "$CALLS_FILE") + 1))" > "$CALLS_FILE"; }}
"""
    proc = _run(body, "work-66", "redraw", "discover", "3")
    assert proc.returncode == 1, (proc.stdout, proc.stderr)
    assert proc.stdout.strip() == "work-66"
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


def test_call_site_wires_the_redraw_before_the_differs_assertion() -> None:
    """The step-2 call site must redraw BEFORE the `differs` check runs
    (not after -- a check-then-redraw ordering would still record the false
    result), and must report the cap-exhausted case distinctly rather than
    silently reusing the colliding name."""
    text = _SCRIPT.read_text(encoding="utf-8")
    redraw_call = text.index("redraw_until_distinct \"$A_NAME\"")
    differs_check = text.index('check "the resumed session\'s name differs')
    assert redraw_call < differs_check, "the redraw must run before the differs assertion"
    cap_report = re.search(r'redraw_rc.*=.*1.*\n\s*bad "step 2:.*redraws in a row all collided', text)
    assert cap_report, "cap exhaustion (exit 1) must be reported via bad(), not silently absorbed"


def test_the_redraw_relaunches_for_real_not_a_retry_of_the_same_process() -> None:
    """A widened random space or a bare retry of the SAME already-launched
    process would not make the collision impossible -- only a genuine new
    process start does (RDR-208: the name changes 'at every process start').
    The step-2 redraw callback must kill and relaunch the pane, and must
    re-arm so NAME_OF actually reflects the new draw."""
    text = _SCRIPT.read_text(encoding="utf-8")
    m = re.search(r"redraw_a2\(\) \{ (.*) \}", text)
    assert m, "redraw_a2 callback not found"
    body = m.group(1)
    assert "kill-session" in body, "must kill the existing pane before relaunching"
    assert "launch A2" in body, "must relaunch (a new process start), not just re-poll"
    assert "arm A2" in body, "must re-arm so NAME_OF[A2] reflects the fresh draw"
