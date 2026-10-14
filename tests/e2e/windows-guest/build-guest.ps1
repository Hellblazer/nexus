# SPDX-License-Identifier: AGPL-3.0-or-later
#
# Host (the Hyper-V machine), elevated. Builds a Windows 11 gate guest from
# Microsoft's consumer ISO, shaped like nx-clean-win11: Gen 2, 4 CPUs, 8 GB
# static, TPM with a local key protector, Secure Boot (MicrosoftWindows), a 64 GB
# dynamic disk, no network adapter (run-gate.sh's restore step adds the Default
# Switch one), one local administrator signed in automatically at boot, and one
# checkpoint taken with the VM Off. build-guest.sh stages and runs it.
#
# stdin line 1 is the guest account's password, piped from the macOS keychain by
# build-guest.sh; it is never written to the host. The answer file carries a
# random one-use password generated here instead; the real one replaces it over
# PowerShell Direct and is then stored only as the guest's automatic sign-in
# secret (LSA DefaultPassword), and the answer file is deleted.
#
# Three things this script does on purpose, each learned on 2026-10-13 building
# nx-pro-win11 (T2 nexus_rdr/224-pro-guest-gate-2026-10-13):
#   - Stop-VM then Start-VM, never Restart-VM -Force: that is a hard reset and
#     loses registry writes made just before it.
#   - Automatic sign-in is set again after the first boot. IMAGE_STATE_COMPLETE
#     arrives before the first sign-in, and setup's own first sign-in turns
#     AutoAdminLogon off and leaves its temporary account defaultuser0.
#   - The checkpoint is taken only after two cold starts each show the account
#     owning the console with AutoAdminLogon still 1.
param(
    [string]$VMName = 'nx-pro-win11',
    [string]$Edition = 'Windows 11 Pro',
    [string]$ComputerName = 'NXPRO',
    [string]$GuestUser = 'nxguest',
    [string]$Iso = 'C:\build\guest\win11-consumer-26300.iso',
    [string]$IsoSha256 = 'BD4307DF32BC8AF33B39CCECB1174AEB345386630F89A2B86C7A4E36B55EA650',
    [string]$Checkpoint = 'clean-baseline-pro-2026-10-13',
    [string]$Root = 'C:\build\guest'
)
$ErrorActionPreference = 'Stop'
function Say($m) { [Console]::Out.WriteLine(("{0} {1}" -f (Get-Date).ToUniversalTime().ToString('HH:mm:ss'), $m)) }
$pw = [Console]::In.ReadLine()
if (-not $pw) { throw 'nothing on stdin' }
$sec = New-Object System.Security.SecureString; foreach ($c in $pw.ToCharArray()) { $sec.AppendChar($c) }; $sec.MakeReadOnly(); $pw = $null; $c = $null
$cred = New-Object PSCredential($GuestUser, $sec)

if (Get-Process Runner.Worker -ErrorAction SilentlyContinue) { throw 'the release runner is busy' }
if (Get-VM -Name $VMName -ErrorAction SilentlyContinue) { throw "VM $VMName already exists" }
if ($VMName -match 'qwen|llama') { throw 'the VM name may not contain qwen or llama on this host' }
$dir = Join-Path $Root $VMName
if (Test-Path -LiteralPath $dir) { throw "$dir already exists" }
if ((Get-FileHash -LiteralPath $Iso -Algorithm SHA256).Hash -ne $IsoSha256) { throw 'the ISO does not match the published hash' }
Say 'iso hash ok'

# One-use password for the answer file: letters and digits after a fixed prefix,
# so it needs no XML escaping and meets the complexity rule.
$rng = [System.Security.Cryptography.RandomNumberGenerator]::Create(); $bytes = New-Object byte[] 24; $rng.GetBytes($bytes)
$alpha = 'ABCDEFGHJKLMNPQRSTUVWXYZabcdefghijkmnopqrstuvwxyz23456789'
$boot = 'Nx1!' + (-join ($bytes | ForEach-Object { $alpha[$_ % $alpha.Length] }))
$bsec = New-Object System.Security.SecureString; foreach ($c in $boot.ToCharArray()) { $bsec.AppendChar($c) }; $bsec.MakeReadOnly()
$bcred = New-Object PSCredential($GuestUser, $bsec)

