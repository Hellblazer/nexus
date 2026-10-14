# SPDX-License-Identifier: AGPL-3.0-or-later
#
# Guest (sent by guest-run.ps1 over PowerShell Direct). Step 4 of run-gate.sh:
# run the gate's setup phase as a one-shot scheduled task in the guest's
# console session, because the supervisor must start in the desktop session
# and a PowerShell Direct session is not one. Waits at most 25 minutes for the
# phase's "setup exit=" line. Ends with one "NXG-RESULT:" line: ok only on
# "setup exit=0". The task runs as the session's own account, or as the
# account guest-run.ps1 -RunAs named ($env:NXG_RUN_AS), which must be the one
# signed in at the console.
param(
    [Parameter(Mandatory = $true)][string]$Version,
    [Parameter(Mandatory = $true)][string]$Engine
)
if ($Version -notmatch '^[0-9]+\.[0-9]+\.[0-9]+$' -or $Engine -notmatch '^[0-9]+\.[0-9]+\.[0-9]+$') { 'NXG-RESULT: FAIL version and engine must be X.Y.Z'; return }
$b = "$env:USERPROFILE\nx-gate"
$who = if ($env:NXG_RUN_AS) { $env:NXG_RUN_AS } else { $env:USERNAME }
(@'
$host.UI.RawUI.WindowTitle = 'Phase 5 gate: setup'
$env:PATH = "$env:USERPROFILE\.local\bin;C:\Program Files\Git\cmd;$env:LOCALAPPDATA\Programs\Git\cmd;" + $env:PATH
& powershell.exe -NoProfile -ExecutionPolicy Bypass -File "$env:USERPROFILE\nx-gate\windows-phase5-gate.ps1" -Phase setup -ExpectedVersion @VERSION@ -ExpectedEngine @ENGINE@ *>&1 | Tee-Object -FilePath "$env:USERPROFILE\nx-gate\setup.log"
"setup exit=$LASTEXITCODE" | Tee-Object -Append -FilePath "$env:USERPROFILE\nx-gate\setup.log"
'@).Replace('@VERSION@', $Version).Replace('@ENGINE@', $Engine) | Set-Content -Encoding UTF8 "$b\setup-wrapper.ps1"
Remove-Item "$b\setup.log" -ErrorAction SilentlyContinue
$act = New-ScheduledTaskAction -Execute 'powershell.exe' -Argument "-NoExit -NoProfile -ExecutionPolicy Bypass -File `"$b\setup-wrapper.ps1`""
Register-ScheduledTask -TaskName 'nxgate-setup' -Action $act -Principal (New-ScheduledTaskPrincipal -UserId $who -LogonType Interactive) -Force | Out-Null
Start-ScheduledTask -TaskName 'nxgate-setup'
$t0 = Get-Date
do { Start-Sleep 10; $l = Get-Content "$b\setup.log" -ErrorAction SilentlyContinue } while ((-not ($l -match '^setup exit=')) -and ((Get-Date) - $t0).TotalMinutes -lt 25)
Unregister-ScheduledTask -TaskName 'nxgate-setup' -Confirm:$false
"setup wall time: $([int]((Get-Date)-$t0).TotalSeconds)s"
$l
if (@($l -match '^setup exit=0').Count -gt 0) { 'NXG-RESULT: ok' } else { 'NXG-RESULT: FAIL the setup phase did not finish with "setup exit=0" (see setup.log)' }
