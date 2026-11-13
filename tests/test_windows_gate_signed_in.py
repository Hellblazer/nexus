# SPDX-License-Identifier: AGPL-3.0-or-later
"""The Windows gate's signed-in standard-user checkpoint (nexus-y3y8d): run-gate.sh --build-signed-in and
--signed-in, driver/checkpoint.ps1, restore.ps1 -Resume, stduser.ps1 -Existing, and the hostcheck and end.ps1
rules that hold Sam's one-checkpoint exception of 2026-11-13. Moved out of tests/test_windows_gate_driver.py (over
its size ceiling); the fake host harness and its helpers are imported from there."""
from __future__ import annotations

import re

import pytest

from tests.test_windows_gate_driver import (  # noqa: F401 -- harness is a fixture, used by name
    ALL_PS,
    DRIVER,
    GATE,
    PASSED,
    _BASE,
    Harness,
    _assert_cleaned_up_once,
    _code_lines,
    _read,
    _sh,
    _verdict,
    harness,
)


# ---- the signed-in checkpoint (nexus-y3y8d) ----------------------------------

SIGNED_IN = "stduser-signed-in-20261113"
BASELINE = "clean-baseline-2026-10-06"


def _scripts(h: Harness) -> list[tuple[str, str]]:
    """(script name, ssh log line) for every host script the run started, in order."""
    found = []
    for ln in h.ssh_lines():
        m = re.search(r"-File \S*\\([a-z-]+\.ps1)", ln)
        if m:
            found.append((m.group(1), ln))
    return found


def _line(h: Harness, name: str) -> str:
    return next(ln for n, ln in _scripts(h) if n == name)


@pytest.mark.parametrize("flag", ["--build-signed-in", "--signed-in"])
@pytest.mark.parametrize(
    ("extra", "why"),
    [
        ((), "need --standard-user"),
        (("--standard-user", "--applocker"), "no --applocker* or --wdac* mode"),
        (("--standard-user", "--applocker-allow-dll"), "no --applocker* or --wdac* mode"),
        (("--standard-user", "--no-admin-git", "--wdac-audit"), "no --applocker* or --wdac* mode"),
        (("--standard-user", "--no-admin-git"), "no --no-admin-git"),
        (("--standard-user", "--wheel", "@WHEEL@"), "take no --wheel"),
    ],
)
def test_the_signed_in_flags_hold_the_default_standard_user_state_only(
    harness: Harness, tmp_path: Path, flag: str, extra: tuple[str, ...], why: str
) -> None:
    wheel = tmp_path / "conexus-9.8.7-py3-none-any.whl"
    wheel.write_bytes(b"x")
    args = [str(wheel) if a == "@WHEEL@" else a for a in extra]
    res = harness.run(*_BASE, *args, flag, SIGNED_IN)
    assert res.returncode == 2, res.stderr
    assert why in res.stderr, res.stderr
    assert not (harness.root / "ssh.log").exists() and not (harness.root / "scp.log").exists()


@pytest.mark.parametrize("flag", ["--build-signed-in", "--signed-in"])
@pytest.mark.parametrize(
    "name",
    [
        "stduser-signed-in",
        "stduser-signed-in-2026",
        "stduser-signed-in-2026111x",
        "stduser-signed-in-202611133",
        "x-stduser-signed-in-20261113",
        "stduser-signed-in-20261113-b",
        "clean-baseline-2026-10-06",
        "a b",
    ],
)
def test_the_signed_in_checkpoint_name_must_match_the_pattern(harness: Harness, flag: str, name: str) -> None:
    res = harness.run(*_BASE, "--standard-user", flag, name)
    assert res.returncode == 2
    assert "stduser-signed-in-<8 digits>" in res.stderr, res.stderr
    assert not (harness.root / "ssh.log").exists()


def test_the_signed_in_name_may_not_be_the_baseline_and_the_two_flags_are_separate_runs(harness: Harness) -> None:
    res = harness.run(*_BASE, "--standard-user", "--signed-in", SIGNED_IN, env={"NX_GUEST_CHECKPOINT": SIGNED_IN})
    assert res.returncode == 2 and "must not be a stduser-signed-in-* checkpoint" in res.stderr
    res = harness.run(*_BASE, "--standard-user", "--signed-in", SIGNED_IN, "--build-signed-in", SIGNED_IN)
    assert res.returncode == 2 and "separate runs" in res.stderr
    res = harness.run(*_BASE, "--standard-user", "--build-signed-in", SIGNED_IN, env={"NX_GUEST_SETTLE_S": "soon"})
    assert res.returncode == 2 and "NX_GUEST_SETTLE_S" in res.stderr
    assert not (harness.root / "ssh.log").exists()


@pytest.mark.parametrize("extra", [(), ("--standard-user",)], ids=["plain", "standard-user"])
def test_a_run_without_the_flags_is_unchanged_baseline_restore_and_offline_boot(harness: Harness, extra: tuple[str, ...]) -> None:
    res = harness.run(*_BASE, *extra, "--out", str(harness.out), verdict=_verdict(PASSED))
    assert res.returncode == 0, res.stderr + res.stdout
    hc, restore = _line(harness, "hostcheck.ps1"), _line(harness, "restore.ps1")
    assert f"-Checkpoint {BASELINE} " in hc and "-SignedIn" not in hc and "-NewCheckpoint" not in hc
    assert f"-Checkpoint {BASELINE} " in restore and "-Resume" not in restore and "-Cache yes" in restore
    assert "checkpoint.ps1" not in "".join(n for n, _ in _scripts(harness))
    if extra:
        assert "-Existing" not in _line(harness, "stduser.ps1")
    assert "stduser-signed-in" not in "\n".join(harness.ssh_lines())
    # the offline boot is still restore.ps1's default path, and -Resume defaults to no
    restore_ps = _read("restore.ps1")
    assert "[ValidateSet('yes', 'no')][string]$Resume = 'no'" in restore_ps
    assert "$offline = $Cache -eq 'yes'" in restore_ps
    assert "Disconnect-VMNetworkAdapter -VMName $vm -ErrorAction Stop" in restore_ps


