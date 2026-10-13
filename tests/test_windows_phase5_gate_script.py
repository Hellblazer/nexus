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

import json
import re
from pathlib import Path

import pytest

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
    assert "Try-ClaudeP 'no-endpoint'" in a4
    # the plain-stop leg runs on a stack the logon task started, not a direct spawn
    restart = a4.index("Start-ScheduledTask -TaskName 'NexusStorageService'")
    assert restart < a4.index("@('daemon', 'service', 'stop')")
    assert "@('daemon', 'service', 'start')" not in a4
    assert a4.index("Wait-Gone @('supervisor', 'engine', 'postgres') 90") < a4.index("Try-ClaudeP 'no-endpoint'")
    assert "'was interrupted|not properly shut down|redo starts|automatic recovery'" in a4


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
    assert "(K $_ 'entrypoint') -eq 'cli'" in text
    # the launching call reaches the transcript after the tool starts: poll for it
    assert "$runs = @(Find-VerifyCalls $tx)" in v0 and "Start-Sleep -Seconds 3" in v0
    assert "$claudePid = Claude-Ancestor" in v0


def test_refuses_off_windows_before_touching_paths() -> None:
    text = _text()
    refuse = text.index("$env:OS -ne 'Windows_NT'")
    assert refuse < text.index("Join-Path $env:USERPROFILE")


def test_assertion_bodies_keep_their_deciding_clauses() -> None:
    text = _text()
    a2 = text[text.index("Run-Assertion 2 "):text.index("Run-Assertion 3 ")]
    assert "'mcp__plugin_conexus_nexus__store_put'" in a2 and '-like "*$nonce*"' in a2
    assert "'mcp__plugin_conexus_nexus__search'" in a2 and "-not $v.results[(K $_ 'id')].is_error" in a2
    assert "nx search outside the session does not find the nonce" in a2
    a3 = text[text.index("Run-Assertion 3 "):text.index("function Invoke-ClaudeP")]
    assert """"$(K $_ 'type')" -ne 'hook_success' -or [int](K $_ 'exitCode') -ne 0""" in a3
    assert "'nx-hook session-start'" in a3
    a5 = text[text.index("Run-Assertion 5 "):text.index("# -------------------------------------------------------------- verdict ----")]
    assert "a credential file appeared during the run" in a5
    setup = text[text.index("Run-Assertion 1 "):text.index("# --------------------------------------------------------------- verify ----")]
    assert "setup cannot be re-run on this box" in setup and "exit 1" in setup


def test_k_returns_arrays_unrolled_by_nothing() -> None:
    # A one-element array returned bare from a PowerShell function arrives as
    # its element; the gate missed every single-tool-call message that way on
    # the first real run (2026-10-06).
    assert "return , $d[$k]" in _text()


def test_no_k_result_is_wrapped_in_an_array_subexpression() -> None:
    # K returns arrays intact, so @(K ...) nests them; the plugin check read ''
    # that way on the 2026-10-06 run.
    import re as _re
    assert not _re.search(r"@\(\s*K\s", _text())


LAUNCH: Path = Path(__file__).resolve().parent / "e2e" / "windows-guest" / "launch.sh"


def test_v0_requires_claude_ancestry_and_polls_for_the_launching_call() -> None:
    text = _text()
    v0 = text[text.index("# V0:"):text.index("Run-Assertion 2 ")]
    assert "if (-not $claudePid) { Fail" in v0
    assert "$deadline = (Get-Date).AddSeconds(60)" in v0
    fn = text[text.index("function Claude-Ancestor"):text.index("# V0:")]
    assert "if ($cur.Name -eq 'claude.exe') { return [int]$cur.ProcessId }" in fn
    assert "return $null" in fn


def test_plugin_entries_take_the_first_install_record() -> None:
    assert "$pv = K $(if ($entries -is [array]) { $entries[0] } else { $entries }) 'version'" in _text()


