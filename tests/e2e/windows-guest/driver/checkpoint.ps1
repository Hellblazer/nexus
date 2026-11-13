# SPDX-License-Identifier: AGPL-3.0-or-later
#
# Host (the Hyper-V machine), no guest credential. Step 01k of run-gate.sh --build-signed-in (nexus-y3y8d):
# the one place the driver takes a checkpoint. The guest has just been restored to the baseline checkpoint
# and the standard user has signed in (the stduser step); after a settle period (-SettleSeconds, the
# run-gate.sh NX_GUEST_SETTLE_S, default 600) this takes ONE running checkpoint of that state, named
# -Name (stduser-signed-in-<8 digits>), so that run-gate.sh --signed-in resumes past the sign-in
# instead of paying for it every run (about 300 MiB of network a run).
#
# It refuses, before it touches anything, when -Name is not of that shape, is the baseline's name, or names
# a checkpoint that exists already; when the baseline named by -Baseline does not exist; when the guest is
# not Running; or when the guest's current checkpoint is not the baseline (the new one would then not be its
# child). It removes, renames and replaces no checkpoint, the baseline included. It records the guest's
# CheckpointType, sets it to Standard (a Production checkpoint has no memory state and would need the
# account's secret kept in the guest to sign in again), takes the checkpoint, and puts the recorded type back
# whatever happens. It then prints the new checkpoint's name, parent and type, and fails unless the parent is
# the baseline, the type is Standard and the baseline is still there. The guest holds no account secret: the
# stduser step removed it once the account was signed in. (This script reads and stores none.)
# Exit 3 a refused name, 4 no baseline, 5 the guest is not Running, 6 its checkpoint is not the baseline,
# 7 the checkpoint could not be taken or the type not restored, 8 the new checkpoint's parent is not the
# baseline, 9 its type is not Standard, 10 the baseline is gone.
param(
    [Parameter(Mandatory = $true)][string]$VMName,
    [Parameter(Mandatory = $true)][string]$Baseline,
    [Parameter(Mandatory = $true)][string]$Name,
    [int]$SettleSeconds = 600
)
if ($Name -notmatch '^stduser-signed-in-[0-9]{8}$') { throw 'Name is not stduser-signed-in-<8 digits>' }
if ($SettleSeconds -lt 0) { throw 'SettleSeconds is negative' }
$vm = $VMName
$snaps = @(Get-VMSnapshot -VMName $vm | ForEach-Object Name)
"snapshots before: " + ($snaps -join ', ')
if ($Name -eq $Baseline -or $snaps -contains $Name) { "NXG-RESULT: FAIL REFUSED: a checkpoint named $Name exists already, or is the baseline's name; none is replaced"; exit 3 }
if ($snaps -notcontains $Baseline) { "NXG-RESULT: FAIL REFUSED: the guest has no baseline checkpoint named $Baseline"; exit 4 }
function Check-Guest {
    $v = Get-VM -Name $vm
    Write-Host "vm: state=$($v.State) parent=$($v.ParentCheckpointName) CheckpointType=$($v.CheckpointType)"
    if ($v.State -ne 'Running') { Write-Host "NXG-RESULT: FAIL REFUSED: the guest is $($v.State), not Running"; exit 5 }
    if ($v.ParentCheckpointName -ne $Baseline) { Write-Host "NXG-RESULT: FAIL REFUSED: the guest's checkpoint is $($v.ParentCheckpointName), not $Baseline"; exit 6 }
    $v
}
[void](Check-Guest)
"settling for $SettleSeconds s from $((Get-Date).ToUniversalTime().ToString('o'))"
$t0 = Get-Date
while (((Get-Date) - $t0).TotalSeconds -lt $SettleSeconds) { Start-Sleep 10 }
$v = Check-Guest
$was = $v.CheckpointType
"CheckpointType before: $was"
$err = $null; $restoreErr = $null
try {
    Set-VM -VMName $vm -CheckpointType Standard -ErrorAction Stop
    Checkpoint-VM -Name $vm -SnapshotName $Name -ErrorAction Stop
} catch { $err = ("" + $_.Exception.Message).Split("`n")[0].Trim() } finally {
    try { Set-VM -VMName $vm -CheckpointType $was -ErrorAction Stop } catch { $restoreErr = ("" + $_.Exception.Message).Split("`n")[0].Trim() }
}
"CheckpointType now: $((Get-VM -Name $vm).CheckpointType) (was $was)"
if ($err) { "NXG-RESULT: FAIL the checkpoint was not taken: $err"; exit 7 }
if ($restoreErr) { "NXG-RESULT: FAIL the checkpoint $Name was taken, but CheckpointType was not put back to ${was}: $restoreErr"; exit 7 }
$new = Get-VMSnapshot -VMName $vm -Name $Name -ErrorAction Stop
"checkpoint: name=$($new.Name) parent=$($new.ParentSnapshotName) type=$($new.SnapshotType) created=$($new.CreationTime.ToUniversalTime().ToString('o'))"
$after = @(Get-VMSnapshot -VMName $vm | ForEach-Object Name)
"snapshots after: " + ($after -join ', ')
if ($after -notcontains $Baseline) { "NXG-RESULT: FAIL the baseline checkpoint $Baseline is not in the list"; exit 10 }
if ($new.ParentSnapshotName -ne $Baseline) { "NXG-RESULT: FAIL the new checkpoint's parent is $($new.ParentSnapshotName), not $Baseline"; exit 8 }
if ("$($new.SnapshotType)" -ne 'Standard') { "NXG-RESULT: FAIL the new checkpoint is of type $($new.SnapshotType), not Standard"; exit 9 }
'NXG-RESULT: ok'
