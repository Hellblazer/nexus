# SPDX-License-Identifier: AGPL-3.0-or-later
#
# Host. stdin line 1 = guest account secret; never written anywhere.
# Step 2 of run-gate.sh: restore the guest to the named checkpoint (and to no
# other, and take none), make sure it has a network adapter, start it, wait for
# the network, copy the gate script (and a candidate wheel) in, and parse the
# gate script under PowerShell 5.1 on the guest. Exit 3 guest not Off, 4 no
# network, 5 the gate script does not parse on the guest.
param(
    [Parameter(Mandatory = $true)][string]$VMName,
    [Parameter(Mandatory = $true)][string]$GuestUser,
    [Parameter(Mandatory = $true)][string]$Checkpoint,
    [Parameter(Mandatory = $true)][string]$RunDir,
    [string]$Wheel = ''
)
if ($RunDir -notmatch '^C:\\build\\guest\\nxg-[0-9]{8}T[0-9]{6}Z-[0-9]+$') { throw 'RunDir is not a run directory under C:\build\guest' }
if ($Wheel -and $Wheel -notmatch '^[A-Za-z0-9._+-]+\.whl$') { throw 'Wheel is not a wheel file name' }
$pw = [Console]::In.ReadLine()
if (-not $pw) { throw 'nothing on stdin' }
$cred = New-Object PSCredential($GuestUser, (ConvertTo-SecureString $pw -AsPlainText -Force)); $pw = $null
$vm = $VMName; $cp = $Checkpoint; $t0 = Get-Date
function Nics { @(Get-VMNetworkAdapter -VMName $vm).Count }
function EnsureNic($when) {
    $n = Nics
    "nic count $when = $n"
    if ($n -eq 0) { Add-VMNetworkAdapter -VMName $vm -SwitchName 'Default Switch' -ErrorAction Stop; "added Default Switch NIC ($when); nic count now $(Nics)" }
}
$v = Get-VM -Name $vm
"before: state=$($v.State) parent=$($v.ParentCheckpointName) nics=$(Nics)"
if ($v.State -ne 'Off') { "NXG-RESULT: FAIL REFUSED: guest is not Off (as found it was Off); not touching it"; exit 3 }
Restore-VMSnapshot -VMName $vm -Name $cp -Confirm:$false -ErrorAction Stop
"restored to $cp at $((Get-Date).ToUniversalTime().ToString('o'))"
# The restored configuration (no NIC in the checkpoint) can become visible late: sample it for 15 s first.
for ($i = 0; $i -lt 5; $i++) { "nic count ${i}x3s after the restore = $(Nics)"; Start-Sleep 3 }
EnsureNic 'after the restore, VM Off'
Start-VM -Name $vm -ErrorAction Stop
"started at $((Get-Date).ToUniversalTime().ToString('o'))"
Start-Sleep 5
EnsureNic 'after Start-VM'
$ok = $false
for ($i = 0; $i -lt 60; $i++) {
    if ($i -gt 0 -and ($i % 6) -eq 0 -and (Nics) -eq 0) { EnsureNic "in the network wait, iteration $i" }
    try {
        $r = Invoke-Command -VMName $vm -Credential $cred -ErrorAction Stop { try { (Invoke-WebRequest https://pypi.org/simple/ -UseBasicParsing -TimeoutSec 10).StatusCode } catch { 0 } }
        if ($r -eq 200) { $ok = $true; break }
    } catch {}
    Start-Sleep 5
}
"booted+net=$ok after $([int]((Get-Date)-$t0).TotalSeconds)s"
$v = Get-VM -Name $vm
"after: state=$($v.State) parent=$($v.ParentCheckpointName) nics=$(Nics)"
if (-not $ok) { 'NXG-RESULT: FAIL the guest has no network'; exit 4 }
$rc = 0
$s = New-PSSession -VMName $vm -Credential $cred -ErrorAction Stop
try {
    $b = Invoke-Command -Session $s { New-Item -ItemType Directory -Force "$env:USERPROFILE\nx-gate" | Out-Null; "$env:USERPROFILE\nx-gate" }
    Copy-Item -ToSession $s -Path (Join-Path $RunDir 'stage\windows-phase5-gate.ps1') -Destination "$b\windows-phase5-gate.ps1" -Force
    if ($Wheel) { Copy-Item -ToSession $s -Path (Join-Path $RunDir "stage\$Wheel") -Destination "$b\$Wheel" -Force }
    $info = Invoke-Command -Session $s -ArgumentList $Wheel {
        param($Wheel)
        query user 2>&1 | Out-String
        "PSVersion=" + $PSVersionTable.PSVersion.ToString()
        $os = Get-CimInstance Win32_OperatingSystem; "os=" + $os.Caption + " build " + $os.BuildNumber
        "nexus cfg=" + (Test-Path "$env:USERPROFILE\.config\nexus")
        "uv=" + (Test-Path "$env:USERPROFILE\.local\bin\uv.exe")
        "claude=" + (Test-Path "$env:USERPROFILE\.local\bin\claude.exe")
        "gitexe=" + (Test-Path 'C:\Program Files\Git\cmd\git.exe')
        "lastboot=" + (Get-CimInstance Win32_OperatingSystem).LastBootUpTime.ToUniversalTime().ToString('o')
        "nx-gate contents: " + ((Get-ChildItem "$env:USERPROFILE\nx-gate" -Force | ForEach-Object Name) -join ', ')
        if ($Wheel) { "wheel sha256=" + (Get-FileHash -Algorithm SHA256 -LiteralPath "$env:USERPROFILE\nx-gate\$Wheel").Hash.ToLowerInvariant() }
        $f = "$env:USERPROFILE\nx-gate\windows-phase5-gate.ps1"
        "gate sha256=" + (Get-FileHash -Algorithm SHA256 -LiteralPath $f).Hash.ToLowerInvariant()
        $errs = $null; $toks = $null
        [void][System.Management.Automation.Language.Parser]::ParseFile($f, [ref]$toks, [ref]$errs)
        "PARSE: {0} error(s), {1} tokens" -f @($errs).Count, @($toks).Count
        $errs | ForEach-Object { "  line {0} col {1}: {2}" -f $_.Extent.StartLineNumber, $_.Extent.StartColumnNumber, $_.Message }
    }
    $info
    $pe = @($info | ForEach-Object { if ("$_" -match '^PARSE: ([0-9]+) error') { [int]$Matches[1] } })
    if ($pe.Count -ne 1 -or $pe[0] -ne 0) { 'NXG-RESULT: FAIL the gate script does not parse under Windows PowerShell 5.1 on the guest'; $rc = 5 }
} finally { Remove-PSSession $s }
if ($rc -ne 0) { exit $rc }
'NXG-RESULT: ok'