def test_launcher_consumes_its_one_shot_args_and_preaccepts_trust() -> None:
    launcher = LAUNCHER.read_text(encoding="utf-8")
    assert "Remove-Item $argsFile -Force" in launcher, "a later plain launch must not repeat the args"
    assert "FromBase64String" in launcher, "one base64 argument per line keeps a multi-line prompt whole"
    assert "hasTrustDialogAccepted" in launcher and "hasCompletedOnboarding" in launcher
    assert "[void]$sb.Append('\\', 2 * $bs)" in launcher, "trailing backslashes are doubled before the closing quote"


def test_launch_sh_cleans_up_without_failing_and_empties_the_stage_dir() -> None:
    sh = LAUNCH.read_text(encoding="utf-8")
    assert '[[ -z "${argsdir:-}" ]] || rm -rf "$argsdir"' in sh
    assert '"(if not exist C:\\build\\guest\\nxgate\\stage mkdir C:\\build\\guest\\nxgate\\stage) & del /q' in sh
    assert '${claude_args[@]+"${claude_args[@]}"}' in sh, "bash 3.2 under set -u rejects an empty array expansion"


def test_aspect_worker_survivors_are_observed_with_the_rdr_citation_not_asserted() -> None:
    """nexus-g5rz5: RDR-224 records that `service stop` leaves the aspect-worker
    daemon running (it belongs to the store path), so assertion 4 reports it
    with the citation and CPU seconds and does not fail on it. Asserting it gone
    would contradict the recorded decision; this pins the observation shape."""
    text = _text()
    a4 = text[text.index("Run-Assertion 4 "):text.index("Run-Assertion 5 ")]
    assert "by design per RDR-224" in a4
    assert "aspect-worker processes left" in a4 and "cpu=" in a4
    # observation only: no Fail and no Assert-Absent over the aspect kind
    assert not re.search(r"Assert-Absent\s+@\([^)]*'aspect'", text)
    assert not re.search(r"Wait-Gone\s+@\([^)]*'aspect'", text)
    assert not re.search(r"\$k\.aspect\.Count[^\n]*\bFail\b|\bFail\b[^\n]*\$k\.aspect", a4)


# ---------------------------------------------------------------------------
# Review round (nexus-f9bgu.40, nexus-ijue9.16): hook coupling, stop quality,
# presence of the clauses a mutant used to delete silently.
# ---------------------------------------------------------------------------

REPO: Path = Path(__file__).resolve().parent.parent
HOOKS_JSON: Path = REPO / "conexus" / "hooks" / "hooks.json"
SRC_NEXUS: Path = REPO / "src" / "nexus"


def _ps_literal(text: str, name: str) -> str:
    """The body of a top-level `$NAME = '...'` single-quoted PowerShell literal.

    PowerShell single quotes have no escapes but the doubled quote. The gate
    keeps its hook and stop regexes in such literals so this file can read the
    SAME text the guest runs. The patterns are written in the subset .NET and
    Python share (alternation, anchors, character classes, an escaped dot);
    PowerShell's -match is case-insensitive, so Python compiles with re.I.
    """
    m = re.search(r"^\$" + name + r"\s*=\s*'((?:[^']|'')*)'\s*$", text, re.M)
    assert m, f"the gate must define ${name} as a single-quoted literal on one line"
    return m.group(1).replace("''", "'")


def _hook_commands(hooks_text: str) -> list[str]:
    """Each hook entry as Claude Code records it in a hook attachment's
    `command`: the command and its args joined by spaces, ${CLAUDE_PLUGIN_ROOT}
    left unexpanded; an mcp_tool hook is `<server>/<tool>`."""
    out: list[str] = []
    for groups in json.loads(hooks_text)["hooks"].values():
        for group in groups:
            for hook in group["hooks"]:
                if hook["type"] == "command":
                    out.append(" ".join([hook["command"], *hook.get("args", [])]))
                elif hook["type"] == "mcp_tool":
                    out.append(f'{hook["server"]}/{hook["tool"]}')
                else:
                    raise AssertionError(f"unknown hook type {hook['type']!r}: teach the gate pin about it")
    return out


