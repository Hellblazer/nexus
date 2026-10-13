# SPDX-License-Identifier: AGPL-3.0-or-later
#
# Host. stdin line 1 = guest account secret; never written anywhere.
# The step run-gate.sh --standard-user adds after the restore: make a local
# account that is a member of Users and of nothing else, sign it in at the
# guest's console, and move the staged gate script (and a candidate wheel) into
# its profile. Every later step then runs as that account; the administrator
# account only orchestrates over PowerShell Direct.
#
# What the administrator does here, and a standard user could not:
#   - installs Git for all users (winget's Git.Git package needs elevation), so
#     the run is "a standard user on a machine where Git is already installed";
#   - turns off the first-sign-in animation and the privacy-settings screen, so
#     the new account reaches a desktop unattended;
#   - creates the account and sets it as the automatic sign-in, then restarts.
# The account's secret is 24 random bytes made in the guest. It is written to
# the Winlogon key for the one restart and removed as soon as the account is
# signed in; it is never printed, and the next restore erases the account.
# Nothing here outlives the restore: no checkpoint is taken.
param(
    [Parameter(Mandatory = $true)][string]$VMName,
    [Parameter(Mandatory = $true)][string]$GuestUser,
    [Parameter(Mandatory = $true)][string]$StdUser,
    [Parameter(Mandatory = $true)][string]$RunDir,
    [string]$Wheel = ''
)
if ($RunDir -notmatch '^C:\\build\\guest\\nxg-[0-9]{8}T[0-9]{6}Z-[0-9]+$') { throw 'RunDir is not a run directory under C:\build\guest' }
if ($Wheel -and $Wheel -notmatch '^[A-Za-z0-9._+-]+\.whl$') { throw 'Wheel is not a wheel file name' }
if ($StdUser -notmatch '^[A-Za-z0-9._-]+$' -or $StdUser -eq $GuestUser) { throw 'StdUser must be a plain account name other than GuestUser' }
$pw = [Console]::In.ReadLine()
if (-not $pw) { throw 'nothing on stdin' }
# The characters go into the SecureString one by one: ConvertTo-SecureString -AsPlainText would put the
# plain text in a cmdlet parameter binding, which PowerShell module logging records when it is on.
$sec = New-Object System.Security.SecureString; foreach ($c in $pw.ToCharArray()) { $sec.AppendChar($c) }; $sec.MakeReadOnly(); $pw = $null; $c = $null
$cred = New-Object PSCredential($GuestUser, $sec)
$t0 = Get-Date
$s = New-PSSession -VMName $VMName -Credential $cred -ErrorAction Stop
try {
    Invoke-Command -Session $s -ArgumentList $StdUser {
        param($u)
        $git = 'C:\Program Files\Git\cmd\git.exe'
        if (-not (Test-Path $git)) {
            winget install --id Git.Git -e --source winget --accept-package-agreements --accept-source-agreements --disable-interactivity 2>&1 | Out-String -Width 200 | ForEach-Object { $_.TrimEnd() } | Select-Object -Last 4
            "winget exit=$LASTEXITCODE"
        }
        "gitexe=" + (Test-Path $git) + " (installed for all users by the administrator)"
        New-Item -Force -Path 'HKLM:\SOFTWARE\Policies\Microsoft\Windows\OOBE' | Out-Null
        Set-ItemProperty -Path 'HKLM:\SOFTWARE\Policies\Microsoft\Windows\OOBE' -Name DisablePrivacyExperience -Value 1 -Type DWord
        Set-ItemProperty -Path 'HKLM:\SOFTWARE\Microsoft\Windows\CurrentVersion\Policies\System' -Name EnableFirstLogonAnimation -Value 0 -Type DWord
        if (Get-LocalUser -Name $u -ErrorAction SilentlyContinue) { "NXG-RESULT: FAIL the account $u already exists: this is not the clean baseline"; return }
        $bytes = New-Object byte[] 24; (New-Object System.Security.Cryptography.RNGCryptoServiceProvider).GetBytes($bytes)
        $tp = 'Aa1!' + [Convert]::ToBase64String($bytes); $bytes = $null
        $acct = ([ADSI]"WinNT://$env:COMPUTERNAME,computer").Create('User', $u); $acct.SetPassword($tp); $acct.SetInfo()
        Add-LocalGroupMember -SID 'S-1-5-32-545' -Member $u
        $wl = 'HKLM:\SOFTWARE\Microsoft\Windows NT\CurrentVersion\Winlogon'
        $before = Get-ItemProperty $wl
        "winlogon before: AutoAdminLogon=$($before.AutoAdminLogon) DefaultUserName=$($before.DefaultUserName)"
        Set-ItemProperty -Path $wl -Name AutoAdminLogon -Value '1'
        Set-ItemProperty -Path $wl -Name DefaultUserName -Value $u
        Set-ItemProperty -Path $wl -Name DefaultDomainName -Value $env:COMPUTERNAME
        Set-ItemProperty -Path $wl -Name DefaultPassword -Value $tp; $tp = $null
        Remove-ItemProperty -Path $wl -Name AutoLogonCount -ErrorAction SilentlyContinue
        $groups = @(Get-LocalGroup | Where-Object { @(Get-LocalGroupMember -Group $_ -ErrorAction SilentlyContinue | ForEach-Object Name) -like "*\$u" } | ForEach-Object Name)
        "account $u created; member of: " + ($groups -join ', ')
        "restarting at " + (Get-Date).ToUniversalTime().ToString('o')
        shutdown.exe /r /t 3 /f | Out-Null
    }
} finally { Remove-PSSession $s }
# Wait for the restart and for the new account to own the console.
$ok = $false
for ($i = 0; $i -lt 60; $i++) {
    Start-Sleep 10
    try {
        $who = Invoke-Command -VMName $VMName -Credential $cred -ErrorAction Stop { "" + (Get-CimInstance Win32_ComputerSystem).UserName }
        if ($who -like "*\$StdUser") { $ok = $true; break }
    } catch {}
}
"console user=$who after $([int]((Get-Date)-$t0).TotalSeconds)s"
if (-not $ok) { "NXG-RESULT: FAIL $StdUser did not sign in at the console"; exit 7 }
$rc = 0
$s = New-PSSession -VMName $VMName -Credential $cred -ErrorAction Stop
try {
    $info = Invoke-Command -Session $s -ArgumentList $StdUser, $Wheel {
        param($u, $Wheel)
        $wl = 'HKLM:\SOFTWARE\Microsoft\Windows NT\CurrentVersion\Winlogon'
        Remove-ItemProperty -Path $wl -Name DefaultPassword -ErrorAction SilentlyContinue
        Set-ItemProperty -Path $wl -Name AutoAdminLogon -Value '0'
        "winlogon secret removed=" + (-not (Get-ItemProperty $wl).PSObject.Properties['DefaultPassword'])
        $h = "C:\Users\$u"
        for ($i = 0; $i -lt 36; $i++) {
            $desk = @(Get-CimInstance Win32_Process -Filter "Name='explorer.exe'" | Where-Object { (Invoke-CimMethod -InputObject $_ -MethodName GetOwner).User -eq $u }).Count
            if ($desk -gt 0 -and (Test-Path "$h\AppData\Roaming")) { break }
            Start-Sleep 5
        }
        "profile=" + (Test-Path "$h\AppData\Roaming") + " desktop shell processes=$desk"
        query user 2>&1 | Out-String
        $adm = @(Get-LocalGroupMember -SID 'S-1-5-32-544' | ForEach-Object Name) -like "*\$u"
        "in Administrators=" + [bool]@($adm).Count
        $b = "$h\nx-gate"; $from = "$env:USERPROFILE\nx-gate"
        New-Item -ItemType Directory -Force $b | Out-Null
        Copy-Item -LiteralPath "$from\windows-phase5-gate.ps1" -Destination "$b\windows-phase5-gate.ps1" -Force
        "std gate sha256=" + (Get-FileHash -Algorithm SHA256 -LiteralPath "$b\windows-phase5-gate.ps1").Hash.ToLowerInvariant()
        if ($Wheel) {
            Copy-Item -LiteralPath "$from\$Wheel" -Destination "$b\$Wheel" -Force
            "std wheel sha256=" + (Get-FileHash -Algorithm SHA256 -LiteralPath "$b\$Wheel").Hash.ToLowerInvariant()
        }
        "gitexe=" + (Test-Path 'C:\Program Files\Git\cmd\git.exe')
    }
    $info
    $text = ($info | Out-String)
    if ($text -notmatch 'profile=True desktop shell processes=[1-9]') { "NXG-RESULT: FAIL $StdUser has no profile or no desktop"; $rc = 7 }
    elseif ($text -notmatch 'in Administrators=False') { "NXG-RESULT: FAIL $StdUser is an administrator"; $rc = 8 }
    elseif ($text -notmatch 'gitexe=True') { 'NXG-RESULT: FAIL Git is not installed'; $rc = 9 }
} finally { Remove-PSSession $s }
if ($rc -ne 0) { exit $rc }
'NXG-RESULT: ok'
