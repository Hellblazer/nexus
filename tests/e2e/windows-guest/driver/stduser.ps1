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
#     the run is "a standard user on a machine where Git is already installed".
#     Not with -AdminGit no: then the account installs PortableGit itself;
#   - turns off the first-sign-in animation and the privacy-settings screen, so
#     the new account reaches a desktop unattended;
#   - creates the account and sets it as the automatic sign-in, then restarts;
#   - with -AppLocker yes, applies AppLocker's default rules, enforced, for
#     executables, installers, scripts and packaged apps, and sets the
#     Application Identity service to start. After the restart it copies
#     whoami.exe into the account's nx-gate folder as applocker-control.exe and
#     waits until a start of that file as the account is recorded as blocked
#     (enforcement begins some time after the service starts), failing the step
#     otherwise: a run under a policy not yet in effect proves nothing.
#   - with -Wdac audit or allow, deploys an App Control for Business (WDAC)
#     policy built from Windows' DefaultWindows_Enforced example (only code
#     Windows itself signs runs, for every account, administrators included),
#     with CiTool before the restart. 'audit' puts it in audit mode; 'allow'
#     enforces it with path rules for this install's folders under the
#     account's profile, runtime file-path protection off (the folders are
#     user-writable, which WDAC refuses at run time otherwise) and script
#     enforcement off (enforced, it puts PowerShell in ConstrainedLanguage).
#     After the restart it compiles an unsigned program with the .NET
#     Framework's csc.exe into the account's nx-gate folder as
#     wdac-control.exe and waits until a start of it as the account is
#     recorded in the CodeIntegrity log (3077 blocked, or 3076 in audit). A
#     signed copy like the AppLocker control would not do: WDAC trusts a
#     Windows signature wherever the file is.
# The account's secret is 24 random bytes made in the guest. It is written to
# the Winlogon key for the one restart and removed as soon as the account is
# signed in; it is never printed, and the next restore erases the account.
# Nothing here outlives the restore: no checkpoint is taken.
param(
    [Parameter(Mandatory = $true)][string]$VMName,
    [Parameter(Mandatory = $true)][string]$GuestUser,
    [Parameter(Mandatory = $true)][string]$StdUser,
    [Parameter(Mandatory = $true)][string]$RunDir,
    [string]$Wheel = '',
    # 'no' (run-gate.sh --no-admin-git): the administrator installs no Git. The
    # account installs PortableGit itself in the install step, from
    # portable-git.ps1, which this step copies into its nx-gate folder.
    [string]$AdminGit = 'yes',
    # 'yes' (run-gate.sh --applocker): the administrator enforces AppLocker's default rules.
    # 'audit' (--applocker-audit): the same rules in audit only; the logs record what they would refuse.
    # 'allow' (--applocker-allow): enforced, plus path rules for this install's folders; script rules audit only.
    # 'allow-dll-audit' (--applocker-allow-dll-audit): 'allow', plus the DLL rule collection in audit only; the
    # logs (event 8003) name every DLL the DLL rules would refuse.
    # 'allow-dll' (--applocker-allow-dll): 'allow', plus the DLL rules enforced, with the same folders and the
    # extra DLL folders the audit run recorded.
    [string]$AppLocker = 'no',
    # 'audit' (run-gate.sh --wdac-audit): the DefaultWindows WDAC policy in audit mode; the CodeIntegrity log
    # (event 3076) names every file it would refuse. 'allow' (--wdac-allow): the same policy enforced, with path
    # rules for this install's folders; script enforcement off.
    [ValidateSet('no', 'audit', 'allow')][string]$Wdac = 'no'
)
if ($Wdac -ne 'no' -and $AppLocker -ne 'no') { throw 'WDAC and AppLocker modes are separate runs' }
if ($RunDir -notmatch '^C:\\build\\guest\\nxg-[0-9]{8}T[0-9]{6}Z-[0-9]+$') { throw 'RunDir is not a run directory under C:\build\guest' }
if ($Wheel -and $Wheel -notmatch '^[A-Za-z0-9._+-]+\.whl$') { throw 'Wheel is not a wheel file name' }
if ($StdUser -notmatch '^[A-Za-z0-9._-]+$' -or $StdUser -eq $GuestUser) { throw 'StdUser must be a plain account name other than GuestUser' }
$pw = [Console]::In.ReadLine()
if (-not $pw) { throw 'nothing on stdin' }
# The characters go into the SecureString one by one: ConvertTo-SecureString -AsPlainText would put the
# plain text in a cmdlet parameter binding, which PowerShell module logging records when it is on.
$sec = New-Object System.Security.SecureString; foreach ($c in $pw.ToCharArray()) { $sec.AppendChar($c) }; $sec.MakeReadOnly(); $pw = $null; $c = $null
$cred = New-Object PSCredential($GuestUser, $sec)
# guest-session: the same block in every script that opens a session (tests/test_windows_gate_driver.py).
# Seconds after the guest restarted and an account signed in at the console, PowerShell Direct has failed to
# open a session: "The credential is invalid" with the right password (nexus-bpzt7, cycles 1 and B of
# 2026-10-16; not a lockout) and "A remote session might have ended" (the rerun of 2026-10-17). Causes unknown.
# A failed open is retried every 10 s for 2 minutes while the guest is Running; a guest that is not Running
# stops the script at once. After a failure, the guest's failed sign-ins (events 4625 and 4776) from a minute
# before the first one are printed.
# Everything it prints goes through Write-Host: its pipeline output is its return value, the session.
function Open-GuestSession($Name) {
    $since = $null
    for ($i = 1; ; $i++) {
        try { $sess = New-PSSession -VMName $Name -Credential $cred -ErrorAction Stop; break }
        catch {
            if ($i -gt 12 -or (Get-VM -Name $Name).State -ne 'Running') { throw }
            if (-not $since) { $since = (Get-Date).AddMinutes(-1) }
            $why = ("" + $_.Exception.Message).Split("`n")[0].Trim()
            Write-Host "guest session open failed at $((Get-Date).ToUniversalTime().ToString('HH:mm:ss')) ($why); retry $i of 12 in 10 s"
            Start-Sleep 10
        }
    }
    if ($since) {
        $ev = @(Invoke-Command -Session $sess -ArgumentList $since {
            param($t)
            $fields = '^\s*(Account Name|Account For Which Logon Failed|Logon Account|Failure Reason|Status|Sub Status|Error Code|Logon Type|Caller Process Name|Source Workstation):'
            Get-WinEvent -FilterHashtable @{ LogName = 'Security'; Id = 4625, 4776; StartTime = $t } -ErrorAction SilentlyContinue | ForEach-Object {
                $keep = @($_.Message -split "`r?`n" | Where-Object { $_ -match $fields } | ForEach-Object { $_.Trim() -replace '\s+', ' ' })
                $_.TimeCreated.ToUniversalTime().ToString('HH:mm:ss') + " event $($_.Id): " + ($keep -join '; ')
            }
        })
        Write-Host "guest failed sign-ins since $($since.ToUniversalTime().ToString('HH:mm:ss')): $($ev.Count)"
        foreach ($e in $ev) { Write-Host "  $e" }
    }
    $sess
}
$t0 = Get-Date
$s = Open-GuestSession $VMName
try {
    $pre = Invoke-Command -Session $s -ArgumentList $StdUser, $AdminGit, $AppLocker, $Wdac {
        param($u, $adminGit, $appLocker, $wdac)
        $git = 'C:\Program Files\Git\cmd\git.exe'
        if ($adminGit -eq 'no') { 'the administrator installs no Git (-AdminGit no)' }
        elseif (-not (Test-Path $git)) {
            winget install --id Git.Git -e --source winget --accept-package-agreements --accept-source-agreements --disable-interactivity 2>&1 | Out-String -Width 200 | ForEach-Object { $_.TrimEnd() } | Select-Object -Last 4
            "winget exit=$LASTEXITCODE"
        }
        "gitexe=" + (Test-Path $git) + " (for all users, before the account exists)"
        New-Item -Force -Path 'HKLM:\SOFTWARE\Policies\Microsoft\Windows\OOBE' | Out-Null
        Set-ItemProperty -Path 'HKLM:\SOFTWARE\Policies\Microsoft\Windows\OOBE' -Name DisablePrivacyExperience -Value 1 -Type DWord
        Set-ItemProperty -Path 'HKLM:\SOFTWARE\Microsoft\Windows\CurrentVersion\Policies\System' -Name EnableFirstLogonAnimation -Value 0 -Type DWord
        if ($appLocker -ne 'no') {
            # AppLocker's own default rules (the ones "Create Default Rules" makes), all four collections enforced.
            $everyone = 'UserOrGroupSid="S-1-1-0" Action="Allow"'; $admins = 'UserOrGroupSid="S-1-5-32-544" Action="Allow"'
            $path = { param($id, $name, $who, $p) "<FilePathRule Id=`"$id`" Name=`"$name`" Description=`"`" $who><Conditions><FilePathCondition Path=`"$p`" /></Conditions></FilePathRule>" }
            $signed = { param($id, $name) "<FilePublisherRule Id=`"$id`" Name=`"$name`" Description=`"`" $everyone><Conditions><FilePublisherCondition PublisherName=`"*`" ProductName=`"*`" BinaryName=`"*`"><BinaryVersionRange LowSection=`"0.0.0.0`" HighSection=`"*`" /></FilePublisherCondition></Conditions></FilePublisherRule>" }
            $xml = '<AppLockerPolicy Version="1">' +
                '<RuleCollection Type="Exe" EnforcementMode="Enabled">' +
                (& $path '921cc481-6e17-4653-8f75-050b80acca20' 'Program Files' $everyone '%PROGRAMFILES%\*') +
                (& $path 'a61c8b2c-a319-4cd0-9690-d2177cad7b51' 'Windows' $everyone '%WINDIR%\*') +
                (& $path 'fd686d83-a829-4351-8ff4-27c7de5755d2' 'Administrators, all files' $admins '*') + '@ALLOW@</RuleCollection>' +
                '<RuleCollection Type="Msi" EnforcementMode="Enabled">' +
                (& $signed 'b7af7102-efde-4369-8a89-7a6a392d1473' 'Signed installers') +
                (& $path '5b290184-345a-4453-b184-45305f6d9a54' 'Windows Installer folder' $everyone '%WINDIR%\Installer\*') +
                (& $path '64ad46ff-0d71-4fa0-a30b-3f3d30c5433d' 'Administrators, all installers' $admins '*.*') + '</RuleCollection>' +
                '<RuleCollection Type="Script" EnforcementMode="Enabled">' +
                (& $path '06dce67b-934c-454f-a263-2515c8796a5d' 'Program Files' $everyone '%PROGRAMFILES%\*') +
                (& $path '9428c672-5fc3-47f4-808a-a0011f36dd2c' 'Windows' $everyone '%WINDIR%\*') +
                (& $path 'ed97d0cb-15ff-430f-b82c-8d7832957725' 'Administrators, all scripts' $admins '*') + '</RuleCollection>' +
                '<RuleCollection Type="Appx" EnforcementMode="Enabled">' +
                (& $signed 'a9e18c21-ff8f-43cf-b9fc-db40eed693ba' 'Signed packaged apps') + '</RuleCollection>' +
                '</AppLockerPolicy>'
            # -AppLocker allow: every folder under a profile from which this install starts a program, as the
            # audit run recorded them (8003), plus the two places an installer is downloaded to and started.
            $allow = ''
            $folders = @('.local\bin\*', '.local\share\nexus\*', 'AppData\Roaming\uv\*', 'AppData\Local\uv\cache\*', 'AppData\Local\Programs\Git\*', '.config\nexus\*', '.claude\downloads\*', 'AppData\Local\Temp\PortableGit.exe')
            if ($appLocker -in 'allow', 'allow-dll-audit', 'allow-dll') {
                foreach ($p in $folders) {
                    $allow += (& $path ([guid]::NewGuid().ToString()) "nexus install: $p" $everyone "%OSDRIVE%\Users\*\$p")
                }
                $xml = $xml.Replace('<RuleCollection Type="Script" EnforcementMode="Enabled">', '<RuleCollection Type="Script" EnforcementMode="AuditOnly">')
            }
            if ($appLocker -in 'allow-dll-audit', 'allow-dll') {
                # AppLocker's default DLL rules, the install's folders, and (allow-dll) the folders outside them that
                # the DLL runs of 2026-10-14 needed: the engine's tokenizer library, which it extracts under
                # .cache\nexus\djl, ONNX Runtime, which it extracts to %TEMP%\onnxruntime-java<digits> at every start
                # (blocked under the enforced rules; the audit run did not record it), and Defender's scanning DLL,
                # which Defender loads into every process.
                $dllExtra = @('.cache\nexus\*', 'AppData\Local\Temp\onnxruntime-java*')
                $dll = '<RuleCollection Type="Dll" EnforcementMode="' + $(if ($appLocker -eq 'allow-dll') { 'Enabled' } else { 'AuditOnly' }) + '">' +
                    (& $path 'fe64f59f-6fca-45e5-a731-0f6715327c38' 'Program Files DLLs' $everyone '%PROGRAMFILES%\*') +
                    (& $path 'bac4b0bf-6f1b-40e8-8627-8d4b2af0a8c7' 'Windows DLLs' $everyone '%WINDIR%\*') +
                    (& $path '3737732c-99b7-41d4-9037-9cddfb0de0d0' 'Administrators, all DLLs' $admins '*')
                if ($appLocker -eq 'allow-dll') { $dll += (& $path '6d1f0f63-9c34-4a0e-9a54-2f2b5f0bd1c4' 'Defender platform DLLs' $everyone '%OSDRIVE%\ProgramData\Microsoft\Windows Defender\Platform\*') }
                foreach ($p in @($folders) + $(if ($appLocker -eq 'allow-dll') { $dllExtra } else { @() })) {
                    $dll += (& $path ([guid]::NewGuid().ToString()) "nexus install DLLs: $p" $everyone "%OSDRIVE%\Users\*\$p")
                }
                $xml = $xml.Replace('</AppLockerPolicy>', $dll + '</RuleCollection></AppLockerPolicy>')
            }
            $xml = $xml.Replace('@ALLOW@', $allow)
            if ($appLocker -eq 'audit') { $xml = $xml.Replace('EnforcementMode="Enabled"', 'EnforcementMode="AuditOnly"') }
            $pf = "$env:TEMP\nxgate-applocker.xml"
            Set-Content -LiteralPath $pf -Value $xml -Encoding UTF8
            Set-AppLockerPolicy -XmlPolicy $pf -ErrorAction Stop
            Remove-Item -LiteralPath $pf -Force
            sc.exe config AppIDSvc start= auto | Out-Null
            "applocker: default rules set, mode=$appLocker; AppIDSvc start=auto exit=$LASTEXITCODE"
        }
        if ($wdac -ne 'no') {
            $ex = "$env:windir\schemas\CodeIntegrity\ExamplePolicies\DefaultWindows_Enforced.xml"
            if (-not (Test-Path -LiteralPath $ex)) { "NXG-RESULT: FAIL no $ex on this guest"; return }
            $x = "$env:TEMP\nxgate-wdac.xml"; $cip = "$env:TEMP\nxgate-wdac.cip"
            Copy-Item -LiteralPath $ex -Destination $x -Force
            if ($wdac -eq 'audit') { Set-RuleOption -FilePath $x -Option 3 }
            else {
                Set-RuleOption -FilePath $x -Option 18
                Set-RuleOption -FilePath $x -Option 11
                # The account does not exist yet; WDAC path rules take a wildcard only at the start or the end, so
                # the folders name the account. The same folders as the AppLocker allow-dll mode.
                $folders = @('.local\bin\*', '.local\share\nexus\*', 'AppData\Roaming\uv\*', 'AppData\Local\uv\cache\*', 'AppData\Local\Programs\Git\*', '.config\nexus\*', '.claude\downloads\*', 'AppData\Local\Temp\PortableGit.exe', '.cache\nexus\*', 'AppData\Local\Temp\onnxruntime-java*')
                # += flattens: each New-CIPolicyRule returns an array, and an array of those is refused by
                # Merge-CIPolicy ("Cannot convert Rule to Rule", 2026-10-15), which then left the policy with
                # no path rules at all.
                $rules = @(); foreach ($p in $folders) { $rules += New-CIPolicyRule -FilePathRule "C:\Users\$u\$p" }
                $m = "$env:TEMP\nxgate-wdac-merged.xml"
                Merge-CIPolicy -PolicyPaths $x -OutputFilePath $m -Rules $rules -ErrorAction Stop | Out-Null
                Move-Item -LiteralPath $m -Destination $x -Force -ErrorAction Stop
                $have = @(([xml](Get-Content -Raw -LiteralPath $x)).SiPolicy.FileRules.Allow | Where-Object { $_.FilePath -like "C:\Users\$u\*" }).Count
                "wdac path rules: $have of $($folders.Count) in the policy"
                if ($have -ne $folders.Count) { "NXG-RESULT: FAIL the WDAC policy has $have of $($folders.Count) path rules"; return }
            }
            $id = Set-CIPolicyIdInfo -FilePath $x -ResetPolicyID -PolicyName "nexus gate WDAC $wdac"
            ConvertFrom-CIPolicy -XmlFilePath $x -BinaryFilePath $cip | Out-Null
            $o = & "$env:windir\System32\CiTool.exe" --update-policy $cip -json 2>&1 | Out-String
            "wdac deploy: CiTool exit=$LASTEXITCODE " + ($o -replace '\s+', ' ').Trim()
            "wdac policy: mode=$wdac $id"
            Remove-Item -LiteralPath $x, $cip -Force -ErrorAction SilentlyContinue
            if ($o -notmatch '"OperationResult"\s*:\s*0' -and $LASTEXITCODE -ne 0) { "NXG-RESULT: FAIL CiTool did not deploy the WDAC policy"; return }
        }
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
    $pre
} finally { Remove-PSSession $s }
if (($pre | Out-String) -match 'NXG-RESULT: FAIL') { exit 6 }
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
$s = Open-GuestSession $VMName
try {
    $info = Invoke-Command -Session $s -ArgumentList $StdUser, $Wheel, $AppLocker, $Wdac {
        param($u, $Wheel, $appLocker, $wdac)
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
        if ($appLocker -ne 'no') {
            Start-Service AppIDSvc -ErrorAction SilentlyContinue
            "applocker AppIDSvc=" + (Get-Service AppIDSvc).Status
            $eff = Get-AppLockerPolicy -Effective
            "applocker effective: " + (($eff.RuleCollections | ForEach-Object { "$($_.RuleCollectionType)=$($_.EnforcementMode)($(@($_).Count) rules)" }) -join ' ')
            Copy-Item -LiteralPath "$env:SystemRoot\System32\whoami.exe" -Destination "$b\applocker-control.exe" -Force
            "applocker control for the account: " + (Test-AppLockerPolicy -PolicyObject $eff -Path "$b\applocker-control.exe" -User $u).PolicyDecision
            "applocker System32 for the account: " + (Test-AppLockerPolicy -PolicyObject $eff -Path "$env:SystemRoot\System32\whoami.exe" -User $u).PolicyDecision
            # The readings above are the policy on paper. Enforcement starts some time after the service does
            # (75 s on 2026-10-13, during which the account installed uv and conexus), so start the control
            # file as the account until the AppLocker log records that start as blocked (event 8004).
            $act = New-ScheduledTaskAction -Execute "$b\applocker-control.exe"
            Register-ScheduledTask -TaskName 'nxgate-applocker-control' -Action $act -Principal (New-ScheduledTaskPrincipal -UserId $u -LogonType Interactive -RunLevel Limited) -Force | Out-Null
            # Enforced rules record a refused start as 8004; audit-only rules record the same start as 8003.
            $want = if ($appLocker -eq 'audit') { 8003 } else { 8004 }
            $t1 = Get-Date; $hit = 0
            do {
                Start-ScheduledTask -TaskName 'nxgate-applocker-control'; Start-Sleep 10
                $hit = @(Get-WinEvent -FilterHashtable @{ LogName = 'Microsoft-Windows-AppLocker/EXE and DLL'; Id = 8004, 8003 } -ErrorAction SilentlyContinue | Where-Object { $_.Id -eq $want -and $_.Message -like '*APPLOCKER-CONTROL.EXE*' }).Count
            } while ($hit -eq 0 -and ((Get-Date) - $t1).TotalSeconds -lt 600)
            Unregister-ScheduledTask -TaskName 'nxgate-applocker-control' -Confirm:$false
            "applocker control start as the account: event $want recorded=$($hit -gt 0) after $([int]((Get-Date)-$t1).TotalSeconds)s"
        }
        if ($wdac -ne 'no') {
            $pol = @((& "$env:windir\System32\CiTool.exe" --list-policies -json 2>$null | Out-String | ConvertFrom-Json).Policies | Where-Object { $_.FriendlyName -eq "nexus gate WDAC $wdac" })
            "wdac policy active: count=$($pol.Count) enforced=$(@($pol | Where-Object { $_.IsEnforced }).Count) currently=$(@($pol | Where-Object { $_.IsCurrentlyEnforced }).Count)"
            $src = "$env:TEMP\nxgate-wdac-control.cs"
            Set-Content -LiteralPath $src -Value 'class P { static void Main() { System.Console.WriteLine("wdac control ran"); } }' -Encoding ASCII
            & "$env:windir\Microsoft.NET\Framework64\v4.0.30319\csc.exe" /nologo /out:"$b\wdac-control.exe" $src | Out-Null
            Remove-Item -LiteralPath $src -Force
            "wdac control built: " + (Test-Path "$b\wdac-control.exe") + " signed=" + ((Get-AuthenticodeSignature "$b\wdac-control.exe").Status)
            $act = New-ScheduledTaskAction -Execute "$b\wdac-control.exe"
            Register-ScheduledTask -TaskName 'nxgate-wdac-control' -Action $act -Principal (New-ScheduledTaskPrincipal -UserId $u -LogonType Interactive -RunLevel Limited) -Force | Out-Null
            # Enforced: a refused start is 3077. Audit mode records the same start as 3076 and lets it run.
            $want = if ($wdac -eq 'audit') { 3076 } else { 3077 }
            $t1 = Get-Date; $hit = 0
            do {
                Start-ScheduledTask -TaskName 'nxgate-wdac-control'; Start-Sleep 10
                $hit = @(Get-WinEvent -FilterHashtable @{ LogName = 'Microsoft-Windows-CodeIntegrity/Operational'; Id = $want; StartTime = $t1 } -ErrorAction SilentlyContinue | Where-Object { $_.Message -like '*wdac-control.exe*' }).Count
            } while ($hit -eq 0 -and ((Get-Date) - $t1).TotalSeconds -lt 300)
            Unregister-ScheduledTask -TaskName 'nxgate-wdac-control' -Confirm:$false
            "wdac control start as the account: event $want recorded=$($hit -gt 0) after $([int]((Get-Date)-$t1).TotalSeconds)s"
        }
    }
    $info
    if ($AdminGit -eq 'no') {
        $dest = Invoke-Command -Session $s -ArgumentList $StdUser { param($u) "C:\Users\$u\nx-gate" }
        Copy-Item -ToSession $s -Path (Join-Path $RunDir 'portable-git.ps1') -Destination "$dest\portable-git.ps1" -Force
        'copied portable-git.ps1'
    }
    $text = ($info | Out-String)
    if ($text -notmatch 'profile=True desktop shell processes=[1-9]') { "NXG-RESULT: FAIL $StdUser has no profile or no desktop"; $rc = 7 }
    elseif ($text -notmatch 'in Administrators=False') { "NXG-RESULT: FAIL $StdUser is an administrator"; $rc = 8 }
    elseif ($AdminGit -ne 'no' -and $text -notmatch 'gitexe=True') { 'NXG-RESULT: FAIL Git is not installed'; $rc = 9 }
    elseif ($AppLocker -ne 'no' -and ($text -notmatch 'applocker AppIDSvc=Running' -or $text -notmatch 'applocker control for the account: Denied' -or $text -notmatch 'applocker System32 for the account: Allowed' -or $text -notmatch ('applocker control start as the account: event ' + $(if ($AppLocker -eq 'audit') { '8003' } else { '8004' }) + ' recorded=True'))) { 'NXG-RESULT: FAIL AppLocker is not in effect for the account (service, control file or System32 reading above)'; $rc = 10 }
    elseif ($Wdac -ne 'no' -and ($text -notmatch 'wdac policy active: count=1 ' -or $text -notmatch 'wdac control built: True signed=NotSigned' -or $text -notmatch ('wdac control start as the account: event ' + $(if ($Wdac -eq 'audit') { '3076' } else { '3077' }) + ' recorded=True'))) { 'NXG-RESULT: FAIL WDAC is not in effect for the account (deploy, policy or control reading above)'; $rc = 11 }
    elseif ($AdminGit -eq 'no' -and $text -notmatch 'gitexe=False') { 'NXG-RESULT: FAIL Git is already installed for all users, so this is not a no-Git run'; $rc = 9 }
} finally { Remove-PSSession $s }
if ($rc -ne 0) { exit $rc }
'NXG-RESULT: ok'
