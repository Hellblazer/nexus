# SPDX-License-Identifier: AGPL-3.0-or-later
#
# Guest (sent by guest-run.ps1 -RunAs <account> -AsUser yes over PowerShell
# Direct). Runs one script of the account's nx-gate folder AS that account, in
# its console session, through a one-shot Interactive scheduled task, and waits
# at most 25 minutes for it. The orchestrating session is the administrator's;
# only the task runs as the standard user. The wrapper records who it ran as and
# whether that token is a member of Administrators, and this script fails the
# step unless it was the named account and not a member.
# The inner script's own "NXG-RESULT:" line is the step's result; on a failure
# of this script the inner lines are printed with a prefix so that an inner
# "ok" cannot be read as the step's.
param(
    [Parameter(Mandatory = $true)][string]$Script,
    [string]$A1 = '', [string]$A2 = ''
)
$u = $env:NXG_RUN_AS
if (-not $u) { 'NXG-RESULT: FAIL as-user.ps1 needs the run-as account (guest-run.ps1 -RunAs)'; return }
if ($Script -notmatch '^[a-z0-9-]+\.ps1$') { 'NXG-RESULT: FAIL Script must be a script name'; return }
foreach ($a in @($A1, $A2)) { if ($a -notmatch '^[A-Za-z0-9._+-]*$') { 'NXG-RESULT: FAIL an argument has a character outside A-Za-z0-9._+-'; return } }
$b = "$env:USERPROFILE\nx-gate"
$argText = (@($A1, $A2) | Where-Object { $_ }) -join ' '
(@'
$host.UI.RawUI.WindowTitle = 'Phase 5 gate: @SCRIPT@ (standard user)'
$log = "$env:USERPROFILE\nx-gate\asuser.log"
$member = [bool](@(whoami /groups) -match 'S-1-5-32-544')
"asuser start user=$env:USERNAME administrators-member=$member" | Tee-Object -FilePath $log
& powershell.exe -NoProfile -ExecutionPolicy Bypass -File "$env:USERPROFILE\nx-gate\@SCRIPT@" @ARGS@ *>&1 | Tee-Object -Append -FilePath $log
"asuser exit=$LASTEXITCODE" | Tee-Object -Append -FilePath $log
'@).Replace('@SCRIPT@', $Script).Replace('@ARGS@', $argText) | Set-Content -Encoding UTF8 "$b\asuser-wrapper.ps1"
Remove-Item "$b\asuser.log" -ErrorAction SilentlyContinue
$act = New-ScheduledTaskAction -Execute 'powershell.exe' -Argument "-NoProfile -ExecutionPolicy Bypass -File `"$b\asuser-wrapper.ps1`""
Register-ScheduledTask -TaskName 'nxgate-asuser' -Action $act -Principal (New-ScheduledTaskPrincipal -UserId $u -LogonType Interactive -RunLevel Limited) -Force | Out-Null
Start-ScheduledTask -TaskName 'nxgate-asuser'
$t0 = Get-Date
do { Start-Sleep 10; $l = Get-Content "$b\asuser.log" -ErrorAction SilentlyContinue } while ((-not ($l -match '^asuser exit=')) -and ((Get-Date) - $t0).TotalMinutes -lt 25)
Unregister-ScheduledTask -TaskName 'nxgate-asuser' -Confirm:$false
"as-user wall time: $([int]((Get-Date)-$t0).TotalSeconds)s"
$bad = ''
if (@($l -match '^asuser exit=').Count -eq 0) { $bad = "$Script did not finish as $u within 25 minutes" }
elseif (@($l -match ('^asuser start user=' + [regex]::Escape($u) + ' administrators-member=False$')).Count -ne 1) { $bad = "$Script did not run as $u with a token outside Administrators" }
if ($bad) { $l | ForEach-Object { "inner: $_" }; "NXG-RESULT: FAIL $bad" } else { $l }