$vhd = Join-Path $dir "$VMName.vhdx"
New-Item -ItemType Directory -Path $dir | Out-Null
$img = Mount-DiskImage -ImagePath $Iso -PassThru
$mounted = $false
try {
    $isoLetter = ($img | Get-Volume).DriveLetter
    $wim = "${isoLetter}:\sources\install.wim"
    if (-not (Test-Path -LiteralPath $wim)) { $wim = "${isoLetter}:\sources\install.esd" }
    $images = Get-WindowsImage -ImagePath $wim
    $images | ForEach-Object { Say ("image {0}: {1}" -f $_.ImageIndex, $_.ImageName) }
    $pick = @($images | Where-Object { $_.ImageName -eq $Edition })
    if ($pick.Count -ne 1) { throw "no single image named '$Edition' in the ISO" }

    New-VHD -Path $vhd -SizeBytes 64GB -Dynamic | Out-Null
    $disk = Mount-VHD -Path $vhd -Passthru | Get-Disk
    $mounted = $true
    Initialize-Disk -Number $disk.Number -PartitionStyle GPT
    # Created as a basic data partition so Format-Volume will take it; retyped to EFI System after bcdboot.
    $efi = New-Partition -DiskNumber $disk.Number -Size 260MB -GptType '{ebd0a0a2-b9e5-4433-87c0-68b6b72699c7}' -AssignDriveLetter
    Format-Volume -Partition $efi -FileSystem FAT32 -NewFileSystemLabel System -Confirm:$false | Out-Null
    $win = New-Partition -DiskNumber $disk.Number -UseMaximumSize -AssignDriveLetter
    Format-Volume -Partition $win -FileSystem NTFS -NewFileSystemLabel Windows -Confirm:$false | Out-Null
    $s = (Get-Partition -DiskNumber $disk.Number -PartitionNumber $efi.PartitionNumber).DriveLetter
    $w = (Get-Partition -DiskNumber $disk.Number -PartitionNumber $win.PartitionNumber).DriveLetter
    if (-not $s -or -not $w) { throw 'the new partitions got no drive letters' }
    Say "applying image index $($pick[0].ImageIndex) ($Edition) to ${w}:"
    Expand-WindowsImage -ImagePath $wim -Index $pick[0].ImageIndex -ApplyPath "${w}:\" | Out-Null
    Say 'image applied'
    & "${w}:\Windows\System32\bcdboot.exe" "${w}:\Windows" /s "${s}:" /f UEFI | Out-Null
    if ($LASTEXITCODE -ne 0) { throw "bcdboot exit $LASTEXITCODE" }

    $xml = @"
<?xml version="1.0" encoding="utf-8"?>
<unattend xmlns="urn:schemas-microsoft-com:unattend" xmlns:wcm="http://schemas.microsoft.com/WMIConfig/2002/State">
  <settings pass="specialize">
    <component name="Microsoft-Windows-Shell-Setup" processorArchitecture="amd64" publicKeyToken="31bf3856ad364e35" language="neutral" versionScope="nonSxS">
      <ComputerName>$ComputerName</ComputerName>
      <TimeZone>Pacific Standard Time</TimeZone>
    </component>
    <component name="Microsoft-Windows-Deployment" processorArchitecture="amd64" publicKeyToken="31bf3856ad364e35" language="neutral" versionScope="nonSxS">
      <RunSynchronous>
        <RunSynchronousCommand wcm:action="add">
          <Order>1</Order>
          <Path>reg.exe add HKLM\SOFTWARE\Microsoft\Windows\CurrentVersion\OOBE /v BypassNRO /t REG_DWORD /d 1 /f</Path>
        </RunSynchronousCommand>
      </RunSynchronous>
    </component>
  </settings>
  <settings pass="oobeSystem">
    <component name="Microsoft-Windows-International-Core" processorArchitecture="amd64" publicKeyToken="31bf3856ad364e35" language="neutral" versionScope="nonSxS">
      <InputLocale>en-US</InputLocale>
      <SystemLocale>en-US</SystemLocale>
      <UILanguage>en-US</UILanguage>
      <UserLocale>en-US</UserLocale>
    </component>
    <component name="Microsoft-Windows-Shell-Setup" processorArchitecture="amd64" publicKeyToken="31bf3856ad364e35" language="neutral" versionScope="nonSxS">
      <OOBE>
        <HideEULAPage>true</HideEULAPage>
        <HideOnlineAccountScreens>true</HideOnlineAccountScreens>
        <HideWirelessSetupInOOBE>true</HideWirelessSetupInOOBE>
        <HideOEMRegistrationScreen>true</HideOEMRegistrationScreen>
        <ProtectYourPC>3</ProtectYourPC>
      </OOBE>
      <UserAccounts>
        <LocalAccounts>
          <LocalAccount wcm:action="add">
            <Name>$GuestUser</Name>
            <Group>Administrators</Group>
            <Password><Value>$boot</Value><PlainText>true</PlainText></Password>
          </LocalAccount>
        </LocalAccounts>
      </UserAccounts>
    </component>
  </settings>
</unattend>
"@
    New-Item -ItemType Directory -Path "${w}:\Windows\Panther" -Force | Out-Null
    [System.IO.File]::WriteAllText("${w}:\Windows\Panther\unattend.xml", $xml, (New-Object System.Text.UTF8Encoding($false)))
    $xml = $null; $boot = $null
    Set-Partition -DiskNumber $disk.Number -PartitionNumber $efi.PartitionNumber -GptType '{c12a7328-f81f-11d2-ba4b-00a0c93ec93b}'
} finally {
    if ($mounted) { Dismount-VHD -Path $vhd }
    Dismount-DiskImage -ImagePath $Iso | Out-Null
}
Say 'disk ready'