def test_build_signed_in_restores_the_baseline_signs_in_settles_checkpoints_and_ends(harness: Harness) -> None:
    res = harness.run(*_BASE, "--standard-user", "--build-signed-in", SIGNED_IN, "--out", str(harness.out), env={"NX_GUEST_SETTLE_S": "7"})
    assert res.returncode == 0, res.stderr + res.stdout
    assert [n for n, _ in _scripts(harness)] == ["hostcheck.ps1", "restore.ps1", "stduser.ps1", "checkpoint.ps1", "end.ps1"]
    hc, restore, std, ck = (_line(harness, n) for n in ("hostcheck.ps1", "restore.ps1", "stduser.ps1", "checkpoint.ps1"))
    # the guest is restored to the baseline, as in every run; nothing resumes
    assert f"-Checkpoint {BASELINE} " in restore and "-Resume" not in restore and SIGNED_IN not in restore
    assert f"-Checkpoint {BASELINE} " in hc and f"-NewCheckpoint {SIGNED_IN}" in hc and "-SignedIn" not in hc
    assert "-Existing" not in std
    assert re.search(rf"-Baseline {BASELINE} -Name {SIGNED_IN} -RunDir C:\\build\\guest\\nxg-\S+ -SettleSeconds 7$", ck) and ck.startswith("bytes_on_stdin=0 ::")
    # no install, setup, gate, launch, verdict, cache or collection; the normal end step ran once, shutting the guest down
    assert not (harness.root / "inner.log").exists() and not (harness.root / "launch.log").exists()
    assert not (harness.root / "cache.log").exists()
    assert not any("guest-run.ps1" in ln for ln in harness.ssh_lines())
    assert "-KeepGuest" not in harness.end_calls()[0]
    _assert_cleaned_up_once(harness)
    # its own success line, never a verdict line
    assert f"SIGNED-IN CHECKPOINT BUILT (no gate was run; this is not a gate verdict): the running checkpoint {SIGNED_IN}" in res.stdout
    assert not any(ln.startswith("WINDOWS PHASE 5 GATE") for ln in res.stdout.splitlines())
    # the guest was not collected from: there is no gate run to collect
    assert not (harness.out / "90-collect-on-exit.txt").exists() and not (harness.out / "run").exists()


def test_build_signed_in_settles_for_ten_minutes_by_default(harness: Harness) -> None:
    res = harness.run(*_BASE, "--standard-user", "--build-signed-in", SIGNED_IN, "--out", str(harness.out))
    assert res.returncode == 0, res.stderr + res.stdout
    assert "-SettleSeconds 600" in _line(harness, "checkpoint.ps1")


def test_a_failed_checkpoint_fails_the_run_and_still_ends_the_guest(harness: Harness) -> None:
    res = harness.run(*_BASE, "--standard-user", "--build-signed-in", SIGNED_IN, "--out", str(harness.out), fail_step="checkpoint")
    assert res.returncode == 1
    assert not (harness.root / "inner.log").exists()
    _assert_cleaned_up_once(harness)


def test_a_failed_account_step_in_the_build_takes_no_checkpoint(harness: Harness) -> None:
    res = harness.run(*_BASE, "--standard-user", "--build-signed-in", SIGNED_IN, "--out", str(harness.out), fail_step="stduser")
    assert res.returncode == 1
    assert "checkpoint.ps1" not in "".join(n for n, _ in _scripts(harness))


def test_signed_in_resumes_the_checkpoint_and_checks_the_existing_account(harness: Harness) -> None:
    res = harness.run(*_BASE, "--standard-user", "--signed-in", SIGNED_IN, "--out", str(harness.out), verdict=_verdict(PASSED))
    assert res.returncode == 0, res.stderr + res.stdout
    order = [n for n, _ in _scripts(harness)]
    assert order[:4] == ["hostcheck.ps1", "restore.ps1", "stduser.ps1", "cache.ps1"] and "checkpoint.ps1" not in order
    hc, restore, std = (_line(harness, n) for n in ("hostcheck.ps1", "restore.ps1", "stduser.ps1"))
    # hostcheck names both: the baseline the checkpoint must be a child of, and the checkpoint it checks
    assert f"-Checkpoint {BASELINE} " in hc and f"-SignedIn {SIGNED_IN}" in hc
    assert f"-Checkpoint {SIGNED_IN} " in restore and "-Resume yes" in restore and BASELINE not in restore
    assert "-Existing yes" in std and "-AppLocker no -Wdac no" in std and "-AdminGit yes" in std
    # every later step is the plain standard-user run's
    later = [ln for n, ln in _scripts(harness) if n == "guest-run.ps1"]
    assert len(later) == 4 and all("-RunAs nxstd" in ln for ln in later)
    assert "run as standard user nxstd" in res.stdout
    _assert_cleaned_up_once(harness)


