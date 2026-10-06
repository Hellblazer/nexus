# SPDX-License-Identifier: AGPL-3.0-or-later
#
# RDR-224 Phase 5 gate (nexus-ijue9.16): native Windows, run by hand inside a
# clean Windows 11 guest from an interactive Claude Code session. Not CI.
#
# WHY IT IS HAND-RUN
#   Two of the five assertions are about a LIVE interactive Claude Code
#   session: its MCP round trip (nexus-ijue9.2) and its hooks firing
#   (nexus-efk2h). No CI runner hosts one, and an ssh session cannot stand in:
#   AttachConsole into another Windows session is denied, so a stop sent over
#   ssh cannot reach a supervisor started in the desktop session.
#
# HOST (Sam, 2026-10-05, nexus-f9bgu.6): Hyper-V guest nx-clean-win11 on
#   qwentescence, restored to checkpoint clean-baseline-2026-10-06. Windows
#   PowerShell 5.1 only; the script installs nothing.
#
# OPERATOR PROCEDURE
#   0. Host: restore clean-baseline, start the VM, sign in as the guest user.
#   1. Guest, a normal (non-elevated) PowerShell in the console session:
#        irm https://astral.sh/uv/install.ps1 | iex
#        uv tool install conexus==<ExpectedVersion>
#        irm https://claude.ai/install.ps1 | iex
#        winget install --id Git.Git -e --source winget   # /plugin marketplace add needs git
#   2. Mac: tests/e2e/windows-guest/launch.sh --stage tests/e2e/windows-phase5-gate.ps1
#      opens a signed-in Claude Code window in the guest and copies this
#      script to %USERPROFILE%\nx-gate\. In that window:
#        /plugin marketplace add Hellblazer/nexus
#        /plugin install conexus@nexus-plugins
#   3. Guest, a PowerShell window in the console session:
#        powershell -ExecutionPolicy Bypass -File $HOME\nx-gate\windows-phase5-gate.ps1 `
#          -Phase setup -ExpectedVersion <v> -ExpectedEngine <e>
#      It prints a nonce text.
#   4. Mac: launch.sh again. The new session starts with the plugin loaded and
#      the stack from step 3 live.
#   5. In that session, ask Claude to store the printed nonce text with
#      mcp__plugin_conexus_nexus__store_put (collection windows-phase5-gate),
#      then search for it with mcp__plugin_conexus_nexus__search.
#   6. In the same session, ask Claude to run, with its shell tool:
#        powershell -ExecutionPolicy Bypass -File $HOME\nx-gate\windows-phase5-gate.ps1 `
#          -Phase verify -ExpectedVersion <v> -ExpectedEngine <e>
#      The verdict is the last line. Running it from the session makes the stop
#      in assertion 4 come "from the same session", and gives `claude -p` in
#      assertion 5 the session's own credentials.
#
# THE ASSERTIONS (DECLARED = 5; a precondition runs before them)
#   P0  no system-wide VC++ runtime (vcruntime140*/msvcp140* in System32 or
#       SysWOW64, excluding the .NET Framework's private *_clr0400 copies;
#       VC++ runtime registry keys; VC++/VS uninstall entries), nx reports
#       ExpectedVersion, and no stack exists yet. The guest token (admin role,
#       integrity level) is recorded.
#   1   nx init --service installs the published windows-x64 engine and PG
#       bundle and the supervisor starts both, on BOTH init paths:
#       leg B: no --yes, stdin not a TTY  -> direct spawn, no logon task;
#       leg A: --yes                      -> the Task Scheduler launcher starts it.
#   2   MCP store_put then search from the live session returns the stored
#       text (read from the session's own transcript, not from its say-so).
#   3   the conexus hook launcher (uv run ... nx_hook_shim.py, nexus-efk2h)
#       fires and exits 0 when Claude Code invokes it, SessionStart included.
#   4   nx daemon service stop leaves PostgreSQL running (the Minimum Viable
#       Validation leg); nx daemon service stop --with-pg leaves no supervisor,
#       engine or postgres process, confirmed by the process table and again
#       RECHECK_SECONDS later (the launcher respawns 30 s after a non-zero
#       supervisor exit); the next start's pg.log shows a clean shutdown and no
#       crash recovery.
#   5   a plain `claude -p` returns within ClaudeTimeoutSec. RDR-218 also ran it
#       with no endpoint configured; that leg is not kept here, because a box
#       that passed assertion 1 always has a live endpoint and nexus-fd3zf,
#       which that leg guarded, is closed.
#
# NON-VACUITY: an assertion counts only when it executed (PASS or FAIL). A
#   -ForceSkip'd or never-reached assertion is recorded SKIPPED, and the
#   verdict is FAILED whenever executed != DECLARED. Off Windows the script
#   refuses with exit 2.
#
# WHAT A GREEN RUN DOES NOT COVER: host sleep/shutdown beyond nexus-f9bgu.46's
#   measurement, autostart at the next logon (nexus-f9bgu.23 handed it here;
#   not asserted, recorded as unmeasured), Windows on ARM, Smart App Control in
#   enforcing mode, the desktop bundle (nexus-ijue9.21), and the aspect-worker
#   daemon, which is only observed after assertion 4.
#
# Exit: 0 PASSED, 1 FAILED, 2 refused (wrong host or bad invocation).

