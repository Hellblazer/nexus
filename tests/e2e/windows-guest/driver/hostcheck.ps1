# SPDX-License-Identifier: AGPL-3.0-or-later
#
# Host (the Hyper-V machine), no guest credential. Step 1 of run-gate.sh: reads
# the host and the guest's state, parses every staged script under Windows
# PowerShell 5.1, and refuses before anything is touched. Exit 0 and a final
# "NXG-RESULT: ok" line, or a refusal: 3 guest not Off, 5 a parse error,
# 6 the named checkpoint does not exist. Over ssh the exit code reaches the Mac
# only as non-zero; the NXG-RESULT line says which.
# -Checkpoint never names a signed-in checkpoint (^stduser-signed-in-), in any mode: 11. Only -SignedIn does.
# -SignedIn NAME (run-gate.sh --signed-in, nexus-y3y8d) also refuses unless the guest has a checkpoint
# named NAME and its parent is the checkpoint named by -Checkpoint (the baseline): 7 no such checkpoint,
# 8 its parent is another. Its type is printed, as a reading only: the baseline is also Standard, so the type
# cannot tell a running checkpoint from it (restore.ps1 -Resume yes tests that, by the guest being Saved).
# -NewCheckpoint NAME (run-gate.sh --build-signed-in) holds Sam's exception of 2026-11-13, ONE running child
# checkpoint of nx-clean-win11's baseline, and refuses, before the restore and the sign-in cost anything:
# 11 unless -VMName is nx-clean-win11 and -Checkpoint is clean-baseline-2026-10-06; 10 when NAME is the
# baseline's name or ANY checkpoint matching ^stduser-signed-in- exists already (a rebuild needs Sam to delete
# the old one by hand: no script does); 12 when Set-VM has no -CheckpointType or Checkpoint-VM no -SnapshotName
# (read from the cmdlets' parameter lists; nothing is changed). It prints the VM's CheckpointType.
param(
    [Parameter(Mandatory = $true)][string]$VMName,
    [Parameter(Mandatory = $true)][string]$Checkpoint,
    [Parameter(Mandatory = $true)][string]$RunDir,
    [string]$SignedIn = '',
    [string]$NewCheckpoint = ''
)
if ($RunDir -notmatch '^C:\\build\\guest\\nxg-[0-9]{8}T[0-9]{6}Z-[0-9]+$') { throw 'RunDir is not a run directory under C:\build\guest' }
foreach ($n in $SignedIn, $NewCheckpoint) { if ($n -and $n -notmatch '^stduser-signed-in-[0-9]{8}$') { throw 'a signed-in checkpoint name is stduser-signed-in-<8 digits>' } }
if ($Checkpoint -match '^stduser-signed-in-') { "NXG-RESULT: FAIL REFUSED: -Checkpoint $Checkpoint is a signed-in checkpoint; the baseline is never one (only -SignedIn names it)"; exit 11 }
"now utc " + (Get-Date).ToUniversalTime().ToString('o')
$v = Get-VM -Name $VMName
"vm: state=$($v.State) parent=$($v.ParentCheckpointName) CheckpointType=$($v.CheckpointType) nics=$(@(Get-VMNetworkAdapter -VMName $VMName).Count)"
$snapObjs = @(Get-VMSnapshot -VMName $VMName)
$snaps = @($snapObjs | ForEach-Object Name)
"snapshots: " + ($snaps -join ', ')
"run dir: " + ((Get-ChildItem -LiteralPath $RunDir -Recurse -Force -ErrorAction SilentlyContinue | ForEach-Object { $_.FullName.Substring($RunDir.Length) }) -join ', ')
"Runner.Worker count=" + @(Get-Process Runner.Worker -ErrorAction SilentlyContinue).Count
"host PSVersion=" + $PSVersionTable.PSVersion.ToString()
$parseErrors = 0
foreach ($f in @(Get-ChildItem -LiteralPath $RunDir -Filter '*.ps1' -File) + @(Get-ChildItem -LiteralPath (Join-Path $RunDir 'stage') -Filter '*.ps1' -File -ErrorAction SilentlyContinue)) {
    $errs = $null
    [void][System.Management.Automation.Language.Parser]::ParseFile($f.FullName, [ref]$null, [ref]$errs)
    $parseErrors += @($errs).Count
    "{0}: {1} parse error(s) sha256={2}" -f $f.Name, @($errs).Count, (Get-FileHash -LiteralPath $f.FullName).Hash.ToLower()
    $errs | ForEach-Object { "  line {0} col {1}: {2}" -f $_.Extent.StartLineNumber, $_.Extent.StartColumnNumber, $_.Message }
}
if ($v.State -ne 'Off') { 'NXG-RESULT: FAIL REFUSED: the guest is not Off; not touching it'; exit 3 }
if ($snaps -notcontains $Checkpoint) { "NXG-RESULT: FAIL REFUSED: the guest has no checkpoint named $Checkpoint"; exit 6 }
if ($SignedIn) {
    $sn = @($snapObjs | Where-Object { $_.Name -eq $SignedIn })
    if ($sn.Count -ne 1) { "NXG-RESULT: FAIL REFUSED: the guest has no checkpoint named $SignedIn"; exit 7 }
    "signed-in checkpoint: name=$($sn[0].Name) parent=$($sn[0].ParentSnapshotName) type=$($sn[0].SnapshotType) (a reading only)"
    if ($sn[0].ParentSnapshotName -ne $Checkpoint) { "NXG-RESULT: FAIL REFUSED: $SignedIn is not a child of $Checkpoint (its parent is $($sn[0].ParentSnapshotName))"; exit 8 }
}
if ($NewCheckpoint) {
    if ($VMName -ne 'nx-clean-win11' -or $Checkpoint -ne 'clean-baseline-2026-10-06') { "NXG-RESULT: FAIL REFUSED: a signed-in checkpoint is built only on nx-clean-win11 as a child of clean-baseline-2026-10-06, not on $VMName from $Checkpoint"; exit 11 }
    $have = @($snaps | Where-Object { $_ -match '^stduser-signed-in-' })
    if ($NewCheckpoint -eq $Checkpoint -or $have.Count -gt 0) { "NXG-RESULT: FAIL REFUSED: ONE signed-in checkpoint is allowed and the guest has $($have.Count) ($($have -join ', ')), or $NewCheckpoint is the baseline's name; none is replaced, and a rebuild needs Sam to delete the old one by hand"; exit 10 }
    $setVm = Get-Command Set-VM -ErrorAction SilentlyContinue
    $takeVm = Get-Command Checkpoint-VM -ErrorAction SilentlyContinue
    "cmdlets: Set-VM -CheckpointType=$([bool]($setVm -and $setVm.Parameters.ContainsKey('CheckpointType'))) Checkpoint-VM -SnapshotName=$([bool]($takeVm -and $takeVm.Parameters.ContainsKey('SnapshotName')))"
    if (-not ($setVm -and $setVm.Parameters.ContainsKey('CheckpointType')) -or -not ($takeVm -and $takeVm.Parameters.ContainsKey('SnapshotName'))) { 'NXG-RESULT: FAIL REFUSED: Set-VM has no -CheckpointType or Checkpoint-VM has no -SnapshotName on this host'; exit 12 }
}
if ($parseErrors -gt 0) { "NXG-RESULT: FAIL $parseErrors parse error(s) in the staged scripts under Windows PowerShell 5.1"; exit 5 }
'NXG-RESULT: ok'