def _check_hook_coverage(hooks_text: str, gate_text: str) -> None:
    ours_src = _ps_literal(gate_text, "HOOK_OURS_RE")
    shim_src = _ps_literal(gate_text, "HOOK_SHIM_RE")
    # The filters name what a hook launches, never how the launcher is spelled.
    for label, src in (("HOOK_OURS_RE", ours_src), ("HOOK_SHIM_RE", shim_src)):
        assert not re.search(r"\buv\b", src), f"${label} must not depend on the uv launcher spelling: {src!r}"
    ours, shim = re.compile(ours_src, re.I), re.compile(shim_src, re.I)
    cmds = _hook_commands(hooks_text)
    assert cmds, "hooks.json lists no hook entries"
    unmatched = [c for c in cmds if not ours.search(c)]
    assert not unmatched, f"conexus hook entries the gate's HOOK_OURS_RE does not cover: {unmatched}"
    shim_cmds = [c for c in cmds if "nx_hook_shim.py" in c]
    assert shim_cmds, "hooks.json has no nx_hook_shim.py entry: assertion 3 would have nothing to see"
    assert all(shim.search(c) for c in shim_cmds), f"HOOK_SHIM_RE misses shim entries: {shim_cmds}"
    plain = [c for c in cmds if "hooks/scripts/" in c and "nx_hook_shim.py" not in c]
    assert plain, "hooks.json has no plain-script entry"
    assert all(ours.search(c) for c in plain)


def test_every_conexus_hook_entry_is_covered_by_the_gates_filters() -> None:
    _check_hook_coverage(HOOKS_JSON.read_text(encoding="utf-8"), _text())


def _rewrite_launcher(hooks_text: str, launcher: list[str]) -> str:
    data = json.loads(hooks_text)
    for groups in data["hooks"].values():
        for group in groups:
            for hook in group["hooks"]:
                if hook["type"] == "command" and hook["command"] == "uv":
                    script_and_rest = hook["args"][hook["args"].index("python") + 1:]
                    hook["args"] = [*launcher, *script_and_rest]
    return json.dumps(data)


def test_hook_pin_accepts_either_launcher_spelling() -> None:
    """Positive control: the old `uv run` form and the current `uv tool run` form
    both pass, because the filters match what is launched."""
    base = HOOKS_JSON.read_text(encoding="utf-8")
    old = _rewrite_launcher(base, ["run", "--no-project", "--no-config", "--quiet"])
    assert old != base
    _check_hook_coverage(old, _text())
    _check_hook_coverage(base, _text())


def test_hook_pin_fails_when_the_gate_filters_are_reverted_to_the_old_launcher_form() -> None:
    """Mutant: the pre-7.74.0 filters (nexus-f9bgu.40 C1) against today's hooks.json."""
    text = _text()
    reverted = re.sub(
        r"^\$HOOK_OURS_RE\s*=.*$",
        lambda _m: "$HOOK_OURS_RE = '^nx-hook |^uv run .*CLAUDE_PLUGIN_ROOT|nx_hook_shim\\.py|nx-session-end-launcher'",
        text,
        flags=re.M,
    )
    assert reverted != text
    with pytest.raises(AssertionError):
        _check_hook_coverage(HOOKS_JSON.read_text(encoding="utf-8"), reverted)
    reverted_shim = re.sub(
        r"^\$HOOK_SHIM_RE\s*=.*$", lambda _m: "$HOOK_SHIM_RE = '^uv run .*nx_hook_shim\\.py'", text, flags=re.M
    )
    assert reverted_shim != text
    with pytest.raises(AssertionError):
        _check_hook_coverage(HOOKS_JSON.read_text(encoding="utf-8"), reverted_shim)


def test_hook_pin_fails_when_a_hook_script_moves_out_from_under_hooks_scripts() -> None:
    """Mutant on a scratch copy of hooks.json: a launched script the gate no
    longer recognises must fail the pin, not shrink A3's coverage silently."""
    base = HOOKS_JSON.read_text(encoding="utf-8")
    moved = base.replace("/hooks/scripts/nx_hook_shim.py", "/hooks/nx_hook_shim.py")
    assert moved != base
    with pytest.raises(AssertionError):
        _check_hook_coverage(moved, _text())
    renamed = base.replace('"command": "nx-session-end-launcher"', '"command": "nx-session-end"')
    assert renamed != base
    with pytest.raises(AssertionError):
        _check_hook_coverage(renamed, _text())


