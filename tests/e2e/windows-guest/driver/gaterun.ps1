# SPDX-License-Identifier: AGPL-3.0-or-later
#
# Guest (sent by guest-run.ps1 over PowerShell Direct). Step 5 of run-gate.sh:
# write GATERUN.txt, the instructions the unattended Claude Code session
# follows (gate steps 5 and 6), from the nonce the setup phase stored in
# state.json. -Source candidate names the run as a candidate wheel in the text.
# Ends with one "NXG-RESULT:" line.
param(
    [Parameter(Mandatory = $true)][string]$Version,
    [Parameter(Mandatory = $true)][string]$Engine,
    [string]$Source = ''
)
if ($Version -notmatch '^[0-9]+\.[0-9]+\.[0-9]+$' -or $Engine -notmatch '^[0-9]+\.[0-9]+\.[0-9]+$') { 'NXG-RESULT: FAIL version and engine must be X.Y.Z'; return }
Add-Type -AssemblyName System.Web.Extensions; $j = New-Object System.Web.Script.Serialization.JavaScriptSerializer
$st = $j.DeserializeObject((Get-Content -Raw "$env:USERPROFILE\nx-gate\run\state.json"))
if (-not $st['setup_completed_at']) { 'NO completed setup; GATERUN.txt not written'; 'NXG-RESULT: FAIL the setup phase did not complete'; return }
$nonce = $st['nonce']
$what = if ($Source -eq 'candidate') { "conexus $Version installed from a locally built candidate wheel (not a published build)" } else { "published conexus $Version" }
(@'
RDR-224 Phase 5 gate run on @WHAT@. Do these three steps in order, nothing else:
1. Call mcp__plugin_conexus_nexus__store_put with collection "windows-phase5-gate", title "phase 5 gate run @VERSION@", content exactly: nx phase5 gate nonce NONCE
2. Call mcp__plugin_conexus_nexus__search with query: nx phase5 gate nonce NONCE
3. Run this exact command with your shell tool IN THE BACKGROUND, wait for it to finish (several minutes), then show its last 16 lines:
powershell.exe -NoProfile -ExecutionPolicy Bypass -File '@HOME@/nx-gate/windows-phase5-gate.ps1' -Phase verify -ExpectedVersion @VERSION@ -ExpectedEngine @ENGINE@ -RunDir '@HOME@/nx-gate/run'
'@).Replace('@WHAT@', $what).Replace('@VERSION@', $Version).Replace('@ENGINE@', $Engine).Replace('@HOME@', ($env:USERPROFILE -replace '\\', '/')).Replace('NONCE', $nonce) | Set-Content -Encoding UTF8 "$env:USERPROFILE\nx-gate\GATERUN.txt"
"written; nonce $nonce; user dir $env:USERPROFILE"
Get-Content "$env:USERPROFILE\nx-gate\GATERUN.txt"
'NXG-RESULT: ok'