param(
    [Parameter(Mandatory = $true)][ValidateSet('setup', 'verify')][string]$Phase,
    [Parameter(Mandatory = $true)][string]$ExpectedVersion,
    [Parameter(Mandatory = $true)][string]$ExpectedEngine,
    [string]$RunDir = (Join-Path $env:USERPROFILE 'nx-gate\run'),
    # Comma-separated assertion ids to record SKIPPED (a string: -File cannot bind an array).
    [string]$ForceSkip = '',
    [int]$ClaudeTimeoutSec = 180,
    [int]$HealthTimeoutSec = 600
)

$ErrorActionPreference = 'Stop'
$skipIds = @($ForceSkip -split ',' | Where-Object { $_.Trim() } | ForEach-Object { [int]$_.Trim() })
$DECLARED = 5
$RECHECK_SECONDS = 35

if ($env:OS -ne 'Windows_NT') {
    Write-Output 'REFUSED: this gate runs only on the Windows guest.'
    exit 2
}

Add-Type -AssemblyName System.Web.Extensions
$Json = New-Object System.Web.Script.Serialization.JavaScriptSerializer
$Json.MaxJsonLength = [int]::MaxValue

$nx = Join-Path $env:USERPROFILE '.local\bin\nx.exe'
$cfg = Join-Path $env:USERPROFILE '.config\nexus'
$statePath = Join-Path $RunDir 'state.json'
New-Item -ItemType Directory -Force -Path $RunDir | Out-Null

function Now-Utc { (Get-Date).ToUniversalTime().ToString('o') }
function Say([string]$msg) { Write-Output ("[{0}] {1}" -f (Get-Date).ToUniversalTime().ToString('HH:mm:ss'), $msg) }
function Fail([string]$msg) { throw $msg }
# A native command's stderr under 2>&1 is a terminating error in Windows
# PowerShell 5.1 when ErrorActionPreference is Stop; run natives with Continue.
function Native([scriptblock]$sb) { $ErrorActionPreference = 'Continue'; (& $sb 2>&1 | Out-String) }
# A deserialized JSON object is a Dictionary; read optional keys through K.
function K($d, [string]$k) { if ($d -is [System.Collections.IDictionary] -and $d.ContainsKey($k)) { $d[$k] } else { $null } }

function Load-State {
    if (-not (Test-Path $statePath)) { return $null }
    return $Json.DeserializeObject((Get-Content -Raw -Path $statePath))
}
function Save-State($s) { Set-Content -Path $statePath -Value ($Json.Serialize($s)) -Encoding UTF8 }

function Record([string]$id, [string]$status, [string]$detail) {
    $script:state['results'][$id] = @{ status = $status; detail = $detail; at = (Now-Utc) }
    Save-State $script:state
    Say ("{0} {1}: {2}" -f $id, $status, $detail)
}

function Run-Assertion([int]$id, [string]$name, [scriptblock]$body) {
    $key = "A$id"
    if ($skipIds -contains $id) { Record $key 'SKIPPED' "$name (forced by -ForceSkip)"; return }
    Say "A$id $name"
    try { $detail = (& $body | Out-String).Trim(); Record $key 'PASS' $detail }
    catch { Record $key 'FAIL' $_.Exception.Message }
}