def test_assertion_3_uses_the_named_filters_and_does_not_name_the_launcher() -> None:
    text = _text()
    a3 = text[text.index("Run-Assertion 3 "):text.index("function Invoke-ClaudeP")]
    assert "-match $HOOK_OURS_RE" in a3 and "-match $HOOK_SHIM_RE" in a3
    assert "uv run" not in a3 and "uv tool run" not in a3, "A3 must not name the launcher"


# ---- stop quality (S2) ------------------------------------------------------

_DEGRADED_STRINGS = {"unclean_stop", "hard-killing", "win_console_helper_failed", "bounded_subprocess_timeout"}


def test_stop_degraded_strings_are_what_the_code_logs() -> None:
    lit = _ps_literal(_text(), "STOP_DEGRADED_RE")
    assert set(lit.split("|")) == _DEGRADED_STRINGS
    corpus = "\n".join(p.read_text(encoding="utf-8", errors="replace") for p in SRC_NEXUS.rglob("*.py"))
    for s in _DEGRADED_STRINGS:
        assert s in corpus, f"src/nexus no longer contains {s!r}: the gate's degraded-stop filter is stale"


def test_stop_is_timed_bounded_and_fails_on_the_degraded_path() -> None:
    text = _text()
    m = re.search(r"^\$STOP_MAX_SECONDS\s*=\s*(\d+)\s*$", text, re.M)
    assert m and int(m.group(1)) <= 15, "measured graceful stops are 2.2 to 3.2 s; the broken path was 41 s"
    invoke = text[text.index("function Invoke-Nx"):text.index("function Stop-Problem")]
    assert "Stopwatch" in invoke and "secs =" in invoke
    fn = text[text.index("function Stop-Problem"):text.index("function Get-Status")]
    assert "-match $STOP_DEGRADED_RE" in fn and "-gt $STOP_MAX_SECONDS" in fn
    a1 = text[text.index("Run-Assertion 1 "):text.index("# --------------------------------------------------------------- verify ----")]
    a4 = text[text.index("Run-Assertion 4 "):text.index("Run-Assertion 5 ")]
    # leg B's stop (assertion 1) and both of assertion 4's stops
    _live_statement(a1, "Stop-Problem $stop 'leg B stop --with-pg'")
    _live_statement(a1, "if ($legBStop) { Fail")
    _live_statement(a4, "Stop-Problem $r 'stop --with-pg'")
    _live_statement(a4, "Stop-Problem $r 'plain stop'")
    _live_statement(a4, "if ($stopProblems.Count -gt 0) { Fail")
    # the seconds reach the assertion detail
    assert "$($stop.secs)s" in a1 and "$($r.secs)s" in a4


# ---- clauses a mutant used to delete without any pin noticing (S5) ----------

_DEAD = re.compile(r"\$false|\bif\s*\(\s*0\s*\)|\bwhile\s*\(\s*0\s*\)", re.I)


def _live_statement(block: str, needle: str) -> str:
    """The line of `block` holding `needle`, asserted present, not a comment and
    not switched off by `$false` on it or on the line above (`if ($false) {`)."""
    lines = block.splitlines()
    hits = [i for i, ln in enumerate(lines) if needle in ln]
    assert hits, f"missing statement: {needle!r}"
    for i in hits:
        line = lines[i]
        if line.strip().startswith("#") or _DEAD.search(line):
            continue
        prev = next((lines[j] for j in range(i - 1, -1, -1) if lines[j].strip()), "")
        if _DEAD.search(prev):
            continue
        return line
    raise AssertionError(f"statement is commented out or switched off: {needle!r}")


def test_the_gate_uses_no_block_comments() -> None:
    # a `<# ... #>` block would hide a deleted-by-comment clause from _live_statement
    assert "<#" not in _text()