New-VM -Name $VMName -Generation 2 -MemoryStartupBytes 8GB -VHDPath $vhd -Path $Root | Out-Null
Set-VM -Name $VMName -ProcessorCount 4 -StaticMemory -AutomaticCheckpointsEnabled $false -CheckpointType Standard -AutomaticStartAction Nothing -AutomaticStopAction ShutDown
Get-VMNetworkAdapter -VMName $VMName | Remove-VMNetworkAdapter
Set-VMFirmware -VMName $VMName -EnableSecureBoot On -SecureBootTemplate MicrosoftWindows -FirstBootDevice (Get-VMHardDiskDrive -VMName $VMName)
Set-VMKeyProtector -VMName $VMName -NewLocalKeyProtector
Enable-VMTPM -VMName $VMName
Start-VM -Name $VMName
Say 'vm created and started; waiting for Windows setup'

function Probe($c, [scriptblock]$sb) {
    try { return "" + (Invoke-Command -VMName $VMName -Credential $c -ErrorAction Stop -ScriptBlock $sb) } catch { return '' }
}
function WaitFor($c, $minutes, [scriptblock]$sb, $want) {
    $end = (Get-Date).AddMinutes($minutes)
    while ((Get-Date) -lt $end) { if ((Probe $c $sb) -match $want) { return $true }; Start-Sleep 15 }
    return $false
}
if (-not (WaitFor $bcred 40 { (Get-ItemProperty 'HKLM:\SOFTWARE\Microsoft\Windows\CurrentVersion\Setup\State').ImageState } 'IMAGE_STATE_COMPLETE')) {
    Say 'FAIL: setup did not finish in 40 minutes; the VM is left running for inspection'; exit 6
}
Say 'setup complete'

$setAuto = {
    param($u, [System.Security.SecureString]$s, [bool]$setPassword)
    $ErrorActionPreference = 'Stop'
    if ($setPassword) { Set-LocalUser -Name $u -Password $s -PasswordNeverExpires $true }
    if (-not ('NxLsa' -as [type])) {
        Add-Type -TypeDefinition @'
using System; using System.Runtime.InteropServices;
public static class NxLsa {
  [StructLayout(LayoutKind.Sequential)] struct US { public ushort Length; public ushort MaximumLength; public IntPtr Buffer; }
  [StructLayout(LayoutKind.Sequential)] struct OA { public int Length; public IntPtr RootDirectory; public IntPtr ObjectName; public uint Attributes; public IntPtr SecurityDescriptor; public IntPtr SecurityQualityOfService; }
  [DllImport("advapi32.dll")] static extern uint LsaOpenPolicy(IntPtr SystemName, ref OA oa, uint access, out IntPtr handle);
  [DllImport("advapi32.dll")] static extern uint LsaStorePrivateData(IntPtr handle, ref US key, ref US data);
  [DllImport("advapi32.dll")] static extern uint LsaClose(IntPtr h);
  [DllImport("advapi32.dll")] static extern int LsaNtStatusToWinError(uint s);
  static US S(IntPtr p, int chars) { US u = new US(); u.Buffer = p; u.Length = (ushort)(chars * 2); u.MaximumLength = (ushort)((chars + 1) * 2); return u; }
  public static int Store(string key, System.Security.SecureString val) {
    OA oa = new OA(); oa.Length = Marshal.SizeOf(typeof(OA));
    IntPtr h; uint st = LsaOpenPolicy(IntPtr.Zero, ref oa, 0x20, out h);
    if (st != 0) return LsaNtStatusToWinError(st);
    IntPtr kp = Marshal.StringToHGlobalUni(key); IntPtr vp = Marshal.SecureStringToGlobalAllocUnicode(val);
    US k = S(kp, key.Length); US v = S(vp, val.Length);
    st = LsaStorePrivateData(h, ref k, ref v); LsaClose(h);
    Marshal.ZeroFreeGlobalAllocUnicode(vp); Marshal.FreeHGlobal(kp);
    return st == 0 ? 0 : LsaNtStatusToWinError(st);
  }
}
'@
    }
    $rc = [NxLsa]::Store('DefaultPassword', $s)
    if ($rc -ne 0) { throw "LsaStorePrivateData failed: $rc" }
    $wl = 'HKLM:\SOFTWARE\Microsoft\Windows NT\CurrentVersion\Winlogon'
    Set-ItemProperty -Path $wl -Name AutoAdminLogon -Value '1'
    Set-ItemProperty -Path $wl -Name DefaultUserName -Value $u
    Set-ItemProperty -Path $wl -Name DefaultDomainName -Value $env:COMPUTERNAME
    foreach ($n in 'DefaultPassword', 'AutoLogonCount', 'AutoLogonSID') { Remove-ItemProperty -Path $wl -Name $n -ErrorAction SilentlyContinue }
    Set-ItemProperty -Path 'HKLM:\SOFTWARE\Microsoft\Windows\CurrentVersion\Policies\System' -Name EnableFirstLogonAnimation -Value 0 -Type DWord
    if (Get-LocalUser -Name defaultuser0 -ErrorAction SilentlyContinue) { Remove-LocalUser -Name defaultuser0; 'removed defaultuser0' }
    foreach ($f in 'C:\Windows\Panther\unattend.xml', 'C:\Windows\Panther\unattend-original.xml', 'C:\Windows\System32\Sysprep\unattend.xml') {
        if (Test-Path -LiteralPath $f) { Remove-Item -LiteralPath $f -Force; "removed $f" }
    }
    $os = Get-CimInstance Win32_OperatingSystem; "os=$($os.Caption) build $($os.BuildNumber) computer=$env:COMPUTERNAME"
}