def test_hostcheck_refuses_a_signed_in_checkpoint_that_is_missing_or_wrongly_parented_and_reads_its_type() -> None:
    code = "\n".join(_code_lines(_read("hostcheck.ps1")))
    block = code[code.index("if ($SignedIn) {"):code.index("if ($NewCheckpoint) {")]
    assert "$_.Name -eq $SignedIn" in block and "exit 7" in block
    assert "$sn[0].ParentSnapshotName -ne $Checkpoint" in block and "exit 8" in block
    # the type is a printed reading only: the baseline is also Standard, so it cannot tell a running checkpoint from it
    assert "type=$($sn[0].SnapshotType) (a reading only)" in block and "exit 9" not in block and "-ne 'Standard'" not in block
    assert code.index("if ($SignedIn) {") < code.index("'NXG-RESULT: ok'")
    new = code[code.index("if ($NewCheckpoint) {"):code.index("if ($parseErrors -gt 0)")]
    # Sam's exception, held here without the wrapper: the VM, the baseline, ONE signed-in checkpoint
    assert "$VMName -ne 'nx-clean-win11' -or $Checkpoint -ne 'clean-baseline-2026-10-06'" in new and "exit 11" in new
    assert "$snaps | Where-Object { $_ -match '^stduser-signed-in-' }" in new
    assert "$NewCheckpoint -eq $Checkpoint -or $have.Count -gt 0" in new and "exit 10" in new
    assert "needs Sam to delete the old one by hand" in new
    # the cmdlets this run depends on are read from their parameter lists, and nothing is invoked
    assert "Get-Command Set-VM -ErrorAction SilentlyContinue" in new and "Get-Command Checkpoint-VM -ErrorAction SilentlyContinue" in new
    assert "$setVm.Parameters.ContainsKey('CheckpointType')" in new and "$takeVm.Parameters.ContainsKey('SnapshotName')" in new and "exit 12" in new
    # -Checkpoint never names a signed-in checkpoint, in any mode, before the guest is read
    refuse = "if ($Checkpoint -match '^stduser-signed-in-')"
    assert refuse in code and "exit 11" in code[code.index(refuse):code.index("Get-VM -Name")]
    assert code.index(refuse) < code.index("Get-VM -Name")
    # the VM's CheckpointType is printed
    assert "CheckpointType=$($v.CheckpointType)" in code
    # both names are held to the pattern before the guest is read
    pattern = "'^stduser-signed-in-[0-9]{8}$'"
    assert pattern in code and code.index(pattern) < code.index("Get-VM -Name")


def test_checkpoint_ps1_is_the_one_place_a_checkpoint_is_taken_and_only_under_its_rules() -> None:
    ck = _read("checkpoint.ps1")
    code = "\n".join(_code_lines(ck))
    takers = [n for n in ALL_PS if re.search(r"Checkpoint-VM", "\n".join(_code_lines(_read(n))))]
    # hostcheck.ps1 names the cmdlet to read its parameter list; only checkpoint.ps1 calls it
    assert sorted(takers) == ["checkpoint.ps1", "hostcheck.ps1"]
    assert code.count("Checkpoint-VM") == 1 and "Checkpoint-VM -Name $vm -SnapshotName $Name -ErrorAction Stop" in code
    # the name: the pattern, refused before anything is changed
    assert "$Name -notmatch '^stduser-signed-in-[0-9]{8}$'" in code
    # Sam's exception, held without the wrapper: this VM, this baseline (exit 11), ONE checkpoint of the kind (exit 3)
    exc = code.index("$vm -ne 'nx-clean-win11' -or $Baseline -ne 'clean-baseline-2026-10-06'")
    assert "exit 11" in code[exc:code.index("$snaps =")]
    refuse = code.index("if ($Name -eq $Baseline -or $have.Count -gt 0)")
    assert "$snaps | Where-Object { $_ -match '^stduser-signed-in-' }" in code[exc:refuse]
    assert "exit 3" in code[refuse:code.index("if ($snaps -notcontains $Baseline)")]
    assert "needs Sam to delete the old one by hand" in code[refuse:code.index("if ($snaps -notcontains $Baseline)")]
    assert exc < refuse < code.index("Set-VM") and refuse < code.index("Checkpoint-VM")
    # the baseline must exist and be the guest's current checkpoint, so the new one is its child
    assert "$v.ParentCheckpointName -ne $Baseline" in code and "$v.State -ne 'Running'" in code
    # the settle precedes the checkpoint; the default is ten minutes; the host's Default Switch counter is printed once a minute, not gated on
    assert "[int]$SettleSeconds = 600" in code
    settle = code.index("while (((Get-Date) - $t0).TotalSeconds -lt $SettleSeconds)")
    assert settle < code.index("Set-VM -VMName $vm -CheckpointType Standard")
    loop = code[settle:code.index("Switch-Bytes\n$v = Check-Guest")]
    assert "$nextReading += 60" in loop and "Switch-Bytes" in loop and "exit" not in loop and "throw" not in loop
    assert "Get-NetAdapterStatistics -Name 'vEthernet (Default Switch)'" in code
    # CheckpointType (nexus-y3y8d): recorded in a file in the run directory BEFORE the first Set-VM (and read back),
    # set to Standard, the checkpoint taken, the recorded type put back in a finally, read again after it
    rec = code.index("$was = \"$($v.CheckpointType)\"")
    wr = code.index("Set-Content -LiteralPath $typeFile -Value $was")
    gate = code.index("if ($readBack -ne $was)")
    setstd = code.index("Set-VM -VMName $vm -CheckpointType Standard -ErrorAction Stop")
    take = code.index("Checkpoint-VM -Name")
    fin = code.index("} finally {", take)
    back = code.index("Set-VM -VMName $vm -CheckpointType $was -ErrorAction Stop", fin)
    reread = code.index("$now = \"$((Get-VM -Name $vm).CheckpointType)\"", back)
    assert rec < wr < gate < setstd < take < fin < back < reread
    assert "'checkpoint-type.txt'" in code and "exit 12" in code[gate:setstd]
    assert code.count("Set-VM") == 2 and all("-CheckpointType" in ln for ln in code.splitlines() if "Set-VM" in ln)
    # the failure is a boolean flag, not the truthiness of a message: an exception whose message is empty still fails
    assert "$typeNotBack = $true" in code[fin:reread] and "if ($now -ne $was) { $typeNotBack = $true }" in code
    assert "if ($typeNotBack -or $err) {" in code and "if ($restoreErr)" not in code
    # what it reports and what it fails on; the type of the new checkpoint is a reading only
    assert "parent=$($new.ParentSnapshotName) type=$($new.SnapshotType) (a reading only)" in code
    assert "$new.ParentSnapshotName -ne $Baseline" in code and "-ne 'Standard'" not in code and "exit 9" not in code
    assert "$after -notcontains $Baseline" in code
    assert code.rstrip().endswith("'NXG-RESULT: ok'")
    # no secret: the script has no way to read or store one
    assert not re.search(r"DefaultPassword|Winlogon|Open-GuestSession|New-PSSession", ck)


