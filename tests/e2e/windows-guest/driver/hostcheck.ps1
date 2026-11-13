# SPDX-License-Identifier: AGPL-3.0-or-later
#
# Host (the Hyper-V machine), no guest credential. Step 1 of run-gate.sh: reads
# the host and the guest's state, parses every staged script under Windows
# PowerShell 5.1, and refuses before anything is touched. Exit 0 and a final
# "NXG-RESULT: ok" line, or a refusal: 3 guest not Off, 5 a parse error,
# 6 the named checkpoint does not exist. Over ssh the exit code reaches the Mac
# only as non-zero; the NXG-RESULT line says which.
# -SignedIn NAME (run-gate.sh --signed-in, nexus-y3y8d) also refuses unless the guest has a checkpoint
# named NAME, its parent is the checkpoint named by -Checkpoint (the baseline), and its type is Standard:
# 7 no such checkpoint, 8 its parent is another, 9 its type is not Standard.
# -NewCheckpoint NAME (run-gate.sh --build-signed-in) refuses, before the restore and the sign-in cost
# anything, when NAME is the baseline's name or a checkpoint of that name already exists: 10.
param(
    [Parameter(Mandatory = $true)][string]$VMName,
    [Parameter(Mandatory = $true)][string]$Checkpoint,
    [Parameter(Mandatory = $true)][string]$RunDir,
    [string]$SignedIn = '',
    [string]$NewCheckpoint = ''
)
if ($RunDir -notmatch '^C:\\build\\guest\\nxg-[0-9]{8}T[0-9]{6}Z-[0-9]+$') { throw 'RunDir is not a run directory under C:\build\guest' }
foreach ($n in $SignedIn, $NewCheckpoint) { if ($n -and $n -notmatch '^stduser-signed-in-[0-9]{8}$') { throw 'a signed-in checkpoint name is stduser-signed-in-<8 digits>' } }
"now utc " + (Get-Date).ToUniversalTime().ToString('o')
$v = Get-VM -Name $VMName
"vm: state=$($v.State) parent=$($v.ParentCheckpointName) nics=$(@(Get-VMNetworkAdapter -VMName $VMName).Count)"
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
    "signed-in checkpoint: name=$($sn[0].Name) parent=$($sn[0].ParentSnapshotName) type=$($sn[0].SnapshotType)"
    if ($sn[0].ParentSnapshotName -ne $Checkpoint) { "NXG-RESULT: FAIL REFUSED: $SignedIn is not a child of $Checkpoint (its parent is $($sn[0].ParentSnapshotName))"; exit 8 }
    if ("$($sn[0].SnapshotType)" -ne 'Standard') { "NXG-RESULT: FAIL REFUSED: $SignedIn is of type $($sn[0].SnapshotType), not Standard"; exit 9 }
}
if ($NewCheckpoint) {
    if ($NewCheckpoint -eq $Checkpoint -or $snaps -contains $NewCheckpoint) { "NXG-RESULT: FAIL REFUSED: a checkpoint named $NewCheckpoint exists already, or is the baseline's name; none is replaced"; exit 10 }
}
if ($parseErrors -gt 0) { "NXG-RESULT: FAIL $parseErrors parse error(s) in the staged scripts under Windows PowerShell 5.1"; exit 5 }
'NXG-RESULT: ok'
