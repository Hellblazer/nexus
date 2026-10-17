# SPDX-License-Identifier: AGPL-3.0-or-later
#
# Guest (sent by guest-run.ps1 over PowerShell Direct). Step 3 of run-gate.sh:
# the gate's step 1 installs, in this order: uv, conexus (from PyPI at
# -Version, or from the candidate wheel in nx-gate when -Wheel is given), Claude
# Code, Git (winget), and the conexus plugin through the claude CLI. Ends with
# one "NXG-RESULT:" line: ok only when nx reports -Version and claude, git and
# the plugin record are all present. Git is installed only when it is absent:
# under run-gate.sh --standard-user this script runs as an account that cannot
# install it, and the administrator has installed it first (stduser.ps1). When
# nx-gate holds portable-git.ps1 (run-gate.sh --no-admin-git) that is run
# instead of winget: it is the PortableGit block of docs/windows-install.md, as
# written there, which needs no administrator rights and puts Git under
# %LOCALAPPDATA%\Programs\Git.
# run-gate.sh --cache on (nexus-bpzt7): cache.ps1 seeds nx-gate\payloads with what the host kept from earlier
# runs, and this script uses each item only when its sha256 is the one the upstream publishes for that version
# (uv's zip: the .sha256 next to the release; PortableGit: the digest the install guide's block already checks;
# the Git installer: winget's own record of it), logs why when it does not, and then downloads as before. A
# payload it had to download is recorded in payloads\record.txt for the harvest step. With --cache off nx-gate
# has no payloads folder and none of this runs: every command below the "stock" comments is the fresh-download
# path, unchanged.
param(
    [Parameter(Mandatory = $true)][string]$Version,
    [string]$Wheel = ''
)
$ErrorActionPreference = 'Continue'
$b = "$env:USERPROFILE\nx-gate"
# run-gate.sh --applocker: the stduser step left a copy of whoami.exe here and saw a start of it blocked.
# Start it again as this account, so this log shows the policy acting, and name this script's language mode.
if (Test-Path "$b\applocker-control.exe") {
    try { $o = & "$b\applocker-control.exe" 2>&1 | Out-String; "applocker control run: started, exit=$LASTEXITCODE out=$($o.Trim())" } catch { "applocker control run: blocked: $($_.Exception.Message)" }
    "applocker language mode: $($ExecutionContext.SessionState.LanguageMode)"
}
# run-gate.sh --wdac-audit / --wdac-allow: the stduser step built an unsigned program here and saw its start recorded.
if (Test-Path "$b\wdac-control.exe") {
    try { $o = & "$b\wdac-control.exe" 2>&1 | Out-String; "wdac control run: started, exit=$LASTEXITCODE out=$($o.Trim())" } catch { "wdac control run: blocked: $($_.Exception.Message)" }
    "wdac language mode: $($ExecutionContext.SessionState.LanguageMode)"
}
function Step($n, [scriptblock]$sb) { $t0 = Get-Date; "=== $n"; & $sb 2>&1 | Out-String -Width 200 | % { $_.TrimEnd() } | Select-Object -Last 12; "--- $n took $([int]((Get-Date)-$t0).TotalSeconds)s" }
# nx-gate\payloads exists only when cache.ps1 seeded it (--cache on): it is the one test for every cache read below.
$pay = "$b\payloads"
$cacheOn = Test-Path -LiteralPath $pay
function Record($line) { Add-Content -LiteralPath "$pay\record.txt" -Value $line -Encoding ASCII }
# Invoke-RestMethod hands back bytes for some content types (GitHub's .sha256 assets are octet-stream).
function AsText($r) { if ($r -is [byte[]]) { [Text.Encoding]::UTF8.GetString($r) } else { "$r" } }
# uv's installer script names its version and downloads one zip from UV_DOWNLOAD_URL (it does not check a hash).
# With the zip cached for that version and its sha256 equal to the one published beside the release, the same
# installer script runs against a file: URL for the cached copy; anything else is the stock installer.
function CachedUv {
    try {
        $text = AsText (Invoke-RestMethod -Uri 'https://astral.sh/uv/install.ps1' -ErrorAction Stop)
        if ($text -match "app_version = '([0-9]+\.[0-9]+\.[0-9]+)'") { $ver = $Matches[1] } else { return 'uv-cache: the installer script names no version' }
        $want = ((AsText (Invoke-RestMethod -Uri "https://github.com/astral-sh/uv/releases/download/$ver/uv-x86_64-pc-windows-msvc.zip.sha256" -ErrorAction Stop)) -split '\s+')[0].ToLowerInvariant()
        if ($want -notmatch '^[0-9a-f]{64}$') { return "uv-cache: no usable published sha256 for uv $ver" }
        $dir = "$pay\uv-$ver"
        $zip = "$dir\uv-x86_64-pc-windows-msvc.zip"
        if (-not (Test-Path -LiteralPath $zip)) { Record "uv=$ver|$want"; return "uv-cache: no cached uv $ver" }
        $got = (Get-FileHash -Algorithm SHA256 -LiteralPath $zip).Hash.ToLowerInvariant()
        if ($got -ne $want) { Record "uv=$ver|$want"; return "uv-cache: cached uv $ver sha256 $got is not the published $want" }
        $env:UV_DOWNLOAD_URL = ([Uri]"$dir\").AbsoluteUri.TrimEnd('/')
        try { powershell -NoProfile -ExecutionPolicy Bypass -Command 'irm https://astral.sh/uv/install.ps1 | iex' } finally { Remove-Item -LiteralPath Env:\UV_DOWNLOAD_URL -ErrorAction SilentlyContinue }
        $v = "$(& "$env:USERPROFILE\.local\bin\uv.exe" --version)"
        if ($v -notlike "uv $ver*") { return "uv-cache: after the cached install uv reports '$v', not $ver" }
        return "uv-cache: used uv $ver (sha256 matches the published one)"
    } catch { return "uv-cache: $($_.Exception.Message)" }
}
Step 'uv' {
    $used = $false
    if ($cacheOn) { $m = @(CachedUv); $m; $used = @($m | Where-Object { $_ -like 'uv-cache: used *' }).Count -gt 0 }
    # stock: the official installer, as the install guide gives it
    if (-not $used) { powershell -NoProfile -ExecutionPolicy Bypass -Command 'irm https://astral.sh/uv/install.ps1 | iex' }
}
$env:PATH = "$env:USERPROFILE\.local\bin;" + $env:PATH
$source = if ($Wheel) { "candidate wheel $Wheel" } else { "conexus==$Version from PyPI" }
# uv keeps the Python archive it downloads in UV_PYTHON_CACHE_DIR and installs from it when it is there.
if ($cacheOn) { New-Item -ItemType Directory -Force "$pay\python" | Out-Null; $env:UV_PYTHON_CACHE_DIR = "$pay\python" }
# cache.ps1 wrote this marker only when uv's seeded cache holds the install of exactly this published version
# (never for a candidate wheel): then conexus is installed with uv --offline, so PyPI is not asked at all.
$offline = $cacheOn -and -not $Wheel -and ((@(Get-Content -LiteralPath "$pay\uv-offline.txt" -ErrorAction SilentlyContinue) -join '') -eq "conexus=$Version")
Step "conexus $source" {
    if ($Wheel) {
        "wheel sha256=" + (Get-FileHash -Algorithm SHA256 -LiteralPath "$b\$Wheel").Hash.ToLowerInvariant()
        uv tool install --python 3.12 "$b\$Wheel" 2>&1 | Select-Object -Last 3; $rc = $LASTEXITCODE; "uv exit=$rc"
    } else {
        $done = $false
        if ($offline) {
            "pypi-cache: the seeded uv cache holds conexus==$Version; installing with --offline"
            uv python install 3.12 2>&1 | Select-Object -Last 2
            uv tool install --offline --python 3.12 "conexus==$Version" 2>&1 | Select-Object -Last 3; $rc = $LASTEXITCODE; "uv exit=$rc (offline)"
            $done = ($rc -eq 0) -and ((@(nx --version 2>&1) -join ' ') -like "*version $Version*")
            if (-not $done) { 'pypi-cache: the offline install did not give this version; resolving against PyPI as usual' }
        }
        # stock: resolve and download from PyPI
        if (-not $done) {
            uv tool install --python 3.12 "conexus==$Version" 2>&1 | Select-Object -Last 3; $rc = $LASTEXITCODE; "uv exit=$rc (attempt 1)"
            if ($rc -ne 0) { 'RETRY: waiting 90 s, then one more attempt at the same version'; Start-Sleep 90; uv tool install --python 3.12 "conexus==$Version" 2>&1 | Select-Object -Last 6; "uv exit=$LASTEXITCODE (attempt 2)" }
        }
    }
    nx --version
    $sp = "$env:APPDATA\uv\tools\conexus\Lib\site-packages"
    Get-ChildItem $sp -Filter 'conexus-*.dist-info' | ForEach-Object { "dist-info: $($_.Name); direct_url.json present=" + (Test-Path (Join-Path $_.FullName 'direct_url.json')) + "; INSTALLER=" + ((Get-Content (Join-Path $_.FullName 'INSTALLER') -ErrorAction SilentlyContinue) -join ' ') }
    # Informational only: this probe looks at one file and one string that matter for one release's
    # fix. It never fails the step and prints no error when the file or the string has moved.
    try {
        $pp = "$sp\nexus\db\pg_provision.py"
        if (Test-Path -LiteralPath $pp) {
            $hits = @(Select-String -LiteralPath $pp -SimpleMatch 'CREATE_NEW_PROCESS_GROUP | CREATE_NO_WINDOW' -ErrorAction Stop)
            "informational: installed pg_provision.py has $($hits.Count) line(s) with 'CREATE_NEW_PROCESS_GROUP | CREATE_NO_WINDOW'"
            $hits | ForEach-Object { "  line {0}: {1}" -f $_.LineNumber, $_.Line.Trim() }
            "informational: installed pg_provision.py sha256=" + (Get-FileHash -Algorithm SHA256 -LiteralPath $pp -ErrorAction Stop).Hash.ToLowerInvariant()
        } else { "informational: no installed pg_provision.py at $pp (nothing to probe)" }
    } catch { "informational: the pg_provision.py probe was skipped: $($_.Exception.Message)" }
}
# run-gate.sh --cache on (nexus-bpzt7): when cache.ps1 left a binary in claude-cache, do what
# https://claude.ai/install.ps1 does (latest version, its manifest's sha256, then `claude.exe install`) with that
# file in place of the download, and only if its sha256 is the manifest's for the latest version. Anything else
# (no cached file, an older version, a different hash, a lookup that fails) runs the official installer as before.
function CachedClaude {
    $base = 'https://downloads.claude.ai/claude-code-releases'
    try {
        $ver = "$(Invoke-RestMethod -Uri "$base/latest" -ErrorAction Stop)".Trim()
        if ($ver -notmatch '^[0-9]+\.[0-9]+\.[0-9]+$') { return "claude-cache: latest version not usable ($ver)" }
        $want = (Invoke-RestMethod -Uri "$base/$ver/manifest.json" -ErrorAction Stop).platforms.'win32-x64'.checksum
        $f = "$b\claude-cache\claude-$ver-win32-x64.exe"
        if (-not (Test-Path $f)) { return "claude-cache: no cached $ver" }
        $got = (Get-FileHash -Algorithm SHA256 -LiteralPath $f).Hash.ToLowerInvariant()
        if ($got -ne "$want".ToLowerInvariant()) { return "claude-cache: cached $ver sha256 $got is not the manifest's $want" }
        $dl = "$env:USERPROFILE\.claude\downloads"
        New-Item -ItemType Directory -Force $dl | Out-Null
        Copy-Item -LiteralPath $f -Destination "$dl\claude-$ver-win32-x64.exe" -Force
        & "$dl\claude-$ver-win32-x64.exe" install; $rc = $LASTEXITCODE
        Remove-Item -LiteralPath "$dl\claude-$ver-win32-x64.exe" -Force -ErrorAction SilentlyContinue
        if ($rc -ne 0) { return "claude-cache: claude install exited $rc" }
        return "claude-cache: used $ver (sha256 matches the manifest)"
    } catch { return "claude-cache: $($_.Exception.Message)" }
}
Step 'claude code' {
    $used = $false
    if (Test-Path "$b\claude-cache") { $m = CachedClaude; $m; $used = $m -like 'claude-cache: used *' }
    if (-not $used) { powershell -NoProfile -ExecutionPolicy Bypass -Command 'irm https://claude.ai/install.ps1 | iex' }
    & "$env:USERPROFILE\.local\bin\claude.exe" --version
}
$gitDirs = @('C:\Program Files\Git\cmd', "$env:LOCALAPPDATA\Programs\Git\cmd")
function GitDir { $gitDirs | Where-Object { Test-Path "$_\git.exe" } | Select-Object -First 1 }
# PortableGit: the install guide's block, run unchanged, but with Invoke-WebRequest shadowed by a function that
# copies the cached installer for the URL's file name (the version is in it) instead of downloading it. The
# block still compares the file with the digest GitHub publishes and throws on a mismatch, so a cached file is
# used only if it is the upstream's. A download that does happen is kept and recorded for the harvest.
$portableWrapper = @'
param([string]$Pay, [string]$Use)
$script:fetched = ''
function Invoke-WebRequest {
    param([Parameter(Position = 0)][string]$Uri, [string]$OutFile, [switch]$UseBasicParsing)
    $leaf = ($Uri -split '/')[-1]
    $hit = Join-Path $Pay $leaf
    if ($Use -eq 'yes' -and $OutFile -and $leaf -match '^PortableGit-.*-64-bit\.7z\.exe$' -and (Test-Path -LiteralPath $hit)) {
        Copy-Item -LiteralPath $hit -Destination $OutFile -Force
        "portable-git cache: using the cached $leaf (the block checks it against GitHub's digest)"
        return
    }
    if ($leaf -match '^PortableGit-.*-64-bit\.7z\.exe$') { $script:fetched = $leaf }
    Microsoft.PowerShell.Utility\Invoke-WebRequest @PSBoundParameters
}
. "$PSScriptRoot\portable-git.ps1"
if ($script:fetched -match '^PortableGit-(.+)-64-bit\.7z\.exe$') {
    $ver = $Matches[1]
    Copy-Item -LiteralPath "$env:TEMP\PortableGit.exe" -Destination (Join-Path $Pay $script:fetched) -Force
    Add-Content -LiteralPath (Join-Path $Pay 'record.txt') -Value ("portablegit=$ver|" + (Get-FileHash -Algorithm SHA256 -LiteralPath "$env:TEMP\PortableGit.exe").Hash.ToLowerInvariant()) -Encoding ASCII
}
'@
# winget's Git is Git for Windows' own installer; winget names its version, URL and sha256 (winget show). With
# that installer cached under its file name and its sha256 equal to winget's, it is run with the switches the
# Git.Git manifest gives for a silent install (2026-10-17: /SP- /VERYSILENT /SUPPRESSMSGBOXES /NORESTART);
# anything else is the stock winget install.
function CachedWingetGit {
    try {
        $show = "$(winget show --id Git.Git -e --source winget --accept-source-agreements --disable-interactivity 2>&1 | Out-String)"
        $url = ''; $sha = ''
        if ($show -match '(?m)^\s*Installer Url:\s*(\S+)') { $url = $Matches[1] }
        if ($show -match '(?m)^\s*Installer SHA256:\s*([0-9A-Fa-f]{64})') { $sha = $Matches[1].ToLowerInvariant() }
        $leaf = ($url -split '/')[-1]
        if (-not $sha -or $url -notlike 'https://github.com/git-for-windows/git/releases/download/*' -or $leaf -notmatch '^Git-(.+)-64-bit\.exe$') { return 'git-cache: winget names no usable x64 installer' }
        $ver = $leaf -replace '^Git-(.+)-64-bit\.exe$', '$1'
        $f = "$pay\$leaf"
        if (-not (Test-Path -LiteralPath $f)) { Record "winget-git=$ver|$url|$sha"; return "git-cache: no cached $leaf" }
        $got = (Get-FileHash -Algorithm SHA256 -LiteralPath $f).Hash.ToLowerInvariant()
        if ($got -ne $sha) { Record "winget-git=$ver|$url|$sha"; return "git-cache: cached $leaf sha256 $got is not winget's $sha" }
        $p = Start-Process -FilePath $f -ArgumentList '/SP-', '/VERYSILENT', '/SUPPRESSMSGBOXES', '/NORESTART' -Wait -PassThru
        if ($p.ExitCode -ne 0) { return "git-cache: the cached installer exited $($p.ExitCode)" }
        return "git-cache: used $leaf (sha256 matches winget's)"
    } catch { return "git-cache: $($_.Exception.Message)" }
}
Step 'git (winget)' {
    if (GitDir) { 'Git is already installed; not installing it' }
    elseif (Test-Path "$b\portable-git.ps1") {
        'PortableGit, from the block in docs/windows-install.md, as this account'
        if ($cacheOn) {
            Set-Content -LiteralPath "$b\portable-git-cache.ps1" -Value $portableWrapper -Encoding ASCII
            powershell -NoProfile -ExecutionPolicy Bypass -File "$b\portable-git-cache.ps1" -Pay $pay -Use yes; $rc = $LASTEXITCODE
            if ($rc -ne 0) { "portable-git cache: the block exited $rc with the cache; once more, downloading"; powershell -NoProfile -ExecutionPolicy Bypass -File "$b\portable-git-cache.ps1" -Pay $pay -Use no; $rc = $LASTEXITCODE }
            "portable-git exit=$rc"
        } else {
            # stock: the guide's block, as written
            powershell -NoProfile -ExecutionPolicy Bypass -File "$b\portable-git.ps1"; "portable-git exit=$LASTEXITCODE"
        }
        "user PATH names it=" + ([Environment]::GetEnvironmentVariable('Path', 'User') -like '*\Programs\Git\cmd*')
    } else {
        $used = $false
        if ($cacheOn) { $m = @(CachedWingetGit); $m; $used = (@($m | Where-Object { $_ -like 'git-cache: used *' }).Count -gt 0) -and [bool](GitDir) }
        # stock: winget
        if (-not $used) { winget install --id Git.Git -e --source winget --accept-package-agreements --accept-source-agreements --disable-interactivity; "winget exit=$LASTEXITCODE" }
    }
    "gitdir=" + (GitDir)
}
Step 'plugin' { $env:PATH = "$(GitDir);" + $env:PATH; & "$env:USERPROFILE\.local\bin\claude.exe" plugin marketplace add Hellblazer/nexus 2>&1 | Select-Object -Last 2; & "$env:USERPROFILE\.local\bin\claude.exe" plugin install conexus@nexus-plugins 2>&1 | Select-Object -Last 1; (Get-Content -Raw "$env:USERPROFILE\.claude\plugins\installed_plugins.json") -replace '\s+',' ' }
Step 'versions' {
    $v = @(
        "uv: " + (uv --version)
        "nx: " + (nx --version)
        "python (tool venv): " + (& "$env:APPDATA\uv\tools\conexus\Scripts\python.exe" --version)
        # Informational (nexus-f9bgu.57): the Smart App Control state as nx reads it, through the tool
        # Python, which still runs where Smart App Control blocks nx.exe. Older wheels lack the module.
        "smart app control (nx reader): " + ((& "$env:APPDATA\uv\tools\conexus\Scripts\python.exe" -c "from nexus._smart_app_control import state; print(state())" 2>&1) -join ' ')
        "claude: " + (& "$env:USERPROFILE\.local\bin\claude.exe" --version)
        "gitver: " + (& "$(GitDir)\git.exe" --version) + " at " + (GitDir)
        "ps: " + $PSVersionTable.PSVersion.ToString()
    )
    $v | Set-Content -Encoding UTF8 "$b\inst-versions.txt"; $v
}
# The one check this script adds to the 2026-10-12 version: an unattended run
# needs a machine-readable result, because nobody reads the install output.
$bad = @()
if ((@(nx --version 2>&1) -join ' ') -notlike "*version $Version*") { $bad += "nx does not report version $Version" }
if (-not (Test-Path "$env:USERPROFILE\.local\bin\claude.exe")) { $bad += 'claude.exe is missing' }
if (-not (GitDir)) { $bad += 'git.exe is missing' }
$plugins = Get-Content -Raw "$env:USERPROFILE\.claude\plugins\installed_plugins.json" -ErrorAction SilentlyContinue
if (-not $plugins -or $plugins -notlike '*conexus@nexus-plugins*') { $bad += 'conexus@nexus-plugins is not in installed_plugins.json' }
if ($bad.Count -gt 0) { 'NXG-RESULT: FAIL ' + ($bad -join '; ') } else { 'NXG-RESULT: ok' }
