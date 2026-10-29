# SPDX-License-Identifier: AGPL-3.0-or-later
#
# Nexus installer for Windows (RDR-228 Phase 3, nexus-sv2wx.26). Windows
# PowerShell 5.1 and PowerShell 7 both run it. It also runs on macOS and Linux
# under PowerShell 7, which is how it is tested there.
#
#   irm https://hellblazer.github.io/nexus/install.ps1 | iex
#   & ([scriptblock]::Create((irm https://hellblazer.github.io/nexus/install.ps1))) --local
#
# Everything is inside the one function Install-Nexus, and it is called on the
# last line. A download that is cut short is a parse error or a file with no call
# in it, and runs nothing. The function never calls `exit` when the text came
# from iex, because that would close the user's PowerShell window.

function Install-Nexus {
    param([string[]]$ArgumentList)

    # Native programs write progress to standard error; that is not an error here.
    $ErrorActionPreference = 'Continue'

    $sep = [System.IO.Path]::PathSeparator
    # The PATH this window has. uv's directory is added to this process's PATH below,
    # which says nothing about the windows the user opens afterwards.
    $originalPath = [string]$env:PATH
    $state = @{ Mode = ''; Autostart = ''; AutostartArg = ''; Terminal = $false; Nx = ''; Slim = $false }
    $marketplace = 'Hellblazer/nexus'
    $plugin = 'conexus@nexus-plugins'
    # The uv project's own installer. NX_INSTALL_UV_URL replaces it (a test seam; a file:
    # URL works). The script says the override is in use, by name, and never prints the
    # value; only https: and file: are accepted.
    $uvInstallerUrl = 'https://astral.sh/uv/install.ps1'
    $uvUrlOverridden = $false
    if ($env:NX_INSTALL_UV_URL) { $uvInstallerUrl = $env:NX_INSTALL_UV_URL; $uvUrlOverridden = $true }

    function Say([string]$Text) { Write-Host $Text }
    function Warn([string]$Text) { [Console]::Error.WriteLine($Text) }
    # A step that must stop the install. Caught at the bottom of Install-Nexus.
    function Stop-Install([int]$Code, [string]$Text) { throw ('NXEXIT|{0}|{1}' -f $Code, $Text) }
    function Bad-Usage([string]$Text) {
        Warn ('install.ps1: ' + $Text)
        Warn 'Try: install.ps1 --help'
        Stop-Install 2 ''
    }

    function Show-Usage {
        Say @'
Install the Nexus command-line tool (nx). Adds the conexus plugin to Claude Code
when claude is on the PATH, and sets up the storage service if you ask for it.

Usage: install.ps1 [--local | --cloud | --cli-only] [--autostart | --no-autostart] [--slim]

  --local          Run the storage service on this machine. Downloads about 0.8 GB
                   in all on macOS and 1.0 GB on Linux (Windows is not measured),
                   PDF support included. Runs `nx init`, in local mode even when a
                   service address is already configured.
  --cloud          Use a managed service. Runs `nx config init`, which asks for the
                   service address and your token. With no terminal attached it
                   needs NX_SERVICE_URL and NX_SERVICE_TOKEN (or NX_MINT_TOKEN) in
                   the environment, and stops before downloading anything without
                   them.
  --cli-only       Install the command-line tool and nothing else.
  --autostart      With --local: start the service at login. Passed to `nx init`
                   as --yes.
  --no-autostart   With --local: do not start the service at login. Passed to
                   `nx init` as --no-autostart.
  --slim           Leave PDF support out: the download falls to about 0.2 GB (0.25 GB on Linux) for the
                   command-line tool (about 0.55 GB with --local). The first PDF you
                   index then fetches it (`nx self install --extras pdf` does it
                   ahead of time).
  -h, --help       Show this text.

Typing this command is your consent to install the command-line tool, so the
script does not ask about that. It installs uv first when uv is missing, using
the uv project's own installer. When nx is already installed it skips the build
and says so. By default the build installs everything, PDF support included
(MinerU and torch): it downloads about 0.5 GB on macOS and 0.6 GB on Linux
for the command-line tool, and the service adds the rest of the --local figure. The
sizes were measured on 7.89.0 on macOS and Linux (2026-10-29). Disk use is about 1.9 to
2.2 GB for the command-line tool and 2.6 to 2.9 GB with --local. Windows sizes were not
measured. None of these figures is a promise.

Platforms: Windows 11 on x64. If you run this script under PowerShell on a Mac, that is
an Apple Silicon Mac on macOS 14 or later (the pinned onnxruntime ships macOS 14 wheels
only); on Linux, x86-64 or arm64.

With no service flag, the script asks one question on the terminal, before it
downloads anything. With no terminal attached and no flag, it installs the
command-line tool only and says so.

Start at login: with --local and neither autostart flag, the script asks when a
terminal is attached, and the answer is no unless you type y. With no terminal it
passes --no-autostart. A login unit is never registered without an explicit yes.
'@
    }

    # ---- the terminal -----------------------------------------------------------------
    # A terminal is attached when PowerShell is interactive and its input is not
    # redirected. NX_INSTALL_ASSUME_TERMINAL=1 is a test seam: it treats standard input
    # as the terminal, so answers can be piped in.
    function Test-Terminal {
        if ($env:NX_INSTALL_ASSUME_TERMINAL -eq '1') { return $true }
        try {
            return ([Environment]::UserInteractive -and -not [Console]::IsInputRedirected)
        } catch {
            return $false
        }
    }

    function Read-Answer([string]$Prompt) { # returns $null when the terminal is gone
        # The prompt goes out through Write-Host, not Read-Host -Prompt: Windows PowerShell
        # 5.1 leaves a Read-Host prompt off a redirected output stream.
        Write-Host -NoNewline ($Prompt + ': ')
        try { $line = Read-Host } catch { return $null }
        if ($null -eq $line) { return $null }
        return ([string]$line).Trim()
    }

    function Select-Mode {
        Say ''
        Say 'How should Nexus run its storage service?'
        Say '  1) local     run it on this machine (downloads about 0.8 GB on macOS, 1.0 GB on Linux; Windows not measured)'
        Say '  2) cloud     use a managed service (you type its address and token)'
        Say '  3) CLI only  install the command-line tool and nothing else'
        for ($tries = 0; $tries -lt 3; $tries++) {
            $answer = Read-Answer 'Choose 1, 2 or 3 [3]'
            if ($null -eq $answer) { break }
            switch -Regex ($answer) {
                '^(1|local)$' { $state.Mode = 'local'; return }
                '^(2|cloud)$' { $state.Mode = 'cloud'; return }
                '^(3|cli-only|)$' { $state.Mode = 'cli-only'; return }
            }
            Say 'Please answer 1, 2 or 3.'
        }
        $state.Mode = 'cli-only'
    }

    function Select-Autostart {
        $answer = Read-Answer 'Start the service at login? [y/N]'
        if ($null -ne $answer -and $answer -match '^(y|yes)$') { $state.Autostart = 'yes' } else { $state.Autostart = 'no' }
    }

    try {
        # ---- arguments ----------------------------------------------------------------
        foreach ($raw in $ArgumentList) {
            $name = ([string]$raw).ToLowerInvariant() -replace '^[-/]+', '' -replace '-', ''
            switch ($name) {
                { $_ -in 'h', 'help', '?' } { Show-Usage; Stop-Install 0 '' }
                { $_ -in 'local', 'cloud', 'clionly' } {
                    $wanted = if ($name -eq 'clionly') { 'cli-only' } else { $name }
                    if ($state.Mode -and $state.Mode -ne $wanted) { Bad-Usage ('--' + $state.Mode + ' and ' + $raw + ' cannot be used together') }
                    $state.Mode = $wanted
                }
                'autostart' {
                    if ($state.Autostart -eq 'no') { Bad-Usage '--autostart and --no-autostart cannot be used together' }
                    $state.Autostart = 'yes'; $state.AutostartArg = '--autostart'
                }
                'noautostart' {
                    if ($state.Autostart -eq 'yes') { Bad-Usage '--autostart and --no-autostart cannot be used together' }
                    $state.Autostart = 'no'; $state.AutostartArg = '--no-autostart'
                }
                'slim' { $state.Slim = $true }
                default { Bad-Usage ('unknown option: ' + $raw) }
            }
        }

        # ---- decide everything that needs the user, before any download ---------------
        $state.Terminal = Test-Terminal
        if (-not $state.Mode) {
            if ($state.Terminal) {
                Select-Mode
            } else {
                $state.Mode = 'cli-only'
                Say 'No terminal is attached and no service flag was given, so this installs the'
                Say 'command-line tool only. For the service, run `nx init` (local) or'
                Say '`nx config init` (managed) afterwards, or run this script again with --local'
                Say 'or --cloud.'
            }
        }
        if ($state.Mode -ne 'local' -and $state.Autostart) {
            Say ('Note: ' + $state.AutostartArg + ' only applies to --local; ignored.')
        }
        if ($state.Mode -eq 'local' -and -not $state.Autostart) {
            if ($state.Terminal) {
                Select-Autostart
            } else {
                $state.Autostart = 'no'
                Say 'No terminal is attached, so the service will not start at login (--no-autostart).'
                Say 'Pass --autostart to change that.'
            }
        }

        # --cloud with no terminal: the wizard can only skip its prompts for what the
        # environment already holds. Stop now, not after the build. Presence only; the
        # values are never read or printed.
        if ($state.Mode -eq 'cloud' -and -not $state.Terminal) {
            if (-not $env:NX_SERVICE_URL -or (-not $env:NX_SERVICE_TOKEN -and -not $env:NX_MINT_TOKEN)) {
                Bad-Usage '--cloud needs a terminal to ask for the service address and token. Run it in a terminal, or set NX_SERVICE_URL and NX_SERVICE_TOKEN (or NX_MINT_TOKEN) first. Nothing was changed.'
            }
        }

        # ---- where things are ---------------------------------------------------------
        $homeDir = $env:USERPROFILE
        if (-not $homeDir) { $homeDir = $env:HOME }
        if (-not $homeDir) { Stop-Install 1 'install.ps1: no home directory is known.' }

        function Resolve-Setting([string]$Value, [string]$Default) {
            # A directory setting: ~ and ~/ expanded, a relative path refused.
            if (-not $Value) { return $Default }
            if ($Value -eq '~') { return $homeDir }
            if ($Value -match '^~[/\\]') { return (Join-Path $homeDir $Value.Substring(2)) }
            if ([System.IO.Path]::IsPathRooted($Value)) { return $Value }
            return $Default
        }
        $toolsDir = Resolve-Setting $env:NX_TOOLS_DIR (Join-Path (Join-Path (Join-Path (Join-Path $homeDir '.local') 'share') 'nexus') 'tools')
        $binDir = Resolve-Setting $env:NX_BIN_DIR (Join-Path (Join-Path $homeDir '.local') 'bin')
        $layoutBin = Join-Path (Join-Path $toolsDir 'current') 'bin'

        function Find-In([string]$Dir, [string]$Base) {
            foreach ($ext in '.exe', '.cmd', '') {
                $candidate = Join-Path $Dir ($Base + $ext)
                if (Test-Path -LiteralPath $candidate -PathType Leaf) { return $candidate }
            }
            return $null
        }

        # ---- uv -----------------------------------------------------------------------
        function Add-UvDirToPath {
            $dirs = @()
            if ($env:XDG_BIN_HOME) { $dirs += $env:XDG_BIN_HOME }
            if ($env:UV_INSTALL_DIR) { $dirs += $env:UV_INSTALL_DIR }
            $dirs += $binDir
            # uv's own installer writes to ~/.local/bin whatever NX_BIN_DIR says (install.sh
            # checks it too); without it an NX_BIN_DIR elsewhere stopped the script right after
            # installing uv (7.86.1 shakeout, Windows leg).
            $dirs += (Join-Path (Join-Path $homeDir '.local') 'bin')
            $dirs += (Join-Path (Join-Path $homeDir '.cargo') 'bin')
            foreach ($dir in $dirs) {
                if (Find-In $dir 'uv') { $env:PATH = $dir + $sep + $env:PATH; return $true }
            }
            return $false
        }
        function Get-File([string]$Url, [string]$Destination) {
            if ($Url -like 'file:*') {
                Copy-Item -LiteralPath ([Uri]$Url).LocalPath -Destination $Destination -ErrorAction Stop
                return
            }
            try { [Net.ServicePointManager]::SecurityProtocol = [Net.ServicePointManager]::SecurityProtocol -bor 3072 } catch { }
            Invoke-WebRequest -UseBasicParsing -Uri $Url -OutFile $Destination -ErrorAction Stop
        }
        function Confirm-Uv {
            if (Get-Command uv -ErrorAction SilentlyContinue) { return }
            if (Add-UvDirToPath) { return }
            Say "uv is not installed; installing it with the uv project's own installer."
            if ($uvUrlOverridden) {
                Say 'Using the uv installer named by NX_INSTALL_UV_URL.'
                if ($uvInstallerUrl -notmatch '^(https|file):') {
                    Stop-Install 1 'install.ps1: NX_INSTALL_UV_URL must be an https: or file: URL. Nothing was changed.'
                }
            }
            # Fetch to a file, then run it in a child PowerShell with the execution policy
            # set for that one process, as the uv documentation does.
            $tmp = Join-Path ([System.IO.Path]::GetTempPath()) ('nx-install-uv-' + [guid]::NewGuid().ToString('N') + '.ps1')
            try {
                try { Get-File $uvInstallerUrl $tmp } catch {
                    Stop-Install 1 'install.ps1: could not download the uv installer. Install uv (https://docs.astral.sh/uv/), then run this again.'
                }
                $self = (Get-Process -Id $PID).Path
                & $self -NoProfile -ExecutionPolicy Bypass -File $tmp
                if ($LASTEXITCODE -ne 0) {
                    Stop-Install 1 'install.ps1: the uv installer failed. Install uv (https://docs.astral.sh/uv/), then run this again.'
                }
            } finally {
                Remove-Item -LiteralPath $tmp -Force -ErrorAction SilentlyContinue
            }
            [void](Add-UvDirToPath)
            if (-not (Get-Command uv -ErrorAction SilentlyContinue)) {
                Stop-Install 1 'install.ps1: uv was installed but is not on the PATH. Open a new terminal and run this again.'
            }
        }

        # ---- the CLI ------------------------------------------------------------------
        # A program counts as the CLI when it answers --version the way nx does. Another
        # program named nx (a build tool) does not, and an nx that cannot start does not
        # either: the build below repairs that one.
        function Test-OurNx([string]$Path) {
            if (-not $Path -or -not (Test-Path -LiteralPath $Path -PathType Leaf)) { return $false }
            try { $out = & $Path --version 2>$null } catch { return $false }
            if ($LASTEXITCODE -ne 0) { return $false }
            return ([string]($out | Select-Object -First 1)) -like 'nx, version *'
        }
        function Find-Cli { # the generation layout first, then the bin directory, then PATH
            $candidates = @((Find-In $layoutBin 'nx'), (Find-In $binDir 'nx'))
            foreach ($found in @(Get-Command nx -All -CommandType Application -ErrorAction SilentlyContinue)) { $candidates += $found.Source }
            foreach ($candidate in $candidates) {
                if (Test-OurNx $candidate) { $state.Nx = $candidate; return $true }
            }
            return $false
        }

        Confirm-Uv

        if (Find-Cli) {
            Say ('The Nexus CLI is already installed (' + $state.Nx + '); skipping the build.')
        } else {
            Say 'Building the Nexus CLI (this downloads the conexus package).'
            # conexus@latest, not bare conexus: uv reuses the environment it cached for a bare
            # requirement, which can be a conexus from before --bootstrap existed.
            # On Linux (PowerShell 7 there, which is how this script is tested) the build gets
            # the CPU build of torch, as install.sh gives it; Windows and macOS get nothing.
            $torchBefore = $env:UV_TORCH_BACKEND
            if ($IsLinux) {
                $env:UV_TORCH_BACKEND = $(if ($env:NX_TORCH_BACKEND) { $env:NX_TORCH_BACKEND } else { 'cpu' })
            }
            # The default build installs everything: --extras pdf goes to the GENERATION (the
            # throwaway environment that runs --bootstrap stays slim). --slim leaves PDF
            # support out; the first PDF index fetches it then (nexus-2k66t.4.9).
            $buildArgs = @('tool', 'run', '--python', '3.12', '--from', 'conexus@latest', 'nx', 'self', 'install', '--bootstrap')
            if (-not $state.Slim) { $buildArgs += @('--extras', 'pdf') }
            try {
                & uv @buildArgs
                $buildCode = $LASTEXITCODE
            } finally {
                if ($IsLinux) { $env:UV_TORCH_BACKEND = $torchBefore }
            }
            if ($buildCode -ne 0) { Stop-Install 1 'install.ps1: the build failed (see the lines above). Nothing else was done.' }
            if (-not (Find-Cli)) {
                Stop-Install 1 ('install.ps1: the build finished but nx was not found in ' + $layoutBin + '. Open a new terminal and run this again.')
            }
            if ($state.Slim) {
                Say 'PDF support was left out (-slim). The first PDF you index installs it; to add it now, run: nx self install --extras pdf'
            }
        }

        # ---- the CLI-only record ------------------------------------------------------
        # Claude Code's session-start hook asks about a missing service at every session
        # unless the plugin's setup record says CLI only was chosen. The plugin's setup tool
        # writes that record when it is asked for CLI only; this writes the same file, the
        # same shape. The directory is the one the plugin reads: NEXUS_CONFIG_DIR, else
        # NX_CONFIG_DIR, else ~/.config/nexus. The record holds no secret and sits under the
        # user's profile. A live run of the plugin's setup keeps its record, and a failed
        # write is a note, not a failure. The text is written as UTF-8 with no byte order
        # mark, because the reader is Python's json module.
        function Write-CliOnlyRecord {
            $cfg = $env:NEXUS_CONFIG_DIR
            if (-not $cfg) { $cfg = $env:NX_CONFIG_DIR }
            if (-not $cfg) { $cfg = Join-Path (Join-Path $homeDir '.config') 'nexus' }
            try {
                $sdir = Join-Path ([System.IO.Path]::GetFullPath($cfg)) 'setup'
                if (Test-Path -LiteralPath (Join-Path $sdir 'lock') -PathType Container) { return }
                [void](New-Item -ItemType Directory -Force -Path $sdir)
                $now = [DateTime]::UtcNow.ToString('yyyy-MM-ddTHH:mm:ssZ')
                $record = [ordered]@{
                    state = 'done'; stage = 'build'; mode = 'cli-only'; autostart = $false
                    started_at = $now; updated_at = $now
                    message = 'The Nexus CLI is installed. No service is set up.'
                    steps_done = @('build')
                    next = 'Reconnect the nexus server or start a new session. To set up a service, ask the user first; if they agree, call setup with mode local.'
                }
                $json = ConvertTo-Json -InputObject $record -Compress
                $tmp = Join-Path $sdir ('status.json.' + [guid]::NewGuid().ToString('N') + '.tmp')
                try {
                    [System.IO.File]::WriteAllText($tmp, $json, (New-Object System.Text.UTF8Encoding($false)))
                    Move-Item -LiteralPath $tmp -Destination (Join-Path $sdir 'status.json') -Force
                } finally {
                    Remove-Item -LiteralPath $tmp -Force -ErrorAction SilentlyContinue
                }
                Remove-Item -LiteralPath (Join-Path $sdir 'not-now') -Force -ErrorAction SilentlyContinue
            } catch {
                Warn 'Note: could not record that no service is set up, so Claude Code may ask about one.'
            }
        }

        # ---- the plugin ---------------------------------------------------------------
        if (Get-Command claude -ErrorAction SilentlyContinue) {
            Say 'Adding the conexus plugin to Claude Code.'
            & claude plugin marketplace add $marketplace
            $added = ($LASTEXITCODE -eq 0)
            if ($added) {
                & claude plugin install $plugin
                $added = ($LASTEXITCODE -eq 0)
            }
            if (-not $added) {
                Warn 'Claude Code did not take the plugin. In Claude Code run:'
                Warn ('  /plugin marketplace add ' + $marketplace)
                Warn ('  /plugin install ' + $plugin)
            }
        } else {
            Say 'Claude Code (claude) is not on the PATH, so the plugin was not added. In Claude Code run:'
            Say ('  /plugin marketplace add ' + $marketplace)
            Say ('  /plugin install ' + $plugin)
        }

        # ---- the service --------------------------------------------------------------
        if ($state.Mode -eq 'local') {
            $flag = if ($state.Autostart -eq 'yes') { '--yes' } else { '--no-autostart' }
            Say ('Setting up the local service: nx init ' + $flag + ' --no-beads-prime')
            # NX_LOCAL=1 on this one call: nx init picks the managed arm when a service
            # address is configured, and --local asked for the local service.
            $localBefore = $env:NX_LOCAL
            $env:NX_LOCAL = '1'
            try {
                & $state.Nx init $flag --no-beads-prime
                $initCode = $LASTEXITCODE
            } finally {
                $env:NX_LOCAL = $localBefore
            }
            if ($initCode -ne 0) {
                Stop-Install 1 'install.ps1: nx init did not finish. The CLI is installed; fix the cause above and run `nx init` again.'
            }
        } elseif ($state.Mode -eq 'cloud') {
            Say 'Configuring the managed service: nx config init'
            & $state.Nx config init
            if ($LASTEXITCODE -ne 0) {
                Stop-Install 1 'install.ps1: nx config init did not finish. The CLI is installed; run `nx config init` in a terminal.'
            }
        } else {
            Write-CliOnlyRecord
        }

        # ---- done ---------------------------------------------------------------------
        $nxDir = Split-Path -Parent $state.Nx
        $onPath = $false
        foreach ($entry in $originalPath.Split($sep)) {
            if ($entry.TrimEnd('\', '/') -eq $nxDir.TrimEnd('\', '/')) { $onPath = $true }
        }
        if (-not $onPath) {
            Say ('Note: ' + $nxDir + ' is not on the PATH of this window. Open a new terminal so that nx is found.')
        }
        Say 'Done. If Claude Code is open, reconnect the nexus server (/mcp) or start a new session.'
        $code = 0
    } catch {
        $message = [string]$_.Exception.Message
        if ($message -like 'NXEXIT|*') {
            $parts = $message.Split('|', 3)
            $code = [int]$parts[1]
            if ($parts[2]) { Warn $parts[2] }
        } else {
            Warn ('install.ps1: ' + $message)
            $code = 1
        }
    }

    if ($PSCommandPath) { exit $code }
    $global:LASTEXITCODE = $code
}

Install-Nexus -ArgumentList $args