def test_assertion_4_keeps_its_post_stop_and_pg_log_checks_live() -> None:
    text = _text()
    a4 = text[text.index("Run-Assertion 4 "):text.index("Run-Assertion 5 ")]
    _live_statement(a4, """Assert-Absent @('supervisor', 'engine', 'postgres') "${RECHECK_SECONDS}s after stop --with-pg""")
    _live_statement(a4, """Assert-Absent @('supervisor', 'engine') "${RECHECK_SECONDS}s after plain stop\"""")
    _live_statement(
        a4,
        """if ($new -match 'was interrupted|not properly shut down|redo starts|automatic recovery') { Fail "next start's pg.log shows crash recovery" }""",
    )
    _live_statement(a4, """if ($new -notmatch 'database system was shut down at') { Fail""")


def test_assertion_2_keeps_its_outside_session_check_live() -> None:
    text = _text()
    a2 = text[text.index("Run-Assertion 2 "):text.index("Run-Assertion 3 ")]
    _live_statement(a2, """if ($cli.out -notlike "*$nonce*") { Fail "nx search outside the session does not find the nonce""")
    _live_statement(a2, "$cli = Invoke-Nx 'nx-search-nonce'")


def test_assertion_3_keeps_its_failed_hook_check_live() -> None:
    text = _text()
    a3 = text[text.index("Run-Assertion 3 "):text.index("function Invoke-ClaudeP")]
    _live_statement(a3, """$bad = @($ours | Where-Object { "$(K $_ 'type')" -ne 'hook_success'""")
    _live_statement(a3, "if ($bad.Count -gt 0) { Fail")
    _live_statement(a3, "if ($start.Count -eq 0) { Fail")
    _live_statement(a3, "if ($shim.Count -eq 0) { Fail")


# ---- verdict record (S4, Observation 3) and the printed instruction (Obs 4) -


def test_verdict_records_the_script_hash_before_the_verdict_line() -> None:
    text = _text()
    assert "Get-FileHash -Algorithm SHA256" in text
    assert "script_sha256" in text, "setup stores the staged script's hash so verify can compare"
    block = _verdict_block()
    sha_at = block.index("gate script sha256")
    assert sha_at < block.index('"WINDOWS PHASE 5 GATE FAILED (') and sha_at < block.index('"WINDOWS PHASE 5 GATE PASSED (')
    _live_statement(block, "Add-Content -Path (Join-Path $RunDir 'verdict.txt') -Value $shaLine")
    assert block.rstrip().endswith("exit 0"), "the verdict stays the last line"


def test_session_id_and_endpoint_are_recorded_in_full() -> None:
    text = _text()
    v0 = text[text.index("# V0:"):text.index("Run-Assertion 2 ")]
    assert "live session $($script:sid)" in v0, "the full session id, not a prefix"
    assert "$($s['host']):$($s['port'])" in v0 and "$script:endpoint" in v0
    a2 = text[text.index("Run-Assertion 2 "):text.index("Run-Assertion 3 ")]
    assert "$($script:sid)" in a2 and "$script:endpoint" in a2


def test_verify_instruction_says_background_in_the_header_and_in_the_printed_text() -> None:
    text = _text()
    printed = text[text.index("SETUP DONE."):text.index("exit 0", text.index("SETUP DONE."))]
    assert "in the background" in printed and "600000" not in printed
    header = text[:text.index("param(")]
    assert "in the background" in header and "600000" not in header


def test_header_not_covered_list_names_the_known_residuals() -> None:
    text = _text()
    header = text[:text.index("param(")]
    block = header[header.index("WHAT A GREEN RUN DOES NOT COVER"):header.index("# Exit:")]
    for needle in (
        "install-binary", "retail", "Credential Manager", "sleep", "Git, uv", "Windows on ARM",
        "Smart App Control", "SessionEnd",
    ):
        assert needle in block, f"the not-covered list must name {needle!r}"
    items = re.split(r"\n#   - ", block)[1:]
    assert len(items) >= 10
    for item in items:
        assert re.search(r"nexus-[a-z0-9.]+", item), f"no bead named: {item}"


# ---- measured stop seconds survive a FAIL (nexus-f9bgu.53) ------------------


