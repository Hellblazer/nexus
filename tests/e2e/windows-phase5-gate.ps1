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
#   1. Guest, a normal (non-elevated) PowerShell in the console session. Open a
#      NEW PowerShell window after each installer so PATH is current:
#        irm https://astral.sh/uv/install.ps1 | iex
#        uv tool install conexus==<ExpectedVersion>
#        irm https://claude.ai/install.ps1 | iex
#        winget install --id Git.Git -e --source winget   # /plugin marketplace add needs git
#      These downloads also warm a fresh Windows root store; before that, nx's
#      own downloads can fail CERTIFICATE_VERIFY_FAILED (nexus-f9bgu.46 side
#      finding, nexus-4rqgk).
#   2. Mac: tests/e2e/windows-guest/launch.sh --stage tests/e2e/windows-phase5-gate.ps1
#      opens a signed-in Claude Code window in the guest and copies this
#      script to %USERPROFILE%\nx-gate\. In that window:
#        /plugin marketplace add Hellblazer/nexus
#        /plugin install conexus@nexus-plugins
#   3. Guest, a PowerShell window in the console session:
#        powershell -ExecutionPolicy Bypass -File $HOME\nx-gate\windows-phase5-gate.ps1 -Phase setup -ExpectedVersion <v> -ExpectedEngine <e>
#      It prints a nonce text and the exact verify command.
#   4. Mac: launch.sh again. The new session starts with the plugin loaded and
#      the stack from step 3 live.
#   5. In that session, ask Claude to store the printed nonce text with
#      mcp__plugin_conexus_nexus__store_put (collection windows-phase5-gate),
#      then search for it with mcp__plugin_conexus_nexus__search.
#   6. In the same session, ask Claude to run the verify command setup printed
#      in the background and wait for it to finish (verify takes about 4 to 8
#      minutes on a healthy box and can take up to 20 with every health wait
#      exhausted, past the shell tool's 600 s cap). The verdict is the last
#      line, and also in <RunDir>\verdict.txt. A run with no verdict line is
#      FAILED.
#   Unattended (no one at the guest's console): write steps 5 and 6 into a
#   file in the guest (for example %USERPROFILE%\nx-gate\GATERUN.txt) and do
#   step 4 as
#      tests/e2e/windows-guest/launch.sh --permission-mode auto --prompt 'follow the instructions in C:\Users\<user>\nx-gate\GATERUN.txt'
#   The session starts already working the prompt (first-run prompts are
#   pre-accepted); read the verdict from <RunDir>\verdict.txt. Passed this way
#   on published 7.72.1, 2026-10-07 (T2 nexus_rdr/224-gate-run-7.72.1-2026-10-07).
#
# THE ASSERTIONS (DECLARED = 5; a precondition runs before each phase)
#   P0  (setup) no system-wide VC++ runtime (vcruntime140*/msvcp140* in
#       System32 or SysWOW64, excluding the .NET Framework's private *_clr0400
#       copies; VC++ runtime registry keys; VC++/VS uninstall entries), nx
#       reports ExpectedVersion, and no stack and no engine/PG files exist yet.
#       The guest token (Administrators enabled or deny-only, integrity level)
#       is recorded.
#   V0  (verify) nx, the engine and the conexus plugin still report the
#       expected versions after the relaunch, and this verify process was
#       started by the live interactive session (its transcript holds the tool
#       call that ran it).
#   1   nx init --service installs the published windows-x64 engine and PG
#       bundle and the supervisor starts both, on BOTH init paths:
#       leg B: no --yes, stdin not a TTY  -> direct spawn, no logon task;
#       leg A: --yes                      -> the Task Scheduler launcher starts it.
#   2   MCP store_put then search, in the live session (its sessionId, not a
#       sidechain), returns the stored text; nx search outside the session
#       finds it too. Session id, Claude Code version and engine tag are
#       recorded (nexus-ijue9.2).
#   3   the conexus hooks fire in the live session: nx-hook session-start
#       succeeds WITH output (a missing nx-hook cannot produce it), the
#       uv run ... nx_hook_shim.py launcher (nexus-efk2h) succeeds with output,
#       and no conexus hook run failed. SessionEnd (nx-session-end-launcher)
#       fires after the session and is not observable here.
#   4   against the launcher-started stack: nx daemon service stop --with-pg
#       leaves no supervisor, engine or postgres process, confirmed by the
#       process table and again RECHECK_SECONDS later (the launcher respawns
#       30 s after a non-zero supervisor exit); the next start, through the
#       logon task again, shows a clean shutdown in pg.log and no crash
#       recovery; then the Minimum Viable Validation leg, on that
#       launcher-started stack: a plain nx daemon service stop leaves
#       PostgreSQL up and the supervisor and engine stay down.
#   5   a plain `claude -p` returns within ClaudeTimeoutSec, twice: with the
#       stack up, and with no endpoint (inside assertion 4, after the
#       --with-pg stop; the RDR-218 leg). Claude Code deletes
#       CLAUDE_CODE_OAUTH_TOKEN from its own environment (T2
#       nexus_rdr/219-research-15), so the launcher also exports
#       NX_HARNESS_CLAUDE_OAUTH_TOKEN and this script maps it back for its
#       child only (RDR-219 "The nx-mcp dispatch grant"). An auth failure is
#       reported as such, distinct from a hang. No credential file may appear
#       under %USERPROFILE%\.claude during the run.
#
# NON-VACUITY: an assertion counts only when it executed (PASS or FAIL). A
#   -ForceSkip'd or never-reached assertion is recorded SKIPPED, and the
#   verdict is FAILED whenever executed != DECLARED, any assertion failed, or a
#   precondition failed. Off Windows the script refuses with exit 2.
#
# WHAT A GREEN RUN DOES NOT COVER: autostart at the next logon and the
#   sign-out/restart path (measured by nexus-f9bgu.46, fixed by nexus-f9bgu.51,
#   not re-asserted here), Windows on ARM, Smart App Control in enforcing mode,
#   the desktop bundle (nexus-ijue9.21), SessionEnd, and the aspect-worker
#   daemon, which is only observed (process count, CPU seconds and command
#   lines after the --with-pg stop). It outlives `service stop` by design:
#   RDR-224's recorded decision (Revision History, "`service stop` leaves it
#   running as on POSIX"), nexus-g5rz5.
#
# Exit: 0 PASSED, 1 FAILED, 2 refused (wrong host or bad invocation).