def test_end_ps1_puts_the_checkpoint_type_back_only_to_the_recorded_value_and_fails_loud() -> None:
    end = _read("end.ps1")
    code = "\n".join(_code_lines(end))
    # it reads what checkpoint.ps1 recorded, validates it as a type name, and sets nothing else
    assert "$typeFile = Join-Path $RunDir 'checkpoint-type.txt'" in code
    rec = code.index("$recorded = (\"\" + (Get-Content -LiteralPath $typeFile -TotalCount 1")
    valid = code.index("$recorded -notmatch '^(Disabled|Production|ProductionOnly|Standard)$'")
    setvm = code.index("Set-VM -VMName $vm -CheckpointType $recorded -ErrorAction Stop")
    assert rec < valid < setvm and code.count("Set-VM") == 1
    assert "if (Test-Path -LiteralPath $typeFile) {" in code and "if ($cur -ne $recorded) {" in code
    # it runs after the guest is shut down, reads the type again, and the verdict is a boolean flag
    assert code.index("Stop-VM -Name $vm -Force") < code.index("$typeFile = ")
    assert "if ($now -ne $recorded) { $typeFailed = $true }" in code
    assert "if ($typeFailed) { \"NXG-RESULT: FAIL the guest's CheckpointType is not back" in code
    # the evidence file stays when it could not be applied, and is removed by exact name otherwise
    assert "if (-not $typeFailed) { $mine += Join-Path $RunDir 'checkpoint-type.txt' }" in code
    # checkpoint.ps1 writes the same name, in the same run directory
    assert "'checkpoint-type.txt'" in _read("checkpoint.ps1") and "[Parameter(Mandatory = $true)][string]$RunDir" in _read("checkpoint.ps1")
    # the VM's CheckpointType is printed
    assert "CheckpointType=$($v.CheckpointType)" in code


def test_the_restore_resumes_a_saved_guest_without_the_offline_boot() -> None:
    code = "\n".join(_code_lines(_read("restore.ps1")))
    after = code[code.index("Restore-VMSnapshot"):]
    saved = after.index("if ($st -ne 'Saved')")
    start = after.index("Start-VM -Name $vm")
    assert saved < start and "exit 6" in after[saved:start]
    resume_branch = after[after.index("if ($resuming) {\n    $st"):after.index("} elseif ($offline) {")]
    assert "Disconnect-VMNetworkAdapter" not in resume_branch and "Add-VMNetworkAdapter" not in resume_branch
    # once Start-VM has resumed it, the adapter is on the Default Switch before the quiet block and the probe
    after_start = after[start:]
    assert after_start.index("Connect-VMNetworkAdapter -SwitchName 'Default Switch'") < after_start.index("$quietBlock; $quieted = $true")
    assert "if ($resuming) { $offline = $false }" in after[:start]
    # a stale NAT lease: once, and only for a resumed guest whose probe has failed three times
    loop = after[after.index("for ($i = 0; $i -lt 60; $i++)"):]
    assert "if ($resuming -and -not $ok -and $i -ge 2 -and -not $renewed) {" in loop
    assert loop.count("$renew = { ipconfig /renew 2>&1 | Out-String }") == 1 and "$renewed = $true" in loop
    assert loop.index("Invoke-WebRequest https://pypi.org/simple/") < loop.index("$renew = {")
    # the quiet block still runs for a resumed guest, and the gate script is copied in as always
    assert "$quieted = $Cache -ne 'yes'" in code and "windows-phase5-gate.ps1" in code
    # the once-only flag is set only after the renew actually ran (a failed Invoke-Command leaves it for the next try)
    renew = loop[loop.index("$rn = Invoke-Command"):]
    assert renew.index("$rn = Invoke-Command") < renew.index("$renewed = $true") < renew.index("} catch {")
    assert loop.count("$renewed = $true") == 1
    # the resumed guest's clock: printed against the host's, one w32tm /resync, printed again, nothing gating
    clock = code[code.index("if ($resuming) {\n    function ClockDelta"):code.index("$rc = 0")]
    assert clock.index("before the resync") < clock.index("w32tm /resync") < clock.index("after the resync")
    assert clock.count("$resync = { w32tm /resync") == 1 and "exit" not in clock
    assert code.index("if (-not $ok) { 'NXG-RESULT: FAIL the guest has no network'; exit 4 }") < code.index("function ClockDelta")
    # without the wrapper: the signed-in checkpoint is restored only with -Resume yes, and -Resume yes names nothing else
    assert "if (($Checkpoint -match '^stduser-signed-in-') -ne ($Resume -eq 'yes')) { throw" in code
    assert "if ($Resume -eq 'yes' -and $Checkpoint -notmatch '^stduser-signed-in-[0-9]{8}$') { throw" in code
    # the resume's real test: the guest is Saved after the restore (the SnapshotType cannot tell, the baseline is Standard too)
    assert "if ($st -ne 'Saved') {" in code


