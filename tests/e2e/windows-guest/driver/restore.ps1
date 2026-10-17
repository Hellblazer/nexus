# SPDX-License-Identifier: AGPL-3.0-or-later
#
# Host. stdin line 1 = guest account secret; never written anywhere.
# Step 2 of run-gate.sh: restore the guest to the named checkpoint (and to no
# other, and take none), make sure it has a network adapter, start it, wait for
# the network, copy the gate script (and a candidate wheel) in, and parse the
# gate script under PowerShell 5.1 on the guest. Exit 3 guest not Off, 4 no
# network, 5 the gate script does not parse on the guest; over ssh these reach
# the Mac only as non-zero, so the NXG-RESULT line says which.
# "NXG-RESTORED: ok" is printed the moment the restore has succeeded and
# not before: run-gate.sh powers the guest off at the end only when it sees it,
# so a refusal above it never leaves this run owning a guest it did not restore.
param(
    [Parameter(Mandatory = $true)][string]$VMName,
    [Parameter(Mandatory = $true)][string]$GuestUser,
    [Parameter(Mandatory = $true)][string]$Checkpoint,
    [Parameter(Mandatory = $true)][string]$RunDir,
    [string]$Wheel = '',
    [ValidateSet('yes', 'no')][string]$Cache = 'yes'
)
if ($RunDir -notmatch '^C:\\build\\guest\\nxg-[0-9]{8}T[0-9]{6}Z-[0-9]+$') { throw 'RunDir is not a run directory under C:\build\guest' }
if ($Wheel -and $Wheel -notmatch '^[A-Za-z0-9._+-]+\.whl$') { throw 'Wheel is not a wheel file name' }
$pw = [Console]::In.ReadLine()
if (-not $pw) { throw 'nothing on stdin' }
# The characters go into the SecureString one by one: ConvertTo-SecureString -AsPlainText would put the
# plain text in a cmdlet parameter binding, which PowerShell module logging records when it is on.
$sec = New-Object System.Security.SecureString; foreach ($c in $pw.ToCharArray()) { $sec.AppendChar($c) }; $sec.MakeReadOnly(); $pw = $null; $c = $null
$cred = New-Object PSCredential($GuestUser, $sec)
# guest-session: the same block in every script that opens a session (tests/test_windows_gate_driver.py).
# Seconds after the guest restarted and an account signed in at the console, PowerShell Direct has failed to
# open a session: "The credential is invalid" with the right password (nexus-bpzt7, cycles 1 and B of
# 2026-10-16; not a lockout) and "A remote session might have ended" (the rerun of 2026-10-17). Causes unknown.
# A failed open is retried every 10 s for 2 minutes while the guest is Running; a guest that is not Running
# stops the script at once. After a failure, the guest's failed sign-ins (events 4625 and 4776) from a minute
# before the first one are printed.
# It then sets NX_NO_TELEMETRY=1 in the guest, in the session and Machine-wide: a restored guest is a fresh
# install, and every run used to send conexus an install ping (nexus-ca5rw). The scheduled-task payloads
# set it too, since an Interactive task joins a signed-in session whose environment predates this.
# Everything it prints goes through Write-Host: its pipeline output is its return value, the session.
function Open-GuestSession($Name) {
    $since = $null
    for ($i = 1; ; $i++) {
        try { $sess = New-PSSession -VMName $Name -Credential $cred -ErrorAction Stop; break }
        catch {
            if ($i -gt 12 -or (Get-VM -Name $Name).State -ne 'Running') { throw }
            if (-not $since) { $since = (Get-Date).AddMinutes(-1) }
            $why = ("" + $_.Exception.Message).Split("`n")[0].Trim()
            Write-Host "guest session open failed at $((Get-Date).ToUniversalTime().ToString('HH:mm:ss')) ($why); retry $i of 12 in 10 s"
            Start-Sleep 10
        }
    }
    if ($since) {
        $ev = @(Invoke-Command -Session $sess -ArgumentList $since {
            param($t)
            $fields = '^\s*(Account Name|Account For Which Logon Failed|Logon Account|Failure Reason|Status|Sub Status|Error Code|Logon Type|Caller Process Name|Source Workstation):'
            Get-WinEvent -FilterHashtable @{ LogName = 'Security'; Id = 4625, 4776; StartTime = $t } -ErrorAction SilentlyContinue | ForEach-Object {
                $keep = @($_.Message -split "`r?`n" | Where-Object { $_ -match $fields } | ForEach-Object { $_.Trim() -replace '\s+', ' ' })
                $_.TimeCreated.ToUniversalTime().ToString('HH:mm:ss') + " event $($_.Id): " + ($keep -join '; ')
            }
        })
        Write-Host "guest failed sign-ins since $($since.ToUniversalTime().ToString('HH:mm:ss')): $($ev.Count)"
        foreach ($e in $ev) { Write-Host "  $e" }
    }
    Invoke-Command -Session $sess {
        [Environment]::SetEnvironmentVariable('NX_NO_TELEMETRY', '1', 'Machine'); $env:NX_NO_TELEMETRY = '1'
    } | Out-Null
    $sess
}
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
'NXG-RESTORED: ok'
"restored to $cp at $((Get-Date).ToUniversalTime().ToString('o'))"
# The restored configuration (no NIC in the checkpoint) can become visible late: sample it for 15 s first.
for ($i = 0; $i -lt 5; $i++) { "nic count ${i}x3s after the restore = $(Nics)"; Start-Sleep 3 }
EnsureNic 'after the restore, VM Off'
Start-VM -Name $vm -ErrorAction Stop
"started at $((Get-Date).ToUniversalTime().ToString('o'))"
Start-Sleep 5
EnsureNic 'after Start-VM'
# nexus-bpzt7 (Sam, 2026-10-15): every restore is a fresh Windows that starts its own update downloads, on a
# host shared with other work. Unless -Cache no, the first time the guest answers over PowerShell Direct:
# stop and disable Windows Update, its orchestrator and Delivery Optimization, set the policies that keep
# them from coming back, and stop Defender's signature updates (the gate does not depend on them). Inside
# the guest only: no host setting changes, no checkpoint is taken, and the next restore discards all of it.
# Disabled services stay disabled through the stduser step's restart. Delivery Optimization off can break
# winget, which only the administrator-Git runs use.
$quietBlock = {
    $pol = 'HKLM:\SOFTWARE\Policies\Microsoft\Windows'
    New-Item -Force "$pol\WindowsUpdate\AU" | Out-Null
    Set-ItemProperty "$pol\WindowsUpdate\AU" NoAutoUpdate 1 -Type DWord
    New-Item -Force "$pol\DeliveryOptimization" | Out-Null
    Set-ItemProperty "$pol\DeliveryOptimization" DODownloadMode 99 -Type DWord
    New-Item -Force 'HKLM:\SOFTWARE\Policies\Microsoft\WindowsStore' | Out-Null
    Set-ItemProperty 'HKLM:\SOFTWARE\Policies\Microsoft\WindowsStore' AutoDownload 2 -Type DWord
    foreach ($n in 'wuauserv', 'UsoSvc', 'DoSvc') {
        try { Set-Service -Name $n -StartupType Disabled -ErrorAction Stop }
        catch { Set-ItemProperty "HKLM:\SYSTEM\CurrentControlSet\Services\$n" Start 4 -Type DWord -ErrorAction SilentlyContinue }
        try { Stop-Service -Name $n -Force -ErrorAction Stop } catch { "quiet-updates: $n not stopped: $($_.Exception.Message)" }
        $start = (Get-ItemProperty "HKLM:\SYSTEM\CurrentControlSet\Services\$n" -ErrorAction SilentlyContinue).Start
        "quiet-updates: $n " + (Get-Service -Name $n).Status + " start=$start"
    }
    try { Set-MpPreference -SignatureScheduleDay 8 -SignatureUpdateInterval 0 -SignatureDisableUpdateOnStartupWithoutEngine $true -ErrorAction Stop; 'quiet-updates: Defender signature updates off' }
    catch { "quiet-updates: Defender signature updates not changed: $($_.Exception.Message)" }
    'quiet-updates: applied at ' + (Get-Date).ToUniversalTime().ToString('o')
}
$quieted = $Cache -ne 'yes'
if ($quieted) { "quiet-updates: not with -Cache $Cache" }
$ok = $false
for ($i = 0; $i -lt 60; $i++) {
    if ($i -gt 0 -and ($i % 6) -eq 0 -and (Nics) -eq 0) { EnsureNic "in the network wait, iteration $i" }
    try {
        if (-not $quieted) { Invoke-Command -VMName $vm -Credential $cred -ErrorAction Stop $quietBlock; $quieted = $true }
        # HEAD only: https://pypi.org/simple/ is the whole package index (47 MB on 2026-10-15), and a GET that
        # timed out was fetched again every 5 s; one slow boot spent about 0.9 GB here (nexus-bpzt7).
        $r = Invoke-Command -VMName $vm -Credential $cred -ErrorAction Stop { try { (Invoke-WebRequest https://pypi.org/simple/ -Method Head -UseBasicParsing -TimeoutSec 10).StatusCode } catch { 0 } }
        if ($r -eq 200) { $ok = $true; break }
    } catch {}
    Start-Sleep 5
}
"booted+net=$ok after $([int]((Get-Date)-$t0).TotalSeconds)s"
$v = Get-VM -Name $vm
"after: state=$($v.State) parent=$($v.ParentCheckpointName) nics=$(Nics)"
if (-not $ok) { 'NXG-RESULT: FAIL the guest has no network'; exit 4 }
$rc = 0
$s = Open-GuestSession $vm
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
