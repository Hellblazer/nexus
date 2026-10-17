# SPDX-License-Identifier: AGPL-3.0-or-later
#
# Host. stdin line 1 = guest account secret; never written anywhere.
# Runs one inner script of this run directory in the guest over PowerShell
# Direct, and with -Collect yes then copies the guest's run directory and logs
# (read only) to <RunDir>\out. The inner script's own result is its last
# "NXG-RESULT:" line. With no -Inner it only collects.
# -RunAs <account> (run-gate.sh --standard-user) points the session at that
# account's profile: the session is still the administrator's, but
# $env:USERPROFILE in it becomes C:\Users\<account>, so the inner scripts and
# the collection read and write that account's nx-gate folder, and
# $env:NXG_RUN_AS names the account for the scripts that start a task. With
# -AsUser yes the inner script is copied into that folder and run AS the
# account by as-user.ps1 instead of in this session.
param(
    [Parameter(Mandatory = $true)][string]$VMName,
    [Parameter(Mandatory = $true)][string]$GuestUser,
    [Parameter(Mandatory = $true)][string]$RunDir,
    [string]$Inner = '',
    # Up to three positional values for the inner script's param() block, one
    # token each: a list in one argument would not survive the ssh shell.
    [string]$Arg1 = '', [string]$Arg2 = '', [string]$Arg3 = '',
    [string]$Collect = '',
    [string]$RunAs = '',
    [string]$AsUser = ''
)
if ($RunDir -notmatch '^C:\\build\\guest\\nxg-[0-9]{8}T[0-9]{6}Z-[0-9]+$') { throw 'RunDir is not a run directory under C:\build\guest' }
if ($Inner -and $Inner -notmatch '^[a-z0-9-]+\.ps1$') { throw 'Inner must be a script name in the run directory' }
foreach ($a in @($Arg1, $Arg2, $Arg3)) { if ($a -notmatch '^[A-Za-z0-9._+-]*$') { throw 'an inner argument has a character outside A-Za-z0-9._+-' } }
if ($RunAs -and $RunAs -notmatch '^[A-Za-z0-9._-]+$') { throw 'RunAs must be a plain account name' }
if ($AsUser -eq 'yes' -and -not ($RunAs -and $Inner)) { throw '-AsUser yes needs -RunAs and -Inner' }
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
    $sess
}
$s = Open-GuestSession $VMName
try {
    if ($RunAs) {
        Invoke-Command -Session $s -ArgumentList $RunAs {
            param($u)
            if (-not (Test-Path "C:\Users\$u")) { throw "no profile for $u" }
            $env:USERPROFILE = "C:\Users\$u"; $env:NXG_RUN_AS = $u
        }
    }
    if ($Inner) {
        $innerArgs = @(@($Arg1, $Arg2, $Arg3) | Where-Object { $_ })
        if ($AsUser -eq 'yes') {
            $dest = Invoke-Command -Session $s { "$env:USERPROFILE\nx-gate" }
            Copy-Item -ToSession $s -Path (Join-Path $RunDir $Inner) -Destination "$dest\$Inner" -Force
            Invoke-Command -Session $s -FilePath (Join-Path $RunDir 'as-user.ps1') -ArgumentList (@($Inner) + $innerArgs)
        } else {
            Invoke-Command -Session $s -FilePath (Join-Path $RunDir $Inner) -ArgumentList $innerArgs
        }
    }
    if ($Collect -eq 'yes') {
        $out = Join-Path $RunDir 'out'
        New-Item -ItemType Directory -Force $out | Out-Null
        $files = Invoke-Command -Session $s {
            $b = "$env:USERPROFILE\nx-gate"
            @(Get-ChildItem "$b\run" -File -ErrorAction SilentlyContinue | ForEach-Object FullName) +
            @("$b\setup.log", "$b\nxgate-claude.log", "$b\GATERUN.txt", "$b\inst-versions.txt", "$b\asuser.log" | Where-Object { Test-Path $_ })
        }
        foreach ($f in $files) { Copy-Item -FromSession $s -Path $f -Destination $out -Force; "collected " + (Split-Path $f -Leaf) }
        # Every Claude Code session's hook records, read from the transcripts: one line per session and one per
        # record that is not a success (nexus-498xj: an intermittent exit 10 from `nx-hook rdr` that only a
        # session shows). The guest only reads; the text comes back as output and is written here, on the host.
        try {
            $records = Invoke-Command -Session $s {
                Add-Type -AssemblyName System.Web.Extensions; $J = New-Object System.Web.Script.Serialization.JavaScriptSerializer; $J.MaxJsonLength = [int]::MaxValue
                foreach ($t in @(Get-ChildItem "$env:USERPROFILE\.claude\projects" -Recurse -Filter *.jsonl -ErrorAction SilentlyContinue | Sort-Object CreationTimeUtc)) {
                    $total = 0; $bad = @()
                    foreach ($line in [System.IO.File]::ReadAllLines($t.FullName)) {
                        if ($line -notlike '*"hook_*') { continue }
                        try { $a = $J.DeserializeObject($line)['attachment'] } catch { continue }
                        if (-not $a -or "$($a['type'])" -notlike 'hook_*') { continue }
                        $total++
                        if ("$($a['type'])" -notin 'hook_success', 'hook_additional_context') { $bad += "  $($a['type']) event=$($a['hookEvent']) exit=$($a['exitCode']) ms=$($a['durationMs']) command=[$($a['command'])] stderr=[" + (("$($a['stderr'])" -replace '\s+', ' ').Trim()) + ']' }
                    }
                    "session $($t.BaseName) created=$($t.CreationTimeUtc.ToString('o')) hook records=$total not-success=$($bad.Count)"
                    $bad
                }
            }
            [System.IO.File]::WriteAllLines((Join-Path $out 'hook-records.txt'), [string[]]@($records))
            "collected hook-records.txt (" + @($records | Where-Object { $_ -like '  hook_*' }).Count + " not-success)"
        } catch { "hook records not collected: $($_.Exception.Message)" }
    }
} finally { Remove-PSSession $s }
