# SPDX-License-Identifier: AGPL-3.0-or-later
#
# Guest (sent by guest-run.ps1 over PowerShell Direct). Step 7 of run-gate.sh:
# wait, at most 27 minutes, for the verify phase the unattended session started
# to finish and write verdict.txt, then print each assertion's recorded result.
# The verdict itself is read by run-gate.sh from the collected verdict.txt, not
# from here. Ends with one "NXG-RESULT:" line: ok when verdict.txt exists.
$b = "$env:USERPROFILE\nx-gate\run"; $t0 = Get-Date
$before = (Get-Item "$b\verdict.txt" -ErrorAction SilentlyContinue).LastWriteTime
do { Start-Sleep 10
  $running = @(Get-CimInstance Win32_Process | ? { $_.CommandLine -like '*windows-phase5-gate.ps1*-Phase verify*' }).Count
  $now = (Get-Item "$b\verdict.txt" -ErrorAction SilentlyContinue).LastWriteTime
  $claude = @(Get-Process claude -ErrorAction SilentlyContinue).Count
} while (($running -gt 0 -or $now -eq $before) -and ((Get-Date) - $t0).TotalMinutes -lt 27)
"waited $([int]((Get-Date)-$t0).TotalSeconds)s; verify running=$running; claude processes=$claude; verdict written $now"
Add-Type -AssemblyName System.Web.Extensions; $j = New-Object System.Web.Script.Serialization.JavaScriptSerializer
$r = $j.DeserializeObject((Get-Content -Raw "$b\state.json"))['results']
foreach ($k in 'P0','V0','A1','A2','A3','A4','A5') { if ($r.ContainsKey($k)) { $x = $r[$k]; "$k $($x['status']) at $($x['at']): $($x['detail'])" } }
if (Test-Path "$b\verdict.txt") { Get-Content "$b\verdict.txt" | Select -Last 2; 'NXG-RESULT: ok' } else { 'NO verdict.txt'; 'NXG-RESULT: FAIL no verdict.txt after 27 minutes' }