function Invoke-Nx([string]$label, [string[]]$nxArgs) {
    $log = Join-Path $RunDir "$label.log"
    $out = Native { & $nx @nxArgs }
    $code = $LASTEXITCODE
    Set-Content -Path $log -Value $out -Encoding UTF8
    return @{ code = $code; out = $out; log = $log }
}

function Get-Status {
    $out = Native { & $nx daemon service status }
    $h = @{}
    foreach ($line in ($out -split "`r?`n")) {
        if ($line -match '^\s+([a-z_]+):\s*(.*)$') { $h[$Matches[1]] = $Matches[2].Trim() }
    }
    return $h
}

function Wait-Healthy([int]$timeoutSec) {
    $deadline = (Get-Date).AddSeconds($timeoutSec)
    do {
        $s = Get-Status
        if ($s['health'] -eq 'ok' -and $s['status'] -eq 'live' -and $s['pg'] -eq 'up') { return $s }
        Start-Sleep -Seconds 3
    } while ((Get-Date) -lt $deadline)
    Fail ("not healthy within {0}s (last: health={1} status={2} pg={3})" -f $timeoutSec, $s['health'], $s['status'], $s['pg'])
}

function Get-Stack {
    $procs = Get-CimInstance Win32_Process
    $pgRoot = Join-Path $cfg 'pg-bundle'
    return @{
        launcher   = @($procs | Where-Object { $_.CommandLine -like '*nexus.daemon.windows_autostart*' })
        supervisor = @($procs | Where-Object { $_.CommandLine -like '*daemon service start*' })
        engine     = @($procs | Where-Object { $_.Name -eq 'nexus-service.exe' })
        postgres   = @($procs | Where-Object { $_.Name -eq 'postgres.exe' -and $_.ExecutablePath -like "$pgRoot*" })
        aspect     = @($procs | Where-Object { $_.CommandLine -like '*aspect*worker*' })
        all        = $procs
    }
}

function Describe-Stack($k) {
    "launcher={0} supervisor={1} engine={2} postgres={3}" -f $k.launcher.Count, $k.supervisor.Count, $k.engine.Count, $k.postgres.Count
}

function Wait-Gone([string[]]$kinds, [int]$timeoutSec) {
    $deadline = (Get-Date).AddSeconds($timeoutSec)
    do {
        $k = Get-Stack
        $left = @($kinds | Where-Object { $k[$_].Count -gt 0 })
        if ($left.Count -eq 0) { return $k }
        Start-Sleep -Seconds 2
    } while ((Get-Date) -lt $deadline)
    Fail ("still running after {0}s: {1}" -f $timeoutSec, (Describe-Stack $k))
}

function Assert-Absent([string[]]$kinds, [string]$when) {
    $k = Get-Stack
    $left = @($kinds | Where-Object { $k[$_].Count -gt 0 })
    if ($left.Count -gt 0) { Fail ("{0}: {1} present ({2})" -f $when, ($left -join ','), (Describe-Stack $k)) }
}

function Assert-Engine([hashtable]$s) {
    if ($s['service_release_version'] -ne $ExpectedEngine) {
        Fail ("engine release_version {0}, expected {1}" -f $s['service_release_version'], $ExpectedEngine)
    }
    $meta = $Json.DeserializeObject((Get-Content -Raw (Join-Path $cfg 'service\nexus-service.meta.json')))
    $tag = "engine-service-v$ExpectedEngine"
    if ((K $meta 'tag') -ne $tag) { Fail ("nexus-service.meta.json tag {0}, expected {1}" -f (K $meta 'tag'), $tag) }
    if (-not (Test-Path (Join-Path $cfg 'service\nexus-pg.meta.json'))) { Fail 'no nexus-pg.meta.json: PG bundle not installed from the release' }
    return "engine $tag, release_version $($s['service_release_version']), pgvector $($s['pgvector'])"
}

function Task-Exists { [bool](Get-ScheduledTask -TaskName 'NexusStorageService' -ErrorAction SilentlyContinue) }