def test_existing_mode_checks_the_account_and_builds_nothing() -> None:
    std = _read("stduser.ps1")
    start = std.index("if ($Existing -eq 'yes') {")
    end = std.index("# -Existing ends here")
    branch = "\n".join(_code_lines(std[start:end]))
    # no account, no policy, no Git install, no secret, no restart
    for forbidden in (
        "Add-LocalGroupMember", "ADSI", "SetPassword", "Create(", "winget", "shutdown.exe", "Set-ItemProperty",
        "New-Item -Force", "Set-AppLockerPolicy", "CiTool", "RNGCryptoServiceProvider", "AutoAdminLogon",
    ):
        assert forbidden not in branch, forbidden
    assert not re.search(r"Winlogon[^\n]*(Set-|-Value)|DefaultPassword\s+-Value", branch)
    # the account exists, is in Users and no other group (by SID), is signed in at the console with its desktop shell
    assert "Get-LocalUser -Name $u" in branch and "Get-LocalGroupMember" in branch
    assert "$gsids.Count -eq 1 -and $gsids[0] -eq 'S-1-5-32-545'" in branch
    assert "query user" in branch and "interactive session at the console=" in branch
    assert "explorer running for account=" in branch and "Win32_Process -Filter \"Name='explorer.exe'\"" in branch
    assert "GetOwner).User -eq $u" in branch
    # the policy read-back line, literally (the rows below match on it)
    assert (
        "quiet-guest policy read back: OneDrive,Widgets,ConsumerFeatures,EdgeBackground=$pol (want 1,0,1,0); "
        "default-user OneDriveSetup Run entry: $odRun"
    ) in branch
    assert "'C:\\Program Files\\Git\\cmd\\git.exe'" in branch
    assert "winlogon secret present=" in branch
    # the per-run staging the normal path does after sign-in, with the hashes run-gate.sh greps
    assert 'Copy-Item -LiteralPath "$from\\windows-phase5-gate.ps1" -Destination "$b\\windows-phase5-gate.ps1" -Force' in branch
    assert '"std gate sha256="' in branch and '"std wheel sha256="' in branch
    # every failed check is a FAIL with its own exit code (the table below), and ok only after all of them
    assert [r[0] for r in _existing_rows()] == [12, 13, 14, 18, 15, 15, 16, 17]
    assert branch.rstrip().endswith("exit 0\n}") and branch.index("if ($rcx -ne 0) { exit $rcx }") < branch.index("'NXG-RESULT: ok'")
    assert branch.index("foreach ($c in $checks) {") < branch.index("if ($rcx -ne 0) { exit $rcx }")
    # the mode holds the default state only, sits before anything of the build, and the header says who takes a checkpoint
    assert "if ($Existing -eq 'yes' -and ($AdminGit -eq 'no' -or $AppLocker -ne 'no' -or $Wdac -ne 'no')) { throw" in std[:start]
    assert start < std.index("Add-LocalGroupMember")
    assert "no checkpoint is taken." not in std
    assert "Only run-gate.sh --build-signed-in takes one" in std


def _existing_rows() -> list[tuple[int, str, str, str]]:
    """stduser.ps1's -Existing check table: (exit code, must|mustnot, pattern, message), in the order it is evaluated."""
    rows = re.findall(r"^\s*@\((\d+), '(must|mustnot)', '([^']*)', '([^']*)'\),?$", _read("stduser.ps1"), re.M)
    return [(int(c), kind, pat, msg) for c, kind, pat, msg in rows]


def _existing_chain(text: str) -> int:
    """The chain as stduser.ps1 evaluates it ($text -match pattern, PowerShell's -match being case-insensitive): the
    exit code of the first failed row, 0 when none fails."""
    for code, kind, pat, _msg in _existing_rows():
        hit = re.search(pat, text, re.I) is not None
        if (kind == "must") != hit:
            return code
    return 0


_EXISTING_GOOD = "\n".join(
    [
        "account exists=True user=nxstd",
        "member of (sids): S-1-5-32-545",
        "member of Users only=True",
        "console user=NXG\\nxstd",
        " USERNAME              SESSIONNAME        ID  STATE   IDLE TIME  LOGON TIME",
        ">nxstd                 console             1  Active      none   11/13/2026 1:02 PM",
        "interactive session at the console=True",
        "explorer running for account=True",
        "quiet-guest policy read back: OneDrive,Widgets,ConsumerFeatures,EdgeBackground=1,0,1,0 (want 1,0,1,0); "
        "default-user OneDriveSetup Run entry: absent",
        "gitexe=True (for all users)",
        "winlogon secret present=False",
        "std gate sha256=" + "0" * 64,
    ]
)


