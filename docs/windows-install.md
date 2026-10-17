# Installing nexus on Windows

Native Windows 11 on x64 runs the full local stack: the `nx` client, the
nexus engine, PostgreSQL with pgvector, and the conexus plugin in Claude Code.
Windows 11 x64 is supported, within the limits in
[What is not supported or not measured](#what-is-not-supported-or-not-measured).
The steps below are the
ones the RDR-224 Phase 5 gate ran on a clean Windows 11 machine with published
conexus 7.72.1 (T2 `nexus_rdr/224-gate-run-7.72.1-2026-10-07`); where a step
was not measured, it says so.

## Before you start

- **Windows 11 on x64.** Windows on ARM is not supported: there is no engine
  build for it.
- **No administrator rights are needed.** Every step installs per user. The
  gate passed as a member of Administrators without elevation (a filtered
  token) and as a standard account in Users only, on a machine with no Git
  (conexus 7.78.0, 2026-10-13). Git is the one place to take care: the Git for
  Windows installer always asks for elevation, so step 2 uses PortableGit
  instead.
- **Application control blocks this install.** On a machine where an
  administrator enforces AppLocker's default rules, a standard account can
  complete none of the steps below: the uv and Claude Code installers, the
  PortableGit archive and every program installed under your profile are
  refused (measured 2026-10-13). A WDAC (App Control for Business) policy built
  from Windows' default example refuses the same programs (2026-10-15). An
  administrator has to allow them first; [Application control](#application-control)
  lists the rules that were enough for each.
- **No Visual C++ redistributable install is needed.** The engine and the
  PostgreSQL bundle carry the runtime DLLs beside their executables, and nx
  points its own Python modules (PDF extraction, local embedding) at those
  copies when the system has none (nexus-lqjll). A cloud-mode install, which
  never downloads the local engine, still needs the redistributable for PDF
  extraction.
- **The binaries are unsigned** (signing is deferred, nexus-dj01b). See
  [Signing and SmartScreen](#signing-and-smartscreen).

## Two routes

There are two routes, and both end in the same install. Install Git first (step 2 below), because Claude Code needs it to fetch the plugin and `nx doctor` checks for it. The route-one script does not need Git itself. If Claude Code does not take the plugin, the script prints the two `/plugin` lines for you to type later.

**Route one: run one command.** In a normal (not elevated) PowerShell window:

```powershell
irm https://hellblazer.github.io/nexus/install.ps1 | iex
```

It installs `uv` if you do not have it, installs the `nx` command, adds the plugin to Claude Code when `claude` is on your `PATH`, and asks you one question before it downloads anything. With Local it asks a second, whether to start at login. Typing the command is your permission to install the `nx` command, so it does not ask about that, and its question has no Not now choice. If you choose Local, it also asks whether the service should start when you log in, and the answer is No unless you type `y`. If you press Enter at the question, or the script has no terminal, it installs the `nx` command only. `/conexus:setup` in Claude Code, or `nx init` (Local) or `nx config init` (Cloud) in a terminal, sets up a service later.

The `iex` form cannot take flags. To skip the question, run the script as a script block and add a flag: `--local`, `--cloud` or `--cli-only`, and with `--local` also `--autostart` or `--no-autostart`:

```powershell
& ([scriptblock]::Create((irm https://hellblazer.github.io/nexus/install.ps1))) --local --no-autostart
```

**Route two: install `uv`, add the plugin, answer one question.** Install `uv` (step 1 below), then add the plugin in Claude Code:

```
/plugin marketplace add Hellblazer/nexus
/plugin install conexus@nexus-plugins
```

When the install finishes, run `/reload-plugins` or start a new session. In that session Claude tells you that Nexus is not installed and asks the question. If you want to answer later, type `/conexus:setup`. Setup runs in the background. When Claude says it is done, reconnect the `nexus` server (`/mcp`, the server, Reconnect) or start a new session. In Claude Desktop, add the plugin in Customize → Plugins and say "Set up Nexus" in a conversation (step 5 below). If you added `Hellblazer/nexus` before, remove it and add it again first. An older copy has no setup tool.

**The question.** Route two asks it with these four choices. The script asks the same question without Not now:

| Choice | What happens | Download |
|---|---|---|
| Local | Install the CLI and run a Nexus service on this machine | about 1.3 GB down, 2.4 to 3.1 GB on disk |
| Cloud | Install the CLI and connect it to a managed service you have an address, token and tenant for | about 0.7 GB down, 2.1 to 2.3 GB on disk |
| CLI only | Install the CLI and decide about a service later | about 0.7 GB down, 2.1 to 2.3 GB on disk |
| Not now | Nothing is installed | none |

The sizes on disk are the install itself, measured from nothing with conexus 7.81.0 on macOS, Linux and Windows; Windows is the high end. `uv` also keeps a download cache, about 2.4 GB on Linux, 3.6 GB on macOS and 4.8 GB on Windows after a first install. On Linux the cache and the install together measured 2.4 GB, not 4.4 GB, because they share files, so the cache is not all extra space. `uv cache clean` empties it but frees only the files the install does not share. The macOS and Windows cache figures were measured on 2026-10-16 with conexus 7.81.0.

## Install by hand

The numbered steps below are the install by hand. Use them when a route above fails, or when you want each step yourself.

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

The install by the one-command route measured about 2.3 GB of disk (conexus
7.78.0, CLI only), and about 3.1 GB with Local, which adds the service's
configuration and models. A folder-size tool reports more, because it counts
some files twice: uv installs packages as hardlinks to the copies in its cache
(`%LOCALAPPDATA%\uv\cache`, about 4.8 GB after a first install), so the
environment is mostly the same data under a second name. That sharing was not
measured with a hardlink-aware tool on Windows. The same kind of install on
Linux (CLI only, conexus 7.81.0) shows the effect: 2.4 GB in uv's cache and
2.0 GB in the install occupy 2.4 GB together.

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

## 5. Claude Desktop (chat and Cowork)

In Claude Desktop, open Customize → Plugins, add the marketplace
`Hellblazer/nexus`, and install `conexus`
([Desktop deployment](desktop-deployment.md)). If you added `Hellblazer/nexus` before, remove it and add it again first. An older copy has no setup tool. Chat and Cowork read the plugins
on your claude.ai account, not the ones step 4 installed, so step 4 does not
cover them. The plugin launches `nx-mcp` from the CLI install and talks to the
stack step 3 started.

With no CLI on the machine, open a conversation and say "Set up Nexus". Claude
shows the four choices from [Two routes](#two-routes) and asks which you want.
Desktop does not tell you when Nexus is missing, so you have to ask. Do not type
`/conexus:setup` in Desktop; it lists the plugin's skills, not its commands (seen
on Claude Desktop 2.42304.0 on macOS). To see progress, ask "Is Nexus set up yet?". When Claude
says setup is done, start a new conversation. Or do steps 1 to 3 first, by hand.

An existing marketplace entry stayed at the release it was added at, which is why an older copy has no setup tool.

What was measured (2026-10-11, conexus 7.77.1, Claude Desktop 1.24012.9.0, a
clean Windows 11 machine): with only step 4 done, the Code tab found the plugin
and chat and Cowork found none. After the marketplace was removed and added
again, Desktop installed conexus 7.77.1 and sn 7.77.1. Not yet confirmed: that
the `nexus` and `nexus-catalog` MCP servers connect in a chat or Cowork session
after that install (nexus-qkb9v).

The `conexus.mcpb` Desktop Extension is not a working route on Windows.
Double-clicking the file does nothing, because the Claude Desktop package
registers no `.mcpb` file type, and the Customize → Plugins upload dialog hides
`.mcpb` files. The extension's launch command itself works: run headless on the
clean machine exactly as Claude Desktop runs it, it built its environment,
served 44 tools, stored and searched, and on quit left no process of its own
while the stack kept running (nexus-ijue9.21).

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
if you added it (`/plugin uninstall conexus@nexus-plugins` inside Claude Code),
then close every Claude Code window: a running session keeps the Nexus servers
open, and they hold files the next two commands remove. Measured on 2026-10-14
with a session still open, `nx uninstall` could not remove `.config\nexus`
(`mcp.log` in use) and `uv tool uninstall conexus` failed with "Access is
denied"; with the session closed, both completed and nothing was left.

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
cache (about 4.8 GB after this install; `uv cache clean` empties it) and
PortableGit.

## Signing and SmartScreen

The Windows binaries are not Authenticode-signed. What that means in practice:

- Files `nx init` downloads carry no Mark of the Web, so SmartScreen does not
  screen them.
- A file you download with a browser (such as `conexus.mcpb`) does carry it. The
  extension is opened by Claude Desktop rather than run by Windows; what
  SmartScreen shows on that path has not been observed.
- Smart App Control in enforcing mode blocks unsigned code; see
  [Smart App Control](#smart-app-control).
- AppLocker with its default rules blocks the whole install for a standard
  account, signed or not, because everything installs under the user profile.
  A WDAC policy built from Windows' default example does the same.

### Application control

AppLocker needs Windows 11 Pro or above; Home has no AppLocker. With
AppLocker's default executable rules enforced, the install and the gate passed
for a standard account once an administrator added these path rules to the
executable collection (conexus 7.78.0, 2026-10-13 and 2026-10-14):

```text
%OSDRIVE%\Users\*\.local\bin\*
%OSDRIVE%\Users\*\.local\share\nexus\*
%OSDRIVE%\Users\*\AppData\Roaming\uv\*
%OSDRIVE%\Users\*\AppData\Local\uv\cache\*
%OSDRIVE%\Users\*\AppData\Local\Programs\Git\*
%OSDRIVE%\Users\*\.config\nexus\*
%OSDRIVE%\Users\*\.claude\downloads\*
%OSDRIVE%\Users\*\AppData\Local\Temp\PortableGit.exe
```

They cover uv and the `nx` programs, the Python that uv downloads, PortableGit,
the engine and PostgreSQL, and the two installers. `.local\share\nexus` is where
`nx self install` puts each version (step 3); without that rule `nx self
install` fails with "This program is blocked by group policy" on the new
version's `python.exe` (measured 2026-10-14). With all eight rules, on Windows
11 Pro as a standard user, `nx self install`, `nx upgrade`, the restart through
the logon task onto the new version, and the removal steps below all completed
with no AppLocker block. Three limits:

- These rules let any program in those folders run, and the user can write to
  those folders. Publisher rules cannot replace them today, because the engine
  and the PostgreSQL bundle are not signed.
- The script rules were in audit mode for that run. Enforced, they put
  PowerShell in ConstrainedLanguage mode, where the uv installer in step 1
  fails whatever the executable rules say. Installing uv another way on such a
  machine has not been measured.
- DLL rules need three more folders than the executable rules. With the DLL
  collection enforced, on Windows 11 Pro as a standard user (2026-10-14), the
  engine failed to load ONNX Runtime until these were allowed for DLLs, and
  then the gate passed: `%OSDRIVE%\Users\*\.cache\nexus\*` (the tokenizer
  library the engine extracts), `%OSDRIVE%\Users\*\AppData\Local\Temp\onnxruntime-java*`
  (ONNX Runtime, extracted to the temp folder at every engine start) and
  `%OSDRIVE%\ProgramData\Microsoft\Windows Defender\Platform\*` (Defender's
  own DLL, which it loads into every process). The first two are folders the
  user can write to.

#### WDAC (App Control for Business)

A WDAC policy built from Windows' `DefaultWindows_Enforced.xml` example lets
only code Windows itself signs run, for every account, administrators included.
With it enforced, the install and the gate passed for a standard account on
Windows 11 Enterprise (conexus 7.79.0, 2026-10-15) once the policy had file
path rules for these folders in that account's profile, and two rule options
set: 18, `Disabled:Runtime FilePath Rule Protection`, and 11,
`Disabled:Script Enforcement`.

```text
C:\Users\<account>\.local\bin\*
C:\Users\<account>\.local\share\nexus\*
C:\Users\<account>\AppData\Roaming\uv\*
C:\Users\<account>\AppData\Local\uv\cache\*
C:\Users\<account>\AppData\Local\Programs\Git\*
C:\Users\<account>\.config\nexus\*
C:\Users\<account>\.claude\downloads\*
C:\Users\<account>\AppData\Local\Temp\PortableGit.exe
C:\Users\<account>\.cache\nexus\*
C:\Users\<account>\AppData\Local\Temp\onnxruntime-java*
```

A run with the same policy in audit mode recorded files in these folders and
nowhere else: uv, the `nx` programs, Claude Code and its installer, the Python
that uv downloads, PortableGit's programs and DLLs, the engine, the PostgreSQL
bundle, the tokenizer library and ONNX Runtime. With the rules enforced, the
only file refused in the run was a test program outside them. What differs from
AppLocker:

- WDAC path rules take a wildcard only at the start or the end, so each rule
  names the account's profile instead of `Users\*`.
- Without option 18 WDAC refuses path rules for folders the user can write to,
  which all of these are.
- WDAC always covers DLLs, so the DLL folders from the AppLocker section are in
  the list. Defender's DLL is signed by Windows and needs no rule.
- PortableGit's programs are signed by the Git for Windows project, not by
  Windows, so they need the rule like unsigned ones, and a Git installed for all
  users under Program Files is refused too.
- Script enforcement was off. With it on, PowerShell runs a script the policy
  does not allow in ConstrainedLanguage mode, as AppLocker's script rules do;
  that has not been measured with WDAC.
- The gate does not run `nx self install`, `nx upgrade` or the removal steps,
  so those were not measured under WDAC. The `.local\share\nexus` rule comes
  from the AppLocker walk above.

### Smart App Control

Smart App Control on is not supported. In both measured installs with it on,
the local stack could not be set up, and the two failed at different points.
On 2026-10-14, on Windows 11 Home with it switched from evaluation to on, the
install in steps 1 to 3 completed, and `nx init` then failed with "An
Application Control policy has blocked this file" on the PostgreSQL bundle's
`pg_config.exe` and `initdb.exe`. The log also named DLLs of the Python that uv
downloads and of PortableGit, all for lacking a trusted signature. On
2026-10-15, on the same machine, Windows blocked `nx.exe` itself during the
install with the same message, so `nx` could not report the problem. In a
second run that day `nx.exe` ran, and `nx init` stopped with the refusal
described below. Why the runs differ is not known. In both, files that nexus does not ship were
blocked (uv's Python, the `nx.exe` launcher), so signing the engine and the
bundle would not have been enough on its own.

In releases after conexus 7.80.0, where `nx` does run, `nx init` refuses with
a message that links here when Smart App Control is on (measured on the Home
machine on 2026-10-15), and `nx doctor` has a
Smart App Control row (a failure when it is on in local mode, a warning in
evaluation). Where Windows has no Smart App Control (the setting does not
exist, as on Windows 10 and older Windows 11), there is no row.

A new Windows install starts in evaluation mode. On the same Home machine in
evaluation mode (state value 2), the install and the end-to-end test passed and
nothing they ran was blocked. Windows can later switch evaluation to on. What
that does to an installed nexus has not been measured; the expected result is
that the bundle, uv's Python or `nx.exe` are blocked at the next start. The
`nx doctor` warning can only speak before that switch.

## What is not supported or not measured

- Windows on ARM: not supported.
- Windows 10: not supported and not measured. Every measured machine ran
  Windows 11 x64.
- Smart App Control on: not supported; the local stack could not be set up in
  either measured install (above).
- Smart App Control switching from evaluation to on after the install: expected
  to block, not measured (nexus-f9bgu.58).
- Unsigned binaries: the engine and the PostgreSQL bundle are not code-signed
  (nexus-dj01b).
- AppLocker and WDAC: the install needs an administrator's path rules
  ([Application control](#application-control)).
- The `conexus.mcpb` Desktop Extension: not a working route on Windows; chat
  and Cowork get conexus from the plugin marketplace instead
  ([Claude Desktop](#5-claude-desktop-chat-and-cowork)).
- The first Claude Code session after the install can lose the plugin's
  SessionStart hooks: on 2026-10-15 the end-to-end test failed three times in a
  row because Claude Code stopped them at their 5 and 10 second time limits while
  `uv` prepared the hook environment for the first time. Later sessions were not
  affected. The cause is not known (nexus-3ya9d). If a first session shows no
  nexus context, start a new session.
- AppLocker with enforced script rules, WDAC with script enforcement on, a
  Microsoft or domain account, host sleep, and the Claude Desktop install
  dialog: not measured.