function Has-Ancestor($proc, $ancestorIds, $all) {
    $byId = @{}; foreach ($p in $all) { $byId[[int]$p.ProcessId] = $p }
    $cur = $proc; $hops = 0
    while ($cur -and $hops -lt 10) {
        if ($ancestorIds -contains [int]$cur.ParentProcessId) { return $true }
        $cur = $byId[[int]$cur.ParentProcessId]; $hops++
    }
    return $false
}

# ---------------------------------------------------------------- setup ----
if ($Phase -eq 'setup') {
    $script:state = @{
        started_at = (Now-Utc); expected_version = $ExpectedVersion; expected_engine = $ExpectedEngine
        nonce = ([guid]::NewGuid().ToString('N').Substring(0, 12)); results = @{}
    }
    Save-State $script:state

    # P0: the precondition. Not one of the five; a failure stops the run.
    try {
        $dlls = foreach ($d in "$env:windir\System32", "$env:windir\SysWOW64") {
            Get-ChildItem $d -Filter *.dll -ErrorAction SilentlyContinue |
                Where-Object { $_.Name -match '^(vcruntime140|msvcp140)' -and $_.Name -notmatch '_clr0400' } |
                ForEach-Object FullName
        }
        if (@($dlls).Count -gt 0) { Fail ("system-wide VC++ runtime present: " + ($dlls -join ', ')) }
        $keys = @('HKLM:\SOFTWARE\Microsoft\VisualStudio\14.0\VC\Runtimes',
                  'HKLM:\SOFTWARE\WOW6432Node\Microsoft\VisualStudio\14.0\VC\Runtimes') | Where-Object { Test-Path $_ }
        if (@($keys).Count -gt 0) { Fail ("VC++ runtime registry keys present: " + ($keys -join ', ')) }
        $un = Get-ChildItem 'HKLM:\SOFTWARE\Microsoft\Windows\CurrentVersion\Uninstall',
                            'HKLM:\SOFTWARE\WOW6432Node\Microsoft\Windows\CurrentVersion\Uninstall' -ErrorAction SilentlyContinue |
              ForEach-Object { (Get-ItemProperty $_.PSPath).DisplayName } |
              Where-Object { $_ -match 'Visual C\+\+|Visual Studio' }
        if (@($un).Count -gt 0) { Fail ("VC++/VS uninstall entries present: " + ($un -join ', ')) }
        $ver = (Native { & $nx --version }).Trim()
        if ($ver -notmatch [regex]::Escape("version $ExpectedVersion") + '$') { Fail "nx reports '$ver', expected $ExpectedVersion" }
        Assert-Absent @('launcher', 'supervisor', 'engine', 'postgres') 'before init'
        foreach ($p in 'postgres', 'pg_credentials') {
            if (Test-Path (Join-Path $cfg $p)) { Fail "not a clean box: $cfg\$p exists" }
        }
        if (Task-Exists) { Fail 'not a clean box: scheduled task NexusStorageService exists' }
        $isAdmin = ([Security.Principal.WindowsPrincipal][Security.Principal.WindowsIdentity]::GetCurrent()).IsInRole([Security.Principal.WindowsBuiltInRole]::Administrator)
        $il = ((Native { whoami /groups }) -split "`r?`n" | Select-String 'Mandatory Label\\(\w+) Mandatory Level').Matches | ForEach-Object { $_.Groups[1].Value } | Select-Object -First 1
        $os = (Get-CimInstance Win32_OperatingSystem)
        Record 'P0' 'PASS' ("no VC++ runtime; $ver; clean config dir; token admin_role=$isAdmin integrity=$il; " + $os.Caption + ' build ' + $os.BuildNumber)
    } catch {
        Record 'P0' 'FAIL' $_.Exception.Message
        Write-Output 'WINDOWS PHASE 5 GATE FAILED (precondition)'
        exit 1
    }

    Run-Assertion 1 'nx init --service installs the published assets and starts the stack, both init paths' {
        # Leg B first: on the clean box there is no logon task, so a non-TTY
        # init without --yes must take the direct spawn and register none.
        $r = Native { '' | & $nx init --service }
        Set-Content -Path (Join-Path $RunDir 'init-legB.log') -Value $r -Encoding UTF8
        $s = Wait-Healthy $HealthTimeoutSec
        $b = Assert-Engine $s
        if (Task-Exists) { Fail 'leg B (no --yes, no TTY) registered the logon task' }
        $k = Get-Stack
        if ($k.launcher.Count -gt 0) { Fail 'leg B: a Task Scheduler launcher is running' }
        if ($k.supervisor.Count -eq 0 -or $k.engine.Count -eq 0 -or $k.postgres.Count -eq 0) { Fail ("leg B: stack incomplete: " + (Describe-Stack $k)) }
        "leg B direct spawn: $b; " + (Describe-Stack $k)
        $stop = Invoke-Nx 'legB-stop-with-pg' @('daemon', 'service', 'stop', '--with-pg')
        Wait-Gone @('supervisor', 'engine', 'postgres') 90 | Out-Null
        Start-Sleep -Seconds $RECHECK_SECONDS
        Assert-Absent @('supervisor', 'engine', 'postgres') "leg B, ${RECHECK_SECONDS}s after stop --with-pg"

        # Leg A: --yes registers the logon task, and the task's launcher is
        # the starter: the supervisor descends from it.
        $ra = Invoke-Nx 'init-legA' @('init', '--service', '--yes')
        $s = Wait-Healthy $HealthTimeoutSec
        $a = Assert-Engine $s
        if (-not (Task-Exists)) { Fail 'leg A (--yes) did not register the NexusStorageService task' }
        $k = Get-Stack
        if ($k.launcher.Count -eq 0) { Fail 'leg A: no Task Scheduler launcher process' }
        $launcherIds = @($k.launcher | ForEach-Object { [int]$_.ProcessId })
        $fromLauncher = @($k.supervisor | Where-Object { Has-Ancestor $_ $launcherIds $k.all })
        if ($fromLauncher.Count -eq 0) { Fail 'leg A: the supervisor does not descend from the Task Scheduler launcher' }
        "leg A launcher: $a; " + (Describe-Stack $k)
    }

    $script:state['setup_completed_at'] = (Now-Utc)
    Save-State $script:state
    $text = "nx phase5 gate nonce $($script:state['nonce'])"
    Write-Output ''
    Write-Output 'SETUP DONE. Next (operator procedure steps 4 to 6):'
    Write-Output '  relaunch the Claude Code session (Mac: tests/e2e/windows-guest/launch.sh), then ask it to'
    Write-Output "  store this exact text with mcp__plugin_conexus_nexus__store_put, collection windows-phase5-gate:"
    Write-Output "      $text"
    Write-Output '  then search for it with mcp__plugin_conexus_nexus__search, then run this script with -Phase verify.'
    exit 0
}

