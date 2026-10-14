# SPDX-License-Identifier: AGPL-3.0-or-later
#
# Host, no guest credential. The last step of run-gate.sh: shut the guest down
# (no checkpoint is taken, deleted or renamed) unless -KeepGuest, then remove
# every host file this run created by its exact name, then the run's
# directories if they are empty. Removes this script last. "NXG-RESULT: ok"
# only when the run directory is gone.
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
"vm: state=$($v.State) parent=$($v.ParentCheckpointName) nics=$(@(Get-VMNetworkAdapter -VMName $vm).Count)"
"snapshots after: " + ((Get-VMSnapshot -VMName $vm | ForEach-Object Name) -join ', ')
# Exact names only: the run's own scripts, its staged files, the files collected
# from the guest (named as collected, one by one), and the two files launch.sh
# staged under nxgate\stage. launch.sh's helper scripts in nxgate stay.
$mine = @('hostcheck.ps1', 'restore.ps1', 'guest-run.ps1', 'inst.ps1', 'setup-run.ps1', 'gaterun.ps1', 'waitv.ps1', 'stduser.ps1', 'as-user.ps1', 'end.ps1') | ForEach-Object { Join-Path $RunDir $_ }
$mine += Join-Path $RunDir 'portable-git.ps1'
$mine += Join-Path $RunDir 'stage\windows-phase5-gate.ps1'
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
if (Test-Path -LiteralPath $RunDir) { 'NXG-RESULT: FAIL the run directory was not emptied' } else { 'NXG-RESULT: ok' }
