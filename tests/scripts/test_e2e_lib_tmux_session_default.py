# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (c) 2026 Hal Hildebrand. All rights reserved.
"""tests/e2e/lib.sh's TMUX_SESSION must default to "e2e" without clobbering
a caller's own value (nexus-wauo1.12 follow-up).

THE DEFECT. `tests/e2e/lib.sh` line 4 set `TMUX_SESSION="e2e"`
unconditionally (no `${VAR:-default}` guard). Any script that computes its
own `TMUX_SESSION` value BEFORE sourcing lib.sh, expecting that value to
survive, had it silently overwritten the moment it sourced lib.sh.

Found while migrating `tests/e2e/release-sandbox.sh`'s `tmux` mode to
`claude_credentials.py run --` (RDR-219 P2.1c, nexus-wauo1.12): the script
computes `TMUX_SESSION="${TMUX_SESSION:-nexus-sandbox}"` and echoes it
BEFORE sourcing lib.sh (for the `_tmux` wrapper), but the actual
`new-session`/`send-keys`/`attach` calls all happen AFTER sourcing --
so the real tmux session was always named "e2e", never "nexus-sandbox",
confirmed present in the pre-image at commit 5df9837e1 (i.e. this bug
predates and is independent of the RDR-219 credential-transport change).
`tests/cc-validation/runner.sh` already worked around the identical
defect by re-assigning `TMUX_SESSION="cc-val"` a second time, AFTER its
own `source tests/e2e/lib.sh` call (see its line 47 comment: "override
the e2e default after sourcing").

THE FIX. `TMUX_SESSION="${TMUX_SESSION:-e2e}"` -- a caller's own value,
if already set before sourcing, survives; an unset caller still gets the
"e2e" default tests/e2e/run.sh and the rest of the e2e harness rely on.

Both existing callers must behave identically before and after this fix:
- `tests/e2e/run.sh` never sets TMUX_SESSION before sourcing -> still
  lands on the "e2e" default either way.
- `tests/cc-validation/runner.sh` sets TMUX_SESSION="cc-val" BEFORE
  sourcing (line 23) -> with the fix that value now survives sourcing on
  its own; its own post-source re-assignment (line 47) becomes a
  redundant no-op (same value), not a behavior change.
"""
from __future__ import annotations

import subprocess
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[2]
LIB = REPO_ROOT / "tests" / "e2e" / "lib.sh"
RUN_SH = REPO_ROOT / "tests" / "e2e" / "run.sh"
RUNNER_SH = REPO_ROOT / "tests" / "cc-validation" / "runner.sh"


def _sourced_value(pre_source_export: str | None) -> str:
    """Runs a tiny bash script that (optionally) sets TMUX_SESSION, sources
    lib.sh, and echoes the resulting value -- a real subprocess/source test,
    not a text-grep proxy, so it exercises the actual bash semantics of
    `${VAR:-default}` vs. a bare assignment."""
    script_lines = ["#!/usr/bin/env bash", "set -euo pipefail"]
    if pre_source_export is not None:
        script_lines.append(f'TMUX_SESSION="{pre_source_export}"')
    script_lines.append(f'source "{LIB}"')
    script_lines.append('printf "%s" "$TMUX_SESSION"')
    res = subprocess.run(
        ["bash", "-c", "\n".join(script_lines)],
        capture_output=True, text=True, timeout=10,
    )
    assert res.returncode == 0, res.stderr
    return res.stdout


def test_default_is_e2e_when_caller_never_sets_it() -> None:
    assert _sourced_value(None) == "e2e"


def test_a_callers_pre_source_value_survives_sourcing() -> None:
    """This is the failing case before the fix: a caller sets its own
    TMUX_SESSION before `source tests/e2e/lib.sh`, and that value must
    still be in effect afterward."""
    assert _sourced_value("nexus-sandbox") == "nexus-sandbox"


def test_a_different_pre_source_value_also_survives() -> None:
    assert _sourced_value("cc-val") == "cc-val"


def test_lib_sh_uses_a_default_guard_not_a_bare_assignment() -> None:
    text = LIB.read_text()
    line = next(ln for ln in text.splitlines() if ln.strip().startswith("TMUX_SESSION="))
    assert line.strip() == 'TMUX_SESSION="${TMUX_SESSION:-e2e}"', line


def test_run_sh_never_pre_sets_tmux_session() -> None:
    """run.sh relies entirely on lib.sh's default -- if it ever starts
    setting its own value, this test's premise (and the "behaves the
    same" claim above) needs re-checking."""
    text = RUN_SH.read_text()
    assert not any(
        ln.strip().startswith("TMUX_SESSION=") or ln.strip().startswith("export TMUX_SESSION=")
        for ln in text.splitlines()
    ), "run.sh now sets TMUX_SESSION itself -- re-verify it still gets the right value"


def test_runner_sh_sets_tmux_session_before_sourcing_lib() -> None:
    """runner.sh's pre-fix workaround (re-assigning AFTER sourcing) is
    still present and harmless after this fix, but what actually matters
    going forward is that its FIRST assignment (line ~23, before
    `source tests/e2e/lib.sh`) is the one this fix makes authoritative."""
    text = RUNNER_SH.read_text()
    source_idx = text.index('source "$REPO_ROOT/tests/e2e/lib.sh"')
    before_source = text[:source_idx]
    assert 'TMUX_SESSION="cc-val"' in before_source, (
        "runner.sh no longer sets TMUX_SESSION before sourcing lib.sh -- "
        "the fixed lib.sh default guard only helps a caller whose own "
        "value is set BEFORE the `source` call"
    )


@pytest.mark.parametrize("script", [RUN_SH, RUNNER_SH])
def test_no_caller_regresses_after_the_fix(script: Path) -> None:
    """End-to-end: run.sh (unset) and runner.sh (pre-set "cc-val") each
    get the exact same effective TMUX_SESSION with the fixed lib.sh as
    they did before it."""
    pre_source = None
    if script is RUNNER_SH:
        pre_source = "cc-val"
    expected = "e2e" if pre_source is None else pre_source
    assert _sourced_value(pre_source) == expected