# --------------------------------------------------------------- verify ----
$script:state = Load-State
if (-not (K $script:state 'setup_completed_at')) {
    Write-Output 'WINDOWS PHASE 5 GATE FAILED (no completed setup phase in this run dir; run -Phase setup first)'
    exit 1
}
if ((K $script:state 'expected_version') -ne $ExpectedVersion -or (K $script:state 'expected_engine') -ne $ExpectedEngine) {
    Write-Output 'REFUSED: -ExpectedVersion/-ExpectedEngine differ from the setup phase'
    exit 2
}
$nonce = $script:state['nonce']
$since = [datetime]::Parse($script:state['setup_completed_at']).ToUniversalTime()

# The live session's own record: every transcript written since setup.
function Read-Transcripts {
    $root = Join-Path $env:USERPROFILE '.claude\projects'
    $uses = @{}; $results = @{}; $hooks = New-Object System.Collections.ArrayList
    $files = Get-ChildItem $root -Recurse -Filter *.jsonl -ErrorAction SilentlyContinue | Where-Object { $_.LastWriteTimeUtc -gt $since }
    foreach ($f in $files) {
        foreach ($line in [IO.File]::ReadLines($f.FullName)) {
            if (-not $line) { continue }
            try { $e = $Json.DeserializeObject($line) } catch { continue }
            if (-not (K $e 'timestamp')) { continue }
            if ([datetime]::Parse((K $e 'timestamp')).ToUniversalTime() -lt $since) { continue }
            $att = K $e 'attachment'
            if ("$(K $att 'type')" -like 'hook_*') { [void]$hooks.Add($att) }
            $content = K (K $e 'message') 'content'
            if (-not ($content -is [array])) { continue }
            foreach ($c in $content) {
                if (-not ($c -is [System.Collections.IDictionary])) { continue }
                if ((K $c 'type') -eq 'tool_use') { $uses[(K $c 'id')] = $c }
                elseif ((K $c 'type') -eq 'tool_result') {
                    $body = K $c 'content'
                    if ($body -is [array]) { $body = ($body | ForEach-Object { if ($_ -is [System.Collections.IDictionary]) { K $_ 'text' } else { "$_" } }) -join "`n" }
                    $results[(K $c 'tool_use_id')] = @{ text = "$body"; is_error = [bool](K $c 'is_error') }
                }
            }
        }
    }
    return @{ uses = $uses; results = $results; hooks = $hooks; files = @($files).Count }
}

