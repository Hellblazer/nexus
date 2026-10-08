# Installing nexus on Windows

Native Windows 11 on x64 runs the full local stack: the `nx` client, the
nexus engine, PostgreSQL with pgvector, and the conexus plugin in Claude Code.
Windows is **tested but not yet declared supported**. The steps below are the
ones the RDR-224 Phase 5 gate ran on a clean Windows 11 machine with published
conexus 7.72.1 (T2 `nexus_rdr/224-gate-run-7.72.1-2026-10-07`); where a step
was not measured, it says so.

## Before you start

- **Windows 11 on x64.** Windows on ARM is not supported: there is no engine
  build for it.
- **No administrator rights are needed.** Every step installs per user. The
  gate ran as a member of Administrators without elevation (a filtered token);
  a standard, non-administrator account has not been measured. Git is the one
  place to take care: the Git for Windows installer always asks for elevation,
  so step 2 uses PortableGit instead.
- **No Visual C++ redistributable install is needed.** The engine and the
  PostgreSQL bundle carry the runtime DLLs beside their executables, and nx
  points its own Python modules (PDF extraction, local embedding) at those
  copies when the system has none (nexus-lqjll). A cloud-mode install, which
  never downloads the local engine, still needs the redistributable for PDF
  extraction.
- **The binaries are unsigned** (signing is deferred, nexus-dj01b). See
  [Signing and SmartScreen](#signing-and-smartscreen).

## 1. Install uv

In a normal (not elevated) PowerShell window:

```powershell
powershell -ExecutionPolicy ByPass -c "irm https://astral.sh/uv/install.ps1 | iex"
```

`winget install --id astral-sh.uv --scope user` works too; it is the hint the
plugin's preflight prints when `nx` is missing. The first time you use winget
it asks you to accept its source agreements (answer `Y`). Open a new PowerShell
window afterwards so `uv` is on your PATH.

**If `uv tool install` fails with "untrusted mount point (os error 448)",** a
stale `python3.exe` trampoline in `%USERPROFILE%\.local\bin` is shadowing uv's
managed Python (measured during RDR-218). Add
`--python-preference only-managed` to the install command below.

## 2. Install Git

`nx doctor` requires git, the plugin marketplace is fetched with git, and Claude
Code on Windows runs its Bash tool in Git Bash. The Git for Windows installer
asks for elevation (a UAC prompt) however it is started: through winget, with
`--scope user`, and with its `/CURRENTUSER` switch alike. PortableGit, the same
Git with Git Bash in a self-extracting archive, needs no administrator rights.
This downloads the latest release (about 60 MB, about 400 MB on disk), checks
its SHA-256 against the one GitHub publishes, extracts it to
`%LOCALAPPDATA%\Programs\Git`, and puts its `cmd` folder on your user PATH:

```powershell
$ProgressPreference = 'SilentlyContinue'
$rel = Invoke-RestMethod https://api.github.com/repos/git-for-windows/git/releases/latest
$asset = $rel.assets | Where-Object name -Match '^PortableGit-.*-64-bit\.7z\.exe$'
Invoke-WebRequest $asset.browser_download_url -OutFile "$env:TEMP\PortableGit.exe" -UseBasicParsing
if ((Get-FileHash "$env:TEMP\PortableGit.exe").Hash -ne ($asset.digest -replace '^sha256:')) { throw 'PortableGit checksum mismatch' }
Start-Process "$env:TEMP\PortableGit.exe" -ArgumentList "-o`"$env:LOCALAPPDATA\Programs\Git`"", '-y' -Wait
$userPath = [Environment]::GetEnvironmentVariable('Path', 'User')
[Environment]::SetEnvironmentVariable('Path', "$userPath;$env:LOCALAPPDATA\Programs\Git\cmd", 'User')
```

Measured from a non-elevated window on a clean Windows 11 machine with no Git
(2026-10-07, PortableGit 2.56.0.2): the block above, pasted as is, took 30 to
44 s across two runs with no UAC prompt. In a new window `git`
resolved to `%LOCALAPPDATA%\Programs\Git\cmd\git.exe`, `nx doctor`'s git row
was green, Claude Code's Bash tool ran in that Git Bash with no extra setting,
and `claude plugin marketplace update` fetched over HTTPS. If Claude Code ever
reports that it cannot find Git Bash, set `CLAUDE_CODE_GIT_BASH_PATH` to
`%LOCALAPPDATA%\Programs\Git\bin\bash.exe`.

If you have administrator rights and prefer the installer,
`winget install --id Git.Git -e --source winget` works and puts Git on the
machine PATH; approve the UAC prompt when it appears.

## 3. Install conexus and start the local stack

```powershell
uv tool install conexus --python 3.12
nx self install
```

uv downloads Python 3.12 for you. Measured on the clean machine on 2026-10-07
with conexus 7.72.1: the install took about two minutes (CPython 3.12.15, 218
packages), and `nx init`, `nx doctor` and `nx daemon service status` below all
came out as described. The earlier walks used `--python 3.13` and also worked;
3.12 is the interpreter the install page names, so the two agree.

`nx self install` moves the install to the layout Nexus manages: each version
in its own folder under `%USERPROFILE%\.local\share\nexus\tools`, a `current`
junction naming the one in use, and `tools\current\bin` added to the front of
your user PATH (it prints a line saying so). Upgrades then build the new version
next to the running one and never replace a file a running process uses. The
uv copy it replaces stays until nothing runs from it; the next `nx self install`,
`nx self gc`, or the start of a Claude Code session with the plugin removes it,
together with uv's `nx.exe` launchers in `.local\bin`. Until then `nx doctor`
shows one ⚠ row, "Orphan uv install", which is expected.

Measured on the clean machine on 2026-10-07 with a test build of the branch
that carries the Windows layout (not yet a release): `nx self install` took
35 s and downloaded nothing new, the new window resolved `nx` to
`tools\current\bin\nx.exe`, `nx init` registered the logon task against the new
layout, `nx doctor` ended "All checks passed" (with the ⚠ row above), store and
search worked, and `nx self gc` then removed the uv copy and its launchers. The
installed version has no `av` and no `opencv-python` package, which a plain
`uv tool install` still pulls in.

`nx self install` ends with a line about `nx upgrade` that applies only when it
upgraded an existing install. On the fresh install in this page you do not run
`nx upgrade`: `nx init` below normally leaves the data directory already migrated (its
last step walks the same migration ladder), and `nx doctor` shows no pending
rung afterwards. If `nx init` instead prints "Upgrade-ladder convergence
deferred", run `nx upgrade` once. Run `nx upgrade` after a later `nx self install` that
brought a newer version.

Open a new window, then:

```powershell
nx init --yes
```

`nx init` downloads the pinned engine and PostgreSQL bundle (signature-checked),
creates the local database, fetches the bge-768 embedding model and the
reranker, and registers a Task Scheduler logon task, `NexusStorageService`, that
starts the stack when you sign in. Without `--yes` it asks before registering
the task. Check the result with:

```powershell
nx doctor
nx daemon service status
```

`nx doctor` should end with "All checks passed", and `status` should show
`health: ok` and `pg: up`. A ⚠ row is a soft warning and does not fail the run.
Right after `nx self install` the one to expect is "Orphan uv install", which
clears once the uv copy is reaped (see above); any other ⚠ or ✗ row is worth
reading.

Downloads for conexus 7.72.1: about 0.67 GB for `uv tool install` (653 MB of
packages and a 21 MB Python), and about 0.58 GB for `nx init` (engine 42 MB,
PostgreSQL 7 MB, embedding model 436 MB, reranker 91 MB), so about 1.25 GB in
all. These are the published file sizes of what each step fetches. On disk,
`nx init` unpacks to about 0.75 GB.

The whole install takes about 2.5 GB of disk. A folder-size tool reports about
4 GB, because it counts some files twice: on Windows uv installs packages as
hardlinks to the copies in its cache (`%LOCALAPPDATA%\uv\cache`, about 1.7 GB),
so the tool environment under `%APPDATA%\uv` (about 1.6 GB) is mostly the same
data under a second name. The 2.5 GB is the cache, plus the Python uv
downloaded, plus the 0.75 GB from `nx init`. It was computed from the walk's
folder sizes and uv's documented Windows link mode, not measured with a
hardlink-aware tool on Windows. The same install on Linux, measured
hardlink-aware, shows the effect: 2.2 GB in uv's cache and 2.1 GB in the tool
environment occupy 2.25 GB together.

## 4. Claude Code and the plugin

```powershell
irm https://claude.ai/install.ps1 | iex
```

Open a new window and add the plugin from the command line:

```powershell
claude plugin marketplace add Hellblazer/nexus
claude plugin install conexus@nexus-plugins
```

Then start `claude`. The first launch asks you to sign in with your Claude
account; the gate ran with a pre-provisioned token, so that sign-in has not been
walked on Windows. The conexus hooks and MCP tools run against the local stack.
This path is the one the Phase 5 gate verified end to end: MCP store and search
from a live session, hooks firing, and `claude -p`.

You can add the plugin from inside a session instead, then restart Claude Code:

```
/plugin marketplace add Hellblazer/nexus
/plugin install conexus@nexus-plugins
```

## 5. Claude Desktop (the Desktop Extension)

Download `conexus.mcpb` from the
[latest GitHub release](https://github.com/Hellblazer/nexus/releases/latest) and
double-click it; Claude Desktop registers it under Settings → Connectors →
Desktop as "Conexus" ([Desktop deployment](desktop-deployment.md)). The extension talks to the stack step 3 started; it does not start
the service itself, so do step 3 first.

What was measured: the extension's launch command, run headless on the clean
machine exactly as Claude Desktop runs it, built its environment, served 44
tools, stored and searched, and on quit left no process of its own while the
stack kept running (nexus-ijue9.21). The Claude Desktop install dialog itself
has not been walked on Windows.

## Stopping, starting and upgrading

- `nx daemon service stop` stops the engine and leaves PostgreSQL running.
- `nx daemon service stop --with-pg` stops PostgreSQL too.
- `nx daemon service start` starts the stack again (PostgreSQL included) without
  signing out.
- Signing out, or restarting Windows, stops the stack cleanly, and the logon task
  starts it at the next sign-in (measured, nexus-f9bgu.46 and .51). Sleep is
  unmeasured.
- Upgrade with `nx self install`, then `nx upgrade`. `nx self install` builds the
  new version beside the running one and never replaces files a running process
  uses; it upgrades the program only. `nx upgrade` then runs the migrations the
  new version needs. It also points the `NexusStorageService` task at the new
  version and says so. The storage service that is running keeps its version
  until its next start: the next sign-in, or `nx daemon service stop` followed
  by `Start-ScheduledTask NexusStorageService`. The old version stays on disk
  until nothing runs from it, and a later `nx self install` or `nx self gc`
  removes it. Measured on the clean machine (test build, 2026-10-07): with the
  stack running, `nx self install` took 14 s, the service kept its process and
  stayed healthy, and after a stop and `Start-ScheduledTask` it ran the new
  version within 11 s. If you skipped `nx self install` in step 3, the first
  `nx self install` converts the uv install the same way (about 20 s, measured
  with the stack stopped), and the stack moves off the uv copy at its next
  start. Do not
  use `uv tool install --force conexus` while the stack runs: it replaces files
  in use.
- Remove the logon task with `nx daemon service uninstall --autostart`. That
  leaves the running stack up; stop it as above. Put the task back with
  `nx daemon service install --autostart`.

## Removing Nexus

Run these in a normal PowerShell window, in this order. Remove the plugin first
if you added it (`/plugin uninstall conexus@nexus-plugins` inside Claude Code).

```powershell
nx uninstall --yes --remove-data
uv tool uninstall conexus
Remove-Item -Recurse -Force "$HOME\.local\share\nexus" -ErrorAction SilentlyContinue
Remove-Item -Force "$HOME\.local\bin\nx.exe", "$HOME\.local\bin\nx-mcp.exe", "$HOME\.local\bin\nx-mcp-catalog.exe", "$HOME\.local\bin\nx-session-end-launcher.exe", "$HOME\.local\bin\nx-hook.exe" -ErrorAction SilentlyContinue
```

`nx uninstall --yes --remove-data` stops the stack (PostgreSQL included) and
the background workers nexus started (the aspect worker that `nx store put` and
the plugin start, a topic labeling run after `nx index`, and MinerU), removes
the `NexusStorageService` task and `%LOCALAPPDATA%\nexus`, takes
`tools\current\bin`, which `nx self install` added, off your user PATH, and
deletes `%USERPROFILE%\.config\nexus`, which holds the database, notes, plans
and catalog, and `%USERPROFILE%\.cache\nexus`, which holds the search models
(about 500 MB; in releases after 7.74.0 the engine's tokenizer library lives under
it, in `djl`). In those releases, with
`--remove-data` it also removes the `onnxruntime-java<n>` folder in the temp
directory (the one `TMP` names, else `TEMP`; normally `%TEMP%`) that the
engine's last run left: the engine deletes older ones at its next start, so
the last one always outlives a stop. Only a folder the engine's own cleanup
would delete goes, and only one named `onnxruntime-java` plus digits, so a
folder of yours such as `onnxruntime-java-backup` stays; it goes only after the
stack has stopped. The PATH edit keeps
every other entry as written, `%VAR%` entries included, and keeps the value's
registry type. Windows that are already
open keep their old PATH. `nx uninstall` without flags only shows what it would
do. `nx store export --all -o <dir>` keeps a copy of the knowledge store first
if you want one.

The `Remove-Item` lines remove the program itself, which `nx` cannot delete
while it runs. What `uv tool uninstall conexus` prints depends on whether the uv
copy that `nx self install` left behind has been reaped yet. It is reaped by a
later `nx self install`, by `nx self gc`, or at the start of a Claude Code
session with the plugin. If none of those has run since step 3, the uv copy is
still there and the command removes it, printing "Uninstalled 5 executables"
(measured on the 7.74.0 candidate walk). If one has, the copy is gone and the
command prints "`conexus` is not installed". Both are fine; either way the
`nx` you were running comes from `tools\current\bin`, which the
`Remove-Item -Recurse` line on `.local\share\nexus` removes. On an install made
only with `uv tool install`, `uv tool uninstall conexus` already removes the five
`.exe` files and `.local\share\nexus` never exists; the `Remove-Item` lines skip
missing files quietly.

Releases up to 7.73.0 do less. They leave the search models, the aspect worker
(so `nx uninstall` warns "could not remove data dir ... being used by another
process"), `%LOCALAPPDATA%\nexus` and the PATH entry. On those, run this after
the lines above:

```powershell
Get-CimInstance Win32_Process -Filter "Name='python.exe'" | Where-Object { $_.CommandLine -like '*aspect-worker*' } | ForEach-Object { Stop-Process -Id $_.ProcessId -Force }
Remove-Item -Recurse -Force "$HOME\.config\nexus", "$HOME\.cache\nexus", "$HOME\.local\share\nexus", "$env:LOCALAPPDATA\nexus" -ErrorAction SilentlyContinue
$k = Get-Item 'HKCU:\Environment'
$kind = $k.GetValueKind('Path')
$path = ($k.GetValue('Path', '', 'DoNotExpandEnvironmentNames') -split ';' | Where-Object { $_ -and $_ -notlike '*\.local\share\nexus\tools\current\bin' }) -join ';'
Set-ItemProperty 'HKCU:\Environment' -Name Path -Value $path -Type $kind
```

Those releases, and 7.74.0, also leave two folders the lines above do not name.
`%TEMP%\onnxruntime-java<n>` (about 11 MB) holds the engine's last run's ONNX
Runtime libraries; delete the folders with that name once the stack is stopped.
`%USERPROFILE%\.djl.ai\tokenizers` (about 13 MB) is the engine's tokenizer
library; releases after 7.74.0 keep it under `.cache\nexus` instead, but do not touch
a `.djl.ai` an earlier engine already made. `nx uninstall --remove-data` reports that folder
and never deletes it, because `.djl.ai` is the default cache of every program built on DJL,
a Java deep-learning library, and nexus cannot tell whether another one uses it.
Delete it yourself if nothing else on the machine does.

Measured on the clean machine (test build, 2026-10-07, non-elevated window) on
an install converted by `nx self install`, with the stack running and an aspect
worker started by `nx store put`: `nx uninstall --yes --remove-data` took 9 s
and left no `nx`, PostgreSQL, `nexus-service`, `pythonw` or aspect-worker
process, no task, nothing under `.config\nexus`, `.cache\nexus` or
`%LOCALAPPDATA%\nexus`, and no `tools\current\bin` on the user PATH; a
`%USERPROFILE%` entry and the value's `REG_SZ` type were kept. The `Remove-Item`
lines then took 16 s and left nothing under `.local\share\nexus` and no `nx`
launcher in `.local\bin`, and a new window found no `nx`. With conexus 7.72.1
on a uv-only install, `nx uninstall --yes --remove-data` took 6 s and the same
lines left nothing once the models and `%LOCALAPPDATA%\nexus` were removed as
above.
What stays belongs to uv and to you: uv, the Python 3.12 it downloaded, its
cache (about 2.5 GB after this install; `uv cache clean` empties it) and
PortableGit.

## Signing and SmartScreen

The Windows binaries are not Authenticode-signed. What that means in practice:

- Files `nx init` downloads carry no Mark of the Web, so SmartScreen does not
  screen them.
- A file you download with a browser (such as `conexus.mcpb`) does carry it. The
  extension is opened by Claude Desktop rather than run by Windows; what
  SmartScreen shows on that path has not been observed.
- Smart App Control in enforcing mode blocks unsigned code, and that
  configuration has not been tested. An enterprise application-control policy
  (WDAC, AppLocker) may also block the engine.

## What is not supported or not measured

- Windows on ARM: not supported.
- A standard (non-administrator) account, Smart App Control in enforcing mode,
  host sleep, and the Claude Desktop install dialog: not measured.
