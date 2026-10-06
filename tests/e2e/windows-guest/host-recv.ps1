# SPDX-License-Identifier: AGPL-3.0-or-later
#
# Runs on the Hyper-V host (elevated ssh session). Line 1 of stdin is the guest
# account's password; nothing else arrives on stdin. Opens the host pipe
# \\.\pipe\nxgate-host (current user only), waits for token-send.ps1 to write
# the automation token into it, then over PowerShell Direct: stages files into
# %USERPROFILE%\nx-gate, closes any earlier gate window, and starts Claude Code
# in the guest's console session through a one-shot scheduled task whose
# launcher reads the token from a guest pipe. The token lives only in process
# memory and pipes: no file, task definition or command line carries it.
param(
    [string]$VMName = 'nx-clean-win11',
    [string]$GuestUser = 'nxguest',
    [string]$StageDir = 'C:\build\guest\nxgate\stage',
    [int]$TokenWaitSec = 180
)
$ErrorActionPreference = 'Stop'

$pw = [Console]::In.ReadLine()
if (-not $pw) { throw 'no guest password on stdin' }
$cred = New-Object PSCredential($GuestUser, (ConvertTo-SecureString $pw -AsPlainText -Force)); $pw = $null

$hsec = New-Object System.IO.Pipes.PipeSecurity
$hsec.AddAccessRule((New-Object System.IO.Pipes.PipeAccessRule([Security.Principal.WindowsIdentity]::GetCurrent().User, 'ReadWrite', 'Allow')))
$hp = New-Object System.IO.Pipes.NamedPipeServerStream('nxgate-host', [System.IO.Pipes.PipeDirection]::In, 1,
    [System.IO.Pipes.PipeTransmissionMode]::Byte, [System.IO.Pipes.PipeOptions]::Asynchronous, 0, 0, $hsec)
[Console]::Out.WriteLine('host-pipe-listening'); [Console]::Out.Flush()
$har = $hp.BeginWaitForConnection($null, $null)
if (-not $har.AsyncWaitHandle.WaitOne($TokenWaitSec * 1000)) { [Console]::Out.WriteLine('TIMEOUT: no token sender'); exit 1 }
$hp.EndWaitForConnection($har)
$hr = New-Object System.IO.StreamReader($hp); $tok = $hr.ReadLine(); $hr.Dispose(); $hp.Dispose()
if (-not $tok -or $tok.Length -lt 20) { [Console]::Out.WriteLine('FAIL: empty token from sender'); exit 1 }
[Console]::Out.WriteLine('token-received')

$session = New-PSSession -VMName $VMName -Credential $cred
try {
    $staged = @()
    if (Test-Path $StageDir) {
        Invoke-Command -Session $session { New-Item -ItemType Directory -Force -Path "$env:USERPROFILE\nx-gate" | Out-Null }
        foreach ($f in Get-ChildItem $StageDir -File) {
            $dest = Invoke-Command -Session $session -ArgumentList $f.Name { param($n) "$env:USERPROFILE\nx-gate\$n" }
            Copy-Item -ToSession $session -Path $f.FullName -Destination $dest -Force
            $staged += $f.Name
        }
    }
    if ($staged) { [Console]::Out.WriteLine('staged: ' + ($staged -join ', ')) }

    Invoke-Command -Session $session -ArgumentList $tok {
        param($tok)
        # Close an earlier gate window (its claude and its PowerShell).
        Get-CimInstance Win32_Process | Where-Object { $_.CommandLine -like '*nxgate-claude.ps1*' } | ForEach-Object {
            Get-CimInstance Win32_Process -Filter "ParentProcessId=$($_.ProcessId)" | ForEach-Object { Stop-Process -Id $_.ProcessId -Force -ErrorAction SilentlyContinue }
            Stop-Process -Id $_.ProcessId -Force -ErrorAction SilentlyContinue
        }
        $launcher = "$env:USERPROFILE\nx-gate\nxgate-claude.ps1"
        New-Item -ItemType Directory -Force -Path (Split-Path $launcher) | Out-Null
        @'
param([string]$Pipe)
$c = New-Object System.IO.Pipes.NamedPipeClientStream('.', $Pipe, [System.IO.Pipes.PipeDirection]::In)
$c.Connect(30000)
$r = New-Object System.IO.StreamReader($c)
[Environment]::SetEnvironmentVariable('CLAUDE_CODE_OAUTH_TOKEN', $r.ReadLine(), 'Process')
$r.Dispose(); $c.Dispose()
Set-Location $HOME
$host.UI.RawUI.WindowTitle = 'nx gate: claude (automation token)'
& "$HOME\.local\bin\claude.exe"
"claude exited code=$LASTEXITCODE at $(Get-Date -Format o)" | Tee-Object -Append -FilePath "$HOME\nx-gate\nxgate-claude.log"
'@ | Set-Content -Encoding UTF8 $launcher
        $name = 'nxgate-' + [guid]::NewGuid().ToString('N')
        $sec = New-Object System.IO.Pipes.PipeSecurity
        $sec.AddAccessRule((New-Object System.IO.Pipes.PipeAccessRule([Security.Principal.WindowsIdentity]::GetCurrent().User, 'ReadWrite', 'Allow')))
        $srv = New-Object System.IO.Pipes.NamedPipeServerStream($name, [System.IO.Pipes.PipeDirection]::Out, 1,
            [System.IO.Pipes.PipeTransmissionMode]::Byte, [System.IO.Pipes.PipeOptions]::Asynchronous, 0, 0, $sec)
        $act = New-ScheduledTaskAction -Execute 'powershell.exe' -Argument "-NoExit -NoProfile -ExecutionPolicy Bypass -File `"$launcher`" -Pipe $name"
        $prin = New-ScheduledTaskPrincipal -UserId $env:USERNAME -LogonType Interactive
        Register-ScheduledTask -TaskName 'nxgate-claude' -Action $act -Principal $prin -Force | Out-Null
        try {
            Start-ScheduledTask -TaskName 'nxgate-claude'
            $ar = $srv.BeginWaitForConnection($null, $null)
            if ($ar.AsyncWaitHandle.WaitOne(60000)) {
                $srv.EndWaitForConnection($ar)
                $w = New-Object System.IO.StreamWriter($srv); $w.WriteLine($tok); $w.Flush(); $srv.WaitForPipeDrain(); $w.Dispose()
                'guest: handed-off'
            } else { 'guest: TIMEOUT launcher never connected (is the guest user signed in at the console?)' }
        } finally {
            $tok = $null; $srv.Dispose()
            Start-Sleep -Seconds 2
            Unregister-ScheduledTask -TaskName 'nxgate-claude' -Confirm:$false
        }
        $p = @(Get-Process claude -ErrorAction SilentlyContinue)
        "guest: claude processes=$($p.Count) " + (($p | ForEach-Object { "pid=$($_.Id) session=$($_.SessionId)" }) -join ' ')
    }
} finally {
    $tok = $null
    Remove-PSSession $session
}