def test_the_existing_checks_are_pinned_by_their_conditions_not_by_their_exit_codes() -> None:
    # the canned output is what the guest block prints: its format strings are in the source
    block = _read("stduser.ps1")
    for printed in (
        '"account exists=" + [bool]$acct',
        '"member of Users only="',
        '"interactive session at the console="',
        '"explorer running for account="',
        '"gitexe=" + (Test-Path',
        '"winlogon secret present="',
        "EdgeBackground=$pol (want 1,0,1,0); default-user OneDriveSetup Run entry: $odRun",
    ):
        assert printed in block, printed
    rows = {(c, k): pat for c, k, pat, _ in _existing_rows()}
    # each pattern, literally: a weakened one (gitexe=, present=, a missing "(want") goes red here
    assert rows[(16, "must")] == "gitexe=True"
    assert rows[(17, "must")] == "winlogon secret present=False"
    assert rows[(12, "must")] == "account exists=True" and rows[(13, "must")] == "member of Users only=True"
    assert rows[(14, "must")] == "interactive session at the console=True"
    assert rows[(18, "must")] == "explorer running for account=True"
    assert rows[(15, "must")] == r"EdgeBackground=1,0,1,0 \(want" and rows[(15, "mustnot")] == "OneDriveSetup Run entry: STILL PRESENT"
    assert _existing_chain(_EXISTING_GOOD) == 0
    bad = {
        12: _EXISTING_GOOD.replace("account exists=True", "account exists=False"),
        13: _EXISTING_GOOD.replace("member of Users only=True", "member of Users only=False"),
        14: _EXISTING_GOOD.replace("console=True", "console=False"),
        18: _EXISTING_GOOD.replace("explorer running for account=True", "explorer running for account=False"),
        15: _EXISTING_GOOD.replace("EdgeBackground=1,0,1,0 (want", "EdgeBackground=1,0,1,1 (want"),
        16: _EXISTING_GOOD.replace("gitexe=True", "gitexe=False"),
        17: _EXISTING_GOOD.replace("present=False", "present=True"),
    }
    for code, text in bad.items():
        assert _existing_chain(text) == code, code
    # a policy that did not read back at all (the values are blank), the Run entry still there, a value of 2 for the first
    assert _existing_chain(_EXISTING_GOOD.replace("EdgeBackground=1,0,1,0", "EdgeBackground=,,,")) == 15
    assert _existing_chain(_EXISTING_GOOD.replace("Run entry: absent", "Run entry: STILL PRESENT")) == 15
    assert _existing_chain(_EXISTING_GOOD.replace("EdgeBackground=1,0,1,0", "EdgeBackground=2,0,1,0")) == 15
    # an empty output fails the very first row
    assert _existing_chain("") == 12


def test_the_console_session_condition_wants_the_account_active_on_the_console() -> None:
    src = _read("stduser.ps1")
    line = next(ln.strip() for ln in src.splitlines() if ln.strip().startswith("$mine = @($q -split"))
    assert line.count("-match") == 3
    assert '"^\\s*>?$([regex]::Escape($u))\\s"' in line and "'\\bActive\\b'" in line and "'\\bconsole\\b'" in line

    def sessions(text: str, user: str = "nxstd") -> bool:
        rx = rf"^\s*>?{re.escape(user)}\s"
        return any(re.search(rx, ln, re.I) and re.search(r"\bActive\b", ln, re.I) and re.search(r"\bconsole\b", ln, re.I) for ln in text.splitlines())

    assert sessions(">nxstd                 console             1  Active      none   11/13/2026 1:02 PM")
    assert not sessions(" nxstd                 console             1  Disc        none   11/13/2026 1:02 PM")
    assert not sessions(" nxstd                 rdp-tcp#0           2  Active      none   11/13/2026 1:02 PM")
    assert not sessions(" nxguest               console             1  Active      none   11/13/2026 1:02 PM")
    assert not sessions(" nxstd2                console             1  Active      none   11/13/2026 1:02 PM")
    # a failed session stops the check: it exits through the table's code 14, which is before the policy rows
    codes = [c for c, _, _, _ in _existing_rows()]
    assert codes.index(14) < codes.index(15)


def test_the_build_fails_unless_the_account_secret_was_removed_and_says_so_before_the_checkpoint() -> None:
    std = _read("stduser.ps1")
    guest = std[std.index("# Wait for the restart and for the new account to own the console."):]
    assert "winlogon secret removed=" in guest
    assert "elseif ($text -notmatch 'winlogon secret removed=True') {" in guest and "$rc = 19" in guest
    sh = "\n".join(_code_lines(_sh()))
    gate = sh.index("grep -qF 'winlogon secret removed=True' \"$out/01s-stduser.txt\"")
    assert gate < sh.index("run_step 01k checkpoint") and "|| die 1" in sh[gate:sh.index("run_step 01k checkpoint")]


def test_the_build_takes_no_checkpoint_when_the_secret_stays(harness: Harness) -> None:
    res = harness.run(*_BASE, "--standard-user", "--build-signed-in", SIGNED_IN, "--out", str(harness.out), env={"FAKE_SECRET_STAYS": "1"})
    assert res.returncode == 1 and "winlogon secret removed=True" in res.stderr
    assert "checkpoint.ps1" not in "".join(n for n, _ in _scripts(harness))
    assert not (harness.root / "checkpoint.log").exists()
    _assert_cleaned_up_once(harness)


