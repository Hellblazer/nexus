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
#   The whole unattended procedure, from the checkpoint restore to the verdict
#   line and the guest's shutdown, is one command on the Mac:
#      tests/e2e/windows-guest/run-gate.sh --version <v> --engine <e> [--wheel PATH] [--out DIR]
#   (the scripts it stages on the host are in tests/e2e/windows-guest/driver/).
#   It exits 0 only when the collected verdict line is PASSED, and --wheel runs a
#   candidate wheel instead of the published conexus==<v>, named as such in the
#   output. Steps 0 to 6 above are what it does.
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
#       hooks/scripts/nx_hook_shim.py launcher (nexus-efk2h) succeeds with
#       output, and no conexus hook run failed. The hooks are matched by WHAT
#       they launch (an nx-hook verb, the shim, any script under hooks/scripts,
#       nx-session-end-launcher, an mcp_tool hook), not by how the uv launcher
#       is spelled: since 7.74.0 every uv entry in conexus/hooks/hooks.json is
#       `uv tool run ... python <script>`, before it `uv run ...`. A pytest pin
#       (tests/test_windows_phase5_gate_script.py) renders every hooks.json
#       entry the way Claude Code records it and matches it against the
#       HOOK_OURS_RE / HOOK_SHIM_RE literals below, so the filters cannot drift
#       from hooks.json unnoticed. SessionEnd (nx-session-end-launcher) fires
#       after the session and is not observable here.
#   4   against the launcher-started stack: nx daemon service stop --with-pg
#       leaves no supervisor, engine or postgres process, confirmed by the
#       process table and again RECHECK_SECONDS later (the launcher respawns
#       30 s after a non-zero supervisor exit); the next start, through the
#       logon task again, shows a clean shutdown in pg.log and no crash
#       recovery; then the Minimum Viable Validation leg, on that
#       launcher-started stack: a plain nx daemon service stop leaves
#       PostgreSQL up and the supervisor and engine stay down. Every stop (this
#       assertion's two, and assertion 1 leg B's) is also timed, and FAILS the
#       assertion when its output carries a degraded-path log event
#       (STOP_DEGRADED_RE) or it takes longer than STOP_MAX_SECONDS: a hard-kill
#       fallback that always fires still leaves the processes gone (nexus-f9bgu.40 S2).
#   5   a plain `claude -p` returns within ClaudeTimeoutSec, twice: with the
#       stack up, and with no endpoint (inside assertion 4, after the
#       --with-pg stop; the RDR-218 leg). Claude Code deletes
#       CLAUDE_CODE_OAUTH_TOKEN from its own environment (T2
#       nexus_rdr/219-research-15), so the launcher also exports
#       NX_HARNESS_CLAUDE_OAUTH_TOKEN and this script maps it back for its
#       child only (RDR-219 "The nx-mcp dispatch grant"). An auth failure is
#       reported as such, distinct from a hang. No credential file may appear
#       under %USERPROFILE%\.claude during the run, and no Credential Manager
#       target naming Claude or Anthropic may appear between setup and verify
#       (cmdkey /list target names; other new targets are reported).
#
# NON-VACUITY: an assertion counts only when it executed (PASS or FAIL). A
#   -ForceSkip'd or never-reached assertion is recorded SKIPPED, and the
#   verdict is FAILED whenever executed != DECLARED, any assertion failed, or a
#   precondition failed. Off Windows the script refuses with exit 2.
#
# WHAT A GREEN RUN DOES NOT COVER (each item names its bead;
#   RDR and T2 references are where the item is recorded)
#   - nx daemon service install-binary <tag>, the Minimum Viable Validation's
#     second install path (default PG bundle, and --no-pg-bundle). The gate runs
#     only nx init --service; both share install_binary() and install_pg_bundle(),
#     so the library path runs, but the CLI wrapper, its default and its flag
#     do not. RDR-224 Minimum Viable Validation; T2 nexus_rdr/224-review-p5-critique S3;
#     nexus-f9bgu.53.
#   - A stock retail Windows 11 standard-user box. The guest is Windows 11
#     Enterprise Evaluation (build 26300) with a filtered administrator token
#     (P0 records the token). T2 nexus_rdr/224-gate-run-7.72.1-2026-10-07
#     "Not covered"; nexus-f9bgu.53.
#   - A box with no developer tools. The guest has Git, uv, a uv-managed Python
#     and Claude Code installed before the run (procedure step 1); P0 guards
#     only the VC++ runtime. RDR-224 Minimum Viable Validation says "no
#     developer tools"; nexus-f9bgu.53.
#   - Host sleep. Unmeasured; the RDR assumption stays unchecked. RDR-224
#     Critical Assumptions (host shutdown, logoff or sleep), T2
#     nexus_rdr/224-research-32; nexus-f9bgu.46 measured sign-out and restart
#     only; sleep is nexus-f9bgu.53.
#   - Autostart at the next logon and the sign-out/restart path: measured by
#     nexus-f9bgu.46, fixed by nexus-f9bgu.51, not re-asserted here.
#   - Windows Credential Manager beyond target names. Assertion 5 lists file
#     names at depth 1 under %USERPROFILE%\.claude and compares cmdkey /list
#     target names between setup and verify; it reads no secret, fails only on
#     a new target naming Claude or Anthropic, and a non-English listing reads
#     as unmeasured. RDR-219 inventory; nexus-pwscd; nexus-f9bgu.53.
#   - Windows on ARM. Out of scope by decision (no GraalVM windows-aarch64
#     target, no onnxruntime Windows-ARM64 library). RDR-224 Technical
#     Environment and Briefly Rejected; nexus-f9bgu.53.
#   - Smart App Control in enforcing mode. RDR-224 Test Plan (the Smart App
#     Control scenario) and Critical Assumptions; nexus-f9bgu.53.
#   - The desktop bundle: nexus-ijue9.21.
#   - SessionEnd (nx-session-end-launcher) fires after the session and is not
#     observable here; nexus-f9bgu.53.
#   - The aspect-worker daemon, which is only observed (process count, CPU
#     seconds and command lines after the --with-pg stop). It outlives
#     `service stop` by design: RDR-224's recorded decision (Revision History,
#     "`service stop` leaves it running as on POSIX"), nexus-g5rz5.
#
# THE RECORD: the verdict (the last line of the output and of <RunDir>\verdict.txt)
#   is preceded by a line giving the SHA-256 of this script file as it ran on the
#   guest (and whether the setup phase ran the same file), and the V0 and A2
#   details carry the full Claude session id and the engine endpoint.
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
# Stop quality (nexus-f9bgu.40 S2). A graceful stop measured 2.2 to 3.2 s on the
# guest after e16fb79f2 (stop --with-pg 3.1 s); the broken path took 41 s: a
# 20 s helper timeout (bounded_subprocess_timeout, win_console_helper_failed),
# then the 20 s supervisor grace, then a hard kill (storage_service_supervisor_
# unclean_stop). 15 s sits well above the graceful runs and below the first
# 20 s timeout, so any run of the old path fails on time alone as well. The
# strings are what src/nexus logs (a pytest pin greps src/nexus for each).
$STOP_MAX_SECONDS = 15
$STOP_DEGRADED_RE = 'unclean_stop|hard-killing|win_console_helper_failed|bounded_subprocess_timeout'
# Assertion 3 hook filters. Claude Code records a hook's command as the command
# and its args joined by spaces, ${CLAUDE_PLUGIN_ROOT} unexpanded, and an
# mcp_tool hook as <server>/<tool>. They name what is launched, never the
# launcher: `uv run` (before 7.74.0) and `uv tool run` (since) both match. Keep
# the patterns in the subset .NET and Python share (alternation, anchors,
# character classes, an escaped dot): tests/test_windows_phase5_gate_script.py
# reads these two literals and applies them to conexus/hooks/hooks.json with
# Python's re. PowerShell -match is case-insensitive. A hook script under any
# plugin's hooks/scripts matches too (the recorded command keeps
# ${CLAUDE_PLUGIN_ROOT} unexpanded, so the plugin cannot be told apart); the
# guest has only the conexus plugin installed.
# RDR-228: the nx-hook entries run as `uv ... launch/nx_launch.py nx-hook <verb>`,
# so the clause names the launcher and the program, not the start of the command.
$HOOK_OURS_RE = 'nx_launch\.py nx-hook |nx-session-end-launcher|^plugin:conexus:nexus/|hooks/scripts/[^ ]+\.py'
$HOOK_SHIM_RE = 'nx_hook_shim\.py'
if (-not $RunDir) { $RunDir = Join-Path $env:USERPROFILE 'nx-gate\run' }