# The bootstrap password signs in once, to replace itself.
Invoke-Command -VMName $VMName -Credential $bcred -ArgumentList $GuestUser, $sec, $true -ScriptBlock $setAuto | ForEach-Object { Say $_ }
$bcred = $null; $bsec = $null

# First cold start: setup's own first sign-in may turn automatic sign-in off again, so set it once more.
Stop-VM -Name $VMName; Start-VM -Name $VMName
if (-not (WaitFor $cred 10 { 1 } '1')) { Say 'FAIL: no PowerShell Direct with the real password after the first restart; the VM is left running'; exit 7 }
Invoke-Command -VMName $VMName -Credential $cred -ArgumentList $GuestUser, $sec, $false -ScriptBlock $setAuto | ForEach-Object { Say $_ }

$good = 0
foreach ($n in 1, 2) {
    Stop-VM -Name $VMName; Start-VM -Name $VMName
    # The console's signed-in user. Not `query user`: Home has no query.exe or quser.exe (measured 2026-10-14).
    $console = WaitFor $cred 8 { (Get-CimInstance Win32_ComputerSystem).UserName } ('\\' + [regex]::Escape($GuestUser) + '$')
    Start-Sleep 60
    $a = Probe $cred {
        $wl = Get-ItemProperty 'HKLM:\SOFTWARE\Microsoft\Windows NT\CurrentVersion\Winlogon'
        "AutoAdminLogon=$($wl.AutoAdminLogon) DefaultUserName=$($wl.DefaultUserName) plainDefaultPassword=$([bool]$wl.PSObject.Properties['DefaultPassword']) answerFiles=$(@(Get-ChildItem C:\Windows\Panther -Filter '*.xml' -ErrorAction SilentlyContinue | Select-String -Pattern 'PlainText' -List).Count)"
    }
    Say "cold start ${n}: console=$console $a"
    if ($console -and $a -match 'AutoAdminLogon=1 ' -and $a -match 'plainDefaultPassword=False' -and $a -match 'answerFiles=0') { $good++ }
}
Stop-VM -Name $VMName
if ((Get-VM -Name $VMName).State -ne 'Off') { throw 'the guest did not power off' }
if ($good -ne 2) { Say "FAIL: automatic sign-in held on $good of 2 cold starts; no checkpoint taken, the VM is Off"; exit 7 }
Checkpoint-VM -Name $VMName -SnapshotName $Checkpoint
$v = Get-VM -Name $VMName
Say "done: state=$($v.State) nics=$(@(Get-VMNetworkAdapter -VMName $VMName).Count) checkpoints=[$((Get-VMSnapshot -VMName $VMName | ForEach-Object Name) -join ', ')]"
[Console]::Out.WriteLine('BUILD-GUEST-RESULT: ok')
