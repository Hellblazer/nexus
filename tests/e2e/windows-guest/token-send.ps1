# SPDX-License-Identifier: AGPL-3.0-or-later
# Runs on the Hyper-V host, started by token-send.sh under
# claude_credentials.py run --remote. Writes the token from its own
# environment into host-recv.ps1's pipe and prints nothing token-shaped.
$t = [Environment]::GetEnvironmentVariable('CLAUDE_CODE_OAUTH_TOKEN')
if (-not $t) { 'sender: no token in env'; exit 1 }
$c = New-Object System.IO.Pipes.NamedPipeClientStream('.', 'nxgate-host', [System.IO.Pipes.PipeDirection]::Out)
$c.Connect(30000)
$w = New-Object System.IO.StreamWriter($c); $w.WriteLine($t); $w.Flush(); $c.WaitForPipeDrain(); $w.Dispose()
$t = $null
'sender: done'