Add-Type -AssemblyName System.Web.Extensions
$Json = New-Object System.Web.Script.Serialization.JavaScriptSerializer
$Json.MaxJsonLength = [int]::MaxValue

$nx = Join-Path $env:USERPROFILE '.local\bin\nx.exe'
$cfg = Join-Path $env:USERPROFILE '.config\nexus'
$statePath = Join-Path $RunDir 'state.json'
New-Item -ItemType Directory -Force -Path $RunDir | Out-Null
# The identity of this script as it runs on the guest (the staged copy), recorded
# in the verdict: "gate script = develop <sha>" in a T2 record is otherwise the
# operator's claim (nexus-f9bgu.40 Observation 3).
$scriptSha = 'unavailable'
try { $scriptSha = (Get-FileHash -Algorithm SHA256 -LiteralPath $PSCommandPath).Hash.ToLowerInvariant() } catch { $scriptSha = 'unavailable' }

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

# Every stop an assertion ran, with its exit code and measured seconds. A FAIL's
# detail is the exception message and replaces the body, so a failed assertion 4
# lost both stops' seconds (7.77.1, 2026-10-12, nexus-f9bgu.53). The notes live
# outside the body, and Run-Assertion appends them to the record on a PASS and
# on a FAIL alike.
$script:stopNotes = New-Object System.Collections.ArrayList
function Note-Stop($stop, [string]$label) {
    [void]$script:stopNotes.Add(("{0}: exit {1} in {2}s" -f $label, $stop.code, $stop.secs))
}
function Stop-Notes {
    if ($script:stopNotes.Count -eq 0) { return '' }
    return ' [stops measured: ' + ($script:stopNotes -join '; ') + ']'
}

