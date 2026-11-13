# SPDX-License-Identifier: AGPL-3.0-or-later
#
# Host, no guest credential. The last step of run-gate.sh: shut the guest down
# (this script takes, deletes and renames no checkpoint) unless -KeepGuest, then remove
# every host file this run created by its exact name, then the run's
# directories if they are empty. Removes this script last. "NXG-RESULT: ok"
# only when the run directory is gone.
# CheckpointType (nexus-y3y8d): run-gate.sh --build-signed-in's checkpoint.ps1 records the guest's
# CheckpointType in <RunDir>\checkpoint-type.txt before it sets Standard. This script runs on every exit of
# the run (a failed step, a signal, a dropped ssh session), so when that file is present it puts the guest's
# CheckpointType back to the recorded value: the one Set-VM here sets the recorded value and nothing else,
# and only when the guest's type differs. It reads the type again, and the result is FAIL, with the file left
# in place, unless the type equals the recorded one.
param(
    [Parameter(Mandatory = $true)][string]$VMName,
    [Parameter(Mandatory = $true)][string]$RunDir,
    [string]$Wheel = '',
    [switch]$KeepGuest
)
if ($RunDir -notmatch '^C:\\build\\guest\\nxg-[0-9]{8}T[0-9]{6}Z-[0-9]+$') { throw 'RunDir is not a run directory under C:\build\guest' }
if ($Wheel -and $Wheel -notmatch '^[A-Za-z0-9._+-]+\.whl$') { throw 'Wheel is not a wheel file name' }
$vm = $VMName
"snapshots before: " + ((Get-VMSnapshot -VMName $vm | ForEach-Object Name) -join ', ')
if ($KeepGuest) { 'guest left as it is (-KeepGuest)' }
elseif ((Get-VM -Name $vm).State -eq 'Off') { 'guest is already Off' }
else { Stop-VM -Name $vm -Force -ErrorAction Continue }
$v = Get-VM -Name $vm
"vm: state=$($v.State) parent=$($v.ParentCheckpointName) CheckpointType=$($v.CheckpointType) nics=$(@(Get-VMNetworkAdapter -VMName $vm).Count)"
"snapshots after: " + ((Get-VMSnapshot -VMName $vm | ForEach-Object Name) -join ', ')
$typeFile = Join-Path $RunDir 'checkpoint-type.txt'
$typeFailed = $false
if (Test-Path -LiteralPath $typeFile) {
    $recorded = ("" + (Get-Content -LiteralPath $typeFile -TotalCount 1 -ErrorAction SilentlyContinue)).Trim()
    if ($recorded -notmatch '^(Disabled|Production|ProductionOnly|Standard)$') { "CheckpointType: the recorded value in $typeFile is not a type name; not setting anything"; $typeFailed = $true }
    else {
        $cur = "$((Get-VM -Name $vm).CheckpointType)"
        if ($cur -ne $recorded) {
            try { Set-VM -VMName $vm -CheckpointType $recorded -ErrorAction Stop; "CheckpointType put back to $recorded (it was $cur)" }
            catch { $m = ("" + $_.Exception.Message).Split("`n")[0].Trim(); "CheckpointType could not be put back to ${recorded}: $m" }
        }
        $now = "$((Get-VM -Name $vm).CheckpointType)"
        "CheckpointType: recorded=$recorded now=$now"
        if ($now -ne $recorded) { $typeFailed = $true }
    }
}
# Exact names only: the run's own scripts, its staged files, the files collected
# from the guest (named as collected, one by one), and the two files launch.sh
# staged under nxgate\stage. launch.sh's helper scripts in nxgate stay.
$mine = @('hostcheck.ps1', 'restore.ps1', 'guest-run.ps1', 'inst.ps1', 'setup-run.ps1', 'gaterun.ps1', 'waitv.ps1', 'stduser.ps1', 'as-user.ps1', 'cache.ps1', 'checkpoint.ps1', 'end.ps1') | ForEach-Object { Join-Path $RunDir $_ }
$mine += Join-Path $RunDir 'portable-git.ps1'
$mine += Join-Path $RunDir 'stage\windows-phase5-gate.ps1'
# The recorded type stays when it could not be applied, as the evidence and for a hand fix.
if (-not $typeFailed) { $mine += Join-Path $RunDir 'checkpoint-type.txt' }
if ($Wheel) { $mine += Join-Path $RunDir "stage\$Wheel" }
foreach ($f in @(Get-ChildItem -LiteralPath (Join-Path $RunDir 'out') -File -Force -ErrorAction SilentlyContinue)) { $mine += $f.FullName }
$mine += 'C:\build\guest\nxgate\stage\claude-args.txt'
$mine += 'C:\build\guest\nxgate\stage\windows-phase5-gate.ps1'
foreach ($p in $mine) { if (Test-Path -LiteralPath $p) { Remove-Item -LiteralPath $p -Force }; "{0} gone={1}" -f $p, (-not (Test-Path -LiteralPath $p)) }
foreach ($d in @((Join-Path $RunDir 'stage'), (Join-Path $RunDir 'out'), $RunDir)) {
    $left = @(Get-ChildItem -LiteralPath $d -Force -ErrorAction SilentlyContinue).Count
    if ($left -eq 0 -and (Test-Path -LiteralPath $d)) { Remove-Item -LiteralPath $d -Force }
    "{0} dir gone={1} (left inside: {2})" -f $d, (-not (Test-Path -LiteralPath $d)), $left
}
"nxgate\stage contents: " + ((Get-ChildItem 'C:\build\guest\nxgate\stage' -Force -ErrorAction SilentlyContinue | ForEach-Object Name) -join ', ')
"Runner.Worker count=" + @(Get-Process Runner.Worker -ErrorAction SilentlyContinue).Count
if ($typeFailed) { "NXG-RESULT: FAIL the guest's CheckpointType is not back to the recorded value in $typeFile" }
elseif (Test-Path -LiteralPath $RunDir) { 'NXG-RESULT: FAIL the run directory was not emptied' } else { 'NXG-RESULT: ok' }