param(
    [Parameter(Mandatory = $true)][ValidateSet('setup', 'verify')][string]$Phase,
    [Parameter(Mandatory = $true)][string]$ExpectedVersion,
    [Parameter(Mandatory = $true)][string]$ExpectedEngine,
    [string]$RunDir = '',
    # Comma-separated assertion ids to record SKIPPED (a string: -File cannot bind an array).
    [string]$ForceSkip = '',
    [int]$ClaudeTimeoutSec = 180,
    [int]$HealthTimeoutSec = 300
)

if ($env:OS -ne 'Windows_NT') {
    Write-Output 'REFUSED: this gate runs only on the Windows guest.'
    exit 2
}

$ErrorActionPreference = 'Stop'
$skipIds = @($ForceSkip -split ',' | Where-Object { $_.Trim() } | ForEach-Object { [int]$_.Trim() })
$DECLARED = 5
$RECHECK_SECONDS = 35
if (-not $RunDir) { $RunDir = Join-Path $env:USERPROFILE 'nx-gate\run' }

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
# The leading comma matters: PowerShell unrolls an array a function returns, so
# a one-element array (a transcript message whose content holds a single tool
# call) would come back as its element and fail every `-is [array]` test.
function K($d, [string]$k) { if ($d -is [System.Collections.IDictionary] -and $k -and $d.ContainsKey($k)) { return , $d[$k] } else { return $null } }

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
    $procs = @(Get-CimInstance Win32_Process)
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

function Read-Meta([string]$name) {
    $p = Join-Path $cfg "service\$name"
    if (-not (Test-Path $p)) { Fail "no ${name}: not installed from the release" }
    return $Json.DeserializeObject((Get-Content -Raw $p))
}

