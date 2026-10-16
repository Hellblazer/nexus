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
#   -Mode harvest  after a successful install: copy that account's uv cache and
#                  installed claude.exe back to the host when the cache does not
#                  yet hold this install (keys.txt: conexus=<-Key>, and
#                  claude=<version>).
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
$pw = [Console]::In.ReadLine()
if (-not $pw) { throw 'nothing on stdin' }
# The characters go into the SecureString one by one: ConvertTo-SecureString -AsPlainText would put the
# plain text in a cmdlet parameter binding, which PowerShell module logging records when it is on.
$sec = New-Object System.Security.SecureString; foreach ($c in $pw.ToCharArray()) { $sec.AppendChar($c) }; $sec.MakeReadOnly(); $pw = $null; $c = $null
$cred = New-Object PSCredential($GuestUser, $sec)
New-Item -ItemType Directory -Force $root | Out-Null
$have = @(Get-Content $keys -ErrorAction SilentlyContinue | Where-Object { $_ -match '^(conexus|claude)=[A-Za-z0-9._+-]+$' })
function ClaudeBin($v) { Join-Path $root "claude-$v-win32-x64.exe" }
# Host to guest over PowerShell Direct (Sam, 2026-10-15: no host-side VM settings, so no Copy-VMFile).
function ToGuest($src, $dst) { Copy-Item -ToSession $s -Path $src -Destination $dst -Force; "copied $(Split-Path $src -Leaf)" }
$t0 = Get-Date
$s = New-PSSession -VMName $VMName -Credential $cred -ErrorAction Stop
try {
    $prof = "C:\Users\$Account"
    if (-not (Invoke-Command -Session $s -ArgumentList $prof { param($p) Test-Path $p })) { 'NXG-RESULT: FAIL no profile for the account in the guest'; exit 3 }
    $guestTar = "$prof\nx-gate\uv-cache.tar"
    $guestCc = "$prof\nx-gate\claude-cache"
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
        # The installing account writes its own uv cache (locks, new entries): make it the owner of what was copied in.
        Invoke-Command -Session $s -ArgumentList $Account, "$prof\AppData\Local\uv", $guestCc {
            param($a, $u, $c)
            foreach ($d in @($u, $c)) { if (Test-Path $d) { icacls $d /setowner $a /T /C /Q | Out-Null; icacls $d /grant "${a}:(OI)(CI)F" /T /C /Q | Out-Null } }
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
        $keep | Sort-Object -Unique | Set-Content -Encoding UTF8 $keys
    }
} finally { Remove-PSSession $s }
"cache $Mode took $([int]((Get-Date)-$t0).TotalSeconds)s"
'NXG-RESULT: ok'
