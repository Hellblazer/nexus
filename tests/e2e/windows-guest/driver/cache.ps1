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
#                  a missing or mismatching one is downloaded as before. The
#                  Python archive has no upstream sha256 here, so harvest writes
#                  <archive>.sha256 next to it and inst.ps1 hashes the archive
#                  against that before the first uv call (a mismatch deletes it).
#                  uv also verifies an archive against the sha256 in its own
#                  download metadata (astral-sh/uv, crates/uv-python-managed/
#                  src/downloads.rs, extract_reader; read 2026-10-17). conexus
#                  is always installed from PyPI by uv, never --offline.
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
#                  from the guest (the archive only when the install step reported
#                  ok), uv's zip and the Git installer are downloaded
#                  by the host, each checked against the sha256 the guest
#                  recorded from the upstream; a version other than the new one
#                  is removed from the cache.
# Host writes stay under C:\build\guest\cache; files there are replaced or
# removed by exact name only (a temp file of a run that died, a day old, too).
# In the guest: the account's uv cache folder, nx-gate\claude-cache, and one
# archive file in nx-gate, removed after use.
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
# keys.txt is read once at the top ($have) and written once at the end of a harvest, minutes apart, and two guests
# that harvest at once share it (nexus-ndsei). Save-Keys takes an exclusive lock (keys.txt.lock, opened with
# FileShare.None, 500 ms retries for up to 60 s, released in finally) and, under it, re-reads keys.txt and merges:
# every kind this run added or replaced ($mine against $before) takes this run's line, every other kind keeps the
# line another harvest wrote in the meantime. keys.txt.lock stays in the folder; it holds nothing.
function Save-Keys($mine, $before) {
    $keyRx = '^(conexus|claude|models|uv|portablegit|winget-git|python)=[A-Za-z0-9._+-]+$'
    $kindOf = { param($l) $l.Substring(0, $l.IndexOf('=')) }
    $lock = $null
    for ($i = 0; ; $i++) {
        try { $lock = New-Object System.IO.FileStream("$keys.lock", [System.IO.FileMode]::OpenOrCreate, [System.IO.FileAccess]::ReadWrite, [System.IO.FileShare]::None); break }
        catch {
            # New-Object wraps the constructor's IOException; retry only when one is in the chain
            $e = $_.Exception; while ($e -and -not ($e -is [System.IO.IOException])) { $e = $e.InnerException }
            if (-not $e) { throw }
            if ($i -ge 120) { throw "keys.txt stayed locked for 60 s ($keys.lock); the keys of this harvest are not recorded" }
            Start-Sleep -Milliseconds 500
        }
    }
    try {
        $added = @($mine | Where-Object { $before -notcontains $_ })
        $dropped = @($before | Where-Object { $mine -notcontains $_ })
        $touched = @(@($added) + @($dropped) | ForEach-Object { & $kindOf $_ } | Sort-Object -Unique)
        $fresh = @(Get-Content $keys -ErrorAction SilentlyContinue | Where-Object { $_ -match $keyRx })
        $merged = @($fresh | Where-Object { $touched -notcontains (& $kindOf $_) }) + $added
        # Written whole beside keys.txt (an empty merge still writes the file), then swapped in with File.Replace,
        # so a seed reading keys.txt without the lock sees the old file or the new one, never a torn write. The
        # .NET calls throw on failure, so a failed write leaves keys.txt as it was and fails the harvest loudly.
        [System.IO.File]::WriteAllLines("$keys.tmp", [string[]]@($merged | Sort-Object -Unique), (New-Object System.Text.UTF8Encoding $true))
        # Windows PowerShell 5.1 (.NET Framework) throws "path is not of a legal form" for a $null backup name, so the swap
        # names one and removes it afterwards (2026-10-22: the warm run's harvest wrote its payloads but never keys.txt).
        # A seed's unlocked read can hold keys.txt open without delete-share for a moment; retry the swap briefly.
        for ($j = 0; ; $j++) {
            try {
                if (Test-Path -LiteralPath $keys) { [System.IO.File]::Replace("$keys.tmp", $keys, "$keys.bak") } else { [System.IO.File]::Move("$keys.tmp", $keys) }
                break
            } catch { if ($j -ge 20) { throw }; Start-Sleep -Milliseconds 250 }
        }
        # Outside the retry: a swap that landed must not be retried because the backup is held (an indexer or a scanner);
        # a leftover .bak is overwritten by the next swap.
        try { [System.IO.File]::Delete("$keys.bak") } catch { }
    } finally { $lock.Dispose() }
}
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
# A temp name per call: two guests that harvest at once share this folder.
function NewTmp { Join-Path $root ('payload-' + [guid]::NewGuid().ToString('N') + '.new') }
function Sha($p) { (Get-FileHash -Algorithm SHA256 -LiteralPath $p).Hash.ToLowerInvariant() }
# True when keys.txt names this payload, its file is there, and (when a sha256 is given) the file has it.
function HavePay($kind, $v, $sha) {
    $f = HostPay $kind $v
    ($have -contains "$kind=$v") -and (Test-Path -LiteralPath $f) -and (-not $sha -or (Sha $f) -eq $sha)
}
# The Python archive counts as held only with its sidecar: without one inst.ps1 would delete it.
function HavePy($name) { (HavePay 'python' $name '') -and (Test-Path -LiteralPath ((HostPay 'python' $name) + '.sha256')) }
# Move a checked temp file into place as the one cached payload of its kind (with its sha256 sidecar when one is
# given: the Python archive's), and remove the other versions of that kind by exact name (uv's version folder is removed only when it is empty).
function PlacePay($kind, $v, $tmp, $sidecarSha) {
    $dst = HostPay $kind $v
    New-Item -ItemType Directory -Force (Split-Path $dst) | Out-Null
    Move-Item -LiteralPath $tmp -Destination $dst -Force
    if ($sidecarSha) { Set-Content -LiteralPath (Join-Path $payloads ((PayRel $kind $v) + '.sha256')) -Value $sidecarSha -Encoding ASCII }
    foreach ($o in @($script:keep | Where-Object { $_ -like "$kind=*" } | ForEach-Object { $_.Substring($kind.Length + 1) })) {
        if ($o -eq $v) { continue }
        Remove-Item -LiteralPath (HostPay $kind $o) -Force -ErrorAction SilentlyContinue
        Remove-Item -LiteralPath ((HostPay $kind $o) + '.sha256') -Force -ErrorAction SilentlyContinue
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
            if ($kind -eq 'python' -and -not (HavePy $val)) { "seeded payload ${k}: no sha256 sidecar on the host; not seeded"; continue }
            ToGuest (HostPay $kind $val) "$guestPay\$rel"
            if ($kind -eq 'python') { ToGuest ((HostPay $kind $val) + '.sha256') "$guestPay\$rel.sha256" }
            $nPay++
        }
        "seeded payloads: $nPay"
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
            # A copy that fails part way must not be moved into place and recorded as harvested (nexus-okbiz).
            $tmp = $null
            try {
                if ("$n" -notlike '*pack exit=0') { throw "the guest could not pack its uv cache ($n)" }
                $tmp = NewTmp
                Copy-Item -FromSession $s -Path $guestTar -Destination $tmp -Force -ErrorAction Stop
                Invoke-Command -Session $s -ArgumentList $guestTar { param($t) Remove-Item -LiteralPath $t -Force }
                Move-Item -LiteralPath $tmp -Destination $uvTar -Force -ErrorAction Stop
                # One uv archive: it now holds what this install used, so it stands for this key alone.
                $keep = @($keep | Where-Object { $_ -notlike 'conexus=*' }) + "conexus=$Key"
                'harvested uv cache: {0:N0} MB' -f ((Get-Item $uvTar).Length / 1MB)
            } catch { if ($tmp) { Remove-Item -LiteralPath $tmp -Force -ErrorAction SilentlyContinue }; "harvest uv cache: $($_.Exception.Message)" }
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
                $tmp = $null
                try {
                    $tmp = NewTmp
                    Copy-Item -FromSession $s -Path $guestMt -Destination $tmp -Force -ErrorAction Stop
                    Invoke-Command -Session $s -ArgumentList $guestMt { param($t) Remove-Item -LiteralPath $t -Force }
                    Move-Item -LiteralPath $tmp -Destination $modelsTar -Force -ErrorAction Stop
                    $keep = @($keep | Where-Object { $_ -notlike 'models=*' }) + "models=$Key"
                    'harvested models: {0:N0} MB' -f ((Get-Item $modelsTar).Length / 1MB)
                } catch { if ($tmp) { Remove-Item -LiteralPath $tmp -Force -ErrorAction SilentlyContinue }; "harvest models: $($_.Exception.Message)" }
            }
        } else { "harvest: the models archive already holds models=$Key" }
        # Install payloads (nexus-bpzt7). Each is filled on its own and a failure only skips that one.
        # A temp file left by a harvest that died is removed by exact name once it is a day old (a younger one may be
        # another guest's harvest in flight).
        foreach ($old in @(Get-ChildItem -LiteralPath $root -File -Filter 'payload-*.new' -ErrorAction SilentlyContinue | Where-Object { $_.LastWriteTime -lt (Get-Date).AddDays(-1) })) {
            Remove-Item -LiteralPath $old.FullName -Force -ErrorAction SilentlyContinue
        }
        $gp = Invoke-Command -Session $s -ArgumentList $guestPay {
            param($d)
            @{
                record = @(Get-Content "$d\record.txt" -ErrorAction SilentlyContinue)
                python = @(Get-ChildItem "$d\python" -File -ErrorAction SilentlyContinue | Where-Object { $_.Name -notlike '*.sha256' } | Sort-Object LastWriteTime -Descending | ForEach-Object Name)
                ok = [bool](Test-Path -LiteralPath "$d\install-ok.txt")
            }
        }
        $ProgressPreference = 'SilentlyContinue'
        # the newest archive in the guest's folder that the host does not hold yet (uv put it there), and only when
        # inst.ps1 reported ok: a failed install can leave a partial archive
        $pyName = @($gp.python | Where-Object { $_ -match '^[A-Za-z0-9._+-]+$' -and -not (HavePy $_) } | Select-Object -First 1)
        $tmp = $null
        if ($pyName.Count -eq 1 -and -not $gp.ok) {
            "harvest: not taking python archive $($pyName[0]); the install step did not report ok"
        } elseif ($pyName.Count -eq 1) {
            try {
                $gsrc = "$guestPay\python\$($pyName[0])"
                $gsha = Invoke-Command -Session $s -ArgumentList $gsrc { param($f) (Get-FileHash -Algorithm SHA256 -LiteralPath $f).Hash.ToLowerInvariant() }
                $tmp = NewTmp
                Copy-Item -FromSession $s -Path $gsrc -Destination $tmp -Force -ErrorAction Stop
                if ((Sha $tmp) -ne $gsha) { throw "the copied python archive $($pyName[0]) is not the sha256 the guest computed" }
                PlacePay 'python' $pyName[0] $tmp $gsha
                "harvested python archive $($pyName[0]) (sha256 $gsha)"
            } catch { if ($tmp) { Remove-Item -LiteralPath $tmp -Force -ErrorAction SilentlyContinue }; "harvest python: $($_.Exception.Message)" }
        } else { 'harvest: no new python archive in the guest' }
        foreach ($line in @($gp.record | Sort-Object -Unique)) {
            $rec = "$line"
            $tmp = $null
            try {
                if ($rec -match '^portablegit=([A-Za-z0-9._+-]+)\|([0-9a-f]{64})$') {
                    $ver = $Matches[1]; $sha = $Matches[2]
                    if (HavePay 'portablegit' $ver $sha) { "harvest: PortableGit $ver already cached"; continue }
                    $tmp = NewTmp
                    Copy-Item -FromSession $s -Path "$guestPay\$(PayRel 'portablegit' $ver)" -Destination $tmp -Force -ErrorAction Stop
                    if ((Sha $tmp) -ne $sha) { throw "the copied PortableGit $ver is not the sha256 the guest recorded" }
                    PlacePay 'portablegit' $ver $tmp
                    "harvested PortableGit $ver"
                } elseif ($rec -match '^uv=([0-9]+\.[0-9]+\.[0-9]+)\|([0-9a-f]{64})$') {
                    $ver = $Matches[1]; $sha = $Matches[2]
                    if (HavePay 'uv' $ver $sha) { "harvest: uv $ver already cached"; continue }
                    $tmp = NewTmp
                    Invoke-WebRequest -UseBasicParsing -Uri "https://github.com/astral-sh/uv/releases/download/$ver/uv-x86_64-pc-windows-msvc.zip" -OutFile $tmp -ErrorAction Stop
                    if ((Sha $tmp) -ne $sha) { throw "the downloaded uv $ver zip is not the published sha256" }
                    PlacePay 'uv' $ver $tmp
                    "harvested uv $ver"
                } elseif ($rec -match '^winget-git=([A-Za-z0-9._+-]+)\|(https://github\.com/git-for-windows/git/releases/download/(?!.*\.\.)[A-Za-z0-9._/+-]+)\|([0-9a-f]{64})$') {
                    $ver = $Matches[1]; $url = $Matches[2]; $sha = $Matches[3]
                    if (HavePay 'winget-git' $ver $sha) { "harvest: Git installer $ver already cached"; continue }
                    $tmp = NewTmp
                    Invoke-WebRequest -UseBasicParsing -Uri $url -OutFile $tmp -ErrorAction Stop
                    if ((Sha $tmp) -ne $sha) { throw "the downloaded Git installer $ver is not the sha256 winget names" }
                    PlacePay 'winget-git' $ver $tmp
                    "harvested Git installer $ver"
                }
            } catch { if ($tmp) { Remove-Item -LiteralPath $tmp -Force -ErrorAction SilentlyContinue }; "harvest payload ($rec): $($_.Exception.Message)" }
        }
        Save-Keys $keep $have
    }
} finally { Remove-PSSession $s }
"cache $Mode took $([int]((Get-Date)-$t0).TotalSeconds)s"
'NXG-RESULT: ok'