def test_only_the_standard_user_signed_in_path_may_name_the_new_checkpoint() -> None:
    sh = "\n".join(_code_lines(_sh()))
    # restore names the signed-in checkpoint in one place, inside the --signed-in branch; the default is the baseline's
    assert sh.count('restore_checkpoint="$signed_in"') == 1 and 'restore_checkpoint="$CHECKPOINT"' in sh
    branch = sh[sh.index('if [[ -n "$signed_in" ]]; then\n    hostcheck_extra'):]
    assert branch.index('restore_checkpoint="$signed_in"') < branch.index("fi")
    # the flags need --standard-user and refuse the other modes, before any remote call; NX_GUEST_CHECKPOINT never names one
    guard = sh[sh.index("for pair in"):sh.index('install_doc="')]
    for needle in (
        "$standard_user -eq 1", '"$applocker" == no && "$wdac" == no', '"$admin_git" == yes', '-z "$wheel"',
        "stduser-signed-in-[0-9]{8}", '"$cache" == on', '[[ ! "$CHECKPOINT" =~ ^stduser-signed-in- ]]',
    ):
        assert needle in guard, needle
    # the build ends at the end step: nothing after the checkpoint runs, and no collection is attempted
    build = sh[sh.index('if [[ -n "$signed_in_build" ]]; then\n    grep -qF'):]
    build = build[: build.index("\nfi\n")]
    assert "finish" in build and "exit 0" in build and "run_step 01k" in build
    for later in ("run_step 02", "run_step 03", "run_step 04", "run_step 05", "run_step 06", "harvest", "launch_guest"):
        assert later not in build, later
    # nothing is collected from a build, whatever step stops it: the flag is set before the first step runs
    assert sh.index("collected=1\nfi") < sh.index("run_step 00 hostcheck") < sh.index("run_step 01k")
    # the header documents both flags and the step
    head = _sh()[: _sh().index("set -uo pipefail")]
    for needle in ("--build-signed-in NAME", "--signed-in NAME", "01k checkpoint", "NX_GUEST_SETTLE_S", "stduser-signed-in-<8 digits>"):
        assert needle in head, needle


@pytest.mark.parametrize(
    "extra",
    [(), ("--standard-user",), ("--standard-user", "--build-signed-in", SIGNED_IN), ("--standard-user", "--signed-in", SIGNED_IN)],
    ids=["plain", "standard-user", "build", "signed-in"],
)
def test_nx_guest_checkpoint_never_names_a_signed_in_checkpoint_in_any_mode(harness: Harness, extra: tuple[str, ...]) -> None:
    for name in (SIGNED_IN, "stduser-signed-in-x"):
        res = harness.run(*_BASE, *extra, env={"NX_GUEST_CHECKPOINT": name})
        assert res.returncode == 2 and "NX_GUEST_CHECKPOINT must not be a stduser-signed-in-* checkpoint" in res.stderr, res.stderr
        assert not (harness.root / "ssh.log").exists()


@pytest.mark.parametrize(
    "env",
    [{"NX_GUEST_VM": "nx-pro-win11"}, {"NX_GUEST_CHECKPOINT": "clean-baseline-pro-2026-10-13"}, {"NX_GUEST_VM": "nx-pro-win11", "NX_GUEST_CHECKPOINT": "x"}],
)
def test_the_build_is_refused_outside_sams_exception_before_anything_remote(harness: Harness, env: dict[str, str]) -> None:
    res = harness.run(*_BASE, "--standard-user", "--build-signed-in", SIGNED_IN, env=env)
    assert res.returncode == 2 and "--build-signed-in is allowed only" in res.stderr, res.stderr
    assert not (harness.root / "ssh.log").exists() and not (harness.root / "scp.log").exists()


def test_the_signed_in_run_is_not_refused_for_the_vm_or_the_baseline_by_the_wrapper(harness: Harness) -> None:
    # the exception binds the build; a resume is checked against the baseline by hostcheck (the checkpoint's parent)
    res = harness.run(*_BASE, "--standard-user", "--signed-in", SIGNED_IN, "--out", str(harness.out), verdict=_verdict(PASSED), env={"NX_GUEST_VM": "nx-pro-win11"})
    assert res.returncode == 0, res.stderr


@pytest.mark.parametrize("flag", ["--build-signed-in", "--signed-in"])
def test_the_signed_in_flags_refuse_cache_off(harness: Harness, flag: str) -> None:
    res = harness.run(*_BASE, "--standard-user", "--cache", "off", flag, SIGNED_IN)
    assert res.returncode == 2 and "need --cache on" in res.stderr, res.stderr
    assert not (harness.root / "ssh.log").exists()


@pytest.mark.parametrize("settle", ["08", "09", "0010", "000"])
def test_a_settle_period_with_leading_zeros_is_decimal(harness: Harness, settle: str) -> None:
    res = harness.run(*_BASE, "--standard-user", "--build-signed-in", SIGNED_IN, "--out", str(harness.out), env={"NX_GUEST_SETTLE_S": settle})
    assert res.returncode == 0, res.stderr + res.stdout
    assert f"-SettleSeconds {int(settle, 10)}" in _line(harness, "checkpoint.ps1")


@pytest.mark.parametrize("settle", ["08", "09"])
def test_a_bad_octal_settle_does_not_abort_a_plain_run(harness: Harness, settle: str) -> None:
    res = harness.run(*_BASE, "--out", str(harness.out), verdict=_verdict(PASSED), env={"NX_GUEST_SETTLE_S": settle})
    assert res.returncode == 0, res.stderr + res.stdout


def test_a_failed_checkpoint_step_is_not_collected_from(harness: Harness) -> None:
    res = harness.run(*_BASE, "--standard-user", "--build-signed-in", SIGNED_IN, "--out", str(harness.out), fail_step="checkpoint")
    assert res.returncode == 1
    assert not (harness.out / "90-collect-on-exit.txt").exists()
    assert not any("-Collect yes" in ln for ln in harness.ssh_lines())


