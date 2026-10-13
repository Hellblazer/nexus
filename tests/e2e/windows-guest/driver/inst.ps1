# SPDX-License-Identifier: AGPL-3.0-or-later
#
# Guest (sent by guest-run.ps1 over PowerShell Direct). Step 3 of run-gate.sh:
# the gate's step 1 installs, in this order: uv, conexus (from PyPI at
# -Version, or from the candidate wheel in nx-gate when -Wheel is given), Claude
# Code, Git (winget), and the conexus plugin through the claude CLI. Ends with
# one "NXG-RESULT:" line: ok only when nx reports -Version and claude, git and
# the plugin record are all present. Git is installed only when it is absent:
# under run-gate.sh --standard-user this script runs as an account that cannot
# install it, and the administrator has installed it first (stduser.ps1).
param(
    [Parameter(Mandatory = $true)][string]$Version,
    [string]$Wheel = ''
)
$ErrorActionPreference = 'Continue'
$b = "$env:USERPROFILE\nx-gate"
function Step($n, [scriptblock]$sb) { $t0 = Get-Date; "=== $n"; & $sb 2>&1 | Out-String -Width 200 | % { $_.TrimEnd() } | Select-Object -Last 12; "--- $n took $([int]((Get-Date)-$t0).TotalSeconds)s" }
Step 'uv' { powershell -NoProfile -ExecutionPolicy Bypass -Command 'irm https://astral.sh/uv/install.ps1 | iex' }
$env:PATH = "$env:USERPROFILE\.local\bin;" + $env:PATH
$source = if ($Wheel) { "candidate wheel $Wheel" } else { "conexus==$Version from PyPI" }
Step "conexus $source" {
    if ($Wheel) {
        "wheel sha256=" + (Get-FileHash -Algorithm SHA256 -LiteralPath "$b\$Wheel").Hash.ToLowerInvariant()
        uv tool install --python 3.12 "$b\$Wheel" 2>&1 | Select-Object -Last 3; $rc = $LASTEXITCODE; "uv exit=$rc"
    } else {
        uv tool install --python 3.12 "conexus==$Version" 2>&1 | Select-Object -Last 3; $rc = $LASTEXITCODE; "uv exit=$rc (attempt 1)"
        if ($rc -ne 0) { 'RETRY: waiting 90 s, then one more attempt at the same version'; Start-Sleep 90; uv tool install --python 3.12 "conexus==$Version" 2>&1 | Select-Object -Last 6; "uv exit=$LASTEXITCODE (attempt 2)" }
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
Step 'claude code' { powershell -NoProfile -ExecutionPolicy Bypass -Command 'irm https://claude.ai/install.ps1 | iex'; & "$env:USERPROFILE\.local\bin\claude.exe" --version }
Step 'git (winget)' { if (Test-Path 'C:\Program Files\Git\cmd\git.exe') { 'Git is already installed for all users; not installing it' } else { winget install --id Git.Git -e --source winget --accept-package-agreements --accept-source-agreements --disable-interactivity; "winget exit=$LASTEXITCODE" }; "gitexe=" + (Test-Path 'C:\Program Files\Git\cmd\git.exe') }
Step 'plugin' { $env:PATH = "C:\Program Files\Git\cmd;" + $env:PATH; & "$env:USERPROFILE\.local\bin\claude.exe" plugin marketplace add Hellblazer/nexus 2>&1 | Select-Object -Last 2; & "$env:USERPROFILE\.local\bin\claude.exe" plugin install conexus@nexus-plugins 2>&1 | Select-Object -Last 1; (Get-Content -Raw "$env:USERPROFILE\.claude\plugins\installed_plugins.json") -replace '\s+',' ' }
Step 'versions' {
    $v = @(
        "uv: " + (uv --version)
        "nx: " + (nx --version)
        "python (tool venv): " + (& "$env:APPDATA\uv\tools\conexus\Scripts\python.exe" --version)
        "claude: " + (& "$env:USERPROFILE\.local\bin\claude.exe" --version)
        "gitver: " + (& 'C:\Program Files\Git\cmd\git.exe' --version)
        "ps: " + $PSVersionTable.PSVersion.ToString()
    )
    $v | Set-Content -Encoding UTF8 "$b\inst-versions.txt"; $v
}
# The one check this script adds to the 2026-10-12 version: an unattended run
# needs a machine-readable result, because nobody reads the install output.
$bad = @()
if ((@(nx --version 2>&1) -join ' ') -notlike "*version $Version*") { $bad += "nx does not report version $Version" }
if (-not (Test-Path "$env:USERPROFILE\.local\bin\claude.exe")) { $bad += 'claude.exe is missing' }
if (-not (Test-Path 'C:\Program Files\Git\cmd\git.exe')) { $bad += 'git.exe is missing' }
$plugins = Get-Content -Raw "$env:USERPROFILE\.claude\plugins\installed_plugins.json" -ErrorAction SilentlyContinue
if (-not $plugins -or $plugins -notlike '*conexus@nexus-plugins*') { $bad += 'conexus@nexus-plugins is not in installed_plugins.json' }
if ($bad.Count -gt 0) { 'NXG-RESULT: FAIL ' + ($bad -join '; ') } else { 'NXG-RESULT: ok' }