function Assert-Engine([hashtable]$s) {
    if ($s['service_release_version'] -ne $ExpectedEngine) {
        Fail ("engine release_version {0}, expected {1}" -f $s['service_release_version'], $ExpectedEngine)
    }
    $tag = "engine-service-v$ExpectedEngine"
    foreach ($m in 'nexus-service.meta.json', 'nexus-pg.meta.json') {
        $meta = Read-Meta $m
        if ((K $meta 'tag') -ne $tag) { Fail ("{0} tag {1}, expected {2}" -f $m, (K $meta 'tag'), $tag) }
        if ("$(K $meta 'asset')" -notlike '*windows-x64*') { Fail ("{0} asset '{1}' is not a windows-x64 asset" -f $m, (K $meta 'asset')) }
    }
    return "engine and PG bundle $tag (windows-x64 assets), release_version $($s['service_release_version']), pgvector $($s['pgvector'])"
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

function Supervisor-From-Launcher($k) {
    $launcherIds = @($k.launcher | ForEach-Object { [int]$_.ProcessId })
    return @($k.supervisor | Where-Object { Has-Ancestor $_ $launcherIds $k.all }).Count -gt 0
}

function Credential-Files {
    @(Get-ChildItem (Join-Path $env:USERPROFILE '.claude') -Force -Recurse -Depth 1 -File -ErrorAction SilentlyContinue |
        Where-Object { $_.Name -match 'credential' })
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
        $dlls = @(foreach ($d in "$env:windir\System32", "$env:windir\SysWOW64") {
            Get-ChildItem $d -Filter *.dll -ErrorAction SilentlyContinue |
                Where-Object { $_.Name -match '^(vcruntime140|msvcp140)' -and $_.Name -notmatch '_clr0400' } |
                ForEach-Object FullName
        })
        if ($dlls.Count -gt 0) { Fail ("system-wide VC++ runtime present: " + ($dlls -join ', ')) }
        $keys = @(@('HKLM:\SOFTWARE\Microsoft\VisualStudio\14.0\VC\Runtimes',
                    'HKLM:\SOFTWARE\WOW6432Node\Microsoft\VisualStudio\14.0\VC\Runtimes') | Where-Object { Test-Path $_ })
        if ($keys.Count -gt 0) { Fail ("VC++ runtime registry keys present: " + ($keys -join ', ')) }
        $un = @(Get-ChildItem 'HKLM:\SOFTWARE\Microsoft\Windows\CurrentVersion\Uninstall',
                              'HKLM:\SOFTWARE\WOW6432Node\Microsoft\Windows\CurrentVersion\Uninstall' -ErrorAction SilentlyContinue |
                ForEach-Object { (Get-ItemProperty $_.PSPath).DisplayName } |
                Where-Object { $_ -match 'Visual C\+\+|Visual Studio' })
        if ($un.Count -gt 0) { Fail ("VC++/VS uninstall entries present: " + ($un -join ', ')) }
        $ver = (Native { & $nx --version }).Trim()
        if ($ver -notmatch [regex]::Escape("version $ExpectedVersion") + '$') { Fail "nx reports '$ver', expected $ExpectedVersion" }
        Assert-Absent @('launcher', 'supervisor', 'engine', 'postgres') 'before init'
        foreach ($p in 'postgres', 'pg_credentials', 'service', 'pg-bundle') {
            if (Test-Path (Join-Path $cfg $p)) { Fail "not a clean box: $cfg\$p exists" }
        }
        if (Task-Exists) { Fail 'not a clean box: scheduled task NexusStorageService exists' }
        $groups = Native { whoami /groups }
        $il = ($groups -split "`r?`n" | Select-String 'Mandatory Label\\(\w+) Mandatory Level').Matches | ForEach-Object { $_.Groups[1].Value } | Select-Object -First 1
        $adm = ($groups -split "`r?`n" | Where-Object { $_ -match 'BUILTIN\\Administrators' }) -join ' '
        $admState = if (-not $adm) { 'not a member' } elseif ($adm -match 'deny only') { 'member, filtered (deny only)' } else { 'member, enabled' }
        $os = Get-CimInstance Win32_OperatingSystem
        Record 'P0' 'PASS' ("no VC++ runtime; $ver; clean config dir; token: Administrators $admState, integrity $il; " + $os.Caption + ' build ' + $os.BuildNumber)
    } catch {
        Record 'P0' 'FAIL' $_.Exception.Message
        Write-Output 'WINDOWS PHASE 5 GATE FAILED (precondition)'
        exit 1
    }

    Run-Assertion 1 'nx init --service installs the published assets and starts the stack, both init paths' {
        # Leg B first: on the clean box there is no logon task, so a non-TTY
        # init without --yes must take the direct spawn and register none.
        $r = Native { '' | & $nx init --service }
        $code = $LASTEXITCODE
        Set-Content -Path (Join-Path $RunDir 'init-legB.log') -Value $r -Encoding UTF8
        if ($code -ne 0) { Fail "leg B: nx init --service exited $code (see init-legB.log)" }
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
        Assert-Absent @('supervisor', 'engine', 'postgres') "leg B, ${RECHECK_SECONDS}s after stop --with-pg (exit $($stop.code))"

        # Leg A: --yes registers the logon task, and the task's launcher is
        # the starter: the supervisor descends from it.
        $ra = Invoke-Nx 'init-legA' @('init', '--service', '--yes')
        if ($ra.code -ne 0) { Fail "leg A: nx init --service --yes exited $($ra.code) (see init-legA.log)" }
        $s = Wait-Healthy $HealthTimeoutSec
        $a = Assert-Engine $s
        if (-not (Task-Exists)) { Fail 'leg A (--yes) did not register the NexusStorageService task' }
        $k = Get-Stack
        if ($k.launcher.Count -eq 0) { Fail 'leg A: no Task Scheduler launcher process' }
        if (-not (Supervisor-From-Launcher $k)) { Fail 'leg A: the supervisor does not descend from the Task Scheduler launcher' }
        "leg A launcher: $a; " + (Describe-Stack $k)
    }

    if ((K (K $script:state['results'] 'A1') 'status') -ne 'PASS' -and $skipIds -notcontains 1) {
        Write-Output 'WINDOWS PHASE 5 GATE FAILED (assertion 1; setup cannot be re-run on this box: restore clean-baseline)'
        exit 1
    }
    $script:state['setup_completed_at'] = (Now-Utc)
    Save-State $script:state
    $text = "nx phase5 gate nonce $($script:state['nonce'])"
    $self = ($PSCommandPath -replace '\\', '/')
    $rd = ($RunDir -replace '\\', '/')
    Write-Output ''
    Write-Output 'SETUP DONE. Next (operator procedure steps 4 to 6):'
    Write-Output '  relaunch the Claude Code session (Mac: tests/e2e/windows-guest/launch.sh), then ask it to'
    Write-Output '  store this exact text with mcp__plugin_conexus_nexus__store_put, collection windows-phase5-gate:'
    Write-Output "      $text"
    Write-Output '  then search for it with mcp__plugin_conexus_nexus__search, then run, with a 600000 ms timeout:'
    Write-Output "      powershell.exe -NoProfile -ExecutionPolicy Bypass -File '$self' -Phase verify -ExpectedVersion $ExpectedVersion -ExpectedEngine $ExpectedEngine -RunDir '$rd'"
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
$credBefore = @(Credential-Files | ForEach-Object FullName)

# The live session's own record: every transcript line written since setup.
# Claude Code holds the live transcript open for append, so open it ReadWrite.
function Read-Transcripts {
    $root = Join-Path $env:USERPROFILE '.claude\projects'
    $lines = New-Object System.Collections.ArrayList
    $files = @(Get-ChildItem $root -Recurse -Filter *.jsonl -ErrorAction SilentlyContinue | Where-Object { $_.LastWriteTimeUtc -gt $since })
    foreach ($f in $files) {
        try {
            $fs = New-Object System.IO.FileStream($f.FullName, [System.IO.FileMode]::Open, [System.IO.FileAccess]::Read, [System.IO.FileShare]::ReadWrite)
            $sr = New-Object System.IO.StreamReader($fs)
            while ($null -ne ($line = $sr.ReadLine())) {
                if (-not $line) { continue }
                try { $e = $Json.DeserializeObject($line) } catch { continue }
                $ts = K $e 'timestamp'
                if (-not $ts) { continue }
                if ([datetime]::Parse($ts).ToUniversalTime() -lt $since) { continue }
                [void]$lines.Add($e)
            }
            $sr.Dispose()
        } catch { [Console]::Error.WriteLine("transcript unreadable, skipped: $($f.Name): $($_.Exception.Message)") }
    }
    return @{ lines = $lines; files = $files.Count }
}

function Session-View($tx, [string]$sid) {
    $uses = @{}; $results = @{}; $hooks = New-Object System.Collections.ArrayList; $version = ''
    foreach ($e in $tx.lines) {
        if ((K $e 'sessionId') -ne $sid -or (K $e 'isSidechain') -eq $true) { continue }
        if (K $e 'version') { $version = K $e 'version' }
        $att = K $e 'attachment'
        if ("$(K $att 'type')" -like 'hook_*') { [void]$hooks.Add($att) }
        $content = K (K $e 'message') 'content'
        if (-not ($content -is [array])) { continue }
        foreach ($c in $content) {
            $id = K $c 'id'; $tid = K $c 'tool_use_id'
            if ((K $c 'type') -eq 'tool_use' -and $id) { $uses[$id] = $c }
            elseif ((K $c 'type') -eq 'tool_result' -and $tid) {
                $body = K $c 'content'
                if ($body -is [array]) { $body = ($body | ForEach-Object { if ($_ -is [System.Collections.IDictionary]) { K $_ 'text' } else { "$_" } }) -join "`n" }
                $results[$tid] = @{ text = "$body"; is_error = [bool](K $c 'is_error') }
            }
        }
    }
    return @{ uses = $uses; results = $results; hooks = $hooks; version = $version }
}

$script:sid = $null
$script:view = $null

function Find-VerifyCalls($tx) {
    @($tx.lines | Where-Object { (K $_ 'isSidechain') -ne $true -and (K $_ 'entrypoint') -eq 'cli' } | ForEach-Object {
        $e = $_; $content = K (K $e 'message') 'content'
        if ($content -is [array]) { foreach ($c in $content) {
            if ((K $c 'type') -eq 'tool_use' -and "$(K (K $c 'input') 'command')" -match 'windows-phase5-gate\.ps1.*-Phase\s+verify') { $e } } } } |
        Sort-Object { K $_ 'timestamp' })
}

function Claude-Ancestor {
    $all = @(Get-CimInstance Win32_Process); $byId = @{}; foreach ($p in $all) { $byId[[int]$p.ProcessId] = $p }
    $cur = $byId[[int]$PID]; $hops = 0
    while ($cur -and $hops -lt 12) { if ($cur.Name -eq 'claude.exe') { return [int]$cur.ProcessId }; $cur = $byId[[int]$cur.ParentProcessId]; $hops++ }
    return $null
}

# V0: versions after the relaunch, and this process belongs to the live session.
try {
    $ver = (Native { & $nx --version }).Trim()
    if ($ver -notmatch [regex]::Escape("version $ExpectedVersion") + '$') { Fail "nx now reports '$ver', expected $ExpectedVersion" }
    $s = Get-Status
    if ($s['service_release_version'] -ne $ExpectedEngine) { Fail ("engine now reports {0}, expected {1}" -f $s['service_release_version'], $ExpectedEngine) }
    $ip = Join-Path $env:USERPROFILE '.claude\plugins\installed_plugins.json'
    $entries = K (K $Json.DeserializeObject((Get-Content -Raw $ip)) 'plugins') 'conexus@nexus-plugins'
    $pv = K $(if ($entries -is [array]) { $entries[0] } else { $entries }) 'version'
    if ($pv -ne $ExpectedVersion) { Fail "conexus plugin is '$pv', expected $ExpectedVersion" }
    # This verify was started by a Claude Code session: claude.exe is an
    # ancestor, and that session's transcript holds the tool call that ran it.
    # Claude Code writes the call's message to the transcript after the tool
    # has started (measured 2026-10-06), so poll for it.
    $claudePid = Claude-Ancestor
    if (-not $claudePid) { Fail 'no claude.exe among this process''s ancestors: run verify from the live session' }
    $deadline = (Get-Date).AddSeconds(60)
    do {
        $tx = Read-Transcripts
        $runs = @(Find-VerifyCalls $tx)
        if ($runs.Count -gt 0) { break }
        Start-Sleep -Seconds 3
    } while ((Get-Date) -lt $deadline)
    if ($runs.Count -eq 0) { Fail 'no interactive (entrypoint cli) session transcript holds the tool call that ran this verify within 60 s: run it from the live session' }
    $script:sid = K $runs[-1] 'sessionId'
    $script:view = Session-View $tx $script:sid
    Record 'V0' 'PASS' ("$ver; engine $($s['service_release_version']); conexus plugin $pv; live session $($script:sid) (Claude Code $($script:view.version)), started under claude.exe pid $claudePid")
} catch {
    Record 'V0' 'FAIL' $_.Exception.Message
}

Run-Assertion 2 'MCP store_put then search from the live session returns the stored text' {
    if (-not $script:view) { Fail 'no live session identified (V0 failed)' }
    $v = $script:view
    $stores = @($v.uses.Values | Where-Object { (K $_ 'name') -eq 'mcp__plugin_conexus_nexus__store_put' -and "$(K (K $_ 'input') 'content')" -like "*$nonce*" })
    if ($stores.Count -eq 0) { Fail "no store_put of the nonce $nonce in live session $($script:sid)" }
    $okStore = @($stores | Where-Object { $v.results.ContainsKey((K $_ 'id')) -and -not $v.results[(K $_ 'id')].is_error })
    if ($okStore.Count -eq 0) { Fail 'the store_put of the nonce has no successful tool_result' }
    $hits = @($v.uses.Values | Where-Object {
        (K $_ 'name') -eq 'mcp__plugin_conexus_nexus__search' -and $v.results.ContainsKey((K $_ 'id')) -and
        -not $v.results[(K $_ 'id')].is_error -and $v.results[(K $_ 'id')].text -like "*$nonce*" })
    if ($hits.Count -eq 0) { Fail "no search tool_result in live session $($script:sid) contains the nonce $nonce" }
    $cli = Invoke-Nx 'nx-search-nonce' @('search', "nx phase5 gate nonce $nonce", '--corpus', 'knowledge', '-c')
    if ($cli.out -notlike "*$nonce*") { Fail "nx search outside the session does not find the nonce (exit $($cli.code), see nx-search-nonce.log)" }
    "session $($script:sid), Claude Code $($script:view.version), engine-service-v$ExpectedEngine; store_put ok ($($okStore.Count)); search returned the nonce ($($hits.Count)); nx search finds it outside the session"
}

Run-Assertion 3 'the conexus hooks fire in the live session' {
    if (-not $script:view) { Fail 'no live session identified (V0 failed)' }
    $h = @($script:view.hooks)
    $ours = @($h | Where-Object { "$(K $_ 'command')" -match '^nx-hook |^uv run .*CLAUDE_PLUGIN_ROOT|nx_hook_shim\.py|nx-session-end-launcher' })
    $bad = @($ours | Where-Object { "$(K $_ 'type')" -ne 'hook_success' -or [int](K $_ 'exitCode') -ne 0 })
    if ($bad.Count -gt 0) { Fail ("{0} conexus hook run(s) failed, first: {1} {2} exit={3}" -f $bad.Count, (K $bad[0] 'type'), (K $bad[0] 'command'), (K $bad[0] 'exitCode')) }
    $start = @($ours | Where-Object { "$(K $_ 'command')" -eq 'nx-hook session-start' -and "$(K $_ 'hookEvent')" -eq 'SessionStart' -and "$(K $_ 'content')$(K $_ 'stdout')".Trim() })
    if ($start.Count -eq 0) { Fail 'nx-hook session-start did not run with output in the live session' }
    $shim = @($ours | Where-Object { "$(K $_ 'command')" -match '^uv run .*nx_hook_shim\.py' -and "$(K $_ 'content')$(K $_ 'stdout')".Trim() })
    if ($shim.Count -eq 0) { Fail 'no uv run ... nx_hook_shim.py hook produced output in the live session' }
    $families = ($ours | ForEach-Object { "$(K $_ 'hookEvent'):" + ("$(K $_ 'command')" -replace '^uv run .*nx_hook_shim\.py', 'shim') } | Sort-Object -Unique) -join ', '
    "conexus hook runs ok: $($ours.Count) (none failed); nx-hook session-start with output: $($start.Count); shim with output: $($shim.Count); seen: $families"
}

function Invoke-ClaudeP([string]$label) {
    $claude = (Get-Command claude -ErrorAction SilentlyContinue).Source
    if (-not $claude) { return @{ ok = $false; why = 'claude not on PATH' } }
    $want = "NXGATE-OK-$nonce"
    $outF = Join-Path $RunDir "claude-p-$label.out"; $errF = Join-Path $RunDir "claude-p-$label.err"; $inF = Join-Path $RunDir 'empty.in'
    Set-Content -Path $inF -Value '' -NoNewline
    $saved = @{}
    foreach ($n in 'CLAUDE_CODE_OAUTH_TOKEN', 'CLAUDECODE', 'CLAUDE_CODE_ENTRYPOINT') { $saved[$n] = [Environment]::GetEnvironmentVariable($n, 'Process') }
    $route = if ([Environment]::GetEnvironmentVariable('NX_HARNESS_CLAUDE_OAUTH_TOKEN', 'Process')) { 'harness token' } else { 'stored login' }
    try {
        # The token only for this child: Claude Code deleted its own copy.
        $harness = [Environment]::GetEnvironmentVariable('NX_HARNESS_CLAUDE_OAUTH_TOKEN', 'Process')
        if ($harness) { [Environment]::SetEnvironmentVariable('CLAUDE_CODE_OAUTH_TOKEN', $harness, 'Process') }
        $harness = $null
        [Environment]::SetEnvironmentVariable('CLAUDECODE', $null, 'Process')
        [Environment]::SetEnvironmentVariable('CLAUDE_CODE_ENTRYPOINT', $null, 'Process')
        $t0 = Get-Date
        $p = Start-Process -FilePath $claude -ArgumentList "-p `"Reply with exactly $want and nothing else`"" -NoNewWindow -PassThru `
            -RedirectStandardOutput $outF -RedirectStandardError $errF -RedirectStandardInput $inF
        $null = $p.Handle   # Windows PowerShell 5.1 loses ExitCode unless the handle is cached
    } finally {
        foreach ($n in @($saved.Keys)) { [Environment]::SetEnvironmentVariable($n, $saved[$n], 'Process') }
    }
    if (-not $p.WaitForExit($ClaudeTimeoutSec * 1000)) {
        & taskkill.exe /T /F /PID $p.Id | Out-Null
        return @{ ok = $false; why = "HANG: did not return within ${ClaudeTimeoutSec}s" }
    }
    $p.WaitForExit()
    $secs = [int]((Get-Date) - $t0).TotalSeconds
    $out = "$(Get-Content -Raw $outF -ErrorAction SilentlyContinue)"
    $err = "$(Get-Content -Raw $errF -ErrorAction SilentlyContinue)"
    if ($p.ExitCode -ne 0) {
        $kind = if ("$out$err" -match 'Not logged in|login|auth|401|credential') { 'AUTH FAILURE (returned, not a hang)' } else { 'error' }
        return @{ ok = $false; why = ("{0}: exit {1} after {2}s via {3}: {4}" -f $kind, $p.ExitCode, $secs, $route, ("$err$out".Trim() -replace '\s+', ' ')) }
    }
    if ($out -notlike "*$want*") { return @{ ok = $false; why = "returned in ${secs}s (exit 0) via $route without the expected reply" } }
    return @{ ok = $true; why = "returned in ${secs}s with the expected reply via $route" }
}

$script:claudeUp = $null
$script:claudeNoEndpoint = $null
function Try-ClaudeP([string]$label) {
    try { return Invoke-ClaudeP $label } catch { return @{ ok = $false; why = "the claude -p leg threw: $($_.Exception.Message)" } }
}
if ($skipIds -notcontains 5) { $script:claudeUp = Try-ClaudeP 'stack-up' }

Run-Assertion 4 'stop --with-pg leaves nothing (launcher stack); the next start is clean; a plain stop leaves PG running' {
    Wait-Healthy $HealthTimeoutSec | Out-Null
    $k = Get-Stack
    if (-not (Supervisor-From-Launcher $k)) { Fail ('the stack under test is not the launcher-started one: ' + (Describe-Stack $k)) }
    $before = Describe-Stack $k

    $r = Invoke-Nx 'stop-with-pg' @('daemon', 'service', 'stop', '--with-pg')
    Wait-Gone @('supervisor', 'engine', 'postgres') 90 | Out-Null
    Start-Sleep -Seconds $RECHECK_SECONDS
    Assert-Absent @('supervisor', 'engine', 'postgres') "${RECHECK_SECONDS}s after stop --with-pg (launcher respawn?)"
    $k = Get-Stack
    # OBSERVATION, not an assertion, by design (nexus-g5rz5): RDR-224 records
    # that `nx daemon service stop` leaves the aspect-worker daemon running, on
    # Windows as on POSIX, because the worker belongs to the store path, not to
    # the service (Revision History: "its decision that `service stop` leaves it
    # running as on POSIX"). Two processes are the venv trampoline and its
    # child. With the stack down it idles on a bounded backoff; the CPU seconds
    # below are what a spin would show.
    $aspect = ($k.aspect | ForEach-Object {
        $cpu = [math]::Round((([double]$_.KernelModeTime) + ([double]$_.UserModeTime)) / 1e7, 1)
        "$($_.ProcessId):cpu=${cpu}s:$($_.CommandLine)"
    }) -join ' | '
    "stop --with-pg (exit $($r.code)) from [$before]: no supervisor/engine/postgres at stop and ${RECHECK_SECONDS}s later; launcher processes left: $($k.launcher.Count); aspect-worker processes left (observed, by design per RDR-224: service stop does not stop the store path's worker): $($k.aspect.Count) $aspect"
    if ($skipIds -notcontains 5) {
        $script:claudeNoEndpoint = Try-ClaudeP 'no-endpoint'
        Assert-Absent @('supervisor', 'engine', 'postgres') 'after the no-endpoint claude -p (it must not start the stack)'
    }

    # Restart through the logon task, so the plain-stop leg also runs against
    # the launcher shape (the launcher exits with its supervisor's clean exit).
    $pgLog = Join-Path $cfg 'postgres\pg.log'
    $lineCount = @(Get-Content $pgLog -ErrorAction SilentlyContinue).Count
    Start-ScheduledTask -TaskName 'NexusStorageService'
    Wait-Healthy $HealthTimeoutSec | Out-Null
    $k = Get-Stack
    if (-not (Supervisor-From-Launcher $k)) { Fail ('after Start-ScheduledTask the stack is not launcher-started: ' + (Describe-Stack $k)) }
    $new = @(Get-Content $pgLog | Select-Object -Skip $lineCount) -join "`n"
    if ($new -match 'was interrupted|not properly shut down|redo starts|automatic recovery') { Fail "next start's pg.log shows crash recovery" }
    if ($new -notmatch 'database system was shut down at') { Fail "next start's pg.log has no 'database system was shut down at' line" }
    'next start (logon task): launcher-started again, clean shutdown recorded, no crash recovery'

    # The Minimum Viable Validation leg: a plain stop leaves PostgreSQL up.
    $r = Invoke-Nx 'stop-plain' @('daemon', 'service', 'stop')
    Wait-Gone @('supervisor', 'engine') 90 | Out-Null
    Start-Sleep -Seconds $RECHECK_SECONDS
    Assert-Absent @('supervisor', 'engine') "${RECHECK_SECONDS}s after plain stop"
    if ((Get-Stack).postgres.Count -eq 0) { Fail 'plain nx daemon service stop also stopped PostgreSQL' }
    "plain stop (exit $($r.code)): supervisor+engine gone and stayed gone, postgres still up"
}

Run-Assertion 5 'a plain claude -p returns, with the stack up and with no endpoint' {
    if (-not $script:claudeUp) { Fail 'the stack-up leg did not run' }
    if (-not $script:claudeUp.ok) { Fail "stack up: $($script:claudeUp.why)" }
    if (-not $script:claudeNoEndpoint) { Fail 'the no-endpoint leg did not run (assertion 4 did not reach the stopped state)' }
    if (-not $script:claudeNoEndpoint.ok) { Fail "no endpoint: $($script:claudeNoEndpoint.why)" }
    $new = @(Credential-Files | Where-Object { $credBefore -notcontains $_.FullName -or $_.LastWriteTimeUtc -gt $since } | ForEach-Object FullName)
    if ($new.Count -gt 0) { Fail ("a credential file appeared during the run: " + ($new -join ', ')) }
    "stack up: $($script:claudeUp.why); no endpoint: $($script:claudeNoEndpoint.why); no credential file under .claude"
}

# -------------------------------------------------------------- verdict ----
$results = $script:state['results']
$executed = @(1..$DECLARED | ForEach-Object { K $results "A$_" } | Where-Object { (K $_ 'status') -in @('PASS', 'FAIL') }).Count
$failed = @(1..$DECLARED | Where-Object { (K (K $results "A$_") 'status') -ne 'PASS' })
$preOk = (K (K $results 'P0') 'status') -eq 'PASS' -and (K (K $results 'V0') 'status') -eq 'PASS'
Write-Output ''
foreach ($id in @('P0', 'V0') + @(1..$DECLARED | ForEach-Object { "A$_" })) {
    $r = K $results $id
    if ($r) { Write-Output ("{0} {1}: {2}" -f $id, (K $r 'status'), (K $r 'detail')) } else { Write-Output "$id NOT RUN" }
}
$summary = "executed=$executed declared=$DECLARED"
Set-Content -Path (Join-Path $RunDir 'verdict.txt') -Value ($Json.Serialize($results)) -Encoding UTF8
if ($executed -ne $DECLARED -or $failed.Count -gt 0 -or -not $preOk) {
    $line = "WINDOWS PHASE 5 GATE FAILED ($summary; not passing: $((@($failed | ForEach-Object { "A$_" }) + @(if (-not $preOk) { 'precondition' })) -join ','))"
    Add-Content -Path (Join-Path $RunDir 'verdict.txt') -Value $line
    Write-Output $line
    exit 1
}
$line = "WINDOWS PHASE 5 GATE PASSED ($summary)"
Add-Content -Path (Join-Path $RunDir 'verdict.txt') -Value $line
Write-Output $line
exit 0