function Run-Assertion([int]$id, [string]$name, [scriptblock]$body) {
    $key = "A$id"
    if ($skipIds -contains $id) { Record $key 'SKIPPED' "$name (forced by -ForceSkip)"; return }
    Say "A$id $name"
    $script:stopNotes.Clear()
    try { $detail = (& $body | Out-String).Trim(); Record $key 'PASS' ($detail + (Stop-Notes)) }
    catch { Record $key 'FAIL' ($_.Exception.Message + (Stop-Notes)) }
}

function Invoke-Nx([string]$label, [string[]]$nxArgs) {
    $log = Join-Path $RunDir "$label.log"
    $sw = [System.Diagnostics.Stopwatch]::StartNew()
    $out = Native { & $nx @nxArgs }
    $code = $LASTEXITCODE
    $sw.Stop()
    Set-Content -Path $log -Value $out -Encoding UTF8
    return @{ code = $code; out = $out; log = $log; secs = [math]::Round($sw.Elapsed.TotalSeconds, 1) }
}

# '' when the stop was graceful and quick; otherwise what was wrong with it.
function Stop-Problem($stop, [string]$what) {
    if ("$($stop.out)" -match $STOP_DEGRADED_RE) {
        return ("{0} took the degraded path ({1} in its output, {2}s, see {3})" -f $what, $Matches[0], $stop.secs, $stop.log)
    }
    if ($stop.secs -gt $STOP_MAX_SECONDS) {
        return ("{0} took {1}s, over the {2}s bound (see {3})" -f $what, $stop.secs, $STOP_MAX_SECONDS, $stop.log)
    }
    return ''
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

# Target names (never secrets) in this account's Windows Credential Manager,
# from cmdkey's English output. Ok is false when the output is not that shape,
# so a localized or failed listing reads as unmeasured, not as empty.
function Credential-Targets {
    $out = @(& "$env:windir\System32\cmdkey.exe" /list 2>$null)
    $ok = ($LASTEXITCODE -eq 0) -and (($out -join "`n") -match 'Currently stored credentials')
    $targets = @($out | ForEach-Object { if ("$_" -match '^\s*Target:\s*(.+?)\s*$') { $Matches[1] } })
    return @{ ok = $ok; targets = $targets }
}

# ---------------------------------------------------------------- setup ----
if ($Phase -eq 'setup') {
    $script:state = @{
        started_at = (Now-Utc); expected_version = $ExpectedVersion; expected_engine = $ExpectedEngine
        nonce = ([guid]::NewGuid().ToString('N').Substring(0, 12)); results = @{}
        script_sha256 = $scriptSha
    }
    $ct = Credential-Targets
    $script:state['cred_targets_ok'] = [bool]$ct.ok
    $script:state['cred_targets'] = @($ct.targets)
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
        Note-Stop $stop 'A1 leg B --with-pg'
        # Judged at the end of the assertion so the rest of the run still happens.
        $legBStop = Stop-Problem $stop 'leg B stop --with-pg'
        "leg B stop --with-pg: exit $($stop.code) in $($stop.secs)s"
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
        if ($legBStop) { Fail $legBStop }
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
    Write-Output '  then search for it with mcp__plugin_conexus_nexus__search, then run this in the background and wait for it'
    Write-Output '  to finish (a run takes 4 to 8 minutes and up to 20 with every health wait exhausted, past the 600 s cap of a shell tool call):'
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
    # The endpoint the stack answered on, from `nx daemon service status` (no new probe).
    $script:endpoint = if ($s['host']) { "$($s['host']):$($s['port'])" } else { 'unknown' }
    Record 'V0' 'PASS' ("$ver; engine $($s['service_release_version']) at $($s['host']):$($s['port']); conexus plugin $pv; live session $($script:sid) (Claude Code $($script:view.version)), started under claude.exe pid $claudePid")
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
    "session $($script:sid), Claude Code $($script:view.version), engine-service-v$ExpectedEngine at $script:endpoint; store_put ok ($($okStore.Count)); search returned the nonce ($($hits.Count)); nx search finds it outside the session"
}

Run-Assertion 3 'the conexus hooks fire in the live session' {
    if (-not $script:view) { Fail 'no live session identified (V0 failed)' }
    $h = @($script:view.hooks)
    $ours = @($h | Where-Object { "$(K $_ 'command')" -match $HOOK_OURS_RE })
    $bad = @($ours | Where-Object { "$(K $_ 'type')" -ne 'hook_success' -or [int](K $_ 'exitCode') -ne 0 })
    if ($bad.Count -gt 0) { Fail ("{0} conexus hook run(s) failed, first: {1} {2} exit={3}" -f $bad.Count, (K $bad[0] 'type'), (K $bad[0] 'command'), (K $bad[0] 'exitCode')) }
    $start = @($ours | Where-Object { "$(K $_ 'command')" -eq 'nx-hook session-start' -and "$(K $_ 'hookEvent')" -eq 'SessionStart' -and "$(K $_ 'content')$(K $_ 'stdout')".Trim() })
    if ($start.Count -eq 0) { Fail 'nx-hook session-start did not run with output in the live session' }
    $shim = @($ours | Where-Object { "$(K $_ 'command')" -match $HOOK_SHIM_RE -and "$(K $_ 'content')$(K $_ 'stdout')".Trim() })
    if ($shim.Count -eq 0) { Fail 'no nx_hook_shim.py hook produced output in the live session' }
    # A script hook reads as the script and its arguments; an nx-hook verb as itself.
    $families = ($ours | ForEach-Object { "$(K $_ 'hookEvent'):" + ("$(K $_ 'command')" -replace '^.*hooks/scripts/', '') } | Sort-Object -Unique) -join ', '
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
    # Stop quality is judged at the end, so the next-start and claude -p legs
    # still run and A5 still has its no-endpoint reading.
    $stopProblems = New-Object System.Collections.ArrayList

    $r = Invoke-Nx 'stop-with-pg' @('daemon', 'service', 'stop', '--with-pg')
    Note-Stop $r 'A4 --with-pg'
    $sp = Stop-Problem $r 'stop --with-pg'
    if ($sp) { [void]$stopProblems.Add($sp) }
    $withPgSecs = $r.secs
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
    "stop --with-pg (exit $($r.code), $($r.secs)s, bound ${STOP_MAX_SECONDS}s) from [$before]: no supervisor/engine/postgres at stop and ${RECHECK_SECONDS}s later; launcher processes left: $($k.launcher.Count); aspect-worker processes left (observed, by design per RDR-224: service stop does not stop the store path's worker): $($k.aspect.Count) $aspect"
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
    Note-Stop $r 'A4 plain'
    $sp = Stop-Problem $r 'plain stop'
    if ($sp) { [void]$stopProblems.Add($sp) }
    Wait-Gone @('supervisor', 'engine') 90 | Out-Null
    Start-Sleep -Seconds $RECHECK_SECONDS
    Assert-Absent @('supervisor', 'engine') "${RECHECK_SECONDS}s after plain stop"
    if ((Get-Stack).postgres.Count -eq 0) { Fail 'plain nx daemon service stop also stopped PostgreSQL' }
    if ($stopProblems.Count -gt 0) { Fail ('stop quality: ' + ($stopProblems -join '; ')) }
    "plain stop (exit $($r.code), $($r.secs)s, bound ${STOP_MAX_SECONDS}s; stop --with-pg took ${withPgSecs}s): supervisor+engine gone and stayed gone, postgres still up"
}

Run-Assertion 5 'a plain claude -p returns, with the stack up and with no endpoint' {
    if (-not $script:claudeUp) { Fail 'the stack-up leg did not run' }
    if (-not $script:claudeUp.ok) { Fail "stack up: $($script:claudeUp.why)" }
    if (-not $script:claudeNoEndpoint) { Fail 'the no-endpoint leg did not run (assertion 4 did not reach the stopped state)' }
    if (-not $script:claudeNoEndpoint.ok) { Fail "no endpoint: $($script:claudeNoEndpoint.why)" }
    $new = @(Credential-Files | Where-Object { $credBefore -notcontains $_.FullName -or $_.LastWriteTimeUtc -gt $since } | ForEach-Object FullName)
    if ($new.Count -gt 0) { Fail ("a credential file appeared during the run: " + ($new -join ', ')) }
    # Credential Manager: a new target naming Claude or Anthropic fails; any other
    # new target is reported. Unreadable either time is reported as unmeasured.
    $ctNow = Credential-Targets
    if ((K $script:state 'cred_targets_ok') -and $ctNow.ok) {
        $ctBefore = K $script:state 'cred_targets'
        if ($null -eq $ctBefore) { $ctBefore = @() }
        $ctNew = @($ctNow.targets | Where-Object { $ctBefore -notcontains $_ })
        $ctBad = @($ctNew | Where-Object { $_ -match 'claude|anthropic' })
        if ($ctBad.Count -gt 0) { Fail ("a Claude credential appeared in Credential Manager during the run: " + ($ctBad -join ', ')) }
        $cm = "Credential Manager targets $($ctBefore.Count) before, $(@($ctNow.targets).Count) after, new: [$($ctNew -join ', ')]"
    } else {
        $cm = 'Credential Manager not measured (cmdkey /list unreadable)'
    }
    "stack up: $($script:claudeUp.why); no endpoint: $($script:claudeNoEndpoint.why); no credential file under .claude; $cm"
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
# Which script produced this verdict: its hash on the guest, and whether setup ran the same file.
$setupSha = "$(K $script:state 'script_sha256')"
$sameAsSetup = if ($setupSha -eq $scriptSha) { 'the setup phase ran the same file' } else { "the setup phase ran sha256 ${setupSha}, a DIFFERENT file" }
$shaLine = "gate script sha256 $scriptSha ($PSCommandPath; $sameAsSetup)"
Write-Output $shaLine
Add-Content -Path (Join-Path $RunDir 'verdict.txt') -Value $shaLine
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
