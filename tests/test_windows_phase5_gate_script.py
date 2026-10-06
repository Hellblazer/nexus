# SPDX-License-Identifier: AGPL-3.0-or-later
"""Shape pins for the RDR-224 Phase 5 gate (nexus-ijue9.16).

The gate is hand-run inside a Windows guest, so CI cannot execute it. These
pins hold the clauses that decide the verdict and the spec items a reviewer
would otherwise re-derive by reading: published versions are required input,
the assertion count is declared and enforced, a skip or a failure can only
fail, both init paths run, the re-check sleeps after each stop exist, and
claude -p gets the harness token Claude Code removed from its own
environment. Each pin was checked to fail with its clause removed. The red
runs on the guest are recorded in T2 nexus_rdr/224-gate-red-runs-2026-10-06.
"""

from __future__ import annotations

import re
from pathlib import Path

GATE: Path = Path(__file__).resolve().parent / "e2e" / "windows-phase5-gate.ps1"
LAUNCHER: Path = Path(__file__).resolve().parent / "e2e" / "windows-guest" / "host-recv.ps1"


def _text() -> str:
    return GATE.read_text(encoding="utf-8")


def _verdict_block() -> str:
    text = _text()
    return text[text.index("# -------------------------------------------------------------- verdict ----"):]


def test_versions_are_mandatory_with_no_default() -> None:
    text = _text()
    for name in ("ExpectedVersion", "ExpectedEngine"):
        m = re.search(
            r"\[Parameter\(Mandatory\s*=\s*\$true\)\]\s*\[string\]\s*\$" + name + r"\s*[,)]",
            text,
        )
        assert m, f"${name} must be a mandatory string parameter with no default"


def test_verdict_fails_on_count_failure_or_precondition() -> None:
    block = _verdict_block()
    assert re.search(r"\$DECLARED\s*=\s*5\b", _text())
    assert (
        "if ($executed -ne $DECLARED -or $failed.Count -gt 0 -or -not $preOk) {" in block
    ), "the verdict must fail on a short count, any failed assertion, or a failed precondition"
    assert "$preOk = (K (K $results 'P0') 'status') -eq 'PASS' -and (K (K $results 'V0') 'status') -eq 'PASS'" in block


def test_failed_verdict_exits_1_and_passed_is_last() -> None:
    block = _verdict_block()
    failed_at = block.index('"WINDOWS PHASE 5 GATE FAILED (')
    passed_at = block.index('"WINDOWS PHASE 5 GATE PASSED (')
    between = block[failed_at:passed_at]
    assert re.search(r"Write-Output \$line\s+exit 1\s+\}", between), "a FAILED verdict must exit 1"
    assert block.rstrip().endswith("exit 0")


def test_a_skip_is_recorded_as_not_executed() -> None:
    text = _text()
    assert "[string]$ForceSkip" in text
    assert "$skipIds -contains $id" in text
    assert "'SKIPPED'" in text
    # Only an executed (PASS or FAIL) assertion counts toward $executed.
    assert re.search(r"\(K \$_ 'status'\)\s+-in\s+@\('PASS',\s*'FAIL'\)", _verdict_block())


def test_both_init_paths_and_rechecks_after_every_stop() -> None:
    text = _text()
    assert "@('init', '--service', '--yes')" in text
    assert re.search(r"''\s*\|\s*&\s*\$nx\s+init\s+--service\s*\}", text), (
        "leg B must run init with stdin redirected (no TTY) and no --yes"
    )
    m = re.search(r"\$RECHECK_SECONDS\s*=\s*(\d+)", text)
    assert m and int(m.group(1)) >= 35
    # leg B stop, assertion 4's --with-pg stop, assertion 4's plain stop
    assert text.count("Start-Sleep -Seconds $RECHECK_SECONDS") >= 3
    assert text.count("@('daemon', 'service', 'stop', '--with-pg')") == 2


def test_assertion_4_runs_against_the_launcher_stack() -> None:
    text = _text()
    a4 = text[text.index("Run-Assertion 4 "):text.index("Run-Assertion 5 ")]
    assert a4.index("Supervisor-From-Launcher $k") < a4.index("'stop', '--with-pg'")
    assert "Invoke-ClaudeP 'no-endpoint'" in a4


def test_claude_p_gets_the_harness_token_and_no_session_markers() -> None:
    text = _text()
    fn = text[text.index("function Invoke-ClaudeP"):text.index("$script:claudeUp = $null")]
    assert "GetEnvironmentVariable('NX_HARNESS_CLAUDE_OAUTH_TOKEN', 'Process')" in fn
    assert "SetEnvironmentVariable('CLAUDE_CODE_OAUTH_TOKEN', $harness, 'Process')" in fn
    assert "SetEnvironmentVariable('CLAUDECODE', $null, 'Process')" in fn
    assert "$null = $p.Handle" in fn
    launcher = LAUNCHER.read_text(encoding="utf-8")
    assert "$psi.EnvironmentVariables['NX_HARNESS_CLAUDE_OAUTH_TOKEN'] = $t" in launcher
    assert "SetEnvironmentVariable('CLAUDE_CODE_OAUTH_TOKEN'" not in launcher, (
        "the launcher window must not hold the token in its own environment"
    )


def test_verify_checks_versions_and_the_live_session() -> None:
    text = _text()
    v0 = text[text.index("# V0:"):text.index("Run-Assertion 2 ")]
    assert "installed_plugins.json" in v0
    assert "service_release_version" in v0
    assert "(K $_ 'entrypoint') -eq 'cli'" in v0


def test_refuses_off_windows_before_touching_paths() -> None:
    text = _text()
    refuse = text.index("$env:OS -ne 'Windows_NT'")
    assert refuse < text.index("Join-Path $env:USERPROFILE")
