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
uv tool install --python 3.13 conexus
```

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
`health: ok` and `pg: up`.

Downloads, measured on the clean machine with conexus 7.72.1: about 0.6 GB for
`uv tool install` (packages and a Python), and about 0.75 GB for `nx init`
(engine, PostgreSQL, embedding model and reranker). Counting uv's cache, the
whole install takes about 4 GB of disk.

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
  new version needs. The first `nx self install` on an install made with
  `uv tool install` converts it to the side-by-side layout first (its
  `--dry-run` on Windows shows that step; the conversion itself has not been run
  there). Do not use `uv tool install --force conexus` while the stack runs: it
  replaces files in use.
- Remove the logon task with `nx daemon service uninstall --autostart`. That
  leaves the running stack up; stop it as above. Put the task back with
  `nx daemon service install --autostart`.

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
