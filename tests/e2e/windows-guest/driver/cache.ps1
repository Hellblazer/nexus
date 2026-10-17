# SPDX-License-Identifier: AGPL-3.0-or-later
#
# Host. stdin line 1 = guest account secret; never written anywhere.
# run-gate.sh --cache on (the default; nexus-bpzt7): keeps what every restored
# guest would otherwise download again in C:\build\guest\cache on the host,
# which every gate guest on this host shares.
#   -Mode seed     before the install: unpack the cached uv cache into the
#                  installing account's %LOCALAPPDATA%\uv\cache, and copy the
#                  cached Claude Code binary into its nx-gate\claude-cache,
#                  where inst.ps1 uses it only if its sha256 matches the
#                  manifest of the version Anthropic's download service names
#                  as latest. uv still resolves against PyPI; only the files
#                  come from the cache, so the install runs the published path.
#                  When the cache holds models for this -Key, unpack them into
#                  the account's .cache\nexus\onnx_models, where nx init reuses
#                  a model file it finds in place. The engine binary and the
#                  PostgreSQL bundle are never cached: their sha256 and
#                  signature checks stay on the gated download path.
#                  The install step's own downloads come from payloads\ the same
#                  way (nexus-bpzt7), each keyed by its own version in keys.txt:
#                  uv=<version> (uv's release zip), portablegit=<version>,
#                  winget-git=<version> (Git for Windows' installer) and
#                  python=<archive name> (the Python archive uv downloads). The
#                  guest is told where they are and checks each one itself
#                  against the upstream's published sha256 before it uses it;
#                  a missing or mismatching one is downloaded as before. When
#                  uv's cache holds exactly this -Key (a published version, not
#                  a candidate wheel), a marker file lets inst.ps1 install
#                  conexus from that cache with uv --offline.
#   -Mode harvest  after a successful install: copy that account's uv cache,
#                  installed claude.exe and onnx_models back to the host when the
#                  cache does not yet hold this install (keys.txt: conexus=<-Key>,
#                  claude=<version>, models=<-Key>). nx init checked the models'
#                  sha256 when it downloaded them. They are keyed by -Key because
#                  nx init reuses a model by size alone, so an archive from an
#                  older release must not stand in for a changed pinned model.
#                  The payloads the install step recorded as downloaded or
#                  mismatching (payloads\record.txt in the guest) are filled
#                  the same way: PortableGit and the Python archive are copied
#                  from the guest, uv's zip and the Git installer are downloaded
#                  by the host, each checked against the sha256 the guest
#                  recorded from the upstream; a version other than the new one
#                  is removed from the cache.
# Host writes stay under C:\build\guest\cache; files there are replaced or
# removed by exact name only. In the guest: the account's uv cache folder,
# nx-gate\claude-cache, and one archive file in nx-gate, removed after use.
param(
    [Parameter(Mandatory = $true)][string]$VMName,
    [Parameter(Mandatory = $true)][string]$GuestUser,
    [Parameter(Mandatory = $true)][ValidateSet('seed', 'harvest')][string]$Mode,
    [Parameter(Mandatory = $true)][string]$Account,
    [Parameter(Mandatory = $true)][string]$Key
)
if ($Account -notmatch '^[A-Za-z0-9._-]+$') { throw 'Account must be a plain account name' }
if ($Key -notmatch '^[A-Za-z0-9._+-]+$') { throw 'Key has a character outside A-Za-z0-9._+-' }
$root = 'C:\build\guest\cache'
$uvTar = Join-Path $root 'uv-cache.tar'
$keys = Join-Path $root 'keys.txt'
$modelsTar = Join-Path $root 'models.tar'
$payloads = Join-Path $root 'payloads'
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
# It then sets NX_NO_TELEMETRY=1 in the guest, in the session and Machine-wide: a restored guest is a fresh
# install, and every run used to send conexus an install ping (nexus-ca5rw). The scheduled-task payloads
# set it too, since an Interactive task joins a signed-in session whose environment predates this.
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
    Invoke-Command -Session $sess {
        [Environment]::SetEnvironmentVariable('NX_NO_TELEMETRY', '1', 'Machine'); $env:NX_NO_TELEMETRY = '1'
    } | Out-Null
    $sess
}
New-Item -ItemType Directory -Force $root | Out-Null
$have = @(Get-Content $keys -ErrorAction SilentlyContinue | Where-Object { $_ -match '^(conexus|claude|models|uv|portablegit|winget-git|python)=[A-Za-z0-9._+-]+$' })
function ClaudeBin($v) { Join-Path $root "claude-$v-win32-x64.exe" }
# One cached install payload per kind, named by its own version: the path under payloads\ that keys.txt's
# "<kind>=<value>" line stands for (the same relative path in the guest's nx-gate\payloads).
function PayRel($kind, $v) {
    switch ($kind) {
        'uv' { "uv-$v\uv-x86_64-pc-windows-msvc.zip" }
        'portablegit' { "PortableGit-$v-64-bit.7z.exe" }
        'winget-git' { "Git-$v-64-bit.exe" }
        'python' { "python\$v" }
    }
}
function HostPay($kind, $v) { Join-Path $payloads (PayRel $kind $v) }
function Sha($p) { (Get-FileHash -Algorithm SHA256 -LiteralPath $p).Hash.ToLowerInvariant() }
# True when keys.txt names this payload, its file is there, and (when a sha256 is given) the file has it.
function HavePay($kind, $v, $sha) {
    $f = HostPay $kind $v
    ($have -contains "$kind=$v") -and (Test-Path -LiteralPath $f) -and (-not $sha -or (Sha $f) -eq $sha)
}
# Move a checked temp file into place as the one cached payload of its kind, and remove the other versions
# of that kind by exact name (uv's version folder is removed only when it is empty).
function PlacePay($kind, $v, $tmp) {
    $dst = HostPay $kind $v
    New-Item -ItemType Directory -Force (Split-Path $dst) | Out-Null
    Move-Item -LiteralPath $tmp -Destination $dst -Force
    foreach ($o in @($script:keep | Where-Object { $_ -like "$kind=*" } | ForEach-Object { $_.Substring($kind.Length + 1) })) {
        if ($o -eq $v) { continue }
        Remove-Item -LiteralPath (HostPay $kind $o) -Force -ErrorAction SilentlyContinue
        if ($kind -eq 'uv') { Remove-Item -LiteralPath (Split-Path (HostPay $kind $o)) -ErrorAction SilentlyContinue }
    }
    $script:keep = @($script:keep | Where-Object { $_ -notlike "$kind=*" }) + "$kind=$v"
}
# Host to guest over PowerShell Direct (Sam, 2026-10-15: no host-side VM settings, so no Copy-VMFile).
function ToGuest($src, $dst) { Copy-Item -ToSession $s -Path $src -Destination $dst -Force; "copied $(Split-Path $src -Leaf)" }
$t0 = Get-Date
$s = Open-GuestSession $VMName
try {
    $prof = "C:\Users\$Account"
    if (-not (Invoke-Command -Session $s -ArgumentList $prof { param($p) Test-Path $p })) { 'NXG-RESULT: FAIL no profile for the account in the guest'; exit 3 }
    $guestTar = "$prof\nx-gate\uv-cache.tar"
    $guestCc = "$prof\nx-gate\claude-cache"
    $guestPay = "$prof\nx-gate\payloads"
    if ($Mode -eq 'seed') {
        if (Test-Path $uvTar) {
            ToGuest $uvTar $guestTar
            Invoke-Command -Session $s -ArgumentList $prof, $guestTar {
                param($p, $t)
                $d = "$p\AppData\Local\uv"
                New-Item -ItemType Directory -Force $d | Out-Null
                tar.exe -xf $t -C $d; "unpack exit=$LASTEXITCODE"
                Remove-Item -LiteralPath $t -Force
                'seeded uv cache: {0:N0} MB' -f ((Get-ChildItem "$d\cache" -Recurse -File -ErrorAction SilentlyContinue | Measure-Object Length -Sum).Sum / 1MB)
            }
        } else { 'seeded uv cache: none (the cache has no uv archive yet)' }
        $cv = @($have | Where-Object { $_ -like 'claude=*' } | ForEach-Object { $_.Substring(7) })
        if ($cv.Count -eq 1 -and (Test-Path (ClaudeBin $cv[0]))) {
            Invoke-Command -Session $s -ArgumentList $guestCc { param($d) New-Item -ItemType Directory -Force $d | Out-Null }
            ToGuest (ClaudeBin $cv[0]) "$guestCc\claude-$($cv[0])-win32-x64.exe"
            "seeded claude-cache: $($cv[0])"
        } else { 'seeded claude-cache: none' }
        if ($have -contains "models=$Key" -and (Test-Path $modelsTar)) {
            $guestMt = "$prof\nx-gate\models.tar"
            ToGuest $modelsTar $guestMt
            Invoke-Command -Session $s -ArgumentList $prof, $guestMt {
                param($p, $t)
                $d = "$p\.cache\nexus"
                New-Item -ItemType Directory -Force $d | Out-Null
                tar.exe -xf $t -C $d; "unpack exit=$LASTEXITCODE"
                Remove-Item -LiteralPath $t -Force
                Get-ChildItem "$d\onnx_models" -Recurse -File | ForEach-Object { 'seeded model file {0} {1:N0} MB' -f $_.FullName.Substring($d.Length + 1), ($_.Length / 1MB) }
            }
        } else { "seeded models: none for $Key" }
        # Install payloads (nexus-bpzt7): every one keys.txt names and the host still has. Their presence in the
        # guest is also what tells inst.ps1 the cache is on, so the folder is made even when nothing goes in it.
        Invoke-Command -Session $s -ArgumentList $guestPay { param($d) New-Item -ItemType Directory -Force "$d\python" | Out-Null }
        $nPay = 0
        foreach ($k in @($have | Where-Object { $_ -match '^(uv|portablegit|winget-git|python)=' })) {
            $kind = $k.Substring(0, $k.IndexOf('=')); $val = $k.Substring($k.IndexOf('=') + 1)
            $rel = PayRel $kind $val
            if (-not (Test-Path -LiteralPath (HostPay $kind $val))) { "seeded payload ${k}: the file is missing on the host"; continue }
            Invoke-Command -Session $s -ArgumentList "$guestPay\$rel" { param($f) New-Item -ItemType Directory -Force (Split-Path $f) | Out-Null }
            ToGuest (HostPay $kind $val) "$guestPay\$rel"
            $nPay++
        }
        "seeded payloads: $nPay"
        # uv --offline is only right when uv's cache holds exactly this published version's install.
        if ($Key -notlike 'w-*' -and $have -contains "conexus=$Key" -and (Test-Path $uvTar)) {
            Invoke-Command -Session $s -ArgumentList "$guestPay\uv-offline.txt", "conexus=$Key" { param($f, $v) Set-Content -LiteralPath $f -Value $v -Encoding ASCII }
            "seeded uv-offline marker: conexus=$Key"
        } else { 'seeded uv-offline marker: none' }
        # The installing account writes its own uv cache (locks, new entries): make it the owner of what was copied in.
        Invoke-Command -Session $s -ArgumentList $Account, "$prof\AppData\Local\uv", $guestCc, "$prof\.cache", $guestPay {
            param($a, $u, $c, $m, $y)
            foreach ($d in @($u, $c, $m, $y)) { if (Test-Path $d) { icacls $d /setowner $a /T /C /Q | Out-Null; icacls $d /grant "${a}:(OI)(CI)F" /T /C /Q | Out-Null } }
        }
    } else {
        $claudeVer = Invoke-Command -Session $s -ArgumentList $prof {
            param($p) $e = "$p\.local\bin\claude.exe"
            if (Test-Path $e) { ((& $e --version 2>$null) -split ' ')[0] } else { '' }
        }
        $keep = @($have)
        if ($have -notcontains "conexus=$Key") {
            $n = Invoke-Command -Session $s -ArgumentList $prof, $guestTar {
                param($p, $t)
                tar.exe -cf $t -C "$p\AppData\Local\uv" cache; "pack exit=$LASTEXITCODE"
            }
            $n
            $tmp = Join-Path $root 'uv-cache.tar.new'
            Copy-Item -FromSession $s -Path $guestTar -Destination $tmp -Force
            Invoke-Command -Session $s -ArgumentList $guestTar { param($t) Remove-Item -LiteralPath $t -Force }
            Move-Item -LiteralPath $tmp -Destination $uvTar -Force
            # One uv archive: it now holds what this install used, so it stands for this key alone.
            $keep = @($keep | Where-Object { $_ -notlike 'conexus=*' }) + "conexus=$Key"
            'harvested uv cache: {0:N0} MB' -f ((Get-Item $uvTar).Length / 1MB)
        } else { "harvest: the uv archive already holds conexus=$Key" }
        if ($claudeVer -match '^[0-9]+\.[0-9]+\.[0-9]+$' -and $have -notcontains "claude=$claudeVer") {
            Copy-Item -FromSession $s -Path "$prof\.local\bin\claude.exe" -Destination (ClaudeBin $claudeVer) -Force
            foreach ($old in @($have | Where-Object { $_ -like 'claude=*' } | ForEach-Object { $_.Substring(7) })) {
                if ($old -ne $claudeVer) { Remove-Item -LiteralPath (ClaudeBin $old) -Force -ErrorAction SilentlyContinue }
            }
            $keep = @($keep | Where-Object { $_ -notlike 'claude=*' }) + "claude=$claudeVer"
            "harvested claude.exe $claudeVer"
        } else { "harvest: claude $claudeVer already cached or not found" }
        if ($have -notcontains "models=$Key") {
            $guestMt = "$prof\nx-gate\models.tar"
            $found = Invoke-Command -Session $s -ArgumentList $prof, $guestMt {
                param($p, $t)
                if (-not (Test-Path "$p\.cache\nexus\onnx_models")) { return 'no onnx_models' }
                tar.exe -cf $t -C "$p\.cache\nexus" onnx_models; "pack exit=$LASTEXITCODE"
            }
            $found
            if ($found -like 'pack exit=0') {
                $tmp = Join-Path $root 'models.tar.new'
                Copy-Item -FromSession $s -Path $guestMt -Destination $tmp -Force
                Invoke-Command -Session $s -ArgumentList $guestMt { param($t) Remove-Item -LiteralPath $t -Force }
                Move-Item -LiteralPath $tmp -Destination $modelsTar -Force
                $keep = @($keep | Where-Object { $_ -notlike 'models=*' }) + "models=$Key"
                'harvested models: {0:N0} MB' -f ((Get-Item $modelsTar).Length / 1MB)
            }
        } else { "harvest: the models archive already holds models=$Key" }
        # Install payloads (nexus-bpzt7). Each is filled on its own and a failure only skips that one.
        $gp = Invoke-Command -Session $s -ArgumentList $guestPay {
            param($d)
            @{
                record = @(Get-Content "$d\record.txt" -ErrorAction SilentlyContinue)
                python = @(Get-ChildItem "$d\python" -File -ErrorAction SilentlyContinue | Sort-Object LastWriteTime -Descending | ForEach-Object Name)
            }
        }
        $ProgressPreference = 'SilentlyContinue'
        # the newest archive in the guest's folder that the host does not hold yet (uv put it there)
        $pyName = @($gp.python | Where-Object { $_ -match '^[A-Za-z0-9._+-]+$' -and -not (HavePay 'python' $_ '') } | Select-Object -First 1)
        if ($pyName.Count -eq 1) {
            try {
                $tmp = Join-Path $root 'payload.new'
                Copy-Item -FromSession $s -Path "$guestPay\python\$($pyName[0])" -Destination $tmp -Force -ErrorAction Stop
                PlacePay 'python' $pyName[0] $tmp
                "harvested python archive $($pyName[0])"
            } catch { Remove-Item -LiteralPath (Join-Path $root 'payload.new') -Force -ErrorAction SilentlyContinue; "harvest python: $($_.Exception.Message)" }
        } else { 'harvest: no new python archive in the guest' }
        foreach ($line in @($gp.record | Sort-Object -Unique)) {
            $rec = "$line"
            try {
                if ($rec -match '^portablegit=([A-Za-z0-9._+-]+)\|([0-9a-f]{64})$') {
                    $ver = $Matches[1]; $sha = $Matches[2]
                    if (HavePay 'portablegit' $ver $sha) { "harvest: PortableGit $ver already cached"; continue }
                    $tmp = Join-Path $root 'payload.new'
                    Copy-Item -FromSession $s -Path "$guestPay\$(PayRel 'portablegit' $ver)" -Destination $tmp -Force -ErrorAction Stop
                    if ((Sha $tmp) -ne $sha) { Remove-Item -LiteralPath $tmp -Force; throw "the copied PortableGit $ver is not the sha256 the guest recorded" }
                    PlacePay 'portablegit' $ver $tmp
                    "harvested PortableGit $ver"
                } elseif ($rec -match '^uv=([0-9]+\.[0-9]+\.[0-9]+)\|([0-9a-f]{64})$') {
                    $ver = $Matches[1]; $sha = $Matches[2]
                    if (HavePay 'uv' $ver $sha) { "harvest: uv $ver already cached"; continue }
                    $tmp = Join-Path $root 'payload.new'
                    Invoke-WebRequest -UseBasicParsing -Uri "https://github.com/astral-sh/uv/releases/download/$ver/uv-x86_64-pc-windows-msvc.zip" -OutFile $tmp -ErrorAction Stop
                    if ((Sha $tmp) -ne $sha) { Remove-Item -LiteralPath $tmp -Force; throw "the downloaded uv $ver zip is not the published sha256" }
                    PlacePay 'uv' $ver $tmp
                    "harvested uv $ver"
                } elseif ($rec -match '^winget-git=([A-Za-z0-9._+-]+)\|(https://github\.com/git-for-windows/git/releases/download/[A-Za-z0-9._/+-]+)\|([0-9a-f]{64})$') {
                    $ver = $Matches[1]; $url = $Matches[2]; $sha = $Matches[3]
                    if (HavePay 'winget-git' $ver $sha) { "harvest: Git installer $ver already cached"; continue }
                    $tmp = Join-Path $root 'payload.new'
                    Invoke-WebRequest -UseBasicParsing -Uri $url -OutFile $tmp -ErrorAction Stop
                    if ((Sha $tmp) -ne $sha) { Remove-Item -LiteralPath $tmp -Force; throw "the downloaded Git installer $ver is not the sha256 winget names" }
                    PlacePay 'winget-git' $ver $tmp
                    "harvested Git installer $ver"
                }
            } catch { Remove-Item -LiteralPath (Join-Path $root 'payload.new') -Force -ErrorAction SilentlyContinue; "harvest payload ($rec): $($_.Exception.Message)" }
        }
        $keep | Sort-Object -Unique | Set-Content -Encoding UTF8 $keys
    }
} finally { Remove-PSSession $s }
"cache $Mode took $([int]((Get-Date)-$t0).TotalSeconds)s"
'NXG-RESULT: ok'