$tx = Read-Transcripts

Run-Assertion 2 'MCP store_put then search from the live session returns the stored text' {
    if ($tx.files -eq 0) { Fail 'no Claude Code transcript written since setup' }
    $stores = @($tx.uses.Values | Where-Object { (K $_ 'name') -eq 'mcp__plugin_conexus_nexus__store_put' -and "$(K (K $_ 'input') 'content')" -like "*$nonce*" })
    if ($stores.Count -eq 0) { Fail "no store_put of the nonce $nonce in the session transcript" }
    $okStore = @($stores | Where-Object { $tx.results.ContainsKey((K $_ 'id')) -and -not $tx.results[(K $_ 'id')].is_error })
    if ($okStore.Count -eq 0) { Fail 'the store_put of the nonce has no successful tool_result' }
    $hits = @($tx.uses.Values | Where-Object {
        (K $_ 'name') -eq 'mcp__plugin_conexus_nexus__search' -and $tx.results.ContainsKey((K $_ 'id')) -and
        -not $tx.results[(K $_ 'id')].is_error -and $tx.results[(K $_ 'id')].text -like "*$nonce*" })
    if ($hits.Count -eq 0) { Fail "no search tool_result in the session transcript contains the nonce $nonce" }
    "store_put ok ($($okStore.Count)); search returned the nonce ($($hits.Count) result(s)); transcripts scanned: $($tx.files)"
}

Run-Assertion 3 'the conexus hook launcher fires and exits 0 when Claude Code invokes it' {
    $shim = @($tx.hooks | Where-Object { "$(K $_ 'command')" -like '*nx_hook_shim.py*' })
    if ($shim.Count -eq 0) { Fail 'no nx_hook_shim.py hook run recorded since setup' }
    $bad = @($shim | Where-Object { "$(K $_ 'type')" -ne 'hook_success' -or [int](K $_ 'exitCode') -ne 0 })
    if ($bad.Count -gt 0) { Fail ("{0} nx_hook_shim.py hook run(s) failed, first: {1} {2} exit={3}" -f $bad.Count, (K $bad[0] 'type'), (K $bad[0] 'hookName'), (K $bad[0] 'exitCode')) }
    $start = @($tx.hooks | Where-Object { "$(K $_ 'hookEvent')" -eq 'SessionStart' -and "$(K $_ 'type')" -eq 'hook_success' })
    if ($start.Count -eq 0) { Fail 'no successful SessionStart hook recorded since setup' }
    $launcher = "$(K $shim[0] 'command')" -replace '\s+\S*nx_hook_shim\.py.*$', ''
    "nx_hook_shim.py runs ok: $($shim.Count); SessionStart ok: $($start.Count); launcher: $launcher"
}

Run-Assertion 5 'a plain claude -p returns' {
    $claude = (Get-Command claude -ErrorAction SilentlyContinue).Source
    if (-not $claude) { Fail 'claude not on PATH' }
    $want = "NXGATE-OK-$nonce"
    $outF = Join-Path $RunDir 'claude-p.out'; $errF = Join-Path $RunDir 'claude-p.err'; $inF = Join-Path $RunDir 'empty.in'
    Set-Content -Path $inF -Value '' -NoNewline
    $t0 = Get-Date
    $p = Start-Process -FilePath $claude -ArgumentList "-p `"Reply with exactly $want and nothing else`"" -NoNewWindow -PassThru `
        -RedirectStandardOutput $outF -RedirectStandardError $errF -RedirectStandardInput $inF
    if (-not $p.WaitForExit($ClaudeTimeoutSec * 1000)) {
        & taskkill.exe /T /F /PID $p.Id | Out-Null
        Fail "claude -p did not return within ${ClaudeTimeoutSec}s (hang)"
    }
    $secs = [int]((Get-Date) - $t0).TotalSeconds
    $out = Get-Content -Raw $outF -ErrorAction SilentlyContinue
    if ($p.ExitCode -ne 0) { Fail ("claude -p exited {0} after {1}s: {2}" -f $p.ExitCode, $secs, (Get-Content -Raw $errF -ErrorAction SilentlyContinue)) }
    if ("$out" -notlike "*$want*") { Fail "claude -p returned in ${secs}s without the expected reply" }
    "claude -p returned in ${secs}s with the expected reply"
}

