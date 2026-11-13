# SPDX-License-Identifier: AGPL-3.0-or-later
#
# Host (the Hyper-V machine), no guest credential. Step 01k of run-gate.sh --build-signed-in (nexus-y3y8d):
# the one place the driver takes a checkpoint. The guest has just been restored to the baseline checkpoint
# and the standard user has signed in (the stduser step); after a settle period (-SettleSeconds, the
# run-gate.sh NX_GUEST_SETTLE_S, default 600) this takes ONE running checkpoint of that state, named
# -Name (stduser-signed-in-<8 digits>), so that run-gate.sh --signed-in resumes past the sign-in
# instead of paying for it every run (about 300 MiB of network a run).
#
# It holds Sam's exception of 2026-11-13 by itself, without the wrapper: ONE running child checkpoint of
# nx-clean-win11's baseline. It refuses, before it touches anything, when -VMName is not nx-clean-win11 or
# -Baseline is not clean-baseline-2026-10-06 (11); when -Name is not of the shape above (a throw), is the
# baseline's name, or ANY checkpoint matching ^stduser-signed-in- exists already (3: one checkpoint only, and a
# rebuild needs Sam to delete the old one by hand: this script never does); when the baseline does not exist (4);
# when the guest is not Running (5); or when the guest's current checkpoint is not the baseline, so that the new
# one would not be its child (6). It removes, renames and replaces no checkpoint, the baseline included.
# CheckpointType: it records the guest's type in <RunDir>\checkpoint-type.txt BEFORE the first Set-VM (12 when
# the type is unknown or the file cannot be written and read back), sets Standard (a Production checkpoint
# has no memory state and would need the account's secret kept in the guest to sign in again), takes the
# checkpoint, and puts the recorded type back in a finally. end.ps1 runs on every exit of the run, a
# dropped ssh session included, and puts the type back from that file when this script could not; after the
# finally this script reads the type again and fails (7) on a boolean flag, not on a message, unless it equals
# the recorded one. It then prints the new checkpoint's name, parent and type, and fails unless the parent is the
# baseline (8) and the baseline is still there (10). The type it prints is a reading only: the baseline is also
# Standard, so it cannot tell a running checkpoint from the baseline; run-gate.sh --signed-in's resume (the guest
# is Saved after the restore) is the test of that.
# No script stores the account secret (the stduser step removed it from the guest's logon settings, and the
# build is stopped unless it said so), and this one reads and stores none. The checkpoint's memory image holds the signed-in session.
# Once a minute during the settle it prints the host's Default Switch byte counters, for the record only.
# Exit 3, 4, 5, 6, 8, 10, 11, 12 as above; 7 the checkpoint could not be taken or the type is not back.
param(
    [Parameter(Mandatory = $true)][string]$VMName,
    [Parameter(Mandatory = $true)][string]$Baseline,
    [Parameter(Mandatory = $true)][string]$Name,
    [Parameter(Mandatory = $true)][string]$RunDir,
    [int]$SettleSeconds = 600
)
if ($RunDir -notmatch '^C:\\build\\guest\\nxg-[0-9]{8}T[0-9]{6}Z-[0-9]+$') { throw 'RunDir is not a run directory under C:\build\guest' }
if ($Name -notmatch '^stduser-signed-in-[0-9]{8}$') { throw 'Name is not stduser-signed-in-<8 digits>' }
if ($SettleSeconds -lt 0) { throw 'SettleSeconds is negative' }
$vm = $VMName
if ($vm -ne 'nx-clean-win11' -or $Baseline -ne 'clean-baseline-2026-10-06') { "NXG-RESULT: FAIL REFUSED: a signed-in checkpoint is taken only on nx-clean-win11 as a child of clean-baseline-2026-10-06, not on $vm from $Baseline"; exit 11 }
$snaps = @(Get-VMSnapshot -VMName $vm | ForEach-Object Name)
"snapshots before: " + ($snaps -join ', ')
$have = @($snaps | Where-Object { $_ -match '^stduser-signed-in-' })
if ($Name -eq $Baseline -or $have.Count -gt 0) { "NXG-RESULT: FAIL REFUSED: ONE signed-in checkpoint is allowed and the guest has $($have.Count) ($($have -join ', ')), or $Name is the baseline's name; none is replaced, and a rebuild needs Sam to delete the old one by hand"; exit 3 }
if ($snaps -notcontains $Baseline) { "NXG-RESULT: FAIL REFUSED: the guest has no baseline checkpoint named $Baseline"; exit 4 }
function Check-Guest {
    $v = Get-VM -Name $vm
    Write-Host "vm: state=$($v.State) parent=$($v.ParentCheckpointName) CheckpointType=$($v.CheckpointType)"
    if ($v.State -ne 'Running') { Write-Host "NXG-RESULT: FAIL REFUSED: the guest is $($v.State), not Running"; exit 5 }
    if ($v.ParentCheckpointName -ne $Baseline) { Write-Host "NXG-RESULT: FAIL REFUSED: the guest's checkpoint is $($v.ParentCheckpointName), not $Baseline"; exit 6 }
    $v
}
function Switch-Bytes {
    try { $a = Get-NetAdapterStatistics -Name 'vEthernet (Default Switch)' -ErrorAction Stop; "default switch bytes: sent=$($a.SentBytes) received=$($a.ReceivedBytes)" }
    catch { $m = ("" + $_.Exception.Message).Split("`n")[0].Trim(); "default switch bytes: unavailable ($m)" }
}
[void](Check-Guest)
"settling for $SettleSeconds s from $((Get-Date).ToUniversalTime().ToString('o'))"
$t0 = Get-Date; $nextReading = 0
while (((Get-Date) - $t0).TotalSeconds -lt $SettleSeconds) {
    if (((Get-Date) - $t0).TotalSeconds -ge $nextReading) { Switch-Bytes; $nextReading += 60 }
    Start-Sleep 10
}
Switch-Bytes
$v = Check-Guest
$was = "$($v.CheckpointType)"
"CheckpointType before: $was"
$typeFile = Join-Path $RunDir 'checkpoint-type.txt'
$readBack = $null
if ($was -match '^(Disabled|Production|ProductionOnly|Standard)$') {
    try { Set-Content -LiteralPath $typeFile -Value $was -Encoding ASCII -ErrorAction Stop; $readBack = ("" + (Get-Content -LiteralPath $typeFile -TotalCount 1 -ErrorAction Stop)).Trim() } catch { $readBack = $null }
}
if ($readBack -ne $was) { "NXG-RESULT: FAIL REFUSED: the guest's CheckpointType '$was' is unknown or could not be recorded in $typeFile; nothing is changed"; exit 12 }
$err = $null; $restoreErr = $null; $typeNotBack = $false
try {
    Set-VM -VMName $vm -CheckpointType Standard -ErrorAction Stop
    Checkpoint-VM -Name $vm -SnapshotName $Name -ErrorAction Stop
} catch { $err = ("" + $_.Exception.Message).Split("`n")[0].Trim() } finally {
    try { Set-VM -VMName $vm -CheckpointType $was -ErrorAction Stop } catch { $typeNotBack = $true; $restoreErr = ("" + $_.Exception.Message).Split("`n")[0].Trim() }
}
$now = "$((Get-VM -Name $vm).CheckpointType)"
"CheckpointType now: $now (was $was)"
if ($now -ne $was) { $typeNotBack = $true }
if ($typeNotBack -or $err) {
    $why = @(); if ($err) { $why += "the checkpoint was not taken: $err" }; if ($typeNotBack) { $why += "CheckpointType is $now, not back to ${was} (end.ps1 puts it back from $typeFile): $restoreErr" }
    "NXG-RESULT: FAIL " + ($why -join '; ')
    exit 7
}
$new = Get-VMSnapshot -VMName $vm -Name $Name -ErrorAction Stop
"checkpoint: name=$($new.Name) parent=$($new.ParentSnapshotName) type=$($new.SnapshotType) (a reading only) created=$($new.CreationTime.ToUniversalTime().ToString('o'))"
$after = @(Get-VMSnapshot -VMName $vm | ForEach-Object Name)
"snapshots after: " + ($after -join ', ')
if ($after -notcontains $Baseline) { "NXG-RESULT: FAIL the baseline checkpoint $Baseline is not in the list"; exit 10 }
if ($new.ParentSnapshotName -ne $Baseline) { "NXG-RESULT: FAIL the new checkpoint's parent is $($new.ParentSnapshotName), not $Baseline"; exit 8 }
'NXG-RESULT: ok'