def test_a_signed_in_run_says_what_it_no_longer_proves_at_the_start_and_in_the_summary(harness: Harness) -> None:
    res = harness.run(*_BASE, "--standard-user", "--signed-in", SIGNED_IN, "--out", str(harness.out), verdict=_verdict(PASSED))
    assert res.returncode == 0, res.stderr + res.stdout
    lines = [ln for ln in res.stdout.splitlines() if f"resumed from checkpoint {SIGNED_IN}" in ln]
    assert len(lines) == 2, res.stdout
    for ln in lines:
        assert BASELINE in ln and "first sign-in" in ln and "boot after a restore" in ln and "must not be cited" in ln
    assert lines[1].startswith("run-gate.sh: NOTE ") and res.stdout.rstrip().endswith(lines[1])
    # the verdict line is still the gate's, untouched, and a normal run says nothing of the kind
    assert PASSED in res.stdout.splitlines()
    plain = harness.run(*_BASE, "--standard-user", "--out", str(harness.out.parent / "out2"), verdict=_verdict(PASSED))
    assert "resumed from checkpoint" not in plain.stdout


def test_the_build_names_the_memory_image_and_not_a_stored_secret(harness: Harness) -> None:
    res = harness.run(*_BASE, "--standard-user", "--build-signed-in", SIGNED_IN, "--out", str(harness.out))
    assert res.returncode == 0, res.stderr + res.stdout
    said = [ln for ln in res.stdout.splitlines() if "memory image" in ln]
    assert len(said) == 2 and all("removed from Winlogon before the checkpoint" in ln for ln in said), res.stdout
    assert "no script stor" not in res.stdout  # the secret IS written, for the restart; the claim is that it is removed


def test_a_build_whose_end_step_did_not_finish_clean_is_not_a_success(harness: Harness) -> None:
    # end.ps1 puts the CheckpointType back from the recorded file and fails loud when it cannot
    res = harness.run(*_BASE, "--standard-user", "--build-signed-in", SIGNED_IN, "--out", str(harness.out), env={"FAKE_END_BAD": "1"})
    assert res.returncode == 1, res.stdout + res.stderr
    assert "SIGNED-IN CHECKPOINT BUILT" not in res.stdout


def test_the_headers_state_the_exception_and_what_a_resumed_run_no_longer_proves() -> None:
    head = _sh()[: _sh().index("set -uo pipefail")]
    flat = re.sub(r"\n#\s*", " ", head)
    for needle in (
        "Sam's exception (2026-11-13) is ONE running child checkpoint",
        "nx-clean-win11", "clean-baseline-2026-10-06", "^stduser-signed-in-", "needs Sam to delete the old one by hand",
        "A --signed-in RUN IS A DIFFERENT CLAIM", "does not exercise the standard user's first sign-in",
        "does not exercise the boot after a", "must not cite a --signed-in run",
        "MEMORY IMAGE", "removes it before the checkpoint", "SIGNED-IN CHECKPOINT BUILT",
    ):
        assert needle in flat, needle
    # the "exits 0 only on PASSED" claim names its one exception
    assert "The one exception is --build-signed-in" in flat
    # no header claims the checkpoint holds no secret as such
    for text in (head, _read("stduser.ps1"), _read("checkpoint.ps1")):
        assert "It holds no account secret" not in text and "The guest holds no account secret" not in text
        assert "memory image" in text.lower()
    gate = GATE.read_text(encoding="utf-8")
    gate = gate[: gate.index("param(")]
    assert "A RESUMED RUN IS A DIFFERENT CLAIM" in gate
    section = gate[gate.index("A RESUMED RUN IS A DIFFERENT CLAIM"):gate.index("OPERATOR PROCEDURE")]
    for needle in ("clean-baseline-2026-10-06", "first sign-in", "boot after a restore", "must not cite a --signed-in run"):
        assert needle in re.sub(r"\n#\s*", " ", section), needle



def test_the_existing_check_evaluator_and_the_type_exit_are_pinned_literally() -> None:
    # Review r2 (b): the Python evaluator in the -Existing tests stands in for these lines; they must not drift.
    std = (DRIVER / "stduser.ps1").read_text(encoding="utf-8")
    for line in ("$hit = $text -match $c[2]",
                 "if (($c[1] -eq 'must') -ne $hit) { \"NXG-RESULT: FAIL ${StdUser}: $($c[3])\"; $rcx = $c[0]; break }"):
        assert line in std, line
    cp = (DRIVER / "checkpoint.ps1").read_text(encoding="utf-8")
    block = cp[cp.index("if ($typeNotBack -or $err) {"):]
    branch = block[: block.index("\n}\n") + 3]
    assert branch.rstrip().endswith("exit 7\n}"), branch  # the failure branch exits 7 before anything says ok
    assert "a partial checkpoint named $Name may still exist" in cp



def test_no_driver_script_assigns_to_one_of_its_own_parameters_in_any_case() -> None:
    # 2026-11-13 build start: restore.ps1 had `$resume = $Resume -eq 'yes'`. PowerShell variable names are
    # case-blind, so that assigned the -Resume parameter, its ValidateSet refused the bool, $resume kept the string
    # 'no' (true), and a baseline restore took the resume branch. Text pins and the parser cannot see it.
    found = []
    for f in sorted(DRIVER.glob("*.ps1")):
        src = f.read_text(encoding="utf-8")
        m = re.search(r"param\((.*?)\n\)", src, re.S)
        if not m:
            continue
        params = {p.lower() for p in re.findall(r"\]\s*\$(\w+)", m.group(1))}
        body = src[m.end():]
        for ln in body.splitlines():
            a = re.match(r"\s*\$(\w+)\s*=(?!=)", ln)
            if a and a.group(1).lower() in params:
                found.append(f"{f.name}: {ln.strip()}")
    assert not found, found