Run-Assertion 4 'stop leaves PG running; stop --with-pg leaves nothing; the next start shows no crash recovery' {
    # The Minimum Viable Validation leg: a plain stop leaves PostgreSQL up.
    $r = Invoke-Nx 'stop-plain' @('daemon', 'service', 'stop')
    Wait-Gone @('supervisor', 'engine') 90 | Out-Null
    Start-Sleep -Seconds $RECHECK_SECONDS
    Assert-Absent @('supervisor', 'engine') "${RECHECK_SECONDS}s after plain stop (launcher respawn?)"
    if ((Get-Stack).postgres.Count -eq 0) { Fail 'plain nx daemon service stop also stopped PostgreSQL' }
    $mvv = 'plain stop: supervisor+engine gone and stayed gone, postgres still up'

    $r = Invoke-Nx 'start-1' @('daemon', 'service', 'start')
    Wait-Healthy $HealthTimeoutSec | Out-Null
    $r = Invoke-Nx 'stop-with-pg' @('daemon', 'service', 'stop', '--with-pg')
    Wait-Gone @('supervisor', 'engine', 'postgres') 90 | Out-Null
    Start-Sleep -Seconds $RECHECK_SECONDS
    Assert-Absent @('supervisor', 'engine', 'postgres') "${RECHECK_SECONDS}s after stop --with-pg"
    $aspect = (Get-Stack).aspect.Count

    $pgLog = Join-Path $cfg 'postgres\pg.log'
    $before = @(Get-Content $pgLog -ErrorAction SilentlyContinue).Count
    $r = Invoke-Nx 'start-2' @('daemon', 'service', 'start')
    Wait-Healthy $HealthTimeoutSec | Out-Null
    $new = @(Get-Content $pgLog | Select-Object -Skip $before) -join "`n"
    if ($new -match 'was interrupted|not properly shut down|redo starts|automatic recovery') { Fail "next start's pg.log shows crash recovery" }
    if ($new -notmatch 'database system was shut down at') { Fail "next start's pg.log has no 'database system was shut down at' line" }
    "$mvv; stop --with-pg: no supervisor/engine/postgres at stop and ${RECHECK_SECONDS}s later; next start clean (no crash recovery); aspect-worker processes left after the stop: $aspect"
}

# -------------------------------------------------------------- verdict ----
$results = $script:state['results']
$executed = @(1..$DECLARED | ForEach-Object { K $results "A$_" } | Where-Object { (K $_ 'status') -in @('PASS', 'FAIL') }).Count
$failed = @(1..$DECLARED | Where-Object { (K (K $results "A$_") 'status') -ne 'PASS' })
Write-Output ''
foreach ($id in @('P0') + @(1..$DECLARED | ForEach-Object { "A$_" })) {
    $r = K $results $id
    if ($r) { Write-Output ("{0} {1}: {2}" -f $id, (K $r 'status'), (K $r 'detail')) } else { Write-Output "$id NOT RUN" }
}
$summary = "executed=$executed declared=$DECLARED"
Set-Content -Path (Join-Path $RunDir 'verdict.txt') -Value ($Json.Serialize($results)) -Encoding UTF8
if ($executed -ne $DECLARED -or $failed.Count -gt 0 -or (K (K $results 'P0') 'status') -ne 'PASS') {
    Write-Output "WINDOWS PHASE 5 GATE FAILED ($summary; not passing: $(($failed | ForEach-Object { "A$_" }) -join ','))"
    exit 1
}
Write-Output "WINDOWS PHASE 5 GATE PASSED ($summary)"
exit 0