def _check_stop_notes(gate_text: str) -> None:
    """Every stop an assertion runs is noted, and Run-Assertion carries the notes
    into the record on a PASS and on a FAIL.

    A FAIL's detail is the exception message, which replaced the body: 7.77.1's
    failed assertion 4 lost both stops' measured seconds. The notes sit outside
    the body so neither outcome can drop them.
    """
    fn = gate_text[gate_text.index("function Run-Assertion"):gate_text.index("function Invoke-Nx")]
    _live_statement(fn, "$script:stopNotes.Clear()")
    _live_statement(fn, "Record $key 'PASS' ($detail + (Stop-Notes))")
    _live_statement(fn, "Record $key 'FAIL' ($_.Exception.Message + (Stop-Notes))")
    a1 = gate_text[gate_text.index("Run-Assertion 1 "):gate_text.index("# --------------------------------------------------------------- verify ----")]
    a4 = gate_text[gate_text.index("Run-Assertion 4 "):gate_text.index("Run-Assertion 5 ")]
    # each Note-Stop follows the Invoke-Nx that ran its stop, before anything can throw
    assert a1.index("Note-Stop $stop 'A1 leg B --with-pg'") > a1.index("Invoke-Nx 'legB-stop-with-pg'")
    assert a1.index("Note-Stop $stop 'A1 leg B --with-pg'") < a1.index("Stop-Problem $stop")
    _live_statement(a1, "Note-Stop $stop 'A1 leg B --with-pg'")
    _live_statement(a4, "Note-Stop $r 'A4 --with-pg'")
    _live_statement(a4, "Note-Stop $r 'A4 plain'")
    assert a4.index("Note-Stop $r 'A4 --with-pg'") > a4.index("Invoke-Nx 'stop-with-pg'")
    assert a4.index("Note-Stop $r 'A4 --with-pg'") < a4.index("Wait-Gone")
    assert a4.index("Note-Stop $r 'A4 plain'") > a4.index("Invoke-Nx 'stop-plain'")
    assert a4.index("Note-Stop $r 'A4 plain'") < a4.index("Assert-Absent @('supervisor', 'engine') \"${RECHECK_SECONDS}s after plain stop\"")
    # every Invoke-Nx stop in the gate is noted: three stops, three notes
    assert gate_text.count("'daemon', 'service', 'stop'") == 3
    assert len(re.findall(r"^\s*Note-Stop \$", gate_text, re.M)) == 3
    notes = gate_text[gate_text.index("function Note-Stop"):gate_text.index("function Run-Assertion")]
    assert "$stop.code" in notes and "$stop.secs" in notes
    assert "[stops measured: " in notes


def test_measured_stop_seconds_reach_the_record_on_pass_and_on_fail() -> None:
    _check_stop_notes(_text())


def test_stop_notes_pin_fails_when_a_fail_drops_the_notes() -> None:
    """Mutant: the pre-fix Run-Assertion, whose FAIL record is the bare message."""
    text = _text()
    mutant = text.replace(
        "Record $key 'FAIL' ($_.Exception.Message + (Stop-Notes))", "Record $key 'FAIL' $_.Exception.Message"
    )
    assert mutant != text
    with pytest.raises(AssertionError):
        _check_stop_notes(mutant)


def test_stop_notes_pin_fails_when_a_pass_drops_the_notes() -> None:
    text = _text()
    mutant = text.replace("Record $key 'PASS' ($detail + (Stop-Notes))", "Record $key 'PASS' $detail")
    assert mutant != text
    with pytest.raises(AssertionError):
        _check_stop_notes(mutant)


def test_stop_notes_pin_fails_when_a_stop_is_not_noted() -> None:
    text = _text()
    for note in ("Note-Stop $stop 'A1 leg B --with-pg'", "Note-Stop $r 'A4 --with-pg'", "Note-Stop $r 'A4 plain'"):
        mutant = text.replace(note, "# " + note)
        assert mutant != text
        with pytest.raises(AssertionError):
            _check_stop_notes(mutant)


def test_stop_notes_do_not_touch_the_non_vacuity_rules() -> None:
    # the counting clauses the verdict stands on are still exactly what they were
    block = _verdict_block()
    assert "if ($executed -ne $DECLARED -or $failed.Count -gt 0 -or -not $preOk) {" in block
    assert "$skipIds -contains $id) { Record $key 'SKIPPED'" in _text()
