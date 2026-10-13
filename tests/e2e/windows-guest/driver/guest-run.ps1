# SPDX-License-Identifier: AGPL-3.0-or-later
#
# Host. stdin line 1 = guest account secret; never written anywhere.
# Runs one inner script of this run directory in the guest over PowerShell
# Direct, and with -Collect yes then copies the guest's run directory and logs
# (read only) to <RunDir>\out. The inner script's own result is its last
# "NXG-RESULT:" line. With no -Inner it only collects.
param(
    [Parameter(Mandatory = $true)][string]$VMName,
    [Parameter(Mandatory = $true)][string]$GuestUser,
    [Parameter(Mandatory = $true)][string]$RunDir,
    [string]$Inner = '',
    # Up to three positional values for the inner script's param() block, one
    # token each: a list in one argument would not survive the ssh shell.
    [string]$Arg1 = '', [string]$Arg2 = '', [string]$Arg3 = '',
    [string]$Collect = ''
)
if ($RunDir -notmatch '^C:\\build\\guest\\nxg-[0-9]{8}T[0-9]{6}Z-[0-9]+$') { throw 'RunDir is not a run directory under C:\build\guest' }
if ($Inner -and $Inner -notmatch '^[a-z0-9-]+\.ps1$') { throw 'Inner must be a script name in the run directory' }
foreach ($a in @($Arg1, $Arg2, $Arg3)) { if ($a -notmatch '^[A-Za-z0-9._+-]*$') { throw 'an inner argument has a character outside A-Za-z0-9._+-' } }
$pw = [Console]::In.ReadLine()
if (-not $pw) { throw 'nothing on stdin' }
# The characters go into the SecureString one by one: ConvertTo-SecureString -AsPlainText would put the
# plain text in a cmdlet parameter binding, which PowerShell module logging records when it is on.
$sec = New-Object System.Security.SecureString; foreach ($c in $pw.ToCharArray()) { $sec.AppendChar($c) }; $sec.MakeReadOnly(); $pw = $null; $c = $null
$cred = New-Object PSCredential($GuestUser, $sec)
$s = New-PSSession -VMName $VMName -Credential $cred -ErrorAction Stop
try {
    if ($Inner) {
        $innerArgs = @(@($Arg1, $Arg2, $Arg3) | Where-Object { $_ })
        Invoke-Command -Session $s -FilePath (Join-Path $RunDir $Inner) -ArgumentList $innerArgs
    }
    if ($Collect -eq 'yes') {
        $out = Join-Path $RunDir 'out'
        New-Item -ItemType Directory -Force $out | Out-Null
        $files = Invoke-Command -Session $s {
            $b = "$env:USERPROFILE\nx-gate"
            @(Get-ChildItem "$b\run" -File -ErrorAction SilentlyContinue | ForEach-Object FullName) +
            @("$b\setup.log", "$b\nxgate-claude.log", "$b\GATERUN.txt", "$b\inst-versions.txt" | Where-Object { Test-Path $_ })
        }
        foreach ($f in $files) { Copy-Item -FromSession $s -Path $f -Destination $out -Force; "collected " + (Split-Path $f -Leaf) }
    }
} finally { Remove-PSSession $s }
